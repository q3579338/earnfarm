"""Bitget 适配器测试。全部走 httpx.MockTransport，不碰真交易所。

金标准来自 docs/research/exchange-bitget.md 里逐字抄下来的官方签名示例，
以及线上实测的合约规格（BTCUSDT tick 0.1 / lot 0.0001 等）。
"""

from __future__ import annotations

import asyncio
import base64
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
from earnfarm.exchanges.bitget import (
    DEPTH_PATH,
    BitgetAdapter,
    BitgetSpotAdapter,
    _json_body,
    _query_strings,
)
from earnfarm.models import Instrument

API_KEY = "bg_test_key"
SECRET = "bg_test_secret"
PASSPHRASE = "bg_test_pass"


# ---- 脚手架 -------------------------------------------------------------

def ok(data):
    return {"code": "00000", "msg": "success", "requestTime": 1785548132637, "data": data}


CONTRACTS = [
    # 实测值：priceEndStep 不恒为 1，所以 tick != 10^-pricePlace
    {"symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "symbolStatus": "normal",
     "pricePlace": "1", "priceEndStep": "1", "volumePlace": "4", "sizeMultiplier": "0.0001",
     "minTradeNum": "0.0001", "minTradeUSDT": "5", "maxLever": "150", "minLever": "1",
     "fundInterval": "8", "symbolType": "perpetual"},
    {"symbol": "DOGEUSDT", "baseCoin": "DOGE", "quoteCoin": "USDT", "symbolStatus": "normal",
     "pricePlace": "5", "priceEndStep": "1", "volumePlace": "0", "sizeMultiplier": "1",
     "minTradeNum": "1", "minTradeUSDT": "5", "maxLever": "75", "minLever": "1",
     "fundInterval": "4", "symbolType": "perpetual"},
    # priceEndStep=5 的构造样本：tick 应为 0.05 而不是 0.01
    {"symbol": "STEPUSDT", "baseCoin": "STEP", "quoteCoin": "USDT", "symbolStatus": "normal",
     "pricePlace": "2", "priceEndStep": "5", "volumePlace": "1", "sizeMultiplier": "0.1",
     "minTradeNum": "0.1", "minTradeUSDT": "5", "maxLever": "20", "minLever": "1",
     "fundInterval": "1", "symbolType": "perpetual"},
    # minTradeNum 大于 sizeMultiplier 的构造样本：取整后非零但仍不够最小下单量
    {"symbol": "MINUSDT", "baseCoin": "MIN", "quoteCoin": "USDT", "symbolStatus": "normal",
     "pricePlace": "2", "priceEndStep": "1", "volumePlace": "1", "sizeMultiplier": "0.1",
     "minTradeNum": "1", "minTradeUSDT": "5", "maxLever": "20", "minLever": "1",
     "fundInterval": "8", "symbolType": "perpetual"},
    # 已下架的不该进候选池
    {"symbol": "DEADUSDT", "baseCoin": "DEAD", "quoteCoin": "USDT", "symbolStatus": "offline",
     "pricePlace": "2", "priceEndStep": "1", "volumePlace": "1", "sizeMultiplier": "0.1",
     "minTradeNum": "0.1", "minTradeUSDT": "5", "maxLever": "20", "minLever": "1",
     "fundInterval": "8", "symbolType": "perpetual"},
]

DEFAULT_ROUTES = {
    "/api/v2/public/time": ok({"serverTime": "1785548132637"}),
    "/api/v2/mix/market/contracts": ok(CONTRACTS),
    "/api/v2/mix/order/place-order": ok({"orderId": "1234567890", "clientOid": "leg1-001"}),
}


def make_handler(routes, record):
    merged = {**DEFAULT_ROUTES, **routes}

    def handler(request: httpx.Request) -> httpx.Response:
        record.append(request)
        path = request.url.path
        for key, value in merged.items():
            if path == key:
                if callable(value):
                    return value(request)
                return httpx.Response(200, json=value)
        return httpx.Response(404, json={"code": "40404", "msg": f"unrouted {path}"})

    return handler


def make_adapter(routes=None, *, broker_code="", **kwargs):
    record: list[httpx.Request] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(make_handler(routes or {}, record)))
    ad = BitgetAdapter(Credential(API_KEY, SECRET, PASSPHRASE),
                       broker_code=broker_code, client=client, **kwargs)
    # 冻结时钟状态，避免 _request 顺手去校时把请求记录搅浑
    ad._clock.synced_at = time.time()
    ad._clock.offset_ms = 0
    return ad, record


def run(ad, coro):
    async def _go():
        try:
            return await coro
        finally:
            await ad.close()
    return asyncio.run(_go())


def sign_of(prestring: str) -> str:
    return base64.b64encode(
        hmac.new(SECRET.encode(), prestring.encode(), hashlib.sha256).digest()
    ).decode()


def recompute(request: httpx.Request) -> str:
    """从实际发出的请求反推签名——这才是"签什么就发什么"的真检查。"""
    ts = request.headers["ACCESS-TIMESTAMP"]
    method = request.method
    path = request.url.path
    # Bitget 签的是**未编码**的 query，URL 上带的是编码后的
    raw_query = httpx.QueryParams(request.url.query.decode())
    qs = "&".join(f"{k}={v}" for k, v in sorted(raw_query.items()))
    body = request.content.decode() if request.content else ""
    return sign_of(f"{ts}{method}{path}" + (f"?{qs}" if qs else "") + body)


# ---- 签名：官方示例做金标准 --------------------------------------------

def test_get_prestring_matches_official_example():
    """文档逐字示例：16273667805456GET/api/mix/v2/market/depth?limit=20&symbol=BTCUSDT"""
    ad, _ = make_adapter()
    raw, encoded = _query_strings({"symbol": "BTCUSDT", "limit": 20})
    assert raw == "limit=20&symbol=BTCUSDT"       # 必须按 key 升序
    assert encoded == raw                          # 纯 ASCII 时两者一致
    pre = ad._prestring("16273667805456", "GET", "/api/mix/v2/market/depth", raw, "")
    assert pre == "16273667805456GET/api/mix/v2/market/depth?limit=20&symbol=BTCUSDT"
    assert ad._hmac_sha256_b64(pre) == sign_of(pre)
    run(ad, asyncio.sleep(0))


