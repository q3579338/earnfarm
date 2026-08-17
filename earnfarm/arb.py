"""双腿溢价套利计算器（纯函数层）。

存在的理由很具体：想做的是「多本地股腿 + 空 ADR 腿」的**价差**，实际做成了
单边裸空，亏的是板块方向的钱而不是价差的钱。这个模块把「照着感觉做」换成
「照着数字做」——四块：对冲比例、资金费净额、盈亏情景、成本回本线。

三条口径贯穿全文，混掉任何一条后面的数字就全错：

1. **等名义额对冲，不是等股数。** 溢价是个比值；两腿各放 N USDT 时板块同涨
   同跌完全抵消（推导见 pnl_fraction），盈亏只剩价差。等股数对冲会让 ADR 腿
   多出 (1+溢价) 倍名义额——那多出来的部分就是裸单，正是这次亏钱的形状。
2. **一切百分比都以「单腿名义额 N」为分母**，不是两腿合计 2N。盈亏 %、资金费
   年化、手续费占比共用这一个分母，跨块相加减才合法。
3. **资金费用交易所原始约定**：正费率 = 多头付给空头。与 models.CarryRate、
   scoring.funding_income 同一套符号，不在这一层翻译第二遍。

这层不 import nicegui——和 premium.py 同一条纪律：算术要能脱离界面被测。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .premium import PremiumSnapshot

# ---- 方向 ---------------------------------------------------------------
# 用"做空/做多**溢价**"命名而不是"做空 SKHY"：后者正是这次搞混的地方——
# 单腿的多空是方向敞口，溢价的多空才是价差敞口。
SHORT_PREMIUM = "short_premium"     # 空 ADR + 多本地股：赌溢价收窄
LONG_PREMIUM = "long_premium"       # 多 ADR + 空本地股：赌溢价走阔

SIDE_LABELS: dict[str, str] = {
    SHORT_PREMIUM: "做空溢价（空 ADR + 多本地股）",
    LONG_PREMIUM: "做多溢价（多 ADR + 空本地股）",
}

DEFAULT_NOTIONAL = 10_000.0

# 币安 USDⓈ-M 挂牌 taker 费率。不读账户 VIP 档：这一页全程免凭据，
# 拿挂牌价算是**保守**的方向（真实档位只会更低），不会诱导用户高估收益。
DEFAULT_TAKER_FEE = 0.0005
# 开平一轮 = 两条腿 × 开与平两边 = 4 笔吃单
ROUNDTRIP_FILLS = 4

# 两腿的结算周期。币安对这两个标的的现行设置：ADR 腿 8h、本地股腿 4h。
# 写成常量而不是散在调用点，是因为周期错一倍资金费日净就错一倍。
ADR_FUNDING_INTERVAL_H = 8.0
LOCAL_FUNDING_INTERVAL_H = 4.0
# 反推周期时允许的候选（小时）。币安现行只有这几档
_INTERVAL_CANDIDATES = (1.0, 2.0, 4.0, 8.0)

HOURS_PER_DAY = 24.0
DAYS_PER_YEAR = 365.0

# 情景表默认档位（pp = 百分点）
DEFAULT_OFFSETS_PP = (2.0, 5.0, 10.0)
# 溢价再低也不可能触到 -100%（那意味着 ADR 价格归零），夹一道防对数发散
_MIN_PREMIUM_PCT = -99.0
# 标"当前"的容差，与 premium_page 溢价梯子的 0.15 一致
_MARK_TOL_PP = 0.15


def _adr_is_short(side: str) -> bool:
    """做空溢价 = 空 ADR。未知方向直接抛——静默兜底会让两腿方向全反。"""
    if side == SHORT_PREMIUM:
        return True
    if side == LONG_PREMIUM:
        return False
    raise ValueError(f"未知方向：{side!r}")


# ---- 1) 对冲比例 --------------------------------------------------------

@dataclass(frozen=True)
class LegPlan:
    """一条腿要下的单。qty 是合约张数（这两个标的 1 张 = 1 单位标的）。"""
    symbol: str
    label: str
    is_short: bool
    price: float
    qty: float
    notional: float

    @property
    def action(self) -> str:
        return "卖出开空" if self.is_short else "买入开多"


@dataclass(frozen=True)
class HedgePlan:
    side: str
    notional_per_leg: float
    adr: LegPlan
    local: LegPlan
    premium_pct: float
    ratio: float
    # 若改按「等股数」对冲，ADR 腿要下的张数。摆出来是为了让那条歧路可见：
    # 它和 adr.qty 差的正好是 (1+溢价) 倍，差出来的全是方向敞口
    share_equiv_adr_qty: float

    @property
    def share_equiv_extra_pct(self) -> float:
        """等股数比等名义额多出的 ADR 名义额占比（%）。恒等于溢价。"""
        if self.adr.qty <= 0:
            return 0.0
        return (self.share_equiv_adr_qty / self.adr.qty - 1.0) * 100.0


def hedge_plan(*, side: str, adr_symbol: str, local_symbol: str,
               adr_label: str, local_label: str,
               adr_price: float, local_price: float, ratio: float,
               notional_per_leg: float) -> HedgePlan:
    """等名义额对冲：两腿各放 notional_per_leg USDT，张数由各自现价定。

    为什么不是等股数（ADR 张数 = ratio × 本地张数）：
    溢价 P 的定义是 ADR / (本地/ratio) − 1，所以 ratio × 本地价 = ADR 价 × (1+P)。
    等股数意味着 ADR 腿的名义额是本地腿的 (1+P) 倍——SKHY 上 P≈40%，
    等于凭空多挂了四成的单边空头。板块整体跌 1%，那四成就直接变成盈亏。
    价差交易要的是**两腿名义额相等**，ratio 只用来解释"一张对一张差在哪"。
    """
    if notional_per_leg <= 0 or adr_price <= 0 or local_price <= 0 or ratio <= 0:
        raise ValueError("名义额、两腿价格与 ratio 必须为正")
    adr_short = _adr_is_short(side)
    adr_qty = notional_per_leg / adr_price
    local_qty = notional_per_leg / local_price
    fair = local_price / ratio
    return HedgePlan(
        side=side,
        notional_per_leg=notional_per_leg,
        adr=LegPlan(symbol=adr_symbol, label=adr_label, is_short=adr_short,
                    price=adr_price, qty=adr_qty,
                    notional=adr_qty * adr_price),
        local=LegPlan(symbol=local_symbol, label=local_label,
                      is_short=not adr_short, price=local_price, qty=local_qty,
                      notional=local_qty * local_price),
        premium_pct=(adr_price / fair - 1.0) * 100.0,
        ratio=ratio,
        share_equiv_adr_qty=local_qty * ratio,
    )


# ---- 2) 资金费净额 ------------------------------------------------------

def infer_funding_interval_h(next_funding_ts: float | None,
                             default_h: float = ADR_FUNDING_INTERVAL_H) -> float:
    """从下次结算时刻反推结算周期，推不出就用默认值。

    只能推出**上界**，不能推出确值：结算时刻永远落在 UTC 整点边界上，
    4h 周期的边界是 00/04/08/12/16/20，8h 是 00/08/16。看到 04:00 就能断定
    绝不是 8h 周期；但看到 08:00 时 8h/4h/1h 都可能，这时候老实用默认值，
    别拿一个"看起来推出来了"的错数字去乘 24。
    """
    if next_funding_ts is None:
        return default_h
    total_min = int(next_funding_ts // 60) % (24 * 60)
    if total_min % 60:                      # 不在整点上：这套推断不适用
        return default_h
    hour = total_min // 60
    cap = max(h for h in _INTERVAL_CANDIDATES if hour % h == 0)
    return default_h if cap >= default_h else cap


@dataclass(frozen=True)
class FundingLeg:
    symbol: str
    is_short: bool
    rate: float | None              # 上一期费率，None = 没拉到
    interval_h: float
    notional: float
    per_settle_usdt: float          # 每次结算的现金流，正 = 我收到
    settles_per_day: float
    per_day_usdt: float


@dataclass(frozen=True)
class FundingPlan:
    adr: FundingLeg
    local: FundingLeg
    per_day_usdt: float             # 两腿合计，正 = 每天净收
    apr: float                      # 折年化，占**单腿**名义额的比例（0.12 = 12%）
    complete: bool                  # 两腿费率都拿到了才算数

    @property
    def legs(self) -> tuple[FundingLeg, FundingLeg]:
        return (self.adr, self.local)


def leg_funding(*, symbol: str, is_short: bool, rate: float | None,
                interval_h: float, notional: float) -> FundingLeg:
    """一条腿的资金费现金流（有符号，正 = 我收到）。

    符号推导（这里极易搞反，四种组合逐一验算）：

      交易所原始约定：费率 f > 0 表示**多头付给空头**。
      站在「我」的角度，一条腿每次结算的现金流是

          cash = pos · f · notional，   pos = +1（我是空头） / −1（我是多头）

      四种组合逐一验：
        我空 ADR（pos=+1），f = +0.01% → cash = +0.01%·N，我**收**。
            正费率多头付空头，我正是空头，收钱。
        我空 ADR（pos=+1），f = −0.01% → cash = −0.01%·N，我**付**。
            负费率把方向整个翻过来：空头付给多头。用户点名的就是这一种。
        我多 ADR（pos=−1），f = +0.01% → cash = −0.01%·N，我**付**。
        我多 ADR（pos=−1），f = −0.01% → cash = +0.01%·N，我**收**。

      注意 pos 跟的是**我在这条腿上的方向**，不是"做空溢价"这个整体方向：
      做空溢价时 ADR 腿 pos=+1、本地腿 pos=−1，两腿的符号本来就相反。
      把整体方向套到两条腿上是这段最常见的错法。

      归一到"每天"再相加，不能把两个费率直接相加：4h 腿一天结算 6 次、
      8h 腿只有 3 次，同样的费率日净差一倍。
    """
    if interval_h <= 0:
        raise ValueError("结算周期必须为正")
    settles_per_day = HOURS_PER_DAY / interval_h
    pos = 1.0 if is_short else -1.0
    per_settle = 0.0 if rate is None else pos * rate * notional
    return FundingLeg(
        symbol=symbol, is_short=is_short, rate=rate, interval_h=interval_h,
        notional=notional, per_settle_usdt=per_settle,
        settles_per_day=settles_per_day,
        per_day_usdt=per_settle * settles_per_day,
    )


def funding_plan(*, side: str, adr_symbol: str, local_symbol: str,
                 adr_rate: float | None, local_rate: float | None,
                 notional_per_leg: float,
                 adr_interval_h: float = ADR_FUNDING_INTERVAL_H,
                 local_interval_h: float = LOCAL_FUNDING_INTERVAL_H) -> FundingPlan:
    """两腿资金费的日净额与年化。

    年化分母取**单腿名义额**，和盈亏情景表的 % 同一个分母——两块数字要能
    直接相加减（"这波价差赚 1.2%，持有期间资金费又赔 0.3%"），分母不统一
    这个加法就是错的。
    """
    adr_short = _adr_is_short(side)
    adr = leg_funding(symbol=adr_symbol, is_short=adr_short, rate=adr_rate,
                      interval_h=adr_interval_h, notional=notional_per_leg)
    local = leg_funding(symbol=local_symbol, is_short=not adr_short,
                        rate=local_rate, interval_h=local_interval_h,
                        notional=notional_per_leg)
    per_day = adr.per_day_usdt + local.per_day_usdt
    apr = per_day / notional_per_leg * DAYS_PER_YEAR if notional_per_leg > 0 else 0.0
    return FundingPlan(adr=adr, local=local, per_day_usdt=per_day, apr=apr,
                       complete=adr_rate is not None and local_rate is not None)


# ---- 3) 盈亏情景 --------------------------------------------------------

def pnl_fraction(side: str, entry_premium_pct: float,
                 exit_premium_pct: float) -> float:
    """溢价从 P0 走到 P1 时的盈亏，占**单腿名义额**的比例。

    推导（这条式子是整页的地基）：
      两腿各放名义额 N。记 ADR 腿收益率 rA、本地腿收益率 rL。
      做空溢价 = 空 ADR + 多本地，盈亏 = N·(−rA + rL) = −N·(rA − rL)。
      取对数口径 rA = Δln A、rL = Δln L，而按溢价定义
          ln(1+P) = ln A − ln L + ln ratio，   ratio 是常数
      两边取变化量 ⇒ Δln(1+P) = Δln A − Δln L = rA − rL。
      所以
          做空溢价： pnl_frac = −[ln(1+P1) − ln(1+P0)]
          做多溢价： pnl_frac = +[ln(1+P1) − ln(1+P0)]

    两条推论，正是这个模块要讲的话：
      · rA = rL（板块同涨同跌）⇒ Δln(1+P) = 0 ⇒ 盈亏恒为 0。
        对冲之后方向不再影响盈亏，亏损只可能来自价差反向——
        这就是套利与裸单的分界线。
      · 用对数不是为了好看：它让"涨 10% 再跌 10%"这类往返路径不产生虚假
        盈亏，且两腿的对数收益可以直接相减。
    """
    sign = -1.0 if _adr_is_short(side) else 1.0
    p0 = max(_MIN_PREMIUM_PCT, entry_premium_pct) / 100.0
    p1 = max(_MIN_PREMIUM_PCT, exit_premium_pct) / 100.0
    # 末尾 +0.0 是把 IEEE754 的负零抹平：做空溢价时 sign=−1，打平的那一行会
    # 算出 −0.0，界面上显示成 "-0.00" 看着像微亏。对非零值是精确的空操作
    return sign * (math.log1p(p1) - math.log1p(p0)) + 0.0


@dataclass(frozen=True)
class ScenarioRow:
    label: str
    premium_pct: float
    pnl_usdt: float
    pnl_pct: float                  # 占单腿名义额
    is_current: bool
    is_entry: bool


def scenario_table(*, side: str, entry_premium_pct: float,
                   current_premium_pct: float, notional_per_leg: float,
                   ref_lo: float | None = None, ref_hi: float | None = None,
                   ref_mean: float | None = None,
                   offsets_pp: tuple[float, ...] = DEFAULT_OFFSETS_PP
                   ) -> list[ScenarioRow]:
    """溢价走到各水平时的盈亏表，按溢价从高到低排（和溢价梯子同序）。

    档位来自三处：进场价 ± 给定 pp、历史区间高/低/均值、当前溢价。
    历史三档必须在场——±10pp 这种等距档位看着漂亮，但如果整段落在历史区间
    之外，那几行就是纯想象；把历史高低摆在同一张表里，用户才看得出哪一行
    是"真发生过的"。
    """
    if notional_per_leg <= 0:
        raise ValueError("名义额必须为正")
    _adr_is_short(side)             # 早失败：方向拼错不该走到算盈亏

    # 顺序即优先级：同一档位被多个来源命中时，标签按这个顺序拼
    raw: list[tuple[float, str]] = [(entry_premium_pct, "进场"),
                                    (current_premium_pct, "当前")]
    for d in offsets_pp:
        raw.append((entry_premium_pct + d, f"进场 +{d:g}pp"))
        raw.append((entry_premium_pct - d, f"进场 -{d:g}pp"))
    for value, name in ((ref_lo, "历史低"), (ref_mean, "历史均值"),
                        (ref_hi, "历史高")):
        if value is not None:
            raw.append((value, name))

    merged: dict[float, tuple[float, list[str]]] = {}
    for level, name in raw:
        level = max(_MIN_PREMIUM_PCT, level)
        key = round(level, 2)
        if key in merged:
            if name not in merged[key][1]:
                merged[key][1].append(name)
        else:
            merged[key] = (level, [name])

    rows = []
    for level, names in merged.values():
        frac = pnl_fraction(side, entry_premium_pct, level)
        rows.append(ScenarioRow(
            label="·".join(names),
            premium_pct=level,
            pnl_usdt=frac * notional_per_leg,
            pnl_pct=frac * 100.0,
            is_current=abs(level - current_premium_pct) < _MARK_TOL_PP,
            is_entry=abs(level - entry_premium_pct) < _MARK_TOL_PP,
        ))
    rows.sort(key=lambda r: r.premium_pct, reverse=True)
    return rows


# ---- 4) 成本与回本 ------------------------------------------------------

@dataclass(frozen=True)
class CostPlan:
    taker_fee: float
    fills: int                      # 开平一轮的吃单笔数
    fee_frac: float                 # 占单腿名义额的比例
    fee_usdt: float
    breakeven_premium_pct: float    # 溢价走到这里刚好打平
    breakeven_pp: float             # 相对进场要走的百分点，有符号
    funding_days_to_cover: float | None   # 靠资金费净收覆盖手续费要几天


def cost_plan(*, side: str, entry_premium_pct: float, notional_per_leg: float,
              taker_fee: float = DEFAULT_TAKER_FEE,
              funding_per_day_usdt: float | None = None) -> CostPlan:
    """双腿开平的 taker 成本，以及"溢价要走多少 pp 才回本"。

    成本口径：两条腿 × 开与平两边 = 4 笔吃单，每笔按各自名义额收 taker，
    合计 4 × 0.05% = 名义额的 0.2%。**不含滑点**——盘口薄的时候滑点能比
    手续费还大，这里给的是成本的下界，不是全部。

    回本线由 pnl_fraction 反解：令 |pnl_frac| = fee_frac，
        做空溢价（溢价跌才赚）： 1+P1 = (1+P0)·e^(−c)
        做多溢价（溢价涨才赚）： 1+P1 = (1+P0)·e^(+c)
    注意它不等于 fee_frac 直接换算的 0.2pp：溢价是比值，(1+P0) 这个系数
    会把它放大——P0=40% 时回本要走约 0.28pp，不是 0.20pp。
    """
    if notional_per_leg <= 0:
        raise ValueError("名义额必须为正")
    fee_frac = taker_fee * ROUNDTRIP_FILLS
    fee_usdt = fee_frac * notional_per_leg
    p0 = max(_MIN_PREMIUM_PCT, entry_premium_pct) / 100.0
    direction = -1.0 if _adr_is_short(side) else 1.0
    breakeven = ((1.0 + p0) * math.exp(direction * fee_frac) - 1.0) * 100.0

    days = None
    if funding_per_day_usdt is not None and funding_per_day_usdt > 0:
        days = fee_usdt / funding_per_day_usdt
    return CostPlan(
        taker_fee=taker_fee, fills=ROUNDTRIP_FILLS, fee_frac=fee_frac,
        fee_usdt=fee_usdt, breakeven_premium_pct=breakeven,
        breakeven_pp=breakeven - entry_premium_pct,
        funding_days_to_cover=days,
    )


# ---- 装配 ---------------------------------------------------------------

@dataclass(frozen=True)
class ArbCalc:
    """四块算完的整包。界面只负责把这里的字段摆出来，不做任何算术。"""
    side: str
    notional_per_leg: float
    entry_premium_pct: float
    current_premium_pct: float
    hedge: HedgePlan
    funding: FundingPlan
    scenarios: tuple[ScenarioRow, ...]
    cost: CostPlan

    @property
    def side_label(self) -> str:
        return SIDE_LABELS[self.side]


def build_from_snapshot(snapshot: PremiumSnapshot, *, side: str,
                        notional_per_leg: float = DEFAULT_NOTIONAL,
                        entry_premium_pct: float | None = None,
                        taker_fee: float = DEFAULT_TAKER_FEE) -> ArbCalc:
    """从溢价快照直接算齐四块。

    entry_premium_pct 默认取当前溢价；用户改成别的值就是在问"假如我在 X%
    进场会怎样"——那正是这个计算器最该回答的问题，所以它是可改的输入
    而不是只读的现状。

    结算周期优先从 nextFundingTime 反推，推不出来落回常量：币安改过一次
    某些标的的周期，写死会静默错一倍，而这里错一倍就是资金费全错。
    """
    pair = snapshot.pair
    entry = snapshot.premium_pct if entry_premium_pct is None else entry_premium_pct
    ref = snapshot.ref
    hedge = hedge_plan(
        side=side, adr_symbol=pair.adr_symbol, local_symbol=pair.local_symbol,
        adr_label=pair.adr_label, local_label=pair.local_label,
        adr_price=snapshot.adr.price, local_price=snapshot.local.price,
        ratio=pair.ratio, notional_per_leg=notional_per_leg)
    funding = funding_plan(
        side=side, adr_symbol=pair.adr_symbol, local_symbol=pair.local_symbol,
        adr_rate=snapshot.adr.funding_rate, local_rate=snapshot.local.funding_rate,
        notional_per_leg=notional_per_leg,
        adr_interval_h=infer_funding_interval_h(snapshot.adr.next_funding_ts,
                                                ADR_FUNDING_INTERVAL_H),
        local_interval_h=infer_funding_interval_h(snapshot.local.next_funding_ts,
                                                  LOCAL_FUNDING_INTERVAL_H))
    scenarios = scenario_table(
        side=side, entry_premium_pct=entry,
        current_premium_pct=snapshot.premium_pct,
        notional_per_leg=notional_per_leg,
        ref_lo=ref.lo if ref else None, ref_hi=ref.hi if ref else None,
        ref_mean=ref.mean if ref else None)
    cost = cost_plan(side=side, entry_premium_pct=entry,
                     notional_per_leg=notional_per_leg, taker_fee=taker_fee,
                     funding_per_day_usdt=funding.per_day_usdt
                     if funding.complete else None)
    return ArbCalc(side=side, notional_per_leg=notional_per_leg,
                   entry_premium_pct=entry,
                   current_premium_pct=snapshot.premium_pct,
                   hedge=hedge, funding=funding,
                   scenarios=tuple(scenarios), cost=cost)
