"""交易所适配器基类。

每家交易所的签名、精度、符号命名、返佣码带法都不同（调研结论：币安走订单ID前缀、
Bybit 走 X-Referer header、OKX 走 body 的 tag 字段、Gate/Bitget 各有自己的 header、
HTX 另一套），差异全部收敛在子类里，引擎只看这里定义的接口。
"""

from __future__ import annotations

import abc
import asyncio
import hashlib
import hmac
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, AsyncIterator, Callable, Mapping, Sequence

import httpx

from ..models import (
    BookDepth,
    BookTop,
    CarryRate,
    FeeSchedule,
    Instrument,
    Position,
    TradeFill,
    Venue,
)

# 所有交易所都要求本地时钟与服务器对齐（币安 recvWindow 默认 5s，
# 允许最多领先 1s、落后 recvWindow）。启动时校准，之后周期性刷新。
CLOCK_RESYNC_INTERVAL_S = 300

# ---- 外部喂数缓存（应对服务端被交易所拒绝）--------------------------------
#
# 有些交易所按 IP 拒绝我们这台服务器（币安 451 地域封锁、Bybit 403），服务端
# 一条数据都拉不到。这时由**访客的浏览器**把公开行情拉回来（他自己的网络没被封）
# 喂进这里，适配器的取数路径直接读缓存、一个字节都不发出去。
#
# 反过来也有交易所不给浏览器跨域（CORS 拒绝），那几家仍旧由服务端直连——
# 两边正好互补，见 ui/public_feed_client.BROWSER_FETCHABLE 的实测矩阵。
#
# 三条铁律：
#   1. **只服务公开数据**。signed=True 永不读缓存——私有接口要密钥，
#      而缓存里的东西来自"某一个访客"，串号就是把 A 的持仓给 B 看。
#   2. **键里必须带 venue**。九家的路径撞车不是假设：/api/v1/markets 这种
#      通用路径谁都可能有，不带 venue 就是拿 Backpack 的合约表喂 KuCoin。
#   3. **按品种查询的请求不进缓存**。缓存的是「全市场」那一发，
#      带 symbol 的请求各查各的，误命中会让所有品种拿到同一个答案。
_PUBLIC_CACHE: dict[str, tuple[float, Any]] = {}

# 120 秒：机会榜一轮刷新实测约 105 秒，TTL 要盖得住一整轮，
# 否则同一轮里前半段读缓存、后半段回落到必然失败的 HTTP。
PUBLIC_CACHE_TTL_S = 120.0

# 出现任意一个就判定"这是按品种查的"，直接不缓存。宁可漏缓存也不能误命中：
# 漏了只是多发一个请求，误命中是把 BTC 的答案发给全市场且不报任何错。
# （九家的品种参数名：币安/Bybit/Bitget symbol、OKX instId、Gate contract、
#  HTX contract_code、HL coin、KuCoin symbol、Backpack symbol；
#  cursor/pageNo 这类翻页参数同样排除——只缓存第一页没有意义还容易错位。）
_PER_SYMBOL_KEYS = frozenset({
    "symbol", "symbols", "instid", "contract", "contract_code", "coin",
    "coins", "pair", "currency", "ccy", "cursor", "pageno", "page_index",
    "fromid", "starttime", "endtime", "from", "to", "after", "before",
})


def public_cache_key(method: str, path: str,
                     params: Mapping[str, Any] | None = None,
                     body: Mapping[str, Any] | None = None) -> str | None:
    """请求 → 缓存键；返回 None 表示**这个请求不该走缓存**。

    键要把 params/body 一起编进去（不只是 method+path）：Bybit 的
    instruments-info 靠 category 区分 linear/inverse，Bitget 靠 productType 区分
    U 本位/币本位，Hyperliquid 更是所有请求都打同一个 POST /info、
    全靠 body 的 type 区分。只用路径当键，这三家会互相覆盖。
    """
    merged = {**(params or {}), **(body or {})}
    if any(str(k).lower() in _PER_SYMBOL_KEYS for k in merged):
        return None
    key = f"{method.upper()} {path}"
    if merged:
        # 排序 + str()：调用方传 1000 还是 "1000" 都要落到同一个键，
        # 否则喂数侧和取数侧各写各的键，永远不命中
        key += "?" + "&".join(f"{k}={merged[k]}" for k in sorted(merged, key=str))
    return key


