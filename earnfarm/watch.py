"""无界面守护模式：7×24 跑在服务器上，只在有事时通知。

**为什么要有它。** 接上 90 天真实历史之后结论很硬：绝大多数跨所机会是负期望——
当前费差看着好，历史均值接近零，AR(1) 衰减完覆盖不了四笔 taker 手续费。
真能做的机会因此稀少且短暂，往往只活几小时。守着浏览器等它是这个工具最差的用法：
盯屏幕的成本远高于机会本身的收益，而人一定会在它出现的那两小时里去吃饭。
守护模式把这件事反过来——没消息就是没机会。

**三条设计线，每条都是守护进程的全部价值所在：**

1. *节奏*。一轮完整刷新实测约 105 秒（六家全市场费率 + 逐对深度），所以行情轮询
   不能短于 3 分钟（config.MIN_MARKET_INTERVAL_S 会拦），否则程序永远在刷新，
   还把六家的限频配额全花在自己身上。历史增量半小时一次——资金费一天才结算
   1~24 次，跟着行情一起刷是几十倍请求换零信息量。仓位 500ms，那是执行器的
   收敛节拍，不是轮询。

2. *活着比正确重要*。任何一环抛异常都只记账、退避、继续，绝不让进程退出。
   四个循环互不牵连：历史回填炸了不影响行情，行情炸了不影响仓位收敛，
   单家交易所挂了不影响其他五家（隔离在 PublicFeed.fetch_funding 里）。

3. *可观测*。长时间没有告警，可能是"没机会"，也可能是"进程半夜就死了"，
   这两件事在用户那边长得一模一样——所以有心跳（写 meta，不写事件表），
   `--status` 一条命令就能分清；正常退出还会留下 stopped 标记，
   免得优雅停机被误判成崩溃。

**退出顺序是硬约定**：收到 SIGINT/SIGTERM → 停各循环 → 停交易协调器 →
**把仓位状态落库** → 才轮到关网络和库。落库排在所有可能超时的收尾动作之前，
所以就算收尾超时被硬砍，仓位状态也已经在库里了。

告警的策略（门槛、静默期、通道）**全部属于 alerts.py**，这里一个阈值都不复制：
同一个旋钮放两处，早晚出现"调高了却还在响"的鬼故事。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Sequence

from .config import Config, load as load_config
from .history import DEFAULT_PACE_S, HistoryBackfiller
from .models import Venue
from .public_feed import (
    PublicFeed,
    backfill_targets,
    history_days_for,
    open_public_adapters,
)
from .scoring import ScoredOpportunity
from .storage import Storage

LOG = logging.getLogger("earnfarm.watch")

# 心跳存 meta 表：覆盖写、不进 events。
# 心跳要是写成事件，60 秒一条、一天 1440 行，真正该看的告警会被冲到几百行以外。
HEARTBEAT_KEY = "watch:heartbeat"

# 主密码只从环境变量读，绝不落配置文件、绝不硬编码。
# 没给就跑「只看不做」模式：行情和告警照常，仓位一律不碰。
MASTER_PASSWORD_ENV = "EARNFARM_MASTER_PASSWORD"
# vault.password_from_env() 用的是这个名字，而它的存在理由恰好就是无人值守部署。
# 两个名字必须都认：只认一个的话，按另一个名字配好的用户会启动一个
# **静默只读**的守护进程——它一切正常、只是永远不推进仓位，而这是最难发现的故障。
LEGACY_PASSWORD_ENV = "EARNFARM_PASSWORD"


def _master_password() -> tuple[str, str]:
    """读主密码，返回 (密码, 来源变量名)。两个变量都认，显式的那个优先。"""
    for name in (MASTER_PASSWORD_ENV, LEGACY_PASSWORD_ENV):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value, name
    return "", ""

# 回填并发。适配器内部的 _quota 是第二层闸；这里压得低是因为回填是后台任务，
# 抢配额抢赢了行情刷新反而是帮倒忙
HISTORY_CONCURRENCY = 3

# 失败后的退避阶梯（秒）。**不走常规间隔**，走这条独立阶梯：
# 对慢循环（行情 300 秒）它是加速——网络抖一下不该等满一个周期才恢复；
# 对快循环（仓位 500 毫秒）它是刹车——持续报错时别 2 次/秒地空转写日志。
BACKOFF_BASE_S = 5.0
BACKOFF_MAX_S = 120.0

# 行情停更多久算"降级"：轮询间隔的这个倍数，且不低于 10 分钟。
# 偶尔一轮超时不该报警，连着三轮拿不到数据就是真出事了。
STALE_ROUNDS = 3
STALE_FLOOR_S = 600.0


# ---- 告警模块的接线 -------------------------------------------------------
#
# 导入放在 try 里，理由只有一个：**守护进程绝不能因为告警链路的问题而死**。
# 告警是它的产出，不是它的心跳。alerts.py 缺席/写坏时降级成"只写事件表"照常跑，
# 用户至少还能从 --status 和 events 里看到东西。

try:
    from .alerts import (
        Alert,
        AlertEngine,
        EventType,
        Severity,
        StaleMonitor,
        actionable_opportunities,
        opportunity_alert,
    )
    ALERTS_IMPORT_ERROR = ""
except Exception as exc:                    # pragma: no cover - 取决于 alerts.py 的状态
    Alert = AlertEngine = EventType = Severity = StaleMonitor = None  # type: ignore
    actionable_opportunities = opportunity_alert = None               # type: ignore
    ALERTS_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

_AUTO = object()        # "自己去建一个告警引擎"，区别于显式传 None（不要告警）

# 事件级别 → 严重度。Trader 的回调只给 level 字符串，没有 safety 那套结构化档位
_SEVERITY_BY_LEVEL = {"info": 0, "warn": 1, "error": 2, "critical": 3}


def build_alert_engine(config: Config, storage: Storage | None = None) -> Any:
    """按 [alerts] 段建告警引擎；建不起来就返回 None（降级成只写事件表）。

    sink 把本地日志通道接到 events 表：alerts.LogChannel 永远开着且先于远端通道
    落地，所以哪怕微信/TG 全挂了，库里也有账可查。
    """
    if AlertEngine is None:
        if ALERTS_IMPORT_ERROR:
            LOG.warning("告警模块不可用（%s）：告警只写事件表", ALERTS_IMPORT_ERROR)
        return None

    def sink(level: str, category: str, message: str) -> None:
        if storage is None:
            return
        try:
            storage.log_event(level, category, message, int(time.time() * 1000))
        except Exception as exc:            # 落库失败不能反过来打断投递
            LOG.warning("告警落库失败：%s", exc)

    try:
        return AlertEngine.from_config(config.alerts, sink=sink)
    except Exception as exc:
        LOG.warning("告警引擎初始化失败（%s）：降级为只写事件表", exc)
        return None


def _channel_names(engine: Any) -> tuple[str, ...]:
    names = getattr(engine, "channel_names", ())
    try:
        return tuple(str(n) for n in names)
    except TypeError:
        return ()


# ---- 自检 ----------------------------------------------------------------

@dataclass(slots=True)
class SelfCheck:
    """启动自检。用户看一眼就知道这个进程能干什么、不能干什么。"""

    venues: tuple[str, ...]
    accounts: int
    unlocked: bool
    paper: bool
    trading: bool                   # 会不会推进仓位
    alert_channels: tuple[str, ...]
    alert_skipped: dict[str, str]
    alert_note: str
    db_path: str
    market_interval_s: float
    history_interval_s: float
    position_interval_ms: int

    def lines(self) -> list[str]:
        if self.trading:
            mode = "纸上撮合（行情真实、成交模拟）" if self.paper else "实盘"
            # 说清楚边界：协调器只推进**本进程内**的仓位。库里那些是界面进程建的，
            # Trader 目前没有从库里恢复运行时状态的入口（见 positions_round）。
            # 不写明的话，用户会以为守护进程接管了昨天在界面上建的仓。
            position = f"推进已有仓位：是（{mode}，仅限本进程内建的仓位）"
        elif self.accounts == 0:
            position = "推进已有仓位：否（没有交易所账户，纯看榜模式）"
        elif not self.unlocked:
            position = (f"推进已有仓位：否（凭据库没解锁，"
                        f"要解锁请设环境变量 {MASTER_PASSWORD_ENV}）")
        else:
            position = "推进已有仓位：否"
        channels = "、".join(self.alert_channels) if self.alert_channels else "无"
        lines = [
            f"行情源：{len(self.venues)} 家公开端点（{'、'.join(self.venues)}），"
            "实际连通以首轮行情为准",
            f"账户：{self.accounts} 个；{position}",
            f"告警通道：{channels}{('　' + self.alert_note) if self.alert_note else ''}",
        ]
        # 配了却没生效的通道必须逐条说清楚。用户以为配好了、其实 token 没读到，
        # 这种"以为自己有告警"比明知没有告警危险得多
        for name, why in sorted(self.alert_skipped.items()):
            lines.append(f"　　⚠ {name} 没启用：{why}")
        lines += [
            f"节奏：行情 {self.market_interval_s:.0f}s／历史 {self.history_interval_s:.0f}s／"
            f"仓位 {self.position_interval_ms}ms",
            f"数据库：{self.db_path}",
        ]
        return lines


# ---- 守护进程 -------------------------------------------------------------

class Watcher:
    """无界面主循环。

    四个循环各跑各的，只通过 self.rows 一个共享状态耦合：
    行情（拉+评分+告警）／历史（增量回填+重新评分）／仓位（推进）／心跳。
    任何一个抛异常都只影响它自己的下一拍。

    所有外部依赖都可注入（feed_factory / backfiller / trader / alerts），
    于是测试能在不联网、不建 Session 的前提下驱动**真实的循环代码**——
    另写一套"测试专用主循环"的话，测过的东西说明不了线上那套对。
    """

    def __init__(self, config: Config, *,
                 storage: Storage | None = None,
                 session: Any = None,
                 alerts: Any = _AUTO,
                 feed_factory: Callable[[], Any] | None = None,
                 backfiller: HistoryBackfiller | None = None,
                 trader: Any = None,
                 backoff_base_s: float = BACKOFF_BASE_S,
                 backoff_max_s: float = BACKOFF_MAX_S,
                 clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self.wcfg = config.watch
        self.session = session
        self.storage = storage or (session.storage if session is not None
                                   else Storage(config.db_path))
        self._own_storage = storage is None and session is None
        self.alerts = build_alert_engine(config, self.storage) if alerts is _AUTO else alerts
        self._feed_factory = feed_factory or self._default_feed
        self.backfiller = backfiller
        self.trader = trader
        self._backoff_base_s = backoff_base_s
        self._backoff_max_s = backoff_max_s
        self._now = clock

        self.history_days = (self.wcfg.history_days if self.wcfg.history_days > 0
                             else history_days_for(self.wcfg.horizon_h))
        self._history_adapters: dict[Venue, Any] = {}

        # 行情陈旧监控。阈值**不能直接用 [alerts].stale_after_s**：那个默认 300 秒
        # 是按 WS 推送定的，而守护模式是 300 秒轮询一次——直接用会每轮都报陈旧。
        # 取两者较大值，语义才对得上："连着几轮都没拿到这家的数据"。
        self.stale_monitor = (
            StaleMonitor(stale_after_s=self._stale_after_s()) if StaleMonitor else None)

        # ---- 运行时状态（心跳直接读它）----
        self.started_at = self._now()
        self.stopping = False
        self.stopped_at: float | None = None
        self.rows: list[ScoredOpportunity] = []
        self.market_rounds = 0
        self.history_rounds = 0
        self.position_steps = 0
        self.alerts_sent = 0
        self.last_market_ok: float | None = None
        self.last_history_ok: float | None = None
        self.venues_ok: tuple[str, ...] = ()
        self.venues_failed: dict[str, str] = {}
        self.failures: dict[str, int] = {}
        self.last_errors: dict[str, str] = {}
        self._venue_down: set[str] = set()
        self._degraded = False
        self._trader_events: deque[tuple[str, str, str]] = deque(maxlen=500)
        self._stop = asyncio.Event()
        self._stop_reason = ""

    # ---- 惰性依赖 -------------------------------------------------------

    def ensure_backfiller(self) -> HistoryBackfiller:
        """回填器的适配器与行情源的**分开建**：行情源每轮进出一次 async with、
        把自己的 httpx 连接池关掉，而回填跨越多轮，借用它的连接会在半路被关掉。"""
        if self.backfiller is None:
            self._history_adapters, _ = open_public_adapters()
            self.backfiller = HistoryBackfiller(
                self.storage, self._history_adapters,
                max_concurrency=HISTORY_CONCURRENCY,
                # 币安历史端点有 500次/5分钟 的专用桶且**不返回用量响应头**，
                # 适配器自带的"看用量退避"在这条路上是瞎的，只能按时间硬节流
                pace_s=DEFAULT_PACE_S,
            )
        return self.backfiller

    def _default_feed(self) -> PublicFeed:
        # 连了账户就把 Session 当费率来源用：净年化按用户真实 VIP 档算，
        # 而不是六家的 VIP0 挂牌价。没有 Session 时它是 None，行为与从前一致
        fee_source = self.session if hasattr(self.session, "fee_for") else None
        return PublicFeed(backfiller=self.ensure_backfiller(),
                          history_days=self.history_days,
                          fee_source=fee_source)

    # ---- 事件与告警 -----------------------------------------------------

    def _log_event(self, level: str, message: str, *, category: str = "system",
                   detail: str | None = None) -> None:
        try:
            self.storage.log_event(level, category, message,
                                   int(self._now() * 1000), detail=detail)
        except Exception as exc:            # 写日志失败不能反过来搞死守护进程
            LOG.warning("事件落库失败：%s", exc)

    async def dispatch(self, alert: Any) -> bool:
        """投递一条告警，返回是否真的发出去了（被去重静默的不算）。

        引擎缺席时退化成写事件表——**告警可以没有通道，但不能没有痕迹**。
        """
        if alert is None:
            return False
        if self.alerts is None:
            self._log_event(_alert_level(alert), getattr(alert, "title", str(alert)),
                            detail=_alert_text(alert))
            return False
        try:
            result = await self.alerts.dispatch(alert)
        except Exception as exc:
            # 推送失败绝不能中断本轮。写一条 warn 就够了——
            # 再为"告警失败"发一条告警是自指的死循环
            LOG.warning("告警投递失败（%s）：%s", type(exc).__name__, exc)
            self._log_event("warn", f"告警投递失败：{exc}")
            return False
        sent = bool(getattr(result, "sent", True))
        if sent:
            self.alerts_sent += 1
        return sent

    def _system_alert(self, subject: str, severity_name: str, title: str,
                      body: Sequence[str], action: str, bucket: str = "") -> Any:
        """守护进程自己的故障告警。

        借用 STALE 这个类型是准确的，不是凑合：循环停摆的**后果**就是数据不再更新，
        而用户要做的动作也一样——别信界面上的价差，先去看进程。
        """
        if Alert is None:
            return None
        return Alert(
            type=EventType.STALE, subject=subject,
            severity=getattr(Severity, severity_name, Severity.WARN),
            title=title, body=tuple(body), action=action, bucket=bucket,
            ts=self._now(),
        )

    async def alert_opportunities(self, rows: Sequence[ScoredOpportunity]) -> int:
        """把值得打扰人的机会推出去。

        筛选（净年化/容量/可执行）交给 alerts.actionable_opportunities，静默与
        分桶交给引擎的 Deduper——这里只负责"每轮最多推几条"，因为那是循环的事。
        rows 已经是 rank 排好序的，截断取的就是最好的几条。

        顺带一提：is_prior（历史不足）的行不会被推，因为 score_pair 会把它降级成
        marginal，而 require_actionable 把 marginal 挡在外面。对着 0.35 的先验
        推送就是对着噪声推送——这是接上真实历史之后才看清的坑。
        """
        if actionable_opportunities is None or opportunity_alert is None:
            return 0
        picks = actionable_opportunities(rows, self.config.alerts)
        sent = 0
        for op in picks[:max(0, self.wcfg.alert_max_per_round)]:
            if await self.dispatch(opportunity_alert(op, ts=self._now())):
                sent += 1
        if len(picks) > self.wcfg.alert_max_per_round:
            LOG.info("本轮 %d 个机会过筛，只推前 %d 个",
                     len(picks), self.wcfg.alert_max_per_round)
        return sent

    async def _drain_trader_alerts(self) -> None:
        """把交易协调器的 warn/error 事件转成告警。

        无人值守时，裸腿、撤退、熔断恰恰是最该把人叫醒的事——
        它们只写进事件表等于没人知道。
        """
        while self._trader_events:
            level, category, message = self._trader_events.popleft()
            if Alert is None:
                continue
            severity = ("INFO", "WARN", "ERROR", "CRITICAL")[
                _SEVERITY_BY_LEVEL.get(level, 1)]
            # subject 带上消息前缀而不是只带 category：去重键是
            # (类型, subject, bucket)，只按 category 分的话，"分片 A 裸奔"
            # 推过之后，15 分钟内"分片 B 裸奔"会被当成重复吃掉——
            # 而那恰恰是另一笔钱在裸奔。消息前缀里通常就带着分片/仓位 id。
            alert = Alert(
                type=EventType.RISK, subject=f"{category}:{message[:24]}",
                severity=getattr(Severity, severity, Severity.WARN),
                title=message[:80],
                body=(message,),
                action="去界面看这个仓位当前的分片状态；无人值守时优先确认没有单腿敞口",
                bucket=level, ts=self._now(),
            )
            await self.dispatch(alert)

    def _on_trader_event(self, level: str, category: str, message: str) -> None:
        """Trader 的回调是同步的，这里只入队，真正的投递在仓位循环里做。
        在同步回调里发网络请求会把执行器的收敛节拍拖垮。"""
        if level in ("warn", "error", "critical"):
            self._trader_events.append((level, category, message))

    # ---- 四个循环体 -----------------------------------------------------

    async def market_round(self) -> list[ScoredOpportunity]:
        """拉行情 → 评分 → 告警。六家并发，某家挂了不影响其他家。"""
        feed = self._feed_factory()
        async with feed:
            kwargs: dict[str, Any] = {
                "notional": self.wcfg.notional,
                "horizon_h": self.wcfg.horizon_h,
                "top_n": self.wcfg.top_n,
            }
            rows = await feed.build_opportunities(**kwargs)
            venues_ok = tuple(getattr(v, "value", str(v))
                              for v in getattr(feed, "venues_with_data", ()) or ())
            failed: dict[str, str] = {}
            for source in (getattr(feed, "init_errors", None) or {},
                           getattr(feed, "fetch_errors", None) or {}):
                for venue, message in source.items():
                    failed[getattr(venue, "value", str(venue))] = str(message)

        self.rows = list(rows)
        self.market_rounds += 1
        self.last_market_ok = self._now()
        self._track_venues(venues_ok, failed)
        if self.market_rounds == 1:
            actionable = sum(1 for o in self.rows if o.is_actionable)
            self._log_event(
                "info",
                f"首轮行情：已连 {len(self.venues_ok)} 家"
                f"（{'、'.join(self.venues_ok) or '无'}），榜上 {len(self.rows)} 行，"
                f"其中可执行 {actionable} 行")
        await self.alert_opportunities(self.rows)
        return self.rows

    async def history_round(self) -> int:
        """增量补历史，补完就地重新评分。

        只补**榜上出现过的**品种：全市场是六家 3600+ 个永续，光币安 573 个就能
        在 22 秒内打满它 500次/5分钟 的专用桶，而榜单一共才几十行。
        """
        if not self.rows:
            return 0                        # 还没榜单就没有回填目标，绝不做全市场扫描
        targets = backfill_targets(self.rows)
        bf = self.ensure_backfiller()
        results = await bf.backfill_many(targets, days=self.history_days)
        ok = sum(1 for r in results if getattr(r, "ok", False))

        # 重新评分只读库、不联网，所以这个 feed **不进 async with**
        scorer = self._feed_factory()
        self.rows = list(scorer.rescore(self.rows, horizon_h=self.wcfg.horizon_h))
        self.history_rounds += 1
        self.last_history_ok = self._now()
        if ok < len(results):
            LOG.info("历史增量：%d/%d 个品种成功", ok, len(results))
        # 补完历史评分就变了——有可能刚好把"数据不足"的行变成真机会，
        # 所以立刻再判一次告警，不用等下一轮行情
        await self.alert_opportunities(self.rows)
        return ok

    async def positions_round(self) -> None:
        """推进已有仓位。没有账户就是空转——但循环照跑，
        中途解锁加了账户也不需要重启进程。

        **已知边界**：推进的是 Trader 运行时里的仓位，而 Trader 没有"从库里
        恢复运行时"的入口（Hedge 行只有目标和状态，分片的成交进度在内存里）。
        所以守护进程接不了界面进程建的仓，也接不了自己上次重启前的仓。
        要补这一环得在 Trader 上加 load_open_hedges()：按 fills 重建每片的已成交量、
        按 instruments 重建 LegFilters——那是执行器层的改动，不该在这里凑合。
        """
        trader = self.trader
        if trader is None:
            return
        await trader.step_all()
        self.position_steps += 1
        await self._drain_trader_alerts()

    async def heartbeat_round(self) -> None:
        payload = self.heartbeat()
        self.storage.set_meta(HEARTBEAT_KEY, json.dumps(payload, ensure_ascii=False))
        await self._alert_stale(payload)
        LOG.info("心跳：运行 %.0fs，行情 %d 轮，榜 %d 行，告警 %d 条%s",
                 payload["uptime_s"], payload["market_rounds"],
                 payload["opportunities"], payload["alerts_sent"],
                 "，已降级" if payload["degraded"] else "")

    # ---- 健康判定 -------------------------------------------------------

    def _track_venues(self, venues_ok: Sequence[str], failed: dict[str, str]) -> None:
        """记录哪几家给了数据、哪几家掉线了。

        一家静默掉线会让配对少掉一大截，而榜单看上去完全正常——
        无人值守时这是最容易骗过人的故障。给了数据的打点，没给的自然会
        在 StaleMonitor 里越积越老，到阈值由心跳循环喊出来。
        """
        self.venues_ok = tuple(venues_ok)
        self.venues_failed = dict(failed)
        now = self._now()
        if self.stale_monitor is not None:
            for venue in venues_ok:
                self.stale_monitor.touch(f"{venue}:perp", now)
        down = set(failed)
        for venue in sorted(down - self._venue_down):
            self._log_event("warn", f"{venue} 行情拉取失败：{failed[venue]}")
        for venue in sorted(self._venue_down - down):
            self._log_event("info", f"{venue} 行情已恢复")
        self._venue_down = down

    def _stale_after_s(self) -> float:
        configured = float(getattr(getattr(self.config, "alerts", None),
                                   "stale_after_s", 0) or 0)
        return max(STALE_ROUNDS * self.wcfg.market_interval_s, STALE_FLOOR_S, configured)

    async def _alert_stale(self, payload: dict[str, Any]) -> None:
        """行情停更要主动喊一声。

        心跳解决"进程还在不在"，这条解决"进程还在但行情早断了"——
        少了它，"很久没收到告警"仍然有两个解释。
        """
        if self.stale_monitor is not None:
            for alert in self.stale_monitor.alerts(self._now()):
                await self.dispatch(alert)
        degraded = bool(payload.get("degraded"))
        if degraded and not self._degraded:
            await self.dispatch(self._system_alert(
                "watch:market", "ERROR", "行情已停更",
                [payload.get("degraded_reason", ""),
                 f"行情轮 {payload.get('market_rounds', 0)} 次，"
                 f"连续失败 {payload.get('failures', {}).get('market', 0)} 次"],
                "去服务器看进程日志：多半是网络断了或六家全被限频。"
                "这期间界面/推送里的价差都不能信"))
        elif self._degraded and not degraded:
            self._log_event("info", "行情已恢复")
        self._degraded = degraded

    def heartbeat(self) -> dict[str, Any]:
        """心跳内容。既是给人看的，也是 `--status` 判定死活的唯一依据。"""
        now = self._now()
        market_age = (now - self.last_market_ok) if self.last_market_ok else None
        stale_after = self._stale_after_s()
        degraded_reason = ""
        if market_age is not None and market_age > stale_after:
            degraded_reason = (f"行情已 {market_age / 60:.0f} 分钟没有成功刷新"
                               f"（阈值 {stale_after / 60:.0f} 分钟）")
        elif market_age is None and now - self.started_at > stale_after:
            degraded_reason = "启动后一轮行情都没有成功"
        state = "stopped" if self.stopped_at else ("stopping" if self.stopping else "running")
        return {
            "pid": os.getpid(),
            "ts": now,
            "state": state,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "uptime_s": round(now - self.started_at, 1),
            "heartbeat_interval_s": self.wcfg.heartbeat_interval_s,
            "market_interval_s": self.wcfg.market_interval_s,
            "market_rounds": self.market_rounds,
            "history_rounds": self.history_rounds,
            "position_steps": self.position_steps,
            "last_market_ok": self.last_market_ok,
            "last_history_ok": self.last_history_ok,
            "market_age_s": round(market_age, 1) if market_age is not None else None,
            "opportunities": len(self.rows),
            "actionable": sum(1 for o in self.rows if o.is_actionable),
            "alerts_sent": self.alerts_sent,
            "venues_ok": list(self.venues_ok),
            "venues_failed": dict(self.venues_failed),
            "hedges": self._hedge_count(),
            "trading": self.trader is not None,
            "paper": bool(getattr(self.session, "paper_mode", True)),
            "alert_channels": list(_channel_names(self.alerts)),
            "failures": dict(self.failures),
            "last_errors": dict(self.last_errors),
            "degraded": bool(degraded_reason),
            "degraded_reason": degraded_reason,
        }

    def _hedge_count(self) -> int:
        try:
            return len(self.trader.runtimes()) if self.trader is not None else 0
        except Exception:
            return 0

    # ---- 循环监督 -------------------------------------------------------

    async def _sleep_or_stop(self, delay: float) -> bool:
        """睡 delay 秒，期间收到停止信号就立刻醒。返回 True 表示还该继续。

        用裸 sleep(delay) 的话，300 秒的行情循环会让 Ctrl-C 最坏等 5 分钟才响应，
        而优雅退出的全部意义就是"立刻停下来并落库"。
        """
        if self._stop.is_set():
            return False
        if delay <= 0:
            return True
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
            return False
        except (asyncio.TimeoutError, TimeoutError):
            return True

    def _backoff(self, fails: int) -> float:
        return min(self._backoff_base_s * 2 ** max(0, fails - 1), self._backoff_max_s)

    async def _supervise(self, name: str, interval_s: float,
                         fn: Callable[[], Any], *, first_delay: float = 0.0) -> None:
        """一个循环的看门狗。**除了取消，什么都不让它退出。**

        守护进程唯一不可原谅的失败是静默死掉：用户看不到告警时无法区分
        "没机会"和"进程没了"。所以这里只记账、退避、继续。
        """
        if first_delay and not await self._sleep_or_stop(first_delay):
            return
        while not self._stop.is_set():
            delay = interval_s
            try:
                await fn()
                await self._on_loop_ok(name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = await self._on_loop_error(name, exc)
            if not await self._sleep_or_stop(delay):
                return

    async def _on_loop_ok(self, name: str) -> None:
        if self.failures.get(name):
            fails = self.failures.pop(name)
            self.last_errors.pop(name, None)
            LOG.info("%s 循环已恢复（此前连续失败 %d 次）", name, fails)
            if fails >= self.wcfg.error_alert_after:
                self._log_event("info", f"{name} 循环已恢复（连续失败 {fails} 次后）")

    async def _on_loop_error(self, name: str, exc: Exception) -> float:
        fails = self.failures.get(name, 0) + 1
        self.failures[name] = fails
        self.last_errors[name] = f"{type(exc).__name__}: {exc}"
        LOG.warning("%s 循环第 %d 次失败：%s", name, fails, exc, exc_info=fails == 1)
        # 事件表只在第一次、达到告警阈值、之后每 50 次写一条：
        # 500 毫秒一拍的仓位循环持续报错时，逐次落库会把 events 表写爆
        if fails == 1 or fails == self.wcfg.error_alert_after or fails % 50 == 0:
            self._log_event("error" if fails >= self.wcfg.error_alert_after else "warn",
                            f"{name} 循环失败（第 {fails} 次）：{exc}")
        if fails == self.wcfg.error_alert_after:
            await self.dispatch(self._system_alert(
                f"watch:{name}", "ERROR",
                f"{name} 循环连续失败 {fails} 次",
                [f"最后一次错误：{type(exc).__name__}: {exc}",
                 "进程还活着，会按退避阶梯继续重试"],
                "去服务器看日志。这期间这一环的数据是停的：行情环停=价差不可信，"
                "仓位环停=没人在推进你的分片"))
        return self._backoff(fails)

    def _loop_died(self, task: asyncio.Task) -> None:
        """循环任务不该结束。真结束了（比如 BaseException 穿透）要留下痕迹，
        绝不能安安静静地少掉一条腿。"""
        if task.cancelled() or self._stop.is_set():
            return
        exc = task.exception()
        if exc is not None:
            LOG.error("循环 %s 意外退出：%r", task.get_name(), exc)
            self._log_event("critical", f"循环 {task.get_name()} 意外退出：{exc!r}")

    # ---- 启停 -----------------------------------------------------------

    def request_stop(self, reason: str = "收到停止信号") -> None:
        """信号处理器和外部调用共用的停机入口。幂等。"""
        if self._stop.is_set():
            LOG.warning("已经在收尾了（%s），仍在等仓位落库", reason)
            return
        self._stop_reason = reason
        self.stopping = True
        LOG.info("%s，正在收尾：先落库再退出", reason)
        self._stop.set()

    def install_signal_handlers(self) -> tuple[str, ...]:
        """装 SIGINT/SIGTERM。

        Windows 上 Proactor 事件循环没有 add_signal_handler，必须回落到
        signal.signal——用户的服务器就是 Windows，这条回落路径不是可选项。
        回落时用 call_soon_threadsafe：信号处理器不在事件循环的执行上下文里，
        直接 set() 一个 Event 是未定义行为。
        """
        loop = asyncio.get_running_loop()
        installed: list[str] = []
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, self.request_stop, f"收到 {name}")
            except (NotImplementedError, RuntimeError, AttributeError, ValueError, OSError):
                try:
                    signal.signal(
                        sig,
                        lambda *_a, _n=name: loop.call_soon_threadsafe(
                            self.request_stop, f"收到 {_n}"),
                    )
                except (ValueError, OSError, RuntimeError):
                    continue                # 不在主线程 / 平台不支持：跳过这一个
            installed.append(name)
        return tuple(installed)

    async def startup(self) -> SelfCheck:
        self.started_at = self._now()
        await self._connect_accounts()
        if self.stale_monitor is not None:
            # 所有交易所先打一次点：不打的话，从启动就连不上的那家永远没有条目，
            # 也就永远不会被判成陈旧——最该报的故障反而报不出来
            for venue in Venue:
                self.stale_monitor.touch(f"{venue.value}:perp", self.started_at)
        check = self.selfcheck()
        for line in check.lines():
            LOG.info("%s", line)
        # 启动/退出**只记事件不推送**：这是人自己触发的动作，推给自己没有信息量。
        # 通道到底通不通，看自检里的通道清单和 skipped 原因，比一条测试推送更准。
        self._log_event("info", "守护模式启动：" + "；".join(check.lines()))
        try:
            self.storage.set_meta(HEARTBEAT_KEY,
                                  json.dumps(self.heartbeat(), ensure_ascii=False))
        except Exception as exc:
            LOG.warning("首次心跳写入失败：%s", exc)
        return check

    def selfcheck(self) -> SelfCheck:
        accounts = 0
        unlocked = False
        if self.session is not None:
            try:
                accounts = len(self.session.list_accounts())
                unlocked = bool(self.session.is_unlocked)
            except Exception as exc:
                LOG.warning("账户自检失败：%s", exc)
        channels = _channel_names(self.alerts)
        note = ""
        if self.alerts is None:
            note = (f"（告警模块不可用：{ALERTS_IMPORT_ERROR}）" if ALERTS_IMPORT_ERROR
                    else "（告警引擎未启用，只写事件表）")
        elif set(channels) <= {"log"}:
            note = "（只有本地日志通道：手机上收不到任何东西，去 [alerts] 配一个通道）"
        return SelfCheck(
            venues=tuple(v.value for v in Venue),
            accounts=accounts,
            unlocked=unlocked,
            paper=bool(getattr(self.session, "paper_mode", True)),
            trading=self.trader is not None,
            alert_channels=channels,
            alert_skipped=dict(getattr(self.alerts, "skipped", {}) or {}),
            alert_note=note,
            db_path=str(getattr(self.config, "db_path", "")),
            market_interval_s=self.wcfg.market_interval_s,
            history_interval_s=self.wcfg.history_interval_s,
            position_interval_ms=self.wcfg.position_interval_ms,
        )

    async def _connect_accounts(self) -> None:
        """解锁凭据库并连账户。主密码只从环境变量取。

        没有密码就跑「只看不做」：宁可不推进仓位，也不能把主密码写进配置文件。
        """
        session = self.session
        if session is None or self.trader is not None:
            return
        password, source = _master_password()
        if not password:
            return
        try:
            if not session.is_initialized:
                LOG.warning("设了 %s 但这个库还没设过主密码，跳过解锁", source)
                return
            session.unlock(password)
            errors = await session.connect_all()
            for account_id, message in errors.items():
                self._log_event("warn", f"账户 {account_id[:8]} 连接失败：{message}")
            if not session.list_accounts():
                return
            from .paper import PaperGateway
            from .trader import Trader
            # 网关目前只有纸上实现（界面那边同样如此）：行情真实、撮合模拟。
            # 守护模式绝不自作主张切实盘——那必须是人在界面上显式做的决定。
            gateway = PaperGateway(registry=session.adapters,
                                   initial_cash=Decimal("100000"))
            self.trader = Trader(gateway, self.storage,
                                 paper=session.paper_mode,
                                 on_event=self._on_trader_event)
        except Exception as exc:
            LOG.error("账户解锁/连接失败，退回只看不做模式：%s", exc)
            self._log_event("error", f"账户解锁失败，退回只看不做：{exc}")

    async def run(self, *, install_signals: bool = True) -> int:
        """主循环。返回进程退出码。"""
        self._stop = asyncio.Event()
        if install_signals:
            installed = self.install_signal_handlers()
            LOG.info("已接管信号：%s", "、".join(installed) or "无（平台不支持）")
        await self.startup()

        wcfg = self.wcfg
        specs = (
            ("market", wcfg.market_interval_s, self.market_round, 0.0),
            # 历史让行情先跑：第一轮榜单出来之前没有回填目标
            ("history", wcfg.history_interval_s, self.history_round,
             min(30.0, wcfg.market_interval_s)),
            ("positions", wcfg.position_interval_ms / 1000, self.positions_round, 0.0),
            ("heartbeat", wcfg.heartbeat_interval_s, self.heartbeat_round, 0.0),
        )
        tasks: list[asyncio.Task] = []
        for name, interval, fn, delay in specs:
            task = asyncio.create_task(
                self._supervise(name, interval, fn, first_delay=delay),
                name=f"watch-{name}")
            task.add_done_callback(self._loop_died)
            tasks.append(task)

        try:
            await self._stop.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            # 信号处理器没装上时 Ctrl-C 会直接打到这里，照样得走落库
            self.request_stop("键盘中断")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._finish()
        return 0

    async def run_once(self) -> int:
        """跑一整轮就退出：行情 → 历史 → 心跳。cron 和排错用。"""
        self._stop = asyncio.Event()
        await self.startup()
        code = 0
        for name, fn in (("market", self.market_round), ("history", self.history_round)):
            try:
                await fn()
            except Exception as exc:
                LOG.error("%s 轮失败：%s", name, exc)
                self._log_event("error", f"--once 的 {name} 轮失败：{exc}")
                code = 1
        try:
            await self.heartbeat_round()
        except Exception as exc:
            LOG.error("心跳写入失败：%s", exc)
            code = 1
        self.request_stop("单轮模式跑完")
        await self._finish()
        return code

    async def _finish(self) -> None:
        """收尾。**落库排在所有可能超时的动作之前**，
        所以就算超时被硬砍，仓位状态也已经在库里了。"""
        try:
            await asyncio.wait_for(self.shutdown(), timeout=self.wcfg.shutdown_timeout_s)
        except (asyncio.TimeoutError, TimeoutError):
            LOG.error("收尾超过 %.0f 秒，硬退出（仓位状态已先落库）",
                      self.wcfg.shutdown_timeout_s)
        except Exception as exc:
            LOG.error("收尾出错：%s", exc)

    async def shutdown(self) -> None:
        # 1. 先停协调器：一边落库一边还在下单，落下去的就是过期状态
        trader = self.trader
        if trader is not None:
            try:
                await trader.stop()
            except Exception as exc:
                LOG.warning("停止交易协调器失败：%s", exc)
        # 2. 落库——收尾里最重要的一步，排在所有网络清理前面
        saved = self.persist_positions()
        self.stopped_at = self._now()
        self.stopping = False
        try:
            payload = self.heartbeat()
            payload["stop_reason"] = self._stop_reason
            self.storage.set_meta(HEARTBEAT_KEY, json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            LOG.warning("退出心跳写入失败：%s", exc)
        self._log_event("info", f"守护模式退出（{self._stop_reason}），已落库 {saved} 个仓位")
        # 3. 才轮到关连接
        await self._close_alerts()
        await self.close_history()
        if self.session is not None:
            try:
                await self.session.close()      # 它会顺手关掉 storage
            except Exception as exc:
                LOG.warning("关闭会话失败：%s", exc)
        elif self._own_storage:
            try:
                self.storage.close()
            except Exception as exc:
                LOG.warning("关闭数据库失败：%s", exc)

    def persist_positions(self) -> int:
        """把每个对冲仓位的当前状态写回库。

        没有这一步，被 kill 的那一刻正在收敛的分片进度就只存在于内存里，
        重启后没人知道昨晚停在哪一步、有没有留下单腿敞口。
        """
        trader = self.trader
        if trader is None:
            return 0
        now_ms = int(self._now() * 1000)
        saved = 0
        try:
            runtimes = list(trader.runtimes())
        except Exception as exc:
            LOG.error("读取仓位失败，无法落库：%s", exc)
            return 0
        for runtime in runtimes:
            try:
                hedge = runtime.hedge
                self.storage.upsert_hedge(hedge, now_ms)
                state = getattr(runtime.active_state, "value", runtime.active_state)
                self.storage.log_event(
                    "info", "execution",
                    f"退出落库 {hedge.id[:8]}：{runtime.slices_done}/{runtime.slices_total} 片，"
                    f"多腿 {runtime.long_filled} 空腿 {runtime.short_filled}，"
                    f"净敞口 {runtime.net_qty}，状态 {state}",
                    now_ms, hedge_id=hedge.id)
                saved += 1
            except Exception as exc:
                LOG.error("仓位落库失败：%s", exc)
        return saved

    async def _close_alerts(self) -> None:
        for attr in ("aclose", "close", "shutdown"):
            method = getattr(self.alerts, attr, None)
            if callable(method):
                try:
                    result = method()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    LOG.warning("关闭告警引擎失败：%s", exc)
                return

    async def close_history(self) -> None:
        await asyncio.gather(*(a.close() for a in self._history_adapters.values()),
                             return_exceptions=True)
        self._history_adapters.clear()


def _alert_level(alert: Any) -> str:
    severity = getattr(alert, "severity", None)
    return str(getattr(severity, "label", "info"))


def _alert_text(alert: Any) -> str:
    render = getattr(alert, "as_text", None)
    if callable(render):
        try:
            return render()
        except Exception:
            pass
    return str(alert)


# ---- 状态查询 -------------------------------------------------------------

def read_heartbeat(storage: Storage) -> dict[str, Any] | None:
    raw = storage.get_meta(HEARTBEAT_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def format_status(hb: dict[str, Any] | None, now: float | None = None) -> str:
    """把心跳翻译成一句人话。

    这条命令存在的唯一目的：把"没有告警"拆成"没机会"和"进程死了"两种情况。
    """
    if not hb:
        return "没有心跳记录：守护模式没在这个数据库上跑过。"
    now = now if now is not None else time.time()
    age = now - float(hb.get("ts", 0))
    beat = float(hb.get("heartbeat_interval_s", 60) or 60)
    lines: list[str] = []

    if hb.get("state") == "stopped":
        lines.append(f"● 已停止（优雅退出，{_ago(age)}）——不是崩溃，是有人让它停的。")
    elif age > max(3 * beat, 180):
        lines.append(f"● 疑似死亡：心跳停在 {_ago(age)}，进程多半被杀或卡住了。")
    else:
        lines.append(f"● 存活：{_ago(age)} 有过心跳，"
                     f"已运行 {float(hb.get('uptime_s', 0)) / 3600:.1f} 小时。")

    market_age = hb.get("market_age_s")
    market_note = f"最近一轮 {_ago(float(market_age))}" if market_age is not None else "还没成功过"
    lines.append(f"  行情：{hb.get('market_rounds', 0)} 轮（{market_note}），"
                 f"已连 {'、'.join(hb.get('venues_ok') or []) or '无'}")
    if hb.get("venues_failed"):
        lines.append(f"  掉线：{'、'.join(hb['venues_failed'])}")
    lines.append(f"  榜单：{hb.get('opportunities', 0)} 行，可执行 {hb.get('actionable', 0)} 行；"
                 f"累计告警 {hb.get('alerts_sent', 0)} 条")
    lines.append(f"  仓位：{hb.get('hedges', 0)} 个"
                 f"（{'纸上' if hb.get('paper', True) else '实盘'}，"
                 f"{'推进中' if hb.get('trading') else '未接管'}）")
    if hb.get("degraded"):
        lines.append(f"  ⚠ 降级：{hb.get('degraded_reason', '')}")
    if hb.get("failures"):
        broken = "、".join(f"{k} 连续失败 {v} 次" for k, v in hb["failures"].items())
        lines.append(f"  ⚠ {broken}")
    channels = hb.get("alert_channels") or []
    lines.append(f"  告警通道：{'、'.join(channels) if channels else '无（只写事件表）'}")
    return "\n".join(lines)


def _ago(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} 秒前"
    if seconds < 5400:
        return f"{seconds / 60:.0f} 分钟前"
    if seconds < 172_800:
        return f"{seconds / 3600:.1f} 小时前"
    return f"{seconds / 86_400:.1f} 天前"


# ---- 命令行 ---------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run.py --watch",
        description="earnfarm 守护模式：无界面，7×24 跑，有机会才通知")
    parser.add_argument("--config", type=Path, default=None, help="配置文件路径")
    parser.add_argument("--status", action="store_true",
                        help="打印心跳后退出——用来分清「没机会」和「进程死了」")
    parser.add_argument("--once", action="store_true",
                        help="只跑一轮（行情+历史+心跳）就退出，排错和 cron 用")
    parser.add_argument("--log-file", type=Path, default=None,
                        help="日志写文件（自动轮转 5MB×3），不填只打 stdout")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印调试日志")
    args = parser.parse_args(list(argv) if argv is not None else None)

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        from logging.handlers import RotatingFileHandler
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        # 必须轮转：7×24 跑着的进程，不轮转的日志迟早把服务器磁盘写满，
        # 而那会以"守护进程莫名其妙不动了"的形式表现出来
        handlers.append(RotatingFileHandler(args.log_file, maxBytes=5_000_000,
                                            backupCount=3, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s  %(message)s",
        handlers=handlers,
    )
    if not args.verbose:
        # httpx 每个请求打一行 INFO。一轮行情几百个请求、一天几百轮，
        # 心跳和告警会被冲得一条都看不见。要看请求就加 -v。
        for noisy in ("httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    config = load_config(args.config)

    if args.status:
        storage = Storage(config.db_path)
        try:
            print(format_status(read_heartbeat(storage)))
        finally:
            storage.close()
        return 0

    from .session import Session
    session = Session(config)
    watcher = Watcher(config, session=session, storage=session.storage)
    try:
        return asyncio.run(watcher.run_once() if args.once else watcher.run())
    except KeyboardInterrupt:               # pragma: no cover - 信号没接管上的兜底
        return 130


if __name__ == "__main__":                  # pragma: no cover
    raise SystemExit(main())
