"""Backpack 适配器测试。全程 httpx.MockTransport，一次都不连交易所。

金标准取自 docs/research/exchange-backpack.md：
  - 「认证签名」：ED25519（RFC 8032，**确定性签名**），签名串
    `instruction=<指令>&<k=v 按键名字母序>&timestamp=<ms>&window=<ms>`；
    GET 签查询参数、POST/DELETE/PATCH 签 body 键值对；不发 X-Window 也必须把
    5000 签进串里；布尔小写、decimal 全字符串、clientId 十进制整数。
  - 「符号模型」：数量单位就是 base 币，没有张、contract_size=1——八家里唯一
    不需要单位换算的一家。
  - 「资金费」：1 小时结算（fundingInterval=3600000，逐合约字段）；上下限单位
    **基点**而当期费率是小数；历史端点降序 + offset 从最新往回。
  - 「错误码与限频」的 ApiErrorCode 全枚举和分桶建议。

Backpack 的服务端验签吃的是**解析后的键值对**（不是请求原始字节），所以这里最强的
校验方式是：在 handler 里从**真正上线的请求**（URL 查询串 / JSON body 的字节）重建
签名串，再用固定测试密钥对的**公钥 `verify()`**——ED25519 是确定性签名，验签通过
同时证明了(1)拼串规则对、(2)签的和发的是同一份文本形态、(3)timestamp/window 两个
header 与签进串里的是同一个值。比比对十六进制串更强：十六进制比对还要先假设
"期望值算对了"，公钥验签只信数学。

返佣码：本仓库**不注入任何返佣码/邀请码/broker code**（用户明确要求）。Backpack 的
返佣机制是 X-BROKER-ID/X-BROKER-KEY header（CCXT 会自动塞 X-Broker-Id: 1400），
Recorder 对**每一个**出站请求全局断言这族 header 一个都不出现；另有两条专项用例
钉死 body/URL/签名不受 broker_code 影响。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
import zlib
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

import carryfarm.exchanges.backpack as backpack
from carryfarm.exchanges.backpack import (
    ACTUAL_FEE_NOTE,
    BOOK_TOP_LIMIT,
    DEPTH_LIMIT,
    FUNDING_HISTORY_MAX_PAGES,
    LISTED_FEE_NOTE,
    MAX_WINDOW_MS,
    ORDER_HISTORY_PAGE_SIZE,
    WS_BASE,
    BackpackAdapter,
    _INSTRUCTIONS,
    _MarketMeta,
)
from carryfarm.exchanges.base import (
    Credential,
    ExchangeError,
    OrderRejected,
    OrderRequest,
    OrderUnknown,
    RateLimited,
)
from carryfarm.models import MarketKind, Venue

# ---- 固定测试密钥对：ED25519 确定性签名的前提 ------------------------------
# seed 固定 → 公私钥固定 → 同一份签名串永远得到同一个签名（RFC 8032），
# "带不带返佣码逐字节相同"这类比对才有意义。
TEST_SEED = b"carryfarm-backpack-test-seed-01!"          # 恰好 32 字节
_SK = ed25519.Ed25519PrivateKey.from_private_bytes(TEST_SEED)
_PUB = _SK.public_key()
PUB_RAW = _PUB.public_bytes(serialization.Encoding.Raw,
                            serialization.PublicFormat.Raw)
API_KEY = base64.b64encode(PUB_RAW).decode()             # base64 公钥 = api_key
API_SECRET = base64.b64encode(TEST_SEED).decode()        # base64 种子 = api_secret

# 冻结时间戳（13 位毫秒）。1785949200000 恰好是整点（研究文档实测的
# nextFundingTimestamp），资金费历史的网格从它往回铺。
FIXED_TS_MS = 1_785_949_212_345
HOUR_MS = 3_600_000
LATEST_SETTLE = FIXED_TS_MS - FIXED_TS_MS % HOUR_MS      # = 1785949200000

# 字符串 coid → uint32 clientId 的期望值（与适配器同源规则独立重算）
CLIENT_U32 = zlib.crc32(b"leg1-0001") & 0xFFFFFFFF

# instruction 对照表：**从调研文档的表格独立转录**，同时喂给"表格一致性"断言和
# 签名重建（不引用适配器自己的表——否则表错了验签也跟着错，测试就成了自证）。
DOC_INSTRUCTIONS = {
    ("GET", "/api/v1/account"): "accountQuery",
    ("PATCH", "/api/v1/account"): "accountUpdate",
    ("GET", "/api/v1/account/limits/order"): "maxOrderQuantity",
    ("GET", "/api/v1/capital"): "balanceQuery",
    ("GET", "/api/v1/position"): "positionQuery",
    ("POST", "/api/v1/order"): "orderExecute",
    ("DELETE", "/api/v1/order"): "orderCancel",
    ("GET", "/api/v1/order"): "orderQuery",
    ("GET", "/api/v1/orders"): "orderQueryAll",
    ("DELETE", "/api/v1/orders"): "orderCancelAll",
    ("GET", "/wapi/v1/history/orders"): "orderHistoryQueryAll",
    ("GET", "/wapi/v1/history/fills"): "fillHistoryQueryAll",
    ("GET", "/wapi/v1/history/funding"): "fundingHistoryQueryAll",
}

# ---- 市场样例：字段与取值逐条取自调研文档 /api/v1/market 的线上真实响应 ------

BTC_MARKET = {
    "symbol": "BTC_USDC_PERP", "baseSymbol": "BTC", "quoteSymbol": "USDC",
    "marketType": "PERP", "orderBookState": "Open", "visible": True,
    "filters": {
        "price": {"tickSize": "0.1", "minPrice": "0.1", "maxPrice": "1000000"},
        "quantity": {"stepSize": "0.00001", "minQuantity": "0.00001",
                     "maxQuantity": None},          # 实测 BTC 就是 null
    },
    "fundingInterval": 3600000,
    "fundingRateUpperBound": "100", "fundingRateLowerBound": "-100",   # 基点!
    "openInterestLimit": "4000",
    "imfFunction": {"type": "sqrt", "base": "0.01333333", "factor": "0.00002494"},
    "mmfFunction": {"type": "sqrt", "base": "0.00666667", "factor": "0.00001496"},
}
# 上下限**故意不对称**（+150/-100bps），用来验证 (cap, floor) 的顺序而不是"取绝对值"
SOL_MARKET = {
    "symbol": "SOL_USDC_PERP", "baseSymbol": "SOL", "quoteSymbol": "USDC",
    "marketType": "PERP", "orderBookState": "Open", "visible": True,
    "filters": {"price": {"tickSize": "0.01"},
                "quantity": {"stepSize": "0.01", "minQuantity": "0.01",
                             "maxQuantity": "50000"}},
    "fundingInterval": 3600000,
    "fundingRateUpperBound": "150", "fundingRateLowerBound": "-100",
    "openInterestLimit": "200000",
    "imfFunction": {"type": "sqrt", "base": "0.02", "factor": "0.0001"},
}
# 下面这些必须被过滤链滤掉，一条都不许进 instruments / carry
SPOT_ROW = {**BTC_MARKET, "symbol": "BTC_USDC", "marketType": "SPOT"}
IPERP_ROW = {**BTC_MARKET, "symbol": "BTC_USDC_IPERP", "marketType": "IPERP"}
CANCEL_ONLY = {**SOL_MARKET, "symbol": "OLD_USDC_PERP",
               "orderBookState": "CancelOnly"}
HIDDEN = {**SOL_MARKET, "symbol": "HID_USDC_PERP", "visible": False}
RWA_STOCK = {**SOL_MARKET, "symbol": "TSLA_USDC_PERP", "rwaMarketType": "STOCK"}
BAD_TICK = {**SOL_MARKET, "symbol": "BAD_USDC_PERP",
            "filters": {"price": {"tickSize": "0"},
                        "quantity": {"stepSize": "0.01", "minQuantity": "0.01"}}}
# fundingInterval 缺失/为 0 → 兜底 3600s（字段是逐合约的，主路径必须读字段）
NO_INTERVAL = {**SOL_MARKET, "symbol": "NOIV_USDC_PERP", "fundingInterval": 0}
ALL_MARKETS = [BTC_MARKET, SOL_MARKET, SPOT_ROW, IPERP_ROW, CANCEL_ONLY,
               HIDDEN, RWA_STOCK, BAD_TICK, NO_INTERVAL]

# depth 线上真实响应形状：**bids 升序（best bid 在最后）、asks 升序、时间戳微秒**
DEPTH_5 = {"bids": [["64574.3", "0.77413"], ["64575.1", "3.57196"]],
           "asks": [["64575.2", "0.24612"], ["64576.8", "0.77429"]],
           "lastUpdateId": "5796499452", "timestamp": 1785948116439400}
# 手算过的簿：mid = (64575.1+64575.2)/2 = 64575.15
# 10bp → lo = 64510.57485、hi = 64639.72515（边界含）
# bids 首档在带外，一档量为 0 的脏行两项统计都不许计
DEPTH_100 = {"bids": [["64300.0", "5"], ["64520.0", "1"], ["64560.0", "2"],
                      ["64570.0", "0"], ["64575.1", "0.5"]],
             "asks": [["64575.2", "0.4"], ["64600.0", "1"], ["64700.0", "3"]],
             "timestamp": 1785948116439400}

MARK_PRICES = [
    {"fundingRate": "-0.0000117704808946259223127855",   # 线上原文，26 位小数
     "indexPrice": "64620.4597227", "markPrice": "64575.6",
     "nextFundingTimestamp": 1785949200000, "symbol": "BTC_USDC_PERP"},
    {"fundingRate": "0.0000125", "indexPrice": "150.1", "markPrice": "150.2",
     "nextFundingTimestamp": 1785949200000, "symbol": "SOL_USDC_PERP"},
    # CancelOnly 市场：有费率也不许产出（配出一条下不了单的腿）
    {"fundingRate": "0.01", "markPrice": "1.0",
     "nextFundingTimestamp": 1785949200000, "symbol": "OLD_USDC_PERP"},
    {"symbol": "BTC_USDC"},              # spot 行没有费率字段（schema 非必填）
]


def order_row(status: str = "Filled", executed: str = "0.005",
              quote: str = "322.876", *, client_id=CLIENT_U32,
              oid: str = "112233445566778899", order_type: str = "Limit") -> dict:
    """下单/挂单端点的订单对象（createdAt 是**毫秒整数**）。"""
    return {"id": oid, "clientId": client_id, "symbol": "BTC_USDC_PERP",
            "side": "Bid", "orderType": order_type, "quantity": "0.005",
            "price": "64575.2", "status": status, "executedQuantity": executed,
            "executedQuoteQuantity": quote, "timeInForce": "IOC",
            "selfTradePrevention": "RejectTaker", "createdAt": 1785948116439,
            "postOnly": False, "reduceOnly": False}


def hist_row(client_id, status: str = "Filled", executed: str = "0.005",
             quote: str = "322.876", oid: str = "9911") -> dict:
    """历史订单端点的行：createdAt 是 **naive datetime 串**（与挂单端点类型漂移，
    调研文档实锤），clientId 也可能是 string——两个坑都摆进 fixture。"""
    return {"id": oid, "clientId": client_id, "symbol": "BTC_USDC_PERP",
            "side": "Bid", "orderType": "Limit", "quantity": "0.005",
            "price": "64575.2", "status": status, "executedQuantity": executed,
            "executedQuoteQuantity": quote, "createdAt": "2026-08-05T17:00:01",
            "expiryReason": None}


# ---- 基础设施 ------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


def json_response(payload, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def err(code: str, msg: str = "boom", status: int = 400) -> httpx.Response:
    """错误信封：`{"code": ApiErrorCode, "message": ...}` + 真实的 4xx/5xx 状态码
    （这家不像 KuCoin 拿 200 包业务错）。"""
    return json_response({"code": code, "message": msg}, status=status)


def iso_utc(ms: int) -> str:
    """毫秒 → naive UTC datetime 串（"2026-08-05T17:00:00"，没有 Z）——
    fundingRates 的 intervalEndTimestamp 就是这个反直觉格式。"""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S")


def rebuilt_signing_string(request: httpx.Request) -> str:
    """从**真正上线的请求字节**独立重建签名串：GET 取 URL 查询串、其余取 JSON body，
    instruction 查文档转录表、timestamp/window 取 header 原文。布尔小写、int 十进制
    ——与文档 Authentication 节的规则逐条对应，不引用适配器的任何实现。"""
    method = request.method.upper()
    instruction = DOC_INSTRUCTIONS[(method, request.url.path)]
    if method == "GET":
        assert not request.content, "GET 不许带 body，带了签名两边就分叉了"
        source: dict = {k: v for k, v in request.url.params.items()}
    else:
        source = json.loads(request.content) if request.content else {}
    parts = [f"instruction={instruction}"]
    for k in sorted(source):
        v = source[k]
        if isinstance(v, bool):
            v = "true" if v else "false"
        parts.append(f"{k}={v}")
    parts.append(f"timestamp={request.headers['X-Timestamp']}")
    parts.append(f"window={request.headers['X-Window']}")
    return "&".join(parts)


def verify_wire_signature(request: httpx.Request) -> None:
    """公钥验签真实请求。InvalidSignature 直接炸测试——不比对期望串，只信数学。"""
    assert request.headers["X-API-Key"] == API_KEY      # base64 公钥**原文**
    sig = base64.b64decode(request.headers["X-Signature"])
    assert len(sig) == 64                               # Ed25519 签名恒 64 字节
    _PUB.verify(sig, rebuilt_signing_string(request).encode())


class Recorder:
    """按 (method, path) 路由并记录所有出站请求。

    未登记的请求直接 AssertionError——比返回 404 更早暴露"路径写错/多发一次请求"。
    另外对**每一个**请求全局断言不带 X-BROKER-* header：返佣注入是 Backpack 的
    header 机制，这里一个字节都不许出现（用户明确要求）。
    """

    def __init__(self, routes: dict | None = None, default=None):
        self.routes = routes or {}
        self.default = default
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert not any(h.lower().startswith("x-broker") for h in request.headers), \
            "X-BROKER-* 返佣 header 泄漏"
        self.requests.append(request)
        handler = self.routes.get((request.method, request.url.path), self.default)
        if handler is None:
            raise AssertionError(f"未预期的请求: {request.method} {request.url}")
        out = handler(request) if callable(handler) else handler
        return out if isinstance(out, httpx.Response) else json_response(out)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


def make_adapter(recorder: Recorder, broker_code: str = "", *,
                 seed_meta: bool = True, fixed_ts: bool = False,
                 cred: Credential | None = None, **kw) -> BackpackAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder),
                               timeout=httpx.Timeout(1.0))
    ad = BackpackAdapter(cred or Credential(API_KEY, API_SECRET),
                         broker_code=broker_code, client=client, **kw)
    # 冻结校时：否则每个用例都要伺候 /api/v1/time，请求记录还会被校时请求搅浑
    ad._clock.synced_at = time.time()
    ad._clock.offset_ms = 0
    if fixed_ts:
        ad.timestamp_ms = lambda: FIXED_TS_MS
    if seed_meta:
        for raw in (BTC_MARKET, SOL_MARKET):
            ad._meta[raw["symbol"]] = _MarketMeta(raw)
    return ad


@contextlib.contextmanager
def adapter(recorder: Recorder, **kw):
    ad = make_adapter(recorder, **kw)
    try:
        yield ad
    finally:
        run(ad.close())


def order_req(**kw) -> OrderRequest:
    base = dict(symbol="BTC_USDC_PERP", side="buy", qty=Decimal("0.005"),
                price=Decimal("64575.17"), time_in_force="IOC",
                client_order_id="leg1-0001")
    base.update(kw)
    return OrderRequest(**base)


_ORIG_PACE = backpack.FUNDING_HISTORY_PAGE_PACE_S


@pytest.fixture(autouse=True)
def _no_funding_page_pace(monkeypatch):
    """跨页限速是线上历史桶（30 req/min）的约束，离线测试只会白睡——归零。
    顺手钉死生产值本身 >= 2s（≈0.5 req/s 压在配额线下），别被人悄悄调没。"""
    assert _ORIG_PACE >= 2
    monkeypatch.setattr(backpack, "FUNDING_HISTORY_PAGE_PACE_S", 0)


# ---- 签名 ---------------------------------------------------------------

def test_signing_string_matches_the_docs_cancel_example_verbatim():
    """逐字对齐文档给的撤单例（body `{"orderId": 28, "symbol": "BTC_USDT"}`）；
    无参数端点是 instruction+timestamp+window 三段；布尔必须小写（Python str(True)
    是 "True"，直接拼收 INVALID_SIGNATURE 且报错不区分拼串错/编码错）；Decimal 走
    定点，`1E-5` 进签名串等于签了一个 body 里不存在的文本。"""
    assert BackpackAdapter._signing_string(
        "orderCancel", {"orderId": 28, "symbol": "BTC_USDT"}, 1614550000000, 5000
    ) == ("instruction=orderCancel&orderId=28&symbol=BTC_USDT"
          "&timestamp=1614550000000&window=5000")
    assert BackpackAdapter._signing_string("positionQuery", {}, 1614550000000, 5000) \
        == "instruction=positionQuery&timestamp=1614550000000&window=5000"
    s = BackpackAdapter._signing_string(
        "orderExecute", {"postOnly": True, "reduceOnly": False,
                         "price": Decimal("0.00001")}, 1, 5000)
    assert "postOnly=true" in s and "reduceOnly=false" in s
    assert "price=0.00001" in s and "1E-5" not in s
    # 键名字母序：price < postOnly? 不——按 sorted() 的字典序 postOnly < price <
    # reduceOnly，签名串的段顺序必须与之一致
    assert s == ("instruction=orderExecute&postOnly=true&price=0.00001"
                 "&reduceOnly=false&timestamp=1&window=5000")


def test_instruction_table_matches_the_doc_and_batch_is_deliberately_absent():
    """(method, path) → instruction 全表逐条对照调研文档；签错 instruction 只会收到
    INVALID_SIGNATURE、与拼串错完全无法区分，所以表本身必须钉死。批量下单
    POST /api/v1/orders 的签名是"每单各带 instruction= 前缀"的另一种拼法，
    **故意没实现**——它绝不能出现在表里，否则会被当普通端点签出必错的串。"""
    assert dict(_INSTRUCTIONS) == DOC_INSTRUCTIONS
    assert ("POST", "/api/v1/orders") not in _INSTRUCTIONS


def test_signed_post_signature_verifies_against_wire_bytes():
    """公钥验签**真正发出去的请求**（body 字节重建串）。同时钉死 header 编码：
    X-API-Key 是 base64 公钥原文、X-Timestamp 13 位毫秒、X-Window 没显式配置时
    是 "5000"（且必须签进串里——验签通过就是证明）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        verify_wire_signature(request)
        h = request.headers
        assert h["X-Timestamp"].isdigit() and len(h["X-Timestamp"]) == 13
        assert h["X-Window"] == "5000"
        assert h["content-type"] == "application/json"
        return json_response(order_row())

    rec = Recorder({("POST", "/api/v1/order"): handler})
    with adapter(rec) as ad:
        run(ad.place_order(order_req()))
    assert len(rec.requests) == 1


