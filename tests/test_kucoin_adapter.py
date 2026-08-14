"""KuCoin Futures 适配器测试。全程 httpx.MockTransport，一次都不连交易所。

金标准取自 docs/research/exchange-kucoin.md：
  - 「认证签名」：prehash = `timestamp + METHOD + endpoint + body`（四段直接拼接、无分隔
    符），`KC-API-SIGN` 是 **base64 不是 hex**，`KC-API-PASSPHRASE` 同样要 HMAC 再 base64、
    不是明文，endpoint 是**未 URL-encode 的原文**且含查询串，签的时间戳与 header 同值。
  - 「符号模型 / 张 vs 币」：`size` 是**张**，1 张 = multiplier 个 base 币。
  - 「REST 端点」各节的官方响应样例、「错误码与限频」的四张分类表。

KuCoin **没有**公开"某把 key/secret 的签名值"这种可对照常量，所以最强的校验方式是在
handler 里用**真正上线的请求字节**重算签名再比对 KC-API-SIGN：这同时证明 body 段用的是
httpx 实际发出的字节（适配器为此覆写了 `_request` 走 content=）、query 段用的是原文。

返佣码：本仓库**不注入任何返佣码/邀请码/broker code**（用户明确要求）。下面两条用例把它
钉死——请求里没有任何渠道类痕迹，且带不带码的 body 与签名逐字节相同。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import time
from decimal import Decimal

import httpx
import pytest

from earnfarm.exchanges.base import (
    Credential,
    ExchangeError,
    OrderRejected,
    OrderRequest,
    OrderUnknown,
    RateLimited,
)
from earnfarm.exchanges.kucoin import (
    ACTUAL_FEE_NOTE,
    DEPTH_LEVELS_PER_SIDE,
    DEPTH_PATH,
    LISTED_FEE_NOTE,
    KucoinAdapter,
    _ContractMeta,
)
from earnfarm.models import MarketKind, Venue

API_KEY = "kucoin-key-0001"
API_SECRET = "kucoin-secret-0002"
PASSPHRASE = "kucoin-pass-0003"

# 冻结时间戳：给"同一时刻两次采样"的比对用（带不带返佣码、翻页窗口）
FIXED_TS_MS = 1785548892_000
GRID_8H_MS = 28_800_000

# ---- 合约样例：字段与取值逐条取自调研文档 contracts/active 的官方 example ----------

XBT = {
    "symbol": "XBTUSDTM", "rootSymbol": "USDT", "type": "FFWCSX", "expireDate": None,
    "baseCurrency": "XBT", "quoteCurrency": "USDT", "settleCurrency": "USDT",
    "maxOrderQty": 1000000, "marketMaxOrderQty": 800000,     # 限价/市价上限故意不同
    "lotSize": 1, "tickSize": 0.1, "multiplier": 0.001,
    "makerFeeRate": 2.0e-4, "takerFeeRate": 6.0e-4,
    "isInverse": False, "status": "Open",
    "fundingFeeRate": -1.5e-5, "predictedFundingFeeRate": None,
    "fundingRateGranularity": 28800000, "currentFundingRateGranularity": 28800000,
    "fundingRateCap": 0.003, "fundingRateFloor": -0.003,
    "markPrice": 89131.36, "indexPrice": 89148.12,
    # nextFundingRateTime 是**倒计时**，nextFundingRateDateTime 才是绝对时间戳
    "nextFundingRateTime": 3929820, "nextFundingRateDateTime": 4766736000000,
    "maxLeverage": 125, "supportCross": True,
    "marketStage": "NORMAL", "marketType": "CRYPTO",
}
# 4h 结算；配置值(8h)与当前生效值(4h)**故意不同**，读错字段年化直接低估一半。
# 且没有 nextFundingRateDateTime，逼实现走倒计时那条分支。
DOGE = {
    **XBT, "symbol": "DOGEUSDTM", "baseCurrency": "DOGE", "multiplier": 10,
    "lotSize": 1, "tickSize": 0.00001, "markPrice": 0.12,
    "fundingRateGranularity": 28800000, "currentFundingRateGranularity": 14400000,
    "fundingFeeRate": 0.00125, "fundingRateCap": 0.02, "fundingRateFloor": -0.02,
    "maxLeverage": 50, "maxOrderQty": 5000000, "marketMaxOrderQty": 5000000,
    "nextFundingRateDateTime": None, "nextFundingRateTime": 3929820,
}
# 1h 结算；cap/floor **故意不对称**，用来验证 (cap, floor) 的顺序而不是"取绝对值"
PEPE = {
    **XBT, "symbol": "PEPEUSDTM", "baseCurrency": "PEPE", "multiplier": 10000,
    "tickSize": 0.0000001, "markPrice": 0.000012,
    "currentFundingRateGranularity": 3600000, "fundingFeeRate": 0.0001,
    "fundingRateCap": 0.005, "fundingRateFloor": -0.002,
}
# 下面这些必须被过滤链滤掉，一条都不许进 instruments / carry
FUTURE = {**XBT, "symbol": "XBTMH25", "type": "FFICSX"}          # 交割合约
EXPIRING = {**XBT, "symbol": "XBTMU25", "expireDate": 1774000000000}
STOCK = {**XBT, "symbol": "TSLAUSDTM", "marketType": "NASDAQ"}   # 股票永续
PAUSED = {**XBT, "symbol": "PAUSEDUSDTM", "status": "Paused"}
INVERSE = {**XBT, "symbol": "XBTUSDM", "isInverse": True, "settleCurrency": "XBT"}
PREMARKET = {**XBT, "symbol": "NEWUSDTM", "marketStage": "PRE_MARKET"}
BAD_MULT = {**XBT, "symbol": "BADUSDTM", "multiplier": 0}        # 换算的分母，坏行要跳过
NO_RATE = {**XBT, "symbol": "NORATEUSDTM", "fundingFeeRate": None}

ALL_CONTRACTS = [XBT, DOGE, PEPE, FUTURE, EXPIRING, STOCK, PAUSED, INVERSE,
                 PREMARKET, BAD_MULT]

# 下单响应**只给 ID、不给任何成交信息**（Add Order 节）
ORDER_ACK = {"orderId": "234125150956625920", "clientOid": "leg1-0001"}
# 查单样例取自 Get Order By ClientOid（数值换成 XBTUSDTM 口径）
ORDER_DONE = {
    "id": "250444645610336256", "symbol": "XBTUSDTM", "type": "limit", "side": "buy",
    "price": "89130.5", "size": 5, "dealValue": "445.65", "dealSize": 5,
    "timeInForce": "IOC", "postOnly": False, "leverage": "3",
    "clientOid": "leg1-0001", "isActive": False, "cancelExist": False,
    "createdAt": 1732523858568, "orderTime": 1732523858550892322,
    "settleCurrency": "USDT", "marginMode": "CROSS", "positionSide": "BOTH",
    "avgDealPrice": "89130.5", "filledSize": 5, "filledValue": "445.65",
    "status": "done", "reduceOnly": False,
}
TICKER = {
    "sequence": 1697895100310, "symbol": "XBTUSDTM", "side": "sell", "size": 2936,
    "price": "89130.4", "bestBidPrice": "89130.4", "bestBidSize": 32345,
    "bestAskPrice": "89130.5", "bestAskSize": 7251,
    "ts": 1729163001780000000,          # **纳秒**
}
# 手算过的簿：mid = (89130.4+89130.5)/2 = 89130.45
# 10bp → lo = 89041.31955、hi = 89219.58045（边界含）
DEPTH_BOOK = {
    "sequence": 1697895963339, "symbol": "XBTUSDTM",
    "bids": [[89130.4, 20000], [89100.0, 30000], [89050.0, 10000], [89041.0, 999999]],
    "asks": [[89130.5, 10000], [89150.0, 25000], [89219.5, 5000], [89220.0, 999999]],
    "ts": 1729168101216000000,          # 两侧最后一档在带外
}


def run(coro):
    return asyncio.run(coro)


def ok(data) -> dict:
    """成功信封。业务错误也走 HTTP 200，所以 code 才是判据。"""
    return {"code": "200000", "data": data}


def err(code: str, msg: str = "boom", status: int = 200) -> httpx.Response:
    return json_response({"code": code, "msg": msg}, status=status)


def json_response(payload, status: int = 200, headers: dict | None = None) -> httpx.Response:
    h = {"gw-ratelimit-limit": "2000", "gw-ratelimit-remaining": "1988",
         "gw-ratelimit-reset": "27000"}
    h.update(headers or {})
    return httpx.Response(status, json=payload, headers=h)


def expected_sign(request: httpx.Request) -> str:
    """按文档四段式，用**真正上线的请求字节**独立重算签名：endpoint = path + '?' +
    原始未编码 query，body 段直接取 request.content —— 这一步逼"签的字符串"和"发的
    字节"必须是同一个。"""
    query = request.url.query.decode()
    endpoint = request.url.path + (f"?{query}" if query else "")
    prehash = (request.headers["KC-API-TIMESTAMP"] + request.method.upper()
               + endpoint + request.content.decode())
    return base64.b64encode(
        hmac.new(API_SECRET.encode(), prehash.encode(), hashlib.sha256).digest()).decode()


class Recorder:
    """按 (method, path) 路由并记录所有出站请求。

    未登记的请求直接 AssertionError —— 比返回 404 更早暴露"路径写错/多发一次请求"。
    """

    def __init__(self, routes: dict | None = None, default=None):
        self.routes = routes or {}
        self.default = default
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
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
                 seed_meta: bool = True, fixed_ts: bool = False, **kw) -> KucoinAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder),
                               timeout=httpx.Timeout(1.0))
    ad = KucoinAdapter(Credential(API_KEY, API_SECRET, PASSPHRASE),
                       broker_code=broker_code, client=client, **kw)
    # 冻结校时：否则每个用例都要伺候 /api/v1/timestamp，请求记录还会被校时请求搅浑
    ad._clock.synced_at = time.time()
    ad._clock.offset_ms = 0
    if fixed_ts:
        ad.timestamp_ms = lambda: FIXED_TS_MS
    if seed_meta:
        for raw in (XBT, DOGE, PEPE):
            ad._meta[raw["symbol"]] = _ContractMeta(raw)
    return ad


@contextlib.contextmanager
def adapter(recorder: Recorder, **kw):
    ad = make_adapter(recorder, **kw)
    try:
        yield ad
    finally:
        run(ad.close())


def order_req(**kw) -> OrderRequest:
    base = dict(symbol="XBTUSDTM", side="buy", qty=Decimal("0.005"),
                price=Decimal("89130.47"), time_in_force="IOC",
                client_order_id="leg1-0001")
    base.update(kw)
    return OrderRequest(**base)


def body_capture(rec_key: dict, resp=None):
    """把请求体抓进 dict 的通用 handler。"""
    def handler(request: httpx.Request) -> httpx.Response:
        rec_key.clear()
        rec_key.update(json.loads(request.content))
        return json_response(resp if resp is not None else ok(ORDER_ACK))
    return handler


# ---- 签名 ---------------------------------------------------------------

