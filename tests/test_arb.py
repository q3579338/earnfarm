"""双腿溢价套利计算器的测试。纯函数层，一个网络请求都没有。

守的线（每一条都对应一次真金白银的错法）：

1. **等名义额对冲下，板块同涨同跌的盈亏必须恒为 0。** 这是套利的定义性质，
   也是这个模块存在的理由：用户上一轮亏的 3 万多 U 全是板块方向的钱。
   对照组同样要钉死——等股数对冲在同涨同跌时**不**为零，那多出来的
   (1+溢价) 倍名义额就是伪装成对冲的裸单。
2. **资金费符号四种组合逐一验。** 正费率 = 多头付给空头；空腿遇负费率要
   付钱。符号搞反会让"每天净收"变成"每天净付"，而界面上看不出来。
   两腿周期不同（8h/4h）时也不能直接相加费率。
3. **盈亏在 P1>P0 与 P1<P0 两侧符号相反，且随方向翻转。** 做空溢价靠溢价
   收窄赚钱，做多溢价反之。
4. **回本 pp 不等于手续费率。** 溢价是比值，(1+P0) 会把 0.2% 放大到
   0.28pp；照 0.2 去算会系统性低估回本难度。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from earnfarm import arb
from earnfarm.premium import (
    LegQuote,
    PremiumPair,
    PremiumSnapshot,
    RefStats,
    compute_premium,
)

# SKHY 的量级：ADR $164.50、韩股腿 $1,164.90、10 份 ADR = 1 股 ⇒ 溢价约 +41.2%
A0 = 164.50
L0 = 1164.90
RATIO = 10.0
N = 10_000.0

PAIR = PremiumPair(key="t", name="测试", adr_symbol="AUSDT",
                   local_symbol="BUSDT", ratio=RATIO,
                   adr_label="ADR 腿", local_label="本地腿")


def _hedge(side: str, notional: float = N) -> arb.HedgePlan:
    return arb.hedge_plan(
        side=side, adr_symbol="AUSDT", local_symbol="BUSDT",
        adr_label="ADR 腿", local_label="本地腿",
        adr_price=A0, local_price=L0, ratio=RATIO, notional_per_leg=notional)


def _leg_cash(hedge: arb.HedgePlan, adr_px: float, local_px: float) -> float:
    """按实际张数逐腿结算的现金盈亏。故意绕开 pnl_fraction——
    用另一条路径算同一件事，公式写错时两边才会打架。"""
    sign_a = -1.0 if hedge.adr.is_short else 1.0
    sign_l = -1.0 if hedge.local.is_short else 1.0
    return (sign_a * hedge.adr.qty * (adr_px - hedge.adr.price)
            + sign_l * hedge.local.qty * (local_px - hedge.local.price))


def _snapshot(adr_rate: float | None = -0.0001,
              local_rate: float | None = 0.0001,
              with_ref: bool = True) -> PremiumSnapshot:
    fair, prem = compute_premium(A0, L0, RATIO)
    ref = RefStats(n=500, mean=38.0, median=38.5, lo=30.0, hi=50.0,
                   percentile=70.0, first_ts=0.0, last_ts=1.0) if with_ref else None
    return PremiumSnapshot(
        pair=PAIR,
        adr=LegQuote(symbol="AUSDT", price=A0, change_24h_pct=1.0,
                     quote_volume_24h=1e6, funding_rate=adr_rate),
        local=LegQuote(symbol="BUSDT", price=L0, change_24h_pct=1.0,
                       quote_volume_24h=1e6, funding_rate=local_rate),
        fair=fair, premium_pct=prem, ref=ref)


# ---- 1) 对冲比例 --------------------------------------------------------

def test_hedge_is_equal_notional_on_both_legs():
    h = _hedge(arb.SHORT_PREMIUM)
    assert h.adr.is_short and not h.local.is_short
    assert abs(h.adr.qty - N / A0) < 1e-12
    assert abs(h.local.qty - N / L0) < 1e-12
    # 等名义额的定义：两腿名义额相等，且都等于输入
    assert abs(h.adr.notional - N) < 1e-9
    assert abs(h.local.notional - N) < 1e-9


def test_hedge_side_flips_both_legs_but_not_the_quantities():
    short, long = _hedge(arb.SHORT_PREMIUM), _hedge(arb.LONG_PREMIUM)
    assert not long.adr.is_short and long.local.is_short
    # 张数只由名义额和价格决定，和方向无关
    assert short.adr.qty == long.adr.qty and short.local.qty == long.local.qty
    assert short.adr.action == "卖出开空" and long.adr.action == "买入开多"


def test_share_equivalent_quantity_exceeds_equal_notional_by_exactly_the_premium():
    """等股数比等名义额多出的那一截，正好是溢价——这就是裸露的部分。"""
    h = _hedge(arb.SHORT_PREMIUM)
    assert abs(h.share_equiv_adr_qty - RATIO * h.local.qty) < 1e-12
    assert abs(h.share_equiv_extra_pct - h.premium_pct) < 1e-9
    assert 41.0 < h.premium_pct < 41.5


def test_hedge_rejects_unknown_side_and_bad_inputs():
    with pytest.raises(ValueError):
        _hedge("空一点点")
    with pytest.raises(ValueError):
        _hedge(arb.SHORT_PREMIUM, notional=0.0)


# ---- 2) 套利的定义性质：板块同涨同跌盈亏为零 ----------------------------

@pytest.mark.parametrize("side", [arb.SHORT_PREMIUM, arb.LONG_PREMIUM])
@pytest.mark.parametrize("k", [1.10, 0.90, 1.50, 0.5])
def test_equal_notional_hedge_is_flat_to_a_common_move(side: str, k: float):
    """两腿同比例涨跌 ⇒ 溢价不变 ⇒ 逐腿结算的现金盈亏恒为 0。

    这是整个模块的立论。它一旦不成立，用户做的就还是裸单。
    """
    h = _hedge(side)
    assert abs(_leg_cash(h, A0 * k, L0 * k)) < 1e-9
    _, p1 = compute_premium(A0 * k, L0 * k, RATIO)
    assert abs(p1 - h.premium_pct) < 1e-9
    assert abs(arb.pnl_fraction(side, h.premium_pct, p1)) < 1e-12


def test_share_equivalent_hedge_is_not_flat_and_that_gap_is_the_naked_part():
    """对照组：等股数对冲在板块同涨时会亏，亏的比例正好是溢价。

    这条测试是上一条的影子——只有把"错的那种对冲会怎样"也钉住，
    才说得清 10% 的板块波动为什么能变成四位数的亏损。
    """
    h = _hedge(arb.SHORT_PREMIUM)
    k = 1.10
    cash = (-h.share_equiv_adr_qty * (A0 * k - A0)
            + h.local.qty * (L0 * k - L0))
    assert cash < 0
    # 多出的空头名义额 = N × 溢价，板块涨 10% 就亏这么多
    assert abs(cash + N * (h.premium_pct / 100.0) * (k - 1.0)) < 1e-9


def test_pnl_fraction_matches_leg_level_cash_when_one_leg_moves():
    """只有一条腿动时，对数公式与逐腿现金账要对得上（差的只是对数口径）。"""
    h = _hedge(arb.SHORT_PREMIUM)
    a1 = A0 * 0.99                          # ADR 跌 1%，本地不动 ⇒ 溢价收窄
    realized = _leg_cash(h, a1, L0)
    _, p1 = compute_premium(a1, L0, RATIO)
    frac = arb.pnl_fraction(arb.SHORT_PREMIUM, h.premium_pct, p1)
    assert realized > 0 and frac > 0
    assert abs(frac * N - realized) / N < 1e-4


# ---- 3) 资金费符号 ------------------------------------------------------

@pytest.mark.parametrize("is_short,rate,expect", [
    (True, 0.0001, +1.0),    # 我空，正费率：多头付给空头 ⇒ 我收
    (True, -0.0001, -1.0),   # 我空，负费率：方向翻转 ⇒ 我付（用户点名的一种）
    (False, 0.0001, -1.0),   # 我多，正费率 ⇒ 我付
    (False, -0.0001, +1.0),  # 我多，负费率 ⇒ 我收
])
def test_leg_funding_sign_all_four_combinations(is_short, rate, expect):
    leg = arb.leg_funding(symbol="X", is_short=is_short, rate=rate,
                          interval_h=8.0, notional=N)
    assert abs(leg.per_settle_usdt - expect) < 1e-12      # 0.01% × 10000 = 1 U
    assert leg.settles_per_day == 3.0
    assert abs(leg.per_day_usdt - expect * 3.0) < 1e-12


def test_funding_plan_assigns_opposite_positions_to_the_two_legs():
    """做空溢价时 ADR 是空腿、本地是多腿——整体方向不能直接套到两条腿上。"""
    short = arb.funding_plan(side=arb.SHORT_PREMIUM, adr_symbol="A",
                             local_symbol="B", adr_rate=0.0001,
                             local_rate=0.0001, notional_per_leg=N)
    assert short.adr.is_short and not short.local.is_short
    assert short.adr.per_settle_usdt > 0 and short.local.per_settle_usdt < 0
    long = arb.funding_plan(side=arb.LONG_PREMIUM, adr_symbol="A",
                            local_symbol="B", adr_rate=0.0001,
                            local_rate=0.0001, notional_per_leg=N)
    assert not long.adr.is_short and long.local.is_short
    assert long.adr.per_settle_usdt < 0 and long.local.per_settle_usdt > 0


def test_funding_normalises_by_interval_not_by_raw_rate():
    """同样的费率，4h 腿一天结算 6 次、8h 腿 3 次——直接相加费率会错一倍。"""
    plan = arb.funding_plan(side=arb.SHORT_PREMIUM, adr_symbol="A",
                            local_symbol="B", adr_rate=-0.0001,
                            local_rate=0.0001, notional_per_leg=N,
                            adr_interval_h=8.0, local_interval_h=4.0)
    # ADR：空腿 + 负费率 ⇒ 每次付 1 U，一天 3 次 ⇒ −3
    assert abs(plan.adr.per_day_usdt + 3.0) < 1e-12
    # 本地：多腿 + 正费率 ⇒ 每次付 1 U，一天 6 次 ⇒ −6
    assert abs(plan.local.per_day_usdt + 6.0) < 1e-12
    assert abs(plan.per_day_usdt + 9.0) < 1e-12
    assert abs(plan.apr + 9.0 / N * 365) < 1e-12

    # 两腿周期互换日净就变——这正是"不能直接加费率"的证据。
    # 两腿费率必须取不等值：等费率时 3+6 和 6+3 恰好抵消，周期的作用会被掩盖，
    # 那样这条测试就抓不到"把两个费率直接相加"这个错法
    def _plan(adr_h: float, local_h: float) -> arb.FundingPlan:
        return arb.funding_plan(side=arb.SHORT_PREMIUM, adr_symbol="A",
                                local_symbol="B", adr_rate=-0.0002,
                                local_rate=0.0001, notional_per_leg=N,
                                adr_interval_h=adr_h, local_interval_h=local_h)

    assert abs(_plan(8.0, 4.0).per_day_usdt + 12.0) < 1e-12   # 2×3 + 1×6
    assert abs(_plan(4.0, 8.0).per_day_usdt + 15.0) < 1e-12   # 2×6 + 1×3


def test_funding_missing_rate_counts_as_zero_and_is_flagged():
    plan = arb.funding_plan(side=arb.SHORT_PREMIUM, adr_symbol="A",
                            local_symbol="B", adr_rate=None,
                            local_rate=0.0001, notional_per_leg=N)
    assert not plan.complete
    assert plan.adr.per_settle_usdt == 0.0
    assert abs(plan.per_day_usdt + 6.0) < 1e-12


@pytest.mark.parametrize("hour,default,expect", [
    (4, 8.0, 4.0),      # 04:00 不可能是 8h 周期 ⇒ 推翻默认值
    (20, 8.0, 4.0),
    (8, 8.0, 8.0),      # 08:00 三种周期都可能 ⇒ 老实用默认值
    (0, 4.0, 4.0),
    (16, 1.0, 1.0),     # 默认值更短时不往长了猜
])
def test_infer_funding_interval_from_next_settlement(hour, default, expect):
    ts = datetime(2026, 8, 17, hour, tzinfo=timezone.utc).timestamp()
    assert arb.infer_funding_interval_h(ts, default) == expect


def test_infer_funding_interval_falls_back_when_unusable():
    assert arb.infer_funding_interval_h(None, 8.0) == 8.0
    half = datetime(2026, 8, 17, 4, 30, tzinfo=timezone.utc).timestamp()
    assert arb.infer_funding_interval_h(half, 8.0) == 8.0


# ---- 4) 盈亏情景表 ------------------------------------------------------

def test_pnl_sign_on_both_sides_of_entry_for_short_premium():
    """做空溢价：溢价跌了赚、涨了亏。"""
    assert arb.pnl_fraction(arb.SHORT_PREMIUM, 40.0, 35.0) > 0
    assert arb.pnl_fraction(arb.SHORT_PREMIUM, 40.0, 45.0) < 0
    flat = arb.pnl_fraction(arb.SHORT_PREMIUM, 40.0, 40.0)
    assert flat == 0.0
    # 负零会在界面上印成 "-0.00%"，看着像微亏——打平就该印成 0
    assert math.copysign(1.0, flat) > 0


def test_pnl_sign_on_both_sides_of_entry_for_long_premium():
    assert arb.pnl_fraction(arb.LONG_PREMIUM, 40.0, 45.0) > 0
    assert arb.pnl_fraction(arb.LONG_PREMIUM, 40.0, 35.0) < 0
    # 同一段行情，两个方向的盈亏互为相反数
    assert abs(arb.pnl_fraction(arb.LONG_PREMIUM, 40.0, 45.0)
               + arb.pnl_fraction(arb.SHORT_PREMIUM, 40.0, 45.0)) < 1e-15


def test_scenario_table_rows_signs_and_ordering():
    rows = arb.scenario_table(side=arb.SHORT_PREMIUM, entry_premium_pct=40.0,
                              current_premium_pct=40.0, notional_per_leg=N,
                              ref_lo=30.0, ref_hi=50.0, ref_mean=38.0)
    levels = [r.premium_pct for r in rows]
    assert levels == sorted(levels, reverse=True)        # 与溢价梯子同序
    by = {round(r.premium_pct, 2): r for r in rows}
    assert set(by) >= {30.0, 35.0, 38.0, 40.0, 42.0, 45.0, 50.0}
    assert by[42.0].pnl_usdt < 0 and by[45.0].pnl_usdt < by[42.0].pnl_usdt
    assert by[38.0].pnl_usdt > 0 and by[35.0].pnl_usdt > by[38.0].pnl_usdt
    assert by[40.0].pnl_usdt == 0.0
    # 占名义额的 % 与 USDT 同源
    for r in rows:
        assert abs(r.pnl_pct / 100.0 * N - r.pnl_usdt) < 1e-9


def test_scenario_table_marks_current_and_entry_rows():
    rows = arb.scenario_table(side=arb.SHORT_PREMIUM, entry_premium_pct=35.0,
                              current_premium_pct=41.2, notional_per_leg=N)
    current = [r for r in rows if r.is_current]
    entry = [r for r in rows if r.is_entry]
    assert len(current) == 1 and abs(current[0].premium_pct - 41.2) < 1e-9
    assert "当前" in current[0].label
    assert len(entry) == 1 and entry[0].pnl_usdt == 0.0
    # 进场 ≠ 当前时，两行是分开的（这正是"假如我在 X% 进场"的用法）
    assert current[0] is not entry[0]
    assert current[0].pnl_usdt < 0          # 空溢价而溢价还在涨 ⇒ 浮亏


def test_scenario_table_merges_labels_when_levels_collide():
    rows = arb.scenario_table(side=arb.SHORT_PREMIUM, entry_premium_pct=40.0,
                              current_premium_pct=40.0, notional_per_leg=N,
                              ref_lo=30.0, ref_hi=50.0, ref_mean=38.0)
    entry = next(r for r in rows if r.is_entry)
    assert entry.label == "进场·当前"                    # 同一档只出现一行
    mean_row = next(r for r in rows if abs(r.premium_pct - 38.0) < 1e-9)
    assert "历史均值" in mean_row.label and "进场 -2pp" in mean_row.label
    assert len({round(r.premium_pct, 2) for r in rows}) == len(rows)


def test_scenario_table_without_ref_still_gives_the_offset_ladder():
    rows = arb.scenario_table(side=arb.LONG_PREMIUM, entry_premium_pct=40.0,
                              current_premium_pct=40.0, notional_per_leg=N)
    assert len(rows) == 7                                # 进场 + 三组 ±
    assert all("历史" not in r.label for r in rows)


# ---- 5) 成本与回本 ------------------------------------------------------

def test_roundtrip_fee_is_four_taker_fills():
    cost = arb.cost_plan(side=arb.SHORT_PREMIUM, entry_premium_pct=40.0,
                         notional_per_leg=N)
    assert cost.fills == 4
    assert abs(cost.fee_frac - 0.002) < 1e-15            # 4 × 0.05%
    assert abs(cost.fee_usdt - 20.0) < 1e-12


def test_breakeven_pp_is_bigger_than_the_fee_rate_because_premium_is_a_ratio():
    """P0=40% 时回本要走 0.28pp，不是 0.20pp——(1+P0) 会把它放大。"""
    cost = arb.cost_plan(side=arb.SHORT_PREMIUM, entry_premium_pct=40.0,
                         notional_per_leg=N)
    assert cost.breakeven_pp < 0                         # 空溢价靠溢价下跌回本
    expected = (1.4 * (math.exp(-0.002) - 1.0)) * 100.0
    assert abs(cost.breakeven_pp - expected) < 1e-12
    assert 0.279 < abs(cost.breakeven_pp) < 0.281
    assert abs(cost.breakeven_premium_pct - (40.0 + cost.breakeven_pp)) < 1e-12


def test_breakeven_point_exactly_offsets_the_fee():
    """打平点的定义：走到那里赚的正好等于手续费。两个方向都要成立。"""
    for side in (arb.SHORT_PREMIUM, arb.LONG_PREMIUM):
        cost = arb.cost_plan(side=side, entry_premium_pct=40.0,
                             notional_per_leg=N)
        frac = arb.pnl_fraction(side, 40.0, cost.breakeven_premium_pct)
        assert abs(frac - cost.fee_frac) < 1e-12
        assert abs(frac * N - cost.fee_usdt) < 1e-9
    long_cost = arb.cost_plan(side=arb.LONG_PREMIUM, entry_premium_pct=40.0,
                              notional_per_leg=N)
    assert long_cost.breakeven_pp > 0                    # 多溢价靠溢价走阔回本


def test_breakeven_pp_scales_with_the_entry_level():
    """同样 0.2% 的成本，溢价越高需要走的 pp 越多。"""
    low = arb.cost_plan(side=arb.SHORT_PREMIUM, entry_premium_pct=0.0,
                        notional_per_leg=N)
    high = arb.cost_plan(side=arb.SHORT_PREMIUM, entry_premium_pct=40.0,
                         notional_per_leg=N)
    assert abs(high.breakeven_pp) > abs(low.breakeven_pp)
    assert abs(abs(low.breakeven_pp) - 0.1998) < 1e-3


def test_funding_days_to_cover_only_when_funding_is_positive():
    cost = arb.cost_plan(side=arb.SHORT_PREMIUM, entry_premium_pct=40.0,
                         notional_per_leg=N, funding_per_day_usdt=10.0)
    assert abs(cost.funding_days_to_cover - 2.0) < 1e-12
    for bad in (None, 0.0, -5.0):
        c = arb.cost_plan(side=arb.SHORT_PREMIUM, entry_premium_pct=40.0,
                          notional_per_leg=N, funding_per_day_usdt=bad)
        assert c.funding_days_to_cover is None


def test_custom_taker_fee_flows_through():
    cost = arb.cost_plan(side=arb.SHORT_PREMIUM, entry_premium_pct=40.0,
                         notional_per_leg=N, taker_fee=0.0002)
    assert abs(cost.fee_usdt - 8.0) < 1e-12


# ---- 装配 ---------------------------------------------------------------

def test_build_from_snapshot_defaults_entry_to_current_premium():
    calc = arb.build_from_snapshot(_snapshot(), side=arb.SHORT_PREMIUM)
    assert abs(calc.entry_premium_pct - calc.current_premium_pct) < 1e-12
    assert calc.notional_per_leg == arb.DEFAULT_NOTIONAL
    assert calc.side_label.startswith("做空溢价")
    assert calc.hedge.adr.is_short and not calc.hedge.local.is_short
    # 历史三档来自 RefStats，必须在情景表里
    labels = {part for r in calc.scenarios for part in r.label.split("·")}
    assert {"历史低", "历史高", "历史均值"} <= labels
    assert any(r.is_current for r in calc.scenarios)


def test_build_from_snapshot_honours_a_hypothetical_entry():
    calc = arb.build_from_snapshot(_snapshot(), side=arb.SHORT_PREMIUM,
                                   notional_per_leg=25_000.0,
                                   entry_premium_pct=30.0)
    assert calc.entry_premium_pct == 30.0
    assert calc.hedge.notional_per_leg == 25_000.0
    assert abs(calc.cost.fee_usdt - 50.0) < 1e-12
    # 在 30% 进场、现价 41%：做空溢价此刻是浮亏
    current = next(r for r in calc.scenarios if r.is_current)
    assert current.pnl_usdt < 0


def test_build_from_snapshot_without_ref_or_funding_still_works():
    calc = arb.build_from_snapshot(
        _snapshot(adr_rate=None, local_rate=None, with_ref=False),
        side=arb.LONG_PREMIUM)
    assert not calc.funding.complete
    assert calc.funding.per_day_usdt == 0.0
    assert calc.cost.funding_days_to_cover is None
    assert all("历史" not in r.label for r in calc.scenarios)


def test_arb_module_never_imports_nicegui():
    """算术层要能脱离界面被测——和 premium.py 同一条纪律。

    看 AST 的 import 语句而不是搜字符串：注释里正要写"这层不 import nicegui"，
    搜字符串会被自己的注释绊倒。
    """
    import ast
    import inspect

    imported: list[str] = []
    for node in ast.walk(ast.parse(inspect.getsource(arb))):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert imported and not any(n.startswith("nicegui") for n in imported)