def test_signed_get_signs_the_query_and_sends_no_body():
    """GET 签的是查询参数：从 URL 原文重建签名串必须验得过，body 必须为空
    （GET 带 body 会让"签的"和"发的"分叉）。clientId 在 query 里是十进制整数串。"""
    def handler(request: httpx.Request) -> httpx.Response:
        verify_wire_signature(request)
        q = dict(request.url.params)
        assert q == {"symbol": "BTC_USDC_PERP", "clientId": str(CLIENT_U32)}
        return json_response(order_row("New", "0", "0"))

    rec = Recorder({("GET", "/api/v1/order"): handler})
    with adapter(rec) as ad:
        res = run(ad.query_order("BTC_USDC_PERP", "leg1-0001"))
    assert res.status == "new" and len(rec.requests) == 1


def test_signed_no_param_get_signs_exactly_three_segments():
    """无参数端点（positionQuery）的签名串就是 instruction+timestamp+window 三段，
    一个多余的 & 都没有——重建串逐字比对 + 公钥验签双保险。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        verify_wire_signature(request)
        seen["s"] = rebuilt_signing_string(request)
        seen["ts"] = request.headers["X-Timestamp"]
        return json_response([])

    rec = Recorder({("GET", "/api/v1/position"): handler})
    with adapter(rec) as ad:
        run(ad.fetch_positions())
    assert seen["s"] == f"instruction=positionQuery&timestamp={seen['ts']}&window=5000"


def test_signed_delete_and_patch_sign_the_body():
    """DELETE（撤单）和 PATCH（改杠杆）都签 body 键值对。撤单 body 只带 clientId
    **绝不同时带 orderId**——文档原文：两个都给时签名会验不过。"""
    def cancel(request: httpx.Request) -> httpx.Response:
        verify_wire_signature(request)
        body = json.loads(request.content)
        assert set(body) == {"symbol", "clientId"}
        assert "orderId" not in body
        assert type(body["clientId"]) is int and body["clientId"] == CLIENT_U32
        return json_response(order_row("Cancelled", "0", "0"))

    def patch(request: httpx.Request) -> httpx.Response:
        verify_wire_signature(request)
        assert json.loads(request.content) == {"leverageLimit": "10"}
        return httpx.Response(200)          # PATCH account 的 200 是空 body

    rec = Recorder({("DELETE", "/api/v1/order"): cancel,
                    ("GET", "/api/v1/account"): {"leverageLimit": "5"},
                    ("PATCH", "/api/v1/account"): patch})
    with adapter(rec) as ad:
        run(ad.cancel_order("BTC_USDC_PERP", "leg1-0001"))
        run(ad.set_leverage("BTC_USDC_PERP", Decimal("10")))
    assert len(rec.requests) == 3


def test_timestamp_header_and_signature_use_the_same_read():
    """签一个时间戳、发另一个是最常见的 INVALID_SIGNATURE。让 timestamp_ms() 每次
    调用返回不同值：实现若取了两次（一次进签名串一次进 header），公钥验签
    （用 header 里的值重建）就过不了。"""
    ticks = iter(range(FIXED_TS_MS, FIXED_TS_MS + 10_000, 1_000))

    def handler(request: httpx.Request) -> httpx.Response:
        verify_wire_signature(request)
        return json_response(order_row())

    rec = Recorder({("POST", "/api/v1/order"): handler})
    with adapter(rec) as ad:
        ad.timestamp_ms = lambda: next(ticks)
        run(ad.place_order(order_req()))
    assert len(rec.requests) == 1


def test_unregistered_signed_path_fails_locally_before_sending():
    """没登记 instruction 的签名请求必须**本地**炸（NO_INSTRUCTION）：签错指令名
    只会收到交易所的 INVALID_SIGNATURE，排查方向必然带偏。"""
    with adapter(Recorder({})) as ad:
        with pytest.raises(ExchangeError) as ei:
            ad._prepare("POST", "/api/v1/orders", {}, {"a": 1}, True)
        assert ei.value.code == "NO_INSTRUCTION"


def test_window_clamps_to_documented_bounds_and_header_matches_signature():
    """window 上限 60000（签超过上限的值直接被拒），下限 1。夹完的值必须同时进
    header 和签名串——验签通过就是证明。"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Window"] == str(MAX_WINDOW_MS)
        verify_wire_signature(request)
        return json_response([])

    rec = Recorder({("GET", "/api/v1/position"): handler})
    with adapter(rec, window_ms=70_000) as ad:
        assert ad._window_ms == MAX_WINDOW_MS
        run(ad.fetch_positions())
    with adapter(Recorder({}), window_ms=0) as ad2:
        assert ad2._window_ms == 1


