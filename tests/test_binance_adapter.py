"""币安适配器测试。全程 httpx.MockTransport 注入假响应，不连交易所。

签名的金标准用币安官方文档给出的示例向量（LTCBTC 那条），它同时验证了两件事：
totalParams 的拼接顺序，以及 HMAC-SHA256 的最终十六进制值。
"""

from __future__ import annotations

import asyncio
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
from earnfarm.exchanges.binance import BinanceAdapter
from earnfarm.models import Instrument, MarketKind

# 官方文档示例的 key/secret 与预期签名
OFFICIAL_SECRET = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
OFFICIAL_KEY = "vmPUZE6mv9SD5VNHk4HlWFsOr6aKE2zvsw0MuIgwCIPy6utIco14y7Ju91duEh8A"
OFFICIAL_SIG = "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
OFFICIAL_TOTAL = ("symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC"
                  "&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559")

FIXED_TS = 1_785_000_000_000
BROKER = "x-cvBPrNm9"


def run(coro):
    return asyncio.run(coro)


def make_adapter(handler, *, broker_code: str = "", secret: str = OFFICIAL_SECRET,
                 **kwargs) -> BinanceAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ad = BinanceAdapter(
        Credential(api_key=OFFICIAL_KEY, api_secret=secret),
        broker_code=broker_code, client=client, **kwargs,
    )
    # 别让测试触发校时请求；固定时间戳让签名可复现
    ad._clock.synced_at = time.time()
    ad.timestamp_ms = lambda: FIXED_TS          # type: ignore[method-assign]
    return ad


