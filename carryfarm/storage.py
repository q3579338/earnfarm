"""SQLite 持久化层。

设计要点：
- 凭据只以密文形式落盘（见 vault.py），本模块不接触明文
- 所有金额用 TEXT 存 Decimal 的字符串形式，绝不用 REAL（浮点会丢精度）
- 写操作走事务，凭据轮换等多表操作必须原子
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterator, Sequence

from .models import Hedge, HedgeStatus, Leg, Venue
from .vault import Vault, VaultHeader

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id         TEXT PRIMARY KEY,
    venue      TEXT NOT NULL,
    alias      TEXT NOT NULL,
    credential BLOB NOT NULL,          -- AES-GCM 密文，明文永不落盘
    disabled   INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    UNIQUE (venue, alias)
);

CREATE TABLE IF NOT EXISTS hedges (
    id             TEXT PRIMARY KEY,
    long_market    TEXT NOT NULL,
    long_symbol    TEXT NOT NULL,
    long_account   TEXT NOT NULL REFERENCES accounts(id),
    short_market   TEXT NOT NULL,
    short_symbol   TEXT NOT NULL,
    short_account  TEXT NOT NULL REFERENCES accounts(id),
    target         TEXT NOT NULL,
    multiplier     TEXT NOT NULL,
    status         TEXT NOT NULL,
    strategy_id    TEXT,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hedges_status ON hedges(status);

CREATE TABLE IF NOT EXISTS fills (
    id          TEXT PRIMARY KEY,
    hedge_id    TEXT NOT NULL REFERENCES hedges(id),
    market      TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,
    qty         TEXT NOT NULL,
    price       TEXT NOT NULL,
    fee         TEXT NOT NULL,
    fee_asset   TEXT NOT NULL,
    reason      TEXT NOT NULL,          -- fill / liquidation / adl / retreat
    ts_ms       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fills_hedge ON fills(hedge_id, ts_ms);

CREATE TABLE IF NOT EXISTS carry_ledger (
    id        TEXT PRIMARY KEY,
    hedge_id  TEXT NOT NULL REFERENCES hedges(id),
    market    TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    amount    TEXT NOT NULL,           -- 有符号：正=收到
    asset     TEXT NOT NULL,
    kind      TEXT NOT NULL,           -- funding / interest
    ts_ms     INTEGER NOT NULL,
    UNIQUE (hedge_id, market, symbol, ts_ms, kind)
);

CREATE TABLE IF NOT EXISTS funding_history (
    market     TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    settle_ms  INTEGER NOT NULL,
    rate       TEXT NOT NULL,
    interval_s INTEGER NOT NULL,
    PRIMARY KEY (market, symbol, settle_ms)
);

CREATE TABLE IF NOT EXISTS events (
    id       TEXT PRIMARY KEY,
    level    TEXT NOT NULL,            -- info / warn / error / critical
    category TEXT NOT NULL,            -- execution / safety / account / system
    hedge_id TEXT,
    message  TEXT NOT NULL,
    detail   TEXT,
    ts_ms    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts_ms DESC);
"""


class StorageError(Exception):
    pass


@dataclass(slots=True)
class AccountRow:
    id: str
    venue: Venue
    alias: str
    credential: bytes
    disabled: bool


def _dec(value: str) -> Decimal:
    return Decimal(value)


