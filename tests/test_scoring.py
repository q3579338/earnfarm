"""评分引擎测试。

金标准场景来自对 qqq.bot 的审计结论：资金费差 3bp 在它的榜单上排第一，
但双边开+双边平四笔 taker 约 20bp，要熬 7 个结算周期才回本。
我们的评分必须把这种机会判成负期望。
"""

from __future__ import annotations

import math
import time

from carryfarm.scoring import (
    HistoryStats,
    LegQuote,
    breakeven_hours,
    forecast_path,
    orient,
    rank,
    roundtrip_cost,
    score_pair,
    settlement_offsets,
    slippage_rate,
    stability,
)

NOW = 1_785_000_000.0


def make_leg(symbol="BTCUSDT", market="binance:perp", price=100_000.0,
             funding=0.0, interval_h=8.0, next_in_h=4.0, taker=0.0005,
             spread_bp=1.0, depth=2_000_000.0, top_size_usd=200_000.0):
    half = price * spread_bp / 1e4 / 2
    bid, ask = price - half, price + half
    return LegQuote(
        symbol=symbol, market=market, bid=bid, ask=ask,
        bid_size=top_size_usd / bid, ask_size=top_size_usd / ask,
        depth_notional=depth, depth_band_bp=10.0,
        taker_fee=taker, maker_fee=0.0002,
        funding_now=funding, funding_interval_h=interval_h,
        next_funding_ts=NOW + next_in_h * 3600,
        funding_cap=0.0075, funding_floor=-0.0075,
        open_interest_usd=50_000_000.0, volume_24h_usd=200_000_000.0,
    )


def test_settlement_counting():
    """持有期比距下次结算还短 → 一次都收不到。这是最关键的边界。"""
    # 下次结算在 4 小时后，只持有 2 小时
    assert settlement_offsets(NOW, NOW + 4 * 3600, 8.0, 2.0) == []
    # 持有 24 小时，8h 周期，4h 后首次 → 4, 12, 20
    offs = settlement_offsets(NOW, NOW + 4 * 3600, 8.0, 24.0)
    assert offs == [4.0, 12.0, 20.0]
    # 陈旧的 next_ts（已过期）要被推进到未来
    offs = settlement_offsets(NOW, NOW - 3 * 3600, 8.0, 10.0)
    assert offs and all(o > 0 for o in offs)


def test_interval_normalization():
    """1h 周期的 2bp 应该被认为远好于 8h 周期的 2bp。"""
    fast = make_leg(funding=0.0002, interval_h=1.0)
    slow = make_leg(funding=0.0002, interval_h=8.0)
    assert fast.hourly_rate == 8 * slow.hourly_rate
    # 定向：小时化费率低的做多
    long, short = orient(fast, slow)
    assert long is slow and short is fast


def test_forecast_decay():
    """AR(1) 必须向历史中位数衰减，且受交易所上下限约束。"""
    path = forecast_path(0.01, 0.001, 0.8, 5, cap=0.0075, floor=-0.0075)
    assert path[0] == 0.0075                    # 被 cap 夹住
    assert path[-1] < path[1]                   # 单调衰减
    assert all(-0.0075 <= v <= 0.0075 for v in path)
    # rho=0 时立刻塌到中位数
    flat = forecast_path(0.01, 0.001, 0.0, 3, None, None)
    assert flat[1] == flat[2] == 0.001


def test_slippage_grows_with_size():
    leg = make_leg(top_size_usd=100_000.0, depth=1_000_000.0)
    small = slippage_rate(leg, "buy", 50_000)
    at_top = slippage_rate(leg, "buy", 100_000)
    big = slippage_rate(leg, "buy", 800_000)
    assert small == at_top          # 一档以内只付半价差
    assert big > at_top             # 超出部分要付穿透冲击


