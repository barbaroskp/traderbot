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

    # Paper execution checks the interval HIGH/LOW rather than only the latest
    # mark, so a stop or target touched between two polls is not missed the way
    # it used to be. Mirror the mark price by default; tests that need an
    # intra-interval excursion override the side_effect explicitly.
    async def _extremes(symbol, interval="1m", limit=10):
        mark = market.fetch_mark_price.return_value
        return (mark, mark)

    market.fetch_recent_extremes.side_effect = _extremes
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

        # Backdate opened_at to exceed max_hold_minutes
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=200)).isoformat()
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


class TestRoundQty:
    def test_round_to_step(self) -> None:
        assert PaperExecution._round_qty(0.0035, 0.001) == pytest.approx(0.003)
        assert PaperExecution._round_qty(1.567, 0.01) == pytest.approx(1.56)
        assert PaperExecution._round_qty(10.5, 1.0) == pytest.approx(10.0)

    def test_zero_step_returns_original(self) -> None:
        assert PaperExecution._round_qty(0.123, 0) == 0.123
