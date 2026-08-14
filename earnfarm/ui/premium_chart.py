# -*- coding: utf-8 -*-
"""溢价历史曲线：服务端生成静态 SVG。

不引入任何前端图表库——NiceGUI 带的 ECharts 要为这一张图搭一层配置字典，
而这张图的形态是定死的（上区两条价格线、下区溢价贴轴放大），
直接拼 SVG 字符串反而是最短的路，而且**不依赖客户端执行 JS**。

布局沿用和用户一起定稿的版式：
  - 上区：ADR（蓝 = 多腿色）与理论价（橙 = 空腿色），同一美元轴；
  - 下区：溢价率贴 X 轴，**非零起点放大波动**（振荡器画法，像 RSI 面板），
    虚线是回看期均值，填充是对均值的偏离；
  - 竖向浅灰带 = 美股常规时段——溢价的台阶跳变基本都发生在带内，
    这个对应关系是读图的关键，不画出来图就只剩形状没有机制。

本模块不 import nicegui：纯字符串生成，测试不用起界面。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from . import theme

# 版面几何。宽度固定 960 配合 viewBox 缩放，页面上跟随容器宽度
_W, _PADL, _PADR = 960, 50, 104
_H, _PADT = 560, 12
_PRICE_BOT = 300            # 价格区底
_PREM_TOP = 324             # 溢价区顶（中间留分隔带）
_AXIS_Y = 526               # 共享 X 轴 = 溢价区底
_BAND_FILL = "rgba(24,24,27,0.035)"
_PREM_COLOR = theme.SAFE    # 溢价用语义绿：它是这页的"收益对象"


def _bj(ts: float, fmt: str = "%m/%d") -> str:
    return (datetime.fromtimestamp(ts, timezone.utc)
            + timedelta(hours=8)).strftime(fmt)


def _nice_ticks(lo: float, hi: float, n: int) -> list[float]:
    span = hi - lo
    if span <= 0:
        return [lo]
    mag = 10 ** math.floor(math.log10(span / n))
    step = next((m * mag for m in (1, 2, 2.5, 5, 10)
                 if span / (m * mag) <= n + 0.5), 10 * mag)
    v = math.ceil(lo / step) * step
    out = []
    while v <= hi + 1e-9:
        out.append(round(v, 6))
        v += step
    return out


def _us_session_bands(t0: float, t1: float) -> list[tuple[float, float]]:
    """美股常规时段（13:30–20:00 UTC，工作日）。只标常规时段不管节假日，
    理由同 premium.session_status。"""
    out = []
    day = datetime.fromtimestamp(t0, timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    while day.timestamp() < t1:
        if day.weekday() < 5:
            s = day.timestamp() + 13.5 * 3600
            e = day.timestamp() + 20 * 3600
            if e >= t0 and s <= t1:
                out.append((max(s, t0), min(e, t1)))
        day += timedelta(days=1)
    return out


def _day_ticks(t0: float, t1: float) -> list[float]:
    d = datetime.fromtimestamp(t0, timezone.utc) + timedelta(hours=8)
    d = d.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    d -= timedelta(hours=8)
    out = []
    while d.timestamp() <= t1:
        out.append(d.timestamp())
        d += timedelta(days=1)
    return out


def render_svg(series: tuple[tuple[float, float, float, float], ...]) -> str | None:
    """(ts_秒, ADR, 理论价, 溢价%) 序列 → SVG 字符串。样本不足一天返回 None，
    让页面显示原因而不是画一张两个点的假图。"""
    if len(series) < 24:
        return None
    t0, t1 = series[0][0], series[-1][0]
    prems = [r[3] for r in series]
    cur = series[-1]

    def x(t: float) -> float:
        return _PADL + (t - t0) / (t1 - t0) * (_W - _PADL - _PADR)

    plo = min(min(r[2] for r in series), min(r[1] for r in series))
    phi = max(max(r[1] for r in series), max(r[2] for r in series))
    pad = (phi - plo) * 0.06 or 1.0
    plo, phi = plo - pad, phi + pad

    def yp(v: float) -> float:            # 价格区
        return _PADT + (1 - (v - plo) / (phi - plo)) * (_PRICE_BOT - _PADT)

    # 溢价区：非零起点放大波动（和页面标注一致），5 的倍数取整
    qlo = math.floor(min(prems) / 5) * 5
    qhi = math.ceil(max(prems) / 5) * 5
    if qhi == qlo:
        qhi += 5

    def yq(v: float) -> float:
        return _PREM_TOP + (1 - (v - qlo) / (qhi - qlo)) * (_AXIS_Y - _PREM_TOP)

    mean = sum(prems) / len(prems)
    g: list[str] = []

    # 时段带（贯穿两区）
    for s, e in _us_session_bands(t0, t1):
        g.append(f'<rect x="{x(s):.1f}" y="{_PADT}" width="{x(e) - x(s):.1f}" '
                 f'height="{_AXIS_Y - _PADT}" fill="{_BAND_FILL}"/>')

    # 价格区网格 + 刻度
    for v in _nice_ticks(plo, phi, 4):
        g.append(f'<line x1="{_PADL}" x2="{_W - _PADR}" y1="{yp(v):.1f}" '
                 f'y2="{yp(v):.1f}" stroke="#e4e4e7" stroke-width="1"/>')
        g.append(f'<text x="{_PADL - 6}" y="{yp(v) + 3.5:.1f}" text-anchor="end" '
                 f'class="pc-axis">${v:,.0f}</text>')
    # 溢价区网格 + 刻度
    for v in _nice_ticks(qlo, qhi, 3):
        g.append(f'<line x1="{_PADL}" x2="{_W - _PADR}" y1="{yq(v):.1f}" '
                 f'y2="{yq(v):.1f}" stroke="#e4e4e7" stroke-width="1"/>')
        g.append(f'<text x="{_PADL - 6}" y="{yq(v) + 3.5:.1f}" text-anchor="end" '
                 f'class="pc-axis">{v:+.0f}%</text>')

    # 日期刻度：天数多时隔行标注，避免挤成一条
    days = _day_ticks(t0, t1)
    stride = max(1, round(len(days) / 10))
    for i, tt in enumerate(days):
        g.append(f'<line x1="{x(tt):.1f}" x2="{x(tt):.1f}" y1="{_AXIS_Y}" '
                 f'y2="{_AXIS_Y + 4}" stroke="#c3c2b7" stroke-width="1"/>')
        if i % stride == 0:
            g.append(f'<text x="{x(tt):.1f}" y="{_AXIS_Y + 16}" '
                     f'text-anchor="middle" class="pc-axis">{_bj(tt)}</text>')
    g.append(f'<line x1="{_PADL}" x2="{_W - _PADR}" y1="{_AXIS_Y}" '
             f'y2="{_AXIS_Y}" stroke="#c3c2b7" stroke-width="1"/>')
    g.append(f'<line x1="{_PADL}" x2="{_W - _PADR}" y1="{(_PRICE_BOT + _PREM_TOP) / 2:.1f}" '
             f'y2="{(_PRICE_BOT + _PREM_TOP) / 2:.1f}" stroke="#e4e4e7" stroke-width="1"/>')

    # 均值虚线 + 对均值偏离的填充
    ym = yq(mean)
    g.append(f'<line x1="{_PADL}" x2="{_W - _PADR}" y1="{ym:.1f}" y2="{ym:.1f}" '
             f'stroke="#c3c2b7" stroke-width="1" stroke-dasharray="5 4"/>')
    g.append(f'<text x="{_W - _PADR - 4}" y="{ym - 5:.1f}" text-anchor="end" '
             f'class="pc-ann2">均值 {mean:+.1f}%</text>')
    fwd = "L".join(f"{x(r[0]):.1f},{yq(r[3]):.1f}" for r in series)
    g.append(f'<path d="M{x(t0):.1f},{ym:.1f}L{fwd}L{x(t1):.1f},{ym:.1f}Z" '
             f'fill="{_PREM_COLOR}" opacity="0.10"/>')

    # 三条曲线
    for idx, color in ((1, theme.LONG_COLOR), (2, theme.SHORT_COLOR)):
        pts = " ".join(f"{x(r[0]):.1f},{yp(r[idx]):.1f}" for r in series)
        g.append(f'<polyline points="{pts}" fill="none" stroke="{color}" '
                 f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
    pts = " ".join(f"{x(r[0]):.1f},{yq(r[3]):.1f}" for r in series)
    g.append(f'<polyline points="{pts}" fill="none" stroke="{_PREM_COLOR}" '
             f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')

    # 溢价极值标注
    i_min = prems.index(min(prems))
    i_max = prems.index(max(prems))
    for idx, tag in ((i_min, "低点"), (i_max, "高点")):
        r = series[idx]
        anchor = "end" if idx > len(series) * 0.85 else "middle"
        g.append(f'<circle cx="{x(r[0]):.1f}" cy="{yq(r[3]):.1f}" r="4" '
                 f'fill="{_PREM_COLOR}" stroke="#ffffff" stroke-width="2"/>')
        g.append(f'<text x="{x(r[0]):.1f}" y="{yq(r[3]) - 12:.1f}" '
                 f'text-anchor="{anchor}" class="pc-ann">{tag} {r[3]:+.1f}%</text>')

    # 右缘端点标注
    for value, label, y in ((f"${cur[1]:,.1f}", "ADR", yp(cur[1])),
                            (f"${cur[2]:,.1f}", "理论价", yp(cur[2])),
                            (f"{cur[3]:+.1f}%", "溢价", yq(cur[3]))):
        g.append(f'<text x="{x(t1) + 9:.1f}" y="{y + 4:.1f}" class="pc-end">{value}</text>')
        g.append(f'<text x="{x(t1) + 9:.1f}" y="{y + 18:.1f}" class="pc-ann2">{label}</text>')

    # 原生 <title> 悬浮：不用 JS 也有逐小时读数
    half = (x(series[1][0]) - x(series[0][0])) / 2
    for r in series:
        tip = (f"{_bj(r[0], '%m-%d %H:%M')} 北京时间&#10;ADR ${r[1]:,.2f}"
               f"&#10;理论价 ${r[2]:,.2f}&#10;溢价 {r[3]:+.2f}%")
        g.append(f'<rect x="{x(r[0]) - half:.1f}" y="{_PADT}" width="{2 * half:.1f}" '
                 f'height="{_AXIS_Y - _PADT}" fill="transparent"><title>{tip}</title></rect>')

    style = ("<style>.pc-axis{font-size:11px;fill:#898781;"
             "font-variant-numeric:tabular-nums}"
             ".pc-end{font-size:11.5px;font-weight:600;fill:#18181b}"
             ".pc-ann{font-size:11.5px;font-weight:600;fill:#18181b}"
             ".pc-ann2{font-size:10.5px;fill:#71717a}</style>")
    return (f'<svg viewBox="0 0 {_W} {_H}" role="img" '
            f'style="width:100%;height:auto;display:block">{style}{"".join(g)}</svg>')
