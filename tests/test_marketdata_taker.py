from __future__ import annotations

import asyncio

import pytest

from src.bingx_client import BingXClientError
from src.marketdata import MarketData


class _DummyClient:
    async def get_recent_trades(self, symbol: str, limit: int = 200):
        # True  => buyer maker => seller taker
        # False => buyer taker
        return [
            {"qty": "2", "isBuyerMaker": False},  # taker buy
            {"qty": "1", "isBuyerMaker": True},   # taker sell
            {"qty": "1", "isBuyerMaker": False},  # taker buy
        ]


def test_fetch_taker_buy_ratio_real(cfg, db) -> None:
    cfg.use_real_taker_data = True
    md = MarketData(cfg, _DummyClient(), db)
    ratio = asyncio.run(md.fetch_taker_buy_ratio_real("BTC-USDT"))
    assert ratio is not None
    assert ratio == pytest.approx(0.75)


class _FailingClient:
    async def get_recent_trades(self, symbol: str, limit: int = 200):
        raise BingXClientError(404, "endpoint unavailable")


def test_fetch_taker_buy_ratio_real_handles_failure(cfg, db) -> None:
    cfg.use_real_taker_data = True
    md = MarketData(cfg, _FailingClient(), db)
    ratio = asyncio.run(md.fetch_taker_buy_ratio_real("BTC-USDT"))
    assert ratio is None
