"""机会榜的公开行情：能让浏览器拉的就让**访客的浏览器**拉，其余仍走服务端。

为什么是混合取数而不是"全部走浏览器"
------------------------------------

服务器在美国，币安按 IP 返回 451、Bybit 返回 403 —— 这两家服务端一条数据都
拿不到。反过来，另外五家不给浏览器跨域（CORS 预检直接拒），浏览器也拉不到。
两边恰好互补，合起来九家全覆盖，所以最终形态是**混合**：

    2026-08-14 实测矩阵（浏览器 = 从 https://earn.satloot.com 页面 fetch，
    服务器 = 从生产机 curl）

    | 交易所      | 服务器直连      | 浏览器跨域 | 机会榜取数走哪边 |
    |-------------|-----------------|------------|------------------|
    | binance     | ❌ 451 地域封锁 | ✅ 200     | 浏览器           |
    | bybit       | ❌ 403          | ✅ 200     | 浏览器           |
    | bitget      | ✅              | ✅         | 浏览器（服务端兜底）|
    | hyperliquid | ✅              | ✅ POST    | 浏览器（服务端兜底）|
    | okx         | ✅              | ❌ CORS 拒 | 服务端           |
    | gate        | ✅              | ❌ CORS 拒 | 服务端           |
    | htx         | ✅              | ❌ CORS 拒 | 服务端           |
    | kucoin      | ✅              | ❌ CORS 拒 | 服务端           |
    | backpack    | ✅              | ❌ CORS 拒 | 服务端           |

**CORS 策略是交易所随时能改的，这张表要定期复核**（改坏的方向是静默的：
某家哪天关掉 CORS，只会表现成机会榜少一家 + 状态栏多一条失败原因）。
界面上如实写明哪几家是浏览器取的、哪几家仍走服务端——别假装全绕过了。

失败隔离
--------

每个端点独立 try/catch，每家独立一次 WebSocket 往返：一家被拒不影响其他家，
失败原因原样带回来显示给用户。浏览器没拿到的家会**自动回落到服务端直连**
（缓存没喂上 = 适配器照旧发 HTTP），对 bitget/hyperliquid 这种两边都通的家
等于白拿一层冗余；对 binance/bybit 则会在榜上如实少掉那一家。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from nicegui import ui

from ..exchanges.base import feed_public_cache, public_cache_key
from ..models import Venue

# 浏览器能跨域拉的家（2026-08-14 实测，见模块 docstring 的矩阵）。
# 不在这张表里的家**绝不会**被浏览器取数——不是"试试看失败就算了"，
# 是压根不发：明知会被 CORS 拒还发，只会让每轮刷新白等九次超时。
BROWSER_FETCHABLE = frozenset({"binance", "bybit", "bitget", "hyperliquid"})


@dataclass(frozen=True, slots=True)
class _Spec:
    """一发浏览器请求。key 是喂进缓存用的键，**必须与适配器算出来的一致**——
    所以两边都用 base.public_cache_key 算，不手写字符串。"""

    venue: str
    label: str                      # 状态栏/日志用的人话名字
    base: str
    method: str
    path: str
    params: Mapping[str, Any] | None = None
    body: Mapping[str, Any] | None = None
    trim: str = ""                  # 浏览器侧裁字段的函数名，见 _JS._trim

    @property
    def cache_key(self) -> str:
        key = public_cache_key(self.method, self.path, self.params, self.body)
        if key is None:      # 只可能是写错了 spec（带了按品种查的参数）
            raise ValueError(f"{self.venue} {self.path} 的参数不该走缓存")
        return key

    @property
    def url(self) -> str:
        if not self.params:
            return self.base + self.path
        qs = "&".join(f"{k}={self.params[k]}" for k in sorted(self.params, key=str))
        return f"{self.base}{self.path}?{qs}"


# ---- 机会榜要的公开端点（逐家读 fetch_carry_rates / fetch_instruments 得来）--
#
# 只列**不带品种参数的全市场请求**——带 symbol 的那些既不该缓存也没法预取。
# params 必须和适配器实际传的一字不差（Bybit 的 category/limit、Bitget 的
# productType、HL 的 dex），差一个字缓存键就对不上，喂了也永远不命中。
#
#  venue       | method+path                                 | 谁在用            | 服务端 base
#  ------------|---------------------------------------------|-------------------|---------------------------
#  binance     | GET /fapi/v1/exchangeInfo                   | fetch_instruments | https://fapi.binance.com
#  binance     | GET /fapi/v1/fundingInfo                    | _load_funding_info| 同上
#  binance     | GET /fapi/v1/premiumIndex                   | fetch_carry_rates | 同上
#  bybit       | GET /v5/market/instruments-info?category=linear&limit=1000 | fetch_instruments | https://api.bybit.com
#  bybit       | GET /v5/market/tickers?category=linear       | fetch_carry_rates | 同上
#  bitget      | GET /api/v2/mix/market/contracts?productType=USDT-FUTURES        | fetch_instruments | https://api.bitget.com
#  bitget      | GET /api/v2/mix/market/current-fund-rate?productType=USDT-FUTURES| fetch_carry_rates | 同上
#  hyperliquid | POST /info {"type":"meta","dex":""}          | fetch_instruments | https://api.hyperliquid.xyz
#  hyperliquid | POST /info {"type":"metaAndAssetCtxs","dex":""} | fetch_carry_rates | 同上
#  ------------|---------------------------------------------|-------------------|---------------------------
#  以下五家浏览器拉不到（CORS 拒），**服务端直连**，列在这里只为存档端点：
#  okx         | GET /api/v5/public/instruments?instType=SWAP | fetch_instruments | https://openapi.okx.com
#              | GET /api/v5/public/funding-rate?instId=…     | fetch_carry_rates（**逐品种**扇出，本来也不可预取）
#  htx         | GET /linear-swap-api/v1/swap_contract_info   | fetch_instruments | https://api.hbdm.com
#              | GET /linear-swap-api/v1/swap_batch_funding_rate | fetch_carry_rates
#  gate        | GET /futures/usdt/contracts                  | fetch_instruments | https://api.gateio.ws/api/v4
#              | GET /futures/usdt/tickers                    | fetch_carry_rates
#  kucoin      | GET /api/v1/contracts/active                 | 两者共用（_load_contracts）| https://api-futures.kucoin.com
#  backpack    | GET /api/v1/markets                          | fetch_instruments（_load_markets）| https://api.backpack.exchange
#              | GET /api/v1/markPrices                       | fetch_carry_rates
#
# 各家 base 都跟适配器里的常量走；bitget 的 productType / bybit 的 category /
# HL 的 dex 用的是适配器默认值。用户把适配器配成非默认值（如 COIN-FUTURES）时
# 缓存键对不上、自动回落服务端直连——安全的那一边，不会喂错数据。
_SPECS: tuple[_Spec, ...] = (
    _Spec("binance", "合约表", "https://fapi.binance.com", "GET",
          "/fapi/v1/exchangeInfo", trim="binance_exchange_info"),
    _Spec("binance", "结算周期", "https://fapi.binance.com", "GET",
          "/fapi/v1/fundingInfo"),
    _Spec("binance", "资金费", "https://fapi.binance.com", "GET",
          "/fapi/v1/premiumIndex", trim="binance_premium"),
    _Spec("bybit", "合约表", "https://api.bybit.com", "GET",
          "/v5/market/instruments-info", params={"category": "linear", "limit": 1000},
          trim="bybit_instruments"),
    _Spec("bybit", "资金费", "https://api.bybit.com", "GET",
          "/v5/market/tickers", params={"category": "linear"}, trim="bybit_tickers"),
    _Spec("bitget", "合约表", "https://api.bitget.com", "GET",
          "/api/v2/mix/market/contracts", params={"productType": "USDT-FUTURES"},
          trim="bitget_contracts"),
    _Spec("bitget", "资金费", "https://api.bitget.com", "GET",
          "/api/v2/mix/market/current-fund-rate", params={"productType": "USDT-FUTURES"},
          trim="bitget_unwrap"),
    _Spec("hyperliquid", "合约表", "https://api.hyperliquid.xyz", "POST",
          "/info", body={"type": "meta", "dex": ""}),
    _Spec("hyperliquid", "资金费", "https://api.hyperliquid.xyz", "POST",
          "/info", body={"type": "metaAndAssetCtxs", "dex": ""}),
)


# NiceGUI 的 WebSocket 有消息体上限，原样回传 exchangeInfo（500+ 合约 × 全字段）
# 会直接撑爆连接。所以**在浏览器里就把用不到的字段裁掉**再回传。
# 裁什么是逐个读适配器解析代码定下来的，改适配器解析时这里要跟着改——
# 裁掉一个解析要用的字段不会报错，只会让那一列静默变成默认值/零值。
_JS = """
window.efPub = {
  // 每家一次调用（不是九发一起回传）：单条消息只装一家的数据，
  // 撑爆 WebSocket 的风险跟着家数线性下降而不是叠加
  async board(specs) {
    const ok = {}, errors = {};
    for (const s of specs) {
      const id = s.venue + ':' + s.key;
      try {
        const opt = s.body
          ? { method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(s.body) }
          : { method: 'GET' };
        const r = await fetch(s.url, opt);
        if (!r.ok) {
          throw new Error('HTTP ' + r.status + ' ' + (await r.text()).slice(0, 120));
        }
        ok[id] = this._trim(s.trim, await r.json());
      } catch (e) {
        errors[id] = this._why(e);
      }
    }
    return { ok: ok, errors: errors };
  },

  // 跨域被拒时 fetch 抛的是不带任何细节的 TypeError（浏览器故意不告诉脚本
  // 是 CORS 还是断网——那本身就是信息泄露）。文案要说人话，
  // 否则用户只会看到光秃秃的 "Failed to fetch"
  _why(e) {
    const m = String((e && e.message) || e);
    if (/failed to fetch|networkerror|load failed|连接失败/i.test(m)) {
      return '浏览器跨域被拒或网络不通（CORS/断网，浏览器不透露具体是哪个）';
    }
    return m;
  },

  _trim(name, d) {
    const f = this._trims[name];
    return f ? f(d) : d;
  },

  _trims: {
    // 只留 fetch_instruments 真正读的字段：contractType/status 决定要不要，
    // filters 的三条决定精度，其余（orderTypes、timeInForce、underlyingSubType…）
    // 一个都不读，却占了响应体的九成
    binance_exchange_info(d) {
      const KEEP = { PRICE_FILTER: 1, LOT_SIZE: 1, MIN_NOTIONAL: 1 };
      return { symbols: (d.symbols || [])
        .filter(s => s.contractType === 'PERPETUAL')
        .map(s => ({
          symbol: s.symbol, contractType: s.contractType, status: s.status,
          baseAsset: s.baseAsset, quoteAsset: s.quoteAsset,
          pricePrecision: s.pricePrecision, quantityPrecision: s.quantityPrecision,
          filters: (s.filters || []).filter(f => KEEP[f.filterType]),
        })) };
    },

    // fetch_carry_rates 只读这四列，全量 premiumIndex 有 10 列
    binance_premium(d) {
      return (d || []).map(p => ({
        symbol: p.symbol, lastFundingRate: p.lastFundingRate,
        nextFundingTime: p.nextFundingTime, time: p.time,
      }));
    },

    // Bybit 的信封要**整个**留着（适配器 _parse 返回信封，不是 result）。
    // 游标翻页在浏览器里走完再合并：适配器见 nextPageCursor 为空就停，
    // 于是整张合约表一次命中，不会有第二页回落到必然 403 的服务端
    bybit_instruments(d) {
      const list = ((d.result || {}).list || []).map(r => ({
        symbol: r.symbol, contractType: r.contractType, status: r.status,
        baseCoin: r.baseCoin, quoteCoin: r.quoteCoin,
        fundingInterval: r.fundingInterval,
        priceFilter: { tickSize: (r.priceFilter || {}).tickSize },
        lotSizeFilter: { qtyStep: (r.lotSizeFilter || {}).qtyStep,
                         minNotionalValue: (r.lotSizeFilter || {}).minNotionalValue },
        leverageFilter: { maxLeverage: (r.leverageFilter || {}).maxLeverage },
      }));
      return { retCode: d.retCode, retMsg: d.retMsg, time: d.time,
               result: { category: (d.result || {}).category, list: list,
                         nextPageCursor: '' } };
    },

    bybit_tickers(d) {
      const list = ((d.result || {}).list || []).map(r => ({
        symbol: r.symbol, fundingRate: r.fundingRate,
        nextFundingTime: r.nextFundingTime,
      }));
      return { retCode: d.retCode, retMsg: d.retMsg, time: d.time,
               result: { category: (d.result || {}).category, list: list } };
    },

    // Bitget 的 _parse 剥掉 {code,msg,data} 壳只返回 data，
    // 喂进缓存的必须是**剥完之后**的形状，否则适配器拿到的是信封
    bitget_unwrap(d) {
      if (d && d.code !== undefined && d.code !== '00000') {
        throw new Error('bitget ' + d.code + ' ' + (d.msg || ''));
      }
      return (d && d.data !== undefined) ? d.data : d;
    },

    bitget_contracts(d) {
      const rows = window.efPub._trims.bitget_unwrap(d) || [];
      return rows.map(r => ({
        symbol: r.symbol, symbolStatus: r.symbolStatus,
        baseCoin: r.baseCoin, quoteCoin: r.quoteCoin,
        pricePlace: r.pricePlace, priceEndStep: r.priceEndStep,
        sizeMultiplier: r.sizeMultiplier, minTradeUSDT: r.minTradeUSDT,
        minTradeNum: r.minTradeNum, maxLever: r.maxLever,
        fundInterval: r.fundInterval,
      }));
    },
  },
};
"""


class PublicFeedError(Exception):
    """浏览器端取数整体失败（页面已关、JS 没注入、超时）。单家失败不走这里。"""


def install() -> None:
    """注入浏览器取数器。必须在 @ui.page 里调用（add_head_html 是按页的）。"""
    ui.add_head_html(f"<script>{_JS}</script>")


def specs_for(venue: str) -> tuple[_Spec, ...]:
    """某家要浏览器拉的端点。不在 BROWSER_FETCHABLE 里的家一律返回空——
    这道闸是可达性表真正生效的地方，测试钉的也是它。"""
    if venue not in BROWSER_FETCHABLE:
        return ()
    return tuple(s for s in _SPECS if s.venue == venue)


def venue_name(v: Any) -> str:
    """任何形态的"一家交易所" → 小写名字。

    **绝不用 isinstance(v, Venue) 判断**。Venue 是 `str, Enum`，`str(v)` 落到
    `"Venue.BINANCE"` 而不是 `"binance"`——名字对不上 BROWSER_FETCHABLE，
    整张表一家都匹配不上，而且**不报任何错**（那正是上线第一版的 bug：
    机会榜稳定少掉 binance 和 bybit 两条腿，状态栏一个字的提示都没有）。

    而 isinstance 本身就靠不住：NiceGUI 的 ui.run() 用 runpy.run_path 重跑入口
    文件，earnfarm.models 会被加载成两份，两份里的 Venue 是**两个不同的类**，
    枚举成员对另一份的 isinstance 一律 False。所以这里只看鸭子类型：
    有 .value 就用 .value，没有就当它本来就是名字。
    """
    return str(getattr(v, "value", v)).strip().lower()


def _venue_names(venues: Iterable[Any] | None) -> list[str]:
    """Venue 枚举 / 字符串都收，输出小写字符串。"""
    if venues is None:
        return sorted(BROWSER_FETCHABLE)
    return [venue_name(v) for v in venues]


async def feed_board_all(venues: Iterable[Any] | None = None,
                         timeout: float = 45.0) -> tuple[dict[str, int], dict[str, str]]:
    """让访客浏览器把能拉的家拉回来，喂进服务端缓存。

    返回 ``(每家成功的端点数, 每家失败的原因)``。**部分成功是正常结果**：
    调用方拿到 errors 也要继续出榜——榜上少一家，比一条都没有强得多。
    失败的家不喂缓存，于是适配器照旧走服务端直连（对 CORS 拒绝的五家来说
    那本来就是正路，对 binance/bybit 则会在榜上如实缺席）。

    整体异常（页面已关、JS 未注入）会抛 PublicFeedError——那种情况下
    一家都没喂上，和"某家失败"不是一回事。
    """
    fed: dict[str, int] = {}
    errors: dict[str, str] = {}
    names = _venue_names(venues)
    matched = False

    for venue in names:
        specs = specs_for(venue)
        if not specs:
            continue        # 服务端直连的家，压根不发浏览器请求
        matched = True
        payload = [{"venue": s.venue, "key": s.cache_key, "url": s.url,
                    "body": s.body, "trim": s.trim} for s in specs]
        expr = f"window.efPub.board({json.dumps(payload, ensure_ascii=False)})"
        try:
            raw = await ui.run_javascript(expr, timeout=timeout) or {}
        except Exception as exc:
            # 一家的整轮往返挂了（超时/页面断开）只影响这一家，继续下一家：
            # 单点故障不该让整轮取数空掉
            errors[venue] = f"浏览器取数往返失败：{exc}"
            continue

        ok = raw.get("ok") or {}
        for spec in specs:
            ident = f"{spec.venue}:{spec.cache_key}"
            if ident in ok:
                feed_public_cache(spec.venue, spec.cache_key, ok[ident])
                fed[venue] = fed.get(venue, 0) + 1
        for ident, why in (raw.get("errors") or {}).items():
            label = _label_of(ident) or ident
            prev = errors.get(venue)
            note = f"{label}：{why}"
            errors[venue] = f"{prev}；{note}" if prev else note
        if venue not in fed and venue not in errors:
            # 既没喂上也没报错 = 浏览器回了个空壳（响应被截断、efPub 版本对不上）。
            # **不许静默**：这一格空着，调用方会以为这家本来就不用浏览器取数
            errors[venue] = "浏览器回了空响应（既没数据也没错误）"

    if names and not matched:
        # 一家都没匹配上 = 名字对不上表，而不是"这批家都走服务端"。
        # 这正是上线第一版那个 bug 的形态：整个函数瞬间返回两个空 dict，
        # 界面上一个字都不说，机会榜稳定少两条腿。宁可吵，也不能再哑一次。
        errors["浏览器取数"] = (
            f"没有一家匹配上取数表（收到的名字：{'、'.join(sorted(set(names)))}；"
            f"表里的名字：{'、'.join(sorted(BROWSER_FETCHABLE))}）")

    return fed, errors


def _label_of(ident: str) -> str:
    """`"bybit:GET /v5/…"` → `"合约表"`，给用户看的名字。"""
    for spec in _SPECS:
        if ident == f"{spec.venue}:{spec.cache_key}":
            return spec.label
    return ""
