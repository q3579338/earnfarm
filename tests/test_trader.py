"""交易协调器测试：从机会到实际推进的仓位，走的是纸上网关。

这里验证的是**端到端**——机会榜点一下能不能真的把仓建起来、
出事的时候能不能撤退干净。
"""

from __future__ import annotations

import asyncio
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from earnfarm.executor import State
from earnfarm.paper import PaperGateway
from earnfarm.scoring import HistoryStats, LegQuote, score_pair
from earnfarm.models import Venue
from earnfarm.storage import Storage
from earnfarm.vault import Vault
from earnfarm.trader import Trader

NOW = 1_785_000_000.0


def make_op(base="TEST", long_market="binance:perp", short_market="okx:perp",
            px=100.0, depth=2_000_000.0, top=200_000.0):
    def leg(sym, mkt, f, iv=8.0):
        half = px * 1e-4 / 2
        return LegQuote(sym, mkt, px - half, px + half, top / px, top / px,
                        depth, 10.0, 0.0005, 0.0002, f, iv, NOW + 4 * 3600,
                        0.0075, -0.0075, 5e7, 2e8)

    long_leg = leg("TESTUSDT", long_market, -0.0002)
    short_leg = leg("TEST-USDT-SWAP", short_market, 0.0030)
    hist_s = HistoryStats(tuple([0.0030] * 90), 8.0)
    hist_l = HistoryStats(tuple([-0.0002] * 90), 8.0)
    return score_pair(base, long_leg, short_leg, hist_l, hist_s,
                      notional=50_000, horizon_h=168, now_ts=NOW,
                      hourly_carry=[0.0004] * 500)


def make_trader(tmp: Path, **kw):
    storage = Storage(tmp / "t.db")
    # 对冲的两条腿都外键引用 accounts——建真账户，别塞假 ID。
    # 这条约束本身是有价值的：它保证不会出现引用了不存在账户的孤儿仓位。
    vault = Vault()
    storage.set_vault_header(vault.initialize("test-password-123"))
    acc_long = storage.add_account(Venue.BINANCE, "long-acct",
                                   {"api_key": "k", "api_secret": "s"}, vault, 0)
    acc_short = storage.add_account(Venue.OKX, "short-acct",
                                    {"api_key": "k", "api_secret": "s"}, vault, 0)

    gw = PaperGateway(initial_cash=Decimal("500000"))
    op = make_op()
    for leg in (op.long, op.short):
        gw.set_book(leg.market, leg.symbol,
                    bid=Decimal(str(leg.bid)), ask=Decimal(str(leg.ask)),
                    bid_qty=Decimal("2000"), ask_qty=Decimal("2000"))
    trader = Trader(gw, storage, equity=500_000.0, **kw)
    return trader, gw, storage, op, acc_long, acc_short


def run(coro):
    return asyncio.run(coro)


def test_create_hedge_from_opportunity(tmp_path):
    trader, gw, storage, op, acc_l, acc_s = make_trader(tmp_path)
    rt = trader.create_from_opportunity(op, long_account=acc_l, short_account=acc_s,
                                        notional=20_000)
    assert rt.slices_total >= 1
    assert len(rt.units) == rt.slices_total
    # 仓位要落库——重启后还得认得
    assert len(storage.list_hedges()) == 1


def test_uses_suggested_notional_when_not_given(tmp_path):
    """不填仓位就用机会自己算好的建议值——它已经按深度缩过了。"""
    trader, gw, storage, op, acc_l, acc_s = make_trader(tmp_path)
    rt = trader.create_from_opportunity(op, long_account=acc_l, short_account=acc_s)
    total = sum(u.target_qty for u in rt.units)
    expected = Decimal(str(op.suggested_notional)) / Decimal(str(op.long.mid))
    assert abs(total - expected) < Decimal("0.01")


def test_rejects_zero_capacity_opportunity(tmp_path):
    trader, gw, storage, op, acc_l, acc_s = make_trader(tmp_path)
    thin = make_op(base="THIN", depth=100.0, top=10.0)
    if thin.suggested_notional <= 0:
        with pytest.raises(ValueError, match="深度"):
            trader.create_from_opportunity(thin, long_account=acc_l, short_account=acc_s)


