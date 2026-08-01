"""多渠道告警引擎：把稀有且短命的机会和风险推到用户手机上。

**为什么这个模块必须存在。** 接上 90 天真实历史之后结论很硬：绝大多数跨所机会
是负期望——当前费差看着好，历史均值接近零，AR(1) 衰减完覆盖不了四笔 taker 手续费
（见 scoring.score_pair）。真正能做的机会稀少且短暂，常常只活几小时。
守着屏幕刷榜是这个工具最差的用法：它 99% 的时间在告诉你"别做"，
而你要的只是那 1% 的时刻。所以工具形态必须从"看板"变成"哨兵"。

结构抄的是 taoli.tools 唯一做对的那块：**通道 × 事件类型的正交矩阵，
每类事件有自己的检测周期**（它的 Stale 60s / Risks 15s / Price 30s 是 1.5 年
真实运营调出来的，直接沿用）。一个笼统的"开启通知"开关没用——
机会一天可能就一两次，裸腿要秒级；绑在同一个周期上，
要么错过机会，要么被风险噪音淹没到最后全部关掉。

四条设计约束（每条都对应一种"告警系统本身失效"的方式）：

  1. **本地日志通道永远开着**，且先于远端通道落地。
     远端全挂了也必须有账可查——告警系统自己静默失败，比没有告警更危险。

  2. **去重是成败所在，但恶化必须能突破静默。**
     同一个机会每分钟推一次，三天后用户就把通知关了；
     可反过来，SOFT→HARD 的敞口因为"10 分钟前推过"被压住，
     等于把最该看见的那一条精确地吃掉。两个错误都致命，所以去重键之外
     还要看严重度单调性。

  3. **消失后重新出现算新事件。** 一个机会挂了三小时你没动手，它没了；
     半天后它又回来了——这是新的决策时点，不是上一条的重复。

  4. **密钥只从 config / 环境变量读，一行都不落进代码。**
     这个模块自己也不许把 token 打进日志（redact）。

精度约定沿用全局：这里全是统计与文案，不碰下单精度，一律 float。
"""

from __future__ import annotations

import asyncio
import enum
import logging
import math
import os
import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol, Sequence

from .config import AlertsConfig
from .safety import (
    BreakerDecision,
    BreakerLevel,
    ExposureLevel,
    ExposureSnapshot,
    RiskTier,
    UnwindPhase,
)
from .scoring import ScoredOpportunity

log = logging.getLogger("carryfarm.alerts")


# ---- 事件与严重度 -------------------------------------------------------

class EventType(str, enum.Enum):
    """六类事件。划分标准是"要用户做什么"，不是"程序内部发生了什么"。"""

    OPPORTUNITY = "opportunity"   # 出现值得动手的机会 —— 这个工具存在的意义
    NAKED_LEG = "naked_leg"       # 单腿裸露超时（executor 的 ExposureLevel）
    UNWIND = "unwind"             # 触发撤退
    BREAKER = "breaker"           # 熔断（safety.evaluate_breaker）
    STALE = "stale"               # 某家行情不更新了 —— 界面还挂着旧价
    RISK = "risk"                 # 爆仓距离进入危险档（safety.risk_tier）


EVENT_LABELS: Mapping[EventType, str] = {
    EventType.OPPORTUNITY: "机会",
    EventType.NAKED_LEG: "单腿裸露",
    EventType.UNWIND: "撤退",
    EventType.BREAKER: "熔断",
    EventType.STALE: "行情陈旧",
    EventType.RISK: "爆仓风险",
}

# 落库时用的 category，对齐 storage.events 已有的取值口径
EVENT_CATEGORIES: Mapping[EventType, str] = {
    EventType.OPPORTUNITY: "opportunity",
    EventType.NAKED_LEG: "safety",
    EventType.UNWIND: "safety",
    EventType.BREAKER: "safety",
    EventType.STALE: "system",
    EventType.RISK: "safety",
}

# 每类事件的检测周期（秒）。0 = 事件驱动，由状态机在发生的当下直接投递。
#
# 数值取自 taoli.tools 的实测划分，理由都能自洽：
#   STALE 60s   —— 阈值本身是 5 分钟，再密的检测只是白烧 CPU
#   RISK  15s   —— 保证金率是慢变量，但从 ORANGE 掉到 BLACK 只要一根插针
#   OPPORTUNITY 60s —— 资金费按小时结算，比这更密只会拿到同一份数据
#   NAKED_LEG 2s —— 裸腿是唯一按秒计价的东西，但它同时也是事件驱动的，
#                   这个周期只是兜底（回执丢了、状态机卡住时仍然能喊人）
DETECT_INTERVAL_S: Mapping[EventType, float] = {
    EventType.OPPORTUNITY: 60.0,
    EventType.NAKED_LEG: 2.0,
    EventType.UNWIND: 0.0,
    EventType.BREAKER: 0.0,
    EventType.STALE: 60.0,
    EventType.RISK: 15.0,
}


class Severity(enum.IntEnum):
    """严重度。数值可比较——去重的"恶化突破静默"整个建立在这个序上。"""

    INFO = 0
    WARN = 1
    ERROR = 2
    CRITICAL = 3

    @property
    def label(self) -> str:
        """storage.events.level 的口径。"""
        return ("info", "warn", "error", "critical")[int(self)]

    @property
    def zh(self) -> str:
        return ("提示", "注意", "警告", "紧急")[int(self)]


EXPOSURE_SEVERITY: Mapping[ExposureLevel, Severity] = {
    ExposureLevel.DUST: Severity.INFO,
    ExposureLevel.SOFT: Severity.WARN,
    ExposureLevel.HARD: Severity.ERROR,
    ExposureLevel.CRITICAL: Severity.CRITICAL,
}