def test_prehash_layout_and_header_encodings_match_the_doc():
    """逐字对齐文档「认证签名」节的两条 prehash 示例：四段之间**没有任何分隔符**
    （别把别家的 `\\n` 拼串习惯带过来），GET 的参数全在 endpoint 里、body 段是空串。

    编码也一并钉死：SIGN 写成 hexdigest 稳定收 400005；passphrase 发明文稳定收 400004，
    而 400004 的文案是"passphrase 错"、看起来像用户填错了密码，最容易查错方向。
    """
    assert KucoinAdapter._prehash(
        "1768982284539", "GET", "/api/v1/orders/byClientOid?clientOid=arb-1a2b", ""
    ) == "1768982284539GET/api/v1/orders/byClientOid?clientOid=arb-1a2b"
    body = '{"clientOid":"arb-1a2b","symbol":"XBTUSDTM"}'
    assert KucoinAdapter._prehash("1768982284539", "post", "/api/v1/orders", body) == \
        '1768982284539POST/api/v1/orders{"clientOid":"arb-1a2b","symbol":"XBTUSDTM"}'
    rec = Recorder({("POST", "/api/v1/orders"): ok(ORDER_ACK)})
    with adapter(rec) as ad:
        run(ad.place_order(order_req()))
    h = rec.last.headers
    assert len(base64.b64decode(h["KC-API-SIGN"])) == 32    # 摘要原文，不是 64 位 hex
    assert h["KC-API-PASSPHRASE"] != PASSPHRASE
    assert h["KC-API-PASSPHRASE"] == base64.b64encode(
        hmac.new(API_SECRET.encode(), PASSPHRASE.encode(), hashlib.sha256).digest()).decode()
    assert h["KC-API-KEY"] == API_KEY
    assert h["KC-API-KEY-VERSION"] == "3"                   # 字符串，不是数字
    assert h["Content-Type"] == "application/json"          # 缺了是 415000
    assert h["KC-API-TIMESTAMP"].isdigit() and len(h["KC-API-TIMESTAMP"]) == 13   # 毫秒


