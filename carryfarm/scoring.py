"""机会评分。

核心立场：套利是费用生意，排序不是"打分"而是"估值"——把每个机会折算成
「在你的仓位、你的持有期下，扣完所有摩擦后的风险调整净年化」，再按这个实数排。

对照原版工具(qqq.bot)的三宗罪，这里逐条修掉：
  1. 不归一化结算周期  → 一律化成小时化 carry 再比较
  2. 默认当前费率永续  → AR(1) 向历史中位数衰减
  3. 不扣手续费和滑点  → 四笔执行成本 + 冲击成本显式建模

精度约定：价格/数量走 Decimal（下单精度要求），统计与评分走 float
（本来就是启发式排序，Decimal 在这里只会拖慢速度且无收益）。
"""

from __future__ import annotations

import math
from bisect import insort
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

HOURS_PER_YEAR = 8760.0

# AR(1) 自相关的截断上限。太接近 1 等于假设费率永不衰减，退回原版的错误
RHO_CAP = 0.98
# 样本不足时向此值收缩——经验上跨所费差的小时自相关在 0.7 附近
RHO_PRIOR = 0.70
# 滑点保守系数：含撮合延迟和两腿异步的额外损耗
SLIPPAGE_ETA = 1.5
# 平仓紧急度：平仓往往比开仓急，滑点更大。只乘滑点不乘手续费（常见实现 bug）
CLOSE_URGENCY = 1.2
# 深度参与率上限：单次吃掉超过可见深度这个比例，冲击成本就不可控了。
# 超过不等于不能做——是该把仓位降到这个线以下。
MAX_PARTICIPATION = 0.20
# 最小可行仓位。低于这个数，四笔手续费的绝对额会把收益吃光，
# 而且很多合约的 minNotional 也过不去。
MIN_VIABLE_NOTIONAL = 500.0


@dataclass(frozen=True, slots=True)
class LegQuote:
    """评分所需的单腿快照。"""

    symbol: str
    market: str
    bid: float
    ask: float
    bid_size: float          # 一档量（base 数量）
    ask_size: float
    depth_notional: float    # depth_band_bp 带宽内的可见深度（名义）
    depth_band_bp: float     # 深度统计带宽（基点）
    taker_fee: float
    maker_fee: float

    # 下期预测费率。用**交易所原始约定**：正 = 多头付给空头。
    # 六家 API 返回的都是这个符号，适配器不必取反，少一处出错的地方。
    funding_now: float
    funding_interval_h: float
    next_funding_ts: float   # unix 秒
    funding_cap: float | None = None
    funding_floor: float | None = None

    open_interest_usd: float = 0.0
    volume_24h_usd: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def half_spread(self) -> float:
        m = self.mid
        return (self.ask - self.bid) / (2 * m) if m > 0 else 0.0

    @property
    def hourly_rate(self) -> float:
        """小时化费率。1h 的 1bp 和 8h 的 1bp 完全不是一回事，
        比较前必须过这一步——这是原版排序最直接的硬伤。"""
        return self.funding_now / self.funding_interval_h if self.funding_interval_h > 0 else 0.0


@dataclass(frozen=True, slots=True)
class HistoryStats:
    """一条腿的历史资金费统计（已按当前定向取好符号）。"""

    rates: tuple[float, ...]
    interval_h: float

    def median(self) -> float:
        """用中位数不用均值——抗尖峰。一次极端费率不该主导长期预期。"""
        if not self.rates:
            return 0.0
        s = sorted(self.rates)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    def autocorr(self) -> float:
        """一阶自相关，用于 AR(1) 衰减路径。样本少时向先验收缩。"""
        n = len(self.rates)
        if n < 8:
            return RHO_PRIOR
        mu = sum(self.rates) / n
        num = sum((self.rates[i] - mu) * (self.rates[i + 1] - mu) for i in range(n - 1))
        den = sum((r - mu) ** 2 for r in self.rates)
        if den <= 0:
            return RHO_PRIOR
        rho = num / den
        # 样本少时向先验收缩，避免小样本噪声被当成信号
        weight = min(1.0, n / 60)
        rho = weight * rho + (1 - weight) * RHO_PRIOR
        return max(0.0, min(RHO_CAP, rho))


# ---- 结算时点 -----------------------------------------------------------

