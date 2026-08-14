"""纸上交易网关：行情用真的，只有成交是模拟的。

实现 `executor.OrderGateway` 协议，可以直接顶替真实网关塞进 `Executor`，
不花一分钱跑完整个状态机。

为什么值得单独写一层，而不是把 tests 里那个 FakeGateway 拿来用：

1. **假行情测出来的结论没有意义**。滑点、深度、价差是这类工具唯一的真实成本，
   拿中间价假装成交，回测出来的净年化必然是幻觉。所以盘口从真实适配器拉
   （`AdapterRegistry`），只把"撮合"这一步换成本地模拟。没有适配器时退化到
   可注入的合成盘口，用于纯离线单测。

2. **它的最大价值不是模拟赚钱，是模拟出事**。真实交易所的坏情况（下单超时、
   合约突然停牌、盘口冻住、保证金不足）平时根本测不到，而"撤退阶梯""补腿判死"
   这些路径恰恰只在坏情况下才会被走到。所以这里有一整套故障注入开关——
   没有它们，executor 里最重要的那半边代码就是死代码。

3. **成交模拟必须诚实**。这里刻意不当好好先生：
   - IOC 限价单只吃**价格够得着**的档位，够不着的部分不成交（真实行为）
   - 逐档吃，均价是加权出来的，不是拿中间价充数
   - reduce_only 在无仓位时被拒（币安 -2022 那一类）
   - taker 手续费照扣，资金费按持仓时长照结算
   一个乐观的模拟器比没有模拟器更危险，因为它会让人以为自己验证过了。

几条模型边界（别把模拟当实测）：
- 六家的 REST 只给一档最优价（见各 adapter 的 fetch_book_top），所以第 1 档是
  真数据，深档由 `DepthProfile` 外推。报表里会标出 synthetic_levels，
  单笔吃穿到第几档也记着——吃得越深，结果越是模型而非事实。
- 只模拟吃单（IOC / MARKET），挂单排队不模拟，费率恒按 taker 算。
  这个偏差方向是**保守**的（高估成本），可以接受。
- 资金费按持仓时长线性计提；真实交易所是结算时刻的快照。线性计提让
  P&L 曲线连续，代价是抓不到"结算前一秒进场"这种薅法——那种玩法本来也不该验证。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from .executor import PlacedOrder
from .models import BookTop
from .safety import LegHealth

if TYPE_CHECKING:                       # 只为类型标注，运行时不导入 ——
    from .exchanges.base import AdapterRegistry   # 那条链会拖进 httpx，离线用不着

ZERO = Decimal("0")
ONE = Decimal("1")


class PaperError(Exception):
    """纸上网关自身的配置/使用错误。注意它**不是**模拟出来的交易所错误——
    交易所错误一律以 PlacedOrder(status="rejected") 返回。"""


# ---- 盘口 ---------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class DepthProfile:
    """从「最优价 + 该档量」外推出整本梯子的模型。

    六家交易所的 REST 盘口接口都只给一档（fetch_book_top），但诚实的成交模拟
    需要整本梯子——否则"吃穿几档"这个最关键的成本项就没法算。所以第 1 档用真
    数据，2..N 档按本模型外推。

    默认值偏保守（每档只远 2bp、量增速 1.4），宁可高估滑点也不低估。
    """

    levels: int = 8
    step_bp: Decimal = Decimal("2")          # 每深一档远离最优价多少个基点
    qty_growth: Decimal = Decimal("1.4")     # 每深一档的量放大倍数


@dataclass(slots=True)
class Ladder:
    """一本盘口。bids 价格降序、asks 价格升序。"""

    bids: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    asks: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    ts_ms: int = 0
    synthetic_from: int = 0      # 从第几档起是模型外推的；0 表示整本都是模型

    def copy(self, *, ts_ms: int) -> "Ladder":
        return Ladder(list(self.bids), list(self.asks), ts_ms, self.synthetic_from)

    @property
    def best_bid(self) -> tuple[Decimal, Decimal]:
        return self.bids[0] if self.bids else (ZERO, ZERO)

    @property
    def best_ask(self) -> tuple[Decimal, Decimal]:
        return self.asks[0] if self.asks else (ZERO, ZERO)

    @property
    def mid(self) -> Decimal | None:
        b, a = self.best_bid[0], self.best_ask[0]
        if b <= ZERO or a <= ZERO:
            return None
        return (b + a) / 2


# ---- 记账 ---------------------------------------------------------------

@dataclass(slots=True)
class PaperPosition:
    market: str
    symbol: str
    qty: Decimal = ZERO              # 有符号：正=多、负=空
    avg_entry: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    funding: Decimal = ZERO          # 有符号：正=收到
    funding_ts: float | None = None  # 上次计提资金费的时刻


@dataclass(frozen=True, slots=True)
class PaperFill:
    ts: float
    market: str
    symbol: str
    side: str
    qty: Decimal                     # 绝对值
    price: Decimal                   # 本档成交价
    fee: Decimal
    reduce_only: bool
    client_order_id: str


@dataclass(slots=True)
class PaperOrder:
    """一笔下过的单。resolve_unknown 靠它把"超时"还原成真实成交量——
    这正是真实交易所 query_order 干的事。"""

    client_order_id: str
    ts: float
    market: str
    symbol: str
    side: str
    req_qty: Decimal
    price: Decimal | None
    tif: str
    reduce_only: bool
    filled: Decimal = ZERO
    avg_price: Decimal = ZERO
    fee: Decimal = ZERO
    status: str = "filled"
    error: str = ""
    levels_eaten: int = 0
    masked_unknown: bool = False     # 真实结果是否被"超时"故障遮住了


# ---- 故障注入 -----------------------------------------------------------

@dataclass(slots=True)
class _Faults:
    timeout_n: int = 0
    timeout_fills: bool = True       # 超时的那笔单在"交易所侧"是否真的成交了
    reject_n: int = 0
    reject_reason: str = ""
    resolve_fail_n: int = 0
    blind_n: int = 0
    freeze_until: float | None = None
    latency_ms: int = 0

    def any_active(self, now: float) -> bool:
        return bool(self.timeout_n or self.reject_n or self.resolve_fail_n
                    or self.blind_n or self.latency_ms
                    or (self.freeze_until is not None and self.freeze_until > now))


# ---- 报表 ---------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PaperLegReport:
    market: str
    symbol: str
    qty: Decimal
    avg_entry: Decimal
    mark: Decimal
    notional: Decimal
    unrealized: Decimal
    realized: Decimal
    fees: Decimal
    funding: Decimal
    fills: int


@dataclass(frozen=True, slots=True)
class PaperReport:
    """对账报表。equity 那一行是硬约束：
    cash + 浮盈 必须等于 初始资金 + 已实现 - 手续费 + 资金费 + 浮盈，
    对不上就是记账写错了。"""

    ts_ms: int
    initial_cash: Decimal
    cash: Decimal
    realized_pnl: Decimal
    fees: Decimal
    funding: Decimal
    unrealized: Decimal
    equity: Decimal
    net_exposure_usd: Decimal
    gross_exposure_usd: Decimal
    orders: int
    fills: int
    rejects: int
    unknowns: int
    legs: tuple[PaperLegReport, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts_ms": self.ts_ms,
            "initial_cash": str(self.initial_cash),
            "cash": str(self.cash),
            "realized_pnl": str(self.realized_pnl),
            "fees": str(self.fees),
            "funding": str(self.funding),
            "unrealized": str(self.unrealized),
            "equity": str(self.equity),
            "net_exposure_usd": str(self.net_exposure_usd),
            "gross_exposure_usd": str(self.gross_exposure_usd),
            "orders": self.orders, "fills": self.fills,
            "rejects": self.rejects, "unknowns": self.unknowns,
            "legs": [
                {
                    "market": leg.market, "symbol": leg.symbol,
                    "qty": str(leg.qty), "avg_entry": str(leg.avg_entry),
                    "mark": str(leg.mark), "notional": str(leg.notional),
                    "unrealized": str(leg.unrealized), "realized": str(leg.realized),
                    "fees": str(leg.fees), "funding": str(leg.funding),
                    "fills": leg.fills,
                }
                for leg in self.legs
            ],
        }

    def to_text(self) -> str:
        lines = [
            "纸上交易对账报表",
            "=" * 78,
            f"初始资金 {_m(self.initial_cash)}    现金 {_m(self.cash)}"
            f"    浮盈 {_m(self.unrealized)}    权益 {_m(self.equity)}",
            f"已实现 {_m(self.realized_pnl)}    手续费 {_m(-self.fees)}"
            f"    资金费 {_m(self.funding)}",
            f"净敞口 {_m(self.net_exposure_usd)}    毛敞口 {_m(self.gross_exposure_usd)}",
            f"订单 {self.orders}    成交 {self.fills}"
            f"    被拒 {self.rejects}    未知 {self.unknowns}",
            "-" * 78,
            f"{'市场':<16}{'符号':<14}{'持仓':>12}{'均价':>12}"
            f"{'浮盈':>12}{'已实现':>12}",
        ]
        for leg in self.legs:
            lines.append(
                f"{leg.market:<16}{leg.symbol:<14}{_n(leg.qty):>12}"
                f"{_n(leg.avg_entry):>12}{_m(leg.unrealized):>12}"
                f"{_m(leg.realized):>12}"
            )
        return "\n".join(lines)


def _m(x: Decimal) -> str:
    return f"{x.quantize(Decimal('0.0001')):,}"


def _n(x: Decimal) -> str:
    return f"{x.normalize():f}" if x else "0"


# ---- 网关 ---------------------------------------------------------------

class PaperGateway:
    """纸上撮合网关。结构上满足 `executor.OrderGateway`。

    线程模型：单事件循环内使用，不加锁。executor 本身也是单循环推进的。
    """

    def __init__(
        self, *,
        registry: "AdapterRegistry | None" = None,
        clock: Callable[[], float] = time.time,
        initial_cash: Decimal = Decimal("100000"),
        default_taker: Decimal = Decimal("0.0005"),
        default_maker: Decimal = Decimal("0.0002"),
        depth: DepthProfile = DepthProfile(),
        book_ttl_ms: int = 250,
        default_tick: Decimal = Decimal("0.01"),
        consume_depth: bool = False,
    ) -> None:
        self._registry = registry
        self._clock = clock
        self._depth = depth
        self._book_ttl_ms = book_ttl_ms
        self._default_tick = default_tick
        # 吃掉的深度是否从可见盘口里扣掉。默认关：真实行情每 TTL 会重新拉，
        # 扣了也马上被覆盖，反而让合成盘口的单测难写。需要模拟"自己把盘口吃穿"
        # 的场景时再打开。
        self._consume_depth = consume_depth

        self._initial_cash = initial_cash
        self._cash = initial_cash
        self._positions: dict[str, PaperPosition] = {}
        self._fills: list[PaperFill] = []
        self._orders: dict[str, PaperOrder] = {}
        self._order_seq: list[str] = []          # 保序，方便断言"第几笔单"

        self._source: dict[str, Ladder] = {}     # 数据源（适配器或注入）给的盘口
        self._visible: dict[str, Ladder] = {}    # 引擎看得见的盘口；冻结时不再同步
        self._fetched_at: dict[str, float] = {}
        self._ticks: dict[str, Decimal] = {}
        self._fees: dict[str, tuple[Decimal, Decimal]] = {}   # market -> (maker, taker)
        self._default_fees = (default_maker, default_taker)
        self._funding: dict[str, tuple[Decimal, int]] = {}    # key -> (rate, interval_s)
        self._health: dict[str, LegHealth] = {}
        self._faults: dict[str, _Faults] = {}

    # ---- 时间 -----------------------------------------------------------

    def now(self) -> float:
        return self._clock()

    def now_ms(self) -> int:
        return int(self._clock() * 1000)

    # ---- 配置 -----------------------------------------------------------

    @staticmethod
    def key(market: str, symbol: str) -> str:
        return f"{market}|{symbol}"

    def set_tick_size(self, market: str, symbol: str, tick: Decimal) -> None:
        self._ticks[self.key(market, symbol)] = tick

    def _tick(self, market: str, symbol: str) -> Decimal:
        return self._ticks.get(self.key(market, symbol), self._default_tick)

    def set_fees(self, market: str, *, maker: Decimal, taker: Decimal) -> None:
        """用真实费率档。用默认费率会系统性高估收益——这是原版工具的老毛病。"""
        self._fees[market] = (maker, taker)

    def taker_fee(self, market: str) -> Decimal:
        return self._fees.get(market, self._default_fees)[1]

    def set_funding_rate(self, market: str, symbol: str, rate: Decimal,
                         interval_s: int = 28800) -> None:
        """符号沿用交易所原始约定：正 = 多头付给空头。"""
        self._funding[self.key(market, symbol)] = (rate, int(interval_s))

    def set_leg_health(self, market: str, health: LegHealth,
                       symbol: str | None = None) -> None:
        """模拟合约下架 / 交易所只减仓 / 保证金不足。

        这是让 `repair_verdict` 的硬判据真的被命中的唯一办法——
        否则"补腿判死→撤退"整条路径永远走不到。
        """
        self._health[self.key(market, symbol) if symbol else market] = health

    def set_position(self, market: str, symbol: str, qty: Decimal,
                     avg_entry: Decimal) -> None:
        """直接塞一个持仓（跳过撮合）。只用于搭测试场景或从外部状态恢复。"""
        pos = self._pos(market, symbol)
        pos.qty = qty
        pos.avg_entry = avg_entry
        pos.funding_ts = self.now()

    # ---- 盘口注入 -------------------------------------------------------

    def set_ladder(self, market: str, symbol: str, *,
                   bids: Sequence[tuple[Decimal | str, Decimal | str]],
                   asks: Sequence[tuple[Decimal | str, Decimal | str]]) -> None:
        """注入一本完整梯子（离线模式）。这是唯一能精确控制"吃到第几档"的入口。"""
        lad = Ladder(
            bids=sorted(((_d(p), _d(q)) for p, q in bids), key=lambda x: -x[0]),
            asks=sorted(((_d(p), _d(q)) for p, q in asks), key=lambda x: x[0]),
            ts_ms=self.now_ms(),
            synthetic_from=1 << 30,     # 全部是给定的真实档位，没有外推
        )
        self._source[self.key(market, symbol)] = lad

    def set_book(self, market: str, symbol: str,
                 bid: Decimal | str, bid_qty: Decimal | str,
                 ask: Decimal | str, ask_qty: Decimal | str) -> None:
        """只给最优价，深档由 DepthProfile 外推——和接真实适配器时同一条路径。"""
        top = BookTop(_d(bid), _d(bid_qty), _d(ask), _d(ask_qty), self.now_ms())
        self._source[self.key(market, symbol)] = self._expand(top, self._tick(market, symbol))

    def _expand(self, top: BookTop, tick: Decimal) -> Ladder:
        p = self._depth
        bids: list[tuple[Decimal, Decimal]] = []
        asks: list[tuple[Decimal, Decimal]] = []
        for i in range(max(1, p.levels)):
            frac = Decimal(i) * p.step_bp / Decimal("10000")
            mult = p.qty_growth ** i
            ask_p = _to_tick(top.ask_price * (ONE + frac), tick, up=True)
            bid_p = _to_tick(top.bid_price * (ONE - frac), tick, up=False)
            # 步长小于一个 tick 时会撞档，强制单调，否则"逐档"就成了"同一档"
            if asks and ask_p <= asks[-1][0]:
                ask_p = asks[-1][0] + tick
            if bids and bid_p >= bids[-1][0]:
                bid_p = bids[-1][0] - tick
            if ask_p > ZERO:
                asks.append((ask_p, top.ask_qty * mult))
            if bid_p > ZERO:
                bids.append((bid_p, top.bid_qty * mult))
        return Ladder(bids, asks, top.ts_ms or self.now_ms(), synthetic_from=1)

    # ---- 盘口读取 -------------------------------------------------------

    async def ladder(self, market: str, symbol: str) -> Ladder:
        k = self.key(market, symbol)
        now = self.now()

        if self._is_frozen(market, symbol):
            # 冻结 = 行情通道死了。**连数据源的更新也不许渗进来**，
            # 否则测不出"拿着陈旧报价下单"这个最贵的坑。
            # 冻结前一次都没读过时没有可冻的快照，只能先正常读一次再冻住。
            vis = self._visible.get(k)
            if vis is not None:
                return vis

        src = self._source.get(k)
        stale = (now * 1000 - self._fetched_at.get(k, -1e18)) > self._book_ttl_ms
        if self._registry is not None and (src is None or stale):
            fetched = await self._fetch_from_adapter(market, symbol)
            if fetched is not None:
                src = fetched
                self._source[k] = src
                self._fetched_at[k] = now * 1000
        if src is None:
            raise PaperError(
                f"{market}:{symbol} 没有盘口：既没有已连接的适配器，"
                f"也没有用 set_ladder/set_book 注入合成行情"
            )
        vis = src.copy(ts_ms=self.now_ms())
        self._visible[k] = vis
        return vis

    async def _fetch_from_adapter(self, market: str, symbol: str) -> Ladder | None:
        try:
            adapter = self._registry.get(market)      # type: ignore[union-attr]
        except KeyError:
            return None
        try:
            top = await adapter.fetch_book_top(symbol)
        except Exception:
            # 行情拉不动就沿用上一份，让它自然变陈旧——由陈旧度闸门去判死，
            # 而不是在这里抛异常把执行器炸掉。
            return None
        return self._expand(top, self._tick(market, symbol))

    async def book_top(self, market: str, symbol: str) -> BookTop:
        lad = await self.ladder(market, symbol)
        bp, bq = lad.best_bid
        ap, aq = lad.best_ask
        return BookTop(bp, bq, ap, aq, lad.ts_ms)

    async def book(self, market: str, symbol: str
                   ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """OrderGateway 协议要求的四元组 (bid, bid_qty, ask, ask_qty)。"""
        lad = await self.ladder(market, symbol)
        bp, bq = lad.best_bid
        ap, aq = lad.best_ask
        return bp, bq, ap, aq

    def quote_age_ms(self, market: str, symbol: str) -> int:
        """当前可见盘口的陈旧度。没读过盘口时返回一个很大的数（当作无限旧）。"""
        vis = self._visible.get(self.key(market, symbol))
        if vis is None:
            return 1 << 40
        return max(0, self.now_ms() - vis.ts_ms)

    def is_quote_stale(self, market: str, symbol: str, max_age_ms: int) -> bool:
        return self.quote_age_ms(market, symbol) > max_age_ms

    def mark_price(self, market: str, symbol: str) -> Decimal | None:
        """标记价 = 可见盘口中价。没有可见盘口就退回数据源。"""
        k = self.key(market, symbol)
        for lad in (self._visible.get(k), self._source.get(k)):
            if lad is not None and lad.mid is not None:
                return lad.mid
        return None

    # ---- 故障注入 -------------------------------------------------------

    def _fault(self, key: str) -> _Faults:
        f = self._faults.get(key)
        if f is None:
            f = _Faults()
            self._faults[key] = f
        return f

    def _fault_chain(self, market: str, symbol: str) -> list[_Faults]:
        """先看 symbol 级、再看 market 级。两级独立计数、互不遮蔽。"""
        out = []
        for k in (self.key(market, symbol), market):
            f = self._faults.get(k)
            if f is not None:
                out.append(f)
        return out

    def inject_timeout(self, market: str, n: int = 1, *, symbol: str | None = None,
                       actually_fills: bool = True) -> None:
        """接下来 n 笔单返回未知状态（测 ORDER_UNKNOWN 消解）。

        actually_fills=True 时，订单在"交易所侧"**真的成交了**，只是回执丢了——
        这才是最危险也最该测的那一种。简单重发会直接变成双倍仓位；
        正确做法是 cancel 终结 → 查单 → 仓位差分，resolve_unknown 会还原真实成交量。
        """
        f = self._fault(self.key(market, symbol) if symbol else market)
        f.timeout_n = n
        f.timeout_fills = actually_fills

    def inject_reject(self, market: str, n: int = 1, reason: str = "订单被拒",
                      *, symbol: str | None = None) -> None:
        f = self._fault(self.key(market, symbol) if symbol else market)
        f.reject_n = n
        f.reject_reason = reason

    def inject_latency(self, ms: int, *, market: str | None = None,
                       symbol: str | None = None) -> None:
        """给下单/查单/查仓加延迟。不传 market 就是全局。

        延迟本身不改变成交结果，但它会拉长"下单在途"的窗口——
        执行器的 in-flight 锁和幂等 cid 是否真的挡住了重复下单，只有在这里才测得到。
        """
        key = "*" if market is None else (self.key(market, symbol) if symbol else market)
        self._fault(key).latency_ms = ms

    def inject_book_freeze(self, market: str, seconds: float, *,
                           symbol: str | None = None) -> None:
        """盘口在这段时间内不再更新（测报价陈旧度闸门）。

        冻结期间成交仍然按**冻结住的那本盘口**撮合——拿陈旧报价下单会怎么亏，
        这里就怎么亏，不做任何保护。
        """
        f = self._fault(self.key(market, symbol) if symbol else market)
        f.freeze_until = self.now() + seconds

    def inject_resolve_failure(self, market: str, n: int = 1, *,
                               symbol: str | None = None) -> None:
        """接下来 n 次 resolve_unknown 返回 None（交易所不可读 → 执行器该进 FROZEN）。"""
        self._fault(self.key(market, symbol) if symbol else market).resolve_fail_n = n

    def inject_position_blind(self, market: str, n: int = 1, *,
                              symbol: str | None = None) -> None:
        """接下来 n 次查仓抛异常。看不见仓位时下任何单都是最糟的选择。"""
        self._fault(self.key(market, symbol) if symbol else market).blind_n = n

    def clear_faults(self) -> None:
        self._faults.clear()

    def _is_frozen(self, market: str, symbol: str) -> bool:
        now = self.now()
        return any(f.freeze_until is not None and f.freeze_until > now
                   for f in self._fault_chain(market, symbol))

    def _consume(self, market: str, symbol: str, attr: str) -> _Faults | None:
        """消耗一次故障计数。返回命中的那条故障记录（便于读它的附带配置）。"""
        for f in self._fault_chain(market, symbol):
            n = getattr(f, attr)
            if n > 0:
                setattr(f, attr, n - 1)
                return f
        return None

    async def _latency(self, market: str, symbol: str) -> None:
        ms = 0
        for f in (*self._fault_chain(market, symbol), self._faults.get("*") or _Faults()):
            ms = max(ms, f.latency_ms)
        if ms > 0:
            await asyncio.sleep(ms / 1000)

    # ---- 撮合 -----------------------------------------------------------

    async def place(self, market: str, symbol: str, side: str, qty: Decimal,
                    price: Decimal | None, *, reduce_only: bool,
                    client_order_id: str, tif: str) -> PlacedOrder:
        await self._latency(market, symbol)
        side = side.lower()
        if side not in ("buy", "sell"):
            raise PaperError(f"非法方向: {side}")
        if client_order_id in self._orders:
            # 幂等：同一个 cid 重发返回原结果，不会撮合第二次。
            # 真实交易所也是这么做的，而这正是执行器防双倍仓位的最后一道保险。
            return self._as_placed(self._orders[client_order_id])

        order = PaperOrder(
            client_order_id=client_order_id, ts=self.now(), market=market,
            symbol=symbol, side=side, req_qty=qty, price=price, tif=tif.upper(),
            reduce_only=reduce_only,
        )
        self._orders[client_order_id] = order
        self._order_seq.append(client_order_id)

        rejected = self._consume(market, symbol, "reject_n")
        if rejected is not None:
            return self._finish(order, "rejected", rejected.reject_reason or "订单被拒")

        timed_out = self._consume(market, symbol, "timeout_n")
        if timed_out is not None and not timed_out.timeout_fills:
            order.masked_unknown = True
            order.status = "rejected"          # 交易所侧其实没收到，成交量确实是 0
            order.error = "请求超时，订单未被接受"
            return PlacedOrder(client_order_id, ZERO, ZERO, "unknown")

        result = await self._match(order)
        if timed_out is not None:
            # 交易所那边成交了，只是回执丢了。对上层只暴露 unknown——
            # 真实成交量要靠 resolve_unknown 去问，绝不能靠猜。
            order.masked_unknown = True
            return PlacedOrder(client_order_id, ZERO, ZERO, "unknown")
        return result

    async def _match(self, order: PaperOrder) -> PlacedOrder:
        market, symbol = order.market, order.symbol
        pos = self._pos(market, symbol)
        want = order.req_qty
        if want <= ZERO:
            return self._finish(order, "rejected", "下单量为 0")

        if order.reduce_only:
            # 仓位为 0 还下 reduce_only —— 币安 -2022，其余五家各有各的码，行为一致
            if pos.qty == ZERO:
                return self._finish(order, "rejected",
                                    "-2022 ReduceOnly Order is rejected: 当前无仓位")
            signed_want = want if order.side == "buy" else -want
            if (signed_want > ZERO) == (pos.qty > ZERO):
                return self._finish(order, "rejected",
                                    "-2022 ReduceOnly Order is rejected: 该方向会加仓")
            # 交易所会把 reduce_only 单截断到剩余仓位，不会让你平过头
            want = min(want, abs(pos.qty))

        lad = await self.ladder(market, symbol)
        levels = lad.asks if order.side == "buy" else lad.bids
        limit = order.price if order.tif != "MARKET" else None
        filled, notional, rest, eaten = _walk(levels, want, limit, order.side)

        if filled <= ZERO:
            # IOC / 市价单一口没吃到。四种状态里只有 "rejected" 能表达
            # "这笔单没有产生任何东西、可以安全重试"，撤退阶梯也靠它计失败次数。
            why = ("盘口无可成交深度" if not levels
                   else "IOC 限价未触及任何可成交档位")
            return self._finish(order, "rejected", why)

        avg = notional / filled
        fee = notional * self.taker_fee(market)
        signed = filled if order.side == "buy" else -filled
        realized = self._apply_position(pos, signed, avg)

        pos.fees += fee
        pos.realized_pnl += realized
        self._cash += realized - fee
        if pos.funding_ts is None:
            pos.funding_ts = self.now()

        self._fills.append(PaperFill(
            ts=self.now(), market=market, symbol=symbol, side=order.side,
            qty=filled, price=avg, fee=fee, reduce_only=order.reduce_only,
            client_order_id=order.client_order_id,
        ))
        if self._consume_depth:
            # 必须写回**数据源**：可见盘口每次读都从数据源重建，只改可见副本
            # 等于没改。数据源被下一次行情刷新（适配器 TTL 或 set_ladder）覆盖时
            # 深度自然补回来，这正是真实盘口的行为。
            src = self._source.get(self.key(market, symbol))
            for target in (lad, src):
                if target is None:
                    continue
                if order.side == "buy":
                    target.asks = list(rest)
                else:
                    target.bids = list(rest)

        order.filled = filled
        order.avg_price = avg
        order.fee = fee
        order.levels_eaten = eaten
        order.status = "filled" if filled >= order.req_qty else "partial"
        return self._as_placed(order)

    def _finish(self, order: PaperOrder, status: str, error: str) -> PlacedOrder:
        order.status = status
        order.error = error
        return self._as_placed(order)

    @staticmethod
    def _as_placed(order: PaperOrder) -> PlacedOrder:
        return PlacedOrder(order.client_order_id, order.filled, order.avg_price,
                           order.status, order.error)

    @staticmethod
    def _apply_position(pos: PaperPosition, signed_qty: Decimal,
                        price: Decimal) -> Decimal:
        """均价法更新持仓，返回本次实现盈亏。"""
        if pos.qty == ZERO or (pos.qty > ZERO) == (signed_qty > ZERO):
            new_qty = pos.qty + signed_qty
            pos.avg_entry = ((pos.avg_entry * abs(pos.qty) + price * abs(signed_qty))
                             / abs(new_qty))
            pos.qty = new_qty
            return ZERO
        closing = min(abs(signed_qty), abs(pos.qty))
        direction = ONE if pos.qty > ZERO else -ONE
        realized = closing * (price - pos.avg_entry) * direction
        pos.qty += signed_qty
        if pos.qty == ZERO:
            pos.avg_entry = ZERO
        elif (pos.qty > ZERO) != (direction > ZERO):
            pos.avg_entry = price          # 平过头翻了方向，剩下的按新价建仓
        return realized

    # ---- 协议其余部分 ---------------------------------------------------

    async def resolve_unknown(self, market: str, symbol: str,
                              client_order_id: str) -> Decimal | None:
        """消解未知订单。返回真实成交量；None = 交易所不可读。

        对应真实适配器的 `cancel → query → 仓位差分`：这里订单一旦下过就已终结，
        直接给出确定的成交量；从没见过的 cid 说明它从未被接受，返回 0。
        """
        await self._latency(market, symbol)
        if self._consume(market, symbol, "resolve_fail_n") is not None:
            return None
        order = self._orders.get(client_order_id)
        if order is None:
            return ZERO
        order.masked_unknown = False       # 已消解，不再算作未知
        return order.filled

    async def position_qty(self, market: str, symbol: str) -> Decimal:
        await self._latency(market, symbol)
        if self._consume(market, symbol, "blind_n") is not None:
            raise PaperError(f"{market}:{symbol} 查仓失败：账户接口不可读")
        return self._pos(market, symbol).qty

    async def leg_health(self, market: str, symbol: str) -> LegHealth:
        for k in (self.key(market, symbol), market):
            h = self._health.get(k)
            if h is not None:
                return h
        # 没显式设置就按纸上账户的真实权益给保证金，其余字段用宽松默认值
        return LegHealth(available_margin=float(self.equity()), required_margin=0.0)

    # ---- 资金费 ---------------------------------------------------------

    def accrue_funding(self, now: float | None = None) -> Decimal:
        """按持仓时长线性计提资金费，返回本次计提总额（有符号，正=收到）。

        符号约定同 models.CarryRate：正费率 = 多头付给空头。
        所以 现金变动 = -持仓量(有符号) × 标记价 × 费率 × 已过时间/结算周期。
        """
        now = self.now() if now is None else now
        total = ZERO
        for k, pos in self._positions.items():
            cfg = self._funding.get(k)
            last = pos.funding_ts
            pos.funding_ts = now
            if cfg is None or pos.qty == ZERO or last is None:
                continue
            dt = now - last
            if dt <= 0:
                continue
            rate, interval = cfg
            mark = self.mark_price(pos.market, pos.symbol)
            if mark is None:
                continue
            amount = (-pos.qty * mark * rate
                      * Decimal(str(dt)) / Decimal(interval))
            pos.funding += amount
            self._cash += amount
            total += amount
        return total

    # ---- 账目 -----------------------------------------------------------

    def _pos(self, market: str, symbol: str) -> PaperPosition:
        k = self.key(market, symbol)
        p = self._positions.get(k)
        if p is None:
            p = PaperPosition(market=market, symbol=symbol)
            self._positions[k] = p
        return p

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def orders(self) -> list[PaperOrder]:
        return [self._orders[c] for c in self._order_seq]

    @property
    def fills(self) -> list[PaperFill]:
        return list(self._fills)

    def positions(self) -> list[PaperPosition]:
        return list(self._positions.values())

    def unrealized(self) -> Decimal:
        total = ZERO
        for pos in self._positions.values():
            if pos.qty == ZERO:
                continue
            mark = self.mark_price(pos.market, pos.symbol)
            if mark is None:
                continue
            total += pos.qty * (mark - pos.avg_entry)
        return total

    def equity(self) -> Decimal:
        return self._cash + self.unrealized()

    def net_exposure_usd(self) -> Decimal:
        """所有腿的净敞口（有符号美元）。对冲做对了它就该在 0 附近。"""
        total = ZERO
        for pos in self._positions.values():
            mark = self.mark_price(pos.market, pos.symbol)
            if mark is not None:
                total += pos.qty * mark
        return total

    def gross_exposure_usd(self) -> Decimal:
        total = ZERO
        for pos in self._positions.values():
            mark = self.mark_price(pos.market, pos.symbol)
            if mark is not None:
                total += abs(pos.qty) * mark
        return total / 2

    def report(self) -> PaperReport:
        legs: list[PaperLegReport] = []
        fees = realized = funding = ZERO
        for k in sorted(self._positions):
            pos = self._positions[k]
            mark = self.mark_price(pos.market, pos.symbol) or ZERO
            unreal = pos.qty * (mark - pos.avg_entry) if pos.qty else ZERO
            legs.append(PaperLegReport(
                market=pos.market, symbol=pos.symbol, qty=pos.qty,
                avg_entry=pos.avg_entry, mark=mark, notional=pos.qty * mark,
                unrealized=unreal, realized=pos.realized_pnl, fees=pos.fees,
                funding=pos.funding,
                fills=sum(1 for f in self._fills
                          if f.market == pos.market and f.symbol == pos.symbol),
            ))
            fees += pos.fees
            realized += pos.realized_pnl
            funding += pos.funding
        orders = self.orders
        return PaperReport(
            ts_ms=self.now_ms(), initial_cash=self._initial_cash, cash=self._cash,
            realized_pnl=realized, fees=fees, funding=funding,
            unrealized=self.unrealized(), equity=self.equity(),
            net_exposure_usd=self.net_exposure_usd(),
            gross_exposure_usd=self.gross_exposure_usd(),
            orders=len(orders), fills=len(self._fills),
            rejects=sum(1 for o in orders if o.status == "rejected"),
            unknowns=sum(1 for o in orders if o.masked_unknown),
            legs=tuple(legs),
        )

    # ---- 持久化 ---------------------------------------------------------

    def save(self, path: str | Path) -> None:
        """整份状态快照写盘。纸上状态很小，直接全量覆盖，省掉增量同步的错法。

        故障注入**不落盘**：它是测试脚手架，不是账本的一部分，
        重启后还带着"下一笔单必超时"只会让人莫名其妙。
        """
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(p)
        try:
            conn.executescript(_PAPER_SCHEMA)
            with conn:
                conn.execute("DELETE FROM paper_state")
                conn.execute("DELETE FROM paper_positions")
                conn.execute("DELETE FROM paper_fills")
                conn.execute("DELETE FROM paper_orders")
                conn.execute("DELETE FROM paper_books")
                conn.executemany(
                    "INSERT INTO paper_state(key, value) VALUES(?,?)",
                    [("initial_cash", str(self._initial_cash)),
                     ("cash", str(self._cash)),
                     ("saved_at_ms", str(self.now_ms()))],
                )
                conn.executemany(
                    "INSERT INTO paper_positions(market, symbol, qty, avg_entry, "
                    "realized_pnl, fees, funding, funding_ts, funding_rate, "
                    "funding_interval_s) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    [
                        (pos.market, pos.symbol, str(pos.qty), str(pos.avg_entry),
                         str(pos.realized_pnl), str(pos.fees), str(pos.funding),
                         pos.funding_ts,
                         str(self._funding.get(k, (ZERO, 0))[0]),
                         int(self._funding.get(k, (ZERO, 0))[1]))
                        for k, pos in self._positions.items()
                    ],
                )
                conn.executemany(
                    "INSERT INTO paper_fills(ts, market, symbol, side, qty, price, "
                    "fee, reduce_only, client_order_id) VALUES(?,?,?,?,?,?,?,?,?)",
                    [(f.ts, f.market, f.symbol, f.side, str(f.qty), str(f.price),
                      str(f.fee), int(f.reduce_only), f.client_order_id)
                     for f in self._fills],
                )
                conn.executemany(
                    "INSERT INTO paper_orders(client_order_id, seq, ts, market, symbol, "
                    "side, req_qty, price, tif, reduce_only, filled, avg_price, fee, "
                    "status, error, levels_eaten, masked_unknown) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(o.client_order_id, i, o.ts, o.market, o.symbol, o.side,
                      str(o.req_qty), None if o.price is None else str(o.price),
                      o.tif, int(o.reduce_only), str(o.filled), str(o.avg_price),
                      str(o.fee), o.status, o.error, o.levels_eaten,
                      int(o.masked_unknown))
                     for i, o in enumerate(self.orders)],
                )
                conn.executemany(
                    "INSERT INTO paper_books(key, side, level, price, qty, ts_ms, "
                    "synthetic_from) VALUES(?,?,?,?,?,?,?)",
                    [(k, side, i, str(pr), str(q), lad.ts_ms, lad.synthetic_from)
                     for k, lad in self._source.items()
                     for side, rows in (("bid", lad.bids), ("ask", lad.asks))
                     for i, (pr, q) in enumerate(rows)],
                )
        finally:
            conn.close()

    def load(self, path: str | Path) -> None:
        """从盘上恢复。盘口按数据源恢复——重启后引擎看到的仍是"要重新拉一次"的状态。"""
        p = Path(path)
        if not p.exists():
            raise PaperError(f"纸上状态文件不存在: {p}")
        conn = sqlite3.connect(p)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(_PAPER_SCHEMA)
            state = {r["key"]: r["value"]
                     for r in conn.execute("SELECT key, value FROM paper_state")}
            self._initial_cash = Decimal(state.get("initial_cash", str(self._initial_cash)))
            self._cash = Decimal(state.get("cash", str(self._cash)))

            self._positions.clear()
            for r in conn.execute("SELECT * FROM paper_positions"):
                k = self.key(r["market"], r["symbol"])
                self._positions[k] = PaperPosition(
                    market=r["market"], symbol=r["symbol"], qty=Decimal(r["qty"]),
                    avg_entry=Decimal(r["avg_entry"]),
                    realized_pnl=Decimal(r["realized_pnl"]), fees=Decimal(r["fees"]),
                    funding=Decimal(r["funding"]), funding_ts=r["funding_ts"],
                )
                if int(r["funding_interval_s"] or 0) > 0:
                    self._funding[k] = (Decimal(r["funding_rate"]),
                                        int(r["funding_interval_s"]))

            self._fills = [
                PaperFill(ts=r["ts"], market=r["market"], symbol=r["symbol"],
                          side=r["side"], qty=Decimal(r["qty"]),
                          price=Decimal(r["price"]), fee=Decimal(r["fee"]),
                          reduce_only=bool(r["reduce_only"]),
                          client_order_id=r["client_order_id"])
                for r in conn.execute("SELECT * FROM paper_fills ORDER BY id")
            ]

            self._orders.clear()
            self._order_seq.clear()
            for r in conn.execute("SELECT * FROM paper_orders ORDER BY seq"):
                o = PaperOrder(
                    client_order_id=r["client_order_id"], ts=r["ts"],
                    market=r["market"], symbol=r["symbol"], side=r["side"],
                    req_qty=Decimal(r["req_qty"]),
                    price=None if r["price"] is None else Decimal(r["price"]),
                    tif=r["tif"], reduce_only=bool(r["reduce_only"]),
                    filled=Decimal(r["filled"]), avg_price=Decimal(r["avg_price"]),
                    fee=Decimal(r["fee"]), status=r["status"], error=r["error"],
                    levels_eaten=r["levels_eaten"],
                    masked_unknown=bool(r["masked_unknown"]),
                )
                self._orders[o.client_order_id] = o
                self._order_seq.append(o.client_order_id)

            self._source.clear()
            self._visible.clear()
            self._fetched_at.clear()
            books: dict[str, Ladder] = {}
            for r in conn.execute("SELECT * FROM paper_books ORDER BY key, side, level"):
                lad = books.setdefault(
                    r["key"], Ladder(ts_ms=r["ts_ms"],
                                     synthetic_from=r["synthetic_from"]))
                (lad.bids if r["side"] == "bid" else lad.asks).append(
                    (Decimal(r["price"]), Decimal(r["qty"]))
                )
            self._source.update(books)
        finally:
            conn.close()

    @classmethod
    def restore(cls, path: str | Path, **kwargs: Any) -> "PaperGateway":
        gw = cls(**kwargs)
        gw.load(path)
        return gw


_PAPER_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_positions (
    market             TEXT NOT NULL,
    symbol             TEXT NOT NULL,
    qty                TEXT NOT NULL,
    avg_entry          TEXT NOT NULL,
    realized_pnl       TEXT NOT NULL,
    fees               TEXT NOT NULL,
    funding            TEXT NOT NULL,
    funding_ts         REAL,
    funding_rate       TEXT NOT NULL DEFAULT '0',
    funding_interval_s INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (market, symbol)
);

CREATE TABLE IF NOT EXISTS paper_fills (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               REAL NOT NULL,
    market           TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL,
    qty              TEXT NOT NULL,
    price            TEXT NOT NULL,
    fee              TEXT NOT NULL,
    reduce_only      INTEGER NOT NULL,
    client_order_id  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_orders (
    client_order_id TEXT PRIMARY KEY,
    seq             INTEGER NOT NULL,
    ts              REAL NOT NULL,
    market          TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    req_qty         TEXT NOT NULL,
    price           TEXT,
    tif             TEXT NOT NULL,
    reduce_only     INTEGER NOT NULL,
    filled          TEXT NOT NULL,
    avg_price       TEXT NOT NULL,
    fee             TEXT NOT NULL,
    status          TEXT NOT NULL,
    error           TEXT NOT NULL,
    levels_eaten    INTEGER NOT NULL,
    masked_unknown  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_books (
    key            TEXT NOT NULL,
    side           TEXT NOT NULL,
    level          INTEGER NOT NULL,
    price          TEXT NOT NULL,
    qty            TEXT NOT NULL,
    ts_ms          INTEGER NOT NULL,
    synthetic_from INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key, side, level)
);
"""


