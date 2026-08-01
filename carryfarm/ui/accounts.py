"""账户面板：主密码解锁 + 添加/删除交易所 API key。

安全上的三条硬性做法：
1. 凭据用主密码派生的密钥加密后才落盘（scrypt + AES-GCM），明文永不写磁盘
2. 保存前先连一次交易所做自检——时钟偏差、读权限、账户模式，
   宁可在这里报错，也别等下单时才发现
3. 输入框一律 password 类型，且不回填已存的值（避免从 DOM 里读出来）
"""

from __future__ import annotations

import time

from nicegui import ui

from ..models import Venue
from ..session import CREDENTIAL_FIELDS, FIELD_LABELS, Session
from ..storage import StorageError
from ..vault import BadPassword
from . import theme

VENUE_LABELS = {
    Venue.BINANCE: "币安 Binance",
    Venue.OKX: "OKX",
    Venue.HTX: "火币 HTX",
    Venue.GATE: "Gate.io",
    Venue.BYBIT: "Bybit",
    Venue.BITGET: "Bitget",
}

# 各家配 API key 时的必做项。这些是踩出来的，写在界面上比写在文档里有用得多。
VENUE_SETUP_HINTS = {
    Venue.BINANCE: "必须先在 API 管理里填「只访问受信任的 IP」才能勾权限。"
                   "经典账户和统一账户的权限项不同，改过账户类型后必须重建 key。",
    Venue.OKX: "只勾「读取」+「交易」。账户模式需切到合约/跨币种/组合保证金，永续只支持全仓。",
    Venue.HTX: "合约和现货是两套域名但共用同一把 key。需要开合约交易权限。",
    Venue.GATE: "必须先升级成统一账户，否则永续接口不通。",
    Venue.BYBIT: "选「读写权限」并勾上 UTA 的全部四项。",
    Venue.BITGET: "USDC 合约和 U 本位合约要分别设持仓模式。",
}


