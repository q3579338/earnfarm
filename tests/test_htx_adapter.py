"""HTX 适配器单测。全部用 httpx.MockTransport 注入假响应，绝不连交易所。

用 asyncio.run 驱动而不是 pytest-asyncio —— 少一个测试期依赖。
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

from carryfarm.exchanges.base import (
    Credential,
    ExchangeError,
    OrderRejected,
    OrderRequest,
    OrderUnknown,
    RateLimited,
)
from carryfarm.exchanges.htx import HtxAdapter
from carryfarm.models import MarketKind

CRED = Credential(api_key="test-access-key", api_secret="test-secret-key")

# 固定时刻 2017-05-11T15:19:30Z，用于复现官方签名示例
FIXED_MS = 1494515970_000

# swap_contract_info 的假响应写成 JSON **文本**而不是 Python 字典：
# 精度是本适配器的命门，必须让解析路径从字符串起步（price_tick 1e-9 经 float 会失真）
CONTRACT_INFO = """
{"status":"ok","data":[
 {"symbol":"BTC","contract_code":"BTC-USDT","contract_size":0.001,"price_tick":0.1,
  "settlement_period":"8","contract_status":1,"support_margin_mode":"all"},
 {"symbol":"SHIB","contract_code":"SHIB-USDT","contract_size":1000,"price_tick":1e-09,
  "settlement_period":"8","contract_status":1,"support_margin_mode":"all"},
 {"symbol":"JST","contract_code":"JST-USDT","contract_size":100,"price_tick":0.00001,
  "settlement_period":"4","contract_status":1,"support_margin_mode":"all"},
 {"symbol":"DEAD","contract_code":"DEAD-USDT","contract_size":1,"price_tick":0.01,
  "settlement_period":"8","contract_status":3,"support_margin_mode":"all"}
],"ts":1494515970000}
"""


def ok(payload: dict) -> httpx.Response:
    return httpx.Response(200, text=json.dumps(payload))


def err(code: int, msg: str) -> httpx.Response:
    """HTX 的业务错误 HTTP 状态码仍然是 200，错误码在 body 里。"""
    return httpx.Response(200, text=json.dumps(
        {"status": "error", "err_code": code, "err_msg": msg, "ts": FIXED_MS}))


def router(routes: dict, calls: list | None = None):
    """按路径分发的假 transport。routes 的值可以是 Response 或 callable(request)。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        path = request.url.path
        if path == "/linear-swap-api/v1/swap_contract_info":
            return httpx.Response(200, text=CONTRACT_INFO)
        target = routes.get(path)
        if target is None:
            raise AssertionError(f"测试没有为 {path} 准备响应")
        return target(request) if callable(target) else target
    return handler


