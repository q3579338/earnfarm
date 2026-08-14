"""安全决策层测试。

每个测试对应一条原版工具踩过的坑或设计文档里的一条硬性要求。
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from earnfarm.safety import (
    DEFAULT_PARAMS,
    BreakerLevel,
    BreakerSignals,
    EntryMode,
    ExposureLevel,
    LegFilters,
    LegHardness,
    LegHealth,
    RepairVerdict,
    RiskTier,
    UnwindPhase,
    choose_entry_mode,
    choose_leverage,
    classify_exposure,
    dust_threshold_usd,
    evaluate_breaker,
    floor_to_step,
    liquidation_distance,
    normalize_margin_ratio,
    repair_backoff_ms,
    repair_verdict,
    risk_tier,
    second_leg_qty,
    slice_max_usd,
    survival_actions_allowed,
    unwind_step,
)

EQUITY = 100_000.0
MARK = Decimal("100")
ZERO_D = Decimal("0")

# 细粒度合约（现货式，一份=0.001 个币）→ dust 门槛 7.5 USD
FILT = LegFilters(step_size=Decimal("0.001"), min_qty=Decimal("0.001"),
                  min_notional=Decimal("5"), contract_size=Decimal("0.001"))
# 粗粒度合约（一张=10 个币）→ dust 门槛 3000 USD，结构性残差大得多
COARSE = LegFilters(step_size=Decimal("1"), min_qty=Decimal("1"),
                    min_notional=Decimal("10"), contract_size=Decimal("10"))


# ---- 敞口分级 -----------------------------------------------------------

def test_dust_derived_from_filters_not_hardcoded():
    """dust 门槛必须由交易所过滤器推导。

    两所步长差异会产生结构性残差，拍脑袋定阈值会让状态机陷入
    「补 → 因低于 minQty 被拒 → 再补」的死循环。
    """
    fine = dust_threshold_usd(FILT, FILT, MARK)
    coarse = dust_threshold_usd(FILT, COARSE, MARK)
    assert coarse > fine
    # 粗步长那家：max(step, contract)=10 → 3 * 10 * 100 = 3000
    assert coarse == pytest.approx(3000.0)


def test_dust_exposure_triggers_nothing():
    """结构性残差不该触发任何动作。"""
    snap = classify_exposure(Decimal("10"), Decimal("-9.999"), MARK, EQUITY, FILT, FILT)
    assert snap.level is ExposureLevel.DUST
    assert "不可消除" in snap.reason


def test_exposure_levels_escalate():
    balanced = classify_exposure(Decimal("100"), Decimal("-100"), MARK, EQUITY, FILT, FILT)
    assert balanced.level is ExposureLevel.DUST

    # gross = 100*100 = 10000；soft = min(0.4%*1e5, 3%*1e4) = min(400, 300) = 300
    soft = classify_exposure(Decimal("100"), Decimal("-98"), MARK, EQUITY, FILT, FILT)
    assert soft.level is ExposureLevel.SOFT       # naked = 200 < 300

    hard = classify_exposure(Decimal("100"), Decimal("-92"), MARK, EQUITY, FILT, FILT)
    assert hard.level is ExposureLevel.HARD       # naked = 800

    crit = classify_exposure(Decimal("100"), Decimal("-60"), MARK, EQUITY, FILT, FILT)
    assert crit.level is ExposureLevel.CRITICAL   # naked = 4000 > crit 2500


def test_time_weighted_exposure_escalates():
    """一直 SOFT 但裸太久，同样要升级——这是纯金额阈值抓不到的。"""
    kw = dict(mark=MARK, equity=EQUITY, filters_long=FILT, filters_short=FILT)
    normal = classify_exposure(Decimal("100"), Decimal("-98"), **kw)
    assert normal.level is ExposureLevel.SOFT
    lingering = classify_exposure(Decimal("100"), Decimal("-98"),
                                  exposure_integral=1e9, **kw)
    assert lingering.level is ExposureLevel.HARD
    assert "裸太久" in lingering.reason


def test_bleeding_naked_leg_is_critical():
    """裸腿浮亏过大直接 CRITICAL：金额没超标但已经在流血。"""
    snap = classify_exposure(Decimal("100"), Decimal("-98"), MARK, EQUITY, FILT, FILT,
                             naked_pnl=-2_000.0)
    assert snap.level is ExposureLevel.CRITICAL


def test_volatility_scales_thresholds():
    """山寨币的 SOFT 和 BTC 的 SOFT 不是一回事。"""
    kw = dict(mark=MARK, equity=EQUITY, filters_long=FILT, filters_short=FILT)
    calm = classify_exposure(Decimal("100"), Decimal("-96"), vol_scale=3.0, **kw)
    wild = classify_exposure(Decimal("100"), Decimal("-96"), vol_scale=0.3, **kw)
    assert calm.level < wild.level


# ---- 补腿无望 -----------------------------------------------------------

def _healthy() -> LegHealth:
    return LegHealth(est_repair_cost=1.0, est_unwind_cost=10.0)


def test_repair_continues_when_healthy():
    v = repair_verdict(ExposureLevel.SOFT, _healthy(), elapsed_s=3, attempts=2,
                       exposure_integral=0, equity=EQUITY)
    assert v is RepairVerdict.CONTINUE
    assert not v.is_hopeless


@pytest.mark.parametrize("field,value,expected", [
    ("symbol_trading", False, RepairVerdict.SYMBOL_NOT_TRADING),
    ("exchange_reduce_only", True, RepairVerdict.EXCHANGE_REDUCE_ONLY),
    ("account_restricted", True, RepairVerdict.ACCOUNT_RESTRICTED),
    ("in_maintenance", True, RepairVerdict.MAINTENANCE),
    ("dead_for_s", 30.0, RepairVerdict.EXCHANGE_DEAD),
])
def test_hard_verdicts_fire_immediately(field, value, expected):
    """硬判据命中即刻判死——重试 100 次也不可能成功，每一秒都是纯风险。"""
    health = replace(_healthy(), **{field: value})
    v = repair_verdict(ExposureLevel.SOFT, health, elapsed_s=0, attempts=0,
                       exposure_integral=0, equity=EQUITY)
    assert v is expected
    assert v.is_hard and v.is_hopeless


def test_insufficient_margin_only_when_cannot_transfer():
    stuck = replace(_healthy(), available_margin=10.0, required_margin=500.0)
    assert repair_verdict(ExposureLevel.SOFT, stuck, elapsed_s=0, attempts=0,
                          exposure_integral=0, equity=EQUITY) is RepairVerdict.INSUFFICIENT_MARGIN
    rescuable = replace(_healthy(), available_margin=10.0, required_margin=500.0,
                        can_transfer_in=True)
    assert repair_verdict(ExposureLevel.SOFT, rescuable, elapsed_s=0, attempts=0,
                          exposure_integral=0, equity=EQUITY) is RepairVerdict.CONTINUE


def test_uneconomic_repair_gives_up():
    """补腿只有在比撤退便宜时才值得做。流动性枯竭时这条会先于超时触发。"""
    pricey = replace(_healthy(), est_repair_cost=100.0, est_unwind_cost=10.0)
    v = repair_verdict(ExposureLevel.SOFT, pricey, elapsed_s=1, attempts=1,
                       exposure_integral=0, equity=EQUITY)
    assert v is RepairVerdict.UNECONOMIC


def test_hard_level_has_shorter_budget():
    """敞口越大，容忍窗口越短。"""
    h = _healthy()
    assert repair_verdict(ExposureLevel.SOFT, h, elapsed_s=10, attempts=0,
                          exposure_integral=0, equity=EQUITY) is RepairVerdict.CONTINUE
    assert repair_verdict(ExposureLevel.HARD, h, elapsed_s=10, attempts=0,
                          exposure_integral=0, equity=EQUITY) is RepairVerdict.TIMEOUT


def test_critical_skips_repair_entirely():
    v = repair_verdict(ExposureLevel.CRITICAL, _healthy(), elapsed_s=0, attempts=0,
                       exposure_integral=0, equity=EQUITY)
    assert v.is_hopeless


def test_backoff_is_bounded():
    assert repair_backoff_ms(0) == 200
    assert repair_backoff_ms(99) == DEFAULT_PARAMS.repair_backoff_ms[-1]


# ---- 撤退阶梯 -----------------------------------------------------------

def test_unwind_escalates_over_time():
    assert unwind_step(0.5).phase is UnwindPhase.MARKETABLE
    assert unwind_step(3.0).phase is UnwindPhase.ESCALATE
    assert unwind_step(10.0).phase is UnwindPhase.MARKET
    assert unwind_step(1.0, force_market=True).phase is UnwindPhase.MARKET


def test_unwind_cross_price_widens():
    early = unwind_step(2.5).cross_bp
    late = unwind_step(5.5).cross_bp
    assert early is not None and late is not None and late > early


def test_unwind_always_reduce_only():
    """撤退里最阴的坑：仓位若已被 ADL 清掉，普通单会开出反向仓。"""
    for t in (0.1, 3.0, 10.0, 60.0):
        assert unwind_step(t).reduce_only is True


def test_cross_hedge_after_repeated_market_failure():
    step = unwind_step(20.0, phase2_failures=2)
    assert step.phase is UnwindPhase.CROSS_HEDGE


# ---- 建仓 ---------------------------------------------------------------

def test_defaults_to_sequential():
    """默认串行难腿优先——优化的是期望损失而不是平均窗口。"""
    rough = LegHardness(est_slippage_bp=5, spread_bp=4, reject_rate=0.02,
                        latency_p95_ms=200)
    smooth = LegHardness(est_slippage_bp=1, spread_bp=1, reject_rate=0.001,
                         latency_p95_ms=30)
    assert choose_entry_mode(rough, smooth, 1_000, 0.95, 0.95) is EntryMode.SEQUENTIAL_HARD_FIRST


def test_parallel_only_for_deep_small_reliable():
    smooth = LegHardness(est_slippage_bp=1, spread_bp=1, reject_rate=0.001,
                         latency_p95_ms=30)
    assert choose_entry_mode(smooth, smooth, 1_000, 0.95, 0.95) is EntryMode.PARALLEL
    # 单片太大 → 退回串行
    assert choose_entry_mode(smooth, smooth, 50_000, 0.95, 0.95) is EntryMode.SEQUENTIAL_HARD_FIRST
    # 深度不足 → 退回串行
    assert choose_entry_mode(smooth, smooth, 1_000, 0.5, 0.95) is EntryMode.SEQUENTIAL_HARD_FIRST


def test_hardness_weights_reject_rate_most():
    base = dict(est_slippage_bp=2, spread_bp=2, latency_p95_ms=50)
    low = LegHardness(reject_rate=0.001, **base)
    high = LegHardness(reject_rate=0.05, **base)
    assert high.score() > low.score() * 2


def test_slice_capped_by_depth_and_exposure():
    thin = slice_max_usd(hard_leg_top5_depth_usd=1_000, equity=EQUITY)
    deep = slice_max_usd(hard_leg_top5_depth_usd=10_000_000, equity=EQUITY)
    assert thin < deep
    # 单片全裸也不该超过 HARD 档
    assert deep <= DEFAULT_PARAMS.naked_hard_pct_equity * EQUITY * 0.8 + 1e-9


def test_second_leg_uses_actual_fill_not_target():
    """数量错配在结构上消失：第二腿按第一腿实际成交量下单。"""
    qty, residual = second_leg_qty(Decimal("1.2345"), Decimal("0.001"), Decimal("0.01"))
    assert qty == Decimal("1.23")          # 对齐到公共步长 0.01
    assert residual == Decimal("0.0045")   # 残差要削平，不能裸着


def test_floor_to_step_never_overshoots():
    assert floor_to_step(Decimal("1.999"), Decimal("0.5")) == Decimal("1.5")
    assert floor_to_step(Decimal("-1.999"), Decimal("0.5")) == Decimal("-1.5")
    assert floor_to_step(Decimal("0.3"), Decimal("1")) == ZERO_D


# ---- 风险档与杠杆 -------------------------------------------------------

def test_margin_ratio_normalized_binance_style():
    assert normalize_margin_ratio(10_000, 2_000) == 5.0
    assert normalize_margin_ratio(10_000, 0) is None


def test_liquidation_distance():
    assert liquidation_distance(Decimal("100"), Decimal("80")) == pytest.approx(0.2)
    assert liquidation_distance(Decimal("100"), None) is None


def test_risk_tier_takes_worse_of_two_signals():
    # 保证金率很好但爆仓距离很近 → 仍要报警
    assert risk_tier(margin_ratio=10.0, liq_distance=0.03) is RiskTier.BLACK
    assert risk_tier(margin_ratio=10.0, liq_distance=0.5) is RiskTier.GREEN
    assert risk_tier(margin_ratio=1.3, liq_distance=0.5) is RiskTier.RED


def test_leverage_is_conservative_not_max():
    """原版取档位允许的最高杠杆；我们取够用的最低档。"""
    tiers = [(Decimal("50000"), Decimal("50")), (Decimal("500000"), Decimal("20"))]
    lev = choose_leverage(notional=10_000, equity=100_000, tiers=tiers)
    assert lev < Decimal("50"), "不该直接取档位最高杠杆"
    assert lev >= Decimal("1")


def test_leverage_avoids_tier_edge():
    """贴着档位边界开仓，价格一动就跨档触发降杠杆和追加保证金。"""
    tiers = [(Decimal("10000"), Decimal("50")), (Decimal("500000"), Decimal("20"))]
    lev = choose_leverage(notional=9_900, equity=100_000, tiers=tiers)
    assert lev <= Decimal("20"), "接近第一档上限时应退到下一档"


# ---- 熔断 ---------------------------------------------------------------

def test_equity_drop_is_master_switch():
    """中性仓位不该出现权益骤降——这是对冲失效的最强信号。"""
    d = evaluate_breaker(BreakerSignals(equity_drop_15m=0.05))
    assert d.level is BreakerLevel.HARD
    assert "对冲可能已失效" in d.reason


def test_soft_breaker_on_repeated_unwinds():
    d = evaluate_breaker(BreakerSignals(unwinds_30m=5))
    assert d.level is BreakerLevel.SOFT
    assert d.blocks_opening


def test_clear_when_all_good():
    assert evaluate_breaker(BreakerSignals()).level is BreakerLevel.CLEAR


def test_breaker_never_blocks_survival_actions():
    """公理 3：熔断停的是赚钱动作，不停保命动作。
    一刀切的全局停机在有裸腿时等于自杀。"""
    for level in (BreakerLevel.SOFT, BreakerLevel.HARD):
        allowed = survival_actions_allowed(level)
        assert "open" not in allowed
        assert {"close", "unwind", "delever", "transfer_margin"} <= allowed