def settlement_offsets(now_ts: float, next_ts: float, interval_h: float,
                       horizon_h: float) -> list[float]:
    """持有期内每次结算距今多少小时。

    这是"看着 3bp 很香、实际先亏 20bp"的机制化表达：
    如果持有期比距下次结算的时间还短，一分钱资金费都收不到，
    但四笔手续费一分不少。
    """
    if interval_h <= 0:
        return []
    step = interval_h * 3600.0
    t = next_ts
    if t < now_ts:
        # 数据陈旧，推进到未来第一个结算点
        t += math.ceil((now_ts - t) / step) * step
    end = now_ts + horizon_h * 3600.0
    out: list[float] = []
    while t <= end + 1e-6:
        out.append((t - now_ts) / 3600.0)
        t += step
    return out


def forecast_path(f_now: float, f_bar: float, rho: float, n: int,
                  cap: float | None, floor: float | None) -> list[float]:
    """AR(1) 衰减路径：从当前费率向历史中位数收敛。

    k=0 时恰为 f_now（交易所给的下期预测，近似已知）。
    同时施加交易所的费率上下限——命中上限的极端费率不可外推。
    """
    out = []
    for k in range(n):
        v = f_bar + (f_now - f_bar) * (rho ** k)
        if cap is not None:
            v = min(v, cap)
        if floor is not None:
            v = max(v, floor)
        out.append(v)
    return out


def funding_income(long: LegQuote, short: LegQuote,
                   long_hist: HistoryStats, short_hist: HistoryStats,
                   horizon_h: float, now_ts: float) -> tuple[float, int, int]:
    """持有期内的资金费净收入（占名义比例）。返回 (收入, 空腿结算次数, 多腿结算次数)。

    交易所原始约定下 f>0 表示多头付给空头，所以：
    空腿每次结算收 +f_S，多腿每次结算付 -f_L，净收入 = Σf_S − Σf_L。
    两腿各按自己的结算周期计数——周期不同是常态，不能合并计算。
    """
    s_offs = settlement_offsets(now_ts, short.next_funding_ts, short.funding_interval_h, horizon_h)
    l_offs = settlement_offsets(now_ts, long.next_funding_ts, long.funding_interval_h, horizon_h)

    s_path = forecast_path(short.funding_now, short_hist.median(), short_hist.autocorr(),
                           len(s_offs), short.funding_cap, short.funding_floor)
    l_path = forecast_path(long.funding_now, long_hist.median(), long_hist.autocorr(),
                           len(l_offs), long.funding_cap, long.funding_floor)
    return sum(s_path) - sum(l_path), len(s_offs), len(l_offs)


# ---- 成本 ---------------------------------------------------------------

def slippage_rate(leg: LegQuote, side: str, notional: float,
                  eta: float = SLIPPAGE_ETA) -> float:
    """taker 成本 = 半价差（任何大小都要付）+ 穿透冲击（超出一档的部分才付）。"""
    m = leg.mid
    if m <= 0:
        return 0.0
    s = leg.half_spread
    q1 = (leg.ask_size * leg.ask) if side == "buy" else (leg.bid_size * leg.bid)
    if notional <= q1 or leg.depth_notional <= 0 or leg.depth_band_bp <= 0:
        return eta * s
    # 均匀簿密度：带宽内可见深度 / 相对价格带宽
    rho = leg.depth_notional / (leg.depth_band_bp / 1e4)
    excess = notional - q1
    return eta * (s + (excess / notional) * (excess / (2 * rho)))


def roundtrip_cost(long: LegQuote, short: LegQuote, notional: float,
                   transfer_amort: float = 0.0) -> float:
    """开+平共四笔的总成本（占名义比例）。

    紧急度系数只乘滑点、不乘手续费——手续费是固定费率，不会因为你急而变贵。
    """
    open_cost = (long.taker_fee + slippage_rate(long, "buy", notional)
                 + short.taker_fee + slippage_rate(short, "sell", notional))
    close_cost = (long.taker_fee + CLOSE_URGENCY * slippage_rate(long, "sell", notional)
                  + short.taker_fee + CLOSE_URGENCY * slippage_rate(short, "buy", notional))
    return open_cost + close_cost + transfer_amort


def breakeven_hours(long: LegQuote, short: LegQuote,
                    long_hist: HistoryStats, short_hist: HistoryStats,
                    cost_rt: float, now_ts: float, max_h: float = 24 * 30) -> float:
    """要熬多少小时才能覆盖开平仓成本。

    两腿周期不同 + 费率衰减 ⇒ 累计收益是分段阶跃且非线性的，
    闭式解只在同周期无衰减时成立，所以走事件驱动扫描。
    """
    events: list[tuple[float, float]] = []
    # 空腿收钱记正，多腿付钱记负
    for leg, hist, sign in ((short, short_hist, 1.0), (long, long_hist, -1.0)):
        offs = settlement_offsets(now_ts, leg.next_funding_ts, leg.funding_interval_h, max_h)
        base = sign * leg.funding_now
        bar = sign * hist.median()
        path = forecast_path(base, bar, hist.autocorr(), len(offs),
                             leg.funding_cap, leg.funding_floor)
        for h, f in zip(offs, path):
            insort(events, (h, f))
    cum = 0.0
    for h, amount in events:
        cum += amount
        if cum >= cost_rt:
            return h
    return math.inf