def test_signed_post_signature_matches_wire_bytes():
    """用**真正发出去的 body 字节**重算签名，必须和 KC-API-SIGN 一致 —— 这同时证明覆写
    `_request` 是有效的：签名串来自 `_dumps(body)`，httpx 发出去的也是同一个字符串
    （content= 而不是 json=）。文档原文 "Do not include extra spaces"。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["sign"], seen["expected"] = request.headers["KC-API-SIGN"], expected_sign(request)
        seen["body"] = request.content
        return json_response(ok(ORDER_ACK))

    rec = Recorder({("POST", "/api/v1/orders"): handler})
    with adapter(rec) as ad:
        run(ad.place_order(order_req()))
    assert seen["sign"] == seen["expected"]
    assert b", " not in seen["body"] and b": " not in seen["body"]
    assert seen["body"] == json.dumps(json.loads(seen["body"]), ensure_ascii=False,
                                      separators=(",", ":")).encode()


def test_signed_get_and_delete_put_params_in_the_url_and_sign_the_raw_query():
    """GET/DELETE 的参数必须进 URL（不是 body），形状与文档示例
    `/api/v1/orders/byClientOid?clientOid=arb-1a2b` 一致；撤单的 clientOid 走路径、
    **symbol 是必填查询参数**（漏了直接参数错）。"""
    def query(request: httpx.Request) -> httpx.Response:
        assert request.content == b"", "GET 不许带 body，带了签名两边就分叉了"
        assert request.url.query.decode() == "clientOid=leg1-0001"
        assert request.headers["KC-API-SIGN"] == expected_sign(request)
        return json_response(ok(ORDER_DONE))

    def cancel(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/orders/client-order/leg1-0001"
        assert request.url.query.decode() == "symbol=XBTUSDTM"
        assert request.headers["KC-API-SIGN"] == expected_sign(request)
        return json_response(ok({"clientOid": "leg1-0001"}))

    rec = Recorder({("GET", "/api/v1/orders/byClientOid"): query,
                    ("DELETE", "/api/v1/orders/client-order/leg1-0001"): cancel})
    with adapter(rec) as ad:
        run(ad.query_order("XBTUSDTM", "leg1-0001"))
        run(ad.cancel_order("XBTUSDTM", "leg1-0001"))
    assert len(rec.requests) == 2          # 确认两个 handler 里的断言都跑过


def test_timestamp_header_and_prehash_are_the_same_string():
    """签一个时间戳、发另一个是最常见的 400005。这里让 timestamp_ms() 每次调用都返回
    不同的值：实现若取了两次（一次进签名一次进 header），重算出来就对不上。"""
    ticks = iter(range(1785548892_000, 1785548892_000 + 10_000, 1_000))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["KC-API-SIGN"] == expected_sign(request)
        return json_response(ok(ORDER_ACK))

    rec = Recorder({("POST", "/api/v1/orders"): handler})
    with adapter(rec) as ad:
        ad.timestamp_ms = lambda: next(ticks)
        run(ad.place_order(order_req()))
    assert len(rec.requests) == 1


def test_public_endpoints_carry_no_credentials():
    """公开端点一律不签名（多发 KC-API-* 只会在没配 key 的 public_feed 场景下把一整家
    行情打没），且 /api/v1/timestamp 的 `data` **直接是数字**、不是对象。"""
    rec = Recorder({("GET", "/api/v1/timestamp"): ok(1729260030774),
                    ("GET", "/api/v1/contracts/active"): ok(ALL_CONTRACTS),
                    ("GET", DEPTH_PATH): ok(DEPTH_BOOK)})
    with adapter(rec) as ad:
        assert run(ad.fetch_server_time_ms()) == 1729260030774
        run(ad.fetch_instruments())
        run(ad.fetch_book_depth("XBTUSDTM"))
    assert len(rec.requests) == 3
    for req in rec.requests:
        assert not any(k.lower().startswith("kc-api") for k in req.headers), req.url


# ---- 返佣码：一个字节都不许出现 ------------------------------------------

def test_no_referral_trace_anywhere():
    """用户明确要求不加返佣码：请求里不能有任何渠道类 header/字段，clientOid 也不许被加
    前缀（加了会破坏上层按原 ID 对账的假设）。"""
    rec = Recorder({("POST", "/api/v1/orders"): ok(ORDER_ACK)})
    with adapter(rec, broker_code="somepartnercode") as ad:
        run(ad.place_order(order_req()))
    req = rec.last
    banned = ("referral", "referrer", "broker", "channel", "partner", "affiliate",
              "invite", "rebate", "recvwindow")
    for name in req.headers:
        assert not any(b in name.lower() for b in banned), name
    assert "somepartnercode" not in str(req.url)
    assert all("somepartnercode" not in v for v in req.headers.values())
    assert b"somepartnercode" not in req.content
    body = json.loads(req.content)
    assert set(body) == {"clientOid", "symbol", "side", "type", "size",
                         "marginMode", "price", "timeInForce"}
    assert body["clientOid"] == "leg1-0001"        # 无前缀、无后缀


def test_signature_is_byte_identical_with_and_without_broker_code():
    """返佣码对这家**完全不存在**，所以同一时刻带不带码，body 与签名必须逐字节相同。
    任何"悄悄塞进 remark/stp"的实现都会在这里露馅。"""
    seen: list[tuple[bytes, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.content, request.headers["KC-API-SIGN"]))
        return json_response(ok(ORDER_ACK))

    rec = Recorder({("POST", "/api/v1/orders"): handler})
    for code in ("", "somepartnercode"):
        with adapter(rec, broker_code=code, fixed_ts=True) as ad:
            run(ad.place_order(order_req()))
    assert seen[0] == seen[1]


# ---- 币 ↔ 张换算：这家最贵的一处错 ----------------------------------------

def test_coins_to_lots_truncates_toward_zero_never_up():
    """币 → 张**向零取整**，绝不四舍五入到超量：向上取整在 XBTUSDTM 上只是多 0.001 BTC，
    在 multiplier=10000 的 PEPE 上多一张就是 10000 个币的裸露。"""
    xbt, doge, pepe = _ContractMeta(XBT), _ContractMeta(DOGE), _ContractMeta(PEPE)
    assert KucoinAdapter.coins_to_lots(Decimal("0.005"), xbt) == 5     # 整除不掉一张
    assert KucoinAdapter.coins_to_lots(Decimal("0.0055"), xbt) == 5    # 5.5 → 5
    assert KucoinAdapter.coins_to_lots(Decimal("0.0019"), xbt) == 1    # 1.9 → 1，不是 2
    assert KucoinAdapter.coins_to_lots(Decimal("27"), doge) == 2       # 2.7 → 2
    assert KucoinAdapter.coins_to_lots(Decimal("45000"), pepe) == 4    # 4.5 → 4
    assert KucoinAdapter.coins_to_lots(Decimal("0.0009"), xbt) == 0    # 不足一张
    # lotSize > 1：整张之后还要按**张数**步长向下对齐
    lot5 = _ContractMeta({**XBT, "multiplier": 1, "lotSize": 5})
    assert KucoinAdapter.coins_to_lots(Decimal("12"), lot5) == 10
    assert KucoinAdapter.coins_to_lots(Decimal("4"), lot5) == 0


def test_place_order_sends_integer_lots_not_coins():
    """size 必须是**张数正整数**。把币数直接发出去有两种死法：0.005 这种非整数被拒
    （看得见），而 5 被当成 5 个 BTC（看不见，1000 倍）。"""
    captured: dict = {}
    rec = Recorder({("POST", "/api/v1/orders"): body_capture(captured)})
    with adapter(rec) as ad:
        run(ad.place_order(order_req(qty=Decimal("0.0055"))))
        assert captured["size"] == 5 and type(captured["size"]) is int
        assert captured["size"] * Decimal("0.001") <= Decimal("0.0055")   # 绝不超量
        assert "qty" not in captured and "valueQty" not in captured
        run(ad.place_order(order_req(symbol="DOGEUSDTM", qty=Decimal("27"),
                                     price=Decimal("0.123456"),
                                     client_order_id="leg2-0001")))
        assert captured["size"] == 2          # 27 币 / 10 = 2.7 → 2 张（= 20 币）
        run(ad.place_order(order_req(symbol="PEPEUSDTM", qty=Decimal("45000"),
                                     price=Decimal("0.0000123"),
                                     client_order_id="leg3-0001")))
        assert captured["size"] == 4


def test_place_order_rejects_locally_when_size_is_pinched_or_capped():
    """取整后归零、或超单笔上限的单本地就拒，别浪费一次 RTT 和 Futures 池的 weight。
    限价看 maxOrderQty(100 万)、市价看 marketMaxOrderQty(80 万)——用错那个会在市价单上
    撞 300000，而错误信息只说"参数非法"。"""
    rec = Recorder({})
    with adapter(rec) as ad:
        with pytest.raises(OrderRejected) as ei:
            run(ad.place_order(order_req(qty=Decimal("0.0009"))))
        assert ei.value.code == "PINCHED"
        with pytest.raises(OrderRejected) as ei2:
            run(ad.place_order(order_req(qty=Decimal("2000"))))     # 200 万张
        assert ei2.value.code == "ORDER_SIZE_TOO_LARGE"
        with pytest.raises(OrderRejected):      # 90 万张：限价放行、市价超上限
            run(ad.place_order(order_req(qty=Decimal("900"), price=None)))
    assert not rec.requests


def test_query_order_converts_filled_lots_back_to_coins():
    """回程同样要换算：filledSize 是**张**，直接当币数会让"已成交 5 个 BTC"进入仓位差分，
    然后上层照着它去补一条根本不存在的腿。"""
    rec = Recorder({("GET", "/api/v1/orders/byClientOid"): ok(ORDER_DONE)})
    with adapter(rec) as ad:
        res = run(ad.query_order("XBTUSDTM", "leg1-0001"))
    assert res.filled_qty == Decimal("0.005") and res.filled_qty != Decimal("5")
    assert res.status == "filled" and res.is_terminal
    assert res.avg_price == Decimal("89130.5")
    assert res.client_order_id == "leg1-0001"
    assert res.exchange_order_id == "250444645610336256"


# ---- 精度与下单体 --------------------------------------------------------

def test_price_rounds_toward_marketability_and_stays_fixed_point():
    """价格按 tickSize 规整：**买单向上、卖单向下**，方向与 executor.cross_price 一致——
    保护限价是故意穿透对手价的，反向取整会把穿透削回来，在 tick 相对价格很粗的小币上
    那不是少赚一个 tick，而是 IOC 根本挂不上。PEPE 的 tick 是 1e-7：Decimal 默认输出
    `1.23E-5`，发出去被当非法参数拒掉。"""
    tick = Decimal("0.1")
    assert KucoinAdapter.round_price(Decimal("89130.47"), tick, "buy") == Decimal("89130.5")
    assert KucoinAdapter.round_price(Decimal("89130.47"), tick, "sell") == Decimal("89130.4")
    assert KucoinAdapter.round_price(Decimal("89130.5"), tick, "buy") == Decimal("89130.5")
    captured: dict = {}
    rec = Recorder({("POST", "/api/v1/orders"): body_capture(captured)})
    with adapter(rec) as ad:
        run(ad.place_order(order_req(symbol="PEPEUSDTM", qty=Decimal("50000"),
                                     price=Decimal("0.00001234567"))))
    assert captured["price"] == "0.0000124"            # 买单向上 + 定点
    assert "E" not in captured["price"] and isinstance(captured["price"], str)


def test_order_body_shape_market_post_only_and_no_fok():
    """市价单不带 price/timeInForce；GTX → GTC + postOnly。Add Order 页只列 GTC/IOC/RPI
    （与 Get Order 页矛盾，UNVERIFIED），以下单页为准：**不发 FOK，也不静默降级成 IOC**
    —— 降级会允许部分成交，那正是隐形单腿的来源。"""
    captured: dict = {}
    rec = Recorder({("POST", "/api/v1/orders"): body_capture(captured)})
    with adapter(rec) as ad:
        run(ad.place_order(order_req(price=None)))
        assert captured["type"] == "market" and captured["side"] == "buy"
        assert "price" not in captured and "timeInForce" not in captured
        assert captured["marginMode"] == "CROSS"
        run(ad.place_order(order_req(time_in_force="GTX")))
        assert captured["timeInForce"] == "GTC" and captured["postOnly"] is True
        with pytest.raises(OrderRejected) as ei:
            run(ad.place_order(order_req(time_in_force="FOK")))
        assert ei.value.code == "TIF_UNSUPPORTED"
    assert len(rec.requests) == 2          # FOK 那一发根本没上网


def test_client_order_id_is_mandatory_and_charset_checked_not_rewritten():
    """clientOid 只收 [0-9A-Za-z_-]、≤40。**不做字符替换**——替换会破坏 ID 的全局唯一性，
    上层拿原 ID 回查就查不到，而幂等正是靠它撑着。所以必须就地炸。"""
    rec = Recorder({})
    with adapter(rec) as ad:
        for bad in ("", "leg1.0001", "leg1:0001", "leg#1", "x" * 41):
            with pytest.raises(ValueError):
                run(ad.place_order(order_req(client_order_id=bad)))
        assert ad._client_oid("leg1-0001_a") == "leg1-0001_a"
    assert not rec.requests


# ---- 合约表 -------------------------------------------------------------

def test_instrument_precision_multiplier_and_settlement_fields():
    """精度/乘数/结算周期逐字段对齐文档：tick_size=tickSize、contract_size=multiplier、
    lot_size=lotSize×multiplier（**币口径**）、funding_interval_s=currentGranularity/1000。
    lot_size 直接报张粒度是最隐蔽的坑：XBTUSDTM 的 lotSize=1，上层拿它给"币数量"取整
    会把 0.5 BTC 的缺口 floor 成 0，这条腿永远补不平。"""
    rec = Recorder({("GET", "/api/v1/contracts/active"): ok(ALL_CONTRACTS)})
    with adapter(rec) as ad:
        insts = {i.symbol: i for i in run(ad.fetch_instruments())}
    btc = insts["XBTUSDTM"]
    assert btc.tick_size == Decimal("0.1")
    assert btc.contract_size == Decimal("0.001")          # multiplier
    assert btc.lot_size == Decimal("0.001")               # 1 张 × 0.001 币
    assert btc.funding_interval_s == 28800
    assert btc.max_leverage == Decimal("125") and btc.market == "kucoin:perp"
    # baseCurrency 是 "XBT" 不是 "BTC"：**原样带出**，XBT→BTC 归 public_feed.normalize_base；
    # 在适配器里偷偷改会让 symbol 和 base 对不上号
    assert btc.base == "XBT" and btc.quote == "USDT"
    # 没有 quote 计价的最小名义额，只能用 一张的币数 × markPrice 估
    assert btc.min_notional == Decimal("0.001") * Decimal("89131.36")
    assert insts["DOGEUSDTM"].lot_size == Decimal("10")   # 10 币/张，残余 delta 的来源
    assert insts["DOGEUSDTM"].funding_interval_s == 14400
    assert insts["PEPEUSDTM"].funding_interval_s == 3600


def test_instruments_filter_chain_drops_every_kind_of_pollution():
    """过滤链缺一不可：交割合约(FFICSX)、带 expireDate 的、股票永续(NASDAQ)、非 Open、
    反向合约、盘前合约，外加 multiplier<=0 的坏行。只按符号后缀 M 猜会把交割合约算进
    carry —— 那种合约到期就没有资金费，年化是纯幻觉。坏行只跳过不抛错：public_feed 把
    fetch_instruments 整个 try/except 吞掉，抛错 = 这家规格整片消失且零提示。"""
    rec = Recorder({("GET", "/api/v1/contracts/active"): ok(ALL_CONTRACTS)})
    with adapter(rec, seed_meta=False) as ad:
        assert [i.symbol for i in run(ad.fetch_instruments())] == \
            ["XBTUSDTM", "DOGEUSDTM", "PEPEUSDTM"]


# ---- 资金费：当期 -------------------------------------------------------

def test_carry_rate_sign_is_exchange_native_in_both_directions():
    """交易所原始约定：正 = 多头付给空头。**原样存，绝不取反**——取反会让 scoring 的
    `short_rate - long_rate` 全体反号，错的方向恰好是"把亏钱的对推荐成赚钱的"。"""
    rec = Recorder({("GET", "/api/v1/contracts/active"): ok(ALL_CONTRACTS + [NO_RATE])})
    with adapter(rec, seed_meta=False) as ad:
        rates = {r.symbol: r for r in run(ad.fetch_carry_rates())}
    assert rates["XBTUSDTM"].rate == Decimal("-0.000015")       # 负的还是负的
    assert rates["XBTUSDTM"].rate < 0 and rates["XBTUSDTM"].annualized() < 0
    assert rates["DOGEUSDTM"].rate == Decimal("0.00125")        # 正的还是正的
    assert rates["DOGEUSDTM"].annualized() > 0
    assert rates["XBTUSDTM"].market == "kucoin:perp"
    # 当期费率缺失就跳过：补 0 会让它看起来像"资金费恰好为零"的可做机会
    assert "NORATEUSDTM" not in rates


def test_carry_interval_uses_current_granularity_and_annualizes_by_it():
    """周期取 currentFundingRateGranularity（**当前生效值**）而不是 fundingRateGranularity
    （下一周期才生效的配置值）。DOGE 两者故意不同：读错就把 4h 当 8h，年化差一倍。"""
    rec = Recorder({("GET", "/api/v1/contracts/active"): ok(ALL_CONTRACTS)})
    with adapter(rec, seed_meta=False) as ad:
        rates = {r.symbol: r for r in run(ad.fetch_carry_rates(["DOGEUSDTM"]))}
    doge = rates["DOGEUSDTM"]
    assert doge.interval_s == 14400 and doge.interval_s != 28800
    assert doge.annualized() == Decimal("0.00125") * (Decimal(365 * 24 * 3600) / 14400)
    assert len(rates) == 1                     # symbols 过滤生效，不是返回全市场


def test_next_settle_prefers_absolute_field_and_advances_stale_snapshots():
    """nextFundingRateDateTime 才是绝对时间戳；nextFundingRateTime 是**倒计时**
    （样例 3929820 ≈ 65 分钟），望文生义会把 1970-01-01 00:01:05 当成下次结算。"""
    rec = Recorder({("GET", "/api/v1/contracts/active"): ok(ALL_CONTRACTS)})
    with adapter(rec, seed_meta=False) as ad:
        rates = {r.symbol: r for r in run(ad.fetch_carry_rates())}
        now = ad.timestamp_ms()
        assert rates["XBTUSDTM"].next_settle_ms == 4766736000000     # 绝对值原样用
        assert abs(rates["DOGEUSDTM"].next_settle_ms - (now + 3929820)) < 5000
        assert rates["DOGEUSDTM"].next_settle_ms > now
        # 过期的绝对时间戳按周期推进到下一个未来时点，否则"离结算还有多久"永远是负的
        nxt = ad._next_settle_ms(_ContractMeta({**XBT,
                                                "nextFundingRateDateTime": now - 100_000}))
        assert nxt > now and (nxt - (now - 100_000)) % GRID_8H_MS == 0


def test_funding_limits_are_cap_then_floor_and_none_when_missing():
    """基类契约是 **(cap, floor)** 且 cap >= floor。PEPE 的上下限故意不对称
    （+0.005 / -0.002），顺序搞反或取绝对值都会在这里露馅。meta 里没有就查
    funding-rate/{symbol}/current；**查不到返回 None，绝不返回 (0,0)** ——
    (0,0) 等于把费率夹死成 0，scoring 会把所有机会判成零收益。"""
    rec = Recorder({
        ("GET", "/api/v1/funding-rate/SOLUSDTM/current"): ok(
            {"symbol": ".SOLUSDTMFPI8H", "value": 6.1e-5,       # data.symbol 是指数符号
             "fundingRateCap": 0.004, "fundingRateFloor": -0.001}),
        ("GET", "/api/v1/funding-rate/EMPTYUSDTM/current"): ok({"value": 1e-5})})
    with adapter(rec) as ad:
        limits = run(ad.fetch_funding_limits("PEPEUSDTM"))
        assert limits == (Decimal("0.005"), Decimal("-0.002")) and limits[0] > limits[1]
        assert not rec.requests, "contracts/active 已带上下限，不该再多发一次请求"
        assert run(ad.fetch_funding_limits("SOLUSDTM")) == (Decimal("0.004"),
                                                            Decimal("-0.001"))
        assert run(ad.fetch_funding_limits("EMPTYUSDTM")) is None


# ---- 资金费：历史 -------------------------------------------------------

def funding_pages(page_cap: int = 20, windows: list | None = None):
    """假服务端：按 8 小时网格**倒序（新 → 旧）**返回 [from, to] 内的结算点。`page_cap`
    模拟"单页条数上限"（文档未写，UNVERIFIED）：服务端保留**最新**那端、截掉最旧的，
    这正是适配器必须顺着 `to` 往旧翻页的原因。"""
    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        assert {"symbol", "from", "to"} <= set(q), "三个参数都是 required"
        frm, to = int(q["from"]), int(q["to"])
        if windows is not None:
            windows.append((frm, to))
        rows, t = [], to - to % GRID_8H_MS
        while t >= frm and len(rows) < page_cap:
            rows.append({"symbol": q["symbol"], "fundingRate": 2.1e-4, "timepoint": t})
            t -= GRID_8H_MS
        return json_response(ok(rows))
    return handler


def test_funding_history_covers_the_latest_settlement_and_is_ascending_deduped():
    """三条硬契约一次验完：**覆盖到最新一条**、升序、去重。覆盖到最新靠把 `to` 设成
    "现在"（这家按 to 端对齐，不像币安从 startTime 往后数 N 条、给出一段停在两周前的
    假序列）。重复值会抬高 autocorr()、拖偏 median 且不报任何错。"""
    rec = Recorder({("GET", "/api/v1/contract/funding-rates"): funding_pages()})
    with adapter(rec, fixed_ts=True) as ad:
        hist = run(ad.fetch_funding_history("XBTUSDTM",
                                            since_ms=FIXED_TS_MS - 30 * 86_400_000))
    stamps = [t for t, _ in hist]
    assert stamps == sorted(stamps), "基类要升序，服务端给的是倒序"
    assert len(set(stamps)) == len(stamps), "翻页边界不能重复计一笔"
    assert stamps[-1] == FIXED_TS_MS - FIXED_TS_MS % GRID_8H_MS
    assert FIXED_TS_MS - stamps[-1] < GRID_8H_MS       # 末端就是最近一次结算
    # 中间不许有洞：游标少减那 1ms 或多减一格都会在某页边界漏掉一笔
    assert all(b - a == GRID_8H_MS for a, b in zip(stamps, stamps[1:]))
    assert stamps[0] >= FIXED_TS_MS - 30 * 86_400_000
    assert len(hist) >= 88 and hist[0][1] == Decimal("0.00021")   # 30 天 × 3 次/天
    # limit 是页大小语义、在这里什么都不做（专门的回归见
    # test_funding_history_ignores_limit_and_returns_the_full_window）


def test_funding_history_pages_backwards_and_window_moves_monotonically():
    """一页装不下就必须翻页，且每页右端要严格变旧——不前进就是死循环。"""
    windows: list[tuple[int, int]] = []
    rec = Recorder({("GET", "/api/v1/contract/funding-rates"):
                    funding_pages(page_cap=8, windows=windows)})
    with adapter(rec, fixed_ts=True) as ad:
        hist = run(ad.fetch_funding_history("XBTUSDTM",
                                            since_ms=FIXED_TS_MS - 10 * 86_400_000))
    assert len(windows) > 1
    assert all(b[1] < a[1] for a, b in zip(windows, windows[1:]))
    # 单页只有 8 条（不到 3 天），翻页后必须真的比单页深
    assert (hist[-1][0] - hist[0][0]) / 3_600_000 > 8 * 8


def test_funding_history_drops_rows_older_than_since_and_stops_on_empty_page():
    """服务端可能无视 from 多给几条：早于 since 的一律丢掉。空页立刻停——
    这个端点没有游标可续，盲翻会把 40 页的闸全烧在空段上。"""
    since = FIXED_TS_MS - 3 * 86_400_000
    rec = Recorder({("GET", "/api/v1/contract/funding-rates"): ok([
        {"fundingRate": 1e-4, "timepoint": FIXED_TS_MS - GRID_8H_MS},
        {"fundingRate": 9e-4, "timepoint": since - 1_000}])})       # 越界，必须丢
    with adapter(rec, fixed_ts=True) as ad:
        hist = run(ad.fetch_funding_history("XBTUSDTM", since_ms=since))
    assert [t for t, _ in hist] == [FIXED_TS_MS - GRID_8H_MS]

    rec2 = Recorder({("GET", "/api/v1/contract/funding-rates"): ok([])})
    with adapter(rec2, fixed_ts=True) as ad:
        assert run(ad.fetch_funding_history("XBTUSDTM", since_ms=since)) == ()
    assert len(rec2.requests) == 1


def test_scientific_notation_survives_as_exact_decimal():
    """数值字段是 JSON number 且带科学计数（`2.1E-4`）。必须在解析层就走 Decimal：
    落到 Decimal(float) 时 2.1E-4 已经变成 0.000209999999999999993。"""
    raw = (b'{"code":"200000","data":[{"symbol":"XBTUSDTM","fundingRate":2.1E-4,'
           b'"timepoint":' + str(FIXED_TS_MS - 1000).encode() + b'}]}')
    rec = Recorder({("GET", "/api/v1/contract/funding-rates"):
                    lambda r: httpx.Response(200, content=raw)})
    with adapter(rec, fixed_ts=True) as ad:
        rate = run(ad.fetch_funding_history("XBTUSDTM",
                                            since_ms=FIXED_TS_MS - 86_400_000))[0][1]
    assert type(rate) is Decimal
    assert rate == Decimal("0.00021") and rate != Decimal(2.1e-4)   # 走过 float 就挂


# ---- 盘口 ---------------------------------------------------------------

def test_book_top_converts_lots_to_coins_and_nanoseconds_to_ms():
    """两个单位坑一次验完：ts 是纳秒（当毫秒用会让 is_fresh 永远判陈旧、整条腿停摆），
    bestBidSize 是张（不乘 multiplier 在 BTC 上是 1000 倍的档位量错误）。
    单边为空要抛错，不能返回一个"看起来能成交"的零。"""
    rec = Recorder({("GET", "/api/v1/ticker"): ok(TICKER)})
    with adapter(rec) as ad:
        top = run(ad.fetch_book_top("XBTUSDTM"))
    assert top.bid_price == Decimal("89130.4")
    assert top.bid_qty == Decimal("32345") * Decimal("0.001")
    assert top.ask_qty == Decimal("7251") * Decimal("0.001")
    assert top.ts_ms == 1729163001780 and top.ts_ms < 10 ** 15
    assert dict(rec.last.url.params) == {"symbol": "XBTUSDTM"}

    rec2 = Recorder({("GET", "/api/v1/ticker"): ok({**TICKER, "bestBidPrice": "",
                                                    "bestBidSize": 0})})
    with adapter(rec2) as ad:
        with pytest.raises(ExchangeError) as ei:
            run(ad.fetch_book_top("XBTUSDTM"))
        assert ei.value.code == "EMPTY_BOOK" and ei.value.retriable


def test_book_depth_accumulates_notional_within_band():
    """带宽内逐档累计，名义额 = 价 × **张数 × multiplier**。漏乘 multiplier 就把 BTC 的
    10bp 深度低估 1000 倍，容量闸门会放行一个盘口根本吃不下的规模。端点用 depth100：
    与 depth20 同为 weight 5，没理由要更浅的一档；snapshot 虽便宜但流量大两个数量级。"""
    rec = Recorder({("GET", DEPTH_PATH): ok(DEPTH_BOOK)})
    with adapter(rec) as ad:
        d = run(ad.fetch_book_depth("XBTUSDTM", band_bp=10.0))
    # 89130.4*20 + 89100*30 + 89050*10 = 1782608 + 2673000 + 890500
    assert d.bid_notional == Decimal("5346108.0")
    # 89130.5*10 + 89150*25 + 89219.5*5 = 891305 + 2228750 + 446097.5
    assert d.ask_notional == Decimal("3566152.5")
    assert d.levels_used == 6                      # 两侧各 3 档，边界档是**含**的
    assert d.bid_qty == Decimal("20") and d.ask_qty == Decimal("10")   # 张 → 币
    assert d.bid_price == Decimal("89130.4") and d.band_bp == 10.0
    assert d.ts_ms == 1729168101216                # 纳秒 → 毫秒
    assert type(d.bid_notional) is Decimal and type(d.ask_notional) is Decimal
    assert d.bid_notional > d.bid_qty * d.bid_price   # 深度必须远大于一档
    assert rec.last.url.path == DEPTH_PATH and DEPTH_LEVELS_PER_SIDE >= 20
    assert "snapshot" not in rec.last.url.path
    # 带外那两档各挂 999999 张（≈890 亿 U），误收一档结果差四个数量级
    assert d.bid_notional < Decimal("10000000") and d.ask_notional < Decimal("10000000")
    with adapter(rec) as ad:                          # 1bp：lo=89121.537、hi=89139.363
        narrow = run(ad.fetch_book_depth("XBTUSDTM", band_bp=1.0))
    assert narrow.levels_used == 2                    # 两侧各只剩一档
    assert narrow.bid_notional == Decimal("89130.4") * Decimal("20")
    assert narrow.ask_notional == Decimal("89130.5") * Decimal("10")
    assert narrow.bid_notional < d.bid_notional       # 带宽变窄深度只能变小


def test_book_depth_converts_contract_size_to_coins():
    """【坑】档位量是**张数**。DOGE 一张 10 币，不换算就把深度低估 10 倍。
    mid = 0.12345；10bp → lo = 0.12332655、hi = 0.12357345。"""
    rec = Recorder({("GET", DEPTH_PATH): ok({
        "sequence": 7, "ts": 1729168101216000000,
        "bids": [[0.12344, 1000], [0.12330, 999999]],
        "asks": [[0.12346, 500], [0.12360, 999999]]})})
    with adapter(rec) as ad:
        d = run(ad.fetch_book_depth("DOGEUSDTM", band_bp=10.0))
    assert d.bid_notional == Decimal("1234.40")        # 0.12344 × (1000 张 × 10 币)
    assert d.bid_notional != Decimal("123.44")         # 把张当币就是这个数
    assert d.ask_notional == Decimal("617.30")
    assert d.levels_used == 2
    assert d.bid_qty == Decimal("10000") and d.ask_qty == Decimal("5000")


def test_book_depth_reports_short_book_honestly_without_extrapolating():
    """档位盖不满带宽时如实返回 levels_used —— 宁可低估也别按簿密度外推补足。
    1% 带宽在 BTC 上要几百档才盖得满，这里只有 3 档：结果必须恰好等于这 3 档，
    且带宽扩大 10 倍时深度原地不动（外推的话这里会膨胀）。"""
    rec = Recorder({("GET", DEPTH_PATH): ok({
        "sequence": 9, "ts": 1729168101216000000,
        "bids": [[89130.4, 20000], [89100.0, 10000]], "asks": [[89130.5, 10000]]})})
    with adapter(rec) as ad:
        wide = run(ad.fetch_book_depth("XBTUSDTM", band_bp=100.0))
        narrow = run(ad.fetch_book_depth("XBTUSDTM", band_bp=10.0))
    assert wide.levels_used == 3
    assert wide.bid_notional == Decimal("89130.4") * 20 + Decimal("89100.0") * 10
    assert wide.ask_notional == Decimal("89130.5") * 10
    assert narrow.levels_used == 3 and narrow.bid_notional == wide.bid_notional


def test_book_depth_raises_on_missing_side_and_rejects_bad_band_before_sending():
    """缺 bids/asks **必须抛错，不能退化成深度 0**：scoring 里 depth_cap<=0 会跳过容量
    判断，等于把"没数据"读成"深度无限"。真实原因通常是限频或合约暂停。"""
    rec = Recorder({("GET", DEPTH_PATH): ok({"sequence": 1, "asks": [[89130.5, 10]],
                                             "ts": 1729168101216000000})})
    with adapter(rec) as ad:
        with pytest.raises(ValueError):
            run(ad.fetch_book_depth("XBTUSDTM", band_bp=0.0))
        assert not rec.requests, "带宽非法要在发请求之前就炸"
        with pytest.raises(ExchangeError) as ei:
            run(ad.fetch_book_depth("XBTUSDTM"))
        assert ei.value.code == "EMPTY_BOOK" and ei.value.retriable


def test_ws_token_exchange_and_ticker_units():
    """建连前要先 POST /api/v1/bullet-public 换 token（公开、不签名，weight 10），
    **心跳周期由服务器下发**，别硬编码 30 秒；拿不到 token 要报错，否则表现为一条永远
    不推数据的僵尸流。tickerV2 的 size 同样是**张**、ts 同样是**纳秒**。"""
    rec = Recorder({("POST", "/api/v1/bullet-public"): ok({
        "token": "2neAiuYvAU61ZDXANAGAsiL4",
        "instanceServers": [{"endpoint": "wss://ws-api-futures.kucoin.com/",
                             "pingInterval": 18000, "pingTimeout": 10000}]})})
    with adapter(rec) as ad:
        endpoint, token, ping_s = run(ad._ws_endpoint())
        assert endpoint.startswith("wss://") and token == "2neAiuYvAU61ZDXANAGAsiL4"
        assert ping_s == pytest.approx(14.4)      # 留 20% 余量，pingTimeout 只有 10s
        assert not any(k.lower().startswith("kc-api") for k in rec.last.headers)
        top = ad._ws_to_top("XBTUSDTM", {
            "symbol": "XBTUSDTM", "sequence": 1713516609293, "bestBidSize": 5044,
            "bestBidPrice": "86454.5", "bestAskPrice": "86454.6", "bestAskSize": 73,
            "ts": 1740641976241000000})
        assert top.bid_qty == Decimal("5044") * Decimal("0.001")
        assert top.ts_ms == 1740641976241
        # 没有 multiplier 就换算不出币数，宁可丢这一拍也别报一个错误的量
        assert ad._ws_to_top("GHOSTUSDTM", {"bestBidPrice": "1", "bestBidSize": 1,
                                            "bestAskPrice": "2", "bestAskSize": 1}) is None
        assert ad._ws_to_top("XBTUSDTM", {"bestBidPrice": "", "bestAskPrice": "1"}) is None

    rec2 = Recorder({("POST", "/api/v1/bullet-public"): ok({"instanceServers": []})})
    with adapter(rec2) as ad:
        with pytest.raises(ExchangeError) as ei:
            run(ad._ws_endpoint())
        assert ei.value.code == "WS_TOKEN"


# ---- 手续费 -------------------------------------------------------------

def test_fee_schedule_uses_the_signed_per_symbol_endpoint_and_cost_is_positive():
    """实际档位只有签名的 /api/v1/trade-fees 拿得到，且**一次只能查一个 symbol**。
    符号约定 **正数 = 成本**，与本仓库一致、不取反（只有 OKX 要翻）；
    高 VIP 的 maker 可能为负，那是返佣，保持负号正好当收入。"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["KC-API-SIGN"] == expected_sign(request)
        sym = dict(request.url.params)["symbol"]
        maker = "-0.00005" if sym == "DOGEUSDTM" else "0.0002"
        return json_response(ok({"symbol": sym, "takerFeeRate": "0.0006",
                                 "makerFeeRate": maker}))

    rec = Recorder({("GET", "/api/v1/trade-fees"): handler})
    with adapter(rec) as ad:
        fees = {f.symbol: f for f in run(ad.fetch_fee_schedule(["XBTUSDTM", "DOGEUSDTM"]))}
    # 按**合约**给（不是账户级一档），调用方按 symbol 匹配、绝不按下标
    assert set(fees) == {"XBTUSDTM", "DOGEUSDTM"}
    assert fees["XBTUSDTM"].taker == Decimal("0.0006") and fees["XBTUSDTM"].taker > 0
    assert fees["XBTUSDTM"].maker == Decimal("0.0002")
    assert fees["XBTUSDTM"].note == ACTUAL_FEE_NOTE
    assert fees["DOGEUSDTM"].maker == Decimal("-0.00005") < 0