def test_public_endpoints_carry_no_credentials():
    """公开端点一律不签名：多发 X-API-* 只会在没配 key 的 public_feed 场景下
    把一整家行情打没。/api/v1/time 是**裸文本毫秒串**不是 JSON。"""
    rec = Recorder({
        ("GET", "/api/v1/time"): httpx.Response(200, content=b"1785948047107"),
        ("GET", "/api/v1/markets"): ALL_MARKETS,
        ("GET", "/api/v1/depth"): DEPTH_5,
        ("GET", "/api/v1/fundingRates"): [],
    })
    with adapter(rec) as ad:
        assert run(ad.fetch_server_time_ms()) == 1785948047107
        run(ad.fetch_instruments())
        run(ad.fetch_book_top("BTC_USDC_PERP"))
        run(ad.fetch_funding_history("BTC_USDC_PERP", since_ms=1))
    assert len(rec.requests) == 4
    for req in rec.requests:
        assert not any(h.lower().startswith(("x-api", "x-sig", "x-tim", "x-win"))
                       for h in req.headers), req.url


# ---- 返佣码：一个字节都不许出现 ------------------------------------------

def test_no_referral_trace_anywhere():
    """用户明确要求不加返佣码：请求里不能有任何渠道类 header/字段（Backpack 的注入
    机制是 X-BROKER-ID/X-BROKER-KEY，CCXT 默认塞 X-Broker-Id: 1400——绝不许抄），
    clientId 也不许被动手脚（它是 uint32，动了整条消解链就断）。"""
    rec = Recorder({("POST", "/api/v1/order"): json_response(order_row())})
    with adapter(rec, broker_code="somepartnercode") as ad:
        run(ad.place_order(order_req()))
    req = rec.last
    banned = ("referral", "referrer", "broker", "channel", "partner",
              "affiliate", "invite", "rebate")
    for name in req.headers:
        assert not any(b in name.lower() for b in banned), name
    assert "somepartnercode" not in str(req.url)
    assert all("somepartnercode" not in v for v in req.headers.values())
    assert b"somepartnercode" not in req.content
    body = json.loads(req.content)
    assert set(body) == {"symbol", "side", "orderType", "quantity",
                         "clientId", "price", "timeInForce"}
    assert body["clientId"] == CLIENT_U32       # 纯 crc32，无前缀无偏移


def test_wire_is_byte_identical_with_and_without_broker_code():
    """返佣码对这家**完全不存在**。ED25519 是确定性签名（RFC 8032）+ 时间戳冻结，
    所以同一单带不带码，body 与签名必须逐字节相同——任何"悄悄塞进 header/body"
    的实现都会在这里露馅。"""
    seen: list[tuple[bytes, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.content, request.headers["X-Signature"]))
        return json_response(order_row())

    rec = Recorder({("POST", "/api/v1/order"): handler})
    for code in ("", "somepartnercode"):
        with adapter(rec, broker_code=code, fixed_ts=True) as ad:
            run(ad.place_order(order_req()))
    assert seen[0] == seen[1]


# ---- 凭据 ----------------------------------------------------------------

def test_public_adapter_constructs_with_blank_credentials():
    """public_feed 用 Credential("", "") 构造它跑无凭据公开行情——构造期碰 base64
    解码就会把 Backpack 从整个机会榜抹掉。签名路径要在**用到时**才报 NO_CREDENTIAL，
    且下单路径不许升级成 OrderUnknown（请求根本没发出去，撮合没见过它）。"""
    rec = Recorder({("GET", "/api/v1/depth"): DEPTH_5})
    client = httpx.AsyncClient(transport=httpx.MockTransport(rec),
                               timeout=httpx.Timeout(1.0))
    ad = BackpackAdapter(Credential("", ""), client=client)
    ad._clock.synced_at = time.time()
    for raw in (BTC_MARKET, SOL_MARKET):
        ad._meta[raw["symbol"]] = _MarketMeta(raw)
    try:
        assert ad.market == "backpack:perp"
        assert ad.venue is Venue.BACKPACK and ad.venue.value == "backpack"
        assert ad.rest_base == "https://api.backpack.exchange"
        assert run(ad.fetch_book_top("BTC_USDC_PERP")).bid_price == Decimal("64575.1")
        with pytest.raises(ExchangeError) as ei:
            run(ad.fetch_positions())
        assert ei.value.code == "NO_CREDENTIAL"
        n = len(rec.requests)
        # OrderUnknown 不是 ExchangeError 的子类，这个 raises 接不住它——正好当判据
        with pytest.raises(ExchangeError) as ei2:
            run(ad.place_order(order_req()))
        assert ei2.value.code == "NO_CREDENTIAL"
        assert len(rec.requests) == n, "凭据缺失的下单一个字节都不该上网"
    finally:
        run(ad.close())


def test_credential_decoding_failures_and_ccxt_style_64_byte_seed():
    """secret 不是合法 base64 / 长度不对 → BAD_CREDENTIAL（在首次签名时，不在构造期）。
    64 字节的 seed+pub 拼接导出格式（CCXT 同款）取前 32 字节——签出来的签名必须
    仍然被同一把公钥验过，证明取的是种子那一半。"""
    rec = Recorder({})
    with adapter(rec, cred=Credential(API_KEY, "!!!not-base64!!!")) as ad:
        with pytest.raises(ExchangeError) as ei:
            run(ad.fetch_positions())
        assert ei.value.code == "BAD_CREDENTIAL"
    with adapter(rec, cred=Credential(API_KEY,
                                      base64.b64encode(b"tooshort").decode())) as ad:
        with pytest.raises(ExchangeError) as ei2:
            run(ad.fetch_positions())
        assert ei2.value.code == "BAD_CREDENTIAL"
    assert not rec.requests

    def handler(request: httpx.Request) -> httpx.Response:
        verify_wire_signature(request)
        return json_response([])

    concat = base64.b64encode(TEST_SEED + PUB_RAW).decode()
    rec2 = Recorder({("GET", "/api/v1/position"): handler})
    with adapter(rec2, cred=Credential(API_KEY, concat)) as ad:
        assert run(ad.fetch_positions()) == ()
    assert len(rec2.requests) == 1


@pytest.mark.parametrize("kind", [MarketKind.SPOT, MarketKind.MARGIN,
                                  MarketKind.PERP_COIN])
def test_unsupported_market_kinds_are_explicitly_unimplemented(kind):
    """现货共用下单端点但涉及 autoLend/autoBorrow 借贷族和另一套对账口径，
    调研没覆盖；凭记忆补等于赌。显式拒绝，不瞎猜。"""
    with pytest.raises(NotImplementedError):
        BackpackAdapter(Credential(API_KEY, API_SECRET), market_kind=kind)


# ---- clientId：字符串 coid ↔ uint32 的确定性映射 --------------------------

def test_client_id_mapping_is_deterministic_crc32_and_mandatory():
    """clientId 是 **uint32 JSON 整数**不是字符串。映射必须确定性（下单/撤单/查单
    同一个数，否则消解链断），且**单一规则无双路径**；无 ID 下单等于放弃消解能力，
    必须就地炸。"""
    with adapter(Recorder({})) as ad, adapter(Recorder({})) as ad2:
        u32 = ad._client_id_u32("leg1-0001")
        assert u32 == CLIENT_U32 == zlib.crc32(b"leg1-0001") & 0xFFFFFFFF
        assert ad2._client_id_u32("leg1-0001") == u32       # 跨实例同值
        assert 0 <= u32 <= 0xFFFFFFFF
        # 纯数字 coid 也走同一条 crc32 规则，不做"直接 int"的第二条路径
        assert ad._client_id_u32("12345") == zlib.crc32(b"12345") & 0xFFFFFFFF
        assert ad._coid_by_u32[u32] == "leg1-0001"          # 反查表还原原字符串
        with pytest.raises(ValueError):
            run(ad.place_order(order_req(client_order_id="")))


# ---- 下单体 --------------------------------------------------------------

def test_order_body_shape_limit_ioc_and_price_rounds_up_for_buys():
    """side 是 **Bid/Ask** 不是 buy/sell；quantity 是字符串型 decimal（base 币，
    无张无乘数）；价格按 tickSize **买单向上**取整（保护限价故意穿透对手价，
    反向取整会把穿透削回来）；clientId 是 JSON 整数。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.clear()
        captured.update(json.loads(request.content))
        return json_response(order_row())

    rec = Recorder({("POST", "/api/v1/order"): handler})
    with adapter(rec) as ad:
        run(ad.place_order(order_req()))            # 64575.17 买 → tick 0.1 向上
        assert captured["side"] == "Bid" and captured["orderType"] == "Limit"
        assert captured["price"] == "64575.2"
        assert isinstance(captured["quantity"], str)
        assert "E" not in captured["quantity"] and "e" not in captured["quantity"]
        assert Decimal(captured["quantity"]) == Decimal("0.005")
        assert type(captured["clientId"]) is int
        assert captured["timeInForce"] == "IOC"
        assert "postOnly" not in captured and "reduceOnly" not in captured
        # 卖单向下取整
        run(ad.place_order(order_req(side="sell", client_order_id="leg2-0001")))
        assert captured["side"] == "Ask" and captured["price"] == "64575.1"
    # 静态取整方向单测：与 executor.cross_price 的方向约定一致
    tick = Decimal("0.1")
    assert BackpackAdapter.round_price(Decimal("64575.17"), tick, "buy") == \
        Decimal("64575.2")
    assert BackpackAdapter.round_price(Decimal("64575.17"), tick, "sell") == \
        Decimal("64575.1")
    assert BackpackAdapter.round_price(Decimal("64575.2"), tick, "buy") == \
        Decimal("64575.2")


def test_order_body_market_reduce_only_and_qty_truncates_down():
    """市价单不带 price/timeInForce；reduceOnly 是 JSON 布尔；数量向 stepSize
    **向下**取整——宁可少下也不能超过目标（超量的另一半在别家腿上没有对冲）。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        verify_wire_signature(request)      # 布尔小写进签名串，验签就是证明
        captured.clear()
        captured.update(json.loads(request.content))
        return json_response(order_row(order_type="Market"))

    rec = Recorder({("POST", "/api/v1/order"): handler})
    with adapter(rec) as ad:
        run(ad.place_order(order_req(price=None, reduce_only=True)))
        assert captured["orderType"] == "Market"
        assert "price" not in captured and "timeInForce" not in captured
        assert captured["reduceOnly"] is True
        # SOL step 0.01：0.0157 → 0.01（floor），绝不 0.02
        run(ad.place_order(order_req(symbol="SOL_USDC_PERP", qty=Decimal("0.0157"),
                                     price=Decimal("150.155"),
                                     client_order_id="leg3-0001")))
        assert Decimal(captured["quantity"]) == Decimal("0.01")
        assert captured["price"] == "150.16"        # tick 0.01 买单向上


