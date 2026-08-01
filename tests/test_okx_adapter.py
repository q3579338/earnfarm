"""OKX 适配器测试。全部走 httpx.MockTransport，一个字节都不会发到交易所。

金标准取自 docs/research/exchange-okx.md 里 2026-08-01 的实测响应（逐字抄），
签名用例的时间戳取 OKX 官方文档示例的 "2020-12-08T09:08:57.715Z"。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
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
from carryfarm.exchanges.okx import OkxAdapter
from carryfarm.models import MarketKind

API_KEY = "carryfarm-key"
API_SECRET = "6a3f1c9d2b7e4a5081c3d9f2a7b6e5c4"
PASSPHRASE = "carryfarm-pass"
CRED = Credential(api_key=API_KEY, api_secret=API_SECRET, passphrase=PASSPHRASE)

# OKX 官方签名示例用的时间戳，换算成 epoch ms 后钉死本地时钟
PIN_MS = 1_607_418_537_715
PIN_ISO = "2020-12-08T09:08:57.715Z"

# ---- 调研文档实测数据（逐字抄） -------------------------------------------

BTC_SWAP = {
    "instId": "BTC-USDT-SWAP", "instType": "SWAP", "instFamily": "BTC-USDT",
    "uly": "BTC-USDT", "ctType": "linear", "ctVal": "0.01", "ctValCcy": "BTC",
    "ctMult": "1", "settleCcy": "USDT", "tickSz": "0.1", "lotSz": "0.01",
    "minSz": "0.01", "maxLmtSz": "100000000", "maxMktSz": "35000",
    "maxLmtAmt": "20000000", "lever": "100", "state": "live",
    "listTime": "1573557408000", "instIdCode": 10459, "ruleType": "normal",
}
# PEPE 的 ctVal/lotSz/tickSz 来自文档实测，其余字段（state/ctType/…）是为了
# 让解析跑通补的常规值
PEPE_SWAP = {
    "instId": "PEPE-USDT-SWAP", "instType": "SWAP", "instFamily": "PEPE-USDT",
    "uly": "PEPE-USDT", "ctType": "linear", "ctVal": "10000000",
    "ctValCcy": "PEPE", "ctMult": "1", "settleCcy": "USDT",
    "tickSz": "0.000000001", "lotSz": "0.1", "minSz": "0.1",
    "maxMktSz": "16000", "lever": "50", "state": "live", "ruleType": "normal",
}
# 反向合约：PERP 适配器必须把它过滤掉（ctVal 是 USD，单位语义不同）
BTC_INVERSE = {
    "instId": "BTC-USD-SWAP", "instType": "SWAP", "instFamily": "BTC-USD",
    "uly": "BTC-USD", "ctType": "inverse", "ctVal": "100", "ctValCcy": "USD",
    "ctMult": "1", "settleCcy": "BTC", "tickSz": "0.1", "lotSz": "0.1",
    "minSz": "0.1", "lever": "100", "state": "live", "ruleType": "normal",
}

# 文档 2026-08-01 verbatim 的 /public/funding-rate 响应
FUNDING_BTC = {
    "formulaType": "withRate", "fundingRate": "0.0000616430459607",
    "fundingTime": "1785571200000", "impactValue": "20000.0000000000000000",
    "instId": "BTC-USDT-SWAP", "instType": "SWAP",
    "interestRate": "0.0001000000000000", "maxFundingRate": "0.00375",
    "method": "current_period", "minFundingRate": "-0.00375",
    "nextFundingRate": "", "nextFundingTime": "1785600000000",
    "premium": "-0.0004195044127414", "prevFundingTime": "1785542400000",
    "settFundingRate": "0.0000539822210069", "settState": "settled",
    "ts": "1785548167559",
}
# 文档实测：TRB 是 4h 周期，用来验证"周期必须现算"
FUNDING_TRB = {
    "instId": "TRB-USDT-SWAP", "instType": "SWAP", "fundingRate": "-0.0031",
    "fundingTime": "1785556800000", "nextFundingTime": "1785571200000",
    "maxFundingRate": "0.01", "minFundingRate": "-0.01",
    "method": "current_period", "settState": "settled", "ts": "1785548167559",
}


# ---- 测试脚手架 -----------------------------------------------------------

def ok(data) -> httpx.Response:
    return httpx.Response(200, json={"code": "0", "msg": "", "data": data})


def err(code: str, msg: str, data=None, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"code": code, "msg": msg, "data": data or []})


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def last(self, path: str) -> httpx.Request:
        for req in reversed(self.requests):
            if req.url.path == path:
                return req
        raise AssertionError(f"没有发往 {path} 的请求")


DEFAULT_ROUTES = {
    "/api/v5/public/time": lambda r: ok([{"ts": "1785548167559"}]),
    "/api/v5/public/instruments": lambda r: ok([BTC_SWAP, PEPE_SWAP, BTC_INVERSE]),
}


def build(routes: dict | None = None, *, broker_code: str = "",
          kind: MarketKind = MarketKind.PERP, pin: bool = True):
    rec = Recorder()
    table = {**DEFAULT_ROUTES, **(routes or {})}

    def handler(request: httpx.Request) -> httpx.Response:
        rec.requests.append(request)
        fn = table.get(request.url.path)
        if fn is None:
            raise AssertionError(f"测试未定义的路由: {request.url.path}")
        return fn(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OkxAdapter(CRED, broker_code=broker_code, client=client, market_kind=kind)
    if pin:
        adapter.timestamp_ms = lambda: PIN_MS      # type: ignore[method-assign]
    return adapter, rec


def call(adapter, coro):
    async def main():
        try:
            return await coro
        finally:
            await adapter.close()
    return asyncio.run(main())


def sign_of(prehash: str) -> str:
    return base64.b64encode(
        hmac.new(API_SECRET.encode(), prehash.encode(), hashlib.sha256).digest()
    ).decode()


def limit_order(**kw) -> OrderRequest:
    defaults = dict(symbol="BTC-USDT-SWAP", side="buy", qty=Decimal("0.123456"),
                    price=Decimal("111234.47"), time_in_force="GTC",
                    client_order_id="arb1a2b3c4d5e6f7")
    defaults.update(kw)
    return OrderRequest(**defaults)


ORDER_ACK = {"ordId": "312269865356374016", "clOrdId": "arb1a2b3c4d5e6f7",
             "tag": "", "sCode": "0", "sMsg": ""}


# ---- 签名 -----------------------------------------------------------------

def test_get_prehash_includes_query_string():
    """GET 的 requestPath 必须带查询串，且时间戳是带毫秒的 ISO8601 + Z。"""
    ad, rec = build({"/api/v5/account/positions": lambda r: ok([])})
    call(ad, ad.fetch_positions(["BTC-USDT-SWAP"]))

    req = rec.last("/api/v5/account/positions")
    assert req.headers["OK-ACCESS-TIMESTAMP"] == PIN_ISO
    assert req.headers["OK-ACCESS-KEY"] == API_KEY
    assert req.headers["OK-ACCESS-PASSPHRASE"] == PASSPHRASE
    expected = PIN_ISO + "GET" + "/api/v5/account/positions?instType=SWAP&instId=BTC-USDT-SWAP"
    assert req.headers["OK-ACCESS-SIGN"] == sign_of(expected)
    # 查询串只能出现一次（params 已经拼进 URL，不能让 httpx 再拼一遍）
    assert str(req.url).count("instType=SWAP") == 1


def test_post_signature_covers_the_exact_bytes_sent():
    """签名必须覆盖"发出去的那串字节"——序列化两次就是两个签名。"""
    ad, rec = build({"/api/v5/trade/order": lambda r: ok([ORDER_ACK])})
    call(ad, ad.place_order(limit_order()))

    req = rec.last("/api/v5/trade/order")
    body_str = req.content.decode()
    prehash = req.headers["OK-ACCESS-TIMESTAMP"] + "POST" + "/api/v5/trade/order" + body_str
    assert req.headers["OK-ACCESS-SIGN"] == sign_of(prehash)
    assert req.headers["Content-Type"] == "application/json"
    # 数值一律是字符串，不能是 JSON number
    assert '"sz":"' in body_str and '"px":"' in body_str


def test_public_endpoints_are_unsigned():
    ad, rec = build({"/api/v5/public/funding-rate": lambda r: ok([FUNDING_BTC])})
    call(ad, ad.fetch_carry_rates(["BTC-USDT-SWAP"]))
    req = rec.last("/api/v5/public/funding-rate")
    assert "OK-ACCESS-SIGN" not in req.headers
    assert "OK-ACCESS-KEY" not in req.headers


def test_simulated_header_only_when_enabled():
    ad, rec = build({"/api/v5/trade/order": lambda r: ok([ORDER_ACK])})
    call(ad, ad.place_order(limit_order()))
    assert "x-simulated-trading" not in rec.last("/api/v5/trade/order").headers


# ---- 返佣码 ---------------------------------------------------------------

def test_no_broker_tag_leaves_zero_traces():
    """空返佣码：body 里没有 tag 键，header 里没有痕迹，clOrdId 不加前缀。"""
    ad, rec = build({"/api/v5/trade/order": lambda r: ok([ORDER_ACK])}, broker_code="")
    call(ad, ad.place_order(limit_order()))

    req = rec.last("/api/v5/trade/order")
    raw = req.content.decode()
    assert "tag" not in raw                       # 连 "tag":"" 都不能发
    assert '"clOrdId":"arb1a2b3c4d5e6f7"' in raw  # 订单 ID 保持原样，没有前缀
    for name, value in req.headers.items():
        assert "broker" not in name.lower()
        assert "referer" not in name.lower()


def test_broker_tag_goes_into_body_tag_field():
    """非空返佣码：出现在 body 的 tag 字段，且不影响 clOrdId。"""
    ad, rec = build({"/api/v5/trade/order": lambda r: ok([ORDER_ACK])},
                    broker_code="CARRYFARM01")
    call(ad, ad.place_order(limit_order()))

    body = httpx_json(rec.last("/api/v5/trade/order"))
    assert body["tag"] == "CARRYFARM01"
    assert body["clOrdId"] == "arb1a2b3c4d5e6f7"
    assert not body["clOrdId"].startswith("CARRYFARM01")


def test_illegal_broker_tag_rejected_at_construction():
    """带横杠/超长的返佣码在配置期就该炸，别等下单才发现发不出去。"""
    for bad in ("carry-farm", "carryfarm_01", "A" * 17, "tag with space"):
        with pytest.raises(ValueError):
            OkxAdapter(CRED, broker_code=bad)


def httpx_json(req: httpx.Request) -> dict:
    import json
    return json.loads(req.content.decode())


# ---- 资金费符号与周期 -----------------------------------------------------

def test_funding_rate_keeps_exchange_sign_convention():
    """正 = 多头付给空头，原样存，不取反。"""
    ad, rec = build({"/api/v5/public/funding-rate": lambda r: ok([FUNDING_BTC])})
    rates = call(ad, ad.fetch_carry_rates(["BTC-USDT-SWAP"]))

    assert len(rates) == 1
    r = rates[0]
    assert r.rate == Decimal("0.0000616430459607")      # 一字不差，没经过 float
    assert r.rate > 0
    assert r.market == "okx:perp" and r.symbol == "BTC-USDT-SWAP"
    assert r.next_settle_ms == 1785571200000            # fundingTime = 下一次结算
    assert r.ts_ms == 1785548167559


def test_negative_funding_stays_negative():
    """空头付钱的品种解析出来必须是负数，方向搞反等于全盘反向下注。"""
    ad, _ = build({"/api/v5/public/funding-rate": lambda r: ok([FUNDING_TRB])})
    rates = call(ad, ad.fetch_carry_rates(["TRB-USDT-SWAP"]))
    assert rates[0].rate == Decimal("-0.0031") < 0


def test_funding_interval_is_computed_not_assumed():
    """周期只能用 nextFundingTime - fundingTime 现算：TRB 是 4h 不是 8h。"""
    ad, _ = build({"/api/v5/public/funding-rate": lambda r: ok([FUNDING_BTC])})
    assert call(ad, ad.fetch_carry_rates(["BTC-USDT-SWAP"]))[0].interval_s == 28800

    ad2, _ = build({"/api/v5/public/funding-rate": lambda r: ok([FUNDING_TRB])})
    trb = call(ad2, ad2.fetch_carry_rates(["TRB-USDT-SWAP"]))[0]
    assert trb.interval_s == 14400
    # 年化必须按各自周期归一化：4h 的 -0.31% 是 8h 的两倍强度
    assert trb.annualized() == Decimal("-0.0031") * (Decimal(365 * 24 * 3600) / 14400)


def test_funding_limits_are_per_symbol():
    """上下限逐品种不同，返回 (cap, floor)。"""
    ad, _ = build({"/api/v5/public/funding-rate": lambda r: ok([FUNDING_BTC])})
    cap, floor = call(ad, ad.fetch_funding_limits("BTC-USDT-SWAP"))
    assert cap == Decimal("0.00375")
    assert floor == Decimal("-0.00375")


def test_funding_history_ascending_and_uses_realized_rate():
    page1 = [{"instId": "BTC-USDT-SWAP", "fundingRate": "0.0002",
              "realizedRate": "0.00025", "fundingTime": str(1785571200000 - i * 28_800_000)}
             for i in range(100)]
    page2 = [{"instId": "BTC-USDT-SWAP", "fundingRate": "0.0001",
              "realizedRate": "0.00015",
              "fundingTime": str(1785571200000 - (100 + i) * 28_800_000)}
             for i in range(20)]
    calls: list[str] = []

    def route(r):
        after = r.url.params.get("after")
        calls.append(after or "")
        return ok(page2 if after else page1)

    ad, _ = build({"/api/v5/public/funding-rate-history": route})
    hist = call(ad, ad.fetch_funding_history("BTC-USDT-SWAP", since_ms=0, limit=1000))

    assert len(hist) == 120
    assert [t for t, _ in hist] == sorted(t for t, _ in hist)   # 升序
    assert hist[-1][1] == Decimal("0.00025")                    # 用 realizedRate
    assert calls[1] == str(1785571200000 - 99 * 28_800_000)     # after = 上页最旧


def test_funding_history_stops_at_since():
    cutoff = 1785571200000 - 5 * 28_800_000
    page = [{"fundingRate": "0.0002", "realizedRate": "0.0002",
             "fundingTime": str(1785571200000 - i * 28_800_000)} for i in range(100)]
    ad, _ = build({"/api/v5/public/funding-rate-history": lambda r: ok(page)})
    hist = call(ad, ad.fetch_funding_history("BTC-USDT-SWAP", since_ms=cutoff))
    assert len(hist) == 6
    assert all(t >= cutoff for t, _ in hist)


# ---- 精度：张 / lotSz / tickSz --------------------------------------------

def test_qty_rounds_toward_zero_on_lot_size():
    """0.123456 BTC ÷ ctVal 0.01 = 12.3456 张，按 lotSz 0.01 向零取整 → 12.34。"""
    ad, rec = build({"/api/v5/trade/order": lambda r: ok([ORDER_ACK])})
    call(ad, ad.place_order(limit_order(qty=Decimal("0.123456"))))
    assert httpx_json(rec.last("/api/v5/trade/order"))["sz"] == "12.34"


def test_price_rounds_toward_marketability():
    """买单 ROUND_UP、卖单 ROUND_DOWN，按 tickSz 0.1 规整。

    方向必须和 executor.cross_price 一致：保护限价是故意穿透对手价的，反向取整会把
    穿透削回来 —— tick 相对价格很粗的品种上那不是少赚一个 tick，而是挂不上、
    这条腿永远补不平。
    """
    ad, rec = build({"/api/v5/trade/order": lambda r: ok([ORDER_ACK])})
    call(ad, ad.place_order(limit_order(side="buy", price=Decimal("111234.47"))))
    assert httpx_json(rec.last("/api/v5/trade/order"))["px"] == "111234.5"

    ad2, rec2 = build({"/api/v5/trade/order": lambda r: ok([ORDER_ACK])})
    call(ad2, ad2.place_order(limit_order(side="sell", price=Decimal("111234.41"))))
    assert httpx_json(rec2.last("/api/v5/trade/order"))["px"] == "111234.4"


def test_tiny_tick_never_serialized_in_scientific_notation():
    """PEPE 的 tickSz 是 1e-9，body 里写成 "1E-9" 会被 OKX 直接拒。"""
    ad, rec = build({"/api/v5/trade/order": lambda r: ok([ORDER_ACK])})
    call(ad, ad.place_order(limit_order(symbol="PEPE-USDT-SWAP",
                                        qty=Decimal("25000000"),
                                        price=Decimal("0.0000000123"))))
    body = httpx_json(rec.last("/api/v5/trade/order"))
    assert body["px"] == "0.000000013"    # 买单向上贴 tick
    assert body["sz"] == "2.5"            # 25,000,000 PEPE ÷ ctVal 1e7 = 2.5 张
    assert not any(c in body["px"] + body["sz"] for c in "Ee+")


def test_dust_order_rejected_locally_not_sent():
    """取整后为零的单子在本地就拦下，别浪费一次下单配额去换一个拒单。"""
    ad, rec = build({"/api/v5/trade/order": lambda r: ok([ORDER_ACK])})
    with pytest.raises(OrderRejected) as exc:
        call(ad, ad.place_order(limit_order(qty=Decimal("0.00005"))))
    assert exc.value.code == "PINCHED"
    assert all(r.url.path != "/api/v5/trade/order" for r in rec.requests)


def test_inverse_contracts_are_filtered_out_of_perp():
    """反向合约 ctVal 是 USD，混进 linear 列表迟早算错单位。"""
    ad, _ = build()
    insts = call(ad, ad.fetch_instruments())
    symbols = {i.symbol for i in insts}
    assert symbols == {"BTC-USDT-SWAP", "PEPE-USDT-SWAP"}

    btc = next(i for i in insts if i.symbol == "BTC-USDT-SWAP")
    assert btc.tick_size == Decimal("0.1")
    # lot_size 必须是**币**口径（lotSz 0.01 张 × ctVal 0.01 = 0.0001 BTC）：
    # 上层拿它给币数量取整，报张口径等于把粒度说大 100 倍
    assert btc.lot_size == Decimal("0.0001")
    assert btc.contract_size == Decimal("0.01")      # ctVal × ctMult
    assert btc.base == "BTC" and btc.quote == "USDT"
    assert btc.max_leverage == Decimal("100")
    pepe = next(i for i in insts if i.symbol == "PEPE-USDT-SWAP")
    assert pepe.contract_size == Decimal("10000000")
    assert pepe.lot_size == Decimal("1000000")       # lotSz 0.1 张 × 1e7 = 100 万 PEPE


def test_lot_size_is_the_real_coin_granularity():
    """引擎按 Instrument.lot_size 给币数量取整，取整后非零的量必须真的下得出单。

    PEPE 的真实粒度是 100 万个币（lotSz 0.1 张 × ctVal 1e7）。报张口径(0.1)时，
    引擎会认为 50 万 PEPE 的缺口可以补 → adapter 里 5e5/1e7=0.05 张 → 按 lotSz
    取整成 0 → 每一拍都抛 OrderRejected("PINCHED")，死循环。
    """
    from carryfarm.safety import floor_to_step

    ad, rec = build({"/api/v5/trade/order": lambda r: ok([ORDER_ACK])})

    async def main():
        insts = {i.symbol: i for i in await ad.fetch_instruments()}
        pepe = insts["PEPE-USDT-SWAP"]
        assert floor_to_step(Decimal("500000"), pepe.lot_size) == 0   # 补不了，别发
        qty = floor_to_step(Decimal("5500000"), pepe.lot_size)
        assert qty == Decimal("5000000")
        await ad.place_order(limit_order(symbol="PEPE-USDT-SWAP", qty=qty,
                                         price=Decimal("0.0000000123")))
        assert httpx_json(rec.last("/api/v5/trade/order"))["sz"] == "0.5"

    call(ad, main())


# ---- 订单 ID --------------------------------------------------------------

def test_order_never_leaves_without_client_id():
    ad, rec = build({"/api/v5/trade/order": lambda r: ok([ORDER_ACK])})
    res = call(ad, ad.place_order(limit_order(client_order_id="")))
    cl = httpx_json(rec.last("/api/v5/trade/order"))["clOrdId"]
    assert cl and cl.isalnum() and len(cl) <= 32
    assert res.client_order_id == cl


def test_uuid_dashes_are_stripped():
    """带横杠的 UUID 会被 OKX 拒单，必须剥成 32 位 hex。"""
    ad, rec = build({"/api/v5/trade/order": lambda r: ok([ORDER_ACK])})
    raw = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    res = call(ad, ad.place_order(limit_order(client_order_id=raw)))
    sent = httpx_json(rec.last("/api/v5/trade/order"))["clOrdId"]
    assert sent == "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
    assert res.client_order_id == raw     # 返回调用方原始 ID，账本才对得上


def test_illegal_client_id_raises_instead_of_silent_rewrite():
    ad, _ = build({"/api/v5/trade/order": lambda r: ok([ORDER_ACK])})
    with pytest.raises(ExchangeError) as exc:
        call(ad, ad.place_order(limit_order(client_order_id="ord:2026/08/01")))
    assert exc.value.code == "BAD_CLIENT_ID"


# ---- 未知订单状态 ---------------------------------------------------------

def _raiser(exc):
    def route(r):
        raise exc
    return route


def test_timeout_raises_order_unknown():
    """超时绝不能当失败返回：当失败就会重发 → 双倍仓位。"""
    ad, _ = build({"/api/v5/trade/order": _raiser(httpx.ReadTimeout("read timeout"))})
    with pytest.raises(OrderUnknown) as exc:
        call(ad, ad.place_order(limit_order()))
    assert exc.value.client_order_id == "arb1a2b3c4d5e6f7"
    assert exc.value.venue == "okx"


def test_connection_drop_raises_order_unknown():
    ad, _ = build({"/api/v5/trade/order": _raiser(httpx.RemoteProtocolError("closed"))})
    with pytest.raises(OrderUnknown):
        call(ad, ad.place_order(limit_order()))


def test_server_5xx_raises_order_unknown():
    """5xx 说明请求打到了撮合侧，结果不明，同样按未知处理。"""
    ad, _ = build({"/api/v5/trade/order": lambda r: httpx.Response(502, text="bad gateway")})
    with pytest.raises(OrderUnknown):
        call(ad, ad.place_order(limit_order()))


def test_business_rejection_is_not_order_unknown():
    """明确的业务拒单不是未知状态，要如实抛 OrderRejected。"""
    ad, _ = build({"/api/v5/trade/order":
                   lambda r: err("1", "Operation failed.",
                                 [{"sCode": "51008", "sMsg": "Order failed. Insufficient USDT balance",
                                   "ordId": "", "clOrdId": "arb1a2b3c4d5e6f7"}])})
    with pytest.raises(OrderRejected) as exc:
        call(ad, ad.place_order(limit_order()))
    assert exc.value.code == "51008"          # 取 data[0].sCode，不是顶层的 "1"
    assert exc.value.retriable is False


def test_rate_limit_is_retriable():
    ad, _ = build({"/api/v5/trade/order": lambda r: err("50011", "Rate limit reached")})
    with pytest.raises(RateLimited) as exc:
        call(ad, ad.place_order(limit_order()))
    assert exc.value.retriable is True


def test_http_429_is_rate_limited():
    ad, _ = build({"/api/v5/trade/order": lambda r: httpx.Response(429, text="too many")})
    with pytest.raises(RateLimited):
        call(ad, ad.place_order(limit_order()))


def test_stale_timestamp_forces_resync():
    """50102 时间戳过期：标记为可重试，并逼下一次请求重新校时。"""
    ad, _ = build({"/api/v5/trade/order": lambda r: err("50102", "Timestamp request expired")})

    async def main():
        try:
            await ad.place_order(limit_order())
        except ExchangeError as exc:
            assert exc.code == "50102" and exc.retriable is True
            assert ad._clock.is_stale
        finally:
            await ad.close()
    asyncio.run(main())


def test_system_busy_is_order_unknown():
    """50013 系统繁忙：请求已经打到 OKX，结果不明，绝不能当明确失败。"""
    ad, _ = build({"/api/v5/trade/order": lambda r: err("50013", "系统繁忙，请稍后重试")})
    with pytest.raises(OrderUnknown):
        call(ad, ad.place_order(limit_order()))


def test_unreadable_body_is_order_unknown():
    """200 但响应体读不出来（WAF 注入 HTML）：同样是"不知道有没有进撮合"。"""
    ad, _ = build({"/api/v5/trade/order":
                   lambda r: httpx.Response(200, content=b"<html>blocked</html>")})
    with pytest.raises(OrderUnknown):
        call(ad, ad.place_order(limit_order()))


def test_expired_timestamp_on_place_is_not_order_unknown():
    """50102 是在**进撮合之前**被挡下的，订单确定不存在，
    不能因为 retriable=True 就白跑一轮 cancel+查单。"""
    ad, _ = build({"/api/v5/trade/order": lambda r: err("50102", "Timestamp expired")})
    with pytest.raises(ExchangeError) as exc:      # OrderUnknown 不是它的子类
        call(ad, ad.place_order(limit_order()))
    assert exc.value.code == "50102"


@pytest.mark.parametrize("route", [
    lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("read timeout")),
    lambda r: httpx.Response(503, text="service unavailable"),
])
def test_risk_path_errors_are_catchable_by_the_resolver(route):
    """本适配器把所有 httpx 异常吞成 ExchangeError('NETWORK')，而
    base.resolve_unknown_order 只认 (OrderRejected, httpx.HTTPError, RateLimited)。
    撤单/查单必须落进这个集合，否则消解流程会在最危险的时刻带着未捕获异常崩掉。"""
    ad, _ = build({"/api/v5/trade/cancel-order": route})
    with pytest.raises(RateLimited) as exc:
        call(ad, ad.cancel_order("BTC-USDT-SWAP", "arb1a2b3c4d5e6f7"))
    assert exc.value.retriable is True

    ad2, _ = build({"/api/v5/trade/order": route})
    with pytest.raises(RateLimited):
        call(ad2, ad2.query_order("BTC-USDT-SWAP", "arb1a2b3c4d5e6f7"))


def test_missing_order_maps_to_order_rejected():
    """"订单不存在" 必须是 OrderRejected —— resolve_unknown_order 靠它判定
    "这单从未被接受"，翻译错了会一直卡在重试里。"""
    ad, _ = build({"/api/v5/trade/order": lambda r: err("51603", "Order does not exist")})
    with pytest.raises(OrderRejected):
        call(ad, ad.query_order("BTC-USDT-SWAP", "arb1a2b3c4d5e6f7"))


# ---- 持仓 / 挂单 / 盘口 ---------------------------------------------------

def test_positions_convert_contracts_to_coins():
    """pos 是张数且带符号，必须乘 ctVal 折成币，否则空 3 张会被当成空 3 个 BTC。"""
    ad, _ = build({"/api/v5/account/positions": lambda r: ok([
        {"instId": "BTC-USDT-SWAP", "posSide": "net", "pos": "-3", "avgPx": "111000",
         "markPx": "111234.5", "liqPx": "125000", "lever": "3", "mgnMode": "cross"},
    ])})
    pos = call(ad, ad.fetch_positions())[0]
    assert pos.qty == Decimal("-0.03")            # -3 张 × 0.01 BTC
    assert pos.mark_price == Decimal("111234.5")
    assert pos.liquidation_price == Decimal("125000")
    assert pos.leverage == Decimal("3")


def test_long_short_mode_short_leg_gets_negative_sign():
    ad, _ = build({"/api/v5/account/positions": lambda r: ok([
        {"instId": "BTC-USDT-SWAP", "posSide": "short", "pos": "5", "avgPx": "111000",
         "markPx": "111234.5", "liqPx": "", "lever": "3"},
    ])})
    pos = call(ad, ad.fetch_positions())[0]
    assert pos.qty == Decimal("-0.05")
    assert pos.liquidation_price is None          # liqPx 空字符串是 OKX 的 null


def test_position_on_delisted_instrument_still_resolves():
    """下架/暂停的合约会被 state=live 过滤掉，但仓位还在——持仓是真值，
    不能因为查不到规格就让整个 fetch_positions 炸掉。"""
    dead = {**BTC_SWAP, "instId": "OLD-USDT-SWAP", "instFamily": "OLD-USDT",
            "ctVal": "0.5", "state": "suspend"}

    def instruments(r):
        # 带 instId 的单查能拿到非 live 的合约，列表查拿不到
        return ok([dead] if r.url.params.get("instId") == "OLD-USDT-SWAP"
                  else [BTC_SWAP, PEPE_SWAP])

    ad, _ = build({"/api/v5/public/instruments": instruments,
                   "/api/v5/account/positions": lambda r: ok([
                       {"instId": "OLD-USDT-SWAP", "posSide": "net", "pos": "4",
                        "avgPx": "10", "markPx": "11", "liqPx": "", "lever": "2"},
                   ])})
    pos = call(ad, ad.fetch_positions())[0]
    assert pos.qty == Decimal("2.0")          # 4 张 × ctVal 0.5


def test_book_top_sizes_are_in_coins():
    ad, _ = build({"/api/v5/market/books": lambda r: ok([
        {"asks": [["111234.5", "12.3", "0", "5"]],
         "bids": [["111234.4", "8.1", "0", "3"]], "ts": "1785548167559"},
    ])})
    top = call(ad, ad.fetch_book_top("BTC-USDT-SWAP"))
    assert top.bid_price == Decimal("111234.4")
    assert top.bid_qty == Decimal("8.1") * Decimal("0.01")
    assert top.ask_qty == Decimal("12.3") * Decimal("0.01")
    assert top.ts_ms == 1785548167559


# ---- 多档深度 -------------------------------------------------------------
#
# 中价钉在 100000（(99999.9+100000.1)/2），10bp 带宽 = [99900, 100100]，
# 两侧各放一档"恰好压在边界上"和一档"带宽外但量特别大"的档：
# 边界档漏掉会低估，带宽外的档算进去会把容量吹大，两个方向都一眼可见。
BOOK_DEPTH_BTC = {
    "bids": [["99999.9", "10", "0", "3"],
             ["99950.0", "20", "0", "7"],
             ["99900.0", "5", "0", "2"],        # 恰好 = 下边界，闭区间 → 计入
             ["99800.0", "100", "0", "9"]],     # 带宽外
    "asks": [["100000.1", "12", "0", "4"],
             ["100050.0", "8", "0", "5"],
             ["100100.0", "4", "0", "1"],       # 恰好 = 上边界
             ["100200.0", "999", "0", "6"]],    # 带宽外
    "ts": "1785548167559",
}


def test_book_depth_sums_notional_inside_the_band():
    """带宽内的累计名义额（张 → 币 → quote），手算对齐。"""
    ad, rec = build({"/api/v5/market/books": lambda r: ok([BOOK_DEPTH_BTC])})
    d = call(ad, ad.fetch_book_depth("BTC-USDT-SWAP", band_bp=10.0))

    # 买：99999.9×10×0.01 + 99950×20×0.01 + 99900×5×0.01
    #   = 9999.99 + 19990 + 4995
    assert d.bid_notional == Decimal("34984.99")
    # 卖：100000.1×12×0.01 + 100050×8×0.01 + 100100×4×0.01
    #   = 12000.012 + 8004 + 4004
    assert d.ask_notional == Decimal("24008.012")
    assert d.levels_used == 6                     # 两侧各 3 档
    assert d.band_bp == 10.0
    assert d.symbol == "BTC-USDT-SWAP" and d.ts_ms == 1785548167559
    # 一档口径必须和 fetch_book_top 完全一致（币）
    assert d.bid_price == Decimal("99999.9") and d.bid_qty == Decimal("0.1")
    assert d.ask_price == Decimal("100000.1") and d.ask_qty == Decimal("0.12")
    assert d.thinner_side == d.ask_notional       # 对冲按短板算容量
    # 深度接口贵，档数要一次取够 —— 拿 sz=1 当深度代理正是要修的病
    assert rec.last("/api/v5/market/books").url.params["sz"] == "50"


def test_book_depth_ignores_levels_outside_the_band():
    """带宽外的量一分都不能计入：那是"看得见但吃不到"的深度，
    算进去等于把容量吹大，比低估更危险。"""
    fat = {"bids": [*BOOK_DEPTH_BTC["bids"], ["50000", "1000000", "0", "1"]],
           "asks": [*BOOK_DEPTH_BTC["asks"], ["150000", "1000000", "0", "1"]],
           "ts": "1785548167559"}
    ad, _ = build({"/api/v5/market/books": lambda r: ok([fat])})

    async def main():
        wide = await ad.fetch_book_depth("BTC-USDT-SWAP", band_bp=10.0)
        narrow = await ad.fetch_book_depth("BTC-USDT-SWAP", band_bp=1.0)
        return wide, narrow

    wide, narrow = call(ad, main())
    # 远端那两笔百万张（≈500 亿美元）没有污染读数
    assert wide.bid_notional == Decimal("34984.99")
    assert wide.ask_notional == Decimal("24008.012")
    # 1bp 带 = [99990, 100010]，只剩最优价那一档
    assert narrow.bid_notional == Decimal("9999.99")
    assert narrow.ask_notional == Decimal("12000.012")
    assert narrow.levels_used == 2


# PEPE 一张 = 1000 万个币。tickSz 相对价格有 10%，10bp 带里连一档都装不下，
# 所以这里用 2000bp（20%）的带宽：[8e-9, 1.2e-8]。
BOOK_DEPTH_PEPE = {
    "bids": [["0.0000000099", "3", "0", "2"],
             ["0.000000009", "0.5", "0", "1"],
             ["0.0000000079", "1000", "0", "4"]],     # 带宽外
    "asks": [["0.0000000101", "2", "0", "3"],
             ["0.0000000119", "1", "0", "1"],
             ["0.0000000121", "5000", "0", "9"]],     # 带宽外
    "ts": "1785548167559",
}


def test_book_depth_converts_contracts_to_coins_before_notional():
    """忘了乘 ctVal，PEPE 的名义额会小七个数量级——真机会被判成"深度不够"。"""
    ad, _ = build({"/api/v5/market/books": lambda r: ok([BOOK_DEPTH_PEPE])})
    d = call(ad, ad.fetch_book_depth("PEPE-USDT-SWAP", band_bp=2000.0))

    # 买：9.9e-9×3张×1e7 + 9e-9×0.5张×1e7 = 0.297 + 0.045
    assert d.bid_notional == Decimal("0.342")
    # 卖：1.01e-8×2张×1e7 + 1.19e-8×1张×1e7 = 0.202 + 0.119
    assert d.ask_notional == Decimal("0.321")
    assert d.bid_qty == Decimal("30000000")       # 3 张 = 3000 万个 PEPE，不是 3
    assert d.ask_qty == Decimal("20000000")
    assert d.levels_used == 4


def test_book_depth_reports_short_level_count_without_extrapolating():
    """交易所只回了 2 档且都在带内 = 带宽没走完档位就用光了。
    如实报 levels_used 和只统计到的量，宁可低估也不补足。"""
    thin = {"bids": [["99999.9", "10", "0", "3"], ["99950.0", "20", "0", "7"]],
            "asks": [["100000.1", "12", "0", "4"], ["100050.0", "8", "0", "5"]],
            "ts": "1785548167559"}
    ad, _ = build({"/api/v5/market/books": lambda r: ok([thin])})
    d = call(ad, ad.fetch_book_depth("BTC-USDT-SWAP", band_bp=10.0))

    assert d.levels_used == 4                       # 要了 50×2 档，只回来 4 档
    assert d.bid_notional == Decimal("29989.99")    # 9999.99 + 19990，没有外推
    assert d.ask_notional == Decimal("20004.012")   # 12000.012 + 8004


def test_book_depth_is_public_and_refuses_to_guess():
    """公开端点不带签名；空盘和非正带宽都不能静默返回 0——
    scoring 见到 depth_notional<=0 会退回半价差模型，错配置就此埋进读数里。"""
    ad, rec = build({"/api/v5/market/books": lambda r: ok([BOOK_DEPTH_BTC])})
    call(ad, ad.fetch_book_depth("BTC-USDT-SWAP"))          # 默认 10bp
    assert "OK-ACCESS-SIGN" not in rec.last("/api/v5/market/books").headers

    ad2, _ = build({"/api/v5/market/books": lambda r: ok([BOOK_DEPTH_BTC])})
    with pytest.raises(ValueError):
        call(ad2, ad2.fetch_book_depth("BTC-USDT-SWAP", band_bp=0.0))

    ad3, _ = build({"/api/v5/market/books":
                    lambda r: ok([{"bids": [], "asks": [], "ts": "1785548167559"}])})
    with pytest.raises(ExchangeError) as exc:
        call(ad3, ad3.fetch_book_depth("BTC-USDT-SWAP"))
    assert exc.value.code == "EMPTY_BOOK"


def test_ws_bbo_push_converts_to_coins():
    """WS 推送的档位量同样是张数（文档 bbo-tbt 样例），折币逻辑必须和 REST 一致。"""
    ad, _ = build()
    push = {"asks": [["111234.5", "12.3", "0", "5"]],
            "bids": [["111234.4", "8.1", "0", "3"]],
            "ts": "1785548167559", "seqId": 123456789, "prevSeqId": 123456788}

    async def main():
        try:
            await ad.fetch_instruments()          # 预热 ctVal
            return ad._book_from_ws("BTC-USDT-SWAP", push)
        finally:
            await ad.close()

    top = asyncio.run(main())
    assert top.bid_qty == Decimal("0.081") and top.ask_qty == Decimal("0.123")
    assert top.mid == Decimal("111234.45")


def test_open_orders_report_filled_in_coins():
    ad, _ = build({"/api/v5/trade/orders-pending": lambda r: ok([
        {"instId": "BTC-USDT-SWAP", "ordId": "1", "clOrdId": "arb1", "state": "partially_filled",
         "accFillSz": "4", "avgPx": "111200.3", "fee": "-0.55", "feeCcy": "USDT"},
    ])})
    orders = call(ad, ad.fetch_open_orders("BTC-USDT-SWAP"))
    assert orders[0].status == "partial"
    assert orders[0].filled_qty == Decimal("0.04")
    assert orders[0].fee == Decimal("0.55")       # OKX 用负数表示"你付"，这里翻正
    assert not orders[0].is_terminal


def test_cancel_normalizes_client_id():
    ad, rec = build({"/api/v5/trade/cancel-order": lambda r: ok([{"sCode": "0"}])})
    call(ad, ad.cancel_order("BTC-USDT-SWAP", "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"))
    body = httpx_json(rec.last("/api/v5/trade/cancel-order"))
    assert body == {"instId": "BTC-USDT-SWAP",
                    "clOrdId": "0f1e2d3c4b5a69788796a5b4c3d2e1f0"}


def test_set_leverage_sends_plain_decimal_string():
    ad, rec = build({"/api/v5/account/set-leverage": lambda r: ok([{"lever": "3"}])})
    call(ad, ad.set_leverage("BTC-USDT-SWAP", Decimal("3")))
    assert httpx_json(rec.last("/api/v5/account/set-leverage")) == {
        "instId": "BTC-USDT-SWAP", "lever": "3", "mgnMode": "cross"}


BOOK_1 = {"asks": [["111234.5", "10", "0", "3"]], "bids": [["111234.3", "8", "0", "2"]],
          "ts": "1785548167559"}


def test_leverage_tiers_are_quote_notional_not_coins():
    """base.py 的契约是 [(名义上限, 最大杠杆)]，币安/Bybit/Gate/Bitget 给的都是
    quote 名义额。OKX 的 maxSz 是张数，只报到"币"就会让 safety.choose_leverage
    拿美元名义额去比 100（BTC），判定早已越顶档，直接掐到最低杠杆。"""
    ad, _ = build({
        "/api/v5/public/position-tiers": lambda r: ok([
            {"tier": "2", "maxSz": "50000", "maxLever": "50", "instFamily": "BTC-USDT"},
            {"tier": "1", "maxSz": "10000", "maxLever": "100", "instFamily": "BTC-USDT"},
        ]),
        "/api/v5/market/books": lambda r: ok([BOOK_1]),
    })
    tiers = call(ad, ad.fetch_leverage_tiers("BTC-USDT-SWAP"))
    mid = Decimal("111234.4")                       # (111234.3 + 111234.5) / 2
    assert tiers == ((Decimal("100") * mid, Decimal("100")),
                     (Decimal("500") * mid, Decimal("50")))


def test_leverage_tiers_degrade_to_single_tier_without_a_price():
    """拿不到价格时退化成"名义上限未知"的单档（和 HTX/Bybit 一致），
    绝不返回一个单位是币的数字冒充名义额。"""
    def boom(_r):
        raise httpx.ConnectError("no book")

    ad, _ = build({
        "/api/v5/public/position-tiers": lambda r: ok([
            {"tier": "1", "maxSz": "10000", "maxLever": "100", "instFamily": "BTC-USDT"}]),
        "/api/v5/market/books": boom,
    })
    assert call(ad, ad.fetch_leverage_tiers("BTC-USDT-SWAP")) == (
        (Decimal("Infinity"), Decimal("100")),)


# ---- 费率 -----------------------------------------------------------------

def test_fee_schedule_flips_sign_to_cost_convention():
    """OKX 返回负数表示"你付"，评分侧要的是"正数=成本"。"""
    ad, _ = build({"/api/v5/account/trade-fee": lambda r: ok([
        {"level": "Lv1", "instType": "SWAP", "taker": "-0.0005", "maker": "-0.0002",
         "takerU": "-0.0005", "makerU": "0.0001", "ts": "1785548167559"},
    ])})
    fees = call(ad, ad.fetch_fee_schedule(["BTC-USDT-SWAP"]))
    assert fees[0].taker == Decimal("0.0005")     # 5bp 成本
    assert fees[0].maker == Decimal("-0.0001")    # 正数返佣 → 负成本
    assert fees[0].market == "okx:perp"


def test_fee_schedule_aligns_with_input_symbols():
    ad, _ = build({"/api/v5/account/trade-fee": lambda r: ok([
        {"taker": "-0.0005", "maker": "-0.0002", "takerU": "-0.0004", "makerU": "-0.0001"},
    ])})
    fees = call(ad, ad.fetch_fee_schedule(["BTC-USDT-SWAP", "PEPE-USDT-SWAP"]))
    assert len(fees) == 2
    assert all(f.taker == Decimal("0.0004") for f in fees)   # linear 走 takerU


# ---- 市场范围 -------------------------------------------------------------

def test_margin_market_is_not_implemented():
    """杠杆借币利率端点调研文档里没有，宁可炸也不能编利率。"""
    with pytest.raises(NotImplementedError):
        OkxAdapter(CRED, market_kind=MarketKind.MARGIN)


def test_spot_has_no_carry_and_no_leverage():
    ad, _ = build(kind=MarketKind.SPOT)

    async def main():
        try:
            assert await ad.fetch_carry_rates(["BTC-USDT"]) == ()
            assert await ad.fetch_funding_limits("BTC-USDT") is None
            with pytest.raises(NotImplementedError):
                await ad.set_leverage("BTC-USDT", Decimal("3"))
        finally:
            await ad.close()
    asyncio.run(main())


def test_spot_order_uses_cash_td_mode():
    spot_inst = {"instId": "BTC-USDT", "instType": "SPOT", "baseCcy": "BTC",
                 "quoteCcy": "USDT", "tickSz": "0.1", "lotSz": "0.00000001",
                 "minSz": "0.00001", "state": "live", "lever": "5"}
    ad, rec = build({"/api/v5/public/instruments": lambda r: ok([spot_inst]),
                     "/api/v5/trade/order": lambda r: ok([ORDER_ACK])},
                    kind=MarketKind.SPOT)
    call(ad, ad.place_order(limit_order(symbol="BTC-USDT", qty=Decimal("0.5"))))
    body = httpx_json(rec.last("/api/v5/trade/order"))
    assert body["tdMode"] == "cash"
    assert body["sz"] == "0.50000000"      # 现货 contract_size=1，数量即币数
    assert "posSide" not in body           # 现货没有持仓方向
