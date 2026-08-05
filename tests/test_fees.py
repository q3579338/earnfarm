"""真实费率档位的接线测试。

守的是四条线：

1. **零费率不是"免费"，是"没查到"。** 六家里原先有四家在符号缺失时补一条
   `FeeSchedule(maker=0, taker=0)` 占位。四笔 taker 成本凭空消失，负期望的
   机会整片翻成正期望，而且不报任何错——这是这套代码里最贵的一个静默 bug。
   现在的契约是查不到就不返回，上层退回默认挂牌价并在界面上标出来。
2. **按 symbol 匹配，不按下标。** FeeSchedule 早先没有 symbol 字段，三家补零
   对齐、两家跳过，调用方根本分不清哪条对应哪个品种。
3. **档位要缓存。** 这些端点都不便宜（币安权重 20），而档位一天变不了一次。
4. **这件事真的会改变结论。** 文件末尾那个金标准：同一份行情、同一份历史，
   只把费率档位从 VIP0 挂牌价换成 VIP3，判决从"负期望"变成"可做"。
   这就是为什么不能默默用默认值算完就给结论。
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

import httpx
import pytest

from carryfarm.config import Config
from carryfarm.exchanges.base import Credential
from carryfarm.exchanges.binance import BinanceAdapter
from carryfarm.exchanges.bitget import BitgetAdapter
from carryfarm.exchanges.bybit import BybitAdapter
from carryfarm.exchanges.gate import GateAdapter
from carryfarm.exchanges.htx import HtxAdapter
from carryfarm.exchanges.okx import OkxAdapter
from carryfarm.models import BookDepth, FeeSchedule, Venue
from carryfarm.public_feed import (
    DEFAULT_TAKER,
    PublicFeed,
    RawFunding,
    _basis_of,
)
from carryfarm.scoring import HistoryStats
from carryfarm.session import FEE_CACHE_TTL_S, Session

CRED = Credential(api_key="k", api_secret="s", passphrase="p")


# ---- 六家适配器的脚手架 ---------------------------------------------------

def _client(routes: dict, calls: list[httpx.Request]) -> httpx.AsyncClient:
    """路由表按 path 精确匹配；没定义的路由直接炸，免得测试悄悄走进兜底分支。"""
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        payload = routes.get(request.url.path)
        if payload is None:
            raise AssertionError(f"测试未定义的路由: {request.url.path}")
        return httpx.Response(200, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def build(cls, routes: dict, **kwargs):
    """建一个只连 MockTransport 的适配器，并把时钟钉住。

    不钉时钟的话 _request 开头会因为 is_stale 去调 sync_clock，
    多打一个 /time 请求，把"这个方法到底发了几个请求"的断言搅浑。
    """
    calls: list[httpx.Request] = []
    ad = cls(CRED, client=_client(routes, calls), **kwargs)
    ad._clock.synced_at = time.time()
    ad._clock.offset_ms = 0
    return ad, calls


def run(ad, coro):
    async def go():
        try:
            return await coro
        finally:
            await ad.close()
    return asyncio.run(go())


# 各家 feeRate 端点的响应，逐字取自 docs/research/exchange-*.md
BINANCE_ROUTES = {
    "/fapi/v1/commissionRate": {"symbol": "BTCUSDT",
                                "makerCommissionRate": "0.000180",
                                "takerCommissionRate": "0.000400"},
    "/fapi/v1/feeBurn": {"feeBurn": False},
}
OKX_ROUTES = {"/api/v5/account/trade-fee": {"code": "0", "msg": "", "data": [
    {"level": "VIP2", "instType": "SWAP", "taker": "-0.0005", "maker": "-0.0002",
     "takerU": "-0.0003", "makerU": "-0.0001", "ts": "1785548167559"}]}}
BYBIT_ROUTES = {"/v5/account/fee-rate": {
    "retCode": 0, "retMsg": "OK", "time": 1785548167559,
    "result": {"list": [
        {"symbol": "BTCUSDT", "takerFeeRate": "0.00055", "makerFeeRate": "0.0002"},
        {"symbol": "ETHUSDT", "takerFeeRate": "0.0003", "makerFeeRate": "0.0001"}]}}}
GATE_ROUTES = {"/api/v4/futures/usdt/fee": {
    "BTC_USDT": {"taker_fee": "0.0005", "maker_fee": "0.0002"},
    "ETH_USDT": {"taker_fee": "0.0003", "maker_fee": "-0.0001"}}}
HTX_ROUTES = {"/linear-swap-api/v1/swap_fee": {"status": "ok", "ts": 1785548167559,
    "data": [{"contract_code": "BTC-USDT", "open_maker_fee": "0.0002",
              "open_taker_fee": "0.0004", "close_maker_fee": "0.0003",
              "close_taker_fee": "0.0005", "fee_asset": "USDT"}]}}
BITGET_ROUTES = {"/api/v2/common/trade-rate": {
    "code": "00000", "msg": "success", "requestTime": 1785548132637,
    "data": {"makerFeeRate": "0.0002", "takerFeeRate": "0.0006"}}}


# ---- 六家：档位解析 -------------------------------------------------------

def test_binance_parses_the_account_tier_and_tags_the_symbol():
    ad, calls = build(BinanceAdapter, BINANCE_ROUTES)
    fees = run(ad, ad.fetch_fee_schedule(["BTCUSDT"]))
    assert len(fees) == 1
    assert fees[0].symbol == "BTCUSDT"
    assert fees[0].taker == Decimal("0.000400")
    assert fees[0].maker == Decimal("0.000180")
    assert fees[0].market == "binance:perp"
    # commissionRate 是签名端点——公开端点上只有挂牌基础档
    rate = [c for c in calls if c.url.path == "/fapi/v1/commissionRate"][0]
    assert "signature=" in str(rate.url)


def test_binance_no_symbol_gives_an_account_level_row():
    """不传符号时返回 symbol 为空的账户级档位，上层拿它兜底整个市场。"""
    ad, _ = build(BinanceAdapter, BINANCE_ROUTES)
    fees = run(ad, ad.fetch_fee_schedule(()))
    assert len(fees) == 1 and fees[0].symbol == ""
    assert fees[0].taker == Decimal("0.000400")


def test_binance_reads_the_bnb_burn_switch_but_refuses_to_guess_the_discount():
    """折扣**幅度**不在任何 API 里，只有一个布尔开关。

    拿记忆里的百分比去削成本，等于在这个工具唯一的判决变量上凭空造数：
    低估成本会让负期望的机会看起来能做。所以只报状态、不打折——
    实际扣费只会比显示的更低，方向是安全的那一边。
    """
    routes = {**BINANCE_ROUTES, "/fapi/v1/feeBurn": {"feeBurn": True}}
    ad, _ = build(BinanceAdapter, routes)
    fees = run(ad, ad.fetch_fee_schedule(["BTCUSDT"]))
    assert "BNB" in fees[0].note
    assert fees[0].taker == Decimal("0.000400")      # 一分钱都没打折


def test_binance_missing_rate_fields_yield_nothing_not_a_zero_fee():
    routes = {**BINANCE_ROUTES, "/fapi/v1/commissionRate": {"symbol": "BTCUSDT"}}
    ad, _ = build(BinanceAdapter, routes)
    assert run(ad, ad.fetch_fee_schedule(["BTCUSDT"])) == ()


def test_okx_flips_the_sign_and_uses_the_usdt_margined_pair():
    """OKX 返回负数表示"你付"；linear 合约要读 takerU/makerU 而不是 taker/maker。"""
    ad, _ = build(OkxAdapter, OKX_ROUTES)
    fees = run(ad, ad.fetch_fee_schedule(["BTC-USDT-SWAP"]))
    assert fees[0].symbol == "BTC-USDT-SWAP"
    assert fees[0].taker == Decimal("0.0003")       # takerU，且翻成正数=成本
    assert fees[0].maker == Decimal("0.0001")
    assert "VIP2" in fees[0].note                   # 档位等级带给界面


def test_okx_empty_response_yields_nothing_not_a_zero_fee():
    ad, _ = build(OkxAdapter, {"/api/v5/account/trade-fee":
                               {"code": "0", "msg": "", "data": []}})
    assert run(ad, ad.fetch_fee_schedule(["BTC-USDT-SWAP"])) == ()


def test_bybit_returns_per_symbol_rows_keyed_by_symbol():
    """Bybit 的减免是逐 symbol 给的，所以每条必须带自己的 symbol。"""
    ad, _ = build(BybitAdapter, BYBIT_ROUTES)
    fees = run(ad, ad.fetch_fee_schedule(()))
    table = {f.symbol: f for f in fees}
    assert table["BTCUSDT"].taker == Decimal("0.00055")
    assert table["ETHUSDT"].taker == Decimal("0.0003")


def test_bybit_unknown_symbol_is_omitted():
    ad, _ = build(BybitAdapter, BYBIT_ROUTES)
    fees = run(ad, ad.fetch_fee_schedule(["BTCUSDT", "NOPEUSDT"]))
    assert [f.symbol for f in fees] == ["BTCUSDT"]


def test_gate_uses_the_private_endpoint_and_keeps_the_maker_rebate_negative():
    ad, calls = build(GateAdapter, GATE_ROUTES)
    fees = run(ad, ad.fetch_fee_schedule(["ETH_USDT"]))
    assert fees[0].symbol == "ETH_USDT"
    assert fees[0].taker == Decimal("0.0003")
    assert fees[0].maker == Decimal("-0.0001")      # 负数=返佣，当收入
    assert "SIGN" in calls[-1].headers              # /contracts 上的是基础档，不能用


def test_gate_missing_contract_yields_nothing_not_a_zero_fee():
    """这里原来是 `data.get(sym) or {}` → _dec(None) → 0，一个**零手续费**的档位。

    往返四笔 taker 会整个消失，负期望的机会全部翻成正期望，而且一声不吭。
    """
    ad, _ = build(GateAdapter, GATE_ROUTES)
    fees = run(ad, ad.fetch_fee_schedule(["DOGE_USDT"]))
    assert fees == []


def test_htx_takes_the_worse_of_open_and_close():
    ad, _ = build(HtxAdapter, HTX_ROUTES)
    fees = run(ad, ad.fetch_fee_schedule(["BTC-USDT"]))
    assert fees[0].symbol == "BTC-USDT"
    assert fees[0].taker == Decimal("0.0005")       # 开平不同，取较大值
    assert fees[0].maker == Decimal("0.0003")


def test_htx_pulls_the_whole_table_when_asked_for_many_symbols():
    """逐个查是 N 个签名 POST；超过 3 个就不如一把全量拉回来自己挑。"""
    ad, calls = build(HtxAdapter, HTX_ROUTES)
    run(ad, ad.fetch_fee_schedule(["A", "B", "C", "D", "BTC-USDT"]))
    fee_calls = [c for c in calls if c.url.path == "/linear-swap-api/v1/swap_fee"]
    assert len(fee_calls) == 1


def test_bitget_reuses_the_account_tier_across_symbols():
    """Bitget 的 mix 档位是账户级的：探一个符号，全市场通用。"""
    ad, calls = build(BitgetAdapter, BITGET_ROUTES)
    fees = run(ad, ad.fetch_fee_schedule(["BTCUSDT", "DOGEUSDT"]))
    assert [f.symbol for f in fees] == ["BTCUSDT", "DOGEUSDT"]
    assert all(f.taker == Decimal("0.0006") for f in fees)
    probe = [c for c in calls if "trade-rate" in c.url.path]
    assert len(probe) == 1                       # 账户级，不必逐个符号查
    assert b"businessType=mix" in probe[0].url.query


def test_bitget_missing_fields_yield_nothing_not_a_zero_fee():
    routes = {"/api/v2/common/trade-rate": {"code": "00000", "msg": "success",
                                            "requestTime": 1, "data": {}}}
    ad, _ = build(BitgetAdapter, routes)
    assert run(ad, ad.fetch_fee_schedule(["BTCUSDT"])) == []


@pytest.mark.parametrize("cls,routes,symbol,kwargs", [
    (BinanceAdapter, BINANCE_ROUTES, "BTCUSDT", {}),
    (OkxAdapter, OKX_ROUTES, "BTC-USDT-SWAP", {}),
    (BybitAdapter, BYBIT_ROUTES, "BTCUSDT", {}),
    (GateAdapter, GATE_ROUTES, "BTC_USDT", {}),
    (HtxAdapter, HTX_ROUTES, "BTC-USDT", {}),
    (BitgetAdapter, BITGET_ROUTES, "BTCUSDT", {}),
])
def test_every_venue_fills_symbol_and_charges_something(cls, routes, symbol, kwargs):
    """六家的统一契约：每条都带 symbol，taker 一律为正（成本口径），绝不为零。"""
    ad, _ = build(cls, routes, **kwargs)
    fees = run(ad, ad.fetch_fee_schedule([symbol]))
    assert len(fees) == 1, f"{cls.__name__} 没返回该符号的档位"
    assert fees[0].symbol == symbol, f"{cls.__name__} 没填 symbol"
    # 零 taker 的含义是「没查到」，不是「免费」——绝不能让它冒充真实档位
    assert fees[0].taker > 0, f"{cls.__name__} 给出了零 taker"


# ---- Session 缓存 ---------------------------------------------------------

class FakeAdapter:
    """只实现 fetch_fee_schedule 的适配器替身。"""

    def __init__(self, schedules, *, boom: Exception | None = None) -> None:
        self.schedules = schedules
        self.boom = boom
        self.calls = 0

    async def fetch_fee_schedule(self, symbols):
        self.calls += 1
        if self.boom is not None:
            raise self.boom
        return self.schedules


def session_with(tmp_path, adapters: dict) -> Session:
    session = Session(Config(data_dir=tmp_path))
    for market, adapter in adapters.items():
        session.adapters.register(market, adapter)
    return session


def sched(market, symbol, taker, maker="0.0002", note="") -> FeeSchedule:
    return FeeSchedule(market=market, symbol=symbol,
                       maker=Decimal(maker), taker=Decimal(taker), note=note)


def test_session_caches_the_tier_and_does_not_refetch(tmp_path):
    """这些端点都不便宜（币安权重 20），而档位一天变不了一次。"""
    fake = FakeAdapter([sched("bybit:perp", "BTCUSDT", "0.00025")])
    session = session_with(tmp_path, {"bybit:perp": fake})

    assert asyncio.run(session.refresh_fees()) == {}
    assert fake.calls == 1
    asyncio.run(session.refresh_fees())
    assert fake.calls == 1, "缓存没生效，又去打了一次签名端点"

    assert session.fee_for("bybit:perp", "BTCUSDT").taker == Decimal("0.00025")
    assert session.has_real_fees is True


def test_session_refetches_after_the_ttl(tmp_path):
    fake = FakeAdapter([sched("bybit:perp", "BTCUSDT", "0.00025")])
    session = session_with(tmp_path, {"bybit:perp": fake})
    t0 = 1_785_000_000.0
    asyncio.run(session.refresh_fees(now=t0))
    asyncio.run(session.refresh_fees(now=t0 + FEE_CACHE_TTL_S - 1))
    assert fake.calls == 1
    asyncio.run(session.refresh_fees(now=t0 + FEE_CACHE_TTL_S + 1))
    assert fake.calls == 2
    asyncio.run(session.refresh_fees(now=t0, force=True))
    assert fake.calls == 3


def test_session_account_level_row_covers_every_symbol(tmp_path):
    """币安/OKX/Bitget 的合约档位是账户级的（symbol 为空串），一档吃全市场。"""
    fake = FakeAdapter([sched("binance:perp", "", "0.00025")])
    session = session_with(tmp_path, {"binance:perp": fake})
    asyncio.run(session.refresh_fees())
    assert session.fee_for("binance:perp", "ANYTHINGUSDT").taker == Decimal("0.00025")


def test_session_never_extrapolates_a_per_symbol_tier(tmp_path):
    """Bybit 的做市商减免是逐 symbol 给的。

    拿 BTC 的档位替 ETH 猜，猜错的方向恰好是**低估成本**——
    宁可返回 None 让上层退回挂牌价并标注出来。
    """
    fake = FakeAdapter([sched("bybit:perp", "BTCUSDT", "0.0001")])
    session = session_with(tmp_path, {"bybit:perp": fake})
    asyncio.run(session.refresh_fees())
    assert session.fee_for("bybit:perp", "BTCUSDT") is not None
    assert session.fee_for("bybit:perp", "ETHUSDT") is None


def test_session_drops_the_cache_when_the_venue_errors(tmp_path):
    """拉不到不是致命错（退回挂牌价即可），但**绝不能留着旧缓存假装还有效**。"""
    fake = FakeAdapter([sched("gate:perp", "BTC_USDT", "0.00025")])
    session = session_with(tmp_path, {"gate:perp": fake})
    asyncio.run(session.refresh_fees())
    assert session.fee_for("gate:perp", "BTC_USDT") is not None

    fake.boom = RuntimeError("401 invalid signature")
    errors = asyncio.run(session.refresh_fees(force=True))
    assert "gate:perp" in errors
    assert session.fee_for("gate:perp", "BTC_USDT") is None
    assert session.has_real_fees is False


def test_locking_the_vault_forgets_the_tier(tmp_path):
    """档位是账户数据。锁库之后还拿前一个账户的 VIP 档算成本，用户完全看不出来。"""
    fake = FakeAdapter([sched("okx:perp", "", "0.00025")])
    session = session_with(tmp_path, {"okx:perp": fake})
    asyncio.run(session.refresh_fees())
    session.lock()
    assert session.fee_for("okx:perp", "BTC-USDT-SWAP") is None
    assert session.has_real_fees is False


def test_session_surfaces_the_platform_token_note(tmp_path):
    fake = FakeAdapter([sched("binance:perp", "", "0.00025",
                              note="已开 BNB 抵扣，实际扣费低于此处显示")])
    session = session_with(tmp_path, {"binance:perp": fake})
    asyncio.run(session.refresh_fees())
    assert any("BNB" in n for n in session.fee_notes())


# ---- 机会榜：真实档位 vs 默认档位 ------------------------------------------

NOW_S = 1_785_000_000.0
HORIZON_H = 72.0
# 8 小时周期、下次结算在 1 小时后 → 72 小时窗口内正好 9 次结算
SETTLEMENTS = 9
LONG_RATE = Decimal("0")
SHORT_RATE = Decimal("0.00024")


def _depth(symbol: str) -> BookDepth:
    """一档 20 万、带宽内 200 万的健康盘口。

    深度必须远大于评分仓位，否则 verdict 会先被"缩仓位"截胡，
    这个用例要验的是费率，不是容量。
    """
    return BookDepth(symbol=symbol,
                     bid_notional=Decimal("2000000"), ask_notional=Decimal("2000000"),
                     bid_price=Decimal("99.995"), bid_qty=Decimal("2000"),
                     ask_price=Decimal("100.005"), ask_qty=Decimal("2000"),
                     levels_used=20, band_bp=10.0, ts_ms=int(NOW_S * 1000))


class StubBackfiller:
    """一段平的历史：小时 carry 恒为 (0.00024-0)/8 = 0.00003。

    平的历史让 AR(1) 不衰减（中位数=当前值），于是持有期收益正好是
    9 × 0.00024 = 0.00216，可以手算，判决的边界就钉死了。
    """

    HOURLY = 0.00024 / 8

    def stats_for(self, market, symbol, days):
        rate = float(SHORT_RATE) if symbol.endswith("short") else float(LONG_RATE)
        return HistoryStats((rate,) * (days * 3), 8.0)

    def hourly_carry(self, lm, ls, sm, ss, days):
        return [self.HOURLY] * (days * 24)


class StubFeed(PublicFeed):
    """把网络那一层换掉，其余全走 build_opportunities 的真实代码路径。"""

    async def fetch_funding(self):
        def row(venue: Venue, symbol: str, rate: Decimal):
            return RawFunding(venue=venue, symbol=symbol, base="AAA", rate=rate,
                              interval_h=8.0, next_ts=NOW_S + 3600)
        return {
            Venue.BITGET: [row(Venue.BITGET, "AAA-long", LONG_RATE)],
            Venue.BYBIT: [row(Venue.BYBIT, "AAA-short", SHORT_RATE)],
        }

    async def fetch_depth(self, venue, symbol, band_bp=10.0):
        return _depth(symbol)


class StubFees:
    """按 (market, symbol) 给真实档位；表里没有的返回 None（=退回挂牌价）。"""

    def __init__(self, table: dict) -> None:
        self.table = table

    def fee_for(self, market: str, symbol: str):
        return self.table.get((market, symbol))


VIP3 = StubFees({
    ("bitget:perp", "AAA-long"): sched("bitget:perp", "AAA-long", "0.0002"),
    ("bybit:perp", "AAA-short"): sched("bybit:perp", "AAA-short", "0.0002"),
})


def board(fee_source=None, *, backfiller=None):
    feed = StubFeed(backfiller=backfiller or StubBackfiller(), history_days=40)
    rows = asyncio.run(feed.build_opportunities(
        notional=50_000, horizon_h=HORIZON_H, fee_source=fee_source))
    return feed, rows[0]


def test_without_a_fee_source_the_listed_rate_is_used_and_labelled():
    """没连账户就用 VIP0 挂牌价——但**必须标出来**。

    默默用默认值假装精确正是原版工具的病：用户看到一个"负期望"的结论，
    却无从知道那是按挂牌价算的。
    """
    feed, row = board(None)
    assert row.fee_basis == "default"
    assert feed.fee_basis == "default"
    assert feed.fee_hits == 0 and feed.fee_misses == 2
    assert row.long.taker_fee == DEFAULT_TAKER[Venue.BITGET]
    assert row.short.taker_fee == DEFAULT_TAKER[Venue.BYBIT]


def test_real_tiers_replace_the_listed_rate_on_both_legs():
    feed, row = board(VIP3)
    assert row.fee_basis == "real"
    assert feed.fee_basis == "real"
    assert feed.fee_hits == 2 and feed.fee_misses == 0
    assert row.long.taker_fee == 0.0002 and row.short.taker_fee == 0.0002


def test_one_leg_only_is_reported_as_partial():
    """跨所成本是两家相加，缺一条腿这个数就还不能当准——不能报成 real。"""
    half = StubFees({("bybit:perp", "AAA-short"):
                     sched("bybit:perp", "AAA-short", "0.0002")})
    feed, row = board(half)
    assert row.fee_basis == "partial"
    assert feed.fee_basis == "partial"
    assert row.long.taker_fee == DEFAULT_TAKER[Venue.BITGET]     # 另一条腿退回挂牌价


def test_a_broken_fee_source_degrades_to_the_listed_rate():
    """费率源炸了（库锁了、会话过期）不该让整个机会榜空掉。"""

    class Broken:
        def fee_for(self, market, symbol):
            raise RuntimeError("session locked")

    _, row = board(Broken())
    assert row.fee_basis == "default"
    assert row.long.taker_fee == DEFAULT_TAKER[Venue.BITGET]


def test_a_zero_taker_from_the_fee_source_is_refused():
    """零 taker 会让四笔往返成本整个消失，负期望的机会全翻成正期望。

    它几乎只可能来自解析错误，所以宁可不认、退回挂牌价——
    真有零费率的做市商账户，高估他的成本也只是让机会看起来差一点。
    """
    zeroed = StubFees({
        ("bitget:perp", "AAA-long"): sched("bitget:perp", "AAA-long", "0"),
        ("bybit:perp", "AAA-short"): sched("bybit:perp", "AAA-short", "0"),
    })
    _, row = board(zeroed)
    assert row.fee_basis == "default"
    assert row.long.taker_fee == DEFAULT_TAKER[Venue.BITGET]
    assert row.cost_rt > 0.002


def test_rescore_keeps_the_fee_basis():
    """后台补完历史重新评分时丢掉口径，整榜的成本列会突然变回"按默认费率估算"。"""
    feed, row = board(VIP3)
    again = feed.rescore([row], horizon_h=HORIZON_H, now_ts=NOW_S)[0]
    assert again.fee_basis == "real"


def test_basis_of_counts_legs():
    assert _basis_of(2) == "real"
    assert _basis_of(1) == "partial"
    assert _basis_of(0) == "default"


# ---- 界面：估算值必须标出来 ------------------------------------------------

def test_every_estimated_basis_has_an_explanation():
    """新增一种口径却忘了写提示，就会出现一个没人解释得清的标记。

    "real" 是唯一不需要提示的——那一行的成本本来就是按你的真实档位算的。
    """
    from carryfarm.ui.opportunities import FEE_BASIS_HINT

    for basis in ("default", "partial"):
        assert basis in FEE_BASIS_HINT
        assert "挂牌价" in FEE_BASIS_HINT[basis]
    assert "real" not in FEE_BASIS_HINT


def test_the_summary_line_says_when_the_cost_is_estimated():
    """悬停提示不够——整榜按挂牌价估算时，用户不把鼠标放上去也得知道。"""
    from carryfarm.ui.opportunities import OpportunityBoard as B

    _, default_row = board(None)
    _, real_row = board(VIP3)

    assert "挂牌价估算" in B._fee_note([default_row])
    assert B._fee_note([real_row]) == ""
    assert "1 个" in B._fee_note([default_row, real_row])


# ---- 金标准：费率档位真的会翻转判决 ----------------------------------------

def test_the_tier_flips_the_verdict_on_a_marginal_opportunity():
    """同一份行情、同一份历史，只把费率档位从 VIP0 挂牌价换成 VIP3。

    手算（名义 5 万，一档 20 万所以只吃半价差，滑点 = 1.5 × 5bp/2 = 7.5e-5/笔）：
      持有期收益 = 9 次结算 × 2.4bp                       = 0.00216
      VIP0 成本  = 2×(6bp + 5.5bp) + (2+1.2×2)×7.5e-5     = 0.00263  → 净 -0.00047
      VIP3 成本  = 2×(2bp + 2bp)   + (2+1.2×2)×7.5e-5     = 0.00113  → 净 +0.00103

    也就是说：**同一个机会，VIP0 判负期望、VIP3 判可做。**
    这就是为什么费率档位不能用挂牌价糊弄——它不是精度问题，是结论问题。
    """
    _, cheap_off = board(None)
    _, cheap_on = board(VIP3)

    # 成本口径不同，且差额正好是两条腿四笔手续费的差
    assert cheap_off.cost_rt == pytest.approx(0.00263, abs=1e-6)
    assert cheap_on.cost_rt == pytest.approx(0.00113, abs=1e-6)
    assert cheap_off.cost_rt - cheap_on.cost_rt == pytest.approx(0.0015, abs=1e-6)

    # 毛收益一模一样——变的只有成本
    assert cheap_off.gross_apr == pytest.approx(cheap_on.gross_apr, rel=1e-9)

    # 结论翻转：负期望 → 可做
    assert cheap_off.net_apr < 0 and cheap_off.verdict == "negative"
    assert cheap_on.net_apr > 0 and cheap_on.verdict == "good"
    assert cheap_on.is_actionable and not cheap_off.is_actionable

    # 而且理由里写得清清楚楚，用户不用猜
    assert any("扛不住" in r for r in cheap_off.reasons)

    # 口径标记也跟着走，界面据此决定要不要打提示星号
    assert cheap_off.fee_basis == "default" and cheap_on.fee_basis == "real"


# ---- 成交方式切换（界面链路，无头） ----------------------------------------

def test_fill_mode_switch_triggers_a_full_rescore_not_a_render():
    """成交方式改的是成本模型：只重渲染会让表头写着「挂单」而数字还是吃单的。

    UI 那层的约定是：filters.fill_mode 一变就调 on_rescore（由 app 层接到
    「从库重评整榜」上），而不是像其他过滤器那样只 render。
    """
    from carryfarm.ui.opportunities import OpportunityBoard

    b = OpportunityBoard()
    calls = []
    b.on_rescore = lambda: calls.append(b.filters.fill_mode)

    class E:
        value = "maker_one"

    b._set_fill_mode(E())
    assert b.filters.fill_mode == "maker_one"
    assert calls == ["maker_one"], "切换必须走 on_rescore，不能只 render"
    # 重复选同一项不该白跑一次 105 秒都换不来的重评
    b._set_fill_mode(E())
    assert calls == ["maker_one"]


def test_summary_line_pins_the_maker_assumption_on_the_whole_board():
    """挂单口径下整榜都是「假设必成交」的上限，摘要行必须钉着这句话——
    跟哪一行展开没展开无关。"""
    from carryfarm.ui.opportunities import OpportunityBoard

    b = OpportunityBoard()
    assert "挂单" not in b._summary_text([])
    b.filters.fill_mode = "maker_one"
    assert "假设挂单必成交的上限" in b._summary_text([])
    b.filters.fill_mode = "maker_both"
    text = b._summary_text([])
    assert "假设两腿都成交的上限" in text and "裸奔" in text


def test_rescore_keeps_the_fill_mode_of_each_row():
    """历史补完后的重评不能把挂单口径悄悄退回吃单——rescore 复用行上的口径。"""
    feed, row_taker = board(None)
    rows_maker = asyncio.run(feed.build_opportunities(
        notional=50_000, horizon_h=HORIZON_H, fill_mode="maker_one"))
    row = rows_maker[0]
    assert row.fill_mode == "maker_one"
    assert row.cost_rt < row_taker.cost_rt
    again = feed.rescore([row], horizon_h=HORIZON_H)[0]
    assert again.fill_mode == "maker_one"
    assert again.cost_rt == pytest.approx(row.cost_rt, abs=1e-12)


# ---- 配对组合的退而求其次（Backpack 接入时实测出的检测缺口） ----------------

def test_pairing_falls_back_to_the_next_best_legs_when_the_extreme_leg_is_empty():
    """极端腿没盘口时必须退而求其次，不能把整个 base 扔掉。

    实测事故：PENDLE 的 Gate 腿（费率最低）带宽内只有 $39，整币被静默丢弃，
    而"币安多 / Backpack 空"那对费差 120% 年化、两边都是深盘——费率极端的腿
    往往正是盘子最小的腿，只试极端对会系统性错过"次优费差 × 真实深度"。
    """
    from carryfarm.models import Venue
    from carryfarm.public_feed import PublicFeed, RawFunding

    class ThreeLegFeed(PublicFeed):
        async def fetch_funding(self):
            def row(venue, symbol, rate):
                return RawFunding(venue=venue, symbol=symbol, base="AAA",
                                  rate=Decimal(rate), interval_h=8.0,
                                  next_ts=NOW_S + 3600)
            return {
                # 极端多腿：费率最低但盘口是空的
                Venue.GATE: [row(Venue.GATE, "AAA-dead", "-0.0030")],
                # 次优多腿：费率没那么低，盘口健康
                Venue.BITGET: [row(Venue.BITGET, "AAA-long", "-0.0005")],
                Venue.BYBIT: [row(Venue.BYBIT, "AAA-short", "0.0030")],
            }

        async def fetch_depth(self, venue, symbol, band_bp=10.0):
            if symbol == "AAA-dead":
                return BookDepth(symbol=symbol,
                                 bid_notional=Decimal("0"), ask_notional=Decimal("0"),
                                 bid_price=Decimal("99.995"), bid_qty=Decimal("0"),
                                 ask_price=Decimal("100.005"), ask_qty=Decimal("0"),
                                 levels_used=0, band_bp=10.0, ts_ms=int(NOW_S * 1000))
            return _depth(symbol)

    feed = ThreeLegFeed(backfiller=StubBackfiller(), history_days=40)
    rows = asyncio.run(feed.build_opportunities(
        notional=50_000, horizon_h=HORIZON_H))
    assert len(rows) == 1, "极端腿死了，base 不该跟着死"
    o = rows[0]
    assert o.long.symbol == "AAA-long", \
        f"该退到次优多腿，实际配的是 {o.long.symbol}"
    assert o.short.symbol == "AAA-short"


def test_pairing_still_drops_the_base_when_every_combo_is_illiquid():
    """所有组合都没盘口时才允许丢弃——丢弃是最后手段，不是默认动作。"""
    from carryfarm.models import Venue
    from carryfarm.public_feed import PublicFeed, RawFunding

    class AllDeadFeed(PublicFeed):
        async def fetch_funding(self):
            def row(venue, symbol, rate):
                return RawFunding(venue=venue, symbol=symbol, base="AAA",
                                  rate=Decimal(rate), interval_h=8.0,
                                  next_ts=NOW_S + 3600)
            return {Venue.GATE: [row(Venue.GATE, "AAA-a", "-0.0030")],
                    Venue.BYBIT: [row(Venue.BYBIT, "AAA-b", "0.0030")]}

        async def fetch_depth(self, venue, symbol, band_bp=10.0):
            return BookDepth(symbol=symbol,
                             bid_notional=Decimal("0"), ask_notional=Decimal("0"),
                             bid_price=Decimal("99.995"), bid_qty=Decimal("0"),
                             ask_price=Decimal("100.005"), ask_qty=Decimal("0"),
                             levels_used=0, band_bp=10.0, ts_ms=int(NOW_S * 1000))

    feed = AllDeadFeed(backfiller=StubBackfiller(), history_days=40)
    rows = asyncio.run(feed.build_opportunities(notional=50_000, horizon_h=HORIZON_H))
    assert rows == []