RISK_SEVERITY: Mapping[RiskTier, Severity] = {
    RiskTier.GREEN: Severity.INFO,
    RiskTier.YELLOW: Severity.WARN,
    RiskTier.ORANGE: Severity.WARN,
    RiskTier.RED: Severity.ERROR,
    RiskTier.BLACK: Severity.CRITICAL,
}


@dataclass(frozen=True, slots=True)
class Alert:
    """一条待投递的告警。

    subject / bucket 一起构成去重键的后两段：
      subject = 说的是"哪个东西"（币、分片、账户、交易所）
      bucket  = 说的是"哪一档"。同一个币的机会从 30% 涨到 90% 是**新机会**，
                不该被 4 小时静默压住；从 30% 变到 32% 不是。

    action 是必填字段，不是装饰：告警的价值全在"我现在该做什么"。
    只说"XX 出事了"的推送，用户第二次就不看了。
    """

    type: EventType
    subject: str
    severity: Severity
    title: str
    body: tuple[str, ...]
    action: str
    bucket: str = ""
    ts: float = 0.0
    # 附加结构化字段，只走 webhook（人读的通道放不下也不该放）
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # str 也是 Sequence[str]，`body="净年化 42%"` 会被 tuple() 拆成一个字一行，
        # 推到微信上就是一列竖着的单字。类型标注拦不住（str 确实满足 Sequence[str]），
        # 测试也拦不住（内部构造器全传 list），只有真在通道上看见渲染结果才发现。
        # 这里统一收口：单个字符串按一行处理。
        if isinstance(self.body, str):
            object.__setattr__(self, "body", (self.body,))
        elif not isinstance(self.body, tuple):
            object.__setattr__(self, "body", tuple(self.body))

    @property
    def key(self) -> tuple[EventType, str, str]:
        return (self.type, self.subject, self.bucket)

    @property
    def when(self) -> str:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(self.ts or time.time()))

    def as_text(self) -> str:
        """纯文本。Telegram 走这个——不用 parse_mode 是刻意的：
        MarkdownV2 要求转义 `_*[]()~>#+-=|{}.!`，而我们的正文里全是
        小数点、百分号、箭头和减号，漏一个就是 400 Bad Request，
        为了几个粗体把整条告警发不出去不划算。"""
        head = f"【{EVENT_LABELS[self.type]}·{self.severity.zh}】{self.title}"
        lines = [head]
        lines += [f"· {b}" for b in self.body]
        lines.append(f"→ 该做什么：{self.action}")
        lines.append(f"（carryfarm {self.when}）")
        return "\n".join(lines)

    def as_markdown(self) -> str:
        """PushPlus 的 markdown 模板。空行分段，否则微信里会挤成一坨。"""
        parts = [f"**【{EVENT_LABELS[self.type]}·{self.severity.zh}】{self.title}**", ""]
        parts += [f"- {b}" for b in self.body]
        parts += ["", f"**该做什么**：{self.action}", "", f"_carryfarm {self.when}_"]
        return "\n".join(parts)

    def as_dict(self) -> dict[str, Any]:
        """Webhook 的结构化载荷。带上渲染好的 text，中转层想直接转发也行。"""
        return {
            "source": "carryfarm",
            "type": self.type.value,
            "subject": self.subject,
            "bucket": self.bucket,
            "severity": self.severity.label,
            "title": self.title,
            "body": list(self.body),
            "action": self.action,
            "ts": self.ts,
            "text": self.as_text(),
            "data": dict(self.data),
        }


def _fmt_hours(h: float) -> str:
    return "永不" if not math.isfinite(h) else f"{h:.0f} 小时"


# ---- 分桶 ---------------------------------------------------------------

APR_BUCKET_WIDTH = 0.25


def apr_bucket(net_apr: float, width: float = APR_BUCKET_WIDTH) -> str:
    """净年化分桶。

    桶宽 25 个百分点：机会从 30% 跳到 80% 属于"事情变了"，值得再喊一次；
    从 30% 漂到 32% 属于同一件事，压住。没有分桶的话，要么错过质变，
    要么被小数点后的抖动刷屏。
    """
    if not math.isfinite(net_apr):
        return "apr-inf"
    return f"apr{math.floor(net_apr / width):d}"


# ---- 告警构造：六类事件各一个 -------------------------------------------
#
# 全部是纯函数，不碰 I/O。这样"文案是否说清了该做什么"可以被测试断言，
# 而不是等真出事时才发现推送里只有一句"敞口异常"。

def opportunity_alert(op: ScoredOpportunity, *, ts: float | None = None) -> Alert:
    """机会。这是唯一一条"好消息"，但也是唯一一条**过期作废**的。"""
    size = op.suggested_notional
    stab = op.stability
    body = [
        f"多 {op.long.market} {op.long.symbol} ／ 空 {op.short.market} {op.short.symbol}",
        f"净年化 {op.net_apr:.1%}（毛 {op.gross_apr:.1%}，往返成本 {op.cost_rt:.2%}）",
        f"回本 {_fmt_hours(op.breakeven_h)}，历史同号中位存续 "
        f"{stab.median_run_h:.0f} 小时，胜率 {stab.p_win:.0%}",
        f"建议仓位 ${size:,.0f}（容量上限 ${op.capacity_usd:,.0f}）",
    ]
    if stab.is_prior:
        body.append(f"⚠ 历史样本只有 {stab.n_samples} 点，评分走的是保守先验，不可尽信")
    if op.reasons:
        body.append("判决 " + op.verdict + "：" + "；".join(op.reasons))
    action = (f"要做就按 ${size:,.0f} 建仓（超过容量会把自己的滑点吃进去）；"
              f"这个费差历史上中位只活 {stab.median_run_h:.0f} 小时，"
              f"回本要 {_fmt_hours(op.breakeven_h)}，晚一天大概率就没了")
    return Alert(
        type=EventType.OPPORTUNITY,
        subject=f"{op.base} {op.long.market}→{op.short.market}",
        bucket=apr_bucket(op.net_apr),
        severity=Severity.INFO,
        title=f"{op.base} 净年化 {op.net_apr:.0%}，可放 ${size:,.0f}",
        body=tuple(body),
        action=action,
        ts=ts if ts is not None else time.time(),
        data={"base": op.base, "net_apr": op.net_apr, "notional": size,
              "verdict": op.verdict},
    )