def json_route(routes: dict, captured: list | None = None):
    """按路径分发的假响应。routes: path -> dict/list 或 (status, payload)。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        entry = routes.get(request.url.path)
        if entry is None:
            return httpx.Response(404, json={"code": -1121, "msg": f"no route {request.url.path}"})
        status, payload = entry if isinstance(entry, tuple) else (200, entry)
        return httpx.Response(status, content=json.dumps(payload),
                              headers={"Content-Type": "application/json",
                                       "X-MBX-USED-WEIGHT-1M": "37"})
    return handler


def load_btc(ad: BinanceAdapter) -> None:
    """注入 BTCUSDT 规格，免得每个下单测试都要先拉 exchangeInfo。"""
    ad._instruments["BTCUSDT"] = Instrument(
        market="binance:perp", symbol="BTCUSDT", base="BTC", quote="USDT",
        tick_size=Decimal("0.10"), lot_size=Decimal("0.001"),
        min_notional=Decimal("5"), contract_size=Decimal("1"),
    )
    ad._precision["BTCUSDT"] = (2, 3)


ORDER_OK = {
    "orderId": 283194212, "symbol": "BTCUSDT", "status": "FILLED",
    "clientOrderId": "will-be-overwritten", "price": "61234.50",
    "avgPrice": "61234.50", "origQty": "0.012", "executedQty": "0.012",
    "cumQuote": "734.81", "timeInForce": "IOC", "type": "LIMIT", "side": "BUY",
}


# ---- 签名 ---------------------------------------------------------------

def test_official_signature_vector():
    """金标准：官方文档示例的 totalParams 与签名值必须一字不差对上。"""
    ad = make_adapter(json_route({}))
    params = {
        "symbol": "LTCBTC", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC",
        "quantity": Decimal("1"), "price": Decimal("0.1"),
        "recvWindow": 5000, "timestamp": 1499827319559,
    }
    total = ad.total_params(params)
    assert total == OFFICIAL_TOTAL            # Decimal 也要序列化成 "1" / "0.1"
    assert ad._hmac_sha256_hex(total) == OFFICIAL_SIG


def test_total_params_is_query_then_body():
    """官方定义：totalParams = query string 拼接 request body，按出现顺序原样相连。

    同一组参数无论怎么切分到 query / body，签名必须相同。
    """
    ad = make_adapter(json_route({}))
    query = "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC"
    body = "&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"
    assert ad.total_params(query, body) == OFFICIAL_TOTAL
    assert ad._hmac_sha256_hex(ad.total_params(query, body)) == OFFICIAL_SIG


def test_signed_post_sends_exactly_what_it_signed():
    """签一份发另一份是 -1022 的头号来源：发出的字节必须与被签的字节完全一致。"""
    captured: list[httpx.Request] = []
    ad = make_adapter(json_route({"/fapi/v1/order": ORDER_OK}, captured))
    load_btc(ad)
    run(ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy", qty=Decimal("0.012"),
                                    price=Decimal("61234.5"), client_order_id="h1")))
    body = captured[0].content.decode()
    signed_part, _, sig = body.rpartition("&signature=")
    assert sig == hmac.new(OFFICIAL_SECRET.encode(), signed_part.encode(),
                           hashlib.sha256).hexdigest()
    # signature 必须在最末尾，timestamp/recvWindow 走的是校准后的时钟
    assert body.endswith(f"&signature={sig}")
    assert f"timestamp={FIXED_TS}" in signed_part and "recvWindow=5000" in signed_part
    assert captured[0].headers["X-MBX-APIKEY"] == OFFICIAL_KEY
    assert captured[0].headers["Content-Type"] == "application/x-www-form-urlencoded"


def test_get_request_signs_the_query_string():
    captured: list[httpx.Request] = []
    ad = make_adapter(json_route({"/fapi/v3/positionRisk": []}, captured))
    run(ad.fetch_positions(["BTCUSDT"]))
    query = str(captured[0].url).split("?", 1)[1]
    signed_part, _, sig = query.rpartition("&signature=")
    assert sig == hmac.new(OFFICIAL_SECRET.encode(), signed_part.encode(),
                           hashlib.sha256).hexdigest()
    assert captured[0].content == b""          # GET 不带 body


# ---- 返佣码 -------------------------------------------------------------

def test_broker_code_empty_leaves_no_trace():
    """用户默认不填：请求里不能有任何返佣痕迹——没有 x- 前缀、没有多余字段/header。"""
    captured: list[httpx.Request] = []
    ad = make_adapter(json_route({"/fapi/v1/order": ORDER_OK}, captured), broker_code="")
    load_btc(ad)
    res = run(ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy", qty=Decimal("0.012"),
                                          price=Decimal("61234.5"), client_order_id="h1")))
    body = captured[0].content.decode()
    assert "x-" not in body and "x-" not in str(captured[0].url)
    assert "brokerId" not in body and "tag" not in body
    assert not any(h.lower().startswith(("x-referer", "x-broker", "referer"))
                   for h in captured[0].headers)
    # 但订单 ID 本身必须有——绝不允许无 ID 下单
    assert "newClientOrderId=h1" in body
    assert res.client_order_id


def test_broker_code_becomes_client_order_id_prefix():
    captured: list[httpx.Request] = []
    ad = make_adapter(json_route({"/fapi/v1/order": ORDER_OK}, captured), broker_code=BROKER)
    load_btc(ad)
    run(ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy", qty=Decimal("0.012"),
                                    price=Decimal("61234.5"), client_order_id="h1")))
    body = captured[0].content.decode()
    assert f"newClientOrderId={BROKER}h1" in body      # x- 前缀原样拼在最前，不做大小写转换
    # header 里不该出现返佣码，币安只走订单 ID 前缀这一条通道
    assert not any("cvBPrNm9" in v for v in captured[0].headers.values())


def test_broker_code_accepts_bare_link_id():
    """用户可能只填 Link ID，官方要求最终 ID 以 x- 开头，适配器补齐。"""
    ad = make_adapter(json_route({}), broker_code="cvBPrNm9")
    assert ad.make_client_order_id("h1") == "x-cvBPrNm9h1"


def test_broker_code_rejects_illegal_charset():
    for bad in ("x-abc def", "x-abc@1", "x-abc#1"):
        with pytest.raises(ValueError):
            make_adapter(json_route({}), broker_code=bad)


def test_client_order_id_truncates_tag_not_prefix():
    """预算不够时只能截自己的 tag——截掉前缀返佣归属就断了。"""
    ad = make_adapter(json_route({}), broker_code=BROKER)
    cid = ad._build_client_order_id("t" * 80)
    assert cid.startswith(BROKER) and len(cid) == 36

    papi = make_adapter(json_route({}), broker_code=BROKER, portfolio_margin=True)
    cid32 = papi._build_client_order_id("t" * 80)
    assert cid32.startswith(BROKER) and len(cid32) == 32


def test_cancel_and_query_use_the_same_prefixed_id_as_place():
    """下单加了返佣前缀，撤单/查单就必须加同一个前缀。

    两边不是同一个 ID 时，币安回 -2011/-2013「查无此单」→ OrderRejected →
    resolve_unknown_order 判定"从未被接受、成交 0"，上层照着 0 补仓 = 双倍仓位。
    """
    captured: list[httpx.Request] = []
    ad = make_adapter(json_route({"/fapi/v1/order": ORDER_OK}, captured),
                      broker_code=BROKER)
    load_btc(ad)
    run(ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy", qty=Decimal("0.012"),
                                    price=Decimal("61234.5"), client_order_id="h1")))
    run(ad.cancel_order("BTCUSDT", "h1"))
    run(ad.query_order("BTCUSDT", "h1"))
    sent = [f"newClientOrderId={BROKER}h1" in captured[0].content.decode(),
            f"origClientOrderId={BROKER}h1" in str(captured[1].url),
            f"origClientOrderId={BROKER}h1" in str(captured[2].url)]
    assert all(sent), sent
    # 幂等：已经带前缀的 ID 不能被二次拼接成第三种 ID
    run(ad.cancel_order("BTCUSDT", f"{BROKER}h1"))
    assert f"origClientOrderId={BROKER}h1" in str(captured[3].url)
    assert f"{BROKER}{BROKER}" not in str(captured[3].url)


def test_order_result_returns_callers_id_not_exchange_echo():
    """上层用自己发出去的 ID 对账，回传带前缀的版本会让账本对不上（其余五家都回原值）。"""
    ad = make_adapter(json_route({"/fapi/v1/order": {**ORDER_OK,
                                                     "clientOrderId": f"{BROKER}h1"}}),
                      broker_code=BROKER)
    load_btc(ad)
    res = run(ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy",
                                          qty=Decimal("0.012"), price=Decimal("61234.5"),
                                          client_order_id="h1")))
    assert res.client_order_id == "h1"
    assert run(ad.query_order("BTCUSDT", "h1")).client_order_id == "h1"


def test_broker_code_is_stripped():
    """从网页复制返佣码常带尾随换行，六家统一在构造期剥掉。"""
    ad = make_adapter(json_route({}), broker_code=f"  {BROKER}\n")
    assert ad.broker_code == BROKER
    assert ad.make_client_order_id("h1") == f"{BROKER}h1"


def test_generated_client_order_ids_are_unique():
    """同一毫秒内连发也不能撞 ID：同 ID 未成交订单会被直接拒。"""
    ad = make_adapter(json_route({}), broker_code=BROKER)
    ids = {ad.make_client_order_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(i.startswith(BROKER) and len(i) <= 36 for i in ids)


# ---- 资金费符号与周期 ---------------------------------------------------

FUNDING_INFO = [{"symbol": "ETHUSDT", "adjustedFundingRateCap": "0.03",
                 "adjustedFundingRateFloor": "-0.03", "fundingIntervalHours": 4}]


def test_funding_rate_sign_is_exchange_native():
    """正 = 多头付给空头。API 给什么符号就存什么，绝不取反。"""
    routes = {
        "/fapi/v1/fundingInfo": FUNDING_INFO,
        "/fapi/v1/premiumIndex": [
            {"symbol": "BTCUSDT", "lastFundingRate": "-0.00042",
             "nextFundingTime": 1785009600000, "time": 1785000000000},
            {"symbol": "ETHUSDT", "lastFundingRate": "0.00310",
             "nextFundingTime": 1785009600000, "time": 1785000000000},
        ],
    }
    ad = make_adapter(json_route(routes))
    rates = {r.symbol: r for r in run(ad.fetch_carry_rates(["BTCUSDT", "ETHUSDT"]))}
    assert rates["BTCUSDT"].rate == Decimal("-0.00042")   # 空头在付钱，保持负号
    assert rates["ETHUSDT"].rate == Decimal("0.00310")
    assert all(type(r.rate) is Decimal for r in rates.values())
    # 上层用 short_rate - long_rate 直接算净收益，符号错一处整个排序反向
    assert rates["ETHUSDT"].rate - rates["BTCUSDT"].rate > 0


def test_funding_interval_defaults_when_absent_from_funding_info():
    """fundingInfo 是稀疏返回：查不到 = 默认 8 小时，不是「不存在」。"""
    routes = {
        "/fapi/v1/fundingInfo": FUNDING_INFO,
        "/fapi/v1/premiumIndex": [
            {"symbol": "BTCUSDT", "lastFundingRate": "0.0001",
             "nextFundingTime": 1785009600000, "time": 1785000000000},
            {"symbol": "ETHUSDT", "lastFundingRate": "0.0001",
             "nextFundingTime": 1785009600000, "time": 1785000000000},
        ],
    }
    ad = make_adapter(json_route(routes))
    rates = {r.symbol: r for r in run(ad.fetch_carry_rates(["BTCUSDT", "ETHUSDT"]))}
    assert rates["BTCUSDT"].interval_s == 28800      # 不在 fundingInfo 里 → 默认
    assert rates["ETHUSDT"].interval_s == 4 * 3600   # 被调整过的 4 小时品种
    # 年化必须按各自周期归一化，4h 品种一天结算 6 次
    assert rates["ETHUSDT"].annualized() == rates["BTCUSDT"].annualized() * 2


def test_funding_limits_none_means_default_tier():
    ad = make_adapter(json_route({"/fapi/v1/fundingInfo": FUNDING_INFO}))
    assert run(ad.fetch_funding_limits("ETHUSDT")) == (Decimal("0.03"), Decimal("-0.03"))
    assert run(ad.fetch_funding_limits("BTCUSDT")) is None


def test_funding_history_ascending_and_paged():
    page1 = [{"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingTime": 1000},
             {"symbol": "BTCUSDT", "fundingRate": "-0.0002", "fundingTime": 2000}]
    page2 = [{"symbol": "BTCUSDT", "fundingRate": "0.0003", "fundingTime": 3000}]
    pages = [page1, page2]
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=pages.pop(0) if pages else [])

    ad = make_adapter(handler)
    ad._history_page_size = 2          # 真实上限 1000，调小以走到第二页
    rows = run(ad.fetch_funding_history("BTCUSDT", since_ms=0, limit=4))
    assert rows == ((1000, Decimal("0.0001")), (2000, Decimal("-0.0002")),
                    (3000, Decimal("0.0003")))
    # 第二页从上一批最后一条 fundingTime+1 往前滚，不是往回翻
    assert "startTime=2001" in str(captured[1].url)


def _funding_server(points, captured: list | None = None):
    """贴着币安真实行为的假 fundingRate 端点。

    关键是复刻它**与其余五家相反**的截断方向：窗口内超过 limit 时，
    返回的是从 startTime 起最旧的 limit 条，而不是最新的 limit 条。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        q = httpx.QueryParams(request.url.query.decode())
        if captured is not None:
            captured.append(q)
        start = int(q.get("startTime") or 0)
        limit = int(q.get("limit") or 100)
        rows = [{"symbol": "LPTUSDT", "fundingTime": ts, "fundingRate": str(r)}
                for ts, r in points if ts >= start][:limit]
        return httpx.Response(200, json=rows)
    return handler


