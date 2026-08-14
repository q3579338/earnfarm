"""机会榜。这是整个工具的门面，也是相对两个竞品最大的差异点。

qqq.bot 的榜单只有两个原始指标（最新价差、下期资金费差），不年化、不扣手续费、
不看历史、无容量维度——榜首经常是负期望或者根本做不了的机会。
taoli.tools 的榜单更好一些（有归一化和流动性过滤），但只列单腿费率，
把最难也最值钱的那一步（跨所配对 + 净收益）留给用户心算。

我们这里默认就是「跨所配对 + 扣费净年化 + 回本时间 + 容量 + 稳定性」，
并且**把不能做的理由直接写在行里**——用户一眼看到为什么它不是好机会。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from nicegui import ui

from ..scoring import ScoredOpportunity
from . import theme

# 成本列的口径提示。**默认费率必须标出来，绝不能默默用了还装作精确**——
# 成本占了判决的绝对主导，挂牌价和 VIP3 档能差一倍，同一行的判决经常正负相反。
# 原版工具正是拿一个写死的费率算完就直接给结论，用户无从知道那个数是估的。
FEE_BASIS_HINT = {
    "default": (
        "成本是按各家 VIP0 挂牌价估算的，不是你的费率。\n"
        "连上账户后（「账户」页解锁并添加 API key）会自动读你的真实档位重算。\n"
        "这件事很要紧：四笔 taker 手续费经常比资金费差本身还大，"
        "VIP 档或平台币抵扣能把它砍掉一半——那时候这一行的判决可能整个反过来。"
    ),
    "partial": (
        "只有一条腿用上了你的真实档位，另一条还是 VIP0 挂牌价。\n"
        "跨所对冲的成本是两家相加，缺一条腿这个数就还不能当准。"
    ),
}
# 标记字符。用星号不用图标：这一列是等宽数字，插图标会把整列的对齐打散
FEE_BASIS_MARK = "*"


@dataclass(slots=True)
class BoardFilters:
    """榜单过滤器。

    小币开关（include_small_caps）默认关闭，但**关闭不等于"小币不能做"**——
    小币的高费率往往就是真肉，只是容量小。打开后不过滤流动性，
    改由评分给出"这个机会最多能放多少"，仓位按容量缩即可。

    对散户来说这经常是更好的选择：年化 300% 的机会哪怕只能放 2 万，
    也比在 BTC 上放 5 万赚 10% 强。
    """

    # 按**容量**过滤，而不是未平仓额/成交额。
    # 容量是"这个机会能吃下你多少钱"，正是用户真正要的那个数；
    # 未平仓额只是它的一个粗糙代理，而且要额外的行情端点才拿得到。
    min_capacity: float = 1_000.0
    max_interval_h: float = 24.0
    notional: float = 50_000.0
    # 资金费套利默认按一周算。3 天太短——四笔 taker 手续费摊不开，
    # 绝大多数机会会显示成负期望，那不是机会不好，是持有期设得不对。
    horizon_h: float = 168.0
    hide_negative: bool = False
    include_small_caps: bool = False
    search: str = ""
    # 成交方式（taker / maker_one / maker_both）。挂单口径的净年化是
    # **假设必成交的上限**，评分会在理由里附上裸奔窗口的账单；
    # 切换后必须整榜重评（改的是成本模型，不是过滤条件），
    # 所以它不像其他字段那样只触发 render——上层挂了 on_rescore 回调
    fill_mode: str = "taker"


class OpportunityBoard:
    """机会榜。**数据共享、渲染面按客户端登记。**

    _rows 全局一份（AppState 级共享），但 summary/container 这些 ui 元素
    必须每个客户端各留一份：任何一个对 / 的 HTTP GET（预览探测、健康检查、
    爬虫）都会让 NiceGUI 在服务端 build 一次页面。字段要是单例的，
    谁最后 build 谁抢走它——之后 set_rows 全渲染进那个幽灵客户端的容器里，
    真正的浏览器页面永远停在"还没拉到行情"，而且没有任何报错。
    render() 对所有登记过的客户端逐个画，画不进去的（连接已死）当场剔除。
    """

    def __init__(self, on_create_hedge: Callable[[ScoredOpportunity], None] | None = None) -> None:
        self.filters = BoardFilters()
        self._on_create = on_create_hedge
        self._rows: list[ScoredOpportunity] = []
        # client_id -> {"summary": label, "container": column, "cap_slider": slider}
        self._surfaces: dict[str, dict] = {}
        # 成交方式切换时由上层重评整榜（成本模型变了，render 解决不了）。
        # 不直接 import app 里的刷新函数——那是循环依赖
        self.on_rescore: Callable[[], None] | None = None

    @property
    def rows(self) -> list[ScoredOpportunity]:
        """当前榜单的原始数据（未过滤）。后台历史回填完成后要拿它去重新评分。"""
        return list(self._rows)

    # ---- 构建 -----------------------------------------------------------

    def build(self) -> None:
        surface: dict = {}
        with ui.column().classes("w-full gap-2 cf-mono"):
            self._build_filters(surface)
            surface["summary"] = ui.label().classes("text-xs").style(f"color:{theme.NEUTRAL}")
            surface["container"] = ui.column().classes("w-full gap-1")
        # 按客户端 id 登记。同一个客户端不会 build 两次；不同客户端（包括
        # 服务端渲染的幽灵）各占一格，render 时画不进去的自然被剔掉
        self._surfaces[ui.context.client.id] = surface
        self.render()

    def _build_filters(self, surface: dict) -> None:
        with ui.card().classes("w-full p-3"):
            with ui.row().classes("w-full items-center gap-6 flex-wrap"):
                # 对数滑块：把"几个数量级"变成一次拖动，天然防手滑多打一个零
                surface["cap_slider"] = self._log_slider("能吃下 ≥", "min_capacity", 2, 7)

                with ui.column().classes("gap-0"):
                    ui.label("仓位规模").classes("text-xs").style(f"color:{theme.NEUTRAL}")
                    ui.number(value=self.filters.notional, min=100, step=1000,
                              format="%.0f", prefix="$",
                              on_change=lambda e: self._set("notional", e.value)) \
                        .props("dense outlined").classes("w-32")

                with ui.column().classes("gap-0"):
                    ui.label("计划持有").classes("text-xs").style(f"color:{theme.NEUTRAL}")
                    ui.select({72: "3 天", 168: "1 周", 336: "2 周", 720: "1 月"},
                              value=int(self.filters.horizon_h),
                              on_change=lambda e: self._set("horizon_h", float(e.value))) \
                        .props("dense outlined").classes("w-28")

                with ui.column().classes("gap-0"):
                    ui.label("成交方式").classes("text-xs").style(f"color:{theme.NEUTRAL}")
                    ui.select({"taker": "双边吃单", "maker_one": "单边挂单",
                               "maker_both": "双边挂单"},
                              value=self.filters.fill_mode,
                              on_change=self._set_fill_mode) \
                        .props("dense outlined").classes("w-28") \
                        .tooltip("挂单省手续费、不穿价差，但不保证成交：被动腿成交到"
                                 "对侧补上之间是单腿裸露的。挂单口径的净年化是假设"
                                 "必成交的上限，每行的展开理由里有这笔账。")

                ui.input(placeholder="币种…",
                         on_change=lambda e: self._set("search", (e.value or "").upper())) \
                    .props("dense outlined clearable").classes("w-32")

                ui.switch("只看可做",
                          on_change=lambda e: self._set("hide_negative", e.value)) \
                    .props("dense")

                # 小币开关：打开后不再按流动性过滤，改由容量列告诉你能放多少
                ui.switch("包含小币", on_change=self._toggle_small_caps) \
                    .props("dense") \
                    .tooltip("打开后不按流动性过滤。小币的高费率往往是真肉，只是容量小——"
                             "容量列会告诉你这个机会最多能放多少，按那个数下单即可。")

    def _toggle_small_caps(self, e) -> None:
        self.filters.include_small_caps = e.value
        # 容量滑块在小币模式下不起作用了，灰掉免得用户以为没生效。
        # 所有客户端一起灰：过滤器是共享状态，只灰自己那个会让另一个
        # 标签页的滑块看起来还能用
        for surface in list(self._surfaces.values()):
            slider = surface.get("cap_slider")
            if slider is not None:
                try:
                    slider.set_enabled(not e.value)
                except Exception:
                    pass          # 客户端已死，render 的清理会剔掉它
        self.render()

    def _log_slider(self, label: str, field: str, lo: int, hi: int):
        current = getattr(self.filters, field)
        with ui.column().classes("gap-0 w-44"):
            caption = ui.label(f"{label} {theme.usd(current)}").classes("text-xs") \
                .style(f"color:{theme.NEUTRAL}")

            def on_change(e) -> None:
                value = 10 ** e.value
                setattr(self.filters, field, value)
                caption.text = f"{label} {theme.usd(value)}"
                self.render()

            import math
            return ui.slider(min=lo, max=hi, step=1,
                             value=int(math.log10(current)),
                             on_change=on_change).props("dense")

    def _set(self, field: str, value) -> None:
        setattr(self.filters, field, value)
        self.render()

    def _set_fill_mode(self, e) -> None:
        if e.value == self.filters.fill_mode:
            return
        self.filters.fill_mode = e.value
        # 改的是成本模型：每一行的 cost_rt / 净年化 / 容量 / 判决全要重算，
        # 只重渲染会让表头写着"挂单"而数字还是吃单的
        if self.on_rescore is not None:
            self.on_rescore()
        else:
            self.render()

    # ---- 数据 -----------------------------------------------------------

    def set_rows(self, rows: Sequence[ScoredOpportunity]) -> None:
        self._rows = list(rows)
        self.render()

    def _visible(self) -> list[ScoredOpportunity]:
        f = self.filters
        out = []
        for o in self._rows:
            if f.search and f.search not in o.base.upper():
                continue
            if f.hide_negative and not o.is_actionable:
                continue
            # 容量过滤只对**本来能做**的机会生效。
            # 负期望的机会容量恒为 0（二分搜索找不到满足门槛的仓位），
            # 拿容量过滤它们会把"覆盖不了成本"说成"吃不下你这么多钱"——
            # 两个完全不同的原因，说错了用户会去调仓位而不是换持有期。
            if (not f.include_small_caps and o.is_actionable
                    and o.capacity_usd < f.min_capacity):
                continue
            longest = max(o.long.funding_interval_h, o.short.funding_interval_h)
            if longest > f.max_interval_h:
                continue
            out.append(o)
        return out

    # ---- 渲染 -----------------------------------------------------------

    def render(self) -> None:
        """对**每个**登记过的客户端渲染一遍，画不进去的当场剔除。

        剔除必须发生在这里而不是断连回调里：服务端渲染的幽灵客户端
        （HTTP 探测、爬虫）从来没连过 websocket，不会触发任何断连事件，
        只有在真正往它的容器里画东西失败时才暴露出来。
        """
        rows = self._visible()
        summary = self._summary_text(rows)
        for client_id, surface in list(self._surfaces.items()):
            try:
                self._render_into(surface, rows, summary)
            except Exception:
                # 容器的 slot 已经没了 = 客户端已死。剔掉，别让它把
                # 后面活着的客户端一起拖崩
                self._surfaces.pop(client_id, None)

    def _render_into(self, surface: dict, rows: list[ScoredOpportunity],
                     summary: str) -> None:
        surface["summary"].text = summary
        container = surface["container"]
        container.clear()
        with container:
            if not rows:
                ui.label(self._empty_hint()) \
                    .classes("text-sm p-4").style(f"color:{theme.NEUTRAL}")
                return
            self._header()
            for o in rows:
                self._row(o)

    def _summary_text(self, rows: list[ScoredOpportunity]) -> str:
        good = sum(1 for o in rows if o.is_actionable)
        neg = sum(1 for o in rows if not o.is_actionable)
        hidden = len(self._rows) - len(rows)
        parts = [f"{len(rows)} 个机会（{good} 个能做"]
        if neg:
            parts.append(f"，{neg} 个覆盖不了成本")
        parts.append("）")
        if hidden:
            parts.append(f"，{hidden} 个吃不下这么多钱"
                         + ("（打开「包含小币」可以看）"
                            if not self.filters.include_small_caps else ""))
        # 成本口径必须写在明面上，不能只藏在悬停里：整榜按挂牌价估算时，
        # 用户至少要知道自己看的是估算值，才谈得上决定要不要去连账户
        parts.append(self._fee_note(rows))
        # 挂单口径同理：整榜数字是"假设必成交"的上限时，这四个字
        # 必须钉在摘要里，跟哪一行展开没展开无关
        if self.filters.fill_mode == "maker_one":
            parts.append("　·　单边挂单口径：净年化是假设挂单必成交的上限")
        elif self.filters.fill_mode == "maker_both":
            parts.append("　·　双边挂单口径：净年化是假设两腿都成交的上限，"
                         "裸奔窗口也最长")
        return "".join(parts)

    @staticmethod
    def _fee_note(rows: Sequence[ScoredOpportunity]) -> str:
        """成本口径的一句话汇总，接在摘要行后面。"""
        estimated = sum(1 for o in rows if o.fee_basis != "real")
        if not rows or not estimated:
            return ""
        if estimated == len(rows):
            return "　·　成本按 VIP0 挂牌价估算（连上账户后用你的真实档位重算）"
        return f"　·　其中 {estimated} 个的成本按 VIP0 挂牌价估算"

    def _empty_hint(self) -> str:
        """空榜的原因不止一个，别都推给"调低门槛"。

        接上历史后经常是"当前费差按历史会衰减，一周覆盖不了四笔手续费"——
        那时候该改的是持有期，不是仓位。
        """
        if not self._rows:
            return "还没拉到行情。点右上角「刷新行情」。"
        if self.filters.hide_negative:
            return ("当前没有能覆盖成本的机会。关掉「只看可做」能看到全部候选"
                    "及各自差在哪。资金费套利常常整段时间都没有正期望的机会，"
                    "这时候不做才是对的。")
        return "没有满足条件的机会。试着把「能吃下」调低、换个更长的持有期，或打开「包含小币」。"

    def _header(self) -> None:
        cols = [("币种", "w-20", ""), ("多腿 / 空腿", "flex-1", ""),
                ("净年化", "w-24 text-right", ""), ("毛年化", "w-24 text-right", ""),
                ("成本", "w-20 text-right",
                 "开+平四笔 taker 手续费加滑点，占名义的比例。带 * 的行是按各家 "
                 "VIP0 挂牌价估算的——连上账户后会换成你的真实费率档位重算，"
                 "鼠标悬停那一行看细节。"),
                ("回本", "w-20 text-right", ""),
                ("稳定性", "w-20 text-right",
                 "把这个费差的历史拿来按你的持有期滚动回测：胜率、同号存续时长、"
                 "衰减、翻转频率的加权几何均值。第二行是回看了多久历史——"
                 "写着「数据不足」的行用的是保守先验，不是统计结果，鼠标悬停看细节。"),
                ("容量", "w-24 text-right", ""), ("", "w-16", "")]
        with ui.row().classes("w-full items-center px-2 py-1 text-xs border-b") \
                .style(f"color:{theme.NEUTRAL}"):
            for text, cls, hint in cols:
                item = ui.label(text).classes(cls)
                if hint:
                    item.tooltip(hint)

    def _row(self, o: ScoredOpportunity) -> None:
        color, label = theme.VERDICT_STYLE[o.verdict]
        row_cls = "w-full items-start px-2 py-1 text-xs cf-row"
        if o.verdict == "negative":
            row_cls += " cf-row-negative"
        elif o.verdict == "unexecutable":
            row_cls += " cf-row-unexecutable"

        with ui.column().classes("w-full gap-0"):
            with ui.row().classes(row_cls):
                with ui.column().classes("w-20 gap-0"):
                    ui.label(o.base).classes("font-bold")
                    ui.badge(label).style(f"background:{color}").props("dense")

                # 多腿蓝、空腿橙，贯穿全站的约定
                with ui.column().classes("flex-1 gap-0"):
                    with ui.row().classes("gap-2 items-center"):
                        ui.label("多").classes("cf-long font-bold")
                        ui.label(theme.leg_label(o.long.market, o.long.symbol)).classes("cf-long")
                        # 费率后面必须跟结算周期：1h 的 0.05% 和 8h 的 0.05% 差 8 倍，
                        # 不标出来用户会系统性选错币
                        ui.label(f"{theme.pct(o.long.funding_now, 4, signed=True)}"
                                 f" / {o.long.funding_interval_h:.0f}h") \
                            .style(f"color:{theme.NEUTRAL}")
                    with ui.row().classes("gap-2 items-center"):
                        ui.label("空").classes("cf-short font-bold")
                        ui.label(theme.leg_label(o.short.market, o.short.symbol)).classes("cf-short")
                        ui.label(f"{theme.pct(o.short.funding_now, 4, signed=True)}"
                                 f" / {o.short.funding_interval_h:.0f}h") \
                            .style(f"color:{theme.NEUTRAL}")

                net_color = theme.SAFE if o.net_apr > 0 else theme.DANGER
                ui.label(theme.apr(o.net_apr)).classes("w-24 text-right font-bold") \
                    .style(f"color:{net_color}")
                # 毛年化放旁边，让"成本吃掉了多少"一目了然
                ui.label(theme.apr(o.gross_apr)).classes("w-24 text-right") \
                    .style(f"color:{theme.NEUTRAL}")
                self._cost_cell(o)
                ui.label(theme.hours(o.breakeven_h)).classes("w-20 text-right")

                self._stability_cell(o)

                # 容量列显示"这个机会最多能放多少"。低于你填的仓位就标出来——
                # 这不是拒绝，是告诉你该放多少。
                with ui.column().classes("w-24 items-end gap-0"):
                    ui.label(theme.usd(o.capacity_usd))
                    if 0 < o.capacity_usd < o.notional:
                        ui.label("← 建议仓位").classes("text-[10px]") \
                            .style(f"color:{theme.LONG_COLOR}")

                with ui.row().classes("w-16 justify-end"):
                    btn = ui.button(icon="add", on_click=lambda o=o: self._create(o)) \
                        .props("dense flat size=sm")
                    btn.set_enabled(o.is_actionable or o.verdict == "marginal")
                    btn.tooltip("按这个机会建对冲")

            # 判决理由——这是我们相对两个竞品最实用的一处：
            # 不是只给个分数，而是直接说清"为什么它不是好机会"
            for reason in o.reasons[:2]:
                ui.label(f"↳ {reason}").classes("cf-reason pl-2 pb-1")

    def _cost_cell(self, o: ScoredOpportunity) -> None:
        """成本列。用的是真实档位还是挂牌价，必须在这一格上看得见。

        标记只有一个星号、颜色是黄的，数字本身保持中性灰：没连账户时**整列**
        都是估算的，把整列染黄等于没标。真正要传达的是"这个数是估的，点开看为什么"。
        """
        hint = FEE_BASIS_HINT.get(o.fee_basis)
        with ui.row().classes("w-20 justify-end items-baseline gap-0"):
            ui.label(theme.pct(o.cost_rt)).style(f"color:{theme.NEUTRAL}")
            if hint:
                ui.label(FEE_BASIS_MARK).classes("text-[10px] pl-px") \
                    .style(f"color:{theme.WARN}")
                # 用 ui.tooltip 而不是 label.tooltip()：提示是分行写的，
                # 默认样式下 \n 会被 HTML 折成空格，挤成一坨没人会读
                ui.tooltip(hint).style(
                    "white-space:pre-line; max-width:24rem; line-height:1.5")

    def _stability_cell(self, o: ScoredOpportunity) -> None:
        """稳定性列。

        这一列的价值全在于**它是不是真的**。历史接进来之前，所有行都是
        0.35 的先验，整列等于没有；接进来之后必须让用户一眼分清哪些行有
        真实统计撑腰、哪些行还是先验，否则等于把先验伪装成了评分——
        比不显示更糟。所以数字下面永远跟着"看了多久历史"这个第二行。
        """
        stab = o.stability
        with ui.column().classes("w-20 items-end gap-0"):
            if stab.is_prior:
                # 先验值不配用绿/黄/红：那三档是对真实统计的判断
                ui.label(f"{stab.score:.2f}").style(f"color:{theme.NEUTRAL}")
                ui.label("数据不足" if stab.n_samples == 0
                         else f"仅 {theme.hours(stab.n_samples)}") \
                    .classes("text-[10px]").style(f"color:{theme.NEUTRAL}")
            else:
                color = (theme.SAFE if stab.score > 0.6
                         else theme.WARN if stab.score > 0.3 else theme.DANGER)
                ui.label(f"{stab.score:.2f}").style(f"color:{color}")
                ui.label(theme.hours(stab.n_samples)).classes("text-[10px]") \
                    .style(f"color:{theme.NEUTRAL}")
            # 用 ui.tooltip 而不是 cell.tooltip()：三条明细是分行写的，
            # 默认样式下 \n 会被 HTML 折成空格，三行挤成一坨没人会读
            ui.tooltip(self._stability_tooltip(o)) \
                .style("white-space:pre-line; max-width:24rem; line-height:1.5")

    def _stability_tooltip(self, o: ScoredOpportunity) -> str:
        stab = o.stability
        if stab.is_prior:
            if stab.n_samples == 0:
                return ("还没有这一对的历史资金费，评分是 0.35 的保守先验，不是统计结果。"
                        "后台补完历史会自动重算——补不到通常是这个币在某一家太新，"
                        "或者两条腿的历史没有重叠区间。")
            return (f"两条腿只有 {theme.hours(stab.n_samples)}的重叠历史，撑不起一个"
                    f"{theme.hours(self.filters.horizon_h)}持有期的滚动窗口统计，"
                    "评分仍是 0.35 的保守先验。历史必须**重叠**才算数：一条腿 2020 年"
                    "就上线、另一条上个月才有，能用的只有重叠的那一段。样本够了会自动重算。")
        return (
            f"胜率 {stab.p_win:.0%}：按你的持有期滚动回测，扣完成本后赚钱的窗口占比"
            f"（已按 Beta(2,3) 先验收缩，样本少时会被拉向 40%）\n"
            f"同号中位存续 {theme.hours(stab.median_run_h)}：这个费差历史上通常能"
            f"保持同一方向多久。它要是短于回本时间（{theme.hours(o.breakeven_h)}），"
            f"毛年化再漂亮也做不成\n"
            f"有效样本 {stab.n_effective:.1f} 个独立窗口（共 {theme.hours(stab.n_samples)}"
            f"逐小时观测）。滚动窗口是重叠的，所以有效样本远少于窗口个数"
        )

    def _create(self, o: ScoredOpportunity) -> None:
        if self._on_create:
            self._on_create(o)
        else:
            ui.notify(f"建对冲：{o.base} 多 {o.long.market} / 空 {o.short.market}",
                      type="positive")
