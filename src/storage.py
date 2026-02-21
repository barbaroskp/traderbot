"""SQLite persistence layer.

All tables are created on first access (idempotent).
Thread-safe via check_same_thread=False + serialised writes.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator

from src.logger import get_logger

log = get_logger(__name__)

# ── Schema DDL ──────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT    NOT NULL,
    mode        TEXT    NOT NULL,        -- paper | live
    config_json TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS contracts (
    symbol          TEXT PRIMARY KEY,
    base_asset      TEXT NOT NULL,
    quote_asset     TEXT NOT NULL,
    status          TEXT NOT NULL,
    tick_size       REAL,
    step_size       REAL,
    min_qty         REAL,
    max_leverage    INTEGER,
    volume_24h      REAL DEFAULT 0,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_stats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    ts          TEXT    NOT NULL,
    mid_price   REAL,
    mark_price  REAL,
    best_bid    REAL,
    best_ask    REAL,
    spread_bps  REAL,
    bid_depth   REAL,
    ask_depth   REAL,
    fast_ema    REAL,
    slow_ema    REAL,
    z_score_bps REAL
);
CREATE INDEX IF NOT EXISTS idx_ms_symbol_ts ON market_stats(symbol, ts);

CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    side        TEXT    NOT NULL,        -- LONG | SHORT
    z_score_bps REAL    NOT NULL,
    mid_price   REAL    NOT NULL,
    fast_ema    REAL,
    slow_ema    REAL,
    spread_bps  REAL,
    depth_usdt  REAL,
    accepted    INTEGER NOT NULL DEFAULT 0,
    reject_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_sig_ts ON signals(ts);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT    UNIQUE NOT NULL,
    exchange_order_id TEXT,
    ts              TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    side            TEXT    NOT NULL,
    order_type      TEXT    NOT NULL,    -- LIMIT | STOP_MARKET | TAKE_PROFIT
    price           REAL,
    qty             REAL    NOT NULL,
    status          TEXT    NOT NULL,    -- PENDING | FILLED | PARTIAL | CANCELLED | REJECTED
    filled_qty      REAL    DEFAULT 0,
    avg_fill_price  REAL,
    is_paper        INTEGER NOT NULL DEFAULT 1,
    parent_order_id TEXT,
    reduce_only     INTEGER DEFAULT 0,
    updated_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ord_symbol ON orders(symbol, status);
CREATE INDEX IF NOT EXISTS idx_ord_client ON orders(client_order_id);

CREATE TABLE IF NOT EXISTS fills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    ts          TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    side        TEXT    NOT NULL,
    price       REAL    NOT NULL,
    qty         REAL    NOT NULL,
    fee         REAL    DEFAULT 0,
    is_paper    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,
    side            TEXT    NOT NULL,
    entry_price     REAL    NOT NULL,
    qty             REAL    NOT NULL,
    notional        REAL    NOT NULL,
    unrealised_pnl  REAL    DEFAULT 0,
    realised_pnl    REAL    DEFAULT 0,
    sl_order_id     TEXT,
    tp_order_id     TEXT,
    opened_at       TEXT    NOT NULL,
    closed_at       TEXT,
    status          TEXT    NOT NULL DEFAULT 'OPEN',  -- OPEN | CLOSED
    is_paper        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_pos_status ON positions(status);

CREATE TABLE IF NOT EXISTS pnl_daily (
    date            TEXT PRIMARY KEY,
    realised_pnl    REAL DEFAULT 0,
    unrealised_pnl  REAL DEFAULT 0,
    total_trades    INTEGER DEFAULT 0,
    winning_trades  INTEGER DEFAULT 0,
    total_notional  REAL DEFAULT 0,
    balance_usdt    REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS risk_state_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    state       TEXT    NOT NULL,        -- NORMAL | TIGHT | ULTRA_TIGHT
    reason      TEXT,
    consecutive_losses  INTEGER DEFAULT 0,
    drawdown_pct        REAL DEFAULT 0,
    api_error_rate      REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    component   TEXT    NOT NULL,
    error_type  TEXT    NOT NULL,
    message     TEXT,
    traceback   TEXT
);
"""


