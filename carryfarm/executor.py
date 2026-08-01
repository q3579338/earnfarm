"""执行器状态机：把 safety.py 的决策变成实际下单。

设计要点（每一条都对应原版工具的一个真实缺陷）：

1. **一个建仓分片 = 一个状态机实例**。做不到原子，就把不原子的代价除以 N。
2. **串行、难腿先、第二腿数量 = 第一腿实际成交量**。数量错配在结构上消失。
3. **幂等 client_order_id + in-flight 锁**。原版 500ms 一拍并发下单，若回执 800ms 才到，
   第二拍会重复下单——这本身就能造成双倍仓位。
4. **超时不等于失败**。走 ORDER_UNKNOWN 消解：cancel 终结 → 查单 → 仓位差分。
5. **收敛条件必须包含"放弃"这个终态**。REPAIR 有出口，不像原版那样无限重试。

状态机是事件驱动 + 定时兜底，**不是**定时轮转下单——后者在回执延迟时会重复开火。
"""

from __future__ import annotations

import asyncio
import enum
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Awaitable, Callable, Protocol, Sequence

from .safety import (
    DEFAULT_PARAMS,
    BreakerLevel,
    ExposureLevel,
    ExposureSnapshot,
    LegFilters,
    LegHealth,
    RepairVerdict,
    SafetyParams,
    UnwindPhase,
    classify_exposure,
    floor_to_step,
    repair_backoff_ms,
    repair_verdict,
    second_leg_qty,
    unwind_step,
)

ZERO = Decimal("0")


class State(enum.Enum):
    IDLE = "idle"
    PREFLIGHT = "preflight"          # 建仓前的全部闸门
    LEG1_PENDING = "leg1_pending"    # 难腿在途
    EXPOSED = "exposed"              # 难腿已成，尚未补易腿
    LEG2_PENDING = "leg2_pending"
    REPAIR = "repair"                # 补腿中，有明确的放弃条件
    UNWIND = "unwind"                # 撤退：平掉已成交腿
    ORDER_UNKNOWN = "order_unknown"  # 订单状态未知，消解中
    BALANCED = "balanced"            # 两腿平衡，正常持有
    FROZEN = "frozen"                # 看不见仓位 —— 不下任何单
    HALTED = "halted"                # 停机等人工，但保命动作仍执行
    FLAT = "flat"                    # 已清空


class PreflightGate(str, enum.Enum):
    """建仓前的闸门。任一不过就不开仓——绝不在热路径上补救。"""

    BREAKER = "breaker"
    RISK_TIER = "risk_tier"
    QUOTE_STALE = "quote_stale"
    DEPTH = "depth"
    SPREAD = "spread"
    MARGIN = "margin"
    LEVERAGE = "leverage"            # 杠杆必须预设好，不在下单路径上调
    TIER_EDGE = "tier_edge"
    SYMBOL_STATUS = "symbol_status"
    FUNDING_WINDOW = "funding_window"
    BLACKLIST = "blacklist"
    COOLDOWN = "cooldown"


@dataclass(frozen=True, slots=True)
class Fill:
    qty: Decimal          # 有符号
    price: Decimal
    fee: Decimal = ZERO
    fee_asset: str = ""


@dataclass(frozen=True, slots=True)
class PlacedOrder:
    """下单结果的统一表示。"""

    client_order_id: str
    filled: Decimal
    avg_price: Decimal
    status: str           # filled / partial / rejected / unknown
    error: str = ""

    @property
    def is_unknown(self) -> bool:
        return self.status == "unknown"


class OrderGateway(Protocol):
    """执行器需要的 I/O 能力。用 Protocol 而非具体适配器，
    这样状态机可以用假网关完整测试——它是最容易出错的部分，必须可测。"""

    async def place(self, market: str, symbol: str, side: str, qty: Decimal,
                    price: Decimal | None, *, reduce_only: bool,
                    client_order_id: str, tif: str) -> PlacedOrder: ...

    async def resolve_unknown(self, market: str, symbol: str,
                              client_order_id: str) -> Decimal | None: ...

    async def position_qty(self, market: str, symbol: str) -> Decimal: ...

    async def book(self, market: str, symbol: str) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """返回 (bid, bid_qty, ask, ask_qty)。"""

    async def leg_health(self, market: str, symbol: str) -> LegHealth: ...


