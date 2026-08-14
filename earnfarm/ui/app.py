"""NiceGUI 应用入口。

默认拉**真实公开行情**——资金费率和盘口都是无需签名的公开端点，
所以不配 API key 就能看到此刻真实的机会。只有下单和查持仓才需要 key。

监听地址默认 127.0.0.1——不像原版官方文档那条 `--net host -e PORT=80` 的命令
直接把交易面板挂到公网。要对外必须显式开 allow_public 并配 TLS。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import time
from typing import Sequence

from nicegui import app as ng_app, ui

from decimal import Decimal

from ..config import MIN_MARKET_INTERVAL_S, Config, load as load_config
from ..history import DEFAULT_PACE_S, HistoryBackfiller
from ..models import Venue
from ..paper import PaperGateway
from ..public_feed import (
    DEFAULT_HISTORY_DAYS,
    PublicFeed,
    backfill_targets,
    history_days_for,
    open_public_adapters,
)
from ..session import Session
from ..trader import Trader
from . import theme
from .accounts import VENUE_LABELS, AccountsPanel
from .hedges import HedgesPanel, open_create_dialog
from .nav import module_nav
from .opportunities import OpportunityBoard
from .analysis_page import build_analysis_page
from .premium_page import build_premium_page

# 隔多久重新补一次增量历史。行情 60 秒刷一次，但历史一天才结算 1~24 次，
# 跟着行情一起刷等于拿几十倍的请求换零信息量。
HISTORY_REFRESH_S = 1800
# 回填并发。适配器内部的 _quota 是第二层闸；这里压得低是因为回填是后台任务，
# 抢配额抢赢了行情刷新反而是帮倒忙
HISTORY_CONCURRENCY = 3
# 一轮刷新实测约 105 秒。超过这个上限还没结束就认定它已经死了（多半是客户端
# 断开导致 finally 没跑到），强行放行下一轮——否则界面会永远停在"正在拉取"。
STUCK_REFRESH_S = 300.0

# 自动刷新可选的间隔。**最短 3 分钟不是保守，是硬下限**：一轮完整刷新实测约 105 秒
# （六家全市场费率 + 逐对深度），配得比这还短的唯一后果是永远在刷新、并且把六家的
# 限频配额全花在自己身上。这里的下限跟守护模式共用 config.MIN_MARKET_INTERVAL_S，
# 两处配一个数才不会出现"界面能配 1 分钟、守护模式却拒绝启动"的鬼故事。
REFRESH_INTERVAL_CHOICES = (180, 300, 600, 900, 1800, 3600)
DEFAULT_REFRESH_INTERVAL_S = 300
# 自动刷新的开关与间隔存在库的 meta 表里：这是**用户的选择**，不是运行时状态，
# 重启一次就忘掉的设置等于没有设置。
META_AUTO_REFRESH = "ui:auto_refresh"
META_REFRESH_INTERVAL = "ui:refresh_interval_s"
# 勾选了哪些交易所（csv 存 venue.value）。跨所配对至少要两家——
# 只剩一家时每个币都自成 bucket，榜单必然全空，而界面上看不出为什么
MIN_VENUES = 2
META_VENUES = "ui:venues"


def _interval_label(seconds: int) -> str:
    return f"{seconds // 60} 分钟" if seconds < 3600 else f"{seconds // 3600} 小时"


class AppState:
    """界面共享的运行时状态。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.session = Session(config)
        self.board = OpportunityBoard(on_create_hedge=self.open_create)
        self.last_refresh: float = 0.0
        self.refreshing = False
        self.refresh_started_at: float = 0.0
        self.feed_error = ""
        # 状态文案存在共享状态里，各客户端的定时器自己同步过去。
        # 只往触发那一轮的客户端的标签里写，别的标签页会永远是空白
        self.feed_status_text = ""
        self.feed_status_color = theme.NEUTRAL
        self.history_status_color = theme.NEUTRAL
        self.venues_ok: list[str] = []
        # 建不起来 / 拉不到数据的交易所 → 原因。空 dict = 六家都给了数据
        self.venues_failed: dict[str, str] = {}
        self.trader: Trader | None = None
        self._on_hedge_change = None

        # ---- 历史回填 ----
        # 回填器的适配器与 PublicFeed 的**分开建**：PublicFeed 每次刷新都
        # 进出一次 async with、把自己的 httpx 连接池关掉，而回填是跨越多次
        # 刷新的长任务，借用它的连接会在半路被关掉。
        self._history_adapters: dict[Venue, object] = {}
        self.backfiller: HistoryBackfiller | None = None
        # 两轮刷新之间的最小间隔。一轮实测约 105 秒（六家全市场费率 + 逐对深度），
        # 留出富余免得把交易所的限频配额全花在自己身上。
        # 用户可以在机会榜上改，改完存库；这里读的是上次存的值
        # 默认关：行情重拉是显式动作的哲学贯穿全站（溢价页、复盘页都无定时器）。
        # 用户开过一次就存进 meta，之后跟随用户的选择
        self.auto_refresh: bool = False
        self.min_refresh_gap_s: float = float(DEFAULT_REFRESH_INTERVAL_S)
        # 勾选的交易所。默认全家；这是**数据源**选择不是过滤器——
        # 改选要重拉行情（少拉的家连请求都不发，限频配额跟着省下来）。
        # 代际计数解决"改选时上一轮还在跑"的时序：那一轮完成会盖掉 last_refresh，
        # 拿 last_refresh=0 当触发信号会被它覆盖，重拉就得干等满一个间隔。
        # 比较 refreshed_rev != venues_rev 不会被任何完成动作洗掉
        self.enabled_venues: tuple[Venue, ...] = tuple(Venue)
        self.venues_rev = 0
        self.refreshed_rev = 0
        # 最后一次改选的时刻，给重拉做防抖：用户在菜单里连点几家是一个动作，
        # tick 落在点击序列半路会拿着中间态白拉一轮全市场
        self.venues_changed_at = 0.0
        self._load_refresh_prefs()
        self.history_days = DEFAULT_HISTORY_DAYS
        self.history_busy = False
        self.history_done_at: float = 0.0
        self.history_seen: set[tuple[str, str]] = set()   # 已补过的 (market, symbol)
        self.history_status = ""
        self.history_task: asyncio.Task | None = None

    # ---- 自动刷新 ----

    def _load_refresh_prefs(self) -> None:
        """从库里读回上次的选择。读坏了就退回默认值——
        一条脏 meta 不该让界面起不来。"""
        try:
            saved = self.session.storage.get_meta(META_AUTO_REFRESH)
            if saved is not None:
                self.auto_refresh = saved == "1"
            raw = self.session.storage.get_meta(META_REFRESH_INTERVAL)
            if raw:
                # 存进来的值必须落回下拉框的某个选项上，否则下拉框会显示空白——
                # 用户看到一个没有值的间隔框，只能猜它到底几分钟刷一次
                want = self._clamp_interval(float(raw))
                self.min_refresh_gap_s = float(
                    min(REFRESH_INTERVAL_CHOICES, key=lambda s: abs(s - want)))
            saved_venues = self.session.storage.get_meta(META_VENUES)
            if saved_venues:
                # 不认识的名字直接丢（回滚到少一家的版本时别把整个选择废掉），
                # 剩得太少就回全家——榜单空着却不知道为什么，比忘掉选择更糟
                picked = []
                for token in saved_venues.split(","):
                    try:
                        picked.append(Venue(token.strip()))
                    except ValueError:
                        continue
                if len(picked) >= MIN_VENUES:
                    self.enabled_venues = tuple(picked)
        except Exception:
            pass

    @staticmethod
    def _clamp_interval(seconds: float) -> float:
        """低于硬下限一律抬回下限。**不接受用户把它配到 1 分钟**：
        一轮要 105 秒，配 60 秒等于让它永远在刷新，而且六家的限频配额会被自己吃光。"""
        return max(float(MIN_MARKET_INTERVAL_S), float(seconds))

    def set_auto_refresh(self, enabled: bool) -> None:
        self.auto_refresh = bool(enabled)
        with contextlib.suppress(Exception):
            self.session.storage.set_meta(META_AUTO_REFRESH, "1" if enabled else "0")

    def set_refresh_interval(self, seconds: float) -> None:
        self.min_refresh_gap_s = self._clamp_interval(seconds)
        with contextlib.suppress(Exception):
            self.session.storage.set_meta(META_REFRESH_INTERVAL,
                                          str(int(self.min_refresh_gap_s)))

    def next_refresh_in_s(self) -> float:
        """距下次自动刷新还有多少秒。负数/0 = 下一拍就会走。"""
        if not self.auto_refresh or self.refreshing or not self.last_refresh:
            return 0.0
        return max(0.0, self.last_refresh + self.min_refresh_gap_s - time.time())

    def set_venues(self, venues: Sequence[Venue]) -> bool:
        """改勾选的交易所。低于 MIN_VENUES 一律拒绝（返回 False，状态不动）：
        跨所配对至少要两家，剩一家时榜单必然全空，而界面上看不出为什么。"""
        picked = tuple(dict.fromkeys(venues))       # 去重且保序
        if len(picked) < MIN_VENUES:
            return False
        if picked != self.enabled_venues:
            self.venues_rev += 1        # 触发一次尽快重拉，见 __init__ 的注释
            self.venues_changed_at = time.time()
        self.enabled_venues = picked
        with contextlib.suppress(Exception):
            self.session.storage.set_meta(
                META_VENUES, ",".join(v.value for v in picked))
        return True

    def ensure_backfiller(self) -> HistoryBackfiller:
        """惰性建回填器。用的是 Session 的库——历史资金费是公开数据，
        不进 vault、不需要解锁，没配任何账户也该能攒。"""
        if self.backfiller is None:
            self._history_adapters, _ = open_public_adapters()
            self.backfiller = HistoryBackfiller(
                self.session.storage, self._history_adapters,
                max_concurrency=HISTORY_CONCURRENCY,
                # 币安的历史端点有 500次/5分钟 的专用桶，而且**不返回用量响应头**，
                # 适配器自带的"看用量退避"在这条路上是瞎的，只能按时间硬节流
                pace_s=DEFAULT_PACE_S,
            )
        return self.backfiller

    def set_history_days(self, days: int) -> None:
        """改回看天数。**变深就要把"补过"的记录清掉**：那些品种库里只有旧的
        浅深度，不清的话得等半小时的过期计时才会重挖，中间用户切到"持有 1 月"
        看到的仍然是"数据不足"，而且没有任何提示告诉他为什么。"""
        if days > self.history_days:
            self.history_seen.clear()
            self.history_done_at = 0.0
        self.history_days = days

    async def close_history(self) -> None:
        await asyncio.gather(*(a.close() for a in self._history_adapters.values()),
                             return_exceptions=True)
        self._history_adapters.clear()

    def ensure_trader(self) -> Trader | None:
        """按需创建协调器。

        默认纸上模式——撮合模拟、行情真实。要下真单必须显式切，
        不能让人稀里糊涂就把真钱扔出去。
        """
        if self.trader is not None:
            return self.trader
        if not self.session.is_unlocked or not self.session.list_accounts():
            return None
        gateway = PaperGateway(registry=self.session.adapters,
                               initial_cash=Decimal("100000"))
        self.trader = Trader(gateway, self.session.storage,
                             paper=self.session.paper_mode)
        self.trader.start()
        return self.trader

    def open_create(self, op) -> None:
        trader = self.ensure_trader()
        if trader is None:
            ui.notify("请先到「账户」页解锁并添加至少一个交易所账户", type="warning")
            return
        open_create_dialog(trader, op, self.session.list_accounts(),
                           on_created=self._on_hedge_change)


