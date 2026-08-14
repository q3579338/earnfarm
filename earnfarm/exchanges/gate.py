"""Gate.io (Gate.com) apiv4 适配器 —— U 本位永续 (settle=usdt)。

与其它五家最不一样的三点，全部落在这个文件里：

1. **size 是"张数"且带符号，没有 side 字段**。正=买/多，负=卖/空，单位是合约张数
   不是币。币数 = size * quanto_multiplier（BTC 0.0001 币/张、DOGE 10 币/张）。
   本适配器**对外一律用 base 币数量**（OrderRequest.qty / Position.qty / BookTop 的档位量），
   张数换算封死在内部——上层不该知道 Gate 用张。
   代价是 DOGE 这种 10 币/张的合约存在无法消除的残余 delta，所以
   Instrument.lot_size 存的是**币口径**的粒度（order_size_min * quanto_multiplier），
   让评分层直接看得见这个颗粒度。

2. **签名的 query 是原始串、不 url-encode、且顺序敏感**；body 哈希必须是真正上线的字节。
   base._request 用 httpx 的 `json=` 参数发 body，httpx 内部用
   `json.dumps(..., ensure_ascii=False, separators=(",", ":"))` 序列化，
   所以这里必须用**完全相同**的序列化去算 SHA-512，否则 INVALID_SIGNATURE。
   为了让 query 字节级可控，_prepare 直接把 query 拼进 URL，不走 httpx 的 params。

3. **setLeverage 这类路径含 positions/dual_ 的 POST，参数走 query 而不是 body**，
   body 为空（哈希是空串常量）。按 JSON body 签会直接 INVALID_SIGNATURE。

返佣：`X-Gate-Channel-Id` header，不进签名串，broker_code 为空时一个字节都不加。
注意它和订单的 `text`（client order id，必须 t- 前缀）是两码事，把返佣码塞 text 拿不到返佣。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import itertools
import json
import re
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal
from typing import Any, AsyncIterator, Iterator, Sequence
from urllib.parse import urlsplit

import httpx

from ..models import (
    BookDepth,
    BookTop,
    CarryRate,
    FeeSchedule,
    Instrument,
    MarketKind,
    Position,
    Venue,
)
from .base import (
    Credential,
    ExchangeAdapter,
    ExchangeError,
    OrderRejected,
    OrderRequest,
    OrderResult,
    OrderUnknown,
    RateLimited,
)

ZERO = Decimal("0")

REST_BASE = "https://api.gateio.ws/api/v4"
WS_BASE = "wss://fx-ws.gateio.ws/v4/ws"

# 空 body 的 SHA-512（调研文档给的金标准常量，与 hashlib.sha512(b"") 一致）
EMPTY_BODY_SHA512 = hashlib.sha512(b"").hexdigest()

# Timestamp 容忍窗口 60s，且**先于 key 校验**——时间错了报的是 REQUEST_EXPIRED 不是 INVALID_KEY
GATE_RECV_WINDOW_S = 60
# 下单/改单可带 x-gate-exptime（unix 毫秒），迟到的请求由服务端拒掉而不是过期成交。
# 500ms 收敛循环里这是最值钱的安全特性，给 1.5s 余量。
ORDER_EXPTIME_TTL_MS = 1_500

# 资金费历史硬上限：limit<=1000、回溯<=180 天，且**必须显式传 to**，
# 否则服务端会静默把窗口夹成 30 天（未文档化行为，调研实测）
FUNDING_HISTORY_MAX_LIMIT = 1000
FUNDING_HISTORY_MAX_LOOKBACK_S = 180 * 86400
# 向旧翻页的硬闸。180 天 ÷ 1000 条/页：1h 合约最多 5 页，留足余量同时
# 挡住"服务端无视 to"时的无限翻页
FUNDING_HISTORY_MAX_PAGES = 8

# 多档深度取多少档。调研文档只给了端点形状
# （GET /futures/{settle}/order_book?contract=&limit=&with_id=true），**没有写 limit 上限**，
# 所以取一个保守且够用的 50：10bp 带宽在主流币上 50 档远远吃不完，小币的簿本来就稀，
# 要多少档也变不出量来。真取不满就如实回 levels_used，绝不外推补足。
DEPTH_LEVELS = 50

# WS 订阅是**原子**的：payload 里有一个不认识的合约，整批订阅失败且一条数据都不推。
# 合约表里有两个 CJK 名字（币安人生_USDT / 龙虾_USDT），必须先正则过滤再分批订阅。
WS_SYMBOL_RE = re.compile(r"^[A-Z0-9]+_[A-Z]+$")
WS_SUBSCRIBE_BATCH = 100

# 业务错误码翻译。调研文档只实测到其中一部分（REQUEST_EXPIRED / INVALID_KEY /
# INVALID_SIGNATURE / MISSING_REQUIRED_HEADER / INVALID_PARAM_VALUE / ERR_QUOTA_LOWER_LIMIT），
# 其余按 Gate 公开错误表补齐；未命中的 label 一律落到 ExchangeError(retriable=False)，
# 宁可不重试也不要把一个可能已成交的请求当成可安全重发。
_RATE_LIMIT_LABELS = frozenset({
    "TOO_MANY_REQUESTS", "REQUEST_LIMIT_EXCEEDED", "RATE_LIMIT_EXCEEDED",
})
_REJECT_LABELS = frozenset({
    "INVALID_PARAM_VALUE", "INVALID_PARAM", "INVALID_REQUEST_PARAM",
    "INVALID_ARGUMENT", "MISSING_REQUIRED_PARAM",
    "ERR_QUOTA_LOWER_LIMIT",          # 未达最小下单量
    "INVALID_ORDER_SIZE", "ORDER_SIZE_TOO_LARGE", "TOO_MANY_ORDERS",
    "BALANCE_NOT_ENOUGH", "MARGIN_BALANCE_NOT_ENOUGH", "INSUFFICIENT_AVAILABLE",
    "LIQUIDATE_IMMEDIATELY", "POC_FILL_IMMEDIATELY", "FOK_NOT_FILL",
    "REDUCE_ONLY_ORDER_NOT_MATCH", "ORDER_PRICE_DEVIATE",
    "RISK_LIMIT_EXCEEDED", "RISK_LIMIT_NOT_MULTIPLE",
    "CONTRACT_NOT_FOUND", "USER_NOT_FOUND",
})
# 订单不存在：base.resolve_unknown_order 靠 OrderRejected 判定"这单从未被接受"，
# 所以它必须映射成 OrderRejected 而不是通用错误。
_ORDER_GONE_LABELS = frozenset({"ORDER_NOT_FOUND"})
# 订单**存在过且已终结**（可能已成交）。绝不能和 ORDER_NOT_FOUND 混成一个桶：
# 混了之后 query_order 会走 OrderRejected 那条路，让 resolve_unknown_order 返回 0，
# 一张真成交了的单被报成"零成交"，上层照着 0 再补一次仓 = 双倍仓位。
# 对 cancel_order 它是"已终结、继续查单"，对 query_order 必须原样上抛。
_ORDER_TERMINAL_LABELS = frozenset({
    "ORDER_CLOSED", "ORDER_CANCELLED", "ORDER_FINISHED",
})
_RETRIABLE_LABELS = frozenset({
    "SERVER_ERROR", "INTERNAL", "SERVICE_UNAVAILABLE", "TIMEOUT",
    "REQUEST_EXPIRED",                # 时钟漂了，重新校时后可重试
})
# 这些错误在**进撮合之前**就被挡下了（鉴权层 / 时间戳层），订单确定不存在。
# 下单路径上不能把它们升级成 OrderUnknown——那会白白多跑一轮 cancel+查单。
_PRE_MATCH_LABELS = frozenset({
    "REQUEST_EXPIRED", "INVALID_KEY", "INVALID_SIGNATURE",
    "MISSING_REQUIRED_HEADER",
})


def _dec(v: Any, default: str = "0") -> Decimal:
    """交易所字符串 → Decimal。绝不经过 float。"""
    if v is None or v == "":
        return Decimal(default)
    if isinstance(v, Decimal):
        return v
    if isinstance(v, float):        # 只可能来自 json 解析的裸数字，尽量避免
        return Decimal(repr(v))
    return Decimal(str(v))


def _dec_str(v: Decimal) -> str:
    """定点字符串。价格绝不能发成 1E-5 这种科学计数法，Gate 会拒。"""
    return format(v, "f")


def _json_dumps(obj: Any) -> str:
    """必须和 httpx 的 encode_json 逐字节一致，否则签名对不上。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _chunks(seq: Sequence[str], n: int) -> Iterator[Sequence[str]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class _ContractMeta:
    """一个合约里下单/换算真正要用到的字段。

    单独存是因为 Instrument 里没地方放 quanto_multiplier 之外的 Gate 特有项
    （funding_next_apply / funding_rate_limit / enable_decimal）。
    """

    __slots__ = ("symbol", "quanto", "tick", "size_min", "size_max",
                 "funding_interval_s", "funding_next_apply_s", "funding_limit",
                 "enable_decimal")

    def __init__(self, raw: dict) -> None:
        self.symbol: str = raw["name"]
        self.quanto: Decimal = _dec(raw.get("quanto_multiplier"), "1")
        self.tick: Decimal = _dec(raw.get("order_price_round"), "0")
        self.size_min: Decimal = _dec(raw.get("order_size_min"), "1")
        self.size_max: Decimal = _dec(raw.get("order_size_max"), "0")
        self.funding_interval_s: int = int(raw.get("funding_interval") or 28800)
        self.funding_next_apply_s: int = int(raw.get("funding_next_apply") or 0)
        self.funding_limit: Decimal = _dec(raw.get("funding_rate_limit"), "0")
        self.enable_decimal: bool = bool(raw.get("enable_decimal"))

    @property
    def coin_step(self) -> Decimal:
        """币口径的最小变动。order_size_min 为 0（decimal 合约）时退回 1 张。

        TODO: enable_decimal 的小数步长 Gate 没有任何字段暴露（调研实测），
        目前一律按整数张下单——整数张在 decimal 合约上必然合法，只是粒度粗。
        """
        lots = self.size_min if self.size_min > 0 else Decimal("1")
        return lots * self.quanto


class GateAdapter(ExchangeAdapter):
    """Gate U 本位永续适配器。

    settle 固定 usdt。settle=btc 是反向合约（quanto_multiplier 为 "0"，
    一张 = 固定美元面值），本适配器的"币数 = 张数 * quanto"换算在那里不成立，
    直接拒绝构造，别让它悄悄算出错误仓位。
    """

    venue = Venue.GATE
    rest_base = REST_BASE

    def __init__(self, credential: Credential, *, broker_code: str = "",
                 client: httpx.AsyncClient | None = None,
                 settle: str = "usdt",
                 market_kind: MarketKind = MarketKind.PERP,
                 cross_margin: bool = False,
                 crypto_only: bool = True) -> None:
        if market_kind is not MarketKind.PERP:
            # 现货/杠杆没实现：调研文档 exchange-gate.io.md 全篇只覆盖 futures，
            # 缺 (a) /spot/currency_pairs 的 precision/amount_precision/min_quote_amount
            #    (b) /spot/orders 的字段与 account=spot|margin|unified 语义
            #    (c) 杠杆借币利率端点与计息周期（CarryKind.INTEREST 需要）
            #    (d) 现货市价买单的 amount 口径（见 spot_market_buy_amount 的注释）
            # 凭记忆补这些等于赌，宁可不实现。
            raise NotImplementedError(
                f"gate 适配器目前只支持 {MarketKind.PERP.value}；"
                f"{market_kind.value} 缺少现货/杠杆端点调研（精度字段、下单字段、借币利率）"
            )
        if settle != "usdt":
            raise NotImplementedError(
                "gate 反向合约(settle=btc)的 quanto_multiplier 为 0，"
                "币数换算不成立，需要单独的 PERP_COIN 适配器"
            )
        # 用户从网页复制返佣码常带尾随空白/换行，六家统一在构造期剥掉
        super().__init__(credential, broker_code=broker_code.strip(), client=client)
        self.settle = settle
        self.market_kind = market_kind
        self.cross_margin = cross_margin
        self.crypto_only = crypto_only
        self.market = f"{self.venue.value}:{market_kind.value}"
        # rest_base 已含 /api/v4，而签名串里的 PATH 也必须含它
        self._path_prefix = urlsplit(self.rest_base).path
        self._meta: dict[str, _ContractMeta] = {}
        self._ws_id = itertools.count(1)
        # 每次响应都带的剩余配额，读它比猜限频靠谱
        self.rate_limit_remain: int | None = None

    # ---- 签名 / 请求 -----------------------------------------------------

    def _sign_payload(self, method: str, full_path: str, query: str,
                      body_raw: str, ts: str) -> str:
        """签名串 = METHOD\\nPATH\\nQUERY\\nhex(SHA512(body))\\nTIMESTAMP。

        PATH 含 /api/v4；QUERY 是**原始未编码**串且顺序必须和 URL 一致；
        空 body 用空串常量哈希。
        """
        body_hash = hashlib.sha512(body_raw.encode()).hexdigest() if body_raw else EMPTY_BODY_SHA512
        return "\n".join((method.upper(), full_path, query, body_hash, ts))

    def _hmac_sha512_hex(self, payload: str) -> str:
        # 基类只给了 SHA-256，Gate 用 SHA-512
        return hmac.new(self._cred.api_secret.encode(), payload.encode(),
                        hashlib.sha512).hexdigest()

    def _prepare(self, method: str, path: str, params: dict, body: dict,
                 signed: bool) -> tuple[dict[str, str], str, dict, dict]:
        # query 亲手拼：httpx 会按自己的规则编码，而签名要的是原始串，
        # 两边一旦不一致就是 INVALID_SIGNATURE（逗号被 %2C 是最常见的翻车点）
        items = [(k, v) for k, v in params.items() if v is not None]
        query = "&".join(f"{k}={v}" for k, v in items)
        url = self.rest_base + path + (f"?{query}" if query else "")

        headers: dict[str, str] = {"Accept": "application/json"}
        # 不带这个 header，Gate 会把所有 size 截断成整数（实测 5116.8 -> 5116）。
        # REST 和 WS 都吃这个 header，一律带上并把 size 当字符串解析。
        headers["X-Gate-Size-Decimal"] = "1"
        # 返佣码：纯 header，不进签名串。broker_code 为空则**完全不出现**。
        # （构造期已 strip：带尾随换行会让 httpx 在拼 header 时抛 LocalProtocolError，
        #  报错点离配置很远，很难查。）
        if self.broker_code:
            headers["X-Gate-Channel-Id"] = self.broker_code

        body_raw = _json_dumps(body) if body else ""
        if body_raw:
            headers["Content-Type"] = "application/json"

        if signed:
            ts = str(self.timestamp_ms() // 1000)      # 秒，不是毫秒
            payload = self._sign_payload(method, self._path_prefix + path,
                                         query, body_raw, ts)
            headers["KEY"] = self._cred.api_key
            headers["SIGN"] = self._hmac_sha512_hex(payload)
            headers["Timestamp"] = ts

        # params 交空字典：query 已经在 url 里，再传一遍会重复
        return headers, url, {}, body

    def _parse(self, resp: httpx.Response) -> Any:
        remain = resp.headers.get("X-Gate-RateLimit-Requests-Remain")
        if remain is not None:
            try:
                self.rate_limit_remain = int(remain)
            except ValueError:
                pass
        bad_json = False
        try:
            # parse_float=Decimal：Gate 确实会返回裸 JSON 数字（order 的 size/left 是
            # int、order_book 的 current 是浮点秒），靠 _dec 的 Decimal(repr(v)) 兜底
            # 时精度已经掉了。这道闸必须在解析层。
            data = json.loads(resp.text, parse_float=Decimal) if resp.text else None
        except ValueError:
            data = None
            bad_json = True

        if bad_json and resp.status_code < 400:
            # 2xx 但响应体读不出来（WAF/代理注入 HTML）：这单有没有进撮合完全不知道。
            # 以前会一路返回 None 让 _parse_order(None) 抛 AttributeError，
            # 靠 executor 的 except Exception 兜住纯属巧合。统一成 BAD_JSON。
            raise ExchangeError(self.venue.value, "BAD_JSON",
                                f"响应不是合法 JSON: {resp.text[:200]}", retriable=True)

        if resp.status_code >= 400 or (isinstance(data, dict) and data.get("label")):
            label = ""
            message = resp.text[:200]
            if isinstance(data, dict):
                label = str(data.get("label") or "")
                message = str(data.get("message") or data.get("detail") or message)
            raise self._translate(label, message, resp.status_code)
        return data

    def _translate(self, label: str, message: str, status: int) -> ExchangeError:
        venue = self.venue.value
        if label == "REQUEST_EXPIRED":
            # 时钟漂出 60s 窗口。强制下次请求前重新校时，否则会一直撞墙。
            self._clock.synced_at = 0.0
            return ExchangeError(venue, label, message, retriable=True)
        if status == 429 or label in _RATE_LIMIT_LABELS:
            return RateLimited(venue, label or "RATE_LIMITED", message, retriable=True)
        if label in _ORDER_GONE_LABELS:
            return OrderRejected(venue, label, message, retriable=False)
        if label in _ORDER_TERMINAL_LABELS:
            # 已终结 ≠ 不存在。cancel_order 会自己把它当成功吞掉；
            # query_order 让它上抛，绝不能变成 OrderRejected 谎报"零成交"。
            return ExchangeError(venue, label, message, retriable=False)
        if label in _REJECT_LABELS:
            return OrderRejected(venue, label, message, retriable=False)
        if label in _RETRIABLE_LABELS or status >= 500:
            return ExchangeError(venue, label or f"HTTP_{status}", message, retriable=True)
        return ExchangeError(venue, label or f"HTTP_{status}", message, retriable=False)

    # ---- 元数据 ---------------------------------------------------------

    async def fetch_server_time_ms(self) -> int:
        # 调研文档没写校时端点。/spot/time 是 apiv4 唯一的公开时间源；
        # 万一它不可用就退回 order_book 的 current 字段（服务端秒级浮点时间戳）。
        try:
            data = await self._request("GET", "/spot/time", quota="market")
            return int(data["server_time"])
        except (ExchangeError, KeyError, TypeError, ValueError):
            data = await self._request(
                "GET", f"/futures/{self.settle}/order_book",
                quota="market", params={"contract": "BTC_USDT", "limit": 1},
            )
            return self._book_ts_ms(data)

    async def fetch_instruments(self) -> Sequence[Instrument]:
        raw = await self._request("GET", f"/futures/{self.settle}/contracts",
                                  quota="market")
        out: list[Instrument] = []
        for c in raw:
            name = c.get("name") or ""
            # 合约表被污染：284 个股票 perp、指数/金属/外汇，以及两个 CJK 名字的合约。
            # 非 ASCII 名字会让 WS 整批订阅原子失败，任何情况下都要滤掉。
            if not WS_SYMBOL_RE.match(name):
                continue
            if self.crypto_only and (c.get("contract_type") or "") != "":
                continue
            if c.get("in_delisting"):
                continue
            meta = _ContractMeta(c)
            self._meta[name] = meta
            base, _, quote = name.rpartition("_")
            out.append(Instrument(
                market=self.market,
                symbol=name,
                base=base,
                quote=quote,
                tick_size=meta.tick,               # = order_price_round
                lot_size=meta.coin_step,           # 币口径粒度，见类文档
                # Gate 没有 minNotional 字段，有效下限 = lot_size * price，
                # 只有拿到价格才算得出来，这里留 0，下单前用 min_notional_at() 现算。
                min_notional=ZERO,
                contract_size=meta.quanto,
                max_leverage=_dec(c.get("leverage_max"), "1"),
                funding_interval_s=meta.funding_interval_s,
            ))
        return out

    @staticmethod
    def min_notional_at(inst: Instrument, price: Decimal) -> Decimal:
        """Gate 不给 minNotional，有效下限 = 一张的币数 * 价格（BTC 约 $6.3 @63k）。
        低于它下单会拿到 ERR_QUOTA_LOWER_LIMIT。"""
        return inst.lot_size * price

    async def _ensure_meta(self, symbols: Sequence[str]) -> None:
        """按需补齐合约元数据。缺的多就整表拉一次，缺一两个就单个拉。"""
        missing = [s for s in symbols if s not in self._meta]
        if not missing:
            return
        if len(missing) > 3:
            await self.fetch_instruments()
            missing = [s for s in symbols if s not in self._meta]
        for sym in missing:
            raw = await self._request(
                "GET", f"/futures/{self.settle}/contracts/{sym}", quota="market")
            self._meta[sym] = _ContractMeta(raw)

    async def _meta_of(self, symbol: str) -> _ContractMeta:
        await self._ensure_meta([symbol])
        return self._meta[symbol]

    async def fetch_fee_schedule(self, symbols: Sequence[str]) -> Sequence[FeeSchedule]:
        """用户实际费率档位。每条都带 symbol，调用方按 symbol 匹配。

        /contracts 上的 maker_fee_rate/taker_fee_rate 是**基础档**不是你的档，
        拿它算 P&L 会系统性错判。必须走这个私有端点。

        GT 抵扣不用单独处理：这个端点返回的已经是 VIP 档 + GT 折扣 + 子账户
        协商价全部叠加后的**最终**费率（调研文档 exchange-gate.io.md 明确写了这点）。
        另有 GET /wallet/fee 能读到 gt_discount 布尔位，但那只是状态展示，
        费率本身已经含折扣，再乘一次就是重复打折。
        """
        params: dict[str, Any] = {}
        if len(symbols) == 1:
            params["contract"] = symbols[0]
        data = await self._request("GET", f"/futures/{self.settle}/fee",
                                   quota="market", signed=True, params=params)
        data = data if isinstance(data, dict) else {}
        wanted = list(symbols) if symbols else list(data)
        out: list[FeeSchedule] = []
        for sym in wanted:
            row = data.get(sym)
            # 原来这里是 `data.get(sym) or {}`，缺失时 _dec(None) 得到 0 —— 一个
            # **零手续费**的档位。四笔 taker 成本凭空消失，负期望的机会全翻成正期望，
            # 而且不报任何错。查不到就不返回，让上层退回默认挂牌价并在界面标出来。
            if not isinstance(row, dict):
                continue
            maker_raw, taker_raw = row.get("maker_fee"), row.get("taker_fee")
            if maker_raw in (None, "") or taker_raw in (None, ""):
                continue
            out.append(FeeSchedule(
                market=self.market, symbol=sym,
                maker=_dec(maker_raw),
                taker=_dec(taker_raw),
            ))
        return out

    async def fetch_funding_limits(self, symbol: str) -> tuple[Decimal, Decimal] | None:
        """返回 (cap, floor)。Gate 只给单边 funding_rate_limit，上下限对称。"""
        meta = await self._meta_of(symbol)
        if meta.funding_limit <= 0:
            return None
        return meta.funding_limit, -meta.funding_limit

    # ---- 行情 -----------------------------------------------------------

    @staticmethod
    def _book_ts_ms(data: dict) -> int:
        """order_book 的 current/update 是秒级（可能带小数）时间戳。
        调研文档没写单位，这里按量级自适应，别把秒当毫秒用。"""
        for key in ("current", "update"):
            v = data.get(key)
            if v in (None, ""):
                continue
            f = _dec(v)
            return int(f) if f > Decimal("1e12") else int(f * 1000)
        return 0

    async def fetch_book_top(self, symbol: str) -> BookTop:
        meta = await self._meta_of(symbol)
        data = await self._request(
            "GET", f"/futures/{self.settle}/order_book", quota="market",
            params={"contract": symbol, "limit": 1, "with_id": "true"},
        )
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        if not bids or not asks:
            raise ExchangeError(self.venue.value, "EMPTY_BOOK",
                                f"{symbol} 盘口单边为空", retriable=True)
        ts = self._book_ts_ms(data) or self.timestamp_ms()
        return BookTop(
            bid_price=_dec(bids[0]["p"]),
            # 档位量是张数，对外统一换成币
            bid_qty=_dec(bids[0]["s"]) * meta.quanto,
            ask_price=_dec(asks[0]["p"]),
            ask_qty=_dec(asks[0]["s"]) * meta.quanto,
            ts_ms=ts,
        )

    @staticmethod
    def _band_side(levels: Sequence[dict], quanto: Decimal,
                   lo: Decimal, hi: Decimal, descending: bool) -> tuple[Decimal, int]:
        """一侧盘口在 [lo, hi] 内的累计名义额和档数。

        簿是排好序的（bids 降序、asks 升序），所以越过**远端**边界就可以直接停——
        后面只会更远。越过**近端**边界（正常簿不会有，只有交叉/陈旧簿会）只跳过这一档，
        不能停：再往后可能还有落在带内的档位。

        名义额 = 价 * 张数 * quanto_multiplier。少乘 quanto 就是把张数当币数，
        BTC 会低估 1 万倍、DOGE 会高估 10 倍——这正是要修的那个量级错误。
        """
        notional = ZERO
        used = 0
        for lv in levels:
            p = _dec(lv.get("p"))
            s = _dec(lv.get("s"))
            if p <= 0 or s <= 0:
                continue                      # 脏档位不计入，也不算进 levels_used
            if p < lo:
                if descending:
                    break
                continue
            if p > hi:
                if descending:
                    continue
                break
            notional += p * s * quanto
            used += 1
        return notional, used

    async def fetch_book_depth(self, symbol: str, band_bp: float = 10.0) -> BookDepth:
        """band_bp 基点带宽内的可见深度（名义额，quote 计价）。

        以中价为基准，买卖两侧各统计 [mid*(1-band), mid] 和 [mid, mid*(1+band)]
        内的累计名义额。只拿一档当深度代理会把容量低估一两个数量级。

        限频：Gate 公开端点**没有权重制**，一律 200 请求 / 10 秒 / 端点，按 IP 计
        （X-Gate-RateLimit-Requests-Remain 响应头是真值，别猜）。坑在于本方法和
        fetch_book_top 打的是**同一个 order_book 端点桶**——深度轮询直接吃掉一档轮询的
        额度，所以走 quota="market" 和行情共池，绝不能挪去 risk/trade 池。
        贵的地方是带宽不是配额：limit=50 的响应体比 limit=1 大两个数量级。

        levels_used = 买卖两侧计入档位之和。它等于交易所实际返回的档数时，说明簿在
        DEPTH_LEVELS 档处就被截断了、带宽可能没盖满——如实低估，不外推。
        """
        if band_bp <= 0:
            raise ValueError(f"band_bp 必须为正，收到 {band_bp}")
        meta = await self._meta_of(symbol)
        data = await self._request(
            "GET", f"/futures/{self.settle}/order_book", quota="market",
            params={"contract": symbol, "limit": DEPTH_LEVELS, "with_id": "true"},
        )
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        if not bids or not asks:
            # 单边空簿算不出中价，这一拍不可用。和 fetch_book_top 同一口径。
            raise ExchangeError(self.venue.value, "EMPTY_BOOK",
                                f"{symbol} 盘口单边为空", retriable=True)

        best_bid = _dec(bids[0]["p"])
        best_ask = _dec(asks[0]["p"])
        # band_bp 是 float（scoring 那边就是 float 口径），只在这一处经 str 转成 Decimal，
        # 之后全程 Decimal —— 直接 Decimal(float) 会把 10.0/1e4 变成一串二进制尾巴
        mid = (best_bid + best_ask) / 2
        band = Decimal(str(band_bp)) / Decimal("10000")
        lo = mid * (1 - band)
        hi = mid * (1 + band)

        bid_notional, bid_levels = self._band_side(bids, meta.quanto, lo, mid, True)
        ask_notional, ask_levels = self._band_side(asks, meta.quanto, mid, hi, False)

        return BookDepth(
            symbol=symbol,
            bid_notional=bid_notional,
            ask_notional=ask_notional,
            bid_price=best_bid,
            # 一档量同样是张数，对外统一换成币（和 fetch_book_top 一致）
            bid_qty=_dec(bids[0]["s"]) * meta.quanto,
            ask_price=best_ask,
            ask_qty=_dec(asks[0]["s"]) * meta.quanto,
            levels_used=bid_levels + ask_levels,
            band_bp=band_bp,
            ts_ms=self._book_ts_ms(data) or self.timestamp_ms(),
        )

    def _next_settle_ms(self, meta: _ContractMeta) -> int:
        """funding_next_apply 是 /contracts 的快照，放久了会变成过去时间，
        按周期推进到下一个未来时点。"""
        now_s = self.timestamp_ms() // 1000
        nxt = meta.funding_next_apply_s
        iv = meta.funding_interval_s
        if nxt <= 0 or iv <= 0:
            return 0
        if nxt <= now_s:
            nxt += ((now_s - nxt) // iv + 1) * iv
        return nxt * 1000

    async def fetch_carry_rates(self, symbols: Sequence[str] | None = None
                                ) -> Sequence[CarryRate]:
        """符号用**交易所原始约定**：正 = 多头付给空头，原样存不取反。

        （base.py 的抽象方法注释写的是"多头视角、要取负"，与 models.CarryRate
        的类文档和 scoring.LegQuote.funding_now 的约定相反；全仓上下游都按
        models/scoring 的原始约定跑，这里跟随 models —— 取反会让 scoring 里的
        `short_rate - long_rate` 全体反号。）
        """
        params: dict[str, Any] = {}
        if symbols and len(symbols) == 1:
            params["contract"] = symbols[0]
        rows = await self._request("GET", f"/futures/{self.settle}/tickers",
                                   quota="market", params=params)
        wanted = set(symbols) if symbols else None
        names = [r["contract"] for r in rows
                 if wanted is None or r["contract"] in wanted]
        # REST /tickers **不带 funding_interval**（只有 WS 推送和 /contracts 有）。
        # 不 join 就会把 4h/1h 合约当 8h 年化，低估 3 倍和 8 倍。
        await self._ensure_meta(names)

        now_ms = self.timestamp_ms()
        out: list[CarryRate] = []
        for r in rows:
            sym = r["contract"]
            if wanted is not None and sym not in wanted:
                continue
            meta = self._meta.get(sym)
            if meta is None:
                continue
            rate = r.get("funding_rate")
            if rate in (None, ""):
                rate = r.get("funding_rate_indicative")
            out.append(CarryRate(
                market=self.market,
                symbol=sym,
                rate=_dec(rate),
                interval_s=meta.funding_interval_s,
                next_settle_ms=self._next_settle_ms(meta),
                ts_ms=now_ms,
            ))
        return out

    async def fetch_funding_history(self, symbol: str, since_ms: int,
                                    limit: int = 1000
                                    ) -> Sequence[tuple[int, Decimal]]:
        """历史资金费，升序返回 [since_ms, 现在] 的**完整**区间。

        单次最多 1000 条且保留最新端，所以 sub-8h 合约请求长窗口时会被静默削短：
        实测 BANK_USDT（1h）请求 180 天只拿回 161 天，调用方收不到任何提示。
        Gate 有 341 个 sub-8h 合约，所以这里按 `to` 向旧翻页把窗口补齐——
        limit 只当页大小用，不当返回条数的上限（见基类的契约说明）。
        """
        now_s = self.timestamp_ms() // 1000
        # 回溯超过 180 天直接 400，先夹住；留 60s 余量避免踩边界
        oldest = now_s - FUNDING_HISTORY_MAX_LOOKBACK_S + 60
        frm = max(since_ms // 1000, oldest)
        page_size = min(max(int(limit), 1), FUNDING_HISTORY_MAX_LIMIT)

        collected: dict[int, Decimal] = {}
        to_s = now_s
        for _ in range(FUNDING_HISTORY_MAX_PAGES):
            if to_s < frm:
                break
            rows = await self._request("GET", f"/futures/{self.settle}/funding_rate",
                                       quota="market",
                                       params={
                                           "contract": symbol,
                                           "limit": page_size,
                                           "from": frm,
                                           # 未文档化行为：只传 from 不传 to，服务端会把
                                           # 窗口静默夹成 30 天。必须显式传 to，
                                           # 否则拿到的样本量根本撑不起稳定性评分。
                                           "to": to_s,
                                       }) or []
            if not rows:
                break
            for r in rows:
                collected[int(r["t"]) * 1000] = _dec(r["r"])
            if len(rows) < page_size:
                break                      # 不满一页 = 窗口内已经给完了
            # 返回是 newest first，往旧翻：下一页的右端 = 本页最老一条 −1 秒。
            # 减 1 是为了不把同一条再取回来（from/to 都是含边界的）
            nxt = min(int(r["t"]) for r in rows) - 1
            if nxt >= to_s:
                break                      # 游标不前进，再翻就是死循环
            to_s = nxt
        return sorted(collected.items())

    async def stream_book_top(self, symbols: Sequence[str]) -> AsyncIterator[BookTop]:
        """futures.book_ticker 盘口流。

        最小可用版：订阅 + 心跳 + 重连。Gate 是**服务端发 ping**、客户端必须回 pong，
        websockets 库自动回，别把库级 pong 关掉。
        TODO: 增量深度 futures.order_book_update（U == local_id + 1 的序号校验，
        断号必须退订重订）和 futures.obu V2 都没接，目前只有一档。
        """
        import websockets      # 延迟导入：只跑 REST 的场景不该被它拖着

        valid = [s for s in symbols if WS_SYMBOL_RE.match(s)]
        if not valid:
            return
        await self._ensure_meta(valid)
        url = f"{WS_BASE}/{self.settle}"
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    url,
                    additional_headers={"X-Gate-Size-Decimal": "1"},
                    ping_interval=20, ping_timeout=20, max_queue=2048,
                ) as ws:
                    backoff = 1.0
                    # 一个未知合约会让整批订阅原子失败，所以分批 + 已正则过滤
                    for batch in _chunks(valid, WS_SUBSCRIBE_BATCH):
                        await ws.send(_json_dumps({
                            "time": self.timestamp_ms() // 1000,
                            "id": next(self._ws_id) & 0x7FFFFFFF,   # 必须是 int32
                            "channel": "futures.book_ticker",
                            "event": "subscribe",
                            "payload": list(batch),
                        }))
                    async for raw in ws:
                        msg = json.loads(raw, parse_float=Decimal)
                        ch = msg.get("channel")
                        if ch == "futures.system":
                            # result.type == "upgrade" 是服务端预告重启，立刻重连
                            if (msg.get("result") or {}).get("type") == "upgrade":
                                break
                            continue
                        if ch != "futures.book_ticker" or msg.get("event") != "update":
                            continue
                        top = self._parse_book_ticker(msg.get("result") or {})
                        if top is not None:
                            yield top
            except asyncio.CancelledError:
                raise
            except Exception:
                # 断线不该炸掉整个引擎；退避重连，重连后一切订阅都要重来
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _parse_book_ticker(self, r: dict) -> BookTop | None:
        sym = r.get("s")
        meta = self._meta.get(sym or "")
        if meta is None:
            return None
        # b/a 为空字符串表示该边没量，这一拍不可用
        if r.get("b") in (None, "") or r.get("a") in (None, ""):
            return None
        return BookTop(
            bid_price=_dec(r["b"]),
            bid_qty=_dec(r.get("B")) * meta.quanto,
            ask_price=_dec(r["a"]),
            ask_qty=_dec(r.get("A")) * meta.quanto,
            ts_ms=int(r.get("t") or self.timestamp_ms()),
        )

    # ---- 精度 -----------------------------------------------------------

    @staticmethod
    def round_price(price: Decimal, tick: Decimal, side: str) -> Decimal:
        """价格规整到 order_price_round 的整数倍。

        买单向上、卖单向下——方向与 executor.cross_price 一致。保护限价是故意穿透
        对手价的，反向取整会把穿透削回来；在 tick 相对价格很粗的小币上那不是少赚
        一个 tick，而是 IOC 根本挂不上、这条腿永远补不平。
        """
        if tick <= 0:
            return price
        mode = ROUND_CEILING if side == "buy" else ROUND_FLOOR
        n = (price / tick).to_integral_value(rounding=mode)
        return (n * tick).quantize(tick)

    @staticmethod
    def coins_to_contracts(qty_coins: Decimal, meta: _ContractMeta) -> Decimal:
        """币数 → 张数，**向零取整**。宁可少下一点也不能超过目标。"""
        if meta.quanto <= 0:
            raise ExchangeError("gate", "BAD_CONTRACT",
                                f"{meta.symbol} quanto_multiplier 非正，无法换算")
        return (qty_coins / meta.quanto).to_integral_value(rounding=ROUND_DOWN)

    @staticmethod
    def spot_market_buy_amount(qty_coins: Decimal, price: Decimal) -> Decimal:
        """【坑，现货未启用但先把口径钉死】Gate 现货市价**买**单的 amount 是要花掉的
        计价币数量（USDT），不是要买的币数量；市价卖单的 amount 才是币数量。

        直接把币数填进去，会变成"用 0.5 USDT 买 BTC"这种量级错误的成交，
        而不是报错——所以必须显式换算：amount = 币数 * 参考价。
        换算用的是**参考价**，实际成交按盘口逐档吃，成交币数必然与预期有出入，
        上层必须以成交回报为准做仓位差分，不能假定拿到了 qty_coins。

        永续没有这个问题（size 就是张数）。等 /spot 调研补齐后由现货路径调用。
        """
        if price <= 0:
            raise ValueError("现货市价买单需要一个正的参考价来换算 U 数量")
        return qty_coins * price

    # ---- 交易 -----------------------------------------------------------

    @staticmethod
    def _gate_text(client_order_id: str) -> str:
        """client order id → Gate 的 text 字段：必须 t- 前缀，前缀后 <=28 字节，
        字符集 [0-9A-Za-z_.-]。不合法就地报错，别让它到交易所才被拒。

        注意：text **不是**返佣码，把 channel id 塞这里一分钱返佣都拿不到。
        """
        if not client_order_id:
            raise ValueError("gate 下单必须带 client_order_id")
        body = client_order_id[2:] if client_order_id.startswith("t-") else client_order_id
        cleaned = re.sub(r"[^0-9A-Za-z_.\-]", "-", body)
        if len(cleaned.encode()) > 28:
            # 截断会破坏幂等（两个不同订单可能撞成同一个 text），必须由调用方缩短
            raise ValueError(f"gate text 前缀后最多 28 字节，收到 {len(cleaned.encode())}: {body}")
        return "t-" + cleaned

    async def place_order(self, req: OrderRequest) -> OrderResult:
        meta = await self._meta_of(req.symbol)
        text = self._gate_text(req.client_order_id)

        contracts = self.coins_to_contracts(req.qty, meta)
        if contracts <= 0:
            # 取整后归零：这单本来就不该发。本地拒掉，省一次网络往返和一次限频配额。
            raise OrderRejected(self.venue.value, "PINCHED",
                                f"{req.symbol} {req.qty} 币不足一张({meta.quanto})")
        if meta.size_max > 0 and contracts > meta.size_max:
            raise OrderRejected(self.venue.value, "ORDER_SIZE_TOO_LARGE",
                                f"{contracts} > order_size_max {meta.size_max}")
        if contracts < meta.size_min:
            # order_size_min > 1 的合约上，只 floor 到整张还不够。本地先拦一道，
            # 别拿一次网络往返和限频配额去换一个 ERR_QUOTA_LOWER_LIMIT。
            raise OrderRejected(self.venue.value, "ERR_QUOTA_LOWER_LIMIT",
                                f"{contracts} < order_size_min {meta.size_min}")

        # size 带符号，没有 side 字段；买/多为正，卖/空为负
        size = int(contracts) if req.side == "buy" else -int(contracts)
        tif = (req.time_in_force or "IOC").lower()
        if req.price is None:
            # 市价 = price "0" + tif ioc/fok，没有 type 字段。
            # gtc/poc 配 price=0 会被直接拒，强制降级成 ioc。
            price_str = "0"
            if tif not in ("ioc", "fok"):
                tif = "ioc"
        else:
            price_str = _dec_str(self.round_price(req.price, meta.tick, req.side))

        body: dict[str, Any] = {
            "contract": req.symbol,
            "size": size,
            "price": price_str,
            "tif": tif,
            "text": text,
        }
        if req.reduce_only:
            body["reduce_only"] = True

        # x-gate-exptime：请求在网络上迟到就由服务端拒掉，而不是过期后照样成交。
        # 不进签名串，可以随便加。
        extra = {"x-gate-exptime": str(self.timestamp_ms() + ORDER_EXPTIME_TTL_MS)}

        try:
            data = await self._request(
                "POST", f"/futures/{self.settle}/orders", quota="trade",
                signed=True, body=body, extra_headers=extra,
            )
        except OrderRejected:
            raise
        except RateLimited:
            raise
        except ExchangeError as exc:
            # 网络超时/连接中断/5xx：订单**可能已经进了撮合**。
            # 当成失败返回会导致上层重发 → 双倍仓位。必须交给 resolve_unknown_order
            # 走"cancel 终结 → 查单 → 仓位差分"。
            if exc.code in _PRE_MATCH_LABELS:
                raise            # 鉴权/时间戳层挡下的，订单确定不存在
            if exc.code == "NETWORK" or exc.code == "BAD_JSON" or exc.retriable:
                raise OrderUnknown(self.venue.value, req.client_order_id) from exc
            raise
        except httpx.TransportError as exc:
            # TransportError 覆盖 Timeout / Network / Protocol / Proxy 全族，
            # 六家统一用它，别再逐个列（漏一个就是一次裸奔的双倍仓位）
            raise OrderUnknown(self.venue.value, req.client_order_id) from exc

        result = self._parse_order(data, meta)
        # 上层用它自己发出去的 id 对账，回传原值而不是加了 t- 的 gate text
        return OrderResult(
            client_order_id=req.client_order_id,
            exchange_order_id=result.exchange_order_id,
            status=result.status,
            filled_qty=result.filled_qty,
            avg_price=result.avg_price,
            fee=result.fee,
            fee_asset=result.fee_asset,
        )

    def _parse_order(self, o: dict, meta: _ContractMeta) -> OrderResult:
        size = _dec(o.get("size"))
        left = _dec(o.get("left"))
        filled_contracts = abs(size) - abs(left)
        status_raw = str(o.get("status") or "")
        finish_as = str(o.get("finish_as") or "")
        if status_raw == "finished":
            # ioc/fok 部成后也会 finished，那是终结态，用 canceled 表达
            # （filled_qty 里带着已成部分，上层按它做仓位差分）
            status = "filled" if abs(left) == 0 and filled_contracts > 0 else "canceled"
            if filled_contracts == 0 and finish_as in ("filled",):
                status = "filled"
        elif filled_contracts > 0:
            status = "partial"
        else:
            status = "new"
        return OrderResult(
            client_order_id=str(o.get("text") or ""),
            exchange_order_id=str(o.get("id") or ""),
            status=status,
            filled_qty=filled_contracts * meta.quanto,
            avg_price=_dec(o.get("fill_price")),
            # 订单对象只给费率(mkfr/tkfr)不给费用金额，maker/taker 各成交多少也不拆。
            # 精确手续费要查 /my_trades，这里不猜。
            fee=ZERO,
            fee_asset=self.settle.upper(),
        )

    async def cancel_order(self, symbol: str, client_order_id: str) -> None:
        # 订单在册期间以及完成后 60s 内可以用 text 代替 order_id 撤单；
        # 超过就只认数字 id 了，那时会拿到 ORDER_NOT_FOUND → OrderRejected，
        # 正是 resolve_unknown_order 想要的"这单已终结"信号。
        text = self._gate_text(client_order_id)
        try:
            await self._request("DELETE", f"/futures/{self.settle}/orders/{text}",
                                quota="risk", signed=True)
        except ExchangeError as exc:
            if exc.code in _ORDER_TERMINAL_LABELS:
                return          # 已终结 = 撤单的目的已经达到，继续去查成交量
            raise self._risk_path_error(exc) from exc

    def _risk_path_error(self, exc: ExchangeError) -> ExchangeError:
        """风控路径（撤单/查单）上的错误重分类。

        base._request 把传输层异常包成 ExchangeError('NETWORK')，而
        base.resolve_unknown_order 只认 (OrderRejected, httpx.HTTPError, RateLimited)
        —— 不转一手的话，网络抖动或 5xx 会让消解流程在最危险的时刻带着未捕获异常崩掉。
        可重试的挂到 RateLimited（code 保留）；不可重试的原样上抛，绝不降级成
        OrderRejected —— 那等于谎报"成交量为 0"。
        """
        if isinstance(exc, (OrderRejected, RateLimited)) or not exc.retriable:
            return exc
        return RateLimited(self.venue.value, exc.code, exc.message, retriable=True)

    async def query_order(self, symbol: str, client_order_id: str) -> OrderResult:
        meta = await self._meta_of(symbol)
        text = self._gate_text(client_order_id)
        # 已完成订单 10 分钟后会从查询接口消失，自己的成交必须落库，别指望回查
        try:
            data = await self._request("GET", f"/futures/{self.settle}/orders/{text}",
                                       quota="risk", signed=True)
        except ExchangeError as exc:
            raise self._risk_path_error(exc) from exc
        return self._parse_order(data, meta)

    async def fetch_positions(self, symbols: Sequence[str] | None = None
                              ) -> Sequence[Position]:
        rows = await self._request("GET", f"/futures/{self.settle}/positions",
                                   quota="risk", signed=True,
                                   params={"holding": "true"})
        wanted = set(symbols) if symbols else None
        await self._ensure_meta([r["contract"] for r in rows
                                 if wanted is None or r["contract"] in wanted])
        out: list[Position] = []
        for r in rows:
            sym = r.get("contract")
            if wanted is not None and sym not in wanted:
                continue
            meta = self._meta.get(sym)
            if meta is None:
                continue
            lev = _dec(r.get("leverage"))
            if lev == 0:
                # leverage "0" 表示全仓，真实倍数在 cross_leverage_limit 里
                lev = _dec(r.get("cross_leverage_limit"))
            liq = _dec(r.get("liq_price"))
            out.append(Position(
                market=self.market,
                symbol=sym,
                qty=_dec(r.get("size")) * meta.quanto,   # size 带符号，负=空
                entry_price=_dec(r.get("entry_price")),
                mark_price=_dec(r.get("mark_price")),
                liquidation_price=liq if liq > 0 else None,
                leverage=lev,
            ))
        return out

    async def fetch_open_orders(self, symbol: str) -> Sequence[OrderResult]:
        meta = await self._meta_of(symbol)
        rows = await self._request("GET", f"/futures/{self.settle}/orders",
                                   quota="risk", signed=True,
                                   params={"contract": symbol, "status": "open"})
        return [self._parse_order(o, meta) for o in rows]

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        # 【坑】路径含 positions 的 POST，参数走 **query string**，body 为空。
        # 按 JSON body 签名会得到 INVALID_SIGNATURE。
        if self.cross_margin:
            # 全仓：leverage 必须是 0，真实倍数放 cross_leverage_limit
            params = {"leverage": "0", "cross_leverage_limit": _dec_str(leverage)}
        else:
            params = {"leverage": _dec_str(leverage)}
        await self._request(
            "POST", f"/futures/{self.settle}/positions/{symbol}/leverage",
            quota="risk", signed=True, params=params,
        )

    async def fetch_leverage_tiers(self, symbol: str
                                   ) -> Sequence[tuple[Decimal, Decimal]]:
        rows = await self._request("GET", f"/futures/{self.settle}/risk_limit_tiers",
                                   quota="market", params={"contract": symbol})
        tiers: list[tuple[Decimal, Decimal]] = []
        for r in rows or []:
            # 调研文档只给了端点名没给字段名，几种拼法都兼容一下
            cap = _dec(r.get("risk_limit") or r.get("risk_limit_max")
                       or r.get("max_risk_limit"))
            lev = _dec(r.get("leverage_max") or r.get("max_leverage"))
            if cap > 0 and lev > 0:
                tiers.append((cap, lev))
        tiers.sort(key=lambda t: t[0])
        return tiers