def feed_public_cache(venue: str, key: str, payload: Any) -> None:
    """喂一份公开数据。payload 必须是**适配器 _parse 之后**的形状——
    Bitget 要剥 data 壳、Bybit 保留整个信封，喂错形状比不喂更糟。"""
    _PUBLIC_CACHE[f"{venue}:{key}"] = (time.time(), payload)


def public_cache_get(venue: str, key: str) -> Any | None:
    hit = _PUBLIC_CACHE.get(f"{venue}:{key}")
    if hit is None or time.time() - hit[0] > PUBLIC_CACHE_TTL_S:
        return None
    return hit[1]


def public_cache_age_s(venue: str, key: str) -> float | None:
    hit = _PUBLIC_CACHE.get(f"{venue}:{key}")
    return None if hit is None else time.time() - hit[0]


def public_cache_clear() -> None:
    """清空。测试用；生产路径靠 TTL 自然过期，不主动清——
    清了就等于让下一次取数去发那个注定被拒的请求。"""
    _PUBLIC_CACHE.clear()


class ExchangeError(Exception):
    """交易所返回的业务错误。"""

    def __init__(self, venue: str, code: str, message: str, *, retriable: bool = False) -> None:
        super().__init__(f"[{venue}] {code}: {message}")
        self.venue = venue
        self.code = code
        self.message = message
        self.retriable = retriable


class OrderRejected(ExchangeError):
    """订单被明确拒绝——不会成交，可以安全重试。"""


class OrderUnknown(Exception):
    """下单请求超时或连接中断，订单可能已提交也可能没有。

    这是最危险的状态：绝不能简单重发（会导致双倍仓位）。
    必须走「cancel 终结 → 查单 → 仓位差分」的消解流程。
    """

    def __init__(self, venue: str, client_order_id: str) -> None:
        super().__init__(f"[{venue}] 订单状态未知: {client_order_id}")
        self.venue = venue
        self.client_order_id = client_order_id


class RateLimited(ExchangeError):
    """被限频。风控路径要预留独立配额，不能和行情轮询抢。"""


@dataclass(frozen=True, slots=True)
class Credential:
    api_key: str
    api_secret: str
    passphrase: str = ""      # OKX / Bitget 需要
    uid: str = ""             # HTX 部分接口需要


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    side: str                 # "buy" / "sell"
    qty: Decimal
    price: Decimal | None     # None = 市价
    time_in_force: str = "IOC"
    reduce_only: bool = False
    client_order_id: str = ""


@dataclass(frozen=True, slots=True)
class OrderResult:
    client_order_id: str
    exchange_order_id: str
    status: str               # filled / partial / rejected / canceled / new
    filled_qty: Decimal
    avg_price: Decimal
    fee: Decimal = Decimal("0")
    fee_asset: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in ("filled", "rejected", "canceled")


@dataclass(slots=True)
class ClockOffset:
    """本地时钟相对交易所服务器的偏移（毫秒）。

    不能靠把 recvWindow 调大来掩盖漂移——那会让延迟到达的撤改单
    在你以为超时之后才生效。正确做法是主动校时。
    """

    offset_ms: int = 0
    synced_at: float = 0.0

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.offset_ms

    @property
    def is_stale(self) -> bool:
        return time.time() - self.synced_at > CLOCK_RESYNC_INTERVAL_S