def test_fee_schedule_never_returns_a_zero_placeholder():
    """查不到就**不返回这一条**。补 maker=0/taker=0 会让四笔 taker 成本凭空消失、
    负期望的机会整片翻成正期望，而且不报任何错。"""
    def handler(request: httpx.Request) -> httpx.Response:
        sym = dict(request.url.params)["symbol"]
        if sym == "XBTUSDTM":
            return json_response(ok({"symbol": sym, "takerFeeRate": "0.0006",
                                     "makerFeeRate": "0.0002"}))
        return err("400007", "permission denied")

    rec = Recorder({("GET", "/api/v1/trade-fees"): handler})
    with adapter(rec) as ad:
        fees = run(ad.fetch_fee_schedule(["XBTUSDTM", "GHOSTUSDTM"]))
    assert [f.symbol for f in fees] == ["XBTUSDTM"]
    assert all(f.maker not in (None, Decimal("0")) for f in fees)


def test_fee_schedule_never_returns_listed_rates_pretending_to_be_real():
    """私有档位拿不到时**什么都不返回**，让上层退回默认挂牌价并在界面标注。

    这里曾经把 contracts/active 的挂牌档包装成 FeeSchedule 返回。问题不在数值
    （数值就是挂牌价），在**渠道语义**：session.refresh_fees 把返回的每一条都存进
    by_symbol，public_feed._fee_for_leg 只认"返回了就是真档位"，fee_basis 因此被
    标成 "real"，界面上永远不再出现"按默认费率估算"——VIP0 挂牌价冒充用户真实档位，
    正是这个工具立项要修的病。基类契约原话："查不到就不要返回这一条。"
    """
    rec = Recorder({("GET", "/api/v1/trade-fees"): err("400007", "permission denied"),
                    ("GET", "/api/v1/contracts/active"): ok(ALL_CONTRACTS)})
    with adapter(rec) as ad:
        fees = run(ad.fetch_fee_schedule(["DOGEUSDTM"]))
        assert fees == (), "查不到真实档位就一条都不许返回"
        empty = run(ad.fetch_fee_schedule([]))
    assert empty == (), "探测被拒时空 symbols 分支同样一条都不许返回"


