"""KuCoin Futures 适配器（合约 REST v1/v2/v3 混编，linear 永续 `kucoin:perp`）。

调研依据：`docs/research/exchange-kucoin.md`（2026-08，逐字读官方 docs-new 页面）。
端点/字段/错误码一律以那份文档为准；文档没写死的在这里标 **UNVERIFIED**，不猜。

本家的四条特性决定了这份实现的形状：

1. **下单数量单位是"张"（lot），size 必须是正整数**，1 张 = multiplier 个 base 币
   （XBTUSDTM 的 multiplier=0.001，size=1 只有 0.001 BTC）。沿用 okx/gate 的硬性约定：
   **对外一律 base 币，张↔币换算封死在内部**。漏乘一次 multiplier 在 BTC 上就是
   1000 倍错配——签名错立刻报错，量级错是静默成交。
2. **签名的字节必须与发出去的字节逐字节相同**，且官方要求 *URL 用编码形式、签名用
   未编码原文*：query 由 `_prepare` 亲手拼进 URL、body 自己 dumps 后用 `content=` 发
   —— 这就是**必须覆写 `_request`** 的原因（基类走 `json=` 让 httpx 序列化，结果与
   签名串没有任何保证一致；okx.py 同理）。另外 `KC-API-SIGN` 是 **base64 不是 hex**，
   另五家里三家是 hex，写成 hexdigest 会稳定收 400005 而它不区分编码错/拼串错。
3. **逐仓根本没有"设杠杆"接口**，杠杆随下单参数走；按其余五家的惯例写会调到全仓那个
   端点、返回 200 `data:true` 而杠杆一点没变：**静默失效**。
4. **下单响应不带任何成交信息**，手续费也不在订单对象里：IOC 单必须紧跟一次
   `query_order`，精确手续费另走 `/api/v1/fills`。

`CarryRate.rate` 用**交易所原始约定**（正 = 多头付给空头），原样写入不取反
（base.py 那句"取负成多头视角"是过时说法，以 models.CarryRate 与 scoring 的
`short_rate - long_rate` 为准）。

返佣：**不注入任何返佣码/邀请码/broker code**。`broker_code` 照收、收下什么都不做
（用户明确要求）：不加 header、不加 body 字段、不给 clientOid 加前缀。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import random
import re
import uuid
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, AsyncIterator, Iterator, Mapping, Sequence
from urllib.parse import quote

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

# 合约专用域名。现货/统一账户才是 api.kucoin.com；合约的**账户类**接口（trade-fees
# 这种）同样在 api-futures 上，走错域名拿到的是 404000 而不是鉴权错，会带偏排查方向。
REST_BASE = "https://api-futures.kucoin.com"
SUCCESS_CODE = "200000"      # HTTP 200 也可能带业务错误码，只看 status_code 会漏判

# depth20 与 depth100 **同为 weight 5**，没理由为省一个不存在的配额要更浅的一档
# （10bp 在密集簿上 20 档常盖不满）；全量 snapshot 虽只要 weight 3 但流量大两个数量级，
# 文档自己写了它 "uses more server resources and traffic"，绝不放进轮询。
# UNVERIFIED：路径写作 /api/v1/level2/depth{size} 却只举了 20/100 两例，不去试探
# 未文档化的 size —— 拿不到会是 404000 而不是降级返回。
DEPTH_PATH = "/api/v1/level2/depth100"
DEPTH_LEVELS_PER_SIDE = 100

# 资金费历史按**时间窗口**向旧翻页（这个端点没有页码/游标）。单页条数上限文档没写
# （样例 69 条未截断）→ UNVERIFIED，用窗口规避。即便服务端真截断，它倒序（新→旧）
# 返回、截掉的必然是最旧那端，而游标 `to = min(timepoint)-1` 会在下一页补回来。
FUNDING_HISTORY_WINDOW_MS = 7 * 86_400_000
# 硬闸：7 天 × 40 页 = 280 天，比 history.py 默认回填的 90 天宽裕，同时挡住
# "服务端无视 from/to"时的无限翻页。
FUNDING_HISTORY_MAX_PAGES = 40

# pageSize 上限 1000（不是 100，二手资料常写错）；单合约最多 100 个活跃限价单，
# 200 一页够用，但仍按 totalPage 翻——别赌上限。
OPEN_ORDER_PAGE_SIZE = 200
OPEN_ORDER_MAX_PAGES = 8

# clientOid：≤40 字符，只允许数字/字母/下划线/连字符。**绝不做字符替换**——替换会破坏
# ID 的全局唯一性假设，上层拿原 ID 回查就查不到，而幂等正是靠它撑着。
CLIENT_OID_RE = re.compile(r"^[0-9A-Za-z_\-]{1,40}$")

WS_SYMBOL_RE = re.compile(r"^[A-Z0-9]+$")   # 先滤一道比事后猜订阅 ack 便宜
WS_SUBSCRIBE_BATCH = 100                    # 文档：单次订阅最多 100 个 topic
# **断连后重连前必须 sleep 3~5 秒**——文档因一次 DDoS 事故专门补的，
# 崩溃循环里的无退避重连会被当成攻击流量。
WS_RECONNECT_MIN_S = 3.0
WS_MAX_BACKOFF_S = 30.0
# 心跳参数由服务器在 bullet 响应里下发，这只是拿不到时的兜底，**别硬编码 30 秒**。
WS_PING_INTERVAL_FALLBACK_MS = 18_000

DEFAULT_FUNDING_INTERVAL_S = 28_800

# ---- 错误码分桶（判定顺序见 _translate，顺序本身有意义：先特判后兜底）--------

# 时差 >5 秒，**没有 recvWindow 可放宽**。发生在进撮合之前，订单确定不存在。
_CLOCK_CODES = frozenset({"400002"})
# 429000 是网关层（HTTP 429），1015 是 Cloudflare 层（body 可能压根不是 JSON）。
_RATE_LIMIT_CODES = frozenset({"429000", "1015"})
# 200002 **一码两义**：限频（封 10 秒，HTTP 可能仍是 200）vs "query scope for Level 3
# cannot exceed"（参数错）。不看 msg 就会把一个永远不会成功的参数错当限频无限重试。
_RATE_LIMIT_HINTS = ("too many requests", "retry later", "请求过于频繁", "频繁")

# 订单**存在过且已终结**（已成/已撤，因而不可撤）。绝不能和"订单不存在"混桶：混了
# 之后 query_order 会走 OrderRejected 让 resolve_unknown_order 返回 0，一张真成交的单
# 被报成零成交，上层照着 0 再补一次仓 = 双倍仓位。对 cancel_order 它是"目的已达成、
# 继续查单"（就地吞掉），对 query_order 原样上抛。
# （调研素材把 100004 归在 OrderRejected 下；改归这里，消解流程行为一样，
#  但 query_order 那侧不会再把已成交谎报成零成交。）
_ORDER_TERMINAL_CODES = frozenset({"100004"})
# UNVERIFIED：没有"订单不存在"的专用码（实测多半是 200000 但 data 为空，见 query_order）。
# 关键词只收明确点名订单的说法，且判定排在 TERMINAL 之后 ——
# "cannot be canceled because it does not exist or has been filled" 会同时命中两边。
_ORDER_GONE_HINTS = ("order does not exist", "order not exist", "order not found",
                     "订单不存在", "查无此单")
# clientOid 重复 = **幂等保护生效的证据**，说明上一发已经进了撮合。绝不能是
# OrderRejected —— 那等于告诉上层"没进去，换个 ID 重发"，而重发就是双倍仓位。
_DUPLICATE_CLIENT_OID_CODES = frozenset({"300018"})

# 明确的业务拒绝：进撮合前被挡下，订单确定不存在，改参数后可安全重发。
_REJECT_CODES = frozenset({
    "100001", "100003", "200003", "300000", "400100",   # 参数/合约/symbol 非法
    "300001", "300004",     # 活跃限价单(100)/条件单(200) 超限
    "300005",               # 超过最大风险限额
    "300006", "300007",     # 平仓价须高于破产价 / 价格劣于强平价
    "300008", "300013",     # 市场无对手单
    "300009", "300010",     # 持仓为 0 无法平仓 / 平仓失败
    "300011", "300012",     # 价格越 buyLimit / sellLimit
    "300016", "300017",     # 杠杆超上限 / Futures Brawl 持仓不可操作
    "330005", "330011",     # 需切全仓 / 需先设持仓模式
    "300003", "200005",     # 余额不足（至少 2 USDT）
})
# 服务端已收到请求但结果不明：下单路径必须按"未知"处理，当失败重发就是双倍仓位。
# UNVERIFIED：300002/300015 是"已明确拒绝"还是"已受理"文档只给了文案，
# 结算窗口期保守按未知走消解流程，代价只是多一次查单。
_RETRIABLE_CODES = frozenset({
    "500000",   # Internal Server Error —— 请求已到服务端，订单可能已进撮合
    "300014",   # 仓位正在被强平，下单/撤单都可能半途
    "300002",   # Order placement/cancellation suspended
    "300015",   # Contract/Funding is undergoing the settlement process
})
# 鉴权/时间戳层就被挡下，撮合根本没见过这个请求。下单路径不得升级成 OrderUnknown
# ——那只会白跑一轮 cancel+查单。
_PRE_MATCH_CODES = frozenset({
    "400001",   # 缺 header
    "400002",   # 时间戳偏差 >5s
    "400003", "400004", "400005",   # key 不存在 / passphrase 错 / 签名错
    "400006", "400007",             # IP 白名单 / 权限不足
    "415000", "411100", "40010", "404000",   # Content-Type / 冻结 / 地区 / URL 不存在
})
# 纯配置错，重试无意义。200001 = Level 2 查询范围超限（参数错，别跟 200002 混）。
_CONFIG_CODES = frozenset({"100002", "100005", "200001"})

# **文档两页矛盾**：Add Order 页只列 GTC/IOC/RPI，Get Order 页却列 GTC/GTT/IOC/FOK
# （UNVERIFIED）。以下单页为准，所以这里根本不提供 FOK 通道，见 _time_in_force。
_TIF = {"IOC": "IOC", "GTC": "GTC", "GTX": "GTC", "POST_ONLY": "GTC", "MAKER": "GTC"}
_POST_ONLY_TIF = frozenset({"GTX", "POST_ONLY", "MAKER"})

LISTED_FEE_NOTE = "KuCoin 挂牌基础档（非本账户实际档位），未计入 KCS 抵扣"
ACTUAL_FEE_NOTE = "KuCoin trade-fees 实际档位，未计入 KCS 抵扣"


def _dec(value: Any, default: str = "0") -> Decimal:
    """交易所返回值 → Decimal，中途绝不经过 float。

    KuCoin 的数值**大部分是裸 JSON 数字**且带科学计数（6.0E-4 / 1.5E9）。`_parse` 用
    parse_float=Decimal 在解析层就接住了；真走到 str 分支说明有人绕过了 _parse，
    那时 Decimal(str(v)) 仍比 Decimal(float) 安全（后者把 6.0E-4 变成 0.00059999...）。
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
    """区分"字段缺失"和"字段就是 0"。费率这条路上这是硬需求：makerFeeRate 真的可能
    是 0，而缺字段时补 0 会让四笔 taker 成本凭空消失、把负期望的机会整片翻成正期望，
    还不报任何错。"""
    return None if value is None or value == "" else _dec(value)


