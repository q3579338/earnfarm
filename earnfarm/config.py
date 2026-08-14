"""配置模型。所有可调参数集中在这里，从 config.toml 读取。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

# 改名兼容：项目原名 carryfarm，老用户的库（vault/资金费历史）都在 ~/.carryfarm。
# 新目录不存在而老目录存在时继续用老的——改名绝不能把人的账户"改丢"。
# 迁移数据（把目录改名成 ~/.earnfarm）留给用户显式去做，程序不自作主张搬库。
_LEGACY_DATA_DIR = Path.home() / ".carryfarm"
_PREFERRED_DATA_DIR = Path.home() / ".earnfarm"
DEFAULT_DATA_DIR = (
    _LEGACY_DATA_DIR
    if _LEGACY_DATA_DIR.exists() and not _PREFERRED_DATA_DIR.exists()
    else _PREFERRED_DATA_DIR
)
DEFAULT_CONFIG_PATH = DEFAULT_DATA_DIR / "config.toml"


class ConfigError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class BrokerCodes:
    """经纪商返佣码。默认全空 = 下单不带任何返佣标识。

    需要时在 config.toml 的 [broker] 段填入自己申请到的码即可生效，
    无需改动任何代码。各家的带法不同（header / body 字段 / 订单ID前缀），
    由各自的 adapter 负责，这里只存原始字符串。
    """

    binance: str = ""
    okx: str = ""
    htx: str = ""
    gate: str = ""
    bybit: str = ""
    bitget: str = ""

    def for_venue(self, venue: str) -> str:
        return getattr(self, venue, "") or ""

    @property
    def any_enabled(self) -> bool:
        return any(self.for_venue(v) for v in
                   ("binance", "okx", "htx", "gate", "bybit", "bitget"))


@dataclass(frozen=True, slots=True)
class ScoringConfig:
    """机会评分参数。"""

    # 假设的持有期（天），用于把开平仓成本摊到年化里
    assumed_holding_days: Decimal = Decimal("7")
    # 历史资金费回溯天数，用于稳定性评分
    history_days: int = 30
    # 净年化低于此值的机会直接标为负期望（小数，0.05 = 5%）
    min_net_apr: Decimal = Decimal("0.05")
    # 稳定性评分权重（0~1），越高越看重历史一致性而非当前费率
    stability_weight: Decimal = Decimal("0.35")
    # 滑点估算：按对手方一档量的百分之多少来估冲击成本
    slippage_depth_ratio: Decimal = Decimal("0.5")
    # taker 费率兜底值（查不到用户实际档位时使用）
    fallback_taker_fee: Decimal = Decimal("0.0005")


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """执行循环参数。"""

    converge_interval_ms: int = 500
    strategy_interval_ms: int = 1000
    reconcile_interval_ms: int = 2000
    carry_poll_interval_ms: int = 60_000

    # 下单保护限价穿透对手价多少个 tick
    price_protection_ticks: int = 5
    # 盘口数据超过预期更新周期多少毫秒后视为陈旧
    staleness_grace_ms: int = 1_000
    # 目标变化小于该名义金额则不动（防抖）
    step_notional: Decimal = Decimal("100")


@dataclass(frozen=True, slots=True)
class SafetyConfig:
    """安全与撤退参数。原版工具最大的缺陷就在这块——一腿成交另一腿失败时
    它会无限重试补腿而绝不撤退，导致长时间裸单边暴露。"""

    # 净敞口超过目标的该比例即视为裸奔
    naked_exposure_ratio: Decimal = Decimal("0.05")
    # 裸奔后允许补腿的最长时间，超时进入撤退
    naked_retry_seconds: int = 30
    # 补腿连续失败多少次直接判定无望，立即撤退
    naked_max_attempts: int = 10
    # 撤退时是否允许市价单（False 则用穿透更深的限价渐进平）
    retreat_allow_market: bool = False
    # 撤退时限价穿透的 tick 数（比常规下单更激进）
    retreat_protection_ticks: int = 20

    # 爆仓距离分档：距爆仓价的相对距离低于阈值时触发对应动作
    liq_distance_stop_opening: Decimal = Decimal("0.25")   # 停止加仓
    liq_distance_deleverage: Decimal = Decimal("0.15")     # 降杠杆
    liq_distance_reduce: Decimal = Decimal("0.10")         # 强制减仓

    # 熔断：连续下单失败多少次后整体停机等人工
    circuit_breaker_failures: int = 20
    # 账户权益单日回撤超过此比例即熔断
    circuit_breaker_drawdown: Decimal = Decimal("0.15")

    # 杠杆策略：'conservative' 取够用的最低档，'max' 取档位允许的最高值
    leverage_mode: str = "conservative"
    # conservative 模式下保留的保证金冗余倍数
    leverage_margin_buffer: Decimal = Decimal("3")


@dataclass(frozen=True, slots=True)
class WeChatAlertConfig:
    """微信推送（PushPlus）。复用用户既有的推送链路，只把 token 做成配置项。

    token 建议**留空**，改用环境变量 token_env 指向的那个变量：
    config.toml 是明文文件，会被备份、会被同步到网盘、会被贴进聊天框。
    环境变量优先于文件里的字面量——换 key 不用改配置。
    """

    enabled: bool = False
    token: str = ""
    token_env: str = "EARNFARM_PUSHPLUS_TOKEN"
    topic: str = ""            # 群组编码，留空 = 只推给自己
    url: str = "https://www.pushplus.plus/send"


@dataclass(frozen=True, slots=True)
class TelegramAlertConfig:
    """Telegram Bot。注意：Bot 建好后必须由你本人先对它点一次 Start，
    否则 sendMessage 会被拒（"bot can't initiate conversation"）——
    这是这个通道最常见的"配好了却收不到"的原因。"""

    enabled: bool = False
    bot_token: str = ""
    bot_token_env: str = "EARNFARM_TELEGRAM_BOT_TOKEN"
    chat_id: str = ""
    chat_id_env: str = "EARNFARM_TELEGRAM_CHAT_ID"
    api_base: str = "https://api.telegram.org"


@dataclass(frozen=True, slots=True)
class WebhookAlertConfig:
    """通用 Webhook：POST 一份 JSON。飞书/钉钉/Bark/自建中转都接得上。

    style 决定 body 的形状：raw（结构化全量）/ feishu / dingtalk / bark。
    各家机器人的 body 互不兼容，与其让用户自己写转换层，不如内置这几种。
    """

    enabled: bool = False
    url: str = ""
    url_env: str = "EARNFARM_ALERT_WEBHOOK_URL"
    style: str = "raw"
    # 需要鉴权时填头名，值一律走环境变量——不落文件
    auth_header: str = ""
    auth_value_env: str = "EARNFARM_ALERT_WEBHOOK_TOKEN"


@dataclass(frozen=True, slots=True)
class AlertsConfig:
    """告警引擎。

    存在的理由：接上真实历史之后，绝大多数跨所机会是负期望，真能做的稀少且短暂
    （常常只活几小时）。守着屏幕等是这个工具最差的用法，所以它必须能主动喊人。

    静默期的默认值分两档：机会类 4 小时（同一个机会反复推等于噪音），
    风险类 15 分钟（要的是"还没解决"的提醒，但不能每秒响）。
    """

    enabled: bool = True

    # --- OPPORTUNITY 的触发条件 ---
    # 净年化门槛。默认 30% 远高于 scoring.min_net_apr(5%)：那个是"值不值得列出来"，
    # 这个是"值不值得把人从别的事上拉过来"，标准必须更高。
    min_net_apr: Decimal = Decimal("0.30")
    min_capacity_usd: Decimal = Decimal("5000")
    # 只推 verdict 为 good/sized_down 的（marginal 就是让人别做的意思）
    require_actionable: bool = True

    # --- 静默期（秒）---
    silence_opportunity_s: int = 14_400      # 4 小时
    silence_risk_s: int = 900                # 15 分钟
    # 超过这么久没再出现，就认为它已经消失；再出现算**新事件**，立刻推
    reappear_gap_opportunity_s: int = 1_800
    reappear_gap_risk_s: int = 120

    # --- STALE ---
    stale_after_s: int = 300                 # 行情多久没更新算断线

    # 单个通道的发送超时。挂住的通道不能拖住其他通道
    timeout_s: Decimal = Decimal("10")
    # 临时闭麦的事件类型，如 ["stale", "opportunity"]
    mute: tuple[str, ...] = ()

    wechat: WeChatAlertConfig = field(default_factory=WeChatAlertConfig)
    telegram: TelegramAlertConfig = field(default_factory=TelegramAlertConfig)
    webhook: WebhookAlertConfig = field(default_factory=WebhookAlertConfig)

    def validate(self) -> None:
        if self.silence_opportunity_s < 0 or self.silence_risk_s < 0:
            raise ConfigError("alerts 的静默期不能为负")
        if self.webhook.style not in ("raw", "feishu", "dingtalk", "bark"):
            raise ConfigError(
                f"alerts.webhook.style 必须是 raw/feishu/dingtalk/bark，"
                f"当前是 {self.webhook.style!r}"
            )
        known = {"opportunity", "naked_leg", "unwind", "breaker", "stale", "risk"}
        unknown = set(self.mute) - known
        if unknown:
            raise ConfigError(f"alerts.mute 有不认识的事件类型: {', '.join(sorted(unknown))}")


# 行情轮询的硬下限（秒）。一轮完整刷新实测约 105 秒（六家全市场费率 + 逐对深度），
# 配得比这还短只有两个后果：永远在刷新、以及把六家的限频配额全花在自己身上。
MIN_MARKET_INTERVAL_S = 180.0


@dataclass(frozen=True, slots=True)
class WatchConfig:
    """守护模式（无界面）参数。

    只有这一段是给 `run.py --watch` 用的：没有浏览器、7×24 跑在服务器上，
    有事才推送。默认值按"绝大多数跨所机会是负期望"这个实测结论定——
    门槛给得高、重复告警给得吝啬，宁可漏也别把用户训练成无视推送。
    """

    # ---- 节奏 ----
    market_interval_s: float = 300.0        # 行情+评分。下限 180，见上面的常量
    history_interval_s: float = 1800.0      # 历史增量。一天才结算 1~24 次，跟着行情刷是白烧配额
    position_interval_ms: int = 500         # 仓位推进。这是执行器的收敛节拍，不是轮询
    heartbeat_interval_s: float = 60.0
    shutdown_timeout_s: float = 15.0        # 优雅退出的总预算，超了就硬退（落库在最前面）

    # ---- 评分口径（和界面上那几个输入框同义）----
    notional: float = 50_000.0
    horizon_h: float = 72.0
    top_n: int = 40
    history_days: int = 0                   # 0 = 跟着 horizon 自动算，见 history_days_for()

    # ---- 告警 ----
    # **门槛不在这里**：净年化、容量、静默期全部由 [alerts] 段说了算（见 AlertsConfig）。
    # 同一个阈值放两处，早晚会出现"调高了却还在响"的鬼故事。这里只留两个
    # 属于守护循环本身、告警引擎管不到的闸：
    # 单轮最多向告警引擎推几条候选。一次推 20 条等于一条都没推
    alert_max_per_round: int = 5
    # 连续失败多少次才为"某个环节坏了"告警。一次网络抖动不值得叫醒人
    error_alert_after: int = 3

    def validate(self) -> None:
        if self.market_interval_s < MIN_MARKET_INTERVAL_S:
            raise ConfigError(
                f"watch.market_interval_s={self.market_interval_s} 短于 {MIN_MARKET_INTERVAL_S:.0f} 秒。"
                "一轮完整刷新实测约 105 秒，配得更短会让程序永远在刷新，"
                "并把六家交易所的限频配额全部消耗在自己身上。"
            )
        if self.position_interval_ms <= 0:
            raise ConfigError("watch.position_interval_ms 必须为正")
        if self.heartbeat_interval_s <= 0:
            raise ConfigError("watch.heartbeat_interval_s 必须为正")
        if self.history_interval_s <= 0:
            raise ConfigError("watch.history_interval_s 必须为正")


# 复盘可用的分析引擎。CLI 三个是本机装的编码助手（headless 调用），
# api 是任意线上大模型接口（anthropic 或 openai 两种报文风格）
ANALYSIS_ENGINES = ("claude", "grok", "codex", "api")


@dataclass(frozen=True, slots=True)
class AnalysisConfig:
    """操作复盘分析（/analysis 页）。引擎可多选并行，各出一份报告。"""

    # 页面默认勾选的引擎
    default_engines: tuple[str, ...] = ("claude",)
    # 三个本机 CLI 的命令名或完整路径。装在非常规位置时写绝对路径
    claude_cmd: str = "claude"
    grok_cmd: str = "grok"
    codex_cmd: str = "codex"
    # 线上 API：风格 anthropic（Messages API）或 openai（chat/completions，
    # xAI / DeepSeek / 各家中转站都兼容后者）。key 走环境变量，绝不写字面量
    api_url: str = "https://api.anthropic.com/v1/messages"
    api_style: str = "anthropic"
    api_model: str = "claude-sonnet-5"
    api_key_env: str = "EARNFARM_AI_API_KEY"
    api_max_tokens: int = 8192
    # 单个引擎的超时。数据多时模型要算一阵，太短会把正常分析掐死
    timeout_s: int = 600
    # 嵌入 prompt 的成交上限。超出只截最新——汇总统计仍按全量算
    max_fills: int = 2000
    # 分析页默认回看天数
    default_days: int = 7

    def validate(self) -> None:
        for e in self.default_engines:
            if e not in ANALYSIS_ENGINES:
                raise ConfigError(
                    f"analysis.default_engines 含未知引擎 {e!r}，"
                    f"可选：{', '.join(ANALYSIS_ENGINES)}")
        if self.api_style not in ("anthropic", "openai"):
            raise ConfigError("analysis.api_style 必须是 anthropic 或 openai")
        if self.timeout_s <= 0:
            raise ConfigError("analysis.timeout_s 必须为正")
        if self.max_fills <= 0:
            raise ConfigError("analysis.max_fills 必须为正")
        if self.default_days <= 0:
            raise ConfigError("analysis.default_days 必须为正")


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Web 面板。默认只监听回环地址——不像原版官方命令那样直接把面板挂到公网。"""

    host: str = "127.0.0.1"
    port: int = 8777
    # 仅当显式设为 True 才允许监听非回环地址，且必须配了 TLS
    allow_public: bool = False
    tls_certfile: str = ""
    tls_keyfile: str = ""

    def validate(self) -> None:
        is_public = self.host not in ("127.0.0.1", "localhost", "::1")
        if is_public and not self.allow_public:
            raise ConfigError(
                f"server.host={self.host} 不是回环地址。若确需对外提供访问，"
                "请显式设置 allow_public=true 并配好 tls_certfile/tls_keyfile。"
            )
        if is_public and not (self.tls_certfile and self.tls_keyfile):
            raise ConfigError(
                "对外监听时必须配置 TLS 证书：会话 cookie 和配对码在明文 HTTP 上会被中间人截获。"
            )


