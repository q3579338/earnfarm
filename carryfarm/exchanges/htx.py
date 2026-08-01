"""HTX（原火币）USDT 本位永续适配器。

三件事和别家完全不同，实现时全绕着它们转：

1. **签名走 URL query，不走 header**，而且 **POST 的 JSON body 完全不进签名串**。
   套用币安/OKX 的"body 也签"模板会一直吃 1003 invalid signature。
2. **数量单位是「张」且必须是整数**。要下 0.05 BTC 得先拿 contract_size=0.001
   再算 volume=50，余数直接丢。所以本适配器对外一律用**币本位**数量，
   lot_size 就等于 contract_size —— 上层按 lot_size 取整后天然落在整张上。
3. **返佣走 body 的 channel_code 字段**（未公开字段），不是 header 也不是订单ID前缀。
   合约的 client_order_id 是 long，塞不下字母，这正是它要单开一个字段的原因。

账户模式：默认走经典单币种保证金的 /linear-swap-api/v1/*。多币种保证金账户是
/v5/*，两套互斥（CCXT issue #27612 就是无条件路由到 v5 把单币种账户坑了）。
本适配器只走 v1，v5 待用户显式要求再加。
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import time
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, AsyncIterator, Mapping, Sequence
from urllib.parse import quote, urlsplit

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
    ZERO,
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

REST_BASE = "https://api.hbdm.com"          # AWS 优选 api.hbdm.vn，备用 api.btcgateway.pro
WS_MARKET_URL = "wss://api.hbdm.com/linear-swap-ws"

# 历史资金费分页上限硬是 100，传 200/1000 会被**静默截断**（不报错），只能靠 total_page 翻页
HISTORY_PAGE_SIZE = 100
MAX_HISTORY_PAGES = 200
INSTRUMENT_TTL_S = 3600

# 深度端点**没有**「档位数」参数（不像币安的 limit），档数只能靠 type 选：
# step0 = 不合并档位，是能拿到最多档的一档；step1~step5 按价格粒度合并，
# 档位更粗更少，10bp 这种窄带宽会被合并档位糊掉。所以固定用 step0。
DEPTH_TYPE = "step0"

# 明确的拒单类错误码：订单不会成交，重发是安全的
REJECT_CODES: Mapping[int, str] = {
    1013: "合约不存在",
    1017: "订单不存在",
    1034: "订单价格类型错误",
    1039: "价格超出限价",
    1041: "数量超出限制",
    1047: "可用保证金不足",
    1048: "可平数量不足",
    1061: "订单不存在",
    1066: "参数缺失",
    1067: "client_order_id 非法",
    1094: "杠杆为空，需先调 switch_lever_rate",
    1220: "未开通合约交易",
}
# 交易所不认识这张单 —— resolve_unknown_order 靠它判定"从未被接受"
ORDER_NOT_FOUND_CODES = frozenset({1017, 1061})
# 1084 是撤单率封禁（10 分钟内 ≥3000 笔且撤单率 >99%，封 limit/post_only/fok/ioc 五分钟），
# 本质是限频，等解禁后可以恢复，所以归到 RateLimited 而不是拒单
RATE_LIMIT_CODES = frozenset({1084})

# HTX 订单状态：1准备提交 2准备提交 3已提交 4部分成交 5部分成交已撤单 6全部成交 7已撤单 11撤单中
ORDER_STATUS: Mapping[int, str] = {
    1: "new", 2: "new", 3: "new", 4: "partial",
    5: "canceled", 6: "filled", 7: "canceled", 11: "new",
}

# time_in_force → order_price_type。注意 limit/post_only/ioc/fok 这四种恰好是
# 撤单率风控（1084）盯的类型；高频改单场景应该改用不受限的 opponent/optimal_N 变体
PRICE_TYPE_BY_TIF: Mapping[str, str] = {
    "IOC": "ioc", "FOK": "fok", "GTC": "limit",
    "GTX": "post_only", "POST_ONLY": "post_only",
}


def _dec(value: Any) -> Decimal:
    """任何交易所返回值 → Decimal。绝不经过 float。

    JSON 里的数字已经在 _decode 里用 parse_float=Decimal 解析掉了，
    所以这里拿到的要么是 Decimal 要么是 str 要么是 int。
    """
    if value is None or value == "":
        return ZERO
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):        # 防止 True 被当成 1
        raise TypeError(f"不是数值: {value!r}")
    if isinstance(value, float):       # 兜底：正常路径不会走到（parse_float 已拦截）
        return Decimal(repr(value))
    return Decimal(str(value))


def _uri_encode(value: Any) -> str:
    """HTX 要求十六进制**大写**（":"→"%3A" 不能是 "%3a"）。

    Python 的 quote 默认就是大写，逗号也会编成 %2C，正好满足 CCXT 里
    那句 replace('%2c','%2C') 想达到的效果。
    """
    return quote(str(value), safe="")


class HtxAdapter(ExchangeAdapter):
    """HTX USDT 本位永续（经典单币种保证金账户，/linear-swap-api/v1/*）。

    对外的数量单位一律是**币**（base），内部才换算成张。这样 lot_size /
    Position.qty / OrderResult.filled_qty 和别家口径一致，
    上层不必知道 HTX 有"张"这回事 —— 只要按 lot_size(=contract_size) 取整即可。
    """

    venue = Venue.HTX

    def __init__(self, credential: Credential, *, broker_code: str = "",
                 client: httpx.AsyncClient | None = None,
                 kind: MarketKind = MarketKind.PERP,
                 margin_mode: str = "cross",
                 default_leverage: int = 3,
                 rest_base: str = REST_BASE,
                 ws_market_url: str = WS_MARKET_URL) -> None:
        if kind is not MarketKind.PERP:
            # 现货/杠杆没做：调研文档只给了域名(api.huobi.pro)、符号大小写规则
            # 和"返佣走 client-order-id 前缀(≤64字符)"，缺的是下单端点参数表
            # (/v1/order/orders/place 的 account-id 从哪来)、symbols 精度字段
            # (amount-precision / price-precision / min-order-value)、以及杠杆
            # 借币利率端点。凭记忆写这些一定会错，所以直接拒绝构造。
            raise NotImplementedError(
                "HTX 适配器目前只实现 USDT 本位永续（MarketKind.PERP）。"
                "现货/全仓杠杆缺调研：下单端点参数表、account-id 获取方式、"
                "symbols 精度字段、借币利率端点均未覆盖。"
            )
        if margin_mode not in ("cross", "isolated"):
            raise ValueError(f"margin_mode 只能是 cross 或 isolated，当前 {margin_mode!r}")
        # 用户从网页复制返佣码常带尾随空白/换行，六家统一在构造期剥掉
        super().__init__(credential, broker_code=broker_code.strip(), client=client)
        self.rest_base = rest_base.rstrip("/")
        self.ws_market_url = ws_market_url
        self.kind = kind
        self.margin_mode = margin_mode
        self.default_leverage = int(default_leverage)
        self._instruments: dict[str, Instrument] = {}
        self._instruments_at = 0.0
        # HTX 合约的 client_order_id 是 long，装不下字符串 ID，需要一张双向映射表
        self._cid_to_num: dict[str, int] = {}
        self._num_to_cid: dict[int, str] = {}
        # 每个合约当前的杠杆。下单时 lever_rate 必须与持仓杠杆一致，否则直接拒单
        self._leverage: dict[str, int] = {}
        # 响应 header 实时回传的限频余量，用于自适应退避（别靠本地计数猜）
        self.last_ratelimit: dict[str, str] = {}

    @property
    def market(self) -> str:
        return f"{self.venue.value}:{self.kind.value}"

    # ---- 路径 -----------------------------------------------------------

    @property
    def _p(self) -> str:
        """全仓走 swap_cross_*，逐仓走 swap_*。两套路径只差这个中缀。"""
        return "swap_cross_" if self.margin_mode == "cross" else "swap_"

    # ---- 签名 -----------------------------------------------------------

    def _host(self) -> str:
        return (urlsplit(self.rest_base).hostname or "").lower()

    def _timestamp_str(self) -> str:
        """UTC 秒级，格式 YYYY-MM-DDTHH:MM:SS，无毫秒无 Z 后缀。"""
        ts = self.timestamp_ms() // 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    def _auth_params(self) -> dict[str, str]:
        return {
            "AccessKeyId": self._cred.api_key,
            "SignatureMethod": "HmacSHA256",
            "SignatureVersion": "2",
            "Timestamp": self._timestamp_str(),
        }

    @staticmethod
    def _canonical_query(params: Mapping[str, Any]) -> str:
        """参数名 ASCII 升序 + 值 URI 编码（大写十六进制）+ & 连接。"""
        return "&".join(
            f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in sorted(params.items())
        )

    def _sign_payload(self, method: str, path: str, sign_params: Mapping[str, Any]) -> str:
        """待签字符串 = 4 行，\\n 连接，末行后无换行。"""
        return "\n".join(
            [method.upper(), self._host(), path, self._canonical_query(sign_params)]
        )

    def _prepare(self, method: str, path: str, params: dict, body: dict,
                 signed: bool) -> tuple[dict[str, str], str, dict, dict]:
        method = method.upper()
        headers = {
            "Content-Type": "application/x-www-form-urlencoded" if method == "GET"
            else "application/json",
        }
        if not signed:
            query = self._canonical_query(params)
            url = f"{self.rest_base}{path}" + (f"?{query}" if query else "")
            return headers, url, {}, dict(body)

        auth = self._auth_params()
        # 这是 HTX 最大的坑：POST 的业务参数一个都不进签名，只签这 4 个认证参数，
        # 业务参数全部放 JSON body；GET 才把业务参数并进签名串。
        sign_params = {**params, **auth} if method == "GET" else auth
        payload = self._sign_payload(method, path, sign_params)
        signature = self._hmac_sha256_b64(payload)
        # 自己拼 query 而不是交给 httpx —— 签名串里的编码必须和实际发出去的字节
        # 一模一样，交给别人编码就等于赌它的编码规则和 HTX 一致
        query = f"{self._canonical_query(sign_params)}&Signature={_uri_encode(signature)}"
        return headers, f"{self.rest_base}{path}?{query}", {}, dict(body)

    # ---- 响应解析 -------------------------------------------------------

    def _absorb_ratelimit(self, resp: httpx.Response) -> None:
        for key in ("ratelimit-limit", "ratelimit-interval",
                    "ratelimit-remaining", "ratelimit-reset", "recovery-time"):
            value = resp.headers.get(key)
            if value is not None:
                self.last_ratelimit[key] = value

    def _translate(self, code: Any, message: str) -> ExchangeError:
        try:
            num = int(code)
        except (TypeError, ValueError):
            return ExchangeError(self.venue.value, str(code), message)
        if num in RATE_LIMIT_CODES:
            return RateLimited(self.venue.value, str(num), message or "撤单率风控封禁",
                               retriable=True)
        if num in REJECT_CODES:
            return OrderRejected(self.venue.value, str(num),
                                 message or REJECT_CODES[num], retriable=False)
        # 1003 签名无效属于配置问题，重试一万次也还是错的
        return ExchangeError(self.venue.value, str(num), message, retriable=False)

    def _decode(self, resp: httpx.Response) -> Any:
        """HTTP 层 + 业务层错误一起处理，返回完整 payload。

        HTX 的业务错误 HTTP 状态码仍然是 200，错误码在 body 里。
        """
        self._absorb_ratelimit(resp)
        if resp.status_code == 429:
            raise RateLimited(self.venue.value, "429", "HTTP 429 限频", retriable=True)
        if resp.status_code >= 500:
            raise ExchangeError(self.venue.value, f"HTTP_{resp.status_code}",
                                resp.text[:200], retriable=True)
        try:
            # parse_float=Decimal 是全局精度纪律的关键一环：price_tick 小到 1e-10，
            # 经 float 中转会在下单时被拒（价格必须是 tick 的整数倍）
            payload = json.loads(resp.text, parse_float=Decimal)
        except ValueError as exc:
            raise ExchangeError(self.venue.value, "BAD_JSON",
                                f"响应不是合法 JSON: {resp.text[:200]}",
                                retriable=True) from exc
        if resp.status_code >= 400:
            raise ExchangeError(self.venue.value, f"HTTP_{resp.status_code}",
                                resp.text[:200], retriable=False)
        if not isinstance(payload, dict):
            return payload
        if payload.get("status") == "error":
            raise self._translate(
                payload.get("err_code", payload.get("err-code", "UNKNOWN")),
                str(payload.get("err_msg", payload.get("err-msg", ""))),
            )
        code = payload.get("code")          # /v5/* 用 code/msg/data
        if code is not None and int(code) != 200:
            raise self._translate(code, str(payload.get("msg", "")))
        return payload

    def _parse(self, resp: httpx.Response) -> Any:
        payload = self._decode(resp)
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    # ---- 元数据 ---------------------------------------------------------

    async def fetch_server_time_ms(self) -> int:
        """用任意 v1 响应的 ts 字段校时。

        故意不走 self._request：基类在 _request 里发现时钟陈旧会回调 sync_clock，
        而 sync_clock 又调本方法 —— 嵌套获取同一个配额信号量会直接把自己锁死。
        （调研文档没给专门的服务器时间端点，v1 响应统一带毫秒级 ts，用它最稳。）
        """
        url = f"{self.rest_base}/linear-swap-api/v1/swap_funding_rate"
        try:
            resp = await self._client.get(url, params={"contract_code": "BTC-USDT"})
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ExchangeError(self.venue.value, "NETWORK", str(exc), retriable=True) from exc
        payload = self._decode(resp)
        return int(payload["ts"])

    async def fetch_instruments(self) -> Sequence[Instrument]:
        data = await self._request("GET", "/linear-swap-api/v1/swap_contract_info",
                                   quota="market")
        out: list[Instrument] = []
        for row in data or []:
            if int(row.get("contract_status", 1)) != 1:
                continue                    # 实测还存在 status=3，非正常交易状态一律跳过
            code = str(row["contract_code"])
            base = str(row.get("symbol") or code.split("-")[0])
            quote_ccy = code.split("-")[-1]
            contract_size = _dec(row["contract_size"])
            # settlement_period 是**小时**且类型是字符串（"8"/"4"/"1"）。
            # HTX 268 个永续里 67 个是 4h、8 个是 1h，直接拿费率跨所比一定错
            period_h = int(_dec(row.get("settlement_period", 8)))
            out.append(Instrument(
                market=self.market,
                symbol=code,
                base=base,
                quote=quote_ccy,
                tick_size=_dec(row["price_tick"]),
                # HTX 的数量粒度就是一张合约代表的币数，没有比它更细的刻度
                lot_size=contract_size,
                # HTX 没有 minNotional 字段，最小下单量固定 1 张，
                # 该约束已由 lot_size 表达；名义额下限 = contract_size × 标记价，随价格浮动
                min_notional=ZERO,
                contract_size=contract_size,
                # 杠杆上限要单独查 swap_query_elements 的 max_level，
                # 268 个合约逐个查代价太高，这里保持默认值（=未知，偏保守）
                funding_interval_s=period_h * 3600,
            ))
        self._instruments = {i.symbol: i for i in out}
        self._instruments_at = time.time()
        return tuple(out)

    async def _ensure_instruments(self) -> None:
        if not self._instruments or time.time() - self._instruments_at > INSTRUMENT_TTL_S:
            await self.fetch_instruments()

    async def _instrument(self, symbol: str) -> Instrument:
        await self._ensure_instruments()
        inst = self._instruments.get(symbol)
        if inst is None:
            raise ExchangeError(self.venue.value, "UNKNOWN_SYMBOL",
                                f"{symbol} 不在 swap_contract_info 里")
        return inst

    async def fetch_fee_schedule(self, symbols: Sequence[str]) -> Sequence[FeeSchedule]:
        """用户实际费率档位。每条都带 symbol，调用方按 symbol 匹配、绝不按下标。

        HTX 把开仓和平仓费率分开给（open_taker_fee / close_taker_fee），
        而 FeeSchedule 只有一个 taker 位，这里取两者较大值 —— 宁可高估成本。

        符号多于 3 个时改成**一次全量拉**：这是 POST 逐个查，5 个符号就是 5 个签名
        请求，而不带 contract_code 一次就能拿回全部合约。查不到的符号不返回。

        HTX 点卡（HT/HTX Points）抵扣：swap_fee 只给 fee_asset，没有折扣开关，
        折扣幅度也不在任何 API 里，所以这里不打折——方向是高估成本，安全的那一边。
        """
        rows: dict[str, dict] = {}
        # 逐个查是 N 个签名请求；超过 3 个就不如一把全量拉回来自己挑。
        # 全量路径依赖「不传 contract_code 就返回全部合约」这个行为——不传符号时
        # 本来就走这条路，所以两条路共用同一个假设，没有多出新的赌注。
        targets: list[str | None] = (list(symbols) if 0 < len(symbols) <= 3
                                     else [None])
        for symbol in targets:
            body = {"business_type": "swap"}
            if symbol:
                body["contract_code"] = symbol
            data = await self._request("POST", "/linear-swap-api/v1/swap_fee",
                                       quota="market", signed=True, body=body)
            for row in data or []:
                rows[str(row["contract_code"])] = row

        order = list(symbols) if symbols else list(rows)
        out: list[FeeSchedule] = []
        for symbol in order:
            row = rows.get(symbol)
            if row is None:
                continue
            taker = max(_dec(row.get("open_taker_fee")), _dec(row.get("close_taker_fee")))
            maker = max(_dec(row.get("open_maker_fee")), _dec(row.get("close_maker_fee")))
            # 四个费率字段全缺 → taker 会算出 0。零手续费不是"免费"，是"没查到"，
            # 让它冒充真实档位会把负期望的机会整片翻成正期望
            if all(row.get(k) in (None, "") for k in
                   ("open_taker_fee", "close_taker_fee")):
                continue
            out.append(FeeSchedule(
                market=self.market, symbol=symbol, maker=maker, taker=taker,
            ))
        return tuple(out)

    async def fetch_funding_limits(self, symbol: str) -> tuple[Decimal, Decimal] | None:
        """返回 (cap, floor)，顺序与基类文档字符串一致。

        费率上下限是**按合约**给的不是全局统一：BTC ±0.375%，SPX500 ±2%。
        """
        data = await self._request("GET", "/linear-swap-api/v1/swap_query_elements",
                                   quota="market", params={"contract_code": symbol})
        row = self._first_row(data)
        if not row:
            return None
        cap, floor = row.get("funding_rate_cap"), row.get("funding_rate_floor")
        if cap is None or floor is None:
            return None
        return _dec(cap), _dec(floor)

    @staticmethod
    def _first_row(data: Any) -> dict | None:
        if isinstance(data, list):
            return data[0] if data else None
        return data if isinstance(data, dict) else None

    # ---- 行情 -----------------------------------------------------------

    async def _depth_tick(self, symbol: str) -> tuple[Mapping[str, Any], int]:
        """拉一次 step0 深度，返回 (tick, ts_ms)。fetch_book_top / fetch_book_depth 共用。

        注意：调研文档只覆盖了 WS 的 bbo 频道，没给 REST 深度端点。
        这里用官方行情域 /linear-swap-ex/market/depth（step0 = 不合并档位），
        属于文档外补充，接实盘前值得抓一次包核对字段名和 step0 的实际档数上限。
        """
        payload = await self._request(
            "GET", "/linear-swap-ex/market/depth", quota="market",
            params={"contract_code": symbol, "type": DEPTH_TYPE},
        )
        tick = payload.get("tick") if isinstance(payload, dict) else None
        if not tick or not tick.get("bids") or not tick.get("asks"):
            # 单边空簿算不出中价，这一拍不可用
            raise ExchangeError(self.venue.value, "EMPTY_BOOK", f"{symbol} 盘口为空")
        ts_ms = int(_dec(tick.get("ts") or payload.get("ts") or self.timestamp_ms()))
        return tick, ts_ms

    async def fetch_book_top(self, symbol: str) -> BookTop:
        """REST 盘口一档。"""
        inst = await self._instrument(symbol)
        tick, ts_ms = await self._depth_tick(symbol)
        bid, ask = tick["bids"][0], tick["asks"][0]
        # 深度里的量单位是**张**，统一换算成币，和 lot_size / Position.qty 同口径
        return BookTop(
            bid_price=_dec(bid[0]), bid_qty=_dec(bid[1]) * inst.contract_size,
            ask_price=_dec(ask[0]), ask_qty=_dec(ask[1]) * inst.contract_size,
            ts_ms=ts_ms,
        )

    @staticmethod
    def _band_side(levels: Sequence[Any], contract_size: Decimal,
                   lo: Decimal, hi: Decimal, descending: bool) -> tuple[Decimal, int]:
        """一侧盘口在 [lo, hi] 内的累计名义额和档数。

        簿是排好序的（bids 降序、asks 升序），越过**远端**边界可以直接停——
        后面只会更远。越过**近端**边界（正常簿不会有，只有交叉/陈旧簿才会）
        只跳过这一档不能停：再往后可能还有落在带内的档位。
        万一交易所哪天返回乱序，提前 break 的后果是少算几档 = 低估，
        这个方向是安全的（宁可低估也别编）。

        名义额 = 价 × 张数 × contract_size。少乘 contract_size 就是把张数当币数：
        BTC(0.001) 会高估 1000 倍、SHIB(1000) 会低估 1000 倍、PEPE(1e6) 低估 100 万倍。
        """
        notional = ZERO
        used = 0
        for level in levels:
            price, qty = _dec(level[0]), _dec(level[1])
            if price <= 0 or qty <= 0:
                continue                      # 脏档位不计入，也不算进 levels_used
            if price < lo:
                if descending:
                    break
                continue
            if price > hi:
                if descending:
                    continue
                break
            notional += price * qty * contract_size
            used += 1
        return notional, used

    async def fetch_book_depth(self, symbol: str, band_bp: float = 10.0) -> BookDepth:
        """band_bp 基点带宽内的可见深度（名义额，quote 计价）。

        以中价为基准，买卖两侧各统计 [mid*(1-band), mid] 和 [mid, mid*(1+band)]
        内的累计名义额。只拿一档当深度代理会把容量低估一两个数量级。

        限频：走行情域 /linear-swap-ex/*，调研文档口径是「公开行情类 REST
        800 次/秒 每 IP」。HTX 没有币安那种**权重制**——depth 和 bbo 各记一次请求，
        贵的是响应体（step0 一次上百档）不是配额。配额池用 quota="market"，
        和 fetch_book_top 打的是同一个 IP 桶，深度轮询会直接吃掉一档轮询的额度，
        绝不能挪去 risk/trade 池。真实余量看响应 header 的 ratelimit-remaining
        （_absorb_ratelimit 已收下），别靠本地计数猜。

        levels_used = 买卖两侧计入档位之和。它等于交易所实际返回的档数时，说明簿被
        step0 的档数上限截断了、带宽可能没盖满——如实低估，不外推补足。

        坑：价差比带宽还宽时（小币 + 窄 band）连一档都落不进带内，
        此时两侧名义额和 levels_used 都是 0。这是**真实情况**不是异常，
        不要拿一档兜底充数——scoring.slippage_rate 见到 depth_notional<=0
        会自动退回「只收半价差」的口径。
        """
        if band_bp <= 0:
            raise ValueError(f"band_bp 必须为正，收到 {band_bp}")
        inst = await self._instrument(symbol)
        tick, ts_ms = await self._depth_tick(symbol)
        bids, asks = tick["bids"], tick["asks"]

        best_bid, best_ask = _dec(bids[0][0]), _dec(asks[0][0])
        # band_bp 是 float（scoring 那边就是 float 口径），只在这一处经 str 转成 Decimal，
        # 之后全程 Decimal —— 直接 Decimal(float) 会拖一串二进制尾巴进价格比较
        mid = (best_bid + best_ask) / 2
        band = Decimal(str(band_bp)) / Decimal("10000")
        lo, hi = mid * (1 - band), mid * (1 + band)

        bid_notional, bid_levels = self._band_side(bids, inst.contract_size, lo, mid, True)
        ask_notional, ask_levels = self._band_side(asks, inst.contract_size, mid, hi, False)

        return BookDepth(
            symbol=symbol,
            bid_notional=bid_notional,
            ask_notional=ask_notional,
            bid_price=best_bid,
            # 一档量同样是张数，对外统一换成币（和 fetch_book_top 一致）
            bid_qty=_dec(bids[0][1]) * inst.contract_size,
            ask_price=best_ask,
            ask_qty=_dec(asks[0][1]) * inst.contract_size,
            levels_used=bid_levels + ask_levels,
            band_bp=band_bp,
            ts_ms=ts_ms,
        )

    @staticmethod
    def _next_settle_ms(funding_time_ms: int, interval_s: int, now_ms: int) -> int:
        """v1 的 next_funding_time 已废弃恒为 null，只能拿 funding_time 自己往前推。"""
        step = interval_s * 1000
        if step <= 0 or funding_time_ms >= now_ms:
            return funding_time_ms
        missed = (now_ms - funding_time_ms + step - 1) // step
        return funding_time_ms + missed * step

    async def fetch_carry_rates(self, symbols: Sequence[str] | None = None) -> Sequence[CarryRate]:
        """当期资金费。一次请求拿全市场 272 个合约，不要逐个查。

        符号约定：**原样存交易所返回值**（正 = 多头付给空头），不取反。
        基类那句"适配器要取负"是旧注释，models.CarryRate 和 scoring.py
        的 funding_income（Σf_S − Σf_L）才是现行口径，取反反而会让净收益反号。
        """
        await self._ensure_instruments()
        data = await self._request("GET", "/linear-swap-api/v1/swap_batch_funding_rate",
                                   quota="market")
        wanted = set(symbols) if symbols else None
        now_ms = self.timestamp_ms()
        out: list[CarryRate] = []
        for row in data or []:
            code = str(row.get("contract_code") or "")
            if wanted is not None and code not in wanted:
                continue
            rate = row.get("funding_rate")
            if rate in (None, ""):          # 新上合约首期结算前没有费率
                continue
            inst = self._instruments.get(code)
            interval_s = inst.funding_interval_s if inst else 28800
            funding_time = int(_dec(row.get("funding_time") or 0))
            out.append(CarryRate(
                market=self.market,
                symbol=code,
                rate=_dec(rate),
                interval_s=interval_s,
                next_settle_ms=self._next_settle_ms(funding_time, interval_s, now_ms),
                ts_ms=now_ms,
            ))
        return tuple(out)

    async def fetch_funding_history(self, symbol: str, since_ms: int,
                                    limit: int = 1000) -> Sequence[tuple[int, Decimal]]:
        """历史资金费，升序。BTC-USDT 实测能一路翻到 2020 上市日，没有时间窗限制。"""
        out: list[tuple[int, Decimal]] = []
        page, total_page = 1, 1
        while page <= min(total_page, MAX_HISTORY_PAGES):
            data = await self._request(
                "GET", "/linear-swap-api/v1/swap_historical_funding_rate", quota="market",
                params={"contract_code": symbol, "page_index": page,
                        "page_size": HISTORY_PAGE_SIZE},
            )
            data = data if isinstance(data, dict) else {}
            total_page = int(_dec(data.get("total_page") or 1))
            rows = data.get("data") or []
            if not rows:
                break
            oldest = None
            for row in rows:
                ts = int(_dec(row.get("funding_time") or 0))
                # 做回测该用 realized_rate（扣资金费会爆仓时交易所会少收），
                # 但实测它恒为 null，所以必须回落到挂牌费率
                rate = row.get("realized_rate")
                if rate in (None, ""):
                    rate = row.get("funding_rate")
                if rate in (None, ""):
                    continue
                out.append((ts, _dec(rate)))
                oldest = ts if oldest is None else min(oldest, ts)
            if oldest is not None and oldest <= since_ms:
                break
            if len(out) >= limit + HISTORY_PAGE_SIZE:
                break
            page += 1
        out = sorted({ts: rate for ts, rate in out if ts >= since_ms}.items())
        return tuple(out[-limit:]) if limit > 0 else tuple(out)

    async def stream_book_top(self, symbols: Sequence[str]) -> AsyncIterator[BookTop]:
        """行情 WS：wss://api.hbdm.com/linear-swap-ws。

        最小可用版：订阅 + 心跳 + 指数退避重连。三个 HTX 独有的点：
        - 所有响应都是 **GZIP 压缩**的，不解压就是一堆二进制乱码；
        - 心跳是 {"ping":ts} → {"pong":ts}（通知端点是 {"op":"ping"} 那一套，两者不能混），
          连续 5 次不回就断连；
        - bbo 频道官方明说买一卖一有 **约 500ms 延迟**。

        TODO: 500ms 收敛循环用 bbo 等于永远看上一拍的价格，要真实时得订
        market.$code.depth.size_20.high_freq 增量深度并自行维护订单簿。
        TODO: 未做订阅 ack 校验和单连接 40 条/秒的订阅限速。
        """
        import websockets                    # 惰性导入：不跑 WS 的场景不必装这个依赖

        await self._ensure_instruments()
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.ws_market_url, ping_interval=None) as ws:
                    for idx, symbol in enumerate(symbols):
                        await ws.send(json.dumps(
                            {"sub": f"market.{symbol}.bbo", "id": f"carryfarm-{idx}"}))
                    backoff = 1.0
                    async for raw in ws:
                        msg = self._ws_decode(raw)
                        if msg is None:
                            continue
                        if "ping" in msg:
                            await ws.send(json.dumps({"pong": msg["ping"]}))
                            continue
                        tick = msg.get("tick")
                        if not tick or not tick.get("bid") or not tick.get("ask"):
                            continue
                        parts = str(tick.get("ch", msg.get("ch", ""))).split(".")
                        inst = self._instruments.get(parts[1] if len(parts) > 2 else "")
                        size = inst.contract_size if inst else Decimal("1")
                        yield BookTop(
                            bid_price=_dec(tick["bid"][0]), bid_qty=_dec(tick["bid"][1]) * size,
                            ask_price=_dec(tick["ask"][0]), ask_qty=_dec(tick["ask"][1]) * size,
                            ts_ms=int(_dec(tick.get("ts") or msg.get("ts") or 0)),
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    @staticmethod
    def _ws_decode(raw: Any) -> dict | None:
        if isinstance(raw, (bytes, bytearray)):
            raw = gzip.decompress(raw).decode()
        try:
            msg = json.loads(raw, parse_float=Decimal)
        except ValueError:
            return None
        return msg if isinstance(msg, dict) else None

    # ---- 交易 -----------------------------------------------------------

    def _numeric_cid(self, client_order_id: str) -> int:
        """字符串 client_order_id → HTX 要求的 long [1, 2^63-1]。

        合约的 client_order_id 类型是 long，塞不进字母（现货才是字符串，
        所以现货返佣能走 ID 前缀而合约必须另开 channel_code 字段）。
        用 blake2b 取 63 位是**确定性**的 —— 进程重启后同一个字符串仍映射到
        同一个数字，撤单/查单才找得回来。
        """
        if not client_order_id:
            raise ValueError("HTX 下单必须带 client_order_id")
        cached = self._cid_to_num.get(client_order_id)
        if cached is not None:
            return cached
        if client_order_id.isdigit() and 1 <= int(client_order_id) < 2 ** 63:
            num = int(client_order_id)
        else:
            digest = hashlib.blake2b(client_order_id.encode(), digest_size=8).digest()
            num = (int.from_bytes(digest, "big") >> 1) or 1
        self._cid_to_num[client_order_id] = num
        self._num_to_cid[num] = client_order_id
        return num

    def _restore_cid(self, numeric: Any) -> str:
        try:
            num = int(_dec(numeric))
        except Exception:
            return str(numeric)
        return self._num_to_cid.get(num, str(num))

    def _quantize_price(self, price: Decimal, tick: Decimal, side: str) -> Decimal:
        """价格规整到 tick 的整数倍。

        方向敏感：保护限价是故意穿透对手价的，买单向上取整、卖单向下取整，
        免得取整把穿透幅度削回来导致挂不上。
        """
        if tick <= 0:
            return price
        rounding = ROUND_CEILING if side == "buy" else ROUND_FLOOR
        return (price / tick).to_integral_value(rounding=rounding) * tick

    def _inject_broker(self, body: dict) -> dict:
        """返佣：合约走 body 的 channel_code 字段（未公开字段，只对签约经纪商开放）。

        空码时**整个 key 都不放**，而不是发空串 —— 对未签约用户最安全。
        """
        if self.broker_code:
            body["channel_code"] = self.broker_code
        return body

    async def place_order(self, req: OrderRequest) -> OrderResult:
        if not req.client_order_id:
            raise ValueError("HTX 下单必须带 client_order_id（幂等和消解未知状态全靠它）")
        inst = await self._instrument(req.symbol)
        numeric_cid = self._numeric_cid(req.client_order_id)

        # 币 → 张：向零取整，余数直接丢。这就是 HTX 腿数量粒度离散的根源，
        # 上层收敛逻辑必须容忍一个 contract_size 的残差，别死循环追平
        volume = int((abs(req.qty) / inst.contract_size)
                     .to_integral_value(rounding=ROUND_FLOOR))
        if volume <= 0:
            raise OrderRejected(
                self.venue.value, "LOCAL_MIN_QTY",
                f"{req.qty} {inst.base} 不足一张（contract_size={inst.contract_size}）",
                retriable=False,
            )

        side = req.side.lower()
        body: dict[str, Any] = {
            "contract_code": req.symbol,
            "direction": side,
            # 单向持仓模式：offset 只能填 both。对冲模式要按 open/close 填，
            # 那时 reduce_only 反而失效 —— TODO: 支持对冲模式需要先查 position_mode
            "offset": "both",
            "lever_rate": self._leverage.get(req.symbol, self.default_leverage),
            "volume": volume,
            "client_order_id": numeric_cid,
        }
        if req.price is None:
            body["order_price_type"] = "market"
        else:
            body["order_price_type"] = PRICE_TYPE_BY_TIF.get(
                req.time_in_force.upper(), "limit")
            # 价格必须是 price_tick 的整数倍，tick 小到 1e-10，只能用 Decimal 量化
            body["price"] = str(self._quantize_price(req.price, inst.tick_size, side))
        if req.reduce_only:
            body["reduce_only"] = 1
        self._inject_broker(body)

        path = f"/linear-swap-api/v1/{self._p}order"
        try:
            data = await self._request("POST", path, quota="trade", signed=True, body=body)
        except (OrderRejected, RateLimited):
            # 明确被拒 / 被限频 = 撮合肯定没收到这张单，可以安全当作失败
            raise
        except ExchangeError as exc:
            # 超时、连接中断、5xx、响应读不出来 —— 单子可能已经进撮合了。
            # 绝不能当失败返回：上层会重发，直接双倍仓位。
            if exc.code == "NETWORK" or exc.code == "BAD_JSON" or exc.code.startswith("HTTP_5"):
                raise OrderUnknown(self.venue.value, req.client_order_id) from exc
            raise
        except httpx.TransportError as exc:
            # 基类只接了 Timeout/NetworkError，RemoteProtocolError（ProtocolError 分支，
            # 不是 NetworkError 的子类）会漏到这里。六家统一用 TransportError 兜底。
            raise OrderUnknown(self.venue.value, req.client_order_id) from exc

        row = self._first_row(data) or {}
        # 下单响应只回 order_id，不带成交信息；IOC/FOK 的实际成交量要靠 query_order 读
        return OrderResult(
            client_order_id=req.client_order_id,
            exchange_order_id=str(row.get("order_id_str") or row.get("order_id") or ""),
            status="new",
            filled_qty=ZERO,
            avg_price=ZERO,
        )

    async def cancel_order(self, symbol: str, client_order_id: str) -> None:
        """撤单 —— 状态终结操作，消解 OrderUnknown 的第一步。

        坑：HTX 撤单即使整体 status=ok，逐笔失败也藏在 data.errors 里，
        不逐笔检查就会把"订单不存在"当成撤单成功。
        """
        body = {"contract_code": symbol,
                "client_order_id": str(self._numeric_cid(client_order_id))}
        try:
            data = await self._request("POST", f"/linear-swap-api/v1/{self._p}cancel",
                                       quota="risk", signed=True, body=body)
            for err in (self._first_row(data) or {}).get("errors") or []:
                raise self._translate(err.get("err_code"), str(err.get("err_msg", "")))
        except ExchangeError as exc:
            raise self._risk_path_error(exc) from exc

    def _risk_path_error(self, exc: ExchangeError) -> ExchangeError:
        """风控路径（撤单/查单）上的错误重分类。

        base._request 把 httpx 的 Timeout/NetworkError 包成了 ExchangeError('NETWORK')，
        而 base.resolve_unknown_order 只认 (OrderRejected, httpx.HTTPError, RateLimited)
        —— 不转一手的话，网络抖动或 5xx 会让消解流程在最危险的时刻带着未捕获异常崩掉。
        可重试的挂到 RateLimited（code 保留）；不可重试的原样上抛，绝不降级成
        OrderRejected —— 那等于谎报"成交量为 0"。
        """
        if isinstance(exc, (OrderRejected, RateLimited)) or not exc.retriable:
            return exc
        return RateLimited(self.venue.value, exc.code, exc.message, retriable=True)

    async def query_order(self, symbol: str, client_order_id: str) -> OrderResult:
        body = {"contract_code": symbol,
                "client_order_id": str(self._numeric_cid(client_order_id))}
        try:
            data = await self._request("POST", f"/linear-swap-api/v1/{self._p}order_info",
                                       quota="risk", signed=True, body=body)
        except ExchangeError as exc:
            raise self._risk_path_error(exc) from exc
        rows = data if isinstance(data, list) else [data] if data else []
        rows = [r for r in rows if r]
        if not rows:
            # 交易所根本不认识这张单 → OrderRejected，
            # resolve_unknown_order 据此判定成交量为 0
            raise OrderRejected(self.venue.value, "1061",
                                f"{symbol} 无此订单: {client_order_id}", retriable=False)
        row = rows[0]
        inst = await self._instrument(symbol)
        return self._to_order_result(row, inst, fallback_cid=client_order_id)

    def _to_order_result(self, row: Mapping[str, Any], inst: Instrument,
                         fallback_cid: str = "") -> OrderResult:
        status = ORDER_STATUS.get(int(_dec(row.get("status") or 0)), "new")
        cid = row.get("client_order_id")
        return OrderResult(
            client_order_id=self._restore_cid(cid) if cid not in (None, "") else fallback_cid,
            exchange_order_id=str(row.get("order_id_str") or row.get("order_id") or ""),
            status=status,
            # 成交量同样是张，换回币本位
            filled_qty=_dec(row.get("trade_volume")) * inst.contract_size,
            avg_price=_dec(row.get("trade_avg_price")),
            fee=_dec(row.get("fee")),
            fee_asset=str(row.get("fee_asset") or ""),
        )

    async def fetch_positions(self, symbols: Sequence[str] | None = None) -> Sequence[Position]:
        """持仓 —— 真值。本地记账与它冲突时无条件以它为准。

        对冲模式下同一合约会返回多空两条，这里按合约净额合并：
        delta 中性工具关心的是净敞口，不是两条腿的毛数量。
        """
        await self._ensure_instruments()
        body: dict[str, Any] = {}
        if symbols and len(symbols) == 1:
            body["contract_code"] = symbols[0]
        data = await self._request("POST", f"/linear-swap-api/v1/{self._p}position_info",
                                   quota="risk", signed=True, body=body)
        wanted = set(symbols) if symbols else None
        agg: dict[str, dict[str, Any]] = {}
        for row in data or []:
            code = str(row.get("contract_code") or "")
            if wanted is not None and code not in wanted:
                continue
            inst = self._instruments.get(code)
            size = inst.contract_size if inst else Decimal("1")
            qty = _dec(row.get("volume")) * size
            if str(row.get("direction", "")).lower() == "sell":
                qty = -qty
            slot = agg.setdefault(code, {
                "qty": ZERO, "cost": ZERO, "abs": ZERO,
                "mark": ZERO, "lev": Decimal("1"),
            })
            slot["qty"] += qty
            slot["abs"] += abs(qty)
            slot["cost"] += abs(qty) * _dec(row.get("cost_open"))
            slot["mark"] = _dec(row.get("last_price")) or slot["mark"]
            slot["lev"] = _dec(row.get("lever_rate")) or slot["lev"]

        out: list[Position] = []
        for code, slot in agg.items():
            entry = slot["cost"] / slot["abs"] if slot["abs"] > 0 else ZERO
            out.append(Position(
                market=self.market, symbol=code,
                qty=slot["qty"], entry_price=entry, mark_price=slot["mark"],
                # v1 全仓持仓接口不返回强平价（全仓强平是**账户级**的，
                # 要从 swap_cross_account_info 的 risk_rate 推）。调研文档未覆盖，
                # 宁可返回 None 让风控知道"没有这个数"，也不编一个假的出来。
                # TODO: 接 swap_cross_account_info 补账户级强平信息
                liquidation_price=None,
                leverage=slot["lev"],
            ))
        return tuple(out)

    async def fetch_open_orders(self, symbol: str) -> Sequence[OrderResult]:
        inst = await self._instrument(symbol)
        out: list[OrderResult] = []
        page, total_page = 1, 1
        while page <= min(total_page, 20):
            data = await self._request(
                "POST", f"/linear-swap-api/v1/{self._p}openorders", quota="risk",
                signed=True, body={"contract_code": symbol,
                                   "page_index": page, "page_size": 50},
            )
            data = data if isinstance(data, dict) else {}
            total_page = int(_dec(data.get("total_page") or 1))
            rows = data.get("orders") or []
            if not rows:
                break
            out.extend(self._to_order_result(row, inst) for row in rows)
            page += 1
        return tuple(out)

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        """切杠杆。两条硬限制决定了杠杆是**启动参数不是运行时参数**：
        限频 1 次/3 秒，且只有该币种无持仓无挂单时才切得动。

        配额走 risk（六家统一）：safety.liq_distance_deleverage 触发的降杠杆是风控
        动作，宁可多占一点交易所限频，也不能在爆仓距离告急时排在行情/下单后面。
        """
        lever = int(leverage)
        await self._request("POST", f"/linear-swap-api/v1/{self._p}switch_lever_rate",
                            quota="risk", signed=True,
                            body={"contract_code": symbol, "lever_rate": lever})
        # 记下来：后续下单的 lever_rate 必须和持仓杠杆一致，不一致直接拒单
        self._leverage[symbol] = lever

    async def fetch_leverage_tiers(self, symbol: str) -> Sequence[tuple[Decimal, Decimal]]:
        """杠杆档位。

        HTX 没有公开「名义上限 → 最大杠杆」的阶梯接口：调研文档给到的
        swap_query_elements 只有 min_level/max_level，swap_cross_available_level_rate
        只有可用杠杆列表，两者都不含每一档的名义分界。所以这里退化成单档，
        用 Infinity 显式表示"名义上限未知"。
        TODO: 风控层不能靠这个做跨档边界预警，需要另找阶梯保证金接口。
        """
        data = await self._request("GET", "/linear-swap-api/v1/swap_query_elements",
                                   quota="market", params={"contract_code": symbol})
        row = self._first_row(data) or {}
        max_level = _dec(row.get("max_level") or 1)
        return ((Decimal("Infinity"), max_level),)