def test_fee_schedule_empty_symbols_probes_one_signed_call_as_account_tier():
    """symbols 为空（session.refresh_fees 的唯一调用方式）时：
    对一个代表性合约打**一次**签名 trade-fees，作为账户级档位（symbol=""）返回。
    KuCoin 的费率档按账户定级，单点探测足够；绝不逐个查全市场（N×3 weight）。"""
    rec = Recorder({("GET", "/api/v1/trade-fees"):
                        ok({"symbol": "XBTUSDTM", "takerFeeRate": "0.0004",
                            "makerFeeRate": "0.0001"}),
                    ("GET", "/api/v1/contracts/active"): ok(ALL_CONTRACTS)})
    with adapter(rec) as ad:
        fees = run(ad.fetch_fee_schedule([]))
    assert len(fees) == 1
    assert fees[0].symbol == "", "必须走账户级通道（session.account_default）"
    assert fees[0].taker == Decimal("0.0004")
    assert fees[0].maker == Decimal("0.0001")
    probes = [r for r in rec.requests if "/trade-fees" in r.url.path]
    assert len(probes) == 1, "只许探测一次，不许逐个查全市场"


# ---- 错误码映射 ---------------------------------------------------------

@pytest.mark.parametrize("resp", [
    lambda r: err("429000", "Too Many Requests", status=429),
    lambda r: err("429000", "Too Many Requests", status=200),    # 码在 body，HTTP 仍 200
    lambda r: httpx.Response(403, content=b"error code: 1015"),  # Cloudflare 层，非 JSON
])
def test_rate_limit_maps_to_rate_limited(resp):
    """限频必须是 RateLimited（可重试）。1015 在 KuCoin 网关之前，body 可能压根不是
    JSON —— 只看 status_code 或只解析 JSON 都会漏判。"""
    rec = Recorder({("GET", "/api/v1/ticker"): resp})
    with adapter(rec) as ad:
        with pytest.raises(RateLimited) as ei:
            run(ad.fetch_book_top("XBTUSDTM"))
    assert ei.value.retriable is True


def test_200002_is_rate_limit_only_when_the_message_says_so():
    """200002 **一码两义**：业务层限频（封 10 秒）vs Level 3 查询范围超限（参数错）。
    不看 msg 就会把一个永远不会成功的参数错当限频无限重试下去。"""
    rec = Recorder({("GET", "/api/v1/ticker"): err(
        "200002", "Too many requests in a short period of time, please retry later")})
    with adapter(rec) as ad:
        with pytest.raises(RateLimited):
            run(ad.fetch_book_top("XBTUSDTM"))

    rec2 = Recorder({("GET", "/api/v1/ticker"): err(
        "200002", "The query scope for Level 3 cannot exceed 100")})
    with adapter(rec2) as ad:
        with pytest.raises(ExchangeError) as ei:
            run(ad.fetch_book_top("XBTUSDTM"))
        assert not isinstance(ei.value, RateLimited) and ei.value.retriable is False


@pytest.mark.parametrize("boom", [
    lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("read timeout", request=r)),
    lambda r: (_ for _ in ()).throw(httpx.ConnectError("connection reset", request=r)),
    lambda r: (_ for _ in ()).throw(httpx.RemoteProtocolError("half-closed", request=r)),
])
def test_place_order_transport_failures_raise_order_unknown(boom):
    """下单超时/断流**必须**抛 OrderUnknown。当成失败返回会导致重发 → 双倍仓位；
    而且下单响应本来就不带成交信息，"看起来没成交"永远不能当证据。"""
    rec = Recorder({("POST", "/api/v1/orders"): boom})
    with adapter(rec) as ad:
        with pytest.raises(OrderUnknown) as ei:
            run(ad.place_order(order_req()))
    assert ei.value.client_order_id == "leg1-0001" and ei.value.venue == "kucoin"


@pytest.mark.parametrize("resp", [
    lambda r: err("500000", "Internal Server Error", status=500),
    lambda r: err("300015", "Contract is undergoing the settlement process"),
    lambda r: err("300014", "position is being liquidated"),
    lambda r: httpx.Response(200, content=b"<html>blocked</html>"),   # 200 但不是 JSON
    lambda r: err("999999", "who knows"),                             # 认不出的码
])
def test_place_order_treats_everything_unproven_as_unknown(resp):
    """判定标准一句话：只有能证明"没进撮合"时才允许说失败，证不了就是未知。
    多跑一轮 cancel+查单只花一次 RTT，误判是双倍仓位。"""
    rec = Recorder({("POST", "/api/v1/orders"): resp})
    with adapter(rec) as ad:
        with pytest.raises(OrderUnknown):
            run(ad.place_order(order_req()))


@pytest.mark.parametrize("code", ["300003", "300011", "300016", "330011", "200003"])
def test_place_order_explicit_business_reject_is_order_rejected(code):
    """明确的业务拒绝发生在**进撮合之前**，订单确定不存在，改参数后可安全重发。"""
    rec = Recorder({("POST", "/api/v1/orders"): err(code, "rejected")})
    with adapter(rec) as ad:
        with pytest.raises(OrderRejected) as ei:
            run(ad.place_order(order_req()))
    assert ei.value.code == code and ei.value.retriable is False


@pytest.mark.parametrize("code", ["400001", "400005", "400006", "415000", "404000"])
def test_auth_layer_errors_are_not_upgraded_to_unknown(code):
    """鉴权/时间戳/Content-Type 层就被挡下，撮合根本没见过这个请求，升级成 OrderUnknown
    只会白跑一轮 cancel+查单。（OrderUnknown 不是 ExchangeError 的子类，
    所以这个 raises 接不住它 —— 正好当判据。）"""
    rec = Recorder({("POST", "/api/v1/orders"): err(code, "auth")})
    with adapter(rec) as ad:
        with pytest.raises(ExchangeError) as ei:
            run(ad.place_order(order_req()))
    assert ei.value.code == code


