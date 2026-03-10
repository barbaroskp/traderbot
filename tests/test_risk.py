"""Tests for risk state machine transitions (relaxed version)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.config import Settings
from src.risk import RiskManager, RiskState
from src.storage import Storage


@pytest.fixture()
def risk_cfg() -> Settings:
    """Config with risk states ENABLED for testing transitions."""
    return Settings(
        bingx_api_key="test_key_123",
        bingx_api_secret="test_secret_456",
        paper_mode=True,
        allow_live_trading=False,
        initial_capital_usdt=20.0,
        db_path=":memory:",
        log_level="DEBUG",
        log_file="",
        risk_state_disabled=False,
        risk_consec_losses_tight=4,
        risk_drawdown_pct_tight=5.0,
        risk_drawdown_min_for_consec_tight=1.5,
        risk_consec_losses_ultra=7,
        risk_drawdown_pct_ultra=12.0,
        risk_drawdown_min_for_consec_ultra=3.0,
        risk_consec_wins_recover=3,
        risk_stable_cycles_recover=5,
    )


class TestRiskStateTransitions:
    def test_starts_normal(self, risk_cfg, db) -> None:
        rm = RiskManager(risk_cfg, db)
        assert rm.state == RiskState.NORMAL

    def test_consecutive_losses_trigger_tight(self, risk_cfg, db) -> None:
        rm = RiskManager(risk_cfg, db)
        # 4 consecutive losses AND drawdown >= 1.5%
        # With 20 USDT: 4 × -0.2 = -0.8 = 4% drawdown → TIGHT
        for _ in range(4):
            rm.record_trade_result(-0.2)
        state = rm.evaluate()
        assert state == RiskState.TIGHT

    def test_fewer_losses_stay_normal(self, risk_cfg, db) -> None:
        rm = RiskManager(risk_cfg, db)
        # 3 losses not enough (threshold 4), DD = 3% < 5%
        for _ in range(3):
            rm.record_trade_result(-0.2)
        state = rm.evaluate()
        assert state == RiskState.NORMAL

    def test_more_losses_trigger_ultra_tight(self, risk_cfg, db) -> None:
        rm = RiskManager(risk_cfg, db)
        # 7 consecutive losses AND dd >= 3%
        # With 20 USDT: 7 × -0.2 = -1.4 = 7% drawdown → ULTRA
        for _ in range(7):
            rm.record_trade_result(-0.2)
        state = rm.evaluate()
        assert state == RiskState.ULTRA_TIGHT

    def test_drawdown_triggers_tight(self, risk_cfg, db) -> None:
        rm = RiskManager(risk_cfg, db)
        rm._peak_balance = 20
        rm._current_balance = 18.8  # 6% drawdown (threshold 5%)
        state = rm.evaluate()
        assert state == RiskState.TIGHT

    def test_small_drawdown_stays_normal(self, risk_cfg, db) -> None:
        rm = RiskManager(risk_cfg, db)
        rm._peak_balance = 20
        rm._current_balance = 19.5  # 2.5% drawdown (below 5% threshold)
        state = rm.evaluate()
        assert state == RiskState.NORMAL

    def test_drawdown_triggers_ultra(self, risk_cfg, db) -> None:
        rm = RiskManager(risk_cfg, db)
        rm._peak_balance = 20
        rm._current_balance = 17.4  # 13% drawdown (threshold 12%)
        state = rm.evaluate()
        assert state == RiskState.ULTRA_TIGHT

    def test_api_errors_trigger_tight(self, risk_cfg, db) -> None:
        rm = RiskManager(risk_cfg, db)
        state = rm.evaluate(api_error_rate=0.35)
        assert state == RiskState.TIGHT

    def test_recovery_from_tight_with_wins(self, risk_cfg, db) -> None:
        rm = RiskManager(risk_cfg, db)
        # Go to TIGHT (4 losses)
        for _ in range(4):
            rm.record_trade_result(-0.2)
        rm.evaluate()
        assert rm.state == RiskState.TIGHT

        # Reset losses and win 3 times
        rm._consecutive_losses = 0
        rm._current_balance = 20
        rm._peak_balance = 20
        for _ in range(3):
            rm.record_trade_result(0.5)

        rm.evaluate()
        assert rm.state == RiskState.NORMAL

    def test_recovery_from_ultra_to_tight(self, risk_cfg, db) -> None:
        rm = RiskManager(risk_cfg, db)
        for _ in range(7):
            rm.record_trade_result(-0.2)
        rm.evaluate()
        assert rm.state == RiskState.ULTRA_TIGHT

        # Win enough to de-escalate (needs 3 wins)
        rm._consecutive_losses = 0
        rm._current_balance = 20
        rm._peak_balance = 20
        for _ in range(3):
            rm.record_trade_result(0.5)
        rm.evaluate()
        assert rm.state == RiskState.TIGHT

    def test_time_based_recovery_from_tight(self, risk_cfg, db) -> None:
        rm = RiskManager(risk_cfg, db)
        for _ in range(4):
            rm.record_trade_result(-0.2)
        rm.evaluate()
        assert rm.state == RiskState.TIGHT

        # Simulate time passing (31 min; threshold 30 min)
        rm._state_entered_at = datetime.now(timezone.utc) - timedelta(minutes=31)
        rm._consecutive_losses = 0
        rm._current_balance = 20
        rm._peak_balance = 20
        rm.evaluate()
        assert rm.state == RiskState.NORMAL

    def test_time_based_recovery_from_ultra(self, risk_cfg, db) -> None:
        rm = RiskManager(risk_cfg, db)
        for _ in range(7):
            rm.record_trade_result(-0.2)
        rm.evaluate()
        assert rm.state == RiskState.ULTRA_TIGHT

        # Simulate time passing (61 min; threshold 60 min)
        rm._state_entered_at = datetime.now(timezone.utc) - timedelta(minutes=61)
        rm._consecutive_losses = 0
        rm._current_balance = 20
        rm._peak_balance = 20
        rm.evaluate()
        assert rm.state == RiskState.TIGHT

    def test_diagnostics_includes_recovery_info(self, risk_cfg, db) -> None:
        rm = RiskManager(risk_cfg, db)
        rm._state = RiskState.TIGHT
        diag = rm.get_diagnostics()
        assert "minutes_in_current_state" in diag
        assert "auto_recovery_in_minutes" in diag


class TestRiskMarginScaling:
    def test_normal_margin(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        expected = max(cfg.initial_capital_usdt * cfg.max_trade_margin_pct, 1.0)
        assert rm.get_max_trade_margin() == expected

    def test_tight_70pct_margin(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        rm._state = RiskState.TIGHT
        expected = max(cfg.initial_capital_usdt * cfg.max_trade_margin_pct, 1.0) * 0.7
        assert rm.get_max_trade_margin() == expected

    def test_ultra_tight_40pct_margin(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        rm._state = RiskState.ULTRA_TIGHT
        expected = max(cfg.initial_capital_usdt * cfg.max_trade_margin_pct, 1.0) * 0.4
        assert rm.get_max_trade_margin() == expected


class TestPositionSizing:
    def test_basic_sizing_margin_based(self, cfg, db) -> None:
        """Margin-based sizing: margin = balance * fraction, notional = margin * leverage."""
        rm = RiskManager(cfg, db)
        leverage = cfg.leverage  # 5x
        qty = rm.compute_position_size(price=50000, leverage=leverage, current_total_margin=0)
        margin = (qty * 50000) / leverage
        # Margin should be between min_margin (1.0) and max_trade_margin
        max_trade_margin = max(cfg.initial_capital_usdt * cfg.max_trade_margin_pct, 1.0)
        assert 1.0 <= margin <= max_trade_margin

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
        # max_total_margin = 20 * 0.80 = 16.0, fill up most of it
        max_total = max(cfg.initial_capital_usdt * cfg.max_total_margin_pct, 2.0)
        qty = rm.compute_position_size(price=50000, leverage=5, current_total_margin=max_total - 0.5)
        margin = (qty * 50000) / 5
        assert margin <= 1.5  # remaining ~0.5

    def test_zero_price_returns_zero(self, cfg, db) -> None:
        rm = RiskManager(cfg, db)
        assert rm.compute_position_size(price=0, leverage=5, current_total_margin=0) == 0

    def test_risk_state_logged(self, db) -> None:
        risk_cfg = Settings(
            bingx_api_key="test_key_123",
            bingx_api_secret="test_secret_456",
            paper_mode=True,
            allow_live_trading=False,
            initial_capital_usdt=20.0,
            db_path=":memory:",
            log_level="DEBUG",
            log_file="",
            risk_state_disabled=False,
        )
        rm = RiskManager(risk_cfg, db)
        # 4 × -0.2 = 4% DD → TIGHT
        for _ in range(4):
            rm.record_trade_result(-0.2)
        rm.evaluate()

        logs = db.fetch_all("SELECT * FROM risk_state_log")
        assert len(logs) >= 1
        assert logs[-1]["state"] == "TIGHT"


class TestRiskStateDisabled:
    """When risk_state_disabled=True, state must always stay NORMAL."""

    @pytest.fixture()
    def cfg_disabled(self) -> Settings:
        return Settings(
            bingx_api_key="test_key_123",
            bingx_api_secret="test_secret_456",
            paper_mode=True,
            allow_live_trading=False,
            initial_capital_usdt=20.0,
            db_path=":memory:",
            log_level="DEBUG",
            log_file="",
            risk_state_disabled=True,
        )

    def test_stays_normal_despite_losses(self, cfg_disabled, db) -> None:
        rm = RiskManager(cfg_disabled, db)
        for _ in range(20):
            rm.record_trade_result(-0.5)
        state = rm.evaluate()
        assert state == RiskState.NORMAL

    def test_stays_normal_despite_api_errors(self, cfg_disabled, db) -> None:
        rm = RiskManager(cfg_disabled, db)
        state = rm.evaluate(api_error_rate=0.95)
        assert state == RiskState.NORMAL

    def test_full_margin_when_disabled(self, cfg_disabled, db) -> None:
        rm = RiskManager(cfg_disabled, db)
        for _ in range(20):
            rm.record_trade_result(-0.5)
        rm.evaluate()
        # Balance dropped to 10$ after losses; margin scales with current balance
        current_balance = cfg_disabled.initial_capital_usdt - 20 * 0.5  # 10$
        expected = max(current_balance * cfg_disabled.max_trade_margin_pct, 1.0)
        assert rm.get_max_trade_margin() == expected
        # State stays NORMAL even after heavy losses (disabled)
        assert rm.state == RiskState.NORMAL