async def refresh_opportunities(state: AppState, status: ui.label,
                                spinner: ui.spinner,
                                history_status: ui.label | None = None) -> None:
    """拉真实行情并重新评分。

    六家并发拉，某家挂了不影响其他家——单点故障不该让整个榜单空掉。

    历史资金费只从**库里**读（毫秒级），联网回填另起后台任务：
    首次回填几十个品种要跑十几秒到几分钟，挂在这条路上等于首屏白屏。
    """
    # 卡死自愈：刷新中途页面被销毁时 finally 可能跑不到，标志就永远停在 True，
    # 之后每一拍都直接 return——表现成"正在从六家交易所拉取实时资金费率…"永远不动。
    # 超过一轮实测耗时的两倍还没完，就认定上一轮已经死了，强行放行。
    if state.refreshing:
        if time.time() - state.refresh_started_at < STUCK_REFRESH_S:
            return
        _set_feed_status(state, status, "上一轮刷新卡住了，重新开始…", theme.WARN)

    state.refreshing = True
    state.refresh_started_at = time.time()
    spinner.set_visibility(True)
    # 家数从**勾选**里数出来，别写死——上一次写死"六家"，加了两家之后这行文案
    # 骗了所有人一轮
    _set_feed_status(state, status,
                     f"正在从{len(state.enabled_venues)}家交易所拉取实时资金费率…")
    # 这一轮覆盖的是哪一代勾选。改选发生在半路时代际对不上，
    # adaptive_tick 会在这一轮结束后立刻再拉一轮
    rev = state.venues_rev
    try:
        state.set_history_days(_history_days_for(state.board.filters.horizon_h))
        async with PublicFeed(state.enabled_venues,
                              backfiller=state.ensure_backfiller(),
                              history_days=state.history_days) as feed:
            rows = await feed.build_opportunities(
                notional=state.board.filters.notional,
                horizon_h=state.board.filters.horizon_h,
                fill_mode=state.board.filters.fill_mode,
                # 连了账户就按**你的**费率档位算成本，没连就退回 VIP0 挂牌价。
                # Session 满足 FeeSource 协议，档位在 connect_all 时已经缓存好了，
                # 这里一个网络请求都不会多发。
                fee_source=state.session,
            )
            # 用 venues_with_data 而不是 _adapters：后者只说明"适配器建起来了"，
            # 一家中途抛异常照样留在里面。那会让"这家挂了"长得和"这家没机会"
            # 一模一样——跨所套利里少看一家，看到的价差就是错的。
            state.venues_ok = [v.value for v in feed.venues_with_data]
            state.venues_failed = {
                getattr(v, "value", str(v)): str(m)
                for src in (feed.init_errors, feed.fetch_errors)
                for v, m in (src or {}).items()
            }
            state.feed_error = ""
            hits, total = feed.history_hits, feed.history_hits + feed.history_misses
        # 先出榜。历史后台补，补完再刷新评分
        state.board.set_rows(rows)
        state.last_refresh = time.time()
        state.refreshed_rev = rev
        ok_text = (f"已连 {len(state.venues_ok)} 家：{'、'.join(state.venues_ok)}"
                   f"　·　{time.strftime('%H:%M:%S')} 更新")
        if state.venues_failed:
            _set_feed_status(state, status,
                             ok_text + f"　·　{'、'.join(state.venues_failed)} 没取到数据",
                             theme.WARN)
        else:
            _set_feed_status(state, status, ok_text)
        if history_status is not None:
            _set_history_status(state, history_status,
                                f"历史 {hits}/{total} 条腿" if total else "")
            # 必须留强引用：事件循环只持有弱引用，任务跑到一半被 GC 回收会
            # 静默消失，表现成"进度条卡在 30/80 再也不动"
            state.history_task = asyncio.create_task(
                backfill_history(state, rows, history_status))
    except Exception as exc:
        state.feed_error = str(exc)
        _set_feed_status(state, status, f"拉取失败：{exc}", theme.DANGER)
    finally:
        state.refreshing = False
        spinner.set_visibility(False)


