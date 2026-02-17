"""Tests for the tradeable symbol selector pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Settings
from src.marketdata import MarketData, SymbolSnapshot
from src.selector import Selector
from src.storage import Storage
from src.universe import Universe


@pytest.fixture()
def mock_universe() -> MagicMock:
    uni = MagicMock(spec=Universe)
    uni.size = 3
    uni.symbols = ["BTC-USDT", "ETH-USDT", "DOGE-USDT"]
    uni.symbols_by_volume = ["BTC-USDT", "ETH-USDT", "DOGE-USDT"]
    uni.get_contract.side_effect = lambda s: {
        "BTC-USDT": {"symbol": "BTC-USDT", "tick_size": 0.1, "step_size": 0.001, "quote_asset": "USDT"},
        "ETH-USDT": {"symbol": "ETH-USDT", "tick_size": 0.01, "step_size": 0.01, "quote_asset": "USDT"},
        "DOGE-USDT": {"symbol": "DOGE-USDT", "tick_size": 0.0001, "step_size": 1.0, "quote_asset": "USDT"},
    }.get(s)
    return uni


@pytest.fixture()
def mock_market() -> AsyncMock:
    market = AsyncMock(spec=MarketData)
    market.batch_snapshots.return_value = [
        SymbolSnapshot(
            symbol="BTC-USDT", mid_price=50000, mark_price=50000,
            best_bid=49999, best_ask=50001, spread_bps=0.4,
            bid_depth_usdt=5000, ask_depth_usdt=5000,
            fast_ema=50000, slow_ema=49900, z_score_bps=0,
        ),
        SymbolSnapshot(
            symbol="ETH-USDT", mid_price=3000, mark_price=3000,
            best_bid=2999, best_ask=3001, spread_bps=6.7,
            bid_depth_usdt=2000, ask_depth_usdt=2000,
            fast_ema=3000, slow_ema=2990, z_score_bps=0,
        ),
        SymbolSnapshot(
            symbol="DOGE-USDT", mid_price=0.1, mark_price=0.1,
            best_bid=0.099, best_ask=0.101, spread_bps=200,  # wide spread
            bid_depth_usdt=500, ask_depth_usdt=500,  # thin depth
            fast_ema=0.1, slow_ema=0.1, z_score_bps=0,
        ),
    ]
    return market


class TestSelector:
    @pytest.mark.asyncio
    async def test_filters_wide_spread(self, cfg, mock_universe, mock_market) -> None:
        selector = Selector(cfg, mock_universe, mock_market)
        tradeable, stats = await selector.select()

        symbols = [s.symbol for s in tradeable]
        assert "DOGE-USDT" not in symbols  # spread too wide (200 bps > 20)

    @pytest.mark.asyncio
    async def test_filters_thin_depth(self, cfg, mock_universe, mock_market) -> None:
        selector = Selector(cfg, mock_universe, mock_market)
        tradeable, stats = await selector.select()

        # DOGE has 500 depth, below min_depth_usdt=1000
        symbols = [s.symbol for s in tradeable]
        assert "DOGE-USDT" not in symbols

    @pytest.mark.asyncio
    async def test_passes_good_symbols(self, cfg, mock_universe, mock_market) -> None:
        selector = Selector(cfg, mock_universe, mock_market)
        tradeable, stats = await selector.select()

        symbols = [s.symbol for s in tradeable]
        assert "BTC-USDT" in symbols
        assert "ETH-USDT" in symbols

    @pytest.mark.asyncio
    async def test_stats_tracking(self, cfg, mock_universe, mock_market) -> None:
        selector = Selector(cfg, mock_universe, mock_market)
        _, stats = await selector.select()

        assert stats.universe_size == 3
        assert stats.prefiltered == 3
        assert stats.tradeable == 2

    @pytest.mark.asyncio
    async def test_ultra_tight_narrows_to_10(self, cfg, mock_universe, mock_market) -> None:
        selector = Selector(cfg, mock_universe, mock_market)
        _, stats = await selector.select(risk_state="ULTRA_TIGHT")
        assert stats.shortlisted <= 10

    @pytest.mark.asyncio
    async def test_empty_universe(self, cfg, mock_market) -> None:
        empty_uni = MagicMock(spec=Universe)
        empty_uni.size = 0
        empty_uni.symbols = []
        empty_uni.symbols_by_volume = []
        selector = Selector(cfg, empty_uni, mock_market)
        tradeable, stats = await selector.select()
        assert len(tradeable) == 0
