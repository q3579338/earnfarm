"""告警引擎测试。

告警系统的失败方式和交易系统不一样：它不会亏钱，它会让用户**关掉通知**，
然后在真出事的那一刻，谁也没看见。所以这里测的重点是三件事：

  1. 去重对不对（推多了 = 被关掉；漏推 = 白做）
  2. 恶化能不能突破静默（这是唯一不能压的情形）
  3. 一个通道挂了会不会带塌其他通道（尤其本地日志）

外加一条硬性检查：源码里不许出现任何真实密钥。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from dataclasses import replace
from decimal import Decimal

import pytest

from carryfarm import alerts as alerts_mod
from carryfarm.alerts import (
    DEFAULT_RULES,
    DETECT_INTERVAL_S,
    Alert,
    AlertEngine,
    Deduper,
    DetectionClock,
    EventType,
    LogChannel,
    Severity,
    StaleMonitor,
    TelegramChannel,
    Verdict,
    WeChatChannel,
    WebhookChannel,
    actionable_opportunities,
    apr_bucket,
    breaker_alert,
    build_channels,
    naked_leg_alert,
    opportunity_alert,
    redact,
    resolve_secret,
    risk_alert,
    rules_from_config,
    stale_alert,
    unwind_alert,
    webhook_payload,
)
from carryfarm.config import (
    AlertsConfig,
    TelegramAlertConfig,
    WeChatAlertConfig,
    WebhookAlertConfig,
)
from carryfarm.safety import (
    BreakerDecision,
    BreakerLevel,
    ExposureSnapshot,
    LegFilters,
    RiskTier,
    UnwindPhase,
    classify_exposure,
)
from carryfarm.scoring import HistoryStats, LegQuote, score_pair

NOW = 1_785_000_000.0

# 测试里用的全是明显的假值。真 token 一律走环境变量，见 test_no_secrets_*
FAKE_TOKEN = "test-token-not-real"
FAKE_CHAT = "test-chat-id"


# ---- 脚手架 -------------------------------------------------------------

class FakeClock:
    """可手动推进的时钟。静默期测试不可能真的等 4 小时。"""

    def __init__(self, t: float = NOW) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


class RecordingChannel:
    """记下收到的告警。可以配置成必挂 / 挂起不返回。"""

    def __init__(self, name: str = "rec", *, fail: bool = False,
                 hang: bool = False, always_on: bool = False) -> None:
        self.name = name
        self.always_on = always_on
        self.received: list[Alert] = []
        self.fail = fail
        self.hang = hang

    async def send(self, alert: Alert) -> None:
        if self.hang:
            await asyncio.sleep(3600)      # 永不返回，模拟 TCP 连上但不响应
        if self.fail:
            raise RuntimeError(f"{self.name} 故意失败")
        self.received.append(alert)


class FakePoster:
    """假的 HTTP POST。告警测试绝不该真的发网络请求。"""

    def __init__(self, reply: dict | None = None, *, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict, dict]] = []
        self.reply = reply or {}
        self.raises = raises

    async def __call__(self, url: str, payload: dict, headers: dict) -> dict:
        self.calls.append((url, payload, headers))
        if self.raises is not None:
            raise self.raises
        return self.reply


def make_alert(event_type: EventType = EventType.RISK, *, subject: str = "acct",
               severity: Severity = Severity.WARN, bucket: str = "",
               ts: float = NOW) -> Alert:
    return Alert(type=event_type, subject=subject, severity=severity,
                 title="标题", body=("正文",), action="做点什么",
                 bucket=bucket, ts=ts)


def make_leg(symbol="BTCUSDT", market="binance:perp", price=100_000.0,
             funding=0.0, interval_h=8.0, taker=0.0005, depth=5_000_000.0):
    half = price * 1.0 / 1e4 / 2
    bid, ask = price - half, price + half
    return LegQuote(
        symbol=symbol, market=market, bid=bid, ask=ask,
        bid_size=500_000.0 / bid, ask_size=500_000.0 / ask,
        depth_notional=depth, depth_band_bp=10.0,
        taker_fee=taker, maker_fee=0.0002,
        funding_now=funding, funding_interval_h=interval_h,
        next_funding_ts=NOW + 3600, funding_cap=0.0075, funding_floor=-0.0075,
    )


def make_opportunity(net_funding=0.002, base="BTC"):
    """造一个净年化为正的机会。费差给得夸张是为了越过成本线。"""
    long = make_leg(symbol="BTCUSDT", market="binance:perp", funding=-net_funding)
    short = make_leg(symbol="BTC-USDT-SWAP", market="okx:perp", funding=net_funding)
    hist_l = HistoryStats(tuple([-net_funding] * 90), 8.0)
    hist_s = HistoryStats(tuple([net_funding] * 90), 8.0)
    carry = [2 * net_funding / 8] * 400
    return score_pair(base, long, short, hist_l, hist_s, notional=50_000.0,
                      horizon_h=72.0, now_ts=NOW, hourly_carry=carry)


def exposure(level_naked_usd: float) -> ExposureSnapshot:
    filt = LegFilters(step_size=Decimal("0.001"), min_qty=Decimal("0.001"),
                      min_notional=Decimal("5"), contract_size=Decimal("0.001"))
    qty = Decimal(str(level_naked_usd / 100))
    return classify_exposure(Decimal("100") + qty, Decimal("-100"), Decimal("100"),
                             100_000.0, filt, filt)


def run(coro):
    return asyncio.run(coro)


# ---- 事件构造：六类事件都要能说清"该做什么" ------------------------------

def test_all_six_event_types_have_builders():
    """六类事件缺一不可——少一类就等于那类故障永远不会喊人。"""
    built = {
        opportunity_alert(make_opportunity()).type,
        naked_leg_alert("h1", "u1", exposure(600), exposed_for_s=12).type,
        unwind_alert("h1", "u1", UnwindPhase.MARKET, "补腿判死").type,
        breaker_alert(BreakerDecision(BreakerLevel.HARD, "权益骤降")).type,
        stale_alert("binance:perp", 400.0).type,
        risk_alert("acct", RiskTier.RED, margin_ratio=1.5, liq_distance=0.08).type,
    }
    assert built == set(EventType)


@pytest.mark.parametrize("alert", [
    opportunity_alert(make_opportunity()),
    naked_leg_alert("hedge-abcdef12", "u007", exposure(600), exposed_for_s=12,
                    easy_market="okx:perp"),
    unwind_alert("hedge-abcdef12", "u007", UnwindPhase.CROSS_HEDGE, "市价连败"),
    breaker_alert(BreakerDecision(BreakerLevel.SOFT, "连接降级")),
    stale_alert("gate:perp", 900.0),
    risk_alert("binance-main", RiskTier.ORANGE, margin_ratio=2.1, liq_distance=0.12),
])
def test_alert_text_is_complete(alert):
    """格式化后必须信息完整：说了是什么、有多严重、以及**该做什么**。

    只说"XX 异常"的推送，用户第二次就不看了。
    """
    text = alert.as_text()
    assert alert.title in text
    assert "该做什么" in text
    assert alert.action and len(alert.action) >= 8
    assert alert.action in text
    for line in alert.body:
        assert line in text
    # 严重度和事件类型都要出现在标题行里，手机通知栏只看得到第一行
    assert alert.severity.zh in text.splitlines()[0]


def test_opportunity_alert_carries_size_and_lifespan():
    """机会告警必须回答两个问题：能放多少钱、能活多久。

    只报年化的推送等于让用户再去开电脑查一遍，那还不如不推。
    """
    op = make_opportunity()
    a = opportunity_alert(op)
    text = a.as_text()
    assert f"{op.net_apr:.1%}" in text
    assert f"${op.suggested_notional:,.0f}" in text
    assert "回本" in text and "存续" in text
    assert a.data["net_apr"] == op.net_apr


def test_naked_leg_severity_tracks_exposure_level():
    """裸腿的严重度必须跟着 ExposureLevel 走——去重里"恶化突破静默"
    整条通路都建立在这个映射上。"""
    soft = naked_leg_alert("h", "u", exposure(200), exposed_for_s=3)
    hard = naked_leg_alert("h", "u", exposure(900), exposed_for_s=25)
    crit = naked_leg_alert("h", "u", exposure(5000), exposed_for_s=40)
    assert soft.severity is Severity.WARN
    assert hard.severity is Severity.ERROR
    assert crit.severity is Severity.CRITICAL
    assert soft.severity < hard.severity < crit.severity


def test_breaker_alert_distinguishes_soft_and_hard():
    """SOFT 只禁开新仓（会自动恢复），HARD 要人工 ack。
    文案必须说清楚，否则用户以为程序死了。"""
    soft = breaker_alert(BreakerDecision(BreakerLevel.SOFT, "连接降级"))
    hard = breaker_alert(BreakerDecision(BreakerLevel.HARD, "对账发现无法解释的持仓"))
    assert soft.severity is Severity.ERROR and hard.severity is Severity.CRITICAL
    assert "自动解除" in soft.action
    assert "保命动作" in hard.action or "撤退" in hard.action


def test_stale_alert_escalates_when_much_older():
    """陈旧超阈值 3 倍升级为 ERROR：那时几乎可以确定是断线，
    而不是冷门币本来就不动。升级同时也会突破静默期。"""
    mild = stale_alert("gate:perp", 400.0, threshold_s=300.0)
    bad = stale_alert("gate:perp", 1200.0, threshold_s=300.0)
    assert mild.severity is Severity.WARN
    assert bad.severity is Severity.ERROR


def test_risk_alert_maps_every_tier():
    tiers = {t: risk_alert("a", t).severity for t in RiskTier}
    assert tiers[RiskTier.GREEN] is Severity.INFO
    assert tiers[RiskTier.RED] is Severity.ERROR
    assert tiers[RiskTier.BLACK] is Severity.CRITICAL
    # 单调不减：档位越高严重度不能反而降
    ordered = [tiers[t] for t in sorted(RiskTier)]
    assert ordered == sorted(ordered)


def test_alert_dict_is_json_serializable():
    """webhook 载荷必须能直接 json.dumps——里面混进 Decimal/Enum 会在
    真出事的那一刻抛异常，而那正是最不能失败的时刻。"""
    a = opportunity_alert(make_opportunity())
    blob = json.dumps(a.as_dict(), ensure_ascii=False)
    parsed = json.loads(blob)
    assert parsed["type"] == "opportunity"
    assert parsed["severity"] == "info"
    assert parsed["action"] and parsed["body"]


# ---- 去重 ---------------------------------------------------------------

def test_same_alert_twice_is_suppressed():
    """同一个机会反复出现不能反复推——这是告警系统被关掉的头号原因。"""
    d = Deduper()
    a = make_alert(EventType.OPPORTUNITY, subject="BTC")
    assert d.verdict(a, NOW) is Verdict.NEW
    d.record(a, Verdict.NEW, NOW)
    assert d.verdict(a, NOW + 60) is Verdict.SUPPRESSED


def test_silence_window_expires():
    """静默期过了、事情还在，要再推一次——"还没解决"本身就是信息。"""
    d = Deduper()
    a = make_alert(EventType.RISK, subject="acct")
    d.record(a, d.verdict(a, NOW), NOW)
    silence = DEFAULT_RULES[EventType.RISK].silence_s
    # 静默期内每 30 秒出现一次，都要被压住，但要刷新 last_seen
    t = NOW
    while t < NOW + silence - 30:
        t += 30
        v = d.verdict(a, t)
        assert v is Verdict.SUPPRESSED
        d.record(a, v, t)
    assert d.verdict(a, NOW + silence + 1) is Verdict.RENEWED


def test_escalation_breaks_silence():
    """**最关键的一条**：SOFT→HARD 必须立刻推，不能因为 10 分钟前推过就压住。

    这条压错了，等于把最该看见的那一条精确地吃掉。

    这里按真实节奏跑：裸腿每 2 秒检测一次，所以这 10 分钟里它一直被看见、
    一直被压住（不是"消失了又回来"），静默期是实打实生效着的。
    """
    d = Deduper()
    soft = naked_leg_alert("hedge1", "u1", exposure(200), exposed_for_s=3, ts=NOW)
    d.record(soft, d.verdict(soft, NOW), NOW)
    t = NOW
    for _ in range(120):                 # 10 分钟 × 每 5 秒一次
        t += 5
        v = d.verdict(soft, t)
        assert v is Verdict.SUPPRESSED   # 静默期确实在生效
        d.record(soft, v, t)

    hard = naked_leg_alert("hedge1", "u1", exposure(900), exposed_for_s=600, ts=t)
    assert hard.key == soft.key          # 同一个去重键，靠严重度穿透
    assert d.verdict(hard, t) is Verdict.ESCALATED


def test_de_escalation_does_not_break_silence():
    """反过来 HARD→SOFT 不推：情况在好转，没有新信息。"""
    d = Deduper()
    hard = naked_leg_alert("h", "u", exposure(900), exposed_for_s=600, ts=NOW)
    d.record(hard, d.verdict(hard, NOW), NOW)
    soft = naked_leg_alert("h", "u", exposure(200), exposed_for_s=610, ts=NOW + 10)
    assert d.verdict(soft, NOW + 10) is Verdict.SUPPRESSED


def test_escalation_only_once_per_level():
    """连续三条 HARD 只推第一条。恶化突破的是"更严重"，不是"又来一条"。"""
    d = Deduper()
    hard = naked_leg_alert("h", "u", exposure(900), exposed_for_s=10, ts=NOW)
    v = d.verdict(hard, NOW)
    d.record(hard, v, NOW)
    for i in range(1, 4):
        again = naked_leg_alert("h", "u", exposure(900), exposed_for_s=10 + i, ts=NOW + i)
        v2 = d.verdict(again, NOW + i)
        assert v2 is Verdict.SUPPRESSED
        d.record(again, v2, NOW + i)


def test_reappear_after_gap_is_a_new_event():
    """消失后重新出现算新事件：那是新的决策时点，不是上一条的重复。"""
    d = Deduper()
    a = make_alert(EventType.OPPORTUNITY, subject="BTC")
    d.record(a, d.verdict(a, NOW), NOW)
    gap = DEFAULT_RULES[EventType.OPPORTUNITY].reappear_gap_s
    later = replace(a, ts=NOW + gap + 1)
    assert d.verdict(later, NOW + gap + 1) is Verdict.REAPPEARED


def test_flapping_within_gap_stays_suppressed():
    """闪断闪回（消失 5 分钟又回来）不算新事件，否则一个抖动的机会能刷屏。"""
    d = Deduper()
    a = make_alert(EventType.OPPORTUNITY, subject="BTC")
    d.record(a, d.verdict(a, NOW), NOW)
    t = NOW + 300           # 小于 1800 秒的 reappear_gap
    assert d.verdict(replace(a, ts=t), t) is Verdict.SUPPRESSED


def test_continuous_presence_does_not_count_as_reappear():
    """一直存在但一直被静默的事件，绝不能被误判成"重新出现"。

    这要求**被压住的告警也要记账**（刷新 last_seen），否则静默期形同虚设：
    压满 reappear_gap 之后就会当成新事件推出去。
    """
    d = Deduper()
    a = make_alert(EventType.OPPORTUNITY, subject="BTC")
    d.record(a, d.verdict(a, NOW), NOW)
    gap = DEFAULT_RULES[EventType.OPPORTUNITY].reappear_gap_s
    t = NOW
    # 每 60 秒检测一次（OPPORTUNITY 的真实检测周期），一直存在
    for _ in range(int(gap / 60) + 5):
        t += 60
        v = d.verdict(a, t)
        assert v is not Verdict.REAPPEARED
        d.record(a, v, t)


def test_reappear_resets_peak_severity():
    """重新出现要把上一轮的严重度清掉——把已经结束的事故的峰值
    带到下一次，会让新一轮的 SOFT 永远推不出去。"""
    d = Deduper()
    hard = naked_leg_alert("h", "u", exposure(900), exposed_for_s=10, ts=NOW)
    v = d.verdict(hard, NOW)
    d.record(hard, v, NOW)
    gap = DEFAULT_RULES[EventType.NAKED_LEG].reappear_gap_s
    t = NOW + gap + 1
    soft = naked_leg_alert("h", "u", exposure(200), exposed_for_s=2, ts=t)
    v2 = d.verdict(soft, t)
    assert v2 is Verdict.REAPPEARED
    d.record(soft, v2, t)
    st = d.state_for(soft.key)
    assert st is not None and st.peak_severity is Severity.WARN


def test_apr_bucket_separates_material_improvement():
    """同一个币从 30% 涨到 90% 是新机会；30%→32% 不是。"""
    assert apr_bucket(0.30) == apr_bucket(0.32)
    assert apr_bucket(0.30) != apr_bucket(0.90)


def test_better_bucket_bypasses_silence():
    d = Deduper()
    a = make_alert(EventType.OPPORTUNITY, subject="BTC", bucket=apr_bucket(0.30))
    d.record(a, d.verdict(a, NOW), NOW)
    better = make_alert(EventType.OPPORTUNITY, subject="BTC", bucket=apr_bucket(1.20))
    assert d.verdict(better, NOW + 60) is Verdict.NEW


def test_different_subjects_are_independent():
    """BTC 推过不能压住 ETH。去重键必须带标的。"""
    d = Deduper()
    btc = make_alert(EventType.OPPORTUNITY, subject="BTC")
    eth = make_alert(EventType.OPPORTUNITY, subject="ETH")
    d.record(btc, d.verdict(btc, NOW), NOW)
    assert d.verdict(eth, NOW + 1) is Verdict.NEW


def test_different_event_types_are_independent():
    """同一个标的的 RISK 和 STALE 是两件事，不能互相压。"""
    d = Deduper()
    risk = make_alert(EventType.RISK, subject="binance:perp")
    stale = make_alert(EventType.STALE, subject="binance:perp")
    d.record(risk, d.verdict(risk, NOW), NOW)
    assert d.verdict(stale, NOW + 1) is Verdict.NEW


def test_sweep_makes_next_occurrence_new():
    """全量扫描能给出准确的活跃集时，别依赖超时去猜——sweep 是确定性通路。"""
    d = Deduper()
    btc = make_alert(EventType.OPPORTUNITY, subject="BTC")
    eth = make_alert(EventType.OPPORTUNITY, subject="ETH")
    for a in (btc, eth):
        d.record(a, d.verdict(a, NOW), NOW)
    assert d.sweep(EventType.OPPORTUNITY, ["ETH"]) == 1
    assert d.verdict(btc, NOW + 10) is Verdict.NEW       # 掉榜又回来 = 新机会
    assert d.verdict(eth, NOW + 10) is Verdict.SUPPRESSED


def test_sweep_does_not_touch_other_event_types():
    d = Deduper()
    risk = make_alert(EventType.RISK, subject="BTC")
    d.record(risk, d.verdict(risk, NOW), NOW)
    d.sweep(EventType.OPPORTUNITY, [])
    assert d.verdict(risk, NOW + 1) is Verdict.SUPPRESSED


def test_rules_from_config_two_knobs():
    """只暴露两个旋钮（机会/风险）。给六个，用户要么不调，要么调出矛盾的值。"""
    cfg = AlertsConfig(silence_opportunity_s=7200, silence_risk_s=60)
    rules = rules_from_config(cfg)
    assert rules[EventType.OPPORTUNITY].silence_s == 7200
    assert rules[EventType.RISK].silence_s == 60
    assert rules[EventType.NAKED_LEG].silence_s == 60
    # 默认值就是任务要求的 4 小时 / 15 分钟
    d = rules_from_config(AlertsConfig())
    assert d[EventType.OPPORTUNITY].silence_s == 4 * 3600
    assert d[EventType.RISK].silence_s == 900


# ---- 检测周期 -----------------------------------------------------------

def test_each_event_type_has_its_own_interval():
    """统一一个周期必然错：机会一天一两次，裸腿按秒计价。"""
    assert set(DETECT_INTERVAL_S) == set(EventType)
    assert DETECT_INTERVAL_S[EventType.NAKED_LEG] < DETECT_INTERVAL_S[EventType.RISK]
    assert DETECT_INTERVAL_S[EventType.RISK] < DETECT_INTERVAL_S[EventType.OPPORTUNITY]
    # 事件驱动的两类周期为 0，不该被时钟挡住
    assert DETECT_INTERVAL_S[EventType.UNWIND] == 0
    assert DETECT_INTERVAL_S[EventType.BREAKER] == 0


def test_detection_clock_respects_interval():
    clock = FakeClock(0.0)
    dc = DetectionClock(clock=clock)
    assert dc.due(EventType.OPPORTUNITY) is True
    assert dc.due(EventType.OPPORTUNITY) is False
    clock.advance(59)
    assert dc.due(EventType.OPPORTUNITY) is False
    clock.advance(2)
    assert dc.due(EventType.OPPORTUNITY) is True


def test_detection_clock_event_driven_always_due():
    dc = DetectionClock(clock=FakeClock(0.0))
    assert all(dc.due(EventType.BREAKER) for _ in range(5))


def test_detection_clock_types_do_not_interfere():
    clock = FakeClock(0.0)
    dc = DetectionClock(clock=clock)
    dc.due(EventType.OPPORTUNITY)
    clock.advance(16)
    assert dc.due(EventType.RISK) is True            # RISK 15s 到点
    assert dc.due(EventType.OPPORTUNITY) is False    # OPPORTUNITY 60s 没到


# ---- 陈旧监控 -----------------------------------------------------------

def test_stale_monitor_flags_only_the_dead_venue():
    """一边断了另一边没断，只能报断的那边——全报会让人怀疑工具本身。"""
    clock = FakeClock(NOW)
    m = StaleMonitor(stale_after_s=300.0, clock=clock)
    m.touch("binance:perp")
    m.touch("gate:perp")
    clock.advance(400)
    m.touch("binance:perp")
    stale = m.stale()
    assert [s[0] for s in stale] == ["gate:perp"]
    a = m.alerts()[0]
    assert a.type is EventType.STALE and a.subject == "gate:perp"
    assert "不存在" in a.action or "幻觉" in a.action


def test_stale_monitor_sorts_worst_first():
    clock = FakeClock(NOW)
    m = StaleMonitor(stale_after_s=100.0, clock=clock)
    m.touch("a", NOW - 1000)
    m.touch("b", NOW - 200)
    assert [s[0] for s in m.stale()] == ["a", "b"]


# ---- 通道 ---------------------------------------------------------------

def test_log_channel_always_on_and_receives_everything():
    """本地日志永远开着。远端全挂也必须有账可查——
    告警系统静默失败，比压根没有告警更危险。"""
    seen: list[tuple[str, str, str]] = []
    ch = LogChannel(lambda level, category, msg: seen.append((level, category, msg)))
    assert ch.always_on is True
    run(ch.send(make_alert(EventType.RISK, severity=Severity.ERROR)))
    assert seen and seen[0][0] == "error"
    assert seen[0][1] == "safety"
    assert "该做什么" in seen[0][2]


def test_wechat_channel_reuses_pushplus_shape():
    """微信通道复用用户既有的 PushPlus 链路：POST token+title+content+markdown。"""
    poster = FakePoster({"code": 200})
    ch = WeChatChannel(FAKE_TOKEN, poster=poster)
    run(ch.send(make_alert()))
    url, payload, _ = poster.calls[0]
    assert url == "https://www.pushplus.plus/send"
    assert payload["token"] == FAKE_TOKEN
    assert payload["template"] == "markdown"
    assert "该做什么" in payload["content"]
    assert len(payload["title"]) <= 100


def test_wechat_channel_detects_business_error_inside_http_200():
    """PushPlus 在 token 失效时照样返回 HTTP 200，body 里才写 code=401。
    只看状态码等于把失败当成功。"""
    ch = WeChatChannel(FAKE_TOKEN, poster=FakePoster({"code": 401, "msg": "token 无效"}))
    with pytest.raises(RuntimeError, match="PushPlus"):
        run(ch.send(make_alert()))


def test_telegram_channel_detects_not_started_bot():
    """TG Bot 没被点过 Start 时返回 200 + ok:false。这是这个通道
    "配好了却收不到"的头号原因，必须解析 body。"""
    ch = TelegramChannel(FAKE_TOKEN, FAKE_CHAT,
                         poster=FakePoster({"ok": False,
                                            "description": "bot can't initiate conversation"}))
    with pytest.raises(RuntimeError, match="Telegram"):
        run(ch.send(make_alert()))


def test_telegram_sends_plain_text_without_parse_mode():
    """不带 parse_mode 是刻意的：MarkdownV2 要转义 . - ( ) 等等，
    我们的正文里全是这些字符，漏一个就整条 400。"""
    poster = FakePoster({"ok": True})
    ch = TelegramChannel(FAKE_TOKEN, FAKE_CHAT, poster=poster)
    run(ch.send(make_alert()))
    url, payload, _ = poster.calls[0]
    assert f"/bot{FAKE_TOKEN}/sendMessage" in url
    assert "parse_mode" not in payload
    assert payload["chat_id"] == FAKE_CHAT


def test_webhook_styles():
    """飞书/钉钉/Bark 的 body 互不兼容，内置几种比让用户自己写中转靠谱。"""
    a = make_alert()
    assert webhook_payload(a, "feishu")["msg_type"] == "text"
    assert webhook_payload(a, "dingtalk")["msgtype"] == "text"
    assert webhook_payload(a, "bark")["title"] == a.title
    raw = webhook_payload(a, "raw")
    assert raw["source"] == "carryfarm" and raw["action"] == a.action


def test_webhook_bark_level_escalates_for_severe_alerts():
    mild = webhook_payload(make_alert(severity=Severity.WARN), "bark")
    bad = webhook_payload(make_alert(severity=Severity.CRITICAL), "bark")
    assert mild["level"] == "active"
    assert bad["level"] == "timeSensitive"


def test_webhook_posts_auth_header():
    poster = FakePoster()
    ch = WebhookChannel("https://example.invalid/hook", headers={"X-Token": "abc"},
                        poster=poster)
    run(ch.send(make_alert()))
    assert poster.calls[0][2] == {"X-Token": "abc"}


# ---- 通道隔离（一个挂了不影响其他） --------------------------------------

def test_one_failing_channel_does_not_block_others():
    """一个通道挂了，其他通道照发。这是"多渠道"这三个字的全部意义。"""
    bad = RecordingChannel("bad", fail=True)
    good = RecordingChannel("good")
    logged: list[str] = []
    engine = AlertEngine([LogChannel(lambda l, c, m: logged.append(m)), bad, good],
                         clock=FakeClock())
    result = run(engine.dispatch(make_alert()))
    assert result.sent
    assert set(result.delivered) == {"log", "good"}
    assert [n for n, _ in result.failed] == ["bad"]
    assert good.received and logged


def test_hanging_channel_times_out_without_starving_others():
    """没有超时的话，一个 TCP 连上但不返回的通道能把整个扇出挂死——
    "一个通道挂了不影响其他通道" 就成了空话。"""
    hang = RecordingChannel("hang", hang=True)
    good = RecordingChannel("good")
    engine = AlertEngine([hang, good], timeout_s=0.05, clock=FakeClock())
    result = run(engine.dispatch(make_alert()))
    assert "good" in result.delivered
    assert result.failed and "超时" in result.failed[0][1]


def test_local_log_survives_all_remote_failures():
    """远端全挂时本地日志仍然拿到完整内容——事后复盘只能靠它。"""
    logged: list[str] = []
    engine = AlertEngine(
        [LogChannel(lambda l, c, m: logged.append(m)),
         RecordingChannel("a", fail=True), RecordingChannel("b", hang=True)],
        timeout_s=0.05, clock=FakeClock())
    result = run(engine.dispatch(make_alert()))
    assert result.delivered == ("log",)
    assert len(result.failed) == 2
    assert "该做什么" in logged[0]


def test_engine_never_raises_even_if_every_channel_explodes():
    """告警路径上的异常把主循环打断，等于用告警系统制造事故。"""
    engine = AlertEngine([RecordingChannel("x", fail=True)], clock=FakeClock())
    result = run(engine.dispatch(make_alert()))
    assert result.all_failed is True


def test_channel_stats_track_consecutive_failures():
    """连续失败次数要能被界面读到——"配好了但一直发不出去"必须看得见。"""
    bad = RecordingChannel("bad", fail=True)
    engine = AlertEngine([bad], clock=FakeClock())
    for i in range(3):
        run(engine.dispatch(make_alert(subject=f"s{i}")))
    assert engine.stats["bad"].consecutive_failures == 3
    assert engine.stats["bad"].sent == 0


def test_recovery_resets_consecutive_failures():
    ch = RecordingChannel("flaky", fail=True)
    engine = AlertEngine([ch], clock=FakeClock())
    run(engine.dispatch(make_alert(subject="a")))
    ch.fail = False
    run(engine.dispatch(make_alert(subject="b")))
    assert engine.stats["flaky"].consecutive_failures == 0
    assert engine.stats["flaky"].sent == 1


# ---- 引擎的开关与去重接线 ------------------------------------------------

def test_engine_applies_dedup_end_to_end():
    """一个机会连续挂在榜上 4 小时：只在开头和静默期满时各推一次。

    中间必须持续投递（真实检测周期 60 秒），否则"一直存在"和"消失了"
    在引擎看来就没有区别了。
    """
    ch = RecordingChannel("rec")
    clock = FakeClock()
    engine = AlertEngine([ch], clock=clock)
    a = make_alert(EventType.OPPORTUNITY, subject="BTC")
    assert run(engine.dispatch(a)).verdict is Verdict.NEW

    for _ in range(4 * 6 - 1):           # 差 10 分钟满 4 小时，每 10 分钟投一次
        clock.advance(600)
        assert run(engine.dispatch(a)).verdict is Verdict.SUPPRESSED
    assert len(ch.received) == 1

    clock.advance(600)                   # 正好满 4 小时
    assert run(engine.dispatch(a)).verdict is Verdict.RENEWED
    assert len(ch.received) == 2


def test_engine_escalation_reaches_channels():
    ch = RecordingChannel("rec")
    clock = FakeClock()
    engine = AlertEngine([ch], clock=clock)
    run(engine.dispatch(naked_leg_alert("h", "u", exposure(200), exposed_for_s=2)))
    clock.advance(60)
    run(engine.dispatch(naked_leg_alert("h", "u", exposure(900), exposed_for_s=62)))
    assert len(ch.received) == 2
    assert ch.received[-1].severity is Severity.ERROR


def test_engine_disabled_sends_nothing():
    ch = RecordingChannel("rec")
    engine = AlertEngine([ch], enabled=False, clock=FakeClock())
    assert run(engine.dispatch(make_alert())).verdict is Verdict.DISABLED
    assert not ch.received


def test_muted_event_type_is_dropped():
    """六类事件可独立闭麦：被 STALE 刷烦的人不该被迫连 NAKED_LEG 一起关掉。"""
    ch = RecordingChannel("rec")
    engine = AlertEngine([ch], muted=[EventType.STALE], clock=FakeClock())
    assert run(engine.dispatch(make_alert(EventType.STALE))).verdict is Verdict.MUTED
    assert run(engine.dispatch(make_alert(EventType.RISK))).verdict is Verdict.NEW
    assert len(ch.received) == 1


def test_concurrent_dispatch_of_same_key_sends_once():
    """两个协程同时投递同一个键，不加锁会双双判成 NEW，去重当场失效。"""
    ch = RecordingChannel("rec")
    engine = AlertEngine([ch], clock=FakeClock())
    a = make_alert(EventType.OPPORTUNITY, subject="BTC")

    async def race():
        return await asyncio.gather(*(engine.dispatch(a) for _ in range(8)))

    results = run(race())
    assert sum(1 for r in results if r.sent) == 1
    assert len(ch.received) == 1


def test_dispatch_many_preserves_order():
    ch = RecordingChannel("rec")
    engine = AlertEngine([ch], clock=FakeClock())
    batch = [make_alert(EventType.RISK, subject=s) for s in ("a", "b", "c")]
    results = run(engine.dispatch_many(batch))
    assert [r.alert.subject for r in results] == ["a", "b", "c"]
    assert len(ch.received) == 3


# ---- 从配置装配 ---------------------------------------------------------

def test_build_channels_default_is_log_only():
    """默认什么远端通道都不开——装上就往外发网络请求是不能接受的。"""
    plan = build_channels(AlertsConfig())
    assert [c.name for c in plan.channels] == ["log"]
    assert plan.skipped == {}


def test_enabled_channel_without_secret_is_skipped_loudly():
    """开了通道但没读到密钥，必须记下原因给界面看。
    静默地不发告警，比配错更危险——用户以为自己有告警，实际只有本地日志。"""
    cfg = AlertsConfig(wechat=WeChatAlertConfig(enabled=True,
                                                token_env="CARRYFARM_TEST_ABSENT"))
    plan = build_channels(cfg)
    assert [c.name for c in plan.channels] == ["log"]
    assert "wechat" in plan.skipped and "token" in plan.skipped["wechat"]


def test_secret_comes_from_env(monkeypatch):
    monkeypatch.setenv("CARRYFARM_TEST_TOKEN", FAKE_TOKEN)
    cfg = AlertsConfig(wechat=WeChatAlertConfig(enabled=True,
                                                token_env="CARRYFARM_TEST_TOKEN"))
    plan = build_channels(cfg, poster=FakePoster({"code": 200}))
    assert [c.name for c in plan.channels] == ["log", "wechat"]


def test_env_beats_config_literal(monkeypatch):
    """环境变量优先：服务器上换 key 不用改配置文件。"""
    monkeypatch.setenv("CARRYFARM_TEST_TOKEN", "from-env")
    assert resolve_secret("from-file", "CARRYFARM_TEST_TOKEN") == "from-env"
    monkeypatch.delenv("CARRYFARM_TEST_TOKEN")
    assert resolve_secret("from-file", "CARRYFARM_TEST_TOKEN") == "from-file"
    assert resolve_secret("", "CARRYFARM_TEST_TOKEN") == ""


def test_engine_from_config_wires_everything(monkeypatch):
    monkeypatch.setenv("CARRYFARM_TEST_TG", FAKE_TOKEN)
    monkeypatch.setenv("CARRYFARM_TEST_CHAT", FAKE_CHAT)
    monkeypatch.setenv("CARRYFARM_TEST_HOOK", "https://example.invalid/hook")
    cfg = AlertsConfig(
        silence_opportunity_s=1234,
        mute=("stale",),
        telegram=TelegramAlertConfig(enabled=True, bot_token_env="CARRYFARM_TEST_TG",
                                     chat_id_env="CARRYFARM_TEST_CHAT"),
        webhook=WebhookAlertConfig(enabled=True, url_env="CARRYFARM_TEST_HOOK",
                                   style="feishu"),
    )
    poster = FakePoster({"ok": True})
    engine = AlertEngine.from_config(cfg, poster=poster, clock=FakeClock())
    assert engine.channel_names == ("log", "telegram", "webhook")
    assert engine.dedup.rule(EventType.OPPORTUNITY).silence_s == 1234
    assert engine.is_muted(EventType.STALE)
    run(engine.dispatch(make_alert(EventType.RISK)))
    assert len(poster.calls) == 2                 # telegram + webhook


def test_config_rejects_unknown_mute_and_style():
    with pytest.raises(Exception):
        AlertsConfig(mute=("no-such-event",)).validate()
    with pytest.raises(Exception):
        AlertsConfig(webhook=WebhookAlertConfig(style="qq")).validate()


def test_config_alerts_section_parses_nested_tables(tmp_path):
    """[alerts.telegram] 这种嵌套段要能从 config.toml 读进来。"""
    from carryfarm.config import load

    path = tmp_path / "config.toml"
    path.write_text(
        "[alerts]\n"
        "silence_risk_s = 60\n"
        'mute = ["stale"]\n'
        "\n[alerts.telegram]\n"
        "enabled = true\n"
        'chat_id_env = "MY_CHAT"\n',
        encoding="utf-8")
    cfg = load(path)
    assert cfg.alerts.silence_risk_s == 60
    assert cfg.alerts.mute == ("stale",)
    assert cfg.alerts.telegram.enabled is True
    assert cfg.alerts.telegram.chat_id_env == "MY_CHAT"
    # 密钥字段仍然是空的：配置文件里不该有，也没有默认值
    assert cfg.alerts.telegram.bot_token == ""


# ---- 密钥卫生 -----------------------------------------------------------

def test_no_hardcoded_secrets_in_module():
    """源码里不许出现任何像密钥的字面量。

    用户已有的推送链路里 token 是直接内嵌在脚本里的（crypto_social_tracker），
    这里刻意不照抄那一点：这个仓库要能公开、能进 git、能贴给别人看。
    """
    src = inspect.getsource(alerts_mod)
    # 长十六进制串 / base64 串 / 明显的 token 赋值
    assert not re.search(r"[0-9a-fA-F]{24,}", src)
    assert not re.search(r"(token|secret|key|passwd|password)\s*=\s*[\"'][^\"'\s]{12,}",
                         src, re.IGNORECASE)
    # 已知的推送域名可以出现（是端点），但后面绝不能跟着一段密钥路径
    assert not re.search(r"pushplus\.plus/send\?", src)
    assert not re.search(r"api\.telegram\.org/bot\d", src)


def test_channel_config_defaults_are_empty():
    """所有密钥字段的默认值必须是空串，只有 *_env 有默认值。"""
    cfg = AlertsConfig()
    assert cfg.wechat.token == ""
    assert cfg.telegram.bot_token == "" and cfg.telegram.chat_id == ""
    assert cfg.webhook.url == ""
    assert cfg.wechat.token_env and cfg.telegram.bot_token_env and cfg.webhook.url_env


def test_repr_never_leaks_secrets():
    """把 token 打进日志，是告警系统最讽刺的泄露方式。"""
    ch = WeChatChannel("supersecrettoken123456", poster=FakePoster())
    tg = TelegramChannel("botsecret999888777", FAKE_CHAT, poster=FakePoster())
    hook = WebhookChannel("https://open.feishu.cn/hook/abcdef123456", poster=FakePoster())
    for text in (repr(ch), repr(tg), repr(hook)):
        assert "supersecrettoken123456" not in text
        assert "botsecret999888777" not in text
        assert "abcdef123456" not in text
    assert redact("") == "<未配置>"
    assert redact("abcd1234").endswith("1234")
    assert "abcd" not in redact("abcd1234")


def test_channel_error_text_does_not_echo_the_url(monkeypatch):
    """通道失败时记的是异常类型和消息，别把带 token 的 URL 原样塞进错误里。"""
    poster = FakePoster(raises=RuntimeError("boom"))
    ch = WebhookChannel("https://hook.invalid/secret-path-9999", poster=poster)
    engine = AlertEngine([ch], clock=FakeClock())
    result = run(engine.dispatch(make_alert()))
    assert result.failed
    assert "secret-path-9999" not in result.failed[0][1]


# ---- 机会筛选 -----------------------------------------------------------

def test_actionable_filter_uses_a_much_higher_bar_than_the_board():
    """推送门槛必须远高于"值不值得列出来"。

    真实历史接上后大部分机会是负期望，推送的价值和它的稀有度成正比。
    """
    rich = make_opportunity(net_funding=0.003)
    thin = make_opportunity(net_funding=0.00012, base="ETH")
    cfg = AlertsConfig(min_net_apr=Decimal("0.30"), min_capacity_usd=Decimal("5000"))
    picked = actionable_opportunities([rich, thin], cfg)
    assert rich in picked
    assert thin not in picked


def test_actionable_filter_drops_tiny_capacity():
    """看得见吃不着的机会推了只是让人白高兴。"""
    op = make_opportunity(net_funding=0.003)
    cfg = AlertsConfig(min_net_apr=Decimal("0.05"),
                       min_capacity_usd=Decimal("100000000"))
    assert actionable_opportunities([op], cfg) == []


def test_actionable_filter_respects_require_actionable():
    op = make_opportunity(net_funding=0.003)
    marginal = replace(op, verdict="marginal")
    cfg = AlertsConfig(min_net_apr=Decimal("0.05"), min_capacity_usd=Decimal("100"))
    assert actionable_opportunities([marginal], cfg) == []
    assert actionable_opportunities([marginal], replace(cfg, require_actionable=False))


def test_end_to_end_opportunity_flow():
    """完整一轮：榜单 → 筛选 → 告警 → 去重 → 掉榜 → 重新上榜算新事件。"""
    clock = FakeClock()
    ch = RecordingChannel("rec")
    engine = AlertEngine([ch], clock=clock)
    op = make_opportunity(net_funding=0.003)
    cfg = AlertsConfig()

    picked = actionable_opportunities([op], cfg)
    assert picked
    a = opportunity_alert(picked[0], ts=clock())
    assert run(engine.dispatch(a)).verdict is Verdict.NEW

    # 一分钟后还在榜上 —— 压住
    clock.advance(60)
    assert run(engine.dispatch(opportunity_alert(op, ts=clock()))).verdict is Verdict.SUPPRESSED

    # 掉榜（活跃集为空）→ 再上榜就是新机会
    engine.dedup.sweep(EventType.OPPORTUNITY, [])
    clock.advance(60)
    assert run(engine.dispatch(opportunity_alert(op, ts=clock()))).verdict is Verdict.NEW
    assert len(ch.received) == 2


# --- 三轮并行改动之后的接缝回归 ---------------------------------------------


def test_body_given_a_bare_string_is_one_line_not_one_char_per_line():
    """body 是 tuple[str, ...]，但 str 本身也满足 Sequence[str]。

    传一个裸字符串进来，tuple() 会把它拆成一个字一行，推到微信上就是
    一列竖着的单字。类型标注和现有测试都拦不住（内部构造器全传 list），
    只有真在通道上看渲染结果才会发现——所以在这里钉死。
    """
    a = Alert(type=EventType.OPPORTUNITY, severity=Severity.INFO,
              subject="BTCUSDT", title="测试", body="净年化 42%", action="确认容量")
    assert a.body == ("净年化 42%",)
    assert "· 净年化 42%" in a.as_text()
    assert "· 净\n" not in a.as_text()


def test_body_accepts_any_sequence_and_normalises_to_tuple():
    a = Alert(type=EventType.RISK, severity=Severity.WARN, subject="s",
              title="t", body=["一", "二"], action="做点什么")
    assert a.body == ("一", "二")
    assert a.as_dict()["body"] == ["一", "二"]


def test_no_channels_configured_degrades_to_local_log_only():
    """一个远端通道都没配时必须优雅降级：只写本地日志，不报错、不崩。

    这是绝大多数用户的初始状态。这条路径炸了，等于工具开箱即坏。
    """
    seen: list[tuple[str, str, str]] = []
    cfg = AlertsConfig()                    # 三个远端通道默认 enabled=False
    plan = alerts_mod.build_channels(
        cfg, sink=lambda lvl, cat, msg: seen.append((lvl, cat, msg)))
    assert [c.name for c in plan.channels] == ["log"]
    assert plan.skipped == {}

    engine = AlertEngine.from_config(
        cfg, sink=lambda lvl, cat, msg: seen.append((lvl, cat, msg)))
    a = Alert(type=EventType.OPPORTUNITY, severity=Severity.INFO,
              subject="BTCUSDT", title="测试机会", body=("净年化 42%",),
              action="去界面确认容量")
    result = run(engine.dispatch(a))
    assert result.verdict is Verdict.NEW
    assert result.delivered == ("log",)
    assert result.failed == ()
    assert len(seen) == 1
    # 静默期内重复推不会再落一条
    assert run(engine.dispatch(a)).verdict is Verdict.SUPPRESSED
    assert len(seen) == 1