# 回看天数的规则搬去了 public_feed：守护模式（watch.py）要用同一条，
# 而它绝不能为了这个函数去 import nicegui。这里保留旧名字，
# 界面代码和已有测试不用跟着改。
_history_days_for = history_days_for


def _set_feed_status(state: AppState, label: ui.label, text: str,
                     color: str | None = None) -> None:
    """状态文案要**先进共享状态、再进标签**。

    一轮刷新只由某一个客户端触发，而 status 标签是**每个客户端各建一份**的。
    只写标签的话，另开的标签页（或刷新后重连的那个）状态栏会永远空着——
    它既没触发过刷新，也就永远不会有人往它的标签里写字，
    界面看起来就像"卡在什么都没有"。存进 state，各客户端的定时器自己去同步。
    """
    state.feed_status_text = text
    state.feed_status_color = color or theme.NEUTRAL
    label.text = text
    label.style(f"color:{state.feed_status_color}")


def _set_history_status(state: AppState, label: ui.label, text: str,
                        color: str | None = None) -> None:
    state.history_status = text
    state.history_status_color = color or theme.NEUTRAL
    label.text = f"　·　{text}" if text else ""
    label.style(f"color:{state.history_status_color}")


async def backfill_history(state: AppState, rows, history_status: ui.label) -> None:
    """后台把榜上品种的历史资金费补齐，补完重新评分。

    只补**榜上出现过的**品种：全市场是六家 3600+ 个永续，光币安 573 个就能
    在 22 秒内打满它 500次/5分钟 的专用桶，而用户看得见的只有几十行。

    补过的记在 history_seen 里，之后每 30 分钟才做一次增量——
    行情 60 秒刷一次，跟着一起补历史是用几十倍的请求换零信息量。
    """
    if state.history_busy or not rows:
        return
    targets = backfill_targets(rows)
    stale = time.time() - state.history_done_at > HISTORY_REFRESH_S
    if not stale:
        targets = [t for t in targets if t not in state.history_seen]
    if not targets:
        return
    # 回填器的并发是全局的，但 days 是每次调用传的，所以这里不用重建它

    state.history_busy = True
    try:
        bf = state.ensure_backfiller()

        def progress(done: int, total: int) -> None:
            _set_history_status(state, history_status, f"补历史 {done}/{total}")

        _set_history_status(state, history_status, f"补历史 0/{len(targets)}")
        results = await bf.backfill_many(targets, days=state.history_days,
                                         on_progress=progress)
        # 只把**成功的**记成补过。失败的留在集合外，下一轮刷新就会重试——
        # 全都记上的话，一次网络抖动会让那几个品种整整半小时停在"数据不足"
        state.history_seen.update((r.market, r.symbol) for r in results if r.ok)
        state.history_done_at = time.time()

        # 重新评分：不重拉行情，只拿库里新的历史把同一批 LegQuote 再算一遍。
        # 这个 PublicFeed **不进 async with**——rescore 只读库不联网，
        # 建六家适配器纯属浪费
        scorer = PublicFeed(backfiller=bf, history_days=state.history_days)
        state.board.set_rows(scorer.rescore(
            state.board.rows, horizon_h=state.board.filters.horizon_h,
            fill_mode=state.board.filters.fill_mode))

        ok = sum(1 for r in results if r.ok)
        hits, total = scorer.history_hits, scorer.history_hits + scorer.history_misses
        note = f"历史 {hits}/{total} 条腿"
        if ok < len(results):
            note += f"（{len(results) - ok} 个品种没取到）"
        _set_history_status(state, history_status, note)
    except Exception as exc:
        # 回填失败不影响榜单——榜单本来就是能用的，只是稳定性列停留在"数据不足"
        _set_history_status(state, history_status, f"历史回填失败：{exc}",
                            theme.WARN)
    finally:
        state.history_busy = False


