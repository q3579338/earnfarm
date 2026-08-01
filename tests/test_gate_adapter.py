"""Gate 适配器测试。全部走 httpx.MockTransport，一次都不连交易所。

金标准取自 docs/research/exchange-gate.io.md：
  - 签名串 = METHOD\\nPATH\\nQUERY\\nhex(SHA512(body))\\nTIMESTAMP，PATH 含 /api/v4
  - 空 body 哈希常量 cf83e1...da3e
  - 文档给的 curl 示例（POST /api/v4/futures/usdt/orders + X-Gate-Channel-Id）
Gate 没有公开"给定 key/secret 的签名值"示例，所以最强的校验做法是在 handler 里
用**真正上线的请求字节**重算一遍签名，去比对 SIGN header —— 这能同时证明
body 哈希用的是 httpx 实际发出的字节、query 用的是原始未编码串。
"""

from __future__ import annotations

import asyncio
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
from carryfarm.exchanges.gate import (
    EMPTY_BODY_SHA512,
    GateAdapter,
    _ContractMeta,
)
from carryfarm.models import MarketKind

API_KEY = "gate-key-0001"
API_SECRET = "gate-secret-0002"

# 调研文档里的空 body SHA-512 金标准常量
DOC_EMPTY_BODY_SHA512 = (
    "cf83e1357eefb8bdf1542850d66d8007d620e4050b5715dc83f4a921d36ce9ce"
    "47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e"
)

BTC_CONTRACT = {
    "name": "BTC_USDT", "type": "direct", "contract_type": "",
    "quanto_multiplier": "0.0001", "order_price_round": "0.1",
    "mark_price_round": "0.1", "order_size_min": 1, "order_size_max": 12000000,
    "leverage_max": "125", "maker_fee_rate": "-0.0001", "taker_fee_rate": "0.00075",
    "funding_interval": 28800, "funding_offset": 0,
    "funding_next_apply": 1785571200, "funding_rate": "0.000034",
    "funding_rate_indicative": "0.000034", "funding_rate_limit": "0.003",
    "in_delisting": False, "enable_decimal": False,
}
# 4 小时结算的山寨合约：REST /tickers 不带 funding_interval，必须从这里 join
DOGE_CONTRACT = {
    "name": "DOGE_USDT", "type": "direct", "contract_type": "",
    "quanto_multiplier": "10", "order_price_round": "0.00001",
    "order_size_min": 1, "order_size_max": 5000000,
    "leverage_max": "50", "funding_interval": 14400, "funding_offset": 0,
    "funding_next_apply": 1785571200, "funding_rate": "-0.00125",
    "funding_rate_indicative": "-0.0013", "funding_rate_limit": "0.02",
    "in_delisting": False, "enable_decimal": False,
}


def run(coro):
    return asyncio.run(coro)


def expected_sign(method: str, path: str, query: str, body: bytes, ts: str) -> str:
    """按调研文档的 5 段式独立重算一遍签名。"""
    body_hash = hashlib.sha512(body).hexdigest() if body else DOC_EMPTY_BODY_SHA512
    payload = "\n".join((method.upper(), path, query, body_hash, ts))
    return hmac.new(API_SECRET.encode(), payload.encode(), hashlib.sha512).hexdigest()


def json_response(payload, status: int = 200, headers: dict | None = None) -> httpx.Response:
    h = {"X-Gate-RateLimit-Requests-Remain": "199"}
    h.update(headers or {})
    return httpx.Response(status, json=payload, headers=h)


class Recorder:
    """记录所有出站请求，供断言用。"""

    def __init__(self, routes: dict, default=None):
        self.routes = routes
        self.default = default
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        handler = self.routes.get(key, self.default)
        if handler is None:
            raise AssertionError(f"未预期的请求: {request.method} {request.url}")
        if callable(handler):
            return handler(request)
        return json_response(handler)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


def make_adapter(recorder: Recorder, broker_code: str = "", **kw) -> GateAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder),
                               timeout=httpx.Timeout(1.0))
    ad = GateAdapter(Credential(API_KEY, API_SECRET), broker_code=broker_code,
                     client=client, **kw)
    # 跳过自动校时，免得每个用例都要伺候 /spot/time
    ad._clock.synced_at = time.time()
    ad._meta["BTC_USDT"] = _ContractMeta(BTC_CONTRACT)
    ad._meta["DOGE_USDT"] = _ContractMeta(DOGE_CONTRACT)
    return ad


@contextlib.contextmanager
def adapter(recorder: Recorder, **kw):
    ad = make_adapter(recorder, **kw)
    try:
        yield ad
    finally:
        run(ad.close())


def order_req(**kw) -> OrderRequest:
    base = dict(symbol="BTC_USDT", side="buy", qty=Decimal("0.55"),
                price=Decimal("62900.1"), time_in_force="IOC",
                client_order_id="t-leg1-0001")
    base.update(kw)
    return OrderRequest(**base)


ORDER_OK = {
    "id": 987654321, "contract": "BTC_USDT", "size": 5500, "left": 0,
    "price": "62900.1", "fill_price": "62900.2", "text": "t-leg1-0001",
    "status": "finished", "finish_as": "filled", "tif": "ioc",
    "mkfr": "-0.0001", "tkfr": "0.0005",
}


# ---- 签名 ---------------------------------------------------------------

def test_empty_body_hash_matches_doc_constant():
    """空 body 哈希必须等于文档给的常量，否则所有无 body 的签名全错。"""
    assert EMPTY_BODY_SHA512 == DOC_EMPTY_BODY_SHA512
    assert len(EMPTY_BODY_SHA512) == 128


