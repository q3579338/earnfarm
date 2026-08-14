"""双市场溢价检测的测试。网络路径全程 httpx.MockTransport，不连币安。

守的线：

1. **换算方向不能反。** 1 ADS = 1/10 股 ⇒ 理论价 = 本地股价 ÷ 10。
   方向搞反的话溢价会算出 ±10 倍的荒谬值——这是页面上一切数字的地基。
2. **K 线对齐必须取交集。** 一腿缺根时拿旧价配新价算出的"溢价"是幻觉；
   样本不足一天必须返回 None 而不是硬着头皮给统计量。
3. **K 线失败不挡即时溢价。** 参考区间是锦上添花，即时价才是主菜——
   klines 端点挂掉时快照必须照常返回，错误进 errors 而不是抛出去。
"""

from __future__ import annotations

import asyncio
import json

import httpx

from earnfarm.premium import (
    DUAL_PAIRS,
    MAX_REF_HOURS,
    PremiumPair,
    align_series,
    compute_premium,
    fetch_snapshot,
    ladder,
    ladder_levels,
    make_ref_stats,
    session_status,
)
from earnfarm.ui.premium_chart import render_svg

PAIR = PremiumPair(key="t", name="测试", adr_symbol="AUSDT",
                   local_symbol="BUSDT", ratio=10.0,
                   adr_label="ADR", local_label="本地")


def run(coro):
    return asyncio.run(coro)


# ---- 1) 换算方向 ----

def test_compute_premium_direction():
    # 本地股 $1,164.9，ADR $164.5：理论价 116.49，溢价约 +41.2%
    fair, prem = compute_premium(164.5, 1164.9, 10.0)
    assert abs(fair - 116.49) < 0.01
    assert 41.0 < prem < 41.5
    # 平价时溢价为 0
    _, flat = compute_premium(100.0, 1000.0, 10.0)
    assert abs(flat) < 1e-9


def test_ladder_holds_local_leg_fixed():
    rows = dict(ladder(1000.0, 10.0, (0.0, 37.0)))
    assert abs(rows[0.0] - 100.0) < 1e-9
    assert abs(rows[37.0] - 137.0) < 1e-9


def test_ladder_levels_come_from_ref_and_current():
    stats = make_ref_stats(
        {i: 140.0 for i in range(48)},
        {i: 1000.0 for i in range(48)},
        10.0, current_premium=40.0)
    levels = ladder_levels(stats, 41.2)
    assert 41.2 in levels            # 当前值必须在梯子上
    assert levels == tuple(sorted(levels, reverse=True))


# ---- 2) K 线对齐 ----

def test_ref_stats_uses_intersection_only():
    # ADR 有 0..47，本地只有 24..71：交集 24 根，勉强够
    adr = {i: 141.0 for i in range(48)}
    loc = {i: 1000.0 for i in range(24, 72)}
    stats = make_ref_stats(adr, loc, 10.0, current_premium=41.0)
    assert stats is not None and stats.n == 24
    assert abs(stats.mean - 41.0) < 1e-9


def test_ref_stats_too_short_returns_none():
    adr = {i: 141.0 for i in range(10)}
    loc = {i: 1000.0 for i in range(10)}
    assert make_ref_stats(adr, loc, 10.0, 41.0) is None


def test_percentile_counts_below_current():
    adr = {i: 100.0 + i for i in range(48)}          # 溢价单调走高
    loc = {i: 1000.0 for i in range(48)}
    stats = make_ref_stats(adr, loc, 10.0, current_premium=1e9)
    assert stats is not None and stats.percentile == 100.0


# ---- 3) 网络路径 ----

