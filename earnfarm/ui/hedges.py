"""对冲仓位面板 + 建仓对话框。

界面上最要紧的一条：**纸上还是实盘必须一眼可辨**。
下错模式的代价是真金白银，所以纸上模式全站带标记，切实盘要二次确认。
"""

from __future__ import annotations

from decimal import Decimal

from nicegui import ui

from ..executor import State
from ..scoring import ScoredOpportunity
from ..trader import HedgeRuntime, Trader
from . import theme

STATE_LABELS = {
    State.IDLE: ("待建仓", theme.NEUTRAL),
    State.PREFLIGHT: ("检查中", theme.NEUTRAL),
    State.LEG1_PENDING: ("难腿在途", theme.WARN),
    State.EXPOSED: ("单腿裸露", theme.DANGER),
    State.LEG2_PENDING: ("补腿中", theme.WARN),
    State.REPAIR: ("补腿中", theme.WARN),
    State.UNWIND: ("撤退中", theme.DANGER),
    State.ORDER_UNKNOWN: ("订单状态未知", theme.DANGER),
    State.BALANCED: ("已对冲", theme.SAFE),
    State.FROZEN: ("已冻结·看不见仓位", theme.DANGER),
    State.HALTED: ("已停机·等人工", theme.DANGER),
    State.FLAT: ("已平仓", theme.NEUTRAL),
}