def test_clock_skew_marks_the_clock_dirty_for_the_next_request():
    """400002 时差 >5 秒且**没有 recvWindow 可放宽**。收到它要就地把时钟标脏，逼下次
    请求先重新校时——不标脏就会一直撞同一堵墙。"""
    rec = Recorder({("POST", "/api/v1/orders"): err("400002", "Time differs from server")})
    with adapter(rec) as ad:
        assert not ad._clock.is_stale
        with pytest.raises(ExchangeError) as ei:
            run(ad.place_order(order_req()))
        assert ei.value.code == "400002" and ad._clock.is_stale


def test_duplicate_client_oid_goes_to_unknown_not_rejected():
    """300018 = **幂等保护生效的证据**，说明上一发已经进了撮合。报成 OrderRejected
    等于发一张"换个 ID 重发"的许可证 = 双倍仓位。"""
    rec = Recorder({("POST", "/api/v1/orders"): err("300018", "clientOid duplicated")})
    with adapter(rec) as ad:
        with pytest.raises(OrderUnknown):
            run(ad.place_order(order_req()))


def test_rate_limited_on_place_order_stays_rate_limited():
    """限频是下单路径上唯一"确定没进撮合"的可重试错误，允许上层直接安全重试，
    不该被升级成 OrderUnknown 白跑一轮消解。"""
    rec = Recorder({("POST", "/api/v1/orders"): err("429000", "slow down", status=429)})
    with adapter(rec) as ad:
        with pytest.raises(RateLimited):
            run(ad.place_order(order_req()))


# ---- 查单 / 撤单 / 消解 --------------------------------------------------

def test_missing_order_and_terminal_order_land_in_different_buckets():
    """"订单不存在" → **OrderRejected**（resolve_unknown_order 靠它判定"这单从未被接受"
    并返回 0）；这家没有专用码（UNVERIFIED），实测多半是 code=200000 但 data 为空。
    而 100004 说明这单**存在过、可能已成交**，完全不是一回事：混桶会让一张真成交的单被
    报成零成交，上层照着 0 再补一次仓 = 双倍仓位。撤单路径上它是"目的已达成"，就地吞掉。"""
    rec = Recorder({("GET", "/api/v1/orders/byClientOid"): ok(None)})
    with adapter(rec) as ad:
        with pytest.raises(OrderRejected) as ei:
            run(ad.query_order("XBTUSDTM", "leg1-0001"))
        assert ei.value.code == "ORDER_NOT_FOUND"

    gone = err("100004", "order is not in cancellable state")
    rec2 = Recorder({("GET", "/api/v1/orders/byClientOid"): lambda r: gone,
                     ("DELETE", "/api/v1/orders/client-order/leg1-0001"): lambda r: gone})
    with adapter(rec2) as ad:
        with pytest.raises(ExchangeError) as ei2:
            run(ad.query_order("XBTUSDTM", "leg1-0001"))
        assert not isinstance(ei2.value, OrderRejected) and ei2.value.code == "100004"
        run(ad.cancel_order("XBTUSDTM", "leg1-0001"))       # 不抛
    assert len(rec2.requests) == 2


@pytest.mark.parametrize("boom", [
    lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("read timeout", request=r)),
    lambda r: err("500000", "boom", status=500),
])
def test_risk_path_errors_are_catchable_by_the_resolver(boom):
    """`_request` 把所有 httpx 异常收敛成 ExchangeError('NETWORK')，而
    base.resolve_unknown_order 只捕获 (OrderRejected, httpx.HTTPError, RateLimited)
    —— 不转一手，网络抖动会让消解流程在最危险的时刻带着未捕获异常崩掉。"""
    rec = Recorder({("DELETE", "/api/v1/orders/client-order/leg1-0001"): boom})
    with adapter(rec) as ad:
        with pytest.raises(RateLimited) as ei:
            run(ad.cancel_order("XBTUSDTM", "leg1-0001"))
        assert ei.value.retriable is True

    rec2 = Recorder({("GET", "/api/v1/orders/byClientOid"): boom})
    with adapter(rec2) as ad:
        with pytest.raises(RateLimited):
            run(ad.query_order("XBTUSDTM", "leg1-0001"))


    # 反过来，不可重试的错误原样上抛，**绝不降级成 OrderRejected**：
    # 降级等于谎报"成交量为 0"，上层会照着 0 再补一次仓
    rec3 = Recorder({("GET", "/api/v1/orders/byClientOid"): err("400006", "ip not allowed")})
    with adapter(rec3) as ad:
        with pytest.raises(ExchangeError) as ei:
            run(ad.query_order("XBTUSDTM", "leg1-0001"))
    assert not isinstance(ei.value, OrderRejected) and ei.value.code == "400006"


def test_order_status_mapping_open_done_and_cancel_exist():
    """status 只有 open/done 两个值，**没有 canceled**：已撤和已成都是 done。直接把
    status 映射过去会让"被撤的空单"和"全成交的单"长得一模一样。"""
    with adapter(Recorder({})) as ad:
        meta, parse = ad._meta["XBTUSDTM"], ad._parse_order
        assert parse({**ORDER_DONE, "status": "open", "filledSize": 0}, meta).status == "new"
        assert parse({**ORDER_DONE, "status": "open", "filledSize": 2},
                     meta).status == "partial"
        assert parse({**ORDER_DONE, "status": "done", "filledSize": 5, "size": 5},
                     meta).status == "filled"
        canceled = parse({**ORDER_DONE, "status": "done", "filledSize": 0,
                          "cancelExist": True}, meta)
        assert canceled.status == "canceled" and canceled.filled_qty == Decimal("0")
        # 部成后撤：终结态用 canceled 表达，但已成部分必须带出来供上层做仓位差分
        pc = parse({**ORDER_DONE, "status": "done", "filledSize": 2, "size": 5,
                    "cancelExist": True}, meta)
        assert pc.status == "canceled" and pc.is_terminal
        assert pc.filled_qty == Decimal("0.002")
        # 订单响应里没有手续费字段，填 0 是"未知"的诚实表达（精确成本走 fills）
        assert pc.fee == Decimal("0")


def test_resolve_unknown_order_returns_coins_not_lots():
    """消解流程端到端：cancel 终结 → 查单读出真实成交量，单位是**币**；
    交易所压根不认识这单时返回 0。"""
    rec = Recorder({
        ("DELETE", "/api/v1/orders/client-order/leg1-0001"): ok({"clientOid": "leg1-0001"}),
        ("GET", "/api/v1/orders/byClientOid"): ok({**ORDER_DONE, "filledSize": 3,
                                                   "size": 5, "cancelExist": True})})
    with adapter(rec) as ad:
        assert run(ad.resolve_unknown_order("XBTUSDTM", "leg1-0001")) == Decimal("0.003")

    rec2 = Recorder({("DELETE", "/api/v1/orders/client-order/leg1-0001"): ok({}),
                     ("GET", "/api/v1/orders/byClientOid"): ok(None)})
    with adapter(rec2) as ad:
        assert run(ad.resolve_unknown_order("XBTUSDTM", "leg1-0001")) == Decimal("0")


def test_place_order_ack_is_not_a_fill():
    """Ack ≠ 成交：下单响应只有 orderId。把这里的零成交当真会以为没成交，
    IOC 单必须由调用方紧跟一次 query_order。"""
    rec = Recorder({("POST", "/api/v1/orders"): ok(ORDER_ACK)})
    with adapter(rec) as ad:
        res = run(ad.place_order(order_req()))
    assert res.status == "new" and not res.is_terminal
    assert res.filled_qty == Decimal("0") and res.avg_price == Decimal("0")
    assert res.exchange_order_id == "234125150956625920"
    assert res.client_order_id == "leg1-0001"       # 回传调用方原值，上层按它对账


# ---- 持仓 / 挂单 / 杠杆 --------------------------------------------------

POSITIONS = [
    {"id": "500000000001478576", "symbol": "XBTUSDTM", "currentQty": 1, "isOpen": True,
     "markPrice": 120009.57, "markValue": 120.00957, "avgEntryPrice": 120016.4,
     "liquidationPrice": 100475.86, "settleCurrency": "USDT", "marginMode": "ISOLATED",
     "positionSide": "BOTH", "leverage": 6.0, "realLeverage": 6.0, "posMargin": 20.0},
    {"symbol": "DOGEUSDTM", "currentQty": -300, "markPrice": 0.12, "markValue": 120.0,
     "avgEntryPrice": 0.121, "liquidationPrice": 0, "marginMode": "CROSS",
     "positionSide": "BOTH", "leverage": 0, "realLeverage": 0, "posMargin": 20.0},
]


def test_positions_are_signed_coins_with_honest_liquidation_price():
    """currentQty 是**张**、带符号（UNVERIFIED：负数=空头是从字段类型 + 单向模式下
    positionSide 恒为 BOTH 反推的）。liquidationPrice 拿到 0 必须填 None ——
    填 0 会让 liquidation_distance 算出"还有 100% 空间"，风控直接失明。
    零仓账户要正常返回空序列：session.validate_account 拿它做连通性自检。"""
    rec = Recorder({("GET", "/api/v1/positions"): ok(POSITIONS)})
    with adapter(rec) as ad:
        poss = {p.symbol: p for p in run(ad.fetch_positions())}
    btc, doge = poss["XBTUSDTM"], poss["DOGEUSDTM"]
    assert btc.qty == Decimal("0.001")            # 1 张 × 0.001
    assert btc.liquidation_price == Decimal("100475.86") and btc.leverage == Decimal("6.0")
    assert doge.qty == Decimal("-3000")           # -300 张 × 10 币，空头为负
    assert doge.liquidation_price is None
    # 全仓下 realLeverage 缺失/为 0，用 markValue/posMargin 现算——那本就是杠杆的定义
    assert doge.leverage == Decimal("6")
    assert rec.last.headers["KC-API-SIGN"] == expected_sign(rec.last)

    rec2 = Recorder({("GET", "/api/v1/positions"): ok([])})
    with adapter(rec2) as ad:
        assert run(ad.fetch_positions()) == ()


def test_open_orders_must_send_status_active_and_page_through():
    """**status 不传默认是 done** —— 本家最容易踩的默认值：漏传会拿到一堆历史成交单，
    然后这个方法报告"有 50 个未成交挂单"，两腿平衡判断直接崩掉。"""
    pages: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        pages.append(q)
        assert q["status"] == "active"
        page = int(q["currentPage"])
        return json_response(ok({"currentPage": page, "pageSize": int(q["pageSize"]),
                                 "totalNum": 2, "totalPage": 2,
                                 "items": [{**ORDER_DONE, "status": "open",
                                            "filledSize": 0,
                                            "clientOid": f"open-{page}"}]}))

    rec = Recorder({("GET", "/api/v1/orders"): handler})
    with adapter(rec) as ad:
        orders = run(ad.fetch_open_orders("XBTUSDTM"))
    assert [o.client_order_id for o in orders] == ["open-1", "open-2"]
    assert [int(p["currentPage"]) for p in pages] == [1, 2]
    assert all(o.status == "new" for o in orders)


