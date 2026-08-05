"""应用会话：把配置、加密密钥库、存储、交易所适配器串成一个整体。

界面层只跟这里打交道，不直接碰 Vault 或 Storage——
凭据的解密和适配器的构造都集中在这一处，出问题只需要查一个地方。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

from .config import Config
from .exchanges.base import AdapterRegistry, Credential, ExchangeAdapter, ExchangeError
from .models import FeeSchedule, Venue
from .storage import AccountRow, Storage, StorageError
from .vault import BadPassword, Vault, VaultLocked


class SessionError(Exception):
    pass


# 费率档位多久刷一次。VIP 档按 30 天成交量重算、一天最多动一次，
# 而这些端点都不便宜（币安权重 20），一天一次绰绰有余。
FEE_CACHE_TTL_S = 86_400


# 各家需要哪些凭据字段。UI 按这个动态出输入框，
# 不该让用户对着六家都一样的三个框猜哪个要填。
CREDENTIAL_FIELDS: dict[Venue, tuple[str, ...]] = {
    Venue.BINANCE: ("api_key", "api_secret"),
    Venue.OKX: ("api_key", "api_secret", "passphrase"),
    Venue.HTX: ("api_key", "api_secret"),
    Venue.GATE: ("api_key", "api_secret"),
    Venue.BYBIT: ("api_key", "api_secret"),
    Venue.BITGET: ("api_key", "api_secret", "passphrase"),
    # Hyperliquid 是链上 DEX，没有 api key/secret 这对概念。适配器的映射是：
    # api_key = 主钱包地址（查询持仓用，公开信息）、api_secret = API Wallet 私钥
    # （签名用，网页上由主账户授权生成）。字段名不改——vault 的存取、
    # Credential 数据类、六家的界面全都按这两个名字走，改名的代价是全线迁移；
    # 用户看到的是 VENUE_FIELD_LABELS 里的中文，不会被字段名误导。
    Venue.HYPERLIQUID: ("api_key", "api_secret"),
    Venue.KUCOIN: ("api_key", "api_secret", "passphrase"),
    # Backpack 的"API Key"是 base64 的 ED25519 **公钥**，secret 是同对**私钥**——
    # 不是 HMAC 那种共享密钥。字段名沿用，显示名在 VENUE_FIELD_LABELS 里说清
    Venue.BACKPACK: ("api_key", "api_secret"),
}

FIELD_LABELS = {
    "api_key": "API Key",
    "api_secret": "API Secret",
    "passphrase": "Passphrase",
}

# 按交易所覆盖字段的显示名。Hyperliquid 的"API Key"实际是主钱包地址，
# 照六家的标签显示会让用户把私钥填进地址框——标签必须说人话。
VENUE_FIELD_LABELS: dict[Venue, dict[str, str]] = {
    Venue.HYPERLIQUID: {
        "api_key": "主钱包地址（0x…）",
        "api_secret": "API Wallet 私钥（网页授权生成，不是主钱包私钥）",
    },
    Venue.BACKPACK: {
        "api_key": "API Key（base64 的 ED25519 公钥）",
        "api_secret": "API Secret（base64 的 ED25519 私钥）",
    },
}


def _adapter_class(venue: Venue):
    """按需导入，避免启动时把六家适配器全加载进来。"""
    if venue is Venue.BINANCE:
        from .exchanges.binance import BinanceAdapter
        return BinanceAdapter
    if venue is Venue.OKX:
        from .exchanges.okx import OkxAdapter
        return OkxAdapter
    if venue is Venue.HTX:
        from .exchanges.htx import HtxAdapter
        return HtxAdapter
    if venue is Venue.GATE:
        from .exchanges.gate import GateAdapter
        return GateAdapter
    if venue is Venue.BYBIT:
        from .exchanges.bybit import BybitAdapter
        return BybitAdapter
    if venue is Venue.BITGET:
        from .exchanges.bitget import BitgetAdapter
        return BitgetAdapter
    if venue is Venue.HYPERLIQUID:
        from .exchanges.hyperliquid import HyperliquidAdapter
        return HyperliquidAdapter
    if venue is Venue.KUCOIN:
        from .exchanges.kucoin import KucoinAdapter
        return KucoinAdapter
    if venue is Venue.BACKPACK:
        from .exchanges.backpack import BackpackAdapter
        return BackpackAdapter
    raise SessionError(f"未知交易所: {venue}")


@dataclass(slots=True)
class AccountInfo:
    """给界面看的账户摘要。绝不含任何凭据材料。"""

    id: str
    venue: Venue
    alias: str
    disabled: bool
    connected: bool = False
    error: str = ""
    equity: Decimal | None = None


@dataclass(slots=True)
class FeeBookEntry:
    """一个市场的真实费率档位缓存。

    `by_symbol` 是逐品种的档位（Bybit/Gate/HTX 这么给），`account_default` 是
    账户级档位（币安/OKX/Bitget 的合约费率一档吃全市场，symbol 返回空串）。
    查不到的品种**留空**，`fee_for` 返回 None，让上层退回默认挂牌价并在界面标出来——
    绝不拿别的品种的档位替它猜。
    """

    market: str
    by_symbol: dict[str, FeeSchedule]
    account_default: FeeSchedule | None
    fetched_at: float
    note: str = ""

    def is_stale(self, now: float) -> bool:
        return now - self.fetched_at > FEE_CACHE_TTL_S


@dataclass(slots=True)
class ValidationResult:
    """加账户前的连通性自检。

    宁可在这里报错，也别等下单时才发现权限没开、账户模式不对。
    这些坑是 taoli.tools 文档里用一整章教用户避的，我们直接做成程序自检。
    """

    ok: bool
    message: str
    clock_drift_ms: int = 0
    warnings: tuple[str, ...] = ()


class Session:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.storage = Storage(config.db_path)
        self.vault = Vault()
        self.adapters = AdapterRegistry()
        self._paper_mode = True          # 默认纸上模式，要真下单必须显式切
        self._connected: dict[str, ExchangeAdapter] = {}
        # 真实费率档位缓存，按 market key（"binance:perp"）索引
        self._fees: dict[str, FeeBookEntry] = {}
        self.fee_errors: dict[str, str] = {}

    # ---- 主密码 ---------------------------------------------------------

    @property
    def is_initialized(self) -> bool:
        """是否已经设过主密码。"""
        return self.storage.get_vault_header() is not None

    @property
    def is_unlocked(self) -> bool:
        return self.vault.is_unlocked

    def initialize(self, password: str) -> None:
        """首次设置主密码。"""
        if self.is_initialized:
            raise SessionError("主密码已设置过。要换密码请用 change_password。")
        header = self.vault.initialize(password)
        self.storage.set_vault_header(header)

    def unlock(self, password: str) -> None:
        header = self.storage.get_vault_header()
        if header is None:
            raise SessionError("还没设过主密码")
        self.vault.unlock(password, header)      # 密码错会抛 BadPassword

    def lock(self) -> None:
        self.vault.lock()
        self._connected.clear()
        # 档位是账户数据，锁库就得跟着丢——不然锁上之后机会榜还在拿
        # 前一个账户的 VIP 档算成本，用户完全看不出来
        self._fees.clear()
        self.fee_errors.clear()

    def change_password(self, old: str, new: str) -> None:
        self.unlock(old)
        self.storage.rotate_password(self.vault, new)

    # ---- 纸上 / 实盘 ----------------------------------------------------

    @property
    def paper_mode(self) -> bool:
        return self._paper_mode

    def set_paper_mode(self, enabled: bool) -> None:
        """切实盘是要真花钱的，界面上必须二次确认后才调这里。"""
        self._paper_mode = enabled

    # ---- 账户 -----------------------------------------------------------

    def list_accounts(self) -> list[AccountInfo]:
        return [
            AccountInfo(id=r.id, venue=r.venue, alias=r.alias, disabled=r.disabled,
                        connected=r.id in self._connected)
            for r in self.storage.list_accounts()
        ]

    async def validate_credential(self, venue: Venue, credential: dict) -> ValidationResult:
        """在存之前先连一次，把能提前发现的坑全查掉。"""
        missing = [f for f in CREDENTIAL_FIELDS[venue] if not credential.get(f)]
        if missing:
            return ValidationResult(
                False, f"缺少必填项：{'、'.join(FIELD_LABELS[f] for f in missing)}")

        adapter = self._build_adapter(venue, credential)
        warnings: list[str] = []
        try:
            drift = abs(await adapter.sync_clock())
            if drift > 3000:
                return ValidationResult(
                    False,
                    f"本机时钟与 {venue.value} 相差 {drift} 毫秒。"
                    "超过 3 秒交易所会直接拒签，请先校准系统时间。",
                    clock_drift_ms=drift)
            if drift > 1000:
                warnings.append(f"时钟偏差 {drift} 毫秒，偏大但还能用，建议校时")

            positions = await adapter.fetch_positions()
            _ = positions      # 能读持仓说明读权限没问题
            return ValidationResult(True, "连接成功，读权限正常",
                                    clock_drift_ms=drift, warnings=tuple(warnings))
        except ExchangeError as exc:
            return ValidationResult(False, f"{venue.value} 返回错误：{exc.message}")
        except Exception as exc:
            return ValidationResult(False, f"连接失败：{exc}")
        finally:
            await adapter.close()

    def add_account(self, venue: Venue, alias: str, credential: dict, now_ms: int) -> str:
        if not self.is_unlocked:
            raise VaultLocked("凭据库未解锁")
        clean = {k: str(v).strip() for k, v in credential.items()
                 if k in CREDENTIAL_FIELDS[venue] and v}
        return self.storage.add_account(venue, alias.strip(), clean, self.vault, now_ms)

    def delete_account(self, account_id: str) -> None:
        self._connected.pop(account_id, None)
        self.storage.delete_account(account_id)

    def _build_adapter(self, venue: Venue, credential: dict) -> ExchangeAdapter:
        cls = _adapter_class(venue)
        cred = Credential(
            api_key=credential.get("api_key", ""),
            api_secret=credential.get("api_secret", ""),
            passphrase=credential.get("passphrase", ""),
            uid=credential.get("uid", ""),
        )
        return cls(cred, broker_code=self.config.broker.for_venue(venue.value))

    async def connect_all(self) -> dict[str, str]:
        """连上所有已启用的账户。返回 {account_id: 错误信息}，空表示全成功。

        连上之后顺手把真实费率档位拉一次缓存住：成本占了机会判决的绝对主导，
        VIP3 用户的 taker 可能只有挂牌价的一半，用默认档算等于系统性错判。
        档位拉失败**不影响连接**——机会榜退回默认挂牌价，界面上会标出来。
        """
        if not self.is_unlocked:
            raise VaultLocked("凭据库未解锁")
        errors: dict[str, str] = {}
        for row in self.storage.list_accounts():
            if row.disabled or row.id in self._connected:
                continue
            try:
                cred = self.storage.open_credential(row, self.vault)
                adapter = self._build_adapter(row.venue, cred)
                await adapter.sync_clock()
                self._connected[row.id] = adapter
                self.adapters.register(f"{row.venue.value}:perp", adapter)
            except Exception as exc:
                errors[row.id] = str(exc)
        await self.refresh_fees()
        return errors

    # ---- 费率档位 -------------------------------------------------------

    async def refresh_fees(self, *, force: bool = False,
                           now: float | None = None) -> dict[str, str]:
        """拉每个已连市场的真实费率档位并缓存。返回 {market: 错误信息}。

        **不带 symbol 调一次就够**：六家在不传符号时的行为恰好都是我们要的——
        Bybit/Gate/HTX 一把返回整个市场的逐品种表，币安/OKX/Bitget 返回账户级
        档位（symbol 为空串，兜底全市场）。一个签名请求换一家的全部档位，
        比逐个符号查省两三个数量级的配额。

        档位一天变不了一次，所以有缓存就不重拉（force=True 可以强刷）。
        """
        now = time.time() if now is None else now
        errors: dict[str, str] = {}
        for market in self.adapters.markets():
            cached = self._fees.get(market)
            if cached is not None and not force and not cached.is_stale(now):
                continue
            try:
                schedules = await self.adapters.get(market).fetch_fee_schedule(())
            except Exception as exc:
                # 拉不到不是致命错：机会榜退回默认挂牌价，界面标"按默认费率估算"。
                # 但**绝不能**把旧缓存留着假装还有效——那会让用户以为算的是真档位
                errors[market] = str(exc)
                self._fees.pop(market, None)
                continue
            by_symbol = {s.symbol: s for s in schedules if s.symbol}
            account_default = next((s for s in schedules if not s.symbol), None)
            note = next((s.note for s in schedules if s.note), "")
            self._fees[market] = FeeBookEntry(
                market=market, by_symbol=by_symbol,
                account_default=account_default, fetched_at=now, note=note,
            )
        self.fee_errors = errors
        return errors

    def fee_for(self, market: str, symbol: str) -> FeeSchedule | None:
        """该品种的真实费率档位。**None = 没有，请退回默认挂牌价并标注出来。**

        查不到具体品种时只回退到**账户级**档位（币安/OKX/Bitget 本来就是一档
        吃全市场）。绝不拿同市场另一个品种的档位替它猜：Bybit 的做市商减免是
        逐 symbol 给的，猜错的方向恰好是低估成本。
        """
        entry = self._fees.get(market)
        if entry is None:
            return None
        hit = entry.by_symbol.get(symbol)
        if hit is not None:
            return hit
        return entry.account_default

    @property
    def has_real_fees(self) -> bool:
        """有没有任何一个市场拿到了真实档位。界面据此决定要不要标"按默认费率估算"。"""
        return any(e.by_symbol or e.account_default for e in self._fees.values())

    def fee_notes(self) -> tuple[str, ...]:
        """档位附注（如 BNB 抵扣状态、OKX 等级），去重后给界面显示。"""
        out: list[str] = []
        seen: set[str] = set()
        for entry in self._fees.values():
            if entry.note and entry.note not in seen:
                seen.add(entry.note)
                out.append(f"{entry.market}：{entry.note}")
        return tuple(out)

    async def close(self) -> None:
        await asyncio.gather(*(a.close() for a in self._connected.values()),
                             return_exceptions=True)
        self._connected.clear()
        self._fees.clear()
        self.storage.close()