@dataclass(frozen=True, slots=True)
class Config:
    data_dir: Path = DEFAULT_DATA_DIR
    broker: BrokerCodes = field(default_factory=BrokerCodes)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)

    @property
    def db_path(self) -> Path:
        # 改名兼容：目录里已有老名字的库就继续用它，绝不并排新建一个空库
        legacy = self.data_dir / "carryfarm.db"
        if legacy.exists():
            return legacy
        return self.data_dir / "earnfarm.db"

    def validate(self) -> None:
        self.server.validate()
        self.alerts.validate()
        self.watch.validate()
        self.analysis.validate()
        if self.safety.leverage_mode not in ("conservative", "max"):
            raise ConfigError(
                f"safety.leverage_mode 必须是 conservative 或 max，当前是 {self.safety.leverage_mode!r}"
            )
        if not Decimal("0") <= self.scoring.stability_weight <= Decimal("1"):
            raise ConfigError("scoring.stability_weight 必须在 0~1 之间")


# 可嵌套的子段（[alerts.telegram] 这种）。必须按**类名字符串**索引：
# 本模块开了 `from __future__ import annotations`，dataclass 字段拿到的
# 注解是字符串而不是类型对象，没法直接 issubclass 判断。
_NESTED_TYPES: dict[str, type] = {}


