"""Tests for paper execution: fill simulation, SL/TP, partial fills, idempotency."""

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
def mock_market(cfg, db) -> AsyncMock:
    market = AsyncMock(spec=MarketData)
    market.fetch_mark_price.return_value = 50000.0
    return market


@pytest.fixture()
def mock_universe() -> MagicMock:
    uni = MagicMock(spec=Universe)
    uni.get_contract.return_value = {
        "symbol": "BTC-USDT",
        "tick_size": 0.1,
        "step_size": 0.001,
        "min_qty": 0.001,
    }
    return uni


@pytest.fixture()
def paper_exec(cfg, db, mock_market, mock_universe) -> PaperExecution:
    risk = RiskManager(cfg, db)
    return PaperExecution(cfg, db, mock_market, risk, mock_universe)


class TestPaperExecution:
    @pytest.mark.asyncio
    async def test_execute_creates_position(self, paper_exec, db) -> None:
        signal = Signal(
            symbol="BTC-USDT", side="LONG", z_score_bps=-30,
            mid_price=50000, fast_ema=50000, slow_ema=49900,
            spread_bps=2, depth_usdt=5000,
        )
        snap = SymbolSnapshot(
            symbol="BTC-USDT", mid_price=50000, best_bid=49999, best_ask=50001,
            spread_bps=0.4, bid_depth_usdt=5000, ask_depth_usdt=5000,
        )

        result = await paper_exec.execute_signal(signal, snap, qty=0.001, current_total_margin=0)

        assert result is not None
        assert result.status == "FILLED"
        assert result.is_paper is True

        # Check position in DB
        positions = db.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "BTC-USDT"
        assert positions[0]["side"] == "LONG"

    @pytest.mark.asyncio
    async def test_sl_tp_orders_created(self, paper_exec, db) -> None:
        signal = Signal(
            symbol="BTC-USDT", side="LONG", z_score_bps=-30,
            mid_price=50000, fast_ema=50000, slow_ema=49900,
            spread_bps=2, depth_usdt=5000,
        )
        snap = SymbolSnapshot(
            symbol="BTC-USDT", mid_price=50000, best_bid=49999, best_ask=50001,
        )

        await paper_exec.execute_signal(signal, snap, qty=0.001, current_total_margin=0)

        # Should have 3-4 orders: entry + SL + TP (+ optional partial TP1)
        orders = db.fetch_all("SELECT * FROM orders")
        assert len(orders) >= 3

        types = {o["order_type"] for o in orders}
        assert "LIMIT" in types
        assert "STOP_MARKET" in types
        assert "TAKE_PROFIT" in types

    @pytest.mark.asyncio
    async def test_sl_triggers_close(self, paper_exec, db, mock_market) -> None:
        signal = Signal(
            symbol="BTC-USDT", side="LONG", z_score_bps=-30,
            mid_price=50000, fast_ema=50000, slow_ema=49900,
            spread_bps=2, depth_usdt=5000,
        )
        snap = SymbolSnapshot(
            symbol="BTC-USDT", mid_price=50000, best_bid=49999, best_ask=50001,
        )
        await paper_exec.execute_signal(signal, snap, qty=0.001, current_total_margin=0)

        # Set mark price below SL
        mock_market.fetch_mark_price.return_value = 49000.0

        positions = db.get_open_positions()
        closed = await paper_exec.check_exits(positions)

        assert len(closed) == 1
        assert closed[0]["exit_reason"] == "SL"
        assert closed[0]["realised_pnl"] < 0

    @pytest.mark.asyncio
    async def test_tp_triggers_close(self, paper_exec, db, mock_market) -> None:
        signal = Signal(
            symbol="BTC-USDT", side="LONG", z_score_bps=-30,
            mid_price=50000, fast_ema=50000, slow_ema=49900,
            spread_bps=2, depth_usdt=5000,
        )
        snap = SymbolSnapshot(
            symbol="BTC-USDT", mid_price=50000, best_bid=49999, best_ask=50001,
        )
        await paper_exec.execute_signal(signal, snap, qty=0.001, current_total_margin=0)

        # Set mark price above TP (TP=200bps=2% above entry ~50001, so need >51001)
        mock_market.fetch_mark_price.return_value = 51500.0

        positions = db.get_open_positions()
        closed = await paper_exec.check_exits(positions)

        assert len(closed) == 1
        assert closed[0]["exit_reason"] == "TP"
        assert closed[0]["realised_pnl"] > 0

    @pytest.mark.asyncio
    async def test_timeout_triggers_close(self, paper_exec, db, mock_market) -> None:
        signal = Signal(
            symbol="BTC-USDT", side="LONG", z_score_bps=-30,
            mid_price=50000, fast_ema=50000, slow_ema=49900,
            spread_bps=2, depth_usdt=5000,
        )
        snap = SymbolSnapshot(
            symbol="BTC-USDT", mid_price=50000, best_bid=49999, best_ask=50001,
        )
        await paper_exec.execute_signal(signal, snap, qty=0.001, current_total_margin=0)

        # Backdate opened_at to exceed max_hold_minutes (240min default)
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=300)).isoformat()
        db.execute("UPDATE positions SET opened_at=? WHERE status='OPEN'", (old_time,))

        mock_market.fetch_mark_price.return_value = 50100.0
        positions = db.get_open_positions()
        closed = await paper_exec.check_exits(positions)

        assert len(closed) == 1
        assert closed[0]["exit_reason"] == "TIMEOUT"

    @pytest.mark.asyncio
    async def test_zero_qty_returns_none(self, paper_exec) -> None:
        signal = Signal(
            symbol="BTC-USDT", side="LONG", z_score_bps=-30,
            mid_price=50000, fast_ema=50000, slow_ema=49900,
            spread_bps=2, depth_usdt=5000,
        )
        snap = SymbolSnapshot(symbol="BTC-USDT", mid_price=50000)
        result = await paper_exec.execute_signal(signal, snap, qty=0, current_total_margin=0)
        assert result is None

    @pytest.mark.asyncio
    async def test_short_position_pnl(self, paper_exec, db, mock_market) -> None:
        signal = Signal(
            symbol="BTC-USDT", side="SHORT", z_score_bps=30,
            mid_price=50000, fast_ema=50000, slow_ema=49900,
            spread_bps=2, depth_usdt=5000,
        )
        snap = SymbolSnapshot(
            symbol="BTC-USDT", mid_price=50000, best_bid=49999, best_ask=50001,
        )
        await paper_exec.execute_signal(signal, snap, qty=0.001, current_total_margin=0)

        # Price drops → short profits
        mock_market.fetch_mark_price.return_value = 48000.0
        positions = db.get_open_positions()
        closed = await paper_exec.check_exits(positions)

        assert len(closed) == 1
        # Short at ~50000, mark at 48000 → profit
        assert closed[0]["realised_pnl"] > 0


