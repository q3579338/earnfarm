"""守护模式测试。

守的是守护进程的全部价值：**它得一直活着，而且要能证明自己活着**。
所以这里的断言几乎都不是"功能对不对"，而是"出事之后它还在不在":

1. 任何一环抛异常，循环继续（网络断了、库锁了、告警通道挂了）；
2. 一家交易所挂掉，榜单不空——其余五家照常配对；
3. 收到停止信号，**先落库再退出**；
4. 心跳能把"没机会"和"进程死了"区分开——这两件事在用户那边长得一模一样。

用假 feed 驱动**真实的主循环代码**：另写一套测试专用循环的话，
测过的东西说明不了线上那套对。
"""

from __future__ import annotations

import asyncio
import signal
import time
from dataclasses import dataclass, replace
from decimal import Decimal

import pytest

from carryfarm.config import Config, ConfigError, WatchConfig
from carryfarm.models import BookDepth, CarryRate, Hedge, HedgeStatus, Leg
from carryfarm.public_feed import PublicFeed
from carryfarm.scoring import HistoryStats, LegQuote, ScoredOpportunity, score_pair
from carryfarm.storage import Storage
from carryfarm.vault import Vault
from carryfarm.watch import Watcher, format_status, read_heartbeat

NOW = 1_785_000_000.0


# ---- 素材 ----------------------------------------------------------------

def leg(symbol: str, market: str, rate: float, *, px: float = 100.0,
        depth: float = 2e6, top: float = 2e5, iv: float = 8.0) -> LegQuote:
    half = px * 1e-4 / 2
    return LegQuote(symbol, market, px - half, px + half, top / px, top / px,
                    depth, 10.0, 0.0005, 0.0002, rate, iv, NOW + 4 * 3600,
                    0.0075, -0.0075, 5e7, 2e8)


def make_op(base: str = "AAA", *, with_history: bool = True) -> ScoredOpportunity:
    """一个真能过筛的机会（净年化 ~169%，容量充足）。

    with_history=False 时退化成单点历史，stability 走 is_prior 分支——
    这一路必须**推不出去**，对着 0.35 的先验推送就是对着噪声推送。
    """
    long_leg = leg(f"{base}USDT", "binance:perp", -0.0002)
    short_leg = leg(f"{base}-USDT-SWAP", "okx:perp", 0.0030)
    if with_history:
        hist_l = HistoryStats(tuple([-0.0002] * 90), 8.0)
        hist_s = HistoryStats(tuple([0.0030] * 90), 8.0)
        carry = [0.0004] * 500
    else:
        hist_l = HistoryStats((-0.0002,), 8.0)
        hist_s = HistoryStats((0.0030,), 8.0)
        carry = None
    return score_pair(base, long_leg, short_leg, hist_l, hist_s,
                      notional=50_000, horizon_h=168, now_ts=NOW,
                      hourly_carry=carry)


@dataclass
class FakeResult:
    sent: bool = True


class RecordingEngine:
    """假告警引擎。只记不发——真通道的行为是 alerts 那一轮的测试范围。"""

    channel_names = ("log", "wechat")
    skipped: dict[str, str] = {}

    def __init__(self, *, boom: bool = False) -> None:
        self.alerts: list = []
        self.boom = boom

    async def dispatch(self, alert):
        if self.boom:
            raise RuntimeError("推送通道挂了")
        self.alerts.append(alert)
        return FakeResult(sent=True)

    def titles(self) -> list[str]:
        return [a.title for a in self.alerts]