def test_post_prestring_matches_official_example():
    """文档逐字示例：POST 的前缀串直接拼上序列化后的 body。"""
    ad, _ = make_adapter()
    body = {
        "productType": "usdt-futures", "symbol": "BTCUSDT", "size": "8",
        "marginMode": "crossed", "side": "buy", "orderType": "limit",
        "clientOid": "channel#123456",
    }
    body_str = _json_body(body)
    assert body_str == ('{"productType":"usdt-futures","symbol":"BTCUSDT","size":"8",'
                        '"marginMode":"crossed","side":"buy","orderType":"limit",'
                        '"clientOid":"channel#123456"}')
    pre = ad._prestring("16273667805456", "POST", "/api/v2/mix/order/place-order", "", body_str)
    assert pre == ("16273667805456POST/api/v2/mix/order/place-order"
                   '{"productType":"usdt-futures","symbol":"BTCUSDT","size":"8",'
                   '"marginMode":"crossed","side":"buy","orderType":"limit",'
                   '"clientOid":"channel#123456"}')
    run(ad, asyncio.sleep(0))


def test_signed_body_bytes_match_signature():
    """httpx 实际发出的字节必须与被签名的串逐字节一致。

    这条同时守着一个隐性耦合：httpx 的 json 序列化参数一旦变了，这里立刻红。
    """
    ad, rec = make_adapter()
    run(ad, ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy", qty=Decimal("0.01"),
                                        price=Decimal("62900"), client_order_id="leg1-001")))
    req = [r for r in rec if r.url.path.endswith("place-order")][0]
    assert req.headers["ACCESS-SIGN"] == recompute(req)
    # 签名串里的 body 就是传输的 body
    ts = req.headers["ACCESS-TIMESTAMP"]
    pre = f"{ts}POST/api/v2/mix/order/place-order" + req.content.decode()
    assert req.headers["ACCESS-SIGN"] == sign_of(pre)


def test_get_query_sorted_signed_raw_but_url_encoded():
    """clientOid 允许 '#'：签名用原始串，URL 必须带百分号编码，否则 '#' 会被当作 fragment。"""
    ad, rec = make_adapter({
        "/api/v2/mix/order/detail": ok({"orderId": "9", "clientOid": "leg1#123",
                                        "state": "filled", "baseVolume": "0.01",
                                        "priceAvg": "62900"}),
    })
    run(ad, ad.query_order("BTCUSDT", "leg1#123"))
    req = [r for r in rec if r.url.path.endswith("/order/detail")][0]
    assert req.url.query == b"clientOid=leg1%23123&productType=USDT-FUTURES&symbol=BTCUSDT"
    ts = req.headers["ACCESS-TIMESTAMP"]
    raw = "clientOid=leg1#123&productType=USDT-FUTURES&symbol=BTCUSDT"
    assert req.headers["ACCESS-SIGN"] == sign_of(
        f"{ts}GET/api/v2/mix/order/detail?{raw}")


# ---- 返佣码 -------------------------------------------------------------

def _place(ad):
    return ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy", qty=Decimal("0.01"),
                                       price=Decimal("62900"), client_order_id="leg1-001"))


def test_broker_code_empty_leaves_no_trace():
    """用户默认不填 → 请求里不该有任何返佣痕迹：没 header、没字段、订单 ID 无前缀。"""
    ad, rec = make_adapter(broker_code="")
    run(ad, _place(ad))
    req = [r for r in rec if r.url.path.endswith("place-order")][0]
    assert "x-channel-api-code" not in {k.lower() for k in req.headers}
    body = json.loads(req.content)
    assert not any("channel" in k.lower() or "broker" in k.lower() for k in body)
    assert body["clientOid"] == "leg1-001"       # 没有被加前缀
    assert "channel" not in req.content.decode()


def test_broker_code_goes_into_header_only():
    """非空时只进 X-CHANNEL-API-CODE header，且不参与签名。"""
    ad, rec = make_adapter(broker_code="p4sve")
    run(ad, _place(ad))
    req = [r for r in rec if r.url.path.endswith("place-order")][0]
    assert req.headers["X-CHANNEL-API-CODE"] == "p4sve"
    body = json.loads(req.content)
    assert body["clientOid"] == "leg1-001"       # 依然没有前缀
    assert "p4sve" not in json.dumps(body)
    # header 不在 prestring 里 —— 加了码签名照样对得上
    assert req.headers["ACCESS-SIGN"] == recompute(req)


def test_broker_code_whitespace_is_trimmed_and_blank_is_dropped():
    ad, rec = make_adapter(broker_code="   ")
    assert ad.broker_code == ""
    run(ad, _place(ad))
    req = [r for r in rec if r.url.path.endswith("place-order")][0]
    assert "x-channel-api-code" not in {k.lower() for k in req.headers}


# ---- 资金费符号与周期 ---------------------------------------------------

FUND_ROWS = [
    {"symbol": "BTCUSDT", "fundingRate": "0.000054", "fundingRateInterval": "8",
     "nextUpdate": "1785571200000", "minFundingRate": "-0.003", "maxFundingRate": "0.003"},
    {"symbol": "DOGEUSDT", "fundingRate": "-0.0012", "fundingRateInterval": "4",
     "nextUpdate": "1785560400000", "minFundingRate": "-0.005", "maxFundingRate": "0.005"},
]


