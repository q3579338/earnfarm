"""机会榜与历史资金费的接线测试。

守的是三条线：

1. **首屏不能被回填拖住。** build_opportunities 只读库，一个网络请求都不为
   历史发出去。回填要跑十几秒到几分钟，挂在首屏路径上等于白屏。
2. **拿不到历史必须退化成单点，不是退化成空。** 空的 HistoryStats.median()
   返回 0，等于向 AR(1) 断言"这个费率的长期均值就是 0"，费率会被一路衰减到零——
   那不是"没有历史"，是一个凭空捏造的悲观结论。
3. **样本不足要老实标出来。** stability 的 is_prior 必须原样传到界面，
   把 0.35 的先验伪装成评分比不显示还糟。
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

from carryfarm.history import HOUR_MS, HistoryBackfiller
from carryfarm.public_feed import PublicFeed, backfill_targets
from carryfarm.scoring import HistoryStats, LegQuote, ScoredOpportunity, score_pair
from carryfarm.storage import Storage

NOW_MS = 1_785_000_000_000 // (8 * HOUR_MS) * (8 * HOUR_MS)
NOW_S = NOW_MS / 1000
DAY_MS = 86_400_000


def leg(symbol: str, market: str, rate: float, interval_h: float = 8.0) -> LegQuote:
    px, top, depth = 100.0, 2e5, 2e6
    half = px * 1e-4 / 2
    return LegQuote(symbol, market, px - half, px + half, top / px, top / px,
                    depth, 10.0, 0.0005, 0.0002, rate, interval_h,
                    NOW_S + 3600, 0.0075, -0.0075, 5e7, 2e8)


def make_opportunity(hourly_carry=None) -> ScoredOpportunity:
    """首屏那一版：没有任何历史，stability 走 is_prior 分支。"""
    long_leg = leg("L-USDT-SWAP", "okx:perp", 0.0, 1.0)
    short_leg = leg("SUSDT", "binance:perp", 0.0008, 8.0)
    return score_pair("AAA", long_leg, short_leg,
                      HistoryStats((long_leg.funding_now,), 1.0),
                      HistoryStats((short_leg.funding_now,), 8.0),
                      notional=50_000, horizon_h=72, now_ts=NOW_S,
                      hourly_carry=hourly_carry)


def fill_history(storage: Storage, days: int = 40) -> None:
    """两条腿各存 days 天：多腿 1h、空腿 8h，摊平后 carry 恒为 0.00009/h。"""
    h0 = NOW_MS - days * DAY_MS
    storage.save_funding("okx:perp", "L-USDT-SWAP", 3600,
                         [(h0 + k * HOUR_MS, Decimal("0.00001"))
                          for k in range(1, days * 24 + 1)])
    storage.save_funding("binance:perp", "SUSDT", 28800,
                         [(h0 + k * 8 * HOUR_MS, Decimal("0.0008"))
                          for k in range(1, days * 3 + 1)])


def backfiller(tmp_path) -> tuple[HistoryBackfiller, Storage]:
    storage = Storage(tmp_path / "feed.db")
    return HistoryBackfiller(storage, {}, clock=lambda: NOW_MS), storage


# ---- 退化路径 ------------------------------------------------------------

def test_without_a_backfiller_the_score_matches_the_single_point_behaviour(tmp_path):
    """没接历史时的行为必须和接之前一字不差——否则这次改动本身就是一次回归。"""
    feed = PublicFeed(backfiller=None)
    row = make_opportunity()
    scored = feed.rescore([row], horizon_h=72, now_ts=NOW_S)[0]

    assert scored.stability.is_prior is True
    assert scored.stability.n_samples == 0
    # 单点历史 = 中位数就是当前值 = AR(1) 不衰减，与接历史之前完全同构
    hist_l, hist_s, carry = feed._history_for(row.long, row.short)
    assert hist_l.rates == (row.long.funding_now,)
    assert hist_s.rates == (row.short.funding_now,)
    assert carry is None


def test_empty_history_falls_back_to_the_current_rate_not_to_zero(tmp_path):
    """库是空的时候退化成单点，绝不能退化成空序列：
    空的 median() 返回 0，等于断言"这个费率的长期均值就是 0"，
    forecast_path 会把它一路衰减到零——那是凭空捏造的悲观结论，不是"没数据"。
    """
    bf, _ = backfiller(tmp_path)
    feed = PublicFeed(backfiller=bf)
    row = make_opportunity()
    hist_l, hist_s, carry = feed._history_for(row.long, row.short)

    assert hist_s.median() == row.short.funding_now
    assert hist_l.median() == row.long.funding_now
    assert carry is None
    assert feed.history_misses == 2 and feed.history_hits == 0


def test_broken_storage_degrades_instead_of_emptying_the_board(tmp_path):
    """读库炸了（文件损坏、被锁）不该让整个机会榜空掉——行情本来是好的。"""

    class BrokenBackfiller:
        def stats_for(self, *a, **kw):
            raise RuntimeError("database disk image is malformed")

        def hourly_carry(self, *a, **kw):
            raise RuntimeError("database disk image is malformed")

    feed = PublicFeed(backfiller=BrokenBackfiller())
    rows = feed.rescore([make_opportunity()], horizon_h=72, now_ts=NOW_S)
    assert len(rows) == 1
    assert rows[0].stability.is_prior is True


# ---- 真历史 --------------------------------------------------------------

def test_history_turns_the_prior_into_a_real_score(tmp_path):
    """这就是整条链路存在的理由：没有历史时 stability 一律 0.35、is_prior=True，
    机会榜的稳定性整列是同一个数，等于白算。"""
    bf, storage = backfiller(tmp_path)
    fill_history(storage)
    feed = PublicFeed(backfiller=bf, history_days=40)

    before = make_opportunity()
    assert before.stability.is_prior is True

    after = feed.rescore([before], horizon_h=72, now_ts=NOW_S)[0]
    assert after.stability.is_prior is False
    assert after.stability.n_samples > 900
    # 每个窗口都赚钱，但 Beta(2,3) 先验会把 100% 收缩到 0.84（n_eff 只有 13 个
    # 独立窗口）。收缩是对的：40 天样本不该给出"必赢"的结论
    assert 0.8 < after.stability.p_win < 1.0
    assert after.stability.median_run_h > 500
    assert feed.history_hits == 2

    # 稳定性一旦是真的，"历史数据不足，不可尽信"这条理由就该消失
    assert not any("历史数据不足" in r for r in after.reasons)
    assert any("历史数据不足" in r for r in before.reasons)


def test_rescore_keeps_the_position_used_for_scoring(tmp_path):
    """重新评分只该变历史这一个变量。仓位是按深度折算过的可行仓位，
    换回用户填的那个数会让净年化跟着变，用户看到的就不再是"历史生效了"，
    而是一串没法解释的数字跳动。"""
    bf, storage = backfiller(tmp_path)
    fill_history(storage)
    feed = PublicFeed(backfiller=bf, history_days=40)

    before = make_opportunity()
    after = feed.rescore([before], horizon_h=72, now_ts=NOW_S)[0]
    assert after.notional == before.notional
    assert after.long.symbol == before.long.symbol
    assert after.short.symbol == before.short.symbol
    assert abs(after.gross_apr - before.gross_apr) < 1e-12


def test_rescore_does_not_swap_the_legs(tmp_path):
    """score_pair 内部会再定向一次。传进去的两条腿已经定过向，
    再定一次必须得到同样的顺序——调换了就等于把多空搞反，
    历史序列的符号跟着反，稳定性评分整个失去意义。"""
    bf, storage = backfiller(tmp_path)
    fill_history(storage)
    feed = PublicFeed(backfiller=bf, history_days=40)
    row = make_opportunity()
    after = feed.rescore([row], horizon_h=72, now_ts=NOW_S)[0]
    assert (after.long.market, after.short.market) == (row.long.market, row.short.market)
    assert after.stability.p_win > 0.5, "定向反了的话胜率会掉到 0.5 以下"


# ---- 回填清单 ------------------------------------------------------------

def test_backfill_targets_are_deduped_legs():
    """同一个品种在多行里出现（多腿/空腿都可能）只该补一次。"""
    a = make_opportunity()
    b = make_opportunity()
    targets = backfill_targets([a, b])
    assert targets == [("okx:perp", "L-USDT-SWAP"), ("binance:perp", "SUSDT")]


def test_backfill_targets_is_empty_for_an_empty_board():
    assert backfill_targets([]) == []


def test_lookback_covers_the_chosen_holding_period():
    """固定回看 30 天配上"计划持有 1 月"，stability 永远凑不出一个窗口
    （它要 horizon+12 小时），机会榜整列会恒为"数据不足"——
    那不是数据不够，是参数配错了，而用户完全看不出区别。"""
    from carryfarm.scoring import stability
    from carryfarm.ui.app import _history_days_for

    for horizon_h in (72, 168, 336, 720):
        days = _history_days_for(horizon_h)
        assert days * 24 >= horizon_h + 12, f"{horizon_h}h 的窗口凑不满"
        # 真的喂进去要能摆脱先验
        assert stability([0.0001] * (days * 24), horizon_h, 0.002, 2.0).is_prior is False
    assert _history_days_for(72) == 30, "短持有期不该把回看压到 30 天以下"


# ---- 首屏不联网 ----------------------------------------------------------

def test_building_the_board_never_fetches_history_over_the_network(tmp_path):
    """首屏只读库。历史回填是慢操作，挂在这条路上等于白屏——
    "先出榜、历史后台补、补完再刷新评分"里的"先出榜"就是这一条。
    """
    bf, storage = backfiller(tmp_path)
    fill_history(storage)
    calls: list[str] = []

    class TattlingBackfiller(HistoryBackfiller):
        async def backfill(self, *a, **kw):       # pragma: no cover - 不该被调到
            calls.append("backfill")
            raise AssertionError("首屏不该触发回填")

    tattler = TattlingBackfiller(storage, {}, clock=lambda: NOW_MS)
    feed = PublicFeed(backfiller=tattler, history_days=40)
    rows = feed.rescore([make_opportunity()], horizon_h=72, now_ts=NOW_S)

    assert calls == []
    assert rows[0].stability.is_prior is False, "该读到的历史一条都没少读"


def test_progress_driven_rescore_flips_the_column(tmp_path):
    """端到端：先出榜（数据不足）→ 后台回填 → 重新评分（真实评分）。"""
    storage = Storage(tmp_path / "e2e.db")

    class FakeAdapter:
        def __init__(self, points):
            self.points = sorted(points)

        async def fetch_funding_history(self, symbol, since_ms, limit=1000):
            return tuple(p for p in self.points if p[0] >= since_ms)[:limit]

    h0 = NOW_MS - 40 * DAY_MS
    bf = HistoryBackfiller(storage, {
        "okx:perp": FakeAdapter([(h0 + k * HOUR_MS, Decimal("0.00001"))
                                 for k in range(1, 40 * 24 + 1)]),
        "binance:perp": FakeAdapter([(h0 + k * 8 * HOUR_MS, Decimal("0.0008"))
                                     for k in range(1, 40 * 3 + 1)]),
    }, clock=lambda: NOW_MS)

    feed = PublicFeed(backfiller=bf, history_days=40)
    board = [make_opportunity()]
    assert board[0].stability.is_prior is True         # 首屏：数据不足

    seen: list[tuple[int, int]] = []
    results = asyncio.run(bf.backfill_many(backfill_targets(board), days=40,
                                           on_progress=lambda d, t: seen.append((d, t))))
    assert all(r.ok for r in results)
    assert seen[-1] == (2, 2)

    refreshed = feed.rescore(board, horizon_h=72, now_ts=NOW_S)
    assert refreshed[0].stability.is_prior is False    # 补完：真实评分
    assert refreshed[0].stability.n_samples > 900


# --- 交易所健康度必须一路传到界面 --------------------------------------------


def test_a_dead_venue_is_not_reported_as_connected():
    """`_adapters` 说的是"适配器建起来了"，`venues_with_data` 说的是"它真给了数据"。

    这两者分不清的后果很具体：一家挂掉时界面照样写"已连 6 家"，
    而用户看到的价差是拿五家算出来的。跨所套利里少看一家，
    价差不是"少了几个机会"，是**错的**。
    """
    from carryfarm.models import Venue

    feed = PublicFeed()

    class Boom:
        async def fetch_carry_rates(self):
            raise RuntimeError("限频")

    class Fine:
        async def fetch_carry_rates(self):
            return []

    feed._adapters = {Venue.BINANCE: Fine(), Venue.OKX: Boom()}
    asyncio.run(feed.fetch_funding())

    assert feed.venues_with_data == (Venue.BINANCE,)
    assert Venue.OKX in feed.fetch_errors
    assert "限频" in feed.fetch_errors[Venue.OKX]
    # 适配器还在字典里 —— 正是这一点让 `len(_adapters)` 成为一个骗人的数字
    assert len(feed._adapters) == 2


def test_ui_reports_venue_health_from_venues_with_data():
    """界面的"已连 N 家"必须读 venues_with_data。

    public_feed 长出这个字段是为了守护模式，但界面当时没跟着改，
    于是同一个仓库里两条路径对"连上了吗"给出不同答案。用源码扫描钉住，
    因为这一行退回 `_adapters` 时测试是不会红的——只是数字重新开始骗人。
    """
    import inspect

    from carryfarm.ui import app as ui_app

    src = inspect.getsource(ui_app.refresh_opportunities)
    assert "venues_with_data" in src
    assert "feed._adapters" not in src
    # 挂掉的那几家要写在状态栏上，不能只在日志里
    assert "venues_failed" in src