def _dec_str(value: Decimal) -> str:
    """定点字符串。绝不能让 Decimal('1E-5') 以科学计数法进请求体——发成 "1E-5" 会被
    当非法参数拒掉，而错误信息只说"参数错误"。"""
    return format(value, "f")


def _to_str(value: Any) -> str:
    if isinstance(value, Decimal):
        return _dec_str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _dumps(obj: Any) -> str:
    """全仓唯一的 JSON 序列化入口：签名和发送必须用同一个函数作用在同一个 dict 上。
    文档原文 "Do not include extra spaces in JSON strings" = separators=(",",":")，
    并强调"待加密的 body 必须与 Request Body 内容一致"。换分隔符或键序就是另一个签名。
    """
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _query_strings(params: Mapping[str, Any]) -> tuple[str, str]:
    """返回 (签名用的**未编码原文**, URL 用的编码串)。

    文档反例逐字：线上发 `...&passphrase=abc%21%40%2311`，进签名的却要写
    `...&passphrase=abc!@#11`。我们的参数没有需要转义的字符，两者恰好相同；分开生成
    是零成本保险——哪天参数带了 `#`，不分开就会既签错、又把 query 截成 fragment。
    **不排序**：KuCoin 只要求"签名串的 query 与 URL 一致"，不是 Bitget 那种字典序。
    """
    items = [(k, _to_str(v)) for k, v in params.items() if v is not None]
    raw = "&".join(f"{k}={v}" for k, v in items)
    encoded = "&".join(f"{k}={quote(v, safe='')}" for k, v in items)
    return raw, encoded


def _ts_to_ms(raw: Any) -> int:
    """时间戳归一到毫秒，按量级自适应。

    同一份响应里就并存两种单位：订单的 orderTime 纳秒、createdAt 毫秒；行情端点
    （ticker/level2/WS）的 ts 全是纳秒，/api/v1/timestamp 是毫秒。字段名都不带单位
    后缀，挑错字段差 6 个数量级——BookTop.is_fresh 会判成永远陈旧、整条腿停摆。
    """
    if raw in (None, ""):
        return 0
    try:
        value = int(_dec(raw))
    except (InvalidOperation, ValueError):
        return 0
    # 毫秒约 1.7e12、纳秒约 1.7e18，1e15 这条线两边都离得很远
    return value // 1_000_000 if value > 10 ** 15 else value


def _round_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    if step <= 0:
        return value
    return ((value / step).to_integral_value(rounding=rounding) * step).quantize(step)


def _chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


class _ContractMeta:
    """合约里换算/下单/评分真正要用到的字段。

    单独存是因为 Instrument 放不下张数上限、资金费上下限、下次结算时刻、挂牌费率，
    而它们全部来自 `contracts/active` 的同一次响应——这是本家最划算的端点。
    """

    __slots__ = ("symbol", "base", "quote", "multiplier", "lot_size_lots", "tick",
                 "max_order_qty", "market_max_order_qty", "max_leverage",
                 "funding_interval_s", "funding_rate", "next_settle_ms",
                 "cap", "floor", "maker_fee", "taker_fee", "mark_price")

    def __init__(self, raw: Mapping[str, Any]) -> None:
        self.symbol: str = str(raw.get("symbol") or "")
        # baseCurrency 返回 "XBT" 而不是 "BTC"（BitMEX 血统），原样存
        self.base: str = str(raw.get("baseCurrency") or "")
        self.quote: str = str(raw.get("quoteCurrency") or raw.get("settleCurrency") or "")
        self.multiplier: Decimal = _dec(raw.get("multiplier"), "0")   # 1 张 = 多少 base 币
        self.lot_size_lots: Decimal = _dec(raw.get("lotSize"), "1")   # **张数**的最小递增
        self.tick: Decimal = _dec(raw.get("tickSize"), "0")
        self.max_order_qty: Decimal = _dec(raw.get("maxOrderQty"), "0")
        self.market_max_order_qty: Decimal = _dec(raw.get("marketMaxOrderQty"), "0")
        self.max_leverage: Decimal = _dec(raw.get("maxLeverage"), "1")
        # 周期取 currentFundingRateGranularity（**当前生效值**）而不是
        # fundingRateGranularity（下一周期才生效的配置值）：文档举的例子就是 11:10 把
        # 配置从 8h 改成 2h、下次结算立刻从 16:00 提前到 12:00。拿错字段会把 4h/1h
        # 当 8h，年化低估 2 倍/8 倍。
        interval_ms = int(_dec(raw.get("currentFundingRateGranularity")
                               or raw.get("fundingRateGranularity"), "0"))
        self.funding_interval_s: int = (interval_ms // 1000 if interval_ms > 0
                                        else DEFAULT_FUNDING_INTERVAL_S)
        self.funding_rate: Decimal | None = _dec_opt(raw.get("fundingFeeRate"))
        # nextFundingRateDateTime 才是绝对毫秒时间戳；**nextFundingRateTime 是"还剩多少
        # 毫秒"的倒计时**（样例 3929820 ≈ 65 分钟），字段描述 "Next funding rate time
        # (milliseconds)" 是误导。倒计时存成负数：它得配一个"现在"才有意义，
        # 换算交给 _next_settle_ms()（那里才有校准过的时钟）。
        self.next_settle_ms: int = int(_dec(raw.get("nextFundingRateDateTime"), "0"))
        if self.next_settle_ms <= 0:
            countdown = int(_dec(raw.get("nextFundingRateTime"), "0"))
            self.next_settle_ms = -countdown if countdown > 0 else 0
        self.cap: Decimal | None = _dec_opt(raw.get("fundingRateCap"))
        self.floor: Decimal | None = _dec_opt(raw.get("fundingRateFloor"))
        # 挂牌基础档，**不是本账户档位**；只在私有 trade-fees 拿不到时兜底
        self.maker_fee: Decimal | None = _dec_opt(raw.get("makerFeeRate"))
        self.taker_fee: Decimal | None = _dec_opt(raw.get("takerFeeRate"))
        self.mark_price: Decimal = _dec(raw.get("markPrice"), "0")

    @property
    def coin_step(self) -> Decimal:
        """币口径的数量粒度 = lotSize（张）× multiplier（币/张）。

        直接把 lotSize 报出去，上层会拿"张粒度"给"币数量"取整：XBTUSDTM 的 lotSize=1
        会让 0.5 BTC 的缺口被 floor 成 0，这条腿永远补不平。
        """
        return self.lot_size_lots * self.multiplier