def test_post_only_aliases_and_fok_and_unknown_tif_fails_local():
    """GTX/POST_ONLY/MAKER → GTC + postOnly（postOnly 是**独立布尔**不进 TIF）；
    FOK 原生支持直接发；认不出的 TIF 本地炸绝不静默降级——降级 FOK 成 IOC 会允许
    部分成交，那是隐形单腿的来源。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.clear()
        captured.update(json.loads(request.content))
        return json_response(order_row())

    rec = Recorder({("POST", "/api/v1/order"): handler})
    with adapter(rec) as ad:
        run(ad.place_order(order_req(time_in_force="GTX")))
        assert captured["timeInForce"] == "GTC" and captured["postOnly"] is True
        run(ad.place_order(order_req(time_in_force="FOK",
                                     client_order_id="leg4-0001")))
        assert captured["timeInForce"] == "FOK" and "postOnly" not in captured
        with pytest.raises(OrderRejected) as ei:
            run(ad.place_order(order_req(time_in_force="DAY")))
        assert ei.value.code == "TIF_UNSUPPORTED"
    assert len(rec.requests) == 2          # DAY 那一发根本没上网


def test_place_order_rejects_locally_when_pinched_or_capped():
    """取整后归零/不足 minQuantity、或超 maxQuantity 的单**本地**就拒：省一次 RTT
    也不烧限频配额。BTC 的 maxQuantity 是 null（无上限）——None 不许被当 0 拦。"""
    rec = Recorder({})
    with adapter(rec) as ad:
        with pytest.raises(OrderRejected) as ei:
            run(ad.place_order(order_req(qty=Decimal("0.000009"))))
        assert ei.value.code == "PINCHED"
        with pytest.raises(OrderRejected) as ei2:
            run(ad.place_order(order_req(symbol="SOL_USDC_PERP",
                                         qty=Decimal("60000"),
                                         price=Decimal("150"))))
        assert ei2.value.code == "ORDER_SIZE_TOO_LARGE"
    assert not rec.requests


def test_place_order_maps_synchronous_terminal_states():
    """响应 200 是撮合的**同步终态**（单线性命令流）。响应没有均价字段：
    均价 = executedQuoteQuantity / executedQuantity 自己除，executedQuantity=0 时
    置 0 绝不拿委托价顶替；没有手续费字段：fee=0 是"未知"的诚实表达。
    Expired 是 IOC 残量的终态 → canceled，但部成的量必须带出来做仓位差分。"""
    responses = iter([
        order_row("Filled"),
        order_row("Expired", "0.002", "129.1504"),
        order_row("New", "0", "0"),
        order_row("SomethingNew", "0", "0"),        # 认不出的 status
    ])
    rec = Recorder({("POST", "/api/v1/order"):
                    lambda r: json_response(next(responses))})
    with adapter(rec) as ad:
        full = run(ad.place_order(order_req()))
        assert full.status == "filled" and full.is_terminal
        assert full.filled_qty == Decimal("0.005")
        assert full.avg_price == Decimal("64575.2")     # 322.876 / 0.005
        assert full.fee == Decimal("0") and full.fee_asset == ""
        assert full.client_order_id == "leg1-0001"      # 回传原字符串，上层按它对账
        assert full.exchange_order_id == "112233445566778899"

        partial = run(ad.place_order(order_req(client_order_id="leg2-0001")))
        assert partial.status == "canceled" and partial.is_terminal
        assert partial.filled_qty == Decimal("0.002")
        assert partial.avg_price == Decimal("64575.2")  # 129.1504 / 0.002

        fresh = run(ad.place_order(order_req(client_order_id="leg3-0001")))
        assert fresh.status == "new" and fresh.avg_price == Decimal("0")

        # 认不出的 status 映射成非终态 "new"：让上层继续轮询比谎报终态安全
        odd = run(ad.place_order(order_req(client_order_id="leg4-0001")))
        assert odd.status == "new" and not odd.is_terminal


def test_place_order_2xx_without_an_order_object_is_unknown():
    """2xx 但没有订单对象：无法证明引擎没收到，按 OrderUnknown 走消解。"""
    rec = Recorder({("POST", "/api/v1/order"):
                    httpx.Response(200, content=b"null",
                                   headers={"content-type": "application/json"})})
    with adapter(rec) as ad:
        with pytest.raises(OrderUnknown):
            run(ad.place_order(order_req()))


# ---- 错误码映射 ----------------------------------------------------------

@pytest.mark.parametrize("resp", [
    lambda r: err("TOO_MANY_REQUESTS", "slow down", status=429),
    lambda r: httpx.Response(429, content=b"rate limited by gateway"),  # 非 JSON
])
def test_rate_limit_maps_to_rate_limited_on_both_paths(resp):
    """限频必须是 RateLimited（可重试）。网关 429 的 body 可能压根不是 JSON——
    只解析 JSON 会漏判。下单路径上它是唯一"确定没进撮合"的可重试错，
    绝不升级成 OrderUnknown 白跑一轮消解。"""
    rec = Recorder({("GET", "/api/v1/depth"): resp})
    with adapter(rec) as ad:
        with pytest.raises(RateLimited) as ei:
            run(ad.fetch_book_top("BTC_USDC_PERP"))
        assert ei.value.retriable is True
    rec2 = Recorder({("POST", "/api/v1/order"): resp})
    with adapter(rec2) as ad:
        with pytest.raises(RateLimited):
            run(ad.place_order(order_req()))


@pytest.mark.parametrize("code", [
    "INVALID_ORDER", "INVALID_PRICE", "INVALID_QUANTITY", "INSUFFICIENT_FUNDS",
    "INSUFFICIENT_MARGIN", "MAX_LEVERAGE_REACHED", "TRADING_PAUSED",
    "ACCOUNT_LIQUIDATING", "PRECONDITION_FAILED", "FORBIDDEN",
])
def test_place_order_explicit_business_reject_is_order_rejected(code):
    """明确的业务拒绝发生在进撮合之前，订单确定不存在，改参数后可安全重发。"""
    rec = Recorder({("POST", "/api/v1/order"): err(code, "rejected", status=400)})
    with adapter(rec) as ad:
        with pytest.raises(OrderRejected) as ei:
            run(ad.place_order(order_req()))
    assert ei.value.code == code and ei.value.retriable is False


@pytest.mark.parametrize("resp", [
    lambda r: err("TIMEOUT", "engine timeout", status=400),      # 引擎侧超时
    lambda r: err("SERVER_ERROR", "internal", status=500),
    lambda r: err("MAINTENANCE", "maintenance", status=503),
    lambda r: err("TOO_EARLY", "too early", status=400),         # 语义未明的码
    lambda r: err("WHO_KNOWS", "new code", status=400),          # 认不出的码
    lambda r: httpx.Response(200, content=b"<html>ok?</html>"),  # 2xx 非 JSON
])
def test_place_order_treats_everything_unproven_as_unknown(resp):
    """判定标准一句话：只有能证明"没进撮合"时才允许说失败，证不了就是未知。
    单线性命令流下 API 层报 TIMEOUT/5xx 不代表引擎没收到；误判是双倍仓位。"""
    rec = Recorder({("POST", "/api/v1/order"): resp})
    with adapter(rec) as ad:
        with pytest.raises(OrderUnknown) as ei:
            run(ad.place_order(order_req()))
    assert ei.value.client_order_id == "leg1-0001" and ei.value.venue == "backpack"


@pytest.mark.parametrize("boom", [
    lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("read timeout", request=r)),
    lambda r: (_ for _ in ()).throw(httpx.ConnectError("connection reset", request=r)),
    lambda r: (_ for _ in ()).throw(httpx.RemoteProtocolError("half-closed", request=r)),
])
def test_place_order_transport_failures_raise_order_unknown(boom):
    """下单超时/断流必须抛 OrderUnknown——当成失败返回会导致重发 → 双倍仓位。
    RemoteProtocolError 不在基类 NETWORK 收敛范围里，靠适配器的 TransportError
    兜底接住（三种都要走到 OrderUnknown）。"""
    rec = Recorder({("POST", "/api/v1/order"): boom})
    with adapter(rec) as ad:
        with pytest.raises(OrderUnknown):
            run(ad.place_order(order_req()))


@pytest.mark.parametrize("code", ["INVALID_SIGNATURE", "UNAUTHORIZED",
                                  "INVALID_TWO_FACTOR_CODE"])
def test_auth_layer_errors_are_not_upgraded_to_unknown(code):
    """鉴权层就被挡下，撮合根本没见过这个请求，升级成 OrderUnknown 只会白跑一轮
    cancel+查单。（OrderUnknown 不是 ExchangeError 的子类，这个 raises 接不住它
    ——正好当判据。）也不许降成 OrderRejected：那是"换参数重发"的许可证。"""
    rec = Recorder({("POST", "/api/v1/order"): err(code, "auth", status=401)})
    with adapter(rec) as ad:
        with pytest.raises(ExchangeError) as ei:
            run(ad.place_order(order_req()))
    assert ei.value.code == code and not isinstance(ei.value, OrderRejected)
    assert ei.value.retriable is False


def test_unstructured_5xx_bodies_never_enter_keyword_classification():
    """挡在前面的 CDN/WAF 会回自己的 HTML 错误页——那不是交易所写的。把它喂进
    关键词匹配等于让别人家的 HTML 决定"这单进没进撮合"（hyperliquid 修过：错误页
    里的 429 字样、nonce 属性都触发过误判）。这里 body 里故意埋满错误码字样：
    分类只许看 HTTP 语义。"""
    sneaky = (b"<html><meta nonce='INSUFFICIENT_FUNDS'>blocked id=429 "
              b"TOO_MANY_REQUESTS TRADING_PAUSED INVALID_SIGNATURE</html>")
    rec = Recorder({("GET", "/api/v1/depth"): httpx.Response(502, content=sneaky)})
    with adapter(rec) as ad:
        with pytest.raises(ExchangeError) as ei:
            run(ad.fetch_book_top("BTC_USDC_PERP"))
        assert ei.value.code == "HTTP_502" and ei.value.retriable is True
        assert not isinstance(ei.value, (RateLimited, OrderRejected))

    rec2 = Recorder({("GET", "/api/v1/depth"): httpx.Response(403, content=sneaky)})
    with adapter(rec2) as ad:
        with pytest.raises(ExchangeError) as ei2:
            run(ad.fetch_book_top("BTC_USDC_PERP"))
        assert ei2.value.code == "HTTP_403" and ei2.value.retriable is False
        assert not isinstance(ei2.value, (RateLimited, OrderRejected))

    # 下单路径：5xx 的 HTML 无法证明"没进撮合" → OrderUnknown
    rec3 = Recorder({("POST", "/api/v1/order"): httpx.Response(502, content=sneaky)})
    with adapter(rec3) as ad:
        with pytest.raises(OrderUnknown):
            run(ad.place_order(order_req()))


def test_market_path_bad_json_is_retriable_not_a_silent_none():
    """2xx 但不是 JSON：返回 None 会让下游解析炸 AttributeError，retriable=False
    更糟——那是给重发开许可证。必须是 BAD_JSON + retriable=True。"""
    rec = Recorder({("GET", "/api/v1/depth"):
                    httpx.Response(200, content=b"<html>gateway</html>")})
    with adapter(rec) as ad:
        with pytest.raises(ExchangeError) as ei:
            run(ad.fetch_book_top("BTC_USDC_PERP"))
    assert ei.value.code == "BAD_JSON" and ei.value.retriable is True


# ---- 撤单 / 查单 / 消解 ---------------------------------------------------

def test_cancel_swallows_404_and_accepts_202():
    """404 RESOURCE_NOT_FOUND = 单子不在簿上（已成交/已撤/从未存在，此处分不出）
    ——撤单的目的（状态终结）已达成，就地吞掉让消解流程继续去查成交量。
    202 = 受理未执行（空 body），不是终态但也不该抛。"""
    rec = Recorder({("DELETE", "/api/v1/order"):
                    err("RESOURCE_NOT_FOUND", "not on book", status=404)})
    with adapter(rec) as ad:
        run(ad.cancel_order("BTC_USDC_PERP", "leg1-0001"))       # 不抛
    assert len(rec.requests) == 1

    rec2 = Recorder({("DELETE", "/api/v1/order"): httpx.Response(202)})
    with adapter(rec2) as ad:
        run(ad.cancel_order("BTC_USDC_PERP", "leg1-0001"))       # 不抛
    assert len(rec2.requests) == 1


def test_risk_path_errors_are_catchable_by_the_resolver():
    """base.resolve_unknown_order 只捕获 (OrderRejected, httpx.HTTPError,
    RateLimited)——可重试的错误必须重挂到 RateLimited，否则网络抖动让消解流程在
    最危险的时刻带着未捕获异常崩掉。反过来，不可重试的鉴权错原样上抛，**绝不降级
    成 OrderRejected**——降级等于谎报"成交量为 0"，上层照着 0 再补一次仓。"""
    rec = Recorder({("DELETE", "/api/v1/order"): err("SERVER_ERROR", status=500)})
    with adapter(rec) as ad:
        with pytest.raises(RateLimited) as ei:
            run(ad.cancel_order("BTC_USDC_PERP", "leg1-0001"))
        assert ei.value.retriable is True and ei.value.code == "SERVER_ERROR"

    rec2 = Recorder({("GET", "/api/v1/order"): lambda r: (_ for _ in ()).throw(
        httpx.ReadTimeout("read timeout", request=r))})
    with adapter(rec2) as ad:
        with pytest.raises(RateLimited) as ei2:
            run(ad.query_order("BTC_USDC_PERP", "leg1-0001"))
        assert ei2.value.code == "NETWORK"

    rec3 = Recorder({("GET", "/api/v1/order"): err("UNAUTHORIZED", status=401)})
    with adapter(rec3) as ad:
        with pytest.raises(ExchangeError) as ei3:
            run(ad.query_order("BTC_USDC_PERP", "leg1-0001"))
        assert ei3.value.code == "UNAUTHORIZED"
        assert not isinstance(ei3.value, (OrderRejected, RateLimited))


def test_query_order_on_book_hit_needs_no_history_call():
    """还挂在簿上的单一段就查到，不许多打历史端点（/wapi/1 桶只有 30 req/min）。"""
    rec = Recorder({("GET", "/api/v1/order"):
                    json_response(order_row("PartiallyFilled", "0.001", "64.5752"))})
    with adapter(rec) as ad:
        res = run(ad.query_order("BTC_USDC_PERP", "leg1-0001"))
    assert res.status == "partial" and res.filled_qty == Decimal("0.001")
    assert res.avg_price == Decimal("64575.2")
    assert len(rec.requests) == 1


def test_query_order_404_falls_through_to_history_because_filled_is_also_404():
    """**404 绝不等于"从未被接受"**——已成交的单在 orderQuery 也是 404。必须转
    /wapi/v1/history/orders 按 clientId 自配（端点没有 clientId 服务端过滤）。
    历史行的 clientId 是 string（类型漂移实锤），str() 归一后两种形状都要吃。"""
    def history(request: httpx.Request) -> httpx.Response:
        verify_wire_signature(request)
        q = dict(request.url.params)
        assert q["symbol"] == "BTC_USDC_PERP"
        assert q["limit"] == str(ORDER_HISTORY_PAGE_SIZE)
        assert q["offset"] == "0" and q["sortDirection"] == "Desc"
        return json_response([hist_row(str(CLIENT_U32))])       # string clientId

    rec = Recorder({("GET", "/api/v1/order"):
                    err("RESOURCE_NOT_FOUND", "not on book", status=404),
                    ("GET", "/wapi/v1/history/orders"): history})
    with adapter(rec) as ad:
        res = run(ad.query_order("BTC_USDC_PERP", "leg1-0001"))
    assert res.status == "filled" and res.filled_qty == Decimal("0.005")
    assert res.avg_price == Decimal("64575.2")
    assert res.client_order_id == "leg1-0001"
    assert len(rec.requests) == 2


def test_query_order_history_pages_until_match_then_gives_up_at_the_cap():
    """首页 1000 条没配上就翻下一页（offset 步进 = 页大小）；3 页 3000 条都没有
    才允许定性"从未被接受"——那时抛 OrderRejected 让消解流程返回 0。"""
    def history(request: httpx.Request) -> httpx.Response:
        offset = int(dict(request.url.params)["offset"])
        if offset == 0:
            return json_response([hist_row(i) for i in range(ORDER_HISTORY_PAGE_SIZE)])
        return json_response([hist_row(CLIENT_U32, "Expired", "0.002", "129.1504")])

    assert CLIENT_U32 > ORDER_HISTORY_PAGE_SIZE     # 填充行绝不撞真身
    rec = Recorder({("GET", "/api/v1/order"):
                    err("RESOURCE_NOT_FOUND", status=404),
                    ("GET", "/wapi/v1/history/orders"): history})
    with adapter(rec) as ad:
        res = run(ad.query_order("BTC_USDC_PERP", "leg1-0001"))
    assert res.status == "canceled" and res.filled_qty == Decimal("0.002")
    offsets = [dict(r.url.params)["offset"] for r in rec.requests[1:]]
    assert offsets == ["0", str(ORDER_HISTORY_PAGE_SIZE)]

    # 页页全是别人的单：3 页闸到就停，绝不无限翻
    rec2 = Recorder({("GET", "/api/v1/order"):
                     err("RESOURCE_NOT_FOUND", status=404),
                     ("GET", "/wapi/v1/history/orders"): lambda r: json_response(
                         [hist_row(7) for _ in range(ORDER_HISTORY_PAGE_SIZE)])})
    with adapter(rec2) as ad:
        with pytest.raises(OrderRejected) as ei:
            run(ad.query_order("BTC_USDC_PERP", "leg1-0001"))
        assert ei.value.code == "ORDER_NOT_FOUND"
    assert len(rec2.requests) == 1 + 3          # orderQuery + 3 页历史


def test_missing_order_after_both_stages_is_order_rejected():
    """两段都查无此单 → OrderRejected（resolve_unknown_order 靠它判定"从未被接受"
    并返回 0）。历史端点空列表 = 这个账户就没这单。"""
    rec = Recorder({("GET", "/api/v1/order"): err("RESOURCE_NOT_FOUND", status=404),
                    ("GET", "/wapi/v1/history/orders"): json_response([])})
    with adapter(rec) as ad:
        with pytest.raises(OrderRejected) as ei:
            run(ad.query_order("BTC_USDC_PERP", "leg1-0001"))
    assert ei.value.code == "ORDER_NOT_FOUND"


def test_resolve_unknown_order_end_to_end():
    """消解流程端到端：cancel（404=已终结，吞掉）→ 查单 404 → 历史配到 Expired
    部成单 → 返回真实成交量 0.002（单位就是 base 币，无需换算）。
    交易所两段都不认识这单时返回 0。"""
    rec = Recorder({
        ("DELETE", "/api/v1/order"): err("RESOURCE_NOT_FOUND", status=404),
        ("GET", "/api/v1/order"): err("RESOURCE_NOT_FOUND", status=404),
        ("GET", "/wapi/v1/history/orders"): json_response(
            [hist_row(CLIENT_U32, "Expired", "0.002", "129.1504")])})
    with adapter(rec) as ad:
        assert run(ad.resolve_unknown_order("BTC_USDC_PERP", "leg1-0001")) == \
            Decimal("0.002")

    rec2 = Recorder({
        ("DELETE", "/api/v1/order"): err("RESOURCE_NOT_FOUND", status=404),
        ("GET", "/api/v1/order"): err("RESOURCE_NOT_FOUND", status=404),
        ("GET", "/wapi/v1/history/orders"): json_response([])})
    with adapter(rec2) as ad:
        assert run(ad.resolve_unknown_order("BTC_USDC_PERP", "leg1-0001")) == \
            Decimal("0")


# ---- 合约表 --------------------------------------------------------------

def test_instrument_precision_fields_and_the_filter_chain():
    """精度逐字段对齐文档：tick_size=filters.price.tickSize、lot_size=stepSize
    （数量单位就是 base 币）、**contract_size=1**（八家里唯一不用换算的）、
    min_notional=0（这家**没有**名义额下限字段，0 是如实表达不是缺数据）、
    funding_interval_s 读逐合约字段（1h！照 8h 肌肉记忆年化低估 8 倍）。
    过滤链：SPOT/IPERP/CancelOnly/隐藏/RWA 股票/坏 tick 一条都不许进。"""
    rec = Recorder({("GET", "/api/v1/markets"): ALL_MARKETS})
    with adapter(rec, seed_meta=False) as ad:
        insts = {i.symbol: i for i in run(ad.fetch_instruments())}
    assert set(insts) == {"BTC_USDC_PERP", "SOL_USDC_PERP", "NOIV_USDC_PERP"}
    btc = insts["BTC_USDC_PERP"]
    assert btc.tick_size == Decimal("0.1")
    assert btc.lot_size == Decimal("0.00001")
    assert btc.contract_size == Decimal("1")
    assert btc.min_notional == Decimal("0")
    assert btc.funding_interval_s == 3600
    assert btc.max_leverage == Decimal("75.00")     # 1/0.01333333 向下取两位
    assert btc.base == "BTC" and btc.quote == "USDC"
    assert btc.market == "backpack:perp"
    assert insts["SOL_USDC_PERP"].lot_size == Decimal("0.01")
    assert insts["SOL_USDC_PERP"].max_leverage == Decimal("50.00")
    # fundingInterval 缺失 → 兜底 3600（这家全场 1h），绝不兜 8h
    assert insts["NOIV_USDC_PERP"].funding_interval_s == 3600


def test_ensure_meta_fallback_keeps_delisted_perp_but_blocks_other_market_types():
    """兜底的单市场路径是给**下架/暂停**仓位留的：CancelOnly 的 PERP 必须能进
    _meta（仓里可能还有仓位，持仓是真值）；但 marketType 是单位/语义判据，
    IPERP（反向合约）从后门进来会让上层拿错口径——必须过与主路径同款的守卫。"""
    delisted = {**SOL_MARKET, "symbol": "DEAD_USDC_PERP",
                "orderBookState": "CancelOnly"}
    inverse = {**SOL_MARKET, "symbol": "INV_USDC_PERP", "marketType": "IPERP"}

    def single(request: httpx.Request) -> httpx.Response:
        sym = dict(request.url.params)["symbol"]
        return json_response(delisted if sym == "DEAD_USDC_PERP" else inverse)

    rec = Recorder({("GET", "/api/v1/markets"): [BTC_MARKET],
                    ("GET", "/api/v1/market"): single})
    with adapter(rec, seed_meta=False) as ad:
        limits = run(ad.fetch_funding_limits("DEAD_USDC_PERP"))
        assert limits == (Decimal("0.015"), Decimal("-0.01"))
        assert "DEAD_USDC_PERP" in ad._meta
        assert run(ad.fetch_funding_limits("INV_USDC_PERP")) is None
        assert "INV_USDC_PERP" not in ad._meta, "反向合约绝不能进 _meta"
        with pytest.raises(ExchangeError) as ei:
            run(ad._meta_of("INV_USDC_PERP"))
        assert ei.value.code == "UNKNOWN_INSTRUMENT"


def test_funding_limits_are_cap_then_floor_in_decimals_not_bps():
    """基类契约 **(cap, floor)** 且 cap >= floor。上下限在 market 行、单位**基点**
    （"150" = 每期 1.5%），必须除以 10000 才和 markPrices 的小数费率同单位——
    漏换算就是 10000 倍的 clamp 错误。SOL 的上下限故意不对称（+150/-100），
    顺序搞反或取绝对值都在这里露馅。查不到/倒挂返回 None，绝不 (0,0)。"""
    with adapter(Recorder({})) as ad:
        assert run(ad.fetch_funding_limits("BTC_USDC_PERP")) == \
            (Decimal("0.01"), Decimal("-0.01"))
        cap, floor = run(ad.fetch_funding_limits("SOL_USDC_PERP"))
        assert (cap, floor) == (Decimal("0.015"), Decimal("-0.01"))
        assert cap > floor and cap != -floor        # 不对称，绝不是绝对值对
        # 缺上下限 → None（补 0 会把费率夹死成 0）
        ad._meta["NOB_USDC_PERP"] = _MarketMeta({
            **SOL_MARKET, "symbol": "NOB_USDC_PERP",
            "fundingRateUpperBound": None, "fundingRateLowerBound": None})
        assert run(ad.fetch_funding_limits("NOB_USDC_PERP")) is None
        # 上下限倒挂 = 脏数据 → None（悄悄交换是替交易所圆谎）
        ad._meta["SWP_USDC_PERP"] = _MarketMeta({
            **SOL_MARKET, "symbol": "SWP_USDC_PERP",
            "fundingRateUpperBound": "-100", "fundingRateLowerBound": "100"})
        assert run(ad.fetch_funding_limits("SWP_USDC_PERP")) is None


# ---- 资金费：当期 --------------------------------------------------------

def test_carry_rate_sign_is_exchange_native_and_interval_is_hourly():
    """**符号用交易所原始约定：正 = 多头付空头，原样存不取反**——取反会让 scoring
    的 short_rate - long_rate 全体反号，把亏钱的对推荐成赚钱的。fundingRate 是
    **小数**（与 market 行的基点上下限单位不同）；26 位小数的原文必须整段进
    Decimal 不掉精度。interval=3600s，年化 ×8760 不是 ×1095。过滤链挡掉的市场
    （CancelOnly）有费率也不产出——免得配出一条下不了单的腿。"""
    rec = Recorder({("GET", "/api/v1/markets"): ALL_MARKETS,
                    ("GET", "/api/v1/markPrices"): MARK_PRICES})
    with adapter(rec, seed_meta=False, fixed_ts=True) as ad:
        rates = {r.symbol: r for r in run(ad.fetch_carry_rates())}
    assert set(rates) == {"BTC_USDC_PERP", "SOL_USDC_PERP"}
    btc = rates["BTC_USDC_PERP"]
    assert btc.rate == Decimal("-0.0000117704808946259223127855")   # 原文不取反
    assert btc.rate < 0 and btc.annualized() < 0
    assert btc.interval_s == 3600
    assert btc.annualized() == btc.rate * Decimal(365 * 24)         # 1h → ×8760
    assert btc.next_settle_ms == 1785949200000
    assert btc.market == "backpack:perp" and btc.ts_ms == FIXED_TS_MS
    assert rates["SOL_USDC_PERP"].rate == Decimal("0.0000125") > 0
    assert "OLD_USDC_PERP" not in rates         # CancelOnly：有费率也不许出现
    assert "BTC_USDC" not in rates              # spot 行无费率字段

    # symbols 过滤生效，不是每次都返回全市场
    rec2 = Recorder({("GET", "/api/v1/markets"): ALL_MARKETS,
                     ("GET", "/api/v1/markPrices"): MARK_PRICES})
    with adapter(rec2, seed_meta=False) as ad:
        only = run(ad.fetch_carry_rates(["SOL_USDC_PERP"]))
    assert [r.symbol for r in only] == ["SOL_USDC_PERP"]


# ---- 资金费：历史 --------------------------------------------------------

def funding_server(total: int = 200, *, seen: list | None = None,
                   ignore_offset: bool = False):
    """假服务端：**降序（最新在前）**、offset 从最新往回数——照抄线上实测行为。
    ignore_offset 模拟"服务端无视 offset"的死循环风险。"""
    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        assert q["symbol"], "symbol 是必填参数"
        limit, offset = int(q["limit"]), int(q["offset"])
        if seen is not None:
            seen.append((offset, limit))
        if ignore_offset:
            offset = 0
        rows = [{"symbol": q["symbol"], "fundingRate": "0.000125",
                 "intervalEndTimestamp": iso_utc(LATEST_SETTLE - i * HOUR_MS)}
                for i in range(offset, min(offset + limit, total))]
        return json_response(rows)
    return handler


def test_funding_history_covers_the_latest_settlement_ascending_unique():
    """三条硬契约一次验完：**覆盖到最新一条**（这家第一页天然含最新，降序 + offset
    往回）、升序返回（服务端给的是降序，必须反转）、无洞无重。"""
    rec = Recorder({("GET", "/api/v1/fundingRates"): funding_server(total=1000)})
    with adapter(rec, fixed_ts=True) as ad:
        hist = run(ad.fetch_funding_history("BTC_USDC_PERP",
                                            since_ms=FIXED_TS_MS - 720 * HOUR_MS))
    stamps = [t for t, _ in hist]
    assert stamps == sorted(stamps), "基类要升序，服务端给的是降序"
    assert len(set(stamps)) == len(stamps)
    assert stamps[-1] == LATEST_SETTLE, "末端必须是最近一次结算"
    assert FIXED_TS_MS - stamps[-1] < HOUR_MS
    assert all(b - a == HOUR_MS for a, b in zip(stamps, stamps[1:]))
    assert stamps[0] >= FIXED_TS_MS - 720 * HOUR_MS
    assert len(hist) == 720 and hist[0][1] == Decimal("0.000125")


def test_funding_history_limit_is_page_size_and_never_truncates_output():
    """`limit` 是**页大小语义**（基类契约），绝不拿它截尾输出——1h 结算下截尾
    1000 条只剩 41 天，90 天窗口被无声斩掉（HL/KuCoin 都修过同根 bug）。
    窗口 48 期、页大小 10 → 5 页、offset 单调步进、48 条一条不少。"""
    seen: list[tuple[int, int]] = []
    rec = Recorder({("GET", "/api/v1/fundingRates"):
                    funding_server(total=60, seen=seen)})
    with adapter(rec, fixed_ts=True) as ad:
        hist = run(ad.fetch_funding_history(
            "BTC_USDC_PERP", since_ms=LATEST_SETTLE - 47 * HOUR_MS - 1, limit=10))
    assert len(hist) == 48 and len(hist) > 10, "limit 不是输出条数上限"
    assert [o for o, _ in seen] == [0, 10, 20, 30, 40]
    assert all(l == 10 for _, l in seen)
    assert hist[-1][0] == LATEST_SETTLE


def test_funding_history_dedups_the_pagination_shift():
    """offset 翻页途中跨过一次整点结算，整个列表下移一位，同一期进两次——
    重复值会抬高 autocorr()、拖偏 median 且不报错。必须按结算时刻去重。"""
    t0 = LATEST_SETTLE
    pages = {0: [t0, t0 - HOUR_MS, t0 - 2 * HOUR_MS],
             3: [t0 - 2 * HOUR_MS, t0 - 3 * HOUR_MS, t0 - 4 * HOUR_MS],  # 下移一位
             6: [t0 - 5 * HOUR_MS, t0 - 6 * HOUR_MS, t0 - 7 * HOUR_MS]}

    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        assert int(q["limit"]) == 3
        return json_response([{"symbol": q["symbol"], "fundingRate": "0.0001",
                               "intervalEndTimestamp": iso_utc(t)}
                              for t in pages[int(q["offset"])]])

    rec = Recorder({("GET", "/api/v1/fundingRates"): handler})
    with adapter(rec, fixed_ts=True) as ad:
        hist = run(ad.fetch_funding_history("BTC_USDC_PERP",
                                            since_ms=t0 - 4 * HOUR_MS, limit=3))
    stamps = [t for t, _ in hist]
    assert stamps == [t0 - i * HOUR_MS for i in range(4, -1, -1)]
    assert len(set(stamps)) == 5, "翻页边界那一期只能计一次"
    assert len(rec.requests) == 3


def test_funding_history_drops_old_rows_and_returns_empty_for_unknown_symbol():
    """早于 since 的行一律丢掉（服务端按页给、不精确切边）。未知符号 200 `[]`
    不报错（线上实测），如实返回空——空列表 ≠ 出错。"""
    rec = Recorder({("GET", "/api/v1/fundingRates"): funding_server(total=100)})
    with adapter(rec, fixed_ts=True) as ad:
        hist = run(ad.fetch_funding_history("BTC_USDC_PERP",
                                            since_ms=FIXED_TS_MS - 5 * HOUR_MS))
    assert [t for t, _ in hist] == [LATEST_SETTLE - i * HOUR_MS
                                    for i in range(4, -1, -1)]
    assert len(rec.requests) == 1

    rec2 = Recorder({("GET", "/api/v1/fundingRates"): json_response([])})
    with adapter(rec2, fixed_ts=True) as ad:
        assert run(ad.fetch_funding_history("GHOST_USDC_PERP", since_ms=1)) == ()
    assert len(rec2.requests) == 1


def test_funding_history_stops_when_the_cursor_stalls():
    """服务端无视 offset（每页返回同一段）时游标不前进——盲翻是死循环。
    最旧时间戳不再变旧就必须停，且远早于 20 页的硬闸。"""
    seen: list[tuple[int, int]] = []
    rec = Recorder({("GET", "/api/v1/fundingRates"):
                    funding_server(total=5, seen=seen, ignore_offset=True)})
    with adapter(rec, fixed_ts=True) as ad:
        hist = run(ad.fetch_funding_history("BTC_USDC_PERP",
                                            since_ms=FIXED_TS_MS - 100 * HOUR_MS,
                                            limit=5))
    assert len(seen) == 2 < FUNDING_HISTORY_MAX_PAGES
    assert len(hist) == 5


def test_funding_history_parses_naive_utc_timestamps_without_local_tz():
    """intervalEndTimestamp 是**无时区的 UTC 串**（"2026-08-05T17:00:00"，没有 Z）。
    naive datetime 直接 .timestamp() 会按本地时区折算——东八区差 8 小时，正好把
    每一期的结算时刻错到隔壁桶。费率字符串必须整段进 Decimal 不经过 float。"""
    rec = Recorder({("GET", "/api/v1/fundingRates"): json_response(
        [{"symbol": "BTC_USDC_PERP", "fundingRate": "-0.000011756",
          "intervalEndTimestamp": "2026-08-05T17:00:00"}])})
    with adapter(rec, fixed_ts=True) as ad:
        hist = run(ad.fetch_funding_history("BTC_USDC_PERP", since_ms=1))
    expected_ms = int(datetime(2026, 8, 5, 17, tzinfo=timezone.utc)
                      .timestamp() * 1000)
    assert hist == ((expected_ms, Decimal("-0.000011756")),)
    assert type(hist[0][1]) is Decimal


# ---- 盘口 ----------------------------------------------------------------

def test_book_top_uses_depth5_and_best_bid_is_the_last_element():
    """这家**没有 REST bookTicker**（ticker 端点无 bid/ask），盘口用 depth?limit=5
    凑（limit 是枚举字符串）。**大坑：bids 升序，best bid 在 bids[-1]**——照币安
    肌肉记忆取 bids[0] 会把买一读成带宽最远端。timestamp 是**微秒**，当毫秒用会让
    is_fresh 永远判陈旧、整条腿停摆。"""
    rec = Recorder({("GET", "/api/v1/depth"): DEPTH_5})
    with adapter(rec) as ad:
        top = run(ad.fetch_book_top("BTC_USDC_PERP"))
    assert dict(rec.last.url.params) == {"symbol": "BTC_USDC_PERP",
                                         "limit": BOOK_TOP_LIMIT}
    assert top.bid_price == Decimal("64575.1") != Decimal("64574.3")
    assert top.bid_qty == Decimal("3.57196")
    assert top.ask_price == Decimal("64575.2")
    assert top.ask_qty == Decimal("0.24612")
    assert top.ts_ms == 1785948116439 and top.ts_ms < 10 ** 14      # 微秒 → 毫秒

    # 时间戳缺失时用本地时钟兜底，**别填 0**：0 会被 is_fresh 判成永远陈旧
    rec2 = Recorder({("GET", "/api/v1/depth"):
                     {k: v for k, v in DEPTH_5.items() if k != "timestamp"}})
    with adapter(rec2, fixed_ts=True) as ad:
        assert run(ad.fetch_book_top("BTC_USDC_PERP")).ts_ms == FIXED_TS_MS


def test_book_depth_accumulates_notional_within_band_and_skips_dirty_levels():
    """带宽内逐档累计名义额（价×量，数量就是 base 币无乘数）。带外档一分不计、
    量为 0 的脏档两项统计都不计（否则"档位够不够盖带宽"被噪声灌水）；
    档位不够时不外推，带宽变窄深度只能变小。"""
    rec = Recorder({("GET", "/api/v1/depth"): DEPTH_100})
    with adapter(rec) as ad:
        d = run(ad.fetch_book_depth("BTC_USDC_PERP", band_bp=10.0))
        narrow = run(ad.fetch_book_depth("BTC_USDC_PERP", band_bp=1.0))
    assert dict(rec.last.url.params)["limit"] == DEPTH_LIMIT
    # 64520×1 + 64560×2 + 64575.1×0.5 = 225927.55（64300 带外、64570×0 是脏档）
    assert d.bid_notional == Decimal("225927.55")
    # 64575.2×0.4 + 64600×1 = 90430.08（64700 带外）
    assert d.ask_notional == Decimal("90430.08")
    assert d.levels_used == 5                       # 3 + 2，脏档不计
    assert d.bid_price == Decimal("64575.1") and d.bid_qty == Decimal("0.5")
    assert d.ask_price == Decimal("64575.2") and d.ask_qty == Decimal("0.4")
    assert d.band_bp == 10.0 and d.ts_ms == 1785948116439
    assert type(d.bid_notional) is Decimal
    # 1bp：lo=64568.69、hi=64581.61 —— 两侧各剩一档
    assert narrow.levels_used == 2
    assert narrow.bid_notional == Decimal("64575.1") * Decimal("0.5")
    assert narrow.ask_notional == Decimal("64575.2") * Decimal("0.4")
    assert narrow.bid_notional < d.bid_notional


def test_book_depth_raises_on_missing_side_and_rejects_bad_band_before_sending():
    """缺 bids/asks **必须抛错，不能退化成深度 0**：scoring 里 depth_cap<=0 会跳过
    容量判断，等于把"没数据"读成"深度无限"。带宽非法在发请求之前就炸。"""
    rec = Recorder({("GET", "/api/v1/depth"):
                    {"asks": [["64575.2", "1"]], "bids": [],
                     "timestamp": 1785948116439400}})
    with adapter(rec) as ad:
        with pytest.raises(ValueError):
            run(ad.fetch_book_depth("BTC_USDC_PERP", band_bp=0.0))
        assert not rec.requests, "带宽非法要在发请求之前就炸"
        with pytest.raises(ExchangeError) as ei:
            run(ad.fetch_book_depth("BTC_USDC_PERP"))
        assert ei.value.code == "EMPTY_BOOK" and ei.value.retriable
        with pytest.raises(ExchangeError) as ei2:
            run(ad.fetch_book_top("BTC_USDC_PERP"))
        assert ei2.value.code == "EMPTY_BOOK"


# ---- 手续费 --------------------------------------------------------------

def test_fee_schedule_is_account_level_bps_converted_to_decimals():
    """实际档位只有签名的 accountQuery 拿得到。futuresMakerFee/TakerFee 是
    **基点串**（"5" = 0.05%），除以 10000；账户级一档吃全市场 → symbol=""。
    符号约定正=成本、maker 负=返佣，与仓库一致**不翻转**。"""
    def handler(request: httpx.Request) -> httpx.Response:
        verify_wire_signature(request)
        return json_response({"futuresMakerFee": "2", "futuresTakerFee": "5",
                              "spotMakerFee": "8", "spotTakerFee": "10",
                              "leverageLimit": "10", "liquidating": False})

    rec = Recorder({("GET", "/api/v1/account"): handler})
    with adapter(rec) as ad:
        fees = run(ad.fetch_fee_schedule(["BTC_USDC_PERP", "SOL_USDC_PERP"]))
    assert len(fees) == 1
    assert fees[0].symbol == "", "账户级档位必须走 symbol='' 通道"
    assert fees[0].maker == Decimal("0.0002")
    assert fees[0].taker == Decimal("0.0005") and fees[0].taker > 0
    assert fees[0].note == ACTUAL_FEE_NOTE
    assert fees[0].market == "backpack:perp"
    assert len(rec.requests) == 1, "账户级一档，无论几个 symbol 都只打一发"

    # 高档位 maker 返佣是负基点；VIP5 maker 恰好为 0 是**真实字段值**不是占位
    rec2 = Recorder({("GET", "/api/v1/account"):
                     {"futuresMakerFee": "-1", "futuresTakerFee": "3"}})
    with adapter(rec2) as ad:
        rebate = run(ad.fetch_fee_schedule([]))
    assert rebate[0].maker == Decimal("-0.0001") < 0
    rec3 = Recorder({("GET", "/api/v1/account"):
                     {"futuresMakerFee": "0", "futuresTakerFee": "1.8"}})
    with adapter(rec3) as ad:
        vip = run(ad.fetch_fee_schedule([]))
    assert vip[0].maker == Decimal("0") and vip[0].taker == Decimal("0.00018")


def test_fee_schedule_never_placeholders_nor_masquerades_listed_rates():
    """查不到就返回空元组：**绝不 maker=0/taker=0 占位**（零费率让四笔 taker 成本
    凭空消失），**绝不把挂牌基础档冒充账户档位**（调用方按"返回了=真档位"处理，
    挂牌价会被标成 real、界面永远不再提示"按默认费率估算"——KuCoin 修过这个病）。
    也不许再补一发 /markets 去凑数。真实错误（401/429/5xx）**上抛**而不是吞成空，
    见 test_fee_schedule_propagates_real_errors_but_not_missing_credentials。"""
    # 字段缺失（响应形状变了）一条都不返回——这是"确定性的没有"，不是错误
    rec = Recorder({("GET", "/api/v1/account"): {"futuresTakerFee": "5"}})
    with adapter(rec) as ad:
        fees = run(ad.fetch_fee_schedule(["BTC_USDC_PERP"]))
    assert fees == ()
    assert len(rec.requests) == 1, "查不到就是查不到，不许再打别的端点凑数"
    assert LISTED_FEE_NOTE not in [f.note for f in fees]


# ---- 持仓 / 挂单 / 杠杆 ---------------------------------------------------

POSITIONS = [
    {"symbol": "BTC_USDC_PERP", "netQuantity": "0.4", "netCost": "25600.2",
     "entryPrice": "64000.5", "markPrice": "64575.6",
     "estLiquidationPrice": "51000.5", "imf": "0.01333333",
     "pnlUnrealized": "230.04", "cumulativeFundingPayment": "-1.2",
     "positionId": "111"},
    {"symbol": "SOL_USDC_PERP", "netQuantity": "-120.55", "netCost": "-18094.6",
     "entryPrice": "150.1", "markPrice": "150.2", "estLiquidationPrice": "0",
     "imf": "0", "positionId": "112"},
]


def test_positions_are_signed_base_coins_with_honest_liquidation_price():
    """netQuantity **正=多、负=空**（净持仓模型），单位就是 base 币直接进 qty。
    estLiquidationPrice 拿到 0 必须填 None——填 0 会让 liquidation_distance 算出
    "还有 100% 空间"，风控直接失明。零仓账户返回空序列（session.validate_account
    拿它做连通性自检）。符号过滤在客户端做，不带 symbol 参数。"""
    def handler(request: httpx.Request) -> httpx.Response:
        verify_wire_signature(request)
        assert not request.url.query, "全量拉回来客户端筛，不发 symbol 参数"
        return json_response(POSITIONS)

    rec = Recorder({("GET", "/api/v1/position"): handler})
    with adapter(rec) as ad:
        poss = {p.symbol: p for p in run(ad.fetch_positions())}
        only = run(ad.fetch_positions(["SOL_USDC_PERP"]))
    btc, sol = poss["BTC_USDC_PERP"], poss["SOL_USDC_PERP"]
    assert btc.qty == Decimal("0.4") > 0
    assert btc.entry_price == Decimal("64000.5")
    assert btc.mark_price == Decimal("64575.6")
    assert btc.liquidation_price == Decimal("51000.5")
    assert btc.leverage == Decimal("75.00")         # 1/imf 向下取两位
    assert btc.market == "backpack:perp"
    assert sol.qty == Decimal("-120.55") < 0        # 空头为负
    assert sol.liquidation_price is None
    assert sol.leverage == Decimal("1")             # imf=0 时不硬造杠杆
    assert [p.symbol for p in only] == ["SOL_USDC_PERP"]

    rec2 = Recorder({("GET", "/api/v1/position"): json_response([])})
    with adapter(rec2) as ad:
        assert run(ad.fetch_positions()) == ()


def test_open_orders_map_statuses_and_restore_the_original_coid():
    """挂单列表按 crc32 反查表还原原字符串 coid（判断两腿是否平衡要按原 ID 对账）；
    反查不到（进程重启后）退回数字串，上层按 exchange_order_id 兜底。"""
    rec = Recorder({("GET", "/api/v1/orders"): lambda r: json_response([
        order_row("New", "0", "0", oid="1"),
        order_row("PartiallyFilled", "0.001", "64.5752", client_id=424242,
                  oid="2", order_type="Market"),
    ])})
    with adapter(rec) as ad:
        ad._client_id_u32("leg1-0001")      # 模拟本进程下过这单（填反查表）
        orders = run(ad.fetch_open_orders("BTC_USDC_PERP"))
    assert dict(rec.last.url.params) == {"symbol": "BTC_USDC_PERP"}
    assert [(o.client_order_id, o.status) for o in orders] == \
        [("leg1-0001", "new"), ("424242", "partial")]
    assert orders[1].filled_qty == Decimal("0.001")
    assert orders[1].avg_price == Decimal("64575.2")


def test_set_leverage_is_account_level_and_reads_before_writing():
    """杠杆是**账户级** leverageLimit（没有逐合约端点）。两腿同所对冲时两条腿的
    引擎会各自调一次——**设前先读**，值已一致就不发 PATCH，避免互相打架白烧
    risk 配额。symbol 只能忽略：body 里绝不能出现它。"""
    captured: dict = {}

    def patch(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200)

    rec = Recorder({("GET", "/api/v1/account"): {"leverageLimit": "5"},
                    ("PATCH", "/api/v1/account"): patch})
    with adapter(rec) as ad:
        run(ad.set_leverage("BTC_USDC_PERP", Decimal("10")))
    assert len(rec.requests) == 2
    assert captured == {"leverageLimit": "10"} and "symbol" not in captured

    rec2 = Recorder({("GET", "/api/v1/account"): {"leverageLimit": "10"}})
    with adapter(rec2) as ad:
        run(ad.set_leverage("BTC_USDC_PERP", Decimal("10")))
    assert len(rec2.requests) == 1, "值一致就不该发 PATCH"

    rec3 = Recorder({})
    with adapter(rec3) as ad:
        with pytest.raises(ValueError):
            run(ad.set_leverage("BTC_USDC_PERP", Decimal("0.5")))
    assert not rec3.requests


def test_leverage_tiers_single_tier_from_oi_limit_and_never_empty():
    """没有档位表端点（sqrt IMF 连续增长）：返回单档 (openInterestLimit×markPrice,
    1/imf.base) 让上层至少看得见名义硬顶。标记价拿不到时退化 (Infinity, 上限)，
    **绝不返回空列表**——空列表和"没有档位限制"在上层是两种含义。"""
    rec = Recorder({("GET", "/api/v1/markPrices"): MARK_PRICES})
    with adapter(rec) as ad:
        tiers = run(ad.fetch_leverage_tiers("BTC_USDC_PERP"))
    assert tiers == ((Decimal("4000") * Decimal("64575.6"), Decimal("75.00")),)
    assert dict(rec.last.url.params) == {"symbol": "BTC_USDC_PERP"}

    rec2 = Recorder({("GET", "/api/v1/markPrices"):
                     err("SERVER_ERROR", status=500)})
    with adapter(rec2) as ad:
        degraded = run(ad.fetch_leverage_tiers("BTC_USDC_PERP"))
    assert degraded == ((Decimal("Infinity"), Decimal("75.00")),)
    assert len(degraded) == 1, "退化也不许返回空列表"


def test_order_fees_sum_across_all_fill_pages_with_fill_type_user():
    """订单对象没有手续费字段，fills 历史是唯一来源。只拿首页求和会在被撮合成
    几十笔的 IOC 单上静默少算。fillType=User 滤掉清算/ADL 系统单；
    maker 返佣的负 fee 照常求和（那是收入）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        verify_wire_signature(request)
        q = dict(request.url.params)
        assert q["fillType"] == "User" and q["orderId"] == "9911"
        if int(q["offset"]) == 0:
            return json_response([{"fee": "0.01", "feeSymbol": "USDC"}]
                                 * ORDER_HISTORY_PAGE_SIZE)
        return json_response([{"fee": "0.05", "feeSymbol": "USDC"},
                              {"fee": "-0.02", "feeSymbol": "USDC"}])

    rec = Recorder({("GET", "/wapi/v1/history/fills"): handler})
    with adapter(rec) as ad:
        fee, asset = run(ad.fetch_order_fees("BTC_USDC_PERP", "9911"))
    assert fee == Decimal("10.03"), "两页之和（含负返佣），不是首页之和"
    assert asset == "USDC"
    assert len(rec.requests) == 2