def _offline_rows():
    """离线演示数据。只在 --demo 且拉不到网时用，界面上会明确标注。"""
    from decimal import Decimal
    from ..scoring import HistoryStats, LegQuote, rank, score_pair
    now = time.time()

    def leg(sym, mkt, f, iv=8.0, nx=4.0, taker=0.0005, depth=2e6, top=2e5,
            px=100.0, oi=5e7, vol=2e8):
        half = px * 1e-4 / 2
        return LegQuote(sym, mkt, px - half, px + half, top / px, top / px,
                        depth, 10.0, taker, 0.0002, f, iv, now + nx * 3600,
                        0.0075, -0.0075, oi, vol)

    # 第四个元素 = 有多少小时的历史。0 表示还没回填到（新上市或两条腿没有重叠区间），
    # 演示数据里必须留这么一行：稳定性列的"有真实评分"和"数据不足"长得完全不同，
    # 离线模式只演示前者的话，等于把界面上最容易误读的那一格藏起来了
    specs = [
        ("MEME", leg("MEMEUSDT", "binance:perp", 0.0),
         leg("MEME_USDT", "gate:perp", 0.0080, depth=1.5e5, top=1.5e4, oi=2e5, vol=8e5), 500),
        ("TINY", leg("TINYUSDT", "bybit:perp", 0.0),
         leg("TINY_USDT", "gate:perp", 0.0120, depth=2e3, top=2e2, oi=3e4, vol=5e4), 500),
        ("ARB", leg("ARBUSDT", "binance:perp", 0.0),
         leg("ARB-USDT-SWAP", "okx:perp", 0.0006, iv=1.0), 500),
        ("BTC", leg("BTCUSDT", "binance:perp", -0.0002, taker=0.0002, depth=3e7, top=3e6),
         leg("BTC-USDT-SWAP", "okx:perp", 0.0018, taker=0.0002, depth=3e7, top=3e6), 500),
        ("SOL", leg("SOLUSDT", "binance:perp", 0.0),
         leg("SOLUSDT", "bybit:perp", 0.0003), 500),
        # 刚上市的币：两家都有合约，但历史短得撑不起一个持有期的窗口统计
        ("NEWB", leg("NEWBUSDT", "binance:perp", 0.0),
         leg("NEWB_USDT", "gate:perp", 0.0040, depth=8e4, top=8e3, oi=1e5, vol=4e5), 0),
    ]
    out = []
    for base, long_leg, short_leg, hist_h in specs:
        if hist_h:
            hs = HistoryStats(tuple([short_leg.funding_now] * 90),
                              short_leg.funding_interval_h)
            hl = HistoryStats(tuple([long_leg.funding_now] * 90),
                              long_leg.funding_interval_h)
            hourly = [short_leg.hourly_rate - long_leg.hourly_rate] * hist_h
        else:
            # 没历史时退化成单点，不是退化成空——空的 median() 返回 0，
            # 等于断言"这个费率的长期均值就是 0"，是凭空捏造的悲观结论
            hs = HistoryStats((short_leg.funding_now,), short_leg.funding_interval_h)
            hl = HistoryStats((long_leg.funding_now,), long_leg.funding_interval_h)
            hourly = None
        out.append(score_pair(base, long_leg, short_leg, hl, hs,
                              notional=50_000, horizon_h=72, now_ts=now,
                              hourly_carry=hourly))
    return rank(out)