def test_sign_string_layout_matches_doc_example():
    """按文档 curl 示例逐段核对签名串结构。"""
    rec = Recorder({})
    with adapter(rec) as ad:
        body_raw = json.dumps(
            {"contract": "BTC_USDT", "size": 10, "price": "62900.1",
             "tif": "gtc", "text": "t-leg1-0001", "reduce_only": False},
            ensure_ascii=False, separators=(",", ":"))
        payload = ad._sign_payload("POST", "/api/v4/futures/usdt/orders", "",
                                   body_raw, "1785548892")
        parts = payload.split("\n")
        assert len(parts) == 5
        assert parts[0] == "POST"
        assert parts[1] == "/api/v4/futures/usdt/orders"   # PATH 必须含 /api/v4
        assert parts[2] == ""                              # 无 query 时是空串
        assert parts[3] == hashlib.sha512(body_raw.encode()).hexdigest()
        assert parts[4] == "1785548892"                    # 秒，不是毫秒
        # 空 body 走常量
        empty = ad._sign_payload("GET", "/api/v4/futures/usdt/positions",
                                 "holding=true", "", "1785548892")
        assert empty.split("\n")[3] == DOC_EMPTY_BODY_SHA512
        # HMAC 必须是 SHA-512（128 位十六进制小写），不是基类那个 SHA-256
        sig = ad._hmac_sha512_hex(payload)
        assert len(sig) == 128 and sig == sig.lower()
        assert sig == hmac.new(API_SECRET.encode(), payload.encode(),
                               hashlib.sha512).hexdigest()


def test_signed_post_signature_matches_wire_bytes():
    """用真正发出去的 body 字节重算签名，必须和 SIGN header 一致。

    这同时证明了 body 哈希用的是 httpx 实际序列化的字节
    （httpx 用 separators=(",",":") 且 ensure_ascii=False），不是另一份 dumps。
    """
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["sign"] = request.headers["SIGN"]
        seen["expected"] = expected_sign(
            request.method, request.url.path, request.url.query.decode(),
            request.content, request.headers["Timestamp"])
        return json_response(ORDER_OK)

    rec = Recorder({("POST", "/api/v4/futures/usdt/orders"): handler})
    with adapter(rec) as ad:
        run(ad.place_order(order_req()))

    assert seen["sign"] == seen["expected"]
    req = rec.last
    assert req.headers["KEY"] == API_KEY
    assert req.headers["Timestamp"].isdigit()
    # 秒级时间戳（10 位），发成毫秒会拿到 REQUEST_EXPIRED
    assert len(req.headers["Timestamp"]) == 10
    assert req.headers["Content-Type"] == "application/json"
    # 迟到的下单请求要由服务端拒掉，而不是过期后照样成交
    assert int(req.headers["x-gate-exptime"]) > int(req.headers["Timestamp"]) * 1000


def test_signed_get_query_is_raw_and_order_sensitive():
    """query 进签名串时是**原始未编码**串，且顺序与 URL 一致。"""
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.query.decode()
        assert query == "contract=BTC_USDT&status=open", query
        assert request.headers["SIGN"] == expected_sign(
            "GET", "/api/v4/futures/usdt/orders", query, b"",
            request.headers["Timestamp"])
        return json_response([])

    rec = Recorder({("GET", "/api/v4/futures/usdt/orders"): handler})
    with adapter(rec) as ad:
        run(ad.fetch_open_orders("BTC_USDT"))
    assert len(rec.requests) == 1


def test_set_leverage_params_go_in_query_not_body():
    """路径含 positions 的 POST：参数走 query，body 为空（哈希是空串常量）。
    签成 JSON body 会直接 INVALID_SIGNATURE。"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.content == b"", request.content
        assert request.url.query.decode() == "leverage=10"
        assert "Content-Type" not in request.headers or \
            request.headers.get("Content-Type") != "application/json"
        assert request.headers["SIGN"] == expected_sign(
            "POST", "/api/v4/futures/usdt/positions/BTC_USDT/leverage",
            "leverage=10", b"", request.headers["Timestamp"])
        return json_response({"leverage": "10"})

    rec = Recorder({
        ("POST", "/api/v4/futures/usdt/positions/BTC_USDT/leverage"): handler})
    with adapter(rec) as ad:
        run(ad.set_leverage("BTC_USDT", Decimal("10")))
    assert len(rec.requests) == 1        # 确认 handler 里的断言真的跑过


def test_set_leverage_cross_mode_uses_zero_plus_limit():
    """全仓：leverage 必须是 0，真实倍数放 cross_leverage_limit。"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.query.decode() == "leverage=0&cross_leverage_limit=5"
        return json_response({})

    rec = Recorder({
        ("POST", "/api/v4/futures/usdt/positions/BTC_USDT/leverage"): handler})
    with adapter(rec, cross_margin=True) as ad:
        run(ad.set_leverage("BTC_USDT", Decimal("5")))


# ---- 返佣码 -------------------------------------------------------------

def test_broker_code_empty_leaves_no_trace():
    """默认不填返佣码时，请求里不能有任何相关痕迹。"""
    rec = Recorder({("POST", "/api/v4/futures/usdt/orders"): ORDER_OK})
    with adapter(rec, broker_code="") as ad:
        run(ad.place_order(order_req()))
    req = rec.last
    header_names = {k.lower() for k in req.headers.keys()}
    assert "x-gate-channel-id" not in header_names
    body = json.loads(req.content)
    assert "channel" not in body and "channel_id" not in body
    # 返佣码也不该混进 text（塞进去也拿不到返佣，还会污染幂等 id）
    assert body["text"] == "t-leg1-0001"
    assert b"channel" not in req.content.lower()


def test_broker_code_goes_in_header_only():
    """非空时按本家机制：X-Gate-Channel-Id header，不进 body，不进订单 text。"""
    rec = Recorder({("POST", "/api/v4/futures/usdt/orders"): ORDER_OK})
    with adapter(rec, broker_code="myfundingbot") as ad:
        run(ad.place_order(order_req()))
    req = rec.last
    assert req.headers["X-Gate-Channel-Id"] == "myfundingbot"
    body = json.loads(req.content)
    assert body["text"] == "t-leg1-0001"
    assert "myfundingbot" not in req.content.decode()


def test_broker_code_does_not_change_signature():
    """返佣码是纯 header，不进签名串——带不带码，同一时刻的 SIGN 必须一模一样。"""
    signs = []

    def handler(request: httpx.Request) -> httpx.Response:
        signs.append(request.headers["SIGN"])
        return json_response(ORDER_OK)

    rec = Recorder({("POST", "/api/v4/futures/usdt/orders"): handler})
    for code in ("", "myfundingbot"):
        with adapter(rec, broker_code=code) as ad:
            ad.timestamp_ms = lambda: 1785548892_000   # 冻结时间戳
            run(ad.place_order(order_req()))
    assert signs[0] == signs[1]


# ---- 资金费符号 ---------------------------------------------------------

