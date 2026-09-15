"""Tests for the backtest engine and DB cleanup."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.backtest import BacktestEngine, BacktestReport, VirtualPosition, print_report
from src.config import Settings
from src.storage import Storage


# ── DB Cleanup Tests ─────────────────────────────────────────────────────────


class TestDBCleanup:
    """Tests for Storage.cleanup_old_data and vacuum."""

    def test_cleanup_deletes_old_market_stats(self, db: Storage) -> None:
        """Old market_stats rows are removed."""
        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        new_ts = datetime.now(timezone.utc).isoformat()

        db.insert("market_stats", {"symbol": "BTC", "ts": old_ts, "mid_price": 100})
        db.insert("market_stats", {"symbol": "BTC", "ts": new_ts, "mid_price": 200})

        deleted = db.cleanup_old_data(retention_days=21)
        assert deleted["market_stats"] == 1

        remaining = db.fetch_all("SELECT * FROM market_stats")
        assert len(remaining) == 1
        assert remaining[0]["mid_price"] == 200

    def test_cleanup_deletes_old_signals(self, db: Storage) -> None:
        """Old signals are removed."""
        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        new_ts = datetime.now(timezone.utc).isoformat()

        db.insert("signals", {
            "ts": old_ts, "symbol": "ETH", "side": "LONG",
            "z_score_bps": 50, "mid_price": 3000, "accepted": 1,
        })
        db.insert("signals", {
            "ts": new_ts, "symbol": "ETH", "side": "SHORT",
            "z_score_bps": -50, "mid_price": 3100, "accepted": 0,
        })

        deleted = db.cleanup_old_data(retention_days=21)
        assert deleted["signals"] == 1

    def test_cleanup_deletes_old_errors(self, db: Storage) -> None:
        """Old errors are removed."""
        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

        db.insert("errors", {
            "ts": old_ts, "component": "test",
            "error_type": "TestError", "message": "old error",
        })
        db.insert("errors", {
            "ts": datetime.now(timezone.utc).isoformat(),
            "component": "test", "error_type": "TestError", "message": "new error",
        })

        deleted = db.cleanup_old_data(retention_days=21)
        assert deleted["errors"] == 1

    def test_cleanup_preserves_recent_data(self, db: Storage) -> None:
        """Recent data within retention window is kept."""
        recent_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()

        db.insert("market_stats", {"symbol": "BTC", "ts": recent_ts, "mid_price": 100})

        deleted = db.cleanup_old_data(retention_days=21)
        assert deleted["market_stats"] == 0

    def test_cleanup_with_empty_tables(self, db: Storage) -> None:
        """Cleanup works on empty tables without error."""
        deleted = db.cleanup_old_data(retention_days=21)
        assert all(v == 0 for v in deleted.values())

    def test_vacuum_runs_without_error(self, db: Storage) -> None:
        """Vacuum completes without exception."""
        db.insert("market_stats", {
            "symbol": "BTC",
            "ts": datetime.now(timezone.utc).isoformat(),
            "mid_price": 100,
        })
        db.vacuum()  # Should not raise

    def test_db_size_mb(self, db: Storage) -> None:
        """db_size_mb returns a non-negative number."""
        size = db.db_size_mb()
        assert size >= 0


# ── Backtest Engine Unit Tests ───────────────────────────────────────────────


class TestBacktestEngine:
    """Unit tests for BacktestEngine internals."""

    @pytest.fixture()
    def bt_cfg(self) -> Settings:
        return Settings(
            bingx_api_key="test",
            bingx_api_secret="test",
            paper_mode=True,
            initial_capital_usdt=100.0,
            db_path=":memory:",
            tp_bps=100.0,
            sl_bps=50.0,
            max_hold_minutes=120,
            fee_rate_bps=4.0,
            use_dynamic_tp_sl=False,
            use_trailing_stop=False,
            use_breakeven_stop=False,
            use_time_decay_sl=False,
            use_kelly_sizing=False,
            use_volatility_sizing=False,
            max_open_positions=5,
        )

    def test_parse_candle_time_ms_epoch(self) -> None:
        """Parse millisecond epoch timestamp."""
        ts = BacktestEngine._parse_candle_time({"time": 1700000000000})
        assert ts.tzinfo is not None
        assert ts.year >= 2023

    def test_parse_candle_time_sec_epoch(self) -> None:
        """Parse second epoch timestamp."""
        ts = BacktestEngine._parse_candle_time({"time": 1700000000})
        assert ts.tzinfo is not None

    def test_parse_candle_time_iso(self) -> None:
        """Parse ISO format timestamp."""
        ts = BacktestEngine._parse_candle_time({"time": "2024-01-15T12:00:00"})
        assert ts.year == 2024

    def test_profit_bps_long(self) -> None:
        """Profit calculation for LONG position."""
        bps = BacktestEngine._profit_bps("LONG", 100.0, 101.0)
        assert bps == pytest.approx(100.0, rel=0.01)

    def test_profit_bps_short(self) -> None:
        """Profit calculation for SHORT position."""
        bps = BacktestEngine._profit_bps("SHORT", 100.0, 99.0)
        assert bps == pytest.approx(100.0, rel=0.01)

    def test_profit_bps_zero_entry(self) -> None:
        """Zero entry price returns 0."""
        assert BacktestEngine._profit_bps("LONG", 0.0, 100.0) == 0.0

    def test_check_exits_sl_long(self, bt_cfg: Settings) -> None:
        """SL triggers for LONG when low <= sl_price."""
        engine = BacktestEngine(bt_cfg, symbols=["TEST-USDT"], days=1)

        pos = VirtualPosition(
            symbol="TEST-USDT",
            side="LONG",
            entry_price=100.0,
            qty=1.0,
            notional=100.0,
            sl_price=99.5,
            tp_price=101.0,
            opened_at=datetime.now(timezone.utc),
        )
        engine._positions.append(pos)

        engine._check_exits("TEST-USDT", close=99.8, high=100.2, low=99.3, ts=datetime.now(timezone.utc))
        assert len(engine._positions) == 0
        assert len(engine._trades) == 1
        assert engine._trades[0].exit_reason == "SL"

    def test_check_exits_tp_long(self, bt_cfg: Settings) -> None:
        """TP triggers for LONG when high >= tp_price."""
        engine = BacktestEngine(bt_cfg, symbols=["TEST-USDT"], days=1)

        pos = VirtualPosition(
            symbol="TEST-USDT",
            side="LONG",
            entry_price=100.0,
            qty=1.0,
            notional=100.0,
            sl_price=99.0,
            tp_price=101.0,
            opened_at=datetime.now(timezone.utc),
        )
        engine._positions.append(pos)

        engine._check_exits("TEST-USDT", close=101.2, high=101.5, low=100.5, ts=datetime.now(timezone.utc))
        assert len(engine._positions) == 0
        assert engine._trades[0].exit_reason == "TP"
        assert engine._trades[0].pnl > 0

    def test_check_exits_timeout(self, bt_cfg: Settings) -> None:
        """Timeout triggers when position is held too long."""
        engine = BacktestEngine(bt_cfg, symbols=["TEST-USDT"], days=1)

        old_time = datetime.now(timezone.utc) - timedelta(minutes=150)
        pos = VirtualPosition(
            symbol="TEST-USDT",
            side="LONG",
            entry_price=100.0,
            qty=1.0,
            notional=100.0,
            sl_price=95.0,
            tp_price=110.0,
            opened_at=old_time,
        )
        engine._positions.append(pos)

        engine._check_exits("TEST-USDT", close=100.5, high=100.6, low=100.4, ts=datetime.now(timezone.utc))
        assert len(engine._positions) == 0
        assert engine._trades[0].exit_reason == "TIMEOUT"

    def test_check_exits_sl_short(self, bt_cfg: Settings) -> None:
        """SL triggers for SHORT when high >= sl_price."""
        engine = BacktestEngine(bt_cfg, symbols=["TEST-USDT"], days=1)

        pos = VirtualPosition(
            symbol="TEST-USDT",
            side="SHORT",
            entry_price=100.0,
            qty=1.0,
            notional=100.0,
            sl_price=100.5,
            tp_price=99.0,
            opened_at=datetime.now(timezone.utc),
        )
        engine._positions.append(pos)

        engine._check_exits("TEST-USDT", close=100.3, high=100.7, low=100.1, ts=datetime.now(timezone.utc))
        assert len(engine._positions) == 0
        assert engine._trades[0].exit_reason == "SL"
        assert engine._trades[0].pnl < 0

    def test_close_position_pnl_calculation(self, bt_cfg: Settings) -> None:
        """PnL is correctly calculated including fees."""
        engine = BacktestEngine(bt_cfg, symbols=["TEST-USDT"], days=1)

        pos = VirtualPosition(
            symbol="TEST-USDT",
            side="LONG",
            entry_price=100.0,
            qty=1.0,
            notional=100.0,
            sl_price=99.0,
            tp_price=101.0,
            opened_at=datetime.now(timezone.utc),
        )

        engine._close_position(pos, exit_price=101.0, reason="TP", ts=datetime.now(timezone.utc))
        trade = engine._trades[0]

        # The exit now pays slippage as well as the fee: a stop-market or
        # take-profit-market order crosses the book instead of filling exactly at
        # the level. Funding is ~0 here because the hold is ~0 minutes.
        slip = bt_cfg.slippage_assumption_bps / 10_000
        fill_exit = 101.0 * (1 - slip)          # LONG closes by selling
        gross = (fill_exit - 100.0) * 1.0
        fee = fill_exit * 1.0 * (bt_cfg.fee_rate_bps / 10_000)
        expected_pnl = gross - fee

        assert trade.pnl == pytest.approx(expected_pnl, abs=1e-6)
        # And it must be strictly worse than the old fee-only model.
        assert trade.pnl < 1.0 - fee

    def test_build_report_empty(self, bt_cfg: Settings) -> None:
        """Report works with no trades."""
        engine = BacktestEngine(bt_cfg, symbols=["BTC-USDT"], days=1)
        report = engine._build_report()

        assert report.total_trades == 0
        assert report.win_rate == 0.0
        assert report.total_pnl == 0.0

    def test_get_open_positions_dict(self, bt_cfg: Settings) -> None:
        """Virtual positions convert to dict format."""
        engine = BacktestEngine(bt_cfg, symbols=["TEST-USDT"], days=1)

        pos = VirtualPosition(
            symbol="TEST-USDT",
            side="LONG",
            entry_price=100.0,
            qty=1.0,
            notional=100.0,
            sl_price=99.0,
            tp_price=101.0,
            opened_at=datetime.now(timezone.utc),
        )
        engine._positions.append(pos)

        dicts = engine._get_open_positions_dict()
        assert len(dicts) == 1
        assert dicts[0]["symbol"] == "TEST-USDT"
        assert dicts[0]["side"] == "LONG"
        assert dicts[0]["status"] == "OPEN"
