"""公开行情源：不用任何 API key，直接从六家所拉真实资金费率和盘口。

资金费率、合约规格、盘口深度全是公开端点，不需要签名。这意味着：
用户装上就能看到**此刻真实的机会**，不用先去申请 key、不用信任这个软件。

只有下单、查持仓、查自己的费率档位才需要 key。费率档位取不到时用默认 taker
兜底，并在界面上标明"按默认费率估算"——用真实档位算出来的净年化才准。
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, Sequence

from .exchanges.base import Credential, ExchangeAdapter
from .history import HistoryBackfiller
from .models import FeeSchedule, Venue
from .scoring import (
    FILL_TAKER,
    HistoryStats,
    LegQuote,
    ScoredOpportunity,
    rank,
    score_pair,
)


class FeeSource(Protocol):
    """能给出用户真实费率档位的东西。Session 天然满足这个协议。

    用协议而不是直接依赖 Session：机会榜在没解锁、没配任何账户时也必须能跑，
    真实档位是**可选依赖**，就像历史资金费一样。
    """

    def fee_for(self, market: str, symbol: str) -> FeeSchedule | None:
        """None 表示没有该品种的真实档位，调用方必须退回默认挂牌价并标注出来。"""
        ...


# 默认 maker 费率（VIP0）。目前只在展示层有意义——roundtrip_cost 全走 taker，
# 因为两腿对冲的开平都得吃单，挂单等成交会让另一条腿裸奔。
DEFAULT_MAKER = 0.0002

# 各家的默认 taker 费率（VIP0）。取不到用户真实档位时的兜底值。
# 宁可高估成本也别低估——低估会让负期望的机会看起来能做。
DEFAULT_TAKER = {
    Venue.BINANCE: 0.0005,
    Venue.OKX: 0.0005,
    Venue.HTX: 0.0005,
    Venue.GATE: 0.0005,
    Venue.BYBIT: 0.00055,
    Venue.BITGET: 0.0006,
    # feeSchedule.cross 基础档（cross=taker，别读成全仓）。HL 挂牌价比 CEX 低，
    # 不加这条会兜到 .get 的 6bp 默认值，把这家的净年化系统性压低 1.5bp/腿
    Venue.HYPERLIQUID: 0.00045,
    # contracts/active 的 takerFeeRate（XBTUSDTM 实测 6.0E-4）
    Venue.KUCOIN: 0.0006,
}

# 深度统计带宽。各家盘口接口返回的档位数不同，统一按这个带宽估可见深度。
DEPTH_BAND_BP = 10.0
# 一档名义额低于此值的对直接跳过——连几百刀都吃不下，占一行都是浪费
MIN_TOP_NOTIONAL = 500.0
# 单次最多吃掉一档的这个比例。用来把"用户想放多少"折算成"实际能放多少"
MAX_PARTICIPATION_OF_TOP = 0.5

# 主流币：无论费率差多小都保证出现在榜上。
# 理由：按费率差排序会系统性地只选中垃圾小币（费率极端往往正因为盘子小），
# 而用户最可能实际下手的恰恰是这些深度充足的币——哪怕年化只有 15%，
# 它能放 50 万，绝对收益经常比年化 300% 但只能放 2 万的小币高。
MAJORS = (
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK",
    "TON", "TRX", "DOT", "SUI", "APT", "ARB", "OP", "LTC", "BCH",
    "NEAR", "ATOM", "FIL", "ETC", "UNI", "AAVE", "INJ", "SEI", "TIA",
    "PEPE", "WIF", "SHIB", "HYPE", "ENA", "ONDO",
)


# 稳定性评分默认回看多少天。30 天在 168 小时（一周）的持有期下约 4 个独立窗口，
# 刚够 stability() 摆脱先验；再长的话 OKX（93天）和 Bitget（270条）先到顶，
# 拉回来的只是各家墙的位置差异，不是更多信息。
DEFAULT_HISTORY_DAYS = 30


def _basis_of(real_legs: int) -> str:
    """两条腿里有几条用了真实档位 → 这一行的成本口径。

    区分 partial 是必要的：跨所对冲的成本是两家相加，一条腿准另一条腿是挂牌价，
    算出来的净年化仍然不能当真，但比两条都是估算多一分把握。
    """
    return ("real" if real_legs >= 2 else
            "partial" if real_legs == 1 else "default")


def history_days_for(horizon_h: float) -> int:
    """按用户的持有期决定回看多久。

    stability() 少于 horizon+12 小时就直接退回先验，所以"计划持有 1 月"配上
    固定 30 天的回看，机会榜整列永远是"数据不足"——一个窗口都凑不出来。
    按**三个窗口**取，再压一个 30 天的下限：一个窗口得不出统计，
    三个也不多，但至少 is_prior 不会因为参数配错而恒真。
    各家的回溯墙（OKX 93 天、Bitget 270 条）由 HistoryBackfiller 自己夹。

    放在这里而不是界面层：守护模式（watch.py）要用同一条规则，
    而它绝不能为了一个算天数的函数去 import nicegui。
    """
    return max(DEFAULT_HISTORY_DAYS, int(math.ceil(horizon_h * 3 / 24)))


def _adapter_class(venue: Venue):
    from .session import _adapter_class as resolve
    return resolve(venue)


def open_public_adapters(venues: Sequence[Venue] | None = None
                         ) -> tuple[dict[Venue, ExchangeAdapter], dict[Venue, str]]:
    """建一组无凭据的适配器（公开端点不签名，空凭据即可）。

    返回 (适配器, 构造失败的原因)。某家构造失败不影响其他家，但要记下来——
    静默跳过会让人以为是这家没机会，实际是根本没连上。
    """
    adapters: dict[Venue, ExchangeAdapter] = {}
    errors: dict[Venue, str] = {}
    for venue in tuple(venues or tuple(Venue)):
        try:
            adapters[venue] = _adapter_class(venue)(Credential("", ""), broker_code="")
        except Exception as exc:
            errors[venue] = str(exc)
    return adapters, errors


@dataclass(slots=True)
class RawFunding:
    venue: Venue
    symbol: str
    base: str
    rate: Decimal
    interval_h: float
    next_ts: float
    cap: float | None = None
    floor: float | None = None


# 币名别名。KuCoin 沿用老派的 XBT 指代 BTC；放在归一化的最后一步做映射，
# 别的家不用 XBT，全局替换是安全的
_BASE_ALIASES = {"XBT": "BTC"}


def normalize_base(symbol: str, venue: Venue) -> str:
    """把各家五花八门的符号归一成基础币名，好做跨所配对。

    这一步最容易出错的是**千倍前缀**：币安的 1000PEPE、Gate 的 PEPE、
    HTX 的 1000PEPE，指的是同一个币但面值差 1000 倍。
    这里只负责认出它们是同一个 base；面值换算由 Instrument.contract_size 承担，
    两边都做对了才不会出现 1000 倍仓位错配。

    归一错的代价是**整家静默消失**：每个符号自成 bucket、配不上任何对，
    适配器连得上、费率拉得到、界面照常显示"已连"，就是一行机会都不出。
    KuCoin 的 XBTUSDTM 曾经就是这样——结尾多一个 M，后缀表全部落空。
    """
    raw = symbol
    # Hyperliquid 的千倍前缀是**小写 k**（kPEPE/kBONK），必须在 upper() 之前剥：
    # 大写之后 kPEPE 和真实以 K 开头的币再也分不开，而 KAVA→AVA 不是配不上对，
    # 是**配错对**——AVA（Travala）是别家真实存在的另一个币，两条腿会是
    # 完全不同的资产，等于双向裸奔。判据只有原始大小写：小写 k + 全大写基名。
    if (venue is Venue.HYPERLIQUID and len(raw) > 1
            and raw[0] == "k" and raw[1:].isupper()):
        raw = raw[1:]
    s = raw.upper()
    # KuCoin 合约一律带尾缀 M（XBTUSDTM / XBTUSDM）。这个 M 只能按 venue 剥，
    # 塞进下面六家共用的后缀表等于给别家开新的误剥口子
    if venue is Venue.KUCOIN and len(s) > 1 and s.endswith("M"):
        s = s[:-1]
    # 必须循环剥：OKX 的 BTC-USDT-SWAP 要连剥两次（-SWAP 再 -USDT），
    # 只剥一次会得到 "BTC-USDT"，跟别家的 "BTC" 配不上对——
    # 结果就是 OKX 的币全部无法参与跨所配对。
    suffixes = ("-SWAP", "_UMCBL", "-PERP", "_PERP",
                "_USDT", "-USDT", "USDT", "_USD", "-USD", "USD")
    changed = True
    while changed:
        changed = False
        for sep in suffixes:
            if s.endswith(sep) and len(s) > len(sep):
                s = s[: -len(sep)]
                changed = True
                break
    s = s.strip("-_")
    for prefix in ("1000000", "100000", "10000", "1000", "1M", "1K"):
        if s.startswith(prefix) and len(s) > len(prefix):
            s = s[len(prefix):]
            break
    return _BASE_ALIASES.get(s, s)


class PublicFeed:
    """无凭据的公开数据源。

    历史资金费是**可选依赖**：传了 backfiller 就从库里读真实历史算稳定性，
    没传（或库里还没数据）就退回"拿当前费率当单点历史"。退化路径不是凑合，
    是刻意的——单点会让 stability() 返回 is_prior=True、界面标"数据不足"，
    比拿一个点伪装成统计诚实得多。

    注意这里**只读库、不联网取历史**：机会榜的首屏加载不能被回填拖住。
    回填是上层的事（backfill_targets → HistoryBackfiller.backfill_many → rescore）。

    用户的真实费率档位（fee_source）同样是**可选依赖**：连了账户就用你的 VIP 档
    算成本，没连就用各家 VIP0 挂牌价，并把这个事实通过 ScoredOpportunity.fee_basis
    和 feed 上的 fee_basis 汇总量交给界面标注。
    """

    def __init__(self, venues: Sequence[Venue] | None = None, *,
                 backfiller: HistoryBackfiller | None = None,
                 history_days: int = DEFAULT_HISTORY_DAYS,
                 fee_source: FeeSource | None = None) -> None:
        self.venues = tuple(venues or tuple(Venue))
        self._adapters: dict[Venue, ExchangeAdapter] = {}
        self._instruments: dict[Venue, dict] = {}
        self.backfiller = backfiller
        self.history_days = history_days
        self.init_errors: dict[Venue, str] = {}
        # 上一轮拉取里哪几家真的给了数据、哪几家报了错。
        # 守护模式必须能区分"这家没机会"和"这家根本没连上"——无人值守时
        # 一家静默掉线会让机会榜少掉一半的配对，而榜单看起来完全正常。
        self.fetch_errors: dict[Venue, str] = {}
        self.venues_with_data: tuple[Venue, ...] = ()
        # 有多少条腿真的读到了历史 / 一共查了多少条。上层据此在状态栏说人话
        self.history_hits = 0
        self.history_misses = 0
        # 用户真实费率档位的来源。None = 全用各家 VIP0 挂牌价估算
        self.fee_source = fee_source
        # 有多少条腿用上了真实档位。界面据此决定要不要在成本列打提示标记
        self.fee_hits = 0
        self.fee_misses = 0

    @property
    def fee_basis(self) -> str:
        """整张榜的成本口径：real / partial / default。

        状态栏用它说人话。**绝不能不说**——用默认挂牌价算出来的机会榜，
        对 VIP3 用户来说判决可能整片是反的，而他从界面上完全看不出来。
        """
        if self.fee_hits and not self.fee_misses:
            return "real"
        if self.fee_hits:
            return "partial"
        return "default"

    async def __aenter__(self) -> PublicFeed:
        self._adapters, self.init_errors = open_public_adapters(self.venues)
        return self

    async def __aexit__(self, *exc) -> None:
        await asyncio.gather(*(a.close() for a in self._adapters.values()),
                             return_exceptions=True)

    # ---- 拉取 -----------------------------------------------------------

    async def fetch_funding(self) -> dict[Venue, list[RawFunding]]:
        """并发拉六家的全市场资金费率。某家挂了不影响其他家。"""
        async def one(venue: Venue, adapter: ExchangeAdapter):
            rates = await adapter.fetch_carry_rates()
            out = []
            for r in rates:
                out.append(RawFunding(
                    venue=venue, symbol=r.symbol,
                    base=normalize_base(r.symbol, venue),
                    rate=r.rate,
                    interval_h=r.interval_s / 3600 if r.interval_s else 8.0,
                    next_ts=r.next_settle_ms / 1000 if r.next_settle_ms else time.time() + 3600,
                ))
            return venue, out

        # 按 items() 的顺序 zip 回去：gather 的异常项里没有 venue 信息，
        # 不配对的话只知道"有一家挂了"，不知道是哪一家——那等于没记
        items = list(self._adapters.items())
        results = await asyncio.gather(*(one(v, a) for v, a in items),
                                       return_exceptions=True)
        out: dict[Venue, list[RawFunding]] = {}
        errors: dict[Venue, str] = {}
        for (venue, _), item in zip(items, results):
            if isinstance(item, BaseException):
                errors[venue] = str(item) or type(item).__name__
                continue
            _, rows = item
            out[venue] = rows
        self.fetch_errors = errors
        self.venues_with_data = tuple(out)
        return out

    async def fetch_instruments(self, venue: Venue) -> dict:
        """合约规格。缓存起来——它变化很慢，但每 5 分钟要刷一次：
        交易所会动态调整最大杠杆和价格过滤器。"""
        if venue in self._instruments:
            return self._instruments[venue]
        adapter = self._adapters.get(venue)
        if adapter is None:
            return {}
        try:
            items = await adapter.fetch_instruments()
        except Exception:
            return {}
        table = {i.symbol: i for i in items}
        self._instruments[venue] = table
        return table

    async def fetch_book(self, venue: Venue, symbol: str):
        adapter = self._adapters.get(venue)
        if adapter is None:
            return None
        try:
            return await adapter.fetch_book_top(symbol)
        except Exception:
            return None

    async def fetch_depth(self, venue: Venue, symbol: str,
                          band_bp: float = DEPTH_BAND_BP):
        """带宽内的多档深度。取不到就退回一档——只是容量会被低估，
        不至于让整个机会消失。"""
        adapter = self._adapters.get(venue)
        if adapter is None:
            return None
        try:
            return await adapter.fetch_book_depth(symbol, band_bp)
        except Exception:
            return None

    # ---- 历史 -----------------------------------------------------------

    def _history_for(self, long_leg: LegQuote, short_leg: LegQuote
                     ) -> tuple[HistoryStats, HistoryStats, list[float] | None]:
        """两条腿的历史统计 + 对齐后的小时 carry 序列。

        **拿不到历史时优雅退化成单点**，而不是退化成空序列。区别很大：
        空的 HistoryStats.median() 返回 0，等于向 AR(1) 断言"这个费率的长期
        均值就是 0"，费率会被一路衰减到零——那不是"没有历史"，是一个凭空
        捏造的悲观结论。单点则让 median = 当前值，行为与接历史之前完全一致。

        carry 返回 None 表示没有可用的小时序列，stability() 会走
        is_prior=True 的保守分支，界面上标"数据不足"。
        """
        fallback_l = HistoryStats((long_leg.funding_now,), long_leg.funding_interval_h)
        fallback_s = HistoryStats((short_leg.funding_now,), short_leg.funding_interval_h)
        bf = self.backfiller
        if bf is None:
            self.history_misses += 2
            return fallback_l, fallback_s, None

        days = self.history_days
        try:
            hist_l = bf.stats_for(long_leg.market, long_leg.symbol, days)
            hist_s = bf.stats_for(short_leg.market, short_leg.symbol, days)
            carry = bf.hourly_carry(long_leg.market, long_leg.symbol,
                                    short_leg.market, short_leg.symbol, days)
        except Exception:
            # 读库失败（库被锁、文件损坏）不该让整个机会榜空掉
            self.history_misses += 2
            return fallback_l, fallback_s, None

        for hist in (hist_l, hist_s):
            if hist.rates:
                self.history_hits += 1
            else:
                self.history_misses += 1
        return (hist_l if hist_l.rates else fallback_l,
                hist_s if hist_s.rates else fallback_s,
                carry or None)

    def rescore(self, rows: Sequence[ScoredOpportunity], *,
                horizon_h: float, now_ts: float | None = None,
                fill_mode: str | None = None) -> list[ScoredOpportunity]:
        """拿库里**现在**的历史重新给已有榜单打分，不重拉任何行情。

        这是"先出榜、历史后台补、补完再刷新评分"的最后一步。复用原来的
        LegQuote 和评分仓位（o.notional 是按深度折算过的可行仓位，不是用户
        填的那个数），所以两次评分之间唯一变的就是历史——用户看到哪一行的
        稳定性从"数据不足"变成了真实评分，一目了然。

        费率口径也原样带过来（费率就在 LegQuote 里，这里一个字都没改），
        丢掉它会让重新评分之后整榜的成本列突然变回"按默认费率估算"。
        """
        now = now_ts if now_ts is not None else time.time()
        self.history_hits = self.history_misses = 0
        out: list[ScoredOpportunity] = []
        for o in rows:
            hist_l, hist_s, carry = self._history_for(o.long, o.short)
            # o.long / o.short 已经是定向过的（long.hourly_rate <= short.hourly_rate），
            # score_pair 内部再 orient 一次会得到同样的顺序，不会把两腿调换
            out.append(score_pair(o.base, o.long, o.short, hist_l, hist_s,
                                  notional=o.notional, horizon_h=horizon_h,
                                  now_ts=now, hourly_carry=carry,
                                  fee_basis=o.fee_basis,
                                  # None = 保持这一行原来的口径。丢掉它会让
                                  # 历史补完后的那次重评悄悄退回双边吃单
                                  fill_mode=fill_mode or o.fill_mode))
        return rank(out)

    # ---- 配对与评分 -----------------------------------------------------

    def _fee_for_leg(self, market: str, symbol: str, venue: Venue,
                     defaults: dict[Venue, float]) -> tuple[float, float, bool]:
        """这条腿用哪套费率。返回 (taker, maker, 是否是用户的真实档位)。

        真实档位取不到就退回该家的 VIP0 挂牌价，**并把这个事实带出去**。
        默默用默认值假装精确是原版工具的病：成本占了判决的绝对主导，
        VIP3 用户的 taker 可能只有挂牌价的一半，两者算出来的判决经常相反。

        taker ≤ 0 一律不认。零 taker 会让四笔往返成本整个消失、负期望的机会
        全翻成正期望，而它几乎只可能来自解析错误——真有零费率的做市商账户，
        这里高估他的成本也只是让机会看起来差一点，方向是安全的那一边。
        """
        source = self.fee_source
        if source is not None:
            try:
                real = source.fee_for(market, symbol)
            except Exception:
                real = None       # 费率源炸了不该让整个机会榜空掉
            if real is not None and real.taker > 0:
                return float(real.taker), float(real.maker), True
        return defaults.get(venue, 0.0006), DEFAULT_MAKER, False

    async def build_opportunities(self, *, notional: float = 50_000,
                                  horizon_h: float = 72,
                                  top_n: int = 40,
                                  fee_overrides: dict[Venue, float] | None = None,
                                  fee_source: FeeSource | None = None,
                                  fill_mode: str = FILL_TAKER
                                  ) -> list[ScoredOpportunity]:
        """拉真实数据 → 跨所配对 → 评分 → 排序。

        配对逻辑：同一个 base 币在两家所都有永续，就是一个候选对。
        小时化费率低的一边做多、高的一边做空（定向由 scoring.orient 负责）。

        费率优先级：`fee_source` 给的用户真实档位 > `fee_overrides` 手工覆盖 >
        DEFAULT_TAKER 挂牌价。每一行都会带上 `fee_basis`（real/partial/default）
        告诉界面这一行的成本到底是按谁的费率算的，feed 上还有
        `fee_hits/fee_misses/fee_basis` 三个汇总量给状态栏用。
        """
        if fee_source is not None:
            self.fee_source = fee_source
        fees = {**DEFAULT_TAKER, **(fee_overrides or {})}
        self.history_hits = self.history_misses = 0
        self.fee_hits = self.fee_misses = 0
        by_venue = await self.fetch_funding()

        # 按 base 归并：{base: [(venue, RawFunding), ...]}
        buckets: dict[str, list[RawFunding]] = defaultdict(list)
        for venue, rows in by_venue.items():
            for r in rows:
                buckets[r.base].append(r)

        # 只有在两家以上出现的 base 才可能构成跨所对冲
        candidates = {b: rs for b, rs in buckets.items() if len(rs) >= 2}

        # 先按"最大小时化费率差"粗排，只对最有希望的若干个去拉盘口——
        # 盘口是逐个符号的请求，全拉会打爆限频
        def spread_of(rows: list[RawFunding]) -> float:
            hourly = [float(r.rate) / r.interval_h for r in rows if r.interval_h > 0]
            return (max(hourly) - min(hourly)) if len(hourly) >= 2 else 0.0

        # 候选 = 主流币（无条件） + 费率差最大的若干个。
        # 只按费率差排会系统性只剩垃圾小币——费率极端往往正因为盘子小，
        # 这正是原版工具的病。主流币哪怕费率差很小也必须给用户看到，
        # 因为它们能承载的资金量大一两个数量级。
        by_spread = sorted(candidates.items(), key=lambda kv: -spread_of(kv[1]))
        majors = [(b, rs) for b, rs in candidates.items() if b in MAJORS]
        majors.sort(key=lambda kv: -spread_of(kv[1]))
        seen_bases = {b for b, _ in majors}
        ranked_bases = majors + [(b, rs) for b, rs in by_spread
                                 if b not in seen_bases][:top_n * 4]

        now = time.time()
        scored: list[ScoredOpportunity] = []
        for base, rows in ranked_bases:
            if len(scored) >= top_n:
                break
            hourly = sorted(rows, key=lambda r: float(r.rate) / max(r.interval_h, 1e-9))
            long_raw, short_raw = hourly[0], hourly[-1]
            if long_raw.venue is short_raw.venue:
                continue

            # 拉多档深度。只用一档会把容量低估一两个数量级，
            # 把真实可做的机会误判成"做不了"。
            depths = await asyncio.gather(
                self.fetch_depth(long_raw.venue, long_raw.symbol),
                self.fetch_depth(short_raw.venue, short_raw.symbol),
                return_exceptions=True)
            depths = [d if not isinstance(d, Exception) else None for d in depths]

            if all(d is not None for d in depths):
                long_book, short_book = (d.to_book_top() for d in depths)
                # 对冲要两个方向都吃得下，所以按两侧较薄的一边算
                sides = [float(d.thinner_side) for d in depths]
            else:
                # 深度取不到就退回一档——容量会偏低，但不至于让机会消失
                books = await asyncio.gather(
                    self.fetch_book(long_raw.venue, long_raw.symbol),
                    self.fetch_book(short_raw.venue, short_raw.symbol),
                    return_exceptions=True)
                if any(isinstance(b, Exception) or b is None for b in books):
                    continue
                long_book, short_book = books
                sides = [min(float(b.bid_qty) * float(b.bid_price),
                             float(b.ask_qty) * float(b.ask_price))
                         for b in (long_book, short_book)]

            # 深度为 0 不能当成"没限制"——scoring 里 depth_cap<=0 会跳过容量判断，
            # 等于按用户填的仓位放行。真读到 0 就是真没盘口，直接跳过。
            if any(s <= 0 for s in sides):
                continue
            thinnest = min(sides)
            # 主流币无论如何都留下——它可能费率差很小、判"勉强"，
            # 但那是给用户的信息，不该由我替他删掉
            if thinnest < MIN_TOP_NOTIONAL and base not in MAJORS:
                continue

            # **按你实际放得进去的仓位评分**，而不是用户填的名义值。
            # 拿 5 万去评一个容量 70 刀的机会，算出来的负年化毫无信息量——
            # 那不是"这个机会不好"，是"你想放的钱太多"。
            feasible = min(notional, thinnest * MAX_PARTICIPATION_OF_TOP)

            legs = []
            real_legs = 0
            for raw, book, side_depth in ((long_raw, long_book, sides[0]),
                                          (short_raw, short_book, sides[1])):
                market = f"{raw.venue.value}:perp"
                taker, maker, is_real = self._fee_for_leg(
                    market, raw.symbol, raw.venue, fees)
                real_legs += is_real
                legs.append(LegQuote(
                    symbol=raw.symbol, market=market,
                    bid=float(book.bid_price), ask=float(book.ask_price),
                    bid_size=float(book.bid_qty), ask_size=float(book.ask_qty),
                    depth_notional=side_depth,
                    depth_band_bp=DEPTH_BAND_BP,
                    taker_fee=taker, maker_fee=maker,
                    funding_now=float(raw.rate),
                    funding_interval_h=raw.interval_h,
                    next_funding_ts=raw.next_ts,
                    funding_cap=raw.cap, funding_floor=raw.floor,
                    # 未平仓额和 24h 成交额需要额外的行情端点，还没接。
                    # 留 0 表示"未知"，不要拿深度冒充——那会让上层的流动性过滤
                    # 拿万级的深度去比百万级的门槛，把能做的机会全误杀。
                    open_interest_usd=0.0, volume_24h_usd=0.0,
                ))
            long_leg, short_leg = legs
            self.fee_hits += real_legs
            self.fee_misses += 2 - real_legs

            # 只读库，不联网——回填是慢操作，绝不能挂在首屏加载的路径上。
            # 库里没有该品种时退回单点：stability 因样本不足走保守先验，
            # 界面标"数据不足"。等后台回填完，上层调 rescore() 再刷一次。
            hist_l, hist_s, carry = self._history_for(long_leg, short_leg)

            scored.append(score_pair(base, long_leg, short_leg, hist_l, hist_s,
                                     notional=feasible, horizon_h=horizon_h,
                                     now_ts=now, hourly_carry=carry,
                                     fee_basis=_basis_of(real_legs),
                                     fill_mode=fill_mode))
        return rank(scored)


def backfill_targets(rows: Sequence[ScoredOpportunity]) -> list[tuple[str, str]]:
    """榜上所有机会的两条腿，去重后的 (market, symbol) 回填清单。

    **只回填榜上出现过的品种，不做全市场扫描。** 全市场是六家 3600+ 个永续，
    光币安 573 个就会在 22 秒内打满它 500次/5分钟 的专用桶（见 history.DEFAULT_PACE_S），
    而榜单一共才几十行——用户看得见的那几行才需要历史。
    """
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for o in rows:
        for leg in (o.long, o.short):
            key = (leg.market, leg.symbol)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out
