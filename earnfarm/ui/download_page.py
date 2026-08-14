"""下载页（/download）。

公开版能做的事有天花板：浏览器里跑不了本机 AI CLI、也不该让访客碰服务器
的 vault 和下单。所以这一页说清楚两个版本的分界，并把想要完整功能的人
引到 GitHub 自己跑一份——那份在他自己机器上，密钥、AI、下单全归他自己。
"""

from __future__ import annotations

from nicegui import ui

from . import theme
from .nav import module_nav

REPO = "https://github.com/q3579338/earnfarm"

# (功能, 公开版, 本地版)
_MATRIX: tuple[tuple[str, str, str], ...] = (
    ("跨所资金费机会榜", "✓ 只读", "✓"),
    ("双市场溢价监控", "✓", "✓"),
    ("单币分析（AI）", "自带 API key", "✓ 本机 CLI 或 API"),
    ("操作复盘（AI）", "自带 API key", "✓ 本机 CLI 或 API"),
    ("API 凭据保存", "加密存你自己的浏览器", "加密存本机（主密码）"),
    ("对冲仓位 / 下单", "—", "✓"),
    ("7×24 守护与告警", "—", "✓"),
)


def build_download_page() -> None:
    ui.add_head_html(f"<style>{theme.GLOBAL_CSS}</style>")
    ui.dark_mode(False)

    with ui.header().classes("items-center justify-between px-4 py-2") \
            .style("background:#ffffff; color:#18181b; "
                   "border-bottom:1px solid #e4e4e7; box-shadow:none"):
        module_nav("download")

    with ui.column().classes("p-4 gap-4 w-full max-w-3xl"):
        ui.label("下载本地版").classes("text-xl font-bold")
        ui.label("网页版够用就直接用，不用装任何东西。要用自己电脑上的 AI CLI"
                 "（Claude Code / Grok / Codex）、或者要真正下单，就跑一份本地版——"
                 "它完全在你自己机器上，密钥、AI、仓位都不经过任何服务器。") \
            .classes("text-sm").style(f"color:{theme.NEUTRAL}")

        # ---- 两版对照 ----
        ui.label("两个版本的分界").classes("text-base font-bold mt-2")
        ui.table(
            columns=[
                {"name": "f", "label": "功能", "field": "f", "align": "left"},
                {"name": "w", "label": "网页版（本站）", "field": "w", "align": "left"},
                {"name": "l", "label": "本地版", "field": "l", "align": "left"},
            ],
            rows=[{"f": f, "w": w, "l": l} for f, w, l in _MATRIX],
        ).classes("cf-dense w-full").props("dense flat hide-bottom")

        # ---- 安装 ----
        ui.label("三步装好").classes("text-base font-bold mt-2")
        ui.label("需要 Python 3.11 以上。Windows / macOS / Linux 都一样。") \
            .classes("text-xs").style(f"color:{theme.NEUTRAL}")
        ui.code(f"git clone {REPO}.git\n"
                "cd earnfarm\n"
                "pip install -r requirements.txt\n"
                "python run.py", language="bash").classes("w-full")
        ui.label("跑起来后浏览器打开 http://localhost:8777 即可。"
                 "首次使用先设一个主密码，之后 API 凭据都用它加密存在本机。") \
            .classes("text-xs").style(f"color:{theme.NEUTRAL}")

        with ui.row().classes("items-center gap-3 mt-1"):
            ui.button("在 GitHub 上打开", icon="open_in_new",
                      on_click=lambda: ui.navigate.to(REPO, new_tab=True)) \
                .props("unelevated")
            ui.link("直接下载 ZIP", f"{REPO}/archive/refs/heads/master.zip") \
                .classes("text-sm")

        # ---- 说清楚它不是什么 ----
        ui.label("先说清楚").classes("text-base font-bold mt-3")
        for line in (
            "这不是荐股工具。它算的是成本、资金费和历史一致性，"
            "实测结论是绝大多数「高年化机会」扣完成本是负期望。",
            "AI 报告基于你自己的成交与公开行情生成，只做复盘和解释，"
            "不预测行情，也不构成投资建议。",
            "网页版不保存任何人的交易所密钥：加密后存在你自己的浏览器里，"
            "签名也在你的浏览器里算。",
        ):
            ui.label("· " + line).classes("text-xs") \
                .style(f"color:{theme.NEUTRAL}")