def naked_leg_alert(hedge_id: str, unit_uid: str, snap: ExposureSnapshot,
                    *, exposed_for_s: float, easy_market: str = "",
                    ts: float | None = None) -> Alert:
    """单腿裸露。severity 直接取 ExposureLevel——SOFT→HARD 的升级
    必须能突破静默期，这个映射就是那条通路的起点。"""
    sev = EXPOSURE_SEVERITY[snap.level]
    body = [
        f"裸露 ${snap.naked_usd:,.0f}，已持续 {exposed_for_s:.0f} 秒",
        f"净数量 {snap.net_qty}，总仓位 ${snap.gross_usd:,.0f}，"
        f"结构性残差门槛 ${snap.dust_usd:,.0f}",
        f"分级 {snap.level.name}：{snap.reason}",
    ]
    if snap.level is ExposureLevel.SOFT:
        action = "还在正常补腿窗口内，先别手动干预；超时会自动转撤退阶梯"
    elif snap.level is ExposureLevel.HARD:
        action = (f"补腿窗口已缩短，随时会转撤退。去看 {easy_market or '易腿'} 的盘口"
                  "和可用保证金——补不上通常是这两个原因")
    else:
        action = ("立刻处理：已跳过重试直接走撤退阶梯。"
                  "如果撤退也失败，手动到交易所用 reduce-only 平掉裸的那条腿")
    return Alert(
        type=EventType.NAKED_LEG, subject=f"{hedge_id[:8]}/{unit_uid}",
        severity=sev,
        title=f"分片 {unit_uid} 单腿裸露 ${snap.naked_usd:,.0f}（{snap.level.name}）",
        body=tuple(body), action=action,
        ts=ts if ts is not None else time.time(),
        data={"hedge_id": hedge_id, "unit": unit_uid, "level": snap.level.name,
              "naked_usd": snap.naked_usd, "exposed_for_s": exposed_for_s},
    )


def unwind_alert(hedge_id: str, unit_uid: str, phase: UnwindPhase, reason: str,
                 *, naked_usd: float = 0.0, elapsed_s: float = 0.0,
                 ts: float | None = None) -> Alert:
    """撤退。CROSS_HEDGE / SURVIVE 是"自动化已经认输"，必须当紧急事件推。"""
    sev = (Severity.CRITICAL
           if phase in (UnwindPhase.CROSS_HEDGE, UnwindPhase.SURVIVE, UnwindPhase.MARKET)
           else Severity.ERROR)
    body = [
        f"阶段 {phase.name}，已用 {elapsed_s:.0f} 秒",
        f"待中和敞口 ${naked_usd:,.0f}",
        f"触发原因：{reason}",
    ]
    if phase in (UnwindPhase.CROSS_HEDGE, UnwindPhase.SURVIVE):
        action = ("自动撤退已失败，需要人工接管：去交易所用 reduce-only 平掉裸腿，"
                  "或在别的市场开反向仓中和掉")
    else:
        action = "撤退在自动执行，盯着就行；它会按 5→10→20→40bp 递进直到平掉"
    return Alert(
        type=EventType.UNWIND, subject=f"{hedge_id[:8]}/{unit_uid}",
        bucket=phase.name, severity=sev,
        title=f"分片 {unit_uid} 进入撤退（{phase.name}）",
        body=tuple(body), action=action,
        ts=ts if ts is not None else time.time(),
        data={"hedge_id": hedge_id, "unit": unit_uid, "phase": phase.name},
    )


def breaker_alert(decision: BreakerDecision, *, account: str = "全局",
                  ts: float | None = None) -> Alert:
    """熔断。注意 SOFT 和 HARD 差别很大：前者只是禁止开新仓（可自动恢复），
    后者要人工 ack。文案必须说清楚这一点，否则用户以为程序死了。"""
    sev = Severity.CRITICAL if decision.level is BreakerLevel.HARD else Severity.ERROR
    body = [f"级别 {decision.level.name}", f"原因：{decision.reason}"]
    if decision.level is BreakerLevel.HARD:
        action = ("已停机等人工确认。平仓/撤退/降杠杆/划保证金仍然畅通"
                  "（熔断停的是赚钱动作，不停保命动作）。先查清原因再手动解除")
    else:
        action = "已禁止开新仓，现有仓位照常维护。条件恢复后会自动解除，暂时不用管"
    return Alert(
        type=EventType.BREAKER, subject=account, bucket=decision.level.name,
        severity=sev, title=f"熔断 {decision.level.name}：{decision.reason[:40]}",
        body=tuple(body), action=action,
        ts=ts if ts is not None else time.time(),
        data={"level": decision.level.name, "reason": decision.reason},
    )


