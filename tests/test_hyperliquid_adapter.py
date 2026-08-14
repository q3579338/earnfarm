"""Hyperliquid 适配器测试。全部走 httpx.MockTransport，一次都不连交易所。

金标准取自 docs/research/exchange-hyperliquid.md：

  - **签名算法**（该文"L1 action 签名算法"节）：
    ``msgpack(action) ‖ nonce(8B 大端) ‖ vault 段 ‖ expiresAfter 段`` → keccak →
    EIP-712（domain 名 "Exchange" / version "1" / chainId **1337** / verifyingContract
    全零；primaryType "Agent"，字段顺序 source 在前 connectionId 在后；
    source 主网 "a"、测试网 "b"）。
  - **响应样例**全部照抄该文"REST 端点"节（meta / metaAndAssetCtxs / fundingHistory /
    l2Book / userFees / clearinghouseState / frontendOpenOrders / order / cancel /
    orderStatus / userFillsByTime）。
  - **错误串**照抄该文"错误码"节的表格（逐单错误、整批预校验错误、HTTP 层）。

Hyperliquid **签名不覆盖 HTTP body 字节**（和 Gate 的"签什么字节就发什么字节"正好相反），
所以这里最强的校验不是比对某个 SIGN header，而是调研文档"坑"节第 15 条点名的那一条：
**从真正上线的 JSON body 里解析出 action，重新 msgpack 一遍、重算 keccak、恢复 signer，
必须等于 agent 地址**。这一条同时证明了 httpx 实际发出的键顺序就是签名时的 msgpack
键顺序——msgpack 按 dict 插入顺序编码，键顺序错就是签名错（官方点名的五个翻车点之二）。

另外两条贯穿全文的红线：
  - **一个返佣/邀请码都不带**（用户明确要求）：action 里没有 builder 键、没有渠道 header、
    cloid 不加前缀。
  - **缺 eth_account / msgpack 也要能构造并跑公开端点**：/info 全部公开不签名。
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import hashlib
import importlib
import json
import sys
import time
from decimal import Decimal
from typing import Any, Mapping

import httpx
import msgpack
import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

from earnfarm.exchanges import hyperliquid as HL
from earnfarm.exchanges.base import (
    Credential,
    ExchangeError,
    OrderRejected,
    OrderRequest,
    OrderUnknown,
    RateLimited,
)
from earnfarm.exchanges.hyperliquid import (
    EXCHANGE_PATH,
    FUNDING_HISTORY_PAGE,
    INFO_PATH,
    HyperliquidAdapter,
    _wire,
)
from earnfarm.models import MarketKind

# 一把纯测试用的私钥（0x + 64 hex，从未在链上用过）。
API_SECRET = "0x" + "11" * 32
# api_key 放的是**账户地址**不是 agent 地址——调研文档"凭据映射"节点名的
# "最容易配错的一格"：填成 agent 地址会让 fetch_positions 静默返回空账户。
ACCOUNT_ADDRESS = "0x" + "ab" * 20
VAULT_ADDRESS = "0x" + "cd" * 20
AGENT_ADDRESS = Account.from_key(API_SECRET).address.lower()

CRED = Credential(ACCOUNT_ADDRESS, API_SECRET)
CRED_VAULT = Credential(ACCOUNT_ADDRESS, API_SECRET, passphrase=VAULT_ADDRESS)
CRED_PUBLIC = Credential("", "")          # public_feed 就是用这个构造的

# ---- 文档样例 -----------------------------------------------------------
# universe 的**下标就是 asset index**，且 delisted 的币仍留在里面占着下标
# （文档 instruments 节）。这里故意把 OLD 摆在第 1 位、HIP-3 的 xyz:XYZ100 摆在最后：
# 先过滤再 enumerate 的实现会让 kPEPE/HYPE 的 index 左移一位。
META = {
    "universe": [
        {"name": "BTC", "szDecimals": 5, "maxLeverage": 40, "marginTableId": 50},
        {"name": "OLD", "szDecimals": 2, "maxLeverage": 3, "isDelisted": True},
        {"name": "kPEPE", "szDecimals": 0, "maxLeverage": 10, "marginTableId": 51},
        {"name": "HYPE", "szDecimals": 2, "maxLeverage": 5, "marginTableId": 51,
         "onlyIsolated": False, "isDelisted": False},
        {"name": "xyz:XYZ100", "szDecimals": 2, "maxLeverage": 3},   # HIP-3 builder 市场
    ],
    "marginTables": [
        # 文档给的 BTC 主网例子：0-150M → 40x，>150M → 20x
        [50, {"description": "", "marginTiers": [
            {"lowerBound": "0.0", "maxLeverage": 40},
            {"lowerBound": "150000000.0", "maxLeverage": 20}]}],
        [51, {"description": "", "marginTiers": [
            {"lowerBound": "0.0", "maxLeverage": 5}]}],
    ],
}

# metaAndAssetCtxs 的 [1] 与 universe **按下标一一对应**，长度必须相等
CTXS = [
    {"dayNtlVlm": "1169046.29406", "funding": "0.0000125",
     "impactPxs": ["99999.0", "100001.0"], "markPx": "100000.0", "midPx": "100000.0",
     "openInterest": "688.11", "oraclePx": "100000.0", "premium": "0.00031774",
     "prevDayPx": "99000.0"},
    {"funding": "0.0", "markPx": "1.0"},                       # OLD（delisted，会被跳过）
    {"funding": "-0.00022196", "markPx": "0.01"},              # kPEPE：负费率
    {"funding": "0.0004", "markPx": "40.0"},                   # HYPE
    {"funding": "0.5", "markPx": "7.0"},                       # xyz:XYZ100（HIP-3，跳过）
]

# l2Book：levels 是长度 2 的数组，[0]=bids 价降序、[1]=asks 价升序（文档 bookTop 节）。
# mid = (99999.5 + 100000.5) / 2 = 100000
# 10bp → lo = 99900（含）、hi = 100100（含）
BTC_BOOK = {
    "coin": "BTC", "time": 1754450974231,
    "levels": [
        [{"px": "99999.5", "sz": "2", "n": 17},
         {"px": "99950.0", "sz": "3", "n": 5},
         {"px": "99920.0", "sz": "0", "n": 0},        # 脏档：量为 0，既不计额也不计档
         {"px": "99900.0", "sz": "1", "n": 2},        # 正好压在下边界上，**含**
         {"px": "99899.9", "sz": "999999", "n": 1}],  # 带外：误收一档差一个数量级
        [{"px": "100000.5", "sz": "1", "n": 3},
         {"px": "100010.0", "sz": "2.5", "n": 4},
         {"px": "100100.0", "sz": "0.5", "n": 2},     # 正好压在上边界上，**含**
         {"px": "100100.1", "sz": "999999", "n": 1}], # 带外
    ],
}
# kPEPE：上线价是"每 kPEPE"，对外必须换成"每 PEPE"（价除 1000、量乘 1000）
KPEPE_BOOK = {
    "coin": "kPEPE", "time": 1754450974231,
    "levels": [[{"px": "0.010000", "sz": "5000", "n": 3}],
               [{"px": "0.010010", "sz": "4000", "n": 2}]],
}
# 簿盖不满带宽：三档全在带内，结果必须**恰好**等于这三档，不能按密度外推
SHORT_BOOK = {
    "coin": "BTC", "time": 1754450974231,
    "levels": [[{"px": "99999.5", "sz": "2"}, {"px": "99990.0", "sz": "1"}],
               [{"px": "100000.5", "sz": "1"}]],
}

# userFees：文档 feeRate 节的完整样例。cross = taker、add = maker，
# 且 0.00045 * (1 - 0.3) = 0.000315 —— **折扣已经算进去了，不要再打一次折**。
USER_FEES = {
    "dailyUserVlm": [{"date": "2025-05-23", "userCross": "0.0", "userAdd": "0.0",
                      "exchange": "2852367.077"}],
    "feeSchedule": {"cross": "0.00045", "add": "0.00015", "spotCross": "0.0007",
                    "spotAdd": "0.0004", "tiers": {"vip": [], "mm": []},
                    "referralDiscount": "0.04", "stakingDiscountTiers": []},
    "userCrossRate": "0.000315",
    "userAddRate": "0.000105",
    "userSpotCrossRate": "0.00049",
    "userSpotAddRate": "0.00028",
    "activeReferralDiscount": "0.0",
    "trial": None,
    "feeTrialReward": "0.0",
    "nextTrialAvailableTimestamp": None,
    "stakingLink": {"type": "tradingUser", "stakingUser": "0x54c0"},
    "activeStakingDiscount": {"bpsOfMaxSupply": "4.7577998927", "discount": "0.3"},
}

CLEARINGHOUSE = {
    "assetPositions": [
        {"type": "oneWay", "position": {
            "coin": "BTC", "szi": "-0.0335", "entryPx": "2986.3",
            "positionValue": "100.02765", "unrealizedPnl": "-0.0134",
            "returnOnEquity": "-0.0026789", "liquidationPx": "2866.26936529",
            "marginUsed": "4.967826", "maxLeverage": 50,
            "leverage": {"type": "isolated", "value": 20, "rawUsd": "-95.059824"},
            "cumFunding": {"allTime": "514.085417", "sinceOpen": "0.0",
                           "sinceChange": "0.0"}}},
        {"type": "oneWay", "position": {
            "coin": "kPEPE", "szi": "1000", "entryPx": "0.01",
            "positionValue": "10.0", "liquidationPx": None,
            "leverage": {"type": "cross", "value": 5}}},
    ],
    "crossMaintenanceMarginUsed": "0.0",
    "marginSummary": {"accountValue": "13109.48", "totalMarginUsed": "4.97",
                      "totalNtlPos": "100.03", "totalRawUsd": "13009.45"},
    "time": 1708622398623,
    "withdrawable": "13104.514502",
}

FRONTEND_OPEN_ORDERS = [
    {"coin": "BTC", "side": "A", "limitPx": "29792.0", "sz": "5.0", "origSz": "5.0",
     "oid": 91490942, "timestamp": 1681247412573, "orderType": "Limit",
     "reduceOnly": False, "isTrigger": False, "triggerPx": "0.0",
     "triggerCondition": "N/A", "isPositionTpsl": False, "cloid": None},
    {"coin": "BTC", "side": "B", "limitPx": "29000.0", "sz": "2.0", "origSz": "5.0",
     "oid": 91490943, "timestamp": 1681247412599, "orderType": "Limit",
     "reduceOnly": False, "isTrigger": False, "cloid": None},   # cloid 运行时补
    {"coin": "HYPE", "side": "B", "limitPx": "40.0", "sz": "1.0", "origSz": "1.0",
     "oid": 91490944, "timestamp": 1681247412600, "cloid": None},  # 别的币，必须滤掉
]

ORDER_STATUS_FILLED = {
    "status": "order",
    "order": {"order": {"coin": "BTC", "side": "A", "limitPx": "2412.7", "sz": "0.0",
                        "origSz": "0.0076", "oid": 90542681, "timestamp": 1724361546645,
                        "orderType": "Market", "tif": "FrontendMarket",
                        "reduceOnly": True, "isTrigger": False, "triggerPx": "0.0",
                        "triggerCondition": "N/A", "children": [],
                        "isPositionTpsl": False, "cloid": None},
              "status": "filled", "statusTimestamp": 1724361546645},
}
USER_FILLS = [
    {"coin": "BTC", "px": "18.435", "sz": "0.0036", "side": "B", "time": 1681222254710,
     "startPosition": "26.86", "dir": "Open Long", "closedPnl": "0.0", "hash": "0x1",
     "oid": 90542681, "crossed": False, "fee": "0.01", "feeToken": "USDC",
     "tid": 118906512037719},
    {"coin": "BTC", "px": "18.5", "sz": "0.004", "side": "B", "time": 1681222254800,
     "oid": 90542681, "crossed": True, "fee": "0.02", "feeToken": "USDC"},
    {"coin": "BTC", "px": "999", "sz": "9", "side": "B", "oid": 1, "fee": "5"},  # 别的单
]

PLACE_RESTING = {"status": "ok", "response": {"type": "order", "data": {
    "statuses": [{"resting": {"oid": 77738308}}]}}}
PLACE_FILLED = {"status": "ok", "response": {"type": "order", "data": {
    "statuses": [{"filled": {"totalSz": "0.55", "avgPx": "100000.5", "oid": 77747314}}]}}}
CANCEL_OK = {"status": "ok", "response": {"type": "cancel", "data": {
    "statuses": ["success"]}}}
LEVERAGE_OK = {"status": "ok", "response": {"type": "default"}}


def run(coro):
    return asyncio.run(coro)


# ---- 独立重算的签名实现（照着调研文档逐段抄，不引用被测代码） ----------

def doc_connection_id(action: Mapping[str, Any], nonce: int,
                      vault_address: str | None = None,
                      expires_after: int | None = None) -> bytes:
    """调研文档"L1 action 签名算法"第 1-5 步，逐字节独立实现。"""
    data = msgpack.packb(action)
    data += int(nonce).to_bytes(8, "big")
    if vault_address is None:
        data += b"\x00"
    else:
        data += b"\x01"
        raw = vault_address[2:] if vault_address.startswith("0x") else vault_address
        data += bytes.fromhex(raw)
    if expires_after is not None:
        # 这个 0x00 是 expiresAfter 段**自己的前缀**，不是"无"的标记
        data += b"\x00"
        data += int(expires_after).to_bytes(8, "big")
    return keccak(data)


def doc_typed_data(connection_id: bytes, *, mainnet: bool = True) -> dict:
    """调研文档第 6 步的 phantom agent EIP-712 payload。"""
    return {
        "domain": {"chainId": 1337, "name": "Exchange",
                   "verifyingContract": "0x0000000000000000000000000000000000000000",
                   "version": "1"},
        "types": {"Agent": [{"name": "source", "type": "string"},
                            {"name": "connectionId", "type": "bytes32"}],
                  "EIP712Domain": [{"name": "name", "type": "string"},
                                   {"name": "version", "type": "string"},
                                   {"name": "chainId", "type": "uint256"},
                                   {"name": "verifyingContract", "type": "address"}]},
        "primaryType": "Agent",
        "message": {"source": "a" if mainnet else "b", "connectionId": connection_id},
    }


def recover_signer(envelope: Mapping[str, Any], *, mainnet: bool = True) -> str:
    """从**真正上线的 JSON body** 里恢复 signer 地址（小写）。

    这是本文件的核心校验手段：body 里的 action 原样重新 msgpack，能恢复出 agent 地址
    就同时证明了"上线 JSON 的键顺序 == 签名时的 msgpack 键顺序"。
    """
    connection_id = doc_connection_id(
        envelope["action"], envelope["nonce"],
        envelope.get("vaultAddress"), envelope.get("expiresAfter"))
    sig = envelope["signature"]
    return Account.recover_message(
        encode_typed_data(full_message=doc_typed_data(connection_id, mainnet=mainnet)),
        vrs=(sig["v"], int(sig["r"], 16), int(sig["s"], 16))).lower()


# ---- 测试脚手架 ---------------------------------------------------------

def json_response(payload, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


class Recorder:
    """记录所有出站请求。HL 只有两个路径，所以按 body 里的 type 分路由。"""

    def __init__(self, routes: dict, default=None):
        self.routes = routes
        self.default = default
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append(request)
        self.bodies.append(body)
        if request.url.path == EXCHANGE_PATH:
            key = (body.get("action") or {}).get("type")
        else:
            key = body.get("type")
        handler = self.routes.get(key, self.default)
        if handler is None:
            raise AssertionError(f"未预期的请求: {request.url.path} {body}")
        if callable(handler):
            return handler(request)
        return json_response(handler)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    @property
    def last_body(self) -> dict:
        return self.bodies[-1]

    def sent(self, type_: str) -> list[dict]:
        """按 info type / action type 取出所有发过的 body。"""
        out = []
        for body in self.bodies:
            key = ((body.get("action") or {}).get("type") if "action" in body
                   else body.get("type"))
            if key == type_:
                out.append(body)
        return out


def make_adapter(recorder: Recorder, credential: Credential = CRED,
                 broker_code: str = "", seed_meta: bool = True,
                 **kw) -> HyperliquidAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder),
                               timeout=httpx.Timeout(1.0))
    ad = HyperliquidAdapter(credential, broker_code=broker_code, client=client, **kw)
    # 跳过自动校时，免得每个用例都要伺候 exchangeStatus
    ad._clock.synced_at = time.time()
    if seed_meta:
        # _load_universe 是纯同步方法：既建 asset index 表也建杠杆档位表，不发请求
        ad._load_universe(META)
    return ad


@contextlib.contextmanager
def adapter(recorder: Recorder, **kw):
    ad = make_adapter(recorder, **kw)
    try:
        yield ad
    finally:
        run(ad.close())


def order_req(**kw) -> OrderRequest:
    base = dict(symbol="BTC", side="buy", qty=Decimal("0.55"),
                price=Decimal("100000"), time_in_force="IOC",
                client_order_id="cf-leg1-0001")
    base.update(kw)
    return OrderRequest(**base)


# =========================================================================
# 签名
# =========================================================================

def test_action_hash_matches_doc_algorithm_byte_for_byte():
    """connectionId 的四段拼接逐字节对齐调研文档的伪代码。

    三种形态各验一遍：无 vault（单字节 0x00）、有 vault（0x01 + 20 字节地址原文）、
    带 expiresAfter（**先补一个 0x00 再 8 字节大端**——那个 0x00 是本段自己的前缀）。
    错一个字节 = 恢复出另一个地址，而服务端报的是"用户不存在"，离病根很远。
    """
    action = {"type": "order", "orders": [{"a": 0, "b": True, "p": "100000",
                                           "s": "0.55", "r": False,
                                           "t": {"limit": {"tif": "Ioc"}}}],
              "grouping": "na"}
    nonce = 1754450974231
    rec = Recorder({})
    with adapter(rec) as ad:
        assert ad._action_hash(action, nonce, None, None) == \
            doc_connection_id(action, nonce)
        assert ad._action_hash(action, nonce, VAULT_ADDRESS, None) == \
            doc_connection_id(action, nonce, VAULT_ADDRESS)
        assert ad._action_hash(action, nonce, None, nonce + 5000) == \
            doc_connection_id(action, nonce, None, nonce + 5000)
        # 手工核对三段的字节长度：msgpack ‖ 8 字节 nonce ‖ 1 字节 0x00
        raw = msgpack.packb(action)
        assert keccak(raw + nonce.to_bytes(8, "big") + b"\x00") == \
            ad._action_hash(action, nonce, None, None)
        # vault 段是 1 + 20 字节，不是 hex 串
        assert keccak(raw + nonce.to_bytes(8, "big") + b"\x01"
                      + bytes.fromhex(VAULT_ADDRESS[2:])) == \
            ad._action_hash(action, nonce, VAULT_ADDRESS, None)


def test_eip712_domain_is_chain_1337_and_primary_type_agent():
    """chainId 恒为 1337（历史遗留的假链 id），不是 42161 / 998 / 999。

    primaryType "Agent"，字段顺序 source 在前、connectionId 在后——顺序也进哈希。
    """
    assert HL.EIP712_CHAIN_ID == 1337
    assert HL.EIP712_DOMAIN_NAME == "Exchange"
    assert HL.EIP712_DOMAIN_VERSION == "1"
    assert HL.EIP712_VERIFYING_CONTRACT == "0x" + "00" * 20
    assert HL.PHANTOM_SOURCE_MAINNET == "a" and HL.PHANTOM_SOURCE_TESTNET == "b"

    captured = {}
    real = HL._encode_typed_data

    def spy(full_message=None, **kw):
        captured["payload"] = full_message
        return real(full_message=full_message, **kw)

    rec = Recorder({"order": PLACE_RESTING})
    with adapter(rec) as ad:
        HL._encode_typed_data = spy
        try:
            run(ad.place_order(order_req()))
        finally:
            HL._encode_typed_data = real
    payload = captured["payload"]
    assert payload["primaryType"] == "Agent"
    assert [f["name"] for f in payload["types"]["Agent"]] == ["source", "connectionId"]
    assert payload["domain"]["chainId"] == 1337
    assert payload["message"]["source"] == "a"
    assert len(payload["message"]["connectionId"]) == 32


def test_wire_action_recovers_the_agent_address():
    """【核心】用**真正上线的请求字节**重算签名：从 body 解析出 action、重新 msgpack、
    重算 keccak、恢复 signer，必须等于 API Wallet 地址。

    这同时证明了 httpx 实际序列化出去的键顺序就是签名时用的 msgpack 键顺序
    ——调研文档"坑"节第 15 条要求验的正是这一条。
    """
    rec = Recorder({"order": PLACE_FILLED})
    with adapter(rec) as ad:
        run(ad.place_order(order_req()))
    envelope = rec.last_body
    assert recover_signer(envelope) == AGENT_ADDRESS
    # 信封形状照官方 SDK：action / nonce / signature / vaultAddress / expiresAfter
    assert set(envelope) == {"action", "nonce", "signature", "vaultAddress",
                             "expiresAfter"}
    assert envelope["vaultAddress"] is None and envelope["expiresAfter"] is None
    assert set(envelope["signature"]) == {"r", "s", "v"}
    assert envelope["signature"]["v"] in (27, 28)
    assert envelope["nonce"] > 1_600_000_000_000       # 毫秒时间戳，不是秒


def test_key_order_on_the_wire_is_the_signature():
    """msgpack 按 dict 插入顺序编码，所以**键顺序就是签名**。

    上线 JSON 的键序必须是 action: type→orders→grouping，
    order wire: a→b→p→s→r→t→c（官方 SDK 的顺序）。反证：把 orders[0] 的键换个顺序
    重新 msgpack，恢复出来的就是另一个地址——这说明"顺手整理字段顺序"的重构会让下单全灭。
    """
    rec = Recorder({"order": PLACE_RESTING})
    with adapter(rec) as ad:
        run(ad.place_order(order_req()))
    action = rec.last_body["action"]
    assert list(action.keys()) == ["type", "orders", "grouping"]
    assert list(action["orders"][0].keys()) == ["a", "b", "p", "s", "r", "t", "c"]
    assert list(action["orders"][0]["t"].keys()) == ["limit"]

    shuffled = dict(action)
    order = action["orders"][0]
    shuffled["orders"] = [{k: order[k] for k in ("b", "a", "p", "s", "r", "t", "c")}]
    assert doc_connection_id(shuffled, rec.last_body["nonce"]) != \
        doc_connection_id(action, rec.last_body["nonce"])
    bad = dict(rec.last_body, action=shuffled)
    assert recover_signer(bad) != AGENT_ADDRESS


def test_testnet_source_b_changes_the_recovered_address():
    """主网/测试网不是只换域名：phantom agent 的 source 主网 "a"、测试网 "b"。

    用错这一个字符，签名会恢复出**另一个地址**，而服务端报的是
    "User or API Wallet 0x... does not exist"——看到这条错第一反应必须是签名错。
    """
    rec = Recorder({"order": PLACE_RESTING})
    with adapter(rec, testnet=True) as ad:
        assert ad.rest_base == HL.REST_BASE_TESTNET
        run(ad.place_order(order_req()))
    envelope = rec.last_body
    assert recover_signer(envelope, mainnet=False) == AGENT_ADDRESS
    assert recover_signer(envelope, mainnet=True) != AGENT_ADDRESS
    assert rec.last.url.host.endswith("hyperliquid-testnet.xyz")


def test_vault_address_enters_the_hash_and_is_lowercased():
    """vaultAddress 走 passphrase 位，它**进 action hash**（0x01 + 20 字节）。

    地址一律小写后再进签名（官方点名的翻车点之四：有些字段按 bytes 解析并在全网
    被自动小写化）。
    """
    cred = Credential(ACCOUNT_ADDRESS, API_SECRET, passphrase=VAULT_ADDRESS.upper())
    rec = Recorder({"order": PLACE_RESTING})
    with adapter(rec, credential=cred) as ad:
        assert ad.vault_address == VAULT_ADDRESS          # 已小写
        run(ad.place_order(order_req()))
    envelope = rec.last_body
    assert envelope["vaultAddress"] == VAULT_ADDRESS
    assert recover_signer(envelope) == AGENT_ADDRESS
    # 反证：按"没有 vault"重算就恢复不出 agent —— 说明这一段真的进了哈希
    assert recover_signer(dict(envelope, vaultAddress=None)) != AGENT_ADDRESS


def test_expires_after_enters_the_hash_when_enabled():
    """expiresAfter 是 HL 版的"迟到即拒"。它多两段字节进 hash，
    所以要么全程带、要么全程不带，不能只在部分路径带。"""
    rec = Recorder({"order": PLACE_RESTING, "cancelByCloid": CANCEL_OK})
    with adapter(rec, order_ttl_ms=5_000) as ad:
        run(ad.place_order(order_req()))
        place = rec.last_body
        run(ad.cancel_order("BTC", "cf-leg1-0001"))
        cancel = rec.last_body
    for envelope in (place, cancel):
        assert envelope["expiresAfter"] is not None
        assert recover_signer(envelope) == AGENT_ADDRESS
        # 漏掉这一段就恢复不出 agent
        assert recover_signer(dict(envelope, expiresAfter=None)) != AGENT_ADDRESS
    assert place["expiresAfter"] > place["nonce"] - 1000


def test_nonce_is_monotonic_within_the_same_millisecond():
    """nonce 必须**进程内单调递增**：500ms 收敛循环里两个协程同时取
    time.time()*1000 会拿到同一毫秒，第二个请求"nonce 已用过"直接被拒。"""
    rec = Recorder({})
    with adapter(rec) as ad:
        ad.timestamp_ms = lambda: 1754450974231        # 冻结时钟，模拟同毫秒并发
        nonces = [ad._next_nonce() for _ in range(5)]
    assert nonces == sorted(set(nonces)) and len(nonces) == 5
    assert nonces[0] == 1754450974231
    assert all(b == a + 1 for a, b in zip(nonces, nonces[1:]))


def test_only_two_paths_no_query_no_api_key_header():
    """整个 API 只有 POST /info 和 POST /exchange，没有 query string，
    除 Content-Type 外没有任何 header 要求（**没有 api-key header**）。"""
    rec = Recorder({"metaAndAssetCtxs": [META, CTXS], "order": PLACE_RESTING})
    with adapter(rec) as ad:
        run(ad.fetch_carry_rates())
        run(ad.place_order(order_req()))
    allowed = {"content-type", "host", "accept", "accept-encoding", "connection",
               "user-agent", "content-length"}
    for req in rec.requests:
        assert req.method == "POST"
        assert req.url.path in (INFO_PATH, EXCHANGE_PATH)
        assert req.url.query == b""
        assert req.headers["Content-Type"] == "application/json"
        assert {k.lower() for k in req.headers} <= allowed, req.headers


# =========================================================================
# 返佣码：一个字节都不带
# =========================================================================

def test_no_referral_trace_anywhere_even_when_broker_code_is_set():
    """**用户明确要求不注入任何返佣码/邀请码**。

    HL 的返佣等价物是 action 里的 builder 字段（{"b": 地址, "f": 十分之一基点}）。
    它一旦出现，msgpack 字节就变了，签名和已有测试一起失效——所以这里连"传了 broker_code"
    的情况都要保证零痕迹：没有 builder 键、没有渠道 header、cloid 不加前缀。
    """
    rec = Recorder({"order": PLACE_RESTING, "cancelByCloid": CANCEL_OK,
                    "updateLeverage": LEVERAGE_OK})
    with adapter(rec, broker_code="  myfundingbot  ") as ad:
        assert ad.broker_code == "myfundingbot"      # 接下来了，但什么都不做
        run(ad.place_order(order_req()))
        run(ad.cancel_order("BTC", "cf-leg1-0001"))
        run(ad.set_leverage("BTC", Decimal("5")))

    for req, body in zip(rec.requests, rec.bodies):
        raw = req.content.decode().lower()
        for word in ("builder", "referral", "broker", "channel", "affiliate",
                     "partner", "myfundingbot", "agentcode", "brokerid"):
            assert word not in raw, (word, raw)
        for name in req.headers:
            assert "refer" not in name.lower() and "broker" not in name.lower()
            assert "myfundingbot" not in req.headers[name]
        if "action" in body:
            assert "builder" not in body["action"]
    # cloid 是纯 sha256 截断，没有任何前缀
    action = rec.sent("order")[0]["action"]
    assert action["orders"][0]["c"] == \
        "0x" + hashlib.sha256(b"cf-leg1-0001").hexdigest()[:32]


def test_broker_code_does_not_change_the_signature():
    """返佣码既然一个字节都不进 action，带不带码签出来的必须一模一样。"""
    signatures = []

    def handler(request: httpx.Request) -> httpx.Response:
        signatures.append(json.loads(request.content)["signature"])
        return json_response(PLACE_RESTING)

    rec = Recorder({"order": handler})
    for code in ("", "myfundingbot"):
        with adapter(rec, broker_code=code) as ad:
            ad._next_nonce = lambda: 1754450974231     # 冻结 nonce
            run(ad.place_order(order_req()))
    assert signatures[0] == signatures[1]
    assert rec.sent("order")[0]["action"] == rec.sent("order")[1]["action"]


# =========================================================================
# 合约表 / 精度
# =========================================================================

def test_instrument_precision_multiplier_and_funding_interval():
    """Instrument 的五个字段全部按调研文档 instruments 节映射。

    HL **没有 tick_size 字段**：价格合法性是"5 位有效数字"和"小数位 <= 6−szDecimals"
    两条规则的交集，只有后者填得进 tick_size（保守代理值）。
    结算周期是 **3600**，写死 28800 会把这家的年化算成实际值的 1/8。
    """
    rec = Recorder({"meta": META})
    with adapter(rec, seed_meta=False) as ad:
        insts = run(ad.fetch_instruments())
    by_symbol = {i.symbol: i for i in insts}
    assert set(by_symbol) == {"BTC", "kPEPE", "HYPE"}      # 下架 / HIP-3 都不出现

    btc = by_symbol["BTC"]
    assert btc.tick_size == Decimal("0.1")          # 10^-(6-5)
    assert btc.lot_size == Decimal("0.00001")       # 10^-szDecimals
    assert btc.contract_size == Decimal("1")        # 线性合约，1 张 = 1 个 underlying
    assert btc.min_notional == Decimal("10")        # MinTradeNtl: $10
    assert btc.max_leverage == Decimal("40")
    assert btc.funding_interval_s == 3600           # **每小时**，不是 8 小时
    assert btc.base == "BTC" and btc.quote == "USDC"   # 保证金是 USDC 不是 USDT
    assert btc.market == "hyperliquid:perp"

    hype = by_symbol["HYPE"]
    assert hype.tick_size == Decimal("0.0001")      # 10^-(6-2)
    assert hype.lot_size == Decimal("0.01")
    # 请求体就两个键，dex 传空串 = 官方主市场
    assert rec.sent("meta")[0] == {"type": "meta", "dex": ""}


def test_delisted_rows_keep_the_asset_index():
    """**delisted 的币仍留在 universe 里占着下标**，先过滤再 enumerate 会让全表左移，
    下单打到完全不相干的币上——而且不报任何错。"""
    rec = Recorder({"meta": META, "order": PLACE_RESTING})
    with adapter(rec, seed_meta=False) as ad:
        run(ad.fetch_instruments())
        assert ad._meta["BTC"].index == 0
        assert ad._meta["kPEPE"].index == 2          # OLD 占着 1
        assert ad._meta["HYPE"].index == 3
        assert "OLD" not in ad._meta                 # 过滤掉，但下标已经让出去了
        run(ad.place_order(order_req(symbol="HYPE", qty=Decimal("1"),
                                     price=Decimal("40"),
                                     client_order_id="cf-hype")))
    assert rec.sent("order")[0]["action"]["orders"][0]["a"] == 3


def test_k_prefixed_coins_carry_the_multiplier_and_can_be_excluded():
    """k 前缀币（kPEPE）按仓库既有约定处理：base 归一成 PEPE、倍数进 contract_size、
    价除量乘封死在适配器内部——与 binance.py 处理 1000PEPEUSDT 同一套口径。

    **UNVERIFIED**：官方文档完全没提这个前缀的含义（按行业惯例是 1000×）。
    所以留了 include_k_prefixed=False 一键整族排除，且排除时要留名单给界面解释。
    """
    rec = Recorder({"meta": META})
    with adapter(rec, seed_meta=False) as ad:
        kpepe = {i.symbol: i for i in run(ad.fetch_instruments())}["kPEPE"]
    assert kpepe.base == "PEPE"                     # 剥 k，和 normalize_base 对得上
    assert kpepe.contract_size == Decimal("1000")
    assert kpepe.lot_size == Decimal("1000")        # 10^0 * 1000 个 PEPE
    assert kpepe.tick_size == Decimal("1E-9")       # 10^-6 / 1000

    rec2 = Recorder({"meta": META})
    with adapter(rec2, seed_meta=False, include_k_prefixed=False) as ad2:
        names = [i.symbol for i in run(ad2.fetch_instruments())]
        assert names == ["BTC", "HYPE"]
        assert ad2.excluded_symbols == {"kPEPE"}
        # 排除之后连规格都查不到，绝不能退化成"按 1 倍猜"
        with pytest.raises(ExchangeError) as ei:
            run(ad2._meta_of("kPEPE"))
        assert ei.value.code == "SPEC_MISSING"


def test_leverage_tiers_shift_lower_bounds_into_upper_bounds():
    """meta 给的是每档**下限**，基类要的是**上限**，所以要错位换算，
    末档填 Infinity 哨兵。查不到时退化成单档，**绝不返回空列表**
    （空列表在上层等于"没有档位限制"）。"""
    rec = Recorder({})
    with adapter(rec) as ad:
        tiers = run(ad.fetch_leverage_tiers("BTC"))
        assert tiers == [(Decimal("150000000.0"), Decimal("40")),
                         (Decimal("Infinity"), Decimal("20"))]
        assert tiers == sorted(tiers, key=lambda t: t[0])
        # marginTableId 缺失的币退化成单档
        ad._meta["NOTABLE"] = HL._AssetMeta(9, {"name": "NOTABLE", "szDecimals": 2,
                                                "maxLeverage": 3})
        assert run(ad.fetch_leverage_tiers("NOTABLE")) == \
            [(Decimal("Infinity"), Decimal("3"))]
    assert not rec.requests                          # 全从 meta 缓存来，不额外发请求


def test_price_rounding_is_sigfigs_then_decimals():
    """价格是两条规则的**交集**：5 位有效数字 + 小数位 <= 6−szDecimals，
    且**整数价永远合法**。只做 quantize(tick) 会在 1234.56 这种价上被
    "Price must be divisible by tick size." 拒。

    方向：买单向上、卖单向下（保护限价是故意穿透对手价的）。
    """
    r = HyperliquidAdapter.round_price
    assert r(Decimal("1234.5678"), 2, "buy") == Decimal("1234.6")    # 5 位有效数字
    assert r(Decimal("1234.5678"), 2, "sell") == Decimal("1234.5")
    assert r(Decimal("0.00123456"), 0, "buy") == Decimal("0.001235") # 小数位更紧
    assert r(Decimal("123456"), 5, "buy") == Decimal("123456")       # 整数价直接放行
    assert r(Decimal("100000"), 5, "sell") == Decimal("100000")
    # szDecimals 越大，允许的小数位越少
    assert r(Decimal("1.234567"), 5, "buy") == Decimal("1.3")
    assert r(Decimal("1.234567"), 0, "buy") == Decimal("1.2346")


def test_size_rounds_toward_zero_and_wire_never_uses_scientific_notation():
    """数量向零取整（宁可少下也不能超目标）。

    wire 串必须去尾随零且**禁掉科学计数法**——两头都会咬人：
    str(Decimal("50000.0").normalize()) 是 '5E+4'，而 50000 正是官方下单示例里的价格。
    """
    assert HyperliquidAdapter.round_size(Decimal("0.123456789"), 5) == Decimal("0.12345")
    assert HyperliquidAdapter.round_size(Decimal("-0.123456789"), 5) == Decimal("-0.12345")
    assert HyperliquidAdapter.round_size(Decimal("3.7"), 0) == Decimal("3")
    assert _wire(Decimal("50000.0")) == "50000"
    assert _wire(Decimal("0.00001")) == "0.00001"
    assert _wire(Decimal("0.5500")) == "0.55"
    assert _wire(Decimal("-0")) == "0"        # SDK 那句 guard 是死代码，这里真的拦得住
    assert _wire(Decimal("-0.0")) == "0"


# =========================================================================
# 资金费
# =========================================================================

def test_carry_rate_sign_is_exchange_native_and_hourly():
    """交易所原始约定：正 = 多头付给空头。**原样存，绝不取反。**

    取反会让 scoring 的 short_rate − long_rate 全体反号，而错的方向恰好是
    "把亏钱的对推荐成赚钱的"。interval_s 必须是 3600（每小时结算）。
    """
    rec = Recorder({"metaAndAssetCtxs": [META, CTXS]})
    with adapter(rec) as ad:
        rates = run(ad.fetch_carry_rates())
    by_symbol = {r.symbol: r for r in rates}
    assert set(by_symbol) == {"BTC", "kPEPE", "HYPE"}     # delisted / HIP-3 不出现

    btc = by_symbol["BTC"]
    assert btc.rate == Decimal("0.0000125")   # 溢价为 0 时的地板值，不是脏数据
    assert btc.rate > 0 and btc.annualized() > 0
    assert btc.interval_s == 3600
    # 每小时口径：年化 = 8760 期，不是 1095
    assert btc.annualized() == Decimal("0.0000125") * (Decimal(365 * 24 * 3600) / 3600)
    assert btc.market == "hyperliquid:perp"

    kpepe = by_symbol["kPEPE"]
    assert kpepe.rate == Decimal("-0.00022196")           # 负的还是负的
    assert kpepe.rate < 0 and kpepe.annualized() < 0
    # 响应里没有 next_settle_ms，自己算下一个整点
    assert btc.next_settle_ms % 3_600_000 == 0
    assert 0 < btc.next_settle_ms - btc.ts_ms <= 3_600_000
    assert rec.sent("metaAndAssetCtxs")[0] == {"type": "metaAndAssetCtxs", "dex": ""}


def test_meta_and_ctx_length_mismatch_raises_instead_of_zipping():
    """两个数组按下标对齐，长度不等**必须抛错**。

    zip 静默截断之后全表符号错位——BTC 的费率会挂到别的币上，而且不报任何错。
    """
    rec = Recorder({"metaAndAssetCtxs": [META, CTXS[:3]]})
    with adapter(rec) as ad:
        with pytest.raises(ExchangeError) as ei:
            run(ad.fetch_carry_rates())
    assert ei.value.code == "META_CTX_MISMATCH"

    rec2 = Recorder({"metaAndAssetCtxs": {"universe": []}})   # 不是两元素数组
    with adapter(rec2) as ad2:
        with pytest.raises(ExchangeError) as ei2:
            run(ad2.fetch_carry_rates())
    assert ei2.value.code == "BAD_META"


def test_funding_limits_order_is_cap_then_floor_without_any_request():
    """返回顺序固定 (cap, floor)，cap >= floor。顺序搞反会让 scoring 的 clamp
    把费率夹到空区间。全市场同一个 ±4%/小时，**不发任何请求**。"""
    rec = Recorder({})
    with adapter(rec) as ad:
        limits = run(ad.fetch_funding_limits("BTC"))
        assert limits == (Decimal("0.04"), Decimal("-0.04"))
        cap, floor = limits
        assert cap > floor
        # 文档明说 cap 与币种无关，任何币都是同一个数
        assert run(ad.fetch_funding_limits("kPEPE")) == limits
    assert not rec.requests


def _funding_page(start_ms: int, count: int, *, rate: str = "0.00001") -> list[dict]:
    """每小时一条、升序，模拟服务端"从 startTime（**闭区间**）起最多 500 条"。"""
    first = start_ms - start_ms % 3_600_000
    if first < start_ms:
        first += 3_600_000
    return [{"coin": "BTC", "fundingRate": rate, "premium": "-0.0005",
             "time": first + i * 3_600_000} for i in range(count)]


def test_funding_history_pages_forward_to_the_newest_row():
    """翻页方向是向"新"翻（和 Bybit 相反），**每小时一条 → 500 条只有约 20.8 天**。

    30 天窗口必须翻至少两页；返回必须一直覆盖到窗口末端（最新那条），
    截断只能截最旧的一端。
    """
    now = 1754450974231
    end = now - now % 3_600_000
    start = end - 720 * 3_600_000                  # 30 天 = 720 条
    starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        cursor = body["startTime"]
        starts.append(cursor)
        rows = [r for r in _funding_page(start, 721) if r["time"] >= cursor]
        return json_response(rows[:FUNDING_HISTORY_PAGE])

    rec = Recorder({"fundingHistory": handler})
    with adapter(rec) as ad:
        hist = run(ad.fetch_funding_history("BTC", start))

    assert len(starts) >= 2, "720 条 / 每页 500 条，必须翻页"
    assert starts[0] == start
    assert starts[1] > starts[0], "游标必须向新推进"
    assert hist[-1][0] == end, "必须覆盖到窗口末端（最新那条）"
    assert hist[0][0] == start
    assert len(hist) == 721
    # 升序 + 去重（startTime 是闭区间，用末条时间当起点会把那条再收一遍）
    assert [t for t, _ in hist] == sorted(t for t, _ in hist)
    assert len({t for t, _ in hist}) == len(hist)
    # 第二页的起点必须是 last_time + 1，闭区间下才不会重收
    assert starts[1] == starts[0] + (FUNDING_HISTORY_PAGE - 1) * 3_600_000 + 1
    assert all(isinstance(r, Decimal) for _, r in hist)
    assert rec.sent("fundingHistory")[0]["coin"] == "BTC"


def test_funding_history_dedups_when_the_server_replays_the_boundary_row():
    """就算服务端把上一页最后一条又给了一遍（闭区间的典型行为），也必须去重。

    重复值会抬高 autocorr()、拖偏 median，**而且不报任何错**——稳定性评分悄悄偏乐观。
    """
    base = 1_700_000_000_000 - 1_700_000_000_000 % 3_600_000
    page1 = [{"coin": "BTC", "fundingRate": "0.00001", "time": base + i * 3_600_000}
             for i in range(FUNDING_HISTORY_PAGE)]
    # 第二页从上一页**末条**开始（重放一条），再给 3 条新的
    page2 = [page1[-1]] + [{"coin": "BTC", "fundingRate": "0.00002",
                            "time": page1[-1]["time"] + i * 3_600_000}
                           for i in range(1, 4)]
    pages = iter([page1, page2, []])

    rec = Recorder({"fundingHistory": lambda r: json_response(next(pages))})
    with adapter(rec) as ad:
        hist = run(ad.fetch_funding_history("BTC", base))

    times = [t for t, _ in hist]
    assert len(times) == len(set(times)) == FUNDING_HISTORY_PAGE + 3
    assert times == sorted(times)
    assert hist[-1][0] == page2[-1]["time"]
    # 重放那一条只算一次，且值取到了（不是被覆盖成脏值）
    assert dict(hist)[page1[-1]["time"]] in (Decimal("0.00001"), Decimal("0.00002"))


def test_funding_history_ignores_limit_and_returns_the_full_window():
    """limit 是**页大小**不是输出上限（基类契约原话："返回可以多于 limit"）。

    这里曾经拿它截尾：HL 是唯一"1 小时结算 + 页上限 500"的组合，
    history.py 传的 limit 就是 500，截尾等于把历史深度永远钉死在 20.8 天——
    "计划持有 1 月"要 732 个小时点，永远只有 500 个，稳定性评分从此困在
    is_prior，且 venue_capped=False，界面上连一堵"墙"都不显示。
    两个审查镜头各自独立揪出了这一条。
    """
    base = 1_700_000_000_000 - 1_700_000_000_000 % 3_600_000
    rows = [{"coin": "BTC", "fundingRate": "0.00001", "time": base + i * 3_600_000}
            for i in range(10)]
    rec = Recorder({"fundingHistory": rows})
    with adapter(rec) as ad:
        hist = run(ad.fetch_funding_history("BTC", base, limit=4))
    assert len(hist) == 10, "整段窗口都要回来，limit 不是输出上限"
    assert hist[0][0] == rows[0]["time"]
    assert hist[-1][0] == rows[-1]["time"]         # 覆盖到最新那条


def test_funding_history_stops_when_the_cursor_stalls():
    """停机保护：服务端不推进游标时必须**立刻**停，不能靠 32 页硬闸兜底。

    这是全适配器最贵的端点（权重 20 + 每 20 条加权），跑满 32 页 ≈ 1440 权重
    ≈ IP 一分钟总额的一大半，白白从风控路径嘴里抢配额。
    """
    base = 1_700_000_000_000 - 1_700_000_000_000 % 3_600_000
    rows = [{"coin": "BTC", "fundingRate": "0.00001", "time": base + i * 3_600_000}
            for i in range(FUNDING_HISTORY_PAGE)]      # 每次都返回满页且时间不变
    rec = Recorder({"fundingHistory": rows})
    with adapter(rec) as ad:
        hist = run(ad.fetch_funding_history("BTC", base))
    assert len(rec.requests) == 2, "第二页整页都不比游标新，就该收手"
    assert len(hist) == FUNDING_HISTORY_PAGE

    # 反向对照：32 页硬闸仍然是最后一道保险（服务端每页都真往前挪一点点）
    step = iter(range(1, 200))

    def creeping(request: httpx.Request) -> httpx.Response:
        start = json.loads(request.content)["startTime"]
        n = next(step)
        return json_response([{"coin": "BTC", "fundingRate": "0.00001",
                               "time": start + n + i} for i in
                              range(FUNDING_HISTORY_PAGE)])

    rec2 = Recorder({"fundingHistory": creeping})
    with adapter(rec2) as ad2:
        run(ad2.fetch_funding_history("BTC", base))
    assert len(rec2.requests) == HL.FUNDING_HISTORY_MAX_PAGES


# =========================================================================
# 费率档
# =========================================================================

def test_fee_schedule_is_account_level_and_positive_means_cost():
    """cross = taker（吃单），add = maker（挂单）——别把 cross 误读成全仓保证金。

    **折扣已经算进去了**（0.00045 × (1−0.3) = 0.000315），不要再打一次折。
    正数 = 成本，直接填不用翻转（不像 OKX）。账户级一档吃全市场 → symbol 填空串。
    """
    rec = Recorder({"userFees": USER_FEES})
    with adapter(rec) as ad:
        fees = run(ad.fetch_fee_schedule(["BTC", "HYPE"]))
    assert len(fees) == 1                      # 账户级，一条吃全市场
    fee = fees[0]
    assert fee.symbol == ""
    assert fee.taker == Decimal("0.000315")    # userCrossRate 原样
    assert fee.maker == Decimal("0.000105")    # userAddRate 原样
    assert fee.taker > 0 and fee.maker > 0     # 正数 = 成本
    assert fee.taker != Decimal("0.000315") * Decimal("0.7"), "折扣不能打第二次"
    assert fee.market == "hyperliquid:perp"
    assert "0.3" in fee.note                   # 折扣来源写进 note 供界面显示
    # 这是**公开的 info 请求**，不签名（和其它六家不同）
    assert rec.sent("userFees")[0] == {"type": "userFees", "user": ACCOUNT_ADDRESS}
    assert "signature" not in rec.last_body


def test_fee_schedule_returns_nothing_rather_than_a_zero_placeholder():
    """查不到就不返回这一条，**绝不补 maker=0/taker=0 占位**。

    零手续费会让四笔 taker 成本凭空消失、负期望的机会全部翻成正期望，且不报任何错。
    """
    rec = Recorder({"userFees": {"feeSchedule": {"cross": "0.00045", "add": "0.00015"}}})
    with adapter(rec) as ad:
        assert run(ad.fetch_fee_schedule(["BTC"])) == []

    # 没配地址（公开榜单模式）同样返回空，而不是编一个零费率
    rec2 = Recorder({})
    with adapter(rec2, credential=CRED_PUBLIC) as ad2:
        assert run(ad2.fetch_fee_schedule(["BTC"])) == []
    assert not rec2.requests


def test_fee_schedule_is_cached_and_survives_empty_symbols():
    """权重 20 且档位一天变不了一次 → 必须缓存。
    session.refresh_fees 调的是 fetch_fee_schedule(())，symbols 为空必须仍能返回。"""
    rec = Recorder({"userFees": USER_FEES})
    with adapter(rec) as ad:
        first = run(ad.fetch_fee_schedule(()))
        second = run(ad.fetch_fee_schedule(["BTC"]))
    assert first and first[0].taker == second[0].taker
    assert len(rec.sent("userFees")) == 1, "第二次必须走缓存"


# =========================================================================
# 盘口与深度
# =========================================================================

def test_book_top_uses_full_precision_and_converts_k_units():
    """算带内深度必须用**全精度**簿：不传 nSigFigs（聚合过的簿会把边界上的量算错）。

    k 前缀币价除量乘换成 base 口径——这里没有"没缓存就按 1 走"的退路，猜错是 1000 倍。
    """
    rec = Recorder({"l2Book": lambda r: json_response(
        KPEPE_BOOK if json.loads(r.content)["coin"] == "kPEPE" else BTC_BOOK)})
    with adapter(rec) as ad:
        top = run(ad.fetch_book_top("BTC"))
        ktop = run(ad.fetch_book_top("kPEPE"))

    assert top.bid_price == Decimal("99999.5") and top.bid_qty == Decimal("2")
    assert top.ask_price == Decimal("100000.5") and top.ask_qty == Decimal("1")
    assert top.ts_ms == 1754450974231
    assert type(top.bid_qty) is Decimal
    # 每 PEPE 的价 = 每 kPEPE 的价 / 1000；量反过来乘
    assert ktop.bid_price == Decimal("0.010000") / 1000
    assert ktop.bid_qty == Decimal("5000") * 1000
    assert ktop.ask_price == Decimal("0.010010") / 1000
    body = rec.sent("l2Book")[0]
    assert body == {"type": "l2Book", "coin": "BTC"}     # 不发 nSigFigs = 全精度


def test_book_depth_counts_the_band_inclusively_and_skips_dirty_levels():
    """带宽内逐档累计，**边界档是含的**，价或量 <= 0 的脏档既不计额也不计档。

    带外那两档各挂了 999999 个币（约 1000 亿 U），漏一档结果就差好几个数量级——
    这正是容量估算最容易翻车的地方。
    """
    rec = Recorder({"l2Book": BTC_BOOK})
    with adapter(rec) as ad:
        wide = run(ad.fetch_book_depth("BTC", band_bp=10.0))
        narrow = run(ad.fetch_book_depth("BTC", band_bp=1.0))

    # 手算：99999.5*2 + 99950*3 + 99900*1 = 199999 + 299850 + 99900
    assert wide.bid_notional == Decimal("599749.0")
    # 100000.5*1 + 100010*2.5 + 100100*0.5 = 100000.5 + 250025 + 50050
    assert wide.ask_notional == Decimal("400075.5")
    assert wide.levels_used == 6                # 两侧各 3 档，脏档不算
    assert wide.symbol == "BTC" and wide.band_bp == 10.0
    assert wide.ts_ms == 1754450974231
    assert type(wide.bid_notional) is Decimal   # 全程 Decimal，一步都不经过 float
    # 一档信息与 fetch_book_top 同口径
    assert wide.bid_price == Decimal("99999.5") and wide.bid_qty == Decimal("2")
    assert wide.ask_price == Decimal("100000.5") and wide.ask_qty == Decimal("1")
    # 深度必须比一档大得多，否则这个方法白加了
    assert wide.bid_notional > wide.bid_price * wide.bid_qty

    # 1bp：lo = 99990、hi = 100010 —— 买侧只剩一档，卖侧剩两档（100010 压在边界上）
    assert narrow.bid_notional == Decimal("99999.5") * 2
    assert narrow.ask_notional == Decimal("100000.5") + Decimal("100010.0") * Decimal("2.5")
    assert narrow.levels_used == 3
    assert narrow.bid_notional < wide.bid_notional      # 带宽变窄，深度只能变小


def test_book_depth_reports_short_book_honestly_without_extrapolating():
    """簿盖不满带宽时如实返回能统计到的量和档数——宁可低估也绝不外推。"""
    rec = Recorder({"l2Book": SHORT_BOOK})
    with adapter(rec) as ad:
        wide = run(ad.fetch_book_depth("BTC", band_bp=100.0))
        narrow = run(ad.fetch_book_depth("BTC", band_bp=10.0))
    assert wide.levels_used == 3
    assert wide.bid_notional == Decimal("99999.5") * 2 + Decimal("99990.0") * 1
    assert wide.ask_notional == Decimal("100000.5")
    # 带宽扩大 10 倍但簿没变 → 深度必须原地不动（外推的话这里会膨胀）
    assert narrow.bid_notional == wide.bid_notional
    assert narrow.levels_used == wide.levels_used


def test_book_depth_k_prefix_notional_is_multiplier_invariant():
    """名义额与量纲无关（价除量乘正好抵消），只有对外报的那一档价/量要换算。"""
    rec = Recorder({"l2Book": KPEPE_BOOK})
    with adapter(rec) as ad:
        d = run(ad.fetch_book_depth("kPEPE", band_bp=100.0))
    assert d.bid_notional == Decimal("0.010000") * Decimal("5000")     # = 50 USDC
    assert d.ask_notional == Decimal("0.010010") * Decimal("4000")
    assert d.bid_price == Decimal("0.010000") / 1000
    assert d.bid_qty == Decimal("5000") * 1000
    assert d.levels_used == 2


def test_book_depth_rejects_missing_levels_and_empty_side_and_bad_band():
    """响应缺 levels **必须抛错**，不能退化成深度 0——scoring 里 depth_cap<=0 会跳过
    容量判断，等于把"没数据"读成"深度无限"。

    band_bp <= 0 本地就拒，**且不发请求**。
    """
    rec = Recorder({"l2Book": {"coin": "BTC", "time": 1}})            # 没有 levels
    with adapter(rec) as ad:
        with pytest.raises(ValueError):
            run(ad.fetch_book_depth("BTC", band_bp=0.0))
        assert not rec.requests, "算不出带就别浪费一次请求"
        with pytest.raises(ValueError):
            run(ad.fetch_book_depth("BTC", band_bp=-5.0))
        with pytest.raises(ExchangeError) as ei:
            run(ad.fetch_book_depth("BTC"))
        assert ei.value.code == "BAD_BOOK" and ei.value.retriable

    # levels 长度不为 2 同样是坏簿
    rec2 = Recorder({"l2Book": {"coin": "BTC", "time": 1, "levels": [[]]}})
    with adapter(rec2) as ad2:
        with pytest.raises(ExchangeError) as ei2:
            run(ad2.fetch_book_top("BTC"))
        assert ei2.value.code == "BAD_BOOK"

    # 单边空簿算不出中价，这一拍不可用；绝不返回 0 价
    rec3 = Recorder({"l2Book": {"coin": "BTC", "time": 1,
                                "levels": [[{"px": "99999.5", "sz": "2"}], []]}})
    with adapter(rec3) as ad3:
        for call in (lambda: ad3.fetch_book_top("BTC"),
                     lambda: ad3.fetch_book_depth("BTC")):
            with pytest.raises(ExchangeError) as ei3:
                run(call())
            assert ei3.value.code == "EMPTY_BOOK" and ei3.value.retriable


def test_book_depth_uses_the_market_quota():
    """l2Book 权重 2，和一档轮询打的是同一个端点 → 必须和行情共池，
    绝不能挪去 risk/trade 抢平仓配额。"""
    rec = Recorder({"l2Book": BTC_BOOK})

    async def scenario(ad):
        for _ in range(8):                       # 抽干 market 池
            await ad._quota["market"].acquire()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ad.fetch_book_depth("BTC"), timeout=0.2)
        for _ in range(8):
            ad._quota["market"].release()

    with adapter(rec) as ad:
        run(scenario(ad))


def test_ws_bbo_parsing_handles_null_side_and_unknown_coin():
    """bbo 的两侧**任一侧可能是 null**（单边空簿），直接下标解引用会炸。
    合约表里没有的 coin 同样丢弃——宁可少一拍行情，也不能按倍数 1 猜一个 k 币的价。
    （只测解析，不建真连接。）"""
    rec = Recorder({})
    with adapter(rec) as ad:
        top = ad._parse_bbo({"coin": "BTC", "time": 1754450974231,
                             "bbo": [{"px": "99999.5", "sz": "2", "n": 3},
                                     {"px": "100000.5", "sz": "1", "n": 2}]})
        assert top.bid_price == Decimal("99999.5") and top.ts_ms == 1754450974231
        ktop = ad._parse_bbo({"coin": "kPEPE", "time": 1,
                              "bbo": [{"px": "0.01", "sz": "5000"},
                                      {"px": "0.0101", "sz": "4000"}]})
        assert ktop.bid_price == Decimal("0.01") / 1000
        assert ktop.bid_qty == Decimal("5000") * 1000
        assert ad._parse_bbo({"coin": "BTC", "bbo": [None, {"px": "1", "sz": "1"}]}) is None
        assert ad._parse_bbo({"coin": "NOPE", "bbo": [{"px": "1", "sz": "1"},
                                                      {"px": "2", "sz": "1"}]}) is None
        assert ad._parse_bbo({"coin": "BTC"}) is None
        assert ad._parse_bbo(None) is None
    assert not rec.requests


# =========================================================================
# 下单
# =========================================================================

def test_place_order_wire_fields_and_deterministic_cloid():
    """order wire 的七个字段按文档 placeOrder 节映射，cloid 是 client_order_id 的
    **确定性**映射（cancel / query 要能从同一个字符串再算出同一个 cloid）。"""
    rec = Recorder({"order": PLACE_RESTING})
    with adapter(rec) as ad:
        res = run(ad.place_order(order_req()))
    wire = rec.last_body["action"]["orders"][0]
    assert wire["a"] == 0                       # asset index 是整数下标，不是币名
    assert wire["b"] is True                    # isBuy
    assert wire["p"] == "100000"                # 字符串，无尾随零、无科学计数法
    assert wire["s"] == "0.55"
    assert wire["r"] is False
    assert wire["t"] == {"limit": {"tif": "Ioc"}}
    assert wire["c"] == HyperliquidAdapter.to_cloid("cf-leg1-0001")
    assert wire["c"] == HyperliquidAdapter.to_cloid("cf-leg1-0001")   # 确定性
    assert wire["c"].startswith("0x") and len(wire["c"][2:]) == 32
    assert rec.last_body["action"]["grouping"] == "na"
    # resting 只有 oid：状态 new、成交 0
    assert res.client_order_id == "cf-leg1-0001"        # 回传调用方原值，不是 cloid
    assert res.exchange_order_id == "77738308"
    assert res.status == "new" and not res.is_terminal
    assert res.filled_qty == Decimal("0")


def test_place_order_requires_a_client_order_id():
    """无 ID 下单一律拒绝：消解未知状态全靠它。空串就地抛且**不发请求**。"""
    rec = Recorder({})
    with adapter(rec) as ad:
        with pytest.raises(ValueError):
            run(ad.place_order(order_req(client_order_id="")))
        with pytest.raises(ValueError):
            HyperliquidAdapter.to_cloid("")
    assert not rec.requests


def test_place_order_filled_and_partially_filled_ioc():
    """filled 分支一次拿全 OrderResult；IOC 部成之后订单已终结，
    报成 new 会让上层一直等一个永远不会来的成交。"""
    rec = Recorder({"order": PLACE_FILLED})
    with adapter(rec) as ad:
        res = run(ad.place_order(order_req()))
    assert res.status == "filled" and res.is_terminal
    assert res.filled_qty == Decimal("0.55")
    assert res.avg_price == Decimal("100000.5")
    assert res.exchange_order_id == "77747314"
    # filled 分支**不带手续费**，留 0 而不是猜一个数
    assert res.fee == Decimal("0") and res.fee_asset == "USDC"

    partial = {"status": "ok", "response": {"type": "order", "data": {"statuses": [
        {"filled": {"totalSz": "0.20", "avgPx": "100000.5", "oid": 77747315}}]}}}
    rec2 = Recorder({"order": partial})
    with adapter(rec2) as ad2:
        res2 = run(ad2.place_order(order_req()))
    assert res2.filled_qty == Decimal("0.20")
    assert res2.status == "canceled" and res2.is_terminal


def test_place_order_converts_k_prefixed_units_on_the_wire():
    """入参是 base 币口径（PEPE），上线必须是原生口径（kPEPE）：量除 1000、价乘 1000。

    换算封死在适配器内部——上层拿到/给出的价永远是"每个 PEPE"、量永远是"多少个 PEPE"。
    """
    rec = Recorder({"order": PLACE_RESTING})
    with adapter(rec) as ad:
        run(ad.place_order(order_req(symbol="kPEPE", side="sell",
                                     qty=Decimal("5000000"),
                                     price=Decimal("0.00001"),
                                     client_order_id="cf-k")))
    wire = rec.last_body["action"]["orders"][0]
    assert wire["a"] == 2
    assert wire["b"] is False
    assert wire["s"] == "5000"       # 5,000,000 PEPE = 5000 kPEPE
    assert wire["p"] == "0.01"       # 每 PEPE 0.00001 = 每 kPEPE 0.01


def test_place_order_blocks_pinched_and_sub_ten_dollar_orders_locally():
    """取整后归零、以及名义额 < $10 的单子本地就拒——别拿一次 RTT + 一次 action 配额
    去换一个必然的 MinTradeNtl（地址维度 action 配额初始只有 10000 次，烧不起）。

    reduce_only 放行：**UNVERIFIED** $10 下限对平仓单是否豁免，而拦掉平仓单的后果
    （残仓平不掉）比白跑一次请求严重得多。
    """
    rec = Recorder({"order": PLACE_RESTING})
    with adapter(rec) as ad:
        with pytest.raises(OrderRejected) as ei:
            run(ad.place_order(order_req(qty=Decimal("0.000001"))))
        assert ei.value.code == "PINCHED"
        assert not rec.requests

        with pytest.raises(OrderRejected) as ei2:
            run(ad.place_order(order_req(qty=Decimal("0.00005"))))   # 5 USDC
        assert ei2.value.code == "MIN_NOTIONAL"
        assert not rec.requests

        run(ad.place_order(order_req(qty=Decimal("0.00005"), reduce_only=True)))
    assert len(rec.requests) == 1
    assert rec.last_body["action"]["orders"][0]["r"] is True


def test_market_order_becomes_ioc_with_a_protective_limit_price():
    """HL **没有市价单类型**："market" 是前端行为，必须自己从盘口算一个足够穿透的
    限价配 Ioc。**不能发空价**（会被整批预校验拒掉）。"""
    rec = Recorder({"l2Book": BTC_BOOK, "order": PLACE_FILLED})
    with adapter(rec) as ad:
        run(ad.place_order(order_req(price=None, time_in_force="GTC")))
    wire = rec.last_body["action"]["orders"][0]
    assert wire["t"] == {"limit": {"tif": "Ioc"}}, "price=None 必须强制 Ioc"
    assert Decimal(wire["p"]) > Decimal("100000.5"), "买单要穿透卖一"
    assert Decimal(wire["p"]) < Decimal("100000.5") * Decimal("1.02")


def test_time_in_force_maps_to_hyperliquid_tif():
    """tif 只有 Alo(post-only) / Ioc / Gtc 三种，FOK 在这里做不到，退化成 Ioc。"""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["action"]["orders"][0]["t"])
        return json_response(PLACE_RESTING)

    rec = Recorder({"order": handler})
    with adapter(rec) as ad:
        for tif in ("IOC", "FOK", "GTC", "GTX", "POST_ONLY", "什么鬼"):
            run(ad.place_order(order_req(time_in_force=tif)))
    assert [t["limit"]["tif"] for t in seen] == \
        ["Ioc", "Ioc", "Gtc", "Alo", "Alo", "Ioc"]


def test_timeout_and_connection_error_raise_order_unknown():
    """下单超时/连接中断**必须**抛 OrderUnknown。

    nonce 不是去重键，当成失败返回会导致重发 → 双倍仓位。

    这里四个异常走的是**两条不同的代码路径**：base._request 只包住
    (TimeoutException, NetworkError) 两族并翻成 ExchangeError("NETWORK")，
    而 RemoteProtocolError 是 TransportError 却**不是** NetworkError——它一路穿到
    place_order 自己的 except httpx.TransportError 分支。逐个列异常就会漏掉这一支，
    漏一个就是一次裸奔的双倍仓位，所以两条路都要压住。
    """
    for exc in (httpx.ReadTimeout, httpx.ConnectError, httpx.WriteError,
                httpx.RemoteProtocolError):
        def handler(request: httpx.Request, e=exc) -> httpx.Response:
            raise e("simulated", request=request)

        rec = Recorder({"order": handler})
        with adapter(rec) as ad:
            with pytest.raises(OrderUnknown) as ei:
                run(ad.place_order(order_req()))
        assert ei.value.client_order_id == "cf-leg1-0001"
        assert ei.value.venue == "hyperliquid"


def test_server_error_and_unreadable_body_are_order_unknown():
    """5xx 和"200 但读不出 JSON"（WAF 注入 HTML）都是"可能已进撮合"，
    不能当明确失败——那等于发了一张"可安全重发"的许可证。"""
    rec = Recorder({"order": lambda r: json_response({"x": 1}, status=500)})
    with adapter(rec) as ad:
        with pytest.raises(OrderUnknown):
            run(ad.place_order(order_req()))

    rec2 = Recorder({"order": lambda r: httpx.Response(200, content=b"<html>waf</html>")})
    with adapter(rec2) as ad2:
        with pytest.raises(OrderUnknown):
            run(ad2.place_order(order_req()))

    # 200 但读不到 statuses 数组：结构不认识 = 不知道有没有下进去
    rec3 = Recorder({"order": {"status": "ok", "response": {"type": "order",
                                                            "data": {}}}})
    with adapter(rec3) as ad3:
        with pytest.raises(OrderUnknown):
            run(ad3.place_order(order_req()))

    # 认不出的整批错误串也倒向未知侧（多跑一轮 cancel+查单只花一次 RTT）
    rec4 = Recorder({"order": {"status": "err",
                               "response": "Something brand new nobody has seen"}})
    with adapter(rec4) as ad4:
        with pytest.raises(OrderUnknown):
            run(ad4.place_order(order_req()))


def test_per_order_error_hides_inside_status_ok():
    """**{"error": ...} 藏在 status:"ok" 的响应里**：只看 HTTP 码或只看外层 status
    会把"订单被拒"当成"下单成功"，然后引擎按一个不存在的仓位继续加仓。"""
    for message in ("Order must have minimum value of $10.",
                    "Price must be divisible by tick size.",
                    "Insufficient margin to place order.",
                    "Reduce only order would increase position.",
                    "Order price too far from oracle",
                    "Order would cause position to exceed margin tier limit "
                    "at current leverage"):
        rec = Recorder({"order": {"status": "ok", "response": {
            "type": "order", "data": {"statuses": [{"error": message}]}}}})
        with adapter(rec) as ad:
            with pytest.raises(OrderRejected) as ei:
                run(ad.place_order(order_req()))
        assert ei.value.retriable is False
        assert not isinstance(ei.value, OrderUnknown)


def test_ioc_cancel_is_a_normal_result_not_a_fault():
    """IocCancel（一点都没吃到）在 500ms 收敛循环里会频繁出现——它是
    **正常结果不是故障**，映射成 OrderRejected 让上层当"这一拍没成交"处理。"""
    rec = Recorder({"order": {"status": "ok", "response": {"type": "order", "data": {
        "statuses": [{"error": "Order could not immediately match against any "
                               "resting orders."}]}}}})
    with adapter(rec) as ad:
        with pytest.raises(OrderRejected) as ei:
            run(ad.place_order(order_req()))
    assert ei.value.code == "ORDER_REJECTED"


def test_signature_and_422_errors_are_never_upgraded_to_unknown():
    """签名/凭据错发生在**进撮合之前**，订单确定不存在。

    这两条 99% 是签名构造错了（msgpack 键顺序、source a/b、地址大小写、数字尾随零），
    升级成 OrderUnknown 只会白跑一轮 cancel+查单，烧掉本来就紧张的 action 配额。
    """
    cases = [
        ({"status": "err", "response":
          "L1 error: User or API Wallet 0x0123... does not exist."}, 200, "SIGNATURE"),
        ({"status": "err", "response": "Must deposit before performing actions."},
         200, "SIGNATURE"),
        (None, 422, "HTTP_422"),
    ]
    for payload, status, code in cases:
        if payload is None:
            handler = (lambda r: httpx.Response(
                422, text="Failed to deserialize the JSON body into the target type"))
        else:
            handler = (lambda r, p=payload: json_response(p, status=status))
        rec = Recorder({"order": handler})
        with adapter(rec) as ad:
            with pytest.raises(ExchangeError) as ei:
                run(ad.place_order(order_req()))
        assert ei.value.code == code
        assert ei.value.retriable is False
        assert not isinstance(ei.value, OrderUnknown)


def test_rate_limited_maps_to_rate_limited_not_unknown():
    """限频 = **确定没进撮合**，是下单路径上唯一可以直接抛、允许上层安全重试的错。"""
    rec = Recorder({"order": lambda r: httpx.Response(429, text="rate limited")})
    with adapter(rec) as ad:
        with pytest.raises(RateLimited) as ei:
            run(ad.place_order(order_req()))
    assert ei.value.code == "HTTP_429" and ei.value.retriable is True
    assert not isinstance(ei.value, OrderUnknown)

    # 行情路径上的 429 同样是 RateLimited（IP 维度 1200 权重/分钟对 info 也生效）
    rec2 = Recorder({"metaAndAssetCtxs": lambda r: httpx.Response(429, text="slow down")})
    with adapter(rec2) as ad2:
        with pytest.raises(RateLimited):
            run(ad2.fetch_carry_rates())

    # 200 + status:"err" 里的限频串也要认出来
    rec3 = Recorder({"order": {"status": "err", "response": "Too many requests"}})
    with adapter(rec3) as ad3:
        with pytest.raises(RateLimited) as ei3:
            run(ad3.place_order(order_req()))
    assert ei3.value.code == "RATE_LIMITED"


def test_place_order_uses_the_trade_quota():
    """下单走 trade 池，不和行情/风控抢。"""
    rec = Recorder({"order": PLACE_RESTING})

    async def scenario(ad):
        for _ in range(6):
            await ad._quota["trade"].acquire()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ad.place_order(order_req()), timeout=0.2)
        for _ in range(6):
            ad._quota["trade"].release()

    with adapter(rec) as ad:
        run(scenario(ad))


# =========================================================================
# 撤单 / 查单 / 消解
# =========================================================================

def test_cancel_uses_cancel_by_cloid_with_full_key_names():
    """cancelByCloid 的键名是全名 asset / cloid，而按 oid 撤的 cancel 用缩写 a / o
    ——**两者故意不一样**（官方 SDK 的实际写法），别自作主张统一：
    键名变了 msgpack 字节就变了，签名跟着失效。

    **UNVERIFIED**：gitbook 示例里的 "f"（快速撤单）字段官方 SDK 两处都不发，照 SDK 来。
    """
    rec = Recorder({"cancelByCloid": CANCEL_OK})
    with adapter(rec) as ad:
        run(ad.cancel_order("BTC", "cf-leg1-0001"))
    action = rec.last_body["action"]
    assert list(action.keys()) == ["type", "cancels"]
    assert action["type"] == "cancelByCloid"
    assert action["cancels"] == [{"asset": 0,
                                  "cloid": HyperliquidAdapter.to_cloid("cf-leg1-0001")}]
    assert "f" not in action and "f" not in action["cancels"][0]
    assert "a" not in action["cancels"][0] and "o" not in action["cancels"][0]
    assert recover_signer(rec.last_body) == AGENT_ADDRESS


def test_cancel_swallows_missing_order_and_string_success():
    """statuses[i] **类型不统一**：成功是裸字符串 "success"，失败是对象 {"error": ...}。
    直接 st["error"] 会在成功路径上对 str 做下标索引。

    撞上"已终结"就是撤单的目的已经达到——吞掉，让消解流程继续去查成交量。
    """
    rec = Recorder({"cancelByCloid": CANCEL_OK})
    with adapter(rec) as ad:
        run(ad.cancel_order("BTC", "cf-leg1-0001"))          # 不抛

    missing = {"status": "ok", "response": {"type": "cancel", "data": {"statuses": [
        {"error": "Order was never placed, already canceled, or filled."}]}}}
    rec2 = Recorder({"cancelByCloid": missing})
    with adapter(rec2) as ad2:
        run(ad2.cancel_order("BTC", "cf-leg1-0001"))          # 同样不抛


def test_query_order_treats_unknown_outer_status_as_missing():
    """**外层 status != "order" 一律当"订单不存在" → OrderRejected**
    （**UNVERIFIED**：确切取值推测是 "unknownOid"）。
    resolve_unknown_order 靠它判定"这单从未被接受"并返回 0。"""
    # 注意不要拿 {"status": "err"} 当样例：那是 /exchange 的信封形状，
    # _parse 会先把它当整批预校验失败翻译掉，根本走不到 query_order 的判定。
    for payload in ({"status": "unknownOid"}, {"status": "unknownOrder"}, {}):
        rec = Recorder({"orderStatus": payload})
        with adapter(rec) as ad:
            with pytest.raises(OrderRejected) as ei:
                run(ad.query_order("BTC", "cf-leg1-0001"))
        assert ei.value.code == "MISSING_ORDER"
    # 请求把 cloid 塞进 oid 这个键，没有 cloid 请求键
    body = rec.sent("orderStatus")[0]
    assert body == {"type": "orderStatus", "user": ACCOUNT_ADDRESS,
                    "oid": HyperliquidAdapter.to_cloid("cf-leg1-0001")}
    assert "cloid" not in body


def test_query_order_computes_filled_from_orig_minus_remaining():
    """orderStatus 的 sz 是**剩余量**、origSz 是原始量 → filled = origSz − sz。
    均价和手续费它给不出来，要按 oid 去 userFillsByTime 聚合（best-effort）。"""
    rec = Recorder({"orderStatus": ORDER_STATUS_FILLED, "userFillsByTime": USER_FILLS})
    with adapter(rec) as ad:
        res = run(ad.query_order("BTC", "cf-leg1-0001"))
    assert res.status == "filled" and res.is_terminal
    assert res.filled_qty == Decimal("0.0076")
    assert res.exchange_order_id == "90542681"
    # Σ(px*sz)/Σsz，只算同一个 oid 的行（USER_FILLS 里混了一条别的单）
    total_sz = Decimal("0.0036") + Decimal("0.004")
    expect = (Decimal("18.435") * Decimal("0.0036")
              + Decimal("18.5") * Decimal("0.004")) / total_sz
    assert res.avg_price == expect
    assert res.fee == Decimal("0.03") and res.fee_asset == "USDC"   # 正数 = 你付
    assert res.client_order_id == "cf-leg1-0001"                    # 不是 cloid


def test_query_order_never_reports_an_unknown_status_as_canceled():
    """内层 status 文档只给了 6 个、明说还有 "15+ other status values"（**UNVERIFIED**）。

    未知值一律兜底成**非终结的 new**（有成交量就升成 partial）——把不认识的状态
    当成 canceled 会让引擎以为没成交然后重下 = 双倍仓位。
    """
    def status_payload(inner: str, sz: str) -> dict:
        order = dict(ORDER_STATUS_FILLED["order"]["order"], sz=sz)
        return {"status": "order", "order": {"order": order, "status": inner,
                                             "statusTimestamp": 1}}

    cases = {
        "open": ("new", "0.0076"),
        "filled": ("filled", "0.0"),
        "canceled": ("canceled", "0.0076"),
        "rejected": ("rejected", "0.0076"),
        "marginCanceled": ("canceled", "0.0076"),
        "waitingForTrigger": ("new", "0.0076"),      # 文档没给的第 7+ 种
        "somethingBrandNew": ("partial", "0.0"),     # 未知 + 有成交 → partial
    }
    for inner, (expect, sz) in cases.items():
        rec = Recorder({"orderStatus": status_payload(inner, sz),
                        "userFillsByTime": []})
        with adapter(rec, fetch_fill_prices=False) as ad:
            res = run(ad.query_order("BTC", "cf-leg1-0001"))
        assert res.status == expect, (inner, res.status)
        if inner not in ("canceled", "marginCanceled"):
            assert res.status != "canceled"


def test_fill_summary_failure_never_breaks_filled_qty():
    """userFillsByTime 是查单路径上的第二次请求（权重 20 + 每 20 条加权），
    失败绝不能影响 filled_qty 这个真正重要的数。"""
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("fills down", request=request)

    rec = Recorder({"orderStatus": ORDER_STATUS_FILLED, "userFillsByTime": boom})
    with adapter(rec) as ad:
        res = run(ad.query_order("BTC", "cf-leg1-0001"))
    assert res.filled_qty == Decimal("0.0076")
    assert res.avg_price == Decimal("0") and res.fee == Decimal("0")


def test_resolve_unknown_order_returns_zero_when_the_order_never_existed():
    """OrderUnknown 的消解流程：cancel 终结 → 查单。
    交易所连订单都不认识 = 这单从未被接受 → 成交量 0。"""
    rec = Recorder({"cancelByCloid": CANCEL_OK, "orderStatus": {"status": "unknownOid"}})
    with adapter(rec) as ad:
        assert run(ad.resolve_unknown_order("BTC", "cf-leg1-0001")) == Decimal("0")

    # 真成交的那条要读出实际成交量，绝不能也报 0
    rec2 = Recorder({"cancelByCloid": CANCEL_OK, "orderStatus": ORDER_STATUS_FILLED,
                     "userFillsByTime": USER_FILLS})
    with adapter(rec2) as ad2:
        assert run(ad2.resolve_unknown_order("BTC", "cf-leg1-0001")) == Decimal("0.0076")


@pytest.mark.parametrize("route", [
    lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("read timeout")),
    lambda r: json_response({"x": 1}, status=500),
])
def test_risk_path_errors_are_catchable_by_the_resolver(route):
    """base._request 把传输层异常包成 ExchangeError("NETWORK")，而
    base.resolve_unknown_order 只认 (OrderRejected, httpx.HTTPError, RateLimited)
    ——不转一手的话，网络抖动会让消解流程在最危险的时刻带着未捕获异常崩掉。"""
    rec = Recorder({"cancelByCloid": route})
    with adapter(rec) as ad:
        with pytest.raises(RateLimited) as ei:
            run(ad.cancel_order("BTC", "cf-leg1-0001"))
        assert ei.value.retriable is True

    rec2 = Recorder({"orderStatus": route})
    with adapter(rec2) as ad2:
        with pytest.raises(RateLimited):
            run(ad2.query_order("BTC", "cf-leg1-0001"))


# =========================================================================
# 持仓 / 挂单 / 杠杆 / 配额
# =========================================================================

def test_positions_are_signed_coin_amounts_with_derived_mark():
    """szi 是**有符号的 base 币数量**（正=多、负=空），直接对应 Position.qty。

    响应里**没有 markPx**：positionValue / |szi| 就是 HL 自己按 mark 算的名义额，
    等价且省一次请求。liquidationPx 为 null 时必须映射成 None——填 0 会让
    liquidation_distance 算出"还有 100% 空间"，风控直接失明。
    """
    rec = Recorder({"clearinghouseState": CLEARINGHOUSE})
    with adapter(rec) as ad:
        poss = {p.symbol: p for p in run(ad.fetch_positions())}
    btc = poss["BTC"]
    assert btc.qty == Decimal("-0.0335") and btc.qty < 0          # 空头为负
    assert btc.entry_price == Decimal("2986.3")
    assert btc.mark_price == Decimal("100.02765") / Decimal("0.0335")
    assert btc.liquidation_price == Decimal("2866.26936529")
    assert btc.leverage == Decimal("20")
    assert btc.market == "hyperliquid:perp"

    kpepe = poss["kPEPE"]
    assert kpepe.qty == Decimal("1000") * 1000       # 1000 kPEPE = 1,000,000 PEPE
    assert kpepe.entry_price == Decimal("0.01") / 1000
    assert kpepe.liquidation_price is None           # null → None，绝不是 0
    body = rec.sent("clearinghouseState")[0]
    assert body["user"] == ACCOUNT_ADDRESS           # 账户地址，不是 agent 地址
    assert body["dex"] == ""


def test_positions_fill_zero_for_symbols_the_api_omits():
    """assetPositions 里**只有非零仓位**，平掉的币不出现。
    传了 symbols 就必须给未出现的符号补零仓——只返回 API 给的那几条会让上层
    把"已平"读成"读不到"。"""
    rec = Recorder({"clearinghouseState": CLEARINGHOUSE})
    with adapter(rec) as ad:
        poss = {p.symbol: p for p in run(ad.fetch_positions(["BTC", "HYPE"]))}
    assert set(poss) == {"BTC", "HYPE"}              # kPEPE 没要就不返回
    assert poss["HYPE"].is_flat and poss["HYPE"].qty == Decimal("0")
    assert poss["HYPE"].liquidation_price is None


def test_positions_use_the_risk_quota():
    """撤单/查仓/查挂单必须走 risk 配额，不能和行情轮询抢。"""
    rec = Recorder({"clearinghouseState": CLEARINGHOUSE,
                    "metaAndAssetCtxs": [META, CTXS]})

    async def scenario(ad):
        for _ in range(6):
            await ad._quota["risk"].acquire()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ad.fetch_positions(), timeout=0.2)
        # 正向对照：同一时刻 market 配额还在，行情照常
        rates = await asyncio.wait_for(ad.fetch_carry_rates(["BTC"]), timeout=0.5)
        assert rates and rates[0].symbol == "BTC"
        for _ in range(6):
            ad._quota["risk"].release()

    with adapter(rec) as ad:
        run(scenario(ad))


def test_open_orders_use_frontend_endpoint_for_the_cloid():
    """**必须用 frontendOpenOrders 而不是 openOrders**：后者没有 cloid，
    而本项目一切按 client_order_id 索引，拿不到 cloid 的挂单等于查不到。

    sz 是剩余量、origSz 是原始量 → filled = origSz − sz。
    """
    cloid = HyperliquidAdapter.to_cloid("cf-leg2-0002")
    rows = [dict(FRONTEND_OPEN_ORDERS[0]),
            dict(FRONTEND_OPEN_ORDERS[1], cloid=cloid),
            dict(FRONTEND_OPEN_ORDERS[2])]
    rec = Recorder({"frontendOpenOrders": rows})
    with adapter(rec) as ad:
        ad._remember_cloid(cloid, "cf-leg2-0002")
        orders = run(ad.fetch_open_orders("BTC"))
    assert len(orders) == 2, "别的币必须滤掉"
    assert orders[0].status == "new" and orders[0].filled_qty == Decimal("0")
    assert orders[0].exchange_order_id == "91490942"
    assert orders[1].status == "partial"
    assert orders[1].filled_qty == Decimal("3.0")        # 5.0 − 2.0
    assert orders[1].client_order_id == "cf-leg2-0002"   # cloid 翻回上层认识的 id
    assert rec.sent("frontendOpenOrders")[0] == {
        "type": "frontendOpenOrders", "user": ACCOUNT_ADDRESS, "dex": ""}


def test_set_leverage_sends_an_integer_and_rejects_fractions():
    """键顺序 type → asset → isCross → leverage，leverage 是**整数**。

    非整数入参拒绝而不是静默取整——静默取整会让"我设了 3.5 倍"和"实际 3 倍"悄悄分叉。
    """
    rec = Recorder({"updateLeverage": LEVERAGE_OK})
    with adapter(rec) as ad:
        run(ad.set_leverage("BTC", Decimal("5")))
        action = rec.last_body["action"]
        assert list(action.keys()) == ["type", "asset", "isCross", "leverage"]
        assert action == {"type": "updateLeverage", "asset": 0,
                          "isCross": True, "leverage": 5}
        assert isinstance(action["leverage"], int)
        assert recover_signer(rec.last_body) == AGENT_ADDRESS
        with pytest.raises(ValueError):
            run(ad.set_leverage("BTC", Decimal("3.5")))
    assert len(rec.requests) == 1

    rec2 = Recorder({"updateLeverage": LEVERAGE_OK})
    with adapter(rec2, cross_margin=False) as ad2:
        run(ad2.set_leverage("BTC", Decimal("10")))
    assert rec2.last_body["action"]["isCross"] is False


def test_action_quota_is_readable_for_self_monitoring():
    """地址维度限频**只对 action 生效**：初始白送 10000 次，触顶后每 10 秒 1 次
    ——那等于失去及时平仓能力，是安全问题不是性能问题。"""
    payload = {"cumVlm": "123.4", "nRequestsUsed": 9990, "nRequestsCap": 10000}
    rec = Recorder({"userRateLimit": payload})
    with adapter(rec) as ad:
        quota = run(ad.fetch_action_quota())
        assert quota["nRequestsUsed"] == 9990 and quota["nRequestsCap"] == 10000
        assert ad.action_quota == quota
    assert rec.sent("userRateLimit")[0] == {"type": "userRateLimit",
                                            "user": ACCOUNT_ADDRESS}


def test_server_time_falls_back_to_l2book_when_exchange_status_is_odd():
    """**UNVERIFIED**：exchangeStatus 的 {specialStatuses, time} 形状来自
    QuickNode 镜像文档，gitbook 正文没给示例 → 必须有 l2Book.time 的退路。"""
    rec = Recorder({"exchangeStatus": {"specialStatuses": [], "time": 1754450974231}})
    with adapter(rec) as ad:
        assert run(ad.fetch_server_time_ms()) == 1754450974231

    rec2 = Recorder({"exchangeStatus": {"specialStatuses": []},   # 没有 time
                     "l2Book": BTC_BOOK})
    with adapter(rec2) as ad2:
        assert run(ad2.fetch_server_time_ms()) == 1754450974231
    assert rec2.sent("l2Book")[0]["coin"] == "BTC"


def test_missing_account_address_fails_loudly_instead_of_returning_empty():
    """仓位是真值：地址没配/配错时必须就地报错。

    静默返回空仓 = 引擎判定裸奔 = 触发撤退平仓，这是纯配置错误里代价最高的一个。
    """
    rec = Recorder({})
    with adapter(rec, credential=CRED_PUBLIC) as ad:
        with pytest.raises(ExchangeError) as ei:
            run(ad.fetch_positions())
        assert ei.value.code == "NO_ACCOUNT_ADDRESS"
    with adapter(rec, credential=Credential("not-an-address", "")) as ad2:
        with pytest.raises(ExchangeError) as ei2:
            run(ad2.fetch_positions())
        assert ei2.value.code == "BAD_ACCOUNT_ADDRESS"
    assert not rec.requests


# =========================================================================
# 缺 eth_account / msgpack
# =========================================================================

@contextlib.contextmanager
def signing_deps_unavailable():
    """真的把三个签名依赖从 import 系统里拿掉，然后重新 import 本模块。

    比直接改 _SIGN_IMPORT_ERROR 更狠一档：它同时证明**模块级 import 本身**
    不会炸——public_feed.open_public_adapters 用 Credential("", "") 构造这家，
    构造失败等于 Hyperliquid 从机会榜上整片消失，只在 init_errors 里留一行。
    """
    blocked = ("msgpack", "eth_account", "eth_utils")
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name.split(".")[0] in blocked:
            raise ImportError(f"No module named '{name.split('.')[0]}'")
        return real_import(name, globals, locals, fromlist, level)

    saved = {k: v for k, v in sys.modules.items() if k.split(".")[0] in blocked}
    for key in saved:
        del sys.modules[key]
    builtins.__import__ = fake_import
    try:
        importlib.reload(HL)
        yield
    finally:
        builtins.__import__ = real_import
        sys.modules.update(saved)
        importlib.reload(HL)        # 还原，后面的用例还要签名


def test_module_and_adapter_survive_without_signing_deps():
    """缺包时：import 不炸、适配器构造得出来、**全部 /info 公开端点照常**。"""
    with signing_deps_unavailable():
        assert HL._SIGN_IMPORT_ERROR is not None
        assert HL._msgpack is None and HL._Account is None

        rec = Recorder({"meta": META, "metaAndAssetCtxs": [META, CTXS],
                        "l2Book": BTC_BOOK, "userFees": USER_FEES,
                        "clearinghouseState": CLEARINGHOUSE,
                        "frontendOpenOrders": FRONTEND_OPEN_ORDERS,
                        "fundingHistory": []})
        client = httpx.AsyncClient(transport=httpx.MockTransport(rec))
        ad = HL.HyperliquidAdapter(CRED, broker_code="", client=client)
        ad._clock.synced_at = time.time()
        try:
            assert ad.signing_available is False       # UI 据此显示"只读模式"
            insts = run(ad.fetch_instruments())
            assert [i.symbol for i in insts] == ["BTC", "kPEPE", "HYPE"]
            assert run(ad.fetch_carry_rates())[0].rate == Decimal("0.0000125")
            assert run(ad.fetch_book_top("BTC")).bid_price == Decimal("99999.5")
            assert run(ad.fetch_book_depth("BTC", 10.0)).levels_used == 6
            assert run(ad.fetch_fee_schedule(()))[0].taker == Decimal("0.000315")
            assert run(ad.fetch_funding_limits("BTC")) == (Decimal("0.04"),
                                                           Decimal("-0.04"))
            assert run(ad.fetch_positions(["BTC"]))[0].qty == Decimal("-0.0335")
            assert run(ad.fetch_open_orders("BTC"))
        finally:
            run(ad.close())

    # 还原之后签名能力必须回来，否则后面的用例全是假绿
    assert HL._SIGN_IMPORT_ERROR is None


def test_write_paths_raise_a_clear_error_without_signing_deps():
    """三条写路径报 SIGNING_UNAVAILABLE 而不是 ImportError——后者会一路穿过
    except ExchangeError 的分流逻辑被当成"未知错误"。**且一个请求都不发。**"""
    with signing_deps_unavailable():
        rec = Recorder({})
        client = httpx.AsyncClient(transport=httpx.MockTransport(rec))
        ad = HL.HyperliquidAdapter(CRED, client=client)
        ad._clock.synced_at = time.time()
        ad._load_universe(META)
        try:
            for call in (lambda: ad.place_order(order_req()),
                         lambda: ad.cancel_order("BTC", "cf-leg1-0001"),
                         lambda: ad.set_leverage("BTC", Decimal("5"))):
                with pytest.raises(ExchangeError) as ei:
                    run(call())
                assert ei.value.code == "SIGNING_UNAVAILABLE"
                assert ei.value.retriable is False
                assert not isinstance(ei.value, OrderUnknown)
                assert "msgpack" in ei.value.message or "eth" in ei.value.message
            assert not rec.requests
        finally:
            run(ad.close())


def test_no_private_key_is_read_only_not_a_construction_failure():
    """签名依赖装着但没配私钥同样只是"只读模式"，不是构造失败。"""
    rec = Recorder({"metaAndAssetCtxs": [META, CTXS]})
    with adapter(rec, credential=CRED_PUBLIC) as ad:
        assert ad.signing_available is False
        assert run(ad.fetch_carry_rates())          # 公开端点照常
        with pytest.raises(ExchangeError) as ei:
            run(ad.place_order(order_req()))
        assert ei.value.code == "SIGNING_UNAVAILABLE"


def test_bad_private_key_is_caught_locally_before_any_request():
    """私钥格式错本地就能判定，别拿去撞交易所（而且那会报"用户不存在"，离病根很远）。"""
    rec = Recorder({})
    with adapter(rec, credential=Credential(ACCOUNT_ADDRESS, "not-a-key")) as ad:
        with pytest.raises(ExchangeError) as ei:
            run(ad.place_order(order_req()))
        assert ei.value.code == "BAD_PRIVATE_KEY"
        assert not isinstance(ei.value, OrderUnknown)
    assert not rec.requests


def test_master_key_and_empty_api_key_are_flagged_in_the_note():
    """凭据一致性校验：推出的地址等于 api_key = 用户填的是主钱包私钥而不是 API Wallet
    （权限过大、且与网页端共享 nonce 计数器）；api_key 为空则退化成"私钥即主钥"。

    两种情况都只给 note 不拦路——但必须让界面看得见。
    """
    rec = Recorder({})
    with adapter(rec, credential=Credential(AGENT_ADDRESS, API_SECRET)) as ad:
        ad._signer()
        assert "主钱包私钥" in ad.credential_note

    with adapter(rec, credential=Credential("", API_SECRET)) as ad2:
        ad2._signer()
        assert ad2.account_address == AGENT_ADDRESS
        assert "退化" in ad2.credential_note or "空账户" in ad2.credential_note
    assert not rec.requests


# =========================================================================
# 未实现的市场
# =========================================================================

def test_spot_and_margin_are_explicitly_unimplemented():
    """现货缺 spotMeta / asset id / 精度规则调研，杠杆借币在这个平台上**根本不存在**。
    必须显式拒绝而不是瞎猜。"""
    for kind in (MarketKind.SPOT, MarketKind.MARGIN):
        with pytest.raises(NotImplementedError):
            HyperliquidAdapter(CRED, market_kind=kind)


# ---- 对抗审查修复的回归 --------------------------------------------------

def test_positions_never_guess_multiplier_one_for_unknown_k_coins():
    """_meta 里没有的 k 币不许猜 mult=1。

    开了 include_k_prefixed=False 的用户（对 k 币千倍面值没把握、最不该被坑的
    那批）k 币恰好都不进 _meta，持仓走的是兜底分支——曾经兜底猜 1，翻开持仓
    看到的就是错 1000 倍的数。量级错是静默的，比签名错危险得多。
    倍数规则必须与 _AssetMeta.__init__ 同源。
    """
    rec = Recorder({"clearinghouseState": CLEARINGHOUSE})
    with adapter(rec, include_k_prefixed=False) as ad:
        assert "kPEPE" not in ad._meta      # 被开关排除，正是审查设想的场景
        poss = {p.symbol: p for p in run(ad.fetch_positions())}
    kpepe = poss["kPEPE"]
    assert kpepe.qty == Decimal("1000") * 1000, "k 前缀兜底也必须用 1000 倍"
    assert kpepe.entry_price == Decimal("0.01") / 1000


def test_load_universe_rebuilds_and_drops_delisted_coins():
    """三张表整体替换、不做增量：增量只增不减，盘中被下架的币会带着一个
    不再更新的费率继续挂在机会榜上（fetch_carry_rates 的过滤判据正是
    `name in _meta`），_meta_of 还能查到它的 index，下单要到交易所才被拒。"""
    rec = Recorder({})
    with adapter(rec) as ad:
        assert "HYPE" in ad._meta
        shrunk = {"universe": [row for row in META["universe"]
                               if (row.get("name") or "") != "HYPE"],
                  "marginTables": META.get("marginTables") or []}
        ad._load_universe(shrunk)
        assert "HYPE" not in ad._meta, "下架币的旧条目必须被整体替换清掉"
        assert "BTC" in ad._meta
