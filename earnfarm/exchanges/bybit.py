"""Bybit v5 统一账户适配器。

实现范围：category=linear（USDT 本位永续）。现货/杠杆为什么没做，见 `__init__`。

Bybit 和别家最不一样的三处，全部体现在下面的代码里：

1. 返佣走 HTTP header `X-Referer`，**不是** orderLinkId 前缀（币安）也不是 body 字段（OKX）。
   而且它**不参与签名**——加不加返佣码都不会让签名失效。所以它只在下单类请求上注入，
   其余请求一概不带；orderLinkId 的 36 字符全部留给我们自己做幂等键，没有前缀冲突。
2. 待签串 = timestamp + apiKey + recvWindow + (queryString | rawJsonBody)，无分隔符。
   关键是签的必须是**真正上线的那串字节**：body 只序列化一次，签它、发它，
   绝不能把 dict 丢给 HTTP 客户端让它重新序列化（key 重排 = retCode 10004）。
3. fundingInterval 是**动态**的（2025-10-30 起的 Dynamic Settlement Frequency：
   费率打到上下限时自动切成 1 小时结算）。缓存超过几分钟就可能让年化算错 8 倍，
   所以这里的合约缓存 TTL 只有 60 秒，且 carry 的周期一律现查现用，绝不写死 28800。
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, AsyncIterator, Iterable, Mapping, Sequence
from urllib.parse import urlencode

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

_PATH_TIME = "/v5/market/time"
_PATH_INSTRUMENTS = "/v5/market/instruments-info"
_PATH_TICKERS = "/v5/market/tickers"
_PATH_ORDERBOOK = "/v5/market/orderbook"
_PATH_FUNDING_HISTORY = "/v5/market/funding/history"
_PATH_RISK_LIMIT = "/v5/market/risk-limit"
_PATH_FEE_RATE = "/v5/account/fee-rate"
_PATH_ORDER_CREATE = "/v5/order/create"
_PATH_ORDER_CANCEL = "/v5/order/cancel"
_PATH_ORDER_REALTIME = "/v5/order/realtime"
_PATH_ORDER_HISTORY = "/v5/order/history"
_PATH_POSITION_LIST = "/v5/position/list"
_PATH_SET_LEVERAGE = "/v5/position/set-leverage"

# 只有这三个端点会创建/改动订单，返佣 header 只往这里加
_REFERER_PATHS = frozenset({_PATH_ORDER_CREATE, "/v5/order/create-batch", "/v5/order/amend"})

MAINNET = "https://api.bybit.com"
TESTNET = "https://api-testnet.bybit.com"
WS_PUBLIC_LINEAR = "wss://stream.bybit.com/v5/public/linear"
WS_PUBLIC_LINEAR_TESTNET = "wss://stream-testnet.bybit.com/v5/public/linear"

# 公共 args 数组有 ~21000 字符的上限，大盘面要拆多条 subscribe
_WS_ARGS_CHARS = 20_000
# 应用层 JSON 心跳，20 秒一次。协议级 ping 帧不算数，漏了就被踢
_WS_PING_S = 20.0
# 合约规格缓存寿命。不是为了省请求，是为了跟上动态结算周期切换
_INSTRUMENT_TTL_S = 60.0
# 深度请求的档数。取 200 而不是上限 500：容量估算真正吃紧的是山寨币，
# 那边 tick 相对价格粗，200 档远远盖过 10bp；BTC 这种 tick 极细的确实盖不满
# （tickSize 0.1 @ 60000，200 档只有 ~3bp），但 BTC 的容量从来不是瓶颈。
# 盖不满时 levels_used 会顶到这个数，如实暴露截断，而不是外推补足。
_DEPTH_LIMIT = 200

# 撮合状态 → 引擎状态。Untriggered/Triggered 是条件单的中间态，仍算未终结
_ORDER_STATUS = {
    "New": "new",
    "Untriggered": "new",
    "Triggered": "new",
    "PartiallyFilled": "partial",
    "Filled": "filled",
    "Cancelled": "canceled",
    "PartiallyFilledCanceled": "canceled",
    "Deactivated": "canceled",
    "Rejected": "rejected",
}

# 明确的限频码。10006 是账户/接口级，10429 是 WS trade 的系统频控
_RATE_LIMIT_CODES = frozenset({"10006", "10018", "10429"})
# 可重试的服务端/时钟类错误。10002 是时间戳越界——重试前会被 _request 顺手重新校时
_RETRIABLE_CODES = frozenset({"10002", "10016"})
# 这些是**进撮合之前**就被挡下的（时间戳超窗），订单确定不存在，
# 不能因为 retriable=True 就升级成 OrderUnknown 白跑一轮 cancel+查单。
# 10016 不在这里：它是服务端错误，请求已经到了 Bybit，结果不明。
_PRE_MATCH_CODES = frozenset({"10002"})
# 订单不存在。base.resolve_unknown_order 靠 OrderRejected 判定「这单从没被接受」
_ORDER_NOT_FOUND_CODES = frozenset({"110001", "170213", "170140"})


def _dumps(obj: Any) -> str:
    """唯一的 JSON 序列化入口。

    分隔符必须和实际上线的字节一致，所以全流程只准用这一个函数——
    签名用它、发送也用它，中间不留第二种序列化方式。
    """
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _dec(value: Any, default: str = "0") -> Decimal:
    """交易所给的是字符串，直接进 Decimal，中途不碰 float。空串按默认值。"""
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


class BybitAdapter(ExchangeAdapter):
    """Bybit v5 linear 永续适配器。"""

    venue = Venue.BYBIT

    def __init__(self, credential: Credential, *, broker_code: str = "",
                 client: httpx.AsyncClient | None = None,
                 kind: MarketKind = MarketKind.PERP,
                 settle_coin: str = "USDT",
                 recv_window: int = 5000,
                 testnet: bool = False) -> None:
        if kind is not MarketKind.PERP:
            # 现货：调研文档给了 category=spot 的端点，但没记录 spot 的 lotSizeFilter
            # 字段名（basePrecision / minOrderAmt 之类），也没给现货持仓与均价的口径，
            # 精度靠猜必然 retCode 10001。
            # 杠杆：完全没有覆盖 /v5/spot-margin-trade/* 的借币利率端点，
            # 没有利率就给不出 CarryKind.INTEREST 的 carry，做出来也是假的。
            raise NotImplementedError(
                f"Bybit 适配器目前只做 {MarketKind.PERP.value}（category=linear）；"
                f"{kind.value} 缺调研依据：现货缺 lotSizeFilter 字段名与持仓口径，"
                "杠杆缺借币利率端点"
            )
        # 用户从网页复制返佣码常带尾随空白/换行，六家统一在构造期剥掉
        super().__init__(credential, broker_code=broker_code.strip(), client=client)
        self.rest_base = TESTNET if testnet else MAINNET
        self._ws_url = WS_PUBLIC_LINEAR_TESTNET if testnet else WS_PUBLIC_LINEAR
        self._category = "linear"
        self._settle_coin = settle_coin
        # recvWindow 参与签名，必须是字符串且与 header 同值
        self._recv_window = str(int(recv_window))
        self.market = f"{self.venue.value}:{kind.value}"
        self._instruments: dict[str, Instrument] = {}
        self._instruments_at = 0.0

    # ---- 签名与传输 -----------------------------------------------------

    def _referer_headers(self, path: str) -> dict[str, str]:
        """返佣 header。

        空字符串时**什么都不加**——不是发 `X-Referer: ""`。空值行为未经验证且毫无意义，
        调研文档明确要求宁可省略。因为它不参与签名，加不加都不影响 X-BAPI-SIGN。
        """
        if not self.broker_code or path not in _REFERER_PATHS:
            return {}
        return {"X-Referer": self.broker_code}

    def _prepare(self, method: str, path: str, params: dict, body: dict,
                 signed: bool) -> tuple[dict[str, str], str, dict, dict]:
        # None 值不上线，否则签名里的 queryString 和实际 URL 对不上
        send_params = {k: str(v) for k, v in params.items() if v is not None}
        send_body = {k: v for k, v in body.items() if v is not None}
        headers: dict[str, str] = {}
        if method != "GET" and send_body:
            headers["Content-Type"] = "application/json"

        if signed:
            ts = str(self.timestamp_ms())
            # GET 签 queryString（无前导 ?），POST 签 raw JSON body，两者都必须
            # 与真正上线的字节完全一致
            payload = urlencode(send_params) if method == "GET" else (
                _dumps(send_body) if send_body else ""
            )
            headers.update({
                "X-BAPI-API-KEY": self._cred.api_key,
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": self._recv_window,
                "X-BAPI-SIGN": self._hmac_sha256_hex(
                    ts + self._cred.api_key + self._recv_window + payload
                ),
                "X-BAPI-SIGN-TYPE": "2",
            })
        return headers, self.rest_base + path, send_params, send_body

    async def _request(self, method: str, path: str, *, quota: str = "market",
                       signed: bool = False, params: Mapping[str, Any] | None = None,
                       body: Mapping[str, Any] | None = None,
                       extra_headers: Mapping[str, str] | None = None) -> Any:
        """覆写基类版本，两处必要的差异：

        1. POST 用 `content=` 发**已序列化好的那串字节**。基类用 `json=`，
           等于让 httpx 重新序列化一遍，key 顺序一变签名就废（retCode 10004）。
        2. 网络异常保持 httpx 原生类型往上抛，不包成 ExchangeError。
           base.resolve_unknown_order 正是靠 `except (httpx.HTTPError, RateLimited)`
           做退避重试的，包掉之后消解流程会直接崩在 cancel 那一步。
        """
        # 外部喂数缓存（基类 _request 里那道拦截对覆写版不生效，必须自带）。
        # Bybit 服务端实测 403，机会榜那两发全靠访客浏览器喂进来
        cached = self._public_cache_hit(method, path, signed=signed,
                                        params=params, body=body)
        if cached is not None:
            return cached

        if self._clock.is_stale:
            try:
                await self.sync_clock()
            except Exception:
                pass          # 校时失败不阻塞业务，会在 clock_drift_ms 上体现

        async with self._quota.get(quota, self._quota["market"]):
            headers, url, send_params, send_body = self._prepare(
                method, path, dict(params or {}), dict(body or {}), signed
            )
            headers.update(self._referer_headers(path))
            if extra_headers:
                headers = {**headers, **extra_headers}
            content = _dumps(send_body).encode() if (method != "GET" and send_body) else None
            resp = await self._client.request(
                method, url, params=send_params or None, content=content, headers=headers,
            )
            return self._parse(resp)

    def _parse(self, resp: httpx.Response) -> Any:
        """返回**整个信封**（不只是 result）——盘口时效要用信封上的 `time`。"""
        # 错误码串统一用 HTTP_ 下划线（另外五家都是），否则任何跨所的日志过滤/
        # 告警规则都会漏掉 Bybit 这一家
        if resp.status_code == 403:
            # IP 级封禁，附带 10 分钟惩罚。无脑重试会把机器人打下线整整十分钟
            raise RateLimited(self.venue.value, "HTTP_403",
                              "IP 触发风控，通常伴随 10 分钟封禁", retriable=False)
        if resp.status_code >= 400:
            raise ExchangeError(self.venue.value, f"HTTP_{resp.status_code}",
                                resp.text[:200], retriable=resp.status_code >= 500)
        try:
            # parse_float=Decimal：v5 目前所有数值都是字符串，但这道闸不能靠上游
            # 习惯撑着——任何一个裸数字漏进来就是一次静默的精度损失
            data = json.loads(resp.text, parse_float=Decimal) if resp.text else {}
        except ValueError as exc:
            raise ExchangeError(self.venue.value, "BAD_JSON", str(exc), retriable=True) from exc

        code = str(data.get("retCode"))
        if code == "0":
            return data
        msg = str(data.get("retMsg", ""))
        if code in _RATE_LIMIT_CODES:
            raise RateLimited(self.venue.value, code, msg, retriable=True)
        if code in _RETRIABLE_CODES:
            raise ExchangeError(self.venue.value, code, msg, retriable=True)
        # 调研文档只逐条列了 10001/10002/10004/10006/10429/110043 这几个码。
        # 其余按前缀归类：1xxxxx（合约）和 17xxxx（现货）是业务侧拒单，
        # 10001 是参数/精度错——这些都不会成交，判成 OrderRejected 才能让上层安全重试。
        if code == "10001" or code.startswith("11") or code.startswith("17"):
            raise OrderRejected(self.venue.value, code, msg, retriable=False)
        raise ExchangeError(self.venue.value, code, msg, retriable=False)

    # ---- 精度 -----------------------------------------------------------

    @staticmethod
    def _floor_step(value: Decimal, step: Decimal) -> Decimal:
        """按步长向零取整。ROUND_DOWN 在 Decimal 里就是 truncate toward zero，
        负数（减仓方向）也不会被放大。"""
        if step <= 0:
            return value
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    @staticmethod
    def _fmt_step(value: Decimal, step: Decimal) -> str:
        """按步长字符串隐含的小数位数格式化。

        qtyStep="0.001" → 3 位 → "0.003"。绝不用 str(float)：Python 会吐出
        "1e-05" 或 "0.30000000000000004"，Bybit 两个都拒。
        """
        places = -step.as_tuple().exponent if step > 0 else 0
        places = max(int(places), 0)
        return f"{value.quantize(Decimal(1).scaleb(-places)):f}"

    def _round_price(self, price: Decimal, tick: Decimal, side: str) -> Decimal:
        """价格按 tick 规整。方向与 executor.cross_price 一致：买单向上、卖单向下。

        传进来的已经是穿价保护限价（自带 3~10bp 穿透），反向取整会把穿透削回来；
        在 tick 相对价格很粗的小币上那不是少赚一个 tick，而是 IOC 根本挂不上、
        这条腿永远补不平。
        """
        if tick <= 0:
            return price
        rounding = ROUND_UP if side == "buy" else ROUND_DOWN
        return (price / tick).to_integral_value(rounding=rounding) * tick

    # ---- 元数据 ---------------------------------------------------------

    async def fetch_server_time_ms(self) -> int:
        # 故意不走 _request：_request 在时钟陈旧时会调 sync_clock，
        # 而 sync_clock 又调这里，走 _request 就是无限递归
        async with self._quota["market"]:
            resp = await self._client.get(self.rest_base + _PATH_TIME)
        result = self._parse(resp).get("result") or {}
        nano = result.get("timeNano")
        if nano:
            return int(nano) // 1_000_000
        return int(Decimal(str(result.get("timeSecond", "0"))) * 1000)

    async def fetch_instruments(self) -> Sequence[Instrument]:
        out: list[Instrument] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"category": self._category, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            result = (await self._request("GET", _PATH_INSTRUMENTS,
                                          quota="market", params=params))["result"]
            for row in result.get("list") or []:
                inst = self._to_instrument(row)
                if inst is not None:
                    out.append(inst)
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
        self._instruments = {i.symbol: i for i in out}
        self._instruments_at = time.time()
        return tuple(out)

    def _to_instrument(self, row: Mapping[str, Any]) -> Instrument | None:
        # 只要永续且在交易中。PreLaunch 的符号会出现在列表里但下单必拒，
        # 交割合约（LinearFutures / BTCUSDT-21FEB25）根本没有资金费
        if row.get("contractType") != "LinearPerpetual" or row.get("status") != "Trading":
            return None
        lot = row.get("lotSizeFilter") or {}
        pf = row.get("priceFilter") or {}
        lev = row.get("leverageFilter") or {}
        # fundingInterval 单位是**分钟**，不是秒也不是小时
        interval_min = int(row.get("fundingInterval") or 480)
        return Instrument(
            market=self.market,
            symbol=str(row["symbol"]),
            base=str(row.get("baseCoin", "")),
            quote=str(row.get("quoteCoin", "")),
            tick_size=_dec(pf.get("tickSize")),
            lot_size=_dec(lot.get("qtyStep")),
            min_notional=_dec(lot.get("minNotionalValue")),
            # USDT 本位线性永续的 qty 直接就是 base 币数量，没有面值换算
            contract_size=Decimal("1"),
            max_leverage=_dec(lev.get("maxLeverage"), "1"),
            funding_interval_s=interval_min * 60,
        )

    async def _instrument_map(self) -> Mapping[str, Instrument]:
        """带 TTL 的合约缓存。TTL 短是因为 fundingInterval 会在 ~4 分钟内
        从 8h 切到 1h——缓存久了年化就会错 8 倍，而那恰恰是最想抓住的行情。"""
        if not self._instruments or time.time() - self._instruments_at > _INSTRUMENT_TTL_S:
            await self.fetch_instruments()
        return self._instruments

    async def _instrument(self, symbol: str) -> Instrument:
        inst = (await self._instrument_map()).get(symbol)
        if inst is None:
            raise ExchangeError(self.venue.value, "UNKNOWN_SYMBOL",
                                f"{symbol} 不在可交易的 linear 永续里", retriable=False)
        return inst

    async def fetch_fee_schedule(self, symbols: Sequence[str]) -> Sequence[FeeSchedule]:
        """账户实际费率（已含 VIP 档位与做市商减免），签名接口。

        Bybit 是**逐品种**给费率的（做市商可能只在某几个 symbol 上有减免），所以
        每条都带自己的 symbol，调用方按 symbol 匹配。查不到的符号直接不返回——
        补一条零费率占位比缺一条危险得多。

        不传 symbols 时一次拉整个 category 的表（官方推荐的启动动作），
        这也是最省配额的用法：一个请求换全市场的真实档位。

        Bybit 衍生品没有平台币抵扣这回事（没有 BNB/GT 那样的开关），
        这里返回的就是最终扣费率，无需也无从再打折。
        """
        params: dict[str, Any] = {"category": self._category}
        if len(symbols) == 1:
            params["symbol"] = symbols[0]
        result = (await self._request("GET", _PATH_FEE_RATE, quota="market",
                                      signed=True, params=params))["result"]
        rows = {str(r["symbol"]): r for r in (result.get("list") or [])}
        wanted = list(symbols) if symbols else list(rows)
        out: list[FeeSchedule] = []
        for sym in wanted:
            row = rows.get(sym)
            if row is None:
                continue
            maker_raw, taker_raw = row.get("makerFeeRate"), row.get("takerFeeRate")
            if maker_raw in (None, "") or taker_raw in (None, ""):
                continue
            out.append(FeeSchedule(market=self.market, symbol=sym,
                                   maker=_dec(maker_raw),
                                   taker=_dec(taker_raw)))
        return tuple(out)

    async def fetch_funding_limits(self, symbol: str) -> tuple[Decimal, Decimal] | None:
        """Bybit v5 没有任何端点暴露资金费的 cap/floor。

        instruments-info 里的 riskParameters 是**价格保护**参数（下单偏离限制），
        跟资金费上下限完全是两回事，拿它当 cap 会得到一个荒唐的数。
        调研文档也没记录任何替代来源，所以这里诚实地返回 None，
        让评分层不要施加 cap 约束，而不是编一个。
        TODO: 各合约的费率上下限只在帮助中心以文字形式给出，若要用需人工维护静态表。
        """
        return None

    # ---- 行情 -----------------------------------------------------------

    async def fetch_book_top(self, symbol: str) -> BookTop:
        data = await self._request("GET", _PATH_TICKERS, quota="market",
                                   params={"category": self._category, "symbol": symbol})
        rows = (data["result"].get("list") or [])
        if not rows:
            raise ExchangeError(self.venue.value, "NO_TICKER",
                                f"{symbol} 无行情", retriable=False)
        row = rows[0]
        return BookTop(
            bid_price=_dec(row.get("bid1Price")),
            bid_qty=_dec(row.get("bid1Size")),
            ask_price=_dec(row.get("ask1Price")),
            ask_qty=_dec(row.get("ask1Size")),
            # tickers 的结果体里没有逐符号时间戳，用信封上的服务器时间
            ts_ms=int(data.get("time") or self.timestamp_ms()),
        )

    async def fetch_book_depth(self, symbol: str, band_bp: float = 10.0) -> BookDepth:
        """band_bp 基点带宽内的可见深度（名义额，USDT 计价）。

        以中价为基准，买卖两侧各统计 [mid*(1-band), mid] 和 [mid, mid*(1+band)]
        内的累计名义额。一档量当深度代理会把容量低估一两个数量级——山寨币一档常常
        只有几百刀，10bp 内却有几万——那正是"真机会被判成做不了"的来源。

        端点：GET /v5/market/orderbook?category=linear&symbol=&limit=200（公开，免签）。

        限频权重：Bybit v5 公共行情**没有**币安那套 weight 体系，只有 IP 级
        600 请求 / 5 秒的总预算，所以这一发和一发 tickers 在记账上完全等价（各算 1 次）。
        真正的代价在响应体：400 个档位比 bookTicker 的 4 个数大两个数量级，
        贵的是带宽和解析时延，不是配额。因此它属于选币/容量估算路径，
        绝不要塞进 500ms 收敛循环。配额走 market 池（不能占用风控预留的 risk 池）。
        超预算 = retCode 10006；HTTP 403 是 IP 级封禁 10 分钟，别无脑重试。

        TODO: 调研文档只覆盖了 WS 的 orderbook 主题（档位梯度 1/50/200/500/1000、
        b/a 为 [price, size] 字符串对），没有单列 REST /v5/market/orderbook。
        这里的参数名与字段名按 v5 通用约定解析（category/symbol/limit → result.b/a/ts/cts），
        接实盘前需用一次真实响应核对。
        """
        if band_bp <= 0:
            # 带宽为 0 会让两侧深度恒等于 0，而 scoring 拿 0 深度当"无深度数据"处理，
            # 结果是容量约束被静默跳过——比报错危险得多
            raise ValueError(f"band_bp 必须为正: {band_bp}")

        data = await self._request("GET", _PATH_ORDERBOOK, quota="market",
                                   params={"category": self._category, "symbol": symbol,
                                           "limit": _DEPTH_LIMIT})
        result = data.get("result") or {}
        bids = result.get("b") or []
        asks = result.get("a") or []
        if not bids or not asks:
            # 单边空盘定不出中价，宁可报错也不能拿 last/mark 凑一个假基准
            raise ExchangeError(self.venue.value, "NO_BOOK",
                                f"{symbol} 盘口缺少买盘或卖盘，无法定中价", retriable=False)

        bid_price, bid_qty = _dec(bids[0][0]), _dec(bids[0][1])
        ask_price, ask_qty = _dec(asks[0][0]), _dec(asks[0][1])
        mid = (bid_price + ask_price) / 2
        # band_bp 是 float（签名如此），只在这里经 str 转成 Decimal，之后全程 Decimal
        band = Decimal(str(band_bp)) / Decimal("10000")
        bid_notional, n_bid = _sum_band(bids, mid * (1 - band), mid)
        ask_notional, n_ask = _sum_band(asks, mid, mid * (1 + band))

        return BookDepth(
            symbol=str(result.get("s") or symbol),
            bid_notional=bid_notional,
            ask_notional=ask_notional,
            bid_price=bid_price,
            bid_qty=bid_qty,
            ask_price=ask_price,
            ask_qty=ask_qty,
            # 取两侧较大者：只要有一侧顶到 _DEPTH_LIMIT 就说明带宽没盖满，
            # 调用方看 levels_used == _DEPTH_LIMIT 即知这个深度是下界。
            # 取和会把"单侧截断"稀释掉，正是最需要警觉的那种情况
            levels_used=max(n_bid, n_ask),
            band_bp=band_bp,
            # ts 是盘口生成时刻，cts 是撮合引擎时刻（文档：cts 更贴近真实行情时刻）
            ts_ms=int(result.get("cts") or result.get("ts")
                      or data.get("time") or self.timestamp_ms()),
        )

    async def fetch_carry_rates(self, symbols: Sequence[str] | None = None) -> Sequence[CarryRate]:
        """整个 linear 盘面一次 tickers 调用就够（symbol 可省略）。

        符号约定：**原样存交易所的符号**，正 = 多头付给空头。Bybit 的 fundingRate
        本来就是这个约定，取反反而错。上层统一用 short_rate - long_rate。
        """
        want = set(symbols) if symbols else None
        instruments = await self._instrument_map()
        data = await self._request("GET", _PATH_TICKERS, quota="market",
                                   params={"category": self._category})
        ts = int(data.get("time") or self.timestamp_ms())
        out: list[CarryRate] = []
        for row in data["result"].get("list") or []:
            sym = str(row.get("symbol", ""))
            if want is not None and sym not in want:
                continue
            rate_raw = row.get("fundingRate")
            if rate_raw in (None, ""):
                continue
            inst = instruments.get(sym)
            if inst is None:
                # 周期未知就宁可不给。硬套 8h 会在动态切换到 1h 时把年化算错 8 倍
                continue
            out.append(CarryRate(
                market=self.market,
                symbol=sym,
                rate=_dec(rate_raw),
                interval_s=inst.funding_interval_s,
                next_settle_ms=int(row.get("nextFundingTime") or 0),
                ts_ms=ts,
            ))
        return tuple(out)

    async def fetch_funding_history(self, symbol: str, since_ms: int,
                                    limit: int = 1000) -> Sequence[tuple[int, Decimal]]:
        """历史资金费，升序。

        分页只能**往回走**：文档明确「只传 startTime 会报错；只传 endTime 返回截至
        endTime 的 200 条」。所以拿本批最老的 fundingRateTimestamp 减 1ms 当下一个
        endTime，直到越过 since_ms 或返回不满一页。
        """
        collected: dict[int, Decimal] = {}
        end: int | None = None
        # 单页上限 200，留个页数硬闸防止游标不前进时死循环
        for _ in range(max(1, (limit + 199) // 200) + 5):
            params: dict[str, Any] = {"category": self._category, "symbol": symbol,
                                      "limit": 200}
            if end is not None:
                params["endTime"] = end
            rows = (await self._request("GET", _PATH_FUNDING_HISTORY, quota="market",
                                        params=params))["result"].get("list") or []
            if not rows:
                break
            oldest = None
            for row in rows:
                ts = int(row["fundingRateTimestamp"])
                collected[ts] = _dec(row.get("fundingRate"))
                oldest = ts if oldest is None else min(oldest, ts)
            if oldest is None or oldest <= since_ms or len(rows) < 200:
                break
            end = oldest - 1
        series = [(ts, rate) for ts, rate in sorted(collected.items()) if ts >= since_ms]
        return tuple(series[-limit:])

    async def stream_book_top(self, symbols: Sequence[str]) -> AsyncIterator[BookTop]:
        """公共 tickers 流。

        选 tickers 而不是 orderbook.1：一条订阅同时给 bid1/ask1 和 fundingRate/
        nextFundingTime，对资金费套利正好够用（衍生品 100ms 推送）。

        最大的坑：linear 的 tickers 是**增量编码**的（type=snapshot|delta，
        「消息里没出现的字段表示没变化」）。直接读 data["bid1Price"] 在多数消息上是 None，
        必须并进本地状态字典。

        TODO: 未实现 orderbook.1 深度流（需要处理 size="0" 删档、u==1 服务重启重建）、
        私有 order/position/wallet 流、以及 WS trade 下单通道
        （那条通道的返佣不是 HTTP header，而是 JSON 信封里的 header.Referer）。
        """
        import websockets          # 延迟导入：只跑 REST 的场景不该被它拖依赖

        topics = [f"tickers.{s}" for s in symbols]
        state: dict[str, dict[str, Any]] = {}
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self._ws_url, ping_interval=None) as ws:
                    # 任何一次重连都必须丢弃本地状态重建，增量流没有断点续传
                    state.clear()
                    for chunk in _chunk_topics(topics):
                        await ws.send(_dumps({"op": "subscribe", "args": chunk}))
                    hb = asyncio.create_task(_ws_heartbeat(ws))
                    try:
                        async for raw in ws:
                            top = _merge_ticker(state,
                                                json.loads(raw, parse_float=Decimal))
                            if top is not None:
                                yield top
                    finally:
                        hb.cancel()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                # 每 5 分钟每域名只允许 500 条新连接，崩溃重连风暴会被限流
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    # ---- 交易 -----------------------------------------------------------

    async def place_order(self, req: OrderRequest) -> OrderResult:
        if not req.client_order_id:
            raise ValueError("Bybit 下单必须带 orderLinkId：它是唯一的幂等键，"
                             "没有它就无法消解 OrderUnknown")
        if len(req.client_order_id) > 36:
            raise ValueError(f"orderLinkId 超过 36 字符: {req.client_order_id!r}")

        inst = await self._instrument(req.symbol)
        qty = self._floor_step(req.qty, inst.lot_size)
        if qty <= 0:
            raise OrderRejected(self.venue.value, "PRECISION",
                                f"{req.symbol} 数量 {req.qty} 按 lot_size {inst.lot_size} "
                                "取整后为零", retriable=False)

        side = "Buy" if req.side.lower() == "buy" else "Sell"
        is_market = req.price is None
        body: dict[str, Any] = {
            "category": self._category,
            "symbol": req.symbol,
            "side": side,
            "orderType": "Market" if is_market else "Limit",
            "qty": self._fmt_step(qty, inst.lot_size),
        }
        if not is_market:
            price = self._round_price(req.price, inst.tick_size, req.side.lower())
            body["price"] = self._fmt_step(price, inst.tick_size)
        body["timeInForce"] = _TIF.get(req.time_in_force.upper(), req.time_in_force)
        body["orderLinkId"] = req.client_order_id
        # 单向持仓模式固定 0。对冲模式要给每单打 1/2 且 reduceOnly 语义会变，
        # 两腿对冲用单向模式即可，不要混用
        body["positionIdx"] = 0
        if req.reduce_only:
            body["reduceOnly"] = True

        try:
            data = await self._request("POST", _PATH_ORDER_CREATE, quota="trade",
                                       signed=True, body=body)
        except httpx.TransportError as exc:
            # 超时/连接中断 = 订单可能已经在撮合里。当作失败重发就是双倍仓位。
            # 用 TransportError 而不是 (TimeoutException, NetworkError)：
            # RemoteProtocolError（服务端收下请求后半途断连，最典型的"可能已进撮合"）
            # 走的是 ProtocolError 分支，不是 NetworkError 的子类
            raise OrderUnknown(self.venue.value, req.client_order_id) from exc
        except OrderRejected:
            raise                 # 明确被拒 = 撮合肯定没收到，不必白跑一轮消解
        except RateLimited:
            raise                 # 被限频 = 明确没进撮合，安全重试
        except ExchangeError as exc:
            # 5xx / 非 JSON 响应 / 服务端错误（10016）同样是「不知道有没有进去」。
            # exc.retriable 是 _parse 已经打好的标志位，不读它等于白定义
            if (exc.code.startswith("HTTP_5") or exc.code == "BAD_JSON"
                    or (exc.retriable and exc.code not in _PRE_MATCH_CODES)):
                raise OrderUnknown(self.venue.value, req.client_order_id) from exc
            raise

        result = data.get("result") or {}
        # retCode 0 只代表**已受理**，既不是已挂上也不是已成交。
        # 绝不能拿这个响应去更新本地仓位——成交要靠私有 order 流或 query_order 确认
        return OrderResult(
            client_order_id=str(result.get("orderLinkId") or req.client_order_id),
            exchange_order_id=str(result.get("orderId") or ""),
            status="new",
            filled_qty=Decimal("0"),
            avg_price=Decimal("0"),
        )

    async def cancel_order(self, symbol: str, client_order_id: str) -> None:
        try:
            await self._request("POST", _PATH_ORDER_CANCEL, quota="risk", signed=True,
                                body={"category": self._category, "symbol": symbol,
                                      "orderLinkId": client_order_id})
        except ExchangeError as exc:
            raise self._risk_path_error(exc) from exc

    def _risk_path_error(self, exc: ExchangeError) -> ExchangeError:
        """风控路径（撤单/查单）上的错误重分类。

        base.resolve_unknown_order 只认 (OrderRejected, httpx.HTTPError, RateLimited)：
        抛裸 ExchangeError('HTTP_5xx'/'BAD_JSON') 会让消解流程在最危险的时刻带着
        未捕获异常崩掉。可重试的挂到 RateLimited（code 保留）；不可重试的原样上抛，
        绝不降级成 OrderRejected —— 那等于谎报"成交量为 0"。
        """
        if isinstance(exc, (OrderRejected, RateLimited)) or not exc.retriable:
            return exc
        return RateLimited(self.venue.value, exc.code, exc.message, retriable=True)

    async def query_order(self, symbol: str, client_order_id: str) -> OrderResult:
        params = {"category": self._category, "symbol": symbol,
                  "orderLinkId": client_order_id}
        try:
            result = (await self._request("GET", _PATH_ORDER_REALTIME, quota="risk",
                                          signed=True, params=params))["result"]
            rows = result.get("list") or []
            if not rows:
                # realtime 只保留活跃/刚终结的单，全部成交后会从这里消失。
                # 此时报「未成交」是最危险的谎——上层会照着 0 再补一次仓。
                # TODO: /v5/order/history 的响应字段调研文档未覆盖，字段名按 v5 通用
                #       约定解析，接实盘前需用真实响应核对一次。
                result = (await self._request("GET", _PATH_ORDER_HISTORY, quota="risk",
                                              signed=True, params=params))["result"]
                rows = result.get("list") or []
        except ExchangeError as exc:
            raise self._risk_path_error(exc) from exc
        if not rows:
            raise OrderRejected(self.venue.value, "110001",
                                f"订单不存在: {client_order_id}", retriable=False)
        return self._to_order_result(rows[0])

    def _to_order_result(self, row: Mapping[str, Any]) -> OrderResult:
        return OrderResult(
            client_order_id=str(row.get("orderLinkId") or ""),
            exchange_order_id=str(row.get("orderId") or ""),
            status=_ORDER_STATUS.get(str(row.get("orderStatus", "")), "new"),
            filled_qty=_dec(row.get("cumExecQty")),
            avg_price=_dec(row.get("avgPrice")),
            fee=_dec(row.get("cumExecFee")),
            fee_asset=str(row.get("feeCurrency") or ""),
        )

    async def fetch_positions(self, symbols: Sequence[str] | None = None) -> Sequence[Position]:
        """持仓是真值。

        linear 必须给 symbol 或 settleCoin，两个都不给会报错。单符号时直接带 symbol
        （即使空仓也会回一行 size="0"，正好用来确认「确实是平的」）。
        """
        base_params: dict[str, Any] = {"category": self._category, "limit": 200}
        if symbols and len(symbols) == 1:
            base_params["symbol"] = symbols[0]
        else:
            base_params["settleCoin"] = self._settle_coin
        want = set(symbols) if symbols else None

        out: list[Position] = []
        cursor: str | None = None
        while True:
            params = dict(base_params)
            if cursor:
                params["cursor"] = cursor
            result = (await self._request("GET", _PATH_POSITION_LIST, quota="risk",
                                          signed=True, params=params))["result"]
            for row in result.get("list") or []:
                sym = str(row.get("symbol", ""))
                if want is not None and sym not in want:
                    continue
                size = _dec(row.get("size"))
                # side 为 "Sell" 才是空头；平仓后 side 会变成空字符串
                qty = -size if str(row.get("side")) == "Sell" else size
                liq = _dec(row.get("liqPrice"))
                out.append(Position(
                    market=self.market,
                    symbol=sym,
                    qty=qty,
                    entry_price=_dec(row.get("avgPrice") or row.get("entryPrice")),
                    mark_price=_dec(row.get("markPrice")),
                    liquidation_price=liq if liq > 0 else None,
                    leverage=_dec(row.get("leverage"), "1"),
                ))
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
        return tuple(out)

    async def fetch_open_orders(self, symbol: str) -> Sequence[OrderResult]:
        result = (await self._request("GET", _PATH_ORDER_REALTIME, quota="risk",
                                      signed=True,
                                      params={"category": self._category,
                                              "symbol": symbol}))["result"]
        return tuple(self._to_order_result(r) for r in (result.get("list") or []))

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        """单向持仓模式下 buyLeverage 必须等于 sellLeverage。

        只在启动时按符号设一次，绝不要放进收敛循环——它和下单共享 10/s 的账户预算，
        而重复设置本来就是空操作。

        配额走 risk（六家统一）：交易所侧它确实和下单共享预算，但
        safety.liq_distance_deleverage 触发的降杠杆是风控动作，宁可多占一点交易所
        限频，也不能在爆仓距离告急时排在行情/下单后面。
        """
        value = f"{leverage.normalize():f}"
        try:
            await self._request("POST", _PATH_SET_LEVERAGE, quota="risk", signed=True,
                                body={"category": self._category, "symbol": symbol,
                                      "buyLeverage": value, "sellLeverage": value})
        except ExchangeError as exc:
            # 110043 = leverage not modified。这是幂等成功，不是失败
            if exc.code != "110043":
                raise

    async def fetch_leverage_tiers(self, symbol: str) -> Sequence[tuple[Decimal, Decimal]]:
        """风险限额档位，升序。贴着档位边界开仓，价格一动就跨档降杠杆 + 追保。

        TODO: /v5/market/risk-limit 不在调研文档覆盖范围内，字段名
        （riskLimitValue / maxLeverage）按 v5 通用约定解析。任何失败都退回
        instruments-info.leverageFilter 的单档（名义无上限），保证调用方拿得到东西。
        """
        try:
            result = (await self._request("GET", _PATH_RISK_LIMIT, quota="market",
                                          params={"category": self._category,
                                                  "symbol": symbol}))["result"]
            tiers = [(_dec(r.get("riskLimitValue")), _dec(r.get("maxLeverage"), "1"))
                     for r in (result.get("list") or [])]
            if tiers:
                return tuple(sorted(tiers, key=lambda t: t[0]))
        except (ExchangeError, httpx.HTTPError):
            pass
        inst = await self._instrument(symbol)
        return ((Decimal("Infinity"), inst.max_leverage),)


# 引擎用大写的 TIF，Bybit 要驼峰的 PostOnly
_TIF = {"IOC": "IOC", "GTC": "GTC", "FOK": "FOK",
        "POSTONLY": "PostOnly", "POST_ONLY": "PostOnly"}


def _sum_band(levels: Sequence[Sequence[Any]], low: Decimal,
              high: Decimal) -> tuple[Decimal, int]:
    """累计 [low, high] 内的名义额，返回 (名义额, 计入档数)。

    命中区间外就跳过而**不是** break：文档说 b 降序、a 升序，但一次排序意外就会
    把深度截断成"只有一档"——那恰恰是这次要修的病。200 档全过一遍的代价可以忽略。

    linear 永续的 size 单位就是 base 币（contract_size 恒为 1），price*size 直接
    就是 USDT 名义额，没有张→币换算。inverse 的 size 是 USD 面值张数，
    照抄这段会把名义额算成另一个币种——这也是本适配器只做 linear 的原因之一。

    size 为 0 是 WS 增量流的删档约定，REST 快照里不该出现；真出现了也不占档数，
    否则 levels_used 会虚高，掩盖"带宽内档位不够"。
    """
    total = Decimal("0")
    used = 0
    for level in levels:
        price = _dec(level[0])
        qty = _dec(level[1])
        if qty <= 0 or price < low or price > high:
            continue
        total += price * qty
        used += 1
    return total, used


def _chunk_topics(topics: Sequence[str]) -> Iterable[list[str]]:
    """按字符数拆分订阅：args 数组约 21000 字符封顶，不是按条数。"""
    chunk: list[str] = []
    size = 0
    for t in topics:
        if chunk and size + len(t) + 3 > _WS_ARGS_CHARS:
            yield chunk
            chunk, size = [], 0
        chunk.append(t)
        size += len(t) + 3
    if chunk:
        yield chunk


async def _ws_heartbeat(ws: Any) -> None:
    """应用层 JSON 心跳。协议级 ping 帧保不住这条连接，漏发就被断。"""
    while True:
        await asyncio.sleep(_WS_PING_S)
        await ws.send(_dumps({"op": "ping"}))


def _merge_ticker(state: dict[str, dict[str, Any]],
                  msg: Mapping[str, Any]) -> BookTop | None:
    """把 delta 并进本地状态，四个盘口字段齐了才吐一个 BookTop。"""
    topic = str(msg.get("topic", ""))
    if not topic.startswith("tickers."):
        return None
    data = msg.get("data") or {}
    symbol = str(data.get("symbol") or topic.partition(".")[2])
    if msg.get("type") == "snapshot":
        state[symbol] = dict(data)
    else:
        state.setdefault(symbol, {}).update(data)
    cur = state[symbol]
    fields = ("bid1Price", "bid1Size", "ask1Price", "ask1Size")
    if any(cur.get(f) in (None, "") for f in fields):
        return None
    return BookTop(
        bid_price=_dec(cur["bid1Price"]),
        bid_qty=_dec(cur["bid1Size"]),
        ask_price=_dec(cur["ask1Price"]),
        ask_qty=_dec(cur["ask1Size"]),
        # cts 是撮合引擎时间，比网关的 ts 更接近真实行情时刻
        ts_ms=int(msg.get("cts") or msg.get("ts") or 0),
    )
