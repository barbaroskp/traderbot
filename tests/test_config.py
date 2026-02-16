"""Tests for configuration loading and validation."""

from __future__ import annotations

import pytest

from src.config import MarginMode, Settings, load_config


class TestConfig:
    def test_defaults(self) -> None:
        cfg = Settings(bingx_api_key="k", bingx_api_secret="s")
        assert cfg.paper_mode is True
        assert cfg.allow_live_trading is False
        assert cfg.initial_capital_usdt == 50.0
        assert cfg.leverage == 1
        assert cfg.margin_mode == MarginMode.ISOLATED

    def test_is_live_requires_both_flags(self) -> None:
        cfg = Settings(paper_mode=False, allow_live_trading=True)
        assert cfg.is_live() is True

        cfg2 = Settings(paper_mode=False, allow_live_trading=False)
        assert cfg2.is_live() is False

        cfg3 = Settings(paper_mode=True, allow_live_trading=True)
        assert cfg3.is_live() is False

    def test_validate_live_ready(self) -> None:
        cfg = Settings(paper_mode=True, allow_live_trading=False)
        issues = cfg.validate_live_ready()
        assert len(issues) >= 2  # missing key + paper_mode + not allowed

    def test_validate_live_ready_good(self) -> None:
        cfg = Settings(
            bingx_api_key="real_key",
            bingx_api_secret="real_secret",
            paper_mode=False,
            allow_live_trading=True,
        )
        issues = cfg.validate_live_ready()
        assert len(issues) == 0

    def test_leverage_validation(self) -> None:
        with pytest.raises(Exception):
            Settings(leverage=0)
        with pytest.raises(Exception):
            Settings(leverage=200)

    def test_strategy_defaults(self) -> None:
        cfg = Settings()
        assert cfg.fast_ema == 9
        assert cfg.slow_ema == 21
        assert cfg.entry_threshold_bps == 25.0
        assert cfg.tp_bps == 40.0
        assert cfg.sl_bps == 30.0
