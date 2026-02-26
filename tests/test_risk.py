"""Tests for risk state machine transitions (relaxed version)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.config import Settings
from src.risk import RiskManager, RiskState
from src.storage import Storage


class TestRiskStateTransitions:
    def test_starts_normal(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        assert rm.state == RiskState.NORMAL

    def test_consecutive_losses_trigger_tight(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        # 8 consecutive losses AND drawdown >= 2% (but < 18% to avoid ULTRA)
        # With 20 USDT: 8 × -0.2 = -1.6 = 8% drawdown → TIGHT
        for _ in range(8):
            rm.record_trade_result(-0.2)
        state = rm.evaluate()
        assert state == RiskState.TIGHT

    def test_fewer_losses_stay_normal(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        # 7 losses not enough (threshold 8), DD = 7% < 8%
        for _ in range(7):
            rm.record_trade_result(-0.2)
        state = rm.evaluate()
        assert state == RiskState.NORMAL

    def test_more_losses_trigger_ultra_tight(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        # 14 consecutive losses AND dd >= 5%
        # With 20 USDT: 14 × -0.2 = -2.8 = 14% drawdown
        for _ in range(14):
            rm.record_trade_result(-0.2)
        state = rm.evaluate()
        assert state == RiskState.ULTRA_TIGHT

    def test_drawdown_triggers_tight(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        rm._peak_balance = 20
        rm._current_balance = 18  # 10% drawdown (threshold 8%)
        state = rm.evaluate()
        assert state == RiskState.TIGHT

    def test_small_drawdown_stays_normal(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        rm._peak_balance = 20
        rm._current_balance = 19  # 5% drawdown (below 8% threshold)
        state = rm.evaluate()
        assert state == RiskState.NORMAL

    def test_drawdown_triggers_ultra(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        rm._peak_balance = 20
        rm._current_balance = 15.4  # 23% drawdown (threshold 18%)
        state = rm.evaluate()
        assert state == RiskState.ULTRA_TIGHT

    def test_api_errors_trigger_tight(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        # Now needs 0.3 error rate (was 0.2)
        state = rm.evaluate(api_error_rate=0.35)
        assert state == RiskState.TIGHT

    def test_recovery_from_tight_with_wins(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        # Go to TIGHT (8 losses, small amounts to avoid ULTRA)
        for _ in range(8):
            rm.record_trade_result(-0.2)
        rm.evaluate()
        assert rm.state == RiskState.TIGHT

        # Reset losses and win 2 times
        rm._consecutive_losses = 0
        rm._current_balance = 20
        rm._peak_balance = 20
        for _ in range(2):
            rm.record_trade_result(0.5)

        # Evaluate should recover
        rm.evaluate()
        assert rm.state == RiskState.NORMAL

    def test_recovery_from_ultra_to_tight(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        for _ in range(14):
            rm.record_trade_result(-0.2)
        rm.evaluate()
        assert rm.state == RiskState.ULTRA_TIGHT

        # Win enough to de-escalate (needs 2 wins)
        rm._consecutive_losses = 0
        rm._current_balance = 20
        rm._peak_balance = 20
        for _ in range(2):
            rm.record_trade_result(0.5)
        rm.evaluate()
        assert rm.state == RiskState.TIGHT

    def test_time_based_recovery_from_tight(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        for _ in range(8):
            rm.record_trade_result(-0.2)
        rm.evaluate()
        assert rm.state == RiskState.TIGHT

        # Simulate time passing (21 min; threshold 20 min)
        rm._state_entered_at = datetime.now(timezone.utc) - timedelta(minutes=21)
        rm._consecutive_losses = 0  # reset to avoid re-escalation
        rm._current_balance = 20
        rm._peak_balance = 20
        rm.evaluate()
        assert rm.state == RiskState.NORMAL

    def test_time_based_recovery_from_ultra(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        for _ in range(14):
            rm.record_trade_result(-0.2)
        rm.evaluate()
        assert rm.state == RiskState.ULTRA_TIGHT

        # Simulate time passing (46 min; threshold 45 min)
        rm._state_entered_at = datetime.now(timezone.utc) - timedelta(minutes=46)
        rm._consecutive_losses = 0
        rm._current_balance = 20
        rm._peak_balance = 20
        rm.evaluate()
        assert rm.state == RiskState.TIGHT

    def test_diagnostics_includes_recovery_info(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        rm._state = RiskState.TIGHT
        diag = rm.get_diagnostics()
        assert "minutes_in_current_state" in diag
        assert "auto_recovery_in_minutes" in diag


class TestRiskMarginScaling:
    def test_normal_margin(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        assert rm.get_max_trade_margin() == cfg.max_trade_margin_usdt

    def test_tight_70pct_margin(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        rm._state = RiskState.TIGHT
        assert rm.get_max_trade_margin() == cfg.max_trade_margin_usdt * 0.7

    def test_ultra_tight_40pct_margin(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        rm._state = RiskState.ULTRA_TIGHT
        expected = cfg.max_trade_margin_usdt * 0.4
        assert rm.get_max_trade_margin() == expected


class TestPositionSizing:
    def test_basic_sizing_margin_based(self, cfg, db) -> None:
        """Margin-based sizing: margin = balance * fraction, notional = margin * leverage."""
        rm = RiskManager(cfg, db)
        leverage = cfg.leverage  # 5x
        qty = rm.compute_position_size(price=50000, leverage=leverage, current_total_margin=0)
        margin = (qty * 50000) / leverage
        # Margin should be between min_margin (2.0) and max_trade_margin
        assert 1.0 <= margin <= cfg.max_trade_margin_usdt

    def test_leverage_amplifies_notional(self, cfg, db) -> None:
        """Higher leverage = larger notional for same margin."""
        rm = RiskManager(cfg, db)
        qty_5x = rm.compute_position_size(price=50000, leverage=5, current_total_margin=0)
        qty_10x = rm.compute_position_size(price=50000, leverage=10, current_total_margin=0)
        # 10x leverage should give roughly 2x the qty of 5x
        assert qty_10x > qty_5x
        assert qty_10x == pytest.approx(qty_5x * 2, rel=0.01)

    def test_respects_remaining_margin_capacity(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        # max_total_margin_usdt is 15.0, fill up most of it
        qty = rm.compute_position_size(price=50000, leverage=5, current_total_margin=14.5)
        margin = (qty * 50000) / 5
        assert margin <= 2.1  # min 2 USDT floor, remaining ~0.5

    def test_zero_price_returns_zero(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        assert rm.compute_position_size(price=0, leverage=5, current_total_margin=0) == 0

    def test_risk_state_logged(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        # 8 × -0.2 = 8% DD → TIGHT (not ULTRA)
        for _ in range(8):
            rm.record_trade_result(-0.2)
        rm.evaluate()

        logs = db.fetch_all("SELECT * FROM risk_state_log")
        assert len(logs) >= 1
        assert logs[-1]["state"] == "TIGHT"
