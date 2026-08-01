"""纸上交易网关测试。

分三层：
1. 撮合诚实性 —— IOC 吃不完就是吃不完、均价必须逐档加权、reduce_only 会被拒
2. 故障注入 —— 超时/拒单/盘口冻结/查仓失灵，都是真实交易所平时测不到的坏情况
3. 端到端 —— 拿真的 Executor 跑完建仓和撤退两条路径，断言最终净敞口为 0

第 3 层才是这个网关存在的理由：executor 里"撤退阶梯""补腿判死"那半边代码，
没有故障注入就永远是死代码。
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from carryfarm.executor import Executor, LegSpec, State, Unit
from carryfarm.paper import DepthProfile, PaperError, PaperGateway
from carryfarm.safety import LegFilters, LegHealth

FILT = LegFilters(step_size=Decimal("0.001"), min_qty=Decimal("0.001"),
                  min_notional=Decimal("5"), contract_size=Decimal("0.001"))
MARK = Decimal("100")
HARD_M, HARD_S = "gate:perp", "PEPE_USDT"
EASY_M, EASY_S = "binance:perp", "PEPEUSDT"
M, S = "binance:perp", "BTCUSDT"


class FakeClock:
    """可推进的时钟。冻结/资金费这类跟时间有关的东西不能靠 sleep 去测。"""

    def __init__(self, t0: float = 1_700_000_000.0) -> None:
        self.t = t0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def run(coro):
    return asyncio.run(coro)


def D(x) -> Decimal:
    return Decimal(str(x))


def make_gw(**kw) -> PaperGateway:
    kw.setdefault("clock", FakeClock())
    return PaperGateway(**kw)


def make_unit(target="10") -> Unit:
    return Unit(
        uid="u" * 12, hedge_id="h1",
        hard=LegSpec(HARD_M, HARD_S, "buy", FILT),
        easy=LegSpec(EASY_M, EASY_S, "sell", FILT),
        target_qty=Decimal(target), mark=MARK, equity=100_000.0,
    )


def wire_books(gw: PaperGateway, *, easy_bids=None) -> None:
    """两条腿的盘口。难腿深度充足，易腿深度由测试指定。"""
    gw.set_ladder(HARD_M, HARD_S,
                  bids=[("99.98", "50"), ("99.90", "200")],
                  asks=[("100.02", "50"), ("100.10", "200")])
    gw.set_ladder(EASY_M, EASY_S,
                  bids=easy_bids or [("99.97", "50"), ("99.00", "200")],
                  asks=[("100.03", "50"), ("101.00", "200")])


# ---- 1. 撮合诚实性 ------------------------------------------------------

def test_ioc_only_fills_reachable_levels():
    """IOC 限价单吃不完的部分就是不成交。这才是真实行为。

    好好先生式的模拟器会把 5 个全填上，于是滑点这项最大的成本凭空消失。
    """
    gw = make_gw()
    gw.set_ladder(M, S,
                  bids=[("99.90", "10")],
                  asks=[("100.00", "1"), ("100.05", "2"), ("100.20", "3")])
    r = run(gw.place(M, S, "buy", D("5"), D("100.10"), reduce_only=False,
                     client_order_id="c1", tif="IOC"))
    assert r.filled == D("3"), "限价 100.10 够不着 100.20 那一档"
    assert r.status == "partial"


def test_ioc_avg_price_is_level_weighted_not_mid():
    """均价必须是逐档加权出来的，不能拿中间价充数。"""
    gw = make_gw()
    gw.set_ladder(M, S,
                  bids=[("99.90", "10")],
                  asks=[("100.00", "1"), ("100.05", "2"), ("100.20", "3")])
    r = run(gw.place(M, S, "buy", D("5"), D("100.10"), reduce_only=False,
                     client_order_id="c1", tif="IOC"))
    # 1×100.00 + 2×100.05 = 300.10，成交 3
    expected = D("300.10") / D("3")
    assert abs(r.avg_price - expected) < D("1e-18")
    assert r.avg_price > D("100.00"), "吃到第二档就必须比第一档贵"
    mid = (D("99.90") + D("100.00")) / 2
    assert r.avg_price != mid
    assert gw.orders[0].levels_eaten == 2


def test_market_order_eats_visible_depth_only():
    """市价单也只能吃可见深度，深度不够就是部分成交。"""
    gw = make_gw()
    gw.set_ladder(M, S, bids=[("99.90", "10")], asks=[("100.00", "2"), ("100.50", "1")])
    r = run(gw.place(M, S, "buy", D("9"), None, reduce_only=False,
                     client_order_id="c1", tif="MARKET"))
    assert r.filled == D("3")
    assert r.status == "partial"
    assert r.avg_price == (D("2") * D("100.00") + D("1") * D("100.50")) / D("3")


def test_sell_side_walks_bids_downward():
    gw = make_gw()
    gw.set_ladder(M, S, bids=[("100.00", "1"), ("99.95", "2"), ("99.50", "5")],
                  asks=[("100.10", "10")])
    r = run(gw.place(M, S, "sell", D("6"), D("99.90"), reduce_only=False,
                     client_order_id="c1", tif="IOC"))
    assert r.filled == D("3"), "限价 99.90 够不着 99.50 那一档"
    assert r.avg_price == (D("100.00") + D("2") * D("99.95")) / D("3")


def test_ioc_with_no_reachable_level_produces_nothing():
    gw = make_gw()
    gw.set_ladder(M, S, bids=[("99.90", "10")], asks=[("100.00", "1")])
    r = run(gw.place(M, S, "buy", D("1"), D("99.00"), reduce_only=False,
                     client_order_id="c1", tif="IOC"))
    assert r.filled == 0
    assert r.status == "rejected"
    assert run(gw.position_qty(M, S)) == 0


def test_reduce_only_rejected_when_flat():
    """仓位为 0 还下 reduce_only —— 币安 -2022 那一类。"""
    gw = make_gw()
    gw.set_ladder(M, S, bids=[("99.90", "10")], asks=[("100.10", "10")])
    r = run(gw.place(M, S, "sell", D("1"), D("99.00"), reduce_only=True,
                     client_order_id="c1", tif="IOC"))
    assert r.status == "rejected"
    assert "-2022" in r.error
    assert r.filled == 0


def test_reduce_only_cannot_increase_position():
    gw = make_gw()
    gw.set_ladder(M, S, bids=[("99.90", "10")], asks=[("100.10", "10")])
    gw.set_position(M, S, D("5"), D("100"))
    r = run(gw.place(M, S, "buy", D("1"), D("101"), reduce_only=True,
                     client_order_id="c1", tif="IOC"))
    assert r.status == "rejected"
    assert run(gw.position_qty(M, S)) == D("5"), "被拒的单不许动仓位"


def test_reduce_only_truncates_to_remaining_position():
    """交易所会把 reduce_only 单截断到剩余仓位，不会让你平过头开出反向仓。"""
    gw = make_gw()
    gw.set_ladder(M, S, bids=[("99.90", "100")], asks=[("100.10", "100")])
    gw.set_position(M, S, D("3"), D("100"))
    r = run(gw.place(M, S, "sell", D("10"), D("99.00"), reduce_only=True,
                     client_order_id="c1", tif="IOC"))
    assert r.filled == D("3")
    assert run(gw.position_qty(M, S)) == 0


# ---- 记账 ---------------------------------------------------------------

def test_taker_fee_charged_on_notional():
    gw = make_gw(initial_cash=D("100000"), default_taker=D("0.0005"))
    gw.set_ladder(M, S, bids=[("99.90", "10")],
                  asks=[("100.00", "1"), ("100.05", "2"), ("100.20", "3")])
    run(gw.place(M, S, "buy", D("5"), D("100.10"), reduce_only=False,
                 client_order_id="c1", tif="IOC"))
    fee = D("300.10") * D("0.0005")
    assert gw.orders[0].fee == fee
    assert gw.cash == D("100000") - fee, "手续费从现金里扣"
    assert gw.report().fees == fee


def test_realized_pnl_on_close():
    gw = make_gw(default_taker=D("0"))
    gw.set_ladder(M, S, bids=[("99.90", "100")], asks=[("100.00", "100")])
    run(gw.place(M, S, "buy", D("10"), D("100.00"), reduce_only=False,
                 client_order_id="c1", tif="IOC"))
    gw.set_ladder(M, S, bids=[("110.00", "100")], asks=[("110.10", "100")])
    run(gw.place(M, S, "sell", D("10"), D("109.00"), reduce_only=True,
                 client_order_id="c2", tif="IOC"))
    assert run(gw.position_qty(M, S)) == 0
    assert gw.report().realized_pnl == D("100"), "10 × (110 - 100)"


def test_funding_long_pays_short_receives():
    """符号约定：正费率 = 多头付给空头（models.CarryRate）。"""
    clock = FakeClock()
    gw = PaperGateway(clock=clock, default_taker=D("0"))
    gw.set_ladder(M, S, bids=[("99.90", "100")], asks=[("100.10", "100")])
    run(gw.book(M, S))                       # 建立可见盘口，标记价才有值
    gw.set_position(M, S, D("10"), D("100"))
    gw.set_funding_rate(M, S, D("0.0001"), interval_s=28800)

    clock.advance(28800)                     # 整整一个结算周期
    got = gw.accrue_funding()
    # mid = 100.00，名义 1000，费率 1bp → 多头付 0.1
    assert got == D("-0.1")
    assert gw.report().funding == D("-0.1")

    gw.set_position(M, S, D("-10"), D("100"))
    clock.advance(14400)                     # 半个周期
    assert gw.accrue_funding() == D("0.05"), "空头按持仓时长收半个周期"


def test_funding_needs_no_double_count():
    """连续两次计提之间没走时间就不该再扣一次。"""
    clock = FakeClock()
    gw = PaperGateway(clock=clock)
    gw.set_ladder(M, S, bids=[("99.90", "100")], asks=[("100.10", "100")])
    run(gw.book(M, S))
    gw.set_position(M, S, D("10"), D("100"))
    gw.set_funding_rate(M, S, D("0.0001"))
    clock.advance(28800)
    first = gw.accrue_funding()
    second = gw.accrue_funding()
    assert first == D("-0.1") and second == 0


def test_report_equity_identity_holds():
    """报表的硬约束：权益 = 初始资金 + 已实现 - 手续费 + 资金费 + 浮盈。
    对不上就是记账写错了。"""
    clock = FakeClock()
    gw = PaperGateway(clock=clock, initial_cash=D("50000"), default_taker=D("0.0004"))
    gw.set_ladder(M, S, bids=[("99.90", "100")], asks=[("100.10", "100")])
    run(gw.place(M, S, "buy", D("10"), D("100.50"), reduce_only=False,
                 client_order_id="c1", tif="IOC"))
    gw.set_funding_rate(M, S, D("0.0002"))
    clock.advance(28800)
    gw.accrue_funding()
    rep = gw.report()
    expect = (rep.initial_cash + rep.realized_pnl - rep.fees
              + rep.funding + rep.unrealized)
    assert rep.equity == expect


# ---- 2. 故障注入 --------------------------------------------------------

def test_inject_reject():
    gw = make_gw()
    gw.set_ladder(M, S, bids=[("99.90", "10")], asks=[("100.10", "10")])
    gw.inject_reject(M, 2, "合约维护中")
    r1 = run(gw.place(M, S, "buy", D("1"), D("101"), reduce_only=False,
                      client_order_id="c1", tif="IOC"))
    r2 = run(gw.place(M, S, "buy", D("1"), D("101"), reduce_only=False,
                      client_order_id="c2", tif="IOC"))
    r3 = run(gw.place(M, S, "buy", D("1"), D("101"), reduce_only=False,
                      client_order_id="c3", tif="IOC"))
    assert r1.status == r2.status == "rejected"
    assert "合约维护中" in r1.error
    assert r3.status == "filled", "注入次数用完就恢复正常"


def test_inject_timeout_masks_a_real_fill():
    """超时不等于失败：交易所那边真的成交了，只是回执丢了。

    这是最危险的一种，简单重发直接变双倍仓位。
    """
    gw = make_gw()
    gw.set_ladder(M, S, bids=[("99.90", "10")], asks=[("100.00", "10")])
    gw.inject_timeout(M, 1)
    r = run(gw.place(M, S, "buy", D("4"), D("100.10"), reduce_only=False,
                     client_order_id="c1", tif="IOC"))
    assert r.is_unknown and r.filled == 0, "上层只该看到 unknown"
    assert run(gw.position_qty(M, S)) == D("4"), "交易所侧其实成交了"
    assert run(gw.resolve_unknown(M, S, "c1")) == D("4"), "查单能问出真实成交量"


def test_inject_timeout_without_fill():
    gw = make_gw()
    gw.set_ladder(M, S, bids=[("99.90", "10")], asks=[("100.00", "10")])
    gw.inject_timeout(M, 1, actually_fills=False)
    r = run(gw.place(M, S, "buy", D("4"), D("100.10"), reduce_only=False,
                     client_order_id="c1", tif="IOC"))
    assert r.is_unknown
    assert run(gw.position_qty(M, S)) == 0
    assert run(gw.resolve_unknown(M, S, "c1")) == 0


def test_resolve_unknown_of_never_seen_order_is_zero():
    """交易所不认识这个 cid → 它从未被接受 → 成交量确定为 0。"""
    gw = make_gw()
    assert run(gw.resolve_unknown(M, S, "never-existed")) == 0


def test_inject_resolve_failure_returns_none():
    gw = make_gw()
    gw.inject_resolve_failure(M, 1)
    assert run(gw.resolve_unknown(M, S, "c1")) is None
    assert run(gw.resolve_unknown(M, S, "c1")) is not None


def test_duplicate_client_order_id_does_not_match_twice():
    """幂等：同一个 cid 重发返回原结果，不会撮合第二次。"""
    gw = make_gw()
    gw.set_ladder(M, S, bids=[("99.90", "100")], asks=[("100.00", "100")])
    a = run(gw.place(M, S, "buy", D("4"), D("100.10"), reduce_only=False,
                     client_order_id="c1", tif="IOC"))
    b = run(gw.place(M, S, "buy", D("4"), D("100.10"), reduce_only=False,
                     client_order_id="c1", tif="IOC"))
    assert a.filled == b.filled == D("4")
    assert run(gw.position_qty(M, S)) == D("4"), "重发不许造成双倍仓位"


def test_book_freeze_makes_quote_stale():
    clock = FakeClock()
    gw = PaperGateway(clock=clock)
    gw.set_ladder(M, S, bids=[("99.90", "10")], asks=[("100.10", "10")])
    t1 = run(gw.book_top(M, S))
    clock.advance(3.0)
    t2 = run(gw.book_top(M, S))
    assert t2.ts_ms > t1.ts_ms, "没冻结时行情是活的"
    assert gw.quote_age_ms(M, S) == 0

    gw.inject_book_freeze(M, 10.0)
    clock.advance(2.5)
    t3 = run(gw.book_top(M, S))
    assert t3.ts_ms == t2.ts_ms, "冻结后时间戳不再前进"
    assert gw.quote_age_ms(M, S) == 2500
    assert gw.is_quote_stale(M, S, max_age_ms=1000)
    assert not t3.is_fresh(gw.now_ms(), expected_interval_ms=250, grace_ms=500)


def test_book_freeze_blocks_feed_updates():
    """冻结 = 行情通道死了，连数据源的更新也不许渗进来。
    否则测不出"拿着陈旧报价下单"这个最贵的坑。"""
    clock = FakeClock()
    gw = PaperGateway(clock=clock)
    gw.set_ladder(M, S, bids=[("99.90", "10")], asks=[("100.10", "10")])
    run(gw.book(M, S))
    gw.inject_book_freeze(M, 10.0)
    gw.set_ladder(M, S, bids=[("80.00", "10")], asks=[("80.10", "10")])   # 价格崩了
    bid, _, ask, _ = run(gw.book(M, S))
    assert (bid, ask) == (D("99.90"), D("100.10")), "冻结期间看到的还是旧价"

    # 而且成交按冻结住的那本盘口撮合 —— 该亏就亏，不做任何保护
    r = run(gw.place(M, S, "buy", D("1"), D("100.20"), reduce_only=False,
                     client_order_id="c1", tif="IOC"))
    assert r.avg_price == D("100.10")

    clock.advance(11.0)
    bid2, _, _, _ = run(gw.book(M, S))
    assert bid2 == D("80.00"), "冻结解除后恢复正常"


def test_inject_position_blind():
    gw = make_gw()
    gw.inject_position_blind(M, 1)
    with pytest.raises(PaperError):
        run(gw.position_qty(M, S))
    assert run(gw.position_qty(M, S)) == 0


def test_inject_latency_is_actually_awaited():
    gw = make_gw()
    gw.set_ladder(M, S, bids=[("99.90", "10")], asks=[("100.10", "10")])
    gw.inject_latency(30)

    async def timed():
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await gw.place(M, S, "buy", D("1"), D("101"), reduce_only=False,
                       client_order_id="c1", tif="IOC")
        return loop.time() - t0

    assert run(timed()) >= 0.025


def test_set_leg_health_drives_repair_verdict_inputs():
    gw = make_gw()
    assert run(gw.leg_health(M, S)).symbol_trading is True
    gw.set_leg_health(M, LegHealth(symbol_trading=False))
    assert run(gw.leg_health(M, S)).symbol_trading is False
    # symbol 级比 market 级更具体
    gw.set_leg_health(M, LegHealth(exchange_reduce_only=True), symbol=S)
    h = run(gw.leg_health(M, S))
    assert h.exchange_reduce_only and h.symbol_trading


# ---- 行情来源 -----------------------------------------------------------

def test_real_adapter_feeds_the_book():
    """盘口从真实适配器拉，只有成交是模拟的。"""
    from carryfarm.models import BookTop

    class StubAdapter:
        def __init__(self):
            self.calls = 0

        async def fetch_book_top(self, symbol):
            self.calls += 1
            return BookTop(D("99.50"), D("7"), D("100.50"), D("9"), 1234)

    class StubRegistry:
        def __init__(self, a):
            self.a = a

        def get(self, market):
            if market != M:
                raise KeyError(market)
            return self.a

    adapter = StubAdapter()
    gw = make_gw(registry=StubRegistry(adapter))
    bid, bid_qty, ask, ask_qty = run(gw.book(M, S))
    assert (bid, bid_qty, ask, ask_qty) == (D("99.50"), D("7"), D("100.50"), D("9"))
    assert adapter.calls == 1
    lad = run(gw.ladder(M, S))
    assert len(lad.asks) > 1, "深档由 DepthProfile 外推出来"
    assert lad.asks[1][0] > lad.asks[0][0]
    assert lad.synthetic_from == 1, "第 1 档是真数据，其余是模型"


def test_consume_depth_actually_depletes_the_level():
    """打开 consume_depth 后，吃掉的档位不会凭空长回来。

    默认关是因为接真实行情时每 TTL 会重拉、扣了也马上被覆盖；
    但要模拟"自己把盘口吃穿"就必须打开，否则同一档能被反复吃。
    """
    gw = make_gw(consume_depth=True)
    gw.set_ladder(M, S, bids=[("99.90", "5")], asks=[("100.00", "5")])
    r1 = run(gw.place(M, S, "sell", D("5"), D("99.00"), reduce_only=False,
                      client_order_id="c1", tif="IOC"))
    assert r1.filled == D("5")
    r2 = run(gw.place(M, S, "sell", D("5"), D("99.00"), reduce_only=False,
                      client_order_id="c2", tif="IOC"))
    assert r2.filled == 0, "这一档已经被自己吃光了"
    assert r2.status == "rejected"


def test_default_book_replenishes_between_orders():
    """默认（consume_depth=False）盘口每次都从数据源重建——
    这是"行情会刷新"的模型，不是"深度无限"的施舍：限价够不着照样吃不到。"""
    gw = make_gw()
    gw.set_ladder(M, S, bids=[("99.90", "5")], asks=[("100.00", "5")])
    a = run(gw.place(M, S, "sell", D("5"), D("99.00"), reduce_only=False,
                     client_order_id="c1", tif="IOC"))
    b = run(gw.place(M, S, "sell", D("5"), D("99.00"), reduce_only=False,
                     client_order_id="c2", tif="IOC"))
    c = run(gw.place(M, S, "sell", D("5"), D("99.95"), reduce_only=False,
                     client_order_id="c3", tif="IOC"))
    assert a.filled == b.filled == D("5")
    assert c.filled == 0, "限价高于买一，照样一口吃不到"


def test_satisfies_order_gateway_protocol():
    """结构上必须能直接顶替真实网关塞进 Executor。"""
    import inspect

    from carryfarm.executor import OrderGateway

    gw = make_gw()
    for name in ("place", "resolve_unknown", "position_qty", "book", "leg_health"):
        assert hasattr(OrderGateway, name)
        fn = getattr(gw, name, None)
        assert fn is not None and inspect.iscoroutinefunction(fn), name
        # 参数名必须对得上——executor 全部用关键字传 reduce_only/client_order_id/tif
        proto = inspect.signature(getattr(OrderGateway, name))
        assert (list(inspect.signature(fn).parameters)
                == [p for p in proto.parameters if p != "self"]), name


def test_missing_book_is_a_loud_error():
    gw = make_gw()
    with pytest.raises(PaperError):
        run(gw.book("nowhere:perp", "NOPE"))


def test_depth_profile_levels_are_monotonic():
    gw = make_gw(depth=DepthProfile(levels=5, step_bp=Decimal("0.01"),
                                    qty_growth=Decimal("1")))
    gw.set_book(M, S, "99.90", "1", "100.10", "1")
    lad = run(gw.ladder(M, S))
    # step_bp 小于一个 tick 时也必须强制单调，否则"逐档"变成"同一档"
    assert [p for p, _ in lad.asks] == sorted(p for p, _ in lad.asks)
    assert len({p for p, _ in lad.asks}) == len(lad.asks)
    assert [p for p, _ in lad.bids] == sorted((p for p, _ in lad.bids), reverse=True)


# ---- 持久化 -------------------------------------------------------------

def test_state_survives_restart(tmp_path):
    clock = FakeClock()
    gw = PaperGateway(clock=clock, initial_cash=D("50000"), default_taker=D("0.0004"))
    gw.set_ladder(M, S, bids=[("99.90", "100")], asks=[("100.10", "100")])
    run(gw.place(M, S, "buy", D("10"), D("100.50"), reduce_only=False,
                 client_order_id="c1", tif="IOC"))
    gw.set_funding_rate(M, S, D("0.0002"))
    clock.advance(28800)
    gw.accrue_funding()
    before = gw.report()

    path = tmp_path / "paper.db"
    gw.save(path)
    gw2 = PaperGateway.restore(path, clock=clock)
    after = gw2.report()

    assert after.cash == before.cash
    assert after.realized_pnl == before.realized_pnl
    assert after.fees == before.fees
    assert after.funding == before.funding
    assert run(gw2.position_qty(M, S)) == D("10")
    assert run(gw2.resolve_unknown(M, S, "c1")) == D("10"), "重启后仍能消解旧订单"
    assert [o.client_order_id for o in gw2.orders] == ["c1"]
    # 恢复的盘口能继续撮合，且没忘记哪几档是真数据
    assert run(gw2.ladder(M, S)).synthetic_from == run(gw.ladder(M, S)).synthetic_from
    r = run(gw2.place(M, S, "sell", D("10"), D("99.00"), reduce_only=True,
                      client_order_id="c2", tif="IOC"))
    assert r.filled == D("10")


def test_report_text_renders():
    gw = make_gw()
    gw.set_ladder(M, S, bids=[("99.90", "100")], asks=[("100.10", "100")])
    run(gw.place(M, S, "buy", D("1"), D("101"), reduce_only=False,
                 client_order_id="c1", tif="IOC"))
    text = gw.report().to_text()
    assert "纸上交易对账报表" in text and S in text
    assert gw.report().to_dict()["orders"] == 1


# ---- 3. 端到端：真 Executor + 纸上网关 ----------------------------------

def test_e2e_open_reaches_balanced():
    """完整建仓：PREFLIGHT → 难腿 → 易腿 → BALANCED，最终净敞口为 0。"""
    gw = make_gw()
    wire_books(gw)
    ex = Executor(gw)
    u = make_unit("10")

    run(ex.step(u))                       # IDLE -> PREFLIGHT
    run(ex.step(u))                       # 闸门 -> 下难腿 -> EXPOSED
    assert u.state is State.EXPOSED
    assert gw.orders[0].market == HARD_M, "串行难腿优先"
    assert u.hard_filled == D("10")

    run(ex.step(u))                       # 补易腿 -> BALANCED
    assert u.state is State.BALANCED
    assert u.net_qty() == 0

    assert run(gw.position_qty(HARD_M, HARD_S)) == D("10")
    assert run(gw.position_qty(EASY_M, EASY_S)) == D("-10")
    assert abs(gw.net_exposure_usd()) < D("0.01"), "两腿净敞口必须归零"

    rep = gw.report()
    assert rep.orders == 2 and rep.fills == 2 and rep.rejects == 0
    assert rep.fees > 0, "吃单就要付 taker 费"
    # 均价必须是真实吃到的档位价，不是中间价
    assert gw.orders[0].avg_price == D("100.02")
    assert gw.orders[1].avg_price == D("99.97")


def test_e2e_second_leg_qty_equals_first_leg_actual_fill():
    """难腿只吃到一部分时，易腿必须按实际成交量下单。"""
    gw = make_gw()
    gw.set_ladder(HARD_M, HARD_S, bids=[("99.98", "50")],
                  asks=[("100.02", "4"), ("101.00", "100")])
    gw.set_ladder(EASY_M, EASY_S, bids=[("99.97", "50")], asks=[("100.03", "50")])
    ex = Executor(gw)
    u = make_unit("10")
    run(ex.step(u)); run(ex.step(u))
    assert u.hard_filled == D("4"), "只有 4 个够得着"
    run(ex.step(u))
    assert gw.orders[1].req_qty == D("4")
    assert u.state is State.BALANCED and u.net_qty() == 0


def test_e2e_timeout_resolves_through_order_unknown():
    """下单超时 → ORDER_UNKNOWN → 消解 → 按真实成交量记账，全程只下一笔单。"""
    gw = make_gw()
    wire_books(gw)
    gw.inject_timeout(HARD_M, 1)
    ex = Executor(gw)
    u = make_unit("10")

    run(ex.step(u)); run(ex.step(u))
    assert u.state is State.ORDER_UNKNOWN
    assert u.hard_filled == 0, "未消解前不许凭空记账"
    assert run(gw.position_qty(HARD_M, HARD_S)) == D("10"), "交易所侧其实成交了"

    run(ex.step(u))                       # 消解
    assert u.hard_filled == D("10"), "公理 1：仓位是真值"
    assert u.state is State.REPAIR
    assert not u.inflight
    assert len(gw.orders) == 1, "未知状态下绝不能重发，否则双倍仓位"

    # 单腿裸奔必然是 CRITICAL：跳过重试直接撤退，一路平到 FLAT
    for _ in range(6):
        run(ex.step(u))
        if u.state is State.FLAT:
            break
    assert u.state is State.FLAT
    assert u.net_qty() == 0
    assert run(gw.position_qty(HARD_M, HARD_S)) == 0
    assert abs(gw.net_exposure_usd()) < D("0.01")
    assert all(o.reduce_only for o in gw.orders[1:]), "撤退单必须带 reduce_only"


def test_e2e_unresolvable_order_freezes():
    """交易所连订单带仓位都读不出来 → FROZEN，不下任何单。"""
    gw = make_gw()
    wire_books(gw)
    gw.inject_timeout(HARD_M, 1)
    gw.inject_resolve_failure(HARD_M, 5)
    gw.inject_position_blind(HARD_M, 5)
    ex = Executor(gw)
    u = make_unit("10")

    run(ex.step(u)); run(ex.step(u)); run(ex.step(u))
    assert u.state is State.FROZEN
    before = len(gw.orders)
    run(ex.step(u))
    assert u.state is State.FROZEN, "看不见仓位就一直冻着"
    assert len(gw.orders) == before, "FROZEN 状态不许下任何单"


def test_e2e_hopeless_repair_unwinds_to_flat():
    """故障场景：易腿只补上一部分且随后判死 → 撤退 → FLAT，净敞口 0。

    这条路径在真实交易所上几乎不可能主动复现，而它恰恰是原版工具
    完全缺失的那一半（它会永远重试补腿，用户就一直裸单边）。
    """
    gw = make_gw()
    # 易腿浅档只有 9.6 个够得着，剩下的价差太远吃不到
    wire_books(gw, easy_bids=[("99.97", "9.6"), ("99.00", "200")])
    events: list[tuple[str, str, str]] = []
    ex = Executor(gw, on_event=lambda lvl, cat, msg: events.append((lvl, cat, msg)))
    u = make_unit("10")

    run(ex.step(u)); run(ex.step(u))
    assert u.hard_filled == D("10")
    run(ex.step(u))
    assert u.easy_filled == D("9.6"), "IOC 吃不完就是吃不完"
    assert u.state is State.REPAIR
    assert u.net_qty() == D("0.4")

    # 易腿合约突然停牌：硬判据命中，一次重试都不该做
    gw.set_leg_health(EASY_M, LegHealth(symbol_trading=False))
    orders_before = len(gw.orders)
    run(ex.step(u))
    assert u.state is State.UNWIND
    assert len(gw.orders) == orders_before, "判死后不许再补腿"
    assert any(lvl == "warn" and "补腿判死" in msg for lvl, _, msg in events)

    for _ in range(6):
        run(ex.step(u))
        if u.state is State.FLAT:
            break
    assert u.state is State.FLAT
    assert u.net_qty() == 0

    # 最终净敞口为 0：难腿被削到 9.6，与易腿的 -9.6 完全抵消
    assert run(gw.position_qty(HARD_M, HARD_S)) == D("9.6")
    assert run(gw.position_qty(EASY_M, EASY_S)) == D("-9.6")
    assert abs(gw.net_exposure_usd()) < D("0.01")

    unwind_orders = [o for o in gw.orders if o.reduce_only]
    assert unwind_orders, "撤退必须真的下过 reduce_only 单"
    assert all(o.market == HARD_M for o in unwind_orders), "该平的是成交过的那条腿"


def test_e2e_unwind_survives_a_rejected_close():
    """撤退单被拒不等于放弃：阶梯会升级，最终仍要把敞口清掉。"""
    gw = make_gw()
    wire_books(gw, easy_bids=[("99.97", "9.6"), ("99.00", "200")])
    ex = Executor(gw)
    u = make_unit("10")
    run(ex.step(u)); run(ex.step(u)); run(ex.step(u))
    gw.set_leg_health(EASY_M, LegHealth(symbol_trading=False))
    run(ex.step(u))
    assert u.state is State.UNWIND

    gw.inject_reject(HARD_M, 2, "-1013 撮合引擎繁忙")
    for _ in range(8):
        run(ex.step(u))
        if u.state is State.FLAT:
            break
    assert u.state is State.FLAT
    assert u.net_qty() == 0
    assert abs(gw.net_exposure_usd()) < D("0.01")
    assert sum(1 for o in gw.orders if o.status == "rejected") == 2


def test_e2e_funding_accrues_over_a_held_hedge():
    """建完仓后持有一个结算周期：两腿资金费净额就是这单的收益来源。"""
    clock = FakeClock()
    gw = PaperGateway(clock=clock, default_taker=D("0.0004"))
    wire_books(gw)
    ex = Executor(gw)
    u = make_unit("10")
    run(ex.step(u)); run(ex.step(u)); run(ex.step(u))
    assert u.state is State.BALANCED

    # 多腿费率 -5bp（多头收钱），空腿 +8bp（空头收钱）—— 典型的正 carry
    gw.set_funding_rate(HARD_M, HARD_S, D("-0.0005"), interval_s=28800)
    gw.set_funding_rate(EASY_M, EASY_S, D("0.0008"), interval_s=28800)
    clock.advance(28800)
    total = gw.accrue_funding()

    assert total > 0, "正 carry 持有一个周期该是净收"
    rep = gw.report()
    assert rep.funding == total
    hard = next(l for l in rep.legs if l.market == HARD_M)
    easy = next(l for l in rep.legs if l.market == EASY_M)
    assert hard.funding > 0 and easy.funding > 0
    assert abs(rep.net_exposure_usd) < D("0.01"), "收资金费的同时仍然中性"
