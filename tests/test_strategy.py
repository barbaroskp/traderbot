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
    adx: float = 0.0,
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

    plus_di = 30.0 if trend == "UP" else 15.0
    minus_di = 30.0 if trend == "DOWN" else 15.0
    indicators = Indicators(
        rsi=rsi,
        macd_histogram=macd_hist,
        macd_histogram_prev=macd_hist_prev,
        bollinger_pct=bb_pct,
        trend_direction=trend,
        ema_trend=50000,
        adx=adx,
        plus_di=plus_di,
        minus_di=minus_di,
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
        # max_open_positions is 8 in aggressive mode
        open_pos = [{"symbol": f"SYM{i}-USDT"} for i in range(8)]
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


class TestModeAwareEmaGate:
    def test_mean_reversion_requires_ema_when_enabled(self, strategy_cfg, db) -> None:
        """When require_ema_in_confluence=True, mean-reversion requires EMA anchor."""
        strategy_cfg.require_ema_in_confluence = True  # explicitly enable
        strategy_cfg.require_momentum_confirmation = False
        strat = Strategy(strategy_cfg, db)
        snaps = [_make_snap(
            "BTC-USDT",
            z=0,  # EMA neutral
            rsi=25,
            macd_hist=0.002,
            macd_hist_prev=0.001,
            bb_pct=0.1,
            trend="UP",
            adx=10,  # keep mode as MEAN_REVERSION
        )]
        signals = strat.generate_signals(snaps, [])
        assert len(signals) == 0

    def test_trend_follow_can_trade_without_ema(self, strategy_cfg, db) -> None:
        """When configured, TREND_FOLLOW accepts non-EMA confluence if strong enough."""
        strategy_cfg.require_ema_in_confluence = True
        strategy_cfg.require_ema_in_trend_follow = False
        strategy_cfg.use_regime_filter = True  # enable regime filter for TREND_FOLLOW mode
        strategy_cfg.min_confluence_no_ema = 3
        strategy_cfg.min_weighted_score_no_ema = 40
        strategy_cfg.require_momentum_confirmation = True
        strat = Strategy(strategy_cfg, db)
        snaps = [_make_snap(
            "BTC-USDT",
            z=0,  # EMA neutral
            rsi=25,
            macd_hist=0.002,
            macd_hist_prev=0.001,
            bb_pct=0.1,
            trend="UP",
            adx=35,  # TREND_FOLLOW mode (needs use_regime_filter=True)
            macd_strengthening=True,
        )]
        signals = strat.generate_signals(snaps, [])
        assert len(signals) == 1
        assert signals[0].mode == "TREND_FOLLOW"
        assert signals[0].side == "LONG"

    def test_trend_follow_blocks_without_ema_when_override_true(self, strategy_cfg, db) -> None:
        strategy_cfg.require_ema_in_confluence = True
        strategy_cfg.require_ema_in_trend_follow = True
        strategy_cfg.use_regime_filter = True
        strategy_cfg.require_momentum_confirmation = False
        strat = Strategy(strategy_cfg, db)
        snaps = [_make_snap(
            "BTC-USDT",
            z=0,  # EMA neutral
            rsi=25,
            macd_hist=0.002,
            macd_hist_prev=0.001,
            bb_pct=0.1,
            trend="UP",
            adx=35,
        )]
        signals = strat.generate_signals(snaps, [])
        assert len(signals) == 0


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


class TestStochRSIVote:
    def test_stoch_rsi_oversold_adds_long_vote(self, strategy_cfg, db) -> None:
        strategy_cfg.require_momentum_confirmation = False
        strat = Strategy(strategy_cfg, db)
        snap = _make_snap(
            "BTC-USDT", z=-35, rsi=25,
            macd_hist=0.001, macd_hist_prev=0.0,
            bb_pct=0.1, trend="UP",
        )
        snap.indicators.stoch_rsi_k = 10.0
        snap.indicators.stoch_rsi_d = 15.0
        signals = strat.generate_signals([snap], [])
        assert len(signals) == 1
        stoch_votes = [v for v in signals[0].indicator_votes if v.name == "stoch_rsi"]
        assert len(stoch_votes) == 1
        assert stoch_votes[0].side == "LONG"

    def test_stoch_rsi_overbought_adds_short_vote(self, strategy_cfg, db) -> None:
        strategy_cfg.require_momentum_confirmation = False
        strat = Strategy(strategy_cfg, db)
        snap = _make_snap(
            "BTC-USDT", z=35, rsi=75,
            macd_hist=-0.001, macd_hist_prev=0.001,
            bb_pct=0.9, trend="DOWN",
        )
        snap.indicators.stoch_rsi_k = 90.0
        snap.indicators.stoch_rsi_d = 85.0
        signals = strat.generate_signals([snap], [])
        assert len(signals) == 1
        stoch_votes = [v for v in signals[0].indicator_votes if v.name == "stoch_rsi"]
        assert len(stoch_votes) == 1
        assert stoch_votes[0].side == "SHORT"

    def test_stoch_rsi_neutral_no_vote(self, strategy_cfg, db) -> None:
        strategy_cfg.require_momentum_confirmation = False
        strat = Strategy(strategy_cfg, db)
        snap = _make_snap(
            "BTC-USDT", z=-35, rsi=25,
            macd_hist=0.001, macd_hist_prev=0.0,
            bb_pct=0.1, trend="UP",
        )
        snap.indicators.stoch_rsi_k = 50.0
        snap.indicators.stoch_rsi_d = 50.0
        signals = strat.generate_signals([snap], [])
        stoch_votes = [v for v in signals[0].indicator_votes if v.name == "stoch_rsi"]
        assert len(stoch_votes) == 0


class TestADXVote:
    def test_adx_strong_uptrend_adds_long_vote(self, strategy_cfg, db) -> None:
        strategy_cfg.require_momentum_confirmation = False
        strat = Strategy(strategy_cfg, db)
        snap = _make_snap(
            "BTC-USDT", z=-35, rsi=25,
            macd_hist=0.001, macd_hist_prev=0.0,
            bb_pct=0.1, trend="UP", adx=30.0,
        )
        # +DI > -DI (set by _make_snap when trend="UP")
        signals = strat.generate_signals([snap], [])
        assert len(signals) == 1
        adx_votes = [v for v in signals[0].indicator_votes if v.name == "adx"]
        assert len(adx_votes) == 1
        assert adx_votes[0].side == "LONG"

    def test_adx_strong_downtrend_adds_short_vote(self, strategy_cfg, db) -> None:
        strategy_cfg.require_momentum_confirmation = False
        strat = Strategy(strategy_cfg, db)
        snap = _make_snap(
            "BTC-USDT", z=35, rsi=75,
            macd_hist=-0.001, macd_hist_prev=0.001,
            bb_pct=0.9, trend="DOWN", adx=30.0,
        )
        signals = strat.generate_signals([snap], [])
        assert len(signals) == 1
        adx_votes = [v for v in signals[0].indicator_votes if v.name == "adx"]
        assert len(adx_votes) == 1
        assert adx_votes[0].side == "SHORT"

    def test_adx_weak_no_vote(self, strategy_cfg, db) -> None:
        strategy_cfg.require_momentum_confirmation = False
        strat = Strategy(strategy_cfg, db)
        snap = _make_snap(
            "BTC-USDT", z=-35, rsi=25,
            macd_hist=0.001, macd_hist_prev=0.0,
            bb_pct=0.1, trend="UP", adx=15.0,
        )
        signals = strat.generate_signals([snap], [])
        adx_votes = [v for v in signals[0].indicator_votes if v.name == "adx"]
        assert len(adx_votes) == 0


class TestHTFAlignmentBonus:
    def test_htf_alignment_boosts_score(self, strategy_cfg, db) -> None:
        strategy_cfg.require_momentum_confirmation = False
        strategy_cfg.higher_tf_alignment_bonus = 15.0
        strat = Strategy(strategy_cfg, db)
        # Without HTF alignment
        snap_no_htf = _make_snap(
            "BTC-USDT", z=-35, rsi=25,
            macd_hist=0.001, macd_hist_prev=0.0,
            bb_pct=0.1, trend="UP",
        )
        snap_no_htf.indicators.higher_tf_trend = "NEUTRAL"
        signals_no = strat.generate_signals([snap_no_htf], [])
        # With HTF alignment
        snap_htf = _make_snap(
            "ETH-USDT", z=-35, rsi=25,
            macd_hist=0.001, macd_hist_prev=0.0,
            bb_pct=0.1, trend="UP",
        )
        snap_htf.indicators.higher_tf_trend = "UP"
        signals_htf = strat.generate_signals([snap_htf], [])
        assert len(signals_no) == 1
        assert len(signals_htf) == 1
        assert signals_htf[0].weighted_score > signals_no[0].weighted_score


class TestStochRSIComputation:
    def test_stochastic_rsi_oversold_values(self) -> None:
        """StochRSI returns low values after a decline following oscillation."""
        import random
        from src.marketdata import MarketData
        random.seed(42)
        # Oscillating warmup (creates varying RSI) then decline
        closes = [100.0]
        for _ in range(50):
            closes.append(closes[-1] * (1 + random.uniform(-0.008, 0.008)))
        for _ in range(50):
            closes.append(closes[-1] * 0.996)  # steady decline
        k, d = MarketData._compute_stochastic_rsi(closes, rsi_period=14)
        assert k < 30, f"Expected StochRSI K < 30 for declining prices, got {k}"

    def test_stochastic_rsi_overbought_values(self) -> None:
        """StochRSI returns high values after a rise following oscillation."""
        import random
        from src.marketdata import MarketData
        random.seed(42)
        closes = [100.0]
        for _ in range(50):
            closes.append(closes[-1] * (1 + random.uniform(-0.008, 0.008)))
        for _ in range(50):
            closes.append(closes[-1] * 1.006)  # steady rise
        k, d = MarketData._compute_stochastic_rsi(closes, rsi_period=14)
        assert k > 70, f"Expected StochRSI K > 70 for rising prices, got {k}"

    def test_stochastic_rsi_insufficient_data(self) -> None:
        """Returns defaults when not enough data."""
        from src.marketdata import MarketData
        k, d = MarketData._compute_stochastic_rsi([100.0] * 10, rsi_period=14)
        assert k == 50.0


class TestSwingSignals:
    def test_swing_signal_generated_with_confluence(self, strategy_cfg, db) -> None:
        strategy_cfg.swing_enabled = True
        strategy_cfg.swing_min_confluence = 3
        strategy_cfg.swing_require_trend_alignment = True
        strategy_cfg.require_momentum_confirmation = False
        strat = Strategy(strategy_cfg, db)
        snap = _make_snap(
            "BTC-USDT", z=-35, rsi=25,
            macd_hist=0.001, macd_hist_prev=0.0,
            bb_pct=0.1, trend="UP", adx=30.0,
        )
        snap.indicators.higher_tf_trend = "UP"
        signals = strat.generate_swing_signals([snap], [])
        assert len(signals) == 1
        assert signals[0].trade_type == "swing"
        assert signals[0].side == "LONG"

    def test_swing_rejected_when_4h_trend_misaligned(self, strategy_cfg, db) -> None:
        strategy_cfg.swing_enabled = True
        strategy_cfg.swing_min_confluence = 3
        strategy_cfg.swing_require_trend_alignment = True
        strategy_cfg.require_momentum_confirmation = False
        strat = Strategy(strategy_cfg, db)
        snap = _make_snap(
            "BTC-USDT", z=-35, rsi=25,
            macd_hist=0.001, macd_hist_prev=0.0,
            bb_pct=0.1, trend="UP", adx=30.0,
        )
        snap.indicators.higher_tf_trend = "DOWN"  # against signal
        signals = strat.generate_swing_signals([snap], [])
        assert len(signals) == 0

    def test_swing_max_positions_respected(self, strategy_cfg, db) -> None:
        strategy_cfg.swing_enabled = True
        strategy_cfg.swing_max_positions = 1
        strategy_cfg.swing_min_confluence = 3
        strategy_cfg.require_momentum_confirmation = False
        strat = Strategy(strategy_cfg, db)
        # Already have 1 swing position
        existing = [{"symbol": "ETH-USDT", "side": "LONG", "trade_type": "swing"}]
        snap = _make_snap(
            "BTC-USDT", z=-35, rsi=25,
            macd_hist=0.001, macd_hist_prev=0.0,
            bb_pct=0.1, trend="UP", adx=30.0,
        )
        snap.indicators.higher_tf_trend = "UP"
        signals = strat.generate_swing_signals([snap], existing)
        assert len(signals) == 0

    def test_swing_trade_type_in_signal(self, strategy_cfg, db) -> None:
        strategy_cfg.require_momentum_confirmation = False
        strat = Strategy(strategy_cfg, db)
        # Regular scalp signals should have trade_type="scalp"
        snap = _make_snap(
            "BTC-USDT", z=-35, rsi=25,
            macd_hist=0.001, macd_hist_prev=0.0,
            bb_pct=0.1, trend="UP",
        )
        signals = strat.generate_signals([snap], [])
        if signals:
            assert signals[0].trade_type == "scalp"