def test_funding_sign_is_exchange_native():
    """交易所原始约定：正 = 多头付给空头。原样存，绝不取反。"""
    rec = Recorder({("GET", "/api/v4/futures/usdt/tickers"): [
        {"contract": "BTC_USDT", "funding_rate": "0.000034",
         "funding_rate_indicative": "0.000034", "mark_price": "62914.3"},
    ]})
    with adapter(rec) as ad:
        rates = run(ad.fetch_carry_rates(["BTC_USDT"]))
    assert len(rates) == 1
    r = rates[0]
    assert r.rate == Decimal("0.000034")          # 正的还是正的
    assert r.rate > 0
    assert r.annualized() > 0
    assert r.interval_s == 28800
    assert r.market == "gate:perp"


def test_negative_funding_stays_negative_and_interval_is_joined():
    """负费率保持负号；4h 合约的 interval 必须从 /contracts join 来，
    REST /tickers 不带 funding_interval，当 8h 用会低估 3 倍。"""
    rec = Recorder({("GET", "/api/v4/futures/usdt/tickers"): [
        {"contract": "DOGE_USDT", "funding_rate": "-0.00125",
         "funding_rate_indicative": "-0.0013"},
    ]})
    with adapter(rec) as ad:
        rates = run(ad.fetch_carry_rates(["DOGE_USDT"]))
    r = rates[0]
    assert r.rate == Decimal("-0.00125")
    assert r.rate < 0 and r.annualized() < 0
    assert r.interval_s == 14400                   # 不是默认的 28800
    assert r.annualized() == Decimal("-0.00125") * (Decimal(365 * 24 * 3600) / 14400)
    # funding_next_apply 是快照，过期后要被推到未来
    assert r.next_settle_ms > r.ts_ms


def test_funding_limits_are_symmetric_cap_floor():
    rec = Recorder({})
    with adapter(rec) as ad:
        cap, floor = run(ad.fetch_funding_limits("BTC_USDT"))
    assert cap == Decimal("0.003") and floor == Decimal("-0.003")


def test_funding_history_forces_to_and_clamps_lookback():
    """未文档化的坑：只传 from 不传 to，服务端把窗口静默夹成 30 天。
    另外回溯超过 180 天会 400，必须先夹住。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = dict(request.url.params)
        return json_response([
            {"r": "0.00002", "t": 1785542401},     # 服务端是 newest first
            {"r": "-0.00001", "t": 1785513601},
        ])

    rec = Recorder({("GET", "/api/v4/futures/usdt/funding_rate"): handler})
    with adapter(rec) as ad:
        now_s = ad.timestamp_ms() // 1000
        hist = run(ad.fetch_funding_history("BTC_USDT",
                                            since_ms=(now_s - 400 * 86400) * 1000,
                                            limit=5000))
    q = captured["query"]
    assert "to" in q, "必须显式传 to，否则窗口被夹成 30 天"
    assert int(q["limit"]) == 1000                       # limit 上限 1000
    assert now_s - int(q["from"]) <= 180 * 86400         # 回溯被夹在 180 天内
    # 返回必须升序，且费率符号原样
    assert [t for t, _ in hist] == sorted(t for t, _ in hist)
    assert hist[0][1] == Decimal("-0.00001")
    assert hist[-1][1] == Decimal("0.00002")


def test_funding_history_pages_backwards_for_sub_8h_contracts():
    """单次 1000 条封顶且保留最新端，所以 1h 合约请求长窗口会被**静默削短**：
    实测 BANK_USDT 要 180 天只拿回 161 天，调用方收不到任何提示。
    Gate 有 341 个 sub-8h 合约，必须按 to 向旧翻页把窗口补齐。"""
    windows: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        frm, to = int(q["from"]), int(q["to"])
        limit = int(q["limit"])
        windows.append((frm, to))
        # 每小时一条，服务端返回窗口内**最新**的 limit 条（newest first）
        newest = to - to % 3600
        rows = [{"r": "0.00001", "t": newest - 3600 * i} for i in range(limit)]
        rows = [r for r in rows if r["t"] >= frm]
        return json_response(rows)

    rec = Recorder({("GET", "/api/v4/futures/usdt/funding_rate"): handler})
    with adapter(rec) as ad:
        now_s = ad.timestamp_ms() // 1000
        hist = run(ad.fetch_funding_history("BANK_USDT",
                                            since_ms=(now_s - 90 * 86400) * 1000,
                                            limit=100))

    assert len(windows) > 1, "1h 合约 90 天 = 2160 条，一页 100 条必须翻页"
    # 每一页的右端都要比上一页更旧，否则就是在原地打转
    assert all(b[1] < a[1] for a, b in zip(windows, windows[1:]))
    assert [t for t, _ in hist] == sorted(t for t, _ in hist)
    assert len(set(t for t, _ in hist)) == len(hist), "翻页边界不能重复计一笔"
    # 拿回的窗口必须真的比单页深：单页只有 100 小时，翻页后应远超它
    span_h = (hist[-1][0] - hist[0][0]) / 3_600_000
    assert span_h > 100, f"窗口仍被削成 {span_h:.0f} 小时"


# ---- 精度 ---------------------------------------------------------------

def test_price_rounds_toward_marketability():
    """价格按 order_price_round 规整：买单向上、卖单向下。

    方向必须和 executor.cross_price 一致——保护限价是故意穿透对手价的，反向取整会把
    穿透削回来；tick 相对价格很粗的小币上那不是少赚一个 tick，而是挂不上、补不平。
    """
    tick = Decimal("0.1")
    assert GateAdapter.round_price(Decimal("62900.17"), tick, "buy") == Decimal("62900.2")
    assert GateAdapter.round_price(Decimal("62900.17"), tick, "sell") == Decimal("62900.1")
    # 定点输出，绝不能出现科学计数法
    doge_tick = Decimal("0.00001")
    px = GateAdapter.round_price(Decimal("0.000123456"), doge_tick, "buy")
    assert format(px, "f") == "0.00013"


def test_size_truncates_toward_zero_and_is_signed():
    """币数 → 张数向零取整；size 带符号，卖单为负，没有 side 字段。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return json_response({**ORDER_OK, "contract": captured["contract"],
                              "size": captured["size"], "left": 0})

    rec = Recorder({("POST", "/api/v4/futures/usdt/orders"): handler})
    with adapter(rec) as ad:
        # BTC: 0.55 币 / 0.0001 = 5500 张
        run(ad.place_order(order_req(qty=Decimal("0.55"))))
        assert captured["size"] == 5500
        assert "side" not in captured
        # DOGE: 27 币 / 10 = 2.7 张 → 向零取整 2 张，卖单取负
        run(ad.place_order(order_req(symbol="DOGE_USDT", side="sell",
                                     qty=Decimal("27"), price=Decimal("0.123456"),
                                     client_order_id="t-leg2-0001")))
        assert captured["size"] == -2
        # 价格同时被规整到 0.00001，且卖单向下
        assert captured["price"] == "0.12345"


