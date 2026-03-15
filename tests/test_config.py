"""Tests for configuration loading and validation."""

from __future__ import annotations

import pytest

from src.config import MarginMode, Settings, load_config


class TestConfig:
    def test_defaults(self) -> None:
        cfg = Settings(bingx_api_key="k", bingx_api_secret="s")
        assert cfg.paper_mode is True
        assert cfg.allow_live_trading is False
        assert cfg.initial_capital_usdt == 20.0
        assert cfg.leverage == 3
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

    def test_current_defaults(self) -> None:
        """Test current optimized defaults."""
        cfg = Settings()
        # Capital sizing (percentage-based)
        assert cfg.max_total_margin_pct == 0.80
        assert cfg.max_trade_margin_pct == 0.20
        assert cfg.per_trade_fraction == 0.15
        # Signal generation
        assert cfg.entry_threshold_bps == 15.0
        assert cfg.min_confluence_score == 3
        assert cfg.min_confluence_no_ema == 4
        assert cfg.min_weighted_score_no_ema == 35.0
        assert cfg.require_ema_in_confluence is False
        assert cfg.use_regime_filter is False
        assert cfg.rsi_full_vote_only is False
        # Selector
        assert cfg.max_spread_bps == 30.0
        assert cfg.min_depth_usdt == 1000.0
        assert cfg.shortlist_size == 300
        # Positions
        assert cfg.max_open_positions == 5
        assert cfg.cooldown_minutes == 10
        assert cfg.scan_interval_minutes == 3