def test_roundtrip_is_four_legs():
    """往返成本必须约等于四笔 taker，不能只算两笔。"""
    long, short = make_leg(taker=0.0005), make_leg(taker=0.0005)
    cost = roundtrip_cost(long, short, 50_000)
    assert cost > 4 * 0.0005                    # 四笔手续费 + 滑点
    assert cost < 4 * 0.0005 + 0.002            # 但不该离谱


def test_golden_case_3bp_is_negative_ev():
    """金标准：3bp 资金费差 + 5bp taker，必须判成负期望。

    这正是原版工具会排在榜首的那类机会。
    """
    # 空腿收 3bp（多头视角为负），多腿 0
    short = make_leg(symbol="X-A", market="binance:perp", funding=0.0003)
    long = make_leg(symbol="X-B", market="okx:perp", funding=0.0)
    hist = HistoryStats(rates=tuple([0.0003] * 60), interval_h=8.0)
    zero = HistoryStats(rates=tuple([0.0] * 60), interval_h=8.0)

    op = score_pair("X", long, short, zero, hist,
                    notional=50_000, horizon_h=24, now_ts=NOW)

    assert op.verdict in ("negative", "marginal"), op.reasons
    assert op.net_apr < op.gross_apr            # 成本确实被扣了
    # 24 小时内最多 3 次结算 × 3bp = 9bp，扛不住约 20bp 成本
    assert op.net_apr < 0, f"净年化应为负，实际 {op.net_apr:.2%}: {op.reasons}"
    assert any("成本" in r or "回本" in r for r in op.reasons)


def test_genuinely_good_opportunity_passes():
    """真正好的机会（大费差 + 低费率 + 深盘口）应该判 good。"""
    short = make_leg(symbol="Y-A", funding=0.0035, taker=0.0002,
                     depth=20_000_000.0, top_size_usd=2_000_000.0)
    long = make_leg(symbol="Y-B", market="okx:perp", funding=-0.0005, taker=0.0002,
                    depth=20_000_000.0, top_size_usd=2_000_000.0)
    hist_s = HistoryStats(rates=tuple([0.0035] * 90), interval_h=8.0)
    hist_l = HistoryStats(rates=tuple([-0.0005] * 90), interval_h=8.0)
    # 稳定的正 carry 小时序列
    hourly = [0.0005] * 400

    op = score_pair("Y", long, short, hist_l, hist_s,
                    notional=50_000, horizon_h=24, now_ts=NOW,
                    hourly_carry=hourly)
    assert op.net_apr > 0, op.reasons
    assert op.verdict == "good", op.reasons
    assert op.capacity_usd > 0


def test_breakeven_infinite_when_no_carry():
    """没有 carry 的配对永不回本。"""
    a, b = make_leg(funding=0.0), make_leg(funding=0.0)
    h = HistoryStats(rates=tuple([0.0] * 30), interval_h=8.0)
    be = breakeven_hours(a, b, h, h, cost_rt=0.002, now_ts=NOW)
    assert math.isinf(be)


def test_stability_punishes_sign_flipping():
    """符号乱翻的费差必须被否决——这是几何均值的意义。"""
    steady = [0.0004] * 300
    flipping = [0.0004 if i % 2 == 0 else -0.0004 for i in range(300)]
    s1 = stability(steady, 24, 0.002, 2.0)
    s2 = stability(flipping, 24, 0.002, 2.0)
    assert s1.score > s2.score * 3, f"steady={s1.score:.3f} flip={s2.score:.3f}"
    assert s2.median_run_h <= 1.5


def test_stability_insufficient_data_uses_prior():
    s = stability([0.0004] * 10, 24, 0.002, 2.0)
    assert s.is_prior and s.score == 0.35


