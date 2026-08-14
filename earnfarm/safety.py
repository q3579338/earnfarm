"""安全决策层：敞口分级、补腿无望判据、撤退阶梯、熔断。

这里只做**决策**不做 I/O——所有函数都是纯函数，输入状态快照、输出该干什么。
执行副作用在 executor.py。这样拆是为了让最容易出错的这部分能被完整测试。

三条设计公理（后面所有决策都从这里推出）：

  公理 1：仓位是真值，订单是过程。
    本地记账与交易所 position 冲突时无条件以交易所为准。订单回执可以丢、可以迟到、
    可以撒谎（**超时不等于失败**），仓位快照不会。

  公理 2：不确定性要前置消化。
    先做不确定的事（难腿），再做确定的事（易腿）。这同时决定了建仓顺序、平仓顺序，
    以及"超时后先 cancel 再查仓"的固定动作序列。

  公理 3：熔断停的是赚钱动作，不停保命动作。
    一刀切的全局停机在有裸腿时等于自杀。熔断必须有白名单。

原版工具（qqq.bot）的根本错误不是"没有止损"，而是把「两腿并发下单 + 无限重试补腿」
当成了收敛逻辑——**真正的收敛条件必须包含"放弃"这个终态**。
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

ZERO = Decimal("0")


class ExposureLevel(enum.IntEnum):
    """净敞口分级。数值可比较，越大越危险。"""

    DUST = 0        # 结构性残差，不触发任何动作，只记账
    SOFT = 1        # 正常补腿窗口
    HARD = 2        # 缩短窗口，准备撤退
    CRITICAL = 3    # 跳过重试，直接市价撤退


class RiskTier(enum.IntEnum):
    """账户风险档。由保证金率和爆仓距离共同决定。"""

    GREEN = 0
    YELLOW = 1      # 停止加仓
    ORANGE = 2      # 降杠杆
    RED = 3         # 强制减仓
    BLACK = 4       # 硬熔断


class UnwindPhase(enum.IntEnum):
    """撤退阶梯。时间驱动升级，越晚越激进。"""

    TERMINATE = -1  # 前置：终结在途订单（强制，不可跳过）
    MARKETABLE = 0  # 0~2s：对手价穿 5bp 的 IOC
    ESCALATE = 1    # 2~6s：穿价 10→20→40bp 递进 + 切片
    MARKET = 2      # >6s：真市价，被拒则降级为穿 200bp 分 3 片
    CROSS_HEDGE = 3 # Phase2 连败：换个市场中和敞口
    SURVIVE = 4     # 全败：不再尝试成交，转防爆仓 + 持续告警


class RepairVerdict(str, enum.Enum):
    """补腿是否还有希望。"""

    CONTINUE = "continue"
    # 硬判据——命中即刻判死，一次重试都不做
    SYMBOL_NOT_TRADING = "symbol_not_trading"
    EXCHANGE_REDUCE_ONLY = "exchange_reduce_only"
    INSUFFICIENT_MARGIN = "insufficient_margin"
    ACCOUNT_RESTRICTED = "account_restricted"
    MAINTENANCE = "maintenance"
    EXCHANGE_DEAD = "exchange_dead"
    # 软判据——累计计分
    TIMEOUT = "timeout"
    BUDGET_EXHAUSTED = "budget_exhausted"
    BOOK_UNUSABLE = "book_unusable"
    UNECONOMIC = "uneconomic"
    TIME_WEIGHTED = "time_weighted"

    @property
    def is_hopeless(self) -> bool:
        return self is not RepairVerdict.CONTINUE

    @property
    def is_hard(self) -> bool:
        return self in _HARD_VERDICTS


_HARD_VERDICTS = frozenset({
    RepairVerdict.SYMBOL_NOT_TRADING,
    RepairVerdict.EXCHANGE_REDUCE_ONLY,
    RepairVerdict.INSUFFICIENT_MARGIN,
    RepairVerdict.ACCOUNT_RESTRICTED,
    RepairVerdict.MAINTENANCE,
    RepairVerdict.EXCHANGE_DEAD,
})


# ---- 参数 ---------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SafetyParams:
    """安全参数。初值来自设计文档，都可在 config.toml 里覆盖。"""

    # 敞口分级（相对权益 / 相对总仓位，取更严者）
    naked_soft_pct_equity: float = 0.004
    naked_soft_pct_gross: float = 0.03
    naked_hard_pct_equity: float = 0.012
    naked_hard_pct_gross: float = 0.10
    naked_crit_pct_equity: float = 0.030
    naked_crit_pct_gross: float = 0.25
    # 裸腿浮亏超过权益的该比例直接 CRITICAL
    naked_pnl_crit_pct_equity: float = 0.005
    # 时间加权敞口预算（USD·秒）：防"一直 SOFT 但裸了 5 分钟"
    exposure_budget_usd_s: float = 0.003 * 30      # 乘以 equity 后使用

    # 补腿预算
    repair_soft_timeout_s: float = 20.0
    repair_soft_attempts: int = 12
    repair_hard_timeout_s: float = 6.0
    repair_hard_attempts: int = 5
    repair_backoff_ms: tuple[int, ...] = (200, 300, 500, 800, 1200, 1500)
    repair_cross_bp: float = 8.0
    gate_fail_limit: int = 8
    # 补腿成本超过撤退成本的该倍数即判不经济
    econ_ratio: float = 2.0

    # 撤退阶梯
    unwind_p0_s: float = 2.0
    unwind_p0_cross_bp: float = 5.0
    unwind_p1_s: float = 6.0
    unwind_p1_cross_bp: tuple[float, ...] = (10.0, 20.0, 40.0)
    unwind_slice_depth_frac: float = 0.6
    unwind_p2_fallback_bp: float = 200.0
    cooldown_s: float = 600.0
    slice_shrink_on_unwind: float = 0.5
    blacklist_unwinds_24h: int = 2

    # 建仓
    parallel_max_usd: float = 5_000.0
    parallel_min_depth_score: float = 0.9
    parallel_max_reject_rate: float = 0.005
    slice_interval_ms: int = 500
    slice_depth_frac: float = 0.15

    # 风险档（保证金率阈值，币安口径：权益/保证金，越大越安全）
    mr_yellow: float = 4.0
    mr_orange: float = 2.5
    mr_red: float = 1.6
    mr_black: float = 1.25
    # 迟滞：改善超过该比例并保持一段时间才降档，防止在边界反复横跳
    hysteresis_improve: float = 0.20
    hysteresis_hold_s: float = 60.0

    # 熔断
    equity_drop_15m: float = 0.015
    equity_drop_1h: float = 0.030
    api_error_rate_5m: float = 0.25
    unwind_count_30m: int = 3
    slippage_alert_mult: float = 3.0
    consecutive_entry_fail: int = 3

    # 杠杆
    effective_leverage_target: float = 3.0
    leverage_safety_k: float = 3.0
    leverage_hard_cap: Decimal = Decimal("20")
    tier_edge_avoid: float = 0.05
    margin_buffer_per_exchange: float = 0.40


DEFAULT_PARAMS = SafetyParams()


# ---- 敞口 ---------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class LegFilters:
    """交易所过滤器。dust 门槛必须由这些推导，不能拍脑袋。"""

    step_size: Decimal       # 数量最小变动
    min_qty: Decimal
    min_notional: Decimal
    contract_size: Decimal = Decimal("1")


def dust_threshold_usd(a: LegFilters, b: LegFilters, mark: Decimal) -> float:
    """结构性不可消除残差的门槛。

    两所的 stepSize / minQty / 合约面值不同，必然产生一个补不平的残差。
    把它当裸奔会让状态机陷入「补 → 因低于 minQty 被拒 → 再补」的死循环，
    这是这类工具最常见的卡死原因。
    """
    step_notional = max(a.step_size, b.step_size, a.contract_size, b.contract_size) * mark
    return max(
        5.0,
        3 * float(step_notional),
        1.5 * float(max(a.min_notional, b.min_notional)),
    )


@dataclass(frozen=True, slots=True)
class ExposureSnapshot:
    level: ExposureLevel
    naked_usd: float
    gross_usd: float
    net_qty: Decimal
    dust_usd: float
    reason: str = ""


def classify_exposure(pos_long: Decimal, pos_short: Decimal, mark: Decimal,
                      equity: float, filters_long: LegFilters, filters_short: LegFilters,
                      *, vol_scale: float = 1.0, naked_pnl: float = 0.0,
                      exposure_integral: float = 0.0,
                      params: SafetyParams = DEFAULT_PARAMS) -> ExposureSnapshot:
    """净敞口分级。

    vol_scale = 该币 1 分钟波动率 / BTC 的，用于按波动率缩放阈值——
    山寨币的 SOFT 和 BTC 的 SOFT 不是一回事。
    """
    net = pos_long + pos_short          # 理想为 0（空头持仓是负数）
    naked = abs(float(net * mark))
    gross = float((abs(pos_long) + abs(pos_short)) / 2 * mark)
    dust = dust_threshold_usd(filters_long, filters_short, mark)

    if naked < dust:
        return ExposureSnapshot(ExposureLevel.DUST, naked, gross, net, dust,
                                "低于结构性残差门槛，不可消除，只记账")

    scale = max(0.3, min(3.0, vol_scale))
    soft = min(params.naked_soft_pct_equity * equity, params.naked_soft_pct_gross * gross) * scale
    hard = min(params.naked_hard_pct_equity * equity, params.naked_hard_pct_gross * gross) * scale
    crit = min(params.naked_crit_pct_equity * equity, params.naked_crit_pct_gross * gross) * scale

    # 裸腿浮亏过大直接 CRITICAL——金额还没超标但已经在流血了
    if naked_pnl < -params.naked_pnl_crit_pct_equity * equity:
        return ExposureSnapshot(ExposureLevel.CRITICAL, naked, gross, net, dust,
                                f"裸腿浮亏 {naked_pnl:,.0f} 超过权益的 "
                                f"{params.naked_pnl_crit_pct_equity:.1%}")
    # 时间加权：一直 SOFT 但裸了很久，同样要升级
    if exposure_integral > params.exposure_budget_usd_s * equity:
        return ExposureSnapshot(ExposureLevel.HARD, naked, gross, net, dust,
                                "时间加权敞口超预算：金额没超标但裸太久了")

    if naked < soft:
        return ExposureSnapshot(ExposureLevel.SOFT, naked, gross, net, dust, "正常补腿窗口")
    if naked < crit:
        return ExposureSnapshot(ExposureLevel.HARD, naked, gross, net, dust, "缩短窗口，准备撤退")
    return ExposureSnapshot(ExposureLevel.CRITICAL, naked, gross, net, dust,
                            "超出临界阈值，跳过重试直接撤退")


# ---- 补腿无望判据 -------------------------------------------------------

@dataclass(frozen=True, slots=True)
class LegHealth:
    """待补那条腿所在市场的健康状况。"""

    symbol_trading: bool = True
    exchange_reduce_only: bool = False
    available_margin: float = float("inf")
    required_margin: float = 0.0
    can_transfer_in: bool = False
    account_restricted: bool = False
    in_maintenance: bool = False
    dead_for_s: float = 0.0
    consecutive_gate_fails: int = 0
    est_repair_cost: float = 0.0
    est_unwind_cost: float = float("inf")


def repair_verdict(level: ExposureLevel, health: LegHealth, *,
                   elapsed_s: float, attempts: int, exposure_integral: float,
                   equity: float, exchange_dead_s: float = 10.0,
                   params: SafetyParams = DEFAULT_PARAMS) -> RepairVerdict:
    """补腿还有没有希望。

    硬判据是关键：很多情况下重试 100 次也不可能成功，浪费的每一秒都是纯风险。
    这正是原版工具完全缺失的部分——它会永远重试补腿而绝不撤退。
    """
    if level is ExposureLevel.CRITICAL:
        return RepairVerdict.TIME_WEIGHTED      # CRITICAL 不重试，直接走撤退

    # 硬判据：命中即刻判死
    if not health.symbol_trading:
        return RepairVerdict.SYMBOL_NOT_TRADING
    if health.exchange_reduce_only:
        return RepairVerdict.EXCHANGE_REDUCE_ONLY
    if health.available_margin < health.required_margin and not health.can_transfer_in:
        return RepairVerdict.INSUFFICIENT_MARGIN
    if health.account_restricted:
        return RepairVerdict.ACCOUNT_RESTRICTED
    if health.in_maintenance:
        return RepairVerdict.MAINTENANCE
    if health.dead_for_s > exchange_dead_s:
        return RepairVerdict.EXCHANGE_DEAD

    # 软判据
    if level is ExposureLevel.HARD:
        timeout, budget = params.repair_hard_timeout_s, params.repair_hard_attempts
    else:
        timeout, budget = params.repair_soft_timeout_s, params.repair_soft_attempts
    if elapsed_s > timeout:
        return RepairVerdict.TIMEOUT
    if attempts >= budget:
        return RepairVerdict.BUDGET_EXHAUSTED
    if health.consecutive_gate_fails >= params.gate_fail_limit:
        return RepairVerdict.BOOK_UNUSABLE
    # 经济判据：补腿只有在比撤退便宜时才值得做。
    # 空腿市场流动性枯竭时这条会先于超时触发，这是正确的。
    if health.est_repair_cost > params.econ_ratio * health.est_unwind_cost:
        return RepairVerdict.UNECONOMIC
    if exposure_integral > params.exposure_budget_usd_s * equity:
        return RepairVerdict.TIME_WEIGHTED
    return RepairVerdict.CONTINUE


def repair_backoff_ms(attempt: int, params: SafetyParams = DEFAULT_PARAMS) -> int:
    seq = params.repair_backoff_ms
    return seq[min(attempt, len(seq) - 1)]


# ---- 撤退阶梯 -----------------------------------------------------------

@dataclass(frozen=True, slots=True)
class UnwindStep:
    phase: UnwindPhase
    cross_bp: float | None      # None = 真市价
    qty_cap_frac: float         # 单片不超过可吃量的这个比例
    reduce_only: bool = True    # 硬性要求，见下
    note: str = ""


def unwind_step(elapsed_s: float, phase2_failures: int = 0, *,
                force_market: bool = False,
                params: SafetyParams = DEFAULT_PARAMS) -> UnwindStep:
    """按已用时间决定这一步怎么平。

    单一选择必然错：小额裸奔用市价是浪费；大额裸奔一笔市价会打穿多档，
    滑点可能超过裸奔本身的风险。正确结构是时间驱动的升级阶梯。

    注意 reduce_only 恒为 True——如果仓位已被 ADL/强平清掉，
    普通单会**开出反向仓**。这是撤退里最阴的坑。
    """
    if phase2_failures >= 2:
        return UnwindStep(UnwindPhase.CROSS_HEDGE, None, 1.0,
                          note="市价连续失败，改用其他市场中和敞口")
    if force_market or elapsed_s > params.unwind_p1_s:
        return UnwindStep(UnwindPhase.MARKET, None, 1.0,
                          note="真市价清剩余；被拒则降级为穿 200bp 分 3 片")
    if elapsed_s > params.unwind_p0_s:
        idx = min(int((elapsed_s - params.unwind_p0_s) / 1.3), len(params.unwind_p1_cross_bp) - 1)
        return UnwindStep(UnwindPhase.ESCALATE, params.unwind_p1_cross_bp[idx],
                          params.unwind_slice_depth_frac, note="递进穿价，避免一次砸穿")
    return UnwindStep(UnwindPhase.MARKETABLE, params.unwind_p0_cross_bp,
                      params.unwind_slice_depth_frac,
                      note="效果接近市价但有滑点上限，能挡插针和深度真空")


# ---- 建仓：难腿判定与分片 -----------------------------------------------

@dataclass(frozen=True, slots=True)
class LegHardness:
    est_slippage_bp: float
    spread_bp: float
    reject_rate: float          # EWMA
    latency_p95_ms: float
    funding_settle_within_60s: bool = False
    ws_degraded: bool = False

    def score(self) -> float:
        """综合硬度分。拒单率权重最高——它直接决定补腿失败的概率。"""
        return (self.est_slippage_bp
                + self.spread_bp / 2
                + self.reject_rate * 200
                + self.latency_p95_ms / 20
                + (30 if self.funding_settle_within_60s else 0)
                + (50 if self.ws_degraded else 0))


class EntryMode(str, enum.Enum):
    SEQUENTIAL_HARD_FIRST = "sequential_hard_first"
    PARALLEL = "parallel"


def choose_entry_mode(a: LegHardness, b: LegHardness, slice_usd: float,
                      depth_score_a: float, depth_score_b: float,
                      params: SafetyParams = DEFAULT_PARAMS) -> EntryMode:
    """并发还是串行。

    默认串行难腿优先——这和"并发窗口更短"的直觉相反，理由是：
    并发是"窗口的最小值"，串行是"尾部风险的最小值"。
    资金费套利的收益是 bp 级、可预测的；裸奔的损失是尾部、不可预测的。
    一次难腿裸奔 30 秒的损失，能吃掉几百次省下的 50ms 窗口。

    串行还有个更重要的隐性收益：它把"补腿失败"从"可能发生在难腿上"
    变成了"只可能发生在易腿上"——那些补腿无望的硬判据绝大多数只会命中难腿，
    串行等于把最坏情况从状态空间里删掉了。
    """
    if (slice_usd < params.parallel_max_usd
            and min(depth_score_a, depth_score_b) > params.parallel_min_depth_score
            and max(a.reject_rate, b.reject_rate) < params.parallel_max_reject_rate):
        return EntryMode.PARALLEL
    return EntryMode.SEQUENTIAL_HARD_FIRST


def slice_max_usd(hard_leg_top5_depth_usd: float, equity: float,
                  liquidity_tier_mult: float = 1.0,
                  params: SafetyParams = DEFAULT_PARAMS) -> float:
    """单片上限。做不到原子，就把不原子的代价除以 N。

    代价是建仓总时长变长；收益是单次事故的最大敞口从"全仓"降到"一片"。
    资金费是持有期收益，晚 30 秒建完仓损失微乎其微，这个交换绝对划算。
    """
    naked_hard_usd = params.naked_hard_pct_equity * equity
    return max(1.0, min(
        naked_hard_usd * 0.8,                                   # 单片全裸也不超 HARD 档
        params.slice_depth_frac * hard_leg_top5_depth_usd,      # 不超难腿浅档深度
        5_000.0 * liquidity_tier_mult,
    ))


def floor_to_step(qty: Decimal, step: Decimal) -> Decimal:
    """向零取整到步长整数倍。永远只会让量变小，不会超出目标。"""
    if step <= ZERO:
        return qty
    n = (abs(qty) / step).to_integral_value(rounding="ROUND_FLOOR")
    out = n * step
    return -out if qty < ZERO else out


def second_leg_qty(first_leg_filled: Decimal, step_a: Decimal,
                   step_b: Decimal) -> tuple[Decimal, Decimal]:
    """第二腿数量 = 第一腿**实际成交量**（不是目标量）向下对齐到公共步长。

    这样数量错配在结构上就不可能出现——原版按目标量各下各的，
    一腿部分成交就必然错配。

    返回 (第二腿下单量, 需要削平的残差)。残差若大于 dust 应立刻把第一腿
    多出的部分削掉：与其留一个补不上的残差裸着，不如削平。
    """
    common = max(step_a, step_b)
    qty = floor_to_step(first_leg_filled, common)
    return qty, first_leg_filled - qty


# ---- 风险档与杠杆 -------------------------------------------------------

def normalize_margin_ratio(equity: float, maintenance_margin: float) -> float | None:
    """统一到币安口径：权益 / 维持保证金，**越大越安全**。

    其他所通常给的是倒数，适配器要负责转换后再传进来。
    """
    if maintenance_margin <= 0:
        return None
    return equity / maintenance_margin


def liquidation_distance(mark: Decimal, liq: Decimal | None) -> float | None:
    """距爆仓价的相对距离。0.2 表示还有 20% 空间。

    原版算了这个数但只在界面上显示，执行器一行都没读它，
    策略规则也引用不到。这里它会真正进入决策。
    """
    if liq is None or mark == ZERO:
        return None
    return float(abs(mark - liq) / mark)


def risk_tier(margin_ratio: float | None, liq_distance: float | None,
              params: SafetyParams = DEFAULT_PARAMS) -> RiskTier:
    """风险档。保证金率和爆仓距离取更危险的那个。"""
    tier = RiskTier.GREEN
    if margin_ratio is not None:
        if margin_ratio < params.mr_black:
            tier = RiskTier.BLACK
        elif margin_ratio < params.mr_red:
            tier = RiskTier.RED
        elif margin_ratio < params.mr_orange:
            tier = RiskTier.ORANGE
        elif margin_ratio < params.mr_yellow:
            tier = RiskTier.YELLOW
    if liq_distance is not None:
        by_liq = (RiskTier.BLACK if liq_distance < 0.05
                  else RiskTier.RED if liq_distance < 0.10
                  else RiskTier.ORANGE if liq_distance < 0.15
                  else RiskTier.YELLOW if liq_distance < 0.25
                  else RiskTier.GREEN)
        tier = max(tier, by_liq)
    return tier


def choose_leverage(notional: float, equity: float,
                    tiers: Sequence[tuple[Decimal, Decimal]],
                    params: SafetyParams = DEFAULT_PARAMS) -> Decimal:
    """选杠杆。

    原版取档位允许的**最高**杠杆，理由是"全仓下杠杆倍数不影响爆仓价"。
    这个假设在两种情况下不成立：
      1. 账户里混有单边仓位（不是纯中性）——最高杠杆就是在放大整体风险
      2. 逐仓模式下杠杆直接决定爆仓价

    所以默认取"够用的最低档 × 安全系数"，并避开档位边界——
    贴着边界开仓，价格一动就会跨档触发降杠杆和追加保证金。
    """
    if not tiers or equity <= 0:
        return Decimal("1")
    needed = Decimal(str(max(1.0, notional / equity * params.leverage_safety_k)))
    for cap_notional, max_lev in tiers:
        if Decimal(str(notional)) <= cap_notional:
            # 避开档位边界：接近上限就退到下一档去算
            if float(cap_notional) > 0 and notional > float(cap_notional) * (1 - params.tier_edge_avoid):
                continue
            lev = min(max_lev, params.leverage_hard_cap, max(needed, Decimal("1")))
            return lev.quantize(Decimal("1"))
    # 名义额超出所有档位——用最后一档的上限，调用方应当拒绝开这么大
    return min(tiers[-1][1], params.leverage_hard_cap)


# ---- 熔断 ---------------------------------------------------------------

class BreakerLevel(enum.IntEnum):
    CLEAR = 0
    SOFT = 1        # 禁止开新仓，可自动恢复
    HARD = 2        # 停机等人工 ack


@dataclass(frozen=True, slots=True)
class BreakerSignals:
    equity_drop_15m: float = 0.0
    equity_drop_1h: float = 0.0
    unexplained_position: bool = False
    adl_or_liquidation: bool = False
    api_error_rate_5m: float = 0.0
    risk_tier: RiskTier = RiskTier.GREEN
    unwinds_30m: int = 0
    consecutive_entry_failures: int = 0
    slippage_ratio: float = 1.0
    connection_degraded: bool = False


@dataclass(frozen=True, slots=True)
class BreakerDecision:
    level: BreakerLevel
    reason: str = ""

    @property
    def blocks_opening(self) -> bool:
        return self.level >= BreakerLevel.SOFT


def evaluate_breaker(sig: BreakerSignals,
                     params: SafetyParams = DEFAULT_PARAMS) -> BreakerDecision:
    """熔断评估。

    权益骤降是总闸——它是"对冲已经失效"的最强信号，
    因为一个真正 delta 中性的仓位不该出现这种回撤。
    """
    if sig.equity_drop_15m > params.equity_drop_15m:
        return BreakerDecision(BreakerLevel.HARD,
                               f"15 分钟内权益回撤 {sig.equity_drop_15m:.1%}，"
                               "中性仓位不该这样，对冲可能已失效")
    if sig.equity_drop_1h > params.equity_drop_1h:
        return BreakerDecision(BreakerLevel.HARD,
                               f"1 小时内权益回撤 {sig.equity_drop_1h:.1%}")
    if sig.unexplained_position:
        return BreakerDecision(BreakerLevel.HARD, "对账发现无法解释的持仓")
    if sig.adl_or_liquidation:
        return BreakerDecision(BreakerLevel.HARD, "发生 ADL 或强平")
    if sig.api_error_rate_5m > params.api_error_rate_5m:
        return BreakerDecision(BreakerLevel.HARD,
                               f"5 分钟 API 错误率 {sig.api_error_rate_5m:.0%}，"
                               "看不清状态时下单是最糟选择")
    if sig.risk_tier >= RiskTier.BLACK:
        return BreakerDecision(BreakerLevel.HARD, "风险档已到 BLACK")

    if sig.unwinds_30m >= params.unwind_count_30m:
        return BreakerDecision(BreakerLevel.SOFT,
                               f"30 分钟内撤退 {sig.unwinds_30m} 次，环境不适合建仓")
    if sig.consecutive_entry_failures >= params.consecutive_entry_fail:
        return BreakerDecision(BreakerLevel.SOFT, "连续建仓失败")
    if sig.slippage_ratio > params.slippage_alert_mult:
        return BreakerDecision(BreakerLevel.SOFT,
                               f"实际滑点是预期的 {sig.slippage_ratio:.1f} 倍")
    if sig.connection_degraded:
        return BreakerDecision(BreakerLevel.SOFT, "连接降级")
    if sig.risk_tier >= RiskTier.YELLOW:
        return BreakerDecision(BreakerLevel.SOFT, f"风险档 {sig.risk_tier.name}")
    return BreakerDecision(BreakerLevel.CLEAR)


def survival_actions_allowed(level: BreakerLevel) -> frozenset[str]:
    """熔断后仍然允许的动作白名单。

    公理 3：熔断停的是赚钱动作，不停保命动作。
    一刀切的全局停机在有裸腿时等于自杀。
    """
    if level is BreakerLevel.CLEAR:
        return frozenset({"open", "close", "unwind", "delever", "transfer_margin"})
    # 软硬熔断都禁止开新仓，但平仓、撤退、降杠杆、划保证金必须畅通
    return frozenset({"close", "unwind", "delever", "transfer_margin"})
