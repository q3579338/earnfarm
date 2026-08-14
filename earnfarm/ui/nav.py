"""页眉的模块导航。

earnfarm 现在有两个平级模块：跨所资金费套利（首页）和双市场溢价监控。
导航长在品牌名旁边、两页共用一份——之前溢价监控是右上角一个小链接，
和"设置里的一个入口"一个待遇，用户会以为它是附属功能而不是并列模块。

当前页显示为深色粗体且**不可点**（点了也只是刷新自己，白灰一下反而像坏了），
其余模块是中性色链接。
"""

from __future__ import annotations

from nicegui import ui

from . import theme

# (key, 显示名, 路由)。加第三个模块时补一行，两个页面的页眉同时长出来
MODULES: tuple[tuple[str, str, str], ...] = (
    ("ops", "跨所资金费套利", "/"),
    ("premium", "双市场溢价监控", "/premium"),
    ("analysis", "操作复盘", "/analysis"),
    ("market", "单币分析", "/market"),
    ("download", "下载本地版", "/download"),
)


def module_nav(active: str) -> None:
    """品牌名 + 运行模式徽标 + 模块切换。放在 ui.header 的左侧行里调用。

    模式徽标的判据就是登录闸：设了 EARNFARM_WEB_PASSWORD = 在线模式
    （公网部署，整站先过 /login），没设 = 本地模式。应用躲在反代后面时
    自己看不见"是否暴露在公网"，登录闸开关是唯一可靠的信号。
    """
    from .access import gate_enabled, hosted_mode

    with ui.row().classes("items-center gap-3"):
        ui.label("earnfarm").classes("text-lg font-bold cf-mono")
        if hosted_mode():
            ui.badge("公开版").props("color=green") \
                .tooltip("多人共用模式：交易所数据由你的浏览器直连拉取、"
                         "密钥加密存在你自己的设备上，服务器不托管任何人的密钥。")
        elif gate_enabled():
            ui.badge("在线").props("color=blue")
        else:
            ui.badge("本地").props("color=grey") \
                .tooltip("本地模式：数据与凭据都在本机，仅供自己使用。")
        for key, label, route in MODULES:
            if key == active:
                ui.label(label).classes("text-sm font-bold") \
                    .style("color:#18181b")
            else:
                ui.link(label, route).classes("text-sm") \
                    .style(f"color:{theme.NEUTRAL}; text-decoration:none")
