"""accounts 表的 kind 分类（trade / analysis）与 v1→v2 迁移。

复盘凭据和套利账户同库同加密，靠 kind 列隔离。这里钉住三件事：
默认查询永远只见套利账户（connect_all 的语义不能因为加了复盘凭据而变）、
换主密码时全量重加密不漏任何一类、老库打开时自动补上 kind 列。
"""

from __future__ import annotations

import sqlite3

import pytest

from earnfarm.models import Venue
from earnfarm.storage import SCHEMA_VERSION, Storage
from earnfarm.vault import Vault

_PASSWORD = "test-password-123"


@pytest.fixture
def storage(tmp_path):
    st = Storage(tmp_path / "test.db")
    yield st
    st.close()


@pytest.fixture
def vault(storage):
    v = Vault()
    storage.set_vault_header(v.initialize(_PASSWORD))
    return v


def test_trade_and_analysis_accounts_are_isolated(storage, vault):
    """默认只见 trade：套利连接绝不能把复盘专用凭据当成可交易账户拿去连。"""
    trade_id = storage.add_account(
        Venue.BINANCE, "主账户", {"apiKey": "t"}, vault, now_ms=1, kind="trade")
    ana_id = storage.add_account(
        Venue.BINANCE, "复盘只读", {"apiKey": "a"}, vault, now_ms=2, kind="analysis")

    assert [r.id for r in storage.list_accounts()] == [trade_id]
    assert [r.id for r in storage.list_accounts(kind="analysis")] == [ana_id]
    assert {r.id for r in storage.list_accounts(kind=None)} == {trade_id, ana_id}


def test_rotate_password_reencrypts_analysis_credentials(storage, vault, tmp_path):
    """rotate_password 必须覆盖 analysis 凭据。

    重加密如果只取默认的 kind="trade"，分析类凭据仍是旧密钥的密文，
    而旧密钥随换密码即弃——漏掉分析类凭据会让它们在换密码后永远解不开，
    且当场毫无症状，直到下次打开复盘页才炸。所以用"新密码 + 全新 Vault"
    走一遍完整解锁链来验证，而不是只查行数。
    """
    cred = {"apiKey": "ana-key", "apiSecret": "ana-secret"}
    ana_id = storage.add_account(
        Venue.OKX, "复盘", cred, vault, now_ms=1, kind="analysis")

    storage.rotate_password(vault, "new-password-456")

    # 全新 Vault 模拟重启：header 必须从库里读，密钥链一点旧状态都不能带
    reborn = Vault()
    header = storage.get_vault_header()
    assert header is not None
    reborn.unlock("new-password-456", header)
    row = next(r for r in storage.list_accounts(kind="analysis") if r.id == ana_id)
    assert storage.open_credential(row, reborn) == cred


def test_reset_vault_clears_accounts_and_header(storage, vault):
    """忘记主密码的出路：重置后回到"未设置"状态，可重新 initialize。"""
    storage.add_account(Venue.BINANCE, "a", {"apiKey": "k"}, vault, 1, kind="trade")
    storage.add_account(Venue.BINANCE, "b", {"apiKey": "k"}, vault, 2, kind="analysis")

    storage.reset_vault()

    assert storage.list_accounts(kind=None) == []
    assert storage.get_vault_header() is None
    # 重置后能用新密码重新开始
    v2 = Vault()
    storage.set_vault_header(v2.initialize("brand-new-pass"))
    assert storage.get_vault_header() is not None


def test_reset_vault_refuses_when_hedges_reference_accounts(storage, vault):
    """有对冲记录时拒绝重置：静默删掉被引用的账户会让历史对不上账。"""
    aid = storage.add_account(Venue.BINANCE, "a", {"apiKey": "k"}, vault, 1)
    from decimal import Decimal

    from earnfarm.models import Hedge, HedgeStatus, Leg
    storage.upsert_hedge(Hedge(
        id="h1",
        long=Leg("binance:perp", "BTCUSDT", aid),
        short=Leg("bybit:perp", "BTCUSDT", aid),
        target=Decimal("1"), multiplier=Decimal("1"),
        status=HedgeStatus.RUNNING, strategy_id=None,
    ), now_ms=3)

    from earnfarm.storage import StorageError
    with pytest.raises(StorageError, match="对冲"):
        storage.reset_vault()