def test_funding_rate_keeps_exchange_sign():
    """交易所原始约定：正 = 多头付给空头。适配器原样存，绝不取反。"""
    ad, _ = make_adapter({"/api/v2/mix/market/current-fund-rate": ok(FUND_ROWS)})
    rates = run(ad, ad.fetch_carry_rates())
    by = {r.symbol: r for r in rates}
    assert by["BTCUSDT"].rate == Decimal("0.000054")     # 正的还是正的
    assert by["DOGEUSDT"].rate == Decimal("-0.0012")     # 负的还是负的
    assert isinstance(by["BTCUSDT"].rate, Decimal)


def test_funding_interval_is_carried_not_assumed():
    """51% 的 U 本位是 4h，写死 8h 会让年化直接错一倍。"""
    ad, _ = make_adapter({"/api/v2/mix/market/current-fund-rate": ok(FUND_ROWS)})
    rates = run(ad, ad.fetch_carry_rates(["BTCUSDT", "DOGEUSDT"]))
    by = {r.symbol: r for r in rates}
    assert by["BTCUSDT"].interval_s == 28800
    assert by["DOGEUSDT"].interval_s == 14400
    assert by["BTCUSDT"].next_settle_ms == 1785571200000
    # 归一化后 4h 的 -0.0012 年化是 8h 同数值的两倍
    assert by["DOGEUSDT"].annualized() == Decimal("-0.0012") * (365 * 24 * 3600 // 14400)


def test_funding_limits_returns_cap_and_floor():
    ad, _ = make_adapter({"/api/v2/mix/market/current-fund-rate": ok(FUND_ROWS)})
    limits = run(ad, ad.fetch_funding_limits("BTCUSDT"))
    assert limits == (Decimal("0.003"), Decimal("-0.003"))


def test_funding_history_ascending_and_filtered():
    pages = {
        "1": [{"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingTime": str(3000 + i)}
              for i in range(100)],
        "2": [{"symbol": "BTCUSDT", "fundingRate": "0.0002", "fundingTime": str(1000 + i)}
              for i in range(100)],
    }

    def handler(request):
        page = httpx.QueryParams(request.url.query.decode()).get("pageNo")
        return httpx.Response(200, json=ok(pages.get(page, [])))

    ad, _ = make_adapter({"/api/v2/mix/market/history-fund-rate": handler})
    hist = run(ad, ad.fetch_funding_history("BTCUSDT", since_ms=1050))
    assert hist == sorted(hist, key=lambda x: x[0])       # 升序
    assert all(ts >= 1050 for ts, _ in hist)              # since 之前的丢掉
    assert all(isinstance(r, Decimal) for _, r in hist)


def test_funding_history_dedupes_when_a_settlement_lands_mid_pagination():
    """pageNo 分页没有时间锚：翻页途中跨过一次结算，整个列表就整体下移一位，
    原 page1 的最后一条会变成 page2 的第一条，同一笔费率进两次。

    全市场 742 个符号 × 3 页要跑上百秒，跨结算边界是必然事件而不是边角情况。
    后果不报错，只是 autocorr() 被一对相邻相同值抬高、median 轻微偏移——
    稳定性评分悄悄偏乐观。HTX 同样按页号翻，那边用的就是字典去重。
    """
    newest_first = list(range(1199, 999, -1))            # 200 条，新→旧
    state = {"settled": False}

    def handler(request):
        page = int(httpx.QueryParams(request.url.query.decode()).get("pageNo"))
        rows = ([1200] if state["settled"] else []) + newest_first
        if page >= 1:
            state["settled"] = True                       # 第一页之后又结算了一次
        window = rows[(page - 1) * 100: page * 100]
        return httpx.Response(200, json=ok(
            [{"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingTime": str(ts)}
             for ts in window]))

    ad, _ = make_adapter({"/api/v2/mix/market/history-fund-rate": handler})
    hist = run(ad, ad.fetch_funding_history("BTCUSDT", since_ms=0))

    stamps = [ts for ts, _ in hist]
    assert len(stamps) == len(set(stamps)), \
        f"翻页错位造成重复计费：{sorted(t for t in set(stamps) if stamps.count(t) > 1)}"
    assert stamps == sorted(stamps)


# ---- 合约规格与精度 -----------------------------------------------------

def test_instrument_precision_is_derived_not_guessed():
    ad, _ = make_adapter()
    insts = {i.symbol: i for i in run(ad, ad.fetch_instruments())}
    assert "DEADUSDT" not in insts                        # symbolStatus != normal 被过滤
    btc = insts["BTCUSDT"]
    assert btc.tick_size == Decimal("0.1")                # priceEndStep 1 * 10^-1
    assert btc.lot_size == Decimal("0.0001")
    assert btc.min_notional == Decimal("5")
    assert btc.contract_size == Decimal("1")
    assert btc.max_leverage == Decimal("150")
    assert btc.funding_interval_s == 28800
    assert insts["DOGEUSDT"].funding_interval_s == 14400
    assert insts["STEPUSDT"].funding_interval_s == 3600
    # priceEndStep=5 → tick 0.05，不是 10^-2
    assert insts["STEPUSDT"].tick_size == Decimal("0.05")


def test_qty_rounds_toward_zero_and_price_to_tick():
    ad, rec = make_adapter()
    run(ad, ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy",
                                        qty=Decimal("0.012345"),
                                        price=Decimal("62914.37"),
                                        client_order_id="leg1-001")))
    body = json.loads([r for r in rec if r.url.path.endswith("place-order")][0].content)
    assert body["size"] == "0.0123"        # 0.012345 → 向零取整到 0.0001
    assert body["price"] == "62914.4"      # 买单向上取到 0.1 的 tick


def test_sell_price_rounds_down_to_tick():
    """价格取整方向与 executor.cross_price 一致：买单向上、卖单向下。

    传下来的已经是穿价保护限价（自带穿透），反向取整会把穿透削回来；
    tick 相对价格很粗的小币上那不是少赚一个 tick，而是挂不上、这条腿永远补不平。
    """
    ad, rec = make_adapter()
    run(ad, ad.place_order(OrderRequest(symbol="BTCUSDT", side="sell",
                                        qty=Decimal("0.01"),
                                        price=Decimal("62914.31"),
                                        client_order_id="leg2-001")))
    body = json.loads([r for r in rec if r.url.path.endswith("place-order")][0].content)
    assert body["price"] == "62914.3"      # 卖单向下


