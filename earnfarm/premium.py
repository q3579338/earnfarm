"""双市场上市股票的永续溢价检测。

背景：同一家公司在两个市场上市（如 SK 海力士 = 纳斯达克 ADR + 韩交所普通股），
币安把两条腿都做成了 USDⓈ-M 永续。当两地间的股份转换通道受阻（外汇管制、
存托份额上限）时，两腿之间会出现**持续不归零的溢价**——这是一个可交易的
价差标的，而不是数据错误。

口径说明（重要）：这里算的是**两个永续之间**的价差，不是两地现货的真溢价。
每个永续自带一点基差，所以和"ADR 现货 vs 韩股现货"口径能差 1~2 个百分点；
但对在币安上实际下单的人来说，永续口径才是能成交的价格。

这层不 import nicegui——和 public_feed 同一条纪律：数据层要能在守护模式里跑。
"""

from __future__ import annotations

import os
import asyncio
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

# 可用 EARNFARM_FAPI_BASE 顶掉：币安对受限地区的 IP 直接返回 451，
# 部署在那类机器上必须走自建反代/Worker 转发
FAPI_BASE = os.environ.get("EARNFARM_FAPI_BASE", "https://fapi.binance.com").rstrip("/")

# 参考区间默认回看的 1h K 线根数。505 根 ≈ 三周：SKHYNIXUSDT 2026-07-20 才上线，
# 默认拉更长只会拿到零填充；页面上可改，上限压在 fapi 单请求 1500 根以内
DEFAULT_REF_HOURS = 505
MIN_REF_HOURS = 24
MAX_REF_HOURS = 1440

# 两地现货的交易时段（UTC，工作日）。**只标常规时段，不管节假日**——
# 这个标签的用途是提醒"溢价的锚现在冻着还是活着"，标错一个假日的代价
# 只是那天的标签不准，为它去维护两国交易日历不值得。
# 注意 KRX 15:30 KST 收盘 = 06:30 UTC，别按整点记
KRX_OPEN_UTC = (0 * 60, 6 * 60 + 30)        # 00:00–06:30
US_OPEN_UTC = (13 * 60 + 30, 20 * 60)       # 13:30–20:00（夏令时口径）


@dataclass(frozen=True)
class PremiumPair:
    """一对双市场腿。ratio = 多少份 ADR 拼成 1 股本地股。

    理论价（本地股换算到 ADR 单位）= local_price / ratio。
    加新标的只需要在 DUAL_PAIRS 里添一行——前提是两条腿都是**同一公司的
    普通股/ADR**。杠杆 ETF（如 CSOP 2x 系列）不能进这个表：每日再平衡的
    损耗会让"溢价"永远算不平，那不是溢价是损耗。
    """
    key: str                    # 页面锚点用的短名
    name: str                   # 显示名
    adr_symbol: str             # 币安永续：ADR 腿
    local_symbol: str           # 币安永续：本地股腿
    ratio: float                # N 份 ADR = 1 股本地股
    adr_label: str              # 腿的人话标注
    local_label: str
    note: str = ""              # 溢价成因一句话，显示在卡片底部


DUAL_PAIRS: tuple[PremiumPair, ...] = (
    PremiumPair(
        key="skhy",
        name="SK 海力士",
        adr_symbol="SKHYUSDT",
        local_symbol="SKHYNIXUSDT",
        ratio=10.0,
        # 标注"币安永续"不是啰嗦：这两个数字长得就像交易所现货报价，
        # 不写清楚会有人拿它当纳斯达克/韩交所的官方价去对账
        adr_label="纳斯达克 ADR · 币安永续",
        local_label="韩交所普通股 · 币安永续",
        note="1 ADS = 1/10 股。韩国外汇管制堵住转换通道，溢价长期 30%+ 不归零；"
             "历史上溢价收敛主要靠韩股腿补涨，不是 ADR 回落。",
    ),
    # 台积电只有 ADR 腿（TSMUSDT），台股 2330 腿币安没上——上了就能加进来。
    # 三星只有韩股腿 + 港股 2x 杠杆 ETF，后者进不了这个表（见 ratio 的说明）。
)


@dataclass
class LegQuote:
    """一条腿的即时状态。全部来自公开端点，不需要 key。"""
    symbol: str
    price: float
    change_24h_pct: float
    quote_volume_24h: float
    funding_rate: float | None = None       # 上一期资金费率
    next_funding_ts: float | None = None    # 下次结算（epoch 秒）