class Storage:
    """Thin wrapper around sqlite3 with context-managed connections."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._ensure_schema()

    # ── Connection helpers ──────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                timeout=30,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def _ensure_schema(self) -> None:
        conn = self._get_conn()
        conn.executescript(_SCHEMA)
        self._ensure_columns()
        conn.commit()
        log.info("storage schema ensured", extra={"db": str(self.db_path)})

    def _ensure_columns(self) -> None:
        """Idempotent column migrations for existing DBs."""
        conn = self._get_conn()

        def _cols(table: str) -> set[str]:
            cur = conn.execute(f"PRAGMA table_info({table})")
            return {row[1] for row in cur.fetchall()}

        pos_cols = _cols("positions")
        ord_cols = _cols("orders")

        # Positions: trailing / partial TP / breakeven / dynamic TP
        if "tp1_order_id" not in pos_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN tp1_order_id TEXT")
        if "sl_bps" not in pos_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN sl_bps REAL")
        if "tp_bps" not in pos_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN tp_bps REAL")
        if "original_qty" not in pos_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN original_qty REAL")
        if "remaining_qty" not in pos_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN remaining_qty REAL")
        if "partial_tp_filled" not in pos_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN partial_tp_filled INTEGER DEFAULT 0")
        if "breakeven_triggered" not in pos_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN breakeven_triggered INTEGER DEFAULT 0")
        if "highest_price" not in pos_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN highest_price REAL")
        if "lowest_price" not in pos_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN lowest_price REAL")
        if "leverage" not in pos_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN leverage INTEGER DEFAULT 1")

        # Orders: TP level
        if "tp_level" not in ord_cols:
            conn.execute("ALTER TABLE orders ADD COLUMN tp_level INTEGER DEFAULT 0")

        # Contracts: volume
        contract_cols = _cols("contracts")
        if "volume_24h" not in contract_cols:
            conn.execute("ALTER TABLE contracts ADD COLUMN volume_24h REAL DEFAULT 0")

        # Initialize defaults for existing rows
        conn.execute(
            "UPDATE positions SET original_qty=qty WHERE original_qty IS NULL"
        )
        conn.execute(
            "UPDATE positions SET remaining_qty=qty WHERE remaining_qty IS NULL"
        )
        conn.execute(
            "UPDATE positions SET highest_price=entry_price WHERE highest_price IS NULL"
        )
        conn.execute(
            "UPDATE positions SET lowest_price=entry_price WHERE lowest_price IS NULL"
        )

    @contextmanager
    def cursor(self) -> Generator[sqlite3.Cursor, None, None]:
        conn = self._get_conn()
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ── Generic helpers ─────────────────────────────────────────

    def insert(self, table: str, data: dict[str, Any]) -> int:
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"  # noqa: S608
        with self.cursor() as cur:
            cur.execute(sql, list(data.values()))
            return cur.lastrowid or 0

    def upsert_contract(self, data: dict[str, Any]) -> None:
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        updates = ", ".join(f"{k}=excluded.{k}" for k in data if k != "symbol")
        sql = (
            f"INSERT INTO contracts ({cols}) VALUES ({placeholders}) "  # noqa: S608
            f"ON CONFLICT(symbol) DO UPDATE SET {updates}"
        )
        with self.cursor() as cur:
            cur.execute(sql, list(data.values()))

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self.cursor() as cur:
            cur.execute(sql, params)

    # ── Domain helpers ──────────────────────────────────────────

    def get_open_positions(self, is_paper: bool = True) -> list[dict[str, Any]]:
        return self.fetch_all(
            "SELECT * FROM positions WHERE status='OPEN' AND is_paper=?",
            (1 if is_paper else 0,),
        )

    def get_pending_orders(self, symbol: str, is_paper: bool = True) -> list[dict[str, Any]]:
        return self.fetch_all(
            "SELECT * FROM orders WHERE symbol=? AND status='PENDING' AND is_paper=?",
            (symbol, 1 if is_paper else 0),
        )

    def get_recent_signals(self, symbol: str, minutes: int = 60) -> list[dict[str, Any]]:
        cutoff = datetime.now(timezone.utc).isoformat()
        return self.fetch_all(
            "SELECT * FROM signals WHERE symbol=? AND ts>=? ORDER BY ts DESC",
            (symbol, cutoff),
        )

    def get_contracts(self) -> list[dict[str, Any]]:
        return self.fetch_all(
            "SELECT * FROM contracts WHERE status IN ('TRADING', '1', 'ONLINE')"
        )

    def log_error(self, component: str, error_type: str, message: str, tb: str = "") -> None:
        self.insert(
            "errors",
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "component": component,
                "error_type": error_type,
                "message": message,
                "traceback": tb,
            },
        )

    def log_risk_state(
        self,
        state: str,
        reason: str,
        consecutive_losses: int = 0,
        drawdown_pct: float = 0.0,
        api_error_rate: float = 0.0,
    ) -> None:
        self.insert(
            "risk_state_log",
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "state": state,
                "reason": reason,
                "consecutive_losses": consecutive_losses,
                "drawdown_pct": drawdown_pct,
                "api_error_rate": api_error_rate,
            },
        )

    def prune_runtime_data(self, retention_days: int) -> dict[str, int]:
        """Prune high-volume runtime tables older than retention window."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(retention_days, 1))).isoformat()
        deleted: dict[str, int] = {}

        with self.cursor() as cur:
            for table, ts_col in (
                ("market_stats", "ts"),
                ("signals", "ts"),
                ("errors", "ts"),
                ("risk_state_log", "ts"),
            ):
                cur.execute(f"DELETE FROM {table} WHERE {ts_col} < ?", (cutoff,))
                deleted[table] = cur.rowcount if cur.rowcount is not None else 0

        return deleted

    # Alias for CLI / backtest compatibility
    cleanup_old_data = prune_runtime_data

    def checkpoint_and_vacuum(self, vacuum: bool = False) -> None:
        """Compact WAL and optionally VACUUM database file."""
        conn = self._get_conn()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if vacuum:
            conn.execute("VACUUM")

    def vacuum(self) -> None:
        """Reclaim disk space after deletions."""
        conn = self._get_conn()
        conn.execute("VACUUM")

    def db_size_mb(self) -> float:
        """Return database file size in MB."""
        if self.db_path.exists():
            return self.db_path.stat().st_size / (1024 * 1024)
        return 0.0

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
