"""币安适配器：USDⓈ-M 永续（fapi）为主，统一账户（papi）可切换。

三套 API 的分工（调研结论）：
  - 公开元数据/行情：fapi 的 exchangeInfo / fundingInfo / premiumIndex / fundingRate
    —— 即使跑在统一账户下，这些也只有 fapi 有，papi 不提供
  - 私有交易/持仓：默认 fapi，portfolio_margin=True 时切到 papi 的 /um/ 系列
  - 现货 api：调研文档只覆盖了「返佣码塞 newClientOrderId」这一条，
    没有端点/精度/费率的调研，见 _reject_unsupported_market 的说明

返佣：币安走**订单ID前缀**（x-LinkID 拼在 newClientOrderId 最前面），
既不是 header 也不是独立字段。broker_code 为空时 newClientOrderId 里
一个字符都不加，请求与不配返佣码时完全一致。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import re
import time
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, AsyncIterator, Mapping, Sequence
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
    TradeFill,
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

FAPI_BASE = "https://fapi.binance.com"
PAPI_BASE = "https://papi.binance.com"

# bookTicker 在 /public 路由，markPrice 在 /market 路由 —— 2026-03-06 分流改造后
# 两者不在同一条连接上，旧的 /ws/ 直连地址已于 2026-04-23 下线
WS_PUBLIC_BASE = "wss://fstream.binance.com/public"

# 单连接 24 小时被强制断开。提前 1 小时主动重建，别等它断
WS_REBUILD_AFTER_S = 23 * 3600
WS_MAX_STREAMS_PER_CONN = 1024

# 不在 fundingInfo 里出现的 symbol = 默认配置 = 8 小时。
# 这个接口是「稀疏返回」语义，查不到不是不存在
DEFAULT_FUNDING_INTERVAL_S = 28800
FUNDING_INFO_TTL_S = 3600

# 真实世界里出现过的结算周期。反推周期时只认这个白名单：
# 漏一条 8h 数据会造出 16h 的相邻间隔，当成周期就是把年化凭空腰斩，还不报错。
PLAUSIBLE_FUNDING_INTERVALS_S = frozenset(h * 3600 for h in (1, 2, 3, 4, 6, 8, 12, 24))
# 反推周期只看最近这么多个差分——周期是会中途改的，看太远等于把旧制度算进来
INTERVAL_PROBE_WINDOW = 8
# 历史翻页硬闸。1000 条/页，32 页足够任何合约的完整历史，
# 同时挡住"服务端无视 startTime"时的无限翻页
HISTORY_MAX_PAGES = 32

# userTrades 的 startTime/endTime 窗口上限：官方规定不得超过 7 天，
# 超了直接报 -1127。翻整段历史必须按窗切片
USER_TRADES_WINDOW_MS = 7 * 86_400_000

# 保持 5000 并主动校时，不靠调大 recvWindow 掩盖漂移——
# 那会让延迟到达的撤单在你以为它超时之后才生效
RECV_WINDOW_MS = 5000

# ⚠ 调研文档 exchange-binance.md 没有覆盖 REST 深度端点（只在 WS 章节提了
# <symbol>@depth<levels>）。下面的合法档位集合与权重阶梯来自币安官方 fapi 文档口径，
# 不是本仓调研结论。上线前必须用一次真实响应核对：档位数看是否被拒（-1130），
# 权重看响应头 X-MBX-USED-WEIGHT-1M 的增量。写错的后果是限频误判而不是算错钱，
# 所以没有像 _reject_unsupported_market 那样直接拒绝实现。
# limit 只接受这几个离散值，传别的直接报错；IP 权重按档位阶梯计价：
#   5/10/20/50 → 2，100 → 5，500 → 10，1000 → 20
DEPTH_LIMITS = (5, 10, 20, 50, 100, 500, 1000)
# 50 档与 5 档同为权重 2，没有任何理由取少
DEFAULT_DEPTH_LIMIT = 50

# 官方 pattern：^[\.A-Z\:/a-z0-9_-]{1,36}$（fapi）。papi 的 CM 单已确证是 32，
# UM 单没抓到原文，保守也按 32
CID_CHARSET = re.compile(r"^[\.A-Z\:/a-z0-9_-]+$")
CID_MAX_FAPI = 36
CID_MAX_PAPI = 32

# 这三个码的语义就是「执行状态未知」：请求可能已经进撮合引擎。
# 绝不能当作失败返回，必须走 OrderUnknown 的消解流程。
# -1006 UNEXPECTED_RESP 的官方文案就是 "Execution status unknown"，与 -1007 同语义
_UNKNOWN_STATE_CODES = {"-1007", "-1001", "-1006"}
_RATE_LIMIT_CODES = {"-1003", "-1015"}
# -1021 时间戳过期：重新校时后可以安全重发（订单肯定没进撮合）
_RETRIABLE_CODES = {"-1021"}

_STATUS_MAP = {
    "NEW": "new",
    "PARTIALLY_FILLED": "partial",
    "FILLED": "filled",
    "CANCELED": "canceled",
    "EXPIRED": "canceled",
    "EXPIRED_IN_MATCH": "canceled",
    "REJECTED": "rejected",
    "NEW_INSURANCE": "new",
    "NEW_ADL": "new",
}

ZERO = Decimal("0")


def _fmt_decimal(d: Decimal) -> str:
    """Decimal 转上链字符串：去科学计数法、去尾随零。

    "1E-3" 会被币安直接拒掉（-1111），normalize() 又会把 100 变成 1E+2，
    所以必须再用 format(..., "f") 拆回定点形式。
    """
    if d == 0:
        return "0"
    return format(d.normalize(), "f")


def _wire(v: Any) -> str:
    if isinstance(v, Decimal):
        return _fmt_decimal(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _dec(v: Any) -> Decimal:
    """任何交易所字段转 Decimal。JSON 已用 parse_float=Decimal 解析，
    所以这里绝不会有 float 中转。"""
    if isinstance(v, Decimal):
        return v
    if v is None or v == "":
        return ZERO
    return Decimal(str(v))


def _b36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(digits[r])
    return "".join(reversed(out))


# 千倍前缀：1000PEPEUSDT 一张的标的是 1000 个 PEPE，现货符号是 PEPEUSDT。
# 不解析这个乘数，跨腿对冲比例会差 1000 倍
_MULTIPLIER_PREFIX = re.compile(r"^(1000000|100000|10000|1000)(?=[A-Z])")


def _split_multiplier(base_asset: str) -> tuple[str, Decimal]:
    m = _MULTIPLIER_PREFIX.match(base_asset)
    if not m:
        return base_asset, Decimal("1")
    return base_asset[m.end():], Decimal(m.group(1))


class BinanceAdapter(ExchangeAdapter):
    """币安 USDⓈ-M 永续适配器。

    portfolio_margin=True 时私有路径切到统一账户 papi：限频数值不同、
    newClientOrderId 上限 32、且 papi 下 Link 返佣是否被统计官方无任何说明，
    要跑返佣就别开这个开关。
    """

    venue = Venue.BINANCE
    rest_base = FAPI_BASE

    def __init__(self, credential: Credential, *, broker_code: str = "",
                 client: httpx.AsyncClient | None = None,
                 market_kind: MarketKind = MarketKind.PERP,
                 portfolio_margin: bool = False,
                 recv_window_ms: int = RECV_WINDOW_MS) -> None:
        # 用户从网页复制返佣码常带尾随空白/换行，六家统一在构造期剥掉
        broker_code = broker_code.strip()
        super().__init__(credential, broker_code=broker_code, client=client)
        if market_kind is not MarketKind.PERP:
            self._reject_unsupported_market(market_kind)
        self.market_kind = market_kind
        self.market = f"{self.venue.value}:{market_kind.value}"
        self.portfolio_margin = portfolio_margin
        self.recv_window_ms = recv_window_ms

        # 返佣码：用户可能填完整前缀 "x-cvBPrNm9"，也可能只填 Link ID "cvBPrNm9"。
        # 官方要求最终 ID 以 "x-" + LinkID 开头，这里统一补齐；空则完全不注入
        self._broker_prefix = self._normalize_broker_code(broker_code)

        self._instruments: dict[str, Instrument] = {}
        # pricePrecision / quantityPrecision 是「小数位上限」，tickSize / stepSize 是「步长」，
        # 两个条件都要满足，Instrument 装不下精度位数，只能单独缓存
        self._precision: dict[str, tuple[int, int]] = {}
        self._funding_interval_s: dict[str, int] = {}
        self._funding_limits: dict[str, tuple[Decimal, Decimal]] = {}
        self._funding_info_at = 0.0
        self._fee_cache: dict[str, FeeSchedule] = {}
        # BNB 手续费抵扣开关。None = 还没查过（查一次就够，用户不会边跑边切）
        self._fee_burn: bool | None = None
        self._cid_seq = itertools.count()
        self._in_clock_sync = False
        # fundingRate 单次上限 1000 条（测试里调小以验证翻页）
        self._history_page_size = 1000
        # userTrades 单页上限同为 1000（测试里调小以验证翻页）
        self._trades_page_size = 1000
        # 深度取多少档（测试里调小以验证"档位不足不外推"）
        self._depth_limit = DEFAULT_DEPTH_LIMIT

        # 响应头里的实时用量，引擎据此主动退避（429 之前就该减速）
        self.used_weight_1m = 0
        self.order_count_1m = 0
        self.order_count_10s = 0
        self.retry_after_s = 0

    # ---- 市场支持范围 ---------------------------------------------------

    @staticmethod
    def _reject_unsupported_market(kind: MarketKind) -> None:
        raise NotImplementedError(
            f"binance:{kind.value} 未实现。调研文档 exchange-binance.md 只覆盖了现货的"
            " POST /api/v3/order（返佣码落点），缺 exchangeInfo 的 filters 语义、"
            "现货/杠杆费率端点、杠杆借币利率与可借额度、逐仓/全仓账户端点，"
            "凭记忆补这些会写错精度和利息符号。补齐调研后再实现。"
        )

    # ---- 返佣码 ---------------------------------------------------------

    @staticmethod
    def _normalize_broker_code(code: str) -> str:
        if not code:
            return ""          # 空 = 完全不注入，不加前缀不加 header
        prefix = code if code.startswith("x-") else f"x-{code}"
        # 大小写敏感，LinkID 原样照抄不要 upper()
        if not CID_CHARSET.match(prefix):
            raise ValueError(
                f"返佣码含非法字符: {code!r}；newClientOrderId 只允许 A-Za-z0-9 和 . : / _ -"
            )
        return prefix

    @property
    def _cid_max_len(self) -> int:
        return CID_MAX_PAPI if self.portfolio_margin else CID_MAX_FAPI

    def make_client_order_id(self, tag: str = "") -> str:
        """生成带返佣前缀的 client order id。

        唯一性是硬约束：官方原话「同 ID 的订单只有在上一单已成交时才会被接受，
        否则直接拒单」。500ms 收敛循环里毫秒时间戳会撞，所以再拼一个自增计数。
        """
        seq = _b36(next(self._cid_seq) % 1296).rjust(2, "0")
        raw = tag or f"{_b36(self.timestamp_ms())}{seq}"
        return self._build_client_order_id(raw)

    def _build_client_order_id(self, tag: str) -> str:
        prefix = self._broker_prefix
        # 幂等：撤单/查单拿到的 ID 可能已经带过前缀，二次拼接会拼出交易所不认识的
        # 第三种 ID —— 那正是 resolve_unknown_order 查无此单、误判成"零成交"的根源
        if prefix and tag.startswith(prefix):
            return tag
        budget = self._cid_max_len - len(prefix)
        if budget <= 0:
            raise ValueError(
                f"返佣前缀 {prefix!r} 占满了 {self._cid_max_len} 字符预算，没有位置放唯一性 tag"
            )
        # 截尾只能截自己的 tag，绝不能截前缀——截了前缀返佣归属就断了
        cid = f"{prefix}{tag[:budget]}"
        if not CID_CHARSET.match(cid):
            raise ValueError(f"非法 newClientOrderId: {cid!r}")
        return cid

    # ---- 签名 -----------------------------------------------------------

    @staticmethod
    def _encode(params: Mapping[str, Any]) -> str:
        """唯一的序列化入口。签名的串和真正发出去的字节必须一模一样，
        所以签名和发送都只能过这一个函数（签一份发另一份 = 稳定的 -1022）。"""
        return urlencode([(k, _wire(v)) for k, v in params.items()])

    def total_params(self, query: Mapping[str, Any] | str,
                     body: Mapping[str, Any] | str = "") -> str:
        """官方定义的 totalParams = query string 拼接 request body，按出现顺序原样相连。"""
        q = query if isinstance(query, str) else self._encode(query)
        b = body if isinstance(body, str) else self._encode(body)
        return f"{q}{b}"

    def _prepare(self, method: str, path: str, params: dict, body: dict,
                 signed: bool) -> tuple[dict[str, str], str, dict, dict]:
        """构造签名后的 headers / url / params / body。

        约定：GET/DELETE 全部参数走 query，POST/PUT 全部走 body（官方建议，
        混放时签名是 query+body 拼接但取值优先 query，极易签出对不上的串）。
        返回的 url 已经带好完整 query string，params 恒为空 —— 这样发出去的
        字节与签名的字节按构造就是同一份。
        """
        base = PAPI_BASE if path.startswith("/papi") else FAPI_BASE
        use_body = method in ("POST", "PUT")

        container: dict[str, Any] = {}
        container.update(params)
        container.update(body)

        headers: dict[str, str] = {}
        if signed:
            headers["X-MBX-APIKEY"] = self._cred.api_key
            container["recvWindow"] = self.recv_window_ms
            container["timestamp"] = self.timestamp_ms()
            # signature 必须在最末尾
            container["signature"] = self._hmac_sha256_hex(self._encode(container))

        if use_body:
            return headers, f"{base}{path}", {}, container
        query = self._encode(container)
        url = f"{base}{path}?{query}" if query else f"{base}{path}"
        return headers, url, {}, {}

    async def _request(self, method: str, path: str, *, quota: str = "market",
                       signed: bool = False, params: Mapping[str, Any] | None = None,
                       body: Mapping[str, Any] | None = None,
                       extra_headers: Mapping[str, str] | None = None) -> Any:
        """覆写基类：币安是 form-urlencoded 不是 json，且网络异常必须原样抛出。

        基类把 httpx 异常吞成 ExchangeError("NETWORK")，但 resolve_unknown_order
        的重试循环捕获的是 httpx.HTTPError —— 吞掉之后消解流程会直接崩。
        这里保留原始异常，由 place_order 单独翻译成 OrderUnknown。
        """
        # 基类的 _request 在 fetch_server_time_ms 里会递归校时，这里加锁打断
        if self._clock.is_stale and not self._in_clock_sync:
            self._in_clock_sync = True
            try:
                await self.sync_clock()
            except Exception:
                pass          # 校时失败不阻塞业务，drift 上会体现出来
            finally:
                self._in_clock_sync = False

        async with self._quota.get(quota, self._quota["market"]):
            headers, url, _q, send_body = self._prepare(
                method, path, dict(params or {}), dict(body or {}), signed
            )
            content = self._encode(send_body) if send_body else None
            if content is not None:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            if extra_headers:
                headers = {**headers, **extra_headers}
            resp = await self._client.request(method, url, content=content, headers=headers)
            return self._parse(resp)

    def _parse(self, resp: httpx.Response) -> Any:
        """解析响应并把币安错误码翻成统一异常。"""
        h = resp.headers
        self.used_weight_1m = int(h.get("X-MBX-USED-WEIGHT-1M", self.used_weight_1m) or 0)
        self.order_count_1m = int(h.get("X-MBX-ORDER-COUNT-1M", self.order_count_1m) or 0)
        self.order_count_10s = int(h.get("X-MBX-ORDER-COUNT-10S", self.order_count_10s) or 0)
        if "Retry-After" in h:
            self.retry_after_s = int(h.get("Retry-After") or 0)

        status = resp.status_code
        bad_json = False
        try:
            # parse_float=Decimal：币安数值多为字符串，但偶有裸数字字面量，
            # 这道闸保证任何路径都不会出现 float
            data = json.loads(resp.text, parse_float=Decimal) if resp.text else {}
        except ValueError:
            data = {}
            bad_json = True

        code = str(data.get("code")) if isinstance(data, dict) and "code" in data else ""
        msg = str(data.get("msg", "")) if isinstance(data, dict) else ""

        if status == 429:
            raise RateLimited(self.venue.value, code or "429", msg or "请求超限", retriable=True)
        if status == 418:
            # IP 已被封禁，Retry-After 给解封时间。立刻重试只会延长封禁
            raise RateLimited(self.venue.value, "418",
                              f"IP 已被封禁，{self.retry_after_s}s 后解封", retriable=False)
        if status >= 500:
            # 5xx 对**下单**意味着状态未知；place_order 会把它翻成 OrderUnknown
            raise ExchangeError(self.venue.value, f"HTTP_{status}",
                                msg or resp.text[:200], retriable=True)
        if bad_json and status < 400:
            # 2xx 但响应体读不出来（WAF/代理注入 HTML 是典型场景）：
            # 这单有没有进撮合完全不知道。绝不能退化成空 dict —— 那会让
            # _order_result({}) 凭空造出一张"已受理、零成交"的假回执，
            # 比抛任何异常都危险。统一成 BAD_JSON，由 place_order 翻成 OrderUnknown。
            raise ExchangeError(self.venue.value, "BAD_JSON",
                                f"响应不是合法 JSON: {resp.text[:200]}", retriable=True)

        # 网关/WAF 的通用错误信封：HTTP 200 + {"status":"ERROR","type":"GENERAL",
        # "code":"99099990","errorData":"illegal params.","data":null}。
        # 它的 code 不以负号开头，只按负号判错会**原样放行**这个 dict：
        # 历史接口那边 `data if isinstance(data, list) else []` 把它变成空序列，
        # 静默冒充"这个币没有历史"；下单那边更糟——"ERROR" 不在 _STATUS_MAP 里
        # 会被映射成 "new"、executedQty 缺失记 0，凭空造出一张"已受理、零成交"
        # 的假回执，正是 BAD_JSON 那段注释极力防范的后果，只是从另一扇门进来。
        if isinstance(data, dict) and str(data.get("status", "")).upper() == "ERROR" \
                and ("code" in data or "errorData" in data):
            detail = str(data.get("errorData") or msg or data.get("type") or "网关拒绝")
            raise ExchangeError(self.venue.value, code or "GATEWAY_ERROR", detail,
                                retriable=False)

        # 成功响应也可能带 code（如 marginType 返回 {"code":200,"msg":"success"}），
        # 只有负数才是错误
        if code.startswith("-"):
            raise self._translate(code, msg)
        if status >= 400:
            raise ExchangeError(self.venue.value, code or f"HTTP_{status}",
                                msg or resp.text[:200], retriable=False)
        return data

    def _translate(self, code: str, msg: str) -> ExchangeError:
        if code in _RATE_LIMIT_CODES:
            return RateLimited(self.venue.value, code, msg, retriable=True)
        if code in _UNKNOWN_STATE_CODES:
            return ExchangeError(self.venue.value, code, msg, retriable=False)
        if code in _RETRIABLE_CODES:
            return ExchangeError(self.venue.value, code, msg, retriable=True)
        try:
            n = int(code)
        except ValueError:
            return ExchangeError(self.venue.value, code, msg, retriable=False)
        # 币安错误码分族：-11xx 参数/请求问题，-20xx 订单被拒，-4xxx 合约业务拒绝。
        # 这三族都是「明确拒绝、不会成交」，可以安全重试（改正后）
        if -1199 <= n <= -1100 or -2099 <= n <= -2010 or -4999 <= n <= -4000:
            return OrderRejected(self.venue.value, code, msg, retriable=False)
        return ExchangeError(self.venue.value, code, msg, retriable=False)

    # ---- 路径（fapi / papi 切换）----------------------------------------

    @property
    def _order_path(self) -> str:
        return "/papi/v1/um/order" if self.portfolio_margin else "/fapi/v1/order"

    @property
    def _open_orders_path(self) -> str:
        # 调研文档没列 papi 的挂单端点，按 /um/ 前缀规律推导，上线前需核对
        return "/papi/v1/um/openOrders" if self.portfolio_margin else "/fapi/v1/openOrders"

    @property
    def _position_path(self) -> str:
        return "/papi/v1/um/positionRisk" if self.portfolio_margin else "/fapi/v3/positionRisk"

    @property
    def _leverage_path(self) -> str:
        return "/papi/v1/um/leverage" if self.portfolio_margin else "/fapi/v1/leverage"

    @property
    def _user_trades_path(self) -> str:
        # papi 端点按 /um/ 前缀规律推导（同 _open_orders_path 的处境），上线前需核对
        return "/papi/v1/um/userTrades" if self.portfolio_margin else "/fapi/v1/userTrades"

    # ---- 元数据 ---------------------------------------------------------

    async def fetch_server_time_ms(self) -> int:
        data = await self._request("GET", "/fapi/v1/time", quota="market")
        return int(data["serverTime"])

    async def fetch_instruments(self) -> Sequence[Instrument]:
        data = await self._request("GET", "/fapi/v1/exchangeInfo", quota="market")
        await self._load_funding_info()

        out: list[Instrument] = []
        for s in data.get("symbols", []):
            # 只看符号后缀不可靠（交割合约形如 BTCUSDT_250926），必须用 contractType
            if s.get("contractType") != "PERPETUAL" or s.get("status") != "TRADING":
                continue
            filters = {f.get("filterType"): f for f in s.get("filters", [])}
            price_f = filters.get("PRICE_FILTER", {})
            lot_f = filters.get("LOT_SIZE", {})
            notional_f = filters.get("MIN_NOTIONAL", {})
            symbol = s["symbol"]
            base, multiplier = _split_multiplier(str(s.get("baseAsset", "")))

            inst = Instrument(
                market=self.market,
                symbol=symbol,
                base=base,
                quote=str(s.get("quoteAsset", "")),
                # 对外一律 base 币口径（与 OKX/HTX/Gate 一致）：
                # 交易所把 1000PEPEUSDT 的价格标成"每 1000 个 PEPE"、数量标成"张"，
                # 两个轴都要除/乘这个倍数，否则 mark×qty 算出来的名义额差 1000 倍。
                tick_size=_dec(price_f.get("tickSize")) / multiplier,
                lot_size=_dec(lot_f.get("stepSize")) * multiplier,
                min_notional=_dec(notional_f.get("notional")),
                # 1000PEPEUSDT 的一「张」= 1000 个 PEPE，base 归一化成 PEPE
                # 才能和别家的 PEPE 腿配对，换算靠 contract_size
                contract_size=multiplier,
                # exchangeInfo 不给最大杠杆，要 fetch_leverage_tiers（signed）单独查，
                # 这里保持默认值，不编造
                max_leverage=Decimal("1"),
                funding_interval_s=self._funding_interval_s.get(
                    symbol, DEFAULT_FUNDING_INTERVAL_S),
            )
            out.append(inst)
            self._instruments[symbol] = inst
            self._precision[symbol] = (int(s.get("pricePrecision", 8)),
                                       int(s.get("quantityPrecision", 8)))
        return tuple(out)

    async def _load_funding_info(self, force: bool = False) -> None:
        """fundingInfo 只返回「改过默认值」的 symbol。

        查不到 ≠ 不存在，而是「默认 8 小时 + 默认 cap/floor」。写成查不到就跳过，
        会把绝大多数合约全过滤掉。
        """
        if not force and time.time() - self._funding_info_at < FUNDING_INFO_TTL_S:
            return
        data = await self._request("GET", "/fapi/v1/fundingInfo", quota="market")
        for item in data or []:
            symbol = item["symbol"]
            hours = int(item.get("fundingIntervalHours", 8))
            self._funding_interval_s[symbol] = hours * 3600
            cap, floor = item.get("adjustedFundingRateCap"), item.get("adjustedFundingRateFloor")
            if cap is not None and floor is not None:
                self._funding_limits[symbol] = (_dec(cap), _dec(floor))
        self._funding_info_at = time.time()

    async def fetch_fee_burn(self) -> bool | None:
        """BNB 手续费抵扣是否开着。查不到返回 None（权限不足/接口变更都算查不到）。

        单查一次就缓存：这是账户设置，不会在一次运行里被改。
        """
        if self._fee_burn is None:
            try:
                data = await self._request("GET", "/fapi/v1/feeBurn",
                                           quota="market", signed=True)
                self._fee_burn = bool(data.get("feeBurn"))
            except Exception:
                return None
        return self._fee_burn

    async def fetch_fee_schedule(self, symbols: Sequence[str]) -> Sequence[FeeSchedule]:
        """权重 20，很贵，启动时查一次缓存住，绝不能放进循环。

        不传 symbol 时返回账户默认档（币安合约费率本来就是账户级的），
        这一条的 FeeSchedule.symbol 是空串，上层拿它兜底整个市场。

        **BNB 抵扣只报状态不打折。** GET /fapi/v1/feeBurn 只给一个布尔开关，
        折扣幅度不在任何 API 里（挂在帮助中心的文案上，随时可改）。拿一个记忆里的
        百分比去削成本，等于在这个工具唯一的判决变量上凭空造数——低估成本会让
        负期望的机会看起来能做，正是整个项目要修的病。所以这里把状态写进 note
        交给用户，实际扣费只会比显示的更低，方向是安全的。
        """
        burn = await self.fetch_fee_burn()
        note = ("已开 BNB 抵扣，实际扣费低于此处显示（折扣幅度 API 不给，未计入）"
                if burn else "")
        out: list[FeeSchedule] = []
        for symbol in symbols or [""]:
            cached = self._fee_cache.get(symbol)
            if cached is None:
                params = {"symbol": symbol} if symbol else {}
                data = await self._request("GET", "/fapi/v1/commissionRate",
                                           quota="market", signed=True, params=params)
                maker_raw = data.get("makerCommissionRate")
                taker_raw = data.get("takerCommissionRate")
                # 字段缺失时**跳过这一条**，绝不补零：零手续费会让四笔 taker 成本
                # 凭空消失，负期望的机会全部翻成正期望，而且不报任何错
                if maker_raw in (None, "") or taker_raw in (None, ""):
                    continue
                cached = FeeSchedule(
                    market=self.market, symbol=symbol,
                    maker=_dec(maker_raw), taker=_dec(taker_raw), note=note,
                )
                self._fee_cache[symbol] = cached
            out.append(cached)
        return tuple(out)

    async def fetch_funding_limits(self, symbol: str) -> tuple[Decimal, Decimal] | None:
        """返回 (cap, floor)。None = 该合约用默认档，接口不给数值。"""
        await self._load_funding_info()
        return self._funding_limits.get(symbol)

    # ---- 行情 -----------------------------------------------------------

    async def fetch_book_top(self, symbol: str) -> BookTop:
        data = await self._request("GET", "/fapi/v1/ticker/bookTicker",
                                   quota="market", params={"symbol": symbol})
        if isinstance(data, list):        # 不传 symbol 时是数组，这里理论上不会走到
            data = next((d for d in data if d.get("symbol") == symbol), {})
        return self._book_top(data, self._multiplier(symbol))

    @staticmethod
    def _book_top(d: Mapping[str, Any], multiplier: Decimal = Decimal("1")) -> BookTop:
        # REST 用长字段名，WS bookTicker 用 b/B/a/A —— 两种都认，
        # 免得流和轮询各写一份解析。
        # multiplier：把"每张"的价与量换算成"每 base 币"，1000PEPEUSDT 那一族才对得上
        bid = d.get("bidPrice", d.get("b"))
        ask = d.get("askPrice", d.get("a"))
        ts = d.get("time", d.get("T", d.get("E", 0)))
        return BookTop(
            bid_price=_dec(bid) / multiplier,
            bid_qty=_dec(d.get("bidQty", d.get("B"))) * multiplier,
            ask_price=_dec(ask) / multiplier,
            ask_qty=_dec(d.get("askQty", d.get("A"))) * multiplier,
            ts_ms=int(ts or 0),
        )

    async def fetch_book_depth(self, symbol: str, band_bp: float = 10.0) -> BookDepth:
        """band_bp 基点带宽内的可见深度（名义额，quote 计价）。

        端点 GET /fapi/v1/depth（公开，不签名，走 quota="market"）。
        **IP 权重按档位阶梯**：limit 5/10/20/50 → 2，100 → 5，500 → 10，1000 → 20。
        默认取 50 档（权重 2，和取 5 档一个价）。2400/min 的 IP 配额下够全市场轮询，
        但它比 bookTicker 贵一倍，**不要放进 500ms 收敛循环**——那条路上只用 WS。

        以中价为基准，买卖两侧各统计 [mid*(1-band), mid] 和 [mid, mid*(1+band)]
        内的累计名义额（含边界）。50 档还没铺满带宽时只统计拿到的档位，
        levels_used 会等于返回的总档数——宁可低估也不外推补足。
        """
        if band_bp <= 0:
            raise ValueError(f"band_bp 必须为正，收到 {band_bp}")
        data = await self._request(
            "GET", "/fapi/v1/depth", quota="market",
            params={"symbol": symbol, "limit": self._snap_depth_limit(self._depth_limit)},
        )
        return self._book_depth(symbol, data, band_bp, self._multiplier(symbol))

    @staticmethod
    def _snap_depth_limit(n: int) -> int:
        """向上取到最近的合法档位数。传非法值币安直接拒（-1130），本地先兜住。"""
        return next((v for v in DEPTH_LIMITS if n <= v), DEPTH_LIMITS[-1])

    @classmethod
    def _book_depth(cls, symbol: str, d: Mapping[str, Any], band_bp: float,
                    multiplier: Decimal = Decimal("1")) -> BookDepth:
        bids = d.get("bids")
        asks = d.get("asks")
        if bids is None or asks is None:
            # 2xx 但没有 bids/asks（打错端点、代理注入）绝不能退化成"深度 0"：
            # scoring 那边 depth_notional <= 0 会让整段容量上限判断被跳过，
            # 等于把"没数据"读成"深度无限"，直接按用户填的仓位放行。
            raise ExchangeError(cls.venue.value, "BAD_DEPTH",
                                f"{symbol} 深度响应缺少 bids/asks 字段")
        # T 是撮合时间、E 是消息发出时间，与 _book_top 保持同一口径优先取 T
        ts = int(d.get("T", d.get("E", 0)) or 0)

        top_bid_p = _dec(bids[0][0]) if bids else ZERO
        top_bid_q = _dec(bids[0][1]) if bids else ZERO
        top_ask_p = _dec(asks[0][0]) if asks else ZERO
        top_ask_q = _dec(asks[0][1]) if asks else ZERO
        if top_bid_p <= 0 or top_ask_p <= 0:
            # 单边空盘口（新上线/暂停撮合）：没有中价就没有带宽，如实报 0 档
            return BookDepth(
                symbol=symbol, bid_notional=ZERO, ask_notional=ZERO,
                bid_price=top_bid_p / multiplier, bid_qty=top_bid_q * multiplier,
                ask_price=top_ask_p / multiplier, ask_qty=top_ask_q * multiplier,
                levels_used=0, band_bp=band_bp, ts_ms=ts,
            )

        # band_bp 是 float（scoring 的口径），只在这一处转成 Decimal，之后全程 Decimal。
        # 走 str() 而不是 Decimal(float)，免得 10.0000000000000017 这种脏值渗进来
        band = Decimal(str(band_bp)) / Decimal("10000")
        mid = (top_bid_p + top_ask_p) / 2
        lo, hi = mid * (1 - band), mid * (1 + band)

        # 累计名义额在**交易所原生（张）口径**下做：对外要把价 ÷ 面值、量 × 面值，
        # 两者相乘正好抵消，名义额本身与面值无关。先换算再相乘只会多引入除法误差，
        # 而且是 1000PEPEUSDT 那族"把张数当币数"放大 1000 倍的高发地段。
        bid_notional = ZERO
        levels = 0
        for level in bids:
            # 买盘按价格降序，跌破带宽下沿之后不可能再有更高价，直接停
            # （上沿不用判：非交叉盘口的买价天然 <= mid）
            price = _dec(level[0])
            if price < lo:
                break
            bid_notional += price * _dec(level[1])
            levels += 1
        ask_notional = ZERO
        for level in asks:
            price = _dec(level[0])
            if price > hi:
                break
            ask_notional += price * _dec(level[1])
            levels += 1

        return BookDepth(
            symbol=symbol,
            bid_notional=bid_notional,
            ask_notional=ask_notional,
            bid_price=top_bid_p / multiplier,
            bid_qty=top_bid_q * multiplier,
            ask_price=top_ask_p / multiplier,
            ask_qty=top_ask_q * multiplier,
            levels_used=levels,
            band_bp=band_bp,
            ts_ms=ts,
        )

    async def fetch_carry_rates(self, symbols: Sequence[str] | None = None) -> Sequence[CarryRate]:
        """premiumIndex 的 lastFundingRate 是实时滚动的**预测**值，不是已结算值。

        符号用交易所原始约定：正 = 多头付给空头。原样写入，不取反
        （models.CarryRate 的约定；base.py 里那句"适配器要取负"的注释已过期）。
        """
        await self._load_funding_info()
        params: dict[str, Any] = {}
        if symbols and len(symbols) == 1:
            params["symbol"] = symbols[0]     # 带 symbol 权重 1，不带 10
        data = await self._request("GET", "/fapi/v1/premiumIndex",
                                   quota="market", params=params)
        rows = data if isinstance(data, list) else [data]
        wanted = set(symbols) if symbols else None

        out: list[CarryRate] = []
        for row in rows:
            symbol = row.get("symbol", "")
            if wanted is not None and symbol not in wanted:
                continue
            # 已加载过合约表就用它过滤掉交割合约；没加载就不擅自过滤
            if wanted is None and self._instruments and symbol not in self._instruments:
                continue
            out.append(CarryRate(
                market=self.market,
                symbol=symbol,
                rate=_dec(row.get("lastFundingRate")),
                interval_s=self._funding_interval_s.get(symbol, DEFAULT_FUNDING_INTERVAL_S),
                next_settle_ms=int(row.get("nextFundingTime", 0) or 0),
                ts_ms=int(row.get("time", 0) or 0),
            ))
        return tuple(out)

    async def fetch_funding_history(self, symbol: str, since_ms: int,
                                    limit: int = 1000) -> Sequence[tuple[int, Decimal]]:
        """历史资金费，升序返回 since_ms 至今的**完整**区间。

        币安的分页方向与其余五家相反，这里有两个非踩不可的坑：

        1. **服务端的截断方向是反的。** startTime~endTime 之间超过单次上限时，
           返回的是从 startTime 起**最旧**的那一批。旧实现攒够 limit 条就停，
           于是一个 4h 合约请求 180 天会拿回一段**停在 13 天前**的序列，
           而调用方完全看不出来——scoring 会把它当成此刻的稳定性。
           币安有 447 个 sub-8h 合约（占在交易永续的大多数），
           BTC 这类 8h 品种恰好不触发，所以单测和手工点检看不出来。
           修法是一路向前滚到窗口末端为止：limit 只当页大小用，不当返回上限。
        2. **startTime=0 会翻转语义。** 实测 0 被服务端当成"没传"，
           改走"返回最近记录"分支并把上限静默压到 500 条——
           同一个方法因参数值不同而行为反向。夹到 1 即可（1970 年没有永续）。

        翻页是把 startTime 推进到上一批最后一条 fundingTime+1 **向前**滚。
        startTime 含边界，所以 +1 恰好不重不漏；重叠了也有字典兜底去重。
        """
        collected: dict[int, Decimal] = {}
        page_size = max(1, min(int(limit) or self._history_page_size,
                               self._history_page_size))
        start = max(1, int(since_ms))
        pages = 0
        while pages < HISTORY_MAX_PAGES:
            data = await self._request(
                "GET", "/fapi/v1/fundingRate", quota="market",
                params={"symbol": symbol, "startTime": start, "limit": page_size},
            )
            pages += 1
            rows = data if isinstance(data, list) else []
            if not rows:
                break
            for row in rows:
                collected[int(row["fundingTime"])] = _dec(row.get("fundingRate"))
            if len(rows) < page_size:
                break                      # 不满一页 = 已经给完了
            nxt = max(int(r["fundingTime"]) for r in rows) + 1
            if nxt <= start:
                break                      # 游标不前进，再翻就是死循环
            start = nxt
        out = sorted(collected.items())
        self._refresh_interval_from_history(symbol, out)
        return tuple(out)

    def _refresh_interval_from_history(self, symbol: str,
                                       rows: Sequence[tuple[int, Decimal]]) -> None:
        """用相邻结算时刻的差分反推真实周期，**兜住 fundingInfo 缓存过期**。

        它是备胎不是主来源，所以第一件事是让位：fundingInfo 缓存还新鲜、
        而且明确给过这个 symbol 的周期时直接返回。早先那版无条件覆写，
        一个反推值能在整个 1 小时 TTL 窗口里压过权威值，进而污染
        fetch_carry_rates 的年化。

        真要反推时，差分不能直接采信，三层判据：

        1. **只认真实存在过的周期**。漏一条 8h 数据会造出 16h 的间隔，
           把它当周期就是把年化凭空腰斩，而且不报任何错。
        2. **重复出现的间隔才是真周期**。漏点造成的大间隔是孤立的，
           真周期会连着出现。早先那版取的是最后两个差分里**较大**的那个
           （`diffs[len(diffs)//2]` 在只有 2 个元素时是最大值，不是中位数）。
        3. **比常规周期更短的间隔立刻采信**。间隔只会因为漏点而变大，
           绝不会变小，所以一个偏小的间隔是切到更快周期的铁证——
           而这正是最该抓住的一侧：8h 切 1h 会让实收 carry 差 8 倍。
        """
        if (time.time() - self._funding_info_at < FUNDING_INFO_TTL_S
                and symbol in self._funding_interval_s):
            return
        if len(rows) < 3:
            return
        diffs = [(rows[i + 1][0] - rows[i][0]) // 1000
                 for i in range(len(rows) - 1)][-INTERVAL_PROBE_WINDOW:]
        plausible = [d for d in diffs if d in PLAUSIBLE_FUNDING_INTERVALS_S]
        if not plausible:
            return
        repeated = [d for d in plausible if plausible.count(d) >= 2]
        gap_s = repeated[-1] if repeated else sorted(plausible)[len(plausible) // 2]
        gap_s = min(gap_s, plausible[-1])
        if gap_s > 0:
            self._funding_interval_s[symbol] = gap_s

    def stream_book_top(self, symbols: Sequence[str]) -> AsyncIterator[BookTop]:
        """WebSocket 盘口流（最小可用版：组合流订阅 + 心跳 + 重连 + 24h 前主动重建）。

        TODO: (1) 用 payload 的 u（更新ID）做单调性校验检测丢包；
              (2) 资金费 markPrice 在 /market 路由，需要第二条连接，本方法只管 /public；
              (3) 超过 1024 个 stream 要拆多连接，目前直接报错而不是静默截断。
        """
        return self._stream_book_top(symbols)

    @staticmethod
    def _ws_stream_url(symbols: Sequence[str]) -> str:
        if not symbols:
            raise ValueError("stream_book_top 至少要一个 symbol")
        if len(symbols) > WS_MAX_STREAMS_PER_CONN:
            # 宁可报错也不静默截断——少订阅一条腿就是裸奔
            raise ValueError(f"单连接最多 {WS_MAX_STREAMS_PER_CONN} 个 stream，收到 {len(symbols)}")
        streams = "/".join(f"{s.lower()}@bookTicker" for s in symbols)
        return f"{WS_PUBLIC_BASE}/stream?streams={streams}"

    async def _stream_book_top(self, symbols: Sequence[str]) -> AsyncIterator[BookTop]:
        import websockets

        url = self._ws_stream_url(symbols)
        backoff = 1.0
        while True:
            try:
                # 服务端每 3 分钟发 ping，10 分钟收不到 pong 就断；
                # websockets 自动回 pong，这里再主动发 ping 做双保险
                async with websockets.connect(url, ping_interval=60, ping_timeout=30,
                                              close_timeout=5, max_queue=2048) as ws:
                    backoff = 1.0
                    rebuild_at = time.time() + WS_REBUILD_AFTER_S
                    while time.time() < rebuild_at:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            continue      # 冷门币静默期是正常的，靠 ping 保活
                        msg = json.loads(raw, parse_float=Decimal)
                        # 组合流外层包一层 {"stream":..., "data":...}
                        payload = msg.get("data", msg) if isinstance(msg, dict) else {}
                        if payload.get("e") != "bookTicker":
                            continue
                        yield self._book_top(payload,
                                             self._multiplier(str(payload.get("s", ""))))
            except asyncio.CancelledError:
                raise
            except Exception:
                # 反复被断会触发 IP ban，退避必须指数增长
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    # ---- 精度 -----------------------------------------------------------

    def _multiplier(self, symbol: str) -> Decimal:
        """符号里的千倍前缀（1000PEPEUSDT → 1000），未知符号退回 1。

        对外口径是 base 币，交易所原生口径是"张"，两个方向的换算全走它。
        """
        inst = self._instruments.get(symbol)
        if inst is None or inst.contract_size <= 0:
            return Decimal("1")
        return inst.contract_size

    def _round_qty(self, symbol: str, qty_coins: Decimal) -> Decimal:
        """base 币数量 → 交易所原生数量（张），按 stepSize 向零取整。"""
        inst = self._instruments.get(symbol)
        if inst is None:
            return qty_coins  # 没拉过合约表就原样发，让交易所来判（-1111 会暴露问题）
        mult = self._multiplier(symbol)
        out = _floor_to_step(qty_coins / mult, inst.lot_size / mult)
        prec = self._precision.get(symbol)
        if prec:
            out = _truncate(out, prec[1])
        return out

    def _round_price(self, symbol: str, price_per_coin: Decimal, side: str) -> Decimal:
        """每个 base 币的价格 → 交易所原生价格（每张），规整到 tickSize。

        方向与 executor.cross_price 一致：**买单向上、卖单向下**。保护限价本来就是
        故意穿透对手价的，反向取整会把穿透削回来；在 tick 相对价格很粗的小币上
        这不是少赚一个 tick，而是这条腿根本挂不上、永远补不平。
        """
        inst = self._instruments.get(symbol)
        if inst is None:
            return price_per_coin
        mult = self._multiplier(symbol)
        rounding = ROUND_UP if side == "buy" else ROUND_DOWN
        out = _round_to_step(price_per_coin * mult, inst.tick_size * mult, rounding)
        prec = self._precision.get(symbol)
        if prec:
            out = _truncate(out, prec[0])
        return out

    # ---- 交易 -----------------------------------------------------------

    async def place_order(self, req: OrderRequest) -> OrderResult:
        cid = (self._build_client_order_id(req.client_order_id)
               if req.client_order_id else self.make_client_order_id())
        qty = self._round_qty(req.symbol, req.qty)
        if qty <= 0:
            raise OrderRejected(self.venue.value, "LOCAL_PINCH",
                                f"{req.symbol} 数量按 lot_size 取整后为 0")

        body: dict[str, Any] = {
            "symbol": req.symbol,
            "side": "BUY" if req.side.lower() == "buy" else "SELL",
            "type": "LIMIT" if req.price is not None else "MARKET",
        }
        if req.price is not None:
            body["timeInForce"] = req.time_in_force
            body["price"] = self._round_price(req.symbol, req.price, req.side.lower())
        body["quantity"] = qty
        if req.reduce_only:
            # TODO: Hedge(双向)模式下 reduceOnly 不可用，要改成 positionSide + 反向单，
            # 收敛器需要为两种模式分别走分支
            body["reduceOnly"] = True
        body["newClientOrderId"] = cid
        # RESULT 让响应直接带成交量，省掉一次查单往返
        body["newOrderRespType"] = "RESULT"

        try:
            data = await self._request("POST", self._order_path, quota="trade",
                                       signed=True, body=body)
        except httpx.TransportError as exc:
            # 绝不能当失败返回：重发会变双倍仓位。用 TransportError 而不是
            # (TimeoutException, NetworkError) —— RemoteProtocolError（服务端收下
            # 请求后半途断连，最典型的"可能已进撮合"）不是 NetworkError 的子类
            raise OrderUnknown(self.venue.value, cid) from exc
        except ExchangeError as exc:
            if (exc.code in _UNKNOWN_STATE_CODES or exc.code == "BAD_JSON"
                    or exc.code.startswith("HTTP_5")):
                raise OrderUnknown(self.venue.value, cid) from exc
            raise
        # 回传调用方发出去的那个 ID（不是交易所回显的带前缀版本），上层拿它对账
        return self._order_result(data, fallback_cid=cid,
                                  force_cid=req.client_order_id or cid)

    async def cancel_order(self, symbol: str, client_order_id: str) -> None:
        """状态终结操作：执行后订单只可能是已撤或已成，所以它是消解 OrderUnknown 的第一步。

        必须和下单走同一条 ID 规整（带返佣前缀），否则配了返佣码之后这里查的是
        另一个 ID —— 交易所回 -2011「查无此单」，消解流程会把一张可能已成交的单
        判成"从未被接受"，上层照着 0 补仓就是双倍仓位。
        """
        try:
            await self._request(
                "DELETE", self._order_path, quota="risk", signed=True,
                params={"symbol": symbol,
                        "origClientOrderId": self._build_client_order_id(client_order_id)})
        except ExchangeError as exc:
            raise self._risk_path_error(exc) from exc

    async def query_order(self, symbol: str, client_order_id: str) -> OrderResult:
        try:
            data = await self._request(
                "GET", self._order_path, quota="risk", signed=True,
                params={"symbol": symbol,
                        "origClientOrderId": self._build_client_order_id(client_order_id)})
        except ExchangeError as exc:
            raise self._risk_path_error(exc) from exc
        return self._order_result(data, fallback_cid=client_order_id,
                                  force_cid=client_order_id)

    def _risk_path_error(self, exc: ExchangeError) -> ExchangeError:
        """风控路径（撤单/查单）上的错误重分类。

        base.resolve_unknown_order 只认 (OrderRejected, httpx.HTTPError, RateLimited)：
        抛裸 ExchangeError('HTTP_5xx') 会让消解流程在最危险的时刻带着未捕获异常崩掉。
        把可重试的挂到 RateLimited 上（code 保留，日志里仍看得出真实原因）；
        不可重试的原样上抛 —— 绝不能降级成 OrderRejected，那等于谎报"成交量为 0"。
        """
        if isinstance(exc, (OrderRejected, RateLimited)) or not exc.retriable:
            return exc
        return RateLimited(self.venue.value, exc.code, exc.message, retriable=True)

    def _order_result(self, d: Mapping[str, Any], *, fallback_cid: str,
                      force_cid: str = "") -> OrderResult:
        # 张 → 币（1000PEPEUSDT 一张 = 1000 PEPE），价格反向除
        mult = self._multiplier(str(d.get("symbol", "")))
        filled = _dec(d.get("executedQty")) * mult
        avg = _dec(d.get("avgPrice")) / mult
        return OrderResult(
            client_order_id=force_cid or str(d.get("clientOrderId") or fallback_cid),
            exchange_order_id=str(d.get("orderId", "")),
            status=_STATUS_MAP.get(str(d.get("status", "")), "new"),
            filled_qty=filled,
            avg_price=avg,
            # 订单接口不返回手续费，只有 GET /fapi/v1/userTrades 才有（调研文档未覆盖该端点）。
            # TODO: 需要精确成本归因时按 orderId 聚合 userTrades 的 commission
            fee=ZERO,
            fee_asset="",
        )

    async def fetch_positions(self, symbols: Sequence[str] | None = None) -> Sequence[Position]:
        """v3 只返回有敞口的条目 —— 不能用它枚举合约清单，清单只能来自 exchangeInfo。"""
        params: dict[str, Any] = {}
        if symbols and len(symbols) == 1:
            params["symbol"] = symbols[0]
        data = await self._request("GET", self._position_path, quota="risk",
                                   signed=True, params=params)
        wanted = set(symbols) if symbols else None
        out: list[Position] = []
        for row in data if isinstance(data, list) else [data]:
            symbol = row.get("symbol", "")
            if wanted is not None and symbol not in wanted:
                continue
            liq = _dec(row.get("liquidationPrice"))
            mult = self._multiplier(symbol)          # 张 → 币，价格反向除
            out.append(Position(
                market=self.market,
                symbol=symbol,
                qty=_dec(row.get("positionAmt")) * mult,   # 带符号，空头为负
                entry_price=_dec(row.get("entryPrice")) / mult,
                mark_price=_dec(row.get("markPrice")) / mult,
                liquidation_price=liq / mult if liq > 0 else None,   # 0 = 无强平价
                leverage=_dec(row.get("leverage")),
            ))
        return tuple(out)

    async def fetch_open_orders(self, symbol: str) -> Sequence[OrderResult]:
        data = await self._request("GET", self._open_orders_path, quota="risk",
                                   signed=True, params={"symbol": symbol})
        rows = data if isinstance(data, list) else [data]
        return tuple(self._order_result(r, fallback_cid="") for r in rows)

    async def fetch_my_trades(self, symbol: str, since_ms: int,
                              until_ms: int | None = None) -> Sequence[TradeFill]:
        """GET /fapi/v1/userTrades（权重 5）。逐笔成交，含手续费与已实现盈亏。

        服务端约束与对策：
        - startTime/endTime 窗口不得超过 7 天（超了报 -1127）→ 整段按 7 天切窗
        - fromId 不能与 startTime/endTime 同传 → 窗内第一页用时间，
          撑满页（可能还有更多）后改用 fromId=末条id+1 续翻，读到越过窗尾为止
        - fromId 翻页会越进下一窗的时段 → 按成交 id 去重，最后统一升序

        价格/数量保持交易所原生计价（1000PEPE 这类符号不折算乘数）——
        复盘要和用户在币安界面看到的数字对得上，折算过的反而认不出。
        走 quota="market"：这是分析流量，不许挤占风控路径的预留配额。
        """
        until_ms = self.timestamp_ms() if until_ms is None else until_ms
        page_size = self._trades_page_size
        by_id: dict[str, TradeFill] = {}

        window_start = since_ms
        while window_start <= until_ms:
            window_end = min(window_start + USER_TRADES_WINDOW_MS - 1, until_ms)
            rows = await self._request(
                "GET", self._user_trades_path, quota="market", signed=True,
                params={"symbol": symbol, "startTime": window_start,
                        "endTime": window_end, "limit": page_size})
            rows = rows if isinstance(rows, list) else []
            for r in rows:
                by_id[str(r.get("id", ""))] = self._trade_fill(r)

            pages = 1
            while len(rows) == page_size and pages < HISTORY_MAX_PAGES:
                last_id = int(rows[-1].get("id", 0))
                rows = await self._request(
                    "GET", self._user_trades_path, quota="market", signed=True,
                    params={"symbol": symbol, "fromId": last_id + 1,
                            "limit": page_size})
                rows = rows if isinstance(rows, list) else []
                pages += 1
                done = False
                for r in rows:
                    if int(r.get("time", 0)) > window_end:
                        done = True
                        break
                    by_id[str(r.get("id", ""))] = self._trade_fill(r)
                if done:
                    break
            window_start = window_end + 1

        return tuple(sorted(by_id.values(), key=lambda f: (f.ts_ms, int(f.order_id or 0))))

    def _trade_fill(self, row: Mapping[str, Any]) -> TradeFill:
        return TradeFill(
            market=self.market,
            symbol=str(row.get("symbol", "")),
            ts_ms=int(row.get("time", 0)),
            side=str(row.get("side", "")).lower(),
            price=_dec(row.get("price")),
            qty=_dec(row.get("qty")),
            quote_qty=_dec(row.get("quoteQty")),
            fee=_dec(row.get("commission")),
            fee_asset=str(row.get("commissionAsset", "")),
            realized_pnl=_dec(row.get("realizedPnl")),
            maker=bool(row.get("maker", False)),
            order_id=str(row.get("orderId", "")),
        )

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        # leverage 是整数参数，向下取整（要 3.7 倍时给 3 倍，宁可保守）
        lev = int(leverage.to_integral_value(rounding=ROUND_DOWN))
        await self._request("POST", self._leverage_path, quota="risk", signed=True,
                            body={"symbol": symbol, "leverage": lev})

    async def fetch_leverage_tiers(self, symbol: str) -> Sequence[tuple[Decimal, Decimal]]:
        data = await self._request("GET", "/fapi/v1/leverageBracket", quota="risk",
                                   signed=True, params={"symbol": symbol})
        rows = data if isinstance(data, list) else [data]
        row = next((r for r in rows if r.get("symbol") == symbol), rows[0] if rows else {})
        tiers = [(_dec(b.get("notionalCap")), _dec(b.get("initialLeverage")))
                 for b in row.get("brackets", [])]
        tiers.sort(key=lambda t: t[0])
        return tuple(tiers)


def _round_to_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    """规整到步长的整数倍。全程 Decimal —— 用 float 取模会在边界上
    稳定触发 -1111 BAD_PRECISION（0.1+0.2 那个经典问题）。"""
    if step is None or step <= 0:
        return value
    return (value / step).to_integral_value(rounding=rounding) * step


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """向零取整到步长的整数倍。数量必须是 floor 不是 round：
    取整后变大就等于超额建仓。"""
    return _round_to_step(value, step, ROUND_DOWN)


def _truncate(value: Decimal, digits: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_DOWN)