def _mock_client(kline_fail: bool = False,
                 seen_limits: list[int] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        sym = request.url.params.get("symbol", "")
        path = request.url.path
        if path.endswith("ticker/24hr"):
            price = "164.50" if sym == "AUSDT" else "1164.90"
            return httpx.Response(200, json={
                "symbol": sym, "lastPrice": price,
                "priceChangePercent": "1.5", "quoteVolume": "1000000"})
        if path.endswith("premiumIndex"):
            return httpx.Response(200, json={
                "symbol": sym, "lastFundingRate": "-0.0005",
                "nextFundingTime": 1786608000000})
        if path.endswith("klines"):
            if kline_fail:
                return httpx.Response(500, text="boom")
            limit = int(request.url.params.get("limit", "0"))
            if seen_limits is not None:
                seen_limits.append(limit)
            base = 164.0 if sym == "AUSDT" else 1160.0
            rows = [[i * 3600_000, "0", "0", "0", str(base), "0",
                     0, "0", 0, "0", "0", "0"] for i in range(min(limit, 48))]
            return httpx.Response(200, content=json.dumps(rows))
        raise AssertionError(f"意外请求 {path}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             base_url="https://fapi.binance.com")


def test_fetch_snapshot_happy_path():
    snap = run(fetch_snapshot(PAIR, _mock_client()))
    assert abs(snap.fair - 116.49) < 0.01
    assert 41.0 < snap.premium_pct < 41.5
    assert snap.adr.funding_rate == -0.0005
    assert snap.ref is not None and snap.ref.n == 48
    assert snap.errors == ()


def test_fetch_snapshot_survives_kline_failure():
    snap = run(fetch_snapshot(PAIR, _mock_client(kline_fail=True)))
    assert snap.ref is None                     # 参考区间缺席
    assert snap.series == ()                    # 序列同源同缺席
    assert 41.0 < snap.premium_pct < 41.5       # 即时溢价照常
    assert any("K线" in e for e in snap.errors)  # 原因写进 errors


def test_fetch_snapshot_passes_and_clamps_ref_hours():
    seen: list[int] = []
    run(fetch_snapshot(PAIR, _mock_client(seen_limits=seen), ref_hours=168))
    assert seen == [168, 168]
    seen.clear()
    # 超出 fapi 单请求上限必须夹回来，而不是把 400 错误留给用户
    run(fetch_snapshot(PAIR, _mock_client(seen_limits=seen), ref_hours=99999))
    assert seen == [MAX_REF_HOURS, MAX_REF_HOURS]


# ---- 序列与曲线 ----

def test_align_series_intersection_and_order():
    adr = {i * 3600_000: 141.0 for i in range(48)}
    loc = {i * 3600_000: 1000.0 for i in range(24, 72)}
    rows = align_series(adr, loc, 10.0)
    assert len(rows) == 24
    assert rows == sorted(rows)                 # 时间升序
    ts, a, fair, prem = rows[0]
    assert (a, fair) == (141.0, 100.0) and abs(prem - 41.0) < 1e-9


def test_render_svg_contains_three_curves_and_hover():
    series = tuple((i * 3600.0, 141.0 + i * 0.1, 100.0, 41.0 + i * 0.1)
                   for i in range(48))
    svg = render_svg(series)
    assert svg is not None and svg.startswith("<svg")
    assert svg.count("<polyline") == 3          # ADR、理论价、溢价
    assert "均值" in svg and "<title>" in svg    # 均值线与原生悬浮


def test_render_svg_refuses_short_series():
    series = tuple((i * 3600.0, 141.0, 100.0, 41.0) for i in range(10))
    assert render_svg(series) is None


# ---- 配置与时段 ----

def test_dual_pairs_ratio_sanity():
    for p in DUAL_PAIRS:
        assert p.ratio > 0
        assert p.adr_symbol != p.local_symbol


def test_session_status_known_times():
    # 2026-08-13（周四）03:00 UTC：韩股盘中
    krx, us, _ = session_status(1786676400)
    assert krx and not us
    # 同日 15:00 UTC：美股盘中
    krx, us, _ = session_status(1786719600)
    assert not krx and us
    # 周六 03:00 UTC：都休市
    krx, us, _ = session_status(1786849200)
    assert not krx and not us
