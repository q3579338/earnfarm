"""Backpack 适配器（REST api/v1 + wapi/v1，ED25519 签名，linear 永续 `backpack:perp`）。

调研依据：`docs/research/exchange-backpack.md`（2026-08，整份 OpenAPI spec 从 Redoc 页
抠出逐字核对 + 公开端点线上实测）。端点/字段/错误码一律以那份文档为准；
文档没写死的在这里标 **UNVERIFIED**，不猜。

这家的六条特性决定了这份实现的形状：

1. **签名是 ED25519（RFC 8032）不是 HMAC**：api_key = base64 的 32 字节公钥（原文放
   X-API-Key），api_secret = base64 的 32 字节私钥种子。签名串是
   `instruction=<指令>&<按键名字母序的 k=v>&timestamp=<ms>&window=<ms>`——服务端验的是
   **解析后的键值对重建串**而不是请求原始字节，所以 body 可以交给 httpx 的 `json=`
   序列化（与 KuCoin/Gate 必须逐字节控制不同），但值的文本形态必须两边一致：
   布尔小写、decimal 全字符串、clientId 十进制整数。没发 X-Window 也必须把
   window=5000 签进串里。私钥构造**延迟到第一次签名**：public_feed 用
   `Credential("", "")` 跑无凭据公开行情，构造期碰 base64 解码就会把这家从机会榜
   整个抹掉。
2. **数量单位就是 base 币**：没有张、没有乘数、没有千倍前缀，contract_size=1。
   八家里唯一不需要任何单位换算的一家——也正因此这里没有 kucoin/gate 那套
   coins_to_lots。
3. **资金费 1 小时结算**（fundingInterval=3600000，逐合约字段）。年化 ×24×365；
   照币安 8h 肌肉记忆算会低估 8 倍，interval_s 一律读字段不写死。
4. **`clientId` 是 uint32 整数**不是字符串：仓库的字符串 client_order_id 用 crc32 做
   确定性映射，撤单/查单用同一个数。**服务端是否拒绝重复 clientId 未文档化
   （UNVERIFIED）**，绝不能当幂等闸，只能当查找键；OrderUnknown 的消解全靠
   cancel → 查在簿单 → 404 转历史端点自配这条链。
5. **查单是两段式**：`GET /api/v1/order` 只认还挂在簿上的单，已成交/已撤/过期一律
   404 RESOURCE_NOT_FOUND——**404 绝不等于"从未接受"**，必须转
   `/wapi/v1/history/orders` 按 clientId 自配后才可定性。
6. **杠杆是账户级** leverageLimit（PATCH /api/v1/account），没有逐合约杠杆端点、
   没有档位表端点，保证金走每市场的 sqrt imfFunction。

`CarryRate.rate` 用**交易所原始约定**（正 = 多头付给空头，官方 learn 文章原文
"If positive, longs pay shorts"），原样写入不取反。

返佣：**不注入任何返佣码/broker code**。Backpack 的返佣机制是下单可选 header
`X-BROKER-ID`(uint16)/`X-BROKER-KEY`——本仓库一个都不发（用户明确要求；CCXT 会自动塞
`X-Broker-Id: 1400`，抄它的签名实现时已把这行剔掉）。`broker_code` 照收、收下什么都
不做：不加 header、不加 body 字段、不给 clientId 动手脚。

批量下单（POST /api/v1/orders，每单各带 instruction= 前缀的特殊拼串）**未实现**：
executor 是单单下发的，批量通道多一条签名路径就多一处签错的可能，等真有批量需求
再按调研文档"批量下单拼串"节补。
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import re
import zlib
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, AsyncIterator, Mapping, Sequence

import httpx

from ..models import (
    ZERO,
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

REST_BASE = "https://api.backpack.exchange"
WS_BASE = "wss://ws.backpack.exchange"

# 签名窗口毫秒。**不发 X-Window header 也必须把这个值签进串里**（文档原文），
# 所以 header 和签名串统一都发、都用同一个变量。上限 60000。
DEFAULT_WINDOW_MS = 5_000
MAX_WINDOW_MS = 60_000

# 资金费结算周期兜底（秒）。2026-08-05 实测 84 个 PERP 全部是 3600000ms=1h，
# 但周期是逐合约字段、可能被调整，正常路径一律读 fundingInterval，这里只兜缺字段。
DEFAULT_FUNDING_INTERVAL_S = 3_600

# 基点 → 小数的除数。账户费率（futuresTakerFee）和资金费上下限
# （fundingRateUpperBound/LowerBound）都是**基点**，而 markPrices 的 fundingRate
# 是小数——同一个概念两处两种单位，换算漏一处就是 10000 倍的错。
BPS = Decimal("10000")

# depth 端点的 limit 是**枚举字符串**（5/10/20/50/100/500/1000），不传默认每边 5000 档。
# 盘口一档用 5、多档深度用 100：BTC tick 0.1 在 6 万价位上 10bp≈64 个 tick，
# 100 档盖得住；小币的簿本来就稀。真不够就如实回 levels_used，绝不外推。
BOOK_TOP_LIMIT = "5"
DEPTH_LIMIT = "100"
DEPTH_LEVELS_PER_SIDE = 100

# 资金费历史翻页。单页上限 10000（一页 ≈416 天，正常窗口一页就够）；页数硬闸挡住
# "服务端无视 offset"时的无限翻页——20 页 × 1000 条 ≈ 2.3 年，远超回填需求。
FUNDING_HISTORY_MAX_PAGE_SIZE = 10_000
FUNDING_HISTORY_MAX_PAGES = 20
# 历史桶限频是 30 req/min（官方 FAQ；/api/v1/fundingRates 是否算入 UNVERIFIED，
# 按最坏情况处理）。跨页时主动睡 2.1s ≈ 0.48 req/s，压在配额线下面。
# 跨符号的节流归 history.DEFAULT_PACE_S（主控的文件），这里只管单次调用内部。
FUNDING_HISTORY_PAGE_PACE_S = 2.1

# 历史订单端点没有 clientId 服务端过滤（只有 orderId 参数），只能拉回来自己配。
# 单页上限 1000；OrderUnknown 消解找的都是刚下的单，Desc 排序下 3 页 3000 条
# 找不到就可以定性"从未被接受"了。
ORDER_HISTORY_PAGE_SIZE = 1_000
ORDER_HISTORY_MAX_PAGES = 3

# 符号全是 BTC_USDC_PERP 这种大写+下划线。先滤一道比事后猜订阅失败便宜。
WS_SYMBOL_RE = re.compile(r"^[A-Z0-9_]+$")
WS_RECONNECT_MIN_S = 1.0
WS_MAX_BACKOFF_S = 30.0

LISTED_FEE_NOTE = "Backpack 挂牌基础档（非本账户实际档位）"
ACTUAL_FEE_NOTE = "Backpack accountQuery 实际档位（账户级，一档吃全市场）"

# ---- 错误码分桶（spec 的 ApiErrorCode 全枚举，判定顺序见 _translate）----------

# 明确的业务拒绝：进撮合前被挡下，订单确定不存在，改参数后可安全重发。
# 借贷族（BORROW/LEND）是 spot margin 的码，本适配器不碰借贷，撞上也是确定性拒绝。
_REJECT_CODES = frozenset({
    "INVALID_ORDER", "INVALID_PRICE", "INVALID_QUANTITY", "INVALID_SYMBOL",
    "INVALID_MARKET", "INVALID_CLIENT_REQUEST", "INVALID_ASSET",
    "INSUFFICIENT_FUNDS", "INSUFFICIENT_MARGIN", "MAX_LEVERAGE_REACHED",
    "ORDER_LIMIT", "POSITION_LIMIT", "TRADING_PAUSED",
    "ACCOUNT_LIQUIDATING", "ACCOUNT_DEACTIVATED", "PRECONDITION_FAILED",
    "FORBIDDEN",
    "BORROW_LIMIT", "BORROW_REQUIRES_LEND_REDEEM", "INSUFFICIENT_SUPPLY",
    "LEND_LIMIT", "LEND_REQUIRES_BORROW_REPAY",
})
# 引擎侧结果不明：Backpack 是单线性命令流，API 层报 TIMEOUT/SERVER_ERROR 不代表
# 引擎没收到这单。下单路径必须当 OrderUnknown，当失败重发就是双倍仓位。
_ENGINE_UNKNOWN_CODES = frozenset({"TIMEOUT", "SERVER_ERROR"})
# 鉴权层的配置错：重试一万次也是同样结果，直接抛致命；下单路径**不得**升级成
# OrderUnknown——签名都没过，撮合根本没见过这个请求，白跑一轮 cancel+查单。
_FATAL_AUTH_CODES = frozenset({
    "INVALID_SIGNATURE", "UNAUTHORIZED", "INVALID_TWO_FACTOR_CODE",
})
# RESOURCE_NOT_FOUND 故意不进任何桶：它在 orderQuery 语境下是"不在簿上"
# （已成交的单也 404！），在撤单语境下是"目的已达成"，语境只有调用方知道，
# 所以 _translate 保持它是普通 ExchangeError(retriable=False)，由调用方按 code 特判。

# 下单响应 status → OrderResult.status。Expired 是 IOC 残量/FOK 杀单的终态，
# 部成后过期时 executedQuantity 带着已成部分，用 canceled 表达终结、量走 filled_qty，
# 上层按仓位差分处理——报成 new 会让状态机以为这单还活着。
# TriggerPending/TriggerFailed 是触发单的状态（本适配器不下触发单，防御性映射）。
_STATUS_MAP = {
    "New": "new",
    "PartiallyFilled": "partial",
    "Filled": "filled",
    "Cancelled": "canceled",
    "Expired": "canceled",
    "TriggerPending": "new",
    "TriggerFailed": "canceled",
}

# timeInForce 枚举就是 GTC/IOC/FOK 三个（Backpack 全支持，postOnly 是**独立布尔**
# 不进 TIF）。挂单意图的三个别名统一翻成 GTC+postOnly。
_TIF = {"GTC": "GTC", "IOC": "IOC", "FOK": "FOK",
        "GTX": "GTC", "POST_ONLY": "GTC", "MAKER": "GTC"}
_POST_ONLY_TIF = frozenset({"GTX", "POST_ONLY", "MAKER"})

# (method, path) → 签名串里的 instruction。签名必须知道指令名，而 _prepare 只拿得到
# 路径——查表比让每个调用点多传一个参数可靠（传错指令名的签名错要靠交易所报
# INVALID_SIGNATURE 才发现，查表漏登记则在本地就炸）。
_INSTRUCTIONS: Mapping[tuple[str, str], str] = {
    ("GET", "/api/v1/account"): "accountQuery",
    ("PATCH", "/api/v1/account"): "accountUpdate",
    ("GET", "/api/v1/account/limits/order"): "maxOrderQuantity",
    ("GET", "/api/v1/capital"): "balanceQuery",
    ("GET", "/api/v1/position"): "positionQuery",
    ("POST", "/api/v1/order"): "orderExecute",
    ("DELETE", "/api/v1/order"): "orderCancel",
    ("GET", "/api/v1/order"): "orderQuery",
    ("GET", "/api/v1/orders"): "orderQueryAll",
    ("DELETE", "/api/v1/orders"): "orderCancelAll",
    ("GET", "/wapi/v1/history/orders"): "orderHistoryQueryAll",
    ("GET", "/wapi/v1/history/fills"): "fillHistoryQueryAll",
    ("GET", "/wapi/v1/history/funding"): "fundingHistoryQueryAll",
}


def _dec(value: Any, default: str = "0") -> Decimal:
    """交易所返回值 → Decimal，中途绝不经过 float。

    Backpack 的数值几乎全是字符串型 decimal（spec 要求），但 _parse 仍用
    parse_float=Decimal 兜住偶发的裸 JSON 数字（visible 之外的字段哪天变裸数字，
    Decimal(repr(float)) 兜底时精度已经掉了）。
    """
    if value is None or value == "":
        return Decimal(default)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal(default)


def _dec_opt(value: Any) -> Decimal | None:
    """区分"字段缺失"和"字段就是 0"。费率这条路上是硬需求：futuresMakerFee 真的可能
    是 0（VIP5 maker 0.000%），而缺字段时补 0 会让四笔 taker 成本凭空消失、
    把负期望的机会整片翻成正期望，还不报任何错。"""
    return None if value is None or value == "" else _dec(value)


def _dec_str(value: Decimal) -> str:
    """定点字符串。绝不能让 Decimal('1E-5') 以科学计数法进请求体——签名串和 body
    都要这个文本形态，发成 "1E-5" 会被当非法参数拒掉。"""
    return format(value, "f")


def _sig_value(value: Any) -> str:
    """签名串里值的文本形态。布尔必须小写 true/false（Python str(True) 是 "True"，
    直接拼会 INVALID_SIGNATURE 且报错信息不区分编码错/拼串错）；Decimal 走定点；
    其余 str()。这个形态必须与发出去的 JSON/query 一致——json.dumps 的布尔本来
    就是小写，int 就是十进制，所以两边天然对齐。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return _dec_str(value)
    return str(value)