def stale_alert(market: str, age_s: float, *, threshold_s: float = 300.0,
                ts: float | None = None) -> Alert:
    """行情陈旧。跨所套利最隐蔽的杀手：一边断了但价格还挂在界面上，
    你按一个根本不存在的价差反复下单。

    超过阈值 3 倍升级为 ERROR——这时候几乎可以确定是连接断了而不是冷门币不动。
    升级同时会突破静默期，这正是"状态恶化必须能穿透"要覆盖的场景。
    """
    sev = Severity.ERROR if age_s > threshold_s * 3 else Severity.WARN
    body = [
        f"{market} 的行情已 {age_s / 60:.1f} 分钟没有更新（阈值 {threshold_s / 60:.0f} 分钟）",
        "可能原因：WS 断线 / 该合约本来就没成交 / 本机时钟跑快了",
    ]
    action = (f"别按 {market} 现在显示的价格做任何判断。先点强制刷新，"
              "刷不出来就把这家的机会当作不可用——用陈旧价算出的价差是幻觉")
    return Alert(
        type=EventType.STALE, subject=market, severity=sev,
        title=f"{market} 行情已 {age_s / 60:.0f} 分钟未更新",
        body=tuple(body), action=action,
        ts=ts if ts is not None else time.time(),
        data={"market": market, "age_s": age_s},
    )


def risk_alert(account: str, tier: RiskTier, *, margin_ratio: float | None = None,
               liq_distance: float | None = None, ts: float | None = None) -> Alert:
    """爆仓风险。tier 由 safety.risk_tier 给，这里只负责翻译成人话 + 动作。"""
    sev = RISK_SEVERITY[tier]
    body = [f"风险档 {tier.name}"]
    if margin_ratio is not None:
        body.append(f"保证金率 {margin_ratio:.2f}（币安口径，越大越安全）")
    if liq_distance is not None:
        body.append(f"距爆仓价还有 {liq_distance:.1%}")
    actions = {
        RiskTier.YELLOW: "已自动停止加仓。检查是不是某一腿在单边亏 —— 中性仓位不该掉到这一档",
        RiskTier.ORANGE: "去降杠杆或补保证金。再掉一档就会被强制减仓",
        RiskTier.RED: "立刻补保证金或手动减仓，别等自动减仓在最差的价格上执行",
        RiskTier.BLACK: "已硬熔断。马上人工介入：补保证金 / 手动平仓，随时可能被强平或 ADL",
    }
    action = actions.get(tier, "暂无风险，无需动作")
    return Alert(
        type=EventType.RISK, subject=account, severity=sev,
        title=f"{account} 风险档 {tier.name}"
              + (f"，距爆仓 {liq_distance:.1%}" if liq_distance is not None else ""),
        body=tuple(body), action=action,
        ts=ts if ts is not None else time.time(),
        data={"account": account, "tier": tier.name,
              "margin_ratio": margin_ratio, "liq_distance": liq_distance},
    )


# ---- 去重与节流 ---------------------------------------------------------

class Verdict(str, enum.Enum):
    """为什么发（或不发）。带原因是为了让"怎么又推了"和"怎么没推"都能查。"""

    NEW = "new"                 # 第一次见
    REAPPEARED = "reappeared"   # 消失后重新出现 —— 新的决策时点
    ESCALATED = "escalated"     # 状态恶化，突破静默
    RENEWED = "renewed"         # 静默期已过，仍未解决
    SUPPRESSED = "suppressed"   # 静默期内压住
    MUTED = "muted"             # 该类事件被用户关掉了
    DISABLED = "disabled"       # 告警总开关关着

    @property
    def is_send(self) -> bool:
        return self in (Verdict.NEW, Verdict.REAPPEARED,
                        Verdict.ESCALATED, Verdict.RENEWED)


@dataclass(frozen=True, slots=True)
class ThrottleRule:
    """一类事件的节流规则。

    reappear_gap_s 是这里最容易被忽略的一个参数：它定义"多久没再出现就算它已经
    结束了"。没有它的话，一个机会消失半天又回来，会被上一次的静默期压掉——
    而那恰恰是新的决策时点。
    """

    silence_s: float
    reappear_gap_s: float
    escalate_breaks_silence: bool = True


DEFAULT_RULES: Mapping[EventType, ThrottleRule] = {
    # 机会：4 小时。同一个机会一天推 6 次已经是噪音的上限了
    EventType.OPPORTUNITY: ThrottleRule(silence_s=4 * 3600, reappear_gap_s=1800),
    # 风险类：15 分钟。要的是"还没解决"的提醒，恶化时靠 severity 穿透
    EventType.NAKED_LEG: ThrottleRule(silence_s=900, reappear_gap_s=120),
    EventType.UNWIND: ThrottleRule(silence_s=900, reappear_gap_s=300),
    EventType.BREAKER: ThrottleRule(silence_s=900, reappear_gap_s=300),
    EventType.STALE: ThrottleRule(silence_s=900, reappear_gap_s=600),
    EventType.RISK: ThrottleRule(silence_s=900, reappear_gap_s=120),
}


@dataclass(slots=True)
class KeyState:
    """一个去重键的状态。peak_severity 是"恶化突破静默"的判据基准。"""

    first_ts: float
    last_seen_ts: float
    last_sent_ts: float
    peak_severity: Severity
    sent_count: int = 0
    seen_count: int = 0