# ---- 撮合内核 -----------------------------------------------------------

def _walk(levels: Sequence[tuple[Decimal, Decimal]], want: Decimal,
          limit: Decimal | None, side: str
          ) -> tuple[Decimal, Decimal, list[tuple[Decimal, Decimal]], int]:
    """逐档吃。返回 (成交量, 成交额, 剩余梯子, 吃到的档数)。

    这是整个模拟器最不能偷懒的地方：拿中间价假装成交，滑点这项最大的成本
    就直接消失了，跑出来的净年化全是幻觉。
    """
    filled = ZERO
    notional = ZERO
    rest: list[tuple[Decimal, Decimal]] = []
    eaten = 0
    for price, qty in levels:
        reachable = (limit is None
                     or (price <= limit if side == "buy" else price >= limit))
        if filled >= want or not reachable or qty <= ZERO:
            rest.append((price, qty))
            continue
        take = min(qty, want - filled)
        filled += take
        notional += take * price
        eaten += 1
        if qty > take:
            rest.append((price, qty - take))
    return filled, notional, rest, eaten


def _to_tick(price: Decimal, tick: Decimal, *, up: bool) -> Decimal:
    if tick <= ZERO:
        return price
    n = (price / tick).to_integral_value(
        rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return n * tick


def _d(x: Decimal | str | int) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))