def build(config: Config, offline: bool = False,
          state: AppState | None = None) -> None:
    """建界面。

    **必须在 @ui.page 里调用，不能建在自动首页上**：自动首页的元素是全局的，
    浏览器断开重连时容器被销毁而定时器还挂着，之后每一拍都抛
    "The parent slot of Timer has been deleted"，日志被刷屏而且刷新彻底停摆。

    state 传进来（而不是在这里建）是为了让会话、回填器、协调器在多个客户端之间共享——
    每个标签页各建一份 Session 会各自解锁、各自回填，纯属浪费。
    """
    ui.add_head_html(f"<style>{theme.GLOBAL_CSS}</style>")
    ui.dark_mode(False)
    state = state or AppState(config)

    with ui.header().classes("items-center justify-between px-4 py-2") \
            .style("background:#ffffff; color:#18181b; "
                   "border-bottom:1px solid #e4e4e7; box-shadow:none"):
        module_nav("ops")
        with ui.row().classes("items-center gap-2"):
            if offline:
                ui.badge("离线演示数据").props("color=orange")
            if not config.broker.any_enabled:
                ui.label("下单不带返佣码").classes("text-xs px-2 py-1 rounded") \
                    .style(f"color:{theme.NEUTRAL}; border:1px solid {theme.NEUTRAL}") \
                    .tooltip("要启用请在 config.toml 的 [broker] 段填入自己申请到的码。")

    # 多人共用模式下藏掉「对冲仓位」「账户」：它们操作的是**服务器上那一份**
    # vault 和仓位，公开部署里等于把主人的账户交给每个访客。机会榜是只读行情，
    # 人人可看；真要交易请用本地那份。
    from .access import hosted_mode
    _hosted = hosted_mode()

    with ui.tabs().classes("w-full") as tabs:
        tab_ops = ui.tab("机会榜")
        tab_hedges = ui.tab("对冲仓位")
        tab_accounts = ui.tab("账户")
        tab_settings = ui.tab("设置")
        if _hosted:
            tab_hedges.set_visibility(False)
            tab_accounts.set_visibility(False)

    with ui.tab_panels(tabs, value=tab_ops).classes("w-full cf-dense"):
        with ui.tab_panel(tab_ops):
            with ui.row().classes("w-full items-center gap-0 mb-1"):
                feed_status = ui.label().classes("text-xs").style(f"color:{theme.NEUTRAL}")
                # 回填进度紧贴行情状态：同一行、同一号字，用户扫一眼就知道
                # "行情是新的、历史还在补"，不用去别处找
                history_status = ui.label().classes("text-xs") \
                    .style(f"color:{theme.NEUTRAL}")
                ui.element("div").classes("w-3")
                feed_spinner = ui.spinner(size="sm")
                feed_spinner.set_visibility(False)
                ui.space()
                # 倒计时不是装饰：自动刷新是个后台行为，不显示"还有多久"的话，
                # 用户唯一能确认它还活着的办法是盯着时间戳等——那还不如手动点
                next_label = ui.label().classes("text-xs mr-1") \
                    .style(f"color:{theme.NEUTRAL}")
                auto_switch = ui.switch("自动", value=state.auto_refresh) \
                    .props("dense").tooltip("按下面的间隔自动重拉行情")
                interval_select = ui.select(
                    {s: _interval_label(s) for s in REFRESH_INTERVAL_CHOICES},
                    value=int(state.min_refresh_gap_s),
                ).props("dense outlined").classes("w-28") \
                    .tooltip(f"两轮之间至少隔多久。一轮实测约 105 秒，"
                             f"所以最短只能配到 {int(MIN_MARKET_INTERVAL_S) // 60} 分钟")
                refresh_btn = ui.button(
                    "刷新行情", icon="refresh",
                    on_click=lambda: refresh_opportunities(
                        state, feed_status, feed_spinner, history_status),
                ).props("dense outline")

            # 交易所勾选：**平铺一行，不进任何弹层**。这里曾经是"N 家"按钮 +
            # 弹出菜单——弹层依赖 Quasar 的进入动画，在不合成帧的环境（后台标签、
            # 嵌入式 webview）里永远停在透明的第一帧，用户点了按钮什么都看不见，
            # 表现就是"选不了"。平铺勾选框没有动画、没有 portal，在哪都能点。
            # 语义：这是**数据源**选择不是过滤器——改选后几秒内自动重拉行情，
            # 没勾的家连请求都不发（限频配额跟着省）。选择存库，重启保留。
            venue_boxes: dict[Venue, ui.checkbox] = {}
            with ui.row().classes("w-full items-center gap-3 flex-wrap mb-1"):
                ui.label("交易所").classes("text-xs").style(f"color:{theme.NEUTRAL}") \
                    .tooltip("勾选参与配对的交易所。至少两家——跨所配对没有对手腿"
                             "不成立。改选后几秒内自动重拉行情。")

                def on_venue_toggle() -> None:
                    picked = [v for v, box in venue_boxes.items() if box.value]
                    if not state.set_venues(picked):
                        ui.notify("至少要勾两家：跨所配对需要对手腿", type="warning")
                        # 拒绝后把界面掰回真实状态，否则勾选框和实际选择
                        # 从此各说各话
                        for v, box in venue_boxes.items():
                            box.value = v in state.enabled_venues
                        return
                    # 重拉由 adaptive_tick 按代际差触发（≤5 秒），
                    # 不直接调 tick：这里是同步回调，而且上一轮可能还在跑

                for v in Venue:
                    venue_boxes[v] = ui.checkbox(
                        VENUE_LABELS.get(v, v.value),
                        value=v in state.enabled_venues,
                        on_change=lambda e: on_venue_toggle()) \
                        .props("dense").classes("text-xs")

            def rescore_in_place() -> None:
                """成交方式切换：整榜重评，**不重拉行情**。

                行情（LegQuote）没变，变的只是成本口径——重拉一次要 105 秒，
                还白烧六家的限频配额；从库里重评是毫秒级的。"""
                if not state.board.rows:
                    return
                scorer = PublicFeed(backfiller=state.ensure_backfiller(),
                                    history_days=state.history_days)
                state.board.set_rows(scorer.rescore(
                    state.board.rows, horizon_h=state.board.filters.horizon_h,
                    fill_mode=state.board.filters.fill_mode))

            state.board.on_rescore = rescore_in_place
            state.board.build()

            if offline:
                state.board.set_rows(_offline_rows())
                feed_status.text = "离线模式：显示的是演示数据，不是真实行情"
                feed_status.style(f"color:{theme.WARN}")
                refresh_btn.set_enabled(False)
                # 离线模式下自动刷新没有意义，控件一并禁掉——
                # 留着一个转不动的开关只会让人以为是坏了
                auto_switch.set_enabled(False)
                interval_select.set_enabled(False)
                for box in venue_boxes.values():
                    box.set_enabled(False)
                next_label.text = ""
            else:
                def tick():
                    return refresh_opportunities(state, feed_status, feed_spinner,
                                                 history_status)

                def on_auto_change(e) -> None:
                    state.set_auto_refresh(bool(e.value))
                    interval_select.set_enabled(state.auto_refresh)
                    render_countdown()

                def on_interval_change(e) -> None:
                    state.set_refresh_interval(float(e.value))
                    render_countdown()

                auto_switch.on_value_change(on_auto_change)
                interval_select.on_value_change(on_interval_change)
                interval_select.set_enabled(state.auto_refresh)

                def sync_from_state() -> None:
                    """把共享状态同步到**本客户端**的标签上。

                    多开一个标签页、或刷新后重连，那个客户端从来没触发过刷新，
                    也就没人往它的标签里写过字。不同步的话它会一直停在
                    "还没拉到行情"，而榜单其实早就有数据了。"""
                    if feed_status.text != state.feed_status_text:
                        feed_status.text = state.feed_status_text
                        feed_status.style(f"color:{state.feed_status_color}")
                    want = f"　·　{state.history_status}" if state.history_status else ""
                    if history_status.text != want:
                        history_status.text = want
                        history_status.style(f"color:{state.history_status_color}")
                    feed_spinner.set_visibility(state.refreshing)
                    # 勾选也是共享状态：另一个标签页改了选择，这边的勾选框
                    # 不跟着动的话，两个页面会各说各话
                    for v, box in venue_boxes.items():
                        want_checked = v in state.enabled_venues
                        if box.value != want_checked:
                            box.value = want_checked

                def render_countdown() -> None:
                    sync_from_state()
                    if not state.auto_refresh:
                        next_label.text = "自动刷新已关"
                        return
                    if state.refreshing:
                        ran = time.time() - state.refresh_started_at
                        # 一轮实测约 105 秒。明显超时多半是上一轮的页面被销毁、
                        # finally 没跑到，标志卡在 True——refresh_opportunities 的
                        # 自愈分支是**静默 return** 的，重载后的新页面状态栏一片空白，
                        # 用户只会觉得"它挂了"。这里把那段等待说出来
                        if ran > STUCK_REFRESH_S / 2:
                            left = int(STUCK_REFRESH_S - ran)
                            next_label.text = f"上一轮像是卡住了，{left} 秒后强制重来"
                        else:
                            next_label.text = "刷新中…"
                        return
                    if not state.last_refresh:
                        next_label.text = ""
                        return
                    left = int(state.next_refresh_in_s())
                    next_label.text = f"下次自动刷新 {left // 60}:{left % 60:02d}"

                # 启动即拉一次，之后**按实测耗时自适应**间隔。
                # 固定 60 秒是错的：一轮要拉六家全市场费率再逐对拉深度，
                # 实测约 105 秒，比 60 秒长——结果是永远在刷新、永远看不到结果，
                # 而且六家的限频配额被自己吃光。
                ui.timer(0.3, tick, once=True)

                def adaptive_tick():
                    render_countdown()
                    if state.refreshing:
                        return          # 上一轮还没跑完，这一拍直接跳过
                    if state.refreshed_rev != state.venues_rev:
                        # 勾选变了：榜上还是旧家数的数据，尽快重拉一轮。
                        # 这一条**不看自动开关**——改选是用户刚做的显式动作，
                        # 不响应它比"自动刷新关着"更让人困惑。
                        # 3 秒防抖：连点几家是一个动作，落在点击序列半路
                        # 会拿着中间态白拉一轮全市场
                        if time.time() - state.venues_changed_at < 3.0:
                            return
                        return tick()
                    if not state.auto_refresh:
                        return          # 用户把自动关了，只更新倒计时文案
                    if time.time() - state.last_refresh < state.min_refresh_gap_s:
                        return
                    return tick()

                # 5 秒一拍只是"看一眼要不要刷"，真正的节流是 min_refresh_gap_s。
                # 拍子密一点是为了倒计时走得像秒表，而不是每 15 秒跳一次
                ui.timer(5.0, adaptive_tick)
                # 回填器的 httpx 连接池不跟着 PublicFeed 的生命周期走，得自己收
                ng_app.on_shutdown(state.close_history)

        with ui.tab_panel(tab_hedges):
            # 面板只建一次。它自己每秒刷新，解锁/建仓后无需重建——
            # 重建会把定时器的父容器删掉，之后每秒抛一次异常
            hedges_panel = HedgesPanel(state.ensure_trader)
            hedges_panel.build()
            state._on_hedge_change = hedges_panel.render

        with ui.tab_panel(tab_accounts):
            _accounts_container = ui.column().classes("w-full")

            def render_accounts() -> None:
                _accounts_container.clear()
                with _accounts_container:
                    AccountsPanel(state.session, on_change=render_accounts).build()

            render_accounts()

        with ui.tab_panel(tab_settings):
            _settings_panel(config)


