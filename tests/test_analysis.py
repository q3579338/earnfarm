"""操作复盘模块测试。AI 子进程与 HTTP 全程 mock——测试不依赖本机装了什么。"""

from __future__ import annotations

import asyncio
import json
import subprocess
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from earnfarm import analysis
from earnfarm.analysis import (
    AnalysisError,
    OperationBundle,
    SymbolOps,
    build_instruction,
    build_resolve_instruction,
    bundle_payload,
    collect_operations,
    parse_resolved,
    resolve_local,
    run_api,
    run_claude,
    run_engine,
    summarize,
)
from earnfarm.config import AnalysisConfig
from earnfarm.models import TradeFill


def run(coro):
    return asyncio.run(coro)


def fill(ts_ms: int, side: str, price: str, qty: str, *, symbol: str = "OPUSDT",
         fee: str = "0.01", pnl: str = "0", maker: bool = False) -> TradeFill:
    p, q = Decimal(price), Decimal(qty)
    return TradeFill(
        market="binance:perp", symbol=symbol, ts_ms=ts_ms, side=side,
        price=p, qty=q, quote_qty=p * q, fee=Decimal(fee), fee_asset="USDT",
        realized_pnl=Decimal(pnl), maker=maker, order_id="1",
    )


def bundle(fills, symbol: str = "OPUSDT", **kwargs) -> OperationBundle:
    return OperationBundle(
        venue="binance", since_ms=1_700_000_000_000, until_ms=1_700_100_000_000,
        ops=(SymbolOps(symbol=symbol, fills=tuple(fills)),), **kwargs,
    )


# ---- summarize ----------------------------------------------------------

def test_summarize_counts_vwap_fees_and_pnl_split():
    fills = [
        fill(1_700_000_000_000, "buy", "2.00", "10", maker=True),          # 20 USDT
        fill(1_700_000_060_000, "buy", "4.00", "10"),                      # 40 USDT
        fill(1_700_000_120_000, "sell", "5.00", "20", pnl="3.5"),          # 100 USDT
        fill(1_700_000_180_000, "sell", "1.00", "2", pnl="-1.25"),         # 2 USDT
    ]
    s = summarize(fills)
    assert s["fill_count"] == 4
    assert s["buy_count"] == 2 and s["sell_count"] == 2
    assert s["maker_count"] == 1 and s["taker_count"] == 3
    # 买入 VWAP = (20+40)/20 = 3
    assert Decimal(s["buy_vwap"]) == Decimal("3")
    assert Decimal(s["quote_volume"]) == Decimal("162")
    assert s["fees_by_asset"] == {"USDT": "0.04"}
    assert Decimal(s["realized_pnl_total"]) == Decimal("2.25")
    assert s["closing_fill_wins"] == 1 and s["closing_fill_losses"] == 1
    assert Decimal(s["win_pnl_sum"]) == Decimal("3.5")
    assert Decimal(s["loss_pnl_sum"]) == Decimal("-1.25")


def test_summarize_empty_side_has_no_vwap():
    s = summarize([fill(1_700_000_000_000, "buy", "2", "1")])
    assert s["sell_vwap"] is None
    assert s["buy_vwap"] == "2"


# ---- 标的解析 -----------------------------------------------------------

AVAILABLE = {"BTCUSDT", "ETHUSDT", "OPUSDT", "SKHYUSDT", "SKHYNIXUSDT",
             "1000PEPEUSDT"}


def test_resolve_exact_and_base_symbols():
    resolved, unresolved = resolve_local(AVAILABLE, "btcusdt ETH")
    assert resolved == ["BTCUSDT", "ETHUSDT"]
    assert unresolved == []


def test_resolve_hynix_alias_expands_to_both_legs():
    """"海力士"命中的是配对表里的两条腿——那本来就是一笔配对交易。"""
    resolved, unresolved = resolve_local(AVAILABLE, "海力士")
    assert resolved == ["SKHYUSDT", "SKHYNIXUSDT"]
    assert unresolved == []


def test_resolve_unknown_goes_to_unresolved_not_fuzzy_guess():
    resolved, unresolved = resolve_local(AVAILABLE, "OPUL")
    assert resolved == []
    assert unresolved == ["OPUL"]        # 绝不 difflib 瞎猜成 OPUSDT