def test_v1_database_gains_kind_column_on_open(tmp_path):
    """v1 老库（accounts 没有 kind 列）打开即迁移，老账户默认归为 trade。"""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    # 照 _SCHEMA 的 v1 形态手工建库：CREATE IF NOT EXISTS 不会给已有表补列，
    # 迁移必须靠 _migrate 的显式 ALTER，这正是本测试要钉住的路径
    conn.executescript("""
        CREATE TABLE meta (
            key   TEXT PRIMARY KEY,
            value BLOB NOT NULL
        );
        CREATE TABLE accounts (
            id         TEXT PRIMARY KEY,
            venue      TEXT NOT NULL,
            alias      TEXT NOT NULL,
            credential BLOB NOT NULL,
            disabled   INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            UNIQUE (venue, alias)
        );
    """)
    conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (b"1",))
    conn.execute(
        "INSERT INTO accounts(id, venue, alias, credential, disabled, created_at) "
        "VALUES('acct1', 'binance', '老账户', X'00', 0, 1)")
    conn.commit()
    conn.close()

    st = Storage(db)   # 打开即迁移，不应报错
    try:
        cols = {r["name"] for r in st._conn.execute("PRAGMA table_info(accounts)")}
        assert "kind" in cols
        rows = st.list_accounts(kind=None)
        assert [r.id for r in rows] == ["acct1"]
        assert rows[0].kind == "trade", "老账户全是套利账户，默认值必须是 trade"
        assert st.get_meta("schema_version") == str(SCHEMA_VERSION)
        assert SCHEMA_VERSION == 2
    finally:
        st.close()


# ---- 加密小密钥（secret:* 行）--------------------------------------------

def test_secret_save_load_round_trip_and_listing(storage, vault):
    """AI API key 这类小秘密与账户凭据同一把主密码加密。
    读不存在的名字必须是 None 而不是抛错——调用方靠它走"未保存"分支；
    同名重存是覆盖不是叠加，list 里不能出现重复行。"""
    storage.save_secret("ai:deepseek", {"api_key": "sk-test"}, vault)
    storage.save_secret("ai:xai", {"api_key": "xai-test"}, vault)

    assert storage.load_secret("ai:deepseek", vault) == {"api_key": "sk-test"}
    assert storage.load_secret("ai:nope", vault) is None
    assert sorted(storage.list_secret_names()) == ["ai:deepseek", "ai:xai"]

    storage.save_secret("ai:deepseek", {"api_key": "sk-new"}, vault)
    assert storage.load_secret("ai:deepseek", vault) == {"api_key": "sk-new"}
    assert sorted(storage.list_secret_names()) == ["ai:deepseek", "ai:xai"]


def test_reset_vault_also_clears_secrets(storage, vault):
    """重置主密码必须连 secret:* 一起清：主密码一弃这些密文永远解不开，
    留着只会让界面上的"已保存 ✓"变成谎言。"""
    storage.save_secret("ai:deepseek", {"api_key": "k"}, vault)
    storage.reset_vault()
    assert storage.list_secret_names() == []


# ---- session 层 AI key（锁着时的行为）------------------------------------

def test_session_ai_key_locked_save_raises_load_returns_none(tmp_path):
    """锁着时 save 必须抛 VaultLocked、load 必须静默 None：
    save 静默丢弃会让用户以为存上了；load 抛错则会把"回退到环境变量"
    这条正常路径变成报错。"""
    from dataclasses import replace

    from earnfarm.config import Config
    from earnfarm.session import Session
    from earnfarm.vault import VaultLocked

    session = Session(replace(Config(), data_dir=tmp_path))
    try:
        with pytest.raises(VaultLocked):
            session.save_ai_key("deepseek", "sk-x")
        assert session.load_ai_key("deepseek") is None
        # 什么都没存过，锁着也能回答"没保存"
        assert not session.has_ai_key("deepseek")
    finally:
        session.storage.close()