class Storage:
    """数据库门面。所有 SQL 都收在这里，业务层不写 SQL。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._ensure_version()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # ---- meta / vault header -------------------------------------------

    def _ensure_version(self) -> None:
        row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION).encode(),),
            )
            return
        found = int(bytes(row["value"]).decode())
        if found > SCHEMA_VERSION:
            raise StorageError(
                f"数据库 schema 版本 {found} 高于本程序支持的 {SCHEMA_VERSION}，请升级程序"
            )

    def set_meta(self, key: str, value: str) -> None:
        """通用 meta 写入。守护模式的心跳走这里：一行、覆盖写、不进 events 表。

        心跳绝不能写成事件——60 秒一条，一天 1440 行，几天就把 events 表淹了，
        真正要看的告警会被冲到几百行以外。
        """
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value.encode("utf-8")),
        )

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return bytes(row["value"]).decode("utf-8")
        except UnicodeDecodeError:
            return None

    def get_vault_header(self) -> VaultHeader | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key='vault_header'").fetchone()
        return VaultHeader.from_blob(bytes(row["value"])) if row else None

    def set_vault_header(self, header: VaultHeader, conn: sqlite3.Connection | None = None) -> None:
        (conn or self._conn).execute(
            "INSERT INTO meta(key, value) VALUES('vault_header', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (header.to_blob(),),
        )

    # ---- accounts -------------------------------------------------------

    def add_account(self, venue: Venue, alias: str, credential: dict, vault: Vault,
                    now_ms: int) -> str:
        account_id = uuid.uuid4().hex
        # aad 绑定账户ID，防止密文被挪到另一个账户行上冒用
        blob = vault.seal(credential, aad=account_id.encode())
        try:
            self._conn.execute(
                "INSERT INTO accounts(id, venue, alias, credential, disabled, created_at) "
                "VALUES(?,?,?,?,0,?)",
                (account_id, venue.value, alias, blob, now_ms),
            )
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"{venue.value} 下已存在别名为 {alias!r} 的账户") from exc
        return account_id

    def list_accounts(self) -> list[AccountRow]:
        rows = self._conn.execute(
            "SELECT id, venue, alias, credential, disabled FROM accounts ORDER BY venue, alias"
        ).fetchall()
        return [
            AccountRow(
                id=r["id"], venue=Venue(r["venue"]), alias=r["alias"],
                credential=bytes(r["credential"]), disabled=bool(r["disabled"]),
            )
            for r in rows
        ]

    def open_credential(self, row: AccountRow, vault: Vault) -> dict:
        return vault.open(row.credential, aad=row.id.encode())

    def delete_account(self, account_id: str) -> None:
        used = self._conn.execute(
            "SELECT 1 FROM hedges WHERE long_account=? OR short_account=? LIMIT 1",
            (account_id, account_id),
        ).fetchone()
        if used:
            raise StorageError("该账户仍被对冲仓位引用，请先删除相关对冲")
        self._conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    def rotate_password(self, vault: Vault, new_password: str) -> None:
        """换主密码：解开所有凭据重新加密，与新验证头一起原子写回。"""
        rows = self.list_accounts()
        blobs = {r.id: r.credential for r in rows}
        aads = {r.id: r.id.encode() for r in rows}
        header, resealed = vault.rotate(new_password, blobs, aads)
        with self.transaction() as conn:
            self.set_vault_header(header, conn)
            for account_id, blob in resealed.items():
                conn.execute("UPDATE accounts SET credential=? WHERE id=?", (blob, account_id))

    # ---- hedges ---------------------------------------------------------

    def upsert_hedge(self, hedge: Hedge, now_ms: int) -> None:
        self._conn.execute(
            "INSERT INTO hedges(id, long_market, long_symbol, long_account, "
            "short_market, short_symbol, short_account, target, multiplier, status, "
            "strategy_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET target=excluded.target, "
            "multiplier=excluded.multiplier, status=excluded.status, "
            "strategy_id=excluded.strategy_id, updated_at=excluded.updated_at",
            (
                hedge.id, hedge.long.market, hedge.long.symbol, hedge.long.account_id,
                hedge.short.market, hedge.short.symbol, hedge.short.account_id,
                str(hedge.target), str(hedge.multiplier), hedge.status.value,
                hedge.strategy_id, now_ms, now_ms,
            ),
        )

    def list_hedges(self, statuses: Sequence[HedgeStatus] | None = None) -> list[Hedge]:
        sql = ("SELECT id, long_market, long_symbol, long_account, short_market, "
               "short_symbol, short_account, target, multiplier, status, strategy_id FROM hedges")
        params: tuple = ()
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            sql += f" WHERE status IN ({placeholders})"
            params = tuple(s.value for s in statuses)
        rows = self._conn.execute(sql, params).fetchall()
        return [
            Hedge(
                id=r["id"],
                long=Leg(r["long_market"], r["long_symbol"], r["long_account"]),
                short=Leg(r["short_market"], r["short_symbol"], r["short_account"]),
                target=_dec(r["target"]),
                multiplier=_dec(r["multiplier"]),
                status=HedgeStatus(r["status"]),
                strategy_id=r["strategy_id"],
            )
            for r in rows
        ]

    def set_target(self, hedge_id: str, target: Decimal, now_ms: int,
                   only_if_status: HedgeStatus | None = None) -> bool:
        """改目标。带状态条件——一旦对冲被风控打成 reduce_only 或 retreating，
        策略就再也抬不高它的目标，必须人工介入。返回是否真的改了。"""
        sql = "UPDATE hedges SET target=?, updated_at=? WHERE id=?"
        params: list = [str(target), now_ms, hedge_id]
        if only_if_status is not None:
            sql += " AND status=?"
            params.append(only_if_status.value)
        cur = self._conn.execute(sql, params)
        return cur.rowcount > 0

    def set_status(self, hedge_id: str, status: HedgeStatus, now_ms: int) -> None:
        self._conn.execute(
            "UPDATE hedges SET status=?, updated_at=? WHERE id=?",
            (status.value, now_ms, hedge_id),
        )

    # ---- funding history ------------------------------------------------

    def save_funding(self, market: str, symbol: str, interval_s: int,
                     points: Sequence[tuple[int, Decimal]]) -> None:
        self._conn.executemany(
            "INSERT INTO funding_history(market, symbol, settle_ms, rate, interval_s) "
            "VALUES(?,?,?,?,?) ON CONFLICT(market, symbol, settle_ms) DO NOTHING",
            [(market, symbol, ts, str(rate), interval_s) for ts, rate in points],
        )

    def load_funding(self, market: str, symbol: str, since_ms: int) -> list[Decimal]:
        rows = self._conn.execute(
            "SELECT rate FROM funding_history WHERE market=? AND symbol=? AND settle_ms>=? "
            "ORDER BY settle_ms",
            (market, symbol, since_ms),
        ).fetchall()
        return [_dec(r["rate"]) for r in rows]

    def load_funding_series(self, market: str, symbol: str,
                            since_ms: int) -> list[tuple[int, Decimal, int]]:
        """带结算时刻和周期的完整序列，升序。

        load_funding 只给费率，对齐成小时序列时不够用：不知道每条覆盖多少小时，
        就没法把 1h 腿和 8h 腿放到同一根时间轴上比较。
        """
        rows = self._conn.execute(
            "SELECT settle_ms, rate, interval_s FROM funding_history "
            "WHERE market=? AND symbol=? AND settle_ms>=? ORDER BY settle_ms",
            (market, symbol, since_ms),
        ).fetchall()
        return [(int(r["settle_ms"]), _dec(r["rate"]), int(r["interval_s"])) for r in rows]

    def latest_funding_ms(self, market: str, symbol: str) -> int | None:
        """库里最新一条的结算时刻。增量回填就是从这里往后拉。"""
        row = self._conn.execute(
            "SELECT MAX(settle_ms) AS ts FROM funding_history WHERE market=? AND symbol=?",
            (market, symbol),
        ).fetchone()
        return int(row["ts"]) if row and row["ts"] is not None else None

    def earliest_funding_ms(self, market: str, symbol: str) -> int | None:
        """库里最老一条。用来判断历史深度够不够——只看最新一条会漏掉
        「只存了三天却要 90 天回测」这种情况。"""
        row = self._conn.execute(
            "SELECT MIN(settle_ms) AS ts FROM funding_history WHERE market=? AND symbol=?",
            (market, symbol),
        ).fetchone()
        return int(row["ts"]) if row and row["ts"] is not None else None

    def get_history_floor(self, market: str, symbol: str) -> int | None:
        """已探到的交易所回溯墙：该品种在这家所能取到的最老结算时刻。

        没有这个标记的话，凡是历史短于请求天数的品种（新上市的、
        Bitget 那种 270 条硬顶的），每天都会重新深挖一遍拉不到的区间。
        """
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key=?", (f"history_floor:{market}:{symbol}",)
        ).fetchone()
        if row is None:
            return None
        try:
            return int(bytes(row["value"]).decode())
        except ValueError:
            return None

    def set_history_floor(self, market: str, symbol: str, settle_ms: int) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"history_floor:{market}:{symbol}", str(int(settle_ms)).encode()),
        )

    # ---- events ---------------------------------------------------------

    def log_event(self, level: str, category: str, message: str, now_ms: int,
                  hedge_id: str | None = None, detail: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO events(id, level, category, hedge_id, message, detail, ts_ms) "
            "VALUES(?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, level, category, hedge_id, message, detail, now_ms),
        )

    def recent_events(self, limit: int = 200) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM events ORDER BY ts_ms DESC LIMIT ?", (limit,)
        ).fetchall()