def test_funding_history_reaches_the_end_of_the_window():
    """旧实现攒够 limit 条就停，拿到的是 since 之后**最旧**的 limit 条——
    LPTUSDT 请求 180 天会返回一段停在 13 天前的序列，而调用方无从得知，
    scoring 会把这段陈旧数据当成此刻的稳定性。

    币安服务端的截断方向就是这样（其余五家都保留最新端），适配器必须把它掰回来：
    一路向前滚到窗口末端，limit 只当页大小用。
    """
    pts = [(1000 * i, f"0.000{i}") for i in range(1, 6)]     # 5 条
    ad = make_adapter(_funding_server(pts))
    rows = run(ad.fetch_funding_history("LPTUSDT", since_ms=0, limit=2))

    assert rows[-1][0] == 5000, "序列必须一直覆盖到窗口末端，不能停在最旧那几条"
    assert [ts for ts, _ in rows] == [1000, 2000, 3000, 4000, 5000]
    assert rows[-1][1] == Decimal("0.0005")


def test_funding_history_since_zero_does_not_flip_the_semantics():
    """实测坑：startTime=0 被服务端当作"没传"，改走"返回最近记录"分支并把
    上限静默压到 500 条——同一个方法因参数值不同而语义反向。夹到 1 即可。"""
    captured: list[httpx.QueryParams] = []
    ad = make_adapter(_funding_server([(1000, "0.0001")], captured))
    run(ad.fetch_funding_history("LPTUSDT", since_ms=0, limit=10))
    assert int(captured[0]["startTime"]) >= 1


def test_funding_history_gateway_error_envelope_is_not_an_empty_series():
    """网关的错误信封是 HTTP 200 + {"status":"ERROR","code":"99099990",...}。
    code 不以负号开头，只按负号判错就会原样放行，历史接口那句
    `data if isinstance(data, list) else []` 把它变成空序列——
    "这个币没有历史"和"请求被拒了"变得无法区分。"""
    envelope = {"status": "ERROR", "type": "GENERAL", "code": "99099990",
                "errorData": "illegal params.", "data": None}
    ad = make_adapter(lambda req: httpx.Response(200, json=envelope))
    with pytest.raises(ExchangeError) as exc:
        run(ad.fetch_funding_history("LPTUSDT", since_ms=1, limit=10))
    assert "illegal params" in str(exc.value)


def test_gateway_error_envelope_never_becomes_a_fake_order_receipt():
    """同一条信封走下单路径更危险：'ERROR' 不在 _STATUS_MAP 里会被映射成
    'new'、executedQty 缺失记 0，凭空造出一张"已受理、零成交"的假回执。"""
    envelope = {"status": "ERROR", "type": "GENERAL", "code": "99099990",
                "errorData": "illegal params.", "data": None}
    ad = make_adapter(lambda req: httpx.Response(200, json=envelope))
    load_btc(ad)
    with pytest.raises((ExchangeError, OrderUnknown)):
        run(ad.place_order(OrderRequest("BTCUSDT", "buy", Decimal("0.01"),
                                        Decimal("50000"), client_order_id="cf-1")))


def test_interval_inference_catches_a_switch_to_a_faster_period():
    """8h 切 4h 时旧实现会判成 8h、年化低估一半——而 Bybit 那份调研写明
    8h→1h 的切换会让实收 carry 差 8 倍，正是最该抓住的一侧。

    间隔只会因为漏点而**变大**、绝不会变小，所以偏小的间隔是切换的铁证。
    """
    h = 3_600_000
    pts = [(8 * h * i, "0.0001") for i in range(1, 6)]        # 8h × 4 个间隔
    pts.append((8 * h * 5 + 4 * h, "0.0001"))                 # 刚切到 4h
    ad = make_adapter(_funding_server(pts))
    run(ad.fetch_funding_history("LPTUSDT", since_ms=1, limit=100))
    assert ad._funding_interval_s["LPTUSDT"] == 4 * 3600


def test_interval_inference_ignores_an_isolated_hole():
    """漏一条 8h 数据会造出 16h 或 24h 的间隔。24h 本身是合法周期，
    光靠白名单挡不住，得靠"重复出现的间隔才是真周期"。"""
    h = 3_600_000
    pts = [(8 * h, "0.0001"), (16 * h, "0.0001"), (40 * h, "0.0001"),   # 漏两条 → 24h
           (48 * h, "0.0001"), (56 * h, "0.0001")]
    ad = make_adapter(_funding_server(pts))
    run(ad.fetch_funding_history("LPTUSDT", since_ms=1, limit=100))
    assert ad._funding_interval_s["LPTUSDT"] == 8 * 3600


def test_fresh_funding_info_wins_over_the_history_guess():
    """fundingInfo 是权威来源且 TTL 只有 1 小时。反推值无条件覆写的话，
    它能在整个 TTL 窗口里压过权威值，进而污染 fetch_carry_rates 的年化。"""
    h = 3_600_000
    pts = [(4 * h * i, "0.0001") for i in range(1, 8)]        # 历史看起来是 4h
    ad = make_adapter(_funding_server(pts))
    ad._funding_interval_s["LPTUSDT"] = 8 * 3600              # fundingInfo 说 8h
    ad._funding_info_at = time.time()                         # 且刚刷新过
    run(ad.fetch_funding_history("LPTUSDT", since_ms=1, limit=100))
    assert ad._funding_interval_s["LPTUSDT"] == 8 * 3600