# ---- 服务器时间 ----------------------------------------------------------

def test_server_time_is_bare_text_not_json():
    """/api/v1/time 返回**裸文本毫秒串**（实测 body 就是 `1785948047107`），
    resp.json() 会直接炸。形状变了要报 BAD_TIME 可重试，不许返回垃圾值。"""
    rec = Recorder({("GET", "/api/v1/time"):
                    httpx.Response(200, content=b"1785948047107")})
    with adapter(rec) as ad:
        assert run(ad.fetch_server_time_ms()) == 1785948047107

    rec2 = Recorder({("GET", "/api/v1/time"):
                     httpx.Response(200, content=b"<html>maintenance</html>")})
    with adapter(rec2) as ad:
        with pytest.raises(ExchangeError) as ei:
            run(ad.fetch_server_time_ms())
        assert ei.value.code == "BAD_TIME" and ei.value.retriable


# ---- WebSocket -----------------------------------------------------------

class _FakeWS:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent: list[str] = []

    async def send(self, msg):
        self.sent.append(msg)

    def __aiter__(self):
        async def gen():
            for frame in self.frames:
                yield frame
        return gen()


class _FakeConnect:
    def __init__(self, ws):
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *exc):
        return False


def _ws_fixture(monkeypatch, frames):
    """整个替换 websockets 模块：适配器是延迟导入，替换 sys.modules 即可截获。"""
    import sys
    import types

    ws = _FakeWS(frames)
    fake = types.ModuleType("websockets")
    captured: dict = {}

    def connect(url, **kw):
        captured["url"] = url
        captured["kwargs"] = kw
        return _FakeConnect(ws)

    fake.connect = connect
    monkeypatch.setitem(sys.modules, "websockets", fake)
    return ws, captured