def test_below_one_contract_is_rejected_locally():
    """取整后归零的单子本地就拒掉，别浪费一次网络往返和限频配额。"""
    rec = Recorder({})
    with adapter(rec) as ad:
        with pytest.raises(OrderRejected) as ei:
            run(ad.place_order(order_req(symbol="DOGE_USDT", qty=Decimal("3"))))
    assert ei.value.code == "PINCHED"
    assert not rec.requests


def test_market_order_is_price_zero_plus_ioc():
    """Gate 没有 type 字段：市价 = price "0" + tif ioc/fok；gtc 配 0 会被拒。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return json_response(ORDER_OK)

    rec = Recorder({("POST", "/api/v4/futures/usdt/orders"): handler})
    with adapter(rec) as ad:
        run(ad.place_order(order_req(price=None, time_in_force="GTC")))
    assert captured["price"] == "0"
    assert captured["tif"] == "ioc"


def test_book_top_sizes_are_converted_to_coins():
    """盘口档位量是张数，对外统一换成币（BTC 一张 0.0001 币）。"""
    rec = Recorder({("GET", "/api/v4/futures/usdt/order_book"): {
        "id": 1, "current": 1785548245.997, "update": 1785548245.976,
        "asks": [{"p": "62914.3", "s": "33644"}],
        "bids": [{"p": "62914.2", "s": "20884.5"}],
    }})
    with adapter(rec) as ad:
        top = run(ad.fetch_book_top("BTC_USDT"))
    assert top.bid_price == Decimal("62914.2")
    assert top.bid_qty == Decimal("20884.5") * Decimal("0.0001")
    assert top.ask_qty == Decimal("33644") * Decimal("0.0001")
    assert top.ts_ms == 1785548245997           # 秒级浮点 → 毫秒
    # 必须带上 size 小数 header，否则 Gate 把 20884.5 截成 20884
    assert rec.last.headers["X-Gate-Size-Decimal"] == "1"


# ---- 多档深度 -----------------------------------------------------------
#
# 一档当深度代理会把容量低估一两个数量级，真机会被误判成"做不了"。
# 下面的簿都是手算过的：中价、带宽边界、张→币换算全部可以逐项核对。

# mid = (62999.5 + 63000.5) / 2 = 63000
# 10bp 带宽 → lo = 63000*0.999 = 62937.0（含）, hi = 63000*1.001 = 63063.0（含）
BTC_DEPTH_BOOK = {
    "id": 42, "current": 1785548245.997, "update": 1785548245.976,
    "bids": [
        {"p": "62999.5", "s": "20000"},     # 一档
        {"p": "62980.0", "s": "30000"},
        {"p": "62937.0", "s": "10000"},     # 正好压在下边界上，含
        {"p": "62936.9", "s": "999999"},    # 带外：误算进来会差一个数量级
    ],
    "asks": [
        {"p": "63000.5", "s": "10000"},     # 一档
        {"p": "63010.0", "s": "25000"},
        {"p": "63063.0", "s": "5000"},      # 正好压在上边界上，含
        {"p": "63063.1", "s": "999999"},    # 带外
    ],
}


def test_book_depth_accumulates_notional_within_band():
    """带宽内逐档累计，名义额 = 价 * 张数 * quanto（BTC 0.0001 币/张）。"""
    rec = Recorder({("GET", "/api/v4/futures/usdt/order_book"): BTC_DEPTH_BOOK})
    with adapter(rec) as ad:
        d = run(ad.fetch_book_depth("BTC_USDT", band_bp=10.0))

    # 手算：62999.5*2 + 62980*3 + 62937*1 = 125999.0 + 188940.0 + 62937.0
    assert d.bid_notional == Decimal("377876.0")
    # 63000.5*1 + 63010*2.5 + 63063*0.5 = 63000.5 + 157525.0 + 31531.5
    assert d.ask_notional == Decimal("252057.0")
    assert d.levels_used == 6                       # 两侧各 3 档
    assert d.symbol == "BTC_USDT" and d.band_bp == 10.0
    # 一档信息与 fetch_book_top 同口径：价原样、量换成币
    assert d.bid_price == Decimal("62999.5")
    assert d.bid_qty == Decimal("2")                # 20000 张 * 0.0001
    assert d.ask_price == Decimal("63000.5")
    assert d.ask_qty == Decimal("1")
    assert d.ts_ms == 1785548245997
    # 全程 Decimal，一步都不许经过 float
    assert type(d.bid_notional) is Decimal and type(d.ask_notional) is Decimal
    # 深度必须比一档大得多，否则这个方法白加了
    assert d.bid_notional > d.bid_qty * d.bid_price


def test_book_depth_excludes_levels_outside_band():
    """带外档位一分都不能计入；边界档是**含**的。

    带外那两档各挂了 999999 张（约 63 亿 U），漏一档结果就差一个数量级——
    这正是"容量估算系统性偏低/偏高"最容易翻车的地方。
    """
    rec = Recorder({("GET", "/api/v4/futures/usdt/order_book"): BTC_DEPTH_BOOK})
    with adapter(rec) as ad:
        wide = run(ad.fetch_book_depth("BTC_USDT", band_bp=10.0))
        # 带宽收窄到 1bp：lo=62993.7 / hi=63006.3，两侧各只剩一档
        narrow = run(ad.fetch_book_depth("BTC_USDT", band_bp=1.0))

    assert wide.bid_notional == Decimal("377876.0")
    # 999999 张 * 0.0001 * 62936.9 ≈ 6.29e9，误收一档立刻看得出来
    assert wide.bid_notional < Decimal("1000000")
    assert wide.ask_notional < Decimal("1000000")

    # 1bp: lo = 63000*0.9999 = 62993.7, hi = 63006.3 —— 两侧各只剩一档
    assert narrow.levels_used == 2
    assert narrow.bid_notional == Decimal("62999.5") * Decimal("2")
    assert narrow.ask_notional == Decimal("63000.5") * Decimal("1")
    # 带宽变窄，深度只能变小，绝不可能变大
    assert narrow.bid_notional < wide.bid_notional


def test_book_depth_converts_contract_size_to_coins():
    """【坑】Gate 的档位量是**张数**。DOGE 一张 10 币，不换算就低估 10 倍。

    mid = (0.12344 + 0.12346)/2 = 0.12345
    10bp → lo = 0.12332655, hi = 0.12357345
    """
    rec = Recorder({("GET", "/api/v4/futures/usdt/order_book"): {
        "id": 7, "current": 1785548245.5,
        "bids": [{"p": "0.12344", "s": "1000"},
                 {"p": "0.12330", "s": "999999"}],     # < lo，带外
        "asks": [{"p": "0.12346", "s": "500"},
                 {"p": "0.12360", "s": "999999"}],     # > hi，带外
    }})
    with adapter(rec) as ad:
        d = run(ad.fetch_book_depth("DOGE_USDT", band_bp=10.0))

    # 0.12344 * (1000 张 * 10 币/张) = 1234.4；把张数当币数只会得到 123.44
    assert d.bid_notional == Decimal("1234.4")
    assert d.bid_notional != Decimal("123.44")
    assert d.ask_notional == Decimal("617.3")          # 0.12346 * 5000 币
    assert d.levels_used == 2
    assert d.bid_qty == Decimal("10000")               # 1000 张 * 10
    assert d.ask_qty == Decimal("5000")


def test_book_depth_reports_short_book_honestly_without_extrapolating():
    """簿里的档位盖不满带宽时，如实返回能统计到的量和档数——宁可低估也别编。

    1% 带宽在 BTC 上要几百档才盖得满，这里服务端只给了 3 档：
    结果必须**恰好**等于这 3 档，不能按簿密度外推补足。
    """
    short_book = {
        "id": 9, "current": 1785548245.0,
        "bids": [{"p": "62999.5", "s": "20000"}, {"p": "62990.0", "s": "10000"}],
        "asks": [{"p": "63000.5", "s": "10000"}],
    }
    rec = Recorder({("GET", "/api/v4/futures/usdt/order_book"): short_book})
    with adapter(rec) as ad:
        d = run(ad.fetch_book_depth("BTC_USDT", band_bp=100.0))

    # 全部 3 档都在 1% 带内，一档不多一档不少
    assert d.levels_used == 3
    assert d.bid_notional == Decimal("62999.5") * Decimal("2") + \
        Decimal("62990.0") * Decimal("1")
    assert d.ask_notional == Decimal("63000.5") * Decimal("1")
    # 带宽扩大 10 倍但簿没变 → 深度必须原地不动（外推的话这里会膨胀）
    with adapter(rec) as ad2:
        narrow = run(ad2.fetch_book_depth("BTC_USDT", band_bp=10.0))
    assert narrow.bid_notional == d.bid_notional
    assert narrow.levels_used == d.levels_used


def test_book_depth_request_asks_for_enough_levels():
    """取 1 档就退回原来的毛病。同时必须带 size 小数 header，
    否则 Gate 把 5116.8 张截成 5116（enable_decimal 合约上直接少算）。"""
    rec = Recorder({("GET", "/api/v4/futures/usdt/order_book"): BTC_DEPTH_BOOK})
    with adapter(rec) as ad:
        run(ad.fetch_book_depth("BTC_USDT"))
    q = dict(rec.last.url.params)
    assert q["contract"] == "BTC_USDT"
    assert int(q["limit"]) >= 20, "一档当深度代理正是要修的 bug"
    assert rec.last.headers["X-Gate-Size-Decimal"] == "1"
    assert "SIGN" not in rec.last.headers          # 公开端点，不必签名


def test_book_depth_uses_market_quota():
    """深度接口比 bookTicker 贵，但必须和行情共用 market 池，
    绝不能去占撤单/平仓的 risk 配额。"""
    rec = Recorder({("GET", "/api/v4/futures/usdt/order_book"): BTC_DEPTH_BOOK})

    async def scenario(ad):
        for _ in range(8):                          # 抽干 market 池
            await ad._quota["market"].acquire()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ad.fetch_book_depth("BTC_USDT"), timeout=0.2)
        for _ in range(8):
            ad._quota["market"].release()

    with adapter(rec) as ad:
        run(scenario(ad))


def test_book_depth_rejects_bad_band_and_empty_side():
    rec = Recorder({("GET", "/api/v4/futures/usdt/order_book"): {
        "id": 1, "current": 1785548245.0,
        "bids": [{"p": "62999.5", "s": "20000"}], "asks": []}})
    with adapter(rec) as ad:
        # 带宽非正 → 算不出带，本地拒掉，别浪费一次请求
        with pytest.raises(ValueError):
            run(ad.fetch_book_depth("BTC_USDT", band_bp=0.0))
        assert not rec.requests
        # 单边空簿算不出中价，和 fetch_book_top 同一口径
        with pytest.raises(ExchangeError) as ei:
            run(ad.fetch_book_depth("BTC_USDT"))
        assert ei.value.code == "EMPTY_BOOK" and ei.value.retriable


def test_book_depth_feeds_scoring_slippage_directly():
    """返回值要能直接喂 scoring.slippage_rate：
    depth_notional 大了之后，同样的仓位不再被当成"穿透整个簿"。"""
    from carryfarm.scoring import LegQuote, slippage_rate

    rec = Recorder({("GET", "/api/v4/futures/usdt/order_book"): BTC_DEPTH_BOOK})
    with adapter(rec) as ad:
        d = run(ad.fetch_book_depth("BTC_USDT", band_bp=10.0))

    def leg(depth: Decimal) -> LegQuote:
        return LegQuote(
            symbol=d.symbol, market="gate:perp",
            bid=float(d.bid_price), ask=float(d.ask_price),
            bid_size=float(d.bid_qty), ask_size=float(d.ask_qty),
            depth_notional=float(depth), depth_band_bp=d.band_bp,
            taker_fee=0.0005, maker_fee=0.0002,
            funding_now=0.0001, funding_interval_h=8.0, next_funding_ts=0.0,
        )

    notional = 100_000.0
    # 用一档名义额当深度代理（旧口径） vs 用 10bp 内的真实深度
    one_level = d.ask_price * d.ask_qty
    assert slippage_rate(leg(d.ask_notional), "buy", notional) < \
        slippage_rate(leg(one_level), "buy", notional)


def test_ws_book_ticker_parsing():
    """WS futures.book_ticker 的档位量同样是张数，且空串表示该边没量。
    （只测解析，不建真连接。）"""
    rec = Recorder({})
    with adapter(rec) as ad:
        top = ad._parse_book_ticker({
            "t": 1785548245214, "u": 120900607904, "s": "BTC_USDT",
            "b": "62914.2", "B": "20884", "a": "62914.3", "A": "33644"})
        assert top.bid_qty == Decimal("20884") * Decimal("0.0001")
        assert top.ts_ms == 1785548245214
        # 单边为空 → 这一拍不可用
        assert ad._parse_book_ticker({"s": "BTC_USDT", "b": "", "B": 0,
                                      "a": "62914.3", "A": "1"}) is None
        # 没有元数据的合约无法换算，直接丢弃
        assert ad._parse_book_ticker({"s": "UNKNOWN_USDT", "b": "1",
                                      "B": "1", "a": "2", "A": "1"}) is None


def test_positions_qty_is_signed_coins():
    rec = Recorder({("GET", "/api/v4/futures/usdt/positions"): [
        {"contract": "BTC_USDT", "size": -5500, "entry_price": "62900",
         "mark_price": "62914.3", "liq_price": "71000", "leverage": "10"},
        {"contract": "DOGE_USDT", "size": 300, "entry_price": "0.12",
         "mark_price": "0.121", "liq_price": "0", "leverage": "0",
         "cross_leverage_limit": "5"},
    ]})
    with adapter(rec) as ad:
        poss = run(ad.fetch_positions())
    btc, doge = poss
    assert btc.qty == Decimal("-0.55")           # 空头为负，单位是币
    assert btc.liquidation_price == Decimal("71000")
    assert doge.qty == Decimal("3000")           # 300 张 * 10 币
    assert doge.liquidation_price is None        # liq_price "0" 表示没有
    assert doge.leverage == Decimal("5")         # leverage "0" = 全仓，读 cross 限额


# ---- 未知状态与错误码 ---------------------------------------------------

def test_timeout_raises_order_unknown():
    """下单超时**必须**抛 OrderUnknown。当成失败返回会导致重发 → 双倍仓位。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated read timeout", request=request)

    rec = Recorder({("POST", "/api/v4/futures/usdt/orders"): handler})
    with adapter(rec) as ad:
        with pytest.raises(OrderUnknown) as ei:
            run(ad.place_order(order_req()))
    assert ei.value.client_order_id == "t-leg1-0001"
    assert ei.value.venue == "gate"