def test_integer_lot_and_no_scientific_notation():
    ad, rec = make_adapter()
    run(ad, ad.place_order(OrderRequest(symbol="DOGEUSDT", side="buy",
                                        qty=Decimal("3.7"),
                                        price=Decimal("0.000123456"),
                                        client_order_id="leg3-001")))
    body = json.loads([r for r in rec if r.url.path.endswith("place-order")][0].content)
    assert body["size"] == "3"             # lot=1 → 向零取整
    assert body["price"] == "0.00013"      # tick=1e-05，且不能是科学计数法
    assert "E" not in body["price"] and "e" not in body["price"]


def test_qty_pinched_to_zero_is_rejected_not_sent():
    ad, rec = make_adapter()
    with pytest.raises(OrderRejected):
        run(ad, ad.place_order(OrderRequest(symbol="DOGEUSDT", side="buy",
                                            qty=Decimal("0.4"), price=Decimal("0.1"),
                                            client_order_id="leg4-001")))
    assert not any(r.url.path.endswith("place-order") for r in rec)


# ---- 下单语义 -----------------------------------------------------------

def test_below_min_trade_num_is_blocked_locally():
    """MINUSDT 取整后是 0.5（非零），但 minTradeNum=1 —— 本地就该拦下。

    交易所同样会拒，但白跑一个下单配额还多等一个 RTT。
    """
    ad, rec = make_adapter()
    with pytest.raises(OrderRejected) as exc:
        run(ad, ad.place_order(OrderRequest(symbol="MINUSDT", side="buy",
                                            qty=Decimal("0.55"), price=Decimal("1"),
                                            client_order_id="c9")))
    assert exc.value.code == "MIN_QTY"
    assert not any(r.url.path.endswith("place-order") for r in rec)


def test_quantize_without_specs_raises_clearly():
    ad, _ = make_adapter()
    with pytest.raises(ExchangeError) as exc:
        ad.quantize_qty("BTCUSDT", Decimal("1"))
    assert exc.value.code == "SPEC_MISSING"
    run(ad, asyncio.sleep(0))


def test_client_order_id_is_mandatory():
    ad, rec = make_adapter()
    with pytest.raises(ValueError):
        run(ad, ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy",
                                            qty=Decimal("0.01"), price=Decimal("62900"))))
    assert not any(r.url.path.endswith("place-order") for r in rec)


def test_one_way_mode_uses_reduce_only_flag():
    ad, rec = make_adapter()
    run(ad, ad.place_order(OrderRequest(symbol="BTCUSDT", side="sell", qty=Decimal("0.01"),
                                        price=Decimal("62900"), reduce_only=True,
                                        time_in_force="FOK", client_order_id="c1")))
    body = json.loads([r for r in rec if r.url.path.endswith("place-order")][0].content)
    assert body["side"] == "sell"
    assert body["reduceOnly"] == "YES"
    assert "tradeSide" not in body
    assert body["force"] == "fok"          # 全小写
    assert body["marginCoin"] == "USDT"


def test_hedge_mode_close_inverts_side():
    """双向模式 side = 仓位方向：平多要写 side=buy，且不能带 reduceOnly。"""
    ad, rec = make_adapter(position_mode="hedge_mode")
    run(ad, ad.place_order(OrderRequest(symbol="BTCUSDT", side="sell", qty=Decimal("0.01"),
                                        price=Decimal("62900"), reduce_only=True,
                                        client_order_id="c2")))
    body = json.loads([r for r in rec if r.url.path.endswith("place-order")][0].content)
    assert body["side"] == "buy"           # 卖出减多头 → side=buy
    assert body["tradeSide"] == "close"
    assert "reduceOnly" not in body


def test_hedge_mode_open_keeps_side():
    ad, rec = make_adapter(position_mode="hedge_mode")
    run(ad, ad.place_order(OrderRequest(symbol="BTCUSDT", side="sell", qty=Decimal("0.01"),
                                        price=Decimal("62900"), client_order_id="c3")))
    body = json.loads([r for r in rec if r.url.path.endswith("place-order")][0].content)
    assert body["side"] == "sell" and body["tradeSide"] == "open"


def test_market_order_has_no_price_or_force():
    ad, rec = make_adapter()
    run(ad, ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy", qty=Decimal("0.01"),
                                        price=None, client_order_id="c4")))
    body = json.loads([r for r in rec if r.url.path.endswith("place-order")][0].content)
    assert body["orderType"] == "market"
    assert "price" not in body and "force" not in body


def test_missing_order_id_falls_back_to_client_oid():
    """单向模式的 reduce-only 可能返回没有 orderId 的响应，状态机只能认 clientOid。"""
    ad, _ = make_adapter({"/api/v2/mix/order/place-order": ok({"clientOid": "c5"})})
    res = run(ad, ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy",
                                              qty=Decimal("0.01"), price=Decimal("62900"),
                                              reduce_only=True, client_order_id="c5")))
    assert res.exchange_order_id == ""
    assert res.client_order_id == "c5"


# ---- 超时 / 错误映射 ----------------------------------------------------

def _raise(exc):
    def handler(request):
        raise exc
    return handler


def test_timeout_raises_order_unknown():
    """超时 != 失败。当成失败返回会导致重发和双倍仓位。"""
    ad, _ = make_adapter({"/api/v2/mix/order/place-order":
                          _raise(httpx.ReadTimeout("timed out"))})
    with pytest.raises(OrderUnknown):
        run(ad, _place(ad))


def test_connection_error_raises_order_unknown():
    ad, _ = make_adapter({"/api/v2/mix/order/place-order":
                          _raise(httpx.ConnectError("connection reset"))})
    with pytest.raises(OrderUnknown):
        run(ad, _place(ad))


