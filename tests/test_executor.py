"""执行器状态机测试。

用假网关驱动，覆盖每一条原版工具会出事的路径：
超时重发导致双倍仓位、一腿裸奔无限重试、撤退时把方向做反、
平仓单没带 reduce_only 导致开出反向仓。
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from earnfarm.executor import (
    Executor,
    LegSpec,
    PlacedOrder,
    State,
    Unit,
)
from earnfarm.safety import BreakerLevel, LegFilters, LegHealth

FILT = LegFilters(step_size=Decimal("0.001"), min_qty=Decimal("0.001"),
                  min_notional=Decimal("5"), contract_size=Decimal("0.001"))
MARK = Decimal("100")


class FakeGateway:
    """可编程的假网关。记录每一次下单，方便断言。"""

    def __init__(self) -> None:
        self.orders: list[dict] = []
        self.fill_ratio = Decimal("1")       # 成交比例
        self.unknown_on: set[str] = set()    # 哪些 tag 会返回 unknown
        self.reject_on: set[str] = set()
        self.resolved: Decimal | None = Decimal("0")
        self.health = LegHealth(est_repair_cost=1.0, est_unwind_cost=10.0)
        self.book_data = (Decimal("99.9"), Decimal("1000"),
                          Decimal("100.1"), Decimal("1000"))
        self.position = Decimal("0")

    async def place(self, market, symbol, side, qty, price, *, reduce_only,
                    client_order_id, tif):
        rec = dict(market=market, symbol=symbol, side=side, qty=qty, price=price,
                   reduce_only=reduce_only, cid=client_order_id, tif=tif)
        self.orders.append(rec)
        marker = client_order_id[8:12]       # tag 编在 cid 里
        if any(m in client_order_id for m in self.unknown_on):
            return PlacedOrder(client_order_id, Decimal("0"), Decimal("0"), "unknown")
        if any(m in client_order_id for m in self.reject_on):
            reason = ("-2022 ReduceOnly Order is rejected: 当前无仓位"
                      if reduce_only and self.position == Decimal("0")
                      else "insufficient liquidity")
            return PlacedOrder(client_order_id, Decimal("0"), Decimal("0"),
                               "rejected", reason)
        filled = qty * self.fill_ratio
        return PlacedOrder(client_order_id, filled, price or Decimal("100"), "filled")

    async def resolve_unknown(self, market, symbol, client_order_id):
        return self.resolved

    async def position_qty(self, market, symbol):
        return self.position

    async def book(self, market, symbol):
        return self.book_data

    async def leg_health(self, market, symbol):
        return self.health


def make_unit(target="10") -> Unit:
    return Unit(
        uid="u" * 12, hedge_id="h1",
        hard=LegSpec("gate:perp", "PEPE_USDT", "buy", FILT),
        easy=LegSpec("binance:perp", "PEPEUSDT", "sell", FILT),
        target_qty=Decimal(target), mark=MARK, equity=100_000.0,
    )


def run(coro):
    return asyncio.run(coro)


# ---- 正常路径 -----------------------------------------------------------

def test_happy_path_reaches_balanced():
    gw = FakeGateway()
    ex = Executor(gw)
    u = make_unit()
    run(ex.step(u))          # IDLE -> PREFLIGHT
    run(ex.step(u))          # PREFLIGHT -> 下难腿 -> EXPOSED
    assert u.state is State.EXPOSED
    run(ex.step(u))          # EXPOSED -> 补易腿 -> BALANCED
    assert u.state is State.BALANCED
    assert abs(u.net_qty()) < Decimal("0.01")


def test_hard_leg_goes_first():
    """串行难腿优先：第一笔单必须打在难腿的市场上。"""
    gw = FakeGateway()
    ex = Executor(gw)
    u = make_unit()
    run(ex.step(u)); run(ex.step(u))
    assert gw.orders[0]["market"] == "gate:perp"


def test_second_leg_uses_actual_fill_not_target():
    """难腿只成交一半时，易腿必须按实际成交量下单——不是目标量。

    原版按目标量各下各的，一腿部分成交就必然错配。
    """
    gw = FakeGateway()
    gw.fill_ratio = Decimal("0.5")
    ex = Executor(gw)
    u = make_unit("10")
    run(ex.step(u)); run(ex.step(u))
    assert u.hard_filled == Decimal("5")
    run(ex.step(u))
    easy_order = gw.orders[1]
    assert easy_order["qty"] == Decimal("5"), "易腿数量必须等于难腿实际成交量"


def test_hard_leg_no_fill_exits_with_zero_exposure():
    """难腿没成 → 零敞口，安全退出。串行的核心好处。"""
    gw = FakeGateway()
    gw.fill_ratio = Decimal("0")
    ex = Executor(gw)
    u = make_unit()
    run(ex.step(u)); run(ex.step(u))
    assert u.state is State.IDLE
    assert u.net_qty() == Decimal("0")
    assert len(gw.orders) == 1, "难腿没成就不该去打易腿"


# ---- 超时与未知订单 -----------------------------------------------------

def test_timeout_goes_to_order_unknown_not_retry():
    """超时不等于失败。直接重发会造成双倍仓位。"""
    gw = FakeGateway()
    gw.unknown_on = {"hard"}
    ex = Executor(gw)
    u = make_unit()
    run(ex.step(u)); run(ex.step(u))
    assert u.state is State.ORDER_UNKNOWN
    assert len(gw.orders) == 1, "未知状态下绝不能重发"


def test_unknown_resolution_applies_actual_fill():
    """消解后按交易所的真实成交量记账——公理 1：仓位是真值。"""
    gw = FakeGateway()
    gw.unknown_on = {"hard"}
    gw.resolved = Decimal("7")           # 其实成交了 7
    ex = Executor(gw)
    u = make_unit("10")
    run(ex.step(u)); run(ex.step(u))
    assert u.state is State.ORDER_UNKNOWN
    run(ex.step(u))
    assert u.hard_filled == Decimal("7")
    assert u.state is State.REPAIR       # 有敞口，去补腿
    assert not u.inflight


def test_unresolvable_order_freezes():
    """交易所不可读时进 FROZEN——在盲的时候下单是最糟的选择。"""
    gw = FakeGateway()
    gw.unknown_on = {"hard"}
    gw.resolved = None
    ex = Executor(gw)
    u = make_unit()
    run(ex.step(u)); run(ex.step(u)); run(ex.step(u))
    assert u.state is State.FROZEN
    before = len(gw.orders)
    run(ex.step(u))
    assert len(gw.orders) == before, "FROZEN 状态不许下任何单"


# ---- 补腿与撤退 ---------------------------------------------------------

def test_repair_gives_up_on_hard_verdict():
    """合约下架这类硬判据 → 立刻撤退，不做无谓重试。

    原版会永远重试补腿而绝不撤退，用户就一直裸单边。
    """
    gw = FakeGateway()
    gw.reject_on = {"easy"}
    ex = Executor(gw)
    u = make_unit()
    run(ex.step(u)); run(ex.step(u)); run(ex.step(u))
    assert u.state is State.REPAIR
    gw.health = LegHealth(symbol_trading=False)      # 合约停牌
    run(ex.step(u))
    assert u.state is State.UNWIND, "硬判据命中必须立刻转撤退"


def test_repair_has_a_terminal_condition():
    """收敛条件必须包含"放弃"这个终态——不能无限重试。"""
    gw = FakeGateway()
    gw.reject_on = {"easy"}
    ex = Executor(gw)
    u = make_unit()
    run(ex.step(u)); run(ex.step(u)); run(ex.step(u))
    u.repair_attempts = 99                            # 预算耗尽
    run(ex.step(u))
    assert u.state is State.UNWIND


def test_unwind_orders_are_always_reduce_only():
    """撤退里最阴的坑：仓位若已被 ADL 清掉，普通单会开出反向仓。"""
    gw = FakeGateway()
    gw.reject_on = {"easy"}
    ex = Executor(gw)
    u = make_unit()
    run(ex.step(u)); run(ex.step(u)); run(ex.step(u))
    gw.health = LegHealth(symbol_trading=False)
    run(ex.step(u))
    gw.reject_on = set()
    run(ex.step(u))
    unwind_orders = [o for o in gw.orders if o["reduce_only"]]
    assert unwind_orders, "撤退必须下 reduce_only 单"
    assert all(o["reduce_only"] for o in gw.orders[3:])


def test_unwind_settles_inflight_before_closing():
    """Phase -1 强制：不先终结在途订单就去平仓，
    迟到的成交回执会把裸奔方向反过来、量翻倍。"""
    gw = FakeGateway()
    ex = Executor(gw)
    u = make_unit()
    u.hard_filled = Decimal("10")
    u.inflight = {"cid-easy-1": "easy"}
    gw.resolved = Decimal("10")          # 易腿其实成交了
    u.goto(State.UNWIND)
    run(ex.step(u))
    assert not u.inflight, "必须先终结在途订单"
    assert u.easy_filled == Decimal("10")
    # 敞口已被迟到的成交抵消 → 不该再平任何仓
    assert not [o for o in gw.orders if o["reduce_only"]]
    assert u.state is State.FLAT


def test_unwind_completes_to_flat():
    gw = FakeGateway()
    ex = Executor(gw)
    u = make_unit()
    u.hard_filled = Decimal("10")
    u.unwind_started = 0.0
    u.goto(State.UNWIND)
    for _ in range(5):
        run(ex.step(u))
        if u.state is State.FLAT:
            break
    assert u.state is State.FLAT


# ---- 闸门 ---------------------------------------------------------------

def test_breaker_blocks_opening():
    gw = FakeGateway()
    ex = Executor(gw, breaker=lambda: BreakerLevel.SOFT)
    u = make_unit()
    run(ex.step(u)); run(ex.step(u))
    assert u.state is State.IDLE
    assert not gw.orders, "熔断时不许开新仓"


def test_gate_blocks_when_market_unhealthy():
    gw = FakeGateway()
    gw.health = LegHealth(symbol_trading=False)
    ex = Executor(gw)
    u = make_unit()
    run(ex.step(u)); run(ex.step(u))
    assert u.state is State.IDLE
    assert not gw.orders


# ---- 价格 ---------------------------------------------------------------

def test_cross_price_direction_and_tick():
    tick = Decimal("0.01")
    buy = Executor.cross_price(Decimal("99.9"), Decimal("100.1"), "buy", 10, tick)
    sell = Executor.cross_price(Decimal("99.9"), Decimal("100.1"), "sell", 10, tick)
    assert buy > Decimal("100.1"), "买单要穿卖一"
    assert sell < Decimal("99.9"), "卖单要穿买一"
    # 取整方向要保住可成交性
    assert buy % tick == 0 and sell % tick == 0


def test_cross_price_widens_with_bp():
    tick = Decimal("0.01")
    near = Executor.cross_price(Decimal("99.9"), Decimal("100.1"), "buy", 5, tick)
    far = Executor.cross_price(Decimal("99.9"), Decimal("100.1"), "buy", 40, tick)
    assert far > near


# ---- 幂等 ---------------------------------------------------------------

def test_client_order_ids_are_unique():
    gw = FakeGateway()
    gw.reject_on = {"easy"}
    ex = Executor(gw)
    u = make_unit()
    run(ex.step(u)); run(ex.step(u)); run(ex.step(u))
    for _ in range(4):
        run(ex.step(u))
    cids = [o["cid"] for o in gw.orders]
    assert len(cids) == len(set(cids)), "client_order_id 必须唯一，否则撞 ID 被拒"


# ---- 撤退记账（回归测试） -----------------------------------------------

def test_unwind_fill_reduces_magnitude_not_adds():
    """撤退成交必须**减少**绝对持仓量。

    回归测试：曾经按买卖方向记账（平空是买 → 加），
    结果平掉 20 个空头后本地记成 40 个空头，与交易所当场分家，
    随后 reduce_only 被拒并无限重试。
    """
    gw = FakeGateway()
    ex = Executor(gw)
    u = make_unit()
    u.hard_filled = Decimal("20")      # 已有 20 个空头（hard 是 sell 腿）
    u.unwind_started = 0.0
    u.goto(State.UNWIND)
    run(ex.step(u))
    assert u.hard_filled < Decimal("20"), "平仓应该减少绝对量，不是增加"


def test_reduce_only_rejection_triggers_reconcile():
    """reduceOnly 被拒且交易所说没仓位 → 以交易所为准对账，不能重下。

    公理 1：仓位是真值。ADL/强平会在我们不知情时清掉仓位。
    """
    gw = FakeGateway()
    gw.reject_on = {"hard"}
    gw.position = Decimal("0")         # 交易所侧其实已经没仓位了
    ex = Executor(gw)
    u = make_unit()
    u.hard_filled = Decimal("20")
    u.unwind_started = 0.0
    u.goto(State.UNWIND)
    run(ex.step(u))
    assert u.hard_filled == Decimal("0"), "本地应被交易所持仓掰回来"
    assert u.state is State.FLAT