def _drain(ad, symbols, n):
    got = []

    async def go():
        stream = ad.stream_book_top(symbols)
        async for top in stream:
            got.append(top)
            if len(got) >= n:
                break
        await stream.aclose()

    run(go())
    return got


def _bt_frame(u, bid="64575.1"):
    return json.dumps({"stream": "bookTicker.BTC_USDC_PERP",
                       "data": {"e": "bookTicker", "E": 1785948116439400,
                                "s": "BTC_USDC_PERP", "a": "64575.2", "A": "1.5",
                                "b": bid, "B": "2.25", "u": str(u),
                                "T": 1785948116439300}})


def test_ws_subscribe_frame_units_and_out_of_order_drop(monkeypatch):
    """订阅帧是 {"method":"SUBSCRIBE","params":["bookTicker.<symbol>"]}；
    ack/无 data 的帧不产出数据；`u` 序列号回退的是乱序/重放包必须丢——盘口倒着走
    会让收敛循环对着旧价格下单。E 是**微秒**；ping_interval 必须是 None
    （服务器发协议级 Ping，websockets 自动回 Pong，再叠客户端心跳是画蛇添足）。"""
    frames = [json.dumps({"id": 1, "result": None}),
              _bt_frame(100), _bt_frame(99, bid="63000.0"), _bt_frame(101,
                                                                      bid="64575.3")]
    ws, captured = _ws_fixture(monkeypatch, frames)
    with adapter(Recorder({})) as ad:
        got = _drain(ad, ["BTC_USDC_PERP"], 2)
    assert captured["url"] == WS_BASE == "wss://ws.backpack.exchange"
    assert captured["kwargs"]["ping_interval"] is None
    assert json.loads(ws.sent[0]) == {"method": "SUBSCRIBE",
                                      "params": ["bookTicker.BTC_USDC_PERP"]}
    assert sum(1 for m in ws.sent
               if json.loads(m).get("method") == "SUBSCRIBE") == 1
    assert [t.bid_price for t in got] == [Decimal("64575.1"), Decimal("64575.3")], \
        "u=99 的回退包必须被丢掉"
    assert got[0].bid_qty == Decimal("2.25") and got[0].ask_qty == Decimal("1.5")
    assert got[0].ts_ms == 1785948116439            # 微秒 → 毫秒


