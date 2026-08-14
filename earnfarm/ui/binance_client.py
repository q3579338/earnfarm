"""浏览器端的币安客户端：请求从**访客自己的机器**发出，签名也在他本机算。

两个问题一次解决：

1. **地域封锁**。币安按 IP 拦截（受限地区直接 451）。服务器在哪不重要了——
   请求从访客的浏览器发出，走他自己的网络/代理。
2. **密钥托管**。HMAC-SHA256 签名用 Web Crypto 在浏览器里做，
   API secret **一次都不发给服务器**。服务器只收到拉回来的行情/成交数据。

可行性是实测过的（不是推测）：币安 REST 对浏览器跨域请求返回 CORS 头，
带 `X-MBX-APIKEY` 的签名请求预检也放行。

服务器仍然负责它擅长的部分：Decimal 汇总、prompt 构造、调 AI、存报告。
"""

from __future__ import annotations

import json
from typing import Any

from nicegui import ui

# NiceGUI 的 WebSocket 有消息体上限，所以**在浏览器里就把数据裁好**再回传：
# 原样回传 1500 根 K 线 + 全量成交会直接撑爆连接（页面上那条
# "Message too long" 就是这么来的）
_JS = """
window.efBin = {
  base: 'https://fapi.binance.com',

  _qs(p) { return Object.entries(p).map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&'); },

  async pub(path, params) {
    const r = await fetch(this.base + path + (params ? '?' + this._qs(params) : ''));
    if (!r.ok) throw new Error(path + ' ' + r.status + ' ' + (await r.text()).slice(0, 160));
    return r.json();
  },

  async signed(path, params, key, secret) {
    const enc = new TextEncoder();
    const ck = await crypto.subtle.importKey(
      'raw', enc.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    const p = Object.assign({}, params, { recvWindow: 5000, timestamp: Date.now() });
    const qs = this._qs(p);
    const sig = await crypto.subtle.sign('HMAC', ck, enc.encode(qs));
    const hex = [...new Uint8Array(sig)].map(b => b.toString(16).padStart(2, '0')).join('');
    const r = await fetch(this.base + path + '?' + qs + '&signature=' + hex,
                          { headers: { 'X-MBX-APIKEY': key } });
    const body = await r.text();
    if (!r.ok) throw new Error('币安拒绝（' + r.status + '）：' + body.slice(0, 160));
    return JSON.parse(body);
  },

  async symbols() {
    const info = await this.pub('/fapi/v1/exchangeInfo');
    return info.symbols.filter(s => s.status === 'TRADING').map(s => s.symbol);
  },

  // ---- 单币分析：纯公开行情 ----
  async market(symbols, days) {
    const interval = days <= 14 ? '1h' : '4h';
    const perDay = interval === '1h' ? 24 : 6;
    const limit = Math.min(1500, Math.max(days * perDay, 24));
    const out = [];
    for (const sym of symbols) {
      const [t, prem, kl, fr] = await Promise.all([
        this.pub('/fapi/v1/ticker/24hr', { symbol: sym }),
        this.pub('/fapi/v1/premiumIndex', { symbol: sym }),
        this.pub('/fapi/v1/klines', { symbol: sym, interval: interval, limit: limit }),
        this.pub('/fapi/v1/fundingRate', { symbol: sym, limit: Math.min(1000, days * 3 + 10) }),
      ]);
      out.push({
        symbol: sym,
        ticker: { lastPrice: t.lastPrice, priceChangePercent: t.priceChangePercent,
                  highPrice: t.highPrice, lowPrice: t.lowPrice, quoteVolume: t.quoteVolume },
        premium: { markPrice: prem.markPrice, indexPrice: prem.indexPrice,
                   lastFundingRate: prem.lastFundingRate, nextFundingTime: prem.nextFundingTime },
        // 只回传要用的 6 列，原始 12 列有一半是补零字段
        klines: kl.map(k => [k[0], k[1], k[2], k[3], k[4], k[7]]),
        funding: fr.map(f => [f.fundingTime, f.fundingRate]),
      });
    }
    return { interval: interval, per_symbol: out };
  },

  // ---- 操作复盘：签名请求，secret 不出浏览器 ----
  async ops(symbols, sinceMs, untilMs, key, secret) {
    const WINDOW = 7 * 86400000;          // userTrades 的 startTime/endTime 跨度上限
    const out = [];
    for (const sym of symbols) {
      const byId = {};
      for (let ws = sinceMs; ws <= untilMs; ws += WINDOW) {
        const we = Math.min(ws + WINDOW - 1, untilMs);
        let rows = await this.signed('/fapi/v1/userTrades',
          { symbol: sym, startTime: ws, endTime: we, limit: 1000 }, key, secret);
        rows.forEach(r => byId[r.id] = r);
        let pages = 1;
        while (rows.length === 1000 && pages < 32) {         // 撑满页 = 可能还有
          const lastId = rows[rows.length - 1].id;
          rows = await this.signed('/fapi/v1/userTrades',
            { symbol: sym, fromId: lastId + 1, limit: 1000 }, key, secret);
          pages += 1;
          let done = false;
          for (const r of rows) {
            if (r.time > we) { done = true; break; }         // 翻过窗尾就停
            byId[r.id] = r;
          }
          if (done) break;
        }
      }
      const fills = Object.values(byId).sort((a, b) => a.time - b.time).map(f => ({
        time: f.time, side: f.side, price: f.price, qty: f.qty, quoteQty: f.quoteQty,
        commission: f.commission, commissionAsset: f.commissionAsset,
        realizedPnl: f.realizedPnl, maker: f.maker, orderId: f.orderId,
      }));

      let positions = [], orders = [], funding = [], notes = [];
      try {
        const ps = await this.signed('/fapi/v3/positionRisk', { symbol: sym }, key, secret);
        positions = ps.filter(p => parseFloat(p.positionAmt) !== 0).map(p => ({
          positionAmt: p.positionAmt, entryPrice: p.entryPrice, markPrice: p.markPrice,
          liquidationPrice: p.liquidationPrice, leverage: p.leverage,
        }));
      } catch (e) { notes.push(sym + ' 持仓拉取失败：' + e.message); }
      try {
        const os = await this.signed('/fapi/v1/openOrders', { symbol: sym }, key, secret);
        orders = os.map(o => ({ status: o.status, executedQty: o.executedQty,
                                avgPrice: o.avgPrice, clientOrderId: o.clientOrderId }));
      } catch (e) { notes.push(sym + ' 挂单拉取失败：' + e.message); }
      try {
        const fr = await this.pub('/fapi/v1/fundingRate', { symbol: sym, limit: 200 });
        funding = fr.filter(f => f.fundingTime >= sinceMs).map(f => [f.fundingTime, f.fundingRate]);
      } catch (e) { notes.push(sym + ' 资金费历史拉取失败：' + e.message); }

      out.push({ symbol: sym, fills: fills, positions: positions,
                 open_orders: orders, funding: funding, notes: notes });
    }
    return { per_symbol: out };
  },
};
"""


