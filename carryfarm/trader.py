"""交易协调器：把机会变成实际推进的对冲仓位。

它负责三件事：
1. 把一个 ScoredOpportunity 变成 Hedge 存库
2. 按分片把 Hedge 拆成 Unit 交给 Executor 推进
3. 驱动主循环，并把状态暴露给界面

纸上模式和实盘的唯一区别是塞进去的 gateway 不同——**执行逻辑完全同一套**。
这很重要：如果纸上跑的是另一套代码，那纸上验证过的东西不能说明实盘也对。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Sequence

from .executor import Executor, LegSpec, OrderGateway, State, Unit
from .models import Hedge, HedgeStatus, Leg, validate_pairing
from .safety import (
    DEFAULT_PARAMS,
    BreakerLevel,
    BreakerSignals,
    LegFilters,
    SafetyParams,
    evaluate_breaker,
    slice_max_usd,
)
from .scoring import ScoredOpportunity
from .storage import Storage

ZERO = Decimal("0")


@dataclass(slots=True)
class HedgeRuntime:
    """一个对冲仓位的运行时状态。界面直接读它渲染。"""

    hedge: Hedge
    units: list[Unit] = field(default_factory=list)
    long_filled: Decimal = ZERO
    short_filled: Decimal = ZERO
    last_event: str = ""
    last_event_level: str = "info"       # info / warn / error
    slices_done: int = 0
    slices_total: int = 0

    @property
    def net_qty(self) -> Decimal:
        return self.long_filled + self.short_filled

    @property
    def is_settled(self) -> bool:
        return all(u.state in (State.BALANCED, State.FLAT) for u in self.units) \
            and self.slices_done >= self.slices_total

    @property
    def progress(self) -> float:
        return self.slices_done / self.slices_total if self.slices_total else 0.0

    @property
    def active_state(self) -> State:
        """最"危险"的那个分片的状态——界面上要显示这个，
        而不是显示"平均状态"那种没有意义的东西。"""
        order = [State.HALTED, State.FROZEN, State.UNWIND, State.ORDER_UNKNOWN,
                 State.REPAIR, State.EXPOSED, State.LEG2_PENDING, State.LEG1_PENDING,
                 State.PREFLIGHT, State.BALANCED, State.FLAT, State.IDLE]
        states = {u.state for u in self.units}
        for s in order:
            if s in states:
                return s
        return State.IDLE


class Trader:
    """对冲仓位的协调器。"""

    def __init__(self, gateway: OrderGateway, storage: Storage, *,
                 params: SafetyParams = DEFAULT_PARAMS,
                 equity: float = 100_000.0,
                 paper: bool = True,
                 on_event: Callable[[str, str, str], None] | None = None) -> None:
        self._gw = gateway
        self._storage = storage
        self._params = params
        self._equity = equity
        self.paper = paper
        self._runtimes: dict[str, HedgeRuntime] = {}
        self._breaker = BreakerLevel.CLEAR
        self._signals = BreakerSignals()
        self._on_event = on_event or (lambda level, category, msg: None)
        self._executor = Executor(gateway, params=params,
                                  breaker=lambda: self._breaker,
                                  on_event=self._record_event)
        self._running = False
        self._task: asyncio.Task | None = None

    # ---- 事件 -----------------------------------------------------------

    def _record_event(self, level: str, category: str, message: str) -> None:
        self._storage.log_event(level, category, message, int(time.time() * 1000))
        self._on_event(level, category, message)

    # ---- 建仓 -----------------------------------------------------------

    def create_from_opportunity(self, op: ScoredOpportunity, *,
                                long_account: str, short_account: str,
                                notional: float | None = None,
                                multiplier: Decimal = Decimal("1"),
                                long_filters: LegFilters | None = None,
                                short_filters: LegFilters | None = None,
                                long_tick: Decimal = Decimal("0.01"),
                                short_tick: Decimal = Decimal("0.01")) -> HedgeRuntime:
        """从一个机会建对冲。

        notional 留空则用机会自己的建议仓位——它已经按深度缩过，
        直接照用比让用户再想一遍靠谱。
        """
        size_usd = notional if notional is not None else op.suggested_notional
        if size_usd <= 0:
            raise ValueError("建议仓位为 0，这个机会的深度撑不住任何仓位")

        long_leg = Leg(op.long.market, op.long.symbol, long_account)
        short_leg = Leg(op.short.market, op.short.symbol, short_account)
        validate_pairing(long_leg, short_leg)     # 非法组合在这里就拦下

        mark = Decimal(str(op.long.mid))
        target_qty = Decimal(str(size_usd)) / mark

        hedge = Hedge(
            id=uuid.uuid4().hex, long=long_leg, short=short_leg,
            target=target_qty, multiplier=multiplier,
            status=HedgeStatus.RUNNING,
        )
        self._storage.upsert_hedge(hedge, int(time.time() * 1000))

        lf = long_filters or _default_filters(mark)
        sf = short_filters or _default_filters(mark)

        # 分片：做不到原子，就把不原子的代价除以 N。
        # 单片全裸也不该超过 HARD 档，且不超难腿浅档深度的 15%。
        cap = slice_max_usd(min(op.long.depth_notional, op.short.depth_notional),
                            self._equity, params=self._params)
        n_slices = max(1, int(size_usd / cap) + (1 if size_usd % cap else 0))
        per_slice = target_qty / n_slices

        runtime = HedgeRuntime(hedge=hedge, slices_total=n_slices)
        for i in range(n_slices):
            runtime.units.append(Unit(
                uid=f"{hedge.id[:8]}{i:03d}", hedge_id=hedge.id,
                # 难腿先打：这里用滑点+价差粗判，运行时会被真实统计覆盖
                hard=LegSpec(op.short.market, op.short.symbol, "sell", sf, short_tick),
                easy=LegSpec(op.long.market, op.long.symbol, "buy", lf, long_tick),
                target_qty=per_slice, mark=mark, equity=self._equity,
            ))
        self._runtimes[hedge.id] = runtime
        self._record_event(
            "info", "execution",
            f"建仓 {op.base}：多 {op.long.market} / 空 {op.short.market}，"
            f"${size_usd:,.0f} 分 {n_slices} 片"
            + ("（纸上模式，不动真钱）" if self.paper else "（实盘）"))
        return runtime

    # ---- 主循环 ---------------------------------------------------------

    async def step_all(self) -> None:
        """推进一拍。事件驱动 + 定时兜底，**不是**定时轮转下单。"""
        self._breaker = evaluate_breaker(self._signals, self._params).level
        for runtime in list(self._runtimes.values()):
            done = 0
            for unit in runtime.units:
                if unit.state in (State.BALANCED, State.FLAT):
                    done += 1
                    continue
                # 一次只推一个未完成的分片：分片的意义就在于错开，
                # 全部并发等于没分片
                try:
                    await self._executor.step(unit)
                except Exception as exc:
                    runtime.last_event = f"分片 {unit.uid} 异常：{exc}"
                    runtime.last_event_level = "error"
                    self._record_event("error", "execution", runtime.last_event)
                break
            runtime.slices_done = done
            runtime.long_filled = sum(
                (u.signed(u.easy, u.easy_filled) for u in runtime.units), ZERO)
            runtime.short_filled = sum(
                (u.signed(u.hard, u.hard_filled) for u in runtime.units), ZERO)

    async def run(self, interval_s: float = 0.5) -> None:
        self._running = True
        while self._running:
            await self.step_all()
            await asyncio.sleep(interval_s)

    def start(self, interval_s: float = 0.5) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(interval_s))

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ---- 查询与控制 -----------------------------------------------------

    def runtimes(self) -> list[HedgeRuntime]:
        return list(self._runtimes.values())

    def get(self, hedge_id: str) -> HedgeRuntime | None:
        return self._runtimes.get(hedge_id)

    def close_hedge(self, hedge_id: str) -> None:
        """平掉一个对冲：把所有分片打进撤退通道。

        走的是和裸奔撤退同一条路——阶梯穿价 + reduce_only，
        不是简单市价砸出去。
        """
        runtime = self._runtimes.get(hedge_id)
        if runtime is None:
            return
        for unit in runtime.units:
            if unit.state not in (State.FLAT,):
                unit.unwind_started = time.monotonic()
                unit.goto(State.UNWIND)
        self._storage.set_status(hedge_id, HedgeStatus.RETREATING,
                                 int(time.time() * 1000))
        self._record_event("warn", "execution", f"手动平仓 {hedge_id[:8]}，走撤退阶梯")

    def pause_hedge(self, hedge_id: str) -> None:
        """只减不增。风控触发或人工介入后用——一旦进这个状态，
        策略就再也抬不高它的目标，必须人工恢复。"""
        self._storage.set_status(hedge_id, HedgeStatus.REDUCE_ONLY,
                                 int(time.time() * 1000))
        self._record_event("warn", "execution", f"{hedge_id[:8]} 转为只减不增")

    def update_signals(self, **kwargs) -> None:
        from dataclasses import replace
        self._signals = replace(self._signals, **kwargs)

    @property
    def breaker_level(self) -> BreakerLevel:
        return self._breaker


def _default_filters(mark: Decimal) -> LegFilters:
    """没有真实合约规格时的保守兜底。

    真跑起来必须从 fetch_instruments 拿真值——这里的值只保证不崩，
    不保证下单不被拒（精度不对交易所会直接 -1111）。
    """
    step = Decimal("0.001") if mark > 100 else Decimal("1")
    return LegFilters(step_size=step, min_qty=step,
                      min_notional=Decimal("5"), contract_size=step)