class FakeFeed:
    """假行情源：既是 async 上下文管理器，也是评分器（rescore）。"""

    def __init__(self, rows, *, fail_times: int = 0, rescore_boom: bool = False,
                 venues=("binance", "okx"), errors: dict | None = None) -> None:
        self.rows = list(rows)
        self.fail_times = fail_times
        self.rescore_boom = rescore_boom
        self.venues_with_data = tuple(venues)
        self.fetch_errors = dict(errors or {})
        self.init_errors: dict = {}
        self.calls = 0
        self.rescores = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def build_opportunities(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("行情端点连不上")
        return list(self.rows)

    def rescore(self, rows, **kwargs):
        self.rescores += 1
        if self.rescore_boom:
            raise RuntimeError("重新评分炸了")
        return list(rows)


class FakeBackfiller:
    def __init__(self) -> None:
        self.targets: list = []

    async def backfill_many(self, targets, days=90, **kwargs):
        self.targets.append(list(targets))
        return [type("R", (), {"ok": True, "market": m, "symbol": s})()
                for m, s in targets]


def fast_config(tmp_path, **overrides) -> Config:
    """测试用的节奏。**下限只在读配置文件时校验**（Config.validate），
    直接构造 WatchConfig 是绕过它的正当途径——这里要的是毫秒级的循环。"""
    wcfg = WatchConfig(market_interval_s=0.01, history_interval_s=0.01,
                       position_interval_ms=5, heartbeat_interval_s=0.01,
                       shutdown_timeout_s=5.0, **overrides)
    return replace(Config(), data_dir=tmp_path, watch=wcfg)


_DEFAULT = object()     # 区分"没指定引擎"和"显式要求没有引擎"


def make_watcher(tmp_path, rows=None, *, engine=_DEFAULT, feed=None, trader=None,
                 storage=None, clock=None, **cfg_overrides):
    config = fast_config(tmp_path, **cfg_overrides)
    storage = storage or Storage(config.db_path)
    feed = feed if feed is not None else FakeFeed(rows if rows is not None else [make_op()])
    watcher = Watcher(config, storage=storage,
                      alerts=RecordingEngine() if engine is _DEFAULT else engine,
                      feed_factory=lambda: feed,
                      backfiller=FakeBackfiller(), trader=trader,
                      backoff_base_s=0.001, backoff_max_s=0.01,
                      **({"clock": clock} if clock else {}))
    return watcher, storage, feed


async def drive(watcher, until, timeout: float = 3.0) -> None:
    """跑真实主循环，直到条件满足或超时，然后走正常停机路径。"""
    task = asyncio.create_task(watcher.run(install_signals=False))
    deadline = time.monotonic() + timeout
    while not until() and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    watcher.request_stop("测试结束")
    await asyncio.wait_for(task, timeout=timeout)


def events(storage: Storage, needle: str) -> list[str]:
    return [r["message"] for r in storage.recent_events(500) if needle in r["message"]]


# ---- 1. 正常路径 ----------------------------------------------------------

def test_market_round_scores_and_alerts(tmp_path):
    watcher, storage, feed = make_watcher(tmp_path)
    rows = asyncio.run(watcher.market_round())
    assert len(rows) == 1
    assert watcher.market_rounds == 1
    assert watcher.alerts_sent == 1
    assert "AAA" in watcher.alerts.titles()[0]
    # 首轮要在库里留一句话：连了几家、榜上几行、几行可执行
    assert events(storage, "首轮行情")


def test_history_round_only_backfills_legs_on_the_board(tmp_path):
    """全市场是六家 3600+ 个永续，光币安 573 个就能在 22 秒内打满它的专用桶。
    守护进程一天跑 48 轮历史，这条底线破了就是稳定被限频。"""
    watcher, storage, feed = make_watcher(tmp_path)
    asyncio.run(watcher.market_round())
    bf = watcher.backfiller
    ok = asyncio.run(watcher.history_round())
    assert ok == 2                                  # 两条腿
    assert bf.targets == [[("binance:perp", "AAAUSDT"), ("okx:perp", "AAA-USDT-SWAP")]]
    assert feed.rescores == 1                       # 补完就地重新评分


def test_history_round_without_a_board_does_nothing(tmp_path):
    watcher, storage, feed = make_watcher(tmp_path)
    assert asyncio.run(watcher.history_round()) == 0
    assert watcher.backfiller.targets == []


def test_prior_only_opportunity_is_never_pushed(tmp_path):
    """历史不足时 stability 返回的 0.35 是先验、不是评分。
    推它等于对着噪声推送——这正是接上真实历史之后才看清的坑。"""
    watcher, storage, _ = make_watcher(tmp_path, rows=[make_op(with_history=False)])
    asyncio.run(watcher.market_round())
    assert watcher.alerts.alerts == []
    assert watcher.alerts_sent == 0


def test_alerts_are_capped_per_round(tmp_path):
    """一次推 20 条等于一条都没推。"""
    rows = [make_op(f"C{i}") for i in range(10)]
    watcher, storage, _ = make_watcher(tmp_path, rows=rows, alert_max_per_round=2)
    asyncio.run(watcher.market_round())
    assert len(watcher.alerts.alerts) == 2


# ---- 2. 异常不中断循环 -----------------------------------------------------

def test_a_broken_feed_does_not_stop_the_loop(tmp_path):
    """网络中断后自动恢复。守护进程唯一不可原谅的失败是静默死掉。"""
    feed = FakeFeed([make_op()], fail_times=3)
    watcher, storage, _ = make_watcher(tmp_path, feed=feed)

    asyncio.run(drive(watcher, lambda: watcher.market_rounds >= 2))

    assert feed.calls > 3, "失败之后必须继续重试"
    assert watcher.market_rounds >= 2, "恢复之后要真的跑起来"
    assert "market" not in watcher.failures, "成功一次就该把失败计数清零"
    assert events(storage, "market 循环失败"), "失败要留痕，不能静默"


def test_a_broken_history_loop_does_not_take_down_the_market_loop(tmp_path):
    """四个循环互不牵连：回填炸了不影响行情。"""
    feed = FakeFeed([make_op()], rescore_boom=True)
    watcher, storage, _ = make_watcher(tmp_path, feed=feed)

    asyncio.run(drive(watcher, lambda: watcher.market_rounds >= 3
                      and watcher.failures.get("history", 0) >= 2))

    assert watcher.market_rounds >= 3
    assert watcher.failures["history"] >= 2
    assert watcher.history_rounds == 0


def test_a_broken_trader_does_not_take_down_the_market_loop(tmp_path):
    class BoomTrader:
        def __init__(self):
            self.calls = 0

        async def step_all(self):
            self.calls += 1
            raise RuntimeError("交易所回执解析失败")

        def runtimes(self):
            return []

        async def stop(self):
            pass

    trader = BoomTrader()
    watcher, storage, _ = make_watcher(tmp_path, trader=trader)
    asyncio.run(drive(watcher, lambda: watcher.market_rounds >= 2
                      and watcher.failures.get("positions", 0) >= 2))

    assert watcher.market_rounds >= 2
    assert watcher.position_steps == 0
    assert watcher.failures["positions"] >= 2


def test_a_dead_alert_channel_does_not_break_the_round(tmp_path):
    """告警是产出，不是心跳。推不出去也得把这一轮跑完。"""
    watcher, storage, _ = make_watcher(tmp_path, engine=RecordingEngine(boom=True))
    rows = asyncio.run(watcher.market_round())
    assert len(rows) == 1
    assert watcher.alerts_sent == 0
    assert events(storage, "告警投递失败")


def test_without_an_alert_engine_alerts_still_leave_a_trace(tmp_path):
    """告警可以没有通道，但不能没有痕迹。"""
    watcher, storage, _ = make_watcher(tmp_path, engine=None)
    asyncio.run(watcher.market_round())
    assert events(storage, "AAA"), "没有引擎时机会必须落进事件表"


def test_backoff_ladder_is_monotonic_and_capped(tmp_path):
    """失败后走独立的退避阶梯：对慢循环是加速恢复，对快循环是防止空转。"""
    watcher, _, _ = make_watcher(tmp_path)
    ladder = [watcher._backoff(n) for n in range(1, 8)]
    assert ladder == sorted(ladder)
    assert ladder[0] == pytest.approx(0.001)
    assert max(ladder) <= 0.01


# ---- 3. 单家交易所故障隔离 --------------------------------------------------

class FakeAdapter:
    """够 PublicFeed 用的最小适配器。boom=True 模拟这家整个挂掉。"""

    def __init__(self, symbol: str, rate: str, *, boom: bool = False) -> None:
        self.symbol = symbol
        self.rate = Decimal(rate)
        self.boom = boom

    async def fetch_carry_rates(self, symbols=None):
        if self.boom:
            raise ConnectionError("connection reset by peer")
        return [CarryRate(market="x:perp", symbol=self.symbol, rate=self.rate,
                          interval_s=28_800,
                          next_settle_ms=int((NOW + 3600) * 1000),
                          ts_ms=int(NOW * 1000))]

    async def fetch_book_depth(self, symbol, band_bp=10.0):
        return BookDepth(symbol=symbol, bid_notional=Decimal("2000000"),
                         ask_notional=Decimal("2000000"), bid_price=Decimal("99.995"),
                         bid_qty=Decimal("2000"), ask_price=Decimal("100.005"),
                         ask_qty=Decimal("2000"), levels_used=10, band_bp=band_bp,
                         ts_ms=int(NOW * 1000))

    async def close(self):
        pass


def test_one_dead_venue_does_not_empty_the_board(tmp_path):
    """单家挂掉只该少掉它自己的配对，其余五家照常出榜。
    这条要是破了，无人值守时会表现成"最近没什么机会"——最骗人的那种故障。"""
    from carryfarm.models import Venue

    feed = PublicFeed()
    feed._adapters = {
        Venue.BINANCE: FakeAdapter("AAAUSDT", "-0.0002"),
        Venue.OKX: FakeAdapter("AAA-USDT-SWAP", "0.0030"),
        Venue.HTX: FakeAdapter("AAA-USDT", "0.0010", boom=True),
    }
    rows = asyncio.run(feed.build_opportunities(notional=50_000, horizon_h=168))

    assert rows, "两家健康的所必须仍然配出机会"
    assert Venue.HTX in feed.fetch_errors, "挂掉的那家要指名道姓，不能只知道'有一家挂了'"
    assert set(feed.venues_with_data) == {Venue.BINANCE, Venue.OKX}
    markets = {rows[0].long.market, rows[0].short.market}
    assert markets == {"binance:perp", "okx:perp"}


def test_venue_failures_are_recorded_and_surfaced(tmp_path):
    from carryfarm.models import Venue

    feed = FakeFeed([make_op()], venues=("binance", "okx"),
                    errors={Venue.HTX: "connection reset"})
    watcher, storage, _ = make_watcher(tmp_path, feed=feed)
    asyncio.run(watcher.market_round())

    assert watcher.venues_failed == {"htx": "connection reset"}
    assert events(storage, "htx 行情拉取失败")
    assert watcher.heartbeat()["venues_failed"] == {"htx": "connection reset"}


def test_stale_threshold_is_never_shorter_than_the_poll_interval(tmp_path):
    """[alerts].stale_after_s 的 300 秒默认值是按 WS 推送定的，
    而守护模式就是 300 秒轮询一次——直接拿来用会每一轮都报"行情陈旧"。"""
    config = replace(Config(), data_dir=tmp_path)      # 行情间隔 300 秒（默认）
    storage = Storage(config.db_path)
    watcher = Watcher(config, storage=storage, alerts=None,
                      feed_factory=lambda: FakeFeed([]), backfiller=FakeBackfiller())
    assert watcher._stale_after_s() >= 3 * config.watch.market_interval_s
    assert watcher._stale_after_s() > float(config.alerts.stale_after_s)


# ---- 4. 心跳：分清"没机会"和"进程死了" --------------------------------------

def test_heartbeat_is_written_and_readable(tmp_path):
    watcher, storage, _ = make_watcher(tmp_path)
    asyncio.run(drive(watcher, lambda: watcher.market_rounds >= 1))

    hb = read_heartbeat(storage)
    assert hb is not None
    assert hb["market_rounds"] >= 1
    assert hb["state"] == "stopped", "优雅退出要留下 stopped 标记"
    assert hb["pid"] > 0
    assert "存活" not in format_status(hb) and "已停止" in format_status(hb)


def test_status_tells_a_live_process_from_a_dead_one():
    base = {"ts": 1000.0, "state": "running", "uptime_s": 3600,
            "heartbeat_interval_s": 60, "market_rounds": 12, "opportunities": 30,
            "actionable": 2, "alerts_sent": 1, "venues_ok": ["binance", "okx"],
            "market_age_s": 120.0, "hedges": 0, "paper": True, "trading": False,
            "alert_channels": ["log"], "failures": {}, "degraded": False}
    assert "存活" in format_status(base, now=1030.0)
    assert "疑似死亡" in format_status(base, now=1000.0 + 3600)
    assert "已停止" in format_status({**base, "state": "stopped"}, now=1030.0)
    assert "没有心跳记录" in format_status(None)


def test_heartbeat_flags_a_stalled_feed(tmp_path):
    """进程活着但行情早断了——心跳必须说出来，否则"很久没告警"仍然有两个解释。"""
    now = [NOW]
    watcher, storage, _ = make_watcher(tmp_path, clock=lambda: now[0])
    asyncio.run(watcher.market_round())
    assert watcher.heartbeat()["degraded"] is False

    now[0] += 3 * 3600                      # 三小时没有新的一轮
    hb = watcher.heartbeat()
    assert hb["degraded"] is True
    assert "没有成功刷新" in hb["degraded_reason"]
    assert "降级" in format_status(hb, now=now[0])


def test_a_stalled_feed_raises_an_alert_once(tmp_path):
    now = [NOW]
    watcher, storage, _ = make_watcher(tmp_path, clock=lambda: now[0])
    asyncio.run(watcher.market_round())
    now[0] += 3 * 3600
    asyncio.run(watcher.heartbeat_round())
    titles = watcher.alerts.titles()
    assert any("停更" in t or "未更新" in t for t in titles)


# ---- 5. 优雅退出：先落库再退 ------------------------------------------------

def accounts(storage: Storage) -> tuple[str, str]:
    from carryfarm.models import Venue
    vault = Vault()
    storage.set_vault_header(vault.initialize("test-password-123"))
    a = storage.add_account(Venue.BINANCE, "long-acct",
                            {"api_key": "k", "api_secret": "s"}, vault, 0)
    b = storage.add_account(Venue.OKX, "short-acct",
                            {"api_key": "k", "api_secret": "s"}, vault, 0)
    return a, b


class FakeRuntime:
    def __init__(self, hedge) -> None:
        self.hedge = hedge
        self.slices_done = 1
        self.slices_total = 3
        self.long_filled = Decimal("10")
        self.short_filled = Decimal("-10")
        self.active_state = type("S", (), {"value": "balanced"})()

    @property
    def net_qty(self):
        return self.long_filled + self.short_filled


class FakeTrader:
    def __init__(self, runtime) -> None:
        self._runtime = runtime
        self.stopped = False
        self.steps = 0

    async def step_all(self):
        self.steps += 1

    def runtimes(self):
        return [self._runtime]

    async def stop(self):
        self.stopped = True


def test_stop_signal_persists_positions_before_exiting(tmp_path):
    """被 kill 的那一刻，分片进度只存在于内存里。不落库的话，
    重启后没人知道昨晚停在哪一步、有没有留下单腿敞口。"""
    config = fast_config(tmp_path)
    storage = Storage(config.db_path)
    acc_l, acc_s = accounts(storage)
    hedge = Hedge(id="deadbeef" * 4, long=Leg("binance:perp", "AAAUSDT", acc_l),
                  short=Leg("okx:perp", "AAA-USDT-SWAP", acc_s),
                  target=Decimal("10"), multiplier=Decimal("1"),
                  status=HedgeStatus.RUNNING)
    trader = FakeTrader(FakeRuntime(hedge))
    watcher, _, _ = make_watcher(tmp_path, storage=storage, trader=trader)

    assert storage.list_hedges() == [], "前提：这个仓位还没进过库"
    asyncio.run(drive(watcher, lambda: trader.steps >= 1))

    saved = storage.list_hedges()
    assert [h.id for h in saved] == [hedge.id], "退出时必须把仓位写回库"
    assert trader.stopped, "落库之前要先把协调器停掉，否则落的是过期状态"
    assert events(storage, "退出落库"), "落库要有可审计的记录"
    assert events(storage, "守护模式退出")


def test_stopping_twice_is_harmless(tmp_path):
    """第二个 Ctrl-C 不该把收尾打断——落库还没做完呢。"""
    watcher, storage, _ = make_watcher(tmp_path)
    watcher.request_stop("第一次")
    watcher.request_stop("第二次")
    assert watcher._stop_reason == "第一次"


def test_signal_handlers_install_on_this_platform(tmp_path):
    """Windows 的 Proactor 事件循环没有 add_signal_handler，必须回落到
    signal.signal——用户的服务器就是 Windows，这条回落路径不是可选项。"""
    watcher, storage, _ = make_watcher(tmp_path)
    saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}

    async def go():
        installed = watcher.install_signal_handlers()
        assert "SIGINT" in installed and "SIGTERM" in installed
        # 信号处理器最终调的就是这个入口
        watcher.request_stop("模拟 SIGTERM")
        assert watcher.stopping is True

    try:
        asyncio.run(go())
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def test_a_real_sigint_exits_gracefully_and_fast(tmp_path):
    """打一发真信号走完整条路：signal → 事件循环 → 落库 → stopped 心跳。

    "快"和"优雅"同样重要：睡 sleep(interval) 的实现会让 Ctrl-C 最坏等满一个
    行情周期（生产配置是 5 分钟），systemd/任务计划等不了那么久，直接 SIGKILL，
    仓位状态就丢了。
    """
    watcher, storage, _ = make_watcher(tmp_path)
    saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}

    async def go():
        task = asyncio.create_task(watcher.run())        # 真的装信号处理器
        while watcher.market_rounds < 1:
            await asyncio.sleep(0.005)
        signal.raise_signal(signal.SIGINT)
        await asyncio.wait_for(task, timeout=5.0)

    try:
        started = time.monotonic()
        asyncio.run(go())
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)

    assert time.monotonic() - started < 5.0
    hb = read_heartbeat(storage)
    assert hb["state"] == "stopped"
    assert "SIGINT" in (hb.get("stop_reason") or "") or watcher._stop_reason