def test_resolve_mixed_query_dedupes():
    resolved, unresolved = resolve_local(AVAILABLE, "海力士 SKHYUSDT 特斯拉")
    assert resolved == ["SKHYUSDT", "SKHYNIXUSDT"]
    assert unresolved == ["特斯拉"]


def test_parse_resolved_filters_to_available():
    reply = '好的，我认为是：\n["SKHYUSDT", "FAKEUSDT", "skhynixusdt"]'
    assert parse_resolved(reply, AVAILABLE) == ["SKHYUSDT", "SKHYNIXUSDT"]
    assert parse_resolved("认不出", AVAILABLE) == []


def test_resolve_instruction_lists_tokens():
    text = build_resolve_instruction(["海力士", "特斯拉"])
    assert "海力士" in text and "特斯拉" in text and "JSON" in text


# ---- bundle_payload -----------------------------------------------------

def test_payload_is_json_with_per_symbol_sections():
    b = bundle([fill(1_700_000_000_000, "buy", "2", "10")])
    data = json.loads(bundle_payload(b))
    assert data["venue"] == "binance" and data["symbols"] == ["OPUSDT"]
    sec = data["per_symbol"][0]
    assert sec["summary"]["fill_count"] == 1
    assert sec["fills"][0]["side"] == "buy" and sec["fills"][0]["price"] == "2"


def test_payload_truncates_fills_but_keeps_full_summary():
    fills = [fill(1_700_000_000_000 + i * 1000, "buy", "2", "1") for i in range(200)]
    b = bundle(fills)
    data = json.loads(bundle_payload(b, max_fills=60))
    sec = data["per_symbol"][0]
    assert len(sec["fills"]) == 60
    assert sec["summary"]["fill_count"] == 200
    assert any("只嵌入最新 60 笔" in n for n in data["notes"])


# ---- build_instruction --------------------------------------------------

def test_instruction_mentions_symbol_and_focus():
    b = bundle([fill(1_700_000_000_000, "buy", "2", "1")])
    text = build_instruction(b, focus="手续费占比")
    assert "OPUSDT" in text and "binance" in text
    assert "用户额外关注：手续费占比" in text
    assert "用户额外关注" not in build_instruction(b)
    # 单标的不该出现配对复盘的话术
    assert "配对" not in text


def test_instruction_multi_leg_asks_for_pair_review():
    b = OperationBundle(
        venue="binance", since_ms=0, until_ms=1,
        ops=(SymbolOps("SKHYUSDT", (fill(0, "buy", "2", "1", symbol="SKHYUSDT"),)),
             SymbolOps("SKHYNIXUSDT", (fill(0, "sell", "20", "1",
                                            symbol="SKHYNIXUSDT"),))),
    )
    text = build_instruction(b)
    assert "SKHYUSDT" in text and "SKHYNIXUSDT" in text
    assert "配对" in text and "单腿裸奔" in text


# ---- collect_operations -------------------------------------------------

class FakeAdapter:
    """collect_operations 只用到这些方法，鸭子类型即可，不必背整个抽象基类。"""

    class _V:
        value = "binance"

    venue = _V()

    def __init__(self, fills=(), broken_context=False):
        self._fills = tuple(fills)
        self._broken = broken_context

    def timestamp_ms(self) -> int:
        return 1_700_100_000_000

    async def fetch_my_trades(self, symbol, since_ms, until_ms=None):
        return tuple(f for f in self._fills if f.symbol == symbol)

    async def fetch_positions(self, symbols=None):
        if self._broken:
            raise RuntimeError("positions down")
        return ()

    async def fetch_open_orders(self, symbol):
        if self._broken:
            raise RuntimeError("orders down")
        return ()

    async def fetch_funding_history(self, symbol, since_ms, limit=1000):
        if self._broken:
            raise RuntimeError("funding down")
        return ((1_700_000_000_000, Decimal("0.0001")),)


def test_collect_requires_trades_support():
    class NoTrades(FakeAdapter):
        async def fetch_my_trades(self, symbol, since_ms, until_ms=None):
            raise NotImplementedError("okx 尚未实现成交历史查询（fetch_my_trades）")

    with pytest.raises(AnalysisError, match="尚未实现"):
        run(collect_operations(NoTrades(), ["OPUSDT"], 0))