def test_ws_null_side_yields_nothing_instead_of_zero():
    """某侧簿空时 a/A/b/B 是 **null**：这一拍不可用，返回 None 丢掉——折成 0 价
    会让上层拿"0 元可买"当真。"""
    with adapter(Recorder({})) as ad:
        assert ad._ws_to_top({"b": None, "B": None, "a": "1", "A": "2",
                              "E": 1785948116439400}) is None
        top = ad._ws_to_top({"b": "64575.1", "B": "2", "a": "64575.2", "A": "1",
                             "E": 1785948116439400})
        assert top is not None and top.bid_price == Decimal("64575.1")
        assert top.ts_ms == 1785948116439


def test_ws_filters_invalid_symbols_before_connecting(monkeypatch):
    """符号先过白名单正则再订阅（全大写+下划线）：一个坏符号会让整条订阅被拒。
    全部无效时直接空流，连 WS 都不该去连。"""
    _, captured = _ws_fixture(monkeypatch, [])
    with adapter(Recorder({})) as ad:
        got = _drain(ad, ["bad sym!", "btc_lower", ""], 0)
    assert got == []
    assert "url" not in captured, "全部符号无效时连接都不该建"


# ---- 对抗审查 + 实盘冒烟修复的回归 ----------------------------------------

def test_funding_history_drops_the_unsettled_current_interval():
    """线上实测：fundingRates 第一行是**还没结算的当前周期**，
    intervalEndTimestamp 在未来、费率还在随溢价变化。收进来的话，库的按
    时间戳去重会把半路抓到的临时值永久钉死（之后的回填被去重挡住，永远
    不会被结算终值纠正）。只收 ts <= 现在的行。离线 mock 测不出这条——
    它来自实盘冒烟，钉在这里防回退。"""
    future_iso = iso_utc(FIXED_TS_MS + 2_100_000)        # 35 分钟后结算
    settled1 = iso_utc(FIXED_TS_MS - 1_500_000)
    settled2 = iso_utc(FIXED_TS_MS - 5_100_000)
    rec = Recorder({("GET", "/api/v1/fundingRates"): json_response([
        {"symbol": "BTC_USDC_PERP", "intervalEndTimestamp": future_iso,
         "fundingRate": "0.0009"},                          # 未结算，必须丢
        {"symbol": "BTC_USDC_PERP", "intervalEndTimestamp": settled1,
         "fundingRate": "0.0001"},
        {"symbol": "BTC_USDC_PERP", "intervalEndTimestamp": settled2,
         "fundingRate": "0.0002"},
    ])})
    with adapter(rec, fixed_ts=True) as ad:
        hist = run(ad.fetch_funding_history("BTC_USDC_PERP",
                                            FIXED_TS_MS - 86_400_000))
    assert len(hist) == 2, "未来时间戳的行必须被丢掉"
    assert all(ts <= FIXED_TS_MS for ts, _ in hist)
    assert hist[-1][1] == Decimal("0.0001")


