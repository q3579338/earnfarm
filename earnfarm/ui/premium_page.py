"""溢价监控页（/premium）。

**这一页没有自动刷新，一个定时器都没有**——这是设计不是偷懒：
溢价的锚（两地现货价）一天只各更新一个交易时段，其余时间两个永续都拴在
冻结的指数价上，定时重拉换来的只是噪音刷屏。打开时拉一次，之后手动点。

数据全部来自币安 fapi 公开端点（premium.py），不需要任何 API key，
也不占六家全市场刷新的限频配额——这页自己一共只发 6 个请求。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta

from nicegui import ui

from ..premium import (
    DEFAULT_REF_HOURS,
    DUAL_PAIRS,
    PremiumSnapshot,
    fetch_all,
    ladder,
    ladder_levels,
    session_status,
)
from . import theme
from .nav import module_nav
from .premium_chart import render_svg

# 回看期选项。上限 1440h（2 个月）不是抠门：fapi 单请求 1500 根封顶，
# 再长要翻页，这页不值得为它引入翻页复杂度
LOOKBACK_CHOICES = {
    72: "3 天", 168: "1 周", 336: "2 周",
    DEFAULT_REF_HOURS: "3 周", 720: "1 个月", 1440: "2 个月",
}

# 分位到语义色：极端读数标出来，中间地带保持安静。
# 85/15 不是玄学阈值，只是"进入分布尾部"的粗标记——这页是监控不是信号器
_PCT_HI = 85.0
_PCT_LO = 15.0


def _bj(ts: float, with_date: bool = True) -> str:
    """北京时间显示。用户在东八区看盘，UTC 时间戳直接摆出来等于让人心算。"""
    dt = datetime.fromtimestamp(ts, timezone.utc) + timedelta(hours=8)
    fmt = "%m-%d %H:%M" if with_date else "%H:%M"
    return dt.strftime(fmt)


def _premium_color(snapshot: PremiumSnapshot) -> str:
    if snapshot.ref is None:
        return theme.NEUTRAL
    if snapshot.ref.percentile >= _PCT_HI or snapshot.ref.percentile <= _PCT_LO:
        return theme.WARN
    return "#18181b"


def _tile(label: str, value: str, color: str = "#18181b", sub: str = "") -> None:
    with ui.column().classes("gap-0"):
        ui.label(label).classes("text-xs").style(f"color:{theme.NEUTRAL}")
        ui.label(value).classes("text-xl font-bold cf-mono").style(f"color:{color}")
        if sub:
            ui.label(sub).classes("text-xs cf-mono").style(f"color:{theme.NEUTRAL}")


def _render_snapshot(snapshot: PremiumSnapshot) -> None:
    pair = snapshot.pair
    ref = snapshot.ref

    with ui.card().classes("p-4 w-full"):
        # ---- 标题行 ----
        with ui.row().classes("items-baseline gap-3"):
            ui.label(pair.name).classes("text-base font-bold")
            ui.label(f"{pair.adr_symbol} vs {pair.local_symbol}"
                     f"　（{pair.ratio:.0f} 份 ADR = 1 股）") \
                .classes("text-xs cf-mono").style(f"color:{theme.NEUTRAL}")

        # ---- 核心数字 ----
        with ui.row().classes("items-start gap-8 mt-2 flex-wrap"):
            _tile("当前溢价", f"{snapshot.premium_pct:+.2f}%",
                  color=_premium_color(snapshot),
                  sub=(f"近 {ref.n / 24:.0f} 天分位 {ref.percentile:.0f}%"
                       if ref else "参考区间缺席"))
            _tile(pair.adr_label,
                  f"${snapshot.adr.price:,.2f}",
                  sub=f"24h {snapshot.adr.change_24h_pct:+.2f}%"
                      f"　量 {theme.usd(snapshot.adr.quote_volume_24h)}")
            _tile(pair.local_label,
                  f"${snapshot.local.price:,.2f}",
                  sub=f"24h {snapshot.local.change_24h_pct:+.2f}%"
                      f"　量 {theme.usd(snapshot.local.quote_volume_24h)}")
            _tile("理论价（本地÷比例）", f"${snapshot.fair:,.2f}")
            if ref:
                _tile("参考区间",
                      f"{ref.lo:+.1f}% ~ {ref.hi:+.1f}%",
                      sub=f"均值 {ref.mean:+.1f}%　中位 {ref.median:+.1f}%"
                          f"　n={ref.n}h")

        # ---- 资金费：做溢价收敛就是两腿对锁，付的就是这两个数 ----
        with ui.row().classes("items-center gap-6 mt-3 flex-wrap"):
            for q, tag, css in ((snapshot.adr, "ADR 腿", "cf-long"),
                                (snapshot.local, "本地腿", "cf-short")):
                with ui.row().classes("items-baseline gap-2"):
                    ui.label(tag).classes(f"text-xs {css}")
                    if q.funding_rate is None:
                        ui.label("资金费 —").classes("text-xs cf-mono") \
                            .style(f"color:{theme.NEUTRAL}")
                    else:
                        ui.label(f"资金费 {q.funding_rate * 100:+.4f}%") \
                            .classes("text-xs cf-mono")
                        if q.next_funding_ts:
                            ui.label(f"下次 {_bj(q.next_funding_ts, with_date=False)}") \
                                .classes("text-xs cf-mono") \
                                .style(f"color:{theme.NEUTRAL}")

        # ---- 溢价梯子 ----
        levels = ladder_levels(ref, snapshot.premium_pct)
        rows = []
        for lv, px in ladder(snapshot.local.price, pair.ratio, levels):
            dist = (px / snapshot.adr.price - 1.0) * 100.0
            marks = []
            if abs(lv - snapshot.premium_pct) < 0.15:
                marks.append("← 现在")
            if ref:
                if abs(lv - ref.mean) < 0.15:
                    marks.append("均值")
                if abs(lv - ref.lo) < 0.15:
                    marks.append("区间低")
                if abs(lv - ref.hi) < 0.15:
                    marks.append("区间高")
            rows.append({"lv": f"{lv:+.1f}%", "px": f"${px:,.2f}",
                         "dist": f"{dist:+.1f}%", "mark": " ".join(marks)})
        ui.label("溢价梯子（本地腿不动时各溢价对应的 ADR 价）") \
            .classes("text-xs mt-3").style(f"color:{theme.NEUTRAL}")
        ui.table(
            columns=[
                {"name": "lv", "label": "溢价", "field": "lv", "align": "right"},
                {"name": "px", "label": "ADR 对应价", "field": "px", "align": "right"},
                {"name": "dist", "label": "距现价", "field": "dist", "align": "right"},
                {"name": "mark", "label": "", "field": "mark", "align": "left"},
            ],
            rows=rows,
        ).classes("cf-dense w-full").props("dense flat hide-bottom")

        # ---- 回测曲线 ----
        svg = render_svg(snapshot.series)
        if svg:
            ui.label("回测曲线（上：两腿价格　下：溢价率，非零起点放大波动；"
                     "竖向浅灰带 = 美股常规时段）") \
                .classes("text-xs mt-3").style(f"color:{theme.NEUTRAL}")
            ui.html(svg).classes("w-full")
        else:
            ui.label("历史不足一天，曲线缺席——等两腿的 K 线攒够再来") \
                .classes("text-xs mt-3").style(f"color:{theme.WARN}")

        # ---- 脚注：机制提示 ----
        if pair.note:
            ui.label(pair.note).classes("cf-reason mt-2")
        if snapshot.errors:
            ui.label("；".join(snapshot.errors)).classes("text-xs") \
                .style(f"color:{theme.WARN}")


def build_premium_page() -> None:
    """建页面。所有状态都是本页局部的——这页只读公开行情，
    不碰 Session / vault / 六家适配器，和机会榜互不占用。"""
    ui.add_head_html(f"<style>{theme.GLOBAL_CSS}</style>")
    ui.dark_mode(False)

    with ui.header().classes("items-center justify-between px-4 py-2") \
            .style("background:#ffffff; color:#18181b; "
                   "border-bottom:1px solid #e4e4e7; box-shadow:none"):
        module_nav("premium")

    with ui.column().classes("p-4 gap-3 w-full max-w-5xl"):
        with ui.row().classes("items-center gap-3 w-full"):
            status = ui.label().classes("text-xs").style(f"color:{theme.NEUTRAL}")
            spinner = ui.spinner(size="sm")
            spinner.set_visibility(False)
            ui.space()
            session_label = ui.label().classes("text-xs") \
                .style(f"color:{theme.NEUTRAL}")
            lookback = ui.select(LOOKBACK_CHOICES, value=DEFAULT_REF_HOURS) \
                .props("dense outlined").classes("w-24") \
                .tooltip("回测曲线与分位数的回看期。改选立即重拉一次——"
                         "这是你刚做的显式动作，不算自动刷新")
            refresh_btn = ui.button("刷新", icon="refresh").props("dense outline")

        # 口径声明必须常驻页面：下面每一个数字都是币安永续的成交价，
        # 长得却和纳斯达克/韩交所的现货报价一模一样，不写清楚一定有人拿去对账
        ui.label("价格全部来自币安 USDⓈ-M 永续（fapi 公开端点），不是纳斯达克/韩交所现货报价；"
                 "溢价为两个永续之间的价差口径，与现货口径可差 1~2 个百分点。") \
            .classes("text-xs").style(f"color:{theme.NEUTRAL}")

        cards = ui.column().classes("w-full gap-3")

        async def refresh() -> None:
            refresh_btn.set_enabled(False)
            spinner.set_visibility(True)
            status.text = "拉取中…（6 个公开请求，无需 key）"
            try:
                snapshots = await fetch_all(DUAL_PAIRS,
                                            ref_hours=int(lookback.value))
                cards.clear()
                with cards:
                    for snap in snapshots:
                        if isinstance(snap, Exception):
                            ui.label(f"拉取失败：{snap}").classes("text-sm") \
                                .style(f"color:{theme.DANGER}")
                        else:
                            _render_snapshot(snap)
                status.text = f"{time.strftime('%H:%M:%S')} 更新（手动）"
                _, _, sess_text = session_status()
                session_label.text = sess_text
            except Exception as exc:
                status.text = f"拉取失败：{exc}"
                status.style(f"color:{theme.DANGER}")
            finally:
                spinner.set_visibility(False)
                refresh_btn.set_enabled(True)

        refresh_btn.on_click(refresh)
        # 改回看期立即重拉：这是用户刚做的显式动作，和"自动刷新"是两回事——
        # 同一条纪律见机会榜 adaptive_tick 里改选交易所的处理
        lookback.on_value_change(lambda e: refresh())
        # 打开时拉一次是"打开就能看"，**之后没有任何定时器**——
        # 溢价的锚一天只动两个交易时段，剩下的时间自动刷新只产噪音
        ui.timer(0.3, refresh, once=True)