class Deduper:
    """按 (事件类型, 标的, 桶) 去重。

    判定与记账分成两个方法，因为它们的正确性各自独立：
    verdict() 是纯函数（好测），record() 只管更新状态。
    调用方必须两个都调——只调 verdict 会让每次都判成 NEW。
    """

    def __init__(self, rules: Mapping[EventType, ThrottleRule] | None = None) -> None:
        self._rules: dict[EventType, ThrottleRule] = dict(DEFAULT_RULES)
        if rules:
            self._rules.update(rules)
        self._state: dict[tuple[EventType, str, str], KeyState] = {}

    def rule(self, event_type: EventType) -> ThrottleRule:
        return self._rules[event_type]

    def verdict(self, alert: Alert, now: float) -> Verdict:
        rule = self._rules[alert.type]
        st = self._state.get(alert.key)
        if st is None:
            return Verdict.NEW
        # 顺序有讲究：先判"是不是已经消失过一轮"，因为那会连 peak_severity
        # 一起清掉——把一个已经结束的事故的严重度带到下一次是错的
        if now - st.last_seen_ts > rule.reappear_gap_s:
            return Verdict.REAPPEARED
        if rule.escalate_breaks_silence and alert.severity > st.peak_severity:
            return Verdict.ESCALATED
        if now - st.last_sent_ts >= rule.silence_s:
            return Verdict.RENEWED
        return Verdict.SUPPRESSED

    def record(self, alert: Alert, verdict: Verdict, now: float) -> None:
        """记账。**被压住的告警也要记**——否则 last_seen 不刷新，
        一个一直存在但一直被静默的事件会在 reappear_gap 之后被误判成"重新出现"，
        静默期形同虚设。
        """
        st = self._state.get(alert.key)
        if st is None or verdict is Verdict.REAPPEARED:
            st = KeyState(first_ts=now, last_seen_ts=now, last_sent_ts=0.0,
                          peak_severity=alert.severity)
            self._state[alert.key] = st
        st.last_seen_ts = now
        st.seen_count += 1
        st.peak_severity = max(st.peak_severity, alert.severity)
        if verdict.is_send:
            st.last_sent_ts = now
            st.sent_count += 1

    def sweep(self, event_type: EventType, active_subjects: Iterable[str]) -> int:
        """全量扫描后调用：不在活跃集里的键直接忘掉，下次再出现就是新事件。

        这是"消失后重新出现算新事件"的**确定性**通路；reappear_gap 是它的
        兜底（用于事件驱动、拿不到全量快照的场景）。机会榜每分钟重算一次，
        能给出准确的活跃集，就别依赖超时去猜。
        """
        active = set(active_subjects)
        gone = [k for k in self._state if k[0] is event_type and k[1] not in active]
        for k in gone:
            del self._state[k]
        return len(gone)

    def forget(self, key: tuple[EventType, str, str]) -> None:
        self._state.pop(key, None)

    def state_for(self, key: tuple[EventType, str, str]) -> KeyState | None:
        return self._state.get(key)

    def __len__(self) -> int:
        return len(self._state)


def rules_from_config(cfg: AlertsConfig) -> dict[EventType, ThrottleRule]:
    """把 config.toml 的两档静默期铺到六类事件上。

    只暴露两个旋钮（机会 / 风险）是刻意的：给六个旋钮，用户要么不调，
    要么调出一组互相矛盾的值。
    """
    opp = ThrottleRule(silence_s=float(cfg.silence_opportunity_s),
                       reappear_gap_s=float(cfg.reappear_gap_opportunity_s))
    risk = ThrottleRule(silence_s=float(cfg.silence_risk_s),
                        reappear_gap_s=float(cfg.reappear_gap_risk_s))
    return {
        EventType.OPPORTUNITY: opp,
        EventType.NAKED_LEG: risk,
        EventType.UNWIND: replace(risk, reappear_gap_s=max(risk.reappear_gap_s, 300.0)),
        EventType.BREAKER: replace(risk, reappear_gap_s=max(risk.reappear_gap_s, 300.0)),
        EventType.STALE: replace(risk, reappear_gap_s=max(risk.reappear_gap_s, 600.0)),
        EventType.RISK: risk,
    }


# ---- 通道 ---------------------------------------------------------------

def redact(secret: str) -> str:
    """日志里只留尾 4 位。告警系统自己把 token 打进日志，是最讽刺的泄露方式。"""
    if not secret:
        return "<未配置>"
    return "****" if len(secret) <= 4 else "*" * (len(secret) - 4) + secret[-4:]


def resolve_secret(literal: str, env_name: str) -> str:
    """密钥解析：环境变量优先，其次 config 里的字面量。

    环境变量优先是为了让服务器换 key 不用改配置文件；
    允许字面量是为了让本机用户少配一步。两者都为空时通道会被跳过并记原因——
    静默地不发告警，比配错更危险。
    """
    return (os.environ.get(env_name) or "").strip() or (literal or "").strip()


class Channel(Protocol):
    """一个投递通道。实现只要保证：失败就抛异常，别自己吞掉。

    吞异常的通道会让引擎以为投递成功，用户永远不知道自己的 token 早过期了。
    """

    name: str
    always_on: bool

    async def send(self, alert: Alert) -> None: ...


# 统一的 POST 抽象：三个远端通道形状一样（POST 一份 JSON），
# 抽出来是为了让测试能塞一个假的进去 —— 告警测试绝不该真的发网络请求。
Poster = Callable[[str, dict, dict], Awaitable[dict]]


async def httpx_post(url: str, payload: dict, headers: dict) -> dict:
    """默认 POST 实现。返回解析后的 body（非 JSON 则返回空 dict）。

    每次新建 client 是有意的：告警是低频事件（一天几条），
    维持一个长连接池反而多一个要管生命周期的东西。
    """
    import httpx

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        try:
            body = resp.json()
        except Exception:
            return {}
        return body if isinstance(body, dict) else {"result": body}