def test_protocol_error_raises_order_unknown():
    """RemoteProtocolError 不是 NetworkError，会绕过 base 的兜底，必须自己接住。"""
    ad, _ = make_adapter({"/api/v2/mix/order/place-order":
                          _raise(httpx.RemoteProtocolError("server disconnected"))})
    with pytest.raises(OrderUnknown):
        run(ad, _place(ad))


def test_http_5xx_on_place_order_is_unknown():
    ad, _ = make_adapter({"/api/v2/mix/order/place-order":
                          lambda r: httpx.Response(502, text="bad gateway")})
    with pytest.raises(OrderUnknown):
        run(ad, _place(ad))


def test_rate_limited_on_place_order_is_not_unknown():
    """被限频 = 明确没进撮合，可以安全重试，不该被误判成未知状态。"""
    ad, _ = make_adapter({"/api/v2/mix/order/place-order":
                          lambda r: httpx.Response(429, json={"code": "429",
                                                              "msg": "Too many requests"})})
    with pytest.raises(RateLimited) as exc:
        run(ad, _place(ad))
    assert exc.value.retriable is True


def test_business_error_on_place_order_is_rejected():
    ad, _ = make_adapter({"/api/v2/mix/order/place-order":
                          lambda r: httpx.Response(200, json={"code": "43012",
                                                              "msg": "Insufficient balance"})})
    with pytest.raises(OrderRejected) as exc:
        run(ad, _place(ad))
    assert exc.value.retriable is False
    assert exc.value.code == "43012"


def test_chinese_system_busy_is_unknown_like_its_english_twin():
    """三张关键词表的语言口径必须一致：同一句"系统繁忙"，英文进 OrderUnknown、
    中文却落到"可安全重发"的 OrderRejected，就是一个纯靠语言运气的双倍仓位。"""
    for msg in ("system busy, please try again", "系统繁忙，请稍后重试"):
        ad, _ = make_adapter({"/api/v2/mix/order/place-order":
                              lambda r, m=msg: httpx.Response(
                                  200, json={"code": "40725", "msg": m})})
        with pytest.raises(OrderUnknown):
            run(ad, _place(ad))


def test_unrecognized_error_falls_to_unknown_not_rejected():
    """Bitget 没有官方错误码表，新码随时冒出来。认不出来 = 不知道有没有进撮合，
    必须往未知侧倒：多跑一轮 cancel+查单只花一次 RTT，
    误判成 OrderRejected（"可安全重发"）是双倍仓位。"""
    ad, _ = make_adapter({"/api/v2/mix/order/place-order":
                          lambda r: httpx.Response(200, json={"code": "45110",
                                                              "msg": "unlisted new code"})})
    with pytest.raises(OrderUnknown):
        run(ad, _place(ad))


def test_unreadable_200_body_is_unknown_not_rejected():
    ad, _ = make_adapter({"/api/v2/mix/order/place-order":
                          lambda r: httpx.Response(200, content=b"<html>blocked</html>")})
    with pytest.raises(OrderUnknown):
        run(ad, _place(ad))


def test_risk_path_never_downgrades_unknown_errors_to_rejected():
    """OrderRejected 在 resolve_unknown_order 里等价于"成交量确定为 0"。
    把一个看不懂的错误降级成它，就是把一张可能已成交的单谎报成从未被接受。"""
    ad, _ = make_adapter({"/api/v2/mix/order/detail":
                          lambda r: httpx.Response(200, json={"code": "45110",
                                                              "msg": "unlisted new code"})})
    with pytest.raises(ExchangeError) as exc:
        run(ad, ad.query_order("BTCUSDT", "c1"))
    assert not isinstance(exc.value, OrderRejected)


def test_unknown_order_maps_to_order_rejected():
    """交易所说"没这个单" → OrderRejected，让 resolve_unknown_order 判定成交量为 0。"""
    ad, _ = make_adapter({"/api/v2/mix/order/detail":
                          lambda r: httpx.Response(200, json={"code": "40109",
                                                              "msg": "The order does not exist"})})
    with pytest.raises(OrderRejected):
        run(ad, ad.query_order("BTCUSDT", "ghost"))


def test_transport_error_on_query_is_retriable_for_resolver():
    """base.resolve_unknown_order 只对 (httpx.HTTPError, RateLimited) 退避重试，
    传输层抖动必须落进这个集合，否则最危险的状态下消解流程直接崩。"""
    ad, _ = make_adapter({"/api/v2/mix/order/detail":
                          _raise(httpx.ReadTimeout("timed out"))})
    with pytest.raises(RateLimited) as exc:
        run(ad, ad.query_order("BTCUSDT", "c1"))
    assert exc.value.retriable is True
    assert exc.value.code == "NETWORK"


def test_remaining_quota_header_is_recorded():
    ad, _ = make_adapter({"/api/v2/mix/market/current-fund-rate":
                          lambda r: httpx.Response(200, json=ok(FUND_ROWS),
                                                   headers={"X-MBX-USED-REMAIN-LIMIT": "19"})})
    run(ad, ad.fetch_carry_rates())
    assert ad.remaining_quota == 19


# ---- 持仓 / 挂单 / 盘口 -------------------------------------------------

def test_positions_are_signed_and_pos_mode_is_learned():
    rows = [
        {"symbol": "BTCUSDT", "holdSide": "long", "total": "0.5", "openDelegateSize": "0.1",
         "openPriceAvg": "62000", "markPrice": "62900", "leverage": "10",
         "liquidationPrice": "55000", "posMode": "hedge_mode", "marginMode": "crossed"},
        {"symbol": "DOGEUSDT", "holdSide": "short", "total": "1000", "openDelegateSize": "0",
         "openPriceAvg": "0.12", "markPrice": "0.11", "leverage": "5",
         "liquidationPrice": "", "posMode": "hedge_mode", "marginMode": "crossed"},
    ]
    ad, _ = make_adapter({"/api/v2/mix/position/all-position": ok(rows)})
    positions = run(ad, ad.fetch_positions())
    by = {p.symbol: p for p in positions}
    assert by["BTCUSDT"].qty == Decimal("0.5")
    assert by["DOGEUSDT"].qty == Decimal("-1000")        # short 记负
    assert by["BTCUSDT"].liquidation_price == Decimal("55000")
    assert by["DOGEUSDT"].liquidation_price is None      # 空串不是 0
    assert ad.position_mode == "hedge_mode"              # 真值覆盖了配置兜底


