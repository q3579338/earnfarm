"""Bitget USDT-M 永续合约适配器（classic / v2 API）。

调研依据：docs/research/exchange-bitget.md（2026-08-01 对着线上公共 API 核过）。

本家的三条特性决定了这份实现的形状：

1. 返佣走 HTTP header ``X-CHANNEL-API-CODE``，而且**不参与签名**——增删改这个码
   永远不会让签名失效，所以它可以做成纯配置项；空串时整个 header 不发出去。
   （文档里那个 ``clientOid: "channel#123456"`` 是签名示例的占位值，不是返佣机制，
   别在订单 ID 上加前缀。）
2. 签名是 HMAC-SHA256 + Base64。GET 的 query 必须按 key 升序，并且签的是
   **未百分号编码**的原始串，URL 上带的却是编码后的形式。两者对纯 ASCII 符号
   恰好一样，一旦参数里出现 ``#``（clientOid 允许 ``#``）就会分叉，所以这里
   raw / encoded 两份分开生成。
3. 资金费周期不是统一 8 小时：733 个 U 本位合约里 4h 占 375 个、1h 占 4 个。
   适配器如实带上 ``funding_interval_s``，归一化交给上层。

符号约定：``CarryRate.rate`` 用**交易所原始约定**（正 = 多头付给空头），
Bitget 的 ``fundingRate`` 本来就是这个符号，原样写入，**不取反**。
（base.py 里 ``fetch_carry_rates`` 的 docstring 说要取负成"多头视角"，那是过时的说法，
models.py 的 CarryRate 文档和 scoring.py 的 ``short_rate - long_rate`` 才是当前口径。）
"""

from __future__ import annotations

import asyncio
import json
import random
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from typing import Any, AsyncIterator, Mapping, Sequence
from urllib.parse import quote

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

REST_BASE = "https://api.bitget.com"
WS_PUBLIC_URL = "wss://ws.bitget.com/v2/ws/public"

# REST 深度端点。fetch_book_top 和 fetch_book_depth **共用**这一条常量：
# 两处各写一份字符串，路径纠错时只会改好一半。
# TODO(调研缺口): 调研文档没单列 REST 深度端点（它引用的官方签名示例里那条
# /api/mix/v2/market/depth 是文档自身的路径笔误），这里沿用 fetch_book_top 当初按
# v2 惯例推定的名字。官方更可能叫 merge-depth；上线前对着 Get-Merge-Depth 核一次，
# 顺带确认 limit / precision 的取值枚举。
DEPTH_PATH = "/api/v2/mix/market/orderbook"

# limit 是**枚举档位**（文档：1 / 5 / 15 / 50 / max），不是任意整数，别传 20 这种数。
# 取 max 而不是 50：Bitget 限频按**请求次数**算（公共行情 20 次/秒/IP），与响应大小
# 无关，多要档位不额外花配额；而 BTCUSDT 这种 tick=0.1 的密集簿 50 档只覆盖约 1 个
# 基点，10bp 带宽会被截断——那正是"容量低估一两个数量级"这个病本身。
# 实测（2026-08-01 打真实端点）：limit 接受 1/5/15/50/100，**不接受 "max"**
# ——传 "max" 直接回 40020 Parameter error，深度接口整个失效。
# 传 200 会被静默截到 100，所以 100 就是上限。
# 另一个反直觉的实测结果：limit=1 返回的是 100 档而不是 1 档。
DEPTH_LIMIT = "100"

# 不做价格合并。precision=scale1/2/3 会把相邻价位归并成更粗的档，10bp 这种窄带宽下
# 一个粗档就可能横跨边界：统计结果既不是低估也不是高估，而是没法解释。scale0 = 原始档位。
DEPTH_PRECISION = "scale0"

# 一个 productType 对应一种保证金币。COIN-FUTURES 的保证金币是 base coin，
# 只能从合约规格里读，所以不在这张表里。
_MARGIN_COIN = {
    "USDT-FUTURES": "USDT",
    "USDC-FUTURES": "USDC",
    "SUSDT-FUTURES": "SUSDT",   # 模拟盘
    "SUSDC-FUTURES": "SUSDC",
}

# time_in_force → Bitget 的 force 枚举。文档强调**全小写**，
# 而 CCXT 在这个端点发的是大写 'GTC'/'FOK'/'IOC'——手写就按文档来。
_FORCE = {
    "IOC": "ioc",
    "FOK": "fok",
    "GTC": "gtc",
    "GTX": "post_only",
    "POST_ONLY": "post_only",
    "MAKER": "post_only",
}

# 订单状态。调研文档没给状态机枚举，这套取值来自 v2 订单接口的实际取值，
# TODO: 上线前对着 Get-Order-Details 文档补全（尤其是部分成交后撤单的终态）。
_ORDER_STATUS = {
    "init": "new",
    "new": "new",
    "live": "new",
    "partially_filled": "partial",
    "partial_fill": "partial",
    "filled": "filled",
    "full_fill": "filled",
    "canceled": "canceled",
    "cancelled": "canceled",
    "rejected": "rejected",
}

# 调研文档只给了两个业务码：'00000' 成功、'30017' broker id 不匹配（且那是
# 子账户 API key 的事，与返佣 header 无关）。没有官方错误码表，所以这里的分类
# 走「HTTP 状态 + 关键词」保守策略，宁可把不确定的判成不可重试。
# TODO: 拿到官方码表后换成精确映射。
_KNOWN_CODES: Mapping[str, bool] = {"30017": False}          # code -> retriable
_RATE_LIMIT_HINTS = ("too many", "rate limit", "frequency", "request limit", "limit exceed",
                     "请求过于频繁", "频率")
# 三张关键词表必须**语言口径一致**：_NOT_FOUND_HINTS 带中文而 _RETRIABLE_HINTS 只有
# 英文时，同一句「系统繁忙」英文能正确进 OrderUnknown、中文却落到别的分支
_RETRIABLE_HINTS = ("system busy", "service unavailable", "try again", "timeout",
                    "internal error", "系统繁忙", "稍后重试", "请重试", "超时",
                    "服务不可用", "内部错误")