def test_connection_error_raises_order_unknown():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset", request=request)

    rec = Recorder({("POST", "/api/v4/futures/usdt/orders"): handler})
    with adapter(rec) as ad:
        with pytest.raises(OrderUnknown):
            run(ad.place_order(order_req()))


def test_server_error_raises_order_unknown_not_failure():
    """5xx 同样是"可能已进撮合"，不能当明确失败。"""
    rec = Recorder({("POST", "/api/v4/futures/usdt/orders"):
                    lambda r: json_response({"label": "SERVER_ERROR",
                                             "message": "boom"}, status=500)})
    with adapter(rec) as ad:
        with pytest.raises(OrderUnknown):
            run(ad.place_order(order_req()))


def test_request_expired_is_not_order_unknown():
    """时间戳过期是在**进撮合之前**被挡下的，订单确定不存在，
    不能升级成 OrderUnknown 白跑一轮 cancel+查单。"""
    rec = Recorder({("POST", "/api/v4/futures/usdt/orders"):
                    lambda r: json_response(
                        {"label": "REQUEST_EXPIRED",
                         "message": "gap between request Timestamp and server time"},
                        status=403)})
    with adapter(rec) as ad:
        # OrderUnknown 不是 ExchangeError 的子类，抛错了这里就接不住
        with pytest.raises(ExchangeError) as ei:
            run(ad.place_order(order_req()))
    assert ei.value.code == "REQUEST_EXPIRED"


