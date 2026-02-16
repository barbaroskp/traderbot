"""Tests for universe discovery and caching."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bingx_client import BingXClient, BingXClientError
from src.config import Settings
from src.storage import Storage
from src.universe import Universe


@pytest.fixture()
def mock_client() -> AsyncMock:
    client = AsyncMock(spec=BingXClient)
    client.get_contracts.return_value = [
        {
            "symbol": "BTC-USDT",
            "asset": "BTC",
            "currency": "USDT",
            "status": "1",
            "tickSize": "0.1",
            "stepSize": "0.001",
            "tradeMinQuantity": "0.001",
            "maxLongLeverage": "125",
        },
        {
            "symbol": "ETH-USDT",
            "asset": "ETH",
            "currency": "USDT",
            "status": "1",
            "tickSize": "0.01",
            "stepSize": "0.01",
            "tradeMinQuantity": "0.01",
            "maxLongLeverage": "100",
        },
        {
            "symbol": "DEAD-USDT",
            "asset": "DEAD",
            "currency": "USDT",
            "status": "DELISTED",
            "tickSize": "0.001",
            "stepSize": "0.1",
        },
    ]
    return client


class TestUniverse:
    @pytest.mark.asyncio
    async def test_refresh_loads_active_contracts(self, cfg, db, mock_client) -> None:
        uni = Universe(cfg, mock_client, db)
        count = await uni.refresh()
        assert count == 2
        assert "BTC-USDT" in uni.symbols
        assert "ETH-USDT" in uni.symbols
        assert "DEAD-USDT" not in uni.symbols

    @pytest.mark.asyncio
    async def test_refresh_persists_to_db(self, cfg, db, mock_client) -> None:
        uni = Universe(cfg, mock_client, db)
        await uni.refresh()
        rows = db.get_contracts()
        symbols = [r["symbol"] for r in rows]
        assert "BTC-USDT" in symbols

    @pytest.mark.asyncio
    async def test_api_failure_falls_back_to_cache(self, cfg, db, mock_client) -> None:
        uni = Universe(cfg, mock_client, db)
        # First refresh succeeds
        await uni.refresh()
        assert uni.size == 2

        # Second refresh fails → falls back to cache
        mock_client.get_contracts.side_effect = BingXClientError(500, "server error")
        count = await uni.refresh()
        assert count >= 1  # loaded from DB

    def test_needs_refresh_initially(self, cfg, db, mock_client) -> None:
        uni = Universe(cfg, mock_client, db)
        assert uni.needs_refresh() is True

    @pytest.mark.asyncio
    async def test_needs_refresh_after_load(self, cfg, db, mock_client) -> None:
        uni = Universe(cfg, mock_client, db)
        await uni.refresh()
        assert uni.needs_refresh() is False

    @pytest.mark.asyncio
    async def test_get_contract_metadata(self, cfg, db, mock_client) -> None:
        uni = Universe(cfg, mock_client, db)
        await uni.refresh()
        btc = uni.get_contract("BTC-USDT")
        assert btc is not None
        assert btc["tick_size"] == 0.1
        assert btc["step_size"] == 0.001
        assert btc["max_leverage"] == 125