def _round_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    if step <= 0:
        return value
    return ((value / step).to_integral_value(rounding=rounding) * step).quantize(step)


def _naive_utc_ms(raw: Any) -> int:
    """naive UTC datetime 串 → 毫秒。

    `intervalEndTimestamp`（fundingRates / funding 流水）、历史单 `createdAt`、
    fills 的 `timestamp` 都是 **无时区的 UTC 串**（"2026-08-05T17:00:00"，没有 Z），
    而挂单/下单响应的 createdAt 却是毫秒整数——同一家两种表示。fromisoformat 出来的
    naive datetime 若直接 .timestamp() 会按**本地时区**折算，东八区差 8 小时，
    必须先 replace(tzinfo=utc)。
    """
    if raw in (None, ""):
        return 0
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _ts_to_ms(raw: Any) -> int:
    """时间戳归一到毫秒。Backpack 的行情时间戳（depth/openInterest/WS 的 E/T）是
    **微秒**，markPrices 的 nextFundingTimestamp 又是毫秒——字段名都不带单位后缀。
    毫秒 ≈ 1.8e12、微秒 ≈ 1.8e15，1e14 这条线两边都离得很远，按量级自适应。"""
    if raw in (None, ""):
        return 0
    try:
        value = int(_dec(raw))
    except (InvalidOperation, ValueError):
        return 0
    return value // 1_000 if value > 10 ** 14 else value


class _MarketMeta:
    """一个永续市场里换算/下单/评分真正要用到的字段。

    单独存是因为 Instrument 放不下资金费上下限、OI 上限、imf 参数，而它们全部来自
    `/api/v1/markets` 的同一次响应——这是本家最划算的元数据端点（一次全市场）。
    """

    __slots__ = ("symbol", "base", "quote", "tick", "step", "min_qty", "max_qty",
                 "funding_interval_s", "cap", "floor", "oi_limit", "imf_base")

    def __init__(self, raw: Mapping[str, Any]) -> None:
        self.symbol: str = str(raw.get("symbol") or "")
        self.base: str = str(raw.get("baseSymbol") or "")
        self.quote: str = str(raw.get("quoteSymbol") or "")
        filters = raw.get("filters") or {}
        price_f = filters.get("price") or {}
        qty_f = filters.get("quantity") or {}
        self.tick: Decimal = _dec(price_f.get("tickSize"), "0")
        self.step: Decimal = _dec(qty_f.get("stepSize"), "0")
        self.min_qty: Decimal = _dec(qty_f.get("minQuantity"), "0")
        # maxQuantity 可以是 null（BTC 实测就是），None 表示无上限
        self.max_qty: Decimal | None = _dec_opt(qty_f.get("maxQuantity"))
        interval_ms = int(_dec(raw.get("fundingInterval"), "0"))
        self.funding_interval_s: int = (interval_ms // 1000 if interval_ms > 0
                                        else DEFAULT_FUNDING_INTERVAL_S)
        # 上下限单位是**基点**（"100" = 每期 1%），除以 10000 才和 markPrices 的
        # 小数费率同单位。缺失存 None，不补 0——(0,0) 会把费率夹死成 0。
        upper = _dec_opt(raw.get("fundingRateUpperBound"))
        lower = _dec_opt(raw.get("fundingRateLowerBound"))
        self.cap: Decimal | None = upper / BPS if upper is not None else None
        self.floor: Decimal | None = lower / BPS if lower is not None else None
        self.oi_limit: Decimal = _dec(raw.get("openInterestLimit"), "0")
        imf = raw.get("imfFunction") or {}
        self.imf_base: Decimal = _dec(imf.get("base"), "0")

    @property
    def max_leverage(self) -> Decimal:
        """由 imfFunction.base 反推的结构性杠杆上限（BTC base=0.01333≈75x）。

        这是**市场**参数不是账户参数：账户级 leverageLimit（官网标 50x，UNVERIFIED）
        可能比它低，且 sqrt 保证金函数意味着仓位越大有效杠杆越低。评分层只拿它当
        量级参考，真实约束由交易所在下单时执行。
        """
        if self.imf_base <= 0:
            return Decimal("1")
        return (Decimal("1") / self.imf_base).quantize(Decimal("0.01"),
                                                       rounding=ROUND_DOWN)