def test_explicit_reject_is_not_order_unknown():
    """明确的业务拒绝必须是 OrderRejected，不能被误判成未知状态。"""
    rec = Recorder({("POST", "/api/v4/futures/usdt/orders"):
                    lambda r: json_response(
                        {"label": "ERR_QUOTA_LOWER_LIMIT",
                         "message": "Not meeting the minimum order amount"},
                        status=400)})
    with adapter(rec) as ad:
        with pytest.raises(OrderRejected) as ei:
            run(ad.place_order(order_req()))
    assert ei.value.code == "ERR_QUOTA_LOWER_LIMIT"
    assert ei.value.retriable is False


def test_order_not_found_maps_to_order_rejected():
    """resolve_unknown_order 靠 OrderRejected 判定"这单从未被接受"。"""
    rec = Recorder({("GET", "/api/v4/futures/usdt/orders/t-leg1-0001"):
                    lambda r: json_response({"label": "ORDER_NOT_FOUND",
                                             "message": "not found"}, status=404)})
    with adapter(rec) as ad:
        with pytest.raises(OrderRejected):
            run(ad.query_order("BTC_USDT", "t-leg1-0001"))


def test_finished_order_is_not_reported_as_never_accepted():
    """ORDER_FINISHED/CLOSED 说明这单**存在过、可能已成交**，和 ORDER_NOT_FOUND
    完全不是一回事。混成同一个 OrderRejected 桶，resolve_unknown_order 就会返回 0，
    一张真成交的单被报成"零成交"，上层照着 0 再补一次仓 = 双倍仓位。"""
    for label in ("ORDER_FINISHED", "ORDER_CLOSED", "ORDER_CANCELLED"):
        rec = Recorder({("GET", "/api/v4/futures/usdt/orders/t-leg1-0001"):
                        lambda r, lb=label: json_response({"label": lb,
                                                           "message": "gone"}, status=400)})
        with adapter(rec) as ad:
            with pytest.raises(ExchangeError) as ei:
                run(ad.query_order("BTC_USDT", "t-leg1-0001"))
        assert not isinstance(ei.value, OrderRejected)
        assert ei.value.code == label