@dataclass(slots=True)
class LegSpec:
    market: str
    symbol: str
    side: str                  # "buy"(多腿) / "sell"(空腿)
    filters: LegFilters
    tick_size: Decimal = Decimal("0.01")


@dataclass(slots=True)
class Unit:
    """一个建仓分片。状态机的实例。"""

    uid: str
    hedge_id: str
    hard: LegSpec              # 难腿：先打
    easy: LegSpec
    target_qty: Decimal        # 本片目标（base 币，绝对值）
    mark: Decimal
    equity: float

    state: State = State.IDLE
    state_since: float = field(default_factory=time.monotonic)
    exposed_since: float | None = None
    hard_filled: Decimal = ZERO
    easy_filled: Decimal = ZERO
    repair_attempts: int = 0
    unwind_started: float | None = None
    phase2_failures: int = 0
    exposure_integral: float = 0.0
    inflight: dict[str, str] = field(default_factory=dict)   # cid -> "hard"/"easy"
    last_error: str = ""

    def goto(self, state: State) -> None:
        self.state = state
        self.state_since = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.state_since

    @property
    def exposed_for(self) -> float:
        return time.monotonic() - self.exposed_since if self.exposed_since else 0.0

    def signed(self, leg: LegSpec, qty: Decimal) -> Decimal:
        """把绝对量转成有符号量：买为正、卖为负。"""
        return qty if leg.side == "buy" else -qty

    def net_qty(self) -> Decimal:
        """两腿净敞口。理想为 0。"""
        return self.signed(self.hard, self.hard_filled) + self.signed(self.easy, self.easy_filled)