@contextlib.asynccontextmanager
async def adapter(handler, **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ad = HtxAdapter(CRED, client=client, **kwargs)
    # 冻结时钟：既避免基类在 _request 里触发校时请求，也让 Timestamp 可预测
    ad._clock.synced_at = time.time()
    ad.timestamp_ms = lambda: FIXED_MS          # type: ignore[method-assign]
    try:
        yield ad
    finally:
        await ad.close()


def run(coro_fn):
    asyncio.run(coro_fn())


def body_of(request: httpx.Request) -> dict:
    return json.loads(request.content.decode())


# ---- 签名 ---------------------------------------------------------------

# 官方文档的签名示例（火币通用签名章节）。金标准是这 4 行待签串本身：
# 文档给出的 Signature 值 4F65x5A2bLyMWVQj3Aqp+B4w+ivaA7n5Oi2SuYtCJ9o= 用的是
# 打了 xxxxxx 码的 key/secret，实测怎么算都对不上（已穷举验证），
# 所以签名值只能用独立实现的 HMAC 对拍，不能拿文档里那串当断言。
OFFICIAL_PAYLOAD = (
    "GET\n"
    "api.huobi.pro\n"
    "/v1/order/orders\n"
    "AccessKeyId=e2xxxxxx-99xxxxxx-84xxxxxx-7xxxx&SignatureMethod=HmacSHA256"
    "&SignatureVersion=2&Timestamp=2017-05-11T15%3A19%3A30&order-id=1234567890"
)


def test_sign_payload_matches_official_example():
    async def main():
        async with adapter(router({}), rest_base="https://api.huobi.pro") as ad:
            payload = ad._sign_payload("GET", "/v1/order/orders", {
                "AccessKeyId": "e2xxxxxx-99xxxxxx-84xxxxxx-7xxxx",
                "SignatureMethod": "HmacSHA256",
                "SignatureVersion": "2",
                "Timestamp": "2017-05-11T15:19:30",
                "order-id": 1234567890,
            })
            assert payload == OFFICIAL_PAYLOAD
    run(main)


def test_signature_matches_independent_hmac():
    expected = base64.b64encode(hmac.new(
        CRED.api_secret.encode(), OFFICIAL_PAYLOAD.encode(), hashlib.sha256
    ).digest()).decode()

    async def main():
        async with adapter(router({}), rest_base="https://api.huobi.pro") as ad:
            assert ad._hmac_sha256_b64(OFFICIAL_PAYLOAD) == expected
    run(main)


def test_get_signature_includes_business_params_and_uppercase_hex():
    async def main():
        async with adapter(router({})) as ad:
            _, url, params, body = ad._prepare(
                "GET", "/linear-swap-api/v1/swap_query_elements",
                {"contract_code": "BTC-USDT"}, {}, True)
            assert params == {} and body == {}       # query 全在 url 里，不交给 httpx 重编码
            query = url.split("?", 1)[1]
            # 冒号必须编成大写 %3A（小写 %3a 会被判签名错误 1003）
            assert "Timestamp=2017-05-11T15%3A19%3A30" in query
            assert "%3a" not in query
            # GET 的业务参数要进签名串，且按 ASCII 升序排在 4 个认证参数之后
            assert query.startswith(
                "AccessKeyId=test-access-key&SignatureMethod=HmacSHA256"
                "&SignatureVersion=2&Timestamp=2017-05-11T15%3A19%3A30"
                "&contract_code=BTC-USDT&Signature=")
    run(main)


def test_post_body_params_never_enter_signature():
    """HTX 独有：POST 只签 4 个认证参数，JSON body 一个字段都不进签名串。"""
    async def main():
        async with adapter(router({})) as ad:
            headers, url, _, body = ad._prepare(
                "POST", "/linear-swap-api/v1/swap_cross_order", {},
                {"contract_code": "BTC-USDT", "volume": 50}, True)
            query = url.split("?", 1)[1]
            signed_part, sig = query.rsplit("&Signature=", 1)
            assert signed_part == (
                "AccessKeyId=test-access-key&SignatureMethod=HmacSHA256"
                "&SignatureVersion=2&Timestamp=2017-05-11T15%3A19%3A30")
            assert "contract_code" not in signed_part and "volume" not in signed_part
            assert body == {"contract_code": "BTC-USDT", "volume": 50}
            assert headers["Content-Type"] == "application/json"
            # 签名做完还要再 URL 编码一次（base64 里的 + / = 必须转义）
            assert "+" not in sig and "=" not in sig.rstrip()
            expected = ad._hmac_sha256_b64(ad._sign_payload(
                "POST", "/linear-swap-api/v1/swap_cross_order", {
                    "AccessKeyId": CRED.api_key, "SignatureMethod": "HmacSHA256",
                    "SignatureVersion": "2", "Timestamp": "2017-05-11T15:19:30"}))
            assert httpx.URL(url).params["Signature"] == expected
    run(main)


# ---- 返佣码 -------------------------------------------------------------

ORDER_OK = {"status": "ok", "data": {"order_id": 770000000000000001,
                                     "order_id_str": "770000000000000001",
                                     "client_order_id": 9527}, "ts": FIXED_MS}
ORDER_PATH = "/linear-swap-api/v1/swap_cross_order"


def _place(broker_code: str, cid: str = "9527"):
    calls: list[httpx.Request] = []
    handler = router({ORDER_PATH: ok(ORDER_OK)}, calls)

    async def main():
        async with adapter(handler, broker_code=broker_code) as ad:
            res = await ad.place_order(OrderRequest(
                symbol="BTC-USDT", side="buy", qty=Decimal("0.05"),
                price=Decimal("62963.4"), time_in_force="FOK",
                client_order_id=cid))
            assert res.client_order_id == cid
    run(main)
    order_req = [c for c in calls if c.url.path == ORDER_PATH][0]
    return order_req, body_of(order_req)


def test_broker_code_empty_leaves_no_trace():
    """用户默认不填码：body 不能出现 channel_code（连空串都不行）。"""
    request, body = _place("")
    assert "channel_code" not in body
    raw = request.content.decode() + str(request.url) + json.dumps(dict(request.headers))
    assert "channel_code" not in raw
    # 也不能像现货那样往 client_order_id 上加前缀
    assert body["client_order_id"] == 9527


def test_broker_code_injected_into_body_only():
    """合约的返佣走 body 的 channel_code 字段，不是 header 也不是订单ID前缀。"""
    request, body = _place("AA03022abc")
    assert body["channel_code"] == "AA03022abc"
    assert "AA03022abc" not in str(request.url)
    assert not any("AA03022abc" in v for v in request.headers.values())
    assert body["client_order_id"] == 9527      # ID 不受返佣码影响


def test_non_numeric_client_order_id_mapped_deterministically():
    """HTX 合约的 client_order_id 是 long，字符串 ID 要确定性地映射成数字。"""
    _, body_a = _place("", cid="carryfarm-btc-leg1")
    _, body_b = _place("", cid="carryfarm-btc-leg1")
    assert body_a["client_order_id"] == body_b["client_order_id"]
    assert 1 <= body_a["client_order_id"] < 2 ** 63


# ---- 资金费符号 ---------------------------------------------------------

BATCH_FUNDING = {"status": "ok", "data": [
    {"symbol": "BTC", "contract_code": "BTC-USDT", "fee_asset": "USDT",
     "funding_rate": "0.0001", "estimated_rate": None, "next_funding_time": None,
     "funding_time": str(FIXED_MS + 3_600_000)},
    {"symbol": "JST", "contract_code": "JST-USDT", "fee_asset": "USDT",
     "funding_rate": "-0.00022", "estimated_rate": None, "next_funding_time": None,
     "funding_time": str(FIXED_MS - 1000)},
], "ts": FIXED_MS}


def test_funding_sign_is_exchange_native():
    """交易所原始约定：正 = 多头付给空头。适配器原样存，绝不取反。"""
    handler = router({"/linear-swap-api/v1/swap_batch_funding_rate": ok(BATCH_FUNDING)})

    async def main():
        async with adapter(handler) as ad:
            rates = {r.symbol: r for r in await ad.fetch_carry_rates()}
            assert rates["BTC-USDT"].rate == Decimal("0.0001")     # 正的还是正的
            assert rates["JST-USDT"].rate == Decimal("-0.00022")   # 负的还是负的
            assert isinstance(rates["BTC-USDT"].rate, Decimal)
            # 结算周期按合约取：JST 是 4h，直接拿去和别家 8h 费率比会差一倍
            assert rates["BTC-USDT"].interval_s == 28800
            assert rates["JST-USDT"].interval_s == 14400
            # v1 的 next_funding_time 恒为 null，要自己从 funding_time 往前推
            assert rates["BTC-USDT"].next_settle_ms == FIXED_MS + 3_600_000
            assert rates["JST-USDT"].next_settle_ms == FIXED_MS - 1000 + 4 * 3600 * 1000
    run(main)


def test_carry_rate_annualization_uses_native_interval():
    handler = router({"/linear-swap-api/v1/swap_batch_funding_rate": ok(BATCH_FUNDING)})

    async def main():
        async with adapter(handler) as ad:
            rates = {r.symbol: r for r in await ad.fetch_carry_rates(["JST-USDT"])}
            assert list(rates) == ["JST-USDT"]
            # 4h 品种同样费率的年化是 8h 的两倍
            assert rates["JST-USDT"].annualized() == Decimal("-0.00022") * (365 * 24 * 3600 // 14400)
    run(main)


def test_funding_history_prefers_realized_rate_and_sorts_ascending():
    page = {"status": "ok", "data": {"total_page": 1, "current_page": 1, "total_size": 3,
            "data": [
                {"funding_time": "3000", "funding_rate": "0.0003", "realized_rate": None},
                {"funding_time": "2000", "funding_rate": "0.0002", "realized_rate": "0.0001"},
                {"funding_time": "1000", "funding_rate": "-0.0004", "realized_rate": None},
            ]}, "ts": FIXED_MS}
    handler = router({"/linear-swap-api/v1/swap_historical_funding_rate": ok(page)})

    async def main():
        async with adapter(handler) as ad:
            hist = await ad.fetch_funding_history("BTC-USDT", since_ms=1500)
            assert hist == ((2000, Decimal("0.0001")), (3000, Decimal("0.0003")))
    run(main)


# ---- 精度 ---------------------------------------------------------------

def test_instrument_precision_parsed_without_float():
    async def main():
        async with adapter(router({})) as ad:
            insts = {i.symbol: i for i in await ad.fetch_instruments()}
            assert "DEAD-USDT" not in insts             # contract_status=3 不可交易
            shib = insts["SHIB-USDT"]
            assert shib.tick_size == Decimal("0.000000001")
            assert str(shib.tick_size) == "1E-9"        # 没经过 float，精确
            # HTX 的数量粒度就是一张合约的币数
            assert shib.lot_size == shib.contract_size == Decimal("1000")
            assert insts["BTC-USDT"].tick_size == Decimal("0.1")
            assert insts["JST-USDT"].funding_interval_s == 4 * 3600
    run(main)


def test_qty_floors_to_lot_and_price_snaps_to_tick():
    calls: list[httpx.Request] = []
    handler = router({ORDER_PATH: ok(ORDER_OK)}, calls)

    async def main():
        async with adapter(handler) as ad:
            # 0.0507 BTC / 0.001 = 50.7 张 → 向零取整成 50 张，余数丢掉
            await ad.place_order(OrderRequest(
                symbol="BTC-USDT", side="buy", qty=Decimal("0.0507"),
                price=Decimal("62963.47"), time_in_force="FOK",
                client_order_id="a"))
            # 卖单价格向下取整，买单向上 —— 保护限价的穿透方向不能被取整削回来
            await ad.place_order(OrderRequest(
                symbol="SHIB-USDT", side="sell", qty=Decimal("12345"),
                price=Decimal("0.0000123456"), time_in_force="IOC",
                client_order_id="b"))
    run(main)

    first = body_of([c for c in calls if c.url.path == ORDER_PATH][0])
    assert first["volume"] == 50 and isinstance(first["volume"], int)
    assert first["price"] == "62963.5"          # 买单向上贴 tick=0.1
    assert first["order_price_type"] == "fok"

    second = body_of([c for c in calls if c.url.path == ORDER_PATH][1])
    assert second["volume"] == 12                # 12345 / 1000 → 12 张
    assert Decimal(second["price"]) == Decimal("0.000012345")   # tick=1e-9 向下
    assert second["order_price_type"] == "ioc"
    assert second["direction"] == "sell"


def test_qty_below_one_contract_is_rejected_locally():
    handler = router({ORDER_PATH: ok(ORDER_OK)})

    async def main():
        async with adapter(handler) as ad:
            with pytest.raises(OrderRejected):
                await ad.place_order(OrderRequest(
                    symbol="BTC-USDT", side="buy", qty=Decimal("0.0009"),
                    price=Decimal("62963.4"), client_order_id="tiny"))
    run(main)


def test_order_without_client_order_id_refused():
    async def main():
        async with adapter(router({})) as ad:
            with pytest.raises(ValueError):
                await ad.place_order(OrderRequest(
                    symbol="BTC-USDT", side="buy", qty=Decimal("0.05"),
                    price=Decimal("62963.4"), client_order_id=""))
    run(main)


# ---- 未知订单状态 -------------------------------------------------------

@pytest.mark.parametrize("boom", [
    httpx.ConnectTimeout("connect timed out"),
    httpx.ReadTimeout("read timed out"),
    httpx.ConnectError("connection reset"),
    httpx.RemoteProtocolError("server disconnected without response"),
])
def test_network_failure_raises_order_unknown(boom):
    """超时/断连绝不能当失败返回：上层会重发，直接双倍仓位。"""
    def blow(_request):
        raise boom
    handler = router({ORDER_PATH: blow})

    async def main():
        async with adapter(handler) as ad:
            with pytest.raises(OrderUnknown) as excinfo:
                await ad.place_order(OrderRequest(
                    symbol="BTC-USDT", side="buy", qty=Decimal("0.05"),
                    price=Decimal("62963.4"), client_order_id="leg-1"))
            assert excinfo.value.client_order_id == "leg-1"
    run(main)


def test_http_5xx_also_unknown():
    handler = router({ORDER_PATH: httpx.Response(502, text="bad gateway")})

    async def main():
        async with adapter(handler) as ad:
            with pytest.raises(OrderUnknown):
                await ad.place_order(OrderRequest(
                    symbol="BTC-USDT", side="buy", qty=Decimal("0.05"),
                    price=Decimal("62963.4"), client_order_id="leg-2"))
    run(main)


def test_business_reject_is_not_unknown():
    """明确的拒单（保证金不足）说明撮合没收到单，不能升级成 OrderUnknown。"""
    handler = router({ORDER_PATH: err(1047, "Insufficient margin available.")})

    async def main():
        async with adapter(handler) as ad:
            with pytest.raises(OrderRejected) as excinfo:
                await ad.place_order(OrderRequest(
                    symbol="BTC-USDT", side="buy", qty=Decimal("0.05"),
                    price=Decimal("62963.4"), client_order_id="leg-3"))
            assert excinfo.value.code == "1047"
            assert excinfo.value.retriable is False
    run(main)


def test_cancel_rate_ban_maps_to_rate_limited():
    """1084 是撤单率封禁，等解禁能恢复，属于限频不是拒单。"""
    handler = router({ORDER_PATH: err(1084, "banned")})

    async def main():
        async with adapter(handler) as ad:
            with pytest.raises(RateLimited) as excinfo:
                await ad.place_order(OrderRequest(
                    symbol="BTC-USDT", side="buy", qty=Decimal("0.05"),
                    price=Decimal("62963.4"), client_order_id="leg-4"))
            assert excinfo.value.retriable is True
    run(main)


def test_signature_error_is_not_retriable():
    handler = router({"/linear-swap-api/v1/swap_cross_position_info": err(1003, "invalid signature")})

    async def main():
        async with adapter(handler) as ad:
            with pytest.raises(ExchangeError) as excinfo:
                await ad.fetch_positions()
            assert excinfo.value.code == "1003" and excinfo.value.retriable is False
    run(main)


def test_cancel_reports_per_order_error_from_errors_array():
    """撤单整体 status=ok，逐笔失败藏在 data.errors 里，必须逐笔检查。"""
    resp = ok({"status": "ok", "data": {
        "errors": [{"order_id": "1", "err_code": 1061, "err_msg": "订单不存在"}],
        "successes": ""}, "ts": FIXED_MS})
    handler = router({"/linear-swap-api/v1/swap_cross_cancel": resp})

    async def main():
        async with adapter(handler) as ad:
            with pytest.raises(OrderRejected):
                await ad.cancel_order("BTC-USDT", "ghost")
    run(main)


@pytest.mark.parametrize("boom", [
    httpx.ReadTimeout("read timed out"),
    httpx.ConnectError("connection reset"),
])
def test_risk_path_transport_errors_are_catchable_by_the_resolver(boom):
    """base._request 把传输层异常包成 ExchangeError('NETWORK')，而
    base.resolve_unknown_order 只认 (OrderRejected, httpx.HTTPError, RateLimited)。
    撤单/查单必须落进这个集合，否则消解流程会在最危险的时刻带着未捕获异常崩掉。"""
    def blow(_request):
        raise boom

    async def main():
        async with adapter(router({"/linear-swap-api/v1/swap_cross_cancel": blow})) as ad:
            with pytest.raises(RateLimited) as ei:
                await ad.cancel_order("BTC-USDT", "leg-1")
            assert ei.value.retriable is True and ei.value.code == "NETWORK"
        async with adapter(router({"/linear-swap-api/v1/swap_cross_order_info": blow})) as ad2:
            with pytest.raises(RateLimited):
                await ad2.query_order("BTC-USDT", "leg-1")
    run(main)


def test_risk_path_5xx_is_catchable_by_the_resolver():
    handler = router({"/linear-swap-api/v1/swap_cross_cancel": httpx.Response(503, text="down")})

    async def main():
        async with adapter(handler) as ad:
            with pytest.raises(RateLimited) as ei:
                await ad.cancel_order("BTC-USDT", "leg-1")
            assert ei.value.code == "HTTP_503"
    run(main)


def test_resolve_unknown_order_reports_zero_when_exchange_never_saw_it():
    """cancel 终结 → 查单 → 交易所不认识 ⇒ 成交量确定为 0。"""
    cancel_resp = ok({"status": "ok", "data": {"errors": [], "successes": "1"}, "ts": FIXED_MS})
    info_resp = ok({"status": "ok", "data": [], "ts": FIXED_MS})
    handler = router({
        "/linear-swap-api/v1/swap_cross_cancel": cancel_resp,
        "/linear-swap-api/v1/swap_cross_order_info": info_resp,
    })

    async def main():
        async with adapter(handler) as ad:
            assert await ad.resolve_unknown_order("BTC-USDT", "ghost") == Decimal("0")
    run(main)


def test_query_order_converts_contracts_back_to_base_units():
    resp = ok({"status": "ok", "data": [{
        "order_id": 1, "order_id_str": "1", "client_order_id": 9527, "status": 4,
        "volume": 50, "trade_volume": 30, "trade_avg_price": "62963.4",
        "fee": "-0.0755", "fee_asset": "USDT"}], "ts": FIXED_MS})
    handler = router({"/linear-swap-api/v1/swap_cross_order_info": resp})

    async def main():
        async with adapter(handler) as ad:
            res = await ad.query_order("BTC-USDT", "9527")
            assert res.status == "partial" and res.is_terminal is False
            assert res.filled_qty == Decimal("0.030")     # 30 张 × 0.001
            assert res.avg_price == Decimal("62963.4")
            assert res.fee == Decimal("-0.0755")
    run(main)


# ---- 持仓 / 元数据 ------------------------------------------------------

def test_positions_are_netted_and_signed():
    resp = ok({"status": "ok", "data": [
        {"contract_code": "BTC-USDT", "volume": 50, "direction": "buy",
         "cost_open": "62000", "last_price": "63000", "lever_rate": 3},
        {"contract_code": "BTC-USDT", "volume": 20, "direction": "sell",
         "cost_open": "64000", "last_price": "63000", "lever_rate": 3},
        {"contract_code": "SHIB-USDT", "volume": 7, "direction": "sell",
         "cost_open": "0.000012", "last_price": "0.000013", "lever_rate": 5},
    ], "ts": FIXED_MS})
    handler = router({"/linear-swap-api/v1/swap_cross_position_info": resp})

    async def main():
        async with adapter(handler) as ad:
            pos = {p.symbol: p for p in await ad.fetch_positions()}
            # 对冲模式下同一合约会有多空两条，净额才是真实敞口
            assert pos["BTC-USDT"].qty == Decimal("0.030")
            assert pos["SHIB-USDT"].qty == Decimal("-7000")     # 空头为负
            assert pos["SHIB-USDT"].leverage == Decimal("5")
            # v1 全仓持仓接口不给强平价，宁可 None 也不编一个假的
            assert pos["BTC-USDT"].liquidation_price is None
    run(main)


def test_fee_schedule_takes_the_worse_of_open_and_close():
    resp = ok({"status": "ok", "data": [{
        "contract_code": "BTC-USDT", "open_maker_fee": "0.0002", "open_taker_fee": "0.0004",
        "close_maker_fee": "0.0003", "close_taker_fee": "0.0005", "fee_asset": "USDT"}],
        "ts": FIXED_MS})
    handler = router({"/linear-swap-api/v1/swap_fee": resp})

    async def main():
        async with adapter(handler) as ad:
            fees = await ad.fetch_fee_schedule(["BTC-USDT"])
            assert len(fees) == 1
            assert fees[0].maker == Decimal("0.0003")   # 开平费率不同，取较大值
            assert fees[0].taker == Decimal("0.0005")
            assert fees[0].market == "htx:perp"
    run(main)


def test_funding_limits_returns_cap_then_floor():
    resp = ok({"status": "ok", "data": [{
        "contract_code": "BTC-USDT", "funding_rate_cap": "0.00375",
        "funding_rate_floor": "-0.00375", "settle_period": 8,
        "min_level": 1, "max_level": 200}], "ts": FIXED_MS})
    handler = router({"/linear-swap-api/v1/swap_query_elements": resp})

    async def main():
        async with adapter(handler) as ad:
            assert await ad.fetch_funding_limits("BTC-USDT") == (
                Decimal("0.00375"), Decimal("-0.00375"))
            tiers = await ad.fetch_leverage_tiers("BTC-USDT")
            assert tiers[0][1] == Decimal("200")
    run(main)


def test_book_top_converts_contracts_to_base_and_uses_risk_free_quota():
    resp = ok({"ch": "market.BTC-USDT.depth.step0", "status": "ok", "ts": FIXED_MS,
               "tick": {"bids": [["62963.4", 38]], "asks": [["62963.5", 205]],
                        "ts": FIXED_MS, "version": 1}})
    handler = router({"/linear-swap-ex/market/depth": resp})

    async def main():
        async with adapter(handler) as ad:
            top = await ad.fetch_book_top("BTC-USDT")
            assert top.bid_price == Decimal("62963.4")
            assert top.bid_qty == Decimal("0.038")      # 38 张 × 0.001
            assert top.ask_qty == Decimal("0.205")
            assert top.ts_ms == FIXED_MS
    run(main)


# ---- 多档深度 -----------------------------------------------------------

# 深度响应同样写成 JSON **文本**：档位价格要参与带宽边界比较，
# 经 float 中转会在 1e-9 量级（SHIB/PEPE 的 price_tick）上把边界判反。
#
# BTC-USDT: contract_size=0.001，中价 10001，10bp 带宽 = [9990.999, 10011.001]。
# 带外那两档故意堆了 999999 张（≈100 亿名义额），一旦算进去断言必炸。
BTC_DEPTH = """
{"ch":"market.BTC-USDT.depth.step0","status":"ok","ts":1494515970000,
 "tick":{"id":1,"version":1,"ts":1494515970000,
  "bids":[[10000.0,100],[9995.0,200],[9991.5,300],[9990.0,999999],[9980.0,999999]],
  "asks":[[10002.0,150],[10008.0,250],[10010.0,350],[10012.0,999999],[10020.0,999999]]}}
"""

# SHIB-USDT: contract_size=1000（HTX 不做 1000SHIB 这种符号，倍数全藏在张里）。
# 中价 0.000012001，10bp 带宽 = [0.000011988999, 0.000012013001]。
SHIB_DEPTH = """
{"ch":"market.SHIB-USDT.depth.step0","status":"ok","ts":1494515970000,
 "tick":{"id":2,"version":2,"ts":1494515970000,
  "bids":[[0.000012,500],[0.000011995,700],[0.00001199,900],[0.000011985,999999999]],
  "asks":[[0.000012002,600],[0.000012008,800],[0.00001201,1000],[0.00001202,999999999]]}}
"""

# 只有两档的薄簿：整簿都落在带内，levels_used 必须等于返回的总档数
THIN_DEPTH = """
{"ch":"market.BTC-USDT.depth.step0","status":"ok","ts":1494515970000,
 "tick":{"id":3,"version":3,"ts":1494515970000,
  "bids":[[10000.0,40],[9990.0,60]],"asks":[[10010.0,30],[10030.0,70]]}}
"""

# 价差(≈1%)比带宽(10bp)还宽：一档都落不进带内
WIDE_SPREAD_DEPTH = """
{"ch":"market.BTC-USDT.depth.step0","status":"ok","ts":1494515970000,
 "tick":{"id":4,"version":4,"ts":1494515970000,
  "bids":[[10000.0,100],[9990.0,200]],"asks":[[10100.0,150],[10110.0,250]]}}
"""


def depth_router(by_symbol: dict, calls: list | None = None):
    """深度端点按 contract_code 分发原始 JSON 文本。"""
    def depth(request: httpx.Request) -> httpx.Response:
        code = request.url.params["contract_code"]
        return httpx.Response(200, text=by_symbol[code])
    return router({"/linear-swap-ex/market/depth": depth}, calls)


def test_book_depth_sums_band_and_ignores_levels_outside_it():
    """带宽内累计正确，带宽外的档位一分都不能计入。

    手算（张 × contract_size=0.001 换成币再乘价）：
      买：10000×100 + 9995×200 + 9991.5×300 = 5,996,450 张·U → 5996.45 U
      卖：10002×150 + 10008×250 + 10010×350 = 7,505,800 张·U → 7505.80 U
    9990.0 / 9980.0 / 10012.0 / 10020.0 四档在带外，各揣着 999999 张。
    """
    calls: list[httpx.Request] = []
    handler = depth_router({"BTC-USDT": BTC_DEPTH}, calls)

    async def main():
        async with adapter(handler) as ad:
            d = await ad.fetch_book_depth("BTC-USDT", band_bp=10.0)
            assert d.bid_notional == Decimal("5996.45")
            assert d.ask_notional == Decimal("7505.80")
            assert d.levels_used == 6                 # 两侧各 3 档，第 4 档起在带外
            assert d.band_bp == 10.0
            # 一档信息仍然要给，且量是币不是张
            assert d.bid_price == Decimal("10000.0")
            assert d.bid_qty == Decimal("0.1")        # 100 张 × 0.001
            assert d.ask_qty == Decimal("0.15")
            assert d.symbol == "BTC-USDT" and d.ts_ms == FIXED_MS
            # 对冲要两个方向都吃得下，容量按短板算
            assert d.thinner_side == Decimal("5996.45")
            assert isinstance(d.bid_notional, Decimal)
    run(main)

    req = [c for c in calls if c.url.path == "/linear-swap-ex/market/depth"][0]
    # HTX 没有档位数参数，只能靠 type 选：step0 = 不合并，档数最多
    assert req.url.params["type"] == "step0"
    assert req.url.params["contract_code"] == "BTC-USDT"


def test_book_depth_band_is_symmetric_around_mid():
    """带宽外的档位不计入 —— 把带宽收窄到只剩一档，量必须同步塌下来。

    band=1bp ⇒ [9999.9990, 10002.0010]，买侧只剩 10000.0、卖侧只剩 10002.0。
    """
    handler = depth_router({"BTC-USDT": BTC_DEPTH})

    async def main():
        async with adapter(handler) as ad:
            d = await ad.fetch_book_depth("BTC-USDT", band_bp=1.0)
            assert d.bid_notional == Decimal("1000")      # 10000×100×0.001
            assert d.ask_notional == Decimal("1500.3")    # 10002×150×0.001
            assert d.levels_used == 2
    run(main)


def test_book_depth_converts_contract_size_to_base():
    """张 → 币换算：SHIB 一张 = 1000 币，漏乘就把名义额低估 1000 倍。

    手算：买 (0.000012×500 + 0.000011995×700 + 0.00001199×900) × 1000 = 25.1875 U
          卖 (0.000012002×600 + 0.000012008×800 + 0.00001201×1000) × 1000 = 28.8176 U
    """
    handler = depth_router({"SHIB-USDT": SHIB_DEPTH})

    async def main():
        async with adapter(handler) as ad:
            d = await ad.fetch_book_depth("SHIB-USDT", band_bp=10.0)
            assert d.bid_notional == Decimal("25.1875")
            assert d.ask_notional == Decimal("28.8176")
            # 漏乘 contract_size 的话这两个数会是 0.0251875 / 0.0288176
            assert d.bid_notional > Decimal("1")
            assert d.levels_used == 6
            assert d.bid_qty == Decimal("500000")     # 500 张 × 1000
            assert d.ask_qty == Decimal("600000")
    run(main)


def test_book_depth_reports_levels_used_when_book_is_thin():
    """档位不足时如实反映：整簿都在带内 ⇒ levels_used 等于返回的总档数，
    再放宽带宽也变不出量来（绝不外推补足）。"""
    handler = depth_router({"BTC-USDT": THIN_DEPTH})

    async def main():
        async with adapter(handler) as ad:
            d = await ad.fetch_book_depth("BTC-USDT", band_bp=500.0)
            assert d.levels_used == 4                 # 交易所总共就返回了 4 档
            assert d.bid_notional == Decimal("999.4")     # (10000×40+9990×60)×0.001
            assert d.ask_notional == Decimal("1002.4")    # (10010×30+10030×70)×0.001
            wider = await ad.fetch_book_depth("BTC-USDT", band_bp=1000.0)
            assert wider.levels_used == 4
            assert wider.bid_notional == d.bid_notional
            assert wider.ask_notional == d.ask_notional
    run(main)


def test_book_depth_is_zero_when_spread_exceeds_band():
    """价差比带宽还宽 ⇒ 带内一档都没有。如实返回 0，不拿一档兜底充数。"""
    handler = depth_router({"BTC-USDT": WIDE_SPREAD_DEPTH})

    async def main():
        async with adapter(handler) as ad:
            d = await ad.fetch_book_depth("BTC-USDT", band_bp=10.0)
            assert d.bid_notional == Decimal("0") and d.ask_notional == Decimal("0")
            assert d.levels_used == 0
            # 一档价量照给，scoring 还能靠半价差算滑点
            assert d.bid_price == Decimal("10000.0") and d.ask_price == Decimal("10100.0")
            assert d.bid_qty == Decimal("0.1")
    run(main)


def test_book_depth_rejects_non_positive_band():
    handler = depth_router({"BTC-USDT": BTC_DEPTH})

    async def main():
        async with adapter(handler) as ad:
            with pytest.raises(ValueError):
                await ad.fetch_book_depth("BTC-USDT", band_bp=0.0)
    run(main)


def test_book_depth_empty_side_raises():
    """单边空簿算不出中价，和 fetch_book_top 同一口径直接抛。"""
    empty = """{"ch":"market.BTC-USDT.depth.step0","status":"ok","ts":1494515970000,
                "tick":{"id":5,"version":5,"ts":1494515970000,
                        "bids":[],"asks":[[10002.0,150]]}}"""
    handler = depth_router({"BTC-USDT": empty})

    async def main():
        async with adapter(handler) as ad:
            with pytest.raises(ExchangeError) as ei:
                await ad.fetch_book_depth("BTC-USDT")
            assert ei.value.code == "EMPTY_BOOK"
    run(main)


def test_set_leverage_is_remembered_for_subsequent_orders():
    calls: list[httpx.Request] = []
    handler = router({
        "/linear-swap-api/v1/swap_cross_switch_lever_rate":
            ok({"status": "ok", "data": {"contract_code": "BTC-USDT", "lever_rate": 5},
                "ts": FIXED_MS}),
        ORDER_PATH: ok(ORDER_OK),
    }, calls)

    async def main():
        async with adapter(handler) as ad:
            await ad.set_leverage("BTC-USDT", Decimal("5"))
            await ad.place_order(OrderRequest(
                symbol="BTC-USDT", side="buy", qty=Decimal("0.05"),
                price=Decimal("62963.4"), client_order_id="lev"))
    run(main)
    # 下单的 lever_rate 必须和持仓杠杆一致，否则 HTX 直接拒单
    assert body_of([c for c in calls if c.url.path == ORDER_PATH][0])["lever_rate"] == 5


def test_open_orders_restore_original_client_order_id():
    """挂单里的 client_order_id 是数字，要能映射回工具自己的字符串 ID。"""
    calls: list[httpx.Request] = []
    handler = router({
        ORDER_PATH: ok(ORDER_OK),
        "/linear-swap-api/v1/swap_cross_openorders": lambda req: ok(
            {"status": "ok", "data": {"total_page": 1, "orders": [{
                "order_id_str": "77", "client_order_id":
                    body_of(calls[1])["client_order_id"],
                "status": 3, "volume": 50, "trade_volume": 0,
                "trade_avg_price": None}]}, "ts": FIXED_MS}),
    }, calls)

    async def main():
        async with adapter(handler) as ad:
            await ad.place_order(OrderRequest(
                symbol="BTC-USDT", side="buy", qty=Decimal("0.05"),
                price=Decimal("62963.4"), client_order_id="cf-leg-long"))
            orders = await ad.fetch_open_orders("BTC-USDT")
            assert len(orders) == 1
            assert orders[0].client_order_id == "cf-leg-long"
            assert orders[0].status == "new" and orders[0].filled_qty == Decimal("0")
    run(main)


class _FakeWs:
    """假 WS 连接：喂 GZIP 帧、记录客户端回包。"""

    def __init__(self, frames):
        self.frames = frames
        self.sent: list[dict] = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        for frame in self.frames:
            yield frame


def test_stream_book_top_handles_gzip_and_heartbeat(monkeypatch):
    """HTX WS 三件套：全程 GZIP、{"ping"}→{"pong"}、量的单位是张。"""
    import gzip as _gzip
    import sys
    import types

    frames = [
        _gzip.compress(json.dumps({"ping": 18212558000}).encode()),
        _gzip.compress(json.dumps({
            "ch": "market.BTC-USDT.bbo", "ts": FIXED_MS,
            "tick": {"bid": [62963.4, 38], "ask": [62963.5, 205],
                     "ts": FIXED_MS, "ch": "market.BTC-USDT.bbo"}}).encode()),
    ]
    ws = _FakeWs(frames)
    fake = types.ModuleType("websockets")
    fake.connect = lambda url, **kw: ws          # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "websockets", fake)

    async def main():
        async with adapter(router({})) as ad:
            stream = ad.stream_book_top(["BTC-USDT"])
            top = await stream.__anext__()
            await stream.aclose()
            assert top.bid_price == Decimal("62963.4")
            assert top.bid_qty == Decimal("0.038")     # 38 张 × contract_size
            assert top.ask_qty == Decimal("0.205")
    run(main)

    assert ws.sent[0] == {"sub": "market.BTC-USDT.bbo", "id": "carryfarm-0"}
    # 心跳必须原样回填 ts，连续 5 次不回就被断连
    assert {"pong": 18212558000} in ws.sent


def test_spot_and_margin_are_explicitly_unimplemented():
    for kind in (MarketKind.SPOT, MarketKind.MARGIN):
        with pytest.raises(NotImplementedError):
            HtxAdapter(CRED, kind=kind)


def test_isolated_margin_mode_switches_endpoint_family():
    calls: list[httpx.Request] = []
    handler = router({"/linear-swap-api/v1/swap_order": ok(ORDER_OK)}, calls)

    async def main():
        async with adapter(handler, margin_mode="isolated") as ad:
            await ad.place_order(OrderRequest(
                symbol="BTC-USDT", side="buy", qty=Decimal("0.05"),
                price=Decimal("62963.4"), client_order_id="iso"))
    run(main)
    assert any(c.url.path == "/linear-swap-api/v1/swap_order" for c in calls)
