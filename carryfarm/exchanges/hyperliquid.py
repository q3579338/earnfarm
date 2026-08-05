"""Hyperliquid L1 永续适配器（dex="" 的官方主市场）。

调研依据：docs/research/exchange-hyperliquid.md（2026-08-05，对着官方 gitbook +
官方 Python SDK 的 utils/signing.py、exchange.py 逐行核过）。

这家和前六家 CEX 是两个物种，本文件的形状全部由下面四条派生：

1. **鉴权是钱包签名，签的不是 HTTP body 字节而是 ``action`` 的 msgpack 编码**。
   于是 JSON 怎么序列化都不影响签名（和 Gate「签什么字节就发什么字节」正好相反），
   代价是换来一个更刁的约束：**msgpack 按 dict 插入顺序编码，action 的键顺序就是签名**。
   任何"顺手整理一下字段顺序"的重构都会让下单全线失败，而报的错是
   ``User or API Wallet 0x... does not exist``（那地址是从错签名里恢复出来的垃圾地址）。
   所以本文件的 action 一律**字面量 dict**、按官方 SDK 的顺序写死，不用 ``**kwargs``
   和字典推导。

2. **整个 API 只有两个路径**：``POST /info``（只读、公开、不签名）和 ``POST /exchange``
   （写操作、签名）。没有路径参数、没有 query string，请求类型写在 body 的
   ``type`` / ``action.type`` 里。因此 ``_prepare`` 的活儿是"包信封"而不是"拼签名串"。

3. **eth_account / msgpack 是可选依赖**（软导入）。/info 全部公开——榜单、深度、
   历史资金费、费率档、持仓、挂单、查单一个都不需要私钥，缺包时适配器照样构造、
   公开端点照样跑（public_feed.open_public_adapters 用 ``Credential("", "")`` 构造，
   构造失败等于这家从机会榜上整片消失），只有 place/cancel/set_leverage 三条路径
   抛 SIGNING_UNAVAILABLE。

4. **资金费每小时结算**（interval_s = 3600）。写死 8 小时/1095 期会把这家算成 1/8。

单位口径：HL 没有"张"，但有 **k 前缀千倍币**（kPEPE = 1000 PEPE）。倍数按本仓库既有
约定装进 ``Instrument.contract_size``、base 归一成 PEPE，价除量乘的换算封死在本文件内
（与 binance.py 处理 1000PEPEUSDT 一模一样），**对外一律 base 币口径**。
倍数含义本身是 UNVERIFIED，见 K_MULTIPLIER 的注释和 include_k_prefixed 开关。

返佣：HL 的返佣等价物是 action 里的 ``builder`` 字段。**用户明确要求不注入任何
返佣码/邀请码**，所以 broker_code 接下来什么都不做：不加 header、不加 body 字段、
不给 client_order_id 加前缀。这不是"以后再加"——builder 一旦进 action，msgpack
字节就变了，签名和已有测试一起失效。

符号约定：``CarryRate.rate`` 用**交易所原始约定**（正 = 多头付给空头），原样写入
**不取反**。（base.py 那句"取负成多头视角"是过时说法，models.CarryRate 与 scoring 的
``short_rate - long_rate`` 才是当前口径，六家都跟随 models。）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import threading
import time
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, AsyncIterator, Mapping, Sequence

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

# ---- 可选依赖：软导入 ---------------------------------------------------
# 缺这两个包只影响 /exchange 三条写路径，绝不能让 import 本模块或构造适配器失败。
# keccak 取自 eth_utils（eth_account 的传递依赖，装了 eth_account 就一定有），
# 不要为了一个哈希去引 pycryptodome / sha3。
try:                                    # pragma: no cover - 取决于运行环境装没装
    import msgpack as _msgpack
    from eth_account import Account as _Account
    from eth_account.messages import encode_typed_data as _encode_typed_data
    from eth_utils import keccak as _keccak
    from eth_utils import to_hex as _to_hex

    _SIGN_IMPORT_ERROR: Exception | None = None
except ImportError as _exc:             # pragma: no cover
    _msgpack = None                     # type: ignore[assignment]
    _Account = None                     # type: ignore[assignment]
    _encode_typed_data = None           # type: ignore[assignment]
    _keccak = None                      # type: ignore[assignment]
    _to_hex = None                      # type: ignore[assignment]
    _SIGN_IMPORT_ERROR = _exc


ZERO = Decimal("0")

REST_BASE = "https://api.hyperliquid.xyz"
REST_BASE_TESTNET = "https://api.hyperliquid-testnet.xyz"
WS_BASE = "wss://api.hyperliquid.xyz/ws"
WS_BASE_TESTNET = "wss://api.hyperliquid-testnet.xyz/ws"

INFO_PATH = "/info"
EXCHANGE_PATH = "/exchange"

# EIP-712 域。chainId 恒为 **1337**——这是历史遗留的假链 id，
# 不是 Arbitrum 的 42161，也不是 HyperEVM 的 998/999。填错签出来的是另一个地址。
EIP712_CHAIN_ID = 1337
EIP712_DOMAIN_NAME = "Exchange"
EIP712_DOMAIN_VERSION = "1"
EIP712_VERIFYING_CONTRACT = "0x0000000000000000000000000000000000000000"
# 主网 "a" / 测试网 "b"。**主网测试网不是只换域名**：这一个字符错了，
# 签名会恢复出另一个地址，服务端报的却是"用户不存在"，离病根很远。
PHANTOM_SOURCE_MAINNET = "a"
PHANTOM_SOURCE_TESTNET = "b"

# 永续价格规则：小数位 <= MAX_DECIMALS - szDecimals，且有效数字 <= 5（整数价除外）。
# 现货是 8，本适配器不做现货。
PERP_MAX_DECIMALS = 6
PRICE_SIG_FIGS = 5

# 每小时整点结算，全市场统一。文档原文 "The funding rate on Hyperliquid is paid every hour"。
FUNDING_INTERVAL_S = 3600
FUNDING_INTERVAL_MS = FUNDING_INTERVAL_S * 1000
# ±4%/小时，且文档明说 "The funding cap and funding interval do not depend on the asset"
# ——全市场同一个数，不需要逐币查、不需要发任何请求。
# **UNVERIFIED**：文档只写了上限 "capped at 4%/hour"，下限按对称绝对值理解。
# 注意 4%/h 折年化是 350 倍，这个 cap 高得几乎不构成一票否决位，
# 别指望像 Bybit 的 ±0.75%/8h 那样拿它去否决脏数据。
FUNDING_CAP = Decimal("0.04")
FUNDING_FLOOR = Decimal("-0.04")

# $10 最小名义额（错误串 MinTradeNtl: "Order must have minimum value of $10."）。
MIN_ORDER_NOTIONAL = Decimal("10")
# userFees 权重 20 而档位一天变不了一次，适配器内部再兜一层缓存
# （session 那层也缓存，但 public_feed 走的是另一条路，不经过它）。
FEE_CACHE_TTL_S = 3600.0

# fundingHistory：带时间范围的响应最多 500 条；每小时一条 → 单页只有约 20.8 天。
# 30 天窗口**必须**翻两页，这不是可选优化。
FUNDING_HISTORY_PAGE = 500
# 页数硬闸（32 页 ≈ 666 天）。服务端万一不推进游标，没有这道闸就是无限翻页。
FUNDING_HISTORY_MAX_PAGES = 32

# 市价单（OrderRequest.price is None）的保护限价穿透幅度。HL **没有市价单类型**，
# "market" 是前端行为：用 Ioc + 足够穿透的限价实现。穿不够的后果是 IocCancel
# （这一拍没成交），穿太多的后果是极端行情下按坏价成交，50bp 是两害之间。
MARKET_PROTECTION_BP = Decimal("50")

# WS 限额（官方限频页）：单 IP 最多 10 条连接、每分钟 30 条新连接、最多 1000 条订阅、
# 每分钟 2000 条上行消息。一个 coin 一条 subscription，**没有批量订阅数组的写法**。
WS_MAX_SUBSCRIPTIONS = 1000
# **UNVERIFIED**：官方文档没写心跳规格与超时时长，只说"自行处理断线并优雅重连"，
# 且断线"可能不预告地周期性发生"。这里按"自己定时发 ping + 无数据超时即重连"实现。
WS_PING_INTERVAL_S = 30.0
WS_IDLE_TIMEOUT_S = 75.0

# 千倍前缀 k（kPEPE / kBONK / kSHIB / kFLOKI / kLUNC / kNEIRO …）。
# **UNVERIFIED：官方文档完全没提这个前缀的含义**（contract-specifications 只说
# "1 unit of underlying spot asset"）。按行业惯例 1 单位 = 1000 个底层币，等价于币安的
# 1000PEPEUSDT——而本仓库对那一族的处理已经定死：base 归一成 PEPE、倍数进
# contract_size、适配器**对外一律 base 币口径**（binance.py:474-489、860-894）。
# 这里照抄那套：k 币的 contract_size = 1000，价除以它、量乘以它，换算封死在本文件内。
# 猜错的代价是 1000 倍的单腿裸奔，所以留了 include_k_prefixed=False 一键把这些币
# 整个排除出候选集（那时 public_feed 的剥 k 分支自然也就不会被触发）。
K_PREFIX_RE = re.compile(r"^k[A-Z0-9]{2,}$")
K_MULTIPLIER = Decimal("1000")
# HIP-3 builder 市场的币名是 {dex}:{coin}（如 xyz:XYZ100）。本适配器只做 dex=""，
# 带冒号的名字一律跳过；顺带滤掉非 ASCII，避免 WS 订阅出幺蛾子。
SYMBOL_RE = re.compile(r"^[A-Za-z0-9@_\-]+$")

# ---- 错误分类 -----------------------------------------------------------
# Hyperliquid **没有数字错误码，全是英文字符串**，所以只能按关键词分桶。
# 凡是靠关键词猜的分类都标 UNVERIFIED：新错误串随时会冒出来，
# 认不出的一律走安全侧（不可重试；下单路径倒向 OrderUnknown）。

# 逐单/整批的**明确拒绝**：订单确定没进撮合（或确定已终结），可以安全跳过。
# 来源：官方 error-responses 页的错误串表（调研文档"错误码"节逐条抄的）。
_REJECT_HINTS = (
    "must be divisible by tick size",           # Tick
    "minimum value of $",                       # MinTradeNtl
    "minimum value of 10",                      # MinTradeSpotNtl
    "insufficient margin to place order",       # PerpMargin
    "reduce only order would increase",         # ReduceOnly
    "post only order would have immediately matched",   # BadAloPx
    "could not immediately match",              # IocCancel —— 正常结果，不是故障
    "invalid tp/sl price",                      # BadTriggerPx
    "no liquidity available",                   # MarketOrderNoLiquidity
    "open interest is capped",                  # PositionIncrease/FlipAtOpenInterestCap
    "increase open interest too quickly",       # OpenInterestIncrease
    "insufficient spot balance",                # InsufficientSpotBalance
    "price too far from oracle",                # Oracle
    "more aggressive than oracle",              # TooAggressiveAtOpenInterestCap
    "exceed margin tier limit",                 # PerpMaxPosition
    "order has zero size",
)
# MissingOrder。这一句把「从未下单」和「已成交/已撤销」**合并**成了一个错误串，
# 而基类契约要求两者分桶（混桶会让 query_order 谎报零成交 → 双倍仓位）。映射成
# OrderRejected 在这里是安全的：它只出现在 **cancel** 路径上，而 cancel 只负责"终结"，
# 成交量的权威口径是紧接着的 query_order(orderStatus)；订单真不存在时 orderStatus
# 走的是外层 status != "order" 那条路，拿不到这个串。
_MISSING_ORDER_HINTS = ("was never placed", "already canceled")
# 签名/凭据类。**99% 是签名构造错了**（msgpack 键顺序、source a/b 用反、地址大小写、
# 数字尾随零），只有 1% 是真没入金——返回的地址是从错签名恢复出来的垃圾地址。
# 这类错发生在**进撮合之前**，订单确定不存在 → 绝不能升级成 OrderUnknown。
_SIGNATURE_HINTS = ("does not exist", "must deposit before performing actions",
                    "user or api wallet")
# nonce 被拒 = 这个请求确定没被接受（nonce 必须未用过且大于窗口下沿）。同样是进撮合之前。
_NONCE_HINTS = ("nonce",)
# 【别往这里塞裸数字】曾经有一条 "429"：它一个新场景都没多覆盖（HTTP 429 由
# _translate 里同一行的 status == 429 判掉），却会误伤价格——BadAloPx 的官方串是
# "Post only order would have immediately matched, bbo was {bbo}."，bbo 是插值进去的
# 真实价格，104290.0 / 4290.5 这类价格都含 "429"，一条明确的挂单拒绝就被报成限频
# （retriable=True = "确定没进撮合，可安全重试"）→ 上层重发 = 双倍仓位。
# 关键词只挑英文短语，永远别挑数字。
_RATE_LIMIT_HINTS = ("rate limit", "too many request")
_RETRIABLE_HINTS = ("internal error", "service unavailable", "try again",
                    "timeout", "temporarily unavailable", "please retry")

# 这些 code 是"进撮合之前"被挡下的确定性错误，place_order 不得把它们升级成 OrderUnknown。
# BAD_PRIVATE_KEY / SIGNING_UNAVAILABLE 是**请求根本没发出去**（签名时就炸了），
# 漏掉它们会让每一次配置错误都白跑一轮 cancel+查单，烧掉本来就紧张的 action 配额。
_PRE_MATCH_CODES = frozenset({
    "SIGNATURE", "NONCE", "HTTP_422", "SIGNING_UNAVAILABLE", "BAD_PRIVATE_KEY",
    "NO_ACCOUNT_ADDRESS", "BAD_ACCOUNT_ADDRESS", "SPEC_MISSING",
})

# orderStatus 内层 status 的白名单。文档只列了 6 个，明说还有 "15+ other status values"
# 但**没给全表（UNVERIFIED）**。未知值一律兜底成**非终结的 new**——
# 绝不能把不认识的状态当成 canceled，那会让引擎以为没成交然后重下 = 双倍仓位。
_ORDER_STATUS = {
    "open": "new",
    "filled": "filled",
    "canceled": "canceled",
    "cancelled": "canceled",
    "rejected": "rejected",
    "marginCanceled": "canceled",
    "triggered": "new",
}


def _dec(value: Any, default: str = "0") -> Decimal:
    """交易所字符串 → Decimal。绝不经过 float。

    _parse 里已经用 parse_float=Decimal 把裸 JSON 数字接住了，这里的 float 分支
    只是兜底（真踩上说明解析路径出了问题，用 repr 至少不丢十进制字面量）。
    """
    if value is None or value == "":
        return Decimal(default)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def _wire(value: Decimal) -> str:
    """价格/数量进 msgpack 的字符串格式，与官方 SDK 的 float_to_wire 等价。

    ``normalize()`` 去尾随零（官方点名的翻车点：``"50000.0"`` vs ``"50000"``），
    ``format(..., "f")`` 禁掉科学计数法——**两头都会咬人**，不只是小数那头：
    ``str(Decimal("0.00001").normalize())`` → ``'1E-5'``，
    ``str(Decimal("50000.0").normalize())`` → ``'5E+4'``，而 50000 正是官方下单示例
    里的价格。任何地方用 ``str(Decimal)`` 拼 wire 值都是 bug。

    ``-0`` 的判断**必须放在 format 之后**：SDK 那句 ``if rounded == "-0"`` 是死代码
    （``f"{-0.0:.8f}"`` 得到 ``"-0.00000000"``，永不相等），SDK 最终真会把 ``"-0"``
    发上线；放在后面才拦得住。
    """
    text = format(value.normalize(), "f")
    return "0" if text == "-0" else text


def _is_address(value: str) -> bool:
    """0x + 40 hex。地址格式错的后果不是报错而是**静默查到一个空账户**，先拦一道。"""
    return bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", value or ""))


def _norm_address(value: str) -> str:
    """地址一律小写后再用。

    官方点名的翻车点之四：有些字段按 bytes 解析并在全网被自动小写化，
    大小写不一致会签出另一个 action hash。
    """
    return (value or "").strip().lower()


class _AssetMeta:
    """下单真正要用到的合约字段。

    单独存是因为 Instrument 里没地方放 Hyperliquid 特有的三项：**asset index**
    （下单/撤单/改杠杆用的整数）、szDecimals（价格规整要用）、以及 k 前缀倍数。
    """

    __slots__ = ("index", "name", "base", "multiplier", "sz_decimals",
                 "max_leverage", "margin_table_id", "only_isolated", "delisted")

    def __init__(self, index: int, raw: Mapping[str, Any],
                 k_multiplier: Decimal = K_MULTIPLIER) -> None:
        self.index = index
        self.name: str = str(raw.get("name") or "")     # 上线用的原生币名（含 k）
        if K_PREFIX_RE.match(self.name):
            self.base, self.multiplier = self.name[1:], k_multiplier
        else:
            self.base, self.multiplier = self.name, Decimal("1")
        self.sz_decimals = int(raw.get("szDecimals") or 0)
        self.max_leverage = _dec(raw.get("maxLeverage"), "1")
        table = raw.get("marginTableId")
        self.margin_table_id = int(table) if table is not None else None
        self.only_isolated = bool(raw.get("onlyIsolated"))
        self.delisted = bool(raw.get("isDelisted"))

    @property
    def native_lot(self) -> Decimal:
        """**原生**数量粒度 = 10^-szDecimals（单位是 kPEPE 这种上线币名的单位）。

        对外的 Instrument.lot_size 是它乘以 multiplier 之后的 base 币口径。
        """
        return Decimal(1).scaleb(-self.sz_decimals)

    @property
    def price_decimal_step(self) -> Decimal:
        """该币允许的最细小数位 = Instrument.tick_size 的代理值。

        **HL 没有 tick_size 字段**：价格合法性是"5 位有效数字"和"小数位 <= 6−szDecimals"
        两条规则的交集，只有后者与价格无关、填得进 tick_size。所以下单前必须再跑一次
        有效数字规整——只 quantize(tick) 会在 1234.56 这种价上被
        "Price must be divisible by tick size." 拒。
        """
        return Decimal(1).scaleb(-(PERP_MAX_DECIMALS - self.sz_decimals))


class HyperliquidAdapter(ExchangeAdapter):
    """Hyperliquid 永续适配器（dex="" 的官方主市场）。

    **不注入任何返佣/邀请码**：broker_code 接下来什么都不做，action 里绝不出现
    ``builder`` 键，请求头里也没有任何渠道标识。

    凭据映射（和其它六家都不一样，填错代价极高）：

    - ``api_key``    → **账户地址**（主/子账户的 0x…），所有 /info 查询用它。
      **这是最容易配错的一格**：填成 agent 地址会让 fetch_positions 返回一个合法的
      空账户（不报错！），而仓位是真值 → 引擎判定裸奔 → 触发撤退平仓。
    - ``api_secret`` → API Wallet（agent wallet）私钥，"0x" + 64 hex，只用于签名。
    - ``passphrase`` → vaultAddress（可选），金库/子账户下单时填，普通账户留空。
      注意 session.CREDENTIAL_FIELDS 目前只给 HL 开了 (api_key, api_secret)，
      所以走界面加的账户拿不到 vaultAddress——金库下单只能程序化构造。
    - ``uid``        → 不使用。

    api_key 为空而 api_secret 有值时退化成"私钥即主钥"，这对个人主钱包成立、对
    API Wallet **不成立**，所以退化会写进 ``credential_note`` 让界面显示出来。

    单位口径：**对外一律 base 币**。k 前缀币（kPEPE = 1000 PEPE）的倍数走
    ``Instrument.contract_size``，价除量乘的换算封死在本文件内，与 binance.py 处理
    1000PEPEUSDT 的口径完全一致——上层拿到的价永远是"每个 PEPE"、量永远是"多少个 PEPE"。
    """

    venue = Venue.HYPERLIQUID
    rest_base = REST_BASE

    def __init__(self, credential: Credential, *, broker_code: str = "",
                 client: httpx.AsyncClient | None = None,
                 market_kind: MarketKind = MarketKind.PERP,
                 testnet: bool = False,
                 dex: str = "",
                 cross_margin: bool = True,
                 include_k_prefixed: bool = True,
                 k_multiplier: Decimal = K_MULTIPLIER,
                 order_ttl_ms: int | None = None,
                 fetch_fill_prices: bool = True) -> None:
        if market_kind is not MarketKind.PERP:
            # 现货没实现（HL 现货的符号是 PURR/USDC 或 @107，不出现在 meta.universe 里），
            # 缺四项调研：(a) spotMeta 的 tokens/universe 结构与 weiDecimals 语义
            # (b) 现货 asset id = 10000 + spot_index 的换算与下单字段 (c) 现货价格规则
            # MAX_DECIMALS = 8（不是永续的 6）(d) spotClearinghouseState 的余额口径。
            # 杠杆借币（CarryKind.INTEREST）在这个平台上**根本不存在**。
            raise NotImplementedError(
                f"hyperliquid 适配器只支持 {MarketKind.PERP.value}；"
                f"{market_kind.value} 缺现货 spotMeta/asset id/精度规则调研，"
                "且该平台没有杠杆借币市场"
            )
        # 用户从网页复制返佣码常带尾随空白，六家统一在构造期 strip；
        # 这家拿到之后**什么都不做**（见类文档）。
        super().__init__(credential, broker_code=broker_code.strip(), client=client)
        self.market_kind = market_kind
        self.market = f"{self.venue.value}:{market_kind.value}"
        self.testnet = testnet
        self.rest_base = REST_BASE_TESTNET if testnet else REST_BASE
        self.ws_base = WS_BASE_TESTNET if testnet else WS_BASE
        self.dex = dex
        self.cross_margin = cross_margin
        # k 前缀币：include=False 直接排除（倍数含义 UNVERIFIED，见 K_MULTIPLIER），
        # include=True 则把倍数装进 contract_size 并在本文件内做完价除量乘
        self.include_k_prefixed = include_k_prefixed
        self.k_multiplier = k_multiplier
        self.fetch_fill_prices = fetch_fill_prices
        # expiresAfter 是 HL 版"迟到即拒"。它**会进 action hash**（多两段字节），
        # 所以要么全程带、要么全程不带，不能只在部分路径带。默认不带。
        self.order_ttl_ms = order_ttl_ms

        self.account_address = _norm_address(credential.api_key)
        self.vault_address = _norm_address(credential.passphrase) or None
        self.credential_note = ""
        # **绝不在 __init__ 里构造 Account、也绝不 _require_signing()**：
        # 缺 eth_account/msgpack 时连"只看榜单"的用户都构造不出适配器，
        # 而 public_feed.open_public_adapters 正是用 Credential("", "") 构造的，
        # 抛错等于这家在机会榜上整片消失，只在 init_errors 里留一行。
        self._account: Any = None
        self._agent_address: str = ""

        self._meta: dict[str, _AssetMeta] = {}
        self._margin_tiers: dict[int, list[tuple[Decimal, Decimal]]] = {}
        self._meta_lock = asyncio.Lock()
        # 被 include_k_prefixed=False 挡在候选集外的币名，界面可以读它解释"为什么少了几个币"
        self.excluded_symbols: set[str] = set()

        # nonce 必须**进程内单调递增**：500ms 收敛循环里两个协程同时取
        # time.time()*1000 会拿到同一毫秒，第二个请求"nonce 已用过"直接被拒。
        # 用 threading.Lock 而不是 asyncio.Lock，因为 _prepare 是同步方法（基类签名如此），
        # 里面不能 await；同步锁在单事件循环里也是零竞争的。
        self._nonce_lock = threading.Lock()
        self._last_nonce = 0
        # cloid 是 client_order_id 的 sha256 截断，**不可逆**。下单时把映射记下来，
        # fetch_open_orders 才能把 cloid 翻回上层认识的 id。进程内有效，重启即失忆——
        # 所以对账的权威口径仍然是自己落库的成交记录，不是这张表。
        self._cloid_to_client: dict[str, str] = {}
        self._fee_cache: tuple[float, list[FeeSchedule]] | None = None
        # 地址维度 action 配额的最近一次读数（fetch_action_quota 更新），
        # 界面/风控可以读它判断"还剩多少次平仓机会"。
        self.action_quota: dict[str, Any] | None = None

    # ---- 凭据 / 签名可用性 ----------------------------------------------

    @property
    def signing_available(self) -> bool:
        """能不能签名。UI 据此显示"只读模式"。"""
        return _SIGN_IMPORT_ERROR is None and bool(self._cred.api_secret)

    def _require_signing(self) -> None:
        """/exchange 路径的第一行。缺包报 SIGNING_UNAVAILABLE 而不是 ImportError——
        后者会一路穿过 except ExchangeError 的分流逻辑，被当成"未知错误"。"""
        if _SIGN_IMPORT_ERROR is not None:
            raise ExchangeError(
                self.venue.value, "SIGNING_UNAVAILABLE",
                f"下单需要 eth_account + msgpack（pip install eth-account msgpack）："
                f"{_SIGN_IMPORT_ERROR}", retriable=False)
        if not self._cred.api_secret:
            raise ExchangeError(
                self.venue.value, "SIGNING_UNAVAILABLE",
                "没有配置 API Wallet 私钥（api_secret），只能跑公开端点", retriable=False)

    def _signer(self) -> Any:
        """惰性构造并缓存 Account，顺带做一次凭据一致性校验。

        校验的价值全在那条 warning 上：推出的地址**等于** api_key 说明用户填的是
        主钱包私钥而不是 API Wallet——能用，但权限过大（可提现），且和网页端共享同一个
        nonce 计数器，网页上点一下就可能把程序的 nonce 顶掉。
        """
        if self._account is not None:
            return self._account
        self._require_signing()
        secret = self._cred.api_secret.strip()
        try:
            account = _Account.from_key(secret)
        except Exception as exc:        # 私钥格式错：本地就能判定，别拿去撞交易所
            raise ExchangeError(self.venue.value, "BAD_PRIVATE_KEY",
                                f"api_secret 不是合法私钥（应为 0x + 64 hex）：{exc}",
                                retriable=False) from exc
        self._account = account
        self._agent_address = _norm_address(account.address)
        if not self.account_address:
            # 退化：把签名地址当账户地址。对个人主钱包成立，对 API Wallet **不成立**
            # （agent 不持仓，clearinghouseState 会返回空账户）。必须让界面看得见。
            self.account_address = self._agent_address
            self.credential_note = (
                "api_key 为空，已退化成「私钥即主钥」并用签名地址查询账户；"
                "如果你填的是 API Wallet 私钥，这样查到的会是一个空账户，"
                "请把主账户地址填进 api_key"
            )
        elif self._agent_address == self.account_address:
            self.credential_note = (
                "api_secret 是主钱包私钥而不是 API Wallet：权限过大（可提现），"
                "且与网页端共享同一个 nonce 计数器"
            )
        return account

    def _user_address(self) -> str:
        """/info 查询用的账户地址。**必须是主/子账户地址，不是 agent 地址。**

        空地址或格式错的地址绝不能静默返回空结果：仓位是真值，读成空 = 引擎判定裸奔
        = 触发撤退平仓。这是纯配置错误里代价最高的一个，所以就地报错。
        """
        addr = self.account_address
        if not addr and self._cred.api_secret and _SIGN_IMPORT_ERROR is None:
            self._signer()              # 顺带走一次退化逻辑，把地址推出来
            addr = self.account_address
        if not addr:
            raise ExchangeError(self.venue.value, "NO_ACCOUNT_ADDRESS",
                                "没有账户地址：api_key 应填主账户/子账户的 0x 地址",
                                retriable=False)
        if not _is_address(addr):
            raise ExchangeError(self.venue.value, "BAD_ACCOUNT_ADDRESS",
                                f"api_key 不是合法地址（0x + 40 hex）：{addr[:12]}…",
                                retriable=False)
        return addr

    # ---- nonce ----------------------------------------------------------

    def _next_nonce(self) -> int:
        """毫秒时间戳，但**进程内强制单调递增**。

        服务端规则不是简单时间窗口：每个 signer 保存最高的 100 个 nonce，新 nonce 必须
        大于其中最小的那个**且从未用过**，同时落在 (T−2天, T+1天)。所以真正的风险不是
        时钟漂移（窗口是天级），而是**并发**——同毫秒的两个请求会撞车，第二个直接被拒。
        另注 nonce **不是去重键**：超时重发一定变成第二张单，不能靠它做幂等。
        """
        with self._nonce_lock:
            nonce = max(self.timestamp_ms(), self._last_nonce + 1)
            self._last_nonce = nonce
            return nonce

    # ---- L1 action 签名 --------------------------------------------------

    def _action_hash(self, action: Mapping[str, Any], nonce: int,
                     vault_address: str | None, expires_after: int | None) -> bytes:
        """connectionId = keccak(msgpack(action) ‖ nonce ‖ vault 段 ‖ expiresAfter 段)。

        与官方 SDK utils/signing.py 的 action_hash **逐行一致**，四段的顺序和长度一个
        字节都不能挪：(1) msgpack.packb(action)，按 dict 插入顺序编码，**键顺序就是签名**；
        (2) nonce 8 字节大端；(3) vaultAddress 无 → 单字节 0x00，有 → 0x01 + 20 字节地址
        原文；(4) expiresAfter 可选，先补一个 0x00（**这是本段自己的前缀，不是"无"的
        标记**）再 8 字节大端，没有时这两段整个不写。
        """
        data = _msgpack.packb(action)
        data += nonce.to_bytes(8, "big")
        if vault_address is None:
            data += b"\x00"
        else:
            data += b"\x01"
            raw = vault_address[2:] if vault_address.startswith("0x") else vault_address
            data += bytes.fromhex(raw)
        if expires_after is not None:
            data += b"\x00"
            data += expires_after.to_bytes(8, "big")
        return _keccak(data)

    def _sign_l1_action(self, action: Mapping[str, Any], nonce: int,
                        expires_after: int | None) -> dict[str, Any]:
        """EIP-712 "phantom agent" 签名，返回 {r, s, v}。

        domain 名 "Exchange"、version "1"、chainId 1337、verifyingContract 全零；
        primaryType "Agent"，字段顺序 source 在前、connectionId 在后。

        r/s 用 to_hex(int) 生成的是**不补零**的十六进制串（前导零字节会少两个字符），
        这是官方 SDK 的写法，照抄。**UNVERIFIED**：补齐 32 字节的写法是否同样被接受。
        """
        account = self._signer()
        connection_id = self._action_hash(action, nonce, self.vault_address, expires_after)
        payload = {
            "domain": {
                "chainId": EIP712_CHAIN_ID,
                "name": EIP712_DOMAIN_NAME,
                "verifyingContract": EIP712_VERIFYING_CONTRACT,
                "version": EIP712_DOMAIN_VERSION,
            },
            "types": {
                "Agent": [
                    {"name": "source", "type": "string"},
                    {"name": "connectionId", "type": "bytes32"},
                ],
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
            },
            "primaryType": "Agent",
            "message": {
                "source": PHANTOM_SOURCE_TESTNET if self.testnet else PHANTOM_SOURCE_MAINNET,
                "connectionId": connection_id,
            },
        }
        signed = account.sign_message(_encode_typed_data(full_message=payload))
        return {"r": _to_hex(signed.r), "s": _to_hex(signed.s), "v": signed.v}

    # ---- 请求层 ---------------------------------------------------------

    def _prepare(self, method: str, path: str, params: dict, body: dict,
                 signed: bool) -> tuple[dict[str, str], str, dict, dict]:
        """只造信封，不发请求。

        HL 没有 query string、没有路径参数、除 Content-Type 外没有任何 header 要求
        （**没有 api-key header**），所以这里只有两件事：拼 URL、把 action 包进
        {action, nonce, signature} 信封。

        **签名不覆盖 HTTP body 字节**，所以 body 交回原 dict 让 httpx 自己序列化即可，
        不必像 Gate/Bitget 那样手动 dumps 一份对齐字节。但 action 这个 dict 对象
        **必须原样交回去**：它的键顺序既是 msgpack 的编码顺序也是上线 JSON 的键顺序，
        测试正是靠"从上线 JSON 重新 msgpack 能恢复出同一个 signer"守住这条。

        返佣码：一个字节都不加（见类文档）。
        """
        url = self.rest_base + path
        headers = {"Content-Type": "application/json"}
        if not signed:
            # /info 全部公开不签名——榜单/深度/历史费率/费率档/持仓/挂单/查单都走这里
            return headers, url, {}, body

        nonce = self._next_nonce()
        expires_after = (self.timestamp_ms() + self.order_ttl_ms
                         if self.order_ttl_ms else None)
        signature = self._sign_l1_action(body, nonce, expires_after)
        # 官方 SDK 的 _post_action 总是把 vaultAddress / expiresAfter 两个键写进 JSON
        # （值为 null），服务端接受。因为签名不覆盖 body，写不写都行——照 SDK 写。
        envelope = {
            "action": body,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": self.vault_address,
            "expiresAfter": expires_after,
        }
        return headers, url, {}, envelope

    def _parse(self, resp: httpx.Response) -> Any:
        """壳 → data，错误串 → 统一异常。

        Hyperliquid 的成败判定比其它六家多一层：HTTP 200 + ``status:"ok"`` **仍然
        可能是下单被拒**（``{"error": ...}`` 藏在 ``response.data.statuses[i]`` 里）。
        那一层归业务方法判，这里只剥外层的 {status, response} 壳。
        """
        try:
            # parse_float=Decimal：HL 的价格量都是字符串，但 oid/szDecimals/time 是裸整数，
            # 万一哪天冒出裸浮点（如 leverage.value），落到 Decimal(repr(float)) 兜底时
            # 精度已经掉了。这道闸必须在解析层。
            payload = json.loads(resp.text, parse_float=Decimal) if resp.text else None
            bad_json = False
        except ValueError:
            payload = None
            bad_json = True

        if resp.status_code == 429:
            # **UNVERIFIED**：429 是否带 Retry-After、body 什么形状，官方文档没写。
            raise RateLimited(self.venue.value, "HTTP_429", resp.text[:200], retriable=True)
        if bad_json and resp.status_code < 400:
            # 2xx 但响应体不是合法 JSON（WAF/代理注入 HTML）：这单有没有进撮合完全不知道。
            # 返回 None 会让下游解析抛 AttributeError；退化成 retriable=False 会被
            # place_order 当成明确失败 = 发了张"可安全重发"的许可证。统一成 BAD_JSON。
            raise ExchangeError(self.venue.value, "BAD_JSON",
                                f"响应不是合法 JSON: {resp.text[:200]}", retriable=True)
        if resp.status_code >= 400:
            if not isinstance(payload, dict):
                # 【非结构化正文一个字节都不许喂进关键词表】这条正文根本不是交易所写的：
                # 4xx/5xx 时挡在前面的 CDN/WAF/反代会回自己的 HTML 错误页，422 则是
                # axum 的裸文本。把它交给 _translate 等于让别人家的 HTML 决定"这单进没
                # 进撮合"——实测 503 的错误页里只要出现 CSP 的 `nonce="…"` 属性就被判成
                # NONCE(retriable=False，且在 _PRE_MATCH_CODES 白名单里，place_order
                # 原样上抛)，出现 `request id …429…` 就被判成 RateLimited("确定没进撮合，
                # 可安全重试")。两种都是给上层发"这单肯定没成交，放心重发"的许可证 =
                # 双倍仓位。这里只认 HTTP 语义：>=500 才可重试（→ place_order 升级
                # OrderUnknown 走 cancel→查单消解）。
                # 422 = "Failed to deserialize the JSON body into the target type"，
                # 这是**我们的代码 bug**（action 结构不对），重试一万次也是同样结果——
                # 它天然落在 retriable=False 这侧，且 HTTP_422 在 _PRE_MATCH_CODES 里。
                # 同 bitget._parse：关键词表只对交易所自己返回的结构化错误串生效。
                raise ExchangeError(self.venue.value, f"HTTP_{resp.status_code}",
                                    resp.text[:200], retriable=resp.status_code >= 500)
            message = str(payload.get("response") or payload.get("error")
                          or resp.text[:300])
            raise self._translate(message, resp.status_code)

        if isinstance(payload, dict) and payload.get("status") in ("ok", "err"):
            # /exchange 的信封。注意 /info 的 orderStatus 也有 status 键但取值是
            # "order"/"unknownOid"，不会误入这个分支。
            if payload["status"] == "err":
                # 整批预校验失败（空 orders、价格离参考价太远、签名/凭据错…），
                # response 是一个裸字符串
                raise self._translate(str(payload.get("response") or ""), resp.status_code)
            return payload.get("response")
        return payload

    def _translate(self, message: str, status: int) -> ExchangeError:
        """错误串 → 统一异常。判定顺序有意义：先特判、后兜底。

        Hyperliquid 没有数字错误码，只能按关键词分桶，所以每一条分类都可能被
        新错误串绕过（**UNVERIFIED**）。兜底一律走安全侧：不可重试，
        而 place_order 会把"认不出的"倒向 OrderUnknown。
        """
        low = (message or "").lower()
        if status == 429 or any(h in low for h in _RATE_LIMIT_HINTS):
            # 限频 = **确定没进撮合**，是下单路径上唯一可以直接抛、允许上层安全重试的错
            return RateLimited(self.venue.value, "RATE_LIMITED", message, retriable=True)
        if status >= 500:
            # 【HTTP 语义压过任何关键词】5xx 在 /exchange 上意味着订单**可能已经进了撮合**，
            # 契约要求一律升级 OrderUnknown 走 cancel→查单消解。哪怕正文里明明白白写着
            # 某个"确定性拒绝"的串，也不能信：5xx 本身就说明这条正文未必来自撮合引擎
            # （可能是网关在上游超时后自己编的），而"确定没进撮合"这个结论一旦判错，
            # 代价是上层照着补一次仓 = 双倍仓位。放在所有关键词桶之前，不给任何绕过的机会。
            return ExchangeError(self.venue.value, f"HTTP_{status}", message, retriable=True)
        if any(h in low for h in _MISSING_ORDER_HINTS):
            # base.resolve_unknown_order 靠 OrderRejected 判定"这单已终结/从未被接受"
            return OrderRejected(self.venue.value, "MISSING_ORDER", message, retriable=False)
        if any(h in low for h in _SIGNATURE_HINTS):
            # 进撮合之前被挡下，订单确定不存在。**不可重试，也绝不升级成 OrderUnknown。**
            return ExchangeError(self.venue.value, "SIGNATURE", message, retriable=False)
        if any(h in low for h in _NONCE_HINTS):
            return ExchangeError(self.venue.value, "NONCE", message, retriable=False)
        if any(h in low for h in _REJECT_HINTS):
            return OrderRejected(self.venue.value, "ORDER_REJECTED", message, retriable=False)
        if any(h in low for h in _RETRIABLE_HINTS):
            # 走到这里 status 一定 < 500（5xx 已在上面截胡），所以只剩"HTTP 200 但正文
            # 自称内部错误"这一种形态，统一叫 SERVER_BUSY
            return ExchangeError(self.venue.value, "SERVER_BUSY", message, retriable=True)
        return ExchangeError(self.venue.value, f"HTTP_{status}", message, retriable=False)

    def _risk_path_error(self, exc: ExchangeError) -> ExchangeError:
        """风控路径（撤单/查单）上的错误重分类。

        base._request 把传输层异常包成 ExchangeError("NETWORK")，而 resolve_unknown_order
        只捕获 (OrderRejected, httpx.HTTPError, RateLimited)——不转一手的话，网络抖动或
        5xx 会让消解流程在最危险的时刻带着未捕获异常崩掉。可重试的重挂到 RateLimited
        （code 保留真实原因）；不可重试的**原样上抛，绝不降级成 OrderRejected**——
        降级等于谎报"成交量为 0"，上层会照着 0 再补一次仓。
        """
        if isinstance(exc, (OrderRejected, RateLimited)) or not exc.retriable:
            return exc
        return RateLimited(self.venue.value, exc.code, exc.message, retriable=True)

    async def _info(self, payload: dict[str, Any], *, quota: str = "market") -> Any:
        """/info 的薄封装。全部公开不签名——包括 userFees / clearinghouseState 这些
        在别家必须签名的端点（Hyperliquid 的账户数据本来就是链上公开的）。"""
        return await self._request("POST", INFO_PATH, quota=quota, body=payload)

    # ---- 元数据 ---------------------------------------------------------

    async def fetch_server_time_ms(self) -> int:
        """服务器毫秒时间。exchangeStatus 权重 2、公开不签名（校时端点必须不签名，
        否则未校时的时钟去签名会死锁；这家所有 /info 都不签名，天然满足）。

        **UNVERIFIED**：{specialStatuses, time} 这个字段形状是从 QuickNode 镜像文档
        拿的，gitbook 正文没给示例，所以退路是 l2Book 的 time（官方有示例、权重同样 2）。

        另注：HL 的 nonce 窗口是 ±天级，校时对签名几乎无意义，这个方法主要是满足基类
        抽象接口和 session.validate_account 的漂移自检。
        """
        try:
            data = await self._info({"type": "exchangeStatus"})
            return int(_dec(data["time"]))
        except (ExchangeError, KeyError, TypeError, ValueError, InvalidOperation):
            data = await self._info({"type": "l2Book", "coin": "BTC"})
            return int(_dec((data or {}).get("time")))

    def _load_universe(self, meta: Mapping[str, Any]) -> list[Instrument]:
        """meta 响应 → Instrument 列表，顺带重建 asset index 表和杠杆档位表。

        **enumerate 必须在过滤 delisted 之前做**：delisted 的币仍然留在 universe 里
        （带 isDelisted: true），下标因此保持稳定。反了就是全表 index 左移，
        下单会打到完全不相干的币上——而且不报任何错。

        三张表（_meta / _margin_tiers / excluded_symbols）**先在局部建好再整体替换**，
        不做增量更新：增量是只增不减的，一个币盘中被下架后旧条目会一直留在 _meta 里，
        fetch_carry_rates 的过滤判据正是 `name in _meta`——结果是死合约带着一个
        不再更新的费率继续挂在机会榜上，_meta_of 还能查到它的 index，下单要一路
        发到交易所才被拒。方法全程同步、没有 await，整体赋值是原子的。
        """
        universe = (meta or {}).get("universe") or []
        # marginTables 是 [[id, {marginTiers: [...]}], ...] 的配对数组
        new_tiers: dict[int, list[tuple[Decimal, Decimal]]] = {}
        for entry in (meta or {}).get("marginTables") or []:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            try:
                table_id = int(entry[0])
            except (TypeError, ValueError):
                continue
            tiers: list[tuple[Decimal, Decimal]] = []
            for tier in (entry[1] or {}).get("marginTiers") or []:
                tiers.append((_dec(tier.get("lowerBound")), _dec(tier.get("maxLeverage"), "1")))
            tiers.sort(key=lambda t: t[0])
            new_tiers[table_id] = tiers

        new_meta: dict[str, _AssetMeta] = {}
        new_excluded: set[str] = set()
        out: list[Instrument] = []
        for index, row in enumerate(universe):        # ← 先拿 index
            asset = _AssetMeta(index, row, self.k_multiplier)
            name = asset.name
            if not name or not SYMBOL_RE.match(name):
                # HIP-3 builder 市场是 {dex}:{coin}（带冒号），本适配器只做 dex=""
                continue
            if asset.delisted:
                continue
            if asset.multiplier != 1 and not self.include_k_prefixed:
                new_excluded.add(name)
                continue
            new_meta[name] = asset
            mult = asset.multiplier
            out.append(Instrument(
                market=self.market,
                symbol=name,                  # 上线用的原生币名（含 k），不做归一化
                # base 剥掉 k：倍数由 contract_size 承担，和币安 1000PEPE→PEPE 同口径。
                # public_feed.normalize_base 也会剥一次，两边一致才配得上对。
                base=asset.base,
                quote="USDC",                 # 保证金和计价都是 USDC，不是 USDT
                # 价按"每个 base 币"、量按"多少个 base 币"，所以一个除一个乘
                tick_size=asset.price_decimal_step / mult,   # 代理值，见 _AssetMeta
                lot_size=asset.native_lot * mult,
                min_notional=MIN_ORDER_NOTIONAL,
                contract_size=mult,           # 非 k 币恒为 1（线性合约，没有"张"）
                max_leverage=asset.max_leverage,
                funding_interval_s=FUNDING_INTERVAL_S,
            ))
        self._meta = new_meta
        self._margin_tiers = new_tiers
        self.excluded_symbols = new_excluded
        return out

    async def fetch_instruments(self) -> Sequence[Instrument]:
        """合约表。一次 meta 请求同时喂 Instrument、asset index 表和杠杆档位表。

        public_feed.fetch_instruments 把这个调用整个 try/except 吞掉：抛错 = 这家
        规格整片消失且零提示，所以宁可跳过坏行也别整体抛。
        """
        data = await self._info({"type": "meta", "dex": self.dex})
        return self._load_universe(data if isinstance(data, dict) else {})

    async def _ensure_meta(self) -> None:
        """asset index 表必须有。**UNVERIFIED**：文档没写下标会不会被新上币复用，
        所以**绝不把 index 持久化到磁盘或跨进程缓存**——只进程内缓存，每次启动重建，
        运行期靠 fetch_carry_rates 顺带刷新。
        """
        if self._meta:
            return
        async with self._meta_lock:
            if not self._meta:
                await self.fetch_instruments()

    async def _meta_of(self, symbol: str) -> _AssetMeta:
        await self._ensure_meta()
        asset = self._meta.get(symbol)
        if asset is None:
            raise ExchangeError(self.venue.value, "SPEC_MISSING",
                                f"universe 里没有 {symbol}（或它被当作千倍前缀币排除了）",
                                retriable=False)
        return asset

    async def fetch_fee_schedule(self, symbols: Sequence[str]) -> Sequence[FeeSchedule]:
        """账户实际费率档位（账户级一档吃全市场 → symbol 填空串 ""）。

        术语对照（最容易读错的一点）：**cross = taker（吃单），add = maker（挂单）**，
        别把 cross 误读成全仓保证金。``userCrossRate`` / ``userAddRate`` 才是这个账户的
        真实费率，``feeSchedule.cross/add`` 是挂牌基础档；**折扣已经算进去了**
        （0.00045 × (1−0.3) = 0.000315，0.3 是 HYPE 质押折扣），**不要再打一次折**
        ——低估成本比高估危险得多。符号约定正数 = 成本，直接填不用翻转（不像 OKX）。

        查不到就**不返回这一条**（返回空序列），绝不补 maker=0/taker=0 占位：零手续费
        会让四笔 taker 成本凭空消失、负期望的机会全部翻成正期望，而且不报任何错。
        少返回一条时上层会退回默认档并在界面标"按默认费率估算"。

        session.refresh_fees 调的是 fetch_fee_schedule(())，**symbols 为空必须仍能返回**。
        权重 20，必须缓存，绝不能进任何轮询循环。
        """
        if self._fee_cache is not None and time.time() - self._fee_cache[0] < FEE_CACHE_TTL_S:
            return list(self._fee_cache[1])
        try:
            user = self._user_address()
        except ExchangeError:
            # 没配地址（公开榜单模式）就查不到自己的档位 —— 返回空让上层退回默认档，
            # 而不是编一个零费率
            return []
        data = await self._info({"type": "userFees", "user": user})
        data = data if isinstance(data, dict) else {}
        taker_raw = data.get("userCrossRate")
        maker_raw = data.get("userAddRate")
        if taker_raw in (None, "") or maker_raw in (None, ""):
            # 空结果也要写缓存：userFees 权重 20，而 refresh_fees 每次调用都会到这
            # （session 侧的缓存在异常/空表时不建条目）。不缓存的话，一个响应形状
            # 变了的账户会让每一轮都白烧 20 权重去换同一个空答案
            self._fee_cache = (time.time(), [])
            return []
        staking = (data.get("activeStakingDiscount") or {}).get("discount")
        referral = data.get("activeReferralDiscount")
        notes = []
        if staking not in (None, "", "0", "0.0"):
            notes.append(f"HYPE 质押折扣 {staking}（已含在费率里）")
        if referral not in (None, "", "0", "0.0"):
            notes.append(f"推荐折扣 {referral}（已含在费率里）")
        out = [FeeSchedule(market=self.market, symbol="",
                           maker=_dec(maker_raw), taker=_dec(taker_raw),
                           note="；".join(notes))]
        self._fee_cache = (time.time(), out)
        return list(out)

    async def fetch_funding_limits(self, symbol: str) -> tuple[Decimal, Decimal] | None:
        """返回 (cap, floor)，顺序固定、cap >= floor。

        **不发任何请求**：文档明说 "The funding cap and funding interval do not depend
        on the asset"，全市场同一个 ±4%/小时。
        这两个值是**每周期**的夹子——HL 是 1 小时周期，同样数值年化后是 8h 合约的 8 倍，
        别在这里做归一化（归一化是 scoring 的事）。
        """
        return FUNDING_CAP, FUNDING_FLOOR

    # ---- 行情 -----------------------------------------------------------

    async def _l2_book(self, symbol: str) -> dict:
        """l2Book 全精度快照。

        **不传 nSigFigs**（默认就是全精度，等价于文档里的 null）：聚合过的簿会把
        带宽边界上的量算错，而算带内深度正需要原始档位。官方 SDK 的 l2_snapshot
        同样只发 {type, coin}，多发一个 null 键没有好处。
        """
        data = await self._info({"type": "l2Book", "coin": symbol})
        levels = (data or {}).get("levels")
        if not isinstance(levels, (list, tuple)) or len(levels) != 2:
            # 缺 levels 绝不能退化成深度 0：scoring 里 depth_cap<=0 会跳过容量判断，
            # 等于把"没数据"读成"深度无限"。
            raise ExchangeError(self.venue.value, "BAD_BOOK",
                                f"{symbol} l2Book 响应缺 levels", retriable=True)
        return data

    def _book_ts(self, data: Mapping[str, Any]) -> int:
        ts = int(_dec(data.get("time")))
        # 取不到就用本地时间兜底，**别填 0**——BookTop.is_fresh 会把 0 判成永远陈旧，
        # 收敛循环整条腿停摆。
        return ts or self.timestamp_ms()

    async def fetch_book_top(self, symbol: str) -> BookTop:
        """一档盘口。走 quota="market"，公开端点不签名。

        sz 是**原生币名的数量**（不是"张"，HL 没有张制）。绝大多数币 multiplier = 1
        直接就是 base 币；k 前缀币要价除量乘换成 base 口径——所以这里**必须**先拿到
        合约表（不像 Bitget 那样可以"没缓存就按 1 走"：那边系数恒为 1，这边猜错是 1000 倍）。
        """
        asset = await self._meta_of(symbol)
        data = await self._l2_book(symbol)
        bids, asks = data["levels"][0] or [], data["levels"][1] or []
        if not bids or not asks:
            # 单边空簿算不出中价，这一拍不可用。绝不返回 0 价。
            raise ExchangeError(self.venue.value, "EMPTY_BOOK",
                                f"{symbol} 盘口单边为空", retriable=True)
        mult = asset.multiplier
        return BookTop(
            bid_price=_dec(bids[0].get("px")) / mult,
            bid_qty=_dec(bids[0].get("sz")) * mult,
            ask_price=_dec(asks[0].get("px")) / mult,
            ask_qty=_dec(asks[0].get("sz")) * mult,
            ts_ms=self._book_ts(data),
        )

    @staticmethod
    def _band_side(levels: Sequence[Mapping[str, Any]], lo: Decimal, hi: Decimal,
                   descending: bool) -> tuple[Decimal, int]:
        """一侧盘口落在 [lo, hi]（**含边界**）内的累计名义额和档数。

        簿是排好序的（bids 降序、asks 升序），越过**远端**边界可以直接停——后面只会更远。
        越过**近端**边界（正常簿不会有，只有交叉簿/陈旧快照才会）只跳过这一档不能停：
        再往后可能还有落在带内的档位，break 会漏掉它们。

        价或量 <= 0 的脏档既不计名义额也不计 levels_used——否则"档位够不够覆盖带宽"
        这个判断会被灌水。

        全程走**原生**价与量：名义额 = px × sz 与倍数无关（价除量乘正好抵消），
        带宽是相对中价的比例、同样与量纲无关。只有对外报的那一档价/量要换算。
        """
        notional = ZERO
        used = 0
        for level in levels:
            price = _dec(level.get("px"))
            size = _dec(level.get("sz"))
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
            notional += price * size
            used += 1
        return notional, used

    async def fetch_book_depth(self, symbol: str, band_bp: float = 10.0) -> BookDepth:
        """band_bp 基点带宽内的可见深度（名义额 quote 计价，数量 base 币计价）。

        只用一档当深度代理会把容量系统性低估一两个数量级——山寨币一档常常只有几百刀，
        10bp 内却有几万。这个方法存在的全部理由就是修这个。

        限频：l2Book 权重 2（全部 info 里最便宜的一档），且和 fetch_book_top 打的是
        **同一个端点**——深度轮询直接吃一档轮询的额度，所以走 quota="market" 与行情共池，
        **绝不能挪去 risk/trade 抢平仓配额**。

        **UNVERIFIED**：单侧返回多少档文档没写（实测常见 20 档/侧），也没有 limit 参数
        可以要更多。档位不够时如实返回 levels_used，**绝不外推补足**——它等于交易所返回
        的总档数时，说明簿被端点档数上限截断、带宽可能没盖满。宁可低估。
        """
        if band_bp <= 0:
            # 带宽为零 ⇒ 深度恒为 0 ⇒ 上层把能做的机会判成不可执行。
            # 静默返回 0 比抛异常危险得多，而且**不发请求**。
            raise ValueError(f"band_bp 必须为正，收到 {band_bp!r}")
        asset = await self._meta_of(symbol)
        data = await self._l2_book(symbol)
        bids, asks = data["levels"][0] or [], data["levels"][1] or []
        if not bids or not asks:
            raise ExchangeError(self.venue.value, "EMPTY_BOOK",
                                f"{symbol} 盘口单边为空", retriable=True)

        mult = asset.multiplier
        best_bid = _dec(bids[0].get("px"))
        best_ask = _dec(asks[0].get("px"))
        mid = (best_bid + best_ask) / 2
        # band_bp 对外是 float（scoring 那边就是 float 口径），**只在这一处**经 str
        # 转成 Decimal，之后全程 Decimal —— Decimal(10.0) 会带一串二进制尾巴进价格比较
        band = Decimal(str(band_bp)) / Decimal("10000")
        lo, hi = mid * (1 - band), mid * (1 + band)

        bid_notional, bid_levels = self._band_side(bids, lo, mid, True)
        ask_notional, ask_levels = self._band_side(asks, mid, hi, False)
        return BookDepth(
            symbol=symbol,
            bid_notional=bid_notional,       # 名义额与量纲无关，不换算
            ask_notional=ask_notional,
            bid_price=best_bid / mult,
            bid_qty=_dec(bids[0].get("sz")) * mult,
            ask_price=best_ask / mult,
            ask_qty=_dec(asks[0].get("sz")) * mult,
            levels_used=bid_levels + ask_levels,
            band_bp=band_bp,
            ts_ms=self._book_ts(data),
        )

    def _next_settle_ms(self, now_ms: int) -> int:
        """下一个整点。metaAndAssetCtxs 响应里**没有 next_settle_ms**，自己算。

        （也可以用 predictedFundings 的 nextFundingTime，但那是额外一次权重-20 请求，
        为了一个能算出来的整点不值当。）
        """
        return (now_ms // FUNDING_INTERVAL_MS + 1) * FUNDING_INTERVAL_MS

    async def fetch_carry_rates(self, symbols: Sequence[str] | None = None
                                ) -> Sequence[CarryRate]:
        """当前资金费，一次拿全市场（symbols=None 是 public_feed 的默认调法）。

        符号用**交易所原始约定**：正 = 多头付给空头，**原样存，不取反**。取反会让
        scoring 的 ``short_rate - long_rate`` 全体反号，而错的方向恰好是"把亏钱的对
        推荐成赚钱的"。

        metaAndAssetCtxs 返回**两元素数组** [meta, ctxs]，``ctxs[i]`` 与 ``universe[i]``
        **按下标一一对应**：长度不等必须抛错，绝不能 zip 静默截断——截断后全表符号错位，
        BTC 的费率会挂到别的币上且不报任何错。顺带用响应里的 meta 刷新 asset index 表
        （同一次请求，白拿）。

        funding 常常正好是 "0.0000125"，那是溢价为 0 时的地板值（利率分量
        0.01%/8h = 0.00125%/h），不是脏数据。
        """
        data = await self._info({"type": "metaAndAssetCtxs", "dex": self.dex})
        if not isinstance(data, (list, tuple)) or len(data) != 2:
            raise ExchangeError(self.venue.value, "BAD_META",
                                "metaAndAssetCtxs 不是 [meta, ctxs] 两元素数组",
                                retriable=True)
        meta, ctxs = data[0] or {}, data[1] or []
        universe = meta.get("universe") or []
        if len(universe) != len(ctxs):
            raise ExchangeError(
                self.venue.value, "META_CTX_MISMATCH",
                f"universe({len(universe)}) 与 ctxs({len(ctxs)}) 长度不等，"
                "按下标对齐会让全表费率错位", retriable=True)
        self._load_universe(meta)          # 刷新 index / 杠杆档位 / 排除名单

        wanted = set(symbols) if symbols else None
        now_ms = self.timestamp_ms()
        next_settle = self._next_settle_ms(now_ms)
        out: list[CarryRate] = []
        for row, ctx in zip(universe, ctxs):
            name = str(row.get("name") or "")
            if name not in self._meta:     # 被 _load_universe 过滤掉的（delisted/HIP-3/k 前缀）
                continue
            if wanted is not None and name not in wanted:
                continue
            rate = (ctx or {}).get("funding")
            if rate in (None, ""):
                continue
            out.append(CarryRate(
                market=self.market,
                symbol=name,
                rate=_dec(rate),
                interval_s=FUNDING_INTERVAL_S,   # 每小时结算，写死 28800 会低估 8 倍
                next_settle_ms=next_settle,
                ts_ms=now_ms,
            ))
        return out

    async def fetch_funding_history(self, symbol: str, since_ms: int,
                                    limit: int = 1000
                                    ) -> Sequence[tuple[int, Decimal]]:
        """历史资金费，升序返回 [since_ms, 现在] 的**完整**区间。

        ``limit`` 的语义是**页大小**（基类契约原话："不是返回条数的上限……返回可以
        多于 limit"），而 HL 的页大小是服务端固定的 500，所以这个参数在这里
        **什么都不做**。这里曾经拿它当输出上限截尾（``out[-limit:]``）：HL 是唯一
        一家"1 小时结算 + 页上限 500"的组合，500 条只有 20.8 天，于是"计划持有
        1 月"要的 90 天窗口永远被斩到 20.8 天——稳定性评分从此困在 is_prior，
        且 venue_capped=False，界面上连一堵"墙"都不显示，没人知道少了什么。

        翻页方向是向"新"翻（和 Bybit 向旧翻相反），**每小时一条 → 500 条只有约 20.8 天**，
        30 天窗口必须翻至少两页，这不是可选优化。**startTime 是闭区间**，直接用末条时间
        当起点会把那条再收一遍，所以两道保险都上：起点 ``last_time + 1`` + ``dict`` 去重。
        重复值会抬高 autocorr()、拖偏 median，**而且不报任何错**，只是稳定性评分悄悄偏乐观。

        配额：这是全适配器最吃配额的端点——单币一次调用、没有批量模式、权重 20 + 每 20 条
        额外加权。200 个币 × 2 页 ≈ 18000 权重 ≈ 15 分钟的全部 IP 配额。**必须限流 +
        缓存 + 跨分钟摊开，绝不能进任何轮询循环**（history.DEFAULT_PACE_S 归主控接线）。
        """
        collected: dict[int, Decimal] = {}
        cursor = max(int(since_ms), 0)
        for _ in range(FUNDING_HISTORY_MAX_PAGES):
            rows = await self._info({
                "type": "fundingHistory",
                "coin": symbol,
                "startTime": cursor,
            }) or []
            if not rows:
                break
            # newest 必须从 0 起算而**不是**从 cursor 起算：拿 cursor 当初值的话
            # newest >= cursor 恒成立，下面那道"游标不前进"的闸就成了死代码——
            # 服务端一直回同一页陈旧数据时，每轮只把 cursor 推 1 毫秒，
            # 32 页硬闸得跑满才停（这是全适配器最贵的端点，权重 20 + 每 20 条加权，
            # 白烧 32 次 ≈ 1440 权重）。
            newest = 0
            for row in rows:
                ts = int(_dec(row.get("time")))
                rate = row.get("fundingRate")
                if ts <= 0 or rate in (None, ""):
                    continue
                collected[ts] = _dec(rate)
                newest = max(newest, ts)
            if len(rows) < FUNDING_HISTORY_PAGE:
                break                      # 不满一页 = 已经翻到最新那端
            if newest + 1 <= cursor:
                break                      # 整页都不比游标新 = 不前进，再翻就是死循环
            cursor = newest + 1            # 闭区间 → +1，否则末条会被再收一遍
        # 不按 limit 截：见 docstring。gate/binance 也是整段返回
        return sorted(collected.items())

    # ---- WebSocket ------------------------------------------------------

    def stream_book_top(self, symbols: Sequence[str]) -> AsyncIterator[BookTop]:
        """bbo 盘口流。普通 def 返回异步生成器，调用方不 await 也能拿到迭代器。"""
        return self._book_stream(tuple(symbols))

    async def _book_stream(self, symbols: Sequence[str]) -> AsyncIterator[BookTop]:
        """最小可用版：订阅 + 心跳 + 退避重连。

        **每个 coin 一条 subscription，没有批量订阅数组的写法**（和 Gate 的原子批订阅
        不同）——好处是一个不认识的币只毁掉它自己那条订阅，不会让整批哑掉。

        心跳 **UNVERIFIED**：官方文档没写 ping/pong 规格和超时时长，只说"自行处理断线
        并优雅重连"，且断线"可能不预告地周期性发生"。这里发 ``{"method":"ping"}`` 并忽略
        ``{"channel":"pong"}``，同时用无数据超时兜底——即使 ping 的写法是错的，
        超时重连仍然能把流拉回来。

        TODO：(1) 全簿增量（l2Book 频道）没接，只有 bbo 一档；(2) 1000 条订阅上限之外的
        分片、多连接（单 IP 上限 10 条）没做；(3) BookTop 里**没有 symbol 字段**，
        多符号订阅时下游分不清来源——实际使用建议**一个符号一条流**。
        """
        import websockets              # 延迟导入：只跑 REST 的场景不该被它拖着

        valid = [s for s in symbols if SYMBOL_RE.match(s)][:WS_MAX_SUBSCRIPTIONS]
        if not valid:
            return
        # 先把合约表拿到手：k 前缀币的推送要按 multiplier 换算成 base 口径，
        # 缺表时 _parse_bbo 会**整条丢弃**而不是按 1 猜（猜错是 1000 倍）
        await self._ensure_meta()
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.ws_base, ping_interval=None,
                                              max_queue=2048) as ws:
                    for coin in valid:
                        await ws.send(json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "bbo", "coin": coin},
                        }))
                    backoff = 1.0
                    pinger = asyncio.create_task(self._ws_heartbeat(ws))
                    try:
                        while True:
                            raw = await asyncio.wait_for(ws.recv(),
                                                         timeout=WS_IDLE_TIMEOUT_S)
                            if isinstance(raw, bytes):
                                raw = raw.decode()
                            msg = json.loads(raw, parse_float=Decimal)
                            if not isinstance(msg, dict):
                                continue
                            channel = msg.get("channel")
                            if channel in ("pong", "subscriptionResponse", "error"):
                                continue
                            # 推送信封是 {"channel": "bbo", "data": {...}}；
                            # 万一某天直接推裸 data，下面这行也兜得住
                            payload = msg.get("data") if channel else msg
                            top = self._parse_bbo(payload)
                            if top is not None:
                                yield top
                    finally:
                        pinger.cancel()
            except asyncio.CancelledError:
                raise                  # 必须原样 raise，否则任务取消不掉
            except Exception:
                # 重连带抖动：一堆连接同时掉线时不要踩着同一秒集体回来
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, 30.0)

    async def _ws_heartbeat(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(WS_PING_INTERVAL_S)
            await ws.send(json.dumps({"method": "ping"}))

    def _parse_bbo(self, payload: Any) -> BookTop | None:
        """bbo 推送 → BookTop（已换算成 base 币口径）。

        ``bbo`` 是长度 2 的数组，**任一侧可能是 null**（单边空簿），直接下标解引用会炸。
        这一拍没有双边就丢掉，不返回 0 价。合约表里没有这个 coin 时同样丢掉——
        宁可少一拍行情，也不能按倍数 1 猜一个 k 币的价。
        """
        if not isinstance(payload, dict):
            return None
        asset = self._meta.get(str(payload.get("coin") or ""))
        bbo = payload.get("bbo")
        if asset is None or not isinstance(bbo, (list, tuple)) or len(bbo) != 2:
            return None
        bid, ask = bbo[0], bbo[1]
        if not isinstance(bid, dict) or not isinstance(ask, dict):
            return None
        mult = asset.multiplier
        return BookTop(
            bid_price=_dec(bid.get("px")) / mult,
            bid_qty=_dec(bid.get("sz")) * mult,
            ask_price=_dec(ask.get("px")) / mult,
            ask_qty=_dec(ask.get("sz")) * mult,
            ts_ms=int(_dec(payload.get("time"))) or self.timestamp_ms(),
        )

    # ---- 精度 -----------------------------------------------------------

    @staticmethod
    def round_size(qty: Decimal, sz_decimals: int) -> Decimal:
        """数量按 10^-szDecimals **向零取整**（宁可少下也不能超目标）。"""
        step = Decimal(1).scaleb(-sz_decimals)
        out = (abs(qty) / step).to_integral_value(rounding=ROUND_DOWN) * step
        out = out.quantize(step)
        return out if qty >= 0 else -out

    @staticmethod
    def round_price(price: Decimal, sz_decimals: int, side: str) -> Decimal:
        """价格规整：**5 位有效数字**与**小数位 <= 6 − szDecimals**两条规则的交集。

        必须先算有效数字步长、再和小数位步长取更粗的那个——反过来（先夹小数位再削
        有效数字）会在夹完之后又超出有效数字。只做 quantize(tick) 会在 1234.56 这种价上
        被 "Tick: Price must be divisible by tick size." 拒。**整数价永远合法**，
        不受 5 位有效数字限制（123456 合法），直接放行。

        方向：**买单向上、卖单向下**，与 executor.cross_price 和其它六家一致。
        （调研文档那节写的是"买单向下更保守"，那是站在"少花钱"的角度；本项目的保护限价
        是**故意穿透对手价**的，反向取整会把穿透削回来——在 tick 相对价格很粗的小币上
        那不是少赚一个 tick，而是 IOC 根本挂不上、这条腿永远补不平。）
        """
        if price <= 0:
            return price
        if price == price.to_integral_value():
            return price.to_integral_value()
        # adjusted() 是最高位的 10 次幂，减 4 就是第 5 位有效数字所在的量级
        sig_step = Decimal(1).scaleb(price.adjusted() - (PRICE_SIG_FIGS - 1))
        dec_step = Decimal(1).scaleb(-(PERP_MAX_DECIMALS - sz_decimals))
        step = max(sig_step, dec_step)     # 两条规则取更粗的那个
        rounding = ROUND_CEILING if side == "buy" else ROUND_FLOOR
        out = (price / step).to_integral_value(rounding=rounding) * step
        return out.quantize(step)

    @staticmethod
    def to_cloid(client_order_id: str) -> str:
        """client_order_id → cloid（"0x" + 32 个十六进制字符，128 bit）。

        SDK 的校验只有两条：0x 前缀、去前缀后长度正好 32。本项目的 client_order_id 是
        任意字符串，所以做一次**确定性**映射——确定性是关键，cancel_order / query_order
        要能从同一个字符串再算出同一个 cloid。

        **UNVERIFIED**：重复 cloid 会不会被服务端拒绝（从而提供天然幂等）、唯一性窗口
        多长，官方文档**完全没说**。所以绝不能把它当防重放护身符：/exchange 超时必须抛
        OrderUnknown 走 cancelByCloid → orderStatus 的标准消解流程（该流程在"服务端去重"
        和"不去重"两种行为下都正确）。
        """
        if not client_order_id:
            raise ValueError("hyperliquid 下单必须带 client_order_id")
        return "0x" + hashlib.sha256(client_order_id.encode()).hexdigest()[:32]

    def _remember_cloid(self, cloid: str, client_order_id: str) -> None:
        if len(self._cloid_to_client) > 4096:
            self._cloid_to_client.pop(next(iter(self._cloid_to_client)), None)
        self._cloid_to_client[cloid] = client_order_id

    # ---- 交易 -----------------------------------------------------------

    async def _protective_price(self, symbol: str, side: str) -> Decimal:
        """price is None 时的保护限价。

        Hyperliquid **没有市价单类型**（"market" 是前端行为），必须自己从盘口算一个
        足够穿透的限价配 Ioc。**不能发空价**——那会被整批预校验拒掉。
        """
        top = await self.fetch_book_top(symbol)
        slip = MARKET_PROTECTION_BP / Decimal("10000")
        return top.ask_price * (1 + slip) if side == "buy" else top.bid_price * (1 - slip)

    async def place_order(self, req: OrderRequest) -> OrderResult:
        """下单。全仓最危险的一个方法。

        判定标准一句话：**只有能证明"没进撮合"时才允许说"失败"，证不了就是未知。**
        nonce 不是去重键，重发一定会变成第二张单。

        **不注入任何返佣码**：没有 builder 键、没有渠道 header、cloid 不加前缀。
        """
        self._require_signing()
        if not req.client_order_id:
            # 空串就地抛，且**不发请求**
            raise ValueError("hyperliquid 下单必须带 client_order_id（消解未知状态要靠它）")

        asset = await self._meta_of(req.symbol)
        mult = asset.multiplier
        # 入参是 base 币口径（k 币的 1000 倍在这里除回原生单位），再按原生粒度向零取整
        qty = self.round_size(abs(req.qty) / mult, asset.sz_decimals)
        if qty <= 0:
            # 取整后归零：这单本来就不该发。本地拒掉，省一次网络往返和一次 action 配额
            # （地址维度 action 配额初始只有 10000 次，烧不起）。
            raise OrderRejected(self.venue.value, "PINCHED",
                                f"{req.symbol} {req.qty} 按 lot {asset.native_lot * mult}"
                                " 取整后为零", retriable=False)

        price = req.price
        if price is None:
            price = await self._protective_price(req.symbol, req.side)
        # 价同样先回到原生量纲（每 kPEPE），HL 的 5 位有效数字/小数位规则是对原生价说的
        price = self.round_price(price * mult, asset.sz_decimals, req.side)
        if price <= 0:
            raise OrderRejected(self.venue.value, "BAD_PRICE",
                                f"{req.symbol} 规整后价格非正：{price}", retriable=False)
        notional = price * qty
        if notional < MIN_ORDER_NOTIONAL and not req.reduce_only:
            # 本地先拦一道，别拿一次 RTT + 一次 action 配额去换一个 MinTradeNtl。
            # reduce_only 放行是因为 **UNVERIFIED**：$10 下限对平仓单是否豁免文档没写，
            # 而拦掉平仓单的后果（残仓平不掉）比白跑一次请求严重得多。
            raise OrderRejected(self.venue.value, "MIN_NOTIONAL",
                                f"{req.symbol} 名义额 {notional} < ${MIN_ORDER_NOTIONAL}",
                                retriable=False)

        # tif：Alo(post-only) / Ioc / Gtc。req.price is None 走的是"市价"语义，
        # 必须是 Ioc（HL 没有 FOK，全成或全撤在这里做不到，只能靠保护价+Ioc 逼近）。
        tif = {"IOC": "Ioc", "FOK": "Ioc", "GTC": "Gtc",
               "GTX": "Alo", "POST_ONLY": "Alo", "MAKER": "Alo"}.get(
            (req.time_in_force or "IOC").upper(), "Ioc")
        if req.price is None:
            tif = "Ioc"

        cloid = self.to_cloid(req.client_order_id)
        self._remember_cloid(cloid, req.client_order_id)

        # 【键顺序就是签名】order wire = a → b → p → s → r → t → c，
        # action = type → orders → grouping。字面量 dict 构造，绝不用推导式/**kwargs。
        # **绝不出现 builder 键**（那是返佣机制，用户明确不要；一旦出现 msgpack 字节
        # 就变了，签名和已有测试全部失效）。
        order_wire: dict[str, Any] = {
            "a": asset.index,               # asset index 是**整数下标**，不是币名
            "b": req.side == "buy",
            "p": _wire(price),
            "s": _wire(qty),
            "r": bool(req.reduce_only),
            "t": {"limit": {"tif": tif}},
            "c": cloid,
        }
        action: dict[str, Any] = {
            "type": "order",
            "orders": [order_wire],
            "grouping": "na",
        }

        try:
            data = await self._request("POST", EXCHANGE_PATH, quota="trade",
                                       signed=True, body=action)
        except OrderRejected:
            raise                       # 明确拒绝：订单确定不存在
        except RateLimited:
            raise                       # 限频 = 确定没进撮合，可安全重试
        except ExchangeError as exc:
            if exc.code in _PRE_MATCH_CODES:
                # 签名/nonce/422：**进撮合之前**就被挡下，订单确定不存在。
                # 升级成 OrderUnknown 只会白跑一轮 cancel+查单，浪费 action 配额。
                raise
            # NETWORK / BAD_JSON / 5xx 都在这条路上：订单**可能已经进了撮合**，
            # 当成失败返回会让上层重发 → 双倍仓位。
            # 认不出的错误码也倒向这里：多跑一轮 cancel+查单只花一次 RTT，
            # 误判成"可安全重发"的 OrderRejected 是双倍仓位。
            raise OrderUnknown(self.venue.value, req.client_order_id) from exc
        except httpx.TransportError as exc:
            # TransportError 覆盖 Timeout / Network / Protocol / Proxy 全族，
            # 六家统一用它（逐个列就会漏，漏一个就是一次裸奔的双倍仓位）
            raise OrderUnknown(self.venue.value, req.client_order_id) from exc

        return self._parse_place_result(data, req, qty, mult)

    def _parse_place_result(self, data: Any, req: OrderRequest,
                            qty: Decimal, mult: Decimal) -> OrderResult:
        """下单响应 → OrderResult。

        **``{"error": ...}`` 藏在 status:"ok" 的响应里**：只看 HTTP 状态码或只看外层
        status 会把"订单被拒"当成"下单成功"，然后引擎按一个不存在的仓位继续加仓。

        读不到 statuses 数组的 200 响应 → **OrderUnknown**（结构不认识 = 不知道有没有
        下进去），不能当成失败。
        """
        statuses = None
        if isinstance(data, dict):
            statuses = ((data.get("data") or {}).get("statuses")
                        if isinstance(data.get("data"), dict) else None)
        if not isinstance(statuses, (list, tuple)) or not statuses:
            raise OrderUnknown(self.venue.value, req.client_order_id)

        st = statuses[0]
        if isinstance(st, dict) and "error" in st:
            # IocCancel（"Order could not immediately match..."）也走这里：那是
            # **正常结果不是故障**，在 500ms 收敛循环里会频繁出现，上层当"这一拍没成交"处理
            raise self._translate(str(st.get("error") or ""), 200)
        if isinstance(st, dict) and "filled" in st:
            fill = st["filled"] or {}
            filled = _dec(fill.get("totalSz"))          # 原生量，和 qty 同量纲
            # IOC 部成之后订单就终结了，用 canceled 表达并带上已成量；
            # 报成 new 会让上层以为还挂着，一直等一个永远不会来的成交
            status = "filled" if filled >= qty else "canceled"
            return OrderResult(
                client_order_id=req.client_order_id,   # 必须回传调用方的原值
                exchange_order_id=str(fill.get("oid") or ""),
                status=status,
                filled_qty=filled * mult,              # 对外一律 base 币
                avg_price=_dec(fill.get("avgPx")) / mult,
                # filled 分支**不带手续费**，要精确值得另查 userFillsByTime。
                # 这里留 0 而不是猜一个数。
                fee=ZERO,
                fee_asset="USDC",
            )
        if isinstance(st, dict) and "resting" in st:
            return OrderResult(
                client_order_id=req.client_order_id,
                exchange_order_id=str((st["resting"] or {}).get("oid") or ""),
                status="new",
                filled_qty=ZERO,
                avg_price=ZERO,
                fee=ZERO,
                fee_asset="USDC",
            )
        # 认不出的 status 结构（waitingForFill / waitingForTrigger 之类）：
        # 不知道它有没有进撮合，往未知侧倒。
        raise OrderUnknown(self.venue.value, req.client_order_id)

    async def cancel_order(self, symbol: str, client_order_id: str) -> None:
        """按 cloid 撤单。这是**状态终结操作**——执行后订单只可能是已撤或已成，
        所以它是消解 OrderUnknown 的第一步。

        注意 cancelByCloid 的键名是全名 ``asset`` / ``cloid``，而按 oid 撤的 cancel
        用的是缩写 ``a`` / ``o``——**两者故意不一样**，这是官方 SDK 的实际写法
        （bulk_cancel / bulk_cancel_by_cloid），照抄，别自作主张统一：键名变了
        msgpack 字节就变了，签名跟着失效。

        **UNVERIFIED**：gitbook 的 cancel 示例里多了一个 ``"f": false``（快速撤单标记），
        另一处又说它在 cancel 条目里且 false 时省略。官方 SDK 两处都不发，按 SDK 来。
        """
        self._require_signing()
        asset = await self._meta_of(symbol)
        cloid = self.to_cloid(client_order_id)
        action = {
            "type": "cancelByCloid",
            "cancels": [{"asset": asset.index, "cloid": cloid}],
        }
        try:
            data = await self._request("POST", EXCHANGE_PATH, quota="risk",
                                       signed=True, body=action)
        except OrderRejected:
            # MissingOrder 走 _translate 变成 OrderRejected —— 已终结，撤单的目的
            # 已经达到，吞掉让消解流程继续去查成交量。抛出去会让 resolver 白跑一轮。
            return
        except ExchangeError as exc:
            raise self._risk_path_error(exc) from exc

        statuses = ((data or {}).get("data") or {}).get("statuses") \
            if isinstance(data, dict) else None
        for st in statuses or []:
            # **类型不统一**：成功是裸字符串 "success"，失败是对象 {"error": ...}。
            # 直接写 st["error"] 会在成功路径上对 str 做下标索引，拿到一个字符而不是报错。
            if isinstance(st, str):
                continue
            if isinstance(st, dict) and st.get("error"):
                err = self._translate(str(st["error"]), 200)
                if isinstance(err, OrderRejected):
                    return              # 已终结/从未下单：目的已达到
                raise self._risk_path_error(err)

    async def query_order(self, symbol: str, client_order_id: str) -> OrderResult:
        """按 cloid 查单。cloid 直接塞进 ``oid`` 这个键（官方 SDK 的 query_order_by_cloid
        就是这么发的，请求里不存在 cloid 键）。

        **外层 status != "order" 一律当"订单不存在" → OrderRejected**：
        resolve_unknown_order 靠它判定"这单从未被接受"并返回 Decimal("0")。
        （**UNVERIFIED**：订单不存在时外层 status 的确切取值，推测 "unknownOid"。）
        内层 status 用白名单映射，**未知值一律当作非终结的 new**。

        orderStatus **不返回成交均价和手续费**：filled_qty = origSz − sz 能算，
        avg_price / fee 算不出来，必须另查 userFillsByTime 按 oid 聚合（权重 20，
        所以做成可关的 best-effort，绝不因为它失败而让消解流程崩掉）。
        """
        user = self._user_address()
        mult = (await self._meta_of(symbol)).multiplier
        cloid = self.to_cloid(client_order_id)
        try:
            data = await self._info({"type": "orderStatus", "user": user, "oid": cloid},
                                    quota="risk")
        except ExchangeError as exc:
            raise self._risk_path_error(exc) from exc
        if not isinstance(data, dict) or data.get("status") != "order":
            raise OrderRejected(self.venue.value, "MISSING_ORDER",
                                f"{symbol} 查无此单 {client_order_id}", retriable=False)

        wrapper = data.get("order") or {}
        order = wrapper.get("order") or {}
        inner = str(wrapper.get("status") or "")
        origin = _dec(order.get("origSz"))
        left = _dec(order.get("sz"))
        filled = origin - left
        if filled < 0:
            filled = ZERO
        # 未知内层状态兜底成 new（非终结），有成交量就升成 partial——同样是非终结态，
        # 上层会继续跟；把它当 canceled 才是那条通往双倍仓位的路。
        status = _ORDER_STATUS.get(inner, "new")
        if status == "new" and filled > 0:
            status = "partial"
        oid = str(order.get("oid") or "")

        avg_price, fee = ZERO, ZERO
        if self.fetch_fill_prices and filled > 0 and oid:
            avg_price, fee = await self._fill_summary(oid, int(_dec(order.get("timestamp"))))
        return OrderResult(
            client_order_id=client_order_id,     # 回传调用方原值，不是 cloid
            exchange_order_id=oid,
            status=status,
            filled_qty=filled * mult,            # 对外一律 base 币
            avg_price=avg_price / mult if avg_price else ZERO,
            fee=fee,
            fee_asset="USDC",
        )

    async def _fill_summary(self, oid: str, since_ms: int) -> tuple[Decimal, Decimal]:
        """按 oid 聚合成交：avg = Σ(px*sz)/Σsz，fee = Σfee。

        best-effort：这是查单路径上的第二次请求（权重 20 + 每 20 条加权），
        失败绝不能影响 filled_qty 这个真正重要的数——所以整段吞异常。
        fee 单位是 USDC（feeToken），**正数 = 你付**。
        **UNVERIFIED**：做市返佣时 fee 是否为负数，文档没有示例。
        """
        try:
            start = max(int(since_ms) - 60_000, 0) if since_ms else 0
            rows = await self._info({
                "type": "userFillsByTime",
                "user": self._user_address(),
                "startTime": start,
                "endTime": self.timestamp_ms(),
                "aggregateByTime": False,
            }, quota="risk") or []
        except Exception:
            return ZERO, ZERO
        total_sz, total_quote, fee = ZERO, ZERO, ZERO
        for row in rows:
            if str(row.get("oid") or "") != oid:
                continue
            sz = _dec(row.get("sz"))
            total_sz += sz
            total_quote += sz * _dec(row.get("px"))
            fee += _dec(row.get("fee"))
        if total_sz <= 0:
            return ZERO, ZERO
        return total_quote / total_sz, fee

    async def fetch_positions(self, symbols: Sequence[str] | None = None
                              ) -> Sequence[Position]:
        """持仓。**这是真值**——任何时候本地记账与它冲突，无条件以它为准。

        ``user`` 必须是**账户地址不是 agent 地址**：填错返回的是一个合法的空账户，
        不报错，仓位读成 0 → 引擎判定裸奔 → 触发撤退平仓（所以 _user_address() 宁可
        抛错也不返回空）。``assetPositions`` 里**只有非零仓位**，平掉的币不出现——
        某条腿消失 = 已平不是错误；传了 symbols 时给未出现的符号**补零仓位**，
        只返回 API 给的那几条会让上层把"已平"读成"读不到"。

        权重 2，可以放进风控轮询；走 quota="risk"（session.validate_account 拿它做
        连通性与读权限自检，所以零仓账户必须能正常返回）。
        """
        user = self._user_address()
        # 仓位要按 multiplier 换算成 base 币，所以合约表必须先在手上
        await self._ensure_meta()
        data = await self._info({"type": "clearinghouseState", "user": user,
                                 "dex": self.dex}, quota="risk")
        data = data if isinstance(data, dict) else {}
        wanted = list(symbols) if symbols else None
        seen: set[str] = set()
        out: list[Position] = []
        for entry in data.get("assetPositions") or []:
            pos = (entry or {}).get("position") or {}
            coin = str(pos.get("coin") or "")
            if not coin:
                continue
            if wanted is not None and coin not in wanted:
                continue
            seen.add(coin)
            asset = self._meta.get(coin)
            # 不在 _meta 里的币不许猜 mult=1：k 前缀币的真实倍数是 1000，
            # 而 include_k_prefixed=False（默认）时它们恰好都不进 _meta——
            # 最需要这个开关的用户（对 k 币倍数没把握的那批）翻开持仓看到的
            # 就是错 1000 倍的数。倍数规则和 _AssetMeta.__init__ 保持同源
            mult = (asset.multiplier if asset is not None
                    else self.k_multiplier if K_PREFIX_RE.match(coin)
                    else Decimal("1"))
            native = _dec(pos.get("szi"))       # 有符号：正=多、负=空，原生单位
            liq = pos.get("liquidationPx")
            liq_dec = _dec(liq) if liq not in (None, "") else ZERO
            # 响应里**没有 markPx**。positionValue 是 HL 自己按 mark 算的名义额，
            # 除以 |szi| 就等价于 mark，省一次 allMids 请求（这里算的是原生价，
            # 和别的价一样再除 multiplier 换成"每 base 币"）。
            mark = ZERO
            if native != 0:
                mark = _dec(pos.get("positionValue")) / abs(native) / mult
            out.append(Position(
                market=self.market,
                symbol=coin,
                qty=native * mult,              # 对外一律 base 币，有符号
                entry_price=_dec(pos.get("entryPx")) / mult,
                mark_price=mark,
                # 拿到 0 / 空 / null 一律填 None：填 0 会让 liquidation_distance
                # 算出"还有 100% 空间"，风控直接失明
                liquidation_price=liq_dec / mult if liq_dec > 0 else None,
                leverage=_dec((pos.get("leverage") or {}).get("value"), "1"),
            ))
        for sym in wanted or []:
            if sym not in seen:
                # 补零仓：调用方按 symbol 取，缺一条会被当成"读不到"而不是"已平"
                out.append(Position(market=self.market, symbol=sym, qty=ZERO,
                                    entry_price=ZERO, mark_price=ZERO,
                                    liquidation_price=None, leverage=ZERO))
        return out

    async def fetch_open_orders(self, symbol: str) -> Sequence[OrderResult]:
        """未成交挂单。判断两腿是否平衡**必须计入这些**——"A 已成交、B 还挂着"
        是限价模式下最容易出现的隐形单腿。

        **必须用 frontendOpenOrders 而不是 openOrders**：后者只有
        {coin, limitPx, oid, side, sz, timestamp}，**没有 cloid**，而本项目一切按
        client_order_id 索引，拿不到 cloid 的挂单等于查不到；两者权重都是 20。

        cloid → client_order_id 的反查靠进程内映射（下单时记的），**重启即失忆**，
        映射不上时回传原始 cloid——对账的权威口径始终是自己落库的成交记录。
        """
        user = self._user_address()
        mult = (await self._meta_of(symbol)).multiplier
        rows = await self._info({"type": "frontendOpenOrders", "user": user,
                                 "dex": self.dex}, quota="risk") or []
        out: list[OrderResult] = []
        for row in rows:
            if str(row.get("coin") or "") != symbol:
                continue
            origin = _dec(row.get("origSz"))
            left = _dec(row.get("sz"))          # sz 是**剩余**未成交量
            filled = origin - left
            cloid = str(row.get("cloid") or "")
            out.append(OrderResult(
                client_order_id=self._cloid_to_client.get(cloid, cloid),
                exchange_order_id=str(row.get("oid") or ""),
                status="partial" if filled > 0 else "new",
                filled_qty=filled * mult if filled > 0 else ZERO,
                avg_price=ZERO,                 # 挂单端点不给成交均价，不猜
                fee=ZERO,
                fee_asset="USDC",
            ))
        return out

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        """改杠杆。键顺序 type → asset → isCross → leverage。

        ``leverage`` 是**整数**（不是字符串、不是小数）。非整数入参**拒绝而不是静默
        取整**——静默取整会让"我设了 3.5 倍"和"实际 3 倍"悄悄分叉，风控算的距离全错。

        走 quota="risk"：降杠杆是风控动作（safety 会触发），不能被行情轮询挤掉。

        TODO：逐仓（isCross=False）本适配器只按构造参数 cross_margin 走，
        没有按 symbol 分别设置的能力；onlyIsolated 的币（universe 里带该标记）
        在全仓模式下会被交易所拒，目前依赖交易所报错而不是本地预判。
        """
        self._require_signing()
        if leverage != leverage.to_integral_value():
            raise ValueError(f"hyperliquid 的杠杆必须是整数，收到 {leverage}")
        asset = await self._meta_of(symbol)
        action = {
            "type": "updateLeverage",
            "asset": asset.index,
            "isCross": bool(self.cross_margin),
            "leverage": int(leverage),
        }
        await self._request("POST", EXCHANGE_PATH, quota="risk", signed=True, body=action)

    async def fetch_leverage_tiers(self, symbol: str
                                   ) -> Sequence[tuple[Decimal, Decimal]]:
        """杠杆档位 [(名义上限, 最大杠杆), ...] **升序**。

        档位就在 meta 响应里：universe[i].marginTableId → marginTables 那张表的
        marginTiers（{lowerBound, maxLeverage}，lowerBound 的口径是 **Notional Position
        Value (USDC)** 不是账户净值）。基类要"上限"而 HL 给"下限"，所以做一次**错位
        换算**：tiers[i] 的上限 = tiers[i+1].lowerBound，最后一档填 Infinity 哨兵。

        查不到时退化成单档 [(Infinity, max_leverage)]，**不返回空列表**——空列表和
        "没有档位限制"在上层是两种完全不同的含义。退化后避档逻辑等于失效（贴着边界开仓，
        价格一动就跨档触发降杠杆和补保证金），上线前必须确认真实分档拿得到。
        """
        asset = await self._meta_of(symbol)
        tiers = self._margin_tiers.get(asset.margin_table_id or -1) or []
        out: list[tuple[Decimal, Decimal]] = []
        for i, (_lower, max_lev) in enumerate(tiers):
            upper = tiers[i + 1][0] if i + 1 < len(tiers) else Decimal("Infinity")
            if max_lev > 0:
                out.append((upper, max_lev))
        if not out:
            return [(Decimal("Infinity"), asset.max_leverage)]
        return out

    # ---- 地址维度配额自监控 ---------------------------------------------

    async def fetch_action_quota(self) -> dict[str, Any]:
        """地址维度的 action 配额。**这不是基类方法，但必须做。**

        地址维度限频**只对 action 生效**（info 不受限）：累计成交 1 USDC 换 1 次 action，
        初始白送 10000 次，**触顶后降为每 10 秒 1 次**——那等于失去及时平仓能力，
        是安全问题不是性能问题（撤单另有更宽额度 min(limit+100000, limit*2)）。
        低频对冲最容易踩这个坑：500ms 收敛循环每次调整都要 place + cancel，几天就能烧完。
        接近上限时上层应当主动降频甚至进 HedgeStatus.REDUCE_ONLY。走 quota="risk"。
        """
        data = await self._info({"type": "userRateLimit", "user": self._user_address()},
                                quota="risk")
        self.action_quota = data if isinstance(data, dict) else None
        return self.action_quota or {}