def test_capacity_shrinks_on_thin_book():
    """浅盘口的机会容量必须小。"""
    deep_s = make_leg(funding=0.003, depth=50_000_000.0, top_size_usd=5_000_000.0)
    deep_l = make_leg(funding=0.0, depth=50_000_000.0, top_size_usd=5_000_000.0)
    thin_s = make_leg(funding=0.003, depth=200_000.0, top_size_usd=20_000.0)
    thin_l = make_leg(funding=0.0, depth=200_000.0, top_size_usd=20_000.0)
    h = HistoryStats(rates=tuple([0.003] * 60), interval_h=8.0)
    z = HistoryStats(rates=tuple([0.0] * 60), interval_h=8.0)

    deep = score_pair("D", deep_l, deep_s, z, h, notional=20_000,
                      horizon_h=48, now_ts=NOW, hourly_carry=[0.0004] * 400)
    thin = score_pair("T", thin_l, thin_s, z, h, notional=20_000,
                      horizon_h=48, now_ts=NOW, hourly_carry=[0.0004] * 400)
    assert deep.capacity_usd > thin.capacity_usd * 5


def test_rank_sinks_negative_ev():
    """负期望和不可执行的必须沉底，无论毛年化多漂亮。"""
    h = HistoryStats(rates=tuple([0.003] * 60), interval_h=8.0)
    z = HistoryStats(rates=tuple([0.0] * 60), interval_h=8.0)
    good = score_pair("G", make_leg(funding=0.0, taker=0.0002, depth=20_000_000.0,
                                    top_size_usd=2_000_000.0),
                      make_leg(funding=0.003, taker=0.0002, depth=20_000_000.0,
                               top_size_usd=2_000_000.0),
                      z, h, notional=50_000, horizon_h=48, now_ts=NOW,
                      hourly_carry=[0.0005] * 400)
    bad = score_pair("B", make_leg(funding=0.0), make_leg(funding=0.0003),
                     z, HistoryStats(rates=tuple([0.0003] * 60), interval_h=8.0),
                     notional=50_000, horizon_h=24, now_ts=NOW)
    ordered = rank([bad, good])
    assert ordered[0].base == "G"
    assert ordered[-1].base == "B"


def test_no_settlement_in_horizon_is_flagged():
    """持有期内一次结算都没有 → 必须明确报出来。"""
    short = make_leg(funding=0.003, next_in_h=10.0)
    long = make_leg(funding=0.0, next_in_h=10.0)
    h = HistoryStats(rates=tuple([0.003] * 60), interval_h=8.0)
    z = HistoryStats(rates=tuple([0.0] * 60), interval_h=8.0)
    op = score_pair("Z", long, short, z, h, notional=50_000,
                    horizon_h=6, now_ts=NOW)
    assert op.verdict == "negative"
    assert any("一次结算都没有" in r for r in op.reasons)


def test_reasons_never_contradict_each_other():
    """回归测试：深度提示不能在净年化为负时说"收益仍然成立"。

    曾经同一行里同时出现：
      深度限制：这个机会最多放 $3,869…按这个仓位做，收益仍然成立
      净年化 -15.1% 为负：毛收益 17.7% 扛不住 0.42% 的往返成本
    用户看到互相打架的解释，会连对的那条一起不信。
    """
    # 费差小到覆盖不了成本，同时盘口很浅 —— 两个条件同时命中
    short = make_leg(symbol="X-A", funding=0.0002, taker=0.0006,
                     depth=20_000.0, top_size_usd=2_000.0)
    long = make_leg(symbol="X-B", market="okx:perp", funding=0.0, taker=0.0006,
                    depth=20_000.0, top_size_usd=2_000.0)
    hist = HistoryStats(rates=tuple([0.0002] * 60), interval_h=8.0)
    zero = HistoryStats(rates=tuple([0.0] * 60), interval_h=8.0)

    op = score_pair("X", long, short, zero, hist,
                    notional=50_000, horizon_h=168, now_ts=NOW)
    assert op.net_apr < 0, "这个用例本身要判负才有意义"
    joined = " ".join(op.reasons)
    assert "收益仍然成立" not in joined, f"净年化为负却说收益成立：{op.reasons}"
    # 判负的理由必须排在最前面——那才是决定性的那条
    assert "为负" in op.reasons[0]


