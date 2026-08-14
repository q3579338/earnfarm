"""网页访问闸：整站密码登录。

为什么必须有它才能挂公网：AppState / Session 是**所有客户端共享**的一份
（这是本地单人工具的正确设计），也就是说你解锁了 vault，任何访客打开
页面看到的就是已解锁的界面。没有这道闸，公网部署等于把账户拱手送人。

启用方式：设置环境变量 ``EARNFARM_WEB_PASSWORD``。
- 没设 = 本地模式，无登录（默认行为不变，回环地址照旧直接用）；
- 设了 = 所有页面先过 /login，密码对了才放行，会话记在浏览器 cookie 里
  （storage_secret 持久化在数据目录，重启服务不掉线）。

这里不是完整的多用户系统，是单人自托管的门锁：一个密码、失败退避、
仅此而已。要多用户/审计，请在反代层（Cloudflare Access 等）做。
"""

from __future__ import annotations

import asyncio
import os
import secrets
from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import Client, app, ui
from starlette.middleware.base import BaseHTTPMiddleware

from . import theme

WEB_PASSWORD_ENV = "EARNFARM_WEB_PASSWORD"

# 登录失败的退避：连错越多等越久，上限 8 秒。
# 不做 IP 维度（反代后全是同一个来源 IP），全局退避对单人工具足够
_FAIL_DELAY_MAX_S = 8.0
_fail_count = 0

_UNRESTRICTED = {"/login"}


def gate_enabled() -> bool:
    return bool(os.environ.get(WEB_PASSWORD_ENV, "").strip())


def storage_secret(data_dir: Path) -> str:
    """storage_secret 持久化：换一个 secret 等于让所有已登录会话全部掉线，
    所以生成一次就存进数据目录，重启复用。"""
    f = data_dir / "web_secret"
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    data_dir.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_hex(32)
    f.write_text(secret, encoding="utf-8")
    return secret


class _AuthMiddleware(BaseHTTPMiddleware):
    """只拦页面路由：静态资源和 NiceGUI 内部通道放行——
    拦了它们连 /login 页本身都渲染不出来。"""

    async def dispatch(self, request: Request, call_next):
        if not app.storage.user.get("authenticated", False):
            path = request.url.path
            if path in Client.page_routes.values() and path not in _UNRESTRICTED:
                app.storage.user["referrer_path"] = path
                return RedirectResponse("/login")
        return await call_next(request)


def install_gate() -> None:
    """注册中间件和 /login 页。只在 gate_enabled() 时调用。"""
    app.add_middleware(_AuthMiddleware)

    @ui.page("/login")
    def login_page() -> None:
        ui.add_head_html(f"<style>{theme.GLOBAL_CSS}</style>")
        ui.dark_mode(False)

        async def try_login() -> None:
            global _fail_count
            # 退避在校验**之前**：连错后每次尝试都变慢，暴力破解没有快路径
            if _fail_count:
                await asyncio.sleep(min(1.5 * _fail_count, _FAIL_DELAY_MAX_S))
            expected = os.environ.get(WEB_PASSWORD_ENV, "").strip()
            if expected and secrets.compare_digest(pw.value or "", expected):
                _fail_count = 0
                app.storage.user["authenticated"] = True
                ui.navigate.to(app.storage.user.get("referrer_path", "/"))
            else:
                _fail_count += 1
                ui.notify("密码错误", type="negative")

        with ui.column().classes("absolute-center items-center gap-3"):
            ui.label("earnfarm").classes("text-2xl font-bold cf-mono")
            pw = ui.input("访问密码", password=True, password_toggle_button=True) \
                .props("outlined").classes("w-72")
            pw.on("keydown.enter", try_login)
            ui.button("进入", on_click=try_login).props("unelevated").classes("w-72")
            ui.label("单人自托管的门锁。密码在服务器的 EARNFARM_WEB_PASSWORD 环境变量里。") \
                .classes("text-xs").style(f"color:{theme.NEUTRAL}")