def test_cancel_treats_already_terminal_as_done():
    """撤单撞上"已终结"就是撤单的目的已经达到——吞掉，让消解流程继续去查成交量。"""
    rec = Recorder({("DELETE", "/api/v4/futures/usdt/orders/t-leg1-0001"):
                    lambda r: json_response({"label": "ORDER_FINISHED",
                                             "message": "gone"}, status=400)})
    with adapter(rec) as ad:
        run(ad.cancel_order("BTC_USDT", "t-leg1-0001"))     # 不抛


@pytest.mark.parametrize("route", [
    lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("read timeout")),
    lambda r: json_response({"label": "SERVER_ERROR", "message": "boom"}, status=500),
])
def test_risk_path_errors_are_catchable_by_the_resolver(route):
    """base._request 把传输层异常包成 ExchangeError('NETWORK')，而
    base.resolve_unknown_order 只认 (OrderRejected, httpx.HTTPError, RateLimited)。"""
    rec = Recorder({("DELETE", "/api/v4/futures/usdt/orders/t-leg1-0001"): route})
    with adapter(rec) as ad:
        with pytest.raises(RateLimited) as ei:
            run(ad.cancel_order("BTC_USDT", "t-leg1-0001"))
        assert ei.value.retriable is True

    rec2 = Recorder({("GET", "/api/v4/futures/usdt/orders/t-leg1-0001"): route})
    with adapter(rec2) as ad2:
        with pytest.raises(RateLimited):
            run(ad2.query_order("BTC_USDT", "t-leg1-0001"))


def test_unreadable_200_body_is_order_unknown():
    """200 但响应体读不出来：以前一路返回 None，让 _parse_order(None) 抛 AttributeError。"""
    rec = Recorder({("POST", "/api/v4/futures/usdt/orders"):
                    lambda r: httpx.Response(200, content=b"<html>blocked</html>")})
    with adapter(rec) as ad:
        with pytest.raises(OrderUnknown):
            run(ad.place_order(order_req()))


def test_json_numeric_literals_never_become_float():
    """Gate 确实会返回裸 JSON 数字（size/left 是 int，current 是浮点秒）。"""
    rec = Recorder({("GET", "/api/v4/futures/usdt/order_book"): {
        "id": 1, "current": 1785548245.997, "update": 1785548245.976,
        "asks": [{"p": "62914.3", "s": 33644}], "bids": [{"p": "62914.2", "s": 12000}]}})
    with adapter(rec) as ad:
        top = run(ad.fetch_book_top("BTC_USDT"))
    assert type(top.bid_qty) is Decimal and top.bid_qty == Decimal("1.2")
    assert top.ts_ms == 1785548245997


def test_below_order_size_min_is_blocked_locally():
    """order_size_min > 1 的合约上，只 floor 到整张还不够，会撞 ERR_QUOTA_LOWER_LIMIT。"""
    rec = Recorder({})
    with adapter(rec) as ad:
        ad._meta["MIN_USDT"] = _ContractMeta({**DOGE_CONTRACT, "name": "MIN_USDT",
                                              "quanto_multiplier": "1",
                                              "order_size_min": "5"})
        with pytest.raises(OrderRejected) as ei:
            run(ad.place_order(order_req(symbol="MIN_USDT", qty=Decimal("3"),
                                         price=Decimal("1"))))
    assert ei.value.code == "ERR_QUOTA_LOWER_LIMIT"
    assert not rec.requests


def test_rate_limit_and_expired_translation():
    rec = Recorder({("GET", "/api/v4/futures/usdt/tickers"):
                    lambda r: json_response({"label": "TOO_MANY_REQUESTS",
                                             "message": "slow down"}, status=429)})
    with adapter(rec) as ad:
        with pytest.raises(RateLimited) as ei:
            run(ad.fetch_carry_rates(["BTC_USDT"]))
        assert ei.value.retriable is True

    rec2 = Recorder({("GET", "/api/v4/futures/usdt/tickers"):
                     lambda r: json_response({"label": "REQUEST_EXPIRED",
                                              "message": "gap exceeds 60"},
                                             status=403)})
    with adapter(rec2) as ad:
        with pytest.raises(ExchangeError) as ei:
            run(ad.fetch_carry_rates(["BTC_USDT"]))
        assert ei.value.code == "REQUEST_EXPIRED" and ei.value.retriable
        # 时钟被标记为需要重新校准，否则会一直撞同一堵墙
        assert ad._clock.is_stale


def test_cancel_then_query_resolves_unknown():
    """OrderUnknown 的消解流程：cancel 终结 → 查单读出真实成交量。"""
    def cancel(request: httpx.Request) -> httpx.Response:
        return json_response({**ORDER_OK, "status": "finished",
                              "finish_as": "cancelled", "left": 1500})

    def query(request: httpx.Request) -> httpx.Response:
        return json_response({**ORDER_OK, "status": "finished",
                              "finish_as": "cancelled", "size": 5500, "left": 1500})

    rec = Recorder({
        ("DELETE", "/api/v4/futures/usdt/orders/t-leg1-0001"): cancel,
        ("GET", "/api/v4/futures/usdt/orders/t-leg1-0001"): query,
    })
    with adapter(rec) as ad:
        filled = run(ad.resolve_unknown_order("BTC_USDT", "t-leg1-0001"))
    # 5500-1500=4000 张 * 0.0001 = 0.4 币
    assert filled == Decimal("0.4")


def test_risk_path_uses_risk_quota():
    """撤单/查仓/查挂单必须走 risk 配额，不能和行情轮询抢。"""
    rec = Recorder({
        ("GET", "/api/v4/futures/usdt/positions"): [],
        ("GET", "/api/v4/futures/usdt/tickers"): [
            {"contract": "BTC_USDT", "funding_rate": "0.0001"}],
    })

    async def scenario(ad):
        # 把 risk 池抽干：风控调用应当阻塞，行情调用不受影响
        for _ in range(6):
            await ad._quota["risk"].acquire()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ad.fetch_positions(), timeout=0.2)
        # 正向对照：同一时刻 market 配额还在，行情照常
        rates = await asyncio.wait_for(ad.fetch_carry_rates(["BTC_USDT"]),
                                       timeout=0.5)
        assert rates and rates[0].symbol == "BTC_USDT"
        for _ in range(6):
            ad._quota["risk"].release()

    with adapter(rec) as ad:
        run(scenario(ad))


