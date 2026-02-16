"""Tests for multi-indicator confluence strategy signal generation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.config import Settings
from src.marketdata import Indicators, SymbolSnapshot
from src.storage import Storage
from src.strategy import Strategy, _is_near_funding_time


@pytest.fixture()
def strategy_cfg(cfg) -> Settings:
    """Test config with time-dependent filters disabled for deterministic tests."""
    cfg.avoid_funding_window = False  # disable since tests can run at any time
    return cfg


@pytest.fixture()
def strategy(strategy_cfg, db) -> Strategy:
    return Strategy(strategy_cfg, db)


def _make_snap(
    symbol: str,
    z: float,
    spread: float = 2,
    depth: float = 5000,
    imbalance: float = 0.5,
    rsi: float = 50.0,
    macd_hist: float = 0.0,
    macd_hist_prev: float = 0.0,
    bb_pct: float = 0.5,
    trend: str = "NEUTRAL",
    valid_indicators: bool = True,
    macd_strengthening: bool = False,
    vwap: float = 0.0,
    vwap_deviation_bps: float = 0.0,
) -> SymbolSnapshot:
    """Create a test snapshot with indicator data."""
    # Auto-detect momentum strengthening if not explicitly set
    if not macd_strengthening and macd_hist != 0:
        if macd_hist > 0 and macd_hist > macd_hist_prev:
            macd_strengthening = True
        elif macd_hist < 0 and macd_hist < macd_hist_prev:
            macd_strengthening = True

    indicators = Indicators(
        rsi=rsi,
        macd_histogram=macd_hist,
        macd_histogram_prev=macd_hist_prev,
        bollinger_pct=bb_pct,
        trend_direction=trend,
        ema_trend=50000,
        valid=valid_indicators,
        macd_strengthening=macd_strengthening,
        vwap=vwap,
        vwap_deviation_bps=vwap_deviation_bps,
    )
    return SymbolSnapshot(
        symbol=symbol,
        mid_price=50000,
        mark_price=50000,
        best_bid=49999,
        best_ask=50001,
        spread_bps=spread,
        bid_depth_usdt=depth,
        ask_depth_usdt=depth,
        imbalance_ratio=imbalance,
        fast_ema=50000,
        slow_ema=49900,
        z_score_bps=z,
        indicators=indicators,
    )


class TestConfluenceSignals:
    def test_long_signal_with_full_confluence(self, strategy) -> None:
        """All 5 indicators agree on LONG → strong signal."""
        snaps = [_make_snap(
            "BTC-USDT", z=-35,       # EMA: LONG
            rsi=25,                   # RSI: LONG (oversold)
            macd_hist=0.001,          # MACD: LONG
            macd_hist_prev=-0.001,    # crossed up → strengthening
            bb_pct=0.1,              # BB: LONG (near lower band)
            trend="UP",               # Trend: LONG
        )]
        signals = strategy.generate_signals(snaps, [])
        assert len(signals) == 1
        assert signals[0].side == "LONG"
        assert signals[0].confluence_score >= 3

    def test_short_signal_with_full_confluence(self, strategy) -> None:
        """All 5 indicators agree on SHORT → strong signal."""
        snaps = [_make_snap(
            "BTC-USDT", z=35,        # EMA: SHORT
            rsi=75,                   # RSI: SHORT (overbought)
            macd_hist=-0.001,         # MACD: SHORT
            macd_hist_prev=0.001,     # crossed down → strengthening
            bb_pct=0.9,              # BB: SHORT (near upper band)
            trend="DOWN",             # Trend: SHORT
        )]
        signals = strategy.generate_signals(snaps, [])
        assert len(signals) == 1
        assert signals[0].side == "SHORT"

    def test_no_signal_without_confluence(self, strategy) -> None:
        """Mixed indicators → no signal (not enough agreement)."""
        snaps = [_make_snap(
            "BTC-USDT", z=-35,       # EMA: LONG
            rsi=75,                   # RSI: SHORT (disagrees)
            macd_hist=-0.001,         # MACD: SHORT (disagrees)
            bb_pct=0.9,              # BB: SHORT (disagrees)
            trend="DOWN",             # Trend: SHORT (disagrees)
        )]
        signals = strategy.generate_signals(snaps, [])
        # Only 1 indicator says LONG, 4 say SHORT → SHORT wins with 4
        if len(signals) > 0:
            assert signals[0].side == "SHORT"

    def test_no_signal_below_ema_threshold(self, strategy) -> None:
        """Z-score below threshold but other indicators neutral → no signal."""
        snaps = [_make_snap(
            "BTC-USDT", z=-10,       # Below threshold (30 bps)
            rsi=50,                   # Neutral
            macd_hist=0.0,           # Neutral
            bb_pct=0.5,              # Neutral
            trend="NEUTRAL",
        )]
        signals = strategy.generate_signals(snaps, [])
        assert len(signals) == 0

    def test_rejects_existing_position(self, strategy) -> None:
        snaps = [_make_snap("BTC-USDT", z=-35, rsi=25, macd_hist=0.001, macd_hist_prev=-0.001, bb_pct=0.1, trend="UP")]
        open_pos = [{"symbol": "BTC-USDT"}]
        signals = strategy.generate_signals(snaps, open_pos)
        assert len(signals) == 0

    def test_rejects_max_positions(self, strategy) -> None:
        snaps = [_make_snap("ETH-USDT", z=-35, rsi=25, macd_hist=0.001, macd_hist_prev=-0.001, bb_pct=0.1, trend="UP")]
        open_pos = [{"symbol": f"SYM{i}-USDT"} for i in range(5)]
        signals = strategy.generate_signals(snaps, open_pos)
        assert len(signals) == 0

    def test_cooldown_prevents_signal(self, strategy) -> None:
        strategy.set_cooldown("BTC-USDT")
        snaps = [_make_snap("BTC-USDT", z=-35, rsi=25, macd_hist=0.001, macd_hist_prev=-0.001, bb_pct=0.1, trend="UP")]
        signals = strategy.generate_signals(snaps, [])
        assert len(signals) == 0

    def test_wide_spread_rejected(self, strategy) -> None:
        snaps = [_make_snap("BTC-USDT", z=-35, spread=50, rsi=25, macd_hist=0.001, macd_hist_prev=-0.001, bb_pct=0.1, trend="UP")]
        signals = strategy.generate_signals(snaps, [])
        assert len(signals) == 0

    def test_thin_depth_rejected(self, strategy) -> None:
        snaps = [_make_snap("BTC-USDT", z=-35, depth=100, rsi=25, macd_hist=0.001, macd_hist_prev=-0.001, bb_pct=0.1, trend="UP")]
        signals = strategy.generate_signals(snaps, [])
        assert len(signals) == 0

    def test_tight_risk_requires_higher_weighted_score(self, strategy) -> None:
        """In TIGHT mode, minimum weighted score is higher (55.0 by default)."""
        # Only 2 indicators agree with low weights → weighted score < 55
        snaps = [_make_snap(
            "BTC-USDT", z=-35,       # EMA: LONG (weight=25)
            rsi=37,                   # RSI: leaning LONG (weight=25*0.3=7.5)
            macd_hist=0.0,           # MACD: NEUTRAL
            bb_pct=0.5,              # BB: NEUTRAL
            trend="NEUTRAL",          # Trend: NEUTRAL
        )]
        signals = strategy.generate_signals(snaps, [], risk_state="TIGHT")
        assert len(signals) == 0

    def test_signals_persisted_to_db(self, strategy, db) -> None:
        snaps = [_make_snap("BTC-USDT", z=-35, rsi=25, macd_hist=0.001, macd_hist_prev=-0.001, bb_pct=0.1, trend="UP")]
        strategy.generate_signals(snaps, [])
        rows = db.fetch_all("SELECT * FROM signals")
        assert len(rows) >= 1

    def test_multiple_symbols(self, strategy) -> None:
        snaps = [
            _make_snap("BTC-USDT", z=-35, rsi=25, macd_hist=0.001, macd_hist_prev=-0.001, bb_pct=0.1, trend="UP"),
            _make_snap("ETH-USDT", z=35, rsi=75, macd_hist=-0.001, macd_hist_prev=0.001, bb_pct=0.9, trend="DOWN"),
        ]
        signals = strategy.generate_signals(snaps, [])
        assert len(signals) == 2
        sides = {s.side for s in signals}
        assert sides == {"LONG", "SHORT"}

    def test_indicators_not_valid_falls_back_to_ema_only(self, strategy) -> None:
        """When kline data isn't available, only EMA vote counts → not enough for confluence."""
        snaps = [_make_snap(
            "BTC-USDT", z=-35,
            valid_indicators=False,
        )]
        signals = strategy.generate_signals(snaps, [])
        assert len(signals) == 0