_BRIDGE_JS = """
window.efBridge = {
  port: 8790, token: '',
  async ping(port, token) {
    this.port = port || 8790; this.token = token || '';
    const r = await fetch('http://127.0.0.1:' + this.port + '/ping',
                          { headers: { 'X-Earnfarm-Token': this.token } });
    if (!r.ok) throw new Error('桥接返回 ' + r.status + '（令牌不对？）');
    return r.json();
  },
  async run(engine, instruction, payload) {
    const r = await fetch('http://127.0.0.1:' + this.port + '/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json',
                 'X-Earnfarm-Token': this.token },
      body: JSON.stringify({ engine: engine, instruction: instruction,
                             payload: payload }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || ('桥接返回 ' + r.status));
    return j.report;
  },
};
"""


def install_bridge() -> None:
    """注入本地桥接客户端。页面借它调用**访客本机**的 AI CLI——
    数据和报告都不经过服务器。"""
    ui.add_head_html(f"<script>{_BRIDGE_JS}</script>")


async def bridge_ping(port: int, token: str, timeout: float = 8.0) -> list[str]:
    expr = f"window.efBridge.ping({int(port)}, {json.dumps(token)})"
    res = await _call(expr, timeout)
    return list((res or {}).get("engines", []))


async def bridge_run(engine: str, instruction: str, payload: str,
                     timeout: float = 900.0) -> str:
    expr = (f"window.efBridge.run({json.dumps(engine)}, "
            f"{json.dumps(instruction)}, {json.dumps(payload)})")
    return str(await _call(expr, timeout) or "")


class BinanceBrowserError(Exception):
    """浏览器端请求失败。message 直接给用户看（多半是密钥错或网络不通）。"""


def install(base_url: str = "") -> None:
    """注入客户端模块。base_url 非空则覆盖默认接口地址（自建反代时用）。"""
    js = _JS
    if base_url:
        js += f"\nwindow.efBin.base = {json.dumps(base_url.rstrip('/'))};"
    ui.add_head_html(f"<script>{js}</script>")


async def _call(expr: str, timeout: float) -> Any:
    try:
        return await ui.run_javascript(expr, timeout=timeout)
    except Exception as exc:
        # 浏览器里抛的 Error 会带原始文案（含币安的 msg），原样交给用户
        raise BinanceBrowserError(str(exc)) from exc


async def symbols(timeout: float = 30.0) -> set[str]:
    res = await _call("window.efBin.symbols()", timeout)
    return set(res or ())


async def market(symbols_: list[str], days: int, timeout: float = 90.0) -> dict:
    expr = f"window.efBin.market({json.dumps(symbols_)}, {int(days)})"
    return await _call(expr, timeout)


async def ops(symbols_: list[str], since_ms: int, until_ms: int,
              api_key: str, api_secret: str, timeout: float = 180.0) -> dict:
    expr = (f"window.efBin.ops({json.dumps(symbols_)}, {int(since_ms)}, "
            f"{int(until_ms)}, {json.dumps(api_key)}, {json.dumps(api_secret)})")
    return await _call(expr, timeout)
