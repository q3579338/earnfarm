"""视觉语言与格式化。

三条贯穿全站的约定（来自对 taoli.tools 的调研，这几条是它做对的地方）：

1. **多腿=蓝、空腿=橙**，贯穿列表、图表、面板。用户不用读文字就知道在看哪条腿。
2. **等宽字体**，数字列天然对齐——做金融表格几乎是必选。
3. **颜色只有四档语义**：灰=可忽略、绿=安全、黄=注意、红=危险。不多加颜色。

但不抄它的短板：它用 ⟳ ⨯ ◧ ◫ 这类字符当按钮，对新用户毫无可发现性，
文档得用一整页解释每个符号。我们保留密度，但按钮带 tooltip。
"""

from __future__ import annotations

import math
from decimal import Decimal

# 两条腿的颜色约定。白底下要压深一档，否则细字号读不清。
LONG_COLOR = "#1d4ed8"      # 蓝
SHORT_COLOR = "#c2410c"     # 橙

# 四档语义色
NEUTRAL = "#71717a"
SAFE = "#15803d"
WARN = "#a16207"
DANGER = "#b91c1c"

# 判决 → 颜色与标签
# sized_down 用蓝色不用黄色：它不是"要小心"，而是"照建议仓位做就行"，
# 小币的高费率往往就是真肉，只是容量小。
VERDICT_STYLE = {
    "good": (SAFE, "可做"),
    "sized_down": (LONG_COLOR, "缩仓位"),
    "marginal": (WARN, "勉强"),
    "negative": (DANGER, "负期望"),
    "unexecutable": (NEUTRAL, "做不了"),
}

# 报价陈旧度阈值（秒）。这两个数是 taoli.tools 一年半运营验证过的，直接沿用。
# 跨所套利最隐蔽的杀手是"一边 WS 断了但价格还挂在屏幕上"——
# 拿陈旧价算出的差价是幻觉，照着下单必单腿必亏。
STALE_WARN_S = 3.0
STALE_DANGER_S = 9.0
# 超过这个秒数直接拒绝下单，不只是变个颜色
STALE_BLOCK_S = 3.0

GLOBAL_CSS = """
:root { --cf-long: %(long)s; --cf-short: %(short)s; }
body, .q-page-container { background: #ffffff; color: #18181b; }
.cf-mono { font-family: 'Fira Code', 'Cascadia Code', Consolas, monospace;
           font-variant-numeric: tabular-nums; }
.cf-long  { color: var(--cf-long); }
.cf-short { color: var(--cf-short); }
.cf-dense .q-table td, .cf-dense .q-table th { padding: 4px 8px; font-size: 12px; }
.cf-row { border-bottom: 1px solid #f4f4f5; }
.cf-row:hover { background: #fafafa; }
.cf-row-negative { opacity: 0.6; }
.cf-row-unexecutable { opacity: 0.45; }
.cf-reason { font-size: 11px; color: %(neutral)s; }
""" % {"long": LONG_COLOR, "short": SHORT_COLOR, "neutral": NEUTRAL}


def staleness_color(age_s: float) -> str:
    """报价陈旧度分级。"""
    if age_s >= STALE_DANGER_S:
        return DANGER
    if age_s >= STALE_WARN_S:
        return WARN
    return NEUTRAL


def is_quote_blocked(age_s: float) -> bool:
    return age_s >= STALE_BLOCK_S


def pct(value: float | Decimal | None, digits: int = 2, signed: bool = False) -> str:
    if value is None:
        return "—"
    v = float(value) * 100
    sign = "+" if signed and v > 0 else ""
    return f"{sign}{v:.{digits}f}%"


def apr(value: float | Decimal | None, digits: int = 1) -> str:
    """年化。超过 1000% 一律显示 >999%——那个量级的精确值没有意义，
    只会让用户误以为很精确。"""
    if value is None:
        return "—"
    v = float(value) * 100
    if v > 999:
        return ">999%"
    if v < -999:
        return "<-999%"
    return f"{v:.{digits}f}%"


def usd(value: float | Decimal | None, digits: int = 0) -> str:
    if value is None:
        return "—"
    v = float(value)
    for threshold, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= threshold:
            return f"${v / threshold:,.1f}{suffix}"
    return f"${v:,.{digits}f}"


def hours(value: float | None) -> str:
    """回本时间。用户要的是"多久"，不是"几期"——两腿周期不同时"期"没有意义。"""
    if value is None:
        return "—"
    if math.isinf(value):
        return "永不"
    if value < 1:
        return f"{value * 60:.0f}分"
    if value < 48:
        return f"{value:.0f}小时"
    return f"{value / 24:.1f}天"


def interval_label(seconds: int) -> str:
    """结算周期。必须显示出来——1h 的 0.05% 和 8h 的 0.05% 差 8 倍，
    不标出来用户会系统性选错币。"""
    h = seconds / 3600
    return f"{h:.0f}h" if h == int(h) else f"{h:.1f}h"


def leg_label(market: str, symbol: str) -> str:
    """市场+符号的紧凑显示，如 binance:perp BTCUSDT → BN·perp BTCUSDT"""
    abbrev = {
        "binance": "BN", "okx": "OKX", "htx": "HTX",
        "gate": "GATE", "bybit": "BYB", "bitget": "BG",
    }
    venue, _, kind = market.partition(":")
    return f"{abbrev.get(venue, venue.upper())}·{kind} {symbol}"