# ---- 稳定性 -------------------------------------------------------------

def _median(xs: Sequence[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _median_run_length(xs: Sequence[float], target_sign: float) -> float:
    """与当前定向同号的连续段长度中位数。

    这个数回答的是"这个费差通常能活多久"——如果历史上中位只活 12 小时，
    而回本要 56 小时，那么无论毛年化多漂亮都该沉底。
    """
    runs: list[int] = []
    current = 0
    for x in xs:
        if (x > 0) == (target_sign > 0) and x != 0:
            current += 1
        else:
            if current:
                runs.append(current)
            current = 0
    if current:
        runs.append(current)
    return _median(runs) if runs else 0.0


@dataclass(frozen=True, slots=True)
class StabilityResult:
    score: float             # 0~1 综合稳定性
    p_win: float             # 收缩后的胜率
    median_run_h: float      # 同号中位存续小时
    n_effective: float
    apr_median: float
    apr_q25: float           # 下行情形
    is_prior: bool           # True = 样本不足，用的先验值
    # 喂进来的小时 carry 点数。n_effective 是"独立窗口数"（除过持有期），
    # 界面要显示的"我到底看了多久历史"只有这个数答得上来：
    # 0 = 一条历史都没有，48 = 只有两天，两者都会 is_prior，但含义完全不同。
    n_samples: int = 0


def stability(hourly_carry: Sequence[float], horizon_h: float, cost_rt: float,
              kappa: float) -> StabilityResult:
    """滚动窗口"费后"回测——比矩统计诚实得多。

    综合用**加权几何均值**而非加权和：任一分量趋近 0 应当直接否决整个机会。
    符号乱翻的费差，再高的均值也没用；加权和会让 80 分的均值把 5 分的翻转率
    救回来，那正是要修的病。
    """
    W = max(1, int(round(horizon_h)))
    n = len(hourly_carry)
    if n < W + 12:
        # 新合约/数据不足：给一个偏保守的先验，不参与高位排序。
        # n_samples 照实带出去——界面要能区分"一条历史都没有"和"只攒了两天"
        return StabilityResult(0.35, 0.4, 0.0, 0.0, 0.0, 0.0, True, n_samples=n)

    # 滚动窗口累计收益（前缀和实现，O(n)）
    prefix = [0.0]
    for x in hourly_carry:
        prefix.append(prefix[-1] + x)
    nets = [prefix[i + W] - prefix[i] - cost_rt for i in range(n - W + 1)]

    n_eff = max(1.0, n / W)          # 窗口重叠，有效样本不是窗口数
    wins = sum(1 for v in nets if v > 0) / len(nets)
    # Beta(2,3) 先验收缩，先验均值 0.4 偏保守
    p_hat = (wins * n_eff + 2) / (n_eff + 5)

    mu = sum(hourly_carry) / n
    sign = 1.0 if mu >= 0 else -1.0
    same = sum(1 for x in hourly_carry if (x > 0) == (mu > 0)) / n
    consistency = max(0.02, min(1.0, 2 * same - 1))      # 抛硬币(50%) = 0

    med = _median(hourly_carry)
    mad = _median([abs(x - med) for x in hourly_carry]) + 1e-12
    amplitude = abs(mu) / (abs(mu) + mad)                 # 用 MAD 不用 σ，抗尖峰

    tail = hourly_carry[-max(1, n // 4):]
    tail_mu = sum(tail) / len(tail)
    decay = max(0.0, min(1.0, tail_mu / mu)) if mu > 0 else 0.02   # 只罚不奖

    run_h = _median_run_length(hourly_carry, sign)
    persistence = max(0.0, min(1.0, run_h / max(horizon_h, 1e-9)))

    score = (p_hat ** 0.35 * consistency ** 0.20 * max(persistence, 1e-6) ** 0.25
             * max(decay, 1e-6) ** 0.10 * amplitude ** 0.10)

    scale = HOURS_PER_YEAR / horizon_h / kappa
    s_nets = sorted(nets)
    q25 = s_nets[max(0, int(len(s_nets) * 0.25) - 1)]
    return StabilityResult(
        score=score, p_win=p_hat, median_run_h=run_h, n_effective=n_eff,
        apr_median=_median(nets) * scale, apr_q25=q25 * scale, is_prior=False,
        n_samples=n,
    )


# ---- 装配 ---------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ScoredOpportunity:
    base: str
    long: LegQuote
    short: LegQuote

    gross_apr: float          # 毛 carry 年化（未扣成本）
    net_apr: float            # 扣完全部成本的净年化 —— 决策就看这个
    cost_rt: float            # 往返总成本（占名义）
    breakeven_h: float        # 回本小时数
    survival_prob: float      # 活到回本的概率
    stability: StabilityResult
    capacity_usd: float       # 该机会能承载多少钱
    notional: float           # 评分所用的仓位

    # good / sized_down / marginal / negative / unexecutable
    #   sized_down = 机会成立，但你填的仓位放不下，按 capacity_usd 缩小即可做
    verdict: str
    reasons: tuple[str, ...]  # 为什么是这个判决 —— UI 上要一眼看到

    # 成本用的是谁的费率：real = 两条腿都是你的真实档位，partial = 只有一条腿是，
    # default = 两条腿都在用 VIP0 挂牌价估算。
    # 这个字段必须一路带到界面：成本占了判决的绝对主导，VIP3 用户的 taker 可能
    # 只有挂牌价的一半，拿默认档算出来的"负期望"对他可能是正的。默默用默认值
    # 假装精确，正是这个工具要修的病。
    fee_basis: str = "default"

    @property
    def is_actionable(self) -> bool:
        """能做就是能做——只是仓位要按 capacity_usd 缩。"""
        return self.verdict in ("good", "sized_down")

    @property
    def suggested_notional(self) -> float:
        """建议仓位。深度撑不住你填的数时，这里给的是能安全放进去的量。"""
        return min(self.notional, self.capacity_usd) if self.capacity_usd > 0 else 0.0


def orient(a: LegQuote, b: LegQuote) -> tuple[LegQuote, LegQuote]:
    """自动定向：在小时化费率低的一边做多、高的一边做空。

    所有历史统计必须按这个定向取符号，不能取绝对值——
    否则翻转频率这个指标完全失去意义。
    """
    return (a, b) if a.hourly_rate <= b.hourly_rate else (b, a)


def score_pair(base: str, leg_a: LegQuote, leg_b: LegQuote,
               hist_a: HistoryStats, hist_b: HistoryStats,
               *, notional: float, horizon_h: float, now_ts: float,
               kappa: float = 2.0, hourly_carry: Sequence[float] | None = None,
               min_net_apr: float = 0.05,
               fee_basis: str = "default") -> ScoredOpportunity:
    """给一个跨所配对打分。

    fee_basis 只是把"两条腿的 taker_fee 是真实档位还是默认挂牌价"这个事实
    原样带到界面，不参与任何计算——费率本身已经在 LegQuote.taker_fee 里了。
    """
    long, short = orient(leg_a, leg_b)
    long_hist, short_hist = (hist_a, hist_b) if long is leg_a else (hist_b, hist_a)

    cost_rt = roundtrip_cost(long, short, notional)
    income, n_short, n_long = funding_income(long, short, long_hist, short_hist,
                                             horizon_h, now_ts)
    net = income - cost_rt
    net_apr = net * HOURS_PER_YEAR / horizon_h / kappa

    c_hour = short.hourly_rate - long.hourly_rate
    gross_apr = c_hour * HOURS_PER_YEAR / kappa

    be_h = breakeven_hours(long, short, long_hist, short_hist, cost_rt, now_ts)
    stab = stability(hourly_carry or [], horizon_h, cost_rt, kappa)
    survival = (min(1.0, stab.median_run_h / be_h)
                if math.isfinite(be_h) and be_h > 0 and stab.median_run_h > 0 else 0.0)

    # 容量：二分搜索仍能满足最低净年化的最大仓位
    def apr_at(q: float) -> float:
        c = roundtrip_cost(long, short, q)
        return (income - c) * HOURS_PER_YEAR / horizon_h / kappa

    lo, hi = 100.0, 5_000_000.0
    if apr_at(lo) < min_net_apr:
        capacity = 0.0
    else:
        for _ in range(24):
            mid = (lo + hi) / 2
            if apr_at(mid) >= min_net_apr:
                lo = mid
            else:
                hi = mid
        capacity = lo

    reasons: list[str] = []
    verdict = "good"

    # **先判盈亏，再判深度**。反过来会说出自相矛盾的话：
    # 深度那条曾经无条件写"按这个仓位做，收益仍然成立"，而净年化在它之后才算，
    # 结果一行里同时出现"收益仍然成立"和"净年化 -15.1% 为负"。
    # 用户看到互相打架的解释，会连对的那条一起不信。
    is_profitable = net_apr > 0

    if net_apr <= 0:
        verdict = "negative"
        reasons.append(f"净年化 {net_apr:.1%} 为负：毛收益 {gross_apr:.1%} "
                       f"扛不住 {cost_rt:.2%} 的往返成本")

    # 深度不足不等于"这个机会不能做"——小币的高费率往往就是真肉，只是容量小。
    # 工具的职责是告诉你**最多能放多少**，而不是替你把机会删掉。
    # 只有当深度小到连最小可行仓位都撑不住时，才真的判不可执行。
    depth_cap = min(long.depth_notional, short.depth_notional)
    max_by_depth = MAX_PARTICIPATION * depth_cap if depth_cap > 0 else 0.0
    if depth_cap > 0 and notional > max_by_depth:
        capacity = min(capacity, max_by_depth)
        if max_by_depth < MIN_VIABLE_NOTIONAL:
            verdict = "unexecutable"
            reasons.append(f"深度只撑得住 ${max_by_depth:,.0f}，低于最小可行仓位 "
                           f"${MIN_VIABLE_NOTIONAL:,.0f}，手续费会把收益吃光")
        elif is_profitable:
            verdict = "sized_down" if verdict == "good" else verdict
            reasons.append(f"深度限制：这个机会最多放 ${max_by_depth:,.0f}"
                           f"（评分用的是 ${notional:,.0f}）。按这个仓位做，收益仍然成立")
        else:
            # 已经判负了，深度只是补充信息，不能再说"收益成立"
            reasons.append(f"另外深度也只撑得住 ${max_by_depth:,.0f}")
    elif net_apr < min_net_apr:
        verdict = "marginal" if verdict == "good" else verdict
        reasons.append(f"净年化 {net_apr:.1%} 低于门槛 {min_net_apr:.0%}")
    if n_short == 0 and n_long == 0:
        verdict = "negative"
        reasons.append(f"持有 {horizon_h:.0f} 小时内一次结算都没有，"
                       "收不到资金费但四笔手续费一分不少")
    if math.isfinite(be_h) and be_h > horizon_h:
        if verdict == "good":
            verdict = "marginal"
        reasons.append(f"回本需 {be_h:.0f} 小时，超过你的计划持有期 {horizon_h:.0f} 小时")
    if not math.isfinite(be_h):
        verdict = "negative"
        reasons.append("按衰减路径推算永不回本")
    if stab.is_prior:
        if verdict == "good":
            verdict = "marginal"
        reasons.append("历史数据不足，评分用的是保守先验，不可尽信")
    elif survival < 0.3 and math.isfinite(be_h):
        if verdict == "good":
            verdict = "marginal"
        reasons.append(f"该费差历史同号中位只活 {stab.median_run_h:.0f} 小时，"
                       f"回本要 {be_h:.0f} 小时，大概率撑不到")

    if not reasons:
        reasons.append(f"净年化 {net_apr:.1%}，{be_h:.0f} 小时回本，"
                       f"历史胜率 {stab.p_win:.0%}")

    return ScoredOpportunity(
        base=base, long=long, short=short,
        gross_apr=gross_apr, net_apr=net_apr, cost_rt=cost_rt,
        breakeven_h=be_h, survival_prob=survival, stability=stab,
        capacity_usd=capacity, notional=notional,
        verdict=verdict, reasons=tuple(reasons),
        fee_basis=fee_basis,
    )


def rank(opportunities: Sequence[ScoredOpportunity],
         stability_weight: float = 0.35) -> list[ScoredOpportunity]:
    """最终排序：净年化打上稳定性置信折扣。

    不可执行和负期望的沉底——原版会让这类机会堂而皇之地排在榜首。
    sized_down 紧跟 good：机会本身是好的，只是要按容量缩仓位，
    对散户来说往往比大盘那些 10% 年化的强得多。
    """
    order = {"good": 0, "sized_down": 1, "marginal": 2, "negative": 3, "unexecutable": 4}

    def key(o: ScoredOpportunity) -> tuple[int, float]:
        discount = (1 - stability_weight) + stability_weight * o.stability.score
        return (order[o.verdict], -(o.net_apr * discount))

    return sorted(opportunities, key=key)