def test_collect_degrades_context_failures_to_notes():
    ad = FakeAdapter(fills=[fill(1_700_000_000_000, "buy", "2", "1")],
                     broken_context=True)
    b = run(collect_operations(ad, ["OPUSDT"], 0))
    assert b.fill_count == 1
    joined = "；".join(b.notes)
    assert "持仓拉取失败" in joined and "挂单拉取失败" in joined \
        and "资金费历史拉取失败" in joined


def test_collect_multi_symbol_groups_fills():
    ad = FakeAdapter(fills=[
        fill(1, "buy", "2", "1", symbol="SKHYUSDT"),
        fill(2, "sell", "20", "1", symbol="SKHYNIXUSDT"),
    ])
    b = run(collect_operations(ad, ["SKHYUSDT", "SKHYNIXUSDT"], 0))
    assert b.symbols == ("SKHYUSDT", "SKHYNIXUSDT")
    assert [len(o.fills) for o in b.ops] == [1, 1]
    assert any("realizedPnl" in n for n in b.notes)


# ---- 引擎 ----------------------------------------------------------------

CFG = AnalysisConfig()


def test_run_claude_pipes_payload_via_stdin(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(argv, 0, stdout="# 报告\n内容", stderr="")

    monkeypatch.setattr(analysis.shutil, "which", lambda cmd: r"C:\fake\claude.cmd")
    monkeypatch.setattr(analysis.subprocess, "run", fake_run)

    out = run_claude("分析指令", '{"data": 1}')
    assert out == "# 报告\n内容"
    assert "-p" in captured["argv"] and "分析指令" in captured["argv"]
    # 数据必须走 stdin，不进命令行参数（Windows 32K 上限）
    assert captured["input"] == '{"data": 1}'
    assert all('{"data": 1}' not in a for a in captured["argv"])


def test_agent_cli_engines_write_payload_file(monkeypatch, tmp_path):
    """grok/codex 是 agent 型 CLI，headless 不保证读 stdin：
    数据必须落文件、指令里给文件名、不经命令行也不经 stdin。"""
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["input"] = kwargs.get("input")
        captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(argv, 0, stdout="报告", stderr="")

    monkeypatch.setattr(analysis.shutil, "which", lambda cmd: cmd)
    monkeypatch.setattr(analysis.subprocess, "run", fake_run)

    out = run_engine("grok", "指令", '{"x":1}', CFG, workdir=tmp_path)
    assert out == "报告"
    assert captured["input"] is None
    files = list(tmp_path.glob("payload-*-grok.json"))
    assert len(files) == 1 and files[0].read_text(encoding="utf-8") == '{"x":1}'
    assert files[0].name in captured["argv"][-1]     # 指令里给了文件名
    assert captured["cwd"] == str(tmp_path)

    out = run_engine("codex", "指令", '{"x":2}', CFG, workdir=tmp_path)
    assert "exec" in captured["argv"]


def test_run_engine_unknown_name():
    with pytest.raises(AnalysisError, match="未知分析引擎"):
        run_engine("gemini", "x", "y", CFG, workdir=Path("."))


def test_run_claude_missing_cli(monkeypatch):
    monkeypatch.setattr(analysis.shutil, "which", lambda cmd: None)
    with pytest.raises(AnalysisError, match="claude"):
        run_claude("x", "y")


def test_run_claude_nonzero_exit(monkeypatch):
    monkeypatch.setattr(analysis.shutil, "which", lambda cmd: "claude")
    monkeypatch.setattr(
        analysis.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 3, stdout="", stderr="boom"))
    with pytest.raises(AnalysisError, match="退出码 3"):
        run_claude("x", "y")


def test_run_claude_timeout(monkeypatch):
    def fake_run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))

    monkeypatch.setattr(analysis.shutil, "which", lambda cmd: "claude")
    monkeypatch.setattr(analysis.subprocess, "run", fake_run)
    with pytest.raises(AnalysisError, match="超过 5s"):
        run_claude("x", "y", timeout_s=5)


def test_run_claude_empty_output(monkeypatch):
    monkeypatch.setattr(analysis.shutil, "which", lambda cmd: "claude")
    monkeypatch.setattr(
        analysis.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="  \n", stderr=""))
    with pytest.raises(AnalysisError, match="空输出"):
        run_claude("x", "y")