def test_absent_position_means_flat():
    ad, _ = make_adapter({"/api/v2/mix/position/all-position": ok([])})
    assert run(ad, ad.fetch_positions()) == []


def test_open_orders_parsed():
    ad, _ = make_adapter({"/api/v2/mix/order/orders-pending": ok({"entrustedList": [
        {"orderId": "1", "clientOid": "c1", "state": "live", "baseVolume": "0",
         "priceAvg": "0"},
        {"orderId": "2", "clientOid": "c2", "state": "partially_filled",
         "baseVolume": "0.004", "priceAvg": "62900"},
    ]})})
    orders = run(ad, ad.fetch_open_orders("BTCUSDT"))
    assert [o.status for o in orders] == ["new", "partial"]
    assert orders[1].filled_qty == Decimal("0.004")


def test_book_top_is_decimal():
    ad, _ = make_adapter({DEPTH_PATH: ok({
        "asks": [["62914", "2.8221"]], "bids": [["62913.9", "6.9558"]],
        "ts": "1785548251835"})})
    top = run(ad, ad.fetch_book_top("BTCUSDT"))
    assert top.bid_price == Decimal("62913.9") and top.ask_qty == Decimal("2.8221")
    assert top.ts_ms == 1785548251835


# ---- 多档深度 -----------------------------------------------------------
#
# 手算口径（下面几乎所有断言都基于它）：
#   best_bid = 99.99, best_ask = 100.00 → mid = 99.995
#   10bp 带宽 → lo = 99.995*0.999 = 99.895005, hi = 99.995*1.001 = 100.094995
#   买侧带内: 99.99*1 + 99.95*2 + 99.90*3 = 599.59   （99.80 落在 lo 之外）
#   卖侧带内: 100.00*1 + 100.05*2      = 300.10      （100.10 落在 hi 之外）
# 99.90 只比 lo 高 0.0049995、100.10 只比 hi 高 0.005005 —— 故意贴着边界放，
# 边界判反或用 float 比价都会立刻翻车。

DEPTH_BOOK = {
    "bids": [["99.99", "1"], ["99.95", "2"], ["99.90", "3"], ["99.80", "4"]],
    "asks": [["100.00", "1"], ["100.05", "2"], ["100.10", "3"], ["100.20", "4"]],
    "ts": "1785548251835",
}


def test_depth_accumulates_notional_within_band():
    ad, _ = make_adapter({DEPTH_PATH: ok(DEPTH_BOOK)})
    depth = run(ad, ad.fetch_book_depth("BTCUSDT", band_bp=10.0))

    assert depth.bid_notional == Decimal("599.59")
    assert depth.ask_notional == Decimal("300.10")
    assert depth.levels_used == 5                 # 买 3 档 + 卖 2 档
    # 一档信息仍然照带，深度不该顺手把 BookTop 那份数据弄丢
    assert (depth.bid_price, depth.bid_qty) == (Decimal("99.99"), Decimal("1"))
    assert (depth.ask_price, depth.ask_qty) == (Decimal("100.00"), Decimal("1"))
    assert depth.symbol == "BTCUSDT" and depth.band_bp == 10.0
    assert depth.ts_ms == 1785548251835
    # 全程 Decimal：任何一处漏成 float 都会让下单精度这条链路后面出事
    assert all(isinstance(v, Decimal) for v in
               (depth.bid_notional, depth.ask_notional, depth.bid_price,
                depth.bid_qty, depth.ask_price, depth.ask_qty))
    # 一档量当深度代理会低估两个数量级 —— 这正是这个方法存在的理由
    assert depth.thinner_side > depth.bid_price * depth.bid_qty


def test_depth_excludes_levels_outside_band():
    """带宽收窄到 1bp，除一档外全部出界，累计额必须跟着掉。"""
    ad, _ = make_adapter({DEPTH_PATH: ok(DEPTH_BOOK)})
    depth = run(ad, ad.fetch_book_depth("BTCUSDT", band_bp=1.0))
    # lo = 99.9850005, hi = 100.0049995 → 两侧各只剩一档
    assert depth.bid_notional == Decimal("99.99")
    assert depth.ask_notional == Decimal("100.00")
    assert depth.levels_used == 2
    # 99.95 / 100.05 那两档一分钱都不能混进来
    assert depth.bid_notional < Decimal("299.89")
    assert depth.ask_notional < Decimal("300.10")


def test_depth_is_base_units_on_bitget_specs():
    """Bitget mix 的 size 本来就是 base 币（contract_size 恒为 1），不该被二次缩放。"""
    ad, _ = make_adapter({DEPTH_PATH: ok(DEPTH_BOOK)})

    async def go():
        insts = {i.symbol: i for i in await ad.fetch_instruments()}
        assert insts["BTCUSDT"].contract_size == Decimal("1")
        return await ad.fetch_book_depth("BTCUSDT", band_bp=10.0)

    depth = run(ad, go())
    assert depth.bid_qty == Decimal("1")              # 原样，不乘不除
    assert depth.bid_notional == Decimal("599.59")