def test_sized_down_still_says_returns_hold():
    """真的能赚、只是放不下那么多钱时，才可以说"收益仍然成立"。"""
    short = make_leg(symbol="Y-A", funding=0.0035, taker=0.0002,
                     depth=50_000.0, top_size_usd=5_000.0)
    long = make_leg(symbol="Y-B", market="okx:perp", funding=-0.0005, taker=0.0002,
                    depth=50_000.0, top_size_usd=5_000.0)
    hist_s = HistoryStats(rates=tuple([0.0035] * 90), interval_h=8.0)
    hist_l = HistoryStats(rates=tuple([-0.0005] * 90), interval_h=8.0)
    op = score_pair("Y", long, short, hist_l, hist_s, notional=500_000,
                    horizon_h=168, now_ts=NOW, hourly_carry=[0.0005] * 400)
    assert op.net_apr > 0
    assert op.verdict == "sized_down"
    assert any("收益仍然成立" in r for r in op.reasons)


def test_depth_message_does_not_claim_user_input():
    """不能把系统缩过的仓位说成"你填了"——用户填的是别的数。"""
    short = make_leg(symbol="Z-A", funding=0.0035, taker=0.0002,
                     depth=50_000.0, top_size_usd=5_000.0)
    long = make_leg(symbol="Z-B", market="okx:perp", funding=-0.0005, taker=0.0002,
                    depth=50_000.0, top_size_usd=5_000.0)
    h = HistoryStats(rates=tuple([0.0035] * 90), interval_h=8.0)
    z = HistoryStats(rates=tuple([-0.0005] * 90), interval_h=8.0)
    op = score_pair("Z", long, short, z, h, notional=500_000,
                    horizon_h=168, now_ts=NOW, hourly_carry=[0.0005] * 400)
    assert not any("你填了" in r for r in op.reasons)


def _prior_pair(depth: float):
    """一条真能赚钱、但一条历史都没有的机会。depth 是唯一变量。

    两条腿除了盘口深度完全一致——这样"深度"就是判决差异的唯一解释变量，
    对照组一跑就知道降级闸有没有被绕过去。
    """
    short = make_leg(symbol="P-A", funding=0.003, taker=0.0002,
                     depth=depth, top_size_usd=depth / 10)
    long = make_leg(symbol="P-B", market="okx:perp", funding=-0.003, taker=0.0002,
                    depth=depth, top_size_usd=depth / 10)
    z = HistoryStats(rates=(), interval_h=8.0)
    return score_pair("P", long, short, z, z, notional=50_000,
                      horizon_h=72, now_ts=NOW, hourly_carry=[])


def test_depth_cap_cannot_bypass_the_prior_downgrade():
    """深度限仓不得把"历史不足"这条降级顶掉。

    这是真实事故：守护模式推过一条 SKHYNIX——零历史、同号中位存续 0 小时——
    判决 sized_down，一路穿过 alerts.require_actionable 推到手机上。
    根因是所有降级都写作 `if verdict == "good"`，而深度分支抢在它们前面
    把 verdict 置成了 sized_down。表现出来就是**仓位越小越像好机会**，
    正好把这个工具自己的结论顶反。
    """
    thin, deep = _prior_pair(30_000.0), _prior_pair(5_000_000.0)

    assert thin.stability.is_prior and deep.stability.is_prior
    assert thin.net_apr > 0 and deep.net_apr > 0, "这个用例要真能赚才有意义"
    # 深度充足的那条历来判 marginal；深度受限的必须判成同一档
    assert deep.verdict == "marginal"
    assert thin.verdict == "marginal", (
        f"深度限仓把历史不足的降级绕过去了：{thin.verdict} / {thin.reasons}")
    assert not thin.is_actionable, "零历史的行绝不能进 is_actionable"
    assert any("历史数据不足" in r for r in thin.reasons)
    # 已经被降级了就不许再说"收益仍然成立"——那是给 sized_down 用的话
    assert not any("收益仍然成立" in r for r in thin.reasons)