class ExchangeAdapter(abc.ABC):
    """一家交易所的一套市场（现货/杠杆/永续）的适配器。

    子类必须实现所有抽象方法。共用的 HMAC、时钟校准、限频退避在这里。
    """

    venue: Venue
    rest_base: str
    # 返佣码的带法由子类决定；基类只负责把用户配的码传下去
    broker_code: str

    def __init__(self, credential: Credential, *, broker_code: str = "",
                 client: httpx.AsyncClient | None = None) -> None:
        self._cred = credential
        self.broker_code = broker_code
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )
        self._clock = ClockOffset()
        # 防校时递归：fetch_server_time_ms 多半也走 _request，
        # 而 _request 开头又会因时钟陈旧去调 sync_clock —— 不设闸就是无限递归/自锁
        self._syncing_clock = False
        # 限频配额分池：风控路径（撤单/查仓/撤退）预留 30%，
        # 绝不能因为行情轮询把配额吃光而无法平仓
        self._quota = {
            "market": asyncio.Semaphore(8),
            "trade": asyncio.Semaphore(6),
            "risk": asyncio.Semaphore(6),
        }

    async def close(self) -> None:
        await self._client.aclose()

    # ---- 时钟 -----------------------------------------------------------

    async def sync_clock(self) -> int:
        """用服务器时间校准本地偏移，返回偏移毫秒数。

        不能靠把 recvWindow 调大来掩盖漂移——那会让延迟到达的撤改单
        在你以为超时之后才生效，制造幽灵单。
        """
        if self._syncing_clock:
            return self._clock.offset_ms
        self._syncing_clock = True
        try:
            server_ms = await self.fetch_server_time_ms()
            local_ms = int(time.time() * 1000)
            self._clock.offset_ms = server_ms - local_ms
            self._clock.synced_at = time.time()
            return self._clock.offset_ms
        finally:
            self._syncing_clock = False

    def timestamp_ms(self) -> int:
        return self._clock.now_ms()

    @property
    def clock_drift_ms(self) -> int:
        return abs(self._clock.offset_ms)

    # ---- 签名工具 -------------------------------------------------------

    def _hmac_sha256_hex(self, payload: str) -> str:
        return hmac.new(
            self._cred.api_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()

    def _hmac_sha256_b64(self, payload: str) -> str:
        import base64
        digest = hmac.new(
            self._cred.api_secret.encode(), payload.encode(), hashlib.sha256
        ).digest()
        return base64.b64encode(digest).decode()

    # ---- 抽象接口：元数据 -----------------------------------------------

    @abc.abstractmethod
    async def fetch_server_time_ms(self) -> int:
        ...

    @abc.abstractmethod
    async def fetch_instruments(self) -> Sequence[Instrument]:
        """合约规格。必须包含 tick_size / lot_size / min_notional /
        contract_size / funding_interval_s —— 缺一样都会导致下单被拒或数量算错。"""

    @abc.abstractmethod
    async def fetch_fee_schedule(self, symbols: Sequence[str]) -> Sequence[FeeSchedule]:
        """用户的实际费率档位。**只有签名请求拿得到**，公开端点上的是挂牌基础档。

        契约（六家必须一致，不一致的后果见下）：

        - 每条 FeeSchedule 必须填 `symbol`，调用方**按 symbol 匹配，绝不按下标**。
          账户级档位（OKX/Bitget/币安合约都是一档吃全市场）填空串 ""。
        - **查不到就不要返回这一条。** 早先有三家在符号缺失时补
          `FeeSchedule(maker=0, taker=0)` 占位——零手续费会让四笔 taker 成本
          凭空消失，负期望的机会全部翻成正期望，而且不报任何错。
          宁可少返回一条让上层退回默认挂牌价（并在界面标"按默认费率估算"）。
        - 符号约定 **正数 = 成本**。OKX 原样返回"负数=你付"，适配器负责翻转；
          maker 为负即返佣，正好当收入。
        - 这些端点都不便宜（币安权重 20、Bitget 10次/秒/UID），**必须缓存**，
          绝不能进任何轮询循环。档位一天变不了一次。
        - 平台币抵扣（BNB/GT/BGB）能从 API 读到状态的就写进 `note`；
          读不到折扣**幅度**时绝不擅自打折——低估成本比高估危险得多。
        """

    @abc.abstractmethod
    async def fetch_funding_limits(self, symbol: str) -> tuple[Decimal, Decimal] | None:
        """资金费率的上下限。

        **返回顺序固定为 (cap, floor)，即 (上限, 下限)，cap >= floor。**
        六家必须一致——顺序搞反会让 scoring 的 clamp 把费率夹到空区间。

        这是判断极端费率能否持续的一票否决位：看到 -2% 很兴奋，
        但如果该合约 floor 就是 -0.75%，那个数要么是脏数据要么马上被夹回去。
        查不到返回 None（scoring 会跳过 clamp）。
        """

    # ---- 抽象接口：行情 -------------------------------------------------

    @abc.abstractmethod
    async def fetch_book_top(self, symbol: str) -> BookTop:
        ...

    @abc.abstractmethod
    async def fetch_book_depth(self, symbol: str, band_bp: float = 10.0) -> BookDepth:
        """band_bp 基点带宽内的可见深度（名义额 quote 计价，数量 base 币计价）。

        只用一档当深度代理会把容量系统性低估一两个数量级——山寨币一档常常
        只有几百刀，10 个基点内却有几万。容量估错会让真实可做的机会被判成"做不了"。

        实现约定：
        - 带宽内档位不够时如实返回 levels_used，**绝不外推补足**（外推是 scoring 的事）
        - 深度端点通常比 bookTicker 贵，走 quota="market"，不要放进 500ms 收敛循环
        - 响应缺 bids/asks 字段要抛错，不能退化成深度 0——
          scoring 里 depth_cap<=0 会跳过容量判断，等于把"没数据"读成"深度无限"
        """

    @abc.abstractmethod
    async def fetch_carry_rates(self, symbols: Sequence[str] | None = None) -> Sequence[CarryRate]:
        """当前 carry 率。

        **符号用交易所原始约定：正 = 多头付给空头。API 返回什么就原样存，不要取反。**
        六家 API 返回的都是这个符号，少一处出错的地方。杠杆借币利息同样归一到该约定
        （做多借币要付息，记为正）。上层 scoring.funding_income 按 Σf_S − Σf_L 计算，
        取反会让跨所比价整体反向。
        """

    @abc.abstractmethod
    async def fetch_funding_history(self, symbol: str, since_ms: int,
                                    limit: int = 1000) -> Sequence[tuple[int, Decimal]]:
        """历史资金费，用于稳定性评分。返回 (结算时刻, 费率) 升序序列。

        契约（六家必须一致，不一致的后果见下）：

        - `limit` 是**单次向交易所要多少条**，即页大小，不是返回条数的上限。
          各家单页上限差 10 倍（OKX/HTX/Bitget 100、Bybit 200、币安/Gate 1000），
          适配器该内部翻页把 since_ms 到现在这一整段补齐，返回可以多于 limit。
        - **返回必须一直覆盖到窗口末端（最新那条）。** 真要截断就截掉最旧的一端。
          反过来截（返回"since 之后最旧的 N 条"）会给出一段停在两周前、
          看起来却完全正常的序列——调用方无从察觉，scoring 会拿它当此刻的稳定性。
          币安的服务端正是这个方向，适配器必须把它掰回来。
        - 结算时刻要**去重**：按 pageNo/page_index 翻页的家（HTX、Bitget）在
          翻页途中跨过一次结算就会让整个列表下移一位，同一笔费率进两次。
          重复值会抬高 autocorr()、拖偏 median，而且不报任何错。
        """

    @abc.abstractmethod
    def stream_book_top(self, symbols: Sequence[str]) -> AsyncIterator[BookTop]:
        """WebSocket 盘口流。必须自行处理心跳和重连。"""

    # ---- 抽象接口：交易 -------------------------------------------------

    @abc.abstractmethod
    async def place_order(self, req: OrderRequest) -> OrderResult:
        """下单。

        实现约定（关键）：
        - 必须用 client_order_id 保证幂等，绝不允许无 ID 下单
        - 网络超时/连接中断必须抛 OrderUnknown，**不能**当作失败返回
        - 返佣码按本家机制注入（订单ID前缀 / header / body 字段）
        """

    @abc.abstractmethod
    async def cancel_order(self, symbol: str, client_order_id: str) -> None:
        """撤单。这是**状态终结操作**——执行后订单只可能是已撤或已成，
        所以它是消解 OrderUnknown 的第一步。"""

    @abc.abstractmethod
    async def query_order(self, symbol: str, client_order_id: str) -> OrderResult:
        ...

    @abc.abstractmethod
    async def fetch_positions(self, symbols: Sequence[str] | None = None) -> Sequence[Position]:
        """持仓。这是**真值**——任何时候本地记账与它冲突，无条件以它为准。"""

    @abc.abstractmethod
    async def fetch_open_orders(self, symbol: str) -> Sequence[OrderResult]:
        """未成交挂单。判断两腿是否平衡必须计入这些——
        「A 已成交、B 还挂着」是限价模式下最容易出现的隐形单腿。"""

    @abc.abstractmethod
    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        ...

    @abc.abstractmethod
    async def fetch_leverage_tiers(self, symbol: str) -> Sequence[tuple[Decimal, Decimal]]:
        """杠杆档位 [(名义上限, 最大杠杆), ...] 升序。用于避开档位边界——
        贴着边界开仓，价格一动就会跨档触发降杠杆和追加保证金。"""

    async def fetch_my_trades(self, symbol: str, since_ms: int,
                              until_ms: int | None = None) -> Sequence[TradeFill]:
        """历史成交（逐笔 fill），升序。只服务操作复盘分析，不进交易路径。

        故意**不做成抽象方法**：九家适配器不必为一个分析功能全体陪跑，
        没实现的家在这里抛 NotImplementedError，分析页把原话展示给用户。
        实现约定（照 fetch_funding_history 的纪律）：
        - 适配器内部翻页补齐 [since_ms, until_ms] 整段，按成交 id 去重后升序返回
        - 手续费/已实现盈亏保留交易所原始计价资产，不折算
        """
        raise NotImplementedError(
            f"{self.venue.value} 尚未实现成交历史查询（fetch_my_trades）")

    # ---- 通用能力 -------------------------------------------------------

    async def resolve_unknown_order(self, symbol: str,
                                    client_order_id: str) -> Decimal | None:
        """消解未知订单状态。返回实际成交量，None 表示交易所不可读。

        固定动作序列（顺序不可颠倒）：
        1. cancel —— 状态终结，之后订单只可能是 canceled 或 filled
        2. query —— 读出确定的成交量
        3. 若交易所连订单都不认识，用仓位差分推断
        """
        deadline = time.time() + 20
        backoff = 0.2
        while time.time() < deadline:
            try:
                await self.cancel_order(symbol, client_order_id)
            except OrderRejected:
                pass          # 已终结，继续查
            except (httpx.HTTPError, RateLimited):
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 1.0)
                continue
            try:
                result = await self.query_order(symbol, client_order_id)
                return result.filled_qty
            except OrderRejected:
                # 交易所不认识这个订单 —— 说明它从未被接受
                return Decimal("0")
            except (httpx.HTTPError, RateLimited):
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 1.0)
        return None

    def _public_cache_hit(self, method: str, path: str, *, signed: bool = False,
                          params: Mapping[str, Any] | None = None,
                          body: Mapping[str, Any] | None = None) -> Any | None:
        """外部喂进来的公开数据，没有就 None。

        **四个覆写了 _request 的子类（binance/okx/bybit/kucoin）必须各自在自己的
        覆写版开头调用它**——拦截写在基类 _request 里对它们一行都不生效，
        而那四家里恰好有两家（binance/bybit）正是靠浏览器喂数才拿得到数据。
        """
        if signed:
            return None       # 私有接口绝不读缓存：见 _PUBLIC_CACHE 的铁律 1
        key = public_cache_key(method, path, params, body)
        if key is None:
            return None
        return public_cache_get(self.venue.value, key)

    async def _request(self, method: str, path: str, *, quota: str = "market",
                       signed: bool = False, params: Mapping[str, Any] | None = None,
                       body: Mapping[str, Any] | None = None,
                       extra_headers: Mapping[str, str] | None = None) -> Any:
        """带配额控制和退避的 HTTP 请求。子类通过覆写 _sign 注入各家签名。"""
        # 缓存命中要挡在**校时之前**：校时自己也是一发 HTTP，被封锁的家一样会挨拒，
        # 挡在后面等于"读了缓存但还是发了个请求"，白白违背零请求的前提。
        # 本地模式下缓存永远是空的（没人喂），这一段等于不存在。
        cached = self._public_cache_hit(method, path, signed=signed,
                                        params=params, body=body)
        if cached is not None:
            return cached

        if self._clock.is_stale and not self._syncing_clock:
            try:
                await self.sync_clock()
            except Exception:
                pass          # 校时失败不阻塞业务，但会在 drift 上体现出来

        async with self._quota.get(quota, self._quota["market"]):
            headers, url, send_params, send_body = self._prepare(
                method, path, params or {}, body or {}, signed
            )
            if extra_headers:
                headers = {**headers, **extra_headers}
            try:
                resp = await self._client.request(
                    method, url, params=send_params or None,
                    json=send_body if send_body and method != "GET" else None,
                    headers=headers,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise ExchangeError(self.venue.value, "NETWORK", str(exc), retriable=True) from exc
            return self._parse(resp)

    @abc.abstractmethod
    def _prepare(self, method: str, path: str, params: dict, body: dict,
                 signed: bool) -> tuple[dict[str, str], str, dict, dict]:
        """构造签名后的 headers / url / params / body。各家差异全在这里。"""

    @abc.abstractmethod
    def _parse(self, resp: httpx.Response) -> Any:
        """解析响应，把各家的错误码翻译成统一异常。"""


@dataclass(slots=True)
class AdapterRegistry:
    """已连接的适配器集合，按 market key（如 "binance:perp"）索引。"""

    _adapters: dict[str, ExchangeAdapter] = field(default_factory=dict)

    def register(self, market: str, adapter: ExchangeAdapter) -> None:
        self._adapters[market] = adapter

    def get(self, market: str) -> ExchangeAdapter:
        adapter = self._adapters.get(market)
        if adapter is None:
            raise KeyError(f"未连接的市场: {market}")
        return adapter

    def markets(self) -> Sequence[str]:
        return tuple(self._adapters)

    async def close_all(self) -> None:
        await asyncio.gather(*(a.close() for a in self._adapters.values()),
                             return_exceptions=True)
