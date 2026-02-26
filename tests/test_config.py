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
        assert cfg.leverage == 5  # aggressive default
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
        assert cfg.entry_threshold_bps == 25.0  # aggressive
        assert cfg.tp_bps == 120.0              # aggressive
        assert cfg.sl_bps == 50.0

    def test_aggressive_params(self) -> None:
        """Test aggressive profitability parameters."""
        cfg = Settings()
        # Aggressive capital/risk (margin-based)
        assert cfg.max_total_margin_usdt == 15.0
        assert cfg.max_trade_margin_usdt == 4.0
        assert cfg.per_trade_fraction == 0.08
        assert cfg.leverage == 5
        assert cfg.leverage_high_conviction == 10
        assert cfg.max_leverage_allowed == 15
        assert cfg.max_open_positions == 8
        assert cfg.cooldown_minutes == 6
        # Aggressive sizing
        assert cfg.target_risk_pct == 0.02
        assert cfg.max_same_direction_positions == 5
        # Dynamic leverage
        assert cfg.dyn_leverage_tier1 == 7
        assert cfg.dyn_leverage_tier2 == 10
        assert cfg.dyn_leverage_tier3 == 15
        # Adaptive scan
        assert cfg.use_adaptive_scan is True
        assert cfg.scan_interval_active_minutes == 1
        # Mode-aware EMA gate
        assert cfg.require_ema_in_mean_reversion is None
        assert cfg.require_ema_in_trend_follow is None
        assert cfg.require_ema_in_breakout is None
        assert cfg.min_confluence_no_ema == 3
        assert cfg.min_weighted_score_no_ema == 45.0  # normalized 0-100 scale