def test_run_once_completes_a_full_cycle(tmp_path):
    """--once 是排错入口：行情、历史、心跳都真的跑一遍。"""
    watcher, storage, feed = make_watcher(tmp_path)
    code = asyncio.run(watcher.run_once())
    assert code == 0
    assert watcher.market_rounds == 1 and watcher.history_rounds == 1
    assert read_heartbeat(storage)["state"] == "stopped"


def test_run_once_reports_failure_but_still_writes_a_heartbeat(tmp_path):
    watcher, storage, _ = make_watcher(tmp_path, feed=FakeFeed([], fail_times=99))
    assert asyncio.run(watcher.run_once()) == 1
    assert read_heartbeat(storage) is not None, "失败也要留下心跳，否则看不出它跑过"


# ---- 6. 自检与仓位告警 -----------------------------------------------------

def test_selfcheck_says_what_this_process_can_and_cannot_do(tmp_path):
    watcher, storage, _ = make_watcher(tmp_path)
    text = "\n".join(watcher.selfcheck().lines())
    assert "推进已有仓位：否" in text          # 没有 Session = 纯看榜
    assert "log" in text and "wechat" in text
    assert str(tmp_path) in text


def test_selfcheck_warns_when_only_the_local_log_channel_is_live(tmp_path):
    class LogOnly(RecordingEngine):
        channel_names = ("log",)

    watcher, storage, _ = make_watcher(tmp_path, engine=LogOnly())
    assert "手机上收不到" in "\n".join(watcher.selfcheck().lines())