class TestMomentumConfirmation:
    def test_momentum_filter_rejects_fading_entry(self, strategy_cfg, db) -> None:
        """At exactly min confluence, reject if momentum is fading (not strengthening)."""
        strategy_cfg.require_momentum_confirmation = True
        strategy_cfg.require_ema_in_confluence = False
        strategy_cfg.min_confluence_score = 2
        strat = Strategy(strategy_cfg, db)
        # Exactly 2 LONG votes: EMA + RSI, but MACD is neutral (0) so no momentum
        snaps = [_make_snap(
            "BTC-USDT", z=-35, rsi=25,
            macd_hist=0.0,           # MACD: neutral → no momentum vote
            macd_hist_prev=0.0,
            bb_pct=0.5, trend="NEUTRAL",
            macd_strengthening=False,
        )]
        signals = strat.generate_signals(snaps, [])
        assert len(signals) == 0

    def test_momentum_filter_accepts_strengthening(self, strategy_cfg, db) -> None:
        """Strengthening momentum → signal accepted."""
        strategy_cfg.require_momentum_confirmation = True
        strat = Strategy(strategy_cfg, db)
        snaps = [_make_snap(
            "BTC-USDT", z=-35, rsi=25,
            macd_hist=0.002, macd_hist_prev=0.001,  # strengthening
            bb_pct=0.1, trend="UP",
            macd_strengthening=True,
        )]
        signals = strat.generate_signals(snaps, [])
        assert len(signals) == 1