class KucoinAdapter(ExchangeAdapter):
    """KuCoin Futures，linear 永续（`kucoin:perp`）。

    **不注入任何返佣码/邀请码/broker code**：`broker_code` 照收、收下什么都不做
    （用户明确要求）——不加 header、不加 body 字段、不给 clientOid 加前缀
    （加前缀还会破坏上层按原 ID 对账的假设）。构造参数**全部有默认值**
    （session._build_adapter 只传 cred + broker_code），且**绝不校验凭据非空**——
    public_feed 用 `Credential("", "")` 构造它跑无凭据公开行情，这里抛错的后果是
    KuCoin 在整个机会榜上消失、只在 init_errors 里留一行。
    """

    venue = Venue.KUCOIN
    rest_base = REST_BASE

    def __init__(self, credential: Credential, *, broker_code: str = "",
                 client: httpx.AsyncClient | None = None,
                 market_kind: MarketKind = MarketKind.PERP,
                 rest_base: str = REST_BASE,
                 margin_mode: str = "CROSS",
                 key_version: str = "3",
                 hedge_mode: bool = False,
                 crypto_only: bool = True) -> None:
        if market_kind is not MarketKind.PERP:
            # 现货/杠杆在另一个域名（api.kucoin.com）上、另一套端点与精度字段，调研文档
            # 一个字都没覆盖：现货精度字段、现货下单体与 TIF 枚举、杠杆借币利率端点与
            # 计息口径（CarryKind.INTEREST 的符号归一化没依据）、杠杆"持仓"到 Position
            # 的映射（现货没有 Position，要用借贷余额差分）。凭记忆补这些等于赌。
            raise NotImplementedError(
                f"kucoin 适配器目前只支持 {MarketKind.PERP.value}；"
                f"{market_kind.value} 缺现货/杠杆调研（域名、精度字段、下单体、借币利率）")
        if hedge_mode:
            # 对冲模式下 positionSide 必填，且 buy/sell + reduceOnly → LONG/SHORT 的映射
            # 是"把仓位做成两倍"的头号原因。没实测就不写这段分支——静默发错字段比
            # 不支持危险得多。
            raise NotImplementedError(
                "kucoin 对冲(hedge)持仓模式未实现：positionSide 与 reduce-only 的方向"
                "映射未经实测，先用 switch_position_mode() 切回单向")
        # 用户从网页复制的码常带尾随空白，六家统一在构造期剥掉；剥完之后本适配器
        # 对它**不做任何事**。
        super().__init__(credential, broker_code=broker_code.strip(), client=client)
        self.rest_base = rest_base.rstrip("/")
        self.market_kind = market_kind
        # 六家都对外暴露 .market，桥接层按这个名字取，缺了就 AttributeError
        self.market = f"{self.venue.value}:{market_kind.value}"
        self.margin_mode = margin_mode.upper()
        # v1 老 key 是明文 passphrase + VERSION "1"。本仓库只支持 v2/v3，默认 "3"
        # （官方最新示例即 3）。v1 是否仍被接受 UNVERIFIED，不写兼容分支——
        # 明文 passphrase 在 header 里裸奔本身就不该继续支持。
        self.key_version = str(key_version)
        self.crypto_only = crypto_only

        self._meta: dict[str, _ContractMeta] = {}
        self._meta_lock = asyncio.Lock()
        # 逐仓下杠杆是**下单参数**、没有独立端点：set_leverage 存这里，place_order 带上
        self._isolated_leverage: dict[str, Decimal] = {}
        self._ws_id = itertools.count(1)
        # 每个响应都带 gw-ratelimit-*，读 remaining 主动退避比等 429 靠谱：等到 429 时
        # 已被要求"停止访问直到配额重置"，那是整整一个 30 秒窗口的行情空窗。
        self.rate_limit_remain: int | None = None
        self.rate_limit_reset_ms: int | None = None

    # ---- 签名 / 请求 -----------------------------------------------------

    @staticmethod
    def _prehash(ts: str, method: str, endpoint: str, body_str: str) -> str:
        """prehash = timestamp + METHOD + endpoint + body，**四段直接拼接、无分隔符**。

        官方示例逐字：
          1768982284539GET/api/v1/orders/byClientOid?clientOid=arb-1a2b
          1768982284539POST/api/v1/orders{"clientOid":"arb-1a2b","symbol":"XBTUSDTM",...}
        endpoint = 路径**加查询串（含 '?'）**且是未 URL-encode 的原文；GET/DELETE 的
        参数全在 URL 里、body 段是空串；METHOD 全大写。
        """
        return f"{ts}{method.upper()}{endpoint}{body_str}"

    def _passphrase_header(self) -> str:
        """v2/v3 的 passphrase 是 base64(HMAC_SHA256(secret, passphrase))，**不是明文**。
        发明文会稳定收 400004，而 400004 的文案是"passphrase 错"——看起来像用户填错了
        密码，实际是编码没做，最容易查错方向的一处。"""
        return self._hmac_sha256_b64(self._cred.passphrase)

    def _prepare(self, method: str, path: str, params: dict, body: dict,
                 signed: bool) -> tuple[dict[str, str], str, dict, dict]:
        method = method.upper()
        raw_query, url_query = _query_strings(params)
        endpoint = path + (f"?{raw_query}" if raw_query else "")
        url = self.rest_base + path + (f"?{url_query}" if url_query else "")
        # 判据必须与 _request 里发 content 的判据**逐字相同**，否则 GET 带 body 时签名
        # 会分叉（签了 body、发的却没有）。
        body_str = _dumps(body) if body and method != "GET" else ""

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if signed:
            ts = str(self.timestamp_ms())       # 已校准的时钟；绝不用 time.time()
            headers.update({
                "KC-API-KEY": self._cred.api_key,
                # base64 不是 hex —— 最容易被别家肌肉记忆带偏的一处
                "KC-API-SIGN": self._hmac_sha256_b64(
                    self._prehash(ts, method, endpoint, body_str)),
                # 签进 prehash 的值必须和这个 header 是**同一个字符串**，
                # 签一个发另一个是最常见的 400005
                "KC-API-TIMESTAMP": ts,
                "KC-API-PASSPHRASE": self._passphrase_header(),
                "KC-API-KEY-VERSION": self.key_version,
            })
        # send_params 交空 dict：query 已亲手拼进 URL，再交给 httpx 会被它重新编码、
        # 和签名串对不上。返佣码在这里**一个字节都不加**。
        return headers, url, {}, dict(body)

    async def _request(self, method: str, path: str, *, quota: str = "market",
                       signed: bool = False, params: Mapping[str, Any] | None = None,
                       body: Mapping[str, Any] | None = None,
                       extra_headers: Mapping[str, str] | None = None) -> Any:
        """覆写基类：KuCoin 必须签"发出去的那串字节"。

        基类走 `json=send_body` 把序列化交给 httpx，结果和 _prepare 里签名用的字符串
        没有任何保证一致（separators / ensure_ascii / 键序任一不同就废）。这里改成
        `content=`，两处都调用同一个 `_dumps` 作用在同一个 dict 上。
        `_syncing_clock` 那道闸不能省：fetch_server_time_ms 自己也走本方法，而本方法
        开头见时钟陈旧又会去 sync_clock —— 不设闸就是无限递归/自锁。
        """
        if self._clock.is_stale and not self._syncing_clock:
            try:
                await self.sync_clock()
            except Exception:
                pass        # 校时失败不阻塞业务，但会体现在 clock_drift_ms 上

        async with self._quota.get(quota, self._quota["market"]):
            headers, url, _, send_body = self._prepare(
                method, path, dict(params or {}), dict(body or {}), signed)
            if extra_headers:
                headers = {**headers, **extra_headers}
            content = (_dumps(send_body).encode()
                       if send_body and method.upper() != "GET" else None)
            try:
                resp = await self._client.request(method, url, content=content,
                                                  headers=headers)
            except httpx.HTTPError as exc:
                # 超时/连接中断/半途断流全在这里。code 固定 NETWORK，place_order 靠它
                # 翻译成 OrderUnknown（重发 = 双倍仓位）。
                raise ExchangeError(self.venue.value, "NETWORK", str(exc),
                                    retriable=True) from exc
            return self._parse(resp)

    # ---- 响应解析 -------------------------------------------------------

    def _parse(self, resp: httpx.Response) -> Any:
        self._record_quota(resp)
        text = resp.text or ""
        try:
            # parse_float=Decimal：数值大部分是裸 JSON 数字且带科学计数，等落到
            # Decimal(repr(float)) 兜底时精度已经掉了，这道闸必须在解析层。
            payload = json.loads(text, parse_float=Decimal) if text else None
        except ValueError:
            payload = None

        if not isinstance(payload, dict):
            # Cloudflare 的 1015 限频页、WAF/代理注入的 HTML 都落到这里
            if resp.status_code == 429 or "1015" in text[:200]:
                raise RateLimited(self.venue.value,
                                  "1015" if "1015" in text[:200] else "HTTP_429",
                                  text[:200], retriable=True)
            if resp.status_code < 400:
                # 2xx 但响应体不是合法 JSON：这单有没有进撮合完全不知道。返回 None 会让
                # 下游 _parse_order(None) 抛 AttributeError；退化成 retriable=False 更糟
                # ——那等于给 place_order 发了一张"可安全重发"的许可证。
                raise ExchangeError(self.venue.value, "BAD_JSON",
                                    f"响应不是合法 JSON: {text[:200]}", retriable=True)
            raise ExchangeError(self.venue.value, f"HTTP_{resp.status_code}",
                                text[:200], retriable=resp.status_code >= 500)

        code = str(payload.get("code", ""))
        if code == SUCCESS_CODE:
            return payload.get("data")      # 去壳后的 data，业务方法不必重复剥壳
        raise self._translate(code, str(payload.get("msg", "")), resp.status_code)

    def _record_quota(self, resp: httpx.Response) -> None:
        """gw-ratelimit-* 三件套。KuCoin 是**固定 30 秒窗口**（从该用户第一个请求到达
        起算）不是滑动窗口，reset 是毫秒倒计时——退避必须等到真正的重置点，早了照样撞。
        """
        for header, attr in (("gw-ratelimit-remaining", "rate_limit_remain"),
                             ("gw-ratelimit-reset", "rate_limit_reset_ms")):
            value = resp.headers.get(header)
            if value is None:
                continue
            try:
                setattr(self, attr, int(value))
            except ValueError:
                pass

    def _translate(self, code: str, msg: str, status: int) -> ExchangeError:
        venue = self.venue.value
        low = msg.lower()

        if code in _CLOCK_CODES:
            # 就地把时钟标脏，逼下次请求先重新校时——不标脏就会一直撞同一堵墙，
            # 因为 _request 只在 is_stale 时才去校时。
            self._clock.synced_at = 0.0
            return ExchangeError(venue, code, msg, retriable=True)
        if code == "200002":
            # 一码两义，必须看 msg 分：限频 vs Level3 查询范围超限（参数错）
            if any(h in low or h in msg for h in _RATE_LIMIT_HINTS):
                return RateLimited(venue, code, msg, retriable=True)
            return ExchangeError(venue, code, msg, retriable=False)
        if status == 429 or code in _RATE_LIMIT_CODES:
            # 限频 = **确定没进撮合**，所以它是下单路径上唯一可以直接抛、允许上层安全
            # 重试的可重试错误
            return RateLimited(venue, code or f"HTTP_{status}", msg, retriable=True)
        if code in _ORDER_TERMINAL_CODES or "not cancelable" in low:
            # 已终结 ≠ 不存在。cancel_order 自己吞掉；query_order 让它上抛，
            # 绝不能变成 OrderRejected 谎报"零成交"。
            return ExchangeError(venue, code, msg, retriable=False)
        if any(h in low or h in msg for h in _ORDER_GONE_HINTS):
            # 订单不存在 —— resolve_unknown_order 靠 OrderRejected 判定"这单从未被接受"
            return OrderRejected(venue, code or "ORDER_NOT_FOUND", msg, retriable=False)
        if code in _DUPLICATE_CLIENT_OID_CODES:
            # 幂等保护生效的证据，**绝不能是 OrderRejected**；place_order 单独把它
            # 倒向 OrderUnknown
            return ExchangeError(venue, code, msg, retriable=False)
        if code in _REJECT_CODES:
            return OrderRejected(venue, code, msg, retriable=False)
        if code in _RETRIABLE_CODES or status >= 500:
            return ExchangeError(venue, code or f"HTTP_{status}", msg, retriable=True)
        if code in _PRE_MATCH_CODES or code in _CONFIG_CODES:
            return ExchangeError(venue, code, msg, retriable=False)
        # 兜底：未命中的一律不可重试。宁可不重试，也不要把一个可能已成交的请求当成
        # 可安全重发——下单路径会把"认不出的码"单独倒向 OrderUnknown。
        return ExchangeError(venue, code or f"HTTP_{status}", msg, retriable=False)

    def _risk_path_error(self, exc: ExchangeError) -> ExchangeError:
        """风控路径（撤单/查单/查仓）上的错误重分类。

        _request 把**所有** httpx 异常收敛成 ExchangeError('NETWORK')，而
        base.resolve_unknown_order 只捕获 (OrderRejected, httpx.HTTPError, RateLimited)
        —— 不转一手，网络抖动或 5xx 会让消解流程在最危险的时刻带着未捕获异常崩掉。
        可重试的重挂到 RateLimited（code 保留真实原因）；**不可重试的原样上抛，绝不
        降级成 OrderRejected** —— 降级等于谎报"成交量为 0"，上层会照着 0 再补一次仓。
        """
        if isinstance(exc, (OrderRejected, RateLimited)) or not exc.retriable:
            return exc
        return RateLimited(self.venue.value, exc.code, exc.message, retriable=True)

    # ---- 元数据 ---------------------------------------------------------

    async def fetch_server_time_ms(self) -> int:
        """`GET /api/v1/timestamp`，公开不签名，`data` **直接是数字**（毫秒），
        别写 data["timestamp"]。校时不是可选项：400002 的窗口只有 5 秒且没有
        recvWindow 可放宽，未校时的时钟去签名会立刻过期。"""
        return int(_dec(await self._request("GET", "/api/v1/timestamp",
                                            quota="market")))

    async def _load_contracts(self) -> list[Mapping[str, Any]]:
        """拉一次 contracts/active（weight 3，一次全市场）刷新 _meta，返回筛过的永续行。
        fetch_instruments / fetch_carry_rates / fetch_funding_limits 都从这里取。

        **不做长缓存**：文档明说资金费周期可以在一个周期中途被改短，KuCoin 2026 年也
        多次批量调整过结算间隔。启动时读一次然后一直用，会把 4h 当 8h 年化错一倍。
        """
        rows = await self._request("GET", "/api/v1/contracts/active", quota="market") or []
        kept: list[Mapping[str, Any]] = []
        for raw in rows:
            # 四条件过滤链，缺一不可。只按符号后缀 M 猜会把交割合约算进 carry ——
            # 那种合约到期就没有资金费，年化算出来是纯幻觉。
            if raw.get("type") != "FFWCSX":             # FFICSX 是有交割日的期货
                continue
            if raw.get("expireDate") is not None:       # 双保险：带交割日的一律排除
                continue
            if self.crypto_only and str(raw.get("marketType") or "CRYPTO") != "CRYPTO":
                continue        # NASDAQ = 股票永续，资金费逻辑与交易时段都不同
            if raw.get("status") != "Open":             # Init/Paused/Settled/CancelOnly 不可下单
                continue
            if str(raw.get("marketStage") or "NORMAL") == "PRE_MARKET":
                continue        # 盘前合约配不出对手腿，留着只会产生幽灵机会行
            if raw.get("isInverse"):
                # 反向合约 1 张 = 1 USD，"币数 = 张数 × multiplier" 不成立，混进来会让
                # XBTUSDM 的仓位错一个数量级，要单独的 PERP_COIN 适配器
                continue
            meta = _ContractMeta(raw)
            if meta.multiplier <= 0 or not meta.symbol:
                # multiplier 是所有张↔币换算的分母。宁可跳过这一行也别整体抛错：
                # public_feed 把 fetch_instruments 整个 try/except 吞掉，
                # 抛错 = 这家规格整片消失且零提示。
                continue
            self._meta[meta.symbol] = meta
            kept.append(raw)
        return kept

    async def fetch_instruments(self) -> Sequence[Instrument]:
        rows = await self._load_contracts()
        out: list[Instrument] = []
        for raw in rows:
            meta = self._meta[str(raw["symbol"])]
            out.append(Instrument(
                market=self.market,
                symbol=meta.symbol,     # 原生符号，不归一化（那是 public_feed 的事）
                # base 是 "XBT" 不是 "BTC"：**原样带出**，XBT→BTC 的跨所映射归
                # public_feed.normalize_base；在这里偷偷改会让 symbol 和 base 对不上号
                base=meta.base,
                quote=meta.quote,
                tick_size=meta.tick,
                lot_size=meta.coin_step,        # 币口径，见 _ContractMeta.coin_step
                # KuCoin 不给 quote 计价的最小名义额（lotSize 是张数），只能用
                # "一张的币数 × 标记价"估；它是快照、价格一动就偏。真正的硬约束
                # （整数张、≥lotSize）在 place_order 本地拦截，这里只是让评分层
                # 看得见颗粒度。
                min_notional=meta.coin_step * meta.mark_price,
                contract_size=meta.multiplier,
                max_leverage=meta.max_leverage,
                funding_interval_s=meta.funding_interval_s,
            ))
        return tuple(out)

    async def _ensure_meta(self, symbols: Sequence[str]) -> None:
        """按需补齐元数据：缺什么先整表拉一次，剩下的单个补。

        单个补那步是给**下架/暂停**合约留的——它们不在 contracts/active 里但持仓还在，
        而持仓是真值，不能因为查不到规格就让 fetch_positions 整个报错。
        """
        if not [s for s in symbols if s and s not in self._meta]:
            return
        async with self._meta_lock:
            missing = [s for s in symbols if s and s not in self._meta]
            if not missing:
                return
            await self._load_contracts()
            for symbol in [s for s in missing if s not in self._meta]:
                # UNVERIFIED：单合约路径按 Get Symbol 页推定为 /api/v1/contracts/{symbol}
                # （调研文档引用了该页但没给完整路径）。失败就跳过，不拖垮整批持仓。
                try:
                    raw = await self._request("GET", f"/api/v1/contracts/{symbol}",
                                              quota="market")
                except ExchangeError:
                    continue
                if not raw or not raw.get("symbol"):
                    continue
                # 兜底路径也必须过 _load_contracts 那两条**单位模型**判据。
                # 这里曾经裸写 self._meta[...] = _ContractMeta(raw)，于是反向合约
                # 从后门进了 _meta：XBTUSDM 一张是 1 USD 不是 multiplier 个币，
                # "qty = 张 × multiplier" 对它整个不成立——5000 张多头会被
                # fetch_positions 报成 qty=-5000（方向翻转 + 量级错几万倍），
                # 而持仓是真值，executor 会照着这个数去"补齐"对冲。
                # status/marketStage 不在此列：这条路本来就是给下架/暂停合约留的。
                if raw.get("isInverse"):
                    continue
                meta = _ContractMeta(raw)
                if meta.multiplier <= 0 or not meta.symbol:
                    continue    # multiplier=0 会让真实持仓被折成 0 币，风控失明
                self._meta[meta.symbol] = meta

    async def _meta_of(self, symbol: str) -> _ContractMeta:
        await self._ensure_meta([symbol])
        meta = self._meta.get(symbol)
        if meta is None:
            raise ExchangeError(self.venue.value, "UNKNOWN_INSTRUMENT",
                                f"{symbol} 不在 KuCoin 可交易永续列表里，"
                                "拿不到 multiplier 就无法把张折成币")
        return meta

    async def fetch_fee_schedule(self, symbols: Sequence[str]) -> Sequence[FeeSchedule]:
        """用户实际费率档位，只有签名的 `/api/v1/trade-fees` 拿得到。

        - **一次只能查一个 symbol**（无批量参数），N 个符号 = N×3 weight，必须缓存、
          绝不能进轮询循环。每条必须填 symbol：这家是**按合约**给的、不是账户级一档
          （跟 OKX/Bitget/币安合约都不同），调用方按 symbol 匹配、绝不按下标。
        - 符号约定 **正数 = 成本**，与本仓库一致、**不取反**（只有 OKX 要翻）；
          高 VIP 的 maker 可能为负，那是返佣，保持负号正好当收入。
        - **查不到绝不补 maker=0/taker=0 占位**：零手续费会让四笔 taker 成本凭空消失、
          负期望的机会整片翻成正期望且不报错。兜底只用 contracts/active 的挂牌基础档
          （真实、公开、零额外 weight）并在 note 标明；KCS 抵扣的幅度 API 读不到
          （UNVERIFIED），读不到就**绝不擅自打折**——低估成本比高估危险得多。
        - `symbols` 为空时（session.refresh_fees 就这么调）仍要返回：这家没有账户级
          一档可探，逐个查全市场是 N×3 weight 的自杀，所以退回全市场挂牌档。
        """
        wanted = [s for s in symbols if s]
        if not wanted:
            # 这条分支曾经把 contracts/active 的**公开挂牌档**按 symbol 逐条返回。
            # 那是全仓最隐蔽的一类谎：session.refresh_fees 只会传 ()，拿到的挂牌价
            # 进了 by_symbol 缓存，public_feed 只认"返回了就是真档位"，于是 KuCoin
            # 的每一行都被标成 fee_basis="real"、界面上永远不出现"按默认费率估算"——
            # VIP0 挂牌价冒充用户真实档位，正是这个工具立项要修的病。
            # 现在改成：对一个代表性合约打一次签名 trade-fees，把结果作为**账户级**
            # 档位（symbol=""）返回——KuCoin 的费率档按账户定级，单点探测足够。
            # 探测失败（没权限/没签名）就返回空，让上层老老实实退回挂牌价并标注。
            if not self._meta:
                await self._load_contracts()
            probe = "XBTUSDTM" if "XBTUSDTM" in self._meta else next(iter(self._meta), "")
            if not probe:
                return ()
            try:
                data = await self._request("GET", "/api/v1/trade-fees", quota="market",
                                           signed=True, params={"symbol": probe})
            except ExchangeError:
                return ()
            maker = _dec_opt((data or {}).get("makerFeeRate"))
            taker = _dec_opt((data or {}).get("takerFeeRate"))
            if maker is None or taker is None:
                return ()
            return (FeeSchedule(market=self.market, symbol="", maker=maker, taker=taker,
                                note=f"{ACTUAL_FEE_NOTE}（按 {probe} 探测的账户级档位）"),)

        out: list[FeeSchedule] = []
        for symbol in wanted:
            maker = taker = None
            try:
                data = await self._request("GET", "/api/v1/trade-fees", quota="market",
                                           signed=True, params={"symbol": symbol})
            except ExchangeError:
                data = None     # 单符号失败（无权限/限频/下架）不能拖垮整批
            if data:
                maker = _dec_opt(data.get("makerFeeRate"))
                taker = _dec_opt(data.get("takerFeeRate"))
            if maker is None or taker is None:
                # 查不到就**不返回这一条**——契约（base.py）明写宁可少返回。
                # 这里曾经退回 contracts/active 的挂牌档：调用方按"返回了 = 真档位"
                # 处理，挂牌价冒充真档位的病就是这样一条条漏进来的
                continue
            out.append(FeeSchedule(market=self.market, symbol=symbol,
                                   maker=maker, taker=taker, note=ACTUAL_FEE_NOTE))
        return tuple(out)

    async def fetch_funding_limits(self, symbol: str) -> tuple[Decimal, Decimal] | None:
        """返回 **(cap, floor)**，cap >= floor。

        cap/floor 在 contracts/active 和 funding-rate/{symbol}/current 上**都有**，
        KuCoin 命名跟基类要的顺序天然一致（XBTUSDTM ±0.003），**照抄，不要排序**。
        各合约不同，不能拿 BTC 的值套全市场。查不到返回 **None**（scoring 跳过 clamp）；
        **绝不返回 (0, 0)** —— 那等于把费率夹死成 0。这是**每周期**的夹子，
        4h 合约的 cap 年化后是 8h 同数值的两倍，归一化是上层的事。
        """
        meta = self._meta.get(symbol)
        if meta is not None and meta.cap is not None and meta.floor is not None:
            return meta.cap, meta.floor
        data = await self._request("GET", f"/api/v1/funding-rate/{symbol}/current",
                                   quota="market")
        if not data:
            return None
        # UNVERIFIED：这个响应的 data.symbol 可能是**指数符号**（.XBTUSDTMFPI8H）而不是
        # 合约符号（字段表与 example 矛盾），所以一律以请求时的 symbol 为准回写。
        cap = _dec_opt(data.get("fundingRateCap"))
        floor = _dec_opt(data.get("fundingRateFloor"))
        return None if cap is None or floor is None else (cap, floor)

    # ---- 行情 -----------------------------------------------------------

    async def fetch_book_top(self, symbol: str) -> BookTop:
        """一档盘口 `GET /api/v1/ticker`（公开，weight 2，quota="market"）。

        两个单位坑：ts 是**纳秒**、bestBidSize/bestAskSize 是**张**。前者会让 is_fresh
        判成永远陈旧、收敛循环停摆，后者在 BTC 上是 1000 倍的档位量错误。

        扫多符号别循环调它：`GET /api/v1/allTickers`（weight 5，同结构数组）扫 100 个
        符号是 5 对 200，便宜 40 倍 —— 而 Public 池按 IP 计、全 VIP 统一 2000/30s，
        行情轮询才是这家唯一的真瓶颈。
        """
        meta = await self._meta_of(symbol)
        data = await self._request("GET", "/api/v1/ticker", quota="market",
                                   params={"symbol": symbol})
        data = data or {}
        bid, ask = data.get("bestBidPrice"), data.get("bestAskPrice")
        if bid in (None, "") or ask in (None, "") or _dec(bid) <= 0 or _dec(ask) <= 0:
            raise ExchangeError(self.venue.value, "EMPTY_BOOK",
                                f"{symbol} 盘口单边为空", retriable=True)
        return BookTop(
            bid_price=_dec(bid),
            bid_qty=_dec(data.get("bestBidSize")) * meta.multiplier,     # 张 → 币
            ask_price=_dec(ask),
            ask_qty=_dec(data.get("bestAskSize")) * meta.multiplier,
            # 取不到用本地时钟兜底，**别填 0**：is_fresh 会把 0 判成永远陈旧
            ts_ms=_ts_to_ms(data.get("ts")) or self.timestamp_ms(),
        )

    @staticmethod
    def _band_side(levels: Sequence[Sequence[Any]], multiplier: Decimal,
                   lo: Decimal, hi: Decimal, descending: bool) -> tuple[Decimal, int]:
        """一侧盘口落在 [lo, hi] 内的累计名义额和档数（边界**含**）。

        簿是排好序的（bids 降序、asks 升序），越过**远端**边界可以直接停——后面只会更远；
        越过**近端**边界（只有交叉簿/陈旧快照才会）只跳过这一档、不能停，再往后可能
        还有带内的档。名义额 = 价 × 张数 × multiplier：漏乘就把 BTC 深度低估 1000 倍，
        容量闸门会放行一个盘口根本吃不下的规模。价或量 <=0 的脏档既不计名义额也不计
        levels_used——否则"档位够不够覆盖带宽"这个判断会被噪声灌水。
        """
        notional, used = ZERO, 0
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
            notional += price * size * multiplier
            used += 1
        return notional, used

    async def fetch_book_depth(self, symbol: str, band_bp: float = 10.0) -> BookDepth:
        """band_bp 基点带宽内的可见深度（名义额 quote 计价，数量 base 币计价）。

        只用一档当深度代理会把容量**系统性低估一两个数量级**——山寨币一档常常只有几百
        刀、10bp 内却有几万，容量估错会让真实可做的机会被判成"做不了"。这个方法存在的
        全部理由就是修这个；档位不够时**不外推**，levels_used 如实报两侧档数之和，
        接近 2×DEPTH_LEVELS_PER_SIDE 说明簿被端点上限截断、带宽可能没盖满。

        端点 depth100（公开，weight 5）。它的 price 是 **number** 而 ticker 的价格是
        **string**（同一家两个行情端点类型不一致），size 是**张**，ts 是**纳秒**。
        走 quota="market"，与 fetch_book_top **共用同一个池**（本来就是同一个 Public
        桶），绝不能挪去 risk/trade —— 那是拿平仓能力换行情。
        """
        if band_bp <= 0:
            # 带宽非正 ⇒ 深度恒为 0 ⇒ scoring 把能做的机会判成不可执行。
            # 静默返回 0 比抛异常危险得多，入口直接炸，且不发请求。
            raise ValueError(f"band_bp 必须为正基点数，收到 {band_bp!r}")

        meta = await self._meta_of(symbol)
        data = await self._request("GET", DEPTH_PATH, quota="market",
                                   params={"symbol": symbol})
        data = data or {}
        bids, asks = data.get("bids") or [], data.get("asks") or []
        if not bids or not asks:
            # 缺 bids/asks **必须抛错，不能退化成深度 0**：scoring 里 depth_cap<=0 会
            # 跳过容量判断，等于把"没数据"读成"深度无限"。真实原因通常是限频或合约
            # 暂停——那是要报出来的状态，不是一个合法的深度值。
            raise ExchangeError(self.venue.value, "EMPTY_BOOK",
                                f"{symbol} 盘口单边为空", retriable=True)

        best_bid, best_ask = _dec(bids[0][0]), _dec(asks[0][0])
        mid = (best_bid + best_ask) / 2
        # band_bp 对外是 float，**只在这一处**经 str 转 Decimal，之后全程 Decimal ——
        # 直接 Decimal(10.0) 会带一串二进制尾巴进价格比较，恰好压在边界上的那一档
        # 算不算就成了掷骰子。
        band = Decimal(str(band_bp)) / Decimal("10000")
        lo, hi = mid * (1 - band), mid * (1 + band)
        bid_notional, bid_levels = self._band_side(bids, meta.multiplier, lo, mid, True)
        ask_notional, ask_levels = self._band_side(asks, meta.multiplier, mid, hi, False)
        return BookDepth(
            symbol=symbol,
            bid_notional=bid_notional,
            ask_notional=ask_notional,
            bid_price=best_bid,
            bid_qty=_dec(bids[0][1]) * meta.multiplier,     # 张 → 币，与 book_top 同口径
            ask_price=best_ask,
            ask_qty=_dec(asks[0][1]) * meta.multiplier,
            levels_used=bid_levels + ask_levels,
            band_bp=band_bp,
            ts_ms=_ts_to_ms(data.get("ts")) or self.timestamp_ms(),
        )

    def _next_settle_ms(self, meta: _ContractMeta) -> int:
        """下一次结算的绝对毫秒时刻。倒计时在 _ContractMeta 里存成负数（它得配一个
        "现在"才有意义）。绝对时间戳来自快照，放久了会变成过去时刻，按周期推进到下一个
        未来时点，否则上层的"离结算还有多久"永远是负的。"""
        now = self.timestamp_ms()
        raw = meta.next_settle_ms
        if raw < 0:
            return now - raw            # raw 是 -倒计时
        if raw <= 0:
            return 0
        interval_ms = meta.funding_interval_s * 1000
        if raw <= now and interval_ms > 0:
            raw += ((now - raw) // interval_ms + 1) * interval_ms
        return raw

    async def fetch_carry_rates(self, symbols: Sequence[str] | None = None
                                ) -> Sequence[CarryRate]:
        """当前资金费。symbols=None 时一把返回全市场（一次 weight-3 的请求）。

        **符号用交易所原始约定：正 = 多头付给空头，原样存不取反。** base.py 那句
        "取负成多头视角"与 models.CarryRate / scoring 的口径相反，全仓上下游都按 models
        走；取反会让 `short_rate - long_rate` 全体反号，错的方向恰好是"把亏钱的对推荐成
        赚钱的"。interval_s 来自合约表本身，不像 Gate 那样要额外 join 一次。
        """
        rows = await self._load_contracts()
        wanted = set(symbols) if symbols else None
        now_ms = self.timestamp_ms()
        out: list[CarryRate] = []
        for raw in rows:
            symbol = str(raw["symbol"])
            if wanted is not None and symbol not in wanted:
                continue
            meta = self._meta[symbol]
            if meta.funding_rate is None:
                # 当期费率缺失就跳过；补 0 会让它看起来像"资金费恰好为零"的可做机会
                # （predictedFundingFeeRate 经常是 null，别拿它顶替）
                continue
            out.append(CarryRate(
                market=self.market, symbol=symbol, rate=meta.funding_rate,
                interval_s=meta.funding_interval_s,
                next_settle_ms=self._next_settle_ms(meta), ts_ms=now_ms))
        return tuple(out)

    async def fetch_funding_history(self, symbol: str, since_ms: int,
                                    limit: int = 1000
                                    ) -> Sequence[tuple[int, Decimal]]:
        """历史资金费，**升序**返回 [since_ms, 现在] 的完整区间。
        `GET /api/v1/contract/funding-rates?symbol=&from=&to=`（三参数都 required）。

        1. **返回是倒序（新→旧）**，基类要升序，最后统一 sorted()。
        2. **它按 `to` 端对齐**：to 设成"现在"，第一条就是最新一次结算，天然满足基类
           "必须覆盖到窗口末端"——不用像币安那样掰方向（币安从 startTime 往后数 N 条，
           会给一段停在两周前、看起来却完全正常的假序列）。但翻页必须顺着它往旧走。
        3. 字段名是**全小写 timepoint**；私有 funding-history 里叫驼峰 timePoint，
           抄错就是 KeyError。

        `limit` 是页大小语义（基类契约："不是返回条数的上限……返回可以多于 limit"），
        而本端点没有页大小参数，所以它在这里**什么都不做**。曾经拿它当输出上限截尾：
        KuCoin 有 1 小时周期的合约，1000 条只有 41 天，"计划持有 1 月"要的 90 天
        窗口被无声斩掉——和 Hyperliquid 那条被两个审查镜头独立揪出的 bug 同根。
        字典去重保留：重复值会抬高 autocorr()、拖偏 median 且**不报任何错**。
        """
        floor_ms = max(int(since_ms), 0)
        collected: dict[int, Decimal] = {}
        to_ms = self.timestamp_ms()
        for _ in range(FUNDING_HISTORY_MAX_PAGES):
            if to_ms <= floor_ms:
                break
            from_ms = max(floor_ms, to_ms - FUNDING_HISTORY_WINDOW_MS)
            rows = await self._request(
                "GET", "/api/v1/contract/funding-rates", quota="market",
                params={"symbol": symbol, "from": from_ms, "to": to_ms}) or []
            if not rows:
                # 空窗口不一定代表更早没数据，但这端点没有游标可续，继续盲翻会把 40 页
                # 的闸全烧在空段上
                break
            oldest: int | None = None
            for row in rows:
                ts = _ts_to_ms(row.get("timepoint"))
                if ts <= 0:
                    continue
                oldest = ts if oldest is None else min(oldest, ts)
                if ts >= floor_ms:
                    collected[ts] = _dec(row.get("fundingRate"))
            if oldest is None:
                break
            # 下一页右端 = 本页最老一条 −1ms。**那个 -1 不能省**：from/to 含边界，
            # 省掉就每页首尾重叠一条。
            nxt = oldest - 1
            if nxt >= to_ms:
                break                   # 游标不前进，再翻就是死循环
            to_ms = nxt
        # 不按 limit 截：见 docstring。gate/binance/hyperliquid 也是整段返回
        return tuple(sorted(collected.items()))

    # ---- WebSocket -------------------------------------------------------

    def stream_book_top(self, symbols: Sequence[str]) -> AsyncIterator[BookTop]:
        """`/contractMarket/tickerV2` 盘口流。写成普通 def 返回异步生成器
        （Bitget 同款）：调用方不 await 也能拿到迭代器。

        **BookTop 没有 symbol 字段**，多符号订阅时下游分不清来源——实际使用建议
        **一个符号一条流**。TODO（最小可用版，缺的写清楚）：增量深度
        /contractMarket/level2 没接、只有一档 BBO；sequence 只用于丢乱序包、断号没有
        触发重订阅补快照；超过 100 个 topic 时仍在一条连接上分批订阅、没做多连接分片。
        """
        return self._book_stream(tuple(symbols))

    async def _ws_endpoint(self) -> tuple[str, str, float]:
        """`POST /api/v1/bullet-public` 换 token（Public 池，weight **10**，无签名）。

        **心跳参数来自服务器**（pingInterval 18000 / pingTimeout 10000），别硬编码 30 秒。
        token 有有效期（秒数文档未给，UNVERIFIED），所以**每次重连都重新取**、不缓存
        复用——用过期 token 建连会被直接关掉，表现为无限重连。
        """
        data = await self._request("POST", "/api/v1/bullet-public", quota="market")
        data = data or {}
        token = str(data.get("token") or "")
        servers = data.get("instanceServers") or []
        server = servers[0] if servers else {}
        endpoint = str(server.get("endpoint") or "wss://ws-api-futures.kucoin.com/")
        ping_ms = int(_dec(server.get("pingInterval"), str(WS_PING_INTERVAL_FALLBACK_MS)))
        if not token:
            raise ExchangeError(self.venue.value, "WS_TOKEN",
                                "bullet-public 没有返回 token", retriable=True)
        if ping_ms <= 0:
            ping_ms = WS_PING_INTERVAL_FALLBACK_MS
        # 留 20% 余量：pingTimeout 只有 10 秒，踩着 pingInterval 发很容易在抖动时被判死
        return endpoint, token, ping_ms / 1000.0 * 0.8

    async def _book_stream(self, symbols: Sequence[str]) -> AsyncIterator[BookTop]:
        import websockets      # 延迟导入：只跑 REST 的场景不该被这个依赖拖着

        valid = [s for s in symbols if WS_SYMBOL_RE.match(s)]
        if not valid:
            return
        await self._ensure_meta(valid)          # 预热 multiplier，张→币要用
        backoff = WS_RECONNECT_MIN_S
        last_seq: dict[str, int] = {}
        while True:
            pinger: asyncio.Task | None = None
            try:
                endpoint, token, ping_s = await self._ws_endpoint()
                url = (f"{endpoint.rstrip('/')}/?token={token}"
                       f"&connectId={uuid.uuid4().hex}")
                # ping_interval=None：KuCoin 要的是 JSON 帧 {"type":"ping"}，
                # 让库自己发协议级 ping 不算数
                async with websockets.connect(url, ping_interval=None,
                                              max_queue=2048) as ws:
                    for batch in _chunks(valid, WS_SUBSCRIBE_BATCH):
                        # 逗号分隔的多 topic 订阅（文档：单次订阅最多 100 个 topic）。
                        # UNVERIFIED：合约 tickerV2 的逗号列表写法只有通用订阅说明作依据、
                        # 没有逐字示例；一符号一条流走的是文档给了样例的单 topic 路径。
                        await ws.send(_dumps({
                            "id": str(next(self._ws_id)), "type": "subscribe",
                            "topic": "/contractMarket/tickerV2:" + ",".join(batch),
                            "response": True}))
                    pinger = asyncio.create_task(self._ws_heartbeat(ws, ping_s))
                    backoff = WS_RECONNECT_MIN_S
                    async for raw in ws:
                        if isinstance(raw, bytes):
                            raw = raw.decode()
                        msg = json.loads(raw, parse_float=Decimal)
                        kind = msg.get("type")
                        if kind in ("welcome", "ack", "pong"):
                            continue
                        if kind == "error":
                            # 订阅被拒/token 过期：整条连接重来。静默 continue 会变成
                            # 一条永远不推数据的僵尸流。
                            break
                        if kind != "message" or msg.get("subject") != "tickerV2":
                            continue
                        item = msg.get("data") or {}
                        symbol = str(item.get("symbol") or "")
                        seq = int(_dec(item.get("sequence"), "0"))
                        if seq and seq < last_seq.get(symbol, 0):
                            continue            # 乱序/重放包直接丢
                        if seq:
                            last_seq[symbol] = seq
                        top = self._ws_to_top(symbol, item)
                        if top is not None:
                            yield top
            except asyncio.CancelledError:
                raise       # 必须原样上抛，被 except Exception 吞掉就取消不掉任务了
            except Exception:
                pass
            finally:
                if pinger is not None:
                    pinger.cancel()
            # 重连前**必须** sleep 3~5 秒（文档因一次 DDoS 事故专门补的）。
            # 抖动是为了让一堆连接不要踩着同一秒集体回来。
            await asyncio.sleep(backoff + random.random() * 2.0)
            backoff = min(backoff * 2, WS_MAX_BACKOFF_S)

    async def _ws_heartbeat(self, ws: Any, interval_s: float) -> None:
        while True:
            await asyncio.sleep(interval_s)
            # 每条客户端消息都算进"100 条/10 秒"的预算
            await ws.send(_dumps({"id": str(next(self._ws_id)), "type": "ping"}))

    def _ws_to_top(self, symbol: str, item: Mapping[str, Any]) -> BookTop | None:
        """tickerV2 推送 → BookTop。size 是**张**、ts 是**纳秒**，和 REST 一样。"""
        meta = self._meta.get(symbol)
        if meta is None:
            return None     # 没有 multiplier 就换算不出币数，宁可丢这一拍
        bid, ask = item.get("bestBidPrice"), item.get("bestAskPrice")
        if bid in (None, "") or ask in (None, ""):
            return None     # 单边为空，这一拍不可用
        return BookTop(
            bid_price=_dec(bid),
            bid_qty=_dec(item.get("bestBidSize")) * meta.multiplier,
            ask_price=_dec(ask),
            ask_qty=_dec(item.get("bestAskSize")) * meta.multiplier,
            ts_ms=_ts_to_ms(item.get("ts")) or self.timestamp_ms())

    # ---- 精度 / 张币换算 --------------------------------------------------

    @staticmethod
    def round_price(price: Decimal, tick: Decimal, side: str) -> Decimal:
        """价格规整到 tickSize 的整数倍：**买单向上、卖单向下**，方向与
        executor.cross_price 一致。保护限价是**故意穿透对手价**的，反向取整会把穿透削
        回来；在 tick 相对价格很粗的小币上那不是少赚一个 tick，而是 IOC 根本挂不上、
        这条腿永远补不平。"""
        if tick <= 0:
            return price
        return _round_step(price, tick,
                           ROUND_CEILING if side.lower() == "buy" else ROUND_FLOOR)

    @staticmethod
    def coins_to_lots(qty_coins: Decimal, meta: _ContractMeta) -> Decimal:
        """币数 → 张数，**向零取整**（宁可少下也不能超过目标），再按 lotSize 向下对齐。

        size 必须是正整数：把 0.001 当 size 传会被拒，把 1 当"1 个 BTC"算就是 1000 倍
        仓位错配——这一步算错比签名错危险得多，签名错立刻报错，量级错是静默成交。
        """
        if meta.multiplier <= 0:
            raise ExchangeError("kucoin", "BAD_CONTRACT",
                                f"{meta.symbol} multiplier 非正，无法把币折成张")
        lots = (qty_coins / meta.multiplier).to_integral_value(rounding=ROUND_DOWN)
        step = meta.lot_size_lots if meta.lot_size_lots > 0 else Decimal("1")
        return _round_step(lots, step, ROUND_DOWN)

    @staticmethod
    def _time_in_force(req: OrderRequest) -> tuple[str, bool]:
        """(timeInForce, postOnly)。

        Add Order 页只列 **GTC/IOC/RPI**，Get Order 页却列 GTC/GTT/IOC/FOK（两页矛盾，
        UNVERIFIED）。以下单页为准：**不发 FOK**，也不把 FOK 静默降级成 IOC ——
        FOK 的全部意义就是"不许部分成交"，降级会留下一条半成的腿，正是这套系统最怕的
        隐形单腿。调用方要 FOK 就在本地炸掉，让它看得见。
        """
        tif = (req.time_in_force or "IOC").upper()
        if tif == "FOK":
            raise OrderRejected("kucoin", "TIF_UNSUPPORTED",
                                "KuCoin Add Order 只接受 GTC/IOC/RPI，不发 FOK；"
                                "静默降级成 IOC 会允许部分成交，那是隐形单腿的来源")
        return _TIF.get(tif, "GTC"), tif in _POST_ONLY_TIF

    # ---- 交易 -----------------------------------------------------------

    def _client_oid(self, raw: str) -> str:
        """校验 clientOid：≤40 字符，只允许数字/字母/下划线/连字符。

        **绝不做字符替换**：替换会破坏 client id 的全局唯一性假设，上层拿原 ID 回查就
        查不到，而幂等正是靠它撑着。不合法就地炸，由主控去收紧 ID 生成器
        （时间戳拼接常见的 `.` 和 `:` 在这家会被直接拒）。
        """
        if not raw:
            raise ValueError("kucoin 下单必须带 client_order_id：下单响应不带成交信息，"
                             "clientOid 是唯一能回查这一单的抓手")
        if not CLIENT_OID_RE.match(raw):
            raise ValueError(
                f"kucoin clientOid 不合法: {raw!r}；只允许数字/字母/下划线/连字符、"
                "最长 40 字符（这里不做替换——替换会破坏 ID 的全局唯一性）")
        return raw

    async def place_order(self, req: OrderRequest) -> OrderResult:
        """下单 `POST /api/v1/orders`（weight 2，走 quota="trade"）。

        qty 是 **base 币**，这里换算成**张**并向零取整；取整后归零或超上限一律**本地**
        OrderRejected 不发请求（省一次 RTT 和一次限频配额）。**响应不带任何成交信息**，
        所以返回 new + 零成交，IOC 单必须由调用方紧跟一次 query_order——跟币安下单直接
        回成交明细完全不同，把这里的零成交当真会以为没成交。超时/断流/5xx/认不出的
        错误码一律抛 OrderUnknown，**绝不当失败返回**；**不注入任何返佣码**。
        """
        client_oid = self._client_oid(req.client_order_id)
        meta = await self._meta_of(req.symbol)
        tif, post_only = self._time_in_force(req)

        lots = self.coins_to_lots(req.qty, meta)
        if lots <= 0:
            raise OrderRejected(self.venue.value, "PINCHED",
                                f"{req.symbol} {req.qty} 币不足一张"
                                f"（multiplier={meta.multiplier}）")
        cap = meta.market_max_order_qty if req.price is None else meta.max_order_qty
        if cap > 0 and lots > cap:
            raise OrderRejected(self.venue.value, "ORDER_SIZE_TOO_LARGE",
                                f"{req.symbol} {lots} 张 > 单笔上限 {cap} 张")

        body: dict[str, Any] = {
            "clientOid": client_oid,
            "symbol": req.symbol,
            "side": req.side.lower(),
            "type": "limit" if req.price is not None else "market",
            "size": int(lots),                      # **张数，正整数**
            "marginMode": self.margin_mode,
        }
        if req.price is not None:
            body["price"] = _dec_str(self.round_price(req.price, meta.tick, req.side))
            body["timeInForce"] = tif
            if post_only:
                body["postOnly"] = True             # 文档：IOC 下 postOnly 无效
        if req.reduce_only:
            # 文档明列四条后果：数量要自己算（不保证一单平完）；会加仓则直接拒；数量超
            # 持仓则该单被撤；所有活跃减仓单之和超持仓时价差较劣的被撤或部分撤。
            body["reduceOnly"] = True
        if self.margin_mode == "ISOLATED" and not req.reduce_only:
            # 逐仓开仓的杠杆**只能从这里带**（没有逐仓设杠杆端点，见 set_leverage）。
            # 没设过就不带，让服务端用账户默认值，别自己编一个发出去。
            lev = self._isolated_leverage.get(req.symbol)
            if lev is not None:
                body["leverage"] = int(lev)
        # positionSide 单向模式可省（默认 BOTH）；对冲模式已在 __init__ 拒掉。

        try:
            data = await self._request("POST", "/api/v1/orders", quota="trade",
                                       signed=True, body=body)
        except OrderRejected:
            raise           # 明确被拒 = 撮合肯定没收到，不必白跑一轮消解
        except RateLimited:
            raise           # 限频 = 确定没进撮合，可安全重试
        except ExchangeError as exc:
            if exc.code in _DUPLICATE_CLIENT_OID_CODES:
                # 300018 = 上一发已经进去了。**必须转去查单**，换个 ID 重发 = 双倍仓位。
                raise OrderUnknown(self.venue.value, req.client_order_id) from exc
            if exc.code in _PRE_MATCH_CODES:
                # 鉴权/时间戳/Content-Type：撮合根本没见过这个请求，升级成 unknown 只会
                # 白跑一轮 cancel+查单
                raise
            # 其余全部倒向未知：NETWORK / BAD_JSON / 5xx / 500000 / 结算中，以及**认不出
            # 的错误码**。判定标准一句话——只有能证明"没进撮合"时才允许说"失败"，
            # 证不了就是未知。多跑一轮 cancel+查单只花一次 RTT，误判是双倍仓位。
            raise OrderUnknown(self.venue.value, req.client_order_id) from exc
        except httpx.TransportError as exc:
            # _request 已经把 httpx.HTTPError 全族包成了 NETWORK，这条是六家统一的兜底。
            # TransportError 覆盖 Timeout/Network/Protocol/Proxy 全族，逐个列就会漏，
            # 漏一个就是一次裸奔的双倍仓位。
            raise OrderUnknown(self.venue.value, req.client_order_id) from exc

        data = data or {}
        return OrderResult(
            client_order_id=req.client_order_id,    # 回传调用方的原值，上层按它对账
            exchange_order_id=str(data.get("orderId") or ""),
            status="new",       # Ack ≠ 成交：这个响应只表示 KuCoin 收下了请求
            filled_qty=ZERO, avg_price=ZERO)

    async def cancel_order(self, symbol: str, client_order_id: str) -> None:
        """撤单 `DELETE /api/v1/orders/client-order/{clientOid}?symbol=`（weight 1）。

        状态终结操作，消解 OrderUnknown 的第一步。两条 KuCoin 特有的：`symbol` 是
        **必填查询参数**（clientOid 走路径）；**"撤单成功"只代表请求被撮合引擎接收、
        不代表已撤**（文档原文：真实状态要回查或看推送），所以 cancel 返回后立刻 query
        可能仍读到 open——上层读到 status="new" 要按"还没终结"处理，不能当"没成交"。
        """
        oid = self._client_oid(client_order_id)
        try:
            await self._request("DELETE", f"/api/v1/orders/client-order/{oid}",
                                quota="risk", signed=True, params={"symbol": symbol})
        except ExchangeError as exc:
            if exc.code in _ORDER_TERMINAL_CODES:
                # 已成/已撤 = 撤单目的已达成，就地吞掉让消解流程继续去查成交量；
                # 抛出去会让 resolve_unknown_order 白跑（它只捕获 OrderRejected）。
                return
            raise self._risk_path_error(exc) from exc

    async def query_order(self, symbol: str, client_order_id: str) -> OrderResult:
        """查单 `GET /api/v1/orders/byClientOid?clientOid=`（weight 5，quota="risk"）。

        "订单不存在" → **OrderRejected**（resolve_unknown_order 靠它判定"这单从未被接受"
        并返回 0）。这家没给专用错误码（UNVERIFIED），实测多半是 code=200000 但 data 为空，
        所以显式把空 data 翻成 OrderRejected。反过来"存在过且已终结"（100004 那一类）
        **绝不能**映射成 OrderRejected：混桶会让一张真成交的单被报成零成交，上层照着 0
        再补一次仓 = 双倍仓位。Classic 账户下 canceled/filled 只能回查 **7×24 小时**，
        超窗查不到不代表"没这单"——**自己的成交必须落库，别指望回查**。
        """
        oid = self._client_oid(client_order_id)
        meta = await self._meta_of(symbol)
        try:
            data = await self._request("GET", "/api/v1/orders/byClientOid",
                                       quota="risk", signed=True,
                                       params={"clientOid": oid})
        except ExchangeError as exc:
            raise self._risk_path_error(exc) from exc
        if not data:
            raise OrderRejected(self.venue.value, "ORDER_NOT_FOUND",
                                f"{symbol} 查无此单 {client_order_id}")
        return self._parse_order(data, meta, fallback_client_id=client_order_id)

    def _parse_order(self, row: Mapping[str, Any], meta: _ContractMeta,
                     fallback_client_id: str = "") -> OrderResult:
        """订单对象 → OrderResult。

        **status 只有 open / done 两个值，没有 canceled** —— 已撤和已成都是 done，直接
        把 status 映射过去会让"被撤的空单"和"全成交的单"长得一模一样，只能靠
        filledSize + cancelExist 推。另外**文档把 filledSize / filledValue 的描述写反了**，
        按**类型**判断：filledSize 是 integer（张数）、filledValue 是 string（名义额）。
        `fee` 恒为 0：订单响应里**根本没有手续费字段**（精确成本走 fetch_order_fee），
        填一个猜出来的费用比填 0 危险——0 至少是"未知"的诚实表达。
        """
        size_lots = _dec(row.get("size"))
        filled_lots = _dec(row.get("filledSize"))
        cancel_exist = bool(row.get("cancelExist"))
        if str(row.get("status") or "") == "done":
            if filled_lots > 0 and (size_lots <= 0 or filled_lots >= size_lots):
                status = "filled"
            else:
                # 含"部成后撤"：终结态用 canceled 表达，filled_qty 带着已成部分供上层做
                # 仓位差分；报成 new 会让状态机以为这单还活着。
                status = "canceled"
        elif filled_lots > 0:
            status = "partial"
        else:
            status = "canceled" if cancel_exist else "new"
        return OrderResult(
            client_order_id=fallback_client_id or str(row.get("clientOid") or ""),
            exchange_order_id=str(row.get("id") or row.get("orderId") or ""),
            status=status,
            filled_qty=filled_lots * meta.multiplier,       # 张 → 币
            # avgDealPrice 对**反向**合约是 sum(qty)/sum(value) 反着算的（文档明说）；
            # 反向合约已被 _load_contracts 挡在外面，所以这里可以直用
            avg_price=_dec(row.get("avgDealPrice")),
            fee=ZERO, fee_asset=str(row.get("settleCurrency") or ""))

    async def fetch_order_fee(self, order_id: str) -> tuple[Decimal, str]:
        """按交易所单号汇总实际手续费，`GET /api/v1/fills?orderId=`（weight 5，risk 池）。

        不是抽象接口的一部分：订单响应没有手续费字段，而这一次查询多花 weight 5，
        **只在需要精确成本核算时调，别每单都调**。`fee` **正数 = 成本**（注意 positions
        的 dealComm 是负数，两处符号相反，别混用）。数据保留 3 个月但**单次查询范围
        ≤ 7×24 小时**，想拉一个月必须自己切窗口。
        """
        # fills 是分页信封（currentPage/pageSize/totalNum/totalPage/items）。
        # 只拿首页求和，在被撮合成几十笔的市价/IOC 单上会静默少算手续费且不报错——
        # 而这个方法存在的唯一理由就是"订单响应里没有手续费字段时的精确核算"。
        # 页数设硬闸：单合约单单的成交笔数有限，翻不完说明响应形状变了，
        # 宁可停在闸上也别无限翻页烧 risk 池配额。
        total, asset = ZERO, ""
        page, max_pages = 1, 10
        while page <= max_pages:
            data = await self._request(
                "GET", "/api/v1/fills", quota="risk", signed=True,
                params={"orderId": order_id, "currentPage": page, "pageSize": 500})
            items = (data or {}).get("items") or []
            for item in items:
                total += _dec(item.get("fee"))
                asset = asset or str(item.get("feeCurrency") or "")
            if not items or page >= int(_dec((data or {}).get("totalPage"), "1")):
                break
            page += 1
        return total, asset

    async def fetch_positions(self, symbols: Sequence[str] | None = None
                              ) -> Sequence[Position]:
        """持仓 `GET /api/v1/positions`（weight 2，quota="risk"）。持仓是**真值**，
        本地记账与它冲突时无条件以它为准；session.validate_account 也拿它做连通性自检，
        所以**零仓账户上也必须能正常返回空序列**。

        - `currentQty` 是**张**、integer、**带符号**（UNVERIFIED：文档没明文写"负数=空头"，
          是从字段类型 integer 而非 unsigned + 单向模式下 positionSide 恒为 BOTH 反推的）。
        - `liquidationPrice` 拿到 0/空一律填 **None**：填 0 会让 liquidation_distance
          算出"还有 100% 空间"，风控直接失明。
        """
        rows = await self._request("GET", "/api/v1/positions", quota="risk",
                                   signed=True) or []
        wanted = set(symbols) if symbols else None
        await self._ensure_meta([str(r.get("symbol") or "") for r in rows])
        out: list[Position] = []
        for row in rows:
            symbol = str(row.get("symbol") or "")
            if wanted is not None and symbol not in wanted:
                continue
            meta = self._meta.get(symbol)
            if meta is None:
                continue    # 连规格都补不回来就没法折成币；漏一条也别报一个错误的数
            lots = _dec(row.get("currentQty"))
            if str(row.get("positionSide") or "BOTH").upper() == "SHORT" and lots > 0:
                lots = -lots        # 对冲模式两条记录都是正数，方向看 positionSide
            liq = _dec(row.get("liquidationPrice"))
            out.append(Position(
                market=self.market, symbol=symbol,
                qty=lots * meta.multiplier,                 # 张 → 币，带符号
                entry_price=_dec(row.get("avgEntryPrice")),
                mark_price=_dec(row.get("markPrice")),
                liquidation_price=liq if liq > 0 else None,
                leverage=self._position_leverage(row)))
        return tuple(out)

    @staticmethod
    def _position_leverage(row: Mapping[str, Any]) -> Decimal:
        """真实杠杆倍数。填 0 会让杠杆相关风控全部失真，所以要一路兜底：realLeverage
        只在逐仓下有效（文档标了 Only applicable to Isolated Margin），全仓下缺失或为 0。
        都拿不到就用 markValue / posMargin 现算——那本来就是杠杆的定义，比塞一个 1 诚实。
        """
        for key in ("leverage", "realLeverage"):
            lev = _dec(row.get(key))
            if lev > 0:
                return lev
        margin, value = _dec(row.get("posMargin")), _dec(row.get("markValue"))
        if margin > 0 and value != 0:
            return abs(value) / margin
        return Decimal("1")

    async def fetch_open_orders(self, symbol: str) -> Sequence[OrderResult]:
        """未成交挂单 `GET /api/v1/orders?status=active`（weight 2，quota="risk"）。

        **status 不传默认是 done** —— 本家最容易踩的默认值：漏传会拿到一堆历史成交单，
        然后这个方法报告"有 50 个未成交挂单"，两腿平衡判断直接崩掉。判断两腿是否平衡
        必须计入这些：「A 已成交、B 还挂着」是限价模式下最容易出现的隐形单腿。
        """
        meta = await self._meta_of(symbol)
        out: list[OrderResult] = []
        page = 1
        while page <= OPEN_ORDER_MAX_PAGES:
            data = await self._request("GET", "/api/v1/orders", quota="risk",
                                       signed=True,
                                       params={"status": "active", "symbol": symbol,
                                               "currentPage": page,
                                               "pageSize": OPEN_ORDER_PAGE_SIZE})
            data = data or {}
            items = data.get("items") or []
            out.extend(self._parse_order(row, meta) for row in items)
            if page >= int(_dec(data.get("totalPage"), "1")) or not items:
                break
            page += 1
        return tuple(out)

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        """设杠杆。**两种保证金模式走完全不同的路径**：

        - CROSS：`POST /api/v2/changeCrossUserLeverage`，body 里 leverage 是 **string**
          （下单接口里的同名字段却是 integer，两处类型不一致）。
        - ISOLATED：KuCoin **根本没有逐仓设杠杆的接口**，杠杆随下单参数走，所以这里
          只把值存进实例、由 place_order 带上。按惯例去调全仓那个端点会返回 200
          `data:true` 而杠杆一点没变——**静默失效**，直到爆仓距离对不上才发现。

        走 quota="risk"：降杠杆是风控动作，不能被行情轮询挤掉。
        """
        lots = int(leverage)        # KuCoin 杠杆是整数倍；向下取整 = 宁可少借
        if lots < 1:
            raise ValueError(f"kucoin 杠杆必须 >= 1，收到 {leverage}")
        if self.margin_mode != "CROSS":
            self._isolated_leverage[symbol] = Decimal(lots)
            return
        await self._request("POST", "/api/v2/changeCrossUserLeverage", quota="risk",
                            signed=True, body={"symbol": symbol, "leverage": str(lots)})

    async def fetch_leverage_tiers(self, symbol: str
                                   ) -> Sequence[tuple[Decimal, Decimal]]:
        """杠杆档位 [(名义上限, 最大杠杆), ...] **升序**。
        `GET /api/v1/contracts/risk-limit/{symbol}`（**Public 池，不需要签名**，weight 5）。
        响应天然升序，maxRiskLimit 单位是 USDT 且**上边界含等号**，贴边判断要用 >= 不是 >。

        **这张表只对逐仓有效**（端点标题就是 Get Isolated Margin Risk Limit）；全仓的
        名义硬上限在 contracts/active 的 crossRiskLimit 里，另有 Get Cross Margin
        Requirement 给全仓分档（字段结构本次未逐字读取，UNVERIFIED）。用逐仓表判全仓
        **偏保守**，可接受但结论比实际严格。查不到时退化成单档 [(Infinity, maxLeverage)]：
        **退化后避开档位边界的逻辑等于失效**，上线前必须补真实分档；绝不返回空列表
        ——空列表和"没有档位限制"在上层是两种完全不同的含义。
        """
        try:
            rows = await self._request("GET", f"/api/v1/contracts/risk-limit/{symbol}",
                                       quota="market") or []
        except ExchangeError:
            rows = []
        tiers: list[tuple[Decimal, Decimal]] = []
        for row in rows:
            cap, lev = _dec(row.get("maxRiskLimit")), _dec(row.get("maxLeverage"))
            if cap > 0 and lev > 0:
                tiers.append((cap, lev))
        if tiers:
            tiers.sort(key=lambda t: t[0])
            return tuple(tiers)
        meta = await self._meta_of(symbol)
        return ((Decimal("Infinity"), meta.max_leverage),)

    async def fetch_position_mode(self) -> str:
        """`GET /api/v2/position/getPositionMode`。**没设过持仓模式就下单会报 330011**，
        所以启动自检里应该先读一次，而不是等第一单被拒。"""
        data = await self._request("GET", "/api/v2/position/getPositionMode",
                                   quota="risk", signed=True) or {}
        # 不能用 `or` 链：服务端若回数字 0（单向），真值判断会把它吞成空串，
        # 把"单向模式"读成"读不到"
        for key in ("positionMode", "posMode"):
            if data.get(key) is not None:
                return str(data[key])
        return ""

    async def switch_position_mode(self, hedge: bool = False) -> None:
        """`POST /api/v2/position/switchPositionMode` 切单向/对冲。本适配器只支持单向
        （对冲在 __init__ 就被拒了），所以正常用法是把账户**切回**单向。

        positionMode 按官方文档是 **string**（"0" 单向 / "1" 对冲），不是数字。
        这是 __init__ 拒对冲时报错文案指的**唯一逃生出口**——它自己要是发个
        文档不收的类型吃 100001，用户就被卡死在"下单 330011 → 切换又报参数错"里。
        """
        await self._request("POST", "/api/v2/position/switchPositionMode",
                            quota="risk", signed=True,
                            body={"positionMode": "1" if hedge else "0"})