class HedgesPanel:
    def __init__(self, resolve_trader) -> None:
        # 传的是个取值函数而不是对象——解锁/加账户之后 trader 才存在，
        # 面板不该因此被重建（重建会把定时器变成孤儿）
        self._resolve = resolve_trader
        self._container: ui.element | None = None
        self._toolbar_box: ui.element | None = None

    @property
    def trader(self) -> Trader | None:
        return self._resolve()

    def build(self) -> None:
        """建面板。

        注意：定时器必须挂在**不会被 clear() 的**父容器上。
        挂进 render() 会清空的那个容器里，第一次刷新就把它变成孤儿，
        之后每秒抛一次 "parent slot has been deleted"。
        """
        with ui.column().classes("p-4 gap-3 cf-mono w-full"):
            self._toolbar_box = ui.row().classes("w-full items-center gap-3")
            self._container = ui.column().classes("w-full gap-2")
            # 挂在外层 column 上，render() 只清 _container
            ui.timer(1.0, self.render)
        self.render()

    def _toolbar(self) -> None:
        self._toolbar_box.clear()
        if self.trader is None:
            return
        with self._toolbar_box:
            if self.trader.paper:
                ui.label("纸上模式").classes("text-xs px-2 py-1 rounded font-bold") \
                    .style("color:#fff; background:#f97316") \
                    .tooltip("撮合是模拟的，行情是真的。不会向交易所发任何真实订单。")
            else:
                ui.label("实盘").classes("text-xs px-2 py-1 rounded font-bold") \
                    .style("color:#fff; background:#b91c1c")
            breaker = self.trader.breaker_level
            if breaker.value > 0:
                ui.label(f"熔断：{breaker.name}").classes("text-xs px-2 py-1 rounded") \
                    .style(f"color:#fff; background:{theme.DANGER}") \
                    .tooltip("禁止开新仓，但平仓、撤退、降杠杆仍然畅通")

    def render(self) -> None:
        if self._container is None:
            return
        self._toolbar()
        self._container.clear()
        if self.trader is None:
            with self._container:
                ui.label("还没有解锁凭据库或添加交易所账户。到「账户」页办一下才能建仓。") \
                    .classes("text-sm").style(f"color:{theme.NEUTRAL}")
            return
        runtimes = self.trader.runtimes()
        with self._container:
            if not runtimes:
                ui.label("尚无对冲仓位。到机会榜里挑一个，点右侧 + 建仓。") \
                    .classes("text-sm").style(f"color:{theme.NEUTRAL}")
                return
            for rt in runtimes:
                self._row(rt)

    def _row(self, rt: HedgeRuntime) -> None:
        label, color = STATE_LABELS.get(rt.active_state, ("?", theme.NEUTRAL))
        with ui.card().classes("w-full p-3"):
            with ui.row().classes("w-full items-center justify-between"):
                with ui.column().classes("gap-0"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label(rt.hedge.id[:8]).classes("text-sm font-bold")
                        ui.badge(label).style(f"background:{color}")
                    with ui.row().classes("gap-3 text-xs"):
                        ui.label(f"多 {theme.leg_label(rt.hedge.long.market, rt.hedge.long.symbol)}") \
                            .classes("cf-long")
                        ui.label(f"空 {theme.leg_label(rt.hedge.short.market, rt.hedge.short.symbol)}") \
                            .classes("cf-short")

                with ui.column().classes("items-end gap-0 text-xs"):
                    ui.label(f"目标 {rt.hedge.target}")
                    # 净敞口是这一行最该盯的数：理想恒为 0，
                    # 不为 0 就说明有一条腿裸着
                    net = rt.net_qty
                    net_color = theme.SAFE if abs(net) < Decimal("0.01") else theme.DANGER
                    ui.label(f"净敞口 {net}").style(f"color:{net_color}")

                with ui.row().classes("gap-1"):
                    ui.button(icon="pause", on_click=lambda r=rt: self._pause(r)) \
                        .props("dense flat size=sm").tooltip("只减不增")
                    ui.button(icon="close", on_click=lambda r=rt: self._close(r)) \
                        .props("dense flat size=sm color=negative").tooltip("平仓（走撤退阶梯）")

            ui.linear_progress(rt.progress, show_value=False).classes("mt-2") \
                .props("size=4px")
            ui.label(f"{rt.slices_done}/{rt.slices_total} 片完成").classes("text-xs") \
                .style(f"color:{theme.NEUTRAL}")
            if rt.last_event:
                lvl = {"info": theme.NEUTRAL, "warn": theme.WARN,
                       "error": theme.DANGER}[rt.last_event_level]
                ui.label(f"↳ {rt.last_event}").classes("text-xs").style(f"color:{lvl}")

    def _pause(self, rt: HedgeRuntime) -> None:
        self.trader.pause_hedge(rt.hedge.id)
        ui.notify("已转为只减不增", type="warning")

    def _close(self, rt: HedgeRuntime) -> None:
        with ui.dialog() as dialog, ui.card().classes("p-4"):
            ui.label(f"平掉 {rt.hedge.id[:8]}？").classes("text-sm font-bold")
            ui.label("走撤退阶梯：先终结在途订单，再按时间递进穿价平仓，全程 reduce_only。") \
                .classes("text-xs").style(f"color:{theme.NEUTRAL}")
            with ui.row().classes("justify-end gap-2 mt-2"):
                ui.button("取消", on_click=dialog.close).props("dense flat")

                def go() -> None:
                    self.trader.close_hedge(rt.hedge.id)
                    dialog.close()
                    ui.notify("已进入撤退", type="warning")

                ui.button("平仓", on_click=go).props("dense color=negative")
        dialog.open()


def open_create_dialog(trader: Trader, op: ScoredOpportunity, accounts,
                       on_created=None) -> None:
    """建仓对话框。

    默认值直接用机会算好的建议仓位——它已经按深度缩过，
    比让用户再想一遍靠谱。
    """
    by_venue: dict[str, list] = {}
    for a in accounts:
        by_venue.setdefault(a.venue.value, []).append(a)

    long_venue = op.long.market.split(":")[0]
    short_venue = op.short.market.split(":")[0]
    long_opts = {a.id: a.alias for a in by_venue.get(long_venue, [])}
    short_opts = {a.id: a.alias for a in by_venue.get(short_venue, [])}

    with ui.dialog() as dialog, ui.card().classes("p-4 w-[520px] cf-mono"):
        ui.label(f"建对冲：{op.base}").classes("text-base font-bold")

        with ui.row().classes("gap-4 text-xs"):
            ui.label(f"净年化 {theme.apr(op.net_apr)}") \
                .style(f"color:{theme.SAFE if op.net_apr > 0 else theme.DANGER}")
            ui.label(f"回本 {theme.hours(op.breakeven_h)}")
            ui.label(f"往返成本 {theme.pct(op.cost_rt)}")

        missing = []
        if not long_opts:
            missing.append(f"{long_venue}（多腿）")
        if not short_opts:
            missing.append(f"{short_venue}（空腿）")
        if missing:
            ui.label(f"缺少账户：{'、'.join(missing)}。请先到「账户」页添加。") \
                .classes("text-xs p-2 rounded") \
                .style(f"color:{theme.DANGER}; background:#fef2f2")
            with ui.row().classes("justify-end mt-2"):
                ui.button("知道了", on_click=dialog.close).props("dense flat")
            dialog.open()
            return

        with ui.row().classes("w-full gap-2"):
            long_sel = ui.select(long_opts, value=next(iter(long_opts)),
                                 label=f"多腿账户（{long_venue}）") \
                .props("dense outlined").classes("flex-1")
            short_sel = ui.select(short_opts, value=next(iter(short_opts)),
                                  label=f"空腿账户（{short_venue}）") \
                .props("dense outlined").classes("flex-1")

        size = ui.number("仓位（USDT）", value=round(op.suggested_notional),
                         min=100, step=100, format="%.0f") \
            .props("dense outlined").classes("w-full")
        if op.capacity_usd < op.notional:
            ui.label(f"深度只撑得住 ${op.capacity_usd:,.0f}，已按这个数填好。"
                     "填更大会吃穿盘口，滑点不可控。").classes("text-xs") \
                .style(f"color:{theme.WARN}")

        # 面值换算系数：1000PEPE vs PEPE 搞错就是 1000 倍仓位错配，一次就爆
        mult = ui.number("面值换算 N", value=1, min=0.000001, step=1,
                         format="%g").props("dense outlined").classes("w-full")
        ui.label("两腿合约面值不同时填（比如 1MCHEEMS 对 1000CHEEMS 填 1000）。"
                 "同面值填 1——填错就是数量级级别的仓位错配。") \
            .classes("text-xs").style(f"color:{theme.NEUTRAL}")

        mode = ui.label().classes("text-xs px-2 py-1 rounded mt-1")
        if trader.paper:
            mode.text = "纸上模式：撮合是模拟的，行情是真的，不会向交易所发任何真实订单。"
            mode.style("color:#9a3412; background:#fff7ed")
        else:
            mode.text = "实盘：会向交易所发送真实订单，动用真实资金。"
            mode.style("color:#fff; background:#b91c1c")

        status = ui.label().classes("text-xs")

        def create() -> None:
            try:
                trader.create_from_opportunity(
                    op, long_account=long_sel.value, short_account=short_sel.value,
                    notional=float(size.value or 0),
                    multiplier=Decimal(str(mult.value or 1)))
            except Exception as exc:
                status.text = str(exc)
                status.style(f"color:{theme.DANGER}")
                return
            ui.notify(f"已建仓 {op.base}"
                      + ("（纸上）" if trader.paper else "（实盘）"), type="positive")
            dialog.close()
            if on_created:
                on_created()

        with ui.row().classes("justify-end gap-2 mt-3"):
            ui.button("取消", on_click=dialog.close).props("dense flat")
            ui.button("建仓", on_click=create).props("dense")
    dialog.open()