def test_trader_warnings_become_alerts(tmp_path):
    """裸腿、撤退、熔断只写进事件表等于没人知道——无人值守时它们最该叫醒人。"""
    trader = FakeTrader(FakeRuntime(None))
    watcher, storage, _ = make_watcher(tmp_path, trader=trader)
    watcher._on_trader_event("error", "safety", "分片 abc001 裸奔超时，进入撤退")
    watcher._on_trader_event("info", "execution", "建仓完成")     # info 不该打扰人

    asyncio.run(watcher.positions_round())
    assert len(watcher.alerts.alerts) == 1
    assert "裸奔" in watcher.alerts.titles()[0]


# ---- 7. 与真实告警引擎的接线 ------------------------------------------------
#
# 上面的用例都用假引擎，验的是循环；这两个用**真的** AlertEngine，
# 验的是接线：dispatch 的返回形状、Alert 的构造、去重是不是真的在生效。
# 两个模块分两轮落地，接线错了单看哪一边的测试都发现不了。

def test_the_real_alert_engine_delivers_and_then_silences(tmp_path):
    from carryfarm.watch import build_alert_engine

    config = fast_config(tmp_path)
    storage = Storage(config.db_path)
    engine = build_alert_engine(config, storage)
    assert engine is not None, "默认配置下至少要有本地日志通道"

    feed = FakeFeed([make_op()])
    watcher = Watcher(config, storage=storage, alerts=engine,
                      feed_factory=lambda: feed, backfiller=FakeBackfiller())

    asyncio.run(watcher.market_round())
    assert watcher.alerts_sent == 1
    assert events(storage, "AAA"), "本地日志通道要把告警落进事件表"

    # 同一个机会、同一个年化档：4 小时静默期内不该再响一次
    asyncio.run(watcher.market_round())
    assert watcher.alerts_sent == 1, "去重没生效的话，用户三天后就把通知关了"