def test_json_numeric_literals_never_become_float():
    """币安绝大多数数值是字符串，但偶有裸数字；解析闸必须把它也变成 Decimal。"""
    routes = {
        "/fapi/v1/fundingInfo": [],
        "/fapi/v1/premiumIndex": [{"symbol": "BTCUSDT", "lastFundingRate": 0.0001,
                                   "nextFundingTime": 1785009600000, "time": 1}],
    }
    ad = make_adapter(json_route(routes))
    rate = run(ad.fetch_carry_rates(["BTCUSDT"]))[0].rate
    assert type(rate) is Decimal and rate == Decimal("0.0001")


# ---- 精度 ---------------------------------------------------------------

def test_quantity_and_price_are_floored_to_step():
    """数量向零取整到步长整数倍（floor 不是 round），且不能出现科学计数法。"""
    captured: list[httpx.Request] = []
    ad = make_adapter(json_route({"/fapi/v1/order": ORDER_OK}, captured))
    load_btc(ad)
    run(ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy",
                                    qty=Decimal("0.0123456"),
                                    price=Decimal("61234.567"),
                                    client_order_id="h1")))
    body = captured[0].content.decode()
    assert "quantity=0.012" in body and "quantity=0.0123" not in body
    assert "E-" not in body and "E%2B" not in body


def test_price_rounds_toward_marketability():
    """价格取整方向必须和 executor.cross_price 一致：买单向上、卖单向下。

    保护限价是故意穿透对手价的，反向取整会把穿透削回来——在 tick 相对价格很粗的
    小币上，那不是少赚一个 tick，而是 IOC 根本挂不上、这条腿永远补不平。
    """
    captured: list[httpx.Request] = []
    ad = make_adapter(json_route({"/fapi/v1/order": ORDER_OK}, captured))
    load_btc(ad)
    run(ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy",
                                    qty=Decimal("0.012"), price=Decimal("61234.567"),
                                    client_order_id="h1")))
    run(ad.place_order(OrderRequest(symbol="BTCUSDT", side="sell",
                                    qty=Decimal("0.012"), price=Decimal("61234.567"),
                                    client_order_id="h2")))
    assert "price=61234.6" in captured[0].content.decode()     # 买单向上贴 tick=0.1
    assert "price=61234.5" in captured[1].content.decode()     # 卖单向下


def test_tiny_quantity_pinched_to_zero_is_rejected_locally():
    """取整后为 0 的单不能发出去——发了必然被拒，还白吃一次下单配额。"""
    ad = make_adapter(json_route({"/fapi/v1/order": ORDER_OK}))
    load_btc(ad)
    with pytest.raises(OrderRejected):
        run(ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy",
                                        qty=Decimal("0.0004"), price=Decimal("61234.5"),
                                        client_order_id="h1")))


def test_market_and_reduce_only_order_shape():
    captured: list[httpx.Request] = []
    ad = make_adapter(json_route({"/fapi/v1/order": ORDER_OK}, captured))
    load_btc(ad)
    run(ad.place_order(OrderRequest(symbol="BTCUSDT", side="sell", qty=Decimal("0.012"),
                                    price=None, reduce_only=True, client_order_id="c1")))
    body = captured[0].content.decode()
    assert "type=MARKET" in body and "price=" not in body and "timeInForce" not in body
    assert "side=SELL" in body and "reduceOnly=true" in body
    # 减仓单不受 5 USDT 最小名义额限制，尾单可以很小；加仓单则不行
    assert "newClientOrderId=c1" in body


# ---- 未知状态 -----------------------------------------------------------

def test_timeout_raises_order_unknown():
    """超时绝不能当失败返回：当作失败会重发，变双倍仓位。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timeout", request=request)

    ad = make_adapter(handler, broker_code=BROKER)
    load_btc(ad)
    with pytest.raises(OrderUnknown) as exc:
        run(ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy", qty=Decimal("0.012"),
                                        price=Decimal("61234.5"), client_order_id="h1")))
    # 消解流程要靠这个 ID 去 cancel → query
    assert exc.value.client_order_id == f"{BROKER}h1"


def test_connect_error_raises_order_unknown():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset", request=request)

    ad = make_adapter(handler)
    load_btc(ad)
    with pytest.raises(OrderUnknown):
        run(ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy", qty=Decimal("0.012"),
                                        price=Decimal("61234.5"), client_order_id="h1")))


def test_http_5xx_and_timeout_code_are_unknown_not_failure():
    for entry in ((503, {"code": -1001, "msg": "internal error"}),
                  (200, {"code": -1007, "msg": "Timeout waiting for response"})):
        ad = make_adapter(json_route({"/fapi/v1/order": entry}))
        load_btc(ad)
        with pytest.raises(OrderUnknown):
            run(ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy",
                                            qty=Decimal("0.012"), price=Decimal("61234.5"),
                                            client_order_id="h1")))


def test_remote_protocol_error_raises_order_unknown():
    """服务端收下请求后半途断连是最典型的"可能已进撮合"。

    RemoteProtocolError 继承 ProtocolError→TransportError，和 NetworkError 是兄弟
    不是子类，只写 (TimeoutException, NetworkError) 会让它裸奔漏出去。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("server disconnected", request=request)

    ad = make_adapter(handler)
    load_btc(ad)
    with pytest.raises(OrderUnknown):
        run(ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy", qty=Decimal("0.012"),
                                        price=Decimal("61234.5"), client_order_id="h1")))


def test_unreadable_200_body_is_unknown_not_a_fake_receipt():
    """200 但响应体读不出来（WAF 注入 HTML）：绝不能造出一张"已受理零成交"的假回执。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>blocked</html>")

    ad = make_adapter(handler)
    load_btc(ad)
    with pytest.raises(OrderUnknown):
        run(ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy", qty=Decimal("0.012"),
                                        price=Decimal("61234.5"), client_order_id="h1")))


def test_execution_status_unknown_code_is_order_unknown():
    """-1006 UNEXPECTED_RESP 官方文案就是 "Execution status unknown"，与 -1007 同语义。"""
    ad = make_adapter(json_route({"/fapi/v1/order": {"code": -1006,
                                                     "msg": "Execution status unknown."}}))
    load_btc(ad)
    with pytest.raises(OrderUnknown):
        run(ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy", qty=Decimal("0.012"),
                                        price=Decimal("61234.5"), client_order_id="h1")))


def test_risk_path_5xx_is_catchable_by_the_resolver():
    """base.resolve_unknown_order 只认 (OrderRejected, httpx.HTTPError, RateLimited)。

    撤单/查单遇 5xx 抛裸 ExchangeError 会让消解流程在最危险的时刻崩掉。
    """
    ad = make_adapter(json_route({"/fapi/v1/order": (503, {"msg": "service unavailable"})}))
    for coro in (ad.cancel_order("BTCUSDT", "h1"), ad.query_order("BTCUSDT", "h1")):
        with pytest.raises(RateLimited) as exc:
            run(coro)
        assert exc.value.retriable and exc.value.code == "HTTP_503"


def test_network_error_on_cancel_stays_httpx_error():
    """resolve_unknown_order 的重试循环捕获的是 httpx.HTTPError，
    撤单路径上的网络异常不能被吞成别的类型，否则消解流程直接崩。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timeout", request=request)

    ad = make_adapter(handler)
    with pytest.raises(httpx.HTTPError):
        run(ad.cancel_order("BTCUSDT", "x-cvBPrNm9h1"))


