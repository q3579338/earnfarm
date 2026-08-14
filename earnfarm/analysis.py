"""操作复盘：拉取标的的真实成交，交给本机 AI CLI 或线上 API 生成分析报告。

引擎（可多选并行，各出一份报告）：
- claude：本机 Claude Code CLI。数据经 **stdin** 管给 `claude -p`——不拼命令行
  （Windows 单条命令 32K 上限），也不需要给子进程开文件工具权限。
- grok / codex：本机 Grok CLI / ChatGPT Codex CLI。这两个是 agent 型 CLI，
  headless 下不保证读 stdin，所以数据落成工作目录里的 JSON 文件、指令里给文件名，
  由它们自己读——文件就是报告的原始底稿，留着不删。
- api：任意线上大模型接口。anthropic（Messages API）或 openai
  （chat/completions，xAI/DeepSeek/中转站全兼容）两种报文，key 走环境变量。

标的支持模糊输入（"海力士"、"BTC"、多个空格分开）：先本地精确/别名匹配，
认不出的拿币安全量符号表让 AI 挑，最后仍不确定就把候选摆给用户，绝不瞎猜——
解析错标的等于复盘了别人的仓位。

纪律：本地先用 Decimal 算好汇总，模型只负责解释；payload 里没有任何密钥材料。
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

import httpx

from .config import AnalysisConfig
from .exchanges.base import ExchangeAdapter, OrderResult
from .models import Position, TradeFill

ZERO = Decimal("0")

# 资金费率上下文只取最近这么多期——给模型看趋势用，不是给它做积分
_FUNDING_CONTEXT_N = 50

ENGINE_LABELS = {
    "claude": "Claude Code（本机 CLI）",
    "grok": "Grok（本机 CLI）",
    "codex": "ChatGPT Codex（本机 CLI）",
    "api": "线上 API",
}


@dataclass(frozen=True, slots=True)
class ApiOptions:
    """线上 API 的一组连接参数。页面上现配现用，不必写进 config.toml。"""

    url: str
    style: str            # anthropic / openai
    model: str
    api_key: str = ""     # 空 = 回退到已存 key / 环境变量
    max_tokens: int = 8192


# 常用服务商预设。custom 不在这里：它就是"用户自己填 URL"
API_PRESETS: dict[str, ApiOptions] = {
    "deepseek": ApiOptions("https://api.deepseek.com/chat/completions",
                           "openai", "deepseek-chat"),
    "xai": ApiOptions("https://api.x.ai/v1/chat/completions",
                      "openai", "grok-4"),
    "anthropic": ApiOptions("https://api.anthropic.com/v1/messages",
                            "anthropic", "claude-sonnet-5"),
}

API_PRESET_LABELS = {
    "deepseek": "DeepSeek",
    "xai": "xAI Grok",
    "anthropic": "Anthropic Claude",
    "custom": "自定义",
}


class AnalysisError(Exception):
    """分析流程的用户可读错误。message 直接上界面，不要塞堆栈。"""


def engine_available(engine: str, cfg: AnalysisConfig) -> tuple[bool, str]:
    """这个引擎现在能不能用。(ok, 不能用的原因)——界面按它标灰默认勾选，
    让用户在点「开始分析」**之前**就看到"没装"，而不是等报告区里蹦失败。"""
    if engine == "api":
        if os.environ.get(cfg.api_key_env, "").strip():
            return True, ""
        return False, f"未设环境变量 {cfg.api_key_env}"
    cmd = {"claude": cfg.claude_cmd, "grok": cfg.grok_cmd,
           "codex": cfg.codex_cmd}.get(engine)
    if cmd and shutil.which(cmd):
        return True, ""
    return False, "未安装或不在 PATH"


def _bj_iso(ts_ms: int) -> str:
    """北京时间的可读时刻。模型和用户都在东八区语境下对表，UTC 会让两边都心算。"""
    dt = datetime.fromtimestamp(ts_ms / 1000, timezone.utc) + timedelta(hours=8)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _bj_date(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, timezone.utc) + timedelta(hours=8)
    return dt.strftime("%Y-%m-%d")


# ---- 标的解析 ------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"[\s·．.\-_]+", "", s).lower()


def _alias_groups() -> tuple[tuple[frozenset[str], tuple[str, ...]], ...]:
    """已知的中文/俗名 → 符号组。目前来自双市场溢价的配对表：
    "海力士" 命中的是**两条腿**——那本来就是一笔配对交易，得一起复盘。"""
    from .premium import DUAL_PAIRS
    out = []
    for p in DUAL_PAIRS:
        names = {_norm(p.name), _norm(p.key),
                 _norm(p.adr_symbol), _norm(p.local_symbol)}
        out.append((frozenset(n for n in names if n),
                    (p.adr_symbol, p.local_symbol)))
    return tuple(out)


def split_query(query: str) -> list[str]:
    return [t for t in re.split(r"[,，、;；/\s]+", query.strip()) if t]


def resolve_local(available: set[str], query: str) -> tuple[list[str], list[str]]:
    """本地能确定的部分。返回 (已解析符号, 认不出的原词)。

    只做**确定性**匹配：原符号、base 币名、已知别名（含子串双向包含，
    让"海力士"命中"SK 海力士"）。模糊相似度不在这里做——difflib 把
    OP 认成 OPUL 这类事故安静得可怕，宁可留给 AI/用户去确认。
    """
    by_base = {}
    for s in available:
        for quote in ("USDT", "USDC"):
            if s.endswith(quote):
                by_base.setdefault(s[: -len(quote)], s)
                break
    aliases = _alias_groups()

    resolved: list[str] = []
    unresolved: list[str] = []
    for token in split_query(query):
        upper = token.upper()
        n = _norm(token)
        if upper in available:
            resolved.append(upper)
            continue
        if upper in by_base:
            resolved.append(by_base[upper])
            continue
        hit = None
        for names, symbols in aliases:
            if any(n == m or (len(n) >= 2 and (n in m or m in n)) for m in names):
                hit = [s for s in symbols if s in available] or list(symbols)
                break
        if hit:
            resolved.extend(hit)
        else:
            unresolved.append(token)
    # 去重保序
    seen: set[str] = set()
    resolved = [s for s in resolved if not (s in seen or seen.add(s))]
    return resolved, unresolved


def build_resolve_instruction(unresolved: Sequence[str]) -> str:
    """让 AI 从符号清单里认标的的指令。清单本体走数据通道（stdin/文件）。"""
    return (
        "数据里是一份交易所可用合约符号清单（每行一个）。"
        f"用户想分析这些标的（可能是中文名/公司名/俗称）：{'、'.join(unresolved)}。\n"
        "从清单里挑出对应的合约符号。同一家公司有多条腿（如 ADR 和本地股）就全选上。\n"
        '只输出一个 JSON 数组，如 ["SKHYUSDT","SKHYNIXUSDT"]；认不出的就不要输出，'
        "宁缺毋滥，绝不猜。除数组外不要输出任何文字。"
    )


def parse_resolved(reply: str, available: set[str]) -> list[str]:
    """从 AI 回复里抠出 JSON 数组并过滤到真实存在的符号。"""
    m = re.search(r"\[[^\[\]]*\]", reply, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except ValueError:
        return []
    seen: set[str] = set()
    out = []
    for x in arr:
        s = str(x).strip().upper()
        if s in available and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def suggest_symbols(available: set[str], token: str, n: int = 5) -> list[str]:
    """认不出时给用户的候选，只做展示不做决定。"""
    return difflib.get_close_matches(token.upper(), sorted(available), n=n, cutoff=0.5)


# ---- 数据收集 ------------------------------------------------------------

@dataclass(slots=True)
class SymbolOps:
    """一个标的的全部材料。"""

    symbol: str
    fills: tuple[TradeFill, ...]
    positions: tuple[Position, ...] = ()
    open_orders: tuple[OrderResult, ...] = ()
    funding: tuple[tuple[int, Decimal], ...] = ()


@dataclass(slots=True)
class OperationBundle:
    """一次分析所需的全部原始材料。成交是必需品，其余缺了记进 notes 继续。"""

    venue: str
    since_ms: int
    until_ms: int
    ops: tuple[SymbolOps, ...]
    notes: tuple[str, ...] = ()

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(o.symbol for o in self.ops)

    @property
    def fill_count(self) -> int:
        return sum(len(o.fills) for o in self.ops)


async def collect_operations(adapter: ExchangeAdapter, symbols: Sequence[str],
                             since_ms: int,
                             until_ms: int | None = None) -> OperationBundle:
    """拉齐分析材料。成交是硬需求，拉不到直接抛；持仓/挂单/资金费是上下文，
    拉不到降级为一条 note——报告少一节比整个功能不可用强。"""
    until_ms = adapter.timestamp_ms() if until_ms is None else until_ms
    notes: list[str] = []
    ops: list[SymbolOps] = []

    for symbol in symbols:
        try:
            fills = tuple(await adapter.fetch_my_trades(symbol, since_ms, until_ms))
        except NotImplementedError as exc:
            raise AnalysisError(str(exc)) from exc

        positions: tuple[Position, ...] = ()
        try:
            positions = tuple(p for p in await adapter.fetch_positions((symbol,))
                              if not p.is_flat)
        except Exception as exc:
            notes.append(f"{symbol} 持仓拉取失败，报告缺当前持仓上下文：{exc}")

        open_orders: tuple[OrderResult, ...] = ()
        try:
            open_orders = tuple(await adapter.fetch_open_orders(symbol))
        except Exception as exc:
            notes.append(f"{symbol} 挂单拉取失败，报告缺未成交挂单上下文：{exc}")

        funding: tuple[tuple[int, Decimal], ...] = ()
        try:
            funding = tuple(await adapter.fetch_funding_history(symbol, since_ms))
        except Exception as exc:
            notes.append(f"{symbol} 资金费历史拉取失败，报告缺资金费上下文：{exc}")

        ops.append(SymbolOps(symbol=symbol, fills=fills, positions=positions,
                             open_orders=open_orders, funding=funding))

    notes.append("realizedPnl 是平仓腿的价格盈亏，不含手续费、不含资金费——"
                 "评估净收益必须把 summary 里的手续费合计减掉。")
    return OperationBundle(
        venue=adapter.venue.value, since_ms=since_ms, until_ms=until_ms,
        ops=tuple(ops), notes=tuple(notes),
    )


# ---- 本地汇总 ------------------------------------------------------------

def summarize(fills: Sequence[TradeFill]) -> dict[str, Any]:
    """成交的本地汇总。所有算术都在这里用 Decimal 做完，值转成字符串防精度丢失。"""
    buys = [f for f in fills if f.side == "buy"]
    sells = [f for f in fills if f.side == "sell"]

    def _vwap(side: list[TradeFill]) -> Decimal | None:
        qty = sum((f.qty for f in side), ZERO)
        if qty == 0:
            return None
        return sum((f.quote_qty for f in side), ZERO) / qty

    fees: dict[str, Decimal] = {}
    for f in fills:
        if f.fee:
            fees[f.fee_asset or "?"] = fees.get(f.fee_asset or "?", ZERO) + f.fee

    pnl_fills = [f for f in fills if f.realized_pnl != 0]
    wins = [f for f in pnl_fills if f.realized_pnl > 0]
    losses = [f for f in pnl_fills if f.realized_pnl < 0]

    by_day: dict[str, dict[str, Any]] = {}
    for f in fills:
        d = by_day.setdefault(_bj_date(f.ts_ms), {
            "count": 0, "quote_volume": ZERO, "realized_pnl": ZERO})
        d["count"] += 1
        d["quote_volume"] += f.quote_qty
        d["realized_pnl"] += f.realized_pnl

    maker_count = sum(1 for f in fills if f.maker)
    return {
        "fill_count": len(fills),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "maker_count": maker_count,
        "taker_count": len(fills) - maker_count,
        "buy_qty": str(sum((f.qty for f in buys), ZERO)),
        "sell_qty": str(sum((f.qty for f in sells), ZERO)),
        "quote_volume": str(sum((f.quote_qty for f in fills), ZERO)),
        "buy_vwap": _fmt_opt(_vwap(buys)),
        "sell_vwap": _fmt_opt(_vwap(sells)),
        "fees_by_asset": {k: str(v) for k, v in fees.items()},
        "realized_pnl_total": str(sum((f.realized_pnl for f in fills), ZERO)),
        "closing_fill_wins": len(wins),
        "closing_fill_losses": len(losses),
        "win_pnl_sum": str(sum((f.realized_pnl for f in wins), ZERO)),
        "loss_pnl_sum": str(sum((f.realized_pnl for f in losses), ZERO)),
        "first_fill_at": _bj_iso(fills[0].ts_ms) if fills else None,
        "last_fill_at": _bj_iso(fills[-1].ts_ms) if fills else None,
        "by_day": {k: {"count": v["count"],
                       "quote_volume": str(v["quote_volume"]),
                       "realized_pnl": str(v["realized_pnl"])}
                   for k, v in sorted(by_day.items())},
    }


def _fmt_opt(v: Decimal | None) -> str | None:
    return None if v is None else str(v)


def bundle_payload(bundle: OperationBundle, *, max_fills: int = 2000) -> str:
    """归一成给模型的 JSON。成交超长按标的均分预算、只截最新——截断必须
    显式写进 notes，绝不让模型在不知情时把局部当全量。"""
    notes = list(bundle.notes)
    per_symbol_cap = max(50, max_fills // max(1, len(bundle.ops)))

    sections = []
    for op in bundle.ops:
        fills = op.fills
        if len(fills) > per_symbol_cap:
            notes.append(f"{op.symbol} 成交共 {len(fills)} 笔，只嵌入最新"
                         f" {per_symbol_cap} 笔；summary 的统计口径仍是全量。")
            fills = fills[-per_symbol_cap:]
        sections.append({
            "symbol": op.symbol,
            "summary": summarize(op.fills),
            "positions": [{
                "qty": str(p.qty),
                "entry_price": str(p.entry_price), "mark_price": str(p.mark_price),
                "liquidation_price": _fmt_opt(p.liquidation_price),
                "leverage": str(p.leverage),
            } for p in op.positions],
            "open_orders": [{
                "status": o.status, "filled_qty": str(o.filled_qty),
                "avg_price": str(o.avg_price), "client_order_id": o.client_order_id,
            } for o in op.open_orders],
            "funding_rates_recent": [
                {"at": _bj_iso(ts), "rate": str(rate)}
                for ts, rate in op.funding[-_FUNDING_CONTEXT_N:]
            ],
            "fills": [{
                "at": _bj_iso(f.ts_ms), "side": f.side, "price": str(f.price),
                "qty": str(f.qty), "quote": str(f.quote_qty),
                "fee": str(f.fee), "fee_asset": f.fee_asset,
                "realized_pnl": str(f.realized_pnl),
                "maker": f.maker, "order_id": f.order_id,
            } for f in fills],
        })

    payload = {
        "venue": bundle.venue,
        "symbols": list(bundle.symbols),
        "timezone": "UTC+8（北京时间）",
        "window": {"since": _bj_iso(bundle.since_ms), "until": _bj_iso(bundle.until_ms)},
        "per_symbol": sections,
        "notes": notes,
    }
    return json.dumps(payload, ensure_ascii=False, indent=1)


def build_instruction(bundle: OperationBundle, focus: str = "") -> str:
    """复盘指令。数据本体走数据通道（stdin/文件/报文），这里只描述任务。"""
    syms = "、".join(bundle.symbols)
    lines = [
        f"数据是一份 JSON：{bundle.venue} 交易所 {syms} 在"
        f" {_bj_iso(bundle.since_ms)} ~ {_bj_iso(bundle.until_ms)}（北京时间）"
        "的真实成交记录，含本地算好的汇总（summary）、当前持仓、挂单和资金费率上下文。",
        "你是交易复盘助手。基于且仅基于这份数据写一份 Markdown 复盘报告，包含：",
        "1. 操作时间线：建仓/加仓/减仓/平仓的节奏，标注关键时刻与价格；",
        "2. 成本分析：手续费合计、maker/taker 占比，taker 偏多时指出来；",
        "3. 盈亏归因：realizedPnl 的分布，盈利腿和亏损腿各自的特征；",
        "4. 行为模式：追价、扛单、频繁翻向等能从时间序列里读出的习惯，好坏都说；",
        "5. 改进建议：3 条以内，每条都要能落到具体操作上。",
    ]
    if len(bundle.ops) > 1:
        lines.append(
            "多个标的可能是一笔配对/对冲操作（如同一公司的 ADR 腿和本地股腿）：",
        )
        lines.append(
            "把两腿放在一起看——对冲比例是否配平、两腿建仓的时间差（单腿裸奔窗口）、"
            "价差方向与两腿盈亏的关系，逐腿各写一节再写合并结论。")
    lines.append(
        "纪律：数据里没有的不要编造；不预测行情；金额保留原始计价资产；"
        "notes 里的口径说明必须遵守。报告用中文。")
    if focus.strip():
        lines.append(f"用户额外关注：{focus.strip()}")
    return "\n".join(lines)


# ---- 引擎 ----------------------------------------------------------------

def _which(cmd: str, engine: str) -> str:
    exe = shutil.which(cmd)
    if exe is None:
        raise AnalysisError(
            f"找不到 {cmd!r}（{ENGINE_LABELS.get(engine, engine)}）。"
            "确认已安装且命令在 PATH 里，或在配置 [analysis] 里写完整路径。")
    return exe


def _run_proc(argv: list[str], *, engine: str, stdin_text: str | None,
              timeout_s: int, cwd: Path | None = None) -> str:
    try:
        proc = subprocess.run(
            argv, input=stdin_text, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_s,
            cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired as exc:
        raise AnalysisError(f"{engine} 超过 {timeout_s}s 未返回，已中止。"
                            "可在配置 [analysis] timeout_s 里放宽。") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-1000:]
        raise AnalysisError(f"{engine} 退出码 {proc.returncode}：{tail or '无输出'}")
    out = (proc.stdout or "").strip()
    if not out:
        raise AnalysisError(f"{engine} 返回了空输出")
    return out


def run_claude(instruction: str, payload: str, *, claude_cmd: str = "claude",
               timeout_s: int = 600) -> str:
    """Claude Code headless：指令走 -p，数据走 stdin（cat data | claude -p "..."
    的官方用法）。同步阻塞——界面层负责丢进线程池。"""
    exe = _which(claude_cmd, "claude")
    return _run_proc([exe, "-p", instruction, "--output-format", "text"],
                     engine="claude", stdin_text=payload, timeout_s=timeout_s)


def _run_agent_cli(engine: str, cmd: str, argv_tail: list[str], instruction: str,
                   payload: str, *, timeout_s: int, workdir: Path) -> str:
    """grok / codex 这类 agent 型 CLI：headless 下不保证读 stdin，
    数据落成工作目录里的 JSON 文件、指令里给文件名，由它们自己读。
    文件就是这份报告的原始底稿，留着不删。"""
    exe = _which(cmd, engine)
    workdir.mkdir(parents=True, exist_ok=True)
    fname = f"payload-{time.strftime('%Y%m%d-%H%M%S')}-{engine}.json"
    (workdir / fname).write_text(payload, encoding="utf-8")
    full = (f"{instruction}\n\n数据在当前工作目录的 {fname} 文件里（JSON），"
            "先完整读取它再分析；除报告本身外不要执行任何其他操作。")
    return _run_proc([exe, *argv_tail, full], engine=engine, stdin_text=None,
                     timeout_s=timeout_s, cwd=workdir)


def run_api(instruction: str, payload: str, cfg: AnalysisConfig,
            *, opts: ApiOptions | None = None,
            client: httpx.Client | None = None) -> str:
    """线上大模型接口。opts 来自页面上的服务商预设/自定义（优先），
    不传则回退 config.toml 的 [analysis]。key 的优先级：opts 里显式给的
    > 环境变量。报文里绝不带交易所凭据。"""
    url = opts.url if opts else cfg.api_url
    style = opts.style if opts else cfg.api_style
    model = opts.model if opts else cfg.api_model
    max_tokens = opts.max_tokens if opts else cfg.api_max_tokens
    key = (opts.api_key.strip() if opts and opts.api_key else "") \
        or os.environ.get(cfg.api_key_env, "").strip()
    if not key:
        raise AnalysisError(
            "没有可用的 API key：在页面上填一个（可加密保存），"
            f"或设置环境变量 {cfg.api_key_env}。")
    if style not in ("anthropic", "openai"):
        raise AnalysisError(f"未知 API 报文风格: {style!r}")
    prompt = f"{instruction}\n\n以下是数据 JSON：\n{payload}"

    if style == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        body: dict[str, Any] = {
            "model": model, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
    else:
        headers = {"Authorization": f"Bearer {key}"}
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }

    own = client is None
    c = client or httpx.Client(timeout=httpx.Timeout(cfg.timeout_s))
    try:
        resp = c.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise AnalysisError(f"API 请求失败：{exc}") from exc
    finally:
        if own:
            c.close()
    if resp.status_code >= 400:
        raise AnalysisError(f"API 返回 HTTP {resp.status_code}：{resp.text[:300]}")
    try:
        data = resp.json()
        if style == "anthropic":
            text = "".join(b.get("text", "") for b in data["content"]
                           if b.get("type") == "text")
        else:
            text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AnalysisError(f"API 响应解析失败：{resp.text[:300]}") from exc
    text = (text or "").strip()
    if not text:
        raise AnalysisError("API 返回了空输出")
    return text


def run_engine(engine: str, instruction: str, payload: str, cfg: AnalysisConfig,
               *, workdir: Path, api_opts: ApiOptions | None = None) -> str:
    """按引擎名分发。同步阻塞，调用方自己决定放哪个线程/并发。"""
    if engine == "claude":
        return run_claude(instruction, payload, claude_cmd=cfg.claude_cmd,
                          timeout_s=cfg.timeout_s)
    if engine == "grok":
        return _run_agent_cli("grok", cfg.grok_cmd, ["-p"], instruction, payload,
                              timeout_s=cfg.timeout_s, workdir=workdir)
    if engine == "codex":
        return _run_agent_cli("codex", cfg.codex_cmd,
                              ["exec", "--skip-git-repo-check"],
                              instruction, payload,
                              timeout_s=cfg.timeout_s, workdir=workdir)
    if engine == "api":
        return run_api(instruction, payload, cfg, opts=api_opts)
    raise AnalysisError(f"未知分析引擎: {engine!r}")


# ---- 单币分析（公开行情，无需任何交易所凭据）-----------------------------

# 只用 fapi 公开端点，和溢价页同一来源。不 import 币安适配器：
# 这里一个签名请求都没有，犯不着拖进整个适配器
_FAPI = os.environ.get("EARNFARM_FAPI_BASE", "https://fapi.binance.com").rstrip("/")

# 嵌入 prompt 的 K 线上限。1h 线 14 天 = 336 根，再多模型也读不动
_KLINES_EMBED_MAX = 336


@dataclass(slots=True)
class MarketSection:
    """一个标的的公开行情材料。"""

    symbol: str
    ticker: dict[str, Any]
    premium: dict[str, Any]
    klines: tuple[dict[str, Any], ...]     # {"t","o","h","l","c","quote_vol"}
    funding: tuple[dict[str, Any], ...]    # {"at","rate"}


@dataclass(slots=True)
class MarketBundle:
    symbols: tuple[str, ...]
    days: int
    interval: str
    sections: tuple[MarketSection, ...]
    notes: tuple[str, ...] = ()


async def fetch_public_symbols() -> set[str]:
    """币安永续在售符号全集（公开 exchangeInfo）。模糊标的解析在免凭据
    模式下也要能用，所以不能依赖适配器。"""
    async with httpx.AsyncClient(timeout=httpx.Timeout(20)) as c:
        resp = await c.get(f"{_FAPI}/fapi/v1/exchangeInfo")
        resp.raise_for_status()
        return {s["symbol"] for s in resp.json().get("symbols", [])
                if s.get("status") == "TRADING"}


async def collect_market(symbols: Sequence[str], days: int) -> MarketBundle:
    """拉一组标的的公开行情。全部是无签名请求，不经过任何凭据。

    回看期长时换粗周期：模型要的是结构不是逐 tick——1500 根 4h 线的信息量
    对"90 天走势"远高于同样根数的 1h 线。
    """
    interval = "1h" if days <= 14 else "4h"
    per_day = 24 if interval == "1h" else 6
    limit = min(1500, max(days * per_day, 24))
    notes: list[str] = []
    sections: list[MarketSection] = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(20)) as c:
        async def get(path: str, **params: Any) -> Any:
            resp = await c.get(f"{_FAPI}{path}", params=params)
            resp.raise_for_status()
            return resp.json()

        for sym in symbols:
            try:
                ticker = await get("/fapi/v1/ticker/24hr", symbol=sym)
                premium = await get("/fapi/v1/premiumIndex", symbol=sym)
                kl = await get("/fapi/v1/klines", symbol=sym,
                               interval=interval, limit=limit)
                fr = await get("/fapi/v1/fundingRate", symbol=sym,
                               limit=min(1000, days * 3 + 10))
            except httpx.HTTPError as exc:
                raise AnalysisError(f"{sym} 公开行情拉取失败：{exc}") from exc

            klines = tuple({
                "t": _bj_iso(int(k[0])), "o": str(k[1]), "h": str(k[2]),
                "l": str(k[3]), "c": str(k[4]), "quote_vol": str(k[7]),
            } for k in kl)
            funding = tuple({
                "at": _bj_iso(int(f["fundingTime"])),
                "rate": str(f["fundingRate"]),
            } for f in fr)
            sections.append(MarketSection(
                symbol=sym,
                ticker={k: str(ticker.get(k, "")) for k in
                        ("lastPrice", "priceChangePercent", "highPrice",
                         "lowPrice", "quoteVolume")},
                premium={
                    "mark_price": str(premium.get("markPrice", "")),
                    "index_price": str(premium.get("indexPrice", "")),
                    "last_funding_rate": str(premium.get("lastFundingRate", "")),
                    "next_funding_at": _bj_iso(int(premium["nextFundingTime"]))
                    if premium.get("nextFundingTime") else "",
                },
                klines=klines, funding=funding,
            ))

    notes.append("全部数据来自币安 USDⓈ-M 永续的公开端点；价格是永续口径"
                 "（自带基差），不是现货报价。")
    return MarketBundle(symbols=tuple(symbols), days=days, interval=interval,
                        sections=tuple(sections), notes=tuple(notes))


# ---- 由浏览器拉回的原始数据装配 bundle ----------------------------------
# 线上模式下请求是从**访客自己的浏览器**发的（绕开服务器的地域封锁，
# 且 API secret 不出他的浏览器），服务器只拿到这些已经拉好的原始 JSON。
# 汇总统计、prompt 构造、AI 调用仍在服务端——那才是它该干的活。

def bundle_from_browser(raw: dict[str, Any], since_ms: int, until_ms: int,
                        venue: str = "binance") -> OperationBundle:
    ops_list: list[SymbolOps] = []
    notes: list[str] = []
    for sec in raw.get("per_symbol", []):
        sym = str(sec.get("symbol", ""))
        notes.extend(str(n) for n in sec.get("notes", []))
        fills = tuple(
            TradeFill(
                market="binance:perp", symbol=sym, ts_ms=int(f["time"]),
                side=str(f.get("side", "")).lower(),
                price=Decimal(str(f.get("price", "0"))),
                qty=Decimal(str(f.get("qty", "0"))),
                quote_qty=Decimal(str(f.get("quoteQty", "0"))),
                fee=Decimal(str(f.get("commission", "0"))),
                fee_asset=str(f.get("commissionAsset", "")),
                realized_pnl=Decimal(str(f.get("realizedPnl", "0"))),
                maker=bool(f.get("maker", False)),
                order_id=str(f.get("orderId", "")),
            ) for f in sec.get("fills", []))
        positions = tuple(
            Position(
                market="binance:perp", symbol=sym,
                qty=Decimal(str(p.get("positionAmt", "0"))),
                entry_price=Decimal(str(p.get("entryPrice", "0"))),
                mark_price=Decimal(str(p.get("markPrice", "0"))),
                liquidation_price=(Decimal(str(p["liquidationPrice"]))
                                   if Decimal(str(p.get("liquidationPrice", "0"))) > 0
                                   else None),
                leverage=Decimal(str(p.get("leverage", "1"))),
            ) for p in sec.get("positions", []))
        orders = tuple(
            OrderResult(
                client_order_id=str(o.get("clientOrderId", "")),
                exchange_order_id="", status=str(o.get("status", "")).lower(),
                filled_qty=Decimal(str(o.get("executedQty", "0"))),
                avg_price=Decimal(str(o.get("avgPrice", "0"))),
            ) for o in sec.get("open_orders", []))
        funding = tuple((int(t), Decimal(str(r))) for t, r in sec.get("funding", []))
        ops_list.append(SymbolOps(symbol=sym, fills=fills, positions=positions,
                                  open_orders=orders, funding=funding))

    notes.append("数据由你的浏览器直接向币安拉取，API 密钥未经过服务器。")
    notes.append("realizedPnl 是平仓腿的价格盈亏，不含手续费、不含资金费——"
                 "评估净收益必须把 summary 里的手续费合计减掉。")
    return OperationBundle(venue=venue, since_ms=since_ms, until_ms=until_ms,
                           ops=tuple(ops_list), notes=tuple(notes))


def market_bundle_from_browser(raw: dict[str, Any], symbols_: Sequence[str],
                               days: int) -> MarketBundle:
    sections: list[MarketSection] = []
    for sec in raw.get("per_symbol", []):
        klines = tuple({
            "t": _bj_iso(int(k[0])), "o": str(k[1]), "h": str(k[2]),
            "l": str(k[3]), "c": str(k[4]), "quote_vol": str(k[5]),
        } for k in sec.get("klines", []))
        prem = sec.get("premium", {}) or {}
        sections.append(MarketSection(
            symbol=str(sec.get("symbol", "")),
            ticker={k: str(v) for k, v in (sec.get("ticker", {}) or {}).items()},
            premium={
                "mark_price": str(prem.get("markPrice", "")),
                "index_price": str(prem.get("indexPrice", "")),
                "last_funding_rate": str(prem.get("lastFundingRate", "")),
                "next_funding_at": (_bj_iso(int(prem["nextFundingTime"]))
                                    if prem.get("nextFundingTime") else ""),
            },
            klines=klines,
            funding=tuple({"at": _bj_iso(int(t)), "rate": str(r)}
                          for t, r in sec.get("funding", [])),
        ))
    return MarketBundle(
        symbols=tuple(symbols_), days=days,
        interval=str(raw.get("interval", "1h")), sections=tuple(sections),
        notes=("全部数据来自币安 USDⓈ-M 永续的公开端点（由你的浏览器直接拉取）；"
               "价格是永续口径（自带基差），不是现货报价。",),
    )


def _kline_summary(klines: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """本地算好区间统计。模型擅长解释不擅长算术——同一条纪律。"""
    if not klines:
        return {}
    first = Decimal(klines[0]["o"])
    last = Decimal(klines[-1]["c"])
    high = max(Decimal(k["h"]) for k in klines)
    low = min(Decimal(k["l"]) for k in klines)
    ret = (last - first) / first * 100 if first else ZERO
    return {
        "first_open": str(first), "last_close": str(last),
        "period_high": str(high), "period_low": str(low),
        "period_return_pct": str(ret.quantize(Decimal("0.01"))),
        "quote_volume_sum": str(sum((Decimal(k["quote_vol"]) for k in klines), ZERO)),
        "bars": len(klines),
    }


def market_payload(bundle: MarketBundle) -> str:
    notes = list(bundle.notes)
    per_symbol = []
    for s in bundle.sections:
        klines = s.klines
        if len(klines) > _KLINES_EMBED_MAX:
            notes.append(f"{s.symbol} K 线共 {len(klines)} 根，只嵌入最新"
                         f" {_KLINES_EMBED_MAX} 根；summary 的统计口径仍是全量。")
            klines = klines[-_KLINES_EMBED_MAX:]
        per_symbol.append({
            "symbol": s.symbol,
            "summary": _kline_summary(s.klines),
            "ticker_24h": s.ticker,
            "premium_index": s.premium,
            "funding_recent": list(s.funding[-_FUNDING_CONTEXT_N:]),
            "klines": list(klines),
        })
    payload = {
        "venue": "binance（USDⓈ-M 永续，公开行情）",
        "symbols": list(bundle.symbols),
        "timezone": "UTC+8（北京时间）",
        "window_days": bundle.days,
        "kline_interval": bundle.interval,
        "per_symbol": per_symbol,
        "notes": notes,
    }
    return json.dumps(payload, ensure_ascii=False, indent=1)


def build_market_instruction(bundle: MarketBundle, focus: str = "") -> str:
    syms = "、".join(bundle.symbols)
    lines = [
        f"数据是一份 JSON：币安永续 {syms} 最近 {bundle.days} 天的公开行情"
        f"（{bundle.interval} K 线、24h 快照、标记/指数价、资金费率历史，北京时间）。",
        "你是行情分析助手。基于且仅基于这份数据写一份 Markdown 单币分析，包含：",
        "1. 走势结构：区间、趋势段、关键价位，全部落到数据里的具体价格与时间；",
        "2. 量能：成交额的分布与变化，放量/缩量对应的价格行为；",
        "3. 资金费与基差：多空情绪的读数，标记价与指数价的偏离；",
        "4. 波动特征：大波动时段、单根大 K 线，及其前后行情；",
        "5. 值得跟踪的信号：3 条以内，每条说清触发条件。",
    ]
    if len(bundle.sections) > 1:
        lines.append("多个标的逐个写完后加一节对比；若它们是同一公司的两条腿，"
                     "重点写两腿价差的变化。")
    lines.append(
        "纪律：只基于数据，数据没有的不要编造；对每个判断说明不确定性；"
        "不给出买入/卖出指令式结论，结尾注明本报告不构成投资建议。报告用中文。")
    if focus.strip():
        lines.append(f"用户额外关注：{focus.strip()}")
    return "\n".join(lines)