def test_system_alerts_reach_the_real_engine(tmp_path):
    """守护进程自己的故障告警（行情停更）也得能过真引擎——
    这条路上我构造的是 Alert 对象，字段对不上会在这里炸出来。"""
    from carryfarm.watch import build_alert_engine

    now = [NOW]
    config = fast_config(tmp_path)
    storage = Storage(config.db_path)
    watcher = Watcher(config, storage=storage,
                      alerts=build_alert_engine(config, storage),
                      feed_factory=lambda: FakeFeed([make_op()]),
                      backfiller=FakeBackfiller(), clock=lambda: now[0])
    asyncio.run(watcher.market_round())
    now[0] += 3 * 3600
    asyncio.run(watcher.heartbeat_round())

    assert events(storage, "停更") or events(storage, "未更新")


# ---- 8. 配置闸门 ----------------------------------------------------------

def test_market_interval_floor_is_enforced_when_loading_config():
    """一轮完整刷新实测约 105 秒。配得比 3 分钟还短，程序会永远在刷新，
    并且把六家的限频配额全花在自己身上。"""
    with pytest.raises(ConfigError, match="105"):
        replace(Config(), watch=WatchConfig(market_interval_s=60)).validate()
    replace(Config(), watch=WatchConfig(market_interval_s=180)).validate()