@dataclass
class RefStats:
    """参考区间：两腿 1h 收盘价对齐后算出的溢价分布。"""
    n: int
    mean: float
    median: float
    lo: float
    hi: float
    percentile: float           # 当前溢价在分布中的分位（0~100）
    first_ts: float
    last_ts: float


@dataclass
class PremiumSnapshot:
    pair: PremiumPair
    adr: LegQuote
    local: LegQuote
    fair: float                 # 本地股 / ratio
    premium_pct: float          # (ADR/fair - 1) * 100
    ref: RefStats | None        # K 线拉不到时为 None，页面照常显示即时值
    # 对齐后的历史序列 (ts_秒, ADR收, 理论价收, 溢价%)，给回测曲线用。
    # 和 ref 同源同轮请求——图和分位数如果各拉各的，两者会各说各话
    series: tuple[tuple[float, float, float, float], ...] = ()
    fetched_at: float = field(default_factory=time.time)
    errors: tuple[str, ...] = ()


def compute_premium(adr_price: float, local_price: float, ratio: float) -> tuple[float, float]:
    """返回 (理论价, 溢价%)。单独拆出来是为了让测试不用碰网络。"""
    fair = local_price / ratio
    return fair, (adr_price / fair - 1.0) * 100.0


def ladder(local_price: float, ratio: float,
           levels: tuple[float, ...]) -> list[tuple[float, float]]:
    """溢价梯子：本地腿不动时，各溢价水平对应的 ADR 价格。

    只是参照系，不是预测——历史上溢价回归主要靠本地腿动，
    这张表回答的是"如果只有 ADR 动，要到哪"。
    """
    fair = local_price / ratio
    return [(lv, fair * (1 + lv / 100.0)) for lv in levels]


def ladder_levels(ref: RefStats | None, current: float) -> tuple[float, ...]:
    """梯子的档位从参考分布里取，而不是写死：换一个标的（或换一个月），
    写死的 30/35/40 可能整段落在分布外，表就白给了。"""
    if ref is None:
        base = {round(current), round(current) - 2, round(current) + 2}
    else:
        base = {round(ref.lo, 1), round(ref.median, 1), round(ref.mean, 1),
                round(ref.hi, 1), round(current, 1)}
    return tuple(sorted(base, reverse=True))


def session_status(now_ts: float | None = None) -> tuple[bool, bool, str]:
    """(韩股开着吗, 美股开着吗, 人话)。

    这个标签存在的原因：两地现货都收盘时，两个永续都拴在冻结的指数价上，
    这段时间里溢价的变动基本是噪音；只有现货开着，溢价的变动才有信息量。
    """
    dt = datetime.fromtimestamp(now_ts or time.time(), timezone.utc)
    minutes = dt.hour * 60 + dt.minute
    weekday = dt.weekday() < 5
    krx = weekday and KRX_OPEN_UTC[0] <= minutes < KRX_OPEN_UTC[1]
    us = weekday and US_OPEN_UTC[0] <= minutes < US_OPEN_UTC[1]
    if krx:
        text = "韩股盘中——本地腿的锚是活的"
    elif us:
        text = "美股盘中——ADR 腿的锚是活的"
    else:
        text = "两地现货均休市——溢价此刻的变动多为噪音"
    return krx, us, text


def align_series(adr_closes: dict[int, float], local_closes: dict[int, float],
                 ratio: float) -> list[tuple[float, float, float, float]]:
    """两腿 K 线按**开盘时间戳**对齐成 (ts_秒, ADR, 理论价, 溢价%) 序列。

    只用两边都有的小时——一边停机/缺根时直接丢掉那一小时，
    拿单腿旧价配另一腿新价算出来的"溢价"是幻觉。
    """
    out = []
    for t in sorted(adr_closes.keys() & local_closes.keys()):
        a = adr_closes[t]
        fair = local_closes[t] / ratio
        out.append((t / 1000.0, a, fair, (a / fair - 1.0) * 100.0))
    return out


def make_ref_stats(adr_closes: dict[int, float], local_closes: dict[int, float],
                   ratio: float, current_premium: float) -> RefStats | None:
    """对齐后的溢价分布统计。样本不足一天就返回 None，别硬给统计量。"""
    aligned = align_series(adr_closes, local_closes, ratio)
    if len(aligned) < 24:
        return None
    prems = [row[3] for row in aligned]
    below = sum(1 for p in prems if p < current_premium)
    return RefStats(
        n=len(prems),
        mean=statistics.mean(prems),
        median=statistics.median(prems),
        lo=min(prems),
        hi=max(prems),
        percentile=below / len(prems) * 100.0,
        first_ts=aligned[0][0],
        last_ts=aligned[-1][0],
    )


