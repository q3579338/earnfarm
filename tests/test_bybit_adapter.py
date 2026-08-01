"""Bybit v5 适配器测试。全部用 httpx.MockTransport 注入假响应，不连交易所。

金标准来自 docs/research/exchange-bybit.md：
  待签串 = timestamp + apiKey + recvWindow + (queryString | rawJsonBody)，无分隔符；
  返佣走 X-Referer header 且不参与签名；
  fundingRate 是交易所原始符号（正 = 多头付给空头），适配器不得取反。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import time
from decimal import Decimal

import httpx
import pytest

from carryfarm.exchanges.base import (
    Credential,
    ExchangeError,
    OrderRejected,
    OrderRequest,
    OrderUnknown,
    RateLimited,
)
from carryfarm.exchanges.bybit import (
    _DEPTH_LIMIT,
    BybitAdapter,
    _chunk_topics,
    _merge_ticker,
)
from carryfarm.models import MarketKind

API_KEY = "XXXXXXXXXX"
API_SECRET = "YYYYYYYYYYYYYYYYYYYY"
# 调研文档 §返佣码机制 里的示例时间戳
FIXED_TS = 1672211928338
RECV = "5000"
# hmac_sha256(API_SECRET, "1672211928338" + API_KEY + "5000" + "category=linear&symbol=BTCUSDT")
GOLD_GET_SIGN = "72cbcd2c47ff20820965c117568b4bb154e52f34155014d960c1d7819748d4aa"


# ---- 假响应 -------------------------------------------------------------

def _envelope(result, ts=FIXED_TS, code=0, msg="OK"):
    return {"retCode": code, "retMsg": msg, "result": result, "time": ts}


def _instrument_row(symbol="BTCUSDT", interval=480, contract="LinearPerpetual",
                    status="Trading", tick="0.10", step="0.001"):
    return {
        "symbol": symbol, "contractType": contract, "status": status,
        "baseCoin": symbol.replace("USDT", ""), "quoteCoin": "USDT", "settleCoin": "USDT",
        "fundingInterval": interval,
        "leverageFilter": {"minLeverage": "1", "maxLeverage": "100", "leverageStep": "0.01"},
        "priceFilter": {"minPrice": "0.10", "maxPrice": "1999999.80", "tickSize": tick},
        "lotSizeFilter": {"maxOrderQty": "1190.000", "minOrderQty": "0.001",
                          "qtyStep": step, "maxMktOrderQty": "500.000",
                          "minNotionalValue": "5"},
    }


INSTRUMENTS = _envelope({
    "category": "linear",
    "list": [
        _instrument_row("BTCUSDT", 480),
        _instrument_row("MEMEUSDT", 60, step="1"),          # 动态切到 1 小时结算的品种
        _instrument_row("OLDUSDT", 480, contract="LinearFutures"),   # 交割合约，要滤掉
        _instrument_row("NEWUSDT", 480, status="PreLaunch"),         # 未上线，要滤掉
    ],
    "nextPageCursor": "",
})


class Recorder:
    """路由 + 录音的 MockTransport handler。"""

    def __init__(self, routes):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get(request.url.path)
        if handler is None:
            raise AssertionError(f"测试没有为 {request.url.path} 准备响应")
        if callable(handler):
            return handler(request)
        return httpx.Response(200, json=handler)

    def by_path(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]

    def last(self, path: str) -> httpx.Request:
        return self.by_path(path)[-1]


def make_adapter(routes, *, broker_code="", ts=FIXED_TS):
    rec = Recorder(routes)
    client = httpx.AsyncClient(transport=httpx.MockTransport(rec))
    ad = BybitAdapter(Credential(api_key=API_KEY, api_secret=API_SECRET),
                      broker_code=broker_code, client=client)
    # 固定时钟：签名要可复现，且不能每个请求都先去打一次 /v5/market/time
    ad._clock.synced_at = time.time()
    ad.timestamp_ms = lambda: ts
    return ad, rec


def gold_sign(payload: str) -> str:
    return hmac.new(API_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


# ---- 签名 ---------------------------------------------------------------

def test_get_signature_is_ts_key_recv_querystring():
    """GET 待签串 = timestamp + apiKey + recvWindow + queryString（无分隔符、无前导 ?）。"""
    ad, rec = make_adapter({"/v5/account/fee-rate": _envelope(
        {"list": [{"symbol": "BTCUSDT", "takerFeeRate": "0.00055",
                   "makerFeeRate": "0.0002"}]})})

    async def main():
        return await ad.fetch_fee_schedule(["BTCUSDT"])

    fees = asyncio.run(main())
    req = rec.last("/v5/account/fee-rate")

    query = str(req.url.query, "utf-8")
    # 待签的 queryString 必须和真正上线的 URL 逐字节一致
    assert query == "category=linear&symbol=BTCUSDT"
    # 金标准：按文档顺序手工拼出来的那一串
    expected_payload = "1672211928338" + API_KEY + RECV + "category=linear&symbol=BTCUSDT"
    assert expected_payload == "1672211928338XXXXXXXXXX5000category=linear&symbol=BTCUSDT"
    assert req.headers["X-BAPI-SIGN"] == gold_sign(expected_payload)
    # 离线用文档配方算出的定值。签名逻辑一改，这里必须翻车
    assert req.headers["X-BAPI-SIGN"] == GOLD_GET_SIGN
    assert req.headers["X-BAPI-TIMESTAMP"] == "1672211928338"
    assert req.headers["X-BAPI-RECV-WINDOW"] == RECV
    assert req.headers["X-BAPI-API-KEY"] == API_KEY
    assert req.headers["X-BAPI-SIGN-TYPE"] == "2"
    assert fees[0].taker == Decimal("0.00055") and fees[0].maker == Decimal("0.0002")


def test_post_signature_covers_the_exact_bytes_on_the_wire():
    """POST 签的必须是真正发出去的那串 JSON 字节——再序列化一次就是 retCode 10004。"""
    ad, rec = make_adapter({
        "/v5/market/instruments-info": INSTRUMENTS,
        "/v5/order/create": _envelope({"orderId": "1", "orderLinkId": "cf-1"}),
    })

    async def main():
        return await ad.place_order(OrderRequest(
            symbol="BTCUSDT", side="buy", qty=Decimal("0.001"),
            price=Decimal("60000"), client_order_id="cf-1"))

    asyncio.run(main())
    req = rec.last("/v5/order/create")
    raw = req.content.decode()

    expected_payload = "1672211928338" + API_KEY + RECV + raw
    assert req.headers["X-BAPI-SIGN"] == gold_sign(expected_payload)
    # 序列化风格也必须是紧凑的那种（签名串里没有多余空格）
    assert ", " not in raw and '": ' not in raw
    assert json.loads(raw)["orderLinkId"] == "cf-1"


def test_public_endpoints_are_unsigned():
    """行情是公开接口，不该带任何签名 header（带了也只是白送 apiKey）。"""
    ad, rec = make_adapter({"/v5/market/instruments-info": INSTRUMENTS})
    asyncio.run(ad.fetch_instruments())
    headers = rec.last("/v5/market/instruments-info").headers
    assert "X-BAPI-SIGN" not in headers
    assert "X-BAPI-API-KEY" not in headers


# ---- 返佣码 -------------------------------------------------------------

def _place(ad):
    return ad.place_order(OrderRequest(
        symbol="BTCUSDT", side="buy", qty=Decimal("0.001"),
        price=Decimal("60000"), client_order_id="cf-1"))


ORDER_ROUTES = {
    "/v5/market/instruments-info": INSTRUMENTS,
    "/v5/order/create": _envelope({"orderId": "1", "orderLinkId": "cf-1"}),
}


def test_empty_broker_code_leaves_no_trace_anywhere():
    """用户默认不填返佣码：不加 header、不加 body 字段、订单ID 不加前缀。"""
    ad, rec = make_adapter(dict(ORDER_ROUTES), broker_code="")
    asyncio.run(_place(ad))

    for req in rec.requests:
        for name, _ in req.headers.raw:
            assert b"referer" not in name.lower(), f"{name!r} 不该出现"
    body = json.loads(rec.last("/v5/order/create").content)
    assert body["orderLinkId"] == "cf-1"          # 没有任何前缀
    assert not any("refer" in k.lower() or "broker" in k.lower() for k in body)


def test_broker_code_goes_only_into_referer_header():
    """非空时：X-Referer header 出现在下单请求上，且不污染 body / orderLinkId /签名。"""
    ad, rec = make_adapter(dict(ORDER_ROUTES), broker_code="api.Abc")
    asyncio.run(_place(ad))

    create = rec.last("/v5/order/create")
    assert create.headers["X-Referer"] == "api.Abc"
    body = json.loads(create.content)
    assert body["orderLinkId"] == "cf-1"
    assert "referer" not in json.dumps(body).lower()
    # 返佣 header 不参与签名：待签串里只有 ts+key+recv+body
    assert create.headers["X-BAPI-SIGN"] == gold_sign(
        "1672211928338" + API_KEY + RECV + create.content.decode())
    # 公共行情请求不该带返佣 header
    info = rec.last("/v5/market/instruments-info")
    assert all(b"referer" not in n.lower() for n, _ in info.headers.raw)


def test_broker_code_absent_from_non_order_private_calls():
    ad, rec = make_adapter({"/v5/order/cancel": _envelope({"orderId": "1"})},
                           broker_code="api.Abc")
    asyncio.run(ad.cancel_order("BTCUSDT", "cf-1"))
    assert all(b"referer" not in n.lower()
               for n, _ in rec.last("/v5/order/cancel").headers.raw)


# ---- 资金费符号与周期 ---------------------------------------------------

def _tickers(rows):
    return _envelope({"category": "linear", "list": rows})


def _ticker_row(symbol="BTCUSDT", funding="0.0001", next_ms="1672214400000"):
    return {"symbol": symbol, "fundingRate": funding, "nextFundingTime": next_ms,
            "bid1Price": "60000.10", "bid1Size": "5.5",
            "ask1Price": "60000.20", "ask1Size": "3.1",
            "markPrice": "60000.15", "lastPrice": "60000.15"}


def test_funding_sign_is_kept_as_exchange_native():
    """Bybit fundingRate 正 = 多头付给空头。适配器必须原样存，绝不取反。"""
    ad, _ = make_adapter({
        "/v5/market/instruments-info": INSTRUMENTS,
        "/v5/market/tickers": _tickers([
            _ticker_row("BTCUSDT", "0.0001"),
            _ticker_row("MEMEUSDT", "-0.00025"),
        ]),
    })
    rates = {r.symbol: r for r in asyncio.run(ad.fetch_carry_rates())}

    assert rates["BTCUSDT"].rate == Decimal("0.0001")      # 正数保持正
    assert rates["MEMEUSDT"].rate == Decimal("-0.00025")   # 负数保持负
    assert rates["BTCUSDT"].rate > 0 and rates["MEMEUSDT"].rate < 0
    assert isinstance(rates["BTCUSDT"].rate, Decimal)


def test_funding_interval_comes_from_instruments_not_hardcoded():
    """fundingInterval 单位是分钟且会动态切换，不能写死 8 小时。"""
    ad, _ = make_adapter({
        "/v5/market/instruments-info": INSTRUMENTS,
        "/v5/market/tickers": _tickers([_ticker_row("BTCUSDT"),
                                        _ticker_row("MEMEUSDT")]),
    })
    rates = {r.symbol: r for r in asyncio.run(ad.fetch_carry_rates())}
    assert rates["BTCUSDT"].interval_s == 480 * 60      # 8h
    assert rates["MEMEUSDT"].interval_s == 60 * 60      # 1h，动态频率
    # 年化必须按各自周期归一，1h 的同样费率是 8h 的 8 倍
    assert (rates["MEMEUSDT"].annualized()
            == rates["BTCUSDT"].annualized() * 8)


def test_universe_filter_drops_futures_and_prelaunch():
    ad, _ = make_adapter({"/v5/market/instruments-info": INSTRUMENTS})
    syms = {i.symbol for i in asyncio.run(ad.fetch_instruments())}
    assert syms == {"BTCUSDT", "MEMEUSDT"}


def test_funding_history_pages_backwards_and_returns_ascending():
    """只传 endTime 往回翻页；返回升序 (结算时刻, 费率)。"""
    step = 8 * 3600 * 1000
    newest = 1_700_000_000_000
    page1 = [{"symbol": "BTCUSDT", "fundingRate": "0.0001",
              "fundingRateTimestamp": str(newest - i * step)} for i in range(200)]
    oldest1 = newest - 199 * step
    page2 = [{"symbol": "BTCUSDT", "fundingRate": "-0.0002",
              "fundingRateTimestamp": str(oldest1 - (i + 1) * step)} for i in range(10)]
    pages = [_envelope({"category": "linear", "list": page1}),
             _envelope({"category": "linear", "list": page2})]
    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        return httpx.Response(200, json=pages[min(len(calls) - 1, 1)])

    ad, _ = make_adapter({"/v5/market/funding/history": handler})
    series = asyncio.run(ad.fetch_funding_history("BTCUSDT", since_ms=0))

    assert len(calls) == 2
    assert "startTime" not in calls[0]              # 只传 startTime 会报错
    assert "endTime" not in calls[0]
    assert calls[1]["endTime"] == str(oldest1 - 1)  # 游标 = 本批最老 - 1ms
    ts_list = [t for t, _ in series]
    assert ts_list == sorted(ts_list)
    assert series[-1] == (newest, Decimal("0.0001"))
    assert series[0][1] == Decimal("-0.0002")       # 负费率原样保留


# ---- 精度 ---------------------------------------------------------------

def test_qty_floors_to_lot_size():
    """数量按 lot_size 向零取整，并按步长隐含的小数位数格式化（绝不用 str(float)）。"""
    ad, rec = make_adapter(dict(ORDER_ROUTES))

    async def main():
        await ad.place_order(OrderRequest(
            symbol="BTCUSDT", side="buy", qty=Decimal("0.0037999"),
            price=Decimal("60000"), client_order_id="cf-1"))

    asyncio.run(main())
    assert json.loads(rec.last("/v5/order/create").content)["qty"] == "0.003"


def test_qty_step_of_one_never_emits_scientific_notation():
    ad, rec = make_adapter(dict(ORDER_ROUTES))

    async def main():
        await ad.place_order(OrderRequest(
            symbol="MEMEUSDT", side="sell", qty=Decimal("1234.9"),
            price=Decimal("0.5"), client_order_id="cf-2"))

    asyncio.run(main())
    body = json.loads(rec.last("/v5/order/create").content)
    assert body["qty"] == "1234"
    assert "e" not in body["qty"].lower() and "e" not in body["price"].lower()


def test_price_rounds_toward_marketability():
    """价格取整方向必须和 executor.cross_price 一致：买单向上、卖单向下。

    传下来的已经是穿价保护限价（自带 3~10bp 穿透），反向取整会把穿透削回来；
    tick 相对价格很粗的小币上那不是少赚一个 tick，而是挂不上、这条腿永远补不平。
    """
    ad, rec = make_adapter(dict(ORDER_ROUTES))

    async def main():
        await ad.place_order(OrderRequest(
            symbol="BTCUSDT", side="buy", qty=Decimal("0.01"),
            price=Decimal("60000.07"), client_order_id="cf-b"))
        await ad.place_order(OrderRequest(
            symbol="BTCUSDT", side="sell", qty=Decimal("0.01"),
            price=Decimal("60000.07"), client_order_id="cf-s"))

    asyncio.run(main())
    bodies = [json.loads(r.content) for r in rec.by_path("/v5/order/create")]
    # tickSize "0.10" → 2 位小数
    assert bodies[0]["price"] == "60000.10" and bodies[0]["side"] == "Buy"
    assert bodies[1]["price"] == "60000.00" and bodies[1]["side"] == "Sell"


def test_qty_rounded_to_zero_is_rejected_not_sent():
    ad, rec = make_adapter(dict(ORDER_ROUTES))

    async def main():
        await ad.place_order(OrderRequest(
            symbol="BTCUSDT", side="buy", qty=Decimal("0.0009"),
            price=Decimal("60000"), client_order_id="cf-1"))

    with pytest.raises(OrderRejected):
        asyncio.run(main())
    assert rec.by_path("/v5/order/create") == []


def test_book_top_stays_decimal_exact():
    ad, _ = make_adapter({"/v5/market/tickers": _tickers([_ticker_row()])})
    top = asyncio.run(ad.fetch_book_top("BTCUSDT"))
    assert top.bid_price == Decimal("60000.10") and top.ask_qty == Decimal("3.1")
    assert str(top.bid_price) == "60000.10"        # 没经过 float，尾零都在
    assert top.ts_ms == FIXED_TS


# ---- 多档深度 -----------------------------------------------------------

def _orderbook(bids, asks, symbol="BTCUSDT", ts=FIXED_TS, cts=None):
    result = {"s": symbol, "b": bids, "a": asks, "ts": ts, "u": 42, "seq": 7}
    if cts is not None:
        result["cts"] = cts
    return _envelope(result)


# 买一 99.99 / 卖一 100.01 ⇒ 中价正好 100.00，10bp 带宽 = [99.90, 100.10]（含端点）。
# 每侧最后一档故意放在带外且量极大——被误计入的话断言会以数量级的差距翻车。
BOOK = _orderbook(
    bids=[["99.99", "10"], ["99.95", "20"], ["99.90", "30"], ["99.89", "1000"]],
    asks=[["100.01", "5"], ["100.05", "10"], ["100.10", "20"], ["100.20", "5000"]],
)


def test_depth_accumulates_notional_inside_the_band():
    """手算：买侧 99.99*10 + 99.95*20 + 99.90*30 = 999.9 + 1999 + 2997 = 5995.9；
    卖侧 100.01*5 + 100.05*10 + 100.10*20 = 500.05 + 1000.5 + 2002 = 3502.55。"""
    ad, rec = make_adapter({"/v5/market/orderbook": BOOK})
    depth = asyncio.run(ad.fetch_book_depth("BTCUSDT", band_bp=10))

    assert depth.bid_notional == Decimal("5995.9")
    assert depth.ask_notional == Decimal("3502.55")
    assert isinstance(depth.bid_notional, Decimal)     # 全程 Decimal，没绕过 float
    assert depth.thinner_side == Decimal("3502.55")    # 对冲按短板算容量
    # 一档照旧要给准，深度不是它的替代品而是补充
    assert (depth.bid_price, depth.bid_qty) == (Decimal("99.99"), Decimal("10"))
    assert (depth.ask_price, depth.ask_qty) == (Decimal("100.01"), Decimal("5"))
    assert depth.levels_used == 3                      # 带内各 3 档
    assert depth.band_bp == 10 and depth.symbol == "BTCUSDT"
    assert depth.ts_ms == FIXED_TS
    # 端点/参数：公开免签，档数取够
    params = dict(rec.last("/v5/market/orderbook").url.params)
    assert params == {"category": "linear", "symbol": "BTCUSDT",
                      "limit": str(_DEPTH_LIMIT)}
    assert "X-BAPI-SIGN" not in rec.last("/v5/market/orderbook").headers


def test_depth_excludes_levels_outside_the_band():
    """带外的档位一分都不能计入——买侧那档 99.89*1000 ≈ 98890，
    误计入的话结果会比正确值大一个数量级；卖侧同理 100.20*5000 ≈ 501000。"""
    ad, _ = make_adapter({"/v5/market/orderbook": BOOK})
    depth = asyncio.run(ad.fetch_book_depth("BTCUSDT", band_bp=10))
    assert depth.bid_notional == Decimal("5995.9") < Decimal("6000")
    assert depth.ask_notional == Decimal("3502.55") < Decimal("4000")

    # 收窄到 5bp = [99.95, 100.05]：各再掉一档，端点档（99.95 / 100.05）仍算在内
    ad2, _ = make_adapter({"/v5/market/orderbook": BOOK})
    narrow = asyncio.run(ad2.fetch_book_depth("BTCUSDT", band_bp=5))
    assert narrow.bid_notional == Decimal("2998.9")    # 999.9 + 1999
    assert narrow.ask_notional == Decimal("1500.55")   # 500.05 + 1000.5
    assert narrow.levels_used == 2
    assert narrow.band_bp == 5


def test_depth_size_is_base_coin_not_contract_count():
    """linear 永续的 size 单位就是 base 币（contract_size 恒为 1），名义额 = 价*量。

    把 size 当张数（面值 ≠ 1）或当 USD 面值（inverse 的口径）会得到完全不同的数：
    这里买侧是 0.6 个 BTC ≈ 35996 USDT，绝不是 0.6，也不是 0.6 USDT。
    """
    book = _orderbook(bids=[["59994", "0.5"], ["59990", "0.1"], ["59000", "100"]],
                      asks=[["60006", "0.2"], ["61000", "100"]])
    ad, _ = make_adapter({"/v5/market/orderbook": book})
    depth = asyncio.run(ad.fetch_book_depth("BTCUSDT", band_bp=10))

    # 中价 60000，10bp 带宽 = [59940, 60060]，最后一档在带外
    assert depth.bid_notional == Decimal("35996")      # 59994*0.5 + 59990*0.1
    assert depth.ask_notional == Decimal("12001.2")    # 60006*0.2
    assert depth.levels_used == 2                      # 买 2 / 卖 1，取较大者


def test_depth_reports_short_levels_instead_of_extrapolating():
    """档数被端点截断时如实上报 levels_used，宁可低估也不外推补足。"""
    # 每侧正好 _DEPTH_LIMIT 档，且全部落在 10bp 带内（步长 0.0001，最深也才 ~2bp）
    bids = [[f"{Decimal('99.99') - Decimal('0.0001') * i:f}", "1"]
            for i in range(_DEPTH_LIMIT)]
    asks = [[f"{Decimal('100.01') + Decimal('0.0001') * i:f}", "1"]
            for i in range(_DEPTH_LIMIT)]
    ad, _ = make_adapter({"/v5/market/orderbook": _orderbook(bids, asks)})
    depth = asyncio.run(ad.fetch_book_depth("BTCUSDT", band_bp=10))

    # 顶到请求档数 = 带宽没盖满，这个深度只是下界
    assert depth.levels_used == _DEPTH_LIMIT
    # 最深一档离带底还很远（99.9701 >> 99.90），说明确实是档位不够而不是带宽到头
    assert Decimal(bids[-1][0]) > Decimal("99.90")
    # 返回的就是真实统计到的量，没有按密度外推补到带底
    assert depth.bid_notional == sum((Decimal(p) for p, _ in bids), Decimal("0"))
    assert depth.ask_notional == sum((Decimal(p) for p, _ in asks), Decimal("0"))


def test_depth_ignores_zero_size_levels():
    """size=0 是 WS 增量流的删档约定，快照里出现也不该占档数（否则掩盖档位不足）。"""
    ad, _ = make_adapter({"/v5/market/orderbook": _orderbook(
        bids=[["99.99", "10"], ["99.95", "0"]],
        asks=[["100.01", "5"], ["100.05", "0"]])})
    depth = asyncio.run(ad.fetch_book_depth("BTCUSDT", band_bp=10))
    assert depth.bid_notional == Decimal("999.9")
    assert depth.ask_notional == Decimal("500.05")
    assert depth.levels_used == 1


def test_depth_prefers_matching_engine_timestamp():
    ad, _ = make_adapter({"/v5/market/orderbook": _orderbook(
        bids=[["99.99", "1"]], asks=[["100.01", "1"]], cts=1672211999999)})
    assert asyncio.run(ad.fetch_book_depth("BTCUSDT")).ts_ms == 1672211999999


def test_depth_one_sided_book_raises_instead_of_faking_a_mid():
    ad, _ = make_adapter({"/v5/market/orderbook": _orderbook(
        bids=[], asks=[["100.01", "5"]])})
    with pytest.raises(ExchangeError) as exc:
        asyncio.run(ad.fetch_book_depth("BTCUSDT"))
    assert exc.value.code == "NO_BOOK"


def test_depth_rejects_nonpositive_band():
    """带宽 0 会让深度恒为 0，而 scoring 把 0 当"没有深度数据"静默跳过容量约束。"""
    ad, rec = make_adapter({"/v5/market/orderbook": BOOK})
    with pytest.raises(ValueError):
        asyncio.run(ad.fetch_book_depth("BTCUSDT", band_bp=0))
    assert rec.requests == []


def test_depth_uses_market_quota_not_the_risk_pool():
    """深度是行情路径，绝不能占用风控预留的 risk 配额。"""
    ad, _ = make_adapter({"/v5/market/orderbook": BOOK})
    ad._quota = _QuotaSpy(ad._quota)
    asyncio.run(ad.fetch_book_depth("BTCUSDT"))
    assert ad._quota.seen == ["market"]


# ---- 未知状态 / 错误码 --------------------------------------------------

def _timeout(request):
    raise httpx.ReadTimeout("read timeout", request=request)


def test_timeout_on_place_order_raises_order_unknown():
    """超时绝不能当失败返回——上层要靠 OrderUnknown 走 cancel→查单→仓位差分。"""
    ad, _ = make_adapter({
        "/v5/market/instruments-info": INSTRUMENTS,
        "/v5/order/create": _timeout,
    })
    with pytest.raises(OrderUnknown) as exc:
        asyncio.run(_place(ad))
    assert exc.value.client_order_id == "cf-1"
    assert exc.value.venue == "bybit"


def test_connect_error_on_place_order_raises_order_unknown():
    def boom(request):
        raise httpx.ConnectError("connection reset", request=request)

    ad, _ = make_adapter({"/v5/market/instruments-info": INSTRUMENTS,
                          "/v5/order/create": boom})
    with pytest.raises(OrderUnknown):
        asyncio.run(_place(ad))


def test_http_5xx_on_place_order_is_also_unknown():
    """网关 5xx 同样是「不知道有没有进撮合」，不能当明确失败。"""
    ad, _ = make_adapter({
        "/v5/market/instruments-info": INSTRUMENTS,
        "/v5/order/create": lambda r: httpx.Response(502, text="bad gateway"),
    })
    with pytest.raises(OrderUnknown):
        asyncio.run(_place(ad))


def test_business_rejection_is_not_unknown():
    """明确的业务拒单要抛 OrderRejected，不能升级成 OrderUnknown（那会白跑消解流程）。"""
    ad, _ = make_adapter({
        "/v5/market/instruments-info": INSTRUMENTS,
        "/v5/order/create": lambda r: httpx.Response(
            200, json=_envelope(None, code=110004, msg="Insufficient wallet balance")),
    })
    with pytest.raises(OrderRejected) as exc:
        asyncio.run(_place(ad))
    assert exc.value.code == "110004" and exc.value.retriable is False


def test_place_order_without_client_order_id_is_refused():
    ad, rec = make_adapter(dict(ORDER_ROUTES))

    async def main():
        await ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy",
                                          qty=Decimal("0.01"), price=Decimal("60000")))

    with pytest.raises(ValueError):
        asyncio.run(main())
    assert rec.requests == []


@pytest.mark.parametrize("code,msg,exc_type,retriable", [
    (10006, "Too many visits!", RateLimited, True),
    (10429, "System level frequency protection", RateLimited, True),
    (10002, "invalid request, please check your timestamp", ExchangeError, True),
    (10004, "error sign!", ExchangeError, False),
    (10001, "params error", OrderRejected, False),
    (110001, "order not exists or too late to cancel", OrderRejected, False),
])
def test_error_code_translation(code, msg, exc_type, retriable):
    # 走行情端点：撤单/查单是风控路径，那里会按 resolve_unknown_order 的类型契约
    # 再翻译一次（见 test_risk_path_errors_are_catchable_by_the_resolver），
    # 混在一起测就分不清是 _parse 的翻译还是风控路径的重分类
    ad, _ = make_adapter({"/v5/market/tickers": lambda r: httpx.Response(
        200, json=_envelope(None, code=code, msg=msg))})
    with pytest.raises(exc_type) as exc:
        asyncio.run(ad.fetch_book_top("BTCUSDT"))
    assert exc.value.code == str(code)
    assert exc.value.retriable is retriable
    # RateLimited 是 ExchangeError 的子类，别把限频误判成普通业务错
    if exc_type is ExchangeError:
        assert not isinstance(exc.value, (RateLimited, OrderRejected))


def test_server_error_code_on_place_order_is_unknown():
    """10016 是服务端错误：请求已经到了 Bybit，结果不明。

    _parse 已经把它标成 retriable=True，place_order 必须读这个标志位——
    只按码前缀匹配等于白定义 _RETRIABLE_CODES。
    """
    ad, _ = make_adapter({
        "/v5/market/instruments-info": INSTRUMENTS,
        "/v5/order/create": lambda r: httpx.Response(
            200, json=_envelope(None, code=10016, msg="server error")),
    })
    with pytest.raises(OrderUnknown):
        asyncio.run(_place(ad))


def test_expired_timestamp_on_place_order_is_not_unknown():
    """10002 时间戳越界是在**进撮合之前**挡下的，订单确定不存在，
    不能因为 retriable=True 就白跑一轮 cancel+查单。"""
    ad, _ = make_adapter({
        "/v5/market/instruments-info": INSTRUMENTS,
        "/v5/order/create": lambda r: httpx.Response(
            200, json=_envelope(None, code=10002, msg="invalid timestamp")),
    })
    with pytest.raises(ExchangeError) as exc:      # OrderUnknown 不是它的子类
        asyncio.run(_place(ad))
    assert exc.value.code == "10002"


def test_remote_protocol_error_on_place_order_is_unknown():
    """服务端收下请求后半途断连是最典型的"可能已进撮合"。

    RemoteProtocolError 走 ProtocolError 分支，不是 NetworkError 的子类，
    只写 (TimeoutException, NetworkError) 会让它裸奔漏出去。
    """
    def boom(request):
        raise httpx.RemoteProtocolError("server disconnected", request=request)

    ad, _ = make_adapter({"/v5/market/instruments-info": INSTRUMENTS,
                          "/v5/order/create": boom})
    with pytest.raises(OrderUnknown):
        asyncio.run(_place(ad))


@pytest.mark.parametrize("route", [
    lambda r: httpx.Response(502, text="bad gateway"),
    lambda r: httpx.Response(200, content=b"<html>blocked</html>"),
    lambda r: httpx.Response(200, json=_envelope(None, code=10016, msg="server error")),
])
def test_risk_path_errors_are_catchable_by_the_resolver(route):
    """base.resolve_unknown_order 只认 (OrderRejected, httpx.HTTPError, RateLimited)。

    撤单/查单抛裸 ExchangeError 会让消解流程在最危险的时刻带着未捕获异常崩掉。
    """
    ad, _ = make_adapter({"/v5/order/cancel": route})
    with pytest.raises(RateLimited) as exc:
        asyncio.run(ad.cancel_order("BTCUSDT", "cf-1"))
    assert exc.value.retriable is True

    ad2, _ = make_adapter({"/v5/order/realtime": route})
    with pytest.raises(RateLimited):
        asyncio.run(ad2.query_order("BTCUSDT", "cf-1"))


def test_http_403_is_rate_limited_and_not_retriable():
    """IP 级 403 带 10 分钟封禁，标成可重试会让机器人被打下线整整十分钟。"""
    ad, _ = make_adapter({"/v5/order/cancel": lambda r: httpx.Response(403, text="banned")})
    with pytest.raises(RateLimited) as exc:
        asyncio.run(ad.cancel_order("BTCUSDT", "cf-1"))
    assert exc.value.retriable is False
    assert exc.value.code == "HTTP_403"       # 六家统一用下划线，别让日志规则漏掉


# ---- 查单 / 仓位 / 杠杆 -------------------------------------------------

def _order_row(status="Filled", cum="0.003", link="cf-1"):
    return {"orderId": "abc", "orderLinkId": link, "orderStatus": status,
            "qty": "0.003", "cumExecQty": cum, "avgPrice": "60000.5",
            "cumExecFee": "0.099", "feeCurrency": "USDT"}


def test_query_order_falls_back_to_history_when_realtime_is_empty():
    """全部成交后订单会从 realtime 消失。这时报 0 成交是最危险的谎。"""
    ad, rec = make_adapter({
        "/v5/order/realtime": _envelope({"list": []}),
        "/v5/order/history": _envelope({"list": [_order_row()]}),
    })
    res = asyncio.run(ad.query_order("BTCUSDT", "cf-1"))
    assert res.status == "filled" and res.is_terminal
    assert res.filled_qty == Decimal("0.003")
    assert res.avg_price == Decimal("60000.5")
    assert rec.by_path("/v5/order/history")


def test_query_order_unknown_id_raises_order_rejected():
    """两处都查不到 = 这单从没被接受。base.resolve_unknown_order 靠它判成交量为 0。"""
    ad, _ = make_adapter({"/v5/order/realtime": _envelope({"list": []}),
                          "/v5/order/history": _envelope({"list": []})})
    with pytest.raises(OrderRejected):
        asyncio.run(ad.query_order("BTCUSDT", "cf-1"))


def test_position_side_becomes_signed_qty():
    ad, _ = make_adapter({"/v5/position/list": _envelope({"list": [
        {"symbol": "BTCUSDT", "side": "Sell", "size": "0.5", "avgPrice": "60000",
         "markPrice": "60100", "liqPrice": "70000", "leverage": "5"},
        {"symbol": "MEMEUSDT", "side": "Buy", "size": "1000", "avgPrice": "0.5",
         "markPrice": "0.51", "liqPrice": "", "leverage": "3"},
    ], "nextPageCursor": ""})})
    pos = {p.symbol: p for p in asyncio.run(ad.fetch_positions())}
    assert pos["BTCUSDT"].qty == Decimal("-0.5")       # Sell → 负
    assert pos["MEMEUSDT"].qty == Decimal("1000")      # Buy → 正
    assert pos["BTCUSDT"].liquidation_price == Decimal("70000")
    assert pos["MEMEUSDT"].liquidation_price is None   # 空串不是 0


def test_position_sweep_needs_settle_coin_discriminator():
    """category=linear 时 symbol 和 settleCoin 至少要给一个，否则交易所直接报错。"""
    ad, rec = make_adapter({"/v5/position/list": _envelope({"list": [],
                                                            "nextPageCursor": ""})})
    asyncio.run(ad.fetch_positions())
    params = dict(rec.last("/v5/position/list").url.params)
    assert params["settleCoin"] == "USDT"

    ad2, rec2 = make_adapter({"/v5/position/list": _envelope({"list": [],
                                                              "nextPageCursor": ""})})
    asyncio.run(ad2.fetch_positions(["BTCUSDT"]))
    params2 = dict(rec2.last("/v5/position/list").url.params)
    assert params2["symbol"] == "BTCUSDT" and "settleCoin" not in params2


def test_set_leverage_treats_not_modified_as_success():
    """110043 leverage not modified 是幂等成功，抛出去会让启动流程无谓中断。"""
    ad, rec = make_adapter({"/v5/position/set-leverage": lambda r: httpx.Response(
        200, json=_envelope({}, code=110043, msg="leverage not modified"))})
    asyncio.run(ad.set_leverage("BTCUSDT", Decimal("5")))
    body = json.loads(rec.last("/v5/position/set-leverage").content)
    assert body["buyLeverage"] == body["sellLeverage"] == "5"


def test_leverage_tiers_fall_back_when_risk_limit_unavailable():
    ad, _ = make_adapter({
        "/v5/market/risk-limit": lambda r: httpx.Response(
            200, json=_envelope(None, code=10001, msg="params error")),
        "/v5/market/instruments-info": INSTRUMENTS,
    })
    tiers = asyncio.run(ad.fetch_leverage_tiers("BTCUSDT"))
    assert tiers == ((Decimal("Infinity"), Decimal("100")),)


def test_funding_limits_returns_none_rather_than_inventing_a_cap():
    ad, _ = make_adapter({})
    assert asyncio.run(ad.fetch_funding_limits("BTCUSDT")) is None


# ---- 限频配额分池 -------------------------------------------------------

class _QuotaSpy(dict):
    def __init__(self, inner):
        super().__init__(inner)
        self.seen: list[str] = []

    def get(self, key, default=None):
        self.seen.append(key)
        return super().get(key, default)


def test_risk_path_uses_the_reserved_quota_pool():
    """撤单/查仓/查挂单必须走 risk 池，不能被行情轮询挤掉配额。"""
    ad, _ = make_adapter({
        "/v5/order/cancel": _envelope({"orderId": "1"}),
        "/v5/order/realtime": _envelope({"list": []}),
        "/v5/position/list": _envelope({"list": [], "nextPageCursor": ""}),
        "/v5/market/tickers": _tickers([_ticker_row()]),
    })
    ad._quota = _QuotaSpy(ad._quota)

    async def main():
        await ad.fetch_book_top("BTCUSDT")
        await ad.cancel_order("BTCUSDT", "cf-1")
        await ad.fetch_open_orders("BTCUSDT")
        await ad.fetch_positions()

    asyncio.run(main())
    assert ad._quota.seen == ["market", "risk", "risk", "risk"]


def test_order_path_uses_trade_quota():
    ad, _ = make_adapter(dict(ORDER_ROUTES))
    ad._quota = _QuotaSpy(ad._quota)
    asyncio.run(_place(ad))
    assert ad._quota.seen == ["market", "trade"]     # 先查合约规格，再下单


# ---- WebSocket 增量合并 -------------------------------------------------

def test_ticker_delta_must_be_merged_not_read_directly():
    """linear 的 tickers 是增量编码：字段没出现 = 没变化，直接读会拿到 None。"""
    state: dict = {}
    snap = {"topic": "tickers.BTCUSDT", "type": "snapshot", "cts": 1000,
            "data": {"symbol": "BTCUSDT", "bid1Price": "60000.10", "bid1Size": "5.5",
                     "ask1Price": "60000.20", "ask1Size": "3.1", "fundingRate": "0.0001"}}
    top = _merge_ticker(state, snap)
    assert top is not None and top.ask_price == Decimal("60000.20")

    # 只推了买一，其余字段必须沿用上一状态
    delta = {"topic": "tickers.BTCUSDT", "type": "delta", "cts": 1100,
             "data": {"symbol": "BTCUSDT", "bid1Price": "60000.15"}}
    top2 = _merge_ticker(state, delta)
    assert top2.bid_price == Decimal("60000.15")
    assert top2.ask_price == Decimal("60000.20")     # 没变的字段没丢
    assert top2.ts_ms == 1100                        # 用撮合引擎时间 cts

    assert _merge_ticker(state, {"topic": "orderbook.1.BTCUSDT", "data": {}}) is None


def test_subscribe_args_are_chunked_by_size_not_count():
    """订阅上限是 args 数组的字符数（约 21000），不是条数。"""
    topics = [f"tickers.SYMBOL{i:05d}USDT" for i in range(3000)]
    chunks = list(_chunk_topics(topics))
    assert sum(len(c) for c in chunks) == len(topics)
    assert [t for c in chunks for t in c] == topics      # 不重不漏不乱序
    assert len(chunks) > 1
    assert all(len(json.dumps(c)) < 21_000 for c in chunks)


def test_stream_book_top_is_an_async_generator():
    ad, _ = make_adapter({})
    assert inspect.isasyncgenfunction(BybitAdapter.stream_book_top)
    gen = ad.stream_book_top(["BTCUSDT"])
    assert hasattr(gen, "__anext__")
    asyncio.run(gen.aclose())


def test_incomplete_ticker_state_yields_nothing():
    state: dict = {}
    partial = {"topic": "tickers.NEWUSDT", "type": "snapshot", "cts": 1,
               "data": {"symbol": "NEWUSDT", "bid1Price": "1.0"}}
    assert _merge_ticker(state, partial) is None


# ---- 市场范围 -----------------------------------------------------------

@pytest.mark.parametrize("kind", [MarketKind.SPOT, MarketKind.MARGIN])
def test_unsupported_markets_fail_loudly(kind):
    """现货缺 lotSizeFilter 字段名与持仓口径、杠杆缺借币利率端点——宁可不做也不能猜。"""
    with pytest.raises(NotImplementedError):
        BybitAdapter(Credential(api_key="k", api_secret="s"), kind=kind)
