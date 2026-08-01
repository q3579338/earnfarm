"""历史资金费回填器测试。

这里守的是三条线：
  1. 增量必须真的是增量——每天全量重拉全市场是几万个请求，会被限频拍死；
  2. 两腿周期不同必须各自摊平成小时序列再相减，否则 8h 的费率被当成 1h 的用；
  3. 缺口不能无上限前向填充——拿陈旧数据凑够样本数，会让 stability
     从"数据不足"的诚实先验变成一个假的高分，比没有历史更危险。
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from carryfarm.exchanges.base import ExchangeError, RateLimited
from carryfarm.history import (
    DAY_MS,
    HOUR_MS,
    HistoryBackfiller,
    align_hourly,
    hourly_grid,
    market_key,
    venue_limit,
    _with_intervals,
)
from carryfarm.models import Venue
from carryfarm.scoring import stability
from carryfarm.storage import Storage

# 对齐到整 8 小时的一个"现在"，省得测试里出现半小时的边界
NOW_MS = 1_785_000_000_000 // (8 * HOUR_MS) * (8 * HOUR_MS)


def run(coro):
    return asyncio.run(coro)


class FakeAdapter:
    """只实现 fetch_funding_history 的假适配器。

    行为刻意贴着真实适配器：**有更多数据时正好返回 limit 条**，
    这样外层翻页的终止条件（"不满一页就是到头了"）才会被真正走到。
    """

    def __init__(self, points=(), *, fail_with=None, fail_times=0, delay=0.0):
        self.points = sorted((int(ts), Decimal(str(r))) for ts, r in points)
        self.calls: list[tuple[str, int, int]] = []
        self.fail_with = fail_with
        self.fail_times = fail_times
        self.delay = delay
        self.inflight = 0
        self.max_inflight = 0

    async def fetch_funding_history(self, symbol, since_ms, limit=1000):
        self.calls.append((symbol, int(since_ms), int(limit)))
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail_times > 0:
                self.fail_times -= 1
                raise self.fail_with
            rows = [p for p in self.points if p[0] >= since_ms]
            return tuple(rows[:limit])
        finally:
            self.inflight -= 1


def make(tmp_path, adapters, **kw):
    """默认注入一个假 sleeper：重试退避不该让测试真的睡。"""
    storage = Storage(tmp_path / "h.db")
    slept: list[float] = []

    async def sleeper(sec):
        slept.append(sec)

    kw.setdefault("sleeper", sleeper)
    kw.setdefault("clock", lambda: NOW_MS)
    bf = HistoryBackfiller(storage, adapters, **kw)
    return bf, storage, slept


def series(start_ms, interval_h, count, rate):
    """从 start_ms 起、每 interval_h 小时一条的结算序列。"""
    step = interval_h * HOUR_MS
    return [(start_ms + i * step, Decimal(str(rate))) for i in range(count)]


# ---- 增量 ----------------------------------------------------------------

def test_incremental_only_fetches_the_gap(tmp_path):
    """库里有 30 天数据，再拉 30 天时只能请求最后一条之后的部分。"""
    pts = series(NOW_MS - 30 * DAY_MS, 8, 90, "0.0001")
    ad = FakeAdapter(pts)
    bf, storage, _ = make(tmp_path, {Venue.BINANCE: ad})

    first = run(bf.backfill(Venue.BINANCE, "BTCUSDT", days=30))
    assert first.ok and first.fetched == 90
    assert first.incremental is False              # 第一次必然是深挖
    assert ad.calls[0][1] == NOW_MS - 30 * DAY_MS

    # 交易所又出了两条新的
    ad.points.extend(series(pts[-1][0] + 8 * HOUR_MS, 8, 2, "0.0002"))
    ad.points.sort()
    ad.calls.clear()

    second = run(bf.backfill(Venue.BINANCE, "BTCUSDT", days=30))
    assert second.incremental is True
    assert len(ad.calls) == 1
    assert ad.calls[0][1] == pts[-1][0] + 1, "增量必须从库里最新一条的下一毫秒开始"
    assert second.fetched == 2, "只该拿回缺的那两条，而不是重拉 90 条"

    rows = storage.load_funding_series("binance:perp", "BTCUSDT", 0)
    assert len(rows) == 92
    assert len({ts for ts, _, _ in rows}) == 92, "不能有重复点"


def test_deep_refetch_when_db_shallower_than_request(tmp_path):
    """只看最新一条的实现会有个坑：用户把回溯从 3 天调到 30 天，
    程序会一直拿那 3 天装作有 30 天。必须按最老一条判断要不要往前深挖。"""
    ad = FakeAdapter(series(NOW_MS - 30 * DAY_MS, 8, 90, "0.0001"))
    bf, storage, _ = make(tmp_path, {Venue.BINANCE: ad})

    run(bf.backfill(Venue.BINANCE, "BTCUSDT", days=3))
    shallow = storage.earliest_funding_ms("binance:perp", "BTCUSDT")
    assert shallow >= NOW_MS - 4 * DAY_MS

    ad.calls.clear()
    deeper = run(bf.backfill(Venue.BINANCE, "BTCUSDT", days=30))
    assert deeper.incremental is False
    assert ad.calls[0][1] == NOW_MS - 30 * DAY_MS
    assert storage.earliest_funding_ms("binance:perp", "BTCUSDT") < shallow


def test_truncated_window_is_not_mistaken_for_the_exchange_wall(tmp_path):
    """Bybit / Gate 是从"现在"往回翻的：窗口里的条数超过单次上限时，
    它们保留**最新**那一批，最老一条自然明显晚于请求起点——和"这家只有
    这么多历史"长得一模一样。误记成墙，该品种的历史深度就被永久钉死在
    截断的位置，之后连试都不会再试。

    判据只能是"某一页没装满"：装满了就说明最老那条只是条数上限切出来的边界。
    """

    class NewestFirstAdapter(FakeAdapter):
        async def fetch_funding_history(self, symbol, since_ms, limit=1000):
            self.calls.append((symbol, int(since_ms), int(limit)))
            rows = [p for p in self.points if p[0] >= since_ms]
            return tuple(rows[-limit:])          # 超出上限时保留最新端

    ad = NewestFirstAdapter(series(NOW_MS - 90 * DAY_MS, 8, 270, "0.0001"))
    bf, storage, _ = make(tmp_path, {Venue.BYBIT: ad}, page_limit=10)

    res = run(bf.backfill(Venue.BYBIT, "BTCUSDT", days=90))
    assert res.ok
    assert storage.get_history_floor("bybit:perp", "BTCUSDT") is None, \
        "被条数上限切出来的边界不是交易所的回溯墙"


def test_history_wall_stops_repeated_deep_digging(tmp_path):
    """交易所只有 10 天历史但用户要 90 天时，不能每天都白挖一次 80 天的空气。"""
    ad = FakeAdapter(series(NOW_MS - 10 * DAY_MS, 8, 30, "0.0001"))
    bf, storage, _ = make(tmp_path, {Venue.BINANCE: ad})

    run(bf.backfill(Venue.BINANCE, "BTCUSDT", days=90))
    assert storage.get_history_floor("binance:perp", "BTCUSDT") is not None

    ad.calls.clear()
    again = run(bf.backfill(Venue.BINANCE, "BTCUSDT", days=90))
    assert again.incremental is True, "撞过墙之后应该转增量"
    assert ad.calls[0][1] > NOW_MS - 90 * DAY_MS


# ---- 分页 ----------------------------------------------------------------

def test_pagination_stitches_without_gaps_or_dupes(tmp_path):
    """按 settle_ms 向前滚动翻页：三页拼起来必须不重不漏。"""
    pts = series(NOW_MS - 20 * DAY_MS, 8, 12, "0.0003")
    ad = FakeAdapter(pts)
    bf, storage, _ = make(tmp_path, {Venue.BINANCE: ad}, page_limit=5)

    res = run(bf.backfill(Venue.BINANCE, "BTCUSDT", days=30))
    assert res.pages == 3                     # 5 + 5 + 2
    assert [c[1] for c in ad.calls] == [
        NOW_MS - 30 * DAY_MS, pts[4][0] + 1, pts[9][0] + 1,
    ], "游标必须是上一页最后一条的时间 +1，不能用页号"

    rows = storage.load_funding_series("binance:perp", "BTCUSDT", 0)
    assert [ts for ts, _, _ in rows] == [ts for ts, _ in pts]
    assert res.fetched == 12


def test_pagination_stops_when_cursor_cannot_advance(tmp_path):
    """HTX / Bitget 是按页号翻的，会无视 since 一直返回同一批。
    游标不前进就必须停，否则是个打接口的死循环。"""

    class StuckAdapter(FakeAdapter):
        async def fetch_funding_history(self, symbol, since_ms, limit=1000):
            self.calls.append((symbol, int(since_ms), int(limit)))
            return tuple(self.points[:limit])       # 永远返回最老那批

    ad = StuckAdapter(series(NOW_MS - 20 * DAY_MS, 8, 6, "0.0003"))
    bf, storage, _ = make(tmp_path, {Venue.HTX: ad}, page_limit=3)

    res = run(bf.backfill(Venue.HTX, "BTC-USDT", days=30))
    assert res.ok
    assert len(ad.calls) == 2, f"应在第二轮发现游标不前进就停，实际 {len(ad.calls)} 轮"


# ---- 小时对齐 ------------------------------------------------------------

def test_align_1h_leg_against_8h_leg(tmp_path):
    """手算例子：8h 腿每期 0.0008、1h 腿每期 0.00002。

    摊成小时后：空腿 0.0008/8 = 0.0001，多腿 0.00002/1 = 0.00002，
    carry = short − long = 0.00008，逐小时都应该是这个数。
    直接拿 0.0008 − 0.00002 相减的实现会得到 0.00078，差了近十倍。
    """
    storage = Storage(tmp_path / "h.db")
    h0 = NOW_MS - 48 * HOUR_MS
    # 8h 腿：3 个结算点，各覆盖之前 8 小时 → 覆盖 h0 .. h0+23
    storage.save_funding("binance:perp", "AAA", 28800,
                         [(h0 + k * 8 * HOUR_MS, Decimal("0.0008")) for k in (1, 2, 3)])
    # 1h 腿：24 个结算点，各覆盖之前 1 小时 → 同样覆盖 h0 .. h0+23
    storage.save_funding("okx:perp", "AAA-SWAP", 3600,
                         [(h0 + k * HOUR_MS, Decimal("0.00002")) for k in range(1, 25)])

    bf = HistoryBackfiller(storage, {}, clock=lambda: NOW_MS)
    carry = bf.hourly_carry("okx:perp", "AAA-SWAP", "binance:perp", "AAA", days=10)

    assert len(carry) == 24
    assert all(abs(v - 0.00008) < 1e-12 for v in carry), carry[:3]

    # 定向反过来，序列必须整体变号——取绝对值的实现在这里会两次都返回正数
    mirrored = bf.hourly_carry("binance:perp", "AAA", "okx:perp", "AAA-SWAP", days=10)
    assert all(abs(a + b) < 1e-15 for a, b in zip(carry, mirrored))


def test_hourly_grid_covers_the_period_before_settlement():
    """一条 t 时刻结算的费率覆盖的是它**之前**的 T 小时——资金费是对已持有的
    那段时间收的。摊到未来是把因果搞反了。"""
    t = 100 * 8 * HOUR_MS
    grid = hourly_grid([(t, Decimal("0.0008"), 28800)])
    end_h = t // HOUR_MS
    assert set(grid) == set(range(end_h - 8, end_h))
    assert all(abs(v - 0.0001) < 1e-15 for v in grid.values())


def test_settlement_slightly_before_the_hour_is_rounded():
    """Gate 返回过 23:59:59 这种时刻。直接整除会把整个覆盖窗口整体挪一格。"""
    t = 100 * 8 * HOUR_MS
    grid = hourly_grid([(t - 1000, Decimal("0.0008"), 28800)])
    end_h = t // HOUR_MS
    assert set(grid) == set(range(end_h - 8, end_h))


# ---- 缺口 ----------------------------------------------------------------

def test_small_gap_is_forward_filled(tmp_path):
    """漏一次 8h 结算是常事（接口抖动），不该让整段样本作废。"""
    storage = Storage(tmp_path / "h.db")
    h0 = NOW_MS - 20 * DAY_MS
    long_pts = [(h0 + k * HOUR_MS, Decimal("0.00001")) for k in range(1, 97)]
    storage.save_funding("okx:perp", "L", 3600, long_pts)
    # 8h 腿跳过第 5 期（缺口 8 小时，在 12 小时上限内）
    short_pts = [(h0 + k * 8 * HOUR_MS, Decimal("0.0008"))
                 for k in range(1, 13) if k != 5]
    storage.save_funding("binance:perp", "S", 28800, short_pts)

    bf = HistoryBackfiller(storage, {}, clock=lambda: NOW_MS)
    carry = bf.hourly_carry("okx:perp", "L", "binance:perp", "S", days=30)
    assert len(carry) >= 90, "8 小时的洞应该被前向填充，而不是把序列截断"


def test_large_gap_truncates_the_series(tmp_path):
    """连着漏两期（16 小时）就不是抖动了。前面那段样本必须丢掉——
    保留它等于告诉 stability「这段时间我一直在观测」，而事实是没有。"""
    storage = Storage(tmp_path / "h.db")
    h0 = NOW_MS - 20 * DAY_MS
    storage.save_funding("okx:perp", "L", 3600,
                         [(h0 + k * HOUR_MS, Decimal("0.00001")) for k in range(1, 97)])
    # 跳过第 5、6 期 → 与前一条相隔 24 小时，实际覆盖后仍留 16 小时空洞
    short_pts = [(h0 + k * 8 * HOUR_MS, Decimal("0.0008"))
                 for k in range(1, 13) if k not in (5, 6)]
    storage.save_funding("binance:perp", "S", 28800, short_pts)

    bf = HistoryBackfiller(storage, {}, clock=lambda: NOW_MS)
    carry = bf.hourly_carry("okx:perp", "L", "binance:perp", "S", days=30)
    # 缺口后只剩第 7~12 期共 48 小时（末尾对齐到 1h 腿的覆盖范围）
    assert 40 <= len(carry) <= 50, len(carry)


def test_align_hourly_keeps_only_the_latest_contiguous_run():
    """截断的语义是「只保留最近一段连续样本」，不是「把洞填上继续用」。"""
    long_grid = {h: 0.0 for h in range(0, 100)}
    short_grid = {h: 1.0 for h in range(0, 20)}
    short_grid.update({h: 2.0 for h in range(60, 100)})
    out = align_hourly(long_grid, short_grid, max_fill_h=12)
    assert len(out) == 40
    assert all(v == 2.0 for v in out)


def test_align_hourly_uses_only_the_overlap():
    """一条腿 2020 年就上线、另一条上个月才有，不重叠的部分不能算进来。"""
    long_grid = {h: 0.0 for h in range(0, 1000)}
    short_grid = {h: 1.0 for h in range(900, 1000)}
    assert len(align_hourly(long_grid, short_grid)) == 100


# ---- 周期推断 ------------------------------------------------------------

def test_missing_point_does_not_halve_the_rate():
    """漏一条数据会造成 16h 的相邻间隔。直接把它当周期，
    一条 8h 费率就被摊到 16 小时上、数值凭空腰斩，而且不会有任何报错。"""
    pts = [(0, Decimal("0.0008")),
           (8 * HOUR_MS, Decimal("0.0008")),
           (16 * HOUR_MS, Decimal("0.0008")),
           (32 * HOUR_MS, Decimal("0.0008"))]      # 中间漏了 24h 那条
    rows = _with_intervals(pts, anchor_ms=None)
    assert [iv for _, _, iv in rows] == [28800, 28800, 28800, 28800]


def test_interval_switch_is_recorded_per_point():
    """Bybit 会把合约从 8h 动态切成 1h。一批数据里出现两种周期是正常的，
    整批共用一个 interval_s 的实现会把切换后的费率全部算错 8 倍。"""
    pts = series(0, 8, 6, "0.0008") + series(6 * 8 * HOUR_MS + HOUR_MS, 1, 10, "0.0001")
    rows = _with_intervals(pts, anchor_ms=None)
    assert {iv for _, _, iv in rows} == {28800, 3600}


def test_interval_switch_at_a_clean_boundary():
    """切换发生在结算边界上时更难判：切换后 1h 占多数，中位数变成 1h，
    早期真实的 8h 间隔会被"不超过中位数 1.5 倍"这条规则冤枉成漏点。
    靠"重复出现的间隔才是真周期"兜住。"""
    pts = series(0, 8, 6, "0.0008") + series(41 * HOUR_MS, 1, 12, "0.0001")
    rows = _with_intervals(pts, anchor_ms=None)
    assert [iv for _, _, iv in rows[:6]] == [28800] * 6
    assert [iv for _, _, iv in rows[6:]] == [3600] * 12


def test_isolated_24h_gap_is_not_mistaken_for_a_24h_period():
    """24h 本身是合法周期，所以光靠白名单挡不住"漏了两条 8h 数据"。"""
    pts = [(0, Decimal("0.0008")), (8 * HOUR_MS, Decimal("0.0008")),
           (32 * HOUR_MS, Decimal("0.0008")), (40 * HOUR_MS, Decimal("0.0008"))]
    rows = _with_intervals(pts, anchor_ms=None)
    assert [iv for _, _, iv in rows] == [28800] * 4


def test_anchor_gives_the_first_incremental_point_a_predecessor():
    """增量批次的第一条点如果没有前驱，周期只能靠猜。"""
    rows = _with_intervals([(8 * HOUR_MS, Decimal("0.0008"))], anchor_ms=0)
    assert rows[0][2] == 28800


# ---- 健壮性 --------------------------------------------------------------

def test_one_symbol_failure_does_not_affect_others(tmp_path):
    bad = FakeAdapter(fail_with=ExchangeError("okx", "51001", "不存在的合约"),
                      fail_times=99)
    good = FakeAdapter(series(NOW_MS - 10 * DAY_MS, 8, 30, "0.0002"))
    bf, storage, _ = make(tmp_path, {Venue.OKX: bad, Venue.BINANCE: good})

    results = run(bf.backfill_many(
        [(Venue.OKX, "GHOST-USDT-SWAP"), (Venue.BINANCE, "BTCUSDT")], days=30))

    assert results[0].ok is False and "51001" in results[0].error
    assert results[1].ok is True and results[1].fetched == 30
    assert storage.load_funding_series("binance:perp", "BTCUSDT", 0)


def test_unregistered_market_is_reported_not_raised(tmp_path):
    bf, _, _ = make(tmp_path, {})
    res = run(bf.backfill(Venue.GATE, "BTC_USDT", days=30))
    assert res.ok is False and "未登记" in res.error


def test_retries_with_exponential_backoff(tmp_path):
    ad = FakeAdapter(series(NOW_MS - 5 * DAY_MS, 8, 15, "0.0001"),
                     fail_with=RateLimited("binance", "429", "too many"),
                     fail_times=2)
    bf, storage, slept = make(tmp_path, {Venue.BINANCE: ad},
                              retry_base_s=0.5, retry_max_s=8.0)
    res = run(bf.backfill(Venue.BINANCE, "BTCUSDT", days=30))
    assert res.ok and res.fetched == 15
    assert slept == [0.5, 1.0], slept


def test_business_error_is_not_retried(tmp_path):
    ad = FakeAdapter(fail_with=ExchangeError("binance", "-1121", "Invalid symbol"),
                     fail_times=99)
    bf, _, slept = make(tmp_path, {Venue.BINANCE: ad})
    res = run(bf.backfill(Venue.BINANCE, "NOPEUSDT", days=30))
    assert res.ok is False
    assert len(ad.calls) == 1, "业务错误重试多少次都是同样的结果，还白烧限频配额"
    assert slept == []


def test_requests_are_paced_within_a_venue(tmp_path):
    """币安的历史端点吃 500次/5分钟 的**专用桶**，而且实测**不返回
    X-MBX-USED-WEIGHT-* 头**——适配器自带的"看用量主动退避"在这条路上是瞎的。
    base.py 的 _quota 又只是并发信号量、不是令牌桶，8 路并发能在 22 秒内
    打完 573 个永续，超配额 6 倍。只能按时间硬节流。
    """
    paced: list[float] = []

    async def pace_sleeper(sec):
        paced.append(sec)

    ad = FakeAdapter(series(NOW_MS - 5 * DAY_MS, 8, 10, "0.0001"))
    bf, _, slept = make(tmp_path, {Venue.BINANCE: ad},
                        pace_s={"binance:perp": 0.65}, pace_sleeper=pace_sleeper)
    run(bf.backfill_many([(Venue.BINANCE, f"SYM{i}") for i in range(5)], days=30))

    assert len(ad.calls) == 5
    assert len(paced) == 4, "第一次不用等，之后每次都得排开"
    assert all(0 < s <= 0.65 for s in paced)
    assert slept == [], "节流不能记到退避的账上——两者的语义完全不同"


def test_pacing_is_per_venue(tmp_path):
    """一家的配额和另一家无关。全局节流会让六家一起变慢到最严那家的速度。"""
    paced: list[float] = []

    async def pace_sleeper(sec):
        paced.append(sec)

    pts = series(NOW_MS - 5 * DAY_MS, 8, 10, "0.0001")
    bf, _, _ = make(tmp_path, {Venue.BINANCE: FakeAdapter(pts),
                               Venue.OKX: FakeAdapter(pts)},
                    pace_s={"binance:perp": 0.65}, pace_sleeper=pace_sleeper)
    run(bf.backfill_many([(Venue.OKX, f"S{i}-USDT-SWAP") for i in range(5)], days=30))
    assert paced == [], "没配节流的家不该被拖慢"


def test_backfill_many_reports_progress(tmp_path):
    """一批几十个品种要跑十几秒到几分钟，界面上不给进度用户只会以为程序卡死。"""
    seen: list[tuple[int, int]] = []
    ad = FakeAdapter(series(NOW_MS - 5 * DAY_MS, 8, 10, "0.0001"))
    bf, _, _ = make(tmp_path, {Venue.BINANCE: ad})
    run(bf.backfill_many([(Venue.BINANCE, f"SYM{i}") for i in range(4)], days=30,
                         on_progress=lambda done, total: seen.append((done, total))))
    assert [d for d, _ in seen] == [1, 2, 3, 4]
    assert all(t == 4 for _, t in seen)


def test_progress_callback_failure_does_not_break_backfill(tmp_path):
    """回调抛异常是显示逻辑的问题，不该把数据回填也带崩。"""
    ad = FakeAdapter(series(NOW_MS - 5 * DAY_MS, 8, 10, "0.0001"))
    bf, storage, _ = make(tmp_path, {Venue.BINANCE: ad})

    def boom(done, total):
        raise RuntimeError("界面已经关了")

    results = run(bf.backfill_many([(Venue.BINANCE, "BTCUSDT")], days=30,
                                   on_progress=boom))
    assert results[0].ok
    assert storage.load_funding_series("binance:perp", "BTCUSDT", 0)


def test_concurrency_is_capped(tmp_path):
    """全市场回填必须受控——一次把六家配额打满，连风控撤单都抢不到。"""
    ad = FakeAdapter(series(NOW_MS - 5 * DAY_MS, 8, 10, "0.0001"), delay=0.01)
    bf, _, _ = make(tmp_path, {Venue.BINANCE: ad}, max_concurrency=4)
    run(bf.backfill_many([(Venue.BINANCE, f"SYM{i}") for i in range(12)], days=30))
    assert ad.max_inflight <= 4, ad.max_inflight
    assert ad.max_inflight > 1, "并发上限不该退化成串行"


# ---- 各家回溯上限 --------------------------------------------------------

def test_lookback_is_clamped_to_venue_limit(tmp_path):
    """Gate 是 180 天硬上限，from 更早直接 HTTP 400。请求 365 天要先夹住。"""
    ad = FakeAdapter(series(NOW_MS - 100 * DAY_MS, 8, 300, "0.0001"))
    bf, _, _ = make(tmp_path, {Venue.GATE: ad})
    res = run(bf.backfill(Venue.GATE, "BTC_USDT", days=365))
    assert res.venue_capped is True
    assert ad.calls[0][1] == NOW_MS - 180 * DAY_MS


def test_bitget_record_cap_translates_to_fewer_days_on_fast_symbols():
    """Bitget 是 270 条硬顶，不是 90 天硬顶：同一个上限在 1h 品种上只有 11 天。"""
    limit = venue_limit(Venue.BITGET)
    assert limit.effective_days(90, interval_h=8) == 90
    assert limit.effective_days(90, interval_h=1) == pytest.approx(11.25)
    assert venue_limit(Venue.OKX).effective_days(365, 8) == 93
    assert venue_limit(Venue.BINANCE).effective_days(365, 8) == 365


def test_market_key_normalization():
    assert market_key(Venue.BINANCE) == "binance:perp"
    assert market_key("binance") == "binance:perp"
    assert market_key("binance:perp") == "binance:perp"


# ---- 端到端 --------------------------------------------------------------

def test_end_to_end_stability_is_no_longer_a_prior(tmp_path):
    """回填 → hourly_carry → stability：必须给出真实评分而不是 0.35 的先验。

    这就是这个模块存在的理由：没有历史时 stability 一律返回 is_prior=True、
    score=0.35，机会榜的稳定性整列都是同一个数，等于白算。
    """
    days = 40
    long_pts = series(NOW_MS - days * DAY_MS, 1, days * 24, "0.00001")
    short_pts = series(NOW_MS - days * DAY_MS, 8, days * 3, "0.0008")
    bf, storage, _ = make(tmp_path, {
        Venue.OKX: FakeAdapter(long_pts),
        Venue.BINANCE: FakeAdapter(short_pts),
    })

    results = run(bf.backfill_pair(Venue.OKX, "L-USDT-SWAP",
                                   Venue.BINANCE, "SUSDT", days=days))
    assert all(r.ok for r in results)
    assert results[0].covered_days > 39

    carry = bf.hourly_carry("okx:perp", "L-USDT-SWAP",
                            "binance:perp", "SUSDT", days=days)
    assert len(carry) > 900
    # 空腿 0.0008/8h = 0.0001/h，多腿 0.00001/h → 0.00009/h
    assert all(abs(v - 0.00009) < 1e-12 for v in carry)

    stab = stability(carry, horizon_h=24, cost_rt=0.002, kappa=2.0)
    assert stab.is_prior is False, "有 40 天真实历史了，不该再退回先验"
    assert stab.score > 0.35
    assert stab.p_win > 0.9
    assert stab.median_run_h > 500
    assert stab.apr_median > 0

    # stats_for 走的是每期原始费率（和 LegQuote.funding_now 同一套符号和口径），
    # 不是小时化后的值——scoring.forecast_path 拿它当 AR(1) 的收敛目标
    hs = bf.stats_for(Venue.BINANCE, "SUSDT", days=days)
    assert hs.interval_h == 8.0
    assert abs(hs.median() - 0.0008) < 1e-12
    assert len(hs.rates) == days * 3


def test_flipping_carry_scores_worse_than_steady(tmp_path):
    """符号乱翻的费差必须被压下去——这正是历史序列的价值所在。"""
    storage = Storage(tmp_path / "h.db")
    h0 = NOW_MS - 40 * DAY_MS
    n = 40 * 24
    storage.save_funding("okx:perp", "L", 3600,
                         [(h0 + k * HOUR_MS, Decimal("0")) for k in range(1, n + 1)])
    storage.save_funding("binance:perp", "STEADY", 3600,
                         [(h0 + k * HOUR_MS, Decimal("0.0001")) for k in range(1, n + 1)])
    storage.save_funding("binance:perp", "FLIP", 3600,
                         [(h0 + k * HOUR_MS,
                           Decimal("0.0001") if k % 2 else Decimal("-0.0001"))
                          for k in range(1, n + 1)])

    bf = HistoryBackfiller(storage, {}, clock=lambda: NOW_MS)
    steady = stability(bf.hourly_carry("okx:perp", "L", "binance:perp", "STEADY", 40),
                       24, 0.002, 2.0)
    flip = stability(bf.hourly_carry("okx:perp", "L", "binance:perp", "FLIP", 40),
                     24, 0.002, 2.0)
    assert steady.score > flip.score * 3, (steady.score, flip.score)
