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
        assert cfg.leverage == 2
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
        """Lock in the conservative defaults.

        The previous version of this test asserted the settings that produced the
        -84% run (80%/20% margin, 15bps entry threshold, 300-symbol shortlist,
        30bps spread cap, 5 concurrent positions, 3-minute scans). Those values
        are the thing being corrected, so the test asserts the new ones.
        """
        cfg = Settings()
        # Capital sizing (percentage-based)
        assert cfg.max_total_margin_pct == 0.30
        assert cfg.max_trade_margin_pct == 0.10
        assert cfg.per_trade_fraction == 0.05
        # Signal generation
        assert cfg.entry_threshold_bps == 30.0
        assert cfg.min_cluster_confluence == 4
        assert cfg.min_weighted_score_no_ema == 45.0
        assert cfg.use_regime_filter is True
        assert cfg.rsi_full_vote_only is True
        # Selector — spread is paid as slippage, so keep the universe liquid
        assert cfg.max_spread_bps == 8.0
        assert cfg.min_depth_usdt == 25_000.0
        assert cfg.shortlist_size == 25
        # Frequency — the dominant cost driver
        assert cfg.max_open_positions == 2
        assert cfg.cooldown_minutes == 60
        assert cfg.scan_interval_minutes == 5

    def test_cost_model_is_not_optimistic(self) -> None:
        """Fees, slippage and funding must all be charged.

        The 4bps fee default understated BingX taker cost (5bps, and BOTH sides
        are taker), slippage was assumed at 3bps, and funding was not modelled at
        all. Together that made every backtest and paper run look better than
        live by roughly 20bps per round trip.
        """
        cfg = Settings()
        assert cfg.fee_rate_bps >= 5.0
        assert cfg.slippage_assumption_bps >= 5.0
        assert cfg.use_funding_cost is True
        # 2 x fee + 2 x slippage
        assert cfg.round_trip_cost_bps == pytest.approx(
            2 * cfg.fee_rate_bps + 2 * cfg.slippage_assumption_bps
        )
        # TP must clear the full round trip, not just fees
        assert cfg.min_tp_net_bps > 0
        assert cfg.min_tp_bps > cfg.round_trip_cost_bps

    def test_safety_mechanisms_enabled(self) -> None:
        """A kill switch that never fires is not a kill switch."""
        cfg = Settings()
        assert cfg.soft_kill_switch_enabled is True
        assert cfg.soft_kill_drawdown_pct <= 25.0
        assert cfg.risk_state_disabled is False
        assert cfg.selector_lenient_enabled is False
        # Kelly must be able to size to zero on a negative measured edge
        assert cfg.kelly_min_fraction == 0.0
        assert cfg.kelly_block_on_negative_edge is True
        # Leverage stays low until an edge is demonstrated out-of-sample
        assert cfg.max_leverage_allowed <= 3
        assert cfg.dynamic_leverage_enabled is False

    def test_higher_tf_limits_survive_dropping_partial_candle(self) -> None:
        """compute_indicators() needs >=50 CLOSED candles.

        fetch_* drops the in-progress candle, so a limit of exactly 50 would
        yield 49 and silently leave higher_tf_trend permanently NEUTRAL.
        """
        cfg = Settings()
        assert cfg.higher_tf_limit > 50
        assert cfg.swing_trend_limit > 50
        assert cfg.kline_limit > 50
        assert cfg.swing_kline_limit > 50