def test_watch_section_loads_from_config_toml_and_status_cli_runs(tmp_path, capsys):
    """`run.py --watch --status` 这条链路整个走一遍：读 [watch] 段 → 开库 → 打印死活。
    节奏参数要是没接进 config，用户在服务器上就只能改代码了。"""
    from carryfarm.watch import main

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'data_dir = "{tmp_path.as_posix()}"\n\n'
        "[watch]\nmarket_interval_s = 240.0\nalert_max_per_round = 3\n",
        encoding="utf-8")
    from carryfarm.config import load as load_config
    loaded = load_config(cfg)
    assert loaded.watch.market_interval_s == 240.0
    assert loaded.watch.alert_max_per_round == 3

    assert main(["--status", "--config", str(cfg)]) == 0
    assert "没有心跳记录" in capsys.readouterr().out


def test_history_days_follow_the_holding_period(tmp_path):
    """持有期 720 小时配 30 天回看，stability 一个窗口都凑不满。"""
    watcher, _, _ = make_watcher(tmp_path, horizon_h=720)
    assert watcher.history_days * 24 >= 720 + 12
    fixed, _, _ = make_watcher(tmp_path, horizon_h=72, history_days=45)
    assert fixed.history_days == 45, "显式配了就用配的"


# --- 主密码环境变量：两个名字必须都认 ----------------------------------------