def test_set_leverage_cross_posts_a_string_and_signs_the_exact_bytes():
    """全仓：POST /api/v2/changeCrossUserLeverage，body 里 leverage 是 **string**
    （下单接口里的同名字段却是 integer，两处类型不一致）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.content == b'{"symbol":"XBTUSDTM","leverage":"10"}'
        assert request.headers["KC-API-SIGN"] == expected_sign(request)
        return json_response(ok(True))

    rec = Recorder({("POST", "/api/v2/changeCrossUserLeverage"): handler})
    with adapter(rec) as ad:
        run(ad.set_leverage("XBTUSDTM", Decimal("10.7")))       # 向下取整 = 宁可少借
        assert len(rec.requests) == 1
        with pytest.raises(ValueError):
            run(ad.set_leverage("XBTUSDTM", Decimal("0.5")))


def test_set_leverage_isolated_rides_on_the_order_not_a_separate_endpoint():
    """逐仓**根本没有设杠杆接口**，杠杆随下单参数走。按其余五家的惯例去调全仓那个端点
    会返回 200 `data:true` 而杠杆一点没变——**静默失效**，直到爆仓距离对不上才发现。"""
    captured: dict = {}
    rec = Recorder({("POST", "/api/v1/orders"): body_capture(captured)})
    with adapter(rec, margin_mode="ISOLATED") as ad:
        run(ad.set_leverage("XBTUSDTM", Decimal("7")))
        assert not rec.requests, "逐仓设杠杆一个请求都不该发"
        run(ad.place_order(order_req()))
        assert captured["leverage"] == 7 and captured["marginMode"] == "ISOLATED"
        # 平仓单不带杠杆（文档：只在逐仓开仓时需要）
        run(ad.place_order(order_req(reduce_only=True, client_order_id="leg9-0001")))
        assert "leverage" not in captured and captured["reduceOnly"] is True


def test_leverage_tiers_are_ascending_public_and_never_empty():
    """档位表升序、公开（不签名）。查不到时退化成单档 [(Infinity, maxLeverage)]，
    **绝不返回空列表** —— 空列表和"没有档位限制"在上层是两种完全不同的含义。"""
    rec = Recorder({("GET", "/api/v1/contracts/risk-limit/XBTUSDTM"): ok([
        {"symbol": "XBTUSDTM", "level": 2, "maxRiskLimit": 500000, "maxLeverage": 100},
        {"symbol": "XBTUSDTM", "level": 1, "maxRiskLimit": 100000, "maxLeverage": 125}])})
    with adapter(rec) as ad:
        tiers = run(ad.fetch_leverage_tiers("XBTUSDTM"))
    assert tiers == ((Decimal("100000"), Decimal("125")),
                     (Decimal("500000"), Decimal("100")))
    assert not any(k.lower().startswith("kc-api") for k in rec.last.headers)

    rec2 = Recorder({("GET", "/api/v1/contracts/risk-limit/XBTUSDTM"):
                     err("404000", "url not found")})
    with adapter(rec2) as ad:
        degraded = run(ad.fetch_leverage_tiers("XBTUSDTM"))
    assert len(degraded) == 1 and degraded[0][1] == Decimal("125")
    assert degraded[0][0] == Decimal("Infinity")


# ---- 配额分池 / 边界 -----------------------------------------------------

def test_quota_pools_are_separated_and_rate_limit_headers_are_recorded():
    """深度比 ticker 贵，但必须和行情共用 market 池，绝不能去占撤单/平仓的 risk 配额
    ——那是拿平仓能力换行情；反过来风控池抽干时行情也要照常，否则一次撤单风暴会顺手
    把行情打瞎。gw-ratelimit-* 三件套要记下来：主动退避比等 429 靠谱，
    等到 429 时已被要求"停止访问直到配额重置"，那是整整一个 30 秒窗口的行情空窗。"""
    rec = Recorder({("GET", DEPTH_PATH): ok(DEPTH_BOOK),
                    ("GET", "/api/v1/positions"): ok([]),
                    ("GET", "/api/v1/ticker"): ok(TICKER)})

    async def scenario(ad):
        for _ in range(8):
            await ad._quota["market"].acquire()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ad.fetch_book_depth("XBTUSDTM"), timeout=0.2)
        for _ in range(8):
            ad._quota["market"].release()
        for _ in range(6):
            await ad._quota["risk"].acquire()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ad.fetch_positions(), timeout=0.2)
        top = await asyncio.wait_for(ad.fetch_book_top("XBTUSDTM"), timeout=0.5)
        assert top.bid_price > 0
        for _ in range(6):
            ad._quota["risk"].release()

    with adapter(rec) as ad:
        assert ad.rate_limit_remain is None
        run(scenario(ad))
        assert ad.rate_limit_remain == 1988
        assert ad.rate_limit_reset_ms == 27000      # 毫秒倒计时，固定 30s 窗口


@pytest.mark.parametrize("kw", [{"market_kind": MarketKind.SPOT},
                                {"market_kind": MarketKind.MARGIN},
                                {"market_kind": MarketKind.PERP_COIN},
                                {"hedge_mode": True}])
def test_unsupported_modes_are_explicitly_unimplemented(kw):
    """现货/杠杆在另一个域名、另一套端点与精度字段，调研文档一个字没覆盖；币本位
    1 张 = 1 USD，"币数 = 张数 × multiplier"根本不成立；对冲模式下 buy/sell + reduceOnly
    → LONG/SHORT 的映射是"把仓位做成两倍"的头号原因。没实测就显式拒绝，不瞎猜。"""
    with pytest.raises(NotImplementedError):
        KucoinAdapter(Credential(API_KEY, API_SECRET, PASSPHRASE), **kw)


def test_public_adapter_constructs_with_blank_credentials():
    """public_feed 用 Credential("", "") 构造它跑无凭据公开行情。这里抛错的后果是
    KuCoin 在整个机会榜上消失、只在 init_errors 里留一行。"""
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        Recorder({("GET", "/api/v1/ticker"): ok(TICKER)})))
    ad = KucoinAdapter(Credential("", ""), broker_code="", client=client)
    ad._clock.synced_at = time.time()
    ad._meta["XBTUSDTM"] = _ContractMeta(XBT)
    try:
        assert ad.market == "kucoin:perp"
        assert ad.venue is Venue.KUCOIN and ad.venue.value == "kucoin"
        assert ad.rest_base == "https://api-futures.kucoin.com"   # 不是 api.kucoin.com
        assert run(ad.fetch_book_top("XBTUSDTM")).bid_price == Decimal("89130.4")
    finally:
        run(ad.close())


# ---- 对抗审查修复的回归（金标准：审查报告 + docs/research/exchange-kucoin.md） ----

def test_ensure_meta_fallback_rejects_inverse_contracts():
    """**blocker 回归**：_ensure_meta 的单合约兜底路径必须过和 _load_contracts
    同款的单位模型判据。它曾经裸写 `_meta[...] = _ContractMeta(raw)`，反向合约
    从后门进了 _meta：XBTUSDM 一张是 1 USD，"qty = 张 × multiplier" 对它不成立，
    5000 张多头会被 fetch_positions 报成方向翻转、量级错几万倍的数——
    而持仓是真值，executor 会照着这个数去"补齐"对冲。
    宁可跳过（漏一条也别报一个错误的数），这是 fetch_positions 既有的约定。
    """
    inverse_pos = [{"symbol": "XBTUSDM", "currentQty": 5000, "markPrice": 120000.0,
                    "markValue": 5000.0, "avgEntryPrice": 119000.0,
                    "liquidationPrice": 90000.0, "marginMode": "CROSS",
                    "positionSide": "BOTH", "leverage": 3.0, "realLeverage": 3.0,
                    "posMargin": 10.0}]
    rec = Recorder({("GET", "/api/v1/positions"): ok(inverse_pos),
                    ("GET", "/api/v1/contracts/active"): ok(ALL_CONTRACTS),
                    ("GET", "/api/v1/contracts/XBTUSDM"): ok(INVERSE)})
    with adapter(rec) as ad:
        poss = run(ad.fetch_positions())
    assert poss == (), "反向合约的持仓宁可跳过，也不能折出一个错几万倍的数"
    assert "XBTUSDM" not in ad._meta, "反向合约绝不能进 _meta"


def test_ensure_meta_fallback_rejects_zero_multiplier():
    """multiplier=0 的 meta 会让真实持仓被折成 0 币：executor 照着 0 去 reconcile，
    裸奔检测彻底失明。兜底路径必须带 _load_contracts 同款的 multiplier > 0 守卫。"""
    ghost = {**XBT, "symbol": "GHOSTUSDTM", "multiplier": 0}
    pos = [{"symbol": "GHOSTUSDTM", "currentQty": 7, "markPrice": 1.0,
            "markValue": 7.0, "avgEntryPrice": 1.0, "liquidationPrice": 0.5,
            "marginMode": "CROSS", "positionSide": "BOTH", "leverage": 2.0,
            "realLeverage": 2.0, "posMargin": 3.5}]
    rec = Recorder({("GET", "/api/v1/positions"): ok(pos),
                    ("GET", "/api/v1/contracts/active"): ok(ALL_CONTRACTS),
                    ("GET", "/api/v1/contracts/GHOSTUSDTM"): ok(ghost)})
    with adapter(rec) as ad:
        poss = run(ad.fetch_positions())
    assert poss == ()
    assert "GHOSTUSDTM" not in ad._meta


def test_order_fee_sums_across_all_fill_pages():
    """fills 是分页信封。只拿首页求和，在被撮合成几十笔的 IOC 单上会**静默少算
    手续费且不报错**——而这个方法存在的唯一理由就是精确成本核算。"""
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(dict(request.url.params).get("currentPage", 1))
        items = ([{"fee": "0.25", "feeCurrency": "USDT"}] * 2 if page == 1
                 else [{"fee": "0.05", "feeCurrency": "USDT"}] * 2)
        return json_response(ok({"currentPage": page, "pageSize": 500,
                                 "totalNum": 4, "totalPage": 2, "items": items}))

    rec = Recorder({("GET", "/api/v1/fills"): handler})
    with adapter(rec) as ad:
        fee, asset = run(ad.fetch_order_fee("20001"))
    assert fee == Decimal("0.60"), "两页之和，不是首页之和"
    assert asset == "USDT"
    assert len(rec.requests) == 2


def test_switch_position_mode_sends_a_string_not_a_number():
    """positionMode 按官方文档是 string（"0"/"1"），不是数字。这是 __init__ 拒
    对冲模式时报错文案指的**唯一逃生出口**，它自己发个文档不收的类型，
    用户就被卡死在「下单 330011 → 切换又报参数错」的循环里。"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.content == b'{"positionMode":"0"}', request.content
        assert request.headers["KC-API-SIGN"] == expected_sign(request)
        return json_response(ok(True))

    rec = Recorder({("POST", "/api/v2/position/switchPositionMode"): handler})
    with adapter(rec) as ad:
        run(ad.switch_position_mode(hedge=False))
    assert len(rec.requests) == 1


def test_fetch_position_mode_does_not_swallow_numeric_zero():
    """服务端若回数字 0（单向），`or` 链会把它吞成空串——"单向模式"被读成
    "读不到"。取值必须用 is not None 判断。"""
    rec = Recorder({("GET", "/api/v2/position/getPositionMode"):
                        ok({"positionMode": 0})})
    with adapter(rec) as ad:
        assert run(ad.fetch_position_mode()) == "0"


# ---- 从测试 agent 原稿恢复（修复 agent 中途被会话额度掐死，误删了这一节） ----

