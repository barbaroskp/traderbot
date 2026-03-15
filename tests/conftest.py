"""Shared pytest fixtures."""

from __future__ import annotations

import os
import tempfile

import pytest

from src.config import Settings
from src.storage import Storage


@pytest.fixture()
def cfg() -> Settings:
    """Test config with sensible defaults."""
    return Settings(
        bingx_api_key="test_key_123",
        bingx_api_secret="test_secret_456",
        paper_mode=True,
        allow_live_trading=False,
        initial_capital_usdt=20.0,
        db_path=":memory:",
        log_level="DEBUG",
        log_file="",
    )


@pytest.fixture()
def db(tmp_path) -> Storage:
    """In-memory SQLite for tests."""
    db_path = tmp_path / "test.db"
    return Storage(str(db_path))


@pytest.fixture()
def cfg_live() -> Settings:
    """Config with live trading enabled."""
    return Settings(
        bingx_api_key="live_key",
        bingx_api_secret="live_secret",
        paper_mode=False,
        allow_live_trading=True,
        initial_capital_usdt=20.0,
        db_path=":memory:",
    )