_NOT_FOUND_HINTS = ("not exist", "does not exist", "not found", "no order", "已撤销", "不存在")
# **进撮合之前**就被挡下的确定性错误：订单一定不存在，可以安全按"被拒"处理。
# 与 gate 的 _PRE_MATCH_LABELS 同一用途；Bitget 没有官方错误码表，只能靠关键词。
# 认不出来的一律往未知侧倒（见 place_order），绝不发"可安全重发"的许可证。
_PRE_MATCH_HINTS = ("insufficient", "balance", "parameter", "param", "invalid", "sign",
                    "timestamp", "precision", "less than", "greater than", "not support",
                    "余额不足", "参数", "签名", "精度", "不支持", "超出")

# WS：文档建议单连接不超过 50 个 channel，实测单条 subscribe 消息 60 个 arg 也能全 ack。
WS_SUB_BATCH = 50
WS_PING_INTERVAL_S = 30.0        # 文档：30 秒发一次字符串 'ping'，2 分钟不发就被断开


def _dec(value: Any) -> Decimal:
    """交易所返回的一律是字符串，直接构造 Decimal，中途绝不经过 float。

    真收到 float 说明上游解析路径出了问题——炸掉比默默丢精度好。
    """
    if isinstance(value, float):
        raise TypeError(f"Bitget 返回值不该是 float: {value!r}")
    if isinstance(value, Decimal):
        return value
    return Decimal(value)


