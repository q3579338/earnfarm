"""历史资金费回填，以及把两条腿对齐成小时 carry 序列。

为什么需要这个模块：scoring.stability() 要的是**按小时对齐的 carry 序列**，
只有当前费率一个点时它一律返回 0.35 的保守先验（is_prior=True），
机会榜的"稳定性"整列就是废的。有了真实历史才分得清
「这个费差稳定存在三个月」和「昨天刚出现的偶然」。

三件事这里必须做对，做错了比没有历史更糟（假数据会被当成真样本）：

1. **两腿周期不同是常态，不是例外。** 币安 8h、Bybit 会把某些合约动态切成 1h、
   Gate 有 4h 品种。直接把两条 rates 序列相减等于拿 8 小时的费率去减 1 小时的，
   数量级差 8 倍。必须各自摊平成小时序列再相减。

2. **缺口不能无限前向填充。** 拿三天前的费率冒充今天的样本，会让 stability
   的胜率、存续时长全部虚高——而这几个数正是用来否决机会的。
   超过上限就把序列**截断**，只保留最近一段连续样本，宁可退回"数据不足"。

3. **符号按当前定向取，不能取绝对值。** 返回 short_hourly - long_hourly。
   取绝对值会让 _median_run_length 的翻转检测彻底失效。

精度：落盘和费率算术走 Decimal，喂给 scoring 的统计量走 float
（scoring 本身就是 float 的启发式排序）。
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Mapping, Sequence

from .exchanges.base import ExchangeAdapter, ExchangeError, RateLimited
from .models import Venue
from .scoring import HistoryStats
from .storage import Storage

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
DEFAULT_INTERVAL_S = 28_800          # 8h，取不到时的兜底

# 真实世界里出现过的结算周期。用白名单而不是"任何整小时"：
# 一次漏点会造成 16h 的相邻间隔，若当成周期就会把一条 8h 费率摊到 16 小时上，
# 数值直接腰斩且不会有任何报错。
PLAUSIBLE_INTERVAL_MS = frozenset(h * HOUR_MS for h in (1, 2, 3, 4, 6, 8, 12, 24))

# 缺口前向填充上限（小时）。12 小时刚好容得下 8h 合约漏一次结算，
# 漏两次（16h）就该截断了——那已经不是抖动，是数据真的断了。
MAX_FORWARD_FILL_H = 12

# 单个品种的翻页硬闸。防止适配器忽略 since 导致游标不前进时无限打接口。
MAX_PAGES = 32

# 库里已有的最老一条比请求下界晚这么多以内，就认为深度够了，不再深挖。
# 一个结算周期的误差不值得多打一次接口。
DEEP_REFETCH_TOLERANCE_MS = 2 * DAY_MS


class HistoryError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class VenueHistoryLimit:
    """各家历史接口的回溯深度。数据来自 docs/research/exchange-*.md 的实测。

    max_days / max_records 为 0 表示"没有已知上限"（能翻到合约上线日）。
    这两个上限的意义不是省请求，而是**别把拉不到的区间当成故障**：
    Bitget 就是 270 条到顶，第 4 页永远是空的。
    """

    max_days: int
    max_records: int
    page_limit: int
    note: str

    def effective_days(self, requested_days: int, interval_h: float) -> float:
        """这家所在该周期下实际能给多少天。

        条数上限要换算成天数才能和 max_days 比：Bitget 的 270 条在 8h 品种上
        是 90 天，在 1h 品种上只有 11 天——同一个"上限"差 8 倍。
        """
        days = float(requested_days)
        if self.max_days:
            days = min(days, float(self.max_days))
        if self.max_records and interval_h > 0:
            days = min(days, self.max_records * interval_h / 24.0)
        return days


# 实测结论见 docs/research/exchange-*.md，"文档没写"的部分是探测出来的
VENUE_LIMITS: Mapping[Venue, VenueHistoryLimit] = {
    Venue.BINANCE: VenueHistoryLimit(
        0, 0, 1000,
        "1000条/次，可回溯到合约上线日（onboardDate）；限频与 fundingInfo 共享 500次/5分钟/IP"),
    Venue.OKX: VenueHistoryLimit(
        93, 0, 1000,
        "实测只有约3个月（单页100条，再往前一律返回空数组）——超过3个月的回测必须自己落库"),
    Venue.BYBIT: VenueHistoryLimit(
        0, 0, 1000,
        "文档无保留上限，实测可到上线日；单页200条且只能按 endTime 往回翻"),
    Venue.GATE: VenueHistoryLimit(
        180, 0, 1000,
        "180天硬上限（from 更早直接 400）；只传 from 不传 to 会被静默夹成30天"),
    Venue.HTX: VenueHistoryLimit(
        0, 0, 1000,
        "全历史，BTC-USDT 实测6329条回到2020上线日；单页100条，只能按 page_index 翻"),
    Venue.BITGET: VenueHistoryLimit(
        90, 270, 1000,
        "270条硬顶（8h≈90天／4h≈45天／1h≈11天）；没有时间过滤参数，只能按 pageNo 翻"),
}

_FALLBACK_LIMIT = VenueHistoryLimit(90, 0, 1000, "未知交易所，按 90 天保守处理")

# 每个 market 两次请求之间的最小间隔（秒）。**这不是省事的礼貌，是硬约束**：
# 币安的 fundingRate 端点吃 500次/5分钟/IP 的专用桶（与 fundingInfo 共享），
# 而 base.py 的 _quota 是纯并发信号量、不是令牌桶——8 路并发 ×300ms ≈ 26 req/s，
# 全市场 573 个永续会在 22 秒内打完，超配额 6 倍。更糟的是**这个端点不返回
# X-MBX-USED-WEIGHT-* 头**，适配器自带的"看用量主动退避"在这条路径上完全失明，
# 只能靠这里按时间硬节流。0.65s ≈ 460次/5分钟，压在配额线下面。
# 默认空 = 不节流：单个品种的按需回填根本用不上，一次性上百次调用才需要。
DEFAULT_PACE_S: Mapping[str, float] = {
    "binance:perp": 0.65,
}


@dataclass(frozen=True, slots=True)
class BackfillResult:
    """一个品种一次回填的结果。失败也返回结果对象而不是抛异常——
    一个品种挂了不该带崩整批。"""

    market: str
    symbol: str
    ok: bool
    fetched: int = 0            # 本次从交易所取回的点数（含库里已有的重复点）
    pages: int = 0              # 实际发出的翻页轮数
    since_ms: int = 0           # 本次请求的起点，增量时等于库里最新一条+1
    incremental: bool = True    # False = 深挖（库里深度不够或第一次拉）
    covered_days: float = 0.0   # 回填后库里实际覆盖的天数
    venue_capped: bool = False  # 请求天数被交易所回溯上限砍过
    error: str = ""


class HistoryBackfiller:
    """把六家的历史资金费拉进库，并按需对齐成 scoring 能吃的形状。

    并发做了两层限制：这里的信号量控制"同时有几个品种在拉"，
    适配器内部的 _quota 控制"单家同时有几个请求"。少了外层这一层，
    一次全市场回填会瞬间把六家的配额全打满，连风控用的撤单配额都抢没了。
    """

    def __init__(self, storage: Storage,
                 adapters: Mapping | None = None, *,
                 max_concurrency: int = 4,
                 max_retries: int = 3,
                 retry_base_s: float = 0.5,
                 retry_max_s: float = 8.0,
                 max_forward_fill_h: int = MAX_FORWARD_FILL_H,
                 max_pages: int = MAX_PAGES,
                 page_limit: int | None = None,
                 pace_s: Mapping[str, float] | None = None,
                 sleeper: Callable | None = None,
                 pace_sleeper: Callable | None = None,
                 clock: Callable[[], int] | None = None) -> None:
        self._storage = storage
        self._adapters: dict[str, ExchangeAdapter] = {}
        self.register_all(adapters)
        self._sem = asyncio.Semaphore(max(1, max_concurrency))
        self._max_retries = max_retries
        self._retry_base_s = retry_base_s
        self._retry_max_s = retry_max_s
        self._max_fill_h = max(1, max_forward_fill_h)
        self._max_pages = max(1, max_pages)
        # 覆盖各家的单次条数上限。想更温柔地打接口时调小它
        self._page_limit = page_limit
        self._sleep = sleeper or asyncio.sleep
        # 节流用独立的 sleeper 和 monotonic 时钟：退避 sleeper 在测试里被替换成
        # 空实现以免真的睡，而节流是"每秒最多几次"的物理约束，
        # 两者混在一起会让退避的断言被节流的睡眠污染
        self._pace_s = dict(pace_s or {})
        self._pace_sleep = pace_sleeper or asyncio.sleep
        self._pace_last: dict[str, float] = {}
        self._pace_locks: dict[str, asyncio.Lock] = {}
        self._clock = clock or (lambda: int(time.time() * 1000))

    # ---- 适配器登记 -----------------------------------------------------

    def register(self, target: Venue | str, adapter: ExchangeAdapter) -> None:
        self._adapters[market_key(target)] = adapter

    def register_all(self, adapters: Mapping | None) -> None:
        """接受 {Venue: adapter}、{"binance:perp": adapter} 或 AdapterRegistry。

        PublicFeed 按 Venue 存、AdapterRegistry 按 market key 存，
        统一在这里归一，调用方不用为了喂这个类去改自己的容器。
        """
        if not adapters:
            return
        if hasattr(adapters, "items"):
            for key, adapter in adapters.items():           # type: ignore[union-attr]
                self.register(key, adapter)
        elif hasattr(adapters, "markets"):
            for market in adapters.markets():               # type: ignore[union-attr]
                self.register(market, adapters.get(market))  # type: ignore[union-attr]
        else:
            raise HistoryError("adapters 必须是映射或 AdapterRegistry")

    # ---- 回填 -----------------------------------------------------------

    async def backfill(self, venue: Venue | str, symbol: str, days: int = 90, *,
                       now_ms: int | None = None, full: bool = False) -> BackfillResult:
        """增量回填一个品种。

        增量的判据是两头都看：
        - 库里**最新**一条决定往后拉多少（正常每日任务只补这一段）；
        - 库里**最老**一条决定要不要往前深挖（第一次拉、或者请求天数变长了）。
        只看最新一条的实现会有个隐蔽后果：用户把回溯从 30 天调到 90 天，
        程序会一直用那 30 天的样本装作有 90 天。

        full=True 强制按完整天数重拉（改了周期推断逻辑要重建时用）。
        """
        market = market_key(venue)
        adapter = self._adapters.get(market)
        if adapter is None:
            return BackfillResult(market, symbol, ok=False,
                                  error=f"未登记的市场: {market}")

        now = now_ms if now_ms is not None else self._clock()
        limit = venue_limit(market)
        capped = bool(limit.max_days) and days > limit.max_days
        eff_days = min(days, limit.max_days) if limit.max_days else days
        floor_ms = now - int(eff_days * DAY_MS)

        # 交易所回溯墙：探到过就别再往墙外挖。没有这个标记的话，
        # 凡是历史短于请求天数的品种（新上市的、Bitget 270 条到顶的）
        # 每天都会白白深挖一轮。
        wall = self._storage.get_history_floor(market, symbol)
        if wall is not None:
            floor_ms = max(floor_ms, wall)

        latest = self._storage.latest_funding_ms(market, symbol)
        earliest = self._storage.earliest_funding_ms(market, symbol)
        deep = full or latest is None or earliest is None or \
            earliest > floor_ms + DEEP_REFETCH_TOLERANCE_MS
        since = floor_ms if deep else max(floor_ms, latest + 1)

        try:
            async with self._sem:
                points, pages, saw_full_page = await self._pull(
                    adapter, symbol, since, now,
                    self._page_limit or limit.page_limit, market)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                      # 一个品种失败不影响其他品种
            self._log(f"{market} {symbol} 历史回填失败: {exc}", now)
            return BackfillResult(market, symbol, ok=False, since_ms=since,
                                  incremental=not deep, venue_capped=capped,
                                  error=str(exc))

        if points:
            anchor = latest if not deep else None
            self._persist(market, symbol, points, anchor)
            # 深挖时如果最老一条明显晚于我们要的起点，说明撞到交易所的墙了，
            # 记下来，下次别再挖同一段空气。
            # 但**见过满页就不能记**：那时最老一条只是条数上限切出来的边界，
            # 不是这家的历史尽头（见 _pull 的说明）。误记下来等于把该品种的
            # 历史深度永久钉死在截断处。
            oldest = points[0][0]
            if deep and not saw_full_page and oldest > since + DEEP_REFETCH_TOLERANCE_MS:
                self._storage.set_history_floor(market, symbol, oldest)

        first = self._storage.earliest_funding_ms(market, symbol)
        last = self._storage.latest_funding_ms(market, symbol)
        covered = (last - first) / DAY_MS if first is not None and last is not None else 0.0
        return BackfillResult(market, symbol, ok=True, fetched=len(points), pages=pages,
                              since_ms=since, incremental=not deep,
                              covered_days=covered, venue_capped=capped)

    async def backfill_many(self, targets: Sequence[tuple[Venue | str, str]],
                            days: int = 90, *, now_ms: int | None = None,
                            full: bool = False,
                            on_progress: Callable[[int, int], None] | None = None
                            ) -> list[BackfillResult]:
        """整批回填。并发由信号量控制，某个品种失败只体现在它自己的结果里。

        on_progress(已完成, 总数) 在每个品种落地后回调一次。回填一批几十个品种
        要跑十几秒到几分钟，界面上不给进度用户只会以为程序卡死了。
        回调里抛异常不能带崩回填——那是显示逻辑的问题，不是数据的问题。
        """
        now = now_ms if now_ms is not None else self._clock()
        total = len(targets)
        done = 0

        async def one(venue: Venue | str, symbol: str) -> BackfillResult:
            nonlocal done
            try:
                return await self.backfill(venue, symbol, days, now_ms=now, full=full)
            finally:
                done += 1
                if on_progress is not None:
                    try:
                        on_progress(done, total)
                    except Exception:
                        pass

        raw = await asyncio.gather(*(one(v, s) for v, s in targets),
                                   return_exceptions=True)
        out: list[BackfillResult] = []
        for (venue, symbol), item in zip(targets, raw):
            if isinstance(item, BaseException):
                out.append(BackfillResult(market_key(venue), symbol, ok=False,
                                          error=str(item)))
            else:
                out.append(item)
        return out

    async def backfill_pair(self, long_market: Venue | str, long_symbol: str,
                            short_market: Venue | str, short_symbol: str,
                            days: int = 90, *,
                            now_ms: int | None = None) -> list[BackfillResult]:
        """两腿一起补。算 hourly_carry 之前应该先调它——
        只补一条腿的话，另一条腿的缺口会把整段序列截断掉。"""
        return await self.backfill_many(
            [(long_market, long_symbol), (short_market, short_symbol)],
            days, now_ms=now_ms)

    # ---- 拉取内部实现 ---------------------------------------------------

    async def _pull(self, adapter: ExchangeAdapter, symbol: str, since_ms: int,
                    now_ms: int, page_limit: int,
                    market: str = "") -> tuple[list[tuple[int, Decimal]], int, bool]:
        """按 settle_ms 向前滚动翻页。返回 (点集, 翻页轮数, 是否见过满页)。

        绝不用 offset/pageNo 做外层分页：那套语义在"翻页期间又结算了一次"时
        会整体位移一格，中间必漏一条，而且漏了完全不报错。
        时间游标是幂等的，重叠部分靠主键去重。

        "见过满页"这个返回值是给**回溯墙**用的。判断"这家没有更早的数据了"
        唯一可靠的依据是：它对着我们给的起点返回了一页**没装满**的数据——
        那说明它把有的全给了。一旦某页装满，最老那条就只是被条数上限切出来的
        边界：Bybit / Gate 是从"现在"往回翻的，窗口超过单次上限时保留的是
        **最新**那一批，最老一条自然明显晚于起点，长得和撞墙一模一样。
        把截断误记成墙，该品种的历史深度就被永久钉死在截断的位置，之后连试都不会再试。
        """
        collected: dict[int, Decimal] = {}
        cursor = since_ms
        pages = 0
        saw_full_page = False
        while pages < self._max_pages:
            rows = await self._fetch_with_retry(adapter, symbol, cursor, page_limit,
                                                market)
            pages += 1
            if len(rows) >= page_limit:
                saw_full_page = True
            fresh = [(ts, rate) for ts, rate in rows if ts >= cursor]
            if not fresh:
                break
            for ts, rate in fresh:
                collected[int(ts)] = Decimal(rate)
            newest = max(ts for ts, _ in fresh)
            # 游标不前进说明适配器压根没理会 since（HTX/Bitget 是按页号翻的），
            # 再翻下去就是拿同一批数据死循环
            if newest <= cursor:
                break
            # 不满一页 = 这家已经给完了。多打一次只会拿到空数组
            if len(rows) < page_limit:
                break
            cursor = newest + 1
            if cursor > now_ms:
                break
        return sorted(collected.items()), pages, saw_full_page

    async def _fetch_with_retry(self, adapter: ExchangeAdapter, symbol: str,
                                since_ms: int, limit: int,
                                market: str = "") -> Sequence[tuple[int, Decimal]]:
        """指数退避重试。只重试网络/限频这类临时错误——
        符号不存在之类的业务错误重试多少次都是一样的结果，还白烧配额。"""
        delay = self._retry_base_s
        for attempt in range(self._max_retries + 1):
            try:
                await self._pace(market)
                return await adapter.fetch_funding_history(symbol, since_ms, limit)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt >= self._max_retries or not _is_retriable(exc):
                    raise
                await self._sleep(delay)
                delay = min(delay * 2, self._retry_max_s)
        return ()

    async def _pace(self, market: str) -> None:
        """把同一家的请求按最小间隔排开。

        锁必须**包住睡眠**：不加锁的话 N 个并发协程会同时读到同一个"上次调用时刻"，
        各自算出同样的等待时间，睡醒后一起发车——节流等于没做。
        """
        gap = self._pace_s.get(market, 0.0)
        if gap <= 0:
            return
        lock = self._pace_locks.setdefault(market, asyncio.Lock())
        async with lock:
            last = self._pace_last.get(market)
            now = time.monotonic()
            if last is not None and now - last < gap:
                await self._pace_sleep(gap - (now - last))
            self._pace_last[market] = time.monotonic()

    def _persist(self, market: str, symbol: str,
                 points: Sequence[tuple[int, Decimal]], anchor_ms: int | None) -> None:
        """落库，顺便给每条点推断出它自己的结算周期。

        anchor_ms 是库里已有的最后一条：增量批次的第一条点必须拿它当前驱，
        否则那条点会没有前一条可比，周期只能靠猜。

        save_funding 一次只能写一个 interval_s，所以按周期分组写——
        Bybit 会把合约从 8h 动态切成 1h，一批数据里出现两种周期是正常的。
        """
        rows = _with_intervals(points, anchor_ms)
        grouped: dict[int, list[tuple[int, Decimal]]] = defaultdict(list)
        for ts, rate, interval_s in rows:
            grouped[interval_s].append((ts, rate))
        for interval_s, pts in grouped.items():
            self._storage.save_funding(market, symbol, interval_s, pts)

    def _log(self, message: str, now_ms: int) -> None:
        try:
            self._storage.log_event("warn", "system", message, now_ms)
        except Exception:
            pass          # 记日志失败不该让回填也跟着失败

    # ---- 喂给 scoring 的两个出口 ----------------------------------------

    def stats_for(self, market: Venue | str, symbol: str, days: int = 30, *,
                  now_ms: int | None = None) -> HistoryStats:
        """从库里读出一条腿的历史统计，直接交给 scoring。

        rates 保持**交易所原始约定**（正 = 多头付给空头），和 LegQuote.funding_now
        同一套符号——scoring.funding_income 自己按 Σf_S − Σf_L 处理方向，
        在这里取反会让整个跨所比价反过来。
        """
        key = market_key(market)
        now = now_ms if now_ms is not None else self._clock()
        rows = self._storage.load_funding_series(key, symbol, now - days * DAY_MS)
        if not rows:
            return HistoryStats((), DEFAULT_INTERVAL_S / 3600)
        rates = tuple(float(r) for _, r, _ in rows)
        interval_s = _median_int([iv for _, _, iv in rows]) or DEFAULT_INTERVAL_S
        return HistoryStats(rates, interval_s / 3600)

    def hourly_carry(self, long_market: Venue | str, long_symbol: str,
                     short_market: Venue | str, short_symbol: str,
                     days: int = 30, *, now_ms: int | None = None) -> list[float]:
        """两腿对齐后的小时 carry 序列，直接喂 scoring.stability()。

        符号按**当前定向**取：short_hourly − long_hourly，正数 = 这个方向在赚钱。
        返回的是最近一段**连续**样本：中间只要出现超过上限的缺口，
        缺口之前的部分全部丢弃。宁可样本变少退回"数据不足"，
        也不能把三天前的费率当成今天的观测——stability 的胜率和存续时长
        正是用来否决机会的，喂它陈旧数据等于自己骗自己。
        """
        now = now_ms if now_ms is not None else self._clock()
        since = now - days * DAY_MS
        long_grid = self._grid(market_key(long_market), long_symbol, since)
        short_grid = self._grid(market_key(short_market), short_symbol, since)
        return align_hourly(long_grid, short_grid, self._max_fill_h)

    def _grid(self, market: str, symbol: str, since_ms: int) -> dict[int, float]:
        rows = self._storage.load_funding_series(market, symbol, since_ms)
        return hourly_grid(rows)


# ---- 无状态工具（便于单测直接调） ---------------------------------------

def market_key(target: Venue | str) -> str:
    """把 Venue / "binance" / "binance:perp" 统一成 market key。"""
    if isinstance(target, Venue):
        return f"{target.value}:perp"
    text = str(target)
    return text if ":" in text else f"{text}:perp"


def venue_limit(market: Venue | str) -> VenueHistoryLimit:
    key = market_key(market)
    try:
        return VENUE_LIMITS[Venue(key.split(":", 1)[0])]
    except (ValueError, KeyError):
        return _FALLBACK_LIMIT


def _is_retriable(exc: BaseException) -> bool:
    if isinstance(exc, RateLimited):
        return True
    if isinstance(exc, ExchangeError):
        return bool(getattr(exc, "retriable", False))
    # httpx 的网络错误都继承 OSError 之外的层级，这里按 TimeoutError/OSError 兜底；
    # 名字匹配是为了不把 httpx 变成本模块的硬依赖
    if isinstance(exc, (TimeoutError, OSError, ConnectionError)):
        return True
    return type(exc).__module__.split(".")[0] == "httpx"


def _median_int(values: Sequence[int]) -> int:
    if not values:
        return 0
    s = sorted(values)
    return s[len(s) // 2]


def _with_intervals(points: Sequence[tuple[int, Decimal]],
                    anchor_ms: int | None) -> list[tuple[int, Decimal, int]]:
    """给每条结算点推断它覆盖的周期（秒）。

    周期取**与前一条的间隔**，因为一条费率结算的是它之前那段时间。
    但间隔不能无条件当周期：漏掉一条 8h 数据会造成 16h 或 24h 的间隔，
    直接采信就会把一条 8h 费率摊到 24 小时上、数值凭空缩到三分之一，而且不报任何错。

    判据分三层，缺一层就会误判：
      1. 必须是真实存在过的周期值（16h 这种一眼就是漏点）；
      2. 不超过本批中位数的 1.5 倍——挡掉 24h 这种"恰好也是合法周期"的漏点；
      3. 但超了也不一定是漏点：Bybit 会把合约从 8h 切成 1h，切换后 1h 占多数、
         中位数变成 1h，早期真实的 8h 间隔全都会被第 2 层冤枉。
         所以再补一条——**重复出现**的间隔就是真周期（相邻间隔与它相等），
         漏点造成的大间隔是孤立的，不会连着出现。
    判不出来就回落到中位数，让缺的那几小时以"缺口"形式暴露给前向填充/截断逻辑。
    """
    if not points:
        return []
    ts_list = [int(ts) for ts, _ in points]

    # prev_gaps[i] = 第 i 条点与它前一条的间隔（第一条没有前驱时为 None）
    prev_gaps: list[int | None] = []
    prev = anchor_ms
    for ts in ts_list:
        prev_gaps.append(ts - prev if prev is not None and ts > prev else None)
        prev = ts

    plausible = [g for g in prev_gaps if g in PLAUSIBLE_INTERVAL_MS]
    median_ms = _median_int(plausible) or DEFAULT_INTERVAL_S * 1000
    if median_ms not in PLAUSIBLE_INTERVAL_MS:
        median_ms = DEFAULT_INTERVAL_S * 1000

    def accept(i: int) -> int | None:
        gap = prev_gaps[i]
        if gap is None or gap not in PLAUSIBLE_INTERVAL_MS:
            return None
        if gap <= median_ms * 1.5:
            return gap
        neighbours = [prev_gaps[j] for j in (i - 1, i + 1)
                      if 0 <= j < len(prev_gaps)]
        return gap if gap in neighbours else None

    accepted = [accept(i) for i in range(len(ts_list))]
    # 整批第一条没有前驱可比，拿后面第一个确定下来的周期当它的周期，
    # 比拿中位数强：周期切换时中位数属于新制度，而第一条在旧制度里
    first_known = next((g for g in accepted if g is not None), median_ms)

    out: list[tuple[int, Decimal, int]] = []
    for i, (ts, rate) in enumerate(points):
        interval_ms = accepted[i]
        if interval_ms is None:
            interval_ms = first_known if i == 0 else median_ms
        out.append((int(ts), Decimal(rate), interval_ms // 1000))
    return out


def hourly_grid(rows: Sequence[tuple[int, Decimal, int]]) -> dict[int, float]:
    """把结算点摊平成 {小时下标: 该小时的费率}。

    小时下标 h 代表 [h*3600, (h+1)*3600)。一条 t 时刻结算、周期 T 小时的费率
    覆盖的是它**之前**的 T 个小时，即下标 t_h-T … t_h-1 —— 资金费是对已持有的
    那段时间收的，摊到未来是把因果搞反了。

    两个细节：
    - 结算时刻要**四舍五入**到整点。Gate 返回的是 00:00:01 这种，直接整除没问题，
      但 23:59:59 这种会被整除到上一个小时，整段覆盖窗口偏移一格。
    - 覆盖长度取 min(周期, 与前一条的实际间隔)：中间漏了点时，
      宁可留出空洞让前向填充/截断逻辑去判，也不要把一条费率摊得到处都是。
    """
    grid: dict[int, float] = {}
    prev_ts: int | None = None
    for ts, rate, interval_s in rows:
        span_h = max(1, round(interval_s / 3600))
        end_h = (int(ts) + HOUR_MS // 2) // HOUR_MS
        gap_h = span_h if prev_ts is None else max(1, round((int(ts) - prev_ts) / HOUR_MS))
        cover = min(span_h, gap_h)
        # 除以 span_h 而不是 cover：费率对应的是完整周期，
        # cover 只决定它摊在哪几个小时上
        per_hour = float(rate) / span_h
        for h in range(end_h - cover, end_h):
            grid[h] = per_hour
        prev_ts = int(ts)
    return grid


def align_hourly(long_grid: Mapping[int, float], short_grid: Mapping[int, float],
                 max_fill_h: int = MAX_FORWARD_FILL_H) -> list[float]:
    """两条小时网格对齐相减，返回最近一段连续的 short − long。

    只在两腿都有数据的重叠区间上工作：一条腿 2020 年就上线、另一条上个月才有，
    把不重叠的部分算进来等于凭空捏造对冲收益。
    """
    if not long_grid or not short_grid:
        return []
    start = max(min(long_grid), min(short_grid))
    end = min(max(long_grid), max(short_grid))
    if end < start:
        return []

    out: list[float] = []
    l_last: float | None = None
    s_last: float | None = None
    l_stale = s_stale = 0
    for h in range(start, end + 1):
        lv = long_grid.get(h)
        if lv is None:
            l_stale += 1
        else:
            l_last, l_stale = lv, 0
        sv = short_grid.get(h)
        if sv is None:
            s_stale += 1
        else:
            s_last, s_stale = sv, 0
        if l_last is None or s_last is None or l_stale > max_fill_h or s_stale > max_fill_h:
            # 缺口超限：之前攒的样本作废。留着它们会让 stability 把
            # 一段跨越数据空洞的"伪连续"序列当成有效观测
            out.clear()
            continue
        out.append(s_last - l_last)
    return out