def test_depth_applies_contract_size_when_venue_has_one():
    """守住"别把张数当币数"这条线。

    Bitget 现在没有张制合约（所以 fetch_instruments 里 contract_size 写死 1），
    这里手塞一个面值 10 的规格：万一哪天上了张制品种，名义额必须跟着乘面值，
    否则 BTC 这种面值上会是一万倍的量级错误。
    """
    ad, _ = make_adapter({DEPTH_PATH: ok(DEPTH_BOOK)})
    ad._instruments["FAKEUSDT"] = Instrument(
        market=ad.market_key, symbol="FAKEUSDT", base="FAKE", quote="USDT",
        tick_size=Decimal("0.01"), lot_size=Decimal("1"), min_notional=Decimal("5"),
        contract_size=Decimal("10"),
    )
    depth = run(ad, ad.fetch_book_depth("FAKEUSDT", band_bp=10.0))
    assert depth.bid_notional == Decimal("5995.90")   # 599.59 * 10
    assert depth.ask_notional == Decimal("3001.00")   # 300.10 * 10
    assert depth.bid_qty == Decimal("10")             # 一档量同样换成币
    assert depth.ask_qty == Decimal("10")


def test_depth_reports_short_book_without_extrapolating():
    """端点只返回两档、带宽 50bp 还没走完 —— 如实报 levels_used，绝不补足。"""
    short_book = {
        "bids": [["100", "1"], ["99.99", "2"]],
        "asks": [["100.01", "3"], ["100.02", "4"]],
        "ts": "1785548251835",
    }
    ad, _ = make_adapter({DEPTH_PATH: ok(short_book)})
    depth = run(ad, ad.fetch_book_depth("BTCUSDT", band_bp=50.0))
    # 四档全在带内 ⇒ levels_used 等于返回档数 ⇒ 上层据此知道这是下限不是真值
    assert depth.levels_used == 4
    assert depth.bid_notional == Decimal("299.98")    # 100*1 + 99.99*2，没有外推
    assert depth.ask_notional == Decimal("700.11")    # 100.01*3 + 100.02*4


def test_depth_ignores_zero_size_levels():
    """0 量档位是噪声：既不计名义额，也不能灌水 levels_used
    （否则"档位够不够覆盖带宽"的判断就废了）。"""
    book = {
        "bids": [["99.99", "1"], ["99.98", "0"]],
        "asks": [["100.00", "1"], ["100.01", "0"]],
        "ts": "1785548251835",
    }
    ad, _ = make_adapter({DEPTH_PATH: ok(book)})
    depth = run(ad, ad.fetch_book_depth("BTCUSDT", band_bp=10.0))
    assert depth.levels_used == 2
    assert depth.bid_notional == Decimal("99.99") and depth.ask_notional == Decimal("100.00")


def test_depth_requests_full_levels_on_market_quota(monkeypatch):
    """深度比一档贵得多（同一个端点、同一个 20 次/秒 IP 池），
    必须走 market 配额，绝不能挪去挤风控的额度。"""
    seen: list[tuple[str, str]] = []
    original = BitgetAdapter._request

    async def spy(self, method, path, *, quota="market", **kwargs):
        seen.append((path, quota))
        return await original(self, method, path, quota=quota, **kwargs)

    monkeypatch.setattr(BitgetAdapter, "_request", spy)
    ad, rec = make_adapter({DEPTH_PATH: ok(DEPTH_BOOK)})
    run(ad, ad.fetch_book_depth("BTCUSDT"))

    assert (DEPTH_PATH, "market") in seen
    query = httpx.QueryParams(
        [r for r in rec if r.url.path == DEPTH_PATH][0].url.query.decode())
    # 实测打真实端点得出：limit 只接受 1/5/15/50/100。
    # 之前写的 "max" 会被回 40020 Parameter error，深度接口整个失效
    # ——而失效时上层会静默退回一档代理，容量被低估两个数量级却不报错，
    # 所以这条断言是在锁死一个"静默降级"的坑。
    assert query["limit"] == "100"
    assert query["precision"] == "scale0"   # 不合并价位，否则粗档会横跨带宽边界
    assert query["productType"] == "USDT-FUTURES"


def test_depth_rejects_nonpositive_band():
    """带宽 <= 0 时深度恒为 0，静默返回会让能做的机会被判成不可执行。"""
    ad, _ = make_adapter({DEPTH_PATH: ok(DEPTH_BOOK)})
    with pytest.raises(ValueError):
        run(ad, ad.fetch_book_depth("BTCUSDT", band_bp=0.0))


def test_depth_empty_side_raises():
    ad, _ = make_adapter({DEPTH_PATH: ok({"bids": [], "asks": [["100", "1"]],
                                          "ts": "1785548251835"})})
    with pytest.raises(ExchangeError) as exc:
        run(ad, ad.fetch_book_depth("BTCUSDT"))
    assert exc.value.code == "EMPTY_BOOK"


def test_fee_schedule_uses_signed_trade_rate():
    ad, rec = make_adapter({"/api/v2/common/trade-rate":
                            ok({"makerFeeRate": "0.0002", "takerFeeRate": "0.0006"})})
    fees = run(ad, ad.fetch_fee_schedule(["BTCUSDT", "DOGEUSDT"]))
    assert len(fees) == 2
    assert fees[0].taker == Decimal("0.0006") and fees[0].maker == Decimal("0.0002")
    req = [r for r in rec if "trade-rate" in r.url.path][0]
    assert req.headers["ACCESS-KEY"] == API_KEY          # 必须签名，才拿得到自己的档位
    assert b"businessType=mix" in req.url.query


def test_leverage_tiers_fall_back_to_single_tier():
    ad, _ = make_adapter()      # query-position-lever 未路由 → 404 → 退化
    tiers = run(ad, ad.fetch_leverage_tiers("BTCUSDT"))
    assert tiers == [(Decimal("Infinity"), Decimal("150"))]


def test_set_leverage_cross_sends_only_leverage():
    """全仓下多发 longLeverage 会被静默采纳并覆盖 leverage —— 绝不能带。"""
    ad, rec = make_adapter({"/api/v2/mix/account/set-leverage": ok({})})
    run(ad, ad.set_leverage("BTCUSDT", Decimal("5")))
    body = json.loads([r for r in rec if "set-leverage" in r.url.path][0].content)
    assert body["leverage"] == "5"
    assert "longLeverage" not in body and "shortLeverage" not in body