# ---- 线上 API 引擎 ------------------------------------------------------

def _api_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_run_api_anthropic_style(monkeypatch):
    monkeypatch.setenv("EARNFARM_AI_API_KEY", "test-key")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "# 报告"}]})

    out = run_api("指令", "{}", CFG, client=_api_client(handler))
    assert out == "# 报告"
    assert captured["headers"]["x-api-key"] == "test-key"
    assert captured["body"]["model"] == CFG.api_model
    assert "指令" in captured["body"]["messages"][0]["content"]


def test_run_api_openai_style(monkeypatch):
    from dataclasses import replace
    cfg = replace(CFG, api_style="openai",
                  api_url="https://api.x.ai/v1/chat/completions",
                  api_model="grok-4")
    monkeypatch.setenv("EARNFARM_AI_API_KEY", "xai-key")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "报告"}}]})

    out = run_api("指令", "{}", cfg, client=_api_client(handler))
    assert out == "报告"
    assert captured["auth"] == "Bearer xai-key"


def test_run_api_requires_key_env(monkeypatch):
    monkeypatch.delenv("EARNFARM_AI_API_KEY", raising=False)
    with pytest.raises(AnalysisError, match="EARNFARM_AI_API_KEY"):
        run_api("x", "{}", CFG)


def test_run_api_http_error(monkeypatch):
    monkeypatch.setenv("EARNFARM_AI_API_KEY", "k")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(AnalysisError, match="HTTP 401"):
        run_api("x", "{}", CFG, client=_api_client(handler))


# ---- 引擎可用性探测 ------------------------------------------------------

def test_engine_available_reports_missing_cli(monkeypatch):
    from earnfarm.analysis import engine_available

    monkeypatch.setattr(analysis.shutil, "which",
                        lambda cmd: "/bin/grok" if cmd == "grok" else None)
    ok, _ = engine_available("grok", CFG)
    assert ok
    ok, why = engine_available("claude", CFG)
    assert not ok and "PATH" in why


def test_engine_available_api_needs_key_env(monkeypatch):
    from earnfarm.analysis import engine_available

    monkeypatch.delenv("EARNFARM_AI_API_KEY", raising=False)
    ok, why = engine_available("api", CFG)
    assert not ok and "EARNFARM_AI_API_KEY" in why
    monkeypatch.setenv("EARNFARM_AI_API_KEY", "k")
    assert engine_available("api", CFG) == (True, "")


# ---- ApiOptions / 服务商预设 ---------------------------------------------

from earnfarm.analysis import (  # noqa: E402  页面级新增接口，集中在这一段测
    API_PRESET_LABELS,
    API_PRESETS,
    ApiOptions,
    MarketBundle,
    MarketSection,
    build_market_instruction,
    market_payload,
)


def test_run_api_opts_openai_style_overrides_cfg(monkeypatch):
    """opts 是页面现配的连接参数，url/model/key 必须整体压过 config.toml——
    有一样落回 cfg，用户就在不知情时把数据发去了另一家。"""
    monkeypatch.setenv("EARNFARM_AI_API_KEY", "env-key-should-not-win")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "报告"}}]})

    opts = ApiOptions("https://api.deepseek.com/chat/completions",
                      "openai", "deepseek-chat", api_key="page-key")
    out = run_api("指令", "{}", CFG, opts=opts, client=_api_client(handler))
    assert out == "报告"
    assert captured["url"] == opts.url
    assert captured["body"]["model"] == "deepseek-chat"
    # 页面上显式填的 key 优先于环境变量
    assert captured["auth"] == "Bearer page-key"


def test_run_api_opts_anthropic_style_uses_x_api_key(monkeypatch):
    monkeypatch.delenv("EARNFARM_AI_API_KEY", raising=False)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["key"] = request.headers.get("x-api-key")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "报告"}]})

    opts = ApiOptions("https://api.anthropic.com/v1/messages",
                      "anthropic", "claude-sonnet-5", api_key="page-key")
    out = run_api("指令", "{}", CFG, opts=opts, client=_api_client(handler))
    assert out == "报告"
    assert captured["url"] == opts.url
    assert captured["key"] == "page-key"
    assert captured["body"]["model"] == "claude-sonnet-5"