# ---- 错误码翻译 ---------------------------------------------------------

def test_error_code_translation():
    cases = [
        ({"code": -1111, "msg": "Precision is over the maximum"}, OrderRejected),
        ({"code": -4164, "msg": "Order's notional must be no smaller than 5.0"}, OrderRejected),
        ({"code": -2019, "msg": "Margin is insufficient"}, OrderRejected),
        ({"code": -1003, "msg": "Too many requests"}, RateLimited),
        ({"code": -1015, "msg": "Too many new orders"}, RateLimited),
        ({"code": -1022, "msg": "Signature for this request is not valid"}, ExchangeError),
    ]
    for payload, expected in cases:
        ad = make_adapter(json_route({"/fapi/v1/order": payload}))
        load_btc(ad)
        with pytest.raises(expected):
            run(ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy",
                                            qty=Decimal("0.012"), price=Decimal("61234.5"),
                                            client_order_id="h1")))


def test_rate_limit_status_codes():
    ad = make_adapter(json_route({"/fapi/v1/time": (429, {"code": -1003, "msg": "banned"})}))
    with pytest.raises(RateLimited) as exc:
        run(ad.fetch_server_time_ms())
    assert exc.value.retriable

    ad2 = make_adapter(json_route({"/fapi/v1/time": (418, {"code": -1003, "msg": "banned"})}))
    with pytest.raises(RateLimited) as exc2:
        run(ad2.fetch_server_time_ms())
    assert not exc2.value.retriable      # IP 已封，立刻重试只会延长封禁


def test_unknown_order_on_query_is_rejected_so_resolution_returns_zero():
    """交易所不认识这个订单 = 它从未被接受，消解流程据此判成交量为 0。"""
    ad = make_adapter(json_route({"/fapi/v1/order": {"code": -2013,
                                                     "msg": "Order does not exist."}}))
    with pytest.raises(OrderRejected):
        run(ad.query_order("BTCUSDT", "x-cvBPrNm9h1"))


# ---- 元数据与行情解析 ---------------------------------------------------