class LogChannel:
    """本地日志。**永远开着**，且总是第一个投递。

    其他通道全挂了也必须有账可查：告警系统静默失败，比压根没有告警更危险
    —— 后者你知道自己没有告警，前者你以为自己有。

    sink 的签名 (level, category, message) 刻意对齐 storage.log_event 和
    trader._record_event，接线时一个 lambda 就够：
        LogChannel(lambda l, c, m: storage.log_event(l, c, m, now_ms()))
    """

    name = "log"
    always_on = True

    def __init__(self, sink: Callable[[str, str, str], None] | None = None) -> None:
        self._sink = sink or _default_sink

    async def send(self, alert: Alert) -> None:
        self._sink(alert.severity.label, EVENT_CATEGORIES[alert.type], alert.as_text())


def _default_sink(level: str, category: str, message: str) -> None:
    level_no = {"info": logging.INFO, "warn": logging.WARNING,
                "error": logging.ERROR, "critical": logging.CRITICAL}.get(level, logging.INFO)
    log.log(level_no, "[%s] %s", category, message)


class WeChatChannel:
    """微信推送，走 PushPlus —— 复用用户已有的那条链路
    （crypto_social_tracker / ln_faucet 用的是同一个接口和同一种 token）。

    这里只做两件他们没做的事：token 变成配置项（绝不内嵌），
    以及**检查业务返回码**——PushPlus 在 token 失效时照样返回 HTTP 200，
    body 里才写 {"code": 401}。只看状态码等于把失败当成功。
    """

    name = "wechat"
    always_on = False

    def __init__(self, token: str, *, url: str = "https://www.pushplus.plus/send",
                 topic: str = "", poster: Poster | None = None) -> None:
        if not token:
            raise ValueError("微信通道缺少 PushPlus token")
        self._token = token
        self._url = url
        self._topic = topic
        self._post = poster or httpx_post

    def __repr__(self) -> str:      # 避免 token 出现在任何日志/异常里
        return f"WeChatChannel(token={redact(self._token)})"

    async def send(self, alert: Alert) -> None:
        payload = {
            "token": self._token,
            "title": f"[{EVENT_LABELS[alert.type]}] {alert.title}"[:100],
            "content": alert.as_markdown(),
            "template": "markdown",
        }
        if self._topic:
            payload["topic"] = self._topic
        body = await self._post(self._url, payload, {})
        code = body.get("code")
        if code is not None and int(code) != 200:
            raise RuntimeError(f"PushPlus 拒绝投递：code={code} msg={body.get('msg')}")


class TelegramChannel:
    """Telegram Bot。

    坑：Bot 建好后必须由你本人先对它点一次 Start，否则 sendMessage 返回
    HTTP 200 + {"ok": false, "description": "bot can't initiate conversation"}。
    这是这个通道"配好了却收不到"的头号原因，所以必须解析 body 而不是只看状态码。
    """

    name = "telegram"
    always_on = False

    def __init__(self, bot_token: str, chat_id: str, *,
                 api_base: str = "https://api.telegram.org",
                 poster: Poster | None = None) -> None:
        if not bot_token or not chat_id:
            raise ValueError("Telegram 通道缺少 bot_token 或 chat_id")
        self._token = bot_token
        self._chat_id = chat_id
        self._api_base = api_base.rstrip("/")
        self._post = poster or httpx_post

    def __repr__(self) -> str:
        return f"TelegramChannel(bot_token={redact(self._token)}, chat_id={self._chat_id})"

    async def send(self, alert: Alert) -> None:
        url = f"{self._api_base}/bot{self._token}/sendMessage"
        # 不带 parse_mode：见 Alert.as_text 的说明，转义漏一个就整条发不出去
        payload = {"chat_id": self._chat_id, "text": alert.as_text(),
                   "disable_web_page_preview": True}
        body = await self._post(url, payload, {})
        if body and body.get("ok") is False:
            raise RuntimeError(f"Telegram 拒绝投递：{body.get('description')}")


def webhook_payload(alert: Alert, style: str) -> dict[str, Any]:
    """按目标机器人的形状装载荷。

    各家的 body 互不兼容，与其让用户自己写中转，不如把这几种常见的内置。
    raw 是默认：结构化全量 + 渲染好的 text，自建服务想怎么用怎么用。
    """
    if style == "feishu":
        return {"msg_type": "text", "content": {"text": alert.as_text()}}
    if style == "dingtalk":
        return {"msgtype": "text",
                "text": {"content": f"carryfarm\n{alert.as_text()}"}}
    if style == "bark":
        return {"title": alert.title, "body": alert.as_text(),
                "group": EVENT_LABELS[alert.type],
                "level": "timeSensitive" if alert.severity >= Severity.ERROR else "active"}
    return alert.as_dict()


class WebhookChannel:
    """通用 Webhook。POST 一份 JSON，接飞书/钉钉/Bark/自建都行。"""

    name = "webhook"
    always_on = False

    def __init__(self, url: str, *, style: str = "raw",
                 headers: Mapping[str, str] | None = None,
                 poster: Poster | None = None) -> None:
        if not url:
            raise ValueError("Webhook 通道缺少 url")
        self._url = url
        self._style = style
        self._headers = dict(headers or {})
        self._post = poster or httpx_post

    def __repr__(self) -> str:
        # URL 本身常常就是密钥（飞书/钉钉的 webhook token 在路径里），一并遮掉
        return f"WebhookChannel(url=…{redact(self._url)}, style={self._style})"

    async def send(self, alert: Alert) -> None:
        await self._post(self._url, webhook_payload(alert, self._style), self._headers)


@dataclass(frozen=True, slots=True)
class ChannelPlan:
    """建通道的结果。skipped 必须带出去在界面上显示——
    "开了微信通道但没读到 token" 是一定要让人知道的，
    否则用户以为自己有告警，实际上只有本地日志。"""

    channels: tuple[Channel, ...]
    skipped: Mapping[str, str]