def test_run_api_opts_empty_key_falls_back_to_env(monkeypatch):
    """页面没填 key 时回退环境变量——预设本身不带 key，这是常态路径。"""
    monkeypatch.setenv("EARNFARM_AI_API_KEY", "env-key")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "报告"}}]})

    opts = ApiOptions("https://api.x.ai/v1/chat/completions", "openai", "grok-4")
    assert run_api("指令", "{}", CFG, opts=opts,
                   client=_api_client(handler)) == "报告"
    assert captured["auth"] == "Bearer env-key"


def test_api_presets_are_well_formed():
    """预设是替用户填的连接参数：风格必须是 run_api 认识的两种之一，
    URL 必须是 https——预设错一个字就是把 key 发去错误的地方。"""
    assert set(API_PRESETS) == {"deepseek", "xai", "anthropic"}
    for name, opts in API_PRESETS.items():
        assert opts.style in ("openai", "anthropic"), name
        assert opts.url.startswith("https://"), name
        assert opts.model, name
    # 每个预设都要有页面标签；custom 只在标签表里（它就是"用户自己填 URL"）
    assert set(API_PRESETS) | {"custom"} == set(API_PRESET_LABELS)


# ---- 单币分析（公开行情）-------------------------------------------------

def _kl(o: str = "100", h: str = "120", low: str = "90", c: str = "110",
        vol: str = "100") -> dict:
    return {"t": "2026-01-01 08:00:00", "o": o, "h": h, "l": low, "c": c,
            "quote_vol": vol}


def _market_section(symbol: str, klines) -> MarketSection:
    return MarketSection(
        symbol=symbol,
        ticker={"lastPrice": "110", "priceChangePercent": "1.0"},
        premium={"mark_price": "110.1", "index_price": "110.0"},
        klines=tuple(klines),
        funding=({"at": "2026-01-01 08:00:00", "rate": "0.0001"},),
    )


def test_market_payload_truncates_klines_but_keeps_full_summary():
    """K 线超过嵌入上限（336）只截最新，但 summary 仍按全量算，
    且截断必须显式写进 notes——和成交截断同一条纪律。"""
    many = [_kl() for _ in range(400)]        # first_open=100, last_close=110
    few = [_kl(o="200", h="230", low="190", c="220")]
    b = MarketBundle(
        symbols=("BTCUSDT", "ETHUSDT"), days=30, interval="4h",
        sections=(_market_section("BTCUSDT", many),
                  _market_section("ETHUSDT", few)),
        notes=("来源说明",),
    )
    data = json.loads(market_payload(b))
    big = data["per_symbol"][0]
    assert len(big["klines"]) == 336
    assert big["summary"]["bars"] == 400
    # (110-100)/100 = 10%，本地 Decimal 算好、保留两位
    assert big["summary"]["period_return_pct"] == "10.00"
    assert big["summary"]["period_high"] == "120"
    assert big["summary"]["period_low"] == "90"
    assert any("BTCUSDT" in n and "只嵌入最新 336 根" in n for n in data["notes"])
    # 没超限的标的不背截断的 note，bundle 自带的 note 也得保留
    assert sum("只嵌入最新 336 根" in n for n in data["notes"]) == 1
    assert len(data["per_symbol"][1]["klines"]) == 1
    assert "来源说明" in data["notes"]


def test_market_instruction_single_symbol():
    b = MarketBundle(symbols=("BTCUSDT",), days=7, interval="1h",
                     sections=(_market_section("BTCUSDT", [_kl()]),))
    text = build_market_instruction(b)
    assert "BTCUSDT" in text and "7 天" in text
    # 单标的没有对比一节
    assert "对比" not in text
    # 免凭据模式面向公众行情，免责声明永远在
    assert "不构成投资建议" in text


def test_market_instruction_multi_symbol_adds_comparison():
    b = MarketBundle(
        symbols=("BTCUSDT", "ETHUSDT"), days=7, interval="1h",
        sections=(_market_section("BTCUSDT", [_kl()]),
                  _market_section("ETHUSDT", [_kl()])),
    )
    text = build_market_instruction(b, focus="资金费")
    assert "对比" in text
    assert "不构成投资建议" in text
    assert "用户额外关注：资金费" in text