def _dec_or(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    """空字符串是 Bitget 的常见"还没有值"表示（如未结算的 totalFee）。"""
    if value is None or value == "":
        return default
    try:
        return _dec(value)
    except (InvalidOperation, ValueError):
        return default


def _dec_str(value: Decimal) -> str:
    """定点字符串。绝不能让 Decimal('1E-5') 以科学计数法进请求体。"""
    return format(value, "f")


def _to_str(value: Any) -> str:
    if isinstance(value, Decimal):
        return _dec_str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _query_strings(params: Mapping[str, Any]) -> tuple[str, str]:
    """返回 (用于签名的原始串, 用于 URL 的编码串)。

    两条硬要求：key 必须升序（文档 RSA 示例的注释写明），签名用未编码形式。
    对 BTCUSDT / USDT-FUTURES 这种纯 ASCII 参数两者相同，但 clientOid 允许 '#'，
    那时不分开就会既签错又把 query 截断成 fragment。
    """
    items = sorted((k, _to_str(v)) for k, v in params.items() if v is not None)
    raw = "&".join(f"{k}={v}" for k, v in items)
    encoded = "&".join(f"{k}={quote(v, safe='')}" for k, v in items)
    return raw, encoded


def _json_body(body: Mapping[str, Any]) -> str:
    """必须与 httpx 实际发出的字节完全一致——文档原文：签什么就发什么。

    httpx 0.28 的 encode_json 用 ``separators=(",", ":")`` + ``ensure_ascii=False``，
    这里保持同款参数。测试 test_signed_body_bytes_match_signature 会守住这个耦合：
    httpx 哪天改了序列化方式，那个测试立刻红。
    """
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def _chunks(items: Sequence[str], size: int) -> list[Sequence[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


class BitgetAdapter(ExchangeAdapter):
    """Bitget 永续合约（默认 USDT-FUTURES）。

    ``product_type`` 支持 USDT-FUTURES / USDC-FUTURES / COIN-FUTURES / SUSDT-FUTURES(模拟盘)。
    注意持仓模式（单向/双向）和保证金模式都是**按 productType 分别设置**的：
    U 本位设了单向，USDC 本位还是老样子。所以一个进程同时跑两条产品线时，
    必须给每条线各建一个 adapter 并各自调一次 :meth:`set_position_mode`。
    """

    venue = Venue.BITGET
    rest_base = REST_BASE

    def __init__(self, credential: Credential, *, broker_code: str = "",
                 client: httpx.AsyncClient | None = None,
                 product_type: str = "USDT-FUTURES",
                 margin_mode: str = "crossed",
                 position_mode: str = "one_way_mode",
                 paper: bool = False) -> None:
        super().__init__(credential, broker_code=broker_code.strip(), client=client)
        # 文档：大小写都收，但统一成大写（官方写法）
        self.product_type = product_type.upper()
        self.margin_mode = margin_mode
        # 下单构造依赖持仓模式。文档要求「读 /all-position 的 posMode 再分支，不要假设」，
        # 但 all-position 不返回零仓行，空账户读不出来，所以这里先用配置值兜底，
        # 每次拿到持仓就用真值覆盖。
        self._pos_mode = position_mode
        self._pos_mode_confirmed = False
        self.paper = paper
        self.market_key = f"{Venue.BITGET.value}:{MarketKind.PERP.value}"
        # 另外五家都对外暴露 .market，桥接层按这个名字取；别让 Bitget 这家 AttributeError
        self.market = self.market_key

        self._instruments: dict[str, Instrument] = {}
        self._raw_specs: dict[str, dict] = {}
        self._inst_lock = asyncio.Lock()
        # 文档实测：Bitget 会回一个 Binance 风格的余量 header，可用于自适应退避
        self.remaining_quota: int | None = None

    # ---- 签名 -----------------------------------------------------------

    def _prestring(self, ts: str, method: str, path: str, raw_query: str, body_str: str) -> str:
        """timestamp + METHOD + requestPath [+ '?' + queryString] + body。

        GET 不带 body。官方示例（逐字）：
          16273667805456GET/api/mix/v2/market/depth?limit=20&symbol=BTCUSDT
        """
        qs = f"?{raw_query}" if raw_query else ""
        return f"{ts}{method.upper()}{path}{qs}{body_str}"

    def _prepare(self, method: str, path: str, params: dict, body: dict,
                 signed: bool) -> tuple[dict[str, str], str, dict, dict]:
        method = method.upper()
        raw_query, url_query = _query_strings(params)
        url = f"{self.rest_base}{path}" + (f"?{url_query}" if url_query else "")
        # base._request 只在非 GET 且 body 非空时发送 body，签名口径必须一致
        body_str = _json_body(body) if body and method != "GET" else ""

        headers: dict[str, str] = {}
        if signed:
            ts = str(self.timestamp_ms())
            headers = {
                "ACCESS-KEY": self._cred.api_key,
                "ACCESS-SIGN": self._hmac_sha256_b64(
                    self._prestring(ts, method, path, raw_query, body_str)
                ),
                "ACCESS-TIMESTAMP": ts,
                "ACCESS-PASSPHRASE": self._cred.passphrase,
                "Content-Type": "application/json",
                "locale": "en-US",
            }
            # 返佣码：header 不参与签名，空串时**整个 header 不发**——
            # 发一个空值 header 是未经测试且毫无意义的
            if self.broker_code:
                headers["X-CHANNEL-API-CODE"] = self.broker_code
        if self.paper:
            headers["PAPTRADING"] = "1"
        # params 交给 URL 自己带（签名串和 URL 必须逐字节一致，不能让 httpx 重新编码）
        return headers, url, {}, body

    # ---- 响应解析 -------------------------------------------------------

    def _parse(self, resp: httpx.Response) -> Any:
        remain = resp.headers.get("X-MBX-USED-REMAIN-LIMIT")
        if remain is not None:
            try:
                self.remaining_quota = int(remain)
            except ValueError:
                pass

        try:
            # parse_float=Decimal：_dec 对 float 是硬绊线（抛 TypeError），这道闸
            # 保证正常路径不会踩上去，也不会有任何一个裸数字静默丢精度
            payload = json.loads(resp.text, parse_float=Decimal) if resp.text else None
        except ValueError:
            payload = None

        if payload is None or not isinstance(payload, dict):
            if resp.status_code == 429:
                raise RateLimited(self.venue.value, "HTTP_429", resp.text[:200], retriable=True)
            if resp.status_code < 400:
                # 2xx 但响应体读不出来（WAF/代理注入 HTML）：这单有没有进撮合完全不
                # 知道。以前退化成 HTTP_200 + retriable=False，被 place_order 降级成
                # OrderRejected（= 发了张"可安全重发"的许可证）。统一成 BAD_JSON。
                raise ExchangeError(self.venue.value, "BAD_JSON",
                                    resp.text[:200], retriable=True)
            raise ExchangeError(self.venue.value, f"HTTP_{resp.status_code}",
                                resp.text[:200], retriable=resp.status_code >= 500)

        code = str(payload.get("code", ""))
        if code == "00000":
            return payload.get("data")

        msg = str(payload.get("msg", ""))
        raise self._translate(code, msg, resp.status_code)

    def _translate(self, code: str, msg: str, status: int) -> ExchangeError:
        low = msg.lower()
        if status == 429 or any(h in low for h in _RATE_LIMIT_HINTS):
            return RateLimited(self.venue.value, code or f"HTTP_{status}", msg, retriable=True)
        if any(h in low for h in _NOT_FOUND_HINTS):
            # 订单/仓位不存在是**确定性**结论，上层靠它把 OrderUnknown 消解成"从未成交"
            return OrderRejected(self.venue.value, code, msg, retriable=False)
        if code in _KNOWN_CODES:
            return ExchangeError(self.venue.value, code, msg, retriable=_KNOWN_CODES[code])
        retriable = status >= 500 or any(h in low for h in _RETRIABLE_HINTS)
        return ExchangeError(self.venue.value, code or f"HTTP_{status}", msg, retriable=retriable)

    # ---- 产品线小工具 ---------------------------------------------------

    def _margin_coin(self, symbol: str) -> str:
        coin = _MARGIN_COIN.get(self.product_type)
        if coin:
            return coin
        # COIN-FUTURES：保证金币 = base coin（BTCUSD -> BTC）
        inst = self._instruments.get(symbol)
        if inst is None:
            raise ExchangeError(self.venue.value, "SPEC_MISSING",
                                f"{self.product_type} 需要先加载合约规格才能确定 {symbol} 的保证金币")
        return inst.base

    async def _instrument(self, symbol: str) -> Instrument:
        if symbol not in self._instruments:
            async with self._inst_lock:
                if symbol not in self._instruments:
                    await self.fetch_instruments()
        inst = self._instruments.get(symbol)
        if inst is None:
            raise ExchangeError(self.venue.value, "SPEC_MISSING",
                                f"{self.product_type} 里没有 {symbol} 这个合约")
        return inst

    # ---- 精度 -----------------------------------------------------------

    @staticmethod
    def _round_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
        if step <= 0:
            return value
        return ((value / step).to_integral_value(rounding=rounding) * step).quantize(step)

    def _cached_spec(self, symbol: str) -> Instrument:
        inst = self._instruments.get(symbol)
        if inst is None:
            raise ExchangeError(self.venue.value, "SPEC_MISSING",
                                f"{symbol} 的合约规格还没加载，先调 fetch_instruments()")
        return inst

    def quantize_qty(self, symbol: str, qty: Decimal) -> Decimal:
        """数量按 lot_size **向零取整**（ROUND_DOWN 在 Decimal 里就是朝零）。

        文档：size 必须是 sizeMultiplier 的整数倍、按 volumePlace 截断，
        并同时满足 minTradeNum 与 minTradeUSDT(5U)。float 在 1e-5 步长上
        会静默产生被拒的数量，所以全程 Decimal。
        """
        inst = self._cached_spec(symbol)
        out = self._round_step(abs(qty), inst.lot_size, ROUND_DOWN)
        return out if qty >= 0 else -out

    def quantize_price(self, symbol: str, price: Decimal, side: str) -> Decimal:
        """价格按 tick_size 规整，方向与 executor.cross_price 一致：买单向上、卖单向下。

        传进来的已经是穿价保护限价（自带 5~20 个 tick 的穿透），反向取整会把穿透
        往回削；在 tick 相对价格很粗的小币上那不是少赚一个 tick，而是 IOC 根本挂不上、
        这条腿永远补不平。
        tick = priceEndStep * 10^-pricePlace，priceEndStep 不恒为 1。
        """
        inst = self._cached_spec(symbol)
        rounding = ROUND_UP if side == "buy" else ROUND_DOWN
        return self._round_step(price, inst.tick_size, rounding)

    def min_qty(self, symbol: str) -> Decimal:
        return _dec_or(self._raw_specs.get(symbol, {}).get("minTradeNum"), Decimal("0"))

    # ---- 元数据 ---------------------------------------------------------

    async def fetch_server_time_ms(self) -> int:
        data = await self._request("GET", "/api/v2/public/time", quota="market")
        if isinstance(data, dict):
            return int(data.get("serverTime") or data.get("ts"))
        return int(data)

    async def fetch_instruments(self) -> Sequence[Instrument]:
        """一次调用返回该产品线全部合约（U 本位 733 个）。

        tick/lot/minNotional 都**不是直接字段**，必须自己推：
          tick_size   = priceEndStep * 10^(-pricePlace)
          lot_size    = sizeMultiplier
          min_notional= minTradeUSDT
        """
        rows = await self._request("GET", "/api/v2/mix/market/contracts",
                                   quota="market",
                                   params={"productType": self.product_type}) or []
        out: list[Instrument] = []
        for row in rows:
            if row.get("symbolStatus") != "normal":
                continue          # 下架/待上线的不要进候选池
            symbol = row["symbol"]
            place = int(row["pricePlace"])
            tick = _dec(row["priceEndStep"]) * (Decimal(10) ** -place)
            # fundInterval 是小时的十进制字符串（"8"/"4"/"1"）
            interval_s = int(_dec(row["fundInterval"]) * 3600)
            inst = Instrument(
                market=self.market_key,
                symbol=symbol,
                base=row.get("baseCoin") or symbol,
                quote=row.get("quoteCoin") or "",
                tick_size=tick,
                lot_size=_dec(row["sizeMultiplier"]),
                min_notional=_dec_or(row.get("minTradeUSDT"), Decimal("5")),
                # Bitget 的 size 直接以 base 币计价，没有"张"的概念
                contract_size=Decimal("1"),
                max_leverage=_dec_or(row.get("maxLever"), Decimal("1")),
                funding_interval_s=interval_s,
            )
            out.append(inst)
            self._instruments[symbol] = inst
            self._raw_specs[symbol] = row
        return out

    async def fetch_fee_schedule(self, symbols: Sequence[str]) -> Sequence[FeeSchedule]:
        """必须用签名的 /common/trade-rate 读**自己的** VIP 档。

        contracts 里的 makerFeeRate/takerFeeRate 是默认档（0.0002/0.0006），
        对 VIP 用户会系统性高估成本——而 carry 生意里手续费差常常比费率差还大。

        档位是**账户级**的（mix 业务一档吃全市场），所以只探一个符号再复用。
        返回的每条都带自己的 symbol；不传 symbols 时返回一条 symbol="" 的
        账户级档位，上层拿它兜底整个市场。

        BGB 抵扣：合约业务没有 BGB 抵扣开关可读（现货那边的折扣也不在这个端点上），
        trade-rate 返回的就是最终扣费率，不再打折。
        """
        probe = symbols[0] if symbols else "BTCUSDT"
        data = await self._request("GET", "/api/v2/common/trade-rate", quota="market",
                                   signed=True,
                                   params={"symbol": probe, "businessType": "mix"})
        data = data if isinstance(data, dict) else {}
        maker_raw, taker_raw = data.get("makerFeeRate"), data.get("takerFeeRate")
        # 缺字段时返回空，绝不补零——零手续费会把负期望的机会整片翻成正期望
        if maker_raw in (None, "") or taker_raw in (None, ""):
            return []
        maker, taker = _dec(maker_raw), _dec(taker_raw)
        return [FeeSchedule(market=self.market_key, symbol=sym, maker=maker, taker=taker)
                for sym in (list(symbols) or [""])]

    async def fetch_funding_limits(self, symbol: str) -> tuple[Decimal, Decimal] | None:
        """返回 (cap, floor)。

        current-fund-rate 直接带 minFundingRate / maxFundingRate。这两个值是
        **每周期**的夹子，所以 4h 合约的 cap 年化后是 8h 同数值的两倍。
        """
        rows = await self._request("GET", "/api/v2/mix/market/current-fund-rate",
                                   quota="market",
                                   params={"symbol": symbol,
                                           "productType": self.product_type}) or []
        for row in rows:
            if row.get("symbol") != symbol:
                continue
            cap, floor = row.get("maxFundingRate"), row.get("minFundingRate")
            if cap in (None, "") or floor in (None, ""):
                return None
            return _dec(cap), _dec(floor)
        return None

    # ---- 行情 -----------------------------------------------------------

    async def fetch_book_top(self, symbol: str) -> BookTop:
        """REST 一档盘口。

        TODO(调研缺口): 调研文档只覆盖了 WS 的 books1 频道，REST 深度端点没有单列
        （签名示例里那个 /api/mix/v2/market/depth 是官方文档自身的路径笔误）。
        这里按 v2 命名惯例用 DEPTH_PATH，返回体结构与 books1 一致。
        上线前需对着官方 Get-Merge-Depth 文档核一次路径和 limit 取值。
        """
        data = await self._request("GET", DEPTH_PATH, quota="market",
                                   params={"symbol": symbol,
                                           "productType": self.product_type,
                                           "limit": 1})
        bids, asks = data.get("bids") or [], data.get("asks") or []
        if not bids or not asks:
            raise ExchangeError(self.venue.value, "EMPTY_BOOK", f"{symbol} 盘口为空")
        return BookTop(
            bid_price=_dec(bids[0][0]), bid_qty=_dec(bids[0][1]),
            ask_price=_dec(asks[0][0]), ask_qty=_dec(asks[0][1]),
            ts_ms=int(data.get("ts") or self.timestamp_ms()),
        )

    def _size_multiplier(self, symbol: str) -> Decimal:
        """盘口 size → base 币数量的换算系数。

        Bitget mix 的 size **本来就是 base 币数量**（下单 size 同一口径，
        contracts 里也没有面值字段），整条产品线 contract_size 恒为 1，没有"张"。
        这里仍然乘一次，是为了万一以后上了张制合约不至于静默把张数当币数——
        BTC 上那是一万倍的量级错误。

        但绝不为这个系数额外拉一次 /contracts（733 个合约的大响应）：深度是热路径，
        多一个 RTT 换一个恒为 1 的系数不划算。没缓存就按 1 走，这也正是当前 API 的真实口径。
        """
        inst = self._instruments.get(symbol)
        return inst.contract_size if inst is not None else Decimal("1")

    @staticmethod
    def _band_side(levels: Sequence[Sequence[str]], mult: Decimal,
                   lo: Decimal, hi: Decimal, descending: bool) -> tuple[Decimal, int]:
        """一侧盘口落在 [lo, hi] 内的累计名义额和档数。

        簿是排好序的（bids 降序、asks 升序），越过**远端**边界可以直接停——后面只会更远。
        越过**近端**边界（正常簿不会有，只有交叉簿/陈旧快照才会）只跳过这一档不能停：
        再往后可能还有落在带内的档位。

        量为 0 的档位是噪声，不计名义额也不计进 levels_used——
        否则"档位够不够覆盖带宽"这个判断会被灌水。
        """
        notional = Decimal("0")
        used = 0
        for level in levels:
            if len(level) < 2:
                continue
            price, size = _dec(level[0]), _dec(level[1])
            if price <= 0 or size <= 0:
                continue
            if price < lo:
                if descending:
                    break
                continue
            if price > hi:
                if descending:
                    continue
                break
            notional += price * size * mult
            used += 1
        return notional, used

    async def fetch_book_depth(self, symbol: str, band_bp: float = 10.0) -> BookDepth:
        """band_bp 基点带宽内的可见深度（名义额，quote 计价）。

        以中价为基准，买卖两侧各统计 [mid*(1-band), mid] 和 [mid, mid*(1+band)]
        内的累计名义额。

        限频权重：Bitget **没有币安那种按档数加权的权重制**——一次深度和一次
        books1 各算一个请求，公共行情统一 20 次/秒/IP（另有全局 6000 次/分钟/IP）。
        贵的是带宽不是配额：limit=max 的响应体比 limit=1 大两三个数量级，而且本方法
        和 fetch_book_top 打的是**同一个端点**，深度轮询直接吃一档轮询的额度。
        所以走 quota="market" 与行情共池，绝不能挪去 risk/trade 池抢风控配额。

        档位不够时**不外推**：limit 是枚举档位（见 DEPTH_LIMIT），密集簿可能几十档
        才覆盖一个基点。这时返回的是"至少这么多"，levels_used 如实反映统计了几档
        （买卖两侧之和）——它等于交易所实际返回的档数时，说明簿在端点档数上限处就被
        截断、带宽很可能没盖满。宁可低估，也不编。
        """
        if band_bp <= 0:
            # 带宽为零 ⇒ 深度恒为 0 ⇒ 上层把能做的机会判成 unexecutable，
            # 静默返回 0 比抛异常危险得多
            raise ValueError(f"band_bp 必须为正，收到 {band_bp!r}")

        data = await self._request("GET", DEPTH_PATH, quota="market",
                                   params={"symbol": symbol,
                                           "productType": self.product_type,
                                           "precision": DEPTH_PRECISION,
                                           "limit": DEPTH_LIMIT})
        data = data or {}
        bids, asks = data.get("bids") or [], data.get("asks") or []
        if not bids or not asks:
            # 单边空簿算不出中价，这一拍不可用。与 fetch_book_top 同一口径。
            raise ExchangeError(self.venue.value, "EMPTY_BOOK", f"{symbol} 盘口为空")

        mult = self._size_multiplier(symbol)
        best_bid, best_ask = _dec(bids[0][0]), _dec(asks[0][0])
        mid = (best_bid + best_ask) / 2
        # band_bp 对外是 float（scoring 那边就是 float 口径），只在这一处经 str 转 Decimal，
        # 之后全程 Decimal —— 直接 Decimal(12.5) 会带一串二进制尾巴进价格比较
        band = Decimal(str(band_bp)) / Decimal("10000")
        lo, hi = mid * (1 - band), mid * (1 + band)

        bid_notional, bid_levels = self._band_side(bids, mult, lo, mid, True)
        ask_notional, ask_levels = self._band_side(asks, mult, mid, hi, False)

        return BookDepth(
            symbol=symbol,
            bid_notional=bid_notional,
            ask_notional=ask_notional,
            bid_price=best_bid,
            # 一档量对外统一是 base 币，和名义额一样过换算系数
            bid_qty=_dec(bids[0][1]) * mult,
            ask_price=best_ask,
            ask_qty=_dec(asks[0][1]) * mult,
            levels_used=bid_levels + ask_levels,
            band_bp=band_bp,
            ts_ms=int(data.get("ts") or self.timestamp_ms()),
        )

    async def fetch_carry_rates(self, symbols: Sequence[str] | None = None) -> Sequence[CarryRate]:
        """省略 symbol 就是整条产品线一次拉完（733 个符号一个请求）。

        ``fundingRate`` 原样写入：正 = 多头付给空头，**不取反**。
        ``fundingRateInterval`` 是小时数，必须带出去——Bitget 有 51% 的 U 本位是 4h，
        拿 4h 的原始费率去和 8h 的比较，结论直接错一倍。
        """
        rows = await self._request("GET", "/api/v2/mix/market/current-fund-rate",
                                   quota="market",
                                   params={"productType": self.product_type}) or []
        wanted = set(symbols) if symbols else None
        now = self.timestamp_ms()
        out: list[CarryRate] = []
        for row in rows:
            symbol = row["symbol"]
            if wanted is not None and symbol not in wanted:
                continue
            out.append(CarryRate(
                market=self.market_key,
                symbol=symbol,
                rate=_dec(row["fundingRate"]),
                interval_s=int(_dec_or(row.get("fundingRateInterval"), Decimal("8")) * 3600),
                next_settle_ms=int(_dec_or(row.get("nextUpdate"), Decimal(now))),
                ts_ms=now,
            ))
        return out

    async def fetch_funding_history(self, symbol: str, since_ms: int,
                                    limit: int = 1000) -> Sequence[tuple[int, Decimal]]:
        """历史资金费，升序返回。

        两个硬约束（实测出来的，文档没写）：
          - 没有 startTime/endTime，只能按 pageNo 翻页；
          - 总量硬顶 270 条（pageNo 1/2/3 各 100/100/70，第 4 页起空）。
            8h 合约刚好 90 天，4h 只有 ~45 天，1h 只有 ~11 天。
            要更长的历史必须自己从第一天起持久化快照。

        **必须按结算时刻去重。** pageNo 分页没有时间锚：翻页途中跨过一次结算，
        整个列表就下移一位，原 page1 的第 100 条会变成 page2 的第 1 条。
        全市场回填要跑上百秒，跨结算边界是必然事件，不是边角情况。
        重复进去的后果是 autocorr() 被一对相邻相同值抬高、median 轻微偏移——
        都不报错，只是稳定性评分悄悄偏乐观。HTX 同样是 page_index 分页，
        那边用的就是字典去重，这里是漏写。
        """
        collected: dict[int, Decimal] = {}
        page = 1
        while len(collected) < limit and page <= 4:
            rows = await self._request("GET", "/api/v2/mix/market/history-fund-rate",
                                       quota="market",
                                       params={"symbol": symbol,
                                               "productType": self.product_type,
                                               "pageSize": 100, "pageNo": page}) or []
            if not rows:
                break
            oldest_ms = None
            for row in rows:
                ts = int(row["fundingTime"])
                oldest_ms = ts if oldest_ms is None else min(oldest_ms, ts)
                if ts >= since_ms:
                    collected[ts] = _dec(row["fundingRate"])
            # 返回是新→旧，本页最老一条已经早于 since 就不用再翻了
            if oldest_ms is not None and oldest_ms < since_ms:
                break
            if len(rows) < 100:
                break
            page += 1
        out = sorted(collected.items())
        return out[-limit:] if limit else out

    def stream_book_top(self, symbols: Sequence[str]) -> AsyncIterator[BookTop]:
        return self._book_stream(tuple(symbols))

    async def _book_stream(self, symbols: Sequence[str]) -> AsyncIterator[BookTop]:
        """books1 频道：10ms 推一次、每次都是全量快照，正好喂 500ms 收敛循环。

        心跳是**裸文本帧** 'ping'/'pong'，不是 JSON 也不是协议级 ping——
        直接 json.loads 会抛异常，所以读循环必须先特判字符串。
        同时关掉 websockets 自带的协议 ping（Bitget 不认），改用文本心跳。

        TODO: 最小可用版本，还缺三件事——
          1. symbols 超过 50 个时要分片到多条连接（文档建议单连接 <=50 channel，
             硬上限 1000，IP 上限 100 条连接）；这里只用一条连接。
          2. ``seq`` 单调递增可用于检测丢包/乱序，目前只记录不做补偿重订阅。
          3. BookTop 模型里没有 symbol 字段，多符号订阅时下游无法区分来源，
             实际使用建议一个符号一条流。
        """
        import websockets                      # 延迟导入：不跑 WS 的场景不该被这个依赖卡住

        backoff = 1.0
        last_seq: dict[str, int] = {}
        while True:
            try:
                async with websockets.connect(WS_PUBLIC_URL, ping_interval=None) as ws:
                    for chunk in _chunks(symbols, WS_SUB_BATCH):
                        await ws.send(json.dumps({
                            "op": "subscribe",
                            "args": [{"instType": self.product_type,
                                      "channel": "books1",
                                      "instId": s} for s in chunk],
                        }))
                    pinger = asyncio.create_task(self._ws_heartbeat(ws))
                    backoff = 1.0
                    try:
                        async for raw in ws:
                            if isinstance(raw, bytes):
                                raw = raw.decode()
                            if raw == "pong":
                                continue
                            if raw == "ping":
                                await ws.send("pong")
                                continue
                            msg = json.loads(raw, parse_float=Decimal)
                            if msg.get("event"):
                                continue      # subscribe ack / error，TODO: 把 error 抛给上层
                            arg = msg.get("arg") or {}
                            inst_id = arg.get("instId", "")
                            for item in msg.get("data") or []:
                                seq = int(item.get("seq") or 0)
                                if seq and seq < last_seq.get(inst_id, 0):
                                    continue  # 乱序包直接丢
                                last_seq[inst_id] = seq
                                bids, asks = item.get("bids") or [], item.get("asks") or []
                                if not bids or not asks:
                                    continue
                                yield BookTop(
                                    bid_price=_dec(bids[0][0]), bid_qty=_dec(bids[0][1]),
                                    ask_price=_dec(asks[0][0]), ask_qty=_dec(asks[0][1]),
                                    ts_ms=int(item.get("ts") or self.timestamp_ms()),
                                )
                    finally:
                        pinger.cancel()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 重连带抖动：一堆连接同时掉线时不要踩着同一秒集体回来
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, 30.0)

    async def _ws_heartbeat(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(WS_PING_INTERVAL_S)
            await ws.send("ping")     # 每个 ping 也占 10 条/秒的消息预算

    # ---- 交易 -----------------------------------------------------------

    async def place_order(self, req: OrderRequest) -> OrderResult:
        if not req.client_order_id:
            raise ValueError("Bitget 下单必须带 client_order_id：单向模式的 reduce-only "
                             "订单可能返回没有 orderId 的响应，clientOid 是唯一可查的抓手")

        inst = await self._instrument(req.symbol)
        qty = self.quantize_qty(req.symbol, req.qty)
        if qty <= 0:
            raise OrderRejected(self.venue.value, "PRECISION",
                                f"{req.symbol} 数量 {req.qty} 按 lot {inst.lot_size} 取整后为零")
        floor_qty = self.min_qty(req.symbol)
        if floor_qty > 0 and qty < floor_qty:
            # 本地先拦一道：交易所同样会拒，但白跑一个下单配额还多等一个 RTT
            raise OrderRejected(self.venue.value, "MIN_QTY",
                                f"{req.symbol} 数量 {qty} 低于 minTradeNum {floor_qty}")

        body: dict[str, Any] = {
            "symbol": req.symbol,
            "productType": self.product_type,
            "marginMode": self.margin_mode,
            "marginCoin": self._margin_coin(req.symbol),
            "size": _dec_str(qty),
            "side": req.side,
            "orderType": "limit" if req.price is not None else "market",
            "clientOid": req.client_order_id,
        }
        if req.price is not None:
            body["price"] = _dec_str(self.quantize_price(req.symbol, req.price, req.side))
            # force 只在 limit 下必填，且全小写
            body["force"] = _FORCE.get(req.time_in_force.upper(), "gtc")

        if self._pos_mode == "hedge_mode":
            # 双向模式下 side 表示的是**仓位方向**而不是下单方向：
            # 平多 = side=buy + tradeSide=close。这是把仓位做成两倍的头号原因。
            # 且 reduceOnly 在双向模式下不适用，绝不能带。
            body["side"], body["tradeSide"] = self._hedge_side(req.side, req.reduce_only)
        else:
            body["reduceOnly"] = "YES" if req.reduce_only else "NO"

        headers = {"X-CHANNEL-API-CODE": self.broker_code} if self.broker_code else None

        try:
            data = await self._request("POST", "/api/v2/mix/order/place-order",
                                       quota="trade", signed=True, body=body,
                                       extra_headers=headers)
        except httpx.TransportError as exc:
            # 超时 != 失败。重发会变成双倍仓位，必须交给
            # 「cancel 终结 → 查单 → 仓位差分」的消解流程。
            # TransportError 覆盖 Timeout / Network / Protocol / Proxy 全族，六家统一
            raise OrderUnknown(self.venue.value, req.client_order_id) from exc
        except RateLimited:
            raise                     # 被限频 = 明确没进撮合，安全重试
        except OrderRejected:
            raise
        except ExchangeError as exc:
            if exc.retriable:
                # base._request 把连接层异常包成了 ExchangeError('NETWORK')；5xx 和
                # "system busy" 同样说明请求可能已到达撮合但响应丢了。
                # 在下单这条路上，任何"可重试"都必须先按未知处理再消解，
                # 直接重发就是双倍仓位。（明确的限频已在上一个分支单独放行。）
                raise OrderUnknown(self.venue.value, req.client_order_id) from exc
            if any(h in exc.message.lower() for h in _PRE_MATCH_HINTS):
                # 鉴权/参数/余额这类在进撮合之前就被挡下的错误 = 订单确定不存在
                raise OrderRejected(self.venue.value, exc.code, exc.message,
                                    retriable=False) from exc
            # 关键词一个都不认识（Bitget 没有官方错误码表，新码随时冒出来）：
            # 不知道这单有没有进撮合。**必须往未知侧倒** —— 多跑一轮 cancel+查单
            # 只花一次 RTT，误判成"可安全重发"的 OrderRejected 是双倍仓位。
            raise OrderUnknown(self.venue.value, req.client_order_id) from exc

        data = data or {}
        return OrderResult(
            client_order_id=data.get("clientOid") or req.client_order_id,
            # 单向模式下的 reduce-only 撤改场景，响应可能**没有 orderId**，
            # 文档明说这时只能靠 clientOid 查——所以状态机绝不能只认 orderId
            exchange_order_id=data.get("orderId") or "",
            status="new",
            filled_qty=Decimal("0"),
            avg_price=Decimal("0"),
        )

    @staticmethod
    def _hedge_side(side: str, reduce_only: bool) -> tuple[str, str]:
        """双向模式的 (side, tradeSide)。文档原文：平多 side=buy、平空 side=sell。

        所以"卖出以减少多头"要写成 side=buy + tradeSide=close，方向是反的。
        """
        if not reduce_only:
            return side, "open"
        return ("buy" if side == "sell" else "sell"), "close"

    async def cancel_order(self, symbol: str, client_order_id: str) -> None:
        """状态终结操作：执行后订单只可能是已撤或已成，是消解 OrderUnknown 的第一步。"""
        body = {"symbol": symbol, "productType": self.product_type,
                "clientOid": client_order_id}
        try:
            # TODO(调研缺口): 调研文档没有单列撤单端点，路径按 v2 命名惯例推定，
            # 上线前需对着官方 Cancel-Order 文档核对（尤其 marginCoin 是否必填）。
            await self._request("POST", "/api/v2/mix/order/cancel-order",
                                quota="risk", signed=True, body=body)
        except ExchangeError as exc:
            raise self._risk_path_error(exc) from exc

    async def query_order(self, symbol: str, client_order_id: str) -> OrderResult:
        try:
            # TODO(调研缺口): 同上，订单详情端点未被调研文档覆盖。
            data = await self._request("GET", "/api/v2/mix/order/detail",
                                       quota="risk", signed=True,
                                       params={"symbol": symbol,
                                               "productType": self.product_type,
                                               "clientOid": client_order_id})
        except ExchangeError as exc:
            raise self._risk_path_error(exc) from exc
        if not data:
            raise OrderRejected(self.venue.value, "NOT_FOUND",
                                f"{symbol} 查无此单 {client_order_id}")
        return self._to_order_result(data)

    def _risk_path_error(self, exc: ExchangeError) -> ExchangeError:
        """风控路径上的错误重分类。

        base.resolve_unknown_order 只对 ``(httpx.HTTPError, RateLimited)`` 做退避重试，
        而 base._request 把连接层异常包成了普通 ExchangeError('NETWORK')——直接抛出去
        会让消解流程在最危险的状态下崩掉。所以这里把可重试的传输层错误改挂到
        RateLimited 上（code 字段保留 NETWORK，日志里仍能看出真实原因）。

        不可重试的**原样上抛**：降级成 OrderRejected 会让 resolve_unknown_order
        直接返回 Decimal("0")，把一张可能已成交的单谎报成"从未被接受"。
        宁可让上层看到一个异常，也不能发这种谎报。
        """
        if isinstance(exc, (OrderRejected, RateLimited)) or not exc.retriable:
            return exc
        return RateLimited(self.venue.value, exc.code, exc.message, retriable=True)

    def _to_order_result(self, row: Mapping[str, Any]) -> OrderResult:
        # TODO(调研缺口): 字段名取自 v2 订单接口实际返回，调研文档未列出，
        # 因此这里对成交量/均价做多字段兜底。
        filled = _dec_or(row.get("baseVolume") or row.get("accBaseVolume")
                         or row.get("filledQty"))
        return OrderResult(
            client_order_id=row.get("clientOid") or "",
            exchange_order_id=row.get("orderId") or "",
            status=_ORDER_STATUS.get(str(row.get("state", "")).lower(), "new"),
            filled_qty=filled,
            avg_price=_dec_or(row.get("priceAvg") or row.get("fillPrice")),
            fee=abs(_dec_or(row.get("fee"))),
            fee_asset=row.get("feeCoin") or "",
        )

    async def fetch_positions(self, symbols: Sequence[str] | None = None) -> Sequence[Position]:
        """持仓是真值。注意 all-position **不返回零仓行**——某条腿消失就是已平，不是错误。"""
        params: dict[str, Any] = {"productType": self.product_type}
        coin = _MARGIN_COIN.get(self.product_type)
        if coin:
            params["marginCoin"] = coin
        rows = await self._request("GET", "/api/v2/mix/position/all-position",
                                   quota="risk", signed=True, params=params) or []
        wanted = set(symbols) if symbols else None
        out: list[Position] = []
        for row in rows:
            symbol = row["symbol"]
            # 顺手把真实持仓模式记下来：下单构造要靠它分支，配置值只是兜底
            if row.get("posMode"):
                self._pos_mode = row["posMode"]
                self._pos_mode_confirmed = True
            if wanted is not None and symbol not in wanted:
                continue
            qty = _dec_or(row.get("total"))
            if row.get("holdSide") == "short":
                qty = -qty
            liq = row.get("liquidationPrice")
            out.append(Position(
                market=self.market_key,
                symbol=symbol,
                qty=qty,
                entry_price=_dec_or(row.get("openPriceAvg")),
                mark_price=_dec_or(row.get("markPrice")),
                liquidation_price=_dec(liq) if liq not in (None, "", "0") else None,
                leverage=_dec_or(row.get("leverage"), Decimal("1")),
            ))
        return out

    async def fetch_open_orders(self, symbol: str) -> Sequence[OrderResult]:
        """未成交挂单。判断两腿平衡必须计入这些，否则"A 已成交、B 还挂着"就是隐形单腿。

        另一个等价口径是 all-position 的 ``openDelegateSize``（挂单未成交量），
        双向模式下部分平仓会被已有挂单"占住"额度，对账要用 total - openDelegateSize。
        """
        # TODO(调研缺口): 挂单列表端点未被调研文档覆盖，路径按 v2 惯例推定。
        data = await self._request("GET", "/api/v2/mix/order/orders-pending",
                                   quota="risk", signed=True,
                                   params={"symbol": symbol,
                                           "productType": self.product_type})
        rows = (data or {}).get("entrustedList") if isinstance(data, dict) else data
        return [self._to_order_result(r) for r in (rows or [])]

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        """全仓模式**只能**发 leverage 字段。

        文档警告原文：全仓下传 longLeverage/shortLeverage 不会报错但会照样生效，
        且 longLeverage 优先——等于静默把你配错。
        """
        body: dict[str, Any] = {
            "symbol": symbol,
            "productType": self.product_type,
            "marginCoin": self._margin_coin(symbol),
            "leverage": _dec_str(leverage),
        }
        # TODO: 逐仓 + 双向模式需要额外带 holdSide（或同时给 long/shortLeverage），
        # 当前实现只覆盖全仓与单向逐仓这两种 carry 场景。
        # 降杠杆是风控动作（safety.liq_distance_deleverage 会触发），走 risk 配额
        await self._request("POST", "/api/v2/mix/account/set-leverage",
                            quota="risk", signed=True, body=body)

    async def fetch_leverage_tiers(self, symbol: str) -> Sequence[tuple[Decimal, Decimal]]:
        """杠杆档位 [(名义上限, 最大杠杆), ...] 升序。

        TODO(调研缺口): 调研文档只说"上下界来自 contracts.minLever/maxLever"，
        没给分档端点。这里按 v2 惯例试 query-position-lever，失败就退化成
        单档（上限 +inf，杠杆取 contracts.maxLever）——退化后避档位边界的逻辑
        等于失效，上线前必须补上真实分档。
        """
        try:
            rows = await self._request("GET", "/api/v2/mix/market/query-position-lever",
                                       quota="market",
                                       params={"symbol": symbol,
                                               "productType": self.product_type}) or []
        except ExchangeError:
            rows = []
        tiers = [(_dec(r["endUnit"]), _dec(r["leverage"]))
                 for r in rows if r.get("endUnit") not in (None, "")]
        if tiers:
            return sorted(tiers, key=lambda t: t[0])
        inst = await self._instrument(symbol)
        return [(Decimal("Infinity"), inst.max_leverage)]

    # ---- 一次性账户设置 -------------------------------------------------

    async def set_position_mode(self, mode: str = "one_way_mode") -> None:
        """设置单向/双向持仓。

        这是**按 productType 分别生效**的：U 本位设成单向，USDC 本位仍是原样。
        同时跑两条产品线时必须各建一个 adapter 各调一次，否则 USDC 那条腿会用
        错误的 side 语义下单（双向模式下 side 表示仓位方向）。
        """
        await self._request("POST", "/api/v2/mix/account/set-position-mode",
                            quota="risk", signed=True,
                            body={"productType": self.product_type, "posMode": mode})
        self._pos_mode = mode
        self._pos_mode_confirmed = True

    async def set_margin_mode(self, symbol: str, mode: str = "crossed") -> None:
        await self._request("POST", "/api/v2/mix/account/set-margin-mode",
                            quota="risk", signed=True,
                            body={"symbol": symbol,
                                  "productType": self.product_type,
                                  "marginCoin": self._margin_coin(symbol),
                                  "marginMode": mode})
        self.margin_mode = mode

    @property
    def position_mode(self) -> str:
        return self._pos_mode


class BitgetSpotAdapter(BitgetAdapter):
    """现货 / 全仓杠杆——**未实现**。

    调研文档 (docs/research/exchange-bitget.md) 明确只覆盖 USDT-M 永续，现货侧
    仅提到「/api/v2/spot/public/symbols 的符号与 U 本位 1:1 相同」和
    「spot place-order 也支持返佣 header」两句。缺的是：
      - 现货交易对的精度字段名与推导方式（perp 那套 pricePlace/priceEndStep/
        sizeMultiplier 在现货接口里是另一组字段，不能照搬）；
      - 现货/杠杆下单请求体的完整字段与 force 枚举；
      - 全仓杠杆的借币利率端点、利息计息口径与还币流程
        （CarryKind.INTEREST 的符号归一化没有依据就没法做）；
      - 杠杆的"持仓"概念映射（现货没有 Position，要用借贷余额差分）。
    补齐上述四项调研后再实现，不要凭记忆写。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        raise NotImplementedError(
            "Bitget 现货/杠杆适配器未实现：调研文档只覆盖 U 本位永续，"
            "缺现货精度字段、现货下单体、杠杆借币利率端点与利息符号口径。"
        )
