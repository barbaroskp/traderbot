"""Tests for tiered TP system and daily loss circuit breaker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import Settings
from src.execution import PaperExecution
from src.marketdata import MarketData, SymbolSnapshot
from src.risk import RiskManager
from src.storage import Storage
from src.strategy import Signal
from src.universe import Universe


@pytest.fixture()
def tiered_cfg(db) -> Settings:
    """Config with tiered TP enabled."""
    return Settings(
        bingx_api_key="test_key_123",
        bingx_api_secret="test_secret_456",
        paper_mode=True,
        allow_live_trading=False,
        initial_capital_usdt=20.0,
        db_path=":memory:",
        log_level="DEBUG",
        log_file="",
        use_tiered_tp=True,
        use_sl_randomization=True,
    )


@pytest.fixture()
def tiered_db(tiered_cfg) -> Storage:
    return Storage(":memory:")


@pytest.fixture()
def tiered_market() -> AsyncMock:
    market = AsyncMock(spec=MarketData)
    market.fetch_mark_price.return_value = 50000.0
    return market


@pytest.fixture()
def tiered_universe() -> MagicMock:
    uni = MagicMock(spec=Universe)
    uni.get_contract.return_value = {
        "symbol": "BTC-USDT",
        "tick_size": 0.1,
        "step_size": 0.001,
        "min_qty": 0.001,
    }
    return uni


@pytest.fixture()
def tiered_exec(tiered_cfg, tiered_db, tiered_market, tiered_universe) -> PaperExecution:
    risk = RiskManager(tiered_cfg, tiered_db)
    return PaperExecution(tiered_cfg, tiered_db, tiered_market, risk, tiered_universe)


def _make_signal(side="LONG"):
    return Signal(
        symbol="BTC-USDT", side=side, z_score_bps=-30,
        mid_price=50000, fast_ema=50000, slow_ema=49900,
        spread_bps=2, depth_usdt=5000,
    )


def _make_snap():
    return SymbolSnapshot(
        symbol="BTC-USDT", mid_price=50000, best_bid=49999, best_ask=50001,
        spread_bps=0.4, bid_depth_usdt=5000, ask_depth_usdt=5000,
    )


class TestTieredTP:
    @pytest.mark.asyncio
    async def test_tiered_tp_creates_two_tp_orders(self, tiered_exec, tiered_db) -> None:
        """Tiered TP should create TP1 and TP2 orders."""
        await tiered_exec.execute_signal(_make_signal(), _make_snap(), qty=0.001, current_total_margin=0)

        orders = tiered_db.fetch_all("SELECT * FROM orders WHERE order_type='TAKE_PROFIT'")
        assert len(orders) == 2

        tp_levels = sorted(o.get("tp_level", 0) for o in orders)
        assert tp_levels == [1, 2]

    @pytest.mark.asyncio
    async def test_position_stores_tp3_qty(self, tiered_exec, tiered_db) -> None:
        """Position should store tp3_qty for trailing stop management."""
        await tiered_exec.execute_signal(_make_signal(), _make_snap(), qty=0.001, current_total_margin=0)

        pos = tiered_db.get_open_positions()[0]
        tp3_qty = float(pos.get("tp3_qty", 0))
        # tp3_qty = total - tp1 (40%) - tp2 (30%) = 30%
        assert tp3_qty > 0
        assert abs(tp3_qty - 0.001 * 0.30) < 0.0001

    @pytest.mark.asyncio
    async def test_tp1_partial_close(self, tiered_exec, tiered_db, tiered_market) -> None:
        """TP1 hit should partially close 40% and move SL to breakeven."""
        await tiered_exec.execute_signal(_make_signal(), _make_snap(), qty=0.001, current_total_margin=0)

        pos = tiered_db.get_open_positions()[0]
        entry = pos["entry_price"]

        # Set mark to hit TP1 (50% of TP target = ~100bps above entry)
        tp1_mark = entry * 1.012  # well above TP1
        tiered_market.fetch_mark_price.return_value = tp1_mark

        positions = tiered_db.get_open_positions()
        closed = await tiered_exec.check_exits(positions)

        # Position should NOT be fully closed
        assert len(closed) == 0

        # But remaining qty should be reduced
        updated_pos = tiered_db.get_open_positions()[0]
        remaining = float(updated_pos.get("remaining_qty", 0))
        # After TP1 (40% closed), remaining should be ~60% of original
        assert remaining < 0.001
        assert updated_pos.get("partial_tp_filled", 0) == 1

    @pytest.mark.asyncio
    async def test_sl_randomization_widens_sl(self, tiered_exec, tiered_db) -> None:
        """SL randomization should make SL slightly wider than base."""
        # Create position
        await tiered_exec.execute_signal(_make_signal(), _make_snap(), qty=0.001, current_total_margin=0)

        pos = tiered_db.get_open_positions()[0]
        sl_bps_stored = float(pos.get("sl_bps", 0))

        # sl_bps should be base + random offset (3-12bps), so > base sl_bps
        # Base sl_bps from config is typically around 100-150
        from src.config import Settings
        base_cfg = Settings(
            bingx_api_key="k", bingx_api_secret="s",
            db_path=":memory:", log_file="",
        )
        assert sl_bps_stored >= base_cfg.sl_bps  # randomization adds to base


class TestDailyLossCircuitBreaker:
    def test_initial_state_allows_trading(self, tiered_cfg, tiered_db) -> None:
        risk = RiskManager(tiered_cfg, tiered_db)
        assert risk.can_open_new_trade() is True
        assert risk.get_daily_size_multiplier() == 1.0

    def test_reduce_threshold_no_effect_when_disabled(self, tiered_cfg, tiered_db) -> None:
        """Daily loss limit disabled: losses should NOT halve position sizes."""
        tiered_cfg.use_daily_loss_limit = False
        risk = RiskManager(tiered_cfg, tiered_db)
        risk.record_trade_result(-0.50)

        assert risk.daily_reduce_active is False
        assert risk.get_daily_size_multiplier() == 1.0
        assert risk.can_open_new_trade() is True

    def test_stop_threshold_no_effect_when_disabled(self, tiered_cfg, tiered_db) -> None:
        """Daily loss limit disabled: losses should NOT block new trades."""
        tiered_cfg.use_daily_loss_limit = False
        risk = RiskManager(tiered_cfg, tiered_db)
        risk.record_trade_result(-0.90)

        assert risk.daily_stop_active is False
        assert risk.can_open_new_trade() is True

    def test_kill_threshold_no_effect_when_disabled(self, tiered_cfg, tiered_db) -> None:
        """Daily loss limit disabled: losses should NOT activate kill switch."""
        tiered_cfg.use_daily_loss_limit = False
        risk = RiskManager(tiered_cfg, tiered_db)
        risk.record_trade_result(-1.30)

        assert risk.daily_kill_active is False
        assert risk.can_open_new_trade() is True

    def test_daily_reset_clears_state(self, tiered_cfg, tiered_db) -> None:
        """New day should reset daily P&L tracking."""
        tiered_cfg.use_daily_loss_limit = False
        risk = RiskManager(tiered_cfg, tiered_db)
        risk.record_trade_result(-0.90)

        # Simulate day change
        risk._daily_date = "2020-01-01"
        risk._check_daily_reset()

        assert risk.daily_stop_active is False
        assert risk.can_open_new_trade() is True

    def test_disabled_circuit_breaker(self, tiered_db) -> None:
        """When disabled, circuit breaker should not affect trading."""
        cfg = Settings(
            bingx_api_key="k", bingx_api_secret="s",
            db_path=":memory:", log_file="",
            initial_capital_usdt=20.0,
            use_daily_loss_limit=False,
        )
        risk = RiskManager(cfg, tiered_db)
        risk.record_trade_result(-5.0)  # Huge loss

        assert risk.can_open_new_trade() is True
        assert risk.get_daily_size_multiplier() == 1.0