def build_channels(cfg: AlertsConfig, *,
                   sink: Callable[[str, str, str], None] | None = None,
                   poster: Poster | None = None) -> ChannelPlan:
    """按配置建通道。密钥一律经 resolve_secret 从 env/config 取，绝不硬编码。"""
    channels: list[Channel] = [LogChannel(sink)]      # 永远第一个，永远开着
    skipped: dict[str, str] = {}

    if cfg.wechat.enabled:
        token = resolve_secret(cfg.wechat.token, cfg.wechat.token_env)
        if token:
            channels.append(WeChatChannel(token, url=cfg.wechat.url,
                                          topic=cfg.wechat.topic, poster=poster))
        else:
            skipped["wechat"] = (f"没读到 PushPlus token（环境变量 "
                                 f"{cfg.wechat.token_env} 和 config 都是空的）")

    if cfg.telegram.enabled:
        token = resolve_secret(cfg.telegram.bot_token, cfg.telegram.bot_token_env)
        chat = resolve_secret(cfg.telegram.chat_id, cfg.telegram.chat_id_env)
        if token and chat:
            channels.append(TelegramChannel(token, chat, api_base=cfg.telegram.api_base,
                                            poster=poster))
        else:
            missing = "bot_token" if not token else "chat_id"
            skipped["telegram"] = f"没读到 {missing}"

    if cfg.webhook.enabled:
        url = resolve_secret(cfg.webhook.url, cfg.webhook.url_env)
        if url:
            headers: dict[str, str] = {}
            if cfg.webhook.auth_header:
                value = resolve_secret("", cfg.webhook.auth_value_env)
                if value:
                    headers[cfg.webhook.auth_header] = value
            channels.append(WebhookChannel(url, style=cfg.webhook.style,
                                           headers=headers, poster=poster))
        else:
            skipped["webhook"] = f"没读到 url（环境变量 {cfg.webhook.url_env} 和 config 都是空的）"

    return ChannelPlan(tuple(channels), skipped)


# ---- 引擎 ---------------------------------------------------------------

@dataclass(slots=True)
class ChannelStats:
    sent: int = 0
    failed: int = 0
    consecutive_failures: int = 0
    last_error: str = ""
    last_ok_ts: float = 0.0


@dataclass(frozen=True, slots=True)
class DispatchResult:
    alert: Alert
    verdict: Verdict
    delivered: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()

    @property
    def sent(self) -> bool:
        return self.verdict.is_send

    @property
    def all_failed(self) -> bool:
        return self.sent and not self.delivered


class AlertEngine:
    """告警引擎：去重 → 扇出 → 记账。

    对外只有一个动词：dispatch(alert)。除了协程取消，它**从不抛异常**——
    告警路径上的异常把主循环打断，等于用告警系统制造了事故。
    （取消必须放行，否则优雅退出会被一个卡住的 webhook 拖到超时硬杀。）
    """

    def __init__(self, channels: Sequence[Channel], *,
                 rules: Mapping[EventType, ThrottleRule] | None = None,
                 muted: Iterable[EventType] = (),
                 enabled: bool = True,
                 timeout_s: float = 10.0,
                 clock: Callable[[], float] = time.time,
                 skipped: Mapping[str, str] | None = None) -> None:
        self._channels = tuple(channels)
        self.dedup = Deduper(rules)
        self._muted = frozenset(muted)
        self._enabled = enabled
        self._timeout_s = timeout_s
        self._now = clock
        self.skipped = dict(skipped or {})
        self.stats: dict[str, ChannelStats] = {c.name: ChannelStats() for c in self._channels}
        # 判定和记账必须原子：两个协程同时投递同一个键，
        # 不加锁会双双判成 NEW，去重当场失效
        self._lock = asyncio.Lock()

    @classmethod
    def from_config(cls, cfg: AlertsConfig, *,
                    sink: Callable[[str, str, str], None] | None = None,
                    poster: Poster | None = None,
                    clock: Callable[[], float] = time.time) -> AlertEngine:
        plan = build_channels(cfg, sink=sink, poster=poster)
        return cls(plan.channels,
                   rules=rules_from_config(cfg),
                   muted=[EventType(m) for m in cfg.mute],
                   enabled=cfg.enabled,
                   timeout_s=float(cfg.timeout_s),
                   clock=clock,
                   skipped=plan.skipped)

    @property
    def channels(self) -> tuple[Channel, ...]:
        return self._channels

    @property
    def channel_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self._channels)

    def is_muted(self, event_type: EventType) -> bool:
        return event_type in self._muted

    async def dispatch(self, alert: Alert) -> DispatchResult:
        if not self._enabled:
            return DispatchResult(alert, Verdict.DISABLED)
        if alert.type in self._muted:
            return DispatchResult(alert, Verdict.MUTED)

        async with self._lock:
            now = self._now()
            verdict = self.dedup.verdict(alert, now)
            self.dedup.record(alert, verdict, now)
        if not verdict.is_send:
            return DispatchResult(alert, verdict)

        delivered, failed = await self._fanout(alert)
        if not delivered:
            # 一条都没送出去是真事故——但也只能记本地日志，别再递归告警
            log.error("告警一条通道都没送出去：%s / %s", alert.title, failed)
        return DispatchResult(alert, verdict, tuple(delivered), tuple(failed))

    async def dispatch_many(self, alerts: Sequence[Alert]) -> list[DispatchResult]:
        return [await self.dispatch(a) for a in alerts]

    async def _fanout(self, alert: Alert) -> tuple[list[str], list[tuple[str, str]]]:
        """扇出。本地通道先串行落地，远端通道再并发发。

        顺序是刻意的：远端通道要走网络，可能超时 10 秒；如果和本地日志一起
        gather，进程正好在这 10 秒里被杀掉，就连本地记录都没有了。
        """
        delivered: list[str] = []
        failed: list[tuple[str, str]] = []

        for ch in (c for c in self._channels if getattr(c, "always_on", False)):
            name, err = await self._send_one(ch, alert)
            (failed.append((name, err)) if err else delivered.append(name))

        remote = [c for c in self._channels if not getattr(c, "always_on", False)]
        if remote:
            for name, err in await asyncio.gather(
                    *(self._send_one(c, alert) for c in remote)):
                (failed.append((name, err)) if err else delivered.append(name))
        return delivered, failed

    async def _send_one(self, channel: Channel, alert: Alert) -> tuple[str, str | None]:
        """单通道投递。**永不抛异常**，也永不无限等待。

        没有超时的话，一个 TCP 连上了但不返回的通道能把整个扇出挂死，
        后面的通道一条都发不出去——"一个通道挂了不影响其他通道" 就是空话。
        """
        st = self.stats.setdefault(channel.name, ChannelStats())
        try:
            await asyncio.wait_for(channel.send(alert), timeout=self._timeout_s)
        except asyncio.TimeoutError:
            st.failed += 1
            st.consecutive_failures += 1
            st.last_error = f"投递超时（>{self._timeout_s:.0f}s）"
            return channel.name, st.last_error
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            st.failed += 1
            st.consecutive_failures += 1
            # 异常文本可能带 URL/token，不原样外泄到别的通道，只记类型和消息
            st.last_error = f"{type(exc).__name__}: {exc}"
            return channel.name, st.last_error
        st.sent += 1
        st.consecutive_failures = 0
        st.last_ok_ts = self._now()
        return channel.name, None