async def fetch_snapshot(pair: PremiumPair,
                         client: httpx.AsyncClient | None = None,
                         ref_hours: int = DEFAULT_REF_HOURS) -> PremiumSnapshot:
    """拉一对腿的完整快照。全部是 fapi 公开端点。

    六个请求并发（2×24hr ticker + 2×premiumIndex + 2×klines），
    K 线失败不挡即时溢价——参考区间是锦上添花，即时价才是主菜。
    ref_hours 夹在 [MIN, MAX] 里：低于一天统计量没意义，高于 1440 会
    超出 fapi 单请求上限、还得翻页——这页不值得为它引入翻页复杂度。
    """
    ref_hours = max(MIN_REF_HOURS, min(MAX_REF_HOURS, int(ref_hours)))
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(base_url=FAPI_BASE, timeout=10.0)
    errors: list[str] = []
    try:
        async def get(path: str, **params):
            resp = await client.get(path, params=params)
            resp.raise_for_status()
            return resp.json()

        results = await asyncio.gather(
            get("/fapi/v1/ticker/24hr", symbol=pair.adr_symbol),
            get("/fapi/v1/ticker/24hr", symbol=pair.local_symbol),
            get("/fapi/v1/premiumIndex", symbol=pair.adr_symbol),
            get("/fapi/v1/premiumIndex", symbol=pair.local_symbol),
            get("/fapi/v1/klines", symbol=pair.adr_symbol,
                interval="1h", limit=ref_hours),
            get("/fapi/v1/klines", symbol=pair.local_symbol,
                interval="1h", limit=ref_hours),
            return_exceptions=True,
        )
        t_adr, t_loc, f_adr, f_loc, k_adr, k_loc = results

        # 两个 ticker 是硬依赖：拿不到价格整个快照就不成立
        for r, sym in ((t_adr, pair.adr_symbol), (t_loc, pair.local_symbol)):
            if isinstance(r, Exception):
                raise RuntimeError(f"{sym} 行情拉取失败：{r}") from r

        def leg(ticker, funding) -> LegQuote:
            q = LegQuote(
                symbol=ticker["symbol"],
                price=float(ticker["lastPrice"]),
                change_24h_pct=float(ticker["priceChangePercent"]),
                quote_volume_24h=float(ticker["quoteVolume"]),
            )
            if isinstance(funding, Exception):
                errors.append(f"{q.symbol} 资金费拉取失败：{funding}")
            else:
                q.funding_rate = float(funding["lastFundingRate"])
                q.next_funding_ts = float(funding["nextFundingTime"]) / 1000.0
            return q

        adr = leg(t_adr, f_adr)
        local = leg(t_loc, f_loc)
        fair, premium = compute_premium(adr.price, local.price, pair.ratio)

        ref = None
        series: tuple = ()
        if isinstance(k_adr, Exception) or isinstance(k_loc, Exception):
            bad = k_adr if isinstance(k_adr, Exception) else k_loc
            errors.append(f"K线拉取失败，参考区间缺席：{bad}")
        else:
            adr_closes = {int(k[0]): float(k[4]) for k in k_adr}
            loc_closes = {int(k[0]): float(k[4]) for k in k_loc}
            series = tuple(align_series(adr_closes, loc_closes, pair.ratio))
            ref = make_ref_stats(adr_closes, loc_closes, pair.ratio, premium)

        return PremiumSnapshot(pair=pair, adr=adr, local=local, fair=fair,
                               premium_pct=premium, ref=ref, series=series,
                               errors=tuple(errors))
    finally:
        if own_client:
            await client.aclose()


async def fetch_all(pairs: tuple[PremiumPair, ...] = DUAL_PAIRS,
                    ref_hours: int = DEFAULT_REF_HOURS
                    ) -> list[PremiumSnapshot | Exception]:
    """全部配对并发拉，单对失败不连坐——和机会榜的六家并发同一条纪律。"""
    async with httpx.AsyncClient(base_url=FAPI_BASE, timeout=10.0) as client:
        return list(await asyncio.gather(
            *(fetch_snapshot(p, client, ref_hours=ref_hours) for p in pairs),
            return_exceptions=True,
        ))
