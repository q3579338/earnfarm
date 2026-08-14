"""本地桥接：让公开版网页用上**你自己电脑上**的 AI CLI。

浏览器不能直接执行本机程序（任何网站都能跑命令的世界会很可怕），所以走
这个小桥：你在自己电脑上跑 `python run.py --bridge`，网页通过 localhost
调它，它再去调本机的 claude / grok / codex。

于是公开版也能做到：
- 行情与成交：浏览器直连交易所（密钥不出你的浏览器）
- AI 分析：你自己电脑上的 CLI（不花 API 的钱、数据不出你的机器）
- 服务器：只负责发网页和算汇总，AI 那一步完全不参与

安全上两道闸，缺一不可：
1. **只听 127.0.0.1**——外网碰不到你这个桥；
2. **令牌**——同一台机器上的任何网页都能访问 localhost，没有令牌的话，
   你随便打开一个恶意网站，它就能悄悄用你的 CLI 跑任意 prompt。
   令牌启动时打印，你手动粘到网页上，只有拿到它的页面才能调用。
"""

from __future__ import annotations

import argparse
import os
import secrets

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .analysis import AnalysisError, engine_available, run_engine
from .config import ANALYSIS_ENGINES, load as load_config

DEFAULT_PORT = 8790
TOKEN_ENV = "EARNFARM_BRIDGE_TOKEN"


class RunRequest(BaseModel):
    engine: str
    instruction: str
    payload: str


def build_app(token: str) -> FastAPI:
    cfg = load_config().analysis
    app = FastAPI(title="earnfarm bridge")
    # 允许任意网页来调，但每个请求都必须带令牌——同源限制在这里帮不上忙，
    # 令牌才是真正的闸
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
        allow_headers=["*"], allow_credentials=False,
    )

    @app.middleware("http")
    async def allow_private_network(request, call_next):
        """Chrome 的 Private Network Access：公网页面访问 127.0.0.1 时会发
        带 Access-Control-Request-Private-Network 的预检，本地服务必须显式
        回 Access-Control-Allow-Private-Network: true，否则一律 Failed to fetch。
        CORSMiddleware 不管这个头，只能自己补。"""
        response = await call_next(request)
        response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response

    def _check(tok: str | None) -> None:
        if not tok or not secrets.compare_digest(tok, token):
            raise HTTPException(status_code=401, detail="桥接令牌不对")

    @app.get("/ping")
    def ping(x_earnfarm_token: str | None = Header(default=None)) -> dict:
        _check(x_earnfarm_token)
        return {
            "ok": True,
            "engines": [e for e in ANALYSIS_ENGINES
                        if e != "api" and engine_available(e, cfg)[0]],
        }

    @app.post("/run")
    async def run(req: RunRequest,
                  x_earnfarm_token: str | None = Header(default=None)) -> dict:
        _check(x_earnfarm_token)
        if req.engine not in ANALYSIS_ENGINES or req.engine == "api":
            raise HTTPException(status_code=400, detail=f"不支持的引擎: {req.engine}")
        import anyio
        try:
            report = await anyio.to_thread.run_sync(
                lambda: run_engine(req.engine, req.instruction, req.payload, cfg,
                                   workdir=_workdir()))
        except AnalysisError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"report": report}

    return app


def _workdir():
    from pathlib import Path
    d = Path.home() / ".earnfarm" / "bridge"
    d.mkdir(parents=True, exist_ok=True)
    return d


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="earnfarm 本地 AI 桥接")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)

    token = os.environ.get(TOKEN_ENV, "").strip() or secrets.token_urlsafe(9)
    cfg = load_config().analysis
    ready = [e for e in ANALYSIS_ENGINES if e != "api" and engine_available(e, cfg)[0]]

    print("=" * 60)
    print("earnfarm 本地桥接已启动")
    print(f"  地址：http://127.0.0.1:{args.port}   （只听本机，外网访问不到）")
    print(f"  可用引擎：{'、'.join(ready) if ready else '（本机没装任何 AI CLI）'}")
    print(f"  桥接令牌：{token}")
    print("  把令牌粘进网页的「本地 CLI」那一栏即可。关掉本窗口即断开。")
    print("=" * 60)

    import uvicorn
    uvicorn.run(build_app(token), host="127.0.0.1", port=args.port,
                log_level="warning")
    return 0