# ---- 检测周期与陈旧监控 -------------------------------------------------

class DetectionClock:
    """每类事件各自的检测周期。

    统一一个周期必然错：机会一天就一两次，裸腿按秒计价。绑在一起的话，
    要么把机会检测拉到秒级白烧限频，要么把裸腿检测放到分钟级——
    而裸腿多裸一分钟的代价，可以吃掉几百次机会的收益。

    用单调时钟：系统时间被 NTP 往回拨时，墙钟会让检测停摆几分钟。
    """

    def __init__(self, intervals: Mapping[EventType, float] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._intervals = dict(DETECT_INTERVAL_S)
        if intervals:
            self._intervals.update(intervals)
        self._now = clock
        self._last: dict[EventType, float] = {}

    def interval(self, event_type: EventType) -> float:
        return self._intervals[event_type]

    def due(self, event_type: EventType) -> bool:
        """到点了吗。到点即打点——调用方只要在主循环里问就行。

        周期为 0 的事件类型（UNWIND/BREAKER）恒为 True：它们是事件驱动的，
        由状态机在发生的当下直接投递，这里只是不挡它。
        """
        interval = self._intervals[event_type]
        now = self._now()
        last = self._last.get(event_type)
        if interval <= 0 or last is None or now - last >= interval:
            self._last[event_type] = now
            return True
        return False

    def reset(self, event_type: EventType) -> None:
        self._last.pop(event_type, None)


class StaleMonitor:
    """行情陈旧监控。

    跨所套利最隐蔽的杀手：一边的连接断了，但界面上的价格还挂着，
    你按一个根本不存在的价差反复下单。界面上的三色标记（3s 黄 / 9s 红）
    管的是"这一拍能不能下单"，这里管的是"这条腿是不是已经死了"——
    两者阈值差两个数量级，因为要回答的问题不同。
    """

    def __init__(self, *, stale_after_s: float = 300.0,
                 clock: Callable[[], float] = time.time) -> None:
        self.stale_after_s = stale_after_s
        self._now = clock
        self._last: dict[str, float] = {}

    def touch(self, market: str, ts: float | None = None) -> None:
        self._last[market] = ts if ts is not None else self._now()

    def drop(self, market: str) -> None:
        self._last.pop(market, None)

    def ages(self, now: float | None = None) -> dict[str, float]:
        t = now if now is not None else self._now()
        return {m: t - ts for m, ts in self._last.items()}

    def stale(self, now: float | None = None) -> list[tuple[str, float]]:
        return sorted(((m, age) for m, age in self.ages(now).items()
                       if age > self.stale_after_s), key=lambda kv: -kv[1])

    def alerts(self, now: float | None = None) -> list[Alert]:
        t = now if now is not None else self._now()
        return [stale_alert(m, age, threshold_s=self.stale_after_s, ts=t)
                for m, age in self.stale(t)]


# ---- 机会筛选 -----------------------------------------------------------

def actionable_opportunities(rows: Sequence[ScoredOpportunity], cfg: AlertsConfig
                             ) -> list[ScoredOpportunity]:
    """从榜单里挑出**值得把人从别的事上拉过来**的那几个。

    门槛比 scoring 的 min_net_apr 高一个量级是有意的：那个回答"值不值得列出来"，
    这个回答"值不值得响一声"。真实历史接上后大部分机会是负期望，
    所以这个筛子必须很紧——推送的价值和它的稀有度成正比。
    """
    floor_apr = float(cfg.min_net_apr)
    floor_cap = float(cfg.min_capacity_usd)
    out = []
    for op in rows:
        if cfg.require_actionable and not op.is_actionable:
            continue
        if op.net_apr < floor_apr:
            continue
        # 容量不够就是"看得见吃不着"，推了也只是让人白高兴
        if op.suggested_notional < floor_cap:
            continue
        out.append(op)
    return out