def test_depth_cap_cannot_bypass_the_min_apr_threshold():
    """深度限仓不得把"净年化低于门槛"这条顶掉，理由也不许消失。

    原来这条判定挂在深度分支的 elif 上，深度一命中就整条跳过：
    净年化 0.01% 的机会因为盘口浅反而变成"可做"，而且理由里连
    "低于门槛"都不显示——用户只看到"按这个仓位做，收益仍然成立"。
    """
    def pair(depth: float, funding: float):
        # top_size_usd 两边都给足：滑点由它决定，而这里要单独变的是
        # depth_notional（容量闸看的是它），两个变量搅在一起就说不清是谁造成的差异
        short = make_leg(symbol="Q-A", funding=funding, taker=0.0002,
                         depth=depth, top_size_usd=200_000.0)
        long = make_leg(symbol="Q-B", market="okx:perp", funding=-funding,
                        taker=0.0002, depth=depth, top_size_usd=200_000.0)
        h = HistoryStats(rates=tuple([funding] * 90), interval_h=8.0)
        z = HistoryStats(rates=tuple([-funding] * 90), interval_h=8.0)
        return score_pair("Q", long, short, z, h, notional=50_000,
                          horizon_h=72, now_ts=NOW,
                          hourly_carry=[0.00001] * 200, min_net_apr=0.05)

    # 二分找一个净年化恰好落在 (0, 门槛) 之间的费率。写死一个数会随成本模型改动
    # 而失效，而这个用例的重点从来不是那个数，是两种深度下判决必须一致
    lo, hi = 0.0, 0.001
    for _ in range(60):
        mid = (lo + hi) / 2
        if pair(5_000_000.0, mid).net_apr < 0:
            lo = mid
        else:
            hi = mid
    funding = hi * 1.001

    thin, deep = pair(30_000.0, funding), pair(5_000_000.0, funding)
    assert 0 < thin.net_apr < 0.05, "这个用例要落在 (0, 门槛) 之间才有意义"
    assert deep.verdict == "marginal"
    assert thin.verdict == "marginal", (
        f"深度限仓把门槛判定跳过去了：{thin.verdict} / {thin.reasons}")
    assert any("低于门槛" in r for r in thin.reasons), (
        f"理由里必须留下「为什么不能做」：{thin.reasons}")


def test_prior_rows_are_never_actionable():
    """watch.alert_opportunities 的注释保证过：历史不足的行会被降级成 marginal，
    因此 require_actionable 会把它挡在告警之外。这条不变量必须由测试锁住——
    它是"别把用户训练成无视推送"的最后一道闸。"""
    for depth in (2_000.0, 30_000.0, 500_000.0, 5_000_000.0):
        op = _prior_pair(depth)
        assert not op.is_actionable, (
            f"depth={depth} 时零历史的行变成了可做：{op.verdict}")


# ---- 成交方式（挂单口径） ------------------------------------------------

def _fill_pair(fill_mode, taker_l=0.0005, taker_s=0.0005):
    # maker 用 make_leg 写死的 0.0002。省多少 = taker+滑点−maker，
    # 测试只要能控制 taker 就足以区分两条腿
    short = make_leg(symbol="F-A", funding=0.003, taker=taker_s,
                     depth=2_000_000.0)
    long = make_leg(symbol="F-B", market="okx:perp", funding=-0.003,
                    taker=taker_l, depth=2_000_000.0)
    h = HistoryStats(rates=tuple([0.003] * 90), interval_h=8.0)
    z = HistoryStats(rates=tuple([-0.003] * 90), interval_h=8.0)
    return score_pair("F", long, short, z, h, notional=50_000,
                      horizon_h=72, now_ts=NOW,
                      hourly_carry=[0.00075] * 400, fill_mode=fill_mode)


