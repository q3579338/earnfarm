"""操作复盘 / 单币分析页（/analysis）。

两种模式：
- 操作复盘：拉**我的真实成交**（需只读 API 凭据），复盘自己的操作；
- 单币分析：只拉币安公开行情（免凭据），分析标的本身。

任务在**模块级后台状态**里跑（nicegui background_tasks + 共享 _Job 对象）：
切到别的页、刷新、换标签页回来，分析都还在跑，进度和结果照常显示。
这一页有一个 1 秒的渲染定时器——它只把后台任务的状态搬上屏幕，从不发任何
网络请求，与"行情不自动刷新"的全站纪律不冲突。

凭据纪律：复盘凭据（只读）与套利账户同库同加密但 kind 隔离；AI API key
同库加密（secret:ai:*）；发给 AI 的只有行情/成交数据，没有任何密钥材料。
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from nicegui import app, background_tasks, run, ui

from ..analysis import (
    API_PRESET_LABELS,
    API_PRESETS,
    ENGINE_LABELS,
    AnalysisError,
    ApiOptions,
    build_instruction,
    build_market_instruction,
    build_resolve_instruction,
    bundle_payload,
    collect_market,
    collect_operations,
    engine_available,
    fetch_public_symbols,
    market_payload,
    parse_resolved,
    resolve_local,
    run_engine,
    suggest_symbols,
)
from ..config import ANALYSIS_ENGINES, Config
from ..models import Venue
from ..session import CREDENTIAL_FIELDS, FIELD_LABELS, VENUE_FIELD_LABELS, Session
from ..vault import BadPassword, VaultLocked
from . import browser_creds, theme
from .access import gate_enabled
from .accounts import VENUE_LABELS
from .nav import module_nav

DAYS_CHOICES = {1: "1 天", 2: "2 天", 3: "3 天", 7: "1 周",
                14: "2 周", 30: "1 个月", 90: "3 个月"}

_HISTORY_SHOW_N = 50


# ================= 后台任务状态（模块级，所有客户端共享）=================

@dataclass(slots=True)
class _EngineRun:
    status: str = "running"          # running / done / failed
    report: str = ""
    error: str = ""


@dataclass(slots=True)
class _Job:
    title: str
    stage: str = "准备中…"
    engines: dict[str, _EngineRun] = field(default_factory=dict)
    error: str = ""                  # 引擎开跑前的整体失败（解析/拉数据）
    infos: list[str] = field(default_factory=list)   # 凭据保存结果之类的提示
    finished: bool = False
    started_at: float = field(default_factory=time.time)

    @property
    def signature(self) -> tuple:
        """渲染去抖用：状态没变就不重画，避免每秒重建大段 markdown。"""
        return (self.stage, self.error, self.finished, tuple(self.infos),
                tuple((e, r.status) for e, r in self.engines.items()))


# 任务槽按 (访客, 模块) 分：同一个人的复盘和单币互不占用，
# **不同访客之间更是互不可见**——这工具要给别人用，共用一个槽等于
# 别人跑的分析会顶掉你的、还能看见你的报告
_jobs: dict[tuple[str, str], _Job] = {}


def _visitor_id() -> str:
    """访客标识。本地单人模式恒为 local；线上模式用浏览器 id（cookie 里的随机串）。"""
    if not gate_enabled():
        return "local"
    try:
        return str(app.storage.browser.get("id", "anon"))[:16] or "anon"
    except Exception:
        return "anon"


def _job_key(page_mode: str) -> tuple[str, str]:
    return (_visitor_id(), page_mode)


def _reports_dir(config: Config) -> Path:
    """报告目录。线上模式按访客分目录——别人的复盘报告不该出现在你的列表里。"""
    d = config.data_dir / "analysis"
    vid = _visitor_id()
    if vid != "local":
        d = d / f"v-{vid}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _report_path(config: Config, prefix: str, symbols: list[str], engine: str) -> Path:
    stem = "-".join(symbols)[:40] or "report"
    return _reports_dir(config) / \
        f"{prefix}{stem}-{time.strftime('%Y%m%d-%H%M%S')}-{engine}.md"


async def _run_job(job: _Job, *, session: Session, config: Config, mode: str,
                   query: str, days: int, focus: str, engines: list[str],
                   adapter, save_cred: tuple[Venue, str, dict] | None,
                   api_opts: ApiOptions | None, workdir: Path) -> None:
    """后台执行体。只碰 job 对象和磁盘，绝不碰任何 UI 元素——
    UI 元素属于某个客户端连接，客户端一断它们就死了，任务不能陪葬。

    workdir 必须由调用方算好传进来：它依赖访客身份（cookie），而后台任务
    里没有客户端上下文，现算会抛 "no client context"。
    """
    cfg = config.analysis
    try:
        # ---- 标的解析 ----
        job.stage = "解析标的…"
        if mode == "ops":
            instruments = await adapter.fetch_instruments()
            available = {i.symbol for i in instruments}
        else:
            available = await fetch_public_symbols()
        symbols, unresolved = resolve_local(available, query)
        if unresolved:
            job.stage = (f"「{'、'.join(unresolved)}」本地认不出，"
                         f"请 {ENGINE_LABELS[engines[0]]} 帮忙认…")
            reply = await run.io_bound(
                run_engine, engines[0], build_resolve_instruction(unresolved),
                "\n".join(sorted(available)), cfg,
                workdir=workdir, api_opts=api_opts)
            ai_hits = parse_resolved(reply, available)
            if ai_hits:
                symbols.extend(s for s in ai_hits if s not in symbols)
            else:
                hints = []
                for t in unresolved:
                    close = suggest_symbols(available, t)
                    if close:
                        hints.append(f"{t} → 可能是 {' / '.join(close)}")
                raise AnalysisError(
                    f"认不出标的「{'、'.join(unresolved)}」。"
                    + ("；".join(hints) + "——直接填符号再试。" if hints
                       else "试试直接填交易所符号（如 SKHYUSDT）。"))
        if not symbols:
            raise AnalysisError("没有解析出任何标的")
        job.title = f"{'、'.join(symbols)}（{'复盘' if mode == 'ops' else '单币'}）"

        # ---- 拉数据 ----
        job.stage = f"标的：{'、'.join(symbols)}，拉取数据…"
        since_ms = int((time.time() - days * 86_400) * 1000)
        if mode == "ops":
            bundle = await collect_operations(adapter, symbols, since_ms)
            if bundle.fill_count == 0:
                raise AnalysisError(f"{'、'.join(symbols)} 在回看期内没有任何成交——"
                                    "检查标的或放宽回看期")
            payload = bundle_payload(bundle, max_fills=cfg.max_fills)
            instruction = build_instruction(bundle, focus)
            count_txt = f"共 {bundle.fill_count} 笔成交"
        else:
            mbundle = await collect_market(symbols, days)
            payload = market_payload(mbundle)
            instruction = build_market_instruction(mbundle, focus)
            count_txt = f"{mbundle.interval} K 线 × {len(mbundle.sections)} 个标的"

        # ---- 数据都拉回来了 = 凭据可用，这时保存才有意义 ----
        if save_cred is not None:
            venue, alias, cred = save_cred
            try:
                session.add_analysis_account(venue, alias, cred,
                                             int(time.time() * 1000))
                job.infos.append(f"凭据已加密保存：{alias}")
            except VaultLocked:
                job.infos.append("凭据未保存：凭据库未解锁（本次分析继续）")
            except Exception as exc:
                job.infos.append(f"凭据未保存：{exc}（本次分析继续）")

        # ---- 并行分析 ----
        job.stage = f"{count_txt}，{len(engines)} 个引擎并行分析中…"
        for e in engines:
            job.engines[e] = _EngineRun()

        async def one(e: str) -> None:
            r = job.engines[e]
            try:
                report = await run.io_bound(
                    run_engine, e, instruction, payload, cfg,
                    workdir=workdir, api_opts=api_opts)
                r.report = report
                r.status = "done"
                prefix = "" if mode == "ops" else "market-"
                stem = "-".join(symbols)[:40] or "report"
                name = f"{prefix}{stem}-{time.strftime('%Y%m%d-%H%M%S')}-{e}.md"
                (workdir / name).write_text(report, encoding="utf-8")
            except Exception as exc:
                r.error = str(exc)
                r.status = "failed"

        await asyncio.gather(*(one(e) for e in engines))
        job.stage = f"{time.strftime('%H:%M:%S')} 完成"
    except AnalysisError as exc:
        job.error = str(exc)
    except Exception as exc:
        job.error = f"分析失败：{exc}"
    finally:
        job.finished = True
        if adapter is not None:
            try:
                await adapter.close()
            except Exception:
                pass


# ================= 页面 =================

def build_analysis_page(session: Session, config: Config,
                        page_mode: str = "ops") -> None:
    """page_mode="ops" 是操作复盘（/analysis），"market" 是单币分析（/market）。

    两个导航模块共用同一套引擎、后台任务和历史报告——差别只在数据来源：
    复盘拉我的成交（需凭据），单币只拉公开行情（免凭据）。
    """
    ui.add_head_html(f"<style>{theme.GLOBAL_CSS}</style>")
    ui.dark_mode(False)
    # 线上模式（多人共用）：凭据一律存访客自己的浏览器，服务器不托管任何人的密钥
    online = gate_enabled()
    if online:
        browser_creds.install()

    with ui.header().classes("items-center justify-between px-4 py-2") \
            .style("background:#ffffff; color:#18181b; "
                   "border-bottom:1px solid #e4e4e7; box-shadow:none"):
        module_nav("analysis" if page_mode == "ops" else "market")

    with ui.column().classes("p-4 gap-3 w-full max-w-4xl"):
        if page_mode == "ops":
            ui.label("复盘我的成交（需只读凭据），由选中的 AI 引擎生成报告。"
                     "发给 AI 的只有成交数据，不含任何密钥。"
                     "任务在后台跑——切走再回来，进度和结果都在。") \
                .classes("text-xs").style(f"color:{theme.NEUTRAL}")
            if online:
                ui.label("🔒 你的 API 密钥用你自己设的解锁密码在**浏览器里**加密，"
                         "存在你这台设备上，服务器不保存。分析时服务器会短暂用到"
                         "明文（币安签名必须在服务端算），用完即弃、不写盘。") \
                    .classes("text-xs").style(f"color:{theme.SAFE}")
        else:
            ui.label("只用币安公开行情分析标的本身——K 线、量能、资金费、基差，"
                     "不需要任何交易所凭据。AI 引擎可多选并行，各出一份报告。"
                     "任务在后台跑——切走再回来，进度和结果都在。") \
                .classes("text-xs").style(f"color:{theme.NEUTRAL}")

        # ================= 主密码（就地解锁）=================
        # 只在**本地单人模式**出现：那份 vault 是服务器/本机保管的。
        # 线上多人模式没有 vault——每个访客的密钥在他自己浏览器里，
        # 服务器不托管，自然也就没有"主密码"这回事
        vault_box = None
        if page_mode == "ops" and not online:
            vault_box = ui.column().classes("gap-1 w-full")

        def render_vault_state() -> None:
            if vault_box is None:
                return
            vault_box.clear()
            with vault_box:
                if not session.is_initialized:
                    with ui.row().classes("items-center gap-2"):
                        pw = ui.input("设置主密码（至少 8 位）") \
                            .props("dense outlined type=password").classes("w-64")

                        def do_init() -> None:
                            try:
                                session.initialize(pw.value or "")
                                ui.notify("主密码已设置，凭据库已解锁", type="positive")
                                render_vault_state()
                                refresh_saved()
                            except Exception as exc:
                                ui.notify(str(exc), type="negative")

                        ui.button("设置", on_click=do_init).props("dense unelevated")
                        pw.on("keydown.enter", lambda e: do_init())
                    ui.label("保存凭据前要先设主密码：加密密钥由它派生，密码本身不落盘。"
                             "不设也能分析，只是凭据每次要重填。") \
                        .classes("text-xs").style(f"color:{theme.NEUTRAL}")
                elif not session.is_unlocked:
                    with ui.row().classes("items-center gap-2"):
                        pw = ui.input("主密码") \
                            .props("dense outlined type=password").classes("w-64")

                        def do_unlock() -> None:
                            try:
                                session.unlock(pw.value or "")
                                ui.notify("凭据库已解锁", type="positive")
                                render_vault_state()
                                refresh_saved()
                            except BadPassword:
                                ui.notify("主密码错误", type="negative")
                            except Exception as exc:
                                ui.notify(str(exc), type="negative")

                        ui.button("解锁", on_click=do_unlock).props("dense unelevated")
                        pw.on("keydown.enter", lambda e: do_unlock())

                        async def do_reset() -> None:
                            with ui.dialog() as dlg, ui.card():
                                ui.label("重置凭据库？")
                                ui.label("已保存的全部 API 凭据、AI key 将被删除"
                                         "（没有主密码本来也解不开了），旧主密码作废，"
                                         "之后可重新设置。行情历史等其他数据不受影响。") \
                                    .classes("text-xs").style(f"color:{theme.NEUTRAL}")
                                with ui.row():
                                    ui.button("重置", on_click=lambda: dlg.submit(True)) \
                                        .props("color=red unelevated")
                                    ui.button("取消", on_click=lambda: dlg.submit(False)) \
                                        .props("flat")
                            if await dlg:
                                try:
                                    session.reset_vault()
                                    ui.notify("凭据库已重置，请设置新主密码",
                                              type="positive")
                                    render_vault_state()
                                    refresh_saved()
                                except Exception as exc:
                                    ui.notify(str(exc), type="negative")

                        ui.button("忘记密码？重置", on_click=do_reset) \
                            .props("flat dense no-caps") \
                            .tooltip("删除已存凭据和旧密码，重新开始。密文没有主密码"
                                     "永远解不开，重置不会丢失任何还能用的东西")
                    ui.label("凭据库未解锁：解锁后才能使用已存凭据 / 保存新凭据。"
                             "这个密码是首次在本机运行时设置的，不来自代码仓库。") \
                        .classes("text-xs").style(f"color:{theme.WARN}")
                else:
                    ui.label("✓ 凭据库已解锁").classes("text-xs") \
                        .style(f"color:{theme.SAFE}")

        # ================= 凭据（仅操作复盘模式）=================
        with ui.column().classes("gap-2 w-full") as cred_area:
            source = ui.radio({"saved": "已存复盘凭据", "fresh": "新填凭据"},
                              value="saved").props("inline dense")

            with ui.column().classes("gap-2 w-full") as saved_box:
                with ui.row().classes("items-center gap-2"):
                    saved_sel = ui.select({}, label="复盘凭据") \
                        .props("dense outlined").classes("w-80")
                    del_btn = ui.button(icon="delete").props("dense flat") \
                        .tooltip("删除选中的复盘凭据")
                # 线上：解锁密码在浏览器里解密，服务器不存这个密码也不存密文
                unlock_in = ui.input("解锁密码", password=True) \
                    .props("dense outlined").classes("w-80") \
                    .tooltip("你保存凭据时设的那个密码。密钥在你本机加密，"
                             "解密也在你本机进行")
                unlock_in.set_visibility(online)
                saved_hint = ui.label().classes("text-xs").style(f"color:{theme.WARN}")

            with ui.column().classes("gap-2 w-full") as fresh_box:
                with ui.row().classes("items-center gap-3 flex-wrap"):
                    venue_sel = ui.select(
                        {v.value: VENUE_LABELS.get(v, v.value)
                         for v in CREDENTIAL_FIELDS},
                        value=Venue.BINANCE.value, label="交易所") \
                        .props("dense outlined").classes("w-60")
                    alias_in = ui.input("别名（保存时用）", placeholder="如 币安只读") \
                        .props("dense outlined").classes("w-48")
                cred_inputs: dict[str, ui.input] = {}
                cred_fields_box = ui.column().classes("gap-2 w-full")

                def rebuild_cred_fields() -> None:
                    cred_inputs.clear()
                    cred_fields_box.clear()
                    venue = Venue(venue_sel.value)
                    labels = {**FIELD_LABELS, **VENUE_FIELD_LABELS.get(venue, {})}
                    with cred_fields_box:
                        for f in CREDENTIAL_FIELDS[venue]:
                            cred_inputs[f] = ui.input(labels[f]) \
                                .props("dense outlined type=password").classes("w-96")

                rebuild_cred_fields()
                venue_sel.on_value_change(lambda e: rebuild_cred_fields())
                save_chk = ui.checkbox(
                    "保存供下次使用（加密存在**你的浏览器**里，服务器不保存）"
                    if online else "保存供下次使用（加密进本地库，需主密码）",
                    value=True)
                # 线上保存要一个只有访客自己知道的解锁密码——它就是浏览器里
                # 那份密文的钥匙，服务器不保存它，忘了就只能重填 API
                new_pass_in = ui.input("给这条凭据设一个解锁密码（至少 6 位）",
                                       password=True, password_toggle_button=True) \
                    .props("dense outlined").classes("w-96")
                new_pass_in.set_visibility(online)
                save_chk.on_value_change(
                    lambda e: new_pass_in.set_visibility(online and bool(e.value)))
                ui.label("只开读取权限（查询成交/持仓），不要给交易和提币权限，"
                         "建议绑 IP 白名单。") \
                    .classes("text-xs").style(f"color:{theme.NEUTRAL}")

        def refresh_saved(select_id: str | None = None) -> None:
            """本地模式：列 vault 里的复盘凭据。线上模式请用 refresh_saved_browser。"""
            if online:
                return
            accounts = session.list_analysis_accounts()
            options = {a.id: f"{a.alias}（{VENUE_LABELS.get(a.venue, a.venue.value)}）"
                       for a in accounts}
            saved_sel.set_options(options,
                                  value=select_id or (next(iter(options), None)))
            if not options:
                saved_hint.text = "还没有保存过复盘凭据——切到「新填凭据」加一个"
            elif not session.is_unlocked:
                saved_hint.text = "凭据库未解锁：先解锁，才能使用已存凭据"
            else:
                saved_hint.text = ""

        async def refresh_saved_browser(select: str | None = None) -> None:
            """线上模式：列**这台浏览器**里存的凭据（服务器上没有任何人的密钥）。"""
            aliases = await browser_creds.list_aliases()
            options = {a: a for a in aliases}
            saved_sel.set_options(options, value=select or (next(iter(options), None)))
            saved_hint.text = "" if options else \
                "这台浏览器里还没存过凭据——切到「新填凭据」加一个"

        async def delete_saved() -> None:
            if not saved_sel.value:
                return
            label = saved_sel.options.get(saved_sel.value, saved_sel.value)
            with ui.dialog() as dlg, ui.card():
                ui.label(f"删除凭据 {label}？密文一并删除，不可恢复。")
                with ui.row():
                    ui.button("删除", on_click=lambda: dlg.submit(True)) \
                        .props("color=red unelevated")
                    ui.button("取消", on_click=lambda: dlg.submit(False)).props("flat")
            if await dlg:
                if online:
                    await browser_creds.delete(saved_sel.value)
                    await refresh_saved_browser()
                else:
                    session.delete_account(saved_sel.value)
                    refresh_saved()

        del_btn.on_click(delete_saved)

        def sync_source() -> None:
            saved_box.set_visibility(source.value == "saved")
            fresh_box.set_visibility(source.value == "fresh")

        # ================= 引擎 =================
        ui.label("分析引擎（可多选，并行各出一份报告）").classes("text-xs") \
            .style(f"color:{theme.NEUTRAL}")
        engine_checks: dict[str, ui.checkbox] = {}
        avail = {e: engine_available(e, config.analysis)
                 for e in ANALYSIS_ENGINES if e != "api"}
        defaults = [e for e in config.analysis.default_engines
                    if e == "api" or avail.get(e, (False, ""))[0]] \
            or [e for e, (ok, _) in avail.items() if ok][:1]
        with ui.row().classes("items-center gap-4 flex-wrap"):
            for e in ANALYSIS_ENGINES:
                label = ENGINE_LABELS[e]
                if e != "api" and not avail[e][0]:
                    label += f"（{avail[e][1]}）"
                engine_checks[e] = ui.checkbox(label, value=e in defaults)

        # ---- 线上 API 配置（勾中 api 才显示）----
        with ui.column().classes("gap-2 w-full pl-2") as api_box:
            with ui.row().classes("items-center gap-3 flex-wrap"):
                provider_sel = ui.select(API_PRESET_LABELS, value="deepseek",
                                         label="服务商") \
                    .props("dense outlined").classes("w-44")
                api_model_in = ui.input("模型") \
                    .props("dense outlined").classes("w-52")
                api_style_sel = ui.select(
                    {"openai": "openai 报文", "anthropic": "anthropic 报文"},
                    label="报文风格").props("dense outlined").classes("w-40")
            api_url_in = ui.input("接口 URL").props("dense outlined").classes("w-full")
            with ui.row().classes("items-center gap-3 flex-wrap"):
                api_key_in = ui.input("API Key（留空则用已存的 / 环境变量）") \
                    .props("dense outlined type=password").classes("w-96")
                api_remember = ui.checkbox(
                    "存在我的浏览器里（服务器不保存）" if online else "加密保存这个 key（需解锁）",
                    value=True)
            api_pass_in = ui.input("这个 key 的解锁密码（至少 6 位）", password=True) \
                .props("dense outlined").classes("w-96") \
                .tooltip("AI key 也在你本机加密后存进浏览器，服务器不保管")
            api_pass_in.set_visibility(online)
            api_hint = ui.label().classes("text-xs").style(f"color:{theme.NEUTRAL}")
            if page_mode == "market" and not online:
                ui.label("下面的主密码只在「加密保存 key」时需要——"
                         "不解锁也能直接分析（key 本次有效或走环境变量）。") \
                    .classes("text-xs").style(f"color:{theme.NEUTRAL}")
                vault_box = ui.column().classes("gap-1 w-full")

        def apply_preset() -> None:
            p = API_PRESETS.get(provider_sel.value)
            if p is not None:
                api_model_in.value = p.model
                api_url_in.value = p.url
                api_style_sel.value = p.style
            if online:
                api_hint.text = ("key 加密存在你自己的浏览器里；"
                                 "留空 + 填解锁密码 = 用已存的那把")
                return
            saved_key = session.has_ai_key(provider_sel.value)
            env_key = bool(os.environ.get(config.analysis.api_key_env, "").strip())
            bits = []
            if saved_key:
                bits.append("已有加密保存的 key ✓（留空即用）")
            if env_key:
                bits.append(f"环境变量 {config.analysis.api_key_env} 已设 ✓")
            if not bits:
                bits.append("还没有 key：填一个（勾选保存则加密入库）")
            api_hint.text = "；".join(bits)

        apply_preset()
        provider_sel.on_value_change(lambda e: apply_preset())

        def sync_api_box() -> None:
            api_box.set_visibility(bool(engine_checks["api"].value))

        engine_checks["api"].on_value_change(lambda e: sync_api_box())

        # ================= 参数 =================
        with ui.row().classes("items-center gap-3 flex-wrap"):
            symbol_in = ui.input("标的（可模糊，多个用空格分开）",
                                 placeholder="如：海力士 / BTC / SKHYUSDT") \
                .props("dense outlined").classes("w-96")
            days_sel = ui.select(DAYS_CHOICES, value=config.analysis.default_days,
                                 label="回看期").props("dense outlined").classes("w-32")
        focus_in = ui.textarea("额外关注（可选）",
                               placeholder="想让报告特别分析什么？") \
            .props("dense outlined").classes("w-full")

        with ui.row().classes("items-center gap-3"):
            run_btn = ui.button("开始分析", icon="analytics").props("unelevated")
            spinner = ui.spinner(size="sm")
            status = ui.label().classes("text-xs").style(f"color:{theme.NEUTRAL}")

        results_box = ui.column().classes("w-full gap-2")

        # ================= 历史报告 =================
        with ui.expansion("历史报告", icon="history").classes("w-full") as history_exp:
            history_box = ui.column().classes("w-full gap-1")

        def refresh_history() -> None:
            history_box.clear()
            files = sorted(_reports_dir(config).glob("*.md"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            with history_box:
                if not files:
                    ui.label("还没有报告。").classes("text-xs") \
                        .style(f"color:{theme.NEUTRAL}")
                for p in files[:_HISTORY_SHOW_N]:
                    with ui.row().classes("items-center gap-2"):
                        ui.label(time.strftime("%m-%d %H:%M",
                                               time.localtime(p.stat().st_mtime))) \
                            .classes("text-xs cf-mono").style(f"color:{theme.NEUTRAL}")
                        ui.button(p.stem, on_click=lambda p=p: show_report(p)) \
                            .props("flat dense no-caps")

        def show_report(path: Path) -> None:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                ui.notify(f"读取失败：{exc}", type="negative")
                return
            with ui.dialog() as dlg, ui.card().classes("w-full max-w-3xl"):
                ui.label(path.name).classes("text-xs cf-mono") \
                    .style(f"color:{theme.NEUTRAL}")
                with ui.scroll_area().classes("w-full h-96"):
                    ui.markdown(text)
                ui.button("关闭", on_click=dlg.close).props("flat")
            dlg.open()

        def sync_mode() -> None:
            cred_area.set_visibility(page_mode == "ops")

        # ================= 渲染后台任务状态 =================
        _rendered_sig: list = [None]

        def render_job() -> None:
            job = _jobs.get(_job_key(page_mode))
            running = job is not None and not job.finished
            spinner.set_visibility(running)
            run_btn.set_enabled(not running)
            if job is None:
                return
            status.text = job.error or job.stage
            status.style(f"color:{theme.DANGER if job.error else theme.NEUTRAL}")
            if job.signature == _rendered_sig[0]:
                return
            was_finished_before = isinstance(_rendered_sig[0], tuple) \
                and _rendered_sig[0][2]
            _rendered_sig[0] = job.signature
            results_box.clear()
            with results_box:
                for info in job.infos:
                    ui.label(info).classes("text-xs").style(f"color:{theme.WARN}")
                for e, r in job.engines.items():
                    title = f"{ENGINE_LABELS[e]} 报告"
                    if r.status == "running":
                        title += "（分析中…）"
                    elif r.status == "failed":
                        title += "（失败）"
                    with ui.expansion(title, value=True).classes("w-full"):
                        if r.status == "running":
                            ui.spinner(size="sm")
                        elif r.status == "failed":
                            ui.label(r.error).classes("text-sm") \
                                .style(f"color:{theme.DANGER}")
                        else:
                            ui.markdown(r.report)
            if job.finished and not was_finished_before:
                refresh_history()

        # ================= 启动 =================

        def _fmt_warn(msg: str) -> None:
            status.text = msg
            status.style(f"color:{theme.WARN}")

        async def start() -> None:
            prev = _jobs.get(_job_key(page_mode))
            if prev is not None and not prev.finished:
                _fmt_warn("这个模块上一个分析还在跑——等它完成再发起下一个")
                return
            engines = [e for e, c in engine_checks.items() if c.value]
            if not engines:
                _fmt_warn("至少选一个分析引擎")
                return
            query = (symbol_in.value or "").strip()
            if not query:
                _fmt_warn("先填标的（可以是中文名，如：海力士）")
                return

            # ---- 凭据（仅复盘模式）----
            adapter = None
            save_cred: tuple[Venue, str, dict] | None = None
            if page_mode == "ops":
                try:
                    if source.value == "saved":
                        if not saved_sel.value:
                            raise AnalysisError(
                                "没有可用的复盘凭据——切到「新填凭据」加一个")
                        if online:
                            # 解密在**浏览器里**完成，服务器这边只拿到解出来的明文
                            blob = await browser_creds.load(
                                saved_sel.value, unlock_in.value or "")
                            venue = Venue(blob.get("venue", Venue.BINANCE.value))
                            cred = {k: v for k, v in blob.items() if k != "venue"}
                        else:
                            venue, cred = session.open_account_credential(
                                saved_sel.value)
                    else:
                        venue = Venue(venue_sel.value)
                        cred = {f: (w.value or "").strip()
                                for f, w in cred_inputs.items()}
                        missing = [f for f in CREDENTIAL_FIELDS[venue]
                                   if not cred.get(f)]
                        if missing:
                            raise AnalysisError("缺少：" + "、".join(
                                FIELD_LABELS[f] for f in missing))
                        if save_chk.value:
                            alias = (alias_in.value or "").strip() \
                                or f"{venue.value} 复盘 {time.strftime('%m%d')}"
                            if online:
                                # 存进访客自己的浏览器：加密全程在他本机做，
                                # 服务器不留密文也不留这个解锁密码
                                pw = (new_pass_in.value or "").strip()
                                if len(pw) < 6:
                                    raise AnalysisError(
                                        "给这条凭据设一个至少 6 位的解锁密码"
                                        "（它是你浏览器里那份密文的钥匙），"
                                        "或取消勾选「保存供下次使用」")
                                await browser_creds.save(
                                    alias, {"venue": venue.value, **cred}, pw)
                                await refresh_saved_browser(select=alias)
                                ui.notify(f"已加密保存到本浏览器：{alias}",
                                          type="positive")
                                for w in cred_inputs.values():
                                    w.value = ""
                                new_pass_in.value = ""
                            else:
                                save_cred = (venue, alias, cred)
                    adapter = session.build_adapter(venue, cred)
                except browser_creds.BrowserCredError as exc:
                    _fmt_warn(str(exc))
                    return
                except VaultLocked:
                    _fmt_warn("凭据库未解锁——先在上面输入主密码")
                    return
                except AnalysisError as exc:
                    _fmt_warn(str(exc))
                    return

            # ---- 线上 API 参数 ----
            api_opts: ApiOptions | None = None
            if "api" in engines:
                typed = (api_key_in.value or "").strip()
                provider = provider_sel.value
                if online:
                    # AI key 同样存访客自己的浏览器，服务器不保管
                    alias = f"#ai:{provider}"
                    pw = (api_pass_in.value or "").strip()
                    key = typed
                    if not typed:
                        try:
                            key = (await browser_creds.load(alias, pw)).get("api_key", "")
                        except browser_creds.BrowserCredError as exc:
                            _fmt_warn(f"取用已存的 AI key 失败：{exc}")
                            return
                    elif api_remember.value:
                        if len(pw) < 6:
                            _fmt_warn("给这个 AI key 设一个至少 6 位的解锁密码，"
                                      "或取消勾选「存在我的浏览器里」")
                            return
                        try:
                            await browser_creds.save(alias, {"api_key": typed}, pw)
                            ui.notify("AI key 已加密存进本浏览器", type="positive")
                            api_key_in.value = ""
                        except browser_creds.BrowserCredError as exc:
                            ui.notify(str(exc), type="warning")
                else:
                    key = typed or (session.load_ai_key(provider) or "")
                    if typed and api_remember.value:
                        try:
                            session.save_ai_key(provider, typed)
                            ui.notify("API key 已加密保存", type="positive")
                            api_key_in.value = ""
                            apply_preset()
                        except VaultLocked:
                            ui.notify("key 未保存：凭据库未解锁（本次分析继续用它）",
                                      type="warning")
                api_opts = ApiOptions(
                    url=(api_url_in.value or "").strip(),
                    style=api_style_sel.value,
                    model=(api_model_in.value or "").strip(),
                    api_key=key,
                    max_tokens=config.analysis.api_max_tokens,
                )

            _rendered_sig[0] = None
            job = _Job(title=query)
            _jobs[_job_key(page_mode)] = job
            background_tasks.create(_run_job(
                job, session=session, config=config, mode=page_mode,
                query=query, days=int(days_sel.value),
                focus=focus_in.value or "", engines=engines,
                adapter=adapter, save_cred=save_cred, api_opts=api_opts,
                workdir=_reports_dir(config)))
            render_job()

        run_btn.on_click(start)

        # ---- 初始化 ----
        render_vault_state()
        if online and page_mode == "ops":
            # 读浏览器 localStorage 必须等客户端连上，用一次性定时器延后
            ui.timer(0.3, lambda: refresh_saved_browser(), once=True)
        else:
            refresh_saved()
        sync_source()
        source.on_value_change(lambda e: sync_source())
        sync_mode()
        sync_api_box()
        refresh_history()
        spinner.set_visibility(False)
        render_job()          # 回到页面时立刻显示正在跑/刚跑完的任务
        # 唯一的定时器：把后台任务状态搬上屏幕，不发任何网络请求
        ui.timer(1.0, render_job)