def _hedges_panel(state: AppState) -> None:
    with ui.column().classes("p-4 gap-3 cf-mono w-full"):
        if not state.session.list_accounts():
            ui.label("还没有交易所账户。到「账户」页添加一个才能建仓。") \
                .classes("text-sm").style(f"color:{theme.NEUTRAL}")
            return
        ui.label("尚无对冲仓位。到机会榜里挑一个，点右侧 + 建仓。") \
            .classes("text-sm").style(f"color:{theme.NEUTRAL}")


def _settings_panel(config: Config) -> None:
    with ui.column().classes("p-4 gap-4 cf-mono w-full max-w-3xl"):
        ui.label("经纪商返佣码").classes("text-sm font-bold")
        with ui.card().classes("p-3 w-full"):
            ui.label("默认全空——下单不带任何返佣标识。填上自己申请到的码即可生效，无需改代码。") \
                .classes("text-xs").style(f"color:{theme.NEUTRAL}")
            ui.label("币安的 Link ID 是机构项目（门槛 2 万用户 + 月交易量 1000 万美元），个人拿不到。"
                     "其余五家审的是业务类型，「交易机器人提供商」在合格名单里——"
                     "OKX 和 Gate 门槛最低。注册用的推荐码是账户级的，和 API 下单无关，填了不会有返佣。") \
                .classes("text-xs").style(f"color:{theme.WARN}")
            with ui.grid(columns=3).classes("w-full gap-2 mt-2"):
                for venue, mech in (("binance", "订单ID前缀"), ("okx", "body tag"),
                                    ("htx", "订单ID前缀"), ("gate", "Channel-Id 头"),
                                    ("bybit", "X-Referer 头"), ("bitget", "API-Code 头")):
                    ui.input(label=f"{venue}（{mech}）",
                             value=config.broker.for_venue(venue)) \
                        .props("dense outlined").classes("w-full")

        ui.label("安全").classes("text-sm font-bold")
        with ui.card().classes("p-3 w-full"):
            with ui.row().classes("items-center gap-2"):
                ui.label(f"监听地址：{config.server.host}:{config.server.port}").classes("text-xs")
                if config.server.host in ("127.0.0.1", "localhost", "::1"):
                    ui.badge("仅本机").props("color=green")
                else:
                    ui.badge("对外").props("color=red")
            ui.label("默认只监听回环地址。要对外访问必须显式开 allow_public 并配 TLS 证书——"
                     "会话 cookie 在明文 HTTP 上会被中间人截获。") \
                .classes("text-xs").style(f"color:{theme.NEUTRAL}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="earnfarm 跨所资金费套利工具")
    parser.add_argument("--offline", action="store_true",
                        help="用离线演示数据，不联网")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    config = load_config()
    port = args.port or config.server.port

    # 状态全局共享一份，界面每个客户端各建一份。
    # 反过来做（状态每客户端一份）会让每个标签页各自解锁 vault、各自回填历史；
    # 都放全局（建在自动首页上）则定时器会在客户端断开后变成孤儿。
    state = AppState(config)

    @ui.page("/")
    def index() -> None:
        build(config, offline=args.offline, state=state)

    @ui.page("/premium")
    def premium() -> None:
        # 溢价页不接 offline 开关：它只发 6 个公开请求，没有演示数据的必要；
        # 断网时页面会把失败原因写在状态栏里，比一份假数据诚实
        build_premium_page()

    @ui.page("/analysis")
    def analysis() -> None:
        # 复用共享的 session：已存账户走 vault 解锁态，临时凭据不碰 vault
        build_analysis_page(state.session, config, page_mode="ops")

    @ui.page("/market")
    def market() -> None:
        # 单币分析：公开行情免凭据；与复盘页共用引擎、后台任务和历史报告
        build_analysis_page(state.session, config, page_mode="market")

    # 访问闸：设了 EARNFARM_WEB_PASSWORD 就整站先过 /login。
    # 挂公网（哪怕躲在反代后面）必须开——运行时状态是所有客户端共享的，
    # 没有这道闸，你解锁的 vault 对任何访客都是解锁的
    from .access import gate_enabled, install_gate, storage_secret
    if gate_enabled():
        install_gate(config.data_dir)

    ui.run(host=config.server.host, port=port, title="earnfarm",
           reload=False, show=False, dark=False,
           storage_secret=storage_secret(config.data_dir))


if __name__ in {"__main__", "__mp_main__"}:
    main()