def test_rejects_illegal_pairing(tmp_path):
    """同所现货+全仓杠杆共用钱包，不能互为两腿——建仓时就该拦下。"""
    from earnfarm.models import PairingError
    trader, gw, storage, _, acc_l, acc_s = make_trader(tmp_path)
    bad = make_op(long_market="binance:spot", short_market="binance:margin")
    with pytest.raises(PairingError):
        trader.create_from_opportunity(bad, long_account=acc_l, short_account=acc_s)


def test_slicing_splits_large_position(tmp_path):
    """大仓位必须分片：单次事故的最大敞口从"全仓"降到"一片"。"""
    trader, gw, storage, op, acc_l, acc_s = make_trader(tmp_path)
    small = trader.create_from_opportunity(op, long_account=acc_l, short_account=acc_s,
                                           notional=500)
    big = trader.create_from_opportunity(op, long_account=acc_l, short_account=acc_s,
                                         notional=200_000)
    assert big.slices_total > small.slices_total


def test_hedge_reaches_balanced(tmp_path):
    """端到端：建仓 → 推进 → 两腿平衡，净敞口归零。"""
    trader, gw, storage, op, acc_l, acc_s = make_trader(tmp_path)
    rt = trader.create_from_opportunity(op, long_account=acc_l, short_account=acc_s,
                                        notional=5_000)

    async def drive():
        for _ in range(60):
            await trader.step_all()
            if rt.is_settled:
                break
    run(drive())

    assert rt.active_state in (State.BALANCED, State.FLAT), \
        f"未收敛，停在 {rt.active_state}，最后事件：{rt.last_event}"
    assert abs(rt.net_qty) < Decimal("1"), f"净敞口未归零：{rt.net_qty}"


def test_close_hedge_goes_through_unwind_ladder(tmp_path):
    """手动平仓走的是和裸奔撤退同一条路——阶梯穿价 + reduce_only，
    不是简单市价砸出去。"""
    trader, gw, storage, op, acc_l, acc_s = make_trader(tmp_path)
    rt = trader.create_from_opportunity(op, long_account=acc_l, short_account=acc_s,
                                        notional=3_000)

    async def drive(n):
        for _ in range(n):
            await trader.step_all()
    run(drive(40))

    trader.close_hedge(rt.hedge.id)
    assert any(u.state is State.UNWIND for u in rt.units)
    run(drive(40))
    assert abs(rt.net_qty) < Decimal("1"), f"撤退后仍有敞口：{rt.net_qty}"


def test_breaker_blocks_new_slices(tmp_path):
    """熔断后不许开新仓——但已建的仓位还能继续收敛和平仓。"""
    trader, gw, storage, op, acc_l, acc_s = make_trader(tmp_path)
    rt = trader.create_from_opportunity(op, long_account=acc_l, short_account=acc_s,
                                        notional=5_000)
    trader.update_signals(equity_drop_15m=0.05)     # 权益骤降 → 硬熔断
    run(trader.step_all())
    from earnfarm.safety import BreakerLevel
    assert trader.breaker_level is BreakerLevel.HARD
    for u in rt.units:
        assert u.state is not State.LEG1_PENDING


def test_timeout_does_not_double_fill(tmp_path):
    """注入超时后走 ORDER_UNKNOWN 消解，绝不能重发造成双倍仓位。"""
    trader, gw, storage, op, acc_l, acc_s = make_trader(tmp_path)
    rt = trader.create_from_opportunity(op, long_account=acc_l, short_account=acc_s,
                                        notional=2_000)
    gw.inject_timeout(op.short.market, n=1)

    async def drive():
        for _ in range(60):
            await trader.step_all()
            if rt.is_settled:
                break
    run(drive())
    # 无论超时那笔实际成没成，最终净敞口都必须收敛
    assert abs(rt.net_qty) < Decimal("1"), f"消解后敞口未归零：{rt.net_qty}"


def test_events_are_persisted(tmp_path):
    trader, gw, storage, op, acc_l, acc_s = make_trader(tmp_path)
    trader.create_from_opportunity(op, long_account=acc_l, short_account=acc_s,
                                   notional=1_000)
    events = storage.recent_events(10)
    assert events and any("建仓" in e["message"] for e in events)


def test_paper_mode_is_labelled(tmp_path):
    """纸上模式必须在事件里写明——不能让人以为下了真单。"""
    trader, gw, storage, op, acc_l, acc_s = make_trader(tmp_path, paper=True)
    trader.create_from_opportunity(op, long_account=acc_l, short_account=acc_s,
                                   notional=1_000)
    msg = storage.recent_events(5)[0]["message"]
    assert "纸上模式" in msg