# ---- 订单 text / client_order_id ---------------------------------------

def test_client_order_id_is_mandatory():
    rec = Recorder({})
    with adapter(rec) as ad:
        with pytest.raises(ValueError):
            run(ad.place_order(order_req(client_order_id="")))
    assert not rec.requests


def test_text_gets_t_prefix_and_charset_guard():
    assert GateAdapter._gate_text("leg1-0001") == "t-leg1-0001"
    assert GateAdapter._gate_text("t-leg1-0001") == "t-leg1-0001"
    # 非法字符替换成 -
    assert GateAdapter._gate_text("leg#1@a") == "t-leg-1-a"
    # 超长必须报错而不是截断（截断会让两个订单撞成同一个幂等 id）
    with pytest.raises(ValueError):
        GateAdapter._gate_text("x" * 29)


def test_place_order_result_keeps_caller_id():
    rec = Recorder({("POST", "/api/v4/futures/usdt/orders"): ORDER_OK})
    with adapter(rec) as ad:
        res = run(ad.place_order(order_req()))
    assert res.client_order_id == "t-leg1-0001"
    assert res.status == "filled" and res.is_terminal
    assert res.filled_qty == Decimal("0.55")
    assert res.avg_price == Decimal("62900.2")
    assert res.exchange_order_id == "987654321"


def test_partially_filled_ioc_is_terminal():
    """ioc 部成后 finished：状态必须是终结态，成交量要带出来。"""
    rec = Recorder({("POST", "/api/v4/futures/usdt/orders"):
                    {**ORDER_OK, "status": "finished", "finish_as": "ioc",
                     "size": 5500, "left": 2000}})
    with adapter(rec) as ad:
        res = run(ad.place_order(order_req()))
    assert res.is_terminal
    assert res.filled_qty == Decimal("0.35")     # 3500 张 * 0.0001


# ---- 合约表 -------------------------------------------------------------

def test_instrument_mapping_and_pollution_filter():
    """合约表被污染：股票 perp、CJK 名字、下架合约必须滤掉；
    tick/lot/contract_size 按 Gate 的字段名映射。"""
    rec = Recorder({("GET", "/api/v4/futures/usdt/contracts"): [
        BTC_CONTRACT, DOGE_CONTRACT,
        {**BTC_CONTRACT, "name": "AAPLX_USDT", "contract_type": "stocks"},
        {**BTC_CONTRACT, "name": "币安人生_USDT"},      # CJK：会让 WS 整批订阅失败
        {**BTC_CONTRACT, "name": "OLD_USDT", "in_delisting": True},
    ]})
    with adapter(rec) as ad:
        insts = run(ad.fetch_instruments())
    names = [i.symbol for i in insts]
    assert names == ["BTC_USDT", "DOGE_USDT"]
    btc = insts[0]
    assert btc.tick_size == Decimal("0.1")           # order_price_round
    assert btc.contract_size == Decimal("0.0001")    # quanto_multiplier
    assert btc.lot_size == Decimal("0.0001")         # 币口径粒度
    assert btc.funding_interval_s == 28800
    assert btc.base == "BTC" and btc.quote == "USDT"
    doge = insts[1]
    assert doge.lot_size == Decimal("10")            # 10 币/张，残余 delta 的来源
    assert doge.funding_interval_s == 14400
    # Gate 没有 minNotional 字段，只能按价格现算
    assert btc.min_notional == Decimal("0")
    assert GateAdapter.min_notional_at(btc, Decimal("63000")) == Decimal("6.3")


def test_fee_schedule_uses_private_endpoint():
    """必须用私有 /fee（你的真实档位），不能用 /contracts 上的基础档。"""
    rec = Recorder({("GET", "/api/v4/futures/usdt/fee"): {
        "BTC_USDT": {"taker_fee": "0.0005", "maker_fee": "0.0002"}}})
    with adapter(rec) as ad:
        fees = run(ad.fetch_fee_schedule(["BTC_USDT"]))
    assert fees[0].taker == Decimal("0.0005")
    assert fees[0].maker == Decimal("0.0002")
    assert "SIGN" in rec.last.headers          # 私有端点，必须签名
    assert rec.last.url.query.decode() == "contract=BTC_USDT"


def test_leverage_tiers_sorted_ascending():
    rec = Recorder({("GET", "/api/v4/futures/usdt/risk_limit_tiers"): [
        {"tier": 2, "risk_limit": "5000000", "leverage_max": "50"},
        {"tier": 1, "risk_limit": "1000000", "leverage_max": "125"},
    ]})
    with adapter(rec) as ad:
        tiers = run(ad.fetch_leverage_tiers("BTC_USDT"))
    assert tiers == [(Decimal("1000000"), Decimal("125")),
                     (Decimal("5000000"), Decimal("50"))]


# ---- 未实现的市场 -------------------------------------------------------

def test_spot_and_margin_are_explicitly_unimplemented():
    """调研文档只覆盖 futures，现货/杠杆信息不足，必须显式拒绝而不是瞎猜。"""
    for kind in (MarketKind.SPOT, MarketKind.MARGIN):
        with pytest.raises(NotImplementedError):
            GateAdapter(Credential(API_KEY, API_SECRET), market_kind=kind)
    # 反向合约的 quanto_multiplier 是 "0"，币数换算不成立
    with pytest.raises(NotImplementedError):
        GateAdapter(Credential(API_KEY, API_SECRET), settle="btc")


def test_spot_market_buy_amount_is_quote_not_base():
    """【坑】Gate 现货市价买单的 amount 是要花的 U，不是要买的币。

    直接填币数不会报错，会变成量级完全错误的成交，所以口径必须先钉死。
    """
    assert GateAdapter.spot_market_buy_amount(Decimal("0.5"),
                                              Decimal("62900")) == Decimal("31450.0")
    with pytest.raises(ValueError):
        GateAdapter.spot_market_buy_amount(Decimal("0.5"), Decimal("0"))
