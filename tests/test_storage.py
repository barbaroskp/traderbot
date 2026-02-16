"""Tests for SQLite storage layer."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.storage import Storage


class TestStorage:
    def test_schema_created(self, db) -> None:
        """All tables exist after init."""
        tables = db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        names = {t["name"] for t in tables}
        expected = {
            "contracts", "errors", "fills", "market_stats", "orders",
            "pnl_daily", "positions", "risk_state_log", "runs", "signals",
        }
        assert expected.issubset(names)

    def test_insert_and_fetch(self, db) -> None:
        row_id = db.insert("runs", {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "mode": "paper",
            "config_json": "{}",
        })
        assert row_id > 0

        rows = db.fetch_all("SELECT * FROM runs")
        assert len(rows) == 1
        assert rows[0]["mode"] == "paper"

    def test_upsert_contract(self, db) -> None:
        now = datetime.now(timezone.utc).isoformat()
        db.upsert_contract({
            "symbol": "BTC-USDT",
            "base_asset": "BTC",
            "quote_asset": "USDT",
            "status": "TRADING",
            "tick_size": 0.1,
            "step_size": 0.001,
            "min_qty": 0.001,
            "max_leverage": 125,
            "updated_at": now,
        })

        # Update
        db.upsert_contract({
            "symbol": "BTC-USDT",
            "base_asset": "BTC",
            "quote_asset": "USDT",
            "status": "TRADING",
            "tick_size": 0.01,  # changed
            "step_size": 0.001,
            "min_qty": 0.001,
            "max_leverage": 125,
            "updated_at": now,
        })

        rows = db.fetch_all("SELECT * FROM contracts WHERE symbol='BTC-USDT'")
        assert len(rows) == 1
        assert rows[0]["tick_size"] == 0.01

    def test_open_positions_query(self, db) -> None:
        now = datetime.now(timezone.utc).isoformat()
        db.insert("positions", {
            "symbol": "BTC-USDT", "side": "LONG", "entry_price": 50000,
            "qty": 0.001, "notional": 50, "opened_at": now, "status": "OPEN", "is_paper": 1,
        })
        db.insert("positions", {
            "symbol": "ETH-USDT", "side": "SHORT", "entry_price": 3000,
            "qty": 0.01, "notional": 30, "opened_at": now, "status": "CLOSED", "is_paper": 1,
        })

        open_pos = db.get_open_positions()
        assert len(open_pos) == 1
        assert open_pos[0]["symbol"] == "BTC-USDT"

    def test_log_error(self, db) -> None:
        db.log_error("test", "ValueError", "something broke")
        errors = db.fetch_all("SELECT * FROM errors")
        assert len(errors) == 1
        assert errors[0]["component"] == "test"

    def test_log_risk_state(self, db) -> None:
        db.log_risk_state("TIGHT", "consec_losses=3", consecutive_losses=3)
        logs = db.fetch_all("SELECT * FROM risk_state_log")
        assert len(logs) == 1
        assert logs[0]["state"] == "TIGHT"

    def test_idempotent_schema(self, tmp_path) -> None:
        """Creating Storage twice on same DB doesn't error."""
        path = tmp_path / "double.db"
        s1 = Storage(str(path))
        s2 = Storage(str(path))
        s1.close()
        s2.close()