def test_master_password_accepts_both_env_names(monkeypatch):
    """vault.password_from_env() 用 CARRYFARM_PASSWORD，守护模式原本只认
    CARRYFARM_MASTER_PASSWORD。两个名字是两个 agent 分别起的，只认一个的话，
    按另一个名字配好的用户会启动一个**静默只读**的守护进程——
    行情在刷、告警在推、就是永远不推进仓位。这是最难发现的一类故障。
    """
    from carryfarm import watch as watch_mod
    from carryfarm.vault import password_from_env

    monkeypatch.delenv(watch_mod.MASTER_PASSWORD_ENV, raising=False)
    monkeypatch.delenv(watch_mod.LEGACY_PASSWORD_ENV, raising=False)
    assert watch_mod._master_password() == ("", "")

    # vault 那个函数读的就是 LEGACY 这个名字，守护模式必须认同一个
    monkeypatch.setenv(watch_mod.LEGACY_PASSWORD_ENV, "from-vault-name")
    assert password_from_env() == "from-vault-name"
    assert watch_mod._master_password() == ("from-vault-name",
                                            watch_mod.LEGACY_PASSWORD_ENV)

    # 两个都设时，显式的那个赢，且来源要报得出来（日志里要写清用了哪个）
    monkeypatch.setenv(watch_mod.MASTER_PASSWORD_ENV, "explicit")
    assert watch_mod._master_password() == ("explicit",
                                            watch_mod.MASTER_PASSWORD_ENV)

    # 空白不算数：export FOO= 之后不该被当成"设了密码"
    monkeypatch.setenv(watch_mod.MASTER_PASSWORD_ENV, "   ")
    assert watch_mod._master_password() == ("from-vault-name",
                                            watch_mod.LEGACY_PASSWORD_ENV)