def test_prehash_is_four_segments_concatenated_without_separator():
    """逐字对齐文档「认证签名」节给的两条 prehash 示例：四段之间**没有任何分隔符**
    （别把别家的 `\\n` 拼串习惯带过来），GET 的参数全在 endpoint 里、body 段是空串。"""
    assert KucoinAdapter._prehash(
        "1768982284539", "GET", "/api/v1/orders/byClientOid?clientOid=arb-1a2b", ""
    ) == "1768982284539GET/api/v1/orders/byClientOid?clientOid=arb-1a2b"
    body = '{"clientOid":"arb-1a2b","symbol":"XBTUSDTM"}'
    assert KucoinAdapter._prehash("1768982284539", "post", "/api/v1/orders", body) == \
        '1768982284539POST/api/v1/orders{"clientOid":"arb-1a2b","symbol":"XBTUSDTM"}'


def test_sign_is_base64_and_passphrase_is_hashed_not_plaintext():
    """两处最容易被别家肌肉记忆带偏的编码：SIGN 写成 hexdigest 稳定收 400005，
    passphrase 发明文稳定收 400004 —— 而 400004 的文案是"passphrase 错"，
    看起来像用户填错了密码，最容易查错方向。"""
    rec = Recorder({("POST", "/api/v1/orders"): ok(ORDER_ACK)})
    with adapter(rec) as ad:
        run(ad.place_order(order_req()))
    h = rec.last.headers
    assert len(base64.b64decode(h["KC-API-SIGN"])) == 32    # 摘要原文，不是 64 位 hex
    assert h["KC-API-PASSPHRASE"] != PASSPHRASE
    assert h["KC-API-PASSPHRASE"] == base64.b64encode(
        hmac.new(API_SECRET.encode(), PASSPHRASE.encode(), hashlib.sha256).digest()).decode()
    assert h["KC-API-KEY"] == API_KEY
    assert h["KC-API-KEY-VERSION"] == "3"                   # 字符串，不是数字
    assert h["Content-Type"] == "application/json"          # 缺了是 415000
    assert h["KC-API-TIMESTAMP"].isdigit() and len(h["KC-API-TIMESTAMP"]) == 13   # 毫秒


def test_signed_get_puts_params_in_url_and_signs_the_raw_query():
    """GET 的参数必须进 URL（不是 body），形状与文档示例
    `/api/v1/orders/byClientOid?clientOid=arb-1a2b` 一致。"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.content == b"", "GET 不许带 body，带了签名两边就分叉了"
        assert request.url.query.decode() == "clientOid=leg1-0001"
        assert request.headers["KC-API-SIGN"] == expected_sign(request)
        return json_response(ok(ORDER_DONE))

    rec = Recorder({("GET", "/api/v1/orders/byClientOid"): handler})
    with adapter(rec) as ad:
        run(ad.query_order("XBTUSDTM", "leg1-0001"))
    assert len(rec.requests) == 1          # 确认 handler 里的断言真的跑过


def test_cancel_puts_client_oid_in_path_and_symbol_in_query():
    """撤单：clientOid 走路径、**symbol 是必填查询参数**（漏了直接参数错）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/orders/client-order/leg1-0001"
        assert request.url.query.decode() == "symbol=XBTUSDTM"
        assert request.headers["KC-API-SIGN"] == expected_sign(request)
        return json_response(ok({"clientOid": "leg1-0001"}))

    rec = Recorder({("DELETE", "/api/v1/orders/client-order/leg1-0001"): handler})
    with adapter(rec) as ad:
        run(ad.cancel_order("XBTUSDTM", "leg1-0001"))
    assert len(rec.requests) == 1


def test_funding_history_ignores_limit_and_returns_the_full_window():
    """limit 是页大小语义（基类契约："返回可以多于 limit"），本端点没有页大小参数，
    所以它什么都不做。曾经拿它截尾：KuCoin 有 1 小时周期的合约，1000 条只有
    41 天，90 天窗口被无声斩掉——和 Hyperliquid 被两个审查镜头独立揪出的
    截尾 bug 同根。"""
    rec = Recorder({("GET", "/api/v1/contract/funding-rates"): funding_pages()})
    with adapter(rec, fixed_ts=True) as ad:
        hist = run(ad.fetch_funding_history("XBTUSDTM",
                                            since_ms=FIXED_TS_MS - 20 * 86_400_000,
                                            limit=10))
    assert len(hist) == 60, "20 天 × 3 次/天，整段都要回来，limit 不是输出上限"
    assert hist[-1][0] == FIXED_TS_MS - FIXED_TS_MS % GRID_8H_MS

def test_book_depth_excludes_out_of_band_levels():
    """带外档位一分都不能计入。那两档各挂 999999 张（≈890 亿 U），漏一档结果差四个
    数量级 —— 容量估算最容易翻车的地方。"""
    rec = Recorder({("GET", DEPTH_PATH): ok(DEPTH_BOOK)})
    with adapter(rec) as ad:
        wide = run(ad.fetch_book_depth("XBTUSDTM", band_bp=10.0))
        narrow = run(ad.fetch_book_depth("XBTUSDTM", band_bp=1.0))
    assert wide.bid_notional < Decimal("10000000")
    assert wide.ask_notional < Decimal("10000000")
    # 1bp: lo = 89121.537、hi = 89139.363 —— 两侧各只剩一档
    assert narrow.levels_used == 2
    assert narrow.bid_notional == Decimal("89130.4") * Decimal("20")
    assert narrow.ask_notional == Decimal("89130.5") * Decimal("10")
    assert narrow.bid_notional < wide.bid_notional     # 带宽变窄深度只能变小


def test_missing_order_is_order_rejected_so_the_resolver_can_return_zero():
    """这家没有"订单不存在"的专用码（UNVERIFIED），实测多半是 code=200000 但 data 为空。
    resolve_unknown_order 靠 OrderRejected 判定"这单从未被接受"。"""
    rec = Recorder({("GET", "/api/v1/orders/byClientOid"): ok(None)})
    with adapter(rec) as ad:
        with pytest.raises(OrderRejected) as ei:
            run(ad.query_order("XBTUSDTM", "leg1-0001"))
    assert ei.value.code == "ORDER_NOT_FOUND"


def test_terminal_order_is_not_reported_as_never_accepted():
    """100004 说明这单**存在过、可能已成交**，和"订单不存在"完全不是一回事。混成同一个
    OrderRejected 桶，resolve_unknown_order 就会返回 0，一张真成交的单被报成零成交，
    上层照着 0 再补一次仓 = 双倍仓位。撤单路径上它是"目的已达成"，就地吞掉。"""
    rec = Recorder({("GET", "/api/v1/orders/byClientOid"): err(
        "100004", "order is not in cancellable state")})
    with adapter(rec) as ad:
        with pytest.raises(ExchangeError) as ei:
            run(ad.query_order("XBTUSDTM", "leg1-0001"))
    assert not isinstance(ei.value, OrderRejected) and ei.value.code == "100004"

    rec2 = Recorder({("DELETE", "/api/v1/orders/client-order/leg1-0001"): err(
        "100004", "order is not in cancellable state")})
    with adapter(rec2) as ad:
        run(ad.cancel_order("XBTUSDTM", "leg1-0001"))       # 不抛
    assert len(rec2.requests) == 1


def test_risk_path_never_downgrades_a_hard_error_to_rejected():
    """不可重试的错误原样上抛，**绝不降级成 OrderRejected** —— 降级等于谎报"成交量为 0"，
    上层会照着 0 再补一次仓。"""
    rec = Recorder({("GET", "/api/v1/orders/byClientOid"): err("400006", "ip not allowed")})
    with adapter(rec) as ad:
        with pytest.raises(ExchangeError) as ei:
            run(ad.query_order("XBTUSDTM", "leg1-0001"))
    assert not isinstance(ei.value, OrderRejected) and ei.value.code == "400006"



# ---- WebSocket 流（审查点名的零覆盖区；范式抄 test_bitget_adapter 的 _FakeWS） ----

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


def _ws_fixture(monkeypatch, frames, *, extra_routes=None):
    """搭一条假 WS 流：bullet-public 换 token 走 REST mock，websockets 模块整个替换。"""
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
    routes = {("POST", "/api/v1/bullet-public"):
                  ok({"token": "tok-123",
                      "instanceServers": [{"endpoint": "wss://ws-api-futures.kucoin.com/",
                                           "pingInterval": 18000, "pingTimeout": 10000}]})}
    routes.update(extra_routes or {})
    return ws, captured, Recorder(routes)


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


def test_ws_subscribe_frame_and_ticker_conversion(monkeypatch):
    """首帧订阅体的 topic/type/response 三个字段照文档拼；welcome/ack 不产出数据；
    tickerV2 推送要过 _ws_to_top 的单位换算：size 是**张**、ts 是**纳秒**。"""
    frames = [
        json.dumps({"id": "w1", "type": "welcome"}),
        json.dumps({"id": "1", "type": "ack"}),
        json.dumps({"type": "message", "subject": "tickerV2",
                    "topic": "/contractMarket/tickerV2:XBTUSDTM",
                    "data": {"symbol": "XBTUSDTM", "sequence": 100,
                             "bestBidPrice": "64386.9", "bestBidSize": 640,
                             "bestAskPrice": "64387.0", "bestAskSize": 185,
                             "ts": 1785548892123456789}}),
    ]
    ws, captured, rec = _ws_fixture(monkeypatch, frames)
    with adapter(rec) as ad:
        got = _drain(ad, ["XBTUSDTM"], 1)

    assert "token=tok-123" in captured["url"]
    sub = json.loads(ws.sent[0])
    assert sub["type"] == "subscribe"
    assert sub["topic"] == "/contractMarket/tickerV2:XBTUSDTM"
    assert sub["response"] is True

    top = got[0]
    assert top.bid_price == Decimal("64386.9")
    assert top.bid_qty == Decimal("640") * Decimal("0.001")   # 张 → 币
    assert top.ask_qty == Decimal("185") * Decimal("0.001")
    assert top.ts_ms == 1785548892123          # 纳秒 → 毫秒

    # 每条客户端消息都吃"100 条/10 秒"预算：只该有一条订阅帧（心跳任务被
    # cancel 前未必来得及发，但绝不能出现第二条订阅）
    assert sum(1 for m in ws.sent if json.loads(m)["type"] == "subscribe") == 1


def test_ws_drops_out_of_order_sequence(monkeypatch):
    """sequence 回退的推送是乱序/重放包，必须丢弃——盘口倒着走会让收敛循环
    对着旧价格下单。"""
    def tick(seq, bid):
        return json.dumps({"type": "message", "subject": "tickerV2",
                           "data": {"symbol": "XBTUSDTM", "sequence": seq,
                                    "bestBidPrice": bid, "bestBidSize": 1,
                                    "bestAskPrice": "64400", "bestAskSize": 1,
                                    "ts": 1785548892123456789}})
    frames = [tick(100, "64386.9"), tick(99, "63000.0"), tick(101, "64387.1")]
    ws, _, rec = _ws_fixture(monkeypatch, frames)
    with adapter(rec) as ad:
        got = _drain(ad, ["XBTUSDTM"], 2)
    assert [t.bid_price for t in got] == [Decimal("64386.9"), Decimal("64387.1")], \
        "seq=99 的回退包必须被丢掉"


def test_ws_filters_invalid_symbols_before_subscribing(monkeypatch):
    """符号先过 WS_SYMBOL_RE 再订阅：一个坏符号会让整条订阅被拒，
    比事后猜订阅 ack 便宜得多。全无效时直接空流,连 token 都不该去换。"""
    ws, _, rec = _ws_fixture(monkeypatch, [])
    with adapter(rec) as ad:
        got = _drain(ad, ["bad symbol!", ""], 0)
    assert got == []
    assert not rec.requests, "全部符号无效时不该发任何请求"