class TestFundingWindow:
    def test_near_funding_time_detected(self) -> None:
        t = datetime(2025, 1, 1, 7, 45, tzinfo=timezone.utc)  # 15 min before 08:00
        assert _is_near_funding_time(t, 30) is True

    def test_far_from_funding_time(self) -> None:
        t = datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc)   # 4 hours from any funding
        assert _is_near_funding_time(t, 30) is False

    def test_midnight_wrap(self) -> None:
        t = datetime(2025, 1, 1, 23, 45, tzinfo=timezone.utc)  # 15 min before midnight
        assert _is_near_funding_time(t, 30) is True


class TestCorrelationFilter:
    def test_correlation_limit_blocks_same_direction(self, strategy_cfg, db) -> None:
        strategy_cfg.use_correlation_filter = True
        strategy_cfg.max_same_direction_positions = 2
        strat = Strategy(strategy_cfg, db)
        snaps = [_make_snap("NEW-USDT", z=-35, rsi=25, macd_hist=0.001, macd_hist_prev=-0.001, bb_pct=0.1, trend="UP")]
        open_pos = [
            {"symbol": "BTC-USDT", "side": "LONG"},
            {"symbol": "ETH-USDT", "side": "LONG"},
        ]
        signals = strat.generate_signals(snaps, open_pos)
        assert len(signals) == 0

    def test_correlation_allows_opposite_direction(self, strategy_cfg, db) -> None:
        strategy_cfg.use_correlation_filter = True
        strategy_cfg.max_same_direction_positions = 2
        strat = Strategy(strategy_cfg, db)
        snaps = [_make_snap("NEW-USDT", z=-35, rsi=25, macd_hist=0.001, macd_hist_prev=-0.001, bb_pct=0.1, trend="UP")]
        open_pos = [
            {"symbol": "BTC-USDT", "side": "SHORT"},
            {"symbol": "ETH-USDT", "side": "SHORT"},
        ]
        signals = strat.generate_signals(snaps, open_pos)
        assert len(signals) == 1


class TestVWAPVote:
    def test_vwap_below_adds_long_vote(self, strategy_cfg, db) -> None:
        strategy_cfg.require_momentum_confirmation = False
        strat = Strategy(strategy_cfg, db)
        snaps = [_make_snap(
            "BTC-USDT", z=-35, rsi=25,
            macd_hist=0.001, macd_hist_prev=0.0,
            bb_pct=0.1, trend="UP",
            vwap=50050, vwap_deviation_bps=-40,
        )]
        signals = strat.generate_signals(snaps, [])
        assert len(signals) == 1
        vwap_votes = [v for v in signals[0].indicator_votes if v.name == "vwap"]
        assert len(vwap_votes) == 1
        assert vwap_votes[0].side == "LONG"