def _coerce(cls: type, raw: dict[str, Any]) -> Any:
    """按 dataclass 字段类型转换 TOML 读出来的原始值。

    TOML 没有 Decimal，浮点数会丢精度，所以数值字段一律经字符串转 Decimal。
    """
    kwargs: dict[str, Any] = {}
    hints = {f.name: f.type for f in cls.__dataclass_fields__.values()}
    for key, value in raw.items():
        if key not in hints:
            raise ConfigError(f"{cls.__name__} 不认识的配置项: {key}")
        target = hints[key]
        name = target if isinstance(target, str) else getattr(target, "__name__", str(target))
        if name in _NESTED_TYPES:
            if not isinstance(value, dict):
                raise ConfigError(f"{cls.__name__}.{key} 是一个配置段，应写成 [..{key}]")
            kwargs[key] = _coerce(_NESTED_TYPES[name], value)
        elif name == "Decimal":
            kwargs[key] = Decimal(str(value))
        elif name == "Path":
            kwargs[key] = Path(str(value)).expanduser()
        elif name.startswith("tuple"):
            # TOML 的数组是 list，而这些 dataclass 是 frozen 的，必须是不可变值
            kwargs[key] = tuple(value)
        else:
            kwargs[key] = value
    return cls(**kwargs)


_NESTED_TYPES.update({
    "WeChatAlertConfig": WeChatAlertConfig,
    "TelegramAlertConfig": TelegramAlertConfig,
    "WebhookAlertConfig": WebhookAlertConfig,
})


def load(path: Path | None = None) -> Config:
    """读配置文件。文件不存在则返回全默认值（可直接跑，只是没有账户）。"""
    path = path or DEFAULT_CONFIG_PATH
    cfg = Config()
    if path.exists():
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        sections = {
            "broker": BrokerCodes,
            "scoring": ScoringConfig,
            "execution": ExecutionConfig,
            "safety": SafetyConfig,
            "server": ServerConfig,
            "alerts": AlertsConfig,
            "watch": WatchConfig,
            "analysis": AnalysisConfig,
        }
        updates: dict[str, Any] = {}
        for name, klass in sections.items():
            if name in raw:
                updates[name] = _coerce(klass, raw.pop(name))
        if "data_dir" in raw:
            updates["data_dir"] = Path(str(raw.pop("data_dir"))).expanduser()
        unknown = set(raw)
        if unknown:
            raise ConfigError(f"配置文件有不认识的段: {', '.join(sorted(unknown))}")
        cfg = replace(cfg, **updates)
    cfg.validate()
    return cfg