EXCHANGE_INFO = {
    "serverTime": 1785000000000,
    "symbols": [
        {"symbol": "BTCUSDT", "pair": "BTCUSDT", "contractType": "PERPETUAL",
         "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT",
         "pricePrecision": 2, "quantityPrecision": 3, "filters": [
             {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
             {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
             {"filterType": "MIN_NOTIONAL", "notional": "5"}]},
        {"symbol": "1000PEPEUSDT", "pair": "1000PEPEUSDT", "contractType": "PERPETUAL",
         "status": "TRADING", "baseAsset": "1000PEPE", "quoteAsset": "USDT",
         "pricePrecision": 7, "quantityPrecision": 0, "filters": [
             {"filterType": "PRICE_FILTER", "tickSize": "0.0000001"},
             {"filterType": "LOT_SIZE", "stepSize": "1"},
             {"filterType": "MIN_NOTIONAL", "notional": "5"}]},
        {"symbol": "BTCUSDT_250926", "pair": "BTCUSDT", "contractType": "CURRENT_QUARTER",
         "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT",
         "pricePrecision": 1, "quantityPrecision": 3, "filters": []},
        {"symbol": "SOLUSDT", "contractType": "PERPETUAL", "status": "PENDING_TRADING",
         "baseAsset": "SOL", "quoteAsset": "USDT", "filters": []},
    ],
}


def test_instruments_filters_and_multiplier():
    ad = make_adapter(json_route({"/fapi/v1/exchangeInfo": EXCHANGE_INFO,
                                  "/fapi/v1/fundingInfo": FUNDING_INFO}))
    insts = {i.symbol: i for i in run(ad.fetch_instruments())}
    # 交割合约和未上线合约必须被 contractType / status 挡掉
    assert set(insts) == {"BTCUSDT", "1000PEPEUSDT"}

    btc = insts["BTCUSDT"]
    assert btc.tick_size == Decimal("0.10") and btc.lot_size == Decimal("0.001")
    assert btc.min_notional == Decimal("5") and btc.funding_interval_s == 28800
    assert all(type(x) is Decimal for x in (btc.tick_size, btc.lot_size, btc.min_notional))

    # 千倍前缀：一张 = 1000 个 PEPE，base 归一化后才能和别家的 PEPE 腿配对。
    # 归一化了 base 就必须把两个轴一起换算：数量粒度 ×1000（PEPE 口径），
    # 价格刻度 ÷1000（每个 PEPE 的价），否则 mark × qty 的名义额差 1000 倍。
    pepe = insts["1000PEPEUSDT"]
    assert pepe.base == "PEPE" and pepe.contract_size == Decimal("1000")
    assert pepe.lot_size == Decimal("1000")               # stepSize 1 张 = 1000 PEPE
    assert pepe.tick_size == Decimal("0.0000001") / 1000  # tick 是"每 1000 PEPE"的


PEPE_ORDER_OK = {
    "orderId": 9, "symbol": "1000PEPEUSDT", "status": "FILLED",
    "clientOrderId": "p1", "avgPrice": "0.0076500", "executedQty": "5000",
}


def _load_pepe(ad: BinanceAdapter) -> None:
    ad._instruments["1000PEPEUSDT"] = Instrument(
        market="binance:perp", symbol="1000PEPEUSDT", base="PEPE", quote="USDT",
        tick_size=Decimal("0.0000001") / 1000, lot_size=Decimal("1000"),
        min_notional=Decimal("5"), contract_size=Decimal("1000"),
    )
    ad._precision["1000PEPEUSDT"] = (7, 0)


def test_thousand_multiplier_converts_both_axes_on_the_wire():
    """base 归一化成 PEPE 后，对外全部是 PEPE 口径，发出去的必须是交易所的"张"口径。

    这是本轮最贵的一条：引擎按 base 币算出 5,000,000 PEPE 的缺口，不换算就会被
    币安读成 5,000,000 张「1000PEPE」= 50 亿 PEPE，实盘 1000 倍超额建仓。
    """
    captured: list[httpx.Request] = []
    ad = make_adapter(json_route({"/fapi/v1/order": PEPE_ORDER_OK}, captured))
    _load_pepe(ad)
    res = run(ad.place_order(OrderRequest(
        symbol="1000PEPEUSDT", side="buy", qty=Decimal("5000000"),
        price=Decimal("0.00000765"), client_order_id="p1")))
    body = captured[0].content.decode()
    assert "quantity=5000" in body                 # 5,000,000 PEPE ÷ 1000 = 5000 张
    assert "price=0.00765" in body                 # 每 PEPE 的价 × 1000 = 每张的价
    assert "E-" not in body
    # 回报反向换算：5000 张 = 5,000,000 PEPE，均价折回每个 PEPE
    assert res.filled_qty == Decimal("5000000")
    assert res.avg_price == Decimal("0.00000765")


def test_thousand_multiplier_converts_book_and_positions():
    ad = make_adapter(json_route({
        "/fapi/v1/ticker/bookTicker": {"symbol": "1000PEPEUSDT", "bidPrice": "0.0076400",
                                       "bidQty": "3000", "askPrice": "0.0076500",
                                       "askQty": "1000", "time": 1785000000123},
        "/fapi/v3/positionRisk": [{"symbol": "1000PEPEUSDT", "positionAmt": "-5000",
                                   "entryPrice": "0.0076", "markPrice": "0.00765",
                                   "liquidationPrice": "0.0100", "leverage": "5"}],
    }))
    _load_pepe(ad)
    top = run(ad.fetch_book_top("1000PEPEUSDT"))
    assert top.bid_qty == Decimal("3000000") and top.ask_qty == Decimal("1000000")
    assert top.ask_price == Decimal("0.00000765")
    pos = run(ad.fetch_positions(["1000PEPEUSDT"]))[0]
    assert pos.qty == Decimal("-5000000")          # 5000 张空头 = 500 万 PEPE
    assert pos.mark_price == Decimal("0.00000765")
    assert pos.liquidation_price == Decimal("0.00001")
    # mark × qty 的名义额必须和交易所口径一致（5000 张 × 0.00765 = 38.25 USDT）
    assert abs(pos.qty) * pos.mark_price == Decimal("38.25")


def test_book_top_parse():
    ad = make_adapter(json_route({"/fapi/v1/ticker/bookTicker": {
        "symbol": "BTCUSDT", "bidPrice": "61234.40", "bidQty": "3.500",
        "askPrice": "61234.50", "askQty": "1.200", "time": 1785000000123}}))
    top = run(ad.fetch_book_top("BTCUSDT"))
    assert top.bid_price == Decimal("61234.40") and top.ask_qty == Decimal("1.200")
    assert top.ts_ms == 1785000000123
    assert type(top.bid_price) is Decimal


def test_book_top_accepts_ws_payload_shape():
    """WS bookTicker 用 b/B/a/A，REST 用长名，同一个解析器都要认。"""
    top = BinanceAdapter._book_top({"e": "bookTicker", "s": "BTCUSDT", "b": "61234.40",
                                    "B": "3.5", "a": "61234.50", "A": "1.2",
                                    "T": 1785000000123, "E": 1785000000125})
    assert top.bid_price == Decimal("61234.40") and top.ask_price == Decimal("61234.50")
    assert top.ts_ms == 1785000000123      # 用撮合时间 T 而不是事件时间 E


# ---- 盘口深度 -----------------------------------------------------------

# 中价刚好 100.00（99.99 / 100.01），10bp 带宽 = [99.90, 100.10]，边界值故意压在
# 档位上，用来锁死"含边界"这个语义。最后一档远在带宽外且量特别大，
# 一旦被误计入，断言的数字会离谱到一眼看出来。
DEPTH_BTC = {
    "lastUpdateId": 1027024, "E": 1785000000125, "T": 1785000000123,
    "bids": [["99.99", "2"], ["99.95", "4"], ["99.90", "10"], ["99.89", "1000"]],
    "asks": [["100.01", "3"], ["100.05", "5"], ["100.10", "2"], ["100.11", "5000"]],
}


def test_book_depth_accumulates_notional_inside_the_band():
    """带宽内累计名义额（手算）：这正是"一档当深度代理"低估一两个数量级的地方。

    一档只有 199.98 USDT，10bp 内却有 1598.78 —— 差着一个数量级，
    容量估算直接决定这个机会是"做不了"还是"能放 X 万"。
    """
    captured: list[httpx.Request] = []
    ad = make_adapter(json_route({"/fapi/v1/depth": DEPTH_BTC}, captured))
    d = run(ad.fetch_book_depth("BTCUSDT", band_bp=10.0))

    # 99.99*2 + 99.95*4 + 99.90*10（99.90 是带宽下沿，含边界）
    assert d.bid_notional == Decimal("1598.78")
    # 100.01*3 + 100.05*5 + 100.10*2（100.10 是上沿，含边界）
    assert d.ask_notional == Decimal("1000.48")
    assert d.levels_used == 6                      # 两侧合计
    assert d.bid_price == Decimal("99.99") and d.bid_qty == Decimal("2")
    assert d.ask_price == Decimal("100.01") and d.ask_qty == Decimal("3")
    assert d.band_bp == 10.0 and d.ts_ms == 1785000000123      # 用撮合时间 T
    assert all(type(x) is Decimal for x in (d.bid_notional, d.ask_notional,
                                            d.bid_price, d.ask_qty))
    # 一档的名义额仍然要能单独拿到（scoring 的半价差项只吃一档）
    assert d.to_book_top().bid_price == Decimal("99.99")

    # 公开端点：不签名、走 GET、档位数取默认 50（权重 2，与 5 档同价）
    url = str(captured[0].url)
    assert captured[0].url.path == "/fapi/v1/depth"
    assert "symbol=BTCUSDT" in url and "limit=50" in url
    assert "signature=" not in url and captured[0].content == b""


def test_book_depth_excludes_levels_outside_the_band():
    """带宽外的档位一分都不能计入——那 1000/5000 的大单就在带宽外一个 tick。"""
    ad = make_adapter(json_route({"/fapi/v1/depth": DEPTH_BTC}))
    wide = run(ad.fetch_book_depth("BTCUSDT", band_bp=10.0))
    tight = run(ad.fetch_book_depth("BTCUSDT", band_bp=1.0))

    # 1bp 带宽 = [99.99, 100.01]，两侧各只剩一档
    assert tight.bid_notional == Decimal("199.98")     # 99.99 * 2
    assert tight.ask_notional == Decimal("300.03")     # 100.01 * 3
    assert tight.levels_used == 2
    # 带宽收窄只能让深度变小；且无论哪个带宽，99.89/100.11 那两档都不该进来
    assert tight.bid_notional < wide.bid_notional
    assert wide.bid_notional < Decimal("99.89") * Decimal("1000")


# 1000PEPEUSDT：交易所口径下一"张" = 1000 个 PEPE，价是"每 1000 PEPE"的价
DEPTH_PEPE = {
    "T": 1785000000123, "E": 1785000000125,
    "bids": [["0.0076500", "3000"], ["0.0076400", "2000"]],
    "asks": [["0.0076600", "1000"], ["0.0076700", "4000"]],
}


def test_book_depth_contract_size_converts_qty_not_notional():
    """面值换算：数量对外是 base 币，名义额是 quote —— 名义额**不该**被面值放大。

    价 ÷ 1000、量 × 1000 相乘正好抵消。把张数当币数（量 ×1000 却忘了价 ÷1000）
    会把这个机会的容量吹大 1000 倍，正是要修的反向错误。
    """
    ad = make_adapter(json_route({"/fapi/v1/depth": DEPTH_PEPE}))
    _load_pepe(ad)
    d = run(ad.fetch_book_depth("1000PEPEUSDT", band_bp=100.0))

    # 0.00765*3000 + 0.00764*2000 = 22.95 + 15.28（张口径与币口径同一个数）
    assert d.bid_notional == Decimal("38.23")
    # 0.00766*1000 + 0.00767*4000 = 7.66 + 30.68
    assert d.ask_notional == Decimal("38.34")
    assert d.levels_used == 4
    # 一档：价折回"每个 PEPE"，量放大成 PEPE 个数
    assert d.bid_price == Decimal("0.00000765") and d.bid_qty == Decimal("3000000")
    assert d.ask_price == Decimal("0.00000766") and d.ask_qty == Decimal("1000000")
    # 自洽性：一档的价×量必须等于该档的名义额，两个轴不能只换算一个
    assert d.bid_price * d.bid_qty == Decimal("22.95")


DEPTH_THIN = {
    "T": 1785000000123,
    "bids": [["99.99", "1"], ["99.98", "1"]],
    "asks": [["100.01", "1"], ["100.02", "1"]],
}


def test_book_depth_reports_levels_used_when_book_is_too_shallow():
    """返回的档位全在带宽内 = 带宽没被铺满，只能如实少报，绝不外推补足。

    50bp 带宽是 [99.50, 100.50]，而拿到的 4 档只铺到 ±2bp。真实簿在更远处
    有没有量、有多少，这个响应里根本没有——按均匀簿外推补足是编数据，
    宁可低估（levels_used == 返回总档数就是给上层的"覆盖不全"信号）。
    """
    ad = make_adapter(json_route({"/fapi/v1/depth": DEPTH_THIN}))
    ad._depth_limit = 5          # 真实默认 50，调小以模拟"就这么几档"
    d = run(ad.fetch_book_depth("BTCUSDT", band_bp=50.0))

    assert d.levels_used == 4                       # 等于响应里的总档数
    assert d.bid_notional == Decimal("199.97")      # 99.99 + 99.98，一分不多
    assert d.ask_notional == Decimal("200.03")      # 100.01 + 100.02


def test_book_depth_empty_book_reports_zero_levels():
    """空盘口没有中价，就没有带宽可言：如实报 0 档，不猜。"""
    ad = make_adapter(json_route({"/fapi/v1/depth": {"T": 1, "bids": [], "asks": []}}))
    d = run(ad.fetch_book_depth("BTCUSDT"))
    assert d.levels_used == 0
    assert d.bid_notional == Decimal("0") and d.ask_notional == Decimal("0")


def test_book_depth_missing_fields_raise_instead_of_reporting_zero():
    """2xx 但没有 bids/asks 必须抛错。

    退化成"深度 0"最危险：scoring 里 depth_notional <= 0 会让整段容量上限判断
    被跳过，等于把"没数据"读成"深度无限"，直接按用户填的仓位放行。
    """
    ad = make_adapter(json_route({"/fapi/v1/depth": {"lastUpdateId": 1}}))
    with pytest.raises(ExchangeError) as exc:
        run(ad.fetch_book_depth("BTCUSDT"))
    assert exc.value.code == "BAD_DEPTH"


def test_depth_limit_snaps_to_a_legal_value():
    """档位数是离散集合，传别的会被币安直接拒（-1130）。向上取到最近的合法值。"""
    snap = BinanceAdapter._snap_depth_limit
    assert [snap(n) for n in (1, 5, 21, 50, 60, 200, 900, 5000)] == \
        [5, 5, 50, 50, 100, 500, 1000, 1000]


def test_book_depth_rejects_non_positive_band():
    ad = make_adapter(json_route({"/fapi/v1/depth": DEPTH_BTC}))
    with pytest.raises(ValueError):
        run(ad.fetch_book_depth("BTCUSDT", band_bp=0.0))


def test_positions_parse_sign_and_liquidation():
    ad = make_adapter(json_route({"/fapi/v3/positionRisk": [
        {"symbol": "BTCUSDT", "positionAmt": "-0.500", "entryPrice": "61000.0",
         "markPrice": "61200.0", "liquidationPrice": "70000.0", "leverage": "5"},
        {"symbol": "ETHUSDT", "positionAmt": "2.000", "entryPrice": "3000.0",
         "markPrice": "3010.0", "liquidationPrice": "0", "leverage": "3"},
    ]}))
    pos = {p.symbol: p for p in run(ad.fetch_positions())}
    assert pos["BTCUSDT"].qty == Decimal("-0.500")        # 空头为负
    assert pos["BTCUSDT"].liquidation_price == Decimal("70000.0")
    assert pos["ETHUSDT"].liquidation_price is None       # 0 = 没有强平价
    assert type(pos["ETHUSDT"].qty) is Decimal


def test_open_orders_and_status_map():
    ad = make_adapter(json_route({"/fapi/v1/openOrders": [
        {"orderId": 1, "clientOrderId": "x-cvBPrNm9a", "status": "PARTIALLY_FILLED",
         "executedQty": "0.004", "avgPrice": "61234.50"}]}))
    orders = run(ad.fetch_open_orders("BTCUSDT"))
    assert orders[0].status == "partial" and not orders[0].is_terminal
    assert orders[0].filled_qty == Decimal("0.004")


def test_leverage_tiers_sorted_ascending():
    ad = make_adapter(json_route({"/fapi/v1/leverageBracket": [
        {"symbol": "BTCUSDT", "brackets": [
            {"bracket": 2, "initialLeverage": 50, "notionalCap": 500000},
            {"bracket": 1, "initialLeverage": 125, "notionalCap": 50000}]}]}))
    tiers = run(ad.fetch_leverage_tiers("BTCUSDT"))
    assert tiers == ((Decimal("50000"), Decimal("125")),
                     (Decimal("500000"), Decimal("50")))


def test_set_leverage_truncates_and_posts_body():
    captured: list[httpx.Request] = []
    ad = make_adapter(json_route({"/fapi/v1/leverage": {"leverage": 3, "symbol": "BTCUSDT"}},
                                 captured))
    run(ad.set_leverage("BTCUSDT", Decimal("3.7")))
    body = captured[0].content.decode()
    assert "leverage=3" in body and captured[0].method == "POST"


def test_fee_schedule_uses_real_rates_and_caches():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/fapi/v1/feeBurn":
            return httpx.Response(200, json={"feeBurn": False})
        return httpx.Response(200, json={"symbol": "BTCUSDT",
                                         "makerCommissionRate": "0.000180",
                                         "takerCommissionRate": "0.000400"})

    ad = make_adapter(handler)
    fees = run(ad.fetch_fee_schedule(["BTCUSDT"]))
    assert fees[0].taker == Decimal("0.000400") and fees[0].maker == Decimal("0.000180")
    assert fees[0].symbol == "BTCUSDT"          # 按 symbol 匹配，不按下标
    run(ad.fetch_fee_schedule(["BTCUSDT"]))
    # 权重 20，必须缓存：两次调用只允许打一次 commissionRate。
    # feeBurn 也只查一次（账户设置，一次运行里不会变）
    rate_calls = [c for c in calls if c.url.path == "/fapi/v1/commissionRate"]
    burn_calls = [c for c in calls if c.url.path == "/fapi/v1/feeBurn"]
    assert len(rate_calls) == 1 and len(burn_calls) == 1


# ---- papi / 未实现市场 ---------------------------------------------------

def test_portfolio_margin_switches_paths():
    captured: list[httpx.Request] = []
    ad = make_adapter(json_route({"/papi/v1/um/order": ORDER_OK}, captured),
                      portfolio_margin=True)
    load_btc(ad)
    run(ad.place_order(OrderRequest(symbol="BTCUSDT", side="buy", qty=Decimal("0.012"),
                                    price=Decimal("61234.5"), client_order_id="h1")))
    assert captured[0].url.host == "papi.binance.com"
    assert captured[0].url.path == "/papi/v1/um/order"


# ---- WebSocket -----------------------------------------------------------

def test_ws_url_uses_public_route_and_combined_stream():
    """2026-03 分流改造后 bookTicker 只在 /public，老的 /ws/ 直连地址已下线。"""
    url = BinanceAdapter._ws_stream_url(["BTCUSDT", "ETHUSDT"])
    assert url == ("wss://fstream.binance.com/public/stream?"
                   "streams=btcusdt@bookTicker/ethusdt@bookTicker")


def test_ws_refuses_to_silently_truncate_subscriptions():
    with pytest.raises(ValueError):
        BinanceAdapter._ws_stream_url([f"S{i}USDT" for i in range(1025)])
    assert hasattr(make_adapter(json_route({})).stream_book_top(["BTCUSDT"]), "__anext__")


def test_spot_and_margin_raise_not_implemented():
    for kind in (MarketKind.SPOT, MarketKind.MARGIN):
        with pytest.raises(NotImplementedError):
            make_adapter(json_route({}), market_kind=kind)


# ---- userTrades（操作复盘用的成交历史）------------------------------------

def _trade_row(tid: int, ts: int, side: str = "BUY", *, price: str = "10",
               qty: str = "2", pnl: str = "0", maker: bool = False) -> dict:
    return {
        "id": tid, "orderId": 900 + tid, "symbol": "OPUSDT", "side": side,
        "price": price, "qty": qty,
        "quoteQty": str(Decimal(price) * Decimal(qty)),
        "commission": "0.01", "commissionAsset": "USDT",
        "time": ts, "maker": maker, "buyer": side == "BUY",
        "realizedPnl": pnl,
    }


def test_fetch_my_trades_parses_fields_and_sorts_ascending():
    rows = [_trade_row(2, 2000, "SELL", pnl="1.5", maker=True),
            _trade_row(1, 1000)]
    ad = make_adapter(json_route({"/fapi/v1/userTrades": rows}))
    fills = run(ad.fetch_my_trades("OPUSDT", 0, 5000))

    assert [f.ts_ms for f in fills] == [1000, 2000]
    buy, sell = fills
    assert buy.side == "buy" and sell.side == "sell"
    assert buy.price == Decimal("10") and buy.qty == Decimal("2")
    assert buy.quote_qty == Decimal("20")
    assert buy.fee == Decimal("0.01") and buy.fee_asset == "USDT"
    assert sell.realized_pnl == Decimal("1.5")
    assert sell.maker is True and buy.maker is False
    assert sell.order_id == "902"
    assert buy.market == "binance:perp"


def test_fetch_my_trades_paginates_with_from_id_after_full_page():
    """撑满页说明后面可能还有：续页必须换 fromId 且不带时间参数
    （官方规定 fromId 不能与 startTime/endTime 同传）。"""
    all_rows = [_trade_row(i, 1000 + i) for i in range(1, 6)]     # id 1..5
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        params = request.url.params
        if "fromId" in params:
            assert "startTime" not in params and "endTime" not in params
            start = int(params["fromId"])
            page = [r for r in all_rows if r["id"] >= start][:2]
        else:
            page = all_rows[:2]
        return httpx.Response(200, content=json.dumps(page),
                              headers={"Content-Type": "application/json"})

    ad = make_adapter(handler)
    ad._trades_page_size = 2
    fills = run(ad.fetch_my_trades("OPUSDT", 0, 100_000))

    assert len(fills) == 5                                # 去重后全量
    assert [f.ts_ms for f in fills] == [1001, 1002, 1003, 1004, 1005]
    # 第一页按时间，后面按 fromId 翻
    assert "startTime" in captured[0].url.params
    assert [int(r.url.params["fromId"]) for r in captured[1:]] == [3, 5]


def test_fetch_my_trades_chunks_window_to_seven_days():
    """startTime/endTime 跨度超 7 天服务端直接报错（-1127），必须切窗。"""
    from earnfarm.exchanges.binance import USER_TRADES_WINDOW_MS

    captured: list[httpx.Request] = []
    ad = make_adapter(json_route({"/fapi/v1/userTrades": []}, captured))
    run(ad.fetch_my_trades("OPUSDT", 0, USER_TRADES_WINDOW_MS + 5))

    spans = [(int(r.url.params["startTime"]), int(r.url.params["endTime"]))
             for r in captured]
    assert len(spans) == 2
    for start, end in spans:
        assert end - start < USER_TRADES_WINDOW_MS
    # 两窗必须无缝衔接，漏一毫秒就可能漏一笔成交
    assert spans[1][0] == spans[0][1] + 1
    assert spans[1][1] == USER_TRADES_WINDOW_MS + 5


def test_fetch_my_trades_from_id_page_stops_at_window_end():
    """fromId 翻页会越进下一窗的时段，越界的行必须丢弃并停止本窗翻页，
    否则同一笔成交会在下一窗再进一次（去重兜底）且多烧一页配额。"""
    from earnfarm.exchanges.binance import USER_TRADES_WINDOW_MS

    w = USER_TRADES_WINDOW_MS
    in_window = [_trade_row(1, 100), _trade_row(2, 200)]
    beyond = [_trade_row(3, w + 100), _trade_row(4, w + 200)]

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if "fromId" in params:
            start = int(params["fromId"])       # 续页：从该 id 起（会越进下一窗）
            page = [r for r in in_window + beyond if r["id"] >= start][:2]
        elif int(params["startTime"]) == 0:
            page = in_window                    # 第一窗撑满页
        else:
            page = beyond                       # 第二窗按时间正常返回
        return httpx.Response(200, content=json.dumps(page),
                              headers={"Content-Type": "application/json"})

    ad = make_adapter(handler)
    ad._trades_page_size = 2
    fills = run(ad.fetch_my_trades("OPUSDT", 0, w + 300))

    # 4 笔各出现一次：越界行在第一窗被丢弃，第二窗按时间正常拿到
    assert len(fills) == 4
    assert [f.ts_ms for f in fills] == [100, 200, w + 100, w + 200]


def test_fetch_my_trades_uses_papi_path_under_portfolio_margin():
    captured: list[httpx.Request] = []
    ad = make_adapter(json_route({"/papi/v1/um/userTrades": []}, captured),
                      portfolio_margin=True)
    run(ad.fetch_my_trades("OPUSDT", 0, 1000))
    assert captured[0].url.host == "papi.binance.com"
    assert captured[0].url.path == "/papi/v1/um/userTrades"