def test_carry_rates_whitelist_is_this_rounds_filter_not_meta_cache():
    """费率白名单必须用**这一轮**过滤链的产出，不能用 _meta 缓存：
    _ensure_meta 为持仓兜底加进来的 CancelOnly 市场会从 _meta 泄漏进白名单，
    下不了单的市场带着费率进机会榜——配出一条根本建不了仓的腿。"""
    cancel_only = {**BTC_MARKET, "symbol": "DEAD_USDC_PERP",
                   "orderBookState": "CancelOnly"}
    rec = Recorder({
        ("GET", "/api/v1/markets"): json_response([BTC_MARKET, cancel_only]),
        ("GET", "/api/v1/markPrices"): json_response([
            {"symbol": "BTC_USDC_PERP", "fundingRate": "0.0001",
             "indexPrice": "64000", "markPrice": "64000",
             "nextFundingTimestamp": FIXED_TS_MS + 3_600_000},
            {"symbol": "DEAD_USDC_PERP", "fundingRate": "0.0099",
             "indexPrice": "1", "markPrice": "1",
             "nextFundingTimestamp": FIXED_TS_MS + 3_600_000},
        ]),
    })
    with adapter(rec, fixed_ts=True) as ad:
        # 模拟持仓路径把下架市场塞进了 _meta（兜底的合法用途）
        ad._meta["DEAD_USDC_PERP"] = _MarketMeta(cancel_only)
        rates = run(ad.fetch_carry_rates())
    assert [r.symbol for r in rates] == ["BTC_USDC_PERP"], \
        "CancelOnly 市场绝不能产出费率行"


def test_load_markets_rebuilds_and_drops_stale_entries():
    """_meta 整体替换、不做增量：下架的市场旧条目不能永远留着
    （HL 的 _load_universe 同病同修）。"""
    gone = {**BTC_MARKET, "symbol": "GONE_USDC_PERP"}
    calls = iter([[BTC_MARKET, gone], [BTC_MARKET]])

    def handler(request):
        return json_response(next(calls))

    rec = Recorder({("GET", "/api/v1/markets"): handler})
    with adapter(rec, fixed_ts=True, seed_meta=False) as ad:
        run(ad._load_markets())
        assert "GONE_USDC_PERP" in ad._meta
        run(ad._load_markets())
        assert "GONE_USDC_PERP" not in ad._meta, "下架市场的旧条目必须被替换洗掉"
        assert "BTC_USDC_PERP" in ad._meta


def test_fee_schedule_propagates_real_errors_but_not_missing_credentials():
    """没配凭据=确定性的没有，返回空；真实错误（429/5xx）必须**上抛**——
    session.refresh_fees 对异常记 fee_errors 并丢旧缓存，对空返回却会建一个
    "已取到（空）"的缓存条目，一次瞬时 429 就让这家 24 小时不再重试且不报错。"""
    rec = Recorder({})          # 无凭据时连请求都不该发
    with adapter(rec, fixed_ts=True, cred=Credential("", "")) as ad:
        assert run(ad.fetch_fee_schedule(())) == ()
    assert not rec.requests

    rec2 = Recorder({("GET", "/api/v1/account"):
                         json_response({"code": "TOO_MANY_REQUESTS"}, status=429)})
    with adapter(rec2, fixed_ts=True) as ad:
        with pytest.raises(ExchangeError):
            run(ad.fetch_fee_schedule(()))
