"""网页访问闸：整站密码登录 + 首次访问自助设置密码。

为什么必须有它才能挂公网：AppState / Session 是**所有客户端共享**的一份
（这是本地单人工具的正确设计），也就是说你解锁了 vault，任何访客打开
页面看到的就是已解锁的界面。没有这道闸，公网部署等于把账户拱手送人。

三种运行形态：
- 都没设 → **本地模式**，无登录闸（本机自用，默认行为不变）；
- ``EARNFARM_WEB_AUTH=1`` → **在线模式**，密码由**用户首次访问时自己设**，
  scrypt 哈希存进数据目录，明文不落盘；
- ``EARNFARM_WEB_PASSWORD=xxx`` → 在线模式且密码由运维固定（基础设施化管理，
  也是忘记密码时的应急通道，优先级最高）。

首次设置为什么有时间窗：服务一旦挂上公网，"谁先访问谁设密码"就是抢注
漏洞——爬虫比你先到一步，站点就归它了。这里不让用户去日志里抄令牌，
改成**重启后 15 分钟内**才允许设置：重启是只有服务器管理者能做的动作，
等于把"证明你是主人"这件事从抄码变成了重启。窗口过期就再重启一次。

这里不是多用户系统，是单人自托管的门锁：一个密码、失败退避，仅此而已。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import Client, app, ui
from starlette.middleware.base import BaseHTTPMiddleware

from . import theme

WEB_PASSWORD_ENV = "EARNFARM_WEB_PASSWORD"
WEB_AUTH_ENV = "EARNFARM_WEB_AUTH"

# 网页密码的 scrypt 参数。比 vault 的 2^17 轻一档：这个哈希在**每次登录**
# 都要算，2^15 约 30ms，既不拖慢登录，离线爆破的代价也远高于裸哈希
_SCRYPT_N = 1 << 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32
_MIN_LEN = 8

# 登录失败的退避：连错越多等越久，上限 8 秒。
# 不做 IP 维度（反代后全是同一个来源 IP），全局退避对单人工具足够
_FAIL_DELAY_MAX_S = 8.0
_fail_count = 0

_UNRESTRICTED = {"/login", "/setup"}


def gate_enabled() -> bool:
    """登录闸是否启用（= 在线模式）。"""
    if os.environ.get(WEB_PASSWORD_ENV, "").strip():
        return True
    return os.environ.get(WEB_AUTH_ENV, "").strip().lower() in ("1", "true", "yes", "on")


# ---- 密码存储 ------------------------------------------------------------

def _password_path(data_dir: Path) -> Path:
    return data_dir / "web_password.json"


# 首次设置窗口：进程启动时刻 + SETUP_WINDOW_S。设 0 = 永不过期（本地/内网用）
SETUP_WINDOW_ENV = "EARNFARM_SETUP_WINDOW_S"
_DEFAULT_SETUP_WINDOW_S = 900
_started_at = time.monotonic()


def setup_window_s() -> int:
    raw = os.environ.get(SETUP_WINDOW_ENV, "").strip()
    if raw.isdigit():
        return int(raw)
    return _DEFAULT_SETUP_WINDOW_S


def setup_open() -> bool:
    """首次设置窗口是否还开着。0 = 不限时。"""
    window = setup_window_s()
    if window <= 0:
        return True
    return time.monotonic() - _started_at < window


def setup_left_s() -> int:
    return max(0, int(setup_window_s() - (time.monotonic() - _started_at)))


def password_configured(data_dir: Path) -> bool:
    """密码是否已经设过。环境变量指定的密码同样算已设。"""
    if os.environ.get(WEB_PASSWORD_ENV, "").strip():
        return True
    return _password_path(data_dir).exists()


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N,
                          r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN,
                          maxmem=64 * 1024 * 1024)


def set_password(data_dir: Path, password: str) -> None:
    """首次设置（或重设）网页密码。只存 scrypt 哈希，明文不落盘。"""
    if len(password) < _MIN_LEN:
        raise ValueError(f"访问密码至少 {_MIN_LEN} 位")
    salt = secrets.token_bytes(16)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = _password_path(data_dir)
    path.write_text(json.dumps({
        "salt": salt.hex(), "hash": _derive(password, salt).hex(),
    }), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass          # Windows 上不支持，不影响功能


def verify_password(data_dir: Path, password: str) -> bool:
    env = os.environ.get(WEB_PASSWORD_ENV, "").strip()
    if env:
        return secrets.compare_digest(password, env)
    path = _password_path(data_dir)
    if not path.exists():
        return False
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        salt = bytes.fromhex(blob["salt"])
        expected = bytes.fromhex(blob["hash"])
    except (ValueError, KeyError, OSError):
        return False
    return secrets.compare_digest(_derive(password, salt), expected)


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


# ---- 中间件与页面 --------------------------------------------------------

class _AuthMiddleware(BaseHTTPMiddleware):
    """只拦页面路由：静态资源和 NiceGUI 内部通道放行——
    拦了它们连登录页本身都渲染不出来。"""

    def __init__(self, app_, data_dir: Path) -> None:
        super().__init__(app_)
        self._data_dir = data_dir

    async def dispatch(self, request: Request, call_next):
        if not app.storage.user.get("authenticated", False):
            path = request.url.path
            if path in Client.page_routes.values() and path not in _UNRESTRICTED:
                app.storage.user["referrer_path"] = path
                # 还没设过密码 → 先去设置页，而不是把人挡在一个永远进不去的登录框前
                target = "/login" if password_configured(self._data_dir) else "/setup"
                return RedirectResponse(target)
        return await call_next(request)


def install_gate(data_dir: Path) -> None:
    """注册中间件和 /login、/setup 页。只在 gate_enabled() 时调用。"""
    app.add_middleware(_AuthMiddleware, data_dir=data_dir)

    if not password_configured(data_dir):
        w = setup_window_s()
        print(f"[earnfarm] 尚未设置网页访问密码。打开 /setup 直接设定，"
              f"设置窗口开放 {w} 秒（过期重启本服务即可重新开放）。", flush=True)

    def _shell(title: str):
        ui.add_head_html(f"<style>{theme.GLOBAL_CSS}</style>")
        ui.dark_mode(False)
        col = ui.column().classes("absolute-center items-center gap-3")
        with col:
            ui.label("earnfarm").classes("text-2xl font-bold cf-mono")
            ui.label(title).classes("text-sm").style(f"color:{theme.NEUTRAL}")
        return col

    @ui.page("/setup")
    def setup_page() -> None:
        if password_configured(data_dir):
            ui.navigate.to("/login")
            return

        if not setup_open():
            with _shell("首次设置窗口已关闭"):
                ui.label("为防止站点被陌生人抢先占用，密码只能在服务重启后的"
                         f" {setup_window_s()} 秒内设置。请重启服务后再打开本页：\n"
                         "systemctl restart earnfarm") \
                    .classes("text-sm w-80").style("white-space:pre-line")
            return

        def do_setup() -> None:
            if not setup_open():
                ui.notify("设置窗口已过期，请重启服务后重试", type="negative")
                return
            if (pw1.value or "") != (pw2.value or ""):
                ui.notify("两次输入的密码不一致", type="negative")
                return
            try:
                set_password(data_dir, pw1.value or "")
            except ValueError as exc:
                ui.notify(str(exc), type="negative")
                return
            app.storage.user["authenticated"] = True
            ui.notify("密码已设置", type="positive")
            ui.navigate.to(app.storage.user.get("referrer_path", "/"))

        with _shell("首次设置：请设定你自己的访问密码"):
            pw1 = ui.input("设置访问密码（至少 8 位）", password=True,
                           password_toggle_button=True).props("outlined").classes("w-72")
            pw2 = ui.input("再输一次", password=True) \
                .props("outlined").classes("w-72")
            pw2.on("keydown.enter", do_setup)
            ui.button("设置并进入", on_click=do_setup).props("unelevated").classes("w-72")
            ui.label(f"设置窗口在服务重启后开放 {setup_window_s() // 60} 分钟"
                     "（防止站点被陌生人抢先占用），过期重启服务即可重开。") \
                .classes("text-xs w-72").style(f"color:{theme.NEUTRAL}")

    @ui.page("/login")
    def login_page() -> None:
        if not password_configured(data_dir):
            ui.navigate.to("/setup")
            return

        async def try_login() -> None:
            global _fail_count
            # 退避在校验**之前**：连错后每次尝试都变慢，暴力破解没有快路径
            if _fail_count:
                await asyncio.sleep(min(1.5 * _fail_count, _FAIL_DELAY_MAX_S))
            if verify_password(data_dir, pw.value or ""):
                _fail_count = 0
                app.storage.user["authenticated"] = True
                ui.navigate.to(app.storage.user.get("referrer_path", "/"))
            else:
                _fail_count += 1
                ui.notify("密码错误", type="negative")

        with _shell("请输入访问密码"):
            pw = ui.input("访问密码", password=True, password_toggle_button=True) \
                .props("outlined").classes("w-72")
            pw.on("keydown.enter", try_login)
            ui.button("进入", on_click=try_login).props("unelevated").classes("w-72")
            ui.label("忘记密码：删除服务器数据目录里的 web_password.json 后重启服务，"
                     "即可用新令牌重新设置。") \
                .classes("text-xs w-72").style(f"color:{theme.NEUTRAL}")
