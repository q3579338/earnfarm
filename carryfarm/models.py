"""领域模型。这些类型不依赖任何交易所细节，adapter 负责把各家的原始响应翻译成它们。

约定：所有价格、数量、费率一律用 Decimal，绝不用 float。
交易所返回的是字符串，保持字符串直到构造 Decimal，中途不经过 float。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

ZERO = Decimal("0")


class Venue(str, enum.Enum):
    BINANCE = "binance"
    OKX = "okx"
    HTX = "htx"
    GATE = "gate"
    BYBIT = "bybit"
    BITGET = "bitget"
    # 加新成员前先看 docs/research/exchange-<venue>.md 是否已就位：
    # 枚举是全仓 6 处自动入列点的开关（open_public_adapters、PublicFeed、
    # 账户页下拉、watch 的 StaleMonitor……），一加立刻全线生效，
    # 而 ui/accounts.py 的 VENUE_LABELS 缺条目会让账户页直接打不开——
    # 枚举和它的各处映射必须同一个提交落地。
    HYPERLIQUID = "hyperliquid"
    KUCOIN = "kucoin"


class MarketKind(str, enum.Enum):
    """市场类型。决定了能否做空、carry 来自哪里。"""

    SPOT = "spot"
    MARGIN = "margin"          # 全仓杠杆
    PERP = "perp"              # U 本位永续
    PERP_COIN = "perp_coin"    # 币本位永续


class CarryKind(str, enum.Enum):
    """持仓期间的现金流来源。"""

    FUNDING = "funding"     # 永续资金费
    INTEREST = "interest"   # 杠杆借币利息
    ZERO = "zero"           # 现货，无 carry


class Side(str, enum.Enum):
    LONG = "long"
    SHORT = "short"


class HedgeStatus(str, enum.Enum):
    RUNNING = "running"          # 双向：可加可减
    REDUCE_ONLY = "reduce_only"  # 只减不增（风控触发后自动进入）
    DISABLED = "disabled"        # 完全停止
    RETREATING = "retreating"    # 检测到裸奔，正在撤退平仓


@dataclass(frozen=True, slots=True)
class MarketSpec:
    """某个交易所的某类市场具备什么能力。"""

    venue: Venue
    kind: MarketKind
    carry: CarryKind
    shortable: bool
    # 减仓时是否也要满足最小名义额（现货/杠杆通常要，永续通常不要）
    reduce_min_notional: bool
    # 是否有盘口深度数据可用于截断下单量
    has_book_depth: bool = True

    @property
    def key(self) -> str:
        return f"{self.venue.value}:{self.kind.value}"


@dataclass(frozen=True, slots=True)
class Instrument:
    """合约/交易对规格。下单前必须按这些精度规整，否则交易所直接拒单。"""

    market: str          # MarketSpec.key
    symbol: str          # 交易所原生符号
    base: str
    quote: str
    tick_size: Decimal   # 价格最小变动
    lot_size: Decimal    # 数量最小变动
    min_notional: Decimal
    contract_size: Decimal = Decimal("1")   # 一张合约代表多少 base，现货为 1
    max_leverage: Decimal = Decimal("1")
    # 资金费结算周期（秒）。8小时=28800，也有1小时/4小时的品种。
    funding_interval_s: int = 28800

    @property
    def pair(self) -> str:
        return f"{self.base}/{self.quote}"


@dataclass(frozen=True, slots=True)
class BookTop:
    """盘口最优价及其档位量。"""

    bid_price: Decimal
    bid_qty: Decimal
    ask_price: Decimal
    ask_qty: Decimal
    ts_ms: int

    @property
    def mid(self) -> Decimal:
        return (self.bid_price + self.ask_price) / 2

    def is_fresh(self, now_ms: int, expected_interval_ms: int, grace_ms: int) -> bool:
        return now_ms - self.ts_ms <= expected_interval_ms + grace_ms


@dataclass(frozen=True, slots=True)
class BookDepth:
    """指定基点带宽内的可见深度。

    只用一档当深度代理会把容量系统性低估一两个数量级——
    山寨币一档常常只有几百刀，但 10 个基点内往往有几万。
    容量估错会让真实可做的机会被判成"做不了"。

    名义额是 quote 计价，数量是 base 币计价（有合约面值的所必须换算后再填）。
    """

    symbol: str
    bid_notional: Decimal      # 带宽内买盘累计名义额
    ask_notional: Decimal      # 带宽内卖盘累计名义额
    bid_price: Decimal
    bid_qty: Decimal
    ask_price: Decimal
    ask_qty: Decimal
    levels_used: int           # 实际统计了几档。不足说明带宽内档位不够，别外推
    band_bp: float
    ts_ms: int

    @property
    def thinner_side(self) -> Decimal:
        """两侧较薄的一边。对冲要两个方向都吃得下，所以按短板算容量。"""
        return min(self.bid_notional, self.ask_notional)

    def to_book_top(self) -> BookTop:
        return BookTop(self.bid_price, self.bid_qty, self.ask_price,
                       self.ask_qty, self.ts_ms)


@dataclass(frozen=True, slots=True)
class CarryRate:
    """一个周期的 carry 率。

    符号用**交易所原始约定**：正 = 多头付给空头（空头收钱）。
    六家 API 返回的都是这个符号，适配器原样写入不必取反。
    杠杆借币利息也统一到该约定：做多借币要付利息，记为正。
    上层一律用 `short_rate - long_rate` 算净收益，无需分支。
    """

    market: str
    symbol: str
    rate: Decimal            # 单个结算周期的费率
    interval_s: int          # 该费率对应的结算周期
    next_settle_ms: int
    ts_ms: int

    def annualized(self) -> Decimal:
        """年化。不同结算周期必须归一化后才能横向比较——
        1小时结算的 0.0002 实际是 8小时结算 0.0002 的 8 倍。"""
        if self.interval_s <= 0:
            return ZERO
        periods_per_year = Decimal(365 * 24 * 3600) / Decimal(self.interval_s)
        return self.rate * periods_per_year


@dataclass(frozen=True, slots=True)
class FundingHistory:
    """历史资金费序列，用于稳定性评分。"""

    market: str
    symbol: str
    rates: tuple[Decimal, ...]
    interval_s: int

    @property
    def count(self) -> int:
        return len(self.rates)

    def mean(self) -> Decimal:
        if not self.rates:
            return ZERO
        return sum(self.rates, ZERO) / Decimal(len(self.rates))

    def stdev(self) -> Decimal:
        n = len(self.rates)
        if n < 2:
            return ZERO
        mu = self.mean()
        var = sum(((r - mu) ** 2 for r in self.rates), ZERO) / Decimal(n - 1)
        # Decimal 没有 sqrt，用 Decimal.sqrt()
        return var.sqrt()

    def sign_flip_ratio(self) -> Decimal:
        """符号翻转频率。费率老是正负横跳说明它不可依赖。"""
        if len(self.rates) < 2:
            return ZERO
        flips = sum(
            1 for a, b in zip(self.rates, self.rates[1:])
            if (a > ZERO) != (b > ZERO)
        )
        return Decimal(flips) / Decimal(len(self.rates) - 1)


@dataclass(frozen=True, slots=True)
class Position:
    market: str
    symbol: str
    qty: Decimal              # 有符号：正=多头，负=空头
    entry_price: Decimal
    mark_price: Decimal
    liquidation_price: Decimal | None
    leverage: Decimal

    @property
    def is_flat(self) -> bool:
        return self.qty == ZERO

    def liquidation_distance(self) -> Decimal | None:
        """距爆仓价的相对距离。0.2 表示还有 20% 的空间。

        原版工具算了这个数但只用来在界面上显示，执行器一行都没读它。
        这里它会真正进入风控决策。
        """
        if self.liquidation_price is None or self.mark_price == ZERO:
            return None
        return abs(self.mark_price - self.liquidation_price) / self.mark_price


@dataclass(frozen=True, slots=True)
class Leg:
    """对冲的一条腿。"""

    market: str      # MarketSpec.key
    symbol: str
    account_id: str


@dataclass(frozen=True, slots=True)
class Hedge:
    """一个对冲仓位。

    target 是**多腿要持有多少个 base 币**（不是美元）。
    空腿目标自动等于 -(target * multiplier)。
    multiplier 用于两腿合约面值不同时的单位换算，同单位填 1。
    """

    id: str
    long: Leg
    short: Leg
    target: Decimal
    multiplier: Decimal
    status: HedgeStatus
    strategy_id: str | None = None

    def short_target(self) -> Decimal:
        return -(self.target * self.multiplier)


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """用户在某市场的实际费率档位。评分必须用真实费率，用默认值会系统性错判。

    符号约定与 scoring 一致：**正数 = 成本**。maker 为负表示返佣（收入）。
    OKX 原样返回的是"负数=你付"，适配器负责翻转过来。

    `symbol` 是这条档位属于哪个品种。**空串表示账户级档位**——OKX/Bitget/币安
    的合约费率就是账户级的，一档吃遍全市场；Bybit/Gate/HTX 则逐品种给。
    这个字段是必须的：FeeSchedule 早先没有它，六家里有三家在查不到某个符号时
    补一个零费率占位、另外两家直接跳过，调用方既分不清哪条对应哪个品种，
    也分不清"零手续费"和"没查到"——而零手续费会让负期望的机会全变成正期望。
    现在的契约是**按 symbol 匹配，绝不按下标**。
    """

    market: str
    maker: Decimal
    taker: Decimal
    symbol: str = ""
    # 档位的来源/附注（如"已开 BNB 抵扣，实际扣费更低"）。界面直接显示给用户。
    note: str = ""


# 机会及其评分的权威定义在 scoring.ScoredOpportunity。
# 这里曾有一个同名的 Opportunity，字段扁平且与 scoring 的不一致，
# 已删除——两套定义只会让人写出取错字段的代码。


@dataclass(frozen=True, slots=True)
class OrderPlan:
    """一次下单的完整计划。"""

    market: str
    symbol: str
    delta: Decimal        # 有符号：正=买，负=卖
    price: Decimal        # FOK 保护限价
    reduce_only: bool
    is_opening: bool      # 加仓还是减仓


class PlanOutcome(str, enum.Enum):
    """为什么没有产生订单。"""

    IDLE = "idle"               # 无缺口
    STALE = "stale"             # 盘口数据陈旧
    PINCHED = "pinched"         # 截断/取整后量为零
    BELOW_MIN = "below_min"     # 不满足最小名义额
    BLOCKED = "blocked"         # 被风控闸门拦下


@dataclass(frozen=True, slots=True)
class LegPlan:
    """某条腿这一拍的决策结果。要么有订单，要么有不下单的原因。"""

    order: OrderPlan | None
    outcome: PlanOutcome | None

    @property
    def has_order(self) -> bool:
        return self.order is not None


# ---- 市场能力表 ---------------------------------------------------------

def _spec(venue: Venue, kind: MarketKind, carry: CarryKind, shortable: bool,
          reduce_min: bool) -> tuple[str, MarketSpec]:
    s = MarketSpec(venue=venue, kind=kind, carry=carry, shortable=shortable,
                   reduce_min_notional=reduce_min)
    return s.key, s


def _build_registry() -> Mapping[str, MarketSpec]:
    out: dict[str, MarketSpec] = {}
    for venue in Venue:
        for kind, carry, shortable, reduce_min in (
            (MarketKind.SPOT, CarryKind.ZERO, False, True),
            (MarketKind.MARGIN, CarryKind.INTEREST, True, True),
            (MarketKind.PERP, CarryKind.FUNDING, True, False),
        ):
            key, spec = _spec(venue, kind, carry, shortable, reduce_min)
            out[key] = spec
    return out


MARKETS: Mapping[str, MarketSpec] = _build_registry()


class PairingError(Exception):
    """两腿组合不合法。"""


def validate_pairing(long: Leg, short: Leg) -> None:
    """两腿组合的硬性禁令。在用户创建对冲时就拦下，别等下单才发现。"""
    long_spec = MARKETS.get(long.market)
    short_spec = MARKETS.get(short.market)
    if long_spec is None:
        raise PairingError(f"未知市场: {long.market}")
    if short_spec is None:
        raise PairingError(f"未知市场: {short.market}")
    if not short_spec.shortable:
        raise PairingError(f"{short.market} 不能做空，不能作为空腿")
    if long.market == short.market and long.symbol == short.symbol:
        raise PairingError("两腿是同一个市场的同一个交易对，价差恒为零")
    if (long_spec.venue == short_spec.venue
            and {long_spec.kind, short_spec.kind} == {MarketKind.SPOT, MarketKind.MARGIN}):
        raise PairingError(
            f"{long_spec.venue.value} 的现货与全仓杠杆共用撮合面和钱包，不能互为两腿"
        )