def test_server_time_parsed():
    ad, _ = make_adapter()
    assert run(ad, ad.fetch_server_time_ms()) == 1785548132637


# ---- 限频配额分池 -------------------------------------------------------

def test_quota_buckets(monkeypatch):
    """风控路径（撤单/查单/查仓/查挂单）必须走独立配额，不能被行情轮询挤掉。"""
    seen: list[tuple[str, str]] = []
    original = BitgetAdapter._request

    async def spy(self, method, path, *, quota="market", **kwargs):
        seen.append((path, quota))
        return await original(self, method, path, quota=quota, **kwargs)

    monkeypatch.setattr(BitgetAdapter, "_request", spy)

    ad, _ = make_adapter({
        "/api/v2/mix/market/current-fund-rate": ok(FUND_ROWS),
        "/api/v2/mix/order/cancel-order": ok({}),
        "/api/v2/mix/order/detail": ok({"orderId": "1", "clientOid": "c1", "state": "filled",
                                        "baseVolume": "0.01", "priceAvg": "62900"}),
        "/api/v2/mix/position/all-position": ok([]),
        "/api/v2/mix/order/orders-pending": ok({"entrustedList": []}),
    })

    async def exercise():
        await ad.fetch_carry_rates()
        await ad.fetch_instruments()
        await _place(ad)
        await ad.cancel_order("BTCUSDT", "c1")
        await ad.query_order("BTCUSDT", "c1")
        await ad.fetch_positions()
        await ad.fetch_open_orders("BTCUSDT")

    run(ad, exercise())
    quota = dict(seen)
    assert quota["/api/v2/mix/market/current-fund-rate"] == "market"
    assert quota["/api/v2/mix/market/contracts"] == "market"
    assert quota["/api/v2/mix/order/place-order"] == "trade"
    assert quota["/api/v2/mix/order/cancel-order"] == "risk"
    assert quota["/api/v2/mix/order/detail"] == "risk"
    assert quota["/api/v2/mix/position/all-position"] == "risk"
    assert quota["/api/v2/mix/order/orders-pending"] == "risk"


# ---- WebSocket ----------------------------------------------------------

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


def test_ws_subscribes_and_survives_raw_text_heartbeat(monkeypatch):
    """'ping'/'pong' 是裸文本帧，不是 JSON —— 读循环必须在 json.loads 之前特判。"""
    import sys
    import types

    ws = _FakeWS([
        "pong",                                          # 心跳回执，不能进 json.loads
        json.dumps({"event": "subscribe",
                    "arg": {"instType": "USDT-FUTURES", "channel": "books1",
                            "instId": "BTCUSDT"}}),
        "ping",                                          # 服务端主动 ping，要回 pong
        json.dumps({"action": "snapshot",
                    "arg": {"instType": "USDT-FUTURES", "channel": "books1",
                            "instId": "BTCUSDT"},
                    "data": [{"asks": [["62914", "2.8221"]],
                              "bids": [["62913.9", "6.9558"]],
                              "ts": "1785548251835", "seq": 793680826410, "pseq": 0}],
                    "ts": 1785548251836}),
    ])
    fake = types.ModuleType("websockets")
    fake.connect = lambda url, **kw: _FakeConnect(ws)      # noqa: ARG005
    monkeypatch.setitem(sys.modules, "websockets", fake)

    ad, _ = make_adapter()
    got = []

    async def go():
        stream = ad.stream_book_top(["BTCUSDT"])
        async for top in stream:
            got.append(top)
            break
        await stream.aclose()

    run(ad, go())

    assert len(got) == 1
    assert got[0].bid_price == Decimal("62913.9")
    assert got[0].ask_qty == Decimal("2.8221")
    assert got[0].ts_ms == 1785548251835
    sub = json.loads(ws.sent[0])
    assert sub["op"] == "subscribe"
    assert sub["args"] == [{"instType": "USDT-FUTURES", "channel": "books1",
                            "instId": "BTCUSDT"}]
    assert "pong" in ws.sent                               # 回了服务端的 ping


def test_ws_drops_out_of_order_packets(monkeypatch):
    """seq 单调递增，回退的包是乱序/重放，直接丢。"""
    import sys
    import types

    def frame(seq, bid):
        return json.dumps({"arg": {"instId": "BTCUSDT"},
                           "data": [{"asks": [["62914", "1"]], "bids": [[bid, "1"]],
                                     "ts": "1785548251835", "seq": seq}]})

    ws = _FakeWS([frame(100, "62913.9"), frame(99, "1"), frame(101, "62915.5")])
    fake = types.ModuleType("websockets")
    fake.connect = lambda url, **kw: _FakeConnect(ws)      # noqa: ARG005
    monkeypatch.setitem(sys.modules, "websockets", fake)

    ad, _ = make_adapter()
    got = []

    async def go():
        stream = ad.stream_book_top(["BTCUSDT"])
        async for top in stream:
            got.append(top)
            if len(got) == 2:
                break
        await stream.aclose()

    run(ad, go())
    assert [t.bid_price for t in got] == [Decimal("62913.9"), Decimal("62915.5")]


def test_ws_batches_subscriptions():
    from earnfarm.exchanges.bitget import WS_SUB_BATCH, _chunks
    symbols = [f"S{i}USDT" for i in range(120)]
    chunks = _chunks(symbols, WS_SUB_BATCH)
    assert [len(c) for c in chunks] == [50, 50, 20]
    assert sum(len(c) for c in chunks) == 120


# ---- 未实现的市场 -------------------------------------------------------

def test_spot_adapter_is_explicitly_unimplemented():
    with pytest.raises(NotImplementedError):
        BitgetSpotAdapter(Credential(API_KEY, SECRET, PASSPHRASE))