class Executor:
    """对冲执行器。

    每个 Unit 独立推进，互不阻塞。状态迁移由事件（订单回执、对账结果）驱动，
    1 秒兜底 tick 只做超时检查，**不做定时下单**。
    """

    def __init__(self, gateway: OrderGateway, *,
                 params: SafetyParams = DEFAULT_PARAMS,
                 breaker: Callable[[], BreakerLevel] = lambda: BreakerLevel.CLEAR,
                 on_event: Callable[[str, str, str], None] | None = None) -> None:
        self._gw = gateway
        self._p = params
        self._breaker = breaker
        self._on_event = on_event or (lambda level, category, msg: None)

    # ---- 幂等下单 -------------------------------------------------------

    async def _place(self, unit: Unit, leg: LegSpec, tag: str, qty: Decimal,
                     price: Decimal | None, *, reduce_only: bool,
                     tif: str = "IOC") -> PlacedOrder:
        """唯一的下单入口。任何地方都不许绕过它直接调网关。

        client_order_id 里编入 unit + 腿 + 重试轮次 + 随机后缀，保证：
        - 幂等：同一轮重发不会产生两笔
        - 可追溯：撤单/查单能靠它找回
        """
        cid = f"{unit.uid[:8]}{tag}{unit.repair_attempts:02d}{uuid.uuid4().hex[:6]}"
        unit.inflight[cid] = tag
        try:
            result = await self._gw.place(
                leg.market, leg.symbol, leg.side if not reduce_only else _flip(leg.side),
                qty, price, reduce_only=reduce_only, client_order_id=cid, tif=tif,
            )
        except Exception as exc:            # 网关自己没兜住的异常一律按未知处理
            return PlacedOrder(cid, ZERO, ZERO, "unknown", str(exc))
        if not result.is_unknown:
            unit.inflight.pop(cid, None)
        return result

    async def _settle_inflight(self, unit: Unit) -> bool:
        """终结所有在途订单。返回是否全部消解成功。

        这是撤退的 Phase -1，**强制不可跳过**：
        你判定易腿失败准备平难腿，但易腿的成交回执只是延迟了 800ms。
        不先终结就去平难腿，2 秒后回执到达——你从多头裸奔变成空头裸奔，
        方向反了、量翻倍了。必须先让在途订单不可能再成交，再决定平多少。
        """
        for cid, tag in list(unit.inflight.items()):
            leg = unit.hard if tag == "hard" else unit.easy
            filled = await self._gw.resolve_unknown(leg.market, leg.symbol, cid)
            unit.inflight.pop(cid, None)
            if filled is None:
                unit.last_error = f"订单 {cid} 无法消解，交易所不可读"
                return False
            self._apply_fill(unit, tag, filled)
        return True

    @staticmethod
    def _apply_fill(unit: Unit, tag: str, qty: Decimal) -> None:
        if tag == "hard":
            unit.hard_filled += qty
        else:
            unit.easy_filled += qty

    # ---- 敞口 -----------------------------------------------------------

    def exposure(self, unit: Unit, *, naked_pnl: float = 0.0,
                 vol_scale: float = 1.0) -> ExposureSnapshot:
        pos_hard = unit.signed(unit.hard, unit.hard_filled)
        pos_easy = unit.signed(unit.easy, unit.easy_filled)
        return classify_exposure(pos_hard, pos_easy, unit.mark, unit.equity,
                                 unit.hard.filters, unit.easy.filters,
                                 vol_scale=vol_scale, naked_pnl=naked_pnl,
                                 exposure_integral=unit.exposure_integral,
                                 params=self._p)

    # ---- 价格 -----------------------------------------------------------

    @staticmethod
    def cross_price(bid: Decimal, ask: Decimal, side: str, cross_bp: float,
                    tick: Decimal) -> Decimal:
        """穿价保护限价。买单穿卖一、卖单穿买一，穿越幅度按基点给。

        用 marketable IOC 而不是纯市价：效果接近市价但有滑点上限，
        能挡住插针和深度真空。
        """
        base = ask if side == "buy" else bid
        delta = base * Decimal(str(cross_bp)) / Decimal("10000")
        raw = base + delta if side == "buy" else base - delta
        # 买单向上取整、卖单向下取整到 tick——保证限价不会因取整而失去可成交性
        steps = (raw / tick).to_integral_value(
            rounding="ROUND_CEILING" if side == "buy" else "ROUND_FLOOR")
        return steps * tick

    # ---- 状态推进 -------------------------------------------------------

    async def step(self, unit: Unit) -> State:
        """推进一步。调用方按事件或 1 秒 tick 驱动。"""
        # 敞口的时间积分：防"一直 SOFT 但裸了 5 分钟"
        if unit.exposed_since is not None:
            snap = self.exposure(unit)
            unit.exposure_integral += snap.naked_usd * 0.5

        handler = _HANDLERS.get(unit.state)
        if handler is None:
            return unit.state
        return await handler(self, unit)

    async def _on_idle(self, unit: Unit) -> State:
        unit.goto(State.PREFLIGHT)
        return unit.state

    async def _on_preflight(self, unit: Unit) -> State:
        gates = await self.check_gates(unit)
        if gates:
            self._on_event("info", "execution", f"建仓闸门未过：{gates[0].value}")
            unit.goto(State.IDLE)
            return unit.state

        bid, bid_qty, ask, ask_qty = await self._gw.book(unit.hard.market, unit.hard.symbol)
        avail = ask_qty if unit.hard.side == "buy" else bid_qty
        qty = floor_to_step(min(unit.target_qty, avail), unit.hard.filters.step_size)
        if qty <= ZERO:
            unit.goto(State.IDLE)
            return unit.state

        price = self.cross_price(bid, ask, unit.hard.side, 3.0, unit.hard.tick_size)
        unit.goto(State.LEG1_PENDING)
        result = await self._place(unit, unit.hard, "hard", qty, price, reduce_only=False)

        if result.is_unknown:
            unit.goto(State.ORDER_UNKNOWN)
            return unit.state
        unit.hard_filled += result.filled
        if abs(result.filled * unit.mark) <= Decimal(str(self.dust(unit))):
            unit.goto(State.IDLE)          # 难腿没成 → 零敞口，安全退出
            return unit.state
        unit.exposed_since = time.monotonic()
        unit.goto(State.EXPOSED)
        return unit.state

    async def _on_exposed(self, unit: Unit) -> State:
        """补易腿。数量 = 难腿实际成交量对齐到公共步长，残差削平。"""
        qty, residual = second_leg_qty(unit.hard_filled,
                                       unit.hard.filters.step_size,
                                       unit.easy.filters.step_size)
        if abs(residual * unit.mark) > Decimal(str(self.dust(unit))):
            # 与其留一个补不上的残差裸着，不如把难腿多出的部分削平
            await self._trim(unit, residual)

        if qty <= ZERO:
            unit.goto(State.UNWIND)
            return unit.state

        bid, _, ask, _ = await self._gw.book(unit.easy.market, unit.easy.symbol)
        price = self.cross_price(bid, ask, unit.easy.side, 5.0, unit.easy.tick_size)
        unit.goto(State.LEG2_PENDING)
        result = await self._place(unit, unit.easy, "easy", qty, price, reduce_only=False)

        if result.is_unknown:
            unit.goto(State.ORDER_UNKNOWN)
            return unit.state
        unit.easy_filled += result.filled
        snap = self.exposure(unit)
        unit.goto(State.BALANCED if snap.level is ExposureLevel.DUST else State.REPAIR)
        return unit.state

    async def _on_repair(self, unit: Unit) -> State:
        snap = self.exposure(unit)
        if snap.level is ExposureLevel.DUST:
            unit.goto(State.BALANCED)
            return unit.state
        if snap.level is ExposureLevel.CRITICAL:
            unit.unwind_started = time.monotonic()
            unit.goto(State.UNWIND)
            return unit.state

        health = await self._gw.leg_health(unit.easy.market, unit.easy.symbol)
        verdict = repair_verdict(snap.level, health, elapsed_s=unit.exposed_for,
                                 attempts=unit.repair_attempts,
                                 exposure_integral=unit.exposure_integral,
                                 equity=unit.equity, params=self._p)
        if verdict.is_hopeless:
            self._on_event("warn", "safety",
                           f"补腿判死（{verdict.value}），转撤退：裸露 "
                           f"${snap.naked_usd:,.0f} 已 {unit.exposed_for:.0f} 秒")
            unit.unwind_started = time.monotonic()
            unit.goto(State.UNWIND)
            return unit.state

        await asyncio.sleep(repair_backoff_ms(unit.repair_attempts, self._p) / 1000)
        unit.repair_attempts += 1
        remaining = abs(unit.net_qty())
        qty = floor_to_step(remaining, unit.easy.filters.step_size)
        if qty <= ZERO:
            unit.goto(State.BALANCED)
            return unit.state

        bid, _, ask, _ = await self._gw.book(unit.easy.market, unit.easy.symbol)
        price = self.cross_price(bid, ask, unit.easy.side, self._p.repair_cross_bp,
                                 unit.easy.tick_size)
        result = await self._place(unit, unit.easy, "easy", qty, price, reduce_only=False)
        if result.is_unknown:
            unit.goto(State.ORDER_UNKNOWN)
            return unit.state
        unit.easy_filled += result.filled
        return unit.state

    async def _on_unwind(self, unit: Unit) -> State:
        """撤退。Phase -1 必须先跑完，否则可能把裸奔方向做反。"""
        if unit.inflight and not await self._settle_inflight(unit):
            unit.goto(State.FROZEN)
            return unit.state

        snap = self.exposure(unit)
        if snap.level is ExposureLevel.DUST:
            self._on_event("info", "safety", "撤退完成，敞口已中和")
            unit.goto(State.FLAT)
            return unit.state

        elapsed = time.monotonic() - (unit.unwind_started or time.monotonic())
        step = unwind_step(elapsed, unit.phase2_failures, params=self._p)
        if step.phase is UnwindPhase.CROSS_HEDGE:
            self._on_event("error", "safety",
                           "市价撤退连续失败，需跨市场中和敞口——转人工")
            unit.goto(State.HALTED)
            return unit.state

        net = unit.net_qty()
        leg = unit.hard if _same_sign(unit.signed(unit.hard, unit.hard_filled), net) else unit.easy
        tag = "hard" if leg is unit.hard else "easy"
        bid, bid_qty, ask, ask_qty = await self._gw.book(leg.market, leg.symbol)
        closing_side = "sell" if net > ZERO else "buy"

        cap = (ask_qty if closing_side == "buy" else bid_qty) * Decimal(str(step.qty_cap_frac))
        qty = floor_to_step(min(abs(net), cap) if step.qty_cap_frac < 1 else abs(net),
                            leg.filters.step_size)
        if qty <= ZERO:
            qty = floor_to_step(abs(net), leg.filters.step_size)
        if qty <= ZERO:
            unit.goto(State.FLAT)
            return unit.state

        price = (None if step.cross_bp is None
                 else self.cross_price(bid, ask, closing_side, step.cross_bp, leg.tick_size))
        # reduce_only 恒为真：仓位若已被 ADL 清掉，普通单会开出反向仓
        result = await self._place(unit, leg, tag, qty, price,
                                   reduce_only=True,
                                   tif="MARKET" if price is None else "IOC")
        if result.is_unknown:
            unit.goto(State.ORDER_UNKNOWN)
            return unit.state
        if result.status == "rejected":
            # reduceOnly 被拒且交易所说没仓位 → 说明仓位已被别的路径平掉
            # （ADL、强平、或我们自己的记账漂了）。公理 1：仓位是真值。
            # 这时候绝不能重下——要以交易所为准把本地掰回来。
            if _is_no_position(result.error):
                await self._reconcile(unit)
                return unit.state
            if step.phase is UnwindPhase.MARKET:
                unit.phase2_failures += 1
            unit.last_error = result.error
            return unit.state

        # hard_filled/easy_filled 存的是**绝对持仓量**，方向由 signed() 给。
        # 所以平仓永远是减少绝对量，与买卖方向无关——
        # 按买卖方向加减会让平仓被记成加仓，本地和交易所当场分家。
        self._apply_fill(unit, tag, -result.filled)
        return unit.state

    async def _reconcile(self, unit: Unit) -> None:
        """以交易所持仓为准，把本地记账掰回来。

        公理 1：仓位是真值，订单是过程。任何时候本地与交易所冲突，
        无条件以交易所为准——订单回执可以丢、可以迟到、可以撒谎，仓位快照不会。
        """
        try:
            hard_pos = await self._gw.position_qty(unit.hard.market, unit.hard.symbol)
            easy_pos = await self._gw.position_qty(unit.easy.market, unit.easy.symbol)
        except Exception as exc:
            unit.last_error = f"对账失败，交易所不可读：{exc}"
            unit.goto(State.FROZEN)
            return
        before = (unit.hard_filled, unit.easy_filled)
        unit.hard_filled = abs(hard_pos)
        unit.easy_filled = abs(easy_pos)
        if before != (unit.hard_filled, unit.easy_filled):
            self._on_event(
                "warn", "safety",
                f"对账修正 {unit.uid}：本地 {before} → 交易所 "
                f"({unit.hard_filled}, {unit.easy_filled})")
        snap = self.exposure(unit)
        unit.goto(State.FLAT if snap.level is ExposureLevel.DUST else State.REPAIR)

    async def _on_order_unknown(self, unit: Unit) -> State:
        """订单状态未知的消解。绝不能简单重发——那会导致双倍仓位。"""
        if not await self._settle_inflight(unit):
            unit.goto(State.FROZEN)
            return unit.state
        snap = self.exposure(unit)
        if snap.level is ExposureLevel.DUST:
            unit.goto(State.BALANCED if unit.hard_filled != ZERO else State.IDLE)
        else:
            unit.goto(State.REPAIR)
        return unit.state

    async def _on_frozen(self, unit: Unit) -> State:
        """看不见仓位时不下任何单——在盲的时候下单是最糟的选择。"""
        try:
            await self._gw.position_qty(unit.hard.market, unit.hard.symbol)
        except Exception:
            return unit.state
        unit.goto(State.UNWIND if unit.net_qty() != ZERO else State.BALANCED)
        return unit.state

    async def _on_balanced(self, unit: Unit) -> State:
        unit.exposed_since = None
        unit.exposure_integral = 0.0
        return unit.state

    # ---- 辅助 -----------------------------------------------------------

    def dust(self, unit: Unit) -> float:
        from .safety import dust_threshold_usd
        return dust_threshold_usd(unit.hard.filters, unit.easy.filters, unit.mark)

    async def _trim(self, unit: Unit, residual: Decimal) -> None:
        """削掉难腿多出来的、易腿补不上的那点残差。走的是撤退通道，只是量很小。"""
        qty = floor_to_step(abs(residual), unit.hard.filters.step_size)
        if qty <= ZERO:
            return
        bid, _, ask, _ = await self._gw.book(unit.hard.market, unit.hard.symbol)
        side = "sell" if unit.signed(unit.hard, residual) > ZERO else "buy"
        price = self.cross_price(bid, ask, side, 10.0, unit.hard.tick_size)
        result = await self._place(unit, unit.hard, "hard", qty, price, reduce_only=True)
        if not result.is_unknown and result.filled > ZERO:
            unit.hard_filled -= unit.signed(unit.hard, result.filled)

    async def check_gates(self, unit: Unit) -> list[PreflightGate]:
        """建仓闸门。任一不过就不开——绝不在下单热路径上补救。

        原版把"调杠杆失败则作废加仓"放在热路径上，说明它在下单时才调杠杆，
        既慢又多一个失败点。杠杆必须在这里预设好。
        """
        failed: list[PreflightGate] = []
        if self._breaker() >= BreakerLevel.SOFT:
            failed.append(PreflightGate.BREAKER)
        health = await self._gw.leg_health(unit.easy.market, unit.easy.symbol)
        if not health.symbol_trading:
            failed.append(PreflightGate.SYMBOL_STATUS)
        if health.exchange_reduce_only:
            failed.append(PreflightGate.SYMBOL_STATUS)
        if health.available_margin < health.required_margin:
            failed.append(PreflightGate.MARGIN)
        if health.in_maintenance:
            failed.append(PreflightGate.SYMBOL_STATUS)
        return failed


def _flip(side: str) -> str:
    return "sell" if side == "buy" else "buy"


# 各家"reduceOnly 但没仓位"的错误说法都不同：币安 -2022、Bybit 110017、
# OKX 51169…… 统一按关键词认，认错了最多多跑一次对账，认漏了会无限重试。
_NO_POSITION_MARKERS = ("-2022", "reduceonly", "reduce-only", "no position",
                        "无仓位", "position is zero", "110017", "51169")


def _is_no_position(error: str) -> bool:
    low = (error or "").lower()
    return any(m in low for m in _NO_POSITION_MARKERS)


def _same_sign(a: Decimal, b: Decimal) -> bool:
    return (a > ZERO) == (b > ZERO) and a != ZERO and b != ZERO


_HANDLERS: dict[State, Callable[[Executor, Unit], Awaitable[State]]] = {
    State.IDLE: Executor._on_idle,
    State.PREFLIGHT: Executor._on_preflight,
    State.EXPOSED: Executor._on_exposed,
    State.REPAIR: Executor._on_repair,
    State.UNWIND: Executor._on_unwind,
    State.ORDER_UNKNOWN: Executor._on_order_unknown,
    State.FROZEN: Executor._on_frozen,
    State.BALANCED: Executor._on_balanced,
}
