"""外部喂数缓存（浏览器取数 → 服务端缓存 → 适配器读缓存）的测试。

守住四条不能破的性质：
  1. 公开请求命中缓存时**一个 HTTP 请求都不发**（否则被封锁的家照样 451）；
  2. signed=True **永不**读缓存（缓存来自某一个访客，串号 = 把 A 的数据给 B）；
  3. TTL 过期后老老实实回落 HTTP（陈旧行情比没行情更危险）；
  4. **本地模式行为一字不变**：没人喂缓存 = 缓存全空 = 照旧直连。

外加钉死浏览器可达性表（2026-08-14 实测）：不在表里的家绝不会被浏览器取数。
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

import httpx
import pytest

from earnfarm.exchanges import base
from earnfarm.exchanges.base import (
    Credential,
    feed_public_cache,
    public_cache_age_s,
    public_cache_clear,
    public_cache_get,
    public_cache_key,
)
from earnfarm.exchanges.binance import BinanceAdapter
from earnfarm.exchanges.bybit import BybitAdapter
from earnfarm.exchanges.okx import OkxAdapter


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_cache():
    """每个用例前后都清空。TTL 是 120 秒，不清就会跨用例泄漏——
    那种失败只在整包跑时出现、单跑必过，最难查。"""
    public_cache_clear()
    yield
    public_cache_clear()


def counting_transport(routes: dict, hits: list):
    """记下每一次真实发出的请求。测"没发请求"只能靠数它。"""
    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(f"{request.method} {request.url.path}")
        payload = routes.get(request.url.path, {"code": 0, "msg": "ok"})
        return httpx.Response(200, json=payload)
    return handler


def binance_adapter(routes: dict, hits: list) -> BinanceAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        counting_transport(routes, hits)))
    ad = BinanceAdapter(Credential(api_key="k", api_secret="s"), client=client)
    ad._clock.synced_at = time.time()       # 别让校时混进请求计数
    return ad


def bybit_adapter(routes: dict, hits: list) -> BybitAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        counting_transport(routes, hits)))
    ad = BybitAdapter(Credential(api_key="k", api_secret="s"), client=client)
    ad._clock.synced_at = time.time()
    return ad


# ---- 缓存键 -------------------------------------------------------------

def test_key_includes_params_so_venues_do_not_collide():
    """params 必须编进键：Bybit 靠 category 区分 linear/inverse，
    只用路径当键两个市场会互相覆盖。"""
    linear = public_cache_key("GET", "/v5/market/tickers", {"category": "linear"})
    inverse = public_cache_key("GET", "/v5/market/tickers", {"category": "inverse"})
    assert linear != inverse
    assert linear == "GET /v5/market/tickers?category=linear"


def test_key_is_stable_across_int_and_str_params():
    """喂数侧写 1000、取数侧传 "1000" 也要落到同一个键，否则永远不命中。"""
    assert (public_cache_key("GET", "/v5/market/instruments-info",
                             {"category": "linear", "limit": 1000})
            == public_cache_key("GET", "/v5/market/instruments-info",
                                {"limit": "1000", "category": "linear"}))


def test_key_encodes_post_body_for_hyperliquid():
    """HL 所有请求都打同一个 POST /info，全靠 body 的 type 区分。"""
    meta = public_cache_key("POST", "/info", None, {"type": "meta", "dex": ""})
    ctxs = public_cache_key("POST", "/info", None,
                            {"type": "metaAndAssetCtxs", "dex": ""})
    assert meta != ctxs and meta is not None


def test_per_symbol_requests_are_never_cacheable():
    """按品种查的请求不进缓存：误命中会把 BTC 的答案发给全市场且不报错。"""
    assert public_cache_key("GET", "/fapi/v1/premiumIndex",
                            {"symbol": "BTCUSDT"}) is None
    assert public_cache_key("GET", "/api/v5/public/funding-rate",
                            {"instId": "BTC-USDT-SWAP"}) is None
    assert public_cache_key("GET", "/futures/usdt/tickers",
                            {"contract": "BTC_USDT"}) is None
    # 翻页参数同理：只缓存第一页既没意义又容易错位
    assert public_cache_key("GET", "/v5/market/instruments-info",
                            {"category": "linear", "cursor": "x"}) is None


def test_cache_is_namespaced_by_venue():
    """通用路径（/api/v1/markets 之类）谁都可能有，串号 = 拿别家合约表喂本家。"""
    feed_public_cache("backpack", "GET /api/v1/markets", [{"symbol": "BTC_USDC_PERP"}])
    assert public_cache_get("backpack", "GET /api/v1/markets") is not None
    assert public_cache_get("kucoin", "GET /api/v1/markets") is None


def test_age_and_clear():
    feed_public_cache("binance", "GET /x", {"a": 1})
    assert public_cache_age_s("binance", "GET /x") is not None
    public_cache_clear()
    assert public_cache_age_s("binance", "GET /x") is None


# ---- 命中即不发 HTTP -----------------------------------------------------

def test_cache_hit_sends_no_http_at_all():
    """核心性质：命中时一个字节都不发。被 451 封锁的家全靠这条活着。"""
    hits: list[str] = []
    ad = binance_adapter({}, hits)
    feed_public_cache("binance", "GET /fapi/v1/fundingInfo",
                      [{"symbol": "BTCUSDT", "fundingIntervalHours": 4}])
    data = run(ad._request("GET", "/fapi/v1/fundingInfo", quota="market"))
    assert data == [{"symbol": "BTCUSDT", "fundingIntervalHours": 4}]
    assert hits == []


def test_cache_hit_beats_clock_sync():
    """时钟陈旧时也不许发校时请求——校时本身也是一发会被拒的 HTTP。"""
    hits: list[str] = []
    ad = binance_adapter({}, hits)
    ad._clock.synced_at = 0.0                 # 陈旧，正常路径会先去校时
    feed_public_cache("binance", "GET /fapi/v1/fundingInfo", [])
    run(ad._request("GET", "/fapi/v1/fundingInfo", quota="market"))
    assert hits == []


def test_binance_carry_rates_served_entirely_from_cache():
    """整条取数路径（premiumIndex + fundingInfo）都走缓存，零请求出榜。"""
    hits: list[str] = []
    ad = binance_adapter({}, hits)
    feed_public_cache("binance", "GET /fapi/v1/premiumIndex", [
        {"symbol": "BTCUSDT", "lastFundingRate": "0.0001",
         "nextFundingTime": 1785000000000, "time": 1784999000000},
    ])
    feed_public_cache("binance", "GET /fapi/v1/fundingInfo",
                      [{"symbol": "BTCUSDT", "fundingIntervalHours": 4}])
    rates = run(ad.fetch_carry_rates())
    assert hits == []
    assert [r.symbol for r in rates] == ["BTCUSDT"]
    assert rates[0].rate == Decimal("0.0001")
    assert rates[0].interval_s == 4 * 3600     # 结算周期也来自缓存，没退回默认 8h


def test_binance_instruments_served_from_cache():
    """覆写过的 _request 也必须走同一道拦截（漏了币安就永远拿不到合约表）。"""
    hits: list[str] = []
    ad = binance_adapter({}, hits)
    feed_public_cache("binance", "GET /fapi/v1/exchangeInfo", {"symbols": [{
        "symbol": "BTCUSDT", "contractType": "PERPETUAL", "status": "TRADING",
        "baseAsset": "BTC", "quoteAsset": "USDT",
        "pricePrecision": 2, "quantityPrecision": 3,
        "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"}],
    }]})
    feed_public_cache("binance", "GET /fapi/v1/fundingInfo", [])
    insts = run(ad.fetch_instruments())
    assert hits == []
    assert [i.symbol for i in insts] == ["BTCUSDT"]
    assert insts[0].tick_size == Decimal("0.10")


def test_bybit_overridden_request_honours_cache():
    """Bybit 也覆写了 _request，服务端 403，同样只能靠这道拦截。"""
    hits: list[str] = []
    ad = bybit_adapter({}, hits)
    feed_public_cache("bybit", "GET /v5/market/tickers?category=linear",
                      {"retCode": 0, "retMsg": "OK", "time": 1785000000000,
                       "result": {"category": "linear", "list": []}})
    data = run(ad._request("GET", "/v5/market/tickers", quota="market",
                           params={"category": "linear"}))
    assert hits == []
    assert data["result"]["category"] == "linear"


def test_okx_overridden_request_honours_cache():
    """OKX 浏览器侧拉不到（CORS），但拦截机制本身九家一致。"""
    hits: list[str] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        counting_transport({}, hits)))
    ad = OkxAdapter(Credential(api_key="k", api_secret="s", passphrase="p"),
                    client=client)
    ad._clock.synced_at = time.time()
    feed_public_cache("okx", "GET /api/v5/public/instruments?instType=SWAP",
                      [{"instId": "BTC-USDT-SWAP"}])
    data = run(ad._request("GET", "/api/v5/public/instruments", quota="market",
                           params={"instType": "SWAP"}))
    assert hits == []
    assert data == [{"instId": "BTC-USDT-SWAP"}]


# ---- 私有接口绝不读缓存 ---------------------------------------------------

def test_signed_request_never_reads_cache():
    """哪怕键完全一样：缓存里的东西来自某个访客，喂给签名接口就是串号。"""
    hits: list[str] = []
    ad = binance_adapter({"/fapi/v2/balance": [{"asset": "USDT"}]}, hits)
    feed_public_cache("binance", "GET /fapi/v2/balance", [{"asset": "POISONED"}])
    data = run(ad._request("GET", "/fapi/v2/balance", quota="market", signed=True))
    assert hits == ["GET /fapi/v2/balance"]      # 真的发出去了
    assert data == [{"asset": "USDT"}]


# ---- TTL 过期回落 HTTP ---------------------------------------------------

def test_expired_cache_falls_back_to_http(monkeypatch):
    """陈旧的行情比没有行情更危险，过期必须老实回落。"""
    hits: list[str] = []
    ad = binance_adapter({"/fapi/v1/fundingInfo": []}, hits)
    feed_public_cache("binance", "GET /fapi/v1/fundingInfo", [{"symbol": "STALE"}])
    # 把喂进去的时间戳往回拨到 TTL 之外，不 sleep（测试不该等两分钟）
    monkeypatch.setattr(base, "PUBLIC_CACHE_TTL_S", -1.0)
    data = run(ad._request("GET", "/fapi/v1/fundingInfo", quota="market"))
    assert hits == ["GET /fapi/v1/fundingInfo"]
    assert data == []


# ---- 本地模式行为不变 -----------------------------------------------------

def test_local_mode_behaviour_is_unchanged():
    """没人喂缓存（本地单人模式）= 缓存全空 = 一切照旧直连。

    这是整套改动最重要的一条：机制加进来了，但本地跑起来必须一字不变。
    """
    hits: list[str] = []
    ad = binance_adapter({
        "/fapi/v1/exchangeInfo": {"symbols": [{
            "symbol": "ETHUSDT", "contractType": "PERPETUAL", "status": "TRADING",
            "baseAsset": "ETH", "quoteAsset": "USDT",
            "pricePrecision": 2, "quantityPrecision": 3,
            "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"}],
        }]},
        "/fapi/v1/fundingInfo": [],
    }, hits)
    insts = run(ad.fetch_instruments())
    assert [i.symbol for i in insts] == ["ETHUSDT"]
    # 两发都实实在在打出去了，没有任何一发被缓存悄悄吃掉
    assert sorted(hits) == ["GET /fapi/v1/exchangeInfo", "GET /fapi/v1/fundingInfo"]


# ---- 浏览器可达性表 -------------------------------------------------------

def test_browser_fetchable_matrix_is_pinned():
    """2026-08-14 实测：只有这四家的跨域请求被放行。

    改这张表 = 改一个**实测**结论，必须先重新实测（CORS 策略交易所随时能改）。
    """
    from earnfarm.ui import public_feed_client as pf

    assert pf.BROWSER_FETCHABLE == {"binance", "bybit", "bitget", "hyperliquid"}


def test_cors_blocked_venues_are_never_browser_fetched():
    """CORS 拒绝的五家压根不发浏览器请求——明知会被拒还发，
    只会让每轮刷新白等五次超时。"""
    from earnfarm.ui import public_feed_client as pf

    for venue in ("okx", "gate", "htx", "kucoin", "backpack"):
        assert pf.specs_for(venue) == ()


def test_browser_specs_cover_board_endpoints():
    """四家各自的机会榜端点都要在 spec 表里，且键能算得出来。"""
    from earnfarm.ui import public_feed_client as pf

    keys = {v: {s.cache_key for s in pf.specs_for(v)} for v in pf.BROWSER_FETCHABLE}
    assert keys["binance"] == {"GET /fapi/v1/exchangeInfo",
                               "GET /fapi/v1/fundingInfo",
                               "GET /fapi/v1/premiumIndex"}
    assert keys["bybit"] == {"GET /v5/market/instruments-info?category=linear&limit=1000",
                             "GET /v5/market/tickers?category=linear"}
    assert keys["bitget"] == {
        "GET /api/v2/mix/market/contracts?productType=USDT-FUTURES",
        "GET /api/v2/mix/market/current-fund-rate?productType=USDT-FUTURES"}
    assert len(keys["hyperliquid"]) == 2      # meta / metaAndAssetCtxs


def test_spec_keys_match_what_adapters_actually_request():
    """喂数侧的键必须和适配器算出来的一字不差，否则喂了也永远不命中。

    这是整套机制最容易悄悄断掉的接缝：改了适配器的请求参数、忘了改 spec 表，
    表现是"浏览器明明拉到了，榜上还是没这家"，没有任何报错。
    """
    from earnfarm.ui import public_feed_client as pf

    fed = {s.cache_key for s in pf.specs_for("bybit")}
    # 复刻 bybit.fetch_carry_rates / fetch_instruments 的实际参数
    assert public_cache_key("GET", "/v5/market/tickers",
                            {"category": "linear"}) in fed
    assert public_cache_key("GET", "/v5/market/instruments-info",
                            {"category": "linear", "limit": 1000}) in fed


def test_bitget_carry_rates_served_from_spec_fed_cache():
    """闭环验证：拿 **spec 表算出来的键**喂进去，Bitget 适配器零请求出费率。

    直接用 spec 的键（而不是手写字符串）才测得到"喂数侧和取数侧对得上"这件事。
    """
    from earnfarm.exchanges.bitget import BitgetAdapter
    from earnfarm.ui import public_feed_client as pf

    hits: list[str] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        counting_transport({}, hits)))
    ad = BitgetAdapter(Credential(api_key="k", api_secret="s", passphrase="p"),
                       client=client)
    ad._clock.synced_at = time.time()
    spec = next(s for s in pf.specs_for("bitget") if "current-fund-rate" in s.path)
    # 浏览器侧 bitget_unwrap 已经剥掉 {code,msg,data} 壳，喂的是剥完的形状
    feed_public_cache(spec.venue, spec.cache_key, [
        {"symbol": "BTCUSDT", "fundingRate": "0.0001",
         "fundingRateInterval": "4", "nextUpdate": "1785000000000"},
    ])
    rates = run(ad.fetch_carry_rates())
    assert hits == []
    assert [r.symbol for r in rates] == ["BTCUSDT"]
    assert rates[0].interval_s == 4 * 3600


def test_hyperliquid_carry_rates_served_from_spec_fed_cache():
    """HL 是 POST + body 区分端点的那一家，键必须把 body 编进去才命中。"""
    from earnfarm.exchanges.hyperliquid import HyperliquidAdapter
    from earnfarm.ui import public_feed_client as pf

    hits: list[str] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        counting_transport({}, hits)))
    ad = HyperliquidAdapter(Credential(api_key="0x1", api_secret="0x2"),
                            client=client)
    ad._clock.synced_at = time.time()
    spec = next(s for s in pf.specs_for("hyperliquid")
                if (s.body or {}).get("type") == "metaAndAssetCtxs")
    feed_public_cache(spec.venue, spec.cache_key, [
        {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}],
         "marginTables": []},
        [{"funding": "0.0000125", "markPx": "61000"}],
    ])
    rates = run(ad.fetch_carry_rates())
    assert hits == []
    assert [r.symbol for r in rates] == ["BTC"]
    assert rates[0].rate == Decimal("0.0000125")


def test_spec_urls_are_absolute_and_carry_params():
    from earnfarm.ui import public_feed_client as pf

    urls = {s.url for s in pf.specs_for("bybit")}
    assert "https://api.bybit.com/v5/market/tickers?category=linear" in urls
    hl = [s for s in pf.specs_for("hyperliquid")]
    assert all(s.url == "https://api.hyperliquid.xyz/info" for s in hl)
    assert all(s.method == "POST" and s.body for s in hl)