class TestProfitLock:
    @pytest.mark.asyncio
    async def test_profit_lock_moves_sl_to_breakeven(self, cfg, db, mock_market, mock_universe) -> None:
        """When price reaches 60% of TP, SL should move to entry + buffer."""
        cfg.use_profit_lock = True
        cfg.profit_lock_activation_pct = 0.60
        cfg.profit_lock_buffer_bps = 5.0
        cfg.use_tiered_tp = False
        cfg.use_breakeven_stop = False
        cfg.use_trailing_stop = False
        cfg.use_time_decay_sl = False
        cfg.use_momentum_exit = False
        risk = RiskManager(cfg, db)
        paper = PaperExecution(cfg, db, mock_market, risk, mock_universe)

        signal = Signal(
            symbol="BTC-USDT", side="LONG", z_score_bps=-30,
            mid_price=50000, fast_ema=50000, slow_ema=49900,
            spread_bps=2, depth_usdt=5000,
        )
        snap = SymbolSnapshot(
            symbol="BTC-USDT", mid_price=50000, best_bid=49999, best_ask=50001,
        )
        await paper.execute_signal(signal, snap, qty=0.001, current_total_margin=0)

        # Set mark price to 60% of TP (tp=150bps → 60% = 90bps above entry)
        pos = db.get_open_positions()[0]
        entry = pos["entry_price"]
        tp_bps = float(pos.get("tp_bps") or cfg.tp_bps)
        target_price = entry * (1 + tp_bps * 0.65 / 10_000)  # slightly above 60%
        mock_market.fetch_mark_price.return_value = target_price

        positions = db.get_open_positions()
        await paper.check_exits(positions)

        # SL should have moved to breakeven (entry + buffer)
        pos_after = db.get_open_positions()[0]
        assert pos_after.get("breakeven_triggered") == 1

    @pytest.mark.asyncio
    async def test_profit_lock_disabled(self, cfg, db, mock_market, mock_universe) -> None:
        """When disabled, SL should not move on partial profit."""
        cfg.use_profit_lock = False
        cfg.use_tiered_tp = False
        cfg.use_breakeven_stop = False
        cfg.use_trailing_stop = False
        cfg.use_time_decay_sl = False
        cfg.use_momentum_exit = False
        risk = RiskManager(cfg, db)
        paper = PaperExecution(cfg, db, mock_market, risk, mock_universe)

        signal = Signal(
            symbol="BTC-USDT", side="LONG", z_score_bps=-30,
            mid_price=50000, fast_ema=50000, slow_ema=49900,
            spread_bps=2, depth_usdt=5000,
        )
        snap = SymbolSnapshot(
            symbol="BTC-USDT", mid_price=50000, best_bid=49999, best_ask=50001,
        )
        await paper.execute_signal(signal, snap, qty=0.001, current_total_margin=0)

        pos = db.get_open_positions()[0]
        entry = pos["entry_price"]
        tp_bps = float(pos.get("tp_bps") or cfg.tp_bps)
        target_price = entry * (1 + tp_bps * 0.65 / 10_000)
        mock_market.fetch_mark_price.return_value = target_price

        positions = db.get_open_positions()
        await paper.check_exits(positions)

        pos_after = db.get_open_positions()[0]
        assert not pos_after.get("breakeven_triggered")