class BackpackAdapter(ExchangeAdapter):
    """Backpack U 本位永续（`backpack:perp`）。

    **不注入任何返佣码/broker code**：`broker_code` 照收、收下什么都不做（用户明确
    要求）——不发 X-BROKER-ID/X-BROKER-KEY header、不加 body 字段。构造参数**全部有
    默认值**（session._build_adapter 只传 cred + broker_code），且**绝不校验凭据非空、
    绝不在构造期解码私钥**——public_feed 用 `Credential("", "")` 构造它跑无凭据公开
    行情，这里抛错的后果是 Backpack 在整个机会榜上消失、只在 init_errors 里留一行。
    """

    venue = Venue.BACKPACK
    rest_base = REST_BASE

    def __init__(self, credential: Credential, *, broker_code: str = "",
                 client: httpx.AsyncClient | None = None,
                 market_kind: MarketKind = MarketKind.PERP,
                 rest_base: str = REST_BASE,
                 ws_base: str = WS_BASE,
                 window_ms: int = DEFAULT_WINDOW_MS) -> None:
        if market_kind is not MarketKind.PERP:
            # 现货/杠杆没实现：调研文档只覆盖了 PERP 的下单语义。现货共用
            # /api/v1/order 但涉及 autoLend/autoBorrow 借贷布尔族、余额而非仓位的
            # 对账口径、CarryKind.INTEREST 的借贷利率端点，全都没调研。凭记忆补等于赌。
            raise NotImplementedError(
                f"backpack 适配器目前只支持 {MarketKind.PERP.value}；"
                f"{market_kind.value} 缺现货/借贷调研（autoLend 族、利率端点、对账口径）")
        # 用户从网页复制的码常带尾随空白，八家统一在构造期剥掉；剥完之后本适配器
        # 对它**不做任何事**。
        super().__init__(credential, broker_code=broker_code.strip(), client=client)
        self.rest_base = rest_base.rstrip("/")
        self.ws_base = ws_base
        self.market_kind = market_kind
        # 八家都对外暴露 .market，桥接层按这个名字取，缺了就 AttributeError
        self.market = f"{self.venue.value}:{market_kind.value}"
        # 窗口夹在 [1, 60000]：签超过上限的值会被直接拒
        self._window_ms = max(1, min(int(window_ms), MAX_WINDOW_MS))
        # ED25519 私钥对象**延迟构造**：见类 docstring。None = 还没碰过签名路径。
        self._signing_key: Any = None
        self._meta: dict[str, _MarketMeta] = {}
        self._meta_lock = asyncio.Lock()
        # crc32 映射的反查表：交易所只回 uint32，上层要按原字符串 coid 对账。
        # 只在本进程存活期有效——重启后 fetch_open_orders 只能报数字串，
        # 上层按 exchange_order_id 兜底（八家一致的降级路径）。
        self._coid_by_u32: dict[int, str] = {}

    # ---- 签名 / 请求 -----------------------------------------------------

    def _private_key(self) -> Any:
        """base64 私钥种子 → Ed25519PrivateKey，只在第一次签名时构造并缓存。

        cryptography 的导入也放在这里：只跑公开行情的进程不必加载它。
        64 字节是 seed+pub 的拼接导出格式（某些钱包/CCXT 兼容），取前 32 字节。
        """
        if self._signing_key is None:
            secret = (self._cred.api_secret or "").strip()
            if not secret:
                raise ExchangeError(
                    self.venue.value, "NO_CREDENTIAL",
                    "缺 api_secret（base64 的 ED25519 私钥），签名请求发不出去")
            try:
                raw = base64.b64decode(secret, validate=True)
            except Exception as exc:
                raise ExchangeError(
                    self.venue.value, "BAD_CREDENTIAL",
                    "api_secret 不是合法 base64——Backpack 的 secret 是 base64 私钥"
                    "种子，不是 hex、不是助记词") from exc
            if len(raw) == 64:
                raw = raw[:32]          # seed+pub 拼接格式，前 32 字节才是种子
            if len(raw) != 32:
                raise ExchangeError(
                    self.venue.value, "BAD_CREDENTIAL",
                    f"api_secret 解码后 {len(raw)} 字节，需要 32（或 64 的拼接格式）")
            from cryptography.hazmat.primitives.asymmetric import ed25519
            self._signing_key = ed25519.Ed25519PrivateKey.from_private_bytes(raw)
        return self._signing_key

    @staticmethod
    def _signing_string(instruction: str, source: Mapping[str, Any],
                        ts_ms: int, window_ms: int) -> str:
        """签名串：`instruction=<指令>&<k=v 按键名字母序>&timestamp=<ms>&window=<ms>`。

        官方例逐字：`instruction=orderCancel&orderId=28&symbol=BTC_USDT&timestamp=
        1614550000000&window=5000`。无参数端点就是 instruction+timestamp+window
        三段。值不做 URL-encode（值域全是 URL-safe 字符，文档例子也是原文）。
        """
        parts = [f"instruction={instruction}"]
        parts.extend(f"{k}={_sig_value(v)}" for k, v in sorted(source.items()))
        parts.append(f"timestamp={ts_ms}")
        parts.append(f"window={window_ms}")
        return "&".join(parts)

    def _prepare(self, method: str, path: str, params: dict, body: dict,
                 signed: bool) -> tuple[dict[str, str], str, dict, dict]:
        method = method.upper()
        # None 值剔除、Decimal 统一转定点字符串：签名串和发出去的 JSON/query 必须是
        # **同一份 dict 的同一份文本形态**——json.dumps 不认识 Decimal，漏转会在
        # 发送时炸；转成 float 则签名和 body 各自漂移。
        params = {k: (_dec_str(v) if isinstance(v, Decimal) else v)
                  for k, v in params.items() if v is not None}
        body = {k: (_dec_str(v) if isinstance(v, Decimal) else v)
                for k, v in body.items() if v is not None}
        url = self.rest_base + path
        headers: dict[str, str] = {}
        if signed:
            instruction = _INSTRUCTIONS.get((method, path))
            if instruction is None:
                # 宁可本地炸：签错 instruction 只会收到 INVALID_SIGNATURE，
                # 和拼串错、编码错完全无法区分，排查方向必然带偏
                raise ExchangeError(
                    self.venue.value, "NO_INSTRUCTION",
                    f"{method} {path} 没登记 instruction，无法构造签名串")
            # GET 签查询参数，POST/DELETE/PATCH 签 body 键值对（文档 Authentication
            # 节原文）。服务端验的是解析后的键值对，不是原始字节。
            source = params if method == "GET" else body
            ts = self.timestamp_ms()        # 已校准的时钟；绝不用 time.time()
            payload = self._signing_string(instruction, source, ts, self._window_ms)
            signature = base64.b64encode(
                self._private_key().sign(payload.encode())).decode()
            headers.update({
                "X-API-Key": self._cred.api_key,     # base64 公钥**原文**
                "X-Signature": signature,
                # 签进串里的 timestamp/window 必须和这两个 header 是同一个值，
                # 签一个发另一个是最常见的 INVALID_SIGNATURE
                "X-Timestamp": str(ts),
                "X-Window": str(self._window_ms),
            })
        # 返佣 header 在这里**一个字节都不加**（X-BROKER-ID/X-BROKER-KEY 是 Backpack
        # 的返佣注入机制，本仓库明确不做）。
        return headers, url, params, body

    # 不覆写 _request：Backpack 验签吃的是**解析后的键值对**而非请求原始字节，
    # 基类用 httpx 的 params=/json= 发送即可——布尔 json.dumps 本来就是小写、
    # int 就是十进制、Decimal 已在 _prepare 转成字符串，文本形态与签名串天然一致。
    # （KuCoin/Gate 必须覆写是因为它们签的是原始字节，这家不是。）

    # ---- 响应解析 -------------------------------------------------------

    def _parse(self, resp: httpx.Response) -> Any:
        text = resp.text or ""
        payload: Any = None
        bad_json = False
        if text:
            try:
                # parse_float=Decimal：spec 说数值全是字符串，但哪天冒出裸 JSON
                # 数字，等落到 Decimal(repr(float)) 兜底时精度已经掉了
                payload = json.loads(text, parse_float=Decimal)
            except ValueError:
                bad_json = True

        if resp.status_code >= 400:
            if isinstance(payload, dict) and payload.get("code"):
                raise self._translate(str(payload.get("code")),
                                      str(payload.get("message") or ""),
                                      resp.status_code)
            # 【非结构化正文一个字节都不进分类逻辑】4xx/5xx 时挡在前面的 CDN/WAF/
            # 反代会回自己的 HTML 错误页，那不是交易所写的。把它喂进任何关键词
            # 匹配等于让别人家的 HTML 决定"这单进没进撮合"（hyperliquid 修过：
            # 错误页里 CSP 的 nonce 属性、request id 里的 429 都会触发误判）。
            # 这里只认 HTTP 语义：429 是网关限频（确定没进撮合），>=500 可重试
            # （place_order 会升级成 OrderUnknown 走消解）。
            if resp.status_code == 429:
                raise RateLimited(self.venue.value, "HTTP_429", text[:200],
                                  retriable=True)
            raise ExchangeError(self.venue.value, f"HTTP_{resp.status_code}",
                                text[:200], retriable=resp.status_code >= 500)

        if bad_json:
            # 2xx 但响应体不是合法 JSON：这单有没有进撮合完全不知道。返回 None 会
            # 让下游解析炸 AttributeError；retriable=False 更糟——那等于给
            # place_order 发一张"可安全重发"的许可证。
            raise ExchangeError(self.venue.value, "BAD_JSON",
                                f"响应不是合法 JSON: {text[:200]}", retriable=True)
        # 撤单 202（受理未执行）和 PATCH account 的 200 都是空 body → None。
        # 202 不是终态，调用方要接查单确认。
        return payload

    def _translate(self, code: str, message: str, status: int) -> ExchangeError:
        venue = self.venue.value
        if status == 429 or code == "TOO_MANY_REQUESTS":
            # 限频 = 确定没进撮合，是下单路径上唯一可以直接抛、允许上层安全重试的错
            return RateLimited(venue, code or "HTTP_429", message, retriable=True)
        if status >= 500 or code in _ENGINE_UNKNOWN_CODES:
            # 【HTTP 语义压过错误码分类】单线性命令流下 5xx/TIMEOUT 意味着这单
            # **可能已经进了撮合**，place_order 把 retriable 的错一律倒向 OrderUnknown
            return ExchangeError(venue, code or f"HTTP_{status}", message,
                                 retriable=True)
        if code == "MAINTENANCE":
            return ExchangeError(venue, code, message, retriable=True)
        if code in _REJECT_CODES:
            return OrderRejected(venue, code, message, retriable=False)
        if code in _FATAL_AUTH_CODES:
            return ExchangeError(venue, code, message, retriable=False)
        # 兜底（含 RESOURCE_NOT_FOUND / INVALID_RANGE / TOO_EARLY 这些语境相关或
        # 语义未明的码）：一律不可重试。宁可不重试，也不要把一个可能已成交的请求
        # 当成可安全重发——下单路径会把"认不出的可重试错"单独倒向 OrderUnknown。
        return ExchangeError(venue, code or f"HTTP_{status}", message,
                             retriable=False)

    def _risk_path_error(self, exc: ExchangeError) -> ExchangeError:
        """风控路径（撤单/查单/查仓）上的错误重分类。

        _request 把所有 httpx 异常收敛成 ExchangeError('NETWORK')，而
        base.resolve_unknown_order 只捕获 (OrderRejected, httpx.HTTPError,
        RateLimited)——不转一手，网络抖动或 5xx 会让消解流程在最危险的时刻带着
        未捕获异常崩掉。可重试的重挂到 RateLimited（code 保留真实原因）；
        **不可重试的原样上抛，绝不降级成 OrderRejected**——降级等于谎报
        "成交量为 0"，上层会照着 0 再补一次仓。
        """
        if isinstance(exc, (OrderRejected, RateLimited)) or not exc.retriable:
            return exc
        return RateLimited(self.venue.value, exc.code, exc.message, retriable=True)

    # ---- 元数据 ---------------------------------------------------------

    async def fetch_server_time_ms(self) -> int:
        """`GET /api/v1/time` 返回**裸文本毫秒串**（实测 body 就是
        `1785948047107`），不是 JSON——不走 _request/_parse，直接读 text。
        `/ping` 同理返回裸 `pong`，`/status` 才是 JSON。"""
        try:
            resp = await self._client.get(self.rest_base + "/api/v1/time")
        except httpx.HTTPError as exc:
            raise ExchangeError(self.venue.value, "NETWORK", str(exc),
                                retriable=True) from exc
        if resp.status_code >= 400:
            raise ExchangeError(self.venue.value, f"HTTP_{resp.status_code}",
                                resp.text[:200],
                                retriable=resp.status_code >= 500)
        try:
            return int(resp.text.strip())
        except ValueError as exc:
            raise ExchangeError(self.venue.value, "BAD_TIME",
                                f"/api/v1/time 不是毫秒串: {resp.text[:50]!r}",
                                retriable=True) from exc

    async def _load_markets(self) -> list[_MarketMeta]:
        """拉一次 /api/v1/markets（公开，一次全市场）刷新 _meta，返回筛过的永续。

        不传 marketType[] 过滤参数（数组参数的编码形式没实测，UNVERIFIED），
        默认返回 SPOT+PERP，客户端自己筛——171 行的响应不值得为省流量赌一个
        没验证过的参数写法。**不做长缓存**：fundingInterval 是逐合约字段、
        orderBookState 会变（Open→CancelOnly），启动读一次然后一直用会把
        改过周期的合约年化算错。
        """
        rows = await self._request("GET", "/api/v1/markets", quota="market") or []
        # 先在局部建好再整体替换，不做增量：增量只增不减，orderBookState 变成
        # CancelOnly / 下架的市场旧条目会一直留在 _meta 里，而 fetch_carry_rates
        # 曾拿 _meta 当白名单——死合约带着不再更新的费率继续挂在机会榜上
        # （HL 的 _load_universe 同病同修）。_ensure_meta 兜底加进来的下架市场
        # 条目会被这次替换洗掉，持仓路径按需再补，宁可多一次单市场请求。
        new_meta: dict[str, _MarketMeta] = {}
        kept: list[_MarketMeta] = []
        for raw in rows:
            # 过滤链四条件，缺一不可（调研实测 171 市场只留 84 个 PERP）：
            # - marketType==PERP：IPERP 是反向合约（数量/盈亏模型不同）、DATED 有
            #   交割日（到期就没资金费）、PREDICTION/RFQ 更不是 carry 标的
            # - orderBookState==Open：CancelOnly/PostOnly/Closed 不可正常下单
            # - visible==true：隐藏市场
            # - rwaMarketType==STOCK 是代币化股票，交易时段和资金费逻辑都不同
            if str(raw.get("marketType") or "") != "PERP":
                continue
            if str(raw.get("orderBookState") or "") != "Open":
                continue
            if not bool(raw.get("visible", True)):
                continue
            if str(raw.get("rwaMarketType") or "") == "STOCK":
                continue
            meta = _MarketMeta(raw)
            if not meta.symbol or meta.tick <= 0 or meta.step <= 0:
                # tick/step 是下单精度的分母。宁可跳过这一行也别整体抛错：
                # public_feed 把 fetch_instruments 整个 try/except 吞掉，
                # 抛错 = 这家规格整片消失且零提示
                continue
            new_meta[meta.symbol] = meta
            kept.append(meta)
        self._meta = new_meta
        return kept

    async def fetch_instruments(self) -> Sequence[Instrument]:
        metas = await self._load_markets()
        out: list[Instrument] = []
        for meta in metas:
            out.append(Instrument(
                market=self.market,
                symbol=meta.symbol,     # 原生符号；归一化是 public_feed 的事
                base=meta.base,
                quote=meta.quote,
                tick_size=meta.tick,
                lot_size=meta.step,     # 数量单位就是 base 币，无需换算
                # Backpack **没有名义额下限字段**，最小约束就是 minQuantity（在
                # place_order 本地拦截）。填 0 是如实表达"这家没有名义额门槛"，
                # 不是缺数据——拿 minQuantity×价格伪造一个反而让评分层把快照价
                # 当成硬约束。
                min_notional=ZERO,
                contract_size=Decimal("1"),
                max_leverage=meta.max_leverage,
                funding_interval_s=meta.funding_interval_s,
            ))
        return tuple(out)

    async def _ensure_meta(self, symbols: Sequence[str]) -> None:
        """按需补齐元数据：缺什么先整表拉一次，剩下的单个补。

        单个补那步是给**下架/暂停**市场留的——orderBookState 变成 CancelOnly 后
        它会被 _load_markets 的过滤链挡掉，但仓里可能还有仓位，持仓是真值，
        不能因为查不到规格就让相关方法报错。
        """
        if not [s for s in symbols if s and s not in self._meta]:
            return
        async with self._meta_lock:
            missing = [s for s in symbols if s and s not in self._meta]
            if not missing:
                return
            await self._load_markets()
            for symbol in [s for s in missing if s not in self._meta]:
                try:
                    raw = await self._request("GET", "/api/v1/market",
                                              quota="market",
                                              params={"symbol": symbol})
                except ExchangeError:
                    continue        # 单个失败不拖垮整批
                if not raw or not raw.get("symbol"):
                    continue
                # 兜底路径必须过与主路径**同款的语义守卫**（KuCoin 的 blocker 就在
                # 这一步漏了）：marketType 是单位/语义判据——IPERP 反向合约的
                # netQuantity 口径不同、SPOT 没有资金费，从后门进 _meta 会让上层
                # 拿错口径。orderBookState/visible 不在此列：这条路本来就是给
                # 下架/暂停市场留的。
                if str(raw.get("marketType") or "") != "PERP":
                    continue
                meta = _MarketMeta(raw)
                if not meta.symbol or meta.tick <= 0 or meta.step <= 0:
                    continue
                self._meta[meta.symbol] = meta

    async def _meta_of(self, symbol: str) -> _MarketMeta:
        await self._ensure_meta([symbol])
        meta = self._meta.get(symbol)
        if meta is None:
            raise ExchangeError(self.venue.value, "UNKNOWN_INSTRUMENT",
                                f"{symbol} 不在 Backpack 永续列表里，"
                                "拿不到精度就无法安全下单")
        return meta

    async def fetch_fee_schedule(self, symbols: Sequence[str]) -> Sequence[FeeSchedule]:
        """用户实际费率档位，只有签名的 `GET /api/v1/account`（accountQuery）拿得到。

        - **账户级一档吃全市场**（档位按 30 天量每小时重算，但对任何时刻的账户
          就是一档），所以无论 symbols 传什么都返回一条 symbol="" 的账户级档位，
          session.fee_for 查不到具体品种时自动落到它。
        - `futuresMakerFee/futuresTakerFee` 是**基点 decimal 字符串**（spec 原文
          "in basis points. Negative if a rebate exists."），除以 10000 得小数。
          符号约定正=成本、maker 负=返佣，与仓库一致**不翻转**（只有 OKX 要翻）。
        - **查不到就返回空元组**，绝不 maker=0/taker=0 占位（零费率会让四笔 taker
          成本凭空消失），也绝不把挂牌基础档冒充账户档位返回——调用方按
          "返回了 = 真档位"处理（KuCoin 修过这个病）。上层退回 DEFAULT_TAKER
          并在界面标"按默认费率估算"。
        """
        del symbols     # 账户级档位与符号无关；显式丢弃防止误用
        if not (self._cred.api_key and self._cred.api_secret):
            # 没配凭据是**确定性的没有**，返回空让上层退回挂牌价并标注。
            # 但真实错误（限频/5xx/签名坏）必须**上抛**而不是吞成空：
            # session.refresh_fees 对异常会记 fee_errors 并丢掉旧缓存，
            # 对空返回却会建一个"已取到（空）"的缓存条目——一次瞬时 429
            # 就让这家 24 小时不再重试、界面上连个错误都不显示
            return ()
        data = await self._request("GET", "/api/v1/account", quota="market",
                                   signed=True) or {}
        maker = _dec_opt(data.get("futuresMakerFee"))
        taker = _dec_opt(data.get("futuresTakerFee"))
        if maker is None or taker is None:
            return ()
        return (FeeSchedule(market=self.market, symbol="",
                            maker=maker / BPS, taker=taker / BPS,
                            note=ACTUAL_FEE_NOTE),)

    async def fetch_funding_limits(self, symbol: str) -> tuple[Decimal, Decimal] | None:
        """返回 **(cap, floor)**，cap >= floor；查不到返回 None。

        上下限在 market 行的 fundingRateUpperBound/LowerBound，**单位基点**，
        _MarketMeta 构造时已除以 10000（markPrices 的 fundingRate 是小数，
        单位必须先对齐才能 clamp）。实测 3 个市场 ±100bps（BTC/ETH/SOL 档）、
        其余 81 个 ±150bps——各合约不同，不能拿 BTC 的值套全市场。这是**每期**
        （1 小时）的夹子，年化归一是上层的事。
        """
        await self._ensure_meta([symbol])
        meta = self._meta.get(symbol)
        if meta is None or meta.cap is None or meta.floor is None:
            return None
        if meta.cap < meta.floor:
            # 数据坏了（上下限倒挂）。悄悄交换是替交易所圆谎，返回 None 让
            # scoring 跳过 clamp——它对"没数据"的处理是诚实的
            return None
        return meta.cap, meta.floor

    # ---- 行情 -----------------------------------------------------------

    @staticmethod
    def _best_levels(data: Mapping[str, Any]) -> tuple[list, list]:
        """depth 响应的 bids/asks，缺一侧就抛错。

        **大坑：bids 是价格升序——best bid 在 `bids[-1]`**（线上实测钉死）；
        asks 也升序，best ask 是 `asks[0]`。照币安肌肉记忆取 bids[0] 会把买一价
        读成带宽最远端，价差算出来大得离谱。缺 bids/asks 必须抛错，不能退化成
        深度 0——scoring 里 depth_cap<=0 会跳过容量判断，等于把"没数据"读成
        "深度无限"。
        """
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        if not bids or not asks:
            raise ExchangeError(Venue.BACKPACK.value, "EMPTY_BOOK",
                                "盘口单边为空", retriable=True)
        return bids, asks

    async def fetch_book_top(self, symbol: str) -> BookTop:
        """一档盘口。**这家没有 REST bookTicker**（/api/v1/ticker 只有 24h 统计、
        没有 bid/ask 字段），只能用 `depth?limit=5` 凑。limit 是枚举字符串。
        数量单位就是 base 币，不用换算；timestamp 是**微秒**。"""
        data = await self._request("GET", "/api/v1/depth", quota="market",
                                   params={"symbol": symbol,
                                           "limit": BOOK_TOP_LIMIT}) or {}
        bids, asks = self._best_levels(data)
        best_bid, best_ask = bids[-1], asks[0]      # bids 升序：最优在最后
        bid_p, ask_p = _dec(best_bid[0]), _dec(best_ask[0])
        if bid_p <= 0 or ask_p <= 0:
            raise ExchangeError(self.venue.value, "EMPTY_BOOK",
                                f"{symbol} 盘口价格非正", retriable=True)
        return BookTop(
            bid_price=bid_p,
            bid_qty=_dec(best_bid[1]),
            ask_price=ask_p,
            ask_qty=_dec(best_ask[1]),
            # 取不到用本地时钟兜底，**别填 0**：is_fresh 会把 0 判成永远陈旧
            ts_ms=_ts_to_ms(data.get("timestamp")) or self.timestamp_ms(),
        )

    @staticmethod
    def _band_side(levels: Sequence[Sequence[Any]], lo: Decimal,
                   hi: Decimal) -> tuple[Decimal, int]:
        """一侧盘口落在 [lo, hi] 内的累计名义额和档数（边界含）。

        两侧数组都最多 DEPTH_LEVELS_PER_SIDE 条，全量扫一遍比针对"bids 升序、
        asks 也升序"这个反直觉排列写方向敏感的提前 break 更不容易写反。
        价或量 <=0 的脏档既不计名义额也不计 levels_used——否则"档位够不够覆盖
        带宽"这个判断会被噪声灌水。名义额 = 价 × 量（数量就是 base 币，无乘数）。
        """
        notional, used = ZERO, 0
        for level in levels:
            if len(level) < 2:
                continue
            price, qty = _dec(level[0]), _dec(level[1])
            if price <= 0 or qty <= 0:
                continue
            if lo <= price <= hi:
                notional += price * qty
                used += 1
        return notional, used

    async def fetch_book_depth(self, symbol: str, band_bp: float = 10.0) -> BookDepth:
        """band_bp 基点带宽内的可见深度（名义额 quote 计价，数量 base 币计价）。

        只用一档当深度代理会把容量系统性低估一两个数量级——这个方法存在的全部
        理由就是修这个。档位不够时**不外推**，levels_used 如实报两侧档数之和，
        接近 2×DEPTH_LEVELS_PER_SIDE 说明簿被 limit 截断、带宽可能没盖满。
        """
        if band_bp <= 0:
            # 带宽非正 ⇒ 深度恒为 0 ⇒ scoring 把能做的机会判成不可执行。
            # 静默返回 0 比抛异常危险得多，入口直接炸，且不发请求。
            raise ValueError(f"band_bp 必须为正基点数，收到 {band_bp!r}")
        data = await self._request("GET", "/api/v1/depth", quota="market",
                                   params={"symbol": symbol,
                                           "limit": DEPTH_LIMIT}) or {}
        bids, asks = self._best_levels(data)
        best_bid, best_ask = _dec(bids[-1][0]), _dec(asks[0][0])
        if best_bid <= 0 or best_ask <= 0:
            raise ExchangeError(self.venue.value, "EMPTY_BOOK",
                                f"{symbol} 盘口价格非正", retriable=True)
        mid = (best_bid + best_ask) / 2
        # band_bp 对外是 float，**只在这一处**经 str 转 Decimal，之后全程 Decimal——
        # 直接 Decimal(10.0) 会带一串二进制尾巴进价格比较，恰好压在边界上的那一档
        # 算不算就成了掷骰子。
        band = Decimal(str(band_bp)) / BPS
        lo, hi = mid * (1 - band), mid * (1 + band)
        bid_notional, bid_levels = self._band_side(bids, lo, mid)
        ask_notional, ask_levels = self._band_side(asks, mid, hi)
        return BookDepth(
            symbol=symbol,
            bid_notional=bid_notional,
            ask_notional=ask_notional,
            bid_price=best_bid,
            bid_qty=_dec(bids[-1][1]),
            ask_price=best_ask,
            ask_qty=_dec(asks[0][1]),
            levels_used=bid_levels + ask_levels,
            band_bp=band_bp,
            ts_ms=_ts_to_ms(data.get("timestamp")) or self.timestamp_ms(),
        )

    async def fetch_carry_rates(self, symbols: Sequence[str] | None = None
                                ) -> Sequence[CarryRate]:
        """当前资金费。`GET /api/v1/markPrices` 不带 symbol 一次拉回全部 PERP 的
        费率+下次结算时刻——批量扫描就这一发，别逐符号打 84 次。

        **符号用交易所原始约定：正 = 多头付空头，原样存不取反**（取反会让
        `short_rate - long_rate` 全体反号，把亏钱的对推荐成赚钱的）。
        `fundingRate` 是**当期小数费率**（与 market 行的基点上下限单位不同）。
        interval_s 来自 markets 表（逐合约字段），所以先刷一遍 _meta——顺带把
        过滤链走一遍：被挡掉的市场（CancelOnly/隐藏/RWA）不产出费率行，
        免得配出一条根本下不了单的腿。
        """
        # 白名单用**这一轮**过滤链的产出，不用 self._meta：_meta 里可能躺着
        # _ensure_meta 为持仓兜底加进来的 CancelOnly/下架市场——那些条目的
        # 存在理由是"仓位要能折算"，不是"可以配对"。拿 _meta 当白名单，
        # 下不了单的市场就会带着费率进机会榜。
        tradable = {m.symbol: m for m in await self._load_markets()}
        rows = await self._request("GET", "/api/v1/markPrices",
                                   quota="market") or []
        wanted = set(symbols) if symbols else None
        now_ms = self.timestamp_ms()
        out: list[CarryRate] = []
        for row in rows:
            symbol = str(row.get("symbol") or "")
            if wanted is not None and symbol not in wanted:
                continue
            meta = tradable.get(symbol)
            if meta is None:
                continue        # 不在过滤链白名单里（含 spot 行防御）
            rate = _dec_opt(row.get("fundingRate"))
            if rate is None:
                # 当期费率缺失就跳过；补 0 会让它看起来像"费率恰好为零"的可做机会
                continue
            out.append(CarryRate(
                market=self.market, symbol=symbol, rate=rate,
                interval_s=meta.funding_interval_s,
                next_settle_ms=int(_dec(row.get("nextFundingTimestamp"), "0")),
                ts_ms=now_ms))
        return tuple(out)

    async def fetch_funding_history(self, symbol: str, since_ms: int,
                                    limit: int = 1000
                                    ) -> Sequence[tuple[int, Decimal]]:
        """历史资金费，**升序**返回 [since_ms, 现在] 的完整区间。
        `GET /api/v1/fundingRates?symbol=&limit=&offset=`（公开，无需签名）。

        1. **返回降序（最新在前）、offset 从最新往回数**（线上实测：offset=5 精确
           接在第 5 条后面）——第一页永远覆盖窗口末端，天然满足基类"必须覆盖到
           最新一条"的契约，不用像币安那样掰方向。翻页顺着 offset 往旧走，
           翻到 since_ms 之前即停，最后整体升序返回。
        2. `limit` 是**页大小语义**（基类契约），不是输出条数上限——**绝不拿它
           截尾输出**（HL/KuCoin 都因截尾修过：1h 结算下 1000 条只有 41 天，
           90 天窗口被无声斩掉）。单页上限 10000。
        3. `intervalEndTimestamp` 是 **naive UTC datetime 串**（"2026-08-05T17:00:00"
           没有 Z），不是毫秒——见 _naive_utc_ms 的时区坑。
        4. 结算时刻用 dict **去重**：offset 翻页途中跨过一次整点结算，整个列表
           会下移一位，同一期进两次；重复值会抬高 autocorr()、拖偏 median 且不报错。
        5. 未知符号 200 `[]` 不报错（实测），如实返回空。
        6. 历史桶限频 30 req/min（是否含本端点 UNVERIFIED，按最坏情况跨页限速）。
        """
        floor_ms = max(int(since_ms), 0)
        page_size = max(1, min(int(limit) if limit > 0 else 1000,
                               FUNDING_HISTORY_MAX_PAGE_SIZE))
        # 线上实测（2026-08-06）：第一行是**还没结算的当前周期**——
        # intervalEndTimestamp 在未来、fundingRate 还在随溢价变化。把它当已结算
        # 历史收进来，库的按时间戳去重会把半路抓到的**临时值永久钉死**（之后的
        # 回填被去重挡住，永远不会被结算终值纠正），稳定性评分吃进假样本。
        # 只收 ts <= 现在的行；这一发现来自实盘冒烟，离线 mock 测不出来。
        now_ms = self.timestamp_ms()
        collected: dict[int, Decimal] = {}
        offset = 0
        prev_oldest: int | None = None
        for page in range(FUNDING_HISTORY_MAX_PAGES):
            if page:
                await asyncio.sleep(FUNDING_HISTORY_PAGE_PACE_S)
            rows = await self._request(
                "GET", "/api/v1/fundingRates", quota="market",
                params={"symbol": symbol, "limit": page_size,
                        "offset": offset}) or []
            oldest: int | None = None
            for row in rows:
                ts = _naive_utc_ms(row.get("intervalEndTimestamp"))
                if ts <= 0:
                    continue
                oldest = ts if oldest is None else min(oldest, ts)
                if floor_ms <= ts <= now_ms:
                    collected[ts] = _dec(row.get("fundingRate"))
            if not rows or len(rows) < page_size:
                break               # 到底了（含未知符号的空响应）
            if oldest is None:
                break               # 整页解析不出时间戳，形状变了，别盲翻
            if oldest < floor_ms:
                break               # 已翻过窗口起点
            if prev_oldest is not None and oldest >= prev_oldest:
                break               # 游标不前进（服务端无视 offset），再翻是死循环
            prev_oldest = oldest
            offset += page_size
        # 不按 limit 截：见 docstring。gate/binance/hyperliquid/kucoin 也是整段返回
        return tuple(sorted(collected.items()))

    # ---- WebSocket -------------------------------------------------------

    def stream_book_top(self, symbols: Sequence[str]) -> AsyncIterator[BookTop]:
        """`bookTicker.<symbol>` 盘口流。写成普通 def 返回异步生成器
        （Bitget/KuCoin 同款）：调用方不 await 也能拿到迭代器。

        **BookTop 没有 symbol 字段**，多符号订阅时下游分不清来源——实际使用建议
        一个符号一条流。TODO（最小可用版）：增量深度 depth.<symbol> 没接、只有
        一档 BBO；`u` 序列号只用于丢乱序包、断号没有触发重拉快照。
        """
        return self._book_stream(tuple(symbols))

    async def _book_stream(self, symbols: Sequence[str]) -> AsyncIterator[BookTop]:
        import websockets      # 延迟导入：只跑 REST 的场景不该被这个依赖拖着

        valid = [s for s in symbols if WS_SYMBOL_RE.match(s)]
        if not valid:
            return
        backoff = WS_RECONNECT_MIN_S
        last_seq: dict[str, int] = {}
        while True:
            try:
                # ping_interval=None：服务器每 60s 发协议级 Ping、120s 没 Pong 就断，
                # websockets 库对收到的 Ping 自动回 Pong（与 ping_interval 无关），
                # 不需要再叠一层客户端主动心跳。
                async with websockets.connect(self.ws_base, ping_interval=None,
                                              max_queue=2048) as ws:
                    await ws.send(json.dumps({
                        "method": "SUBSCRIBE",
                        "params": [f"bookTicker.{s}" for s in valid]}))
                    backoff = WS_RECONNECT_MIN_S
                    async for raw in ws:
                        if isinstance(raw, bytes):
                            raw = raw.decode()
                        msg = json.loads(raw, parse_float=Decimal)
                        if not isinstance(msg, dict):
                            continue
                        if msg.get("error"):
                            # 订阅被拒：整条连接重来。静默 continue 会变成一条
                            # 永远不推数据的僵尸流。
                            break
                        data = msg.get("data")
                        if not isinstance(data, dict) or data.get("e") != "bookTicker":
                            continue
                        symbol = str(data.get("s") or "")
                        # u 是更新序列号，乱序/重放包直接丢
                        try:
                            seq = int(_dec(data.get("u"), "0"))
                        except (ValueError, InvalidOperation):
                            seq = 0
                        if seq and seq < last_seq.get(symbol, 0):
                            continue
                        if seq:
                            last_seq[symbol] = seq
                        top = self._ws_to_top(data)
                        if top is not None:
                            yield top
            except asyncio.CancelledError:
                raise       # 必须原样上抛，被 except Exception 吞掉就取消不掉任务了
            except Exception:
                pass
            # 抖动是为了让一堆连接不要踩着同一秒集体回来
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, WS_MAX_BACKOFF_S)

    def _ws_to_top(self, item: Mapping[str, Any]) -> BookTop | None:
        """bookTicker 推送 → BookTop。**某侧簿空时 a/A/b/B 是 null**，要判空；
        E（引擎时间戳）是**微秒**。数量就是 base 币，不换算。"""
        bid, ask = item.get("b"), item.get("a")
        if bid in (None, "") or ask in (None, ""):
            return None     # 单边为空，这一拍不可用
        return BookTop(
            bid_price=_dec(bid),
            bid_qty=_dec(item.get("B")),
            ask_price=_dec(ask),
            ask_qty=_dec(item.get("A")),
            ts_ms=_ts_to_ms(item.get("E")) or self.timestamp_ms())

    # ---- 精度 / clientId 映射 --------------------------------------------

    @staticmethod
    def round_price(price: Decimal, tick: Decimal, side: str) -> Decimal:
        """价格规整到 tickSize 的整数倍：**买单向上、卖单向下**，方向与
        executor.cross_price 一致。保护限价是**故意穿透对手价**的，反向取整会把
        穿透削回来；在 tick 相对价格很粗的小币上那不是少赚一个 tick，而是 IOC
        根本挂不上、这条腿永远补不平。"""
        if tick <= 0:
            return price
        return _round_step(price, tick,
                           ROUND_CEILING if side.lower() == "buy" else ROUND_FLOOR)

    def _client_id_u32(self, raw: str) -> int:
        """字符串 client_order_id → uint32 clientId 的确定性映射（crc32）。

        Backpack 的 clientId 是 **0..4294967295 的 JSON 整数**，不是字符串。
        映射必须确定性——撤单/查单用的必须是下单时的同一个数，否则消解流程
        整个失效。**不做"纯数字就直接 int"的双路径**：同一条规则覆盖所有输入
        才不会出现"下单走 int、回查走 crc32"的错配。crc32 有碰撞概率
        （约 1/4e9），但 clientId 在这里只是查找键不是幂等闸（服务端是否拒重
        UNVERIFIED），碰撞的后果是历史端点自配时多匹配一条旧单——按 Desc
        排序取第一条已把风险压到忽略不计。
        """
        if not raw:
            raise ValueError(
                "backpack 下单必须带 client_order_id：clientId 是查单/撤单唯一"
                "能自配的抓手（查单两段式全靠它），无 ID 下单等于放弃消解能力")
        u32 = zlib.crc32(raw.encode("utf-8")) & 0xFFFFFFFF
        self._coid_by_u32[u32] = raw
        return u32

    @staticmethod
    def _tif_of(req: OrderRequest) -> tuple[str, bool]:
        """(timeInForce, postOnly)。Backpack 的 TIF 枚举是 GTC/IOC/FOK 全支持，
        postOnly 是**独立布尔**不进 TIF。缺省值 spec 没写（UNVERIFIED，推测 GTC），
        所以这里**永远显式发** timeInForce，不赌服务端默认。认不出的值本地炸，
        绝不静默降级——降级 FOK 成 IOC 会允许部分成交，那是隐形单腿的来源。"""
        tif = (req.time_in_force or "IOC").upper()
        mapped = _TIF.get(tif)
        if mapped is None:
            raise OrderRejected(Venue.BACKPACK.value, "TIF_UNSUPPORTED",
                                f"backpack 不认识 timeInForce={tif!r}"
                                "（只支持 GTC/IOC/FOK/挂单别名）")
        return mapped, tif in _POST_ONLY_TIF

    # ---- 交易 -----------------------------------------------------------

    async def place_order(self, req: OrderRequest) -> OrderResult:
        """下单 `POST /api/v1/order`（orderExecute，quota="trade"）。

        响应 200 是撮合引擎的**同步终态**（单线性命令流，HTTP 响应就是执行结果）：
        IOC 单直接拿到 Filled/Expired，不像 KuCoin 那样要紧跟一次查单。但响应
        **没有均价字段、没有手续费字段**：均价 = executedQuoteQuantity /
        executedQuantity 自己除，手续费只能事后查 /wapi/v1/history/fills
        （fee 填 0 是"未知"的诚实表达，不是免费）。

        所有非 postOnly 单吃 **100ms taker 速度带**（撤单豁免）——500ms 收敛循环
        的预算里要算上它。超时/断流/5xx/TIMEOUT/认不出的错误码一律抛 OrderUnknown，
        **绝不当失败返回**；**不注入任何返佣码**。
        """
        client_id = self._client_id_u32(req.client_order_id)
        meta = await self._meta_of(req.symbol)
        tif, post_only = self._tif_of(req)

        # 数量向下取整到 stepSize（宁可少下也不能超过目标），再过 min/max 守卫。
        # 取整后归零或不足最小量一律**本地** OrderRejected 不发请求：
        # 省一次 RTT，也不烧限频配额。
        qty = _round_step(req.qty, meta.step, ROUND_DOWN)
        if qty <= 0 or qty < meta.min_qty:
            raise OrderRejected(self.venue.value, "PINCHED",
                                f"{req.symbol} {req.qty} 取整到 step={meta.step} "
                                f"后不足最小量 {meta.min_qty}")
        if meta.max_qty is not None and meta.max_qty > 0 and qty > meta.max_qty:
            raise OrderRejected(self.venue.value, "ORDER_SIZE_TOO_LARGE",
                                f"{req.symbol} {qty} > 单笔上限 {meta.max_qty}")

        body: dict[str, Any] = {
            "symbol": req.symbol,
            # side 是 Bid/Ask，不是 buy/sell——发错会 INVALID_ORDER
            "side": "Bid" if req.side.lower() == "buy" else "Ask",
            "orderType": "Limit" if req.price is not None else "Market",
            "quantity": _dec_str(qty),      # decimal 一律字符串型
            "clientId": client_id,          # uint32 JSON 整数
        }
        if req.price is not None:
            body["price"] = _dec_str(self.round_price(req.price, meta.tick,
                                                      req.side))
            body["timeInForce"] = tif
            if post_only:
                body["postOnly"] = True     # json.dumps 布尔小写，与签名串一致
        if req.reduce_only:
            body["reduceOnly"] = True

        try:
            data = await self._request("POST", "/api/v1/order", quota="trade",
                                       signed=True, body=body)
        except OrderRejected:
            raise           # 明确被拒 = 撮合肯定没收到，不必白跑一轮消解
        except RateLimited:
            raise           # 网关限频 = 确定没进撮合，可安全重试
        except ExchangeError as exc:
            if exc.code in _FATAL_AUTH_CODES or exc.code in ("NO_CREDENTIAL",
                                                             "BAD_CREDENTIAL",
                                                             "NO_INSTRUCTION"):
                # 签名层就被挡下（或请求根本没发出去），撮合没见过这个请求，
                # 升级成 unknown 只会白跑一轮 cancel+查单
                raise
            # 其余全部倒向未知：NETWORK / BAD_JSON / 5xx / TIMEOUT / MAINTENANCE，
            # 以及**认不出的错误码**。判定标准一句话——只有能证明"没进撮合"时
            # 才允许说"失败"，证不了就是未知。多跑一轮消解只花一次 RTT，
            # 误判是双倍仓位。
            raise OrderUnknown(self.venue.value, req.client_order_id) from exc
        except httpx.TransportError as exc:
            # _request 已把 httpx.HTTPError 全族包成 NETWORK，这条是八家统一的
            # 兜底：TransportError 覆盖 Timeout/Network/Protocol/Proxy 全族
            raise OrderUnknown(self.venue.value, req.client_order_id) from exc

        if not isinstance(data, Mapping):
            # 2xx 但没有订单对象（空 body？）：无法证明引擎没收到，按未知走
            raise OrderUnknown(self.venue.value, req.client_order_id)
        return self._order_to_result(data, fallback_client_id=req.client_order_id)

    def _order_to_result(self, row: Mapping[str, Any],
                         fallback_client_id: str = "") -> OrderResult:
        """订单对象（下单响应/挂单列表/历史单通用）→ OrderResult。

        - 均价自己除：executedQuantity=0 时置 0，绝不拿 price 顶替（限价单的
          price 是委托价不是成交价）。
        - fee 恒为 0：订单对象**根本没有手续费字段**（fills 历史才有），
          填一个猜出来的费用比填 0 危险——0 至少是"未知"的诚实表达。
        - clientId 反查回原字符串 coid：反查表只在本进程存活期有效，查不到时
          退回数字串（上层按 exchange_order_id 兜底）。
        - 认不出的 status 映射成 "new"（非终态）：让上层继续轮询比谎报终态安全。
        """
        raw_client = row.get("clientId")
        client_oid = fallback_client_id
        if not client_oid and raw_client is not None:
            try:
                client_oid = self._coid_by_u32.get(int(_dec(raw_client)),
                                                   str(raw_client))
            except (ValueError, InvalidOperation):
                client_oid = str(raw_client)
        executed = _dec(row.get("executedQuantity"))
        quote = _dec(row.get("executedQuoteQuantity"))
        return OrderResult(
            client_order_id=client_oid,
            exchange_order_id=str(row.get("id") or ""),
            status=_STATUS_MAP.get(str(row.get("status") or ""), "new"),
            filled_qty=executed,
            avg_price=quote / executed if executed > 0 else ZERO,
            fee=ZERO, fee_asset="")

    async def cancel_order(self, symbol: str, client_order_id: str) -> None:
        """撤单 `DELETE /api/v1/order`（orderCancel，quota="risk"）。

        - body 只带 clientId，**绝不同时带 orderId**——文档原文：两个都给时
          签名会验不过。
        - 200 = 被撤订单完整对象（含撤前 executedQuantity）；**202 = 已受理但尚未
          执行**（空 body，_parse 返回 None）——202 不是终态，消解流程接下来的
          query_order 自然会确认，这里不用额外动作。
        - 404 RESOURCE_NOT_FOUND = 单子不在簿上（已成交/已撤/从未存在，三者在
          这里分不出来）——撤单的目的（状态终结）已达成，就地吞掉让消解流程
          继续去查成交量；抛出去会让 resolve_unknown_order 带着未捕获异常崩掉
          （它只捕获 OrderRejected/httpx.HTTPError/RateLimited）。
        """
        client_id = self._client_id_u32(client_order_id)
        try:
            await self._request("DELETE", "/api/v1/order", quota="risk",
                                signed=True,
                                body={"symbol": symbol, "clientId": client_id})
        except ExchangeError as exc:
            if exc.code == "RESOURCE_NOT_FOUND":
                return
            raise self._risk_path_error(exc) from exc

    async def query_order(self, symbol: str, client_order_id: str) -> OrderResult:
        """查单，**两段式**（quota="risk"）。

        `GET /api/v1/order` **只认还挂在簿上的单**：已完全成交/已撤/已过期一律
        404 RESOURCE_NOT_FOUND——**404 绝不能直接当"订单从未被接受"**，已成交的
        单在这也是 404，误判会让 resolve_unknown_order 把已成交仓位当成 0，
        上层照着 0 再补一次仓 = 双倍仓位。404 后转 `/wapi/v1/history/orders`
        按 clientId 字段自配（历史端点没有 clientId 服务端过滤）；两段都查无
        此单才抛 OrderRejected 让消解流程定性"从未被接受"。

        残余风险（UNVERIFIED）：历史端点的索引延迟没有文档承诺。消解流程是
        cancel 在前（状态已终结）、查单在后，真实成交至少已经发生在 cancel 之前，
        历史端点大概率已可见；万一漏了，最后一道防线是仓位差分（持仓是真值）。
        """
        client_id = self._client_id_u32(client_order_id)
        try:
            data = await self._request("GET", "/api/v1/order", quota="risk",
                                       signed=True,
                                       params={"symbol": symbol,
                                               "clientId": client_id})
        except ExchangeError as exc:
            if exc.code != "RESOURCE_NOT_FOUND":
                raise self._risk_path_error(exc) from exc
            data = None
        if isinstance(data, Mapping) and data:
            return self._order_to_result(data, fallback_client_id=client_order_id)

        row = await self._history_order_by_client_id(symbol, client_id)
        if row is not None:
            return self._order_to_result(row, fallback_client_id=client_order_id)
        raise OrderRejected(self.venue.value, "ORDER_NOT_FOUND",
                            f"{symbol} 在簿/历史两段都查无 {client_order_id}"
                            f"（clientId={client_id}）")

    async def _history_order_by_client_id(self, symbol: str, client_id: int
                                          ) -> Mapping[str, Any] | None:
        """`GET /wapi/v1/history/orders`（orderHistoryQueryAll）按 clientId 自配。

        端点没有 from/to 时间过滤、不能按 clientId 服务端过滤，只能拉回来配。
        sortDirection=Desc 让最新的单在前——消解找的都是刚下的单，正常首页就中。
        **clientId 的类型在各端点间漂移**（下单是 uint32、fills 历史是 string），
        这里用 str() 归一后比较，两种形状都吃。
        """
        offset = 0
        want = str(client_id)
        for _ in range(ORDER_HISTORY_MAX_PAGES):
            try:
                rows = await self._request(
                    "GET", "/wapi/v1/history/orders", quota="risk", signed=True,
                    params={"symbol": symbol, "limit": ORDER_HISTORY_PAGE_SIZE,
                            "offset": offset, "sortDirection": "Desc"}) or []
            except ExchangeError as exc:
                raise self._risk_path_error(exc) from exc
            for row in rows:
                if str(row.get("clientId")) == want:
                    return row
            if len(rows) < ORDER_HISTORY_PAGE_SIZE:
                return None
            offset += ORDER_HISTORY_PAGE_SIZE
        return None

    async def fetch_positions(self, symbols: Sequence[str] | None = None
                              ) -> Sequence[Position]:
        """持仓 `GET /api/v1/position`（positionQuery，quota="risk"）。持仓是
        **真值**，本地记账与它冲突时无条件以它为准；session.validate_account 也
        拿它做连通性自检，所以**零仓账户上也必须能正常返回空序列**（实测无仓
        返回 `[]`；404 的确切触发条件 UNVERIFIED，只对坏符号出现）。

        - `netQuantity` **正=多、负=空**（净持仓模型，无双向对冲仓），单位就是
          base 币，直接进 Position.qty 不换算。
        - `estLiquidationPrice` 拿到 0/空一律填 None：填 0 会让
          liquidation_distance 算出"还有 100% 空间"，风控直接失明。
        - leverage 填 1/imf（该仓位的初始保证金率倒数）：Backpack 是全仓账户级
          杠杆，仓位行里没有"实际杠杆"字段，1/imf 是交易所自己给的结构性上限，
          比塞死 1 诚实；真实账户杠杆要 join 权益，那是风控层的事。
        - 符号过滤在客户端做：positionQuery 带 symbol 参数的签名路径没实测过，
          全量拉回来筛一样对，还少一条 UNVERIFIED。
        """
        rows = await self._request("GET", "/api/v1/position", quota="risk",
                                   signed=True) or []
        wanted = set(symbols) if symbols else None
        out: list[Position] = []
        for row in rows:
            symbol = str(row.get("symbol") or "")
            if not symbol or (wanted is not None and symbol not in wanted):
                continue
            liq = _dec(row.get("estLiquidationPrice"))
            imf = _dec(row.get("imf"))
            out.append(Position(
                market=self.market,
                symbol=symbol,
                qty=_dec(row.get("netQuantity")),
                entry_price=_dec(row.get("entryPrice")),
                mark_price=_dec(row.get("markPrice")),
                liquidation_price=liq if liq > 0 else None,
                leverage=(Decimal("1") / imf).quantize(
                    Decimal("0.01"), rounding=ROUND_DOWN) if imf > 0
                    else Decimal("1")))
        return tuple(out)

    async def fetch_open_orders(self, symbol: str) -> Sequence[OrderResult]:
        """未成交挂单 `GET /api/v1/orders`（orderQueryAll，quota="risk"）。

        判断两腿是否平衡必须计入这些——「A 已成交、B 还挂着」是限价模式下最
        容易出现的隐形单腿。响应是 Limit/Market 混合数组（按 orderType 判别，
        字段同下单响应），一次给全、无分页。client_order_id 经 crc32 反查表还原；
        进程重启后反查表是空的，那时报数字串、上层按 exchange_order_id 对账。
        """
        rows = await self._request("GET", "/api/v1/orders", quota="risk",
                                   signed=True, params={"symbol": symbol}) or []
        return tuple(self._order_to_result(row) for row in rows
                     if isinstance(row, Mapping))

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        """设杠杆。**账户级旋钮**：`PATCH /api/v1/account`（accountUpdate）改
        `leverageLimit`，**没有逐合约杠杆端点**——symbol 参数只能忽略（保留签名
        是为了兼容基类接口）。

        两腿同所对冲时这反而省事，但两条腿的引擎会各自调一次 set_leverage：
        **设前先读**，值已一致就不发 PATCH，避免两边反复互写（哪怕写的是同一个
        值，也是白烧 risk 配额）。走 quota="risk"：降杠杆是风控动作，不能被行情
        轮询挤掉。上限官网标 50x（UNVERIFIED），这里不本地夹——让交易所自己拒
        （MAX_LEVERAGE_REACHED），错误信息比本地猜的上限准。
        """
        del symbol      # 账户级杠杆，没有逐合约端点；显式丢弃防止误会
        if leverage < 1:
            raise ValueError(f"backpack 杠杆必须 >= 1，收到 {leverage}")
        data = await self._request("GET", "/api/v1/account", quota="risk",
                                   signed=True) or {}
        current = _dec_opt(data.get("leverageLimit"))
        if current is not None and current == leverage:
            return
        await self._request("PATCH", "/api/v1/account", quota="risk",
                            signed=True,
                            body={"leverageLimit": _dec_str(leverage)})

    async def fetch_leverage_tiers(self, symbol: str
                                   ) -> Sequence[tuple[Decimal, Decimal]]:
        """杠杆档位 [(名义上限, 最大杠杆), ...] 升序。

        Backpack **没有档位表端点**：保证金率是每市场的 sqrt imfFunction
        （IMF 随仓位连续增长，不是阶梯），"避开档位边界"的逻辑对这家天然不适用。
        返回单档 [(openInterestLimit × markPrice, 1/imf.base)] 让上层至少看得见
        名义硬顶；markPrice 取不到时退化成 (Infinity, 上限)——**退化后名义上限
        等于失效**，但绝不返回空列表：空列表和"没有档位限制"在上层是两种完全
        不同的含义。imfFunction 的精确公式 UNVERIFIED（spec 只给参数）。
        """
        meta = await self._meta_of(symbol)
        cap = Decimal("Infinity")
        if meta.oi_limit > 0:
            try:
                rows = await self._request("GET", "/api/v1/markPrices",
                                           quota="market",
                                           params={"symbol": symbol}) or []
                # 传了 symbol 也返回数组（实测），取匹配行防御服务端行为变化
                mark = ZERO
                for row in rows:
                    if str(row.get("symbol") or "") == symbol:
                        mark = _dec(row.get("markPrice"))
                        break
                if mark > 0:
                    cap = meta.oi_limit * mark
            except ExchangeError:
                pass        # 拿不到标记价就退化成 Infinity，档位本身还在
        return ((cap, meta.max_leverage),)

    # ---- 附加能力（不在抽象接口里）---------------------------------------

    async def fetch_order_fees(self, symbol: str, order_id: str
                               ) -> tuple[Decimal, str]:
        """按交易所单号汇总实际手续费，`GET /wapi/v1/history/fills`
        （fillHistoryQueryAll，quota="risk"）。

        订单对象没有手续费字段，这里是**唯一来源**。fee 正数=成本（maker 返佣
        为负），与仓库约定一致。fillType=User 滤掉清算/ADL 系统单。只在需要精确
        成本核算时调，别每单都调——/wapi/1 历史桶可能只有 30 req/min
        （UNVERIFIED）。分页硬闸：单个订单的成交笔数有限，翻不完说明响应形状
        变了，宁可停在闸上也别无限翻页。
        """
        total, asset = ZERO, ""
        offset = 0
        for _ in range(ORDER_HISTORY_MAX_PAGES):
            try:
                rows = await self._request(
                    "GET", "/wapi/v1/history/fills", quota="risk", signed=True,
                    params={"symbol": symbol, "orderId": order_id,
                            "fillType": "User",
                            "limit": ORDER_HISTORY_PAGE_SIZE,
                            "offset": offset}) or []
            except ExchangeError as exc:
                raise self._risk_path_error(exc) from exc
            for row in rows:
                total += _dec(row.get("fee"))
                asset = asset or str(row.get("feeSymbol") or "")
            if len(rows) < ORDER_HISTORY_PAGE_SIZE:
                break
            offset += ORDER_HISTORY_PAGE_SIZE
        return total, asset