def test_fill_modes_are_strictly_ordered_by_cost():
    """挂单只可能更便宜：taker >= maker_one >= maker_both，且省的钱可加。

    这是口径的基本一致性——如果挂单算出来反而更贵，说明有一条腿把
    maker 费算成了 taker，或者滑点被重复计了。
    """
    t = _fill_pair("taker")
    m1 = _fill_pair("maker_one")
    m2 = _fill_pair("maker_both")
    assert t.cost_rt > m1.cost_rt > m2.cost_rt
    assert t.net_apr < m1.net_apr < m2.net_apr
    assert t.maker_saving == 0.0 and t.passive_side == "" and t.naked_notional == 0.0
    assert m1.maker_saving > 0 and m1.passive_side in ("long", "short")
    assert m2.maker_saving > m1.maker_saving and m2.passive_side == "both"


def test_maker_one_picks_the_leg_that_saves_more():
    """单边挂单必须挂在省得最多的那条腿上，挂错边等于白挂。"""
    # 多腿 taker 0.10%、空腿 0.02%：省钱的明显是多腿
    op = _fill_pair("maker_one", taker_l=0.0010, taker_s=0.0002)
    assert op.passive_side == "long"
    op2 = _fill_pair("maker_one", taker_l=0.0002, taker_s=0.0010)
    assert op2.passive_side == "short"


def test_passive_close_is_never_assumed():
    """挂单只作用于开仓，平仓一律按吃单算。

    平仓的时机通常不由你决定（费率翻转、爆仓距离、一腿被强平），
    真要平的时候没有挂着等成交的余裕——把平仓也算成挂单，
    CLOSE_URGENCY 这个系数就被抵消了。
    验证方式：双边挂单省下的钱不可能超过开仓侧的全部 taker 成本。
    """
    t = _fill_pair("taker")
    m2 = _fill_pair("maker_both")
    open_taker_cost = (t.long.taker_fee + slippage_rate(t.long, "buy", t.notional)
                       + t.short.taker_fee + slippage_rate(t.short, "sell", t.notional))
    assert m2.maker_saving <= open_taker_cost + 1e-12


def test_maker_mode_always_carries_the_naked_bill():
    """挂单口径的每一行都必须附裸奔账单——好机会也不例外。

    这条是"乐观模型必须自带警示"的钉子：省下的钱和裸露的亏损同名义，
    价格反向走 maker_saving 省的就全赔回去，这个恒等式必须写在理由里。
    """
    for mode in ("maker_one", "maker_both"):
        op = _fill_pair(mode)
        joined = " ".join(op.reasons)
        assert "挂单口径" in joined, f"{mode} 的理由里没有挂单账单：{op.reasons}"
        assert "假设必成交" in joined
        assert "单腿裸露" in joined
        assert op.naked_breakeven_move == op.maker_saving
        # 账单必须是最后一条：它是对上面所有数字的脚注
        assert "挂单口径" in op.reasons[-1]
    # 吃单口径不许出现这段话——没有假设就没有账单
    t = _fill_pair("taker")
    assert "挂单口径" not in " ".join(t.reasons)


def test_fill_mode_survives_rescore_roundtrip():
    """rescore 复用 fill_mode：历史补完后的重评不能悄悄退回吃单口径。"""
    m1 = _fill_pair("maker_one")
    # 模拟 rescore 的调用方式：从已评分的行里取 fill_mode 再评一遍
    again = score_pair(m1.base, m1.long, m1.short,
                       HistoryStats(rates=tuple([-0.003] * 90), interval_h=8.0),
                       HistoryStats(rates=tuple([0.003] * 90), interval_h=8.0),
                       notional=m1.notional, horizon_h=72, now_ts=NOW,
                       hourly_carry=[0.00075] * 400,
                       fee_basis=m1.fee_basis, fill_mode=m1.fill_mode)
    assert again.fill_mode == "maker_one"
    assert abs(again.cost_rt - m1.cost_rt) < 1e-12