class AccountsPanel:
    def __init__(self, session: Session, on_change=None) -> None:
        self.session = session
        self._on_change = on_change
        self._list_container: ui.element | None = None

    def build(self) -> None:
        with ui.column().classes("p-4 gap-4 cf-mono w-full max-w-4xl"):
            if not self.session.is_unlocked:
                self._lock_screen()
                return
            self._toolbar()
            self._list_container = ui.column().classes("w-full gap-2")
            self._render_list()

    # ---- 解锁 -----------------------------------------------------------

    def _lock_screen(self) -> None:
        first_time = not self.session.is_initialized
        with ui.card().classes("p-6 w-full max-w-lg"):
            ui.label("设置主密码" if first_time else "解锁凭据库").classes("text-base font-bold")
            ui.label(
                "主密码用来加密交易所 API key。它不会存到任何地方——"
                "忘了就只能重新添加账户，没有找回途径。"
                if first_time else
                "输入主密码以解密已保存的 API key。"
            ).classes("text-xs mb-2").style(f"color:{theme.NEUTRAL}")

            pwd = ui.input("主密码", password=True, password_toggle_button=True) \
                .props("dense outlined").classes("w-full")
            confirm = None
            if first_time:
                confirm = ui.input("再输一遍", password=True) \
                    .props("dense outlined").classes("w-full")

            msg = ui.label().classes("text-xs")

            def submit() -> None:
                value = (pwd.value or "").strip()
                if first_time:
                    if len(value) < 8:
                        msg.text = "主密码至少 8 位"
                        msg.style(f"color:{theme.DANGER}")
                        return
                    if value != (confirm.value or "").strip():
                        msg.text = "两次输入不一致"
                        msg.style(f"color:{theme.DANGER}")
                        return
                    self.session.initialize(value)
                else:
                    try:
                        self.session.unlock(value)
                    except BadPassword:
                        msg.text = "主密码错误"
                        msg.style(f"color:{theme.DANGER}")
                        return
                ui.notify("凭据库已解锁", type="positive")
                if self._on_change:
                    self._on_change()

            pwd.on("keydown.enter", submit)
            ui.button("设置并解锁" if first_time else "解锁", on_click=submit) \
                .props("dense").classes("mt-2")

    # ---- 列表 -----------------------------------------------------------

    def _toolbar(self) -> None:
        with ui.row().classes("w-full items-center justify-between"):
            with ui.column().classes("gap-0"):
                ui.label("交易所账户").classes("text-sm font-bold")
                ui.label("API key 已用主密码加密存本地，明文不落盘").classes("text-xs") \
                    .style(f"color:{theme.NEUTRAL}")
            ui.button("添加账户", icon="add", on_click=self._add_dialog).props("dense")

    def _render_list(self) -> None:
        if self._list_container is None:
            return
        self._list_container.clear()
        accounts = self.session.list_accounts()
        with self._list_container:
            if not accounts:
                with ui.card().classes("p-4 w-full"):
                    ui.label("还没有账户。加一个才能建仓。").classes("text-sm")
                    ui.label("提示：配 key 时只开「读取」+「交易」权限，"
                             "务必关掉提现，并绑定 IP 白名单。").classes("text-xs mt-1") \
                        .style(f"color:{theme.WARN}")
                return
            for acc in accounts:
                with ui.card().classes("p-3 w-full"):
                    with ui.row().classes("w-full items-center justify-between"):
                        with ui.row().classes("items-center gap-3"):
                            ui.label(VENUE_LABELS.get(acc.venue, acc.venue.value)) \
                                .classes("text-sm font-bold")
                            ui.label(acc.alias).classes("text-xs") \
                                .style(f"color:{theme.NEUTRAL}")
                            if acc.disabled:
                                ui.badge("已停用").props("color=grey")
                            elif acc.connected:
                                ui.badge("已连接").props("color=green")
                        ui.button(icon="delete", on_click=lambda a=acc: self._confirm_delete(a)) \
                            .props("dense flat size=sm").tooltip("删除账户")
            self._fee_status()

    def _fee_status(self) -> None:
        """费率档位的现状。

        这块必须露出来：成本是这个工具唯一的判决变量，同一行机会按 VIP0 挂牌价
        算是负期望、按 VIP3 算是正期望，正负相反。用户有权知道自己看的榜
        用的是哪一种，以及平台币抵扣有没有被算进去。
        """
        notes = self.session.fee_notes()
        if not (self.session.has_real_fees or notes):
            with ui.card().classes("p-3 w-full"):
                ui.label("费率档位：未取到").classes("text-xs font-bold") \
                    .style(f"color:{theme.WARN}")
                ui.label("机会榜上的成本按各家 VIP0 挂牌价估算。连上账户后会自动"
                         "换成你的真实档位重算——档位差一倍，判决经常正负相反。")\
                    .classes("text-xs mt-1").style(f"color:{theme.NEUTRAL}")
            return
        with ui.card().classes("p-3 w-full"):
            ui.label("费率档位：已按你的真实档位计算").classes("text-xs font-bold") \
                .style(f"color:{theme.SAFE}")
            for note in notes:
                ui.label(f"· {note}").classes("text-xs mt-1") \
                    .style(f"color:{theme.NEUTRAL}")
            if notes:
                # 平台币抵扣一律不打折，只报状态。折扣幅度不在任何交易所的 API 里，
                # 拿记忆里的数字去削成本等于在唯一的判决变量上凭空造数。
                ui.label("平台币抵扣只报状态、不打折：折扣幅度各家 API 都不给。"
                         "实际扣费只会比这里显示的更低，方向是安全的。") \
                    .classes("text-xs mt-2").style(f"color:{theme.NEUTRAL}")

    def _confirm_delete(self, acc) -> None:
        with ui.dialog() as dialog, ui.card().classes("p-4"):
            ui.label(f"删除 {VENUE_LABELS.get(acc.venue, acc.venue.value)} / {acc.alias}？") \
                .classes("text-sm")
            ui.label("只删除本地保存的凭据，不影响交易所上的 API key，也不动任何持仓。") \
                .classes("text-xs").style(f"color:{theme.NEUTRAL}")
            with ui.row().classes("justify-end gap-2 mt-2"):
                ui.button("取消", on_click=dialog.close).props("dense flat")

                def do_delete() -> None:
                    try:
                        self.session.delete_account(acc.id)
                        ui.notify("已删除", type="positive")
                    except StorageError as exc:
                        ui.notify(str(exc), type="negative")
                    dialog.close()
                    self._render_list()

                ui.button("删除", on_click=do_delete).props("dense color=negative")
        dialog.open()

    # ---- 添加 -----------------------------------------------------------

    def _add_dialog(self) -> None:
        with ui.dialog() as dialog, ui.card().classes("p-4 w-[520px]"):
            ui.label("添加交易所账户").classes("text-base font-bold")

            venue_select = ui.select(
                {v: VENUE_LABELS[v] for v in Venue}, value=Venue.BINANCE, label="交易所"
            ).props("dense outlined").classes("w-full")

            alias = ui.input("备注名", placeholder="比如 主号 / 小号2") \
                .props("dense outlined").classes("w-full")

            hint = ui.label().classes("text-xs p-2 rounded") \
                .style(f"color:{theme.WARN}; background:#fefce8")
            fields_box = ui.column().classes("w-full gap-2")
            inputs: dict[str, ui.input] = {}

            def rebuild_fields() -> None:
                venue = venue_select.value
                hint.text = VENUE_SETUP_HINTS.get(venue, "")
                fields_box.clear()
                inputs.clear()
                with fields_box:
                    for f in CREDENTIAL_FIELDS[venue]:
                        # 一律 password 类型：防肩窥，也防从 DOM 里读出来
                        inputs[f] = ui.input(FIELD_LABELS[f], password=True,
                                             password_toggle_button=True) \
                            .props("dense outlined").classes("w-full")

            venue_select.on_value_change(lambda _: rebuild_fields())
            rebuild_fields()

            status = ui.label().classes("text-xs")
            spinner = ui.spinner(size="sm")
            spinner.set_visibility(False)

            async def validate_and_save() -> None:
                venue = venue_select.value
                cred = {k: (w.value or "").strip() for k, w in inputs.items()}
                if not (alias.value or "").strip():
                    status.text = "请填个备注名，多账户时靠它区分"
                    status.style(f"color:{theme.DANGER}")
                    return

                spinner.set_visibility(True)
                status.text = "正在连接交易所自检…"
                status.style(f"color:{theme.NEUTRAL}")
                result = await self.session.validate_credential(venue, cred)
                spinner.set_visibility(False)

                if not result.ok:
                    status.text = result.message
                    status.style(f"color:{theme.DANGER}")
                    return
                try:
                    self.session.add_account(venue, alias.value, cred,
                                             int(time.time() * 1000))
                except StorageError as exc:
                    status.text = str(exc)
                    status.style(f"color:{theme.DANGER}")
                    return

                note = ("；" + "；".join(result.warnings)) if result.warnings else ""
                ui.notify(f"已添加：{VENUE_LABELS[venue]} / {alias.value}{note}",
                          type="positive")
                dialog.close()
                self._render_list()

            with ui.row().classes("justify-end gap-2 mt-3 items-center"):
                ui.button("取消", on_click=dialog.close).props("dense flat")
                ui.button("自检并保存", on_click=validate_and_save).props("dense")
        dialog.open()
