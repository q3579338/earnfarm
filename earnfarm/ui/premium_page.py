"""溢价监控页（/premium）。

**这一页没有自动刷新，一个定时器都没有**——这是设计不是偷懒：
溢价的锚（两地现货价）一天只各更新一个交易时段，其余时间两个永续都拴在
冻结的指数价上，定时重拉换来的只是噪音刷屏。打开时拉一次，之后手动点。

数据全部来自币安 fapi 公开端点（premium.py），不需要任何 API key，
也不占六家全市场刷新的限频配额——这页自己一共只发 6 个请求。
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone, timedelta

from nicegui import ui

from .. import arb
from ..premium import (
    DEFAULT_REF_HOURS,
    DUAL_PAIRS,
    PremiumSnapshot,
    fetch_all,
    ladder,
    ladder_levels,
    session_status,
    snapshot_from_raw,
)
from . import binance_client, theme
from .access import hosted_mode
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


def _money(value: float, signed: bool = False) -> str:
    """金额一律两位小数。theme.usd 会缩成 K/M，下单要照抄的数字不能被缩。"""
    return f"{value:{'+' if signed else ''},.2f}"


def _calc_table(columns: list[tuple[str, str, str]], rows: list[dict]) -> None:
    """(name, label, align) 三元组建密表。这页的四张小表长得一样，别重复三遍。"""
    ui.table(
        columns=[{"name": n, "label": lb, "field": n, "align": al}
                 for n, lb, al in columns],
        rows=rows,
    ).classes("cf-dense w-full").props("dense flat hide-bottom")


def _render_calc_body(calc: arb.ArbCalc) -> None:
    """把算好的四块摆出来。这里一行算术都没有——全在 arb.py 里。"""
    hedge, funding, cost = calc.hedge, calc.funding, calc.cost

    # ---- ① 对冲比例 ----
    ui.label("① 对冲比例（等名义额）").classes("text-xs font-bold mt-1")
    _calc_table(
        [("leg", "腿", "left"), ("act", "动作", "left"),
         ("px", "现价", "right"), ("qty", "张数", "right"),
         ("nom", "名义额", "right")],
        [{"leg": leg.label, "act": leg.action, "px": f"${_money(leg.price)}",
          "qty": f"{leg.qty:,.4f}", "nom": f"${_money(leg.notional)}"}
         for leg in (hedge.adr, hedge.local)],
    )
    ui.label(f"张数按交易所最小步进取整后再下单。若改按「等股数」对冲，"
             f"ADR 腿要 {hedge.share_equiv_adr_qty:,.4f} 张——"
             f"比上表多 {hedge.share_equiv_extra_pct:+.1f}%，"
             f"多出来的那部分是裸单，不是对冲。") \
        .classes("cf-reason")

    # ---- ② 资金费净额 ----
    ui.label("② 资金费净额（按当前费率不变外推）").classes("text-xs font-bold mt-2")
    _calc_table(
        [("leg", "腿", "left"), ("dir", "方向", "left"),
         ("rate", "上期费率", "right"), ("iv", "周期", "right"),
         ("per", "每次收付", "right"), ("day", "每天", "right")],
        [{"leg": leg.symbol, "dir": "空" if leg.is_short else "多",
          "rate": "—" if leg.rate is None else f"{leg.rate * 100:+.4f}%",
          "iv": theme.interval_label(int(leg.interval_h * 3600)),
          "per": f"{_money(leg.per_settle_usdt, signed=True)}",
          "day": f"{_money(leg.per_day_usdt, signed=True)}"}
         for leg in funding.legs],
    )
    net_color = theme.SAFE if funding.per_day_usdt >= 0 else theme.DANGER
    ui.label(f"合计每天净{'收' if funding.per_day_usdt >= 0 else '付'} "
             f"{_money(abs(funding.per_day_usdt))} USDT"
             f"　年化 {theme.apr(funding.apr)}（占单腿名义额）") \
        .classes("text-xs cf-mono").style(f"color:{net_color}")
    if not funding.complete:
        ui.label("有腿的资金费没拉到，缺的那腿按 0 计——这块数字偏乐观") \
            .classes("text-xs").style(f"color:{theme.WARN}")
    ui.label("正费率 = 多头付给空头。这是静态口径：费率每期都会变，"
             "持有几天后的实际净额与这里必然不同。").classes("cf-reason")

    # ---- ③ 盈亏情景 ----
    ui.label(f"③ 盈亏情景（进场溢价 {calc.entry_premium_pct:+.2f}%，"
             f"对数口径）").classes("text-xs font-bold mt-2")
    _calc_table(
        [("prem", "溢价", "right"), ("pnl", "盈亏 USDT", "right"),
         ("pct", "占名义额", "right"), ("mark", "", "left")],
        [{"prem": f"{row.premium_pct:+.2f}%",
          "pnl": _money(row.pnl_usdt, signed=True),
          "pct": f"{row.pnl_pct:+.2f}%",
          "mark": (row.label + ("　← 现在" if row.is_current else ""))}
         for row in calc.scenarios],
    )

    # ---- ④ 成本与回本 ----
    ui.label("④ 成本与回本").classes("text-xs font-bold mt-2")
    with ui.row().classes("items-start gap-8 flex-wrap"):
        _tile("开平总手续费",
              f"${_money(cost.fee_usdt)}",
              sub=f"{cost.fills} 笔 taker × {cost.taker_fee * 100:.3f}%"
                  f" = 名义额的 {cost.fee_frac * 100:.2f}%")
        _tile("回本需溢价走",
              f"{cost.breakeven_pp:+.2f}pp",
              color=theme.WARN,
              sub=f"即走到 {cost.breakeven_premium_pct:+.2f}%")
        if cost.funding_days_to_cover is not None:
            _tile("资金费覆盖手续费",
                  theme.hours(cost.funding_days_to_cover * 24),
                  sub="按当前费率不变")
    ui.label(f"溢价波动小于 {abs(cost.breakeven_pp):.2f}pp 时，"
             f"价差赚的还不够付这 4 笔手续费——这就是不该频繁开平的理由。") \
        .classes("cf-reason")
    ui.label("本表不含滑点、不含强平风险、不构成投资建议。") \
        .classes("text-xs").style(f"color:{theme.WARN}")


def _build_calculator(snapshot: PremiumSnapshot) -> None:
    """计算器的输入控件 + 输出区。只在 expansion 首次展开时被调用。"""
    with ui.column().classes("w-full gap-2"):
        # 这句话是整个模块的论点，摆在输入之前：先看懂再填数
        ui.label("两腿对冲后，板块整体涨跌不再影响盈亏；亏损只来自价差反向。"
                 "这正是裸单与套利的区别。") \
            .classes("text-xs").style(f"color:{theme.WARN}")

        side = ui.radio(arb.SIDE_LABELS, value=arb.SHORT_PREMIUM) \
            .props("inline dense")
        with ui.row().classes("items-center gap-4 flex-wrap"):
            notional = ui.number("每腿名义额 USDT", value=arb.DEFAULT_NOTIONAL,
                                 min=1, step=1000) \
                .props("dense outlined").classes("w-40")
            # 默认填当前溢价，但**可改**——"假如我在 X% 进场"正是这页要回答的问题
            entry = ui.number("进场溢价 %", value=round(snapshot.premium_pct, 2),
                              step=0.5, format="%.2f") \
                .props("dense outlined").classes("w-40") \
                .tooltip("默认是当前溢价。改成别的值即可回答"
                         "「假如我在这个价位进场会怎样」")

        out = ui.column().classes("w-full gap-1")

        def redraw() -> None:
            out.clear()
            with out:
                # 输入框被清空时 value 是 None，别把 None 喂进算术层
                if not notional.value or float(notional.value) <= 0:
                    ui.label("填一个正的名义额").classes("text-xs") \
                        .style(f"color:{theme.NEUTRAL}")
                    return
                _render_calc_body(arb.build_from_snapshot(
                    snapshot, side=side.value,
                    notional_per_leg=float(notional.value),
                    entry_premium_pct=(float(entry.value)
                                       if entry.value is not None else None)))

        for control in (side, notional, entry):
            control.on_value_change(lambda _: redraw())
        redraw()


def _render_calculator(snapshot: PremiumSnapshot) -> None:
    """套利计算器区块。默认收起，且**收起时一次算术都不做**——
    内容在首次展开时才构建，之后保留（重复折叠不重算）。
    这页的纪律是"不自动做任何事"，一个默认收起却照样算全套的区块会破坏它。
    """
    exp = ui.expansion("套利计算器 · 照着数字下单", icon="calculate") \
        .classes("w-full mt-3")
    built: list[bool] = []

    def lazy(e) -> None:
        if e.value and not built:
            built.append(True)
            with exp:
                _build_calculator(snapshot)

    exp.on_value_change(lazy)


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

        # ---- 套利计算器（默认收起，展开才算） ----
        _render_calculator(snapshot)


def build_premium_page() -> None:
    """建页面。所有状态都是本页局部的——这页只读公开行情，
    不碰 Session / vault / 六家适配器，和机会榜互不占用。"""
    ui.add_head_html(f"<style>{theme.GLOBAL_CSS}</style>")
    ui.dark_mode(False)
    # 多人模式：行情由**访客的浏览器**直连币安拉（服务器可能被地域封锁）
    online = hosted_mode()
    if online:
        binance_client.install(os.environ.get("EARNFARM_FAPI_BASE", ""))

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
            status.text = ("拉取中…（由你的浏览器直连币安）" if online
                           else "拉取中…（6 个公开请求，无需 key）")
            try:
                if online:
                    raw = await binance_client.premium(
                        [{"key": p.key, "adr": p.adr_symbol, "local": p.local_symbol}
                         for p in DUAL_PAIRS], int(lookback.value))
                    snapshots = []
                    for p in DUAL_PAIRS:
                        item = (raw or {}).get(p.key) or {}
                        if item.get("err") or "t_adr" not in item:
                            snapshots.append(RuntimeError(
                                f"{p.name} 行情拉取失败：{item.get('err', '无数据')}"))
                        else:
                            snapshots.append(snapshot_from_raw(p, item))
                else:
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