class TestMomentumExitGuards:
    @pytest.mark.asyncio
    async def test_momentum_exit_respects_min_hold(self, cfg, db, mock_market, mock_universe) -> None:
        """Momentum exit should not trigger before minimum hold time."""
        cfg.use_momentum_exit = True
        cfg.momentum_exit_min_hold_pct = 0.25
        cfg.momentum_exit_min_loss_bps = 10.0
        cfg.use_tiered_tp = False
        cfg.use_breakeven_stop = False
        cfg.use_trailing_stop = False
        cfg.use_time_decay_sl = False
        cfg.use_profit_lock = False
        risk = RiskManager(cfg, db)
        paper = PaperExecution(cfg, db, mock_market, risk, mock_universe)

        signal = Signal(
            symbol="BTC-USDT", side="LONG", z_score_bps=-30,
            mid_price=50000, fast_ema=50000, slow_ema=49900,
            spread_bps=2, depth_usdt=5000,
        )
        snap = SymbolSnapshot(
            symbol="BTC-USDT", mid_price=50000, best_bid=49999, best_ask=50001,
        )
        await paper.execute_signal(signal, snap, qty=0.001, current_total_margin=0)

        # Price drops but position just opened (min hold not met)
        mock_market.fetch_mark_price.return_value = 49900.0  # ~20bps loss
        from src.marketdata import Indicators
        mock_snap = MagicMock(spec=SymbolSnapshot)
        mock_snap.indicators = MagicMock()
        mock_snap.indicators.valid = True
        mock_snap.indicators.macd_histogram = -0.001  # bearish cross
        mock_snap.indicators.macd_histogram_prev = 0.001
        mock_market.snapshot_symbol.return_value = mock_snap

        positions = db.get_open_positions()
        closed = await paper.check_exits(positions)

        # Should NOT have momentum exited (position too fresh)
        momentum_exits = [c for c in closed if c.get("exit_reason") == "MOMENTUM_EXIT"]
        assert len(momentum_exits) == 0

    @pytest.mark.asyncio
    async def test_momentum_exit_respects_min_loss(self, cfg, db, mock_market, mock_universe) -> None:
        """Momentum exit should not trigger on tiny losses."""
        cfg.use_momentum_exit = True
        cfg.momentum_exit_min_hold_pct = 0.0  # no hold requirement
        cfg.momentum_exit_min_loss_bps = 30.0  # need 30bps loss
        cfg.use_tiered_tp = False
        cfg.use_breakeven_stop = False
        cfg.use_trailing_stop = False
        cfg.use_time_decay_sl = False
        cfg.use_profit_lock = False
        risk = RiskManager(cfg, db)
        paper = PaperExecution(cfg, db, mock_market, risk, mock_universe)

        signal = Signal(
            symbol="BTC-USDT", side="LONG", z_score_bps=-30,
            mid_price=50000, fast_ema=50000, slow_ema=49900,
            spread_bps=2, depth_usdt=5000,
        )
        snap = SymbolSnapshot(
            symbol="BTC-USDT", mid_price=50000, best_bid=49999, best_ask=50001,
        )
        await paper.execute_signal(signal, snap, qty=0.001, current_total_margin=0)

        # Only 5bps loss — below 30bps threshold
        pos = db.get_open_positions()[0]
        entry = pos["entry_price"]
        mock_market.fetch_mark_price.return_value = entry * (1 - 5.0 / 10_000)

        mock_snap = MagicMock(spec=SymbolSnapshot)
        mock_snap.indicators = MagicMock()
        mock_snap.indicators.valid = True
        mock_snap.indicators.macd_histogram = -0.001
        mock_snap.indicators.macd_histogram_prev = 0.001
        mock_market.snapshot_symbol.return_value = mock_snap

        positions = db.get_open_positions()
        closed = await paper.check_exits(positions)

        momentum_exits = [c for c in closed if c.get("exit_reason") == "MOMENTUM_EXIT"]
        assert len(momentum_exits) == 0


class TestRoundQty:
    def test_round_to_step(self) -> None:
        assert PaperExecution._round_qty(0.0035, 0.001) == pytest.approx(0.003)
        assert PaperExecution._round_qty(1.567, 0.01) == pytest.approx(1.56)
        assert PaperExecution._round_qty(10.5, 1.0) == pytest.approx(10.0)

    def test_zero_step_returns_original(self) -> None:
        assert PaperExecution._round_qty(0.123, 0) == 0.123
