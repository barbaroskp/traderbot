"""Multi-indicator cluster confluence strategy.

Signal generation uses 23 indicators grouped into 6 voting clusters plus one
bonus-only cluster.  Each cluster requires internal consensus (2+ members
agreeing) before casting a single cluster vote.  A trade is taken when enough
*clusters* agree (default 4/6).  This prevents redundant indicators
(RSI/StochRSI/WillR) from inflating confluence and ensures each vote represents
a genuinely different data source.

Clusters are also grouped by market hypothesis so that mean-reverting and
trend-following evidence stay separable rather than cancelling each other out
inside a single bucket — see CLUSTER_MEMBERS below.

Indicators:
  1. EMA Z-Score: Mean-reversion signal based on price deviation from fast EMA (kline-based)
  2. RSI: Oversold → LONG, Overbought → SHORT
  3. MACD: Histogram crossover direction + momentum strength
  4. Bollinger Bands: Price at lower band → LONG, upper band → SHORT
  5. Trend EMA: Price above/below 50-EMA for trend confirmation
  6. Orderbook Imbalance: Bid-heavy → LONG, ask-heavy → SHORT
  7. Volume Spike: Trend-aligned volume surge confirmation
  8. VWAP: Price below VWAP → LONG (undervalued), above → SHORT (mean reversion)
  9. Momentum: MACD histogram strengthening in signal direction
  10. RSI Divergence: Price/RSI divergence reversal signals (1.5x RSI weight)
  11. Stochastic RSI: More sensitive overbought/oversold via stochastic of RSI
  12. ADX Strength: Directional movement confirms trend (+DI vs -DI)
  13. Taker Buy/Sell Ratio: Net buying/selling pressure from candle direction
  14. OBV (On-Balance Volume): Cumulative volume flow + divergence detection
  15. Williams %R: Short-term overbought/oversold oscillator
  16. TTM Squeeze: Bollinger inside Keltner = low vol breakout imminent
  17. Open Interest: OI change + price direction = manipulation detection
  18. Price Velocity: Rate of price change confirms momentum or warns of reversal
  19. Whale Detection: Large orders in orderbook (bid/ask walls) indicate support/resistance
  20. Liquidation Cascade: OI drop + price movement = forced liquidations or squeezes
  21. Volume Profile: POC/VAH/VAL as support/resistance, breakout detection
  22. Composite Sentiment: Funding rate + OI + taker ratio + Fear & Greed Index

Additional filters & bonuses:
  - Funding time avoidance (±30 min around 00/08/16 UTC)
  - Correlation filter (max same-direction positions)
  - Momentum quality filter (require momentum confirmation at min confluence)
  - Signal strength scoring (indicator extremity bonus)
  - Higher-TF alignment bonus (15m trend confirms 5m signal)
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.config import Settings
from src.logger import get_logger
from src.marketdata import Indicators, SymbolSnapshot
from src.storage import Storage

log = get_logger(__name__)


@dataclass
class IndicatorVote:
    """A single indicator's vote."""
    name: str
    side: str  # LONG, SHORT, NEUTRAL
    weight: float = 0.0
    value: float = 0.0  # the indicator's raw value for logging
    reason: str = ""


@dataclass
class ClusterVote:
    """A cluster's aggregated vote — requires internal consensus."""
    name: str
    side: str  # LONG, SHORT, NEUTRAL
    weight: float = 0.0
    member_votes: list[IndicatorVote] = field(default_factory=list)
    agreement: int = 0  # how many members agreed
    total_members: int = 0  # how many members voted non-neutral
    reason: str = ""


# ── Cluster definitions ───────────────────────────────────────────────
# Each cluster groups indicators that measure the same underlying concept.
# A cluster votes only when enough internal members agree (2/3 or 2/2).
# This prevents redundant indicators from inflating confluence.
# Clusters are grouped so that each one represents a single, coherent market
# hypothesis. This matters: the previous "trend" cluster contained BOTH
# ema_zscore (mean reversion: price below the fast EMA is a BUY because it
# should revert) AND trend/adx (trend following: price above EMA50 is a BUY
# because it should persist). Those are contradictory theories of how price
# behaves, and putting them in one cluster let an arbitrary internal majority
# decide the direction while hiding the disagreement from the confluence count.
#
# They now live in separate clusters, so they are allowed to disagree openly and
# the confluence requirement (4 of 6) is what forces them to line up. When
# mean_revert and trend BOTH point the same way you have a pullback inside a
# trend — a coherent setup — rather than two incompatible signals cancelling out
# inside one bucket.
CLUSTER_MEMBERS: dict[str, list[str]] = {
    # ── Mean-reverting family: price is stretched away from a reference level ──
    "oscillator":  ["rsi", "stoch_rsi", "williams_r", "rsi_divergence"],  # overbought/oversold
    "mean_revert": ["ema_zscore", "bollinger", "vwap", "volume_profile"], # price vs fair level
    # ── Trend-following family: direction is expected to persist ──
    "trend":       ["trend", "adx", "macd", "momentum"],                  # direction/strength
    # ── Regime / expansion ──
    # volume_profile_break is the BREAKOUT reading of the volume profile and
    # belongs here, not with the reversion reading in mean_revert.
    "volatility":  ["squeeze", "breakout", "volume_spike", "velocity",
                    "volume_profile_break"],                              # vol regime/breakout
    # ── Positioning and book pressure ──
    "orderflow":   ["orderbook", "taker_ratio", "whale"],                 # market depth
    "flow":        ["obv", "open_interest", "liq_cascade"],               # volume / OI flow
    # ── Bonus only, never votes on its own ──
    # sentiment is driven partly by the Fear & Greed index, which is a DAILY,
    # MARKET-WIDE number: identical for every symbol. It is a portfolio-level
    # tilt, not per-symbol evidence, so it must not count toward confluence.
    "contrarian":  ["sentiment"],
}

# Contrarian cluster doesn't vote independently — it adds bonus/penalty
BONUS_ONLY_CLUSTERS = {"contrarian"}


@dataclass
class Signal:
    """A trading signal with confluence details."""
    symbol: str
    side: str  # LONG or SHORT
    z_score_bps: float
    mid_price: float
    fast_ema: float
    slow_ema: float
    spread_bps: float
    depth_usdt: float
    accepted: bool = True
    reject_reason: str = ""
    ts: str = ""
    # ── Confluence details ──
    confluence_score: int = 0        # how many indicators agree
    weighted_score: float = 0.0      # total weighted score (0-100)
    indicator_votes: list[IndicatorVote] = field(default_factory=list)
    rsi: float = 50.0
    macd_histogram: float = 0.0
    macd_histogram_prev: float = 0.0
    bollinger_pct: float = 0.5
    trend_direction: str = "NEUTRAL"
    higher_tf_trend: str = "NEUTRAL"
    atr: float = 0.0
    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0
    funding_rate: float = 0.0
    volume_ratio: float = 1.0
    volume_spike: bool = False
    mode: str = ""
    # ── New fields ──
    momentum_confirmed: bool = False
    vwap_deviation_bps: float = 0.0
    trade_type: str = "scalp"  # "scalp" or "swing"
    cluster_votes: list[ClusterVote] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = datetime.now(timezone.utc).isoformat()


def _is_near_funding_time(now: datetime, window_minutes: int = 30) -> bool:
    """Check if current time is within ±window_minutes of a funding time (00/08/16 UTC)."""
    funding_hours = (0, 8, 16)
    current_minute_of_day = now.hour * 60 + now.minute
    for fh in funding_hours:
        funding_minute = fh * 60
        # Handle wrap-around at midnight
        diff = abs(current_minute_of_day - funding_minute)
        diff = min(diff, 1440 - diff)  # 1440 = minutes in a day
        if diff <= window_minutes:
            return True
    return False


class Strategy:
    """Multi-indicator confluence signal generator."""

    def __init__(self, cfg: Settings, db: Storage) -> None:
        self.cfg = cfg
        self.db = db
        self._cooldowns: dict[str, datetime] = {}
        # Why candidates were discarded BEFORE a Signal object exists. Without
        # this the cycle log could only say "no candidate reached confluence",
        # which is what made the filters get loosened blind.
        self._gate_stats: Counter[str] = Counter()

    def generate_signals(
        self,
        snapshots: list[SymbolSnapshot],
        open_positions: list[dict[str, Any]],
        risk_state: str = "NORMAL",
    ) -> list[Signal]:
        """Score all snapshots and return accepted signals."""
        now = datetime.now(timezone.utc)
        self._gate_stats.clear()
        open_symbols = {p["symbol"] for p in open_positions}
        signals: list[Signal] = []
        # Prevent over-allocation within the same cycle by counting
        # accepted new signals as pending positions.
        open_count_simulated = len(open_positions)

        # Dynamic threshold based on risk state
        max_positions = self._get_max_positions(risk_state)
        min_confluence = self._get_min_confluence(risk_state)

        for snap in snapshots:
            signal = self._evaluate_snapshot(
                snap=snap,
                now=now,
                min_confluence=min_confluence,
                max_positions=max_positions,
                open_symbols=open_symbols,
                open_count=open_count_simulated,
                open_positions=open_positions,
                risk_state=risk_state,
            )
            if signal is None:
                continue
            # Rejected candidates are returned too (not swallowed as None) so the
            # reject_reasons histogram below is actually populated. Without it the
            # operator can never see WHY a cycle produced no trades, which is how
            # filters ended up being loosened blind.
            signals.append(signal)
            if signal.accepted:
                # Rejected signals were already persisted inside _evaluate_snapshot.
                self._persist_signal(signal)
                open_count_simulated += 1

        accepted_list = [s for s in signals if s.accepted]
        accepted_count = len(accepted_list)
        # Reject reason counts for logging
        reject_counts: dict[str, int] = {}
        for s in signals:
            if not s.accepted and getattr(s, "reject_reason", None):
                r = s.reject_reason or "unknown"
                reject_counts[r] = reject_counts.get(r, 0) + 1

        log.info(
            "signal generation",
            extra={
                "candidates": len(snapshots),
                "signals": len(signals),
                "accepted": accepted_count,
                "risk_state": risk_state,
                "signal_mode": self.cfg.signal_mode,
                "primary_thesis": self.cfg.primary_thesis,
                "min_confluence": min_confluence,
                **({"reject_reasons": reject_counts} if reject_counts else {}),
                **({"gates": dict(self._gate_stats)} if self._gate_stats else {}),
            },
        )
        if not accepted_count and reject_counts:
            log.warning(
                "no accepted signals; all rejected by filters",
                extra={"reject_reasons": reject_counts, "candidates": len(snapshots)},
            )
        elif not accepted_count and len(snapshots) > 0:
            log.warning(
                "no signals: no candidate reached min_confluence",
                extra={"candidates": len(snapshots), "min_confluence": min_confluence},
            )
        return accepted_list

    def _evaluate_snapshot(
        self,
        snap: SymbolSnapshot,
        now: datetime,
        min_confluence: int,
        max_positions: int,
        open_symbols: set[str],
        open_count: int,
        open_positions: list[dict[str, Any]],
        risk_state: str,
    ) -> Signal | None:
        """Evaluate a single snapshot using multi-indicator confluence."""
        symbol = snap.symbol

        # Skip if EMA not warmed up
        if snap.fast_ema <= 0 or snap.slow_ema <= 0:
            return None

        z = snap.z_score_bps
        # An extreme z-score means price has run a long way from the fast EMA.
        # That invalidates a MEAN-REVERSION entry, but it is precisely what a
        # trend/breakout entry wants. Applying the gate unconditionally meant the
        # bot carried breakout machinery (breakout, squeeze, volume_profile_break)
        # while throwing away the very moves that machinery exists to catch.
        gate_extremes = (
            self.cfg.signal_mode != "thesis" or self.cfg.primary_thesis == "mean_revert"
        )
        if gate_extremes and abs(z) > self.cfg.max_z_score_bps:
            self._gate_stats["z_score_extreme"] += 1
            return None

        # ── Compute indicator votes ───────────────────────────
        indicators = snap.indicators
        mode = self._determine_mode(indicators)
        votes = self._compute_votes(snap)

        # ── Cluster-based confluence ──────────────────────────
        # Group 22 individual votes into 6 voting clusters + 1 bonus cluster.
        # This prevents redundant indicators (RSI/StochRSI/WillR) from
        # inflating confluence count.  Each cluster = 1 vote max.
        cluster_votes, contrarian_bonus = self._compute_cluster_votes(votes)

        # Count cluster votes per side
        long_clusters = [c for c in cluster_votes if c.side == "LONG"]
        short_clusters = [c for c in cluster_votes if c.side == "SHORT"]
        long_cluster_count = len(long_clusters)
        short_cluster_count = len(short_clusters)
        long_weighted = sum(c.weight for c in long_clusters)
        short_weighted = sum(c.weight for c in short_clusters)

        # Apply contrarian bonus to the side it favors
        if contrarian_bonus > 0:
            long_weighted += contrarian_bonus
        elif contrarian_bonus < 0:
            short_weighted += abs(contrarian_bonus)

        # Pre-compute momentum for both sides
        long_momentum = self._check_momentum(indicators, "LONG")
        short_momentum = self._check_momentum(indicators, "SHORT")
        if self.cfg.require_momentum_confirmation and indicators.valid:
            no_mom_discount = getattr(self.cfg, "no_momentum_discount", 0.7)
            if not long_momentum:
                long_weighted *= no_mom_discount
            if not short_momentum:
                short_weighted *= no_mom_discount

        # Confluence rule: mode-aware EMA anchor
        require_ema = self._require_ema_for_mode(mode)
        ema_vote = next((v for v in votes if v.name == "ema_zscore"), None)

        # Cluster confluence threshold. Use the value resolved by the caller so
        # that the risk state actually tightens the gate (and so that the value
        # reported in logs is the one being enforced).
        min_cluster_confluence = min_confluence

        if self.cfg.signal_mode == "thesis":
            resolved = self._resolve_by_thesis(cluster_votes)
            if resolved is None:
                return None
            side, confluence_score, weighted_score = resolved
        elif require_ema:
            if not ema_vote or ema_vote.side == "NEUTRAL":
                self._gate_stats["no_ema_anchor"] += 1
                return None
            # EMA is part of the trend cluster; check cluster confluence
            if (ema_vote.side == "LONG" and long_cluster_count >= min_cluster_confluence
                    and long_weighted > short_weighted):
                side = "LONG"
                confluence_score = long_cluster_count
                weighted_score = long_weighted
            elif (ema_vote.side == "SHORT" and short_cluster_count >= min_cluster_confluence
                    and short_weighted > long_weighted):
                side = "SHORT"
                confluence_score = short_cluster_count
                weighted_score = short_weighted
            else:
                return None
        else:
            if long_cluster_count >= min_cluster_confluence and long_weighted > short_weighted:
                side = "LONG"
                confluence_score = long_cluster_count
                weighted_score = long_weighted
            elif short_cluster_count >= min_cluster_confluence and short_weighted > long_weighted:
                side = "SHORT"
                confluence_score = short_cluster_count
                weighted_score = short_weighted
            else:
                return None

        effective_min_confluence = min_cluster_confluence

        min_depth = min(snap.bid_depth_usdt, snap.ask_depth_usdt)
        breakout_up = (
            indicators.volume_spike
            and indicators.bollinger_pct >= self.cfg.breakout_bb_pct_high
        )
        breakout_down = (
            indicators.volume_spike
            and indicators.bollinger_pct <= self.cfg.breakout_bb_pct_low
        )
        breakout_confirmed = breakout_up or breakout_down

        # Momentum confirmation (pre-computed before confluence)
        momentum_confirmed = long_momentum if side == "LONG" else short_momentum

        # ── Normalize weighted score to 0-100 scale ──────────────
        # Raw score can reach ~350+ because each indicator contributes its
        # full config weight.  Normalize by dividing by the maximum possible
        # weight of the indicators that actually voted on the winning side.
        weighted_score, _raw = self._normalize_weighted_score(votes, side)
        # Breadth factor. Agreement quality alone is not conviction: two
        # unopposed indicators are trivially "100% agreed" while the other
        # twenty stay silent. Scaling by the share of voting clusters that
        # actually agreed makes the score increase with BOTH agreement and
        # breadth, which is the property position sizing and leverage need.
        # Without it, sparse evidence outscored broad evidence and the bot
        # sized UP into its weakest signals.
        voting_clusters = len(CLUSTER_MEMBERS) - len(BONUS_ONLY_CLUSTERS)
        if voting_clusters > 0:
            weighted_score *= min(confluence_score, voting_clusters) / voting_clusters
        # Apply momentum discount on normalized score (same effect as before)
        if self.cfg.require_momentum_confirmation and indicators.valid:
            if not momentum_confirmed:
                no_mom_discount = getattr(self.cfg, "no_momentum_discount", 0.7)
                weighted_score *= no_mom_discount
        # Bonuses (extremity, higher-TF, funding) are applied *on top* as
        # additive points on the 0-100 scale – they represent conviction
        # beyond base indicator agreement.
        weighted_score += self._compute_extremity_bonus(indicators, side)
        if self.cfg.higher_tf_alignment_bonus > 0 and indicators.higher_tf_trend != "NEUTRAL":
            if (side == "LONG" and indicators.higher_tf_trend == "UP") or \
               (side == "SHORT" and indicators.higher_tf_trend == "DOWN"):
                weighted_score += self.cfg.higher_tf_alignment_bonus
        if self.cfg.funding_contra_bonus > 0 and abs(snap.funding_rate) >= self.cfg.funding_rate_threshold:
            funding_magnitude = abs(snap.funding_rate) / self.cfg.funding_rate_threshold
            scaled_bonus = self.cfg.funding_contra_bonus * min(funding_magnitude, 3.0)
            if (side == "SHORT" and snap.funding_rate > 0) or \
               (side == "LONG" and snap.funding_rate < 0):
                weighted_score += scaled_bonus
        weighted_score = min(weighted_score, 100.0)

        signal = Signal(
            symbol=symbol,
            side=side,
            z_score_bps=snap.z_score_bps,
            mid_price=snap.mid_price,
            fast_ema=snap.fast_ema,
            slow_ema=snap.slow_ema,
            spread_bps=snap.spread_bps,
            depth_usdt=min_depth,
            confluence_score=confluence_score,
            weighted_score=weighted_score,
            indicator_votes=votes,
            rsi=indicators.rsi,
            macd_histogram=indicators.macd_histogram,
            macd_histogram_prev=indicators.macd_histogram_prev,
            bollinger_pct=indicators.bollinger_pct,
            trend_direction=indicators.trend_direction,
            higher_tf_trend=indicators.higher_tf_trend,
            atr=indicators.atr,
            adx=indicators.adx,
            plus_di=indicators.plus_di,
            minus_di=indicators.minus_di,
            funding_rate=snap.funding_rate,
            volume_ratio=indicators.volume_ratio,
            volume_spike=indicators.volume_spike,
            mode=mode,
            momentum_confirmed=momentum_confirmed,
            vwap_deviation_bps=indicators.vwap_deviation_bps,
            cluster_votes=cluster_votes,
        )

        # ── Rejection checks ───────────────────────────────────

        # Already have a position in this symbol
        if symbol in open_symbols:
            signal.accepted = False
            signal.reject_reason = "position_exists"
            self._persist_signal(signal)
            return signal

        # Max positions reached
        if open_count >= max_positions:
            signal.accepted = False
            signal.reject_reason = "max_positions"
            self._persist_signal(signal)
            return signal

        # Cooldown
        if self._in_cooldown(symbol, now):
            signal.accepted = False
            signal.reject_reason = "cooldown"
            self._persist_signal(signal)
            return signal

        # ── NEW: Funding time avoidance ──────────────────────
        if self.cfg.avoid_funding_window and _is_near_funding_time(now, self.cfg.funding_window_minutes):
            signal.accepted = False
            signal.reject_reason = "funding_window"
            self._persist_signal(signal)
            return signal

        # ── NEW: Correlation filter ──────────────────────────
        if self.cfg.use_correlation_filter:
            same_dir_count = sum(
                1 for p in open_positions if p.get("side") == side
            )
            if same_dir_count >= self.cfg.max_same_direction_positions:
                signal.accepted = False
                signal.reject_reason = "correlation_limit"
                self._persist_signal(signal)
                return signal

        # Spread too wide
        if snap.spread_bps > self.cfg.max_spread_bps:
            signal.accepted = False
            signal.reject_reason = "spread_wide"
            self._persist_signal(signal)
            return signal

        # Depth too thin
        if min_depth < self.cfg.min_depth_usdt:
            signal.accepted = False
            signal.reject_reason = "depth_thin"
            self._persist_signal(signal)
            return signal

        # Funding rate filter
        if self.cfg.use_funding_filter and abs(snap.funding_rate) >= self.cfg.funding_rate_threshold:
            if side == "LONG" and snap.funding_rate > self.cfg.funding_rate_threshold:
                signal.accepted = False
                signal.reject_reason = "high_funding_long"
                self._persist_signal(signal)
                return signal
            if side == "SHORT" and snap.funding_rate < -self.cfg.funding_rate_threshold:
                signal.accepted = False
                signal.reject_reason = "high_funding_short"
                self._persist_signal(signal)
                return signal

        # Regime filter (trend-follow / breakout)
        if self.cfg.use_regime_filter:
            if mode == "TREND_FOLLOW":
                if indicators.trend_direction == "UP" and side != "LONG":
                    signal.accepted = False
                    signal.reject_reason = "trend_follow_block"
                    self._persist_signal(signal)
                    return signal
                if indicators.trend_direction == "DOWN" and side != "SHORT":
                    signal.accepted = False
                    signal.reject_reason = "trend_follow_block"
                    self._persist_signal(signal)
                    return signal
            elif mode == "BREAKOUT_WATCH":
                if not breakout_confirmed:
                    signal.accepted = False
                    signal.reject_reason = "breakout_not_confirmed"
                    self._persist_signal(signal)
                    return signal
                if breakout_up and side != "LONG":
                    signal.accepted = False
                    signal.reject_reason = "breakout_mismatch"
                    self._persist_signal(signal)
                    return signal
                if breakout_down and side != "SHORT":
                    signal.accepted = False
                    signal.reject_reason = "breakout_mismatch"
                    self._persist_signal(signal)
                    return signal

        # Higher TF trend alignment
        if self.cfg.require_higher_tf_alignment and indicators.higher_tf_trend != "NEUTRAL":
            if side == "LONG" and indicators.higher_tf_trend != "UP":
                signal.accepted = False
                signal.reject_reason = "higher_tf_mismatch"
                self._persist_signal(signal)
                return signal
            if side == "SHORT" and indicators.higher_tf_trend != "DOWN":
                signal.accepted = False
                signal.reject_reason = "higher_tf_mismatch"
                self._persist_signal(signal)
                return signal

        # Trend filter: when we have exactly min confluence, don't trade against the trend
        if self.cfg.require_trend_not_against and confluence_score == effective_min_confluence:
            trend = indicators.trend_direction
            if side == "LONG" and trend == "DOWN":
                signal.accepted = False
                signal.reject_reason = "trend_against"
                self._persist_signal(signal)
                return signal
            if side == "SHORT" and trend == "UP":
                signal.accepted = False
                signal.reject_reason = "trend_against"
                self._persist_signal(signal)
                return signal

        # Pullback mode: only trade WITH the trend
        if self.cfg.trade_with_trend_only:
            trend = indicators.trend_direction
            if side == "LONG" and trend not in ("UP", "NEUTRAL"):
                signal.accepted = False
                signal.reject_reason = "trend_not_up"
                self._persist_signal(signal)
                return signal
            if side == "SHORT" and trend not in ("DOWN", "NEUTRAL"):
                signal.accepted = False
                signal.reject_reason = "trend_not_down"
                self._persist_signal(signal)
                return signal

        # Volume filter: skip very low volume (dead market)
        if self.cfg.min_volume_ratio > 0 and indicators.volume_ratio < self.cfg.min_volume_ratio:
            signal.accepted = False
            signal.reject_reason = "low_volume"
            self._persist_signal(signal)
            return signal

        # EMA-less entries must clear a stronger weighted-score floor.
        if (
            not require_ema
            and weighted_score < self.cfg.min_weighted_score_no_ema
        ):
            signal.accepted = False
            signal.reject_reason = (
                f"no_ema_score_{weighted_score:.0f}<{self.cfg.min_weighted_score_no_ema:.0f}"
            )
            self._persist_signal(signal)
            return signal

        # Momentum quality filter:
        # - EMA mode: enforce at minimum confluence
        # - EMA-less mode: enforce always
        if (
            self.cfg.require_momentum_confirmation
            and not momentum_confirmed
            and indicators.valid
            and ((not require_ema) or (confluence_score <= effective_min_confluence))
        ):
            signal.accepted = False
            signal.reject_reason = "no_momentum_no_ema" if not require_ema else "no_momentum"
            self._persist_signal(signal)
            return signal

        # TIGHT/ULTRA: require minimum weighted score
        if risk_state == "TIGHT":
            min_score = getattr(self.cfg, "risk_tight_min_weighted_score", 55.0)
            if weighted_score < min_score:
                signal.accepted = False
                signal.reject_reason = f"tight_score_{weighted_score:.0f}<{min_score}"
                self._persist_signal(signal)
                return signal
        elif risk_state == "ULTRA_TIGHT":
            min_score = getattr(self.cfg, "risk_ultra_min_weighted_score", 65.0)
            if weighted_score < min_score:
                signal.accepted = False
                signal.reject_reason = f"ultra_score_{weighted_score:.0f}<{min_score}"
                self._persist_signal(signal)
                return signal

        # All checks passed
        signal.accepted = True
        return signal

    def _compute_votes(self, snap: SymbolSnapshot) -> list[IndicatorVote]:
        """Compute indicator votes for the given snapshot."""
        votes: list[IndicatorVote] = []
        indicators = snap.indicators
        cfg = self.cfg

        # 1. EMA Z-Score vote
        z = snap.z_score_bps
        if abs(z) >= cfg.entry_threshold_bps:
            ema_side = "LONG" if z < 0 else "SHORT"
            votes.append(IndicatorVote(
                name="ema_zscore",
                side=ema_side,
                weight=cfg.weight_ema,
                value=z,
                reason=f"z={z:.1f}bps, threshold={cfg.entry_threshold_bps}",
            ))
        else:
            votes.append(IndicatorVote(
                name="ema_zscore", side="NEUTRAL", weight=0,
                value=z, reason="below threshold",
            ))

        # If indicators are not valid (not enough kline data), return with just EMA
        if not indicators.valid:
            return votes

        # 2. RSI vote
        rsi = indicators.rsi
        if rsi <= cfg.rsi_oversold:
            votes.append(IndicatorVote(
                name="rsi", side="LONG", weight=cfg.weight_rsi,
                value=rsi, reason=f"RSI={rsi:.1f} <= {cfg.rsi_oversold} (oversold)",
            ))
        elif rsi >= cfg.rsi_overbought:
            votes.append(IndicatorVote(
                name="rsi", side="SHORT", weight=cfg.weight_rsi,
                value=rsi, reason=f"RSI={rsi:.1f} >= {cfg.rsi_overbought} (overbought)",
            ))
        elif not getattr(cfg, "rsi_full_vote_only", False):
            mid_oversold = (cfg.rsi_oversold + 50) / 2
            mid_overbought = (cfg.rsi_overbought + 50) / 2
            if rsi < mid_oversold:
                votes.append(IndicatorVote(
                    name="rsi", side="LONG", weight=cfg.weight_rsi * 0.3,
                    value=rsi, reason=f"RSI={rsi:.1f} leaning oversold",
                ))
            elif rsi > mid_overbought:
                votes.append(IndicatorVote(
                    name="rsi", side="SHORT", weight=cfg.weight_rsi * 0.3,
                    value=rsi, reason=f"RSI={rsi:.1f} leaning overbought",
                ))
            else:
                votes.append(IndicatorVote(
                    name="rsi", side="NEUTRAL", weight=0,
                    value=rsi, reason="neutral zone",
                ))
        else:
            votes.append(IndicatorVote(
                name="rsi", side="NEUTRAL", weight=0,
                value=rsi, reason="RSI not in extreme zone (full vote only)",
            ))

        # 3. MACD vote (crossover + momentum)
        hist = indicators.macd_histogram
        prev_hist = indicators.macd_histogram_prev
        if hist > 0:
            if prev_hist <= 0:
                weight = cfg.weight_macd * cfg.macd_crossover_boost
                reason = "MACD bullish crossover"
            elif hist > prev_hist:
                weight = cfg.weight_macd
                reason = "MACD bullish momentum rising"
            else:
                weight = cfg.weight_macd * cfg.macd_weakening_multiplier
                reason = "MACD bullish but weakening"
            votes.append(IndicatorVote(
                name="macd", side="LONG", weight=weight,
                value=hist, reason=reason,
            ))
        elif hist < 0:
            if prev_hist >= 0:
                weight = cfg.weight_macd * cfg.macd_crossover_boost
                reason = "MACD bearish crossover"
            elif hist < prev_hist:
                weight = cfg.weight_macd
                reason = "MACD bearish momentum rising"
            else:
                weight = cfg.weight_macd * cfg.macd_weakening_multiplier
                reason = "MACD bearish but weakening"
            votes.append(IndicatorVote(
                name="macd", side="SHORT", weight=weight,
                value=hist, reason=reason,
            ))
        else:
            votes.append(IndicatorVote(
                name="macd", side="NEUTRAL", weight=0,
                value=hist, reason="flat",
            ))

        # 4. Bollinger Bands vote
        #
        # Detect the breakout FIRST. A band break confirmed by a volume spike
        # invalidates the mean-reversion reading of the same bar: at BB% >= 0.98
        # "price is at the upper band" (bollinger -> SHORT) and "price broke the
        # upper band on volume" (breakout -> LONG) are the same observation read
        # through opposite theories. They used to both fire, land in different
        # clusters, and cancel each other where the intra-cluster consensus check
        # could not see it. Measured on live data: ~15% of symbols at any moment.
        bb_pct = indicators.bollinger_pct
        breakout_side = ""
        if cfg.use_breakout_mode and indicators.volume_spike:
            if bb_pct >= cfg.breakout_bb_pct_high:
                breakout_side = "LONG"
            elif bb_pct <= cfg.breakout_bb_pct_low:
                breakout_side = "SHORT"

        if breakout_side:
            # Breakout owns this bar; the band is not a reversion level today.
            votes.append(IndicatorVote(
                name="bollinger", side="NEUTRAL", weight=0,
                value=bb_pct, reason="suppressed: band break confirmed by volume",
            ))
            votes.append(IndicatorVote(
                name="breakout", side=breakout_side, weight=cfg.weight_breakout,
                value=bb_pct,
                reason=f"BB breakout {'up' if breakout_side == 'LONG' else 'down'} + volume spike",
            ))
        elif bb_pct <= 0.15:
            votes.append(IndicatorVote(
                name="bollinger", side="LONG", weight=cfg.weight_bollinger,
                value=bb_pct, reason=f"BB%={bb_pct:.2f} (near lower band)",
            ))
        elif bb_pct >= 0.85:
            votes.append(IndicatorVote(
                name="bollinger", side="SHORT", weight=cfg.weight_bollinger,
                value=bb_pct, reason=f"BB%={bb_pct:.2f} (near upper band)",
            ))
        elif bb_pct <= 0.35:
            votes.append(IndicatorVote(
                name="bollinger", side="LONG", weight=cfg.weight_bollinger * 0.4,
                value=bb_pct, reason=f"BB%={bb_pct:.2f} (lower half)",
            ))
        elif bb_pct >= 0.65:
            votes.append(IndicatorVote(
                name="bollinger", side="SHORT", weight=cfg.weight_bollinger * 0.4,
                value=bb_pct, reason=f"BB%={bb_pct:.2f} (upper half)",
            ))
        else:
            votes.append(IndicatorVote(
                name="bollinger", side="NEUTRAL", weight=0,
                value=bb_pct, reason="mid range",
            ))

        # (the breakout vote is emitted together with bollinger above, so the two
        #  readings of the same bar can never contradict each other)

        # 5. Trend EMA vote
        td = indicators.trend_direction
        if td == "UP":
            votes.append(IndicatorVote(
                name="trend", side="LONG", weight=cfg.weight_trend,
                value=indicators.ema_trend,
                reason="price above EMA50 (uptrend)",
            ))
        elif td == "DOWN":
            votes.append(IndicatorVote(
                name="trend", side="SHORT", weight=cfg.weight_trend,
                value=indicators.ema_trend,
                reason="price below EMA50 (downtrend)",
            ))
        else:
            votes.append(IndicatorVote(
                name="trend", side="NEUTRAL", weight=0,
                value=indicators.ema_trend,
                reason="no clear trend",
            ))

        # 6. Orderbook imbalance vote
        if cfg.use_orderbook_vote:
            imb = snap.imbalance_ratio
            if imb >= cfg.orderbook_imbalance_long:
                votes.append(IndicatorVote(
                    name="orderbook", side="LONG", weight=cfg.weight_orderbook,
                    value=imb, reason=f"orderbook bid heavy {imb:.2f}",
                ))
            elif imb <= cfg.orderbook_imbalance_short:
                votes.append(IndicatorVote(
                    name="orderbook", side="SHORT", weight=cfg.weight_orderbook,
                    value=imb, reason=f"orderbook ask heavy {imb:.2f}",
                ))

        # 7. Volume spike vote (trend-aligned)
        if indicators.volume_spike and td in ("UP", "DOWN"):
            spike_side = "LONG" if td == "UP" else "SHORT"
            votes.append(IndicatorVote(
                name="volume_spike", side=spike_side, weight=cfg.weight_volume_spike,
                value=indicators.volume_ratio, reason="volume spike with trend",
            ))

        # 8. VWAP vote (mean-reversion support)
        vwap_dev = indicators.vwap_deviation_bps
        if indicators.vwap > 0 and abs(vwap_dev) >= cfg.entry_threshold_bps:
            if vwap_dev < 0:  # price below VWAP → undervalued → LONG
                votes.append(IndicatorVote(
                    name="vwap", side="LONG", weight=cfg.weight_vwap,
                    value=vwap_dev, reason=f"price {abs(vwap_dev):.0f}bps below VWAP",
                ))
            else:  # price above VWAP → overvalued → SHORT
                votes.append(IndicatorVote(
                    name="vwap", side="SHORT", weight=cfg.weight_vwap,
                    value=vwap_dev, reason=f"price {vwap_dev:.0f}bps above VWAP",
                ))

        # 9. Momentum confirmation vote (MACD histogram strengthening)
        if indicators.macd_strengthening:
            if hist > 0:
                votes.append(IndicatorVote(
                    name="momentum", side="LONG", weight=cfg.weight_momentum,
                    value=indicators.macd_hist_slope,
                    reason="momentum building (bullish)",
                ))
            elif hist < 0:
                votes.append(IndicatorVote(
                    name="momentum", side="SHORT", weight=cfg.weight_momentum,
                    value=indicators.macd_hist_slope,
                    reason="momentum building (bearish)",
                ))

        # 10. RSI Divergence vote (strong reversal signal)
        if indicators.rsi_bullish_divergence:
            votes.append(IndicatorVote(
                name="rsi_divergence", side="LONG", weight=cfg.weight_rsi * 1.5,
                value=indicators.rsi,
                reason="bullish RSI divergence (price lower low, RSI higher low)",
            ))
        elif indicators.rsi_bearish_divergence:
            votes.append(IndicatorVote(
                name="rsi_divergence", side="SHORT", weight=cfg.weight_rsi * 1.5,
                value=indicators.rsi,
                reason="bearish RSI divergence (price higher high, RSI lower high)",
            ))

        # 11. Stochastic RSI vote (more sensitive than RSI for extremes)
        stoch_k = indicators.stoch_rsi_k
        stoch_d = indicators.stoch_rsi_d
        if stoch_k <= cfg.stoch_rsi_oversold and stoch_d <= cfg.stoch_rsi_oversold:
            votes.append(IndicatorVote(
                name="stoch_rsi", side="LONG", weight=cfg.weight_stoch_rsi,
                value=stoch_k,
                reason=f"StochRSI K={stoch_k:.0f} D={stoch_d:.0f} (oversold)",
            ))
        elif stoch_k >= cfg.stoch_rsi_overbought and stoch_d >= cfg.stoch_rsi_overbought:
            votes.append(IndicatorVote(
                name="stoch_rsi", side="SHORT", weight=cfg.weight_stoch_rsi,
                value=stoch_k,
                reason=f"StochRSI K={stoch_k:.0f} D={stoch_d:.0f} (overbought)",
            ))
        # StochRSI crossover: %K crosses %D from below in oversold zone → LONG
        elif stoch_k < 35 and stoch_k > stoch_d and stoch_d < 30:
            votes.append(IndicatorVote(
                name="stoch_rsi", side="LONG", weight=cfg.weight_stoch_rsi * 0.5,
                value=stoch_k,
                reason=f"StochRSI bullish cross K={stoch_k:.0f}>D={stoch_d:.0f}",
            ))
        elif stoch_k > 65 and stoch_k < stoch_d and stoch_d > 70:
            votes.append(IndicatorVote(
                name="stoch_rsi", side="SHORT", weight=cfg.weight_stoch_rsi * 0.5,
                value=stoch_k,
                reason=f"StochRSI bearish cross K={stoch_k:.0f}<D={stoch_d:.0f}",
            ))

        # 12. ADX strength vote (strong trend in signal direction)
        if indicators.adx >= cfg.adx_trend_threshold:
            if indicators.plus_di > indicators.minus_di:
                votes.append(IndicatorVote(
                    name="adx", side="LONG", weight=cfg.weight_adx,
                    value=indicators.adx,
                    reason=f"ADX={indicators.adx:.0f} +DI>{indicators.minus_di:.0f} (strong uptrend)",
                ))
            elif indicators.minus_di > indicators.plus_di:
                votes.append(IndicatorVote(
                    name="adx", side="SHORT", weight=cfg.weight_adx,
                    value=indicators.adx,
                    reason=f"ADX={indicators.adx:.0f} -DI>{indicators.plus_di:.0f} (strong downtrend)",
                ))

        # 13. Taker Buy/Sell Ratio vote (net buying/selling pressure)
        taker = indicators.taker_buy_ratio
        if taker >= cfg.taker_buy_ratio_long:
            votes.append(IndicatorVote(
                name="taker_ratio", side="LONG", weight=cfg.weight_taker_ratio,
                value=taker,
                reason=f"taker buy ratio {taker:.2f} >= {cfg.taker_buy_ratio_long} (buy pressure)",
            ))
        elif taker <= cfg.taker_buy_ratio_short:
            votes.append(IndicatorVote(
                name="taker_ratio", side="SHORT", weight=cfg.weight_taker_ratio,
                value=taker,
                reason=f"taker buy ratio {taker:.2f} <= {cfg.taker_buy_ratio_short} (sell pressure)",
            ))

        # 14. OBV vote (volume flow direction + divergence)
        if indicators.obv_divergence_bullish:
            # Strong reversal signal: price lower low but OBV higher low
            votes.append(IndicatorVote(
                name="obv", side="LONG", weight=cfg.weight_obv * 1.5,
                value=indicators.obv_slope,
                reason="OBV bullish divergence (accumulation despite price drop)",
            ))
        elif indicators.obv_divergence_bearish:
            votes.append(IndicatorVote(
                name="obv", side="SHORT", weight=cfg.weight_obv * 1.5,
                value=indicators.obv_slope,
                reason="OBV bearish divergence (distribution despite price rise)",
            ))
        elif indicators.obv_slope > 0.5:
            votes.append(IndicatorVote(
                name="obv", side="LONG", weight=cfg.weight_obv,
                value=indicators.obv_slope,
                reason=f"OBV rising (slope={indicators.obv_slope:.2f}, accumulation)",
            ))
        elif indicators.obv_slope < -0.5:
            votes.append(IndicatorVote(
                name="obv", side="SHORT", weight=cfg.weight_obv,
                value=indicators.obv_slope,
                reason=f"OBV falling (slope={indicators.obv_slope:.2f}, distribution)",
            ))

        # 15. Williams %R vote (short-term overbought/oversold + partial zones)
        wr = indicators.williams_r
        wr_partial = cfg.weight_williams_r * 0.5  # half weight for transition zones
        if wr <= cfg.williams_r_oversold:
            votes.append(IndicatorVote(
                name="williams_r", side="LONG", weight=cfg.weight_williams_r,
                value=wr,
                reason=f"Williams %R={wr:.0f} <= {cfg.williams_r_oversold} (oversold)",
            ))
        elif wr <= cfg.williams_r_oversold + 15:  # transition zone (e.g. -80 to -65)
            votes.append(IndicatorVote(
                name="williams_r", side="LONG", weight=wr_partial,
                value=wr,
                reason=f"Williams %R={wr:.0f} near oversold (partial)",
            ))
        elif wr >= cfg.williams_r_overbought:
            votes.append(IndicatorVote(
                name="williams_r", side="SHORT", weight=cfg.weight_williams_r,
                value=wr,
                reason=f"Williams %R={wr:.0f} >= {cfg.williams_r_overbought} (overbought)",
            ))
        elif wr >= cfg.williams_r_overbought - 15:  # transition zone (e.g. -35 to -20)
            votes.append(IndicatorVote(
                name="williams_r", side="SHORT", weight=wr_partial,
                value=wr,
                reason=f"Williams %R={wr:.0f} near overbought (partial)",
            ))

        # 16. TTM Squeeze vote (BB inside KC = breakout imminent)
        if indicators.squeeze_on:
            # Squeeze detected: vote based on momentum direction (velocity)
            vel = indicators.price_velocity_bps
            if vel > cfg.velocity_threshold_bps:
                votes.append(IndicatorVote(
                    name="squeeze", side="LONG", weight=cfg.weight_squeeze,
                    value=vel,
                    reason=f"TTM Squeeze ON + bullish velocity {vel:.1f}bps/bar",
                ))
            elif vel < -cfg.velocity_threshold_bps:
                votes.append(IndicatorVote(
                    name="squeeze", side="SHORT", weight=cfg.weight_squeeze,
                    value=vel,
                    reason=f"TTM Squeeze ON + bearish velocity {vel:.1f}bps/bar",
                ))
            else:
                # Squeeze but no clear direction yet - use MACD histogram direction
                if indicators.macd_histogram > 0:
                    votes.append(IndicatorVote(
                        name="squeeze", side="LONG", weight=cfg.weight_squeeze * 0.5,
                        value=indicators.macd_histogram,
                        reason="TTM Squeeze ON + MACD positive (lean bullish)",
                    ))
                elif indicators.macd_histogram < 0:
                    votes.append(IndicatorVote(
                        name="squeeze", side="SHORT", weight=cfg.weight_squeeze * 0.5,
                        value=indicators.macd_histogram,
                        reason="TTM Squeeze ON + MACD negative (lean bearish)",
                    ))

        # 17. Open Interest vote (manipulation detection)
        oi_change = indicators.oi_change_pct
        if abs(oi_change) >= cfg.oi_change_threshold_pct:
            # OI rising + price rising = genuine rally (LONG confirmed)
            # OI rising + price falling = shorts opening (bearish) or long trap
            # OI falling + price rising = short squeeze (caution)
            # OI falling + price falling = longs closing (bearish confirmed)
            vel = indicators.price_velocity_bps
            if oi_change > 0 and vel > 0:
                # New money entering + price up = genuine bullish
                votes.append(IndicatorVote(
                    name="open_interest", side="LONG", weight=cfg.weight_oi,
                    value=oi_change,
                    reason=f"OI +{oi_change:.1f}% + price rising (genuine rally)",
                ))
            elif oi_change > 0 and vel < 0:
                # New money entering + price down = bearish pressure building
                votes.append(IndicatorVote(
                    name="open_interest", side="SHORT", weight=cfg.weight_oi,
                    value=oi_change,
                    reason=f"OI +{oi_change:.1f}% + price falling (shorts entering)",
                ))
            elif oi_change < 0 and vel < 0:
                # Positions closing + price down = longs liquidating (bearish)
                votes.append(IndicatorVote(
                    name="open_interest", side="SHORT", weight=cfg.weight_oi * 0.7,
                    value=oi_change,
                    reason=f"OI {oi_change:.1f}% + price falling (longs exiting)",
                ))
            elif oi_change < 0 and vel > 0:
                # Positions closing + price up = short squeeze (bullish but caution)
                votes.append(IndicatorVote(
                    name="open_interest", side="LONG", weight=cfg.weight_oi * 0.5,
                    value=oi_change,
                    reason=f"OI {oi_change:.1f}% + price rising (short squeeze)",
                ))

        # 18. Price Velocity vote (momentum confirmation / pump-dump detection)
        vel = indicators.price_velocity_bps
        if abs(vel) >= cfg.velocity_threshold_bps:
            accel = indicators.price_acceleration
            if vel > 0 and accel >= 0:
                # Rising price + accelerating = strong bullish momentum
                votes.append(IndicatorVote(
                    name="velocity", side="LONG", weight=cfg.weight_velocity,
                    value=vel,
                    reason=f"velocity +{vel:.1f}bps/bar accel={accel:.1f} (bullish momentum)",
                ))
            elif vel < 0 and accel <= 0:
                # Falling price + accelerating down = strong bearish momentum
                votes.append(IndicatorVote(
                    name="velocity", side="SHORT", weight=cfg.weight_velocity,
                    value=vel,
                    reason=f"velocity {vel:.1f}bps/bar accel={accel:.1f} (bearish momentum)",
                ))
            # Deceleration = potential reversal, don't vote (let other indicators decide)

        # 19. Whale Detection vote (large orders in orderbook)
        whale_imb = snap.whale_imbalance
        whale_total = snap.whale_bid_usdt + snap.whale_ask_usdt
        if whale_total > 0:  # whales detected
            if whale_imb >= cfg.whale_imbalance_threshold:
                votes.append(IndicatorVote(
                    name="whale", side="LONG", weight=cfg.weight_whale,
                    value=whale_imb,
                    reason=f"whale bid wall (imbalance={whale_imb:.2f}, bid={snap.whale_bid_usdt:.0f}$)",
                ))
            elif whale_imb <= (1.0 - cfg.whale_imbalance_threshold):
                votes.append(IndicatorVote(
                    name="whale", side="SHORT", weight=cfg.weight_whale,
                    value=whale_imb,
                    reason=f"whale ask wall (imbalance={whale_imb:.2f}, ask={snap.whale_ask_usdt:.0f}$)",
                ))

        # 20. Liquidation Cascade Detection vote
        if (indicators.liq_cascade_signal != "NONE"
                and indicators.liq_cascade_intensity >= cfg.liq_cascade_min_intensity):
            intensity = indicators.liq_cascade_intensity
            if indicators.liq_cascade_signal == "LONG_LIQ":
                # Long liquidation cascade = more downside pressure → SHORT
                votes.append(IndicatorVote(
                    name="liq_cascade", side="SHORT", weight=cfg.weight_liq_cascade * intensity,
                    value=indicators.oi_change_pct,
                    reason=f"long liquidation cascade (OI {indicators.oi_change_pct:.1f}%, intensity={intensity:.2f})",
                ))
            elif indicators.liq_cascade_signal == "SHORT_SQUEEZE":
                # Short squeeze = upside pressure → LONG
                votes.append(IndicatorVote(
                    name="liq_cascade", side="LONG", weight=cfg.weight_liq_cascade * intensity,
                    value=indicators.oi_change_pct,
                    reason=f"short squeeze (OI {indicators.oi_change_pct:.1f}%, intensity={intensity:.2f})",
                ))

        # 21. Volume Profile vote (POC as support/resistance)
        poc = indicators.volume_profile_poc
        if poc > 0 and indicators.price_vs_poc_bps != 0:
            dist = abs(indicators.price_vs_poc_bps)
            if dist < 50:  # price near POC (within 50 bps = ~0.5%)
                if indicators.price_in_value_area:
                    # Inside value area near POC → mean reversion toward POC
                    if indicators.price_vs_poc_bps > 0:
                        votes.append(IndicatorVote(
                            name="volume_profile", side="SHORT",
                            weight=cfg.weight_volume_profile * 0.7,
                            value=indicators.price_vs_poc_bps,
                            reason=f"price above POC by {indicators.price_vs_poc_bps:.0f}bps (revert to POC)",
                        ))
                    else:
                        votes.append(IndicatorVote(
                            name="volume_profile", side="LONG",
                            weight=cfg.weight_volume_profile * 0.7,
                            value=indicators.price_vs_poc_bps,
                            reason=f"price below POC by {indicators.price_vs_poc_bps:.0f}bps (revert to POC)",
                        ))
            elif not indicators.price_in_value_area:
                # Outside the value area this is a BREAKOUT read, not a reversion
                # one. It is emitted under a separate name so it lands in the
                # volatility cluster: the same indicator used to switch between
                # "revert to POC" and "value area broke, follow it" while always
                # counting as mean-reversion evidence.
                if indicators.price_vs_poc_bps > 100:
                    votes.append(IndicatorVote(
                        name="volume_profile_break", side="LONG",
                        weight=cfg.weight_volume_profile,
                        value=indicators.price_vs_poc_bps,
                        reason=f"price broke above value area (+{indicators.price_vs_poc_bps:.0f}bps)",
                    ))
                elif indicators.price_vs_poc_bps < -100:
                    votes.append(IndicatorVote(
                        name="volume_profile_break", side="SHORT",
                        weight=cfg.weight_volume_profile,
                        value=indicators.price_vs_poc_bps,
                        reason=f"price broke below value area ({indicators.price_vs_poc_bps:.0f}bps)",
                    ))

        # 22. Composite Sentiment vote
        sentiment = indicators.composite_sentiment
        if abs(sentiment) >= cfg.sentiment_threshold:
            side = "LONG" if sentiment > 0 else "SHORT"
            # Scale weight by how extreme the sentiment is (20→100 maps to 0.5→1.0)
            intensity = min(1.0, abs(sentiment) / 80.0 + 0.25)
            votes.append(IndicatorVote(
                name="sentiment", side=side,
                weight=cfg.weight_sentiment * intensity,
                value=sentiment,
                reason=f"composite sentiment {sentiment:+.1f} (F&G={indicators.fear_greed_index:.0f})",
            ))

        return votes

    def _compute_cluster_votes(
        self, votes: list[IndicatorVote],
    ) -> tuple[list[ClusterVote], float]:
        """Group individual indicator votes into clusters.

        A cluster votes for a side when at least 2 members agree (or 1/1 for
        single-member clusters like vwap).  The contrarian cluster doesn't
        produce a vote — it produces a bonus/penalty modifier instead.

        Returns (cluster_votes, contrarian_bonus_weight).
        contrarian_bonus_weight is positive for LONG-leaning, negative for SHORT.
        """
        # Index individual votes by name for quick lookup
        vote_map: dict[str, IndicatorVote] = {}
        for v in votes:
            # Some indicators might not appear (didn't trigger) — that's fine
            vote_map[v.name] = v

        cluster_votes: list[ClusterVote] = []
        contrarian_bonus = 0.0

        for cluster_name, members in CLUSTER_MEMBERS.items():
            # Gather non-neutral votes from cluster members
            member_votes = []
            for m in members:
                v = vote_map.get(m)
                if v and v.side != "NEUTRAL":
                    member_votes.append(v)

            if not member_votes:
                continue

            # Filter out very weak partial votes (< 50% of their base weight)
            # so a "leaning oversold" RSI at 0.3x weight can't activate a cluster alone
            strong_member_votes = []
            for mv in member_votes:
                attr = self._INDICATOR_WEIGHT_ATTRS.get(mv.name)
                base_w = getattr(self.cfg, attr, 0.0) if attr else mv.weight
                # Accept if weight >= 40% of base (allows crossover boost, half-weight zones)
                if base_w > 0 and mv.weight >= base_w * 0.4:
                    strong_member_votes.append(mv)

            if not strong_member_votes:
                continue

            # Count votes per side
            long_members = [v for v in strong_member_votes if v.side == "LONG"]
            short_members = [v for v in strong_member_votes if v.side == "SHORT"]

            # Determine consensus: need 2+ agreeing, or 1/1 if only 1 voted
            total_active = len(strong_member_votes)
            min_agree = 2 if total_active >= 2 else 1

            winning_side = "NEUTRAL"
            winning_members: list[IndicatorVote] = []

            if len(long_members) >= min_agree and len(long_members) > len(short_members):
                winning_side = "LONG"
                winning_members = long_members
            elif len(short_members) >= min_agree and len(short_members) > len(long_members):
                winning_side = "SHORT"
                winning_members = short_members

            if winning_side == "NEUTRAL":
                continue

            # For contrarian cluster: accumulate as bonus, don't create a vote
            if cluster_name in BONUS_ONLY_CLUSTERS:
                bonus_w = sum(v.weight for v in winning_members)
                contrarian_bonus += bonus_w if winning_side == "LONG" else -bonus_w
                continue

            # Cluster weight = sum of agreeing member weights
            cluster_weight = sum(v.weight for v in winning_members)
            member_names = [v.name for v in winning_members]

            cluster_votes.append(ClusterVote(
                name=cluster_name,
                side=winning_side,
                weight=cluster_weight,
                member_votes=winning_members,
                agreement=len(winning_members),
                total_members=total_active,
                reason=f"{cluster_name}: {len(winning_members)}/{total_active} agree ({', '.join(member_names)})",
            ))

        return cluster_votes, contrarian_bonus

    def _resolve_by_thesis(
        self, cluster_votes: list[ClusterVote],
    ) -> tuple[str, int, float] | None:
        """Decide direction from ONE cluster; everything else confirms or vetoes.

        Returns ``(side, agreeing_cluster_count, agreeing_weight)`` or ``None``.

        This replaces majority voting across clusters. Voting silently assumes
        its voters estimate the same quantity, so averaging cancels their noise.
        Mean reversion and trend following do not: they are different models of
        price, and averaging them yields a direction that no component actually
        holds. Worse, when the blend loses there is no way to tell WHICH model
        was wrong, so nothing can be improved — which is exactly what happened.

        Under this rule the primary thesis owns the direction. Other clusters can
        only reduce the number of trades, never flip one, so a losing run is
        attributable to a single, falsifiable claim about the market.
        """
        voting = [c for c in cluster_votes if c.name not in BONUS_ONLY_CLUSTERS]
        thesis = next((c for c in voting if c.name == self.cfg.primary_thesis), None)

        # No opinion from the thesis cluster means no trade — the other clusters
        # are not allowed to invent a direction on their own.
        if thesis is None or thesis.side == "NEUTRAL":
            self._gate_stats["thesis_silent"] += 1
            return None

        side = thesis.side
        others = [c for c in voting if c.name != thesis.name]
        confirmations = [c for c in others if c.side == side]
        vetoes = [c for c in others if c.side not in (side, "NEUTRAL")]

        if len(confirmations) < self.cfg.thesis_min_confirmations:
            self._gate_stats["too_few_confirmations"] += 1
            return None
        if len(vetoes) > self.cfg.thesis_max_vetoes:
            self._gate_stats["vetoed"] += 1
            return None

        agreeing = [thesis, *confirmations]
        return side, len(agreeing), sum(c.weight for c in agreeing)

    def _check_momentum(self, indicators: Indicators, side: str) -> bool:
        """Check if MACD momentum is building in the signal direction."""
        if not indicators.valid:
            return True  # no data → don't block
        if not indicators.macd_strengthening:
            return False
        # Momentum must align with signal direction
        if side == "LONG" and indicators.macd_histogram > 0:
            return True
        if side == "SHORT" and indicators.macd_histogram < 0:
            return True
        # Also accept if histogram just crossed zero in signal direction
        if side == "LONG" and indicators.macd_histogram_prev <= 0 < indicators.macd_histogram:
            return True
        if side == "SHORT" and indicators.macd_histogram_prev >= 0 > indicators.macd_histogram:
            return True
        return False

    # ── Max weight lookup for normalization ──────────────────────
    # Maps indicator name → config attribute holding its max weight.
    # Indicators that use multipliers (e.g. 1.5x for divergence, crossover
    # boost for MACD) are capped at the boosted maximum so normalization
    # reflects the true ceiling of what a single indicator can contribute.
    _INDICATOR_WEIGHT_ATTRS: dict[str, str] = {
        "ema_zscore":      "weight_ema",
        "rsi":             "weight_rsi",
        "macd":            "weight_macd",       # can be boosted by macd_crossover_boost
        "bollinger":       "weight_bollinger",
        "breakout":        "weight_breakout",
        "trend":           "weight_trend",
        "orderbook":       "weight_orderbook",
        "volume_spike":    "weight_volume_spike",
        "vwap":            "weight_vwap",
        "momentum":        "weight_momentum",
        "rsi_divergence":  "weight_rsi",        # uses weight_rsi * 1.5
        "stoch_rsi":       "weight_stoch_rsi",
        "adx":             "weight_adx",
        "taker_ratio":     "weight_taker_ratio",
        "obv":             "weight_obv",         # can be 1.5x on divergence
        "williams_r":      "weight_williams_r",
        "squeeze":         "weight_squeeze",
        "open_interest":   "weight_oi",
        "velocity":        "weight_velocity",
        "whale":           "weight_whale",
        "liq_cascade":     "weight_liq_cascade",
        "volume_profile":  "weight_volume_profile",
        "volume_profile_break": "weight_volume_profile",
        "sentiment":       "weight_sentiment",
    }

    # Multiplier caps – the highest multiplier each indicator can apply.
    _INDICATOR_MAX_MULT: dict[str, float] = {
        "macd":           1.5,   # macd_crossover_boost default
        "rsi_divergence": 1.5,   # hardcoded *1.5
        "obv":            1.5,   # divergence path *1.5
    }

    def _normalize_weighted_score(
        self, votes: list[IndicatorVote], side: str,
    ) -> tuple[float, float]:
        """Normalize the weighted score to a 0-100 agreement measure.

        Returns (normalized_score, raw_winning_weight).

            score = (winning weight - opposing weight) / max weight of every
                    indicator that voted EITHER WAY  x 100

        The denominator previously covered only the WINNING side, which made the
        metric non-monotonic in the amount of evidence: a signal backed by two
        full-weight indicators scored 81.8 while one backed by eight indicators
        (three at partial weight) scored 77.7. Because leverage tiers and the
        high-conviction margin multiplier keyed off this number, the bot put its
        LARGEST positions and HIGHEST leverage behind its THINNEST evidence.

        Counting opposing votes in both the numerator and the denominator makes
        the score strictly decrease when a contradicting indicator appears, so it
        now measures agreement quality rather than rewarding sparsity.

        Breadth is enforced separately by ``min_cluster_confluence`` (how many
        independent clusters must agree), which keeps the two concerns apart.
        """
        opposite = "SHORT" if side == "LONG" else "LONG"
        side_votes = [v for v in votes if v.side == side]
        opp_votes = [v for v in votes if v.side == opposite]
        if not side_votes:
            return 0.0, 0.0

        raw_win = sum(v.weight for v in side_votes)
        raw_lose = sum(v.weight for v in opp_votes)

        # Max possible across every indicator that participated, either side.
        max_possible = 0.0
        seen: set[str] = set()
        for v in side_votes + opp_votes:
            if v.name in seen:
                continue
            seen.add(v.name)
            attr = self._INDICATOR_WEIGHT_ATTRS.get(v.name)
            if attr:
                base = getattr(self.cfg, attr, 0.0)
                mult = self._INDICATOR_MAX_MULT.get(v.name, 1.0)
                max_possible += base * mult
            else:
                # Unknown indicator – use actual weight as ceiling
                max_possible += v.weight

        if max_possible <= 0:
            return 0.0, raw_win

        normalized = ((raw_win - raw_lose) / max_possible) * 100.0
        return max(0.0, min(normalized, 100.0)), raw_win

    def _compute_extremity_bonus(self, indicators: Indicators, side: str) -> float:
        """Bonus weight for extreme indicator values (higher conviction)."""
        if not indicators.valid:
            return 0.0
        bonus = 0.0

        # RSI extremity bonus: RSI < 15 or > 85 gets extra credit
        rsi = indicators.rsi
        if side == "LONG" and rsi < 20:
            bonus += (20 - rsi) * 0.3  # up to 6 pts at RSI=0
        elif side == "SHORT" and rsi > 80:
            bonus += (rsi - 80) * 0.3

        # ADX strength bonus: strong trend in signal direction
        if indicators.adx > self.cfg.adx_strong_threshold:
            if side == "LONG" and indicators.plus_di > indicators.minus_di:
                bonus += 3.0
            elif side == "SHORT" and indicators.minus_di > indicators.plus_di:
                bonus += 3.0

        return bonus

    def _determine_mode(self, indicators: Indicators) -> str:
        """Determine strategy mode based on volatility and trend strength."""
        if not self.cfg.use_regime_filter:
            return "MEAN_REVERSION"

        # Bollinger squeeze -> breakout watch
        bb_width = 0.0
        if indicators.bollinger_mid > 0:
            bb_width = (indicators.bollinger_upper - indicators.bollinger_lower) / indicators.bollinger_mid
        if self.cfg.use_breakout_mode and 0 < bb_width < self.cfg.bb_squeeze_threshold:
            return "BREAKOUT_WATCH"

        # ADX + trend direction -> trend-follow
        if indicators.adx >= self.cfg.adx_trend_threshold and indicators.trend_direction != "NEUTRAL":
            return "TREND_FOLLOW"

        return "MEAN_REVERSION"

    def set_cooldown(self, symbol: str) -> None:
        """Set cooldown after a trade."""
        self._cooldowns[symbol] = datetime.now(timezone.utc)

    def _in_cooldown(self, symbol: str, now: datetime) -> bool:
        last = self._cooldowns.get(symbol)
        if last is None:
            return False
        elapsed_min = (now - last).total_seconds() / 60
        return elapsed_min < self.cfg.cooldown_minutes

    def _get_min_confluence(self, risk_state: str) -> int:
        """How many CLUSTERS must agree — the gate actually applied in
        ``_evaluate_snapshot``.

        This used to return ``min_confluence_score`` (an individual-indicator
        count) while the evaluator read ``min_cluster_confluence`` directly, so
        the value logged as "min_confluence" never matched the rule in force.

        Risk states now RAISE the bar instead of leaving it flat: when the bot is
        losing, it should demand more agreement, not the same amount.
        """
        voting_clusters = len(CLUSTER_MEMBERS) - len(BONUS_ONLY_CLUSTERS)
        base = self.cfg.min_cluster_confluence
        if risk_state == "TIGHT":
            return min(base + 1, voting_clusters)
        if risk_state == "ULTRA_TIGHT":
            return min(base + 2, voting_clusters)
        return base

    def _require_ema_for_mode(self, mode: str) -> bool:
        """Resolve EMA requirement with mode-aware overrides and legacy fallback."""
        legacy = getattr(self.cfg, "require_ema_in_confluence", False)
        if mode == "TREND_FOLLOW":
            override = getattr(self.cfg, "require_ema_in_trend_follow", None)
        elif mode == "BREAKOUT_WATCH":
            override = getattr(self.cfg, "require_ema_in_breakout", None)
        else:
            override = getattr(self.cfg, "require_ema_in_mean_reversion", None)
        return legacy if override is None else bool(override)

    def _get_max_positions(self, risk_state: str) -> int:
        base = self.cfg.max_open_positions
        if risk_state == "TIGHT":
            return max(2, base - 1)
        elif risk_state == "ULTRA_TIGHT":
            return max(2, base - 2)
        return base

    def generate_swing_signals(
        self,
        snapshots: list[SymbolSnapshot],
        open_positions: list[dict[str, Any]],
        risk_state: str = "NORMAL",
    ) -> list[Signal]:
        """Generate swing signals from 1h kline snapshots.

        Reuses the same indicator voting system but with swing-specific filters:
        - Separate max position count (swing_max_positions)
        - 4h trend alignment required
        - Longer cooldowns
        - Higher min confluence
        """
        now = datetime.now(timezone.utc)
        cfg = self.cfg

        # Count existing swing positions separately
        swing_positions = [p for p in open_positions if p.get("trade_type") == "swing"]
        open_swing_symbols = {p["symbol"] for p in swing_positions}
        all_open_symbols = {p["symbol"] for p in open_positions}

        if len(swing_positions) >= cfg.swing_max_positions:
            return []

        signals: list[Signal] = []
        swing_count = len(swing_positions)

        for snap in snapshots:
            if snap.symbol in all_open_symbols:
                continue
            if swing_count >= cfg.swing_max_positions:
                break

            # Check swing cooldown (separate from scalp)
            last_cd = self._cooldowns.get(f"swing_{snap.symbol}")
            if last_cd:
                elapsed = (now - last_cd).total_seconds() / 60
                if elapsed < cfg.swing_cooldown_minutes:
                    continue

            indicators = snap.indicators
            if not indicators.valid:
                continue

            votes = self._compute_votes(snap)
            cluster_votes_swing, contra_bonus = self._compute_cluster_votes(votes)

            if cfg.signal_mode == "thesis":
                # Same single-thesis rule as the scalp path. Left on majority
                # voting, the swing leg would quietly reintroduce the very
                # contradiction the scalp leg just removed.
                resolved = self._resolve_by_thesis(cluster_votes_swing)
                if resolved is None:
                    continue
                side, confluence, _agreeing_weight = resolved
            else:
                long_clusters = [c for c in cluster_votes_swing if c.side == "LONG"]
                short_clusters = [c for c in cluster_votes_swing if c.side == "SHORT"]
                swing_min = cfg.swing_min_confluence
                if len(long_clusters) >= swing_min and len(long_clusters) > len(short_clusters):
                    side = "LONG"
                    confluence = len(long_clusters)
                elif len(short_clusters) >= swing_min and len(short_clusters) > len(long_clusters):
                    side = "SHORT"
                    confluence = len(short_clusters)
                else:
                    continue

            # Normalize swing weighted score to 0-100
            weighted, _raw = self._normalize_weighted_score(votes, side)
            weighted = min(weighted, 100.0)

            # 4h trend must align
            if cfg.swing_require_trend_alignment and indicators.higher_tf_trend != "NEUTRAL":
                if side == "LONG" and indicators.higher_tf_trend != "UP":
                    continue
                if side == "SHORT" and indicators.higher_tf_trend != "DOWN":
                    continue

            # Spread/depth checks
            min_depth = min(snap.bid_depth_usdt, snap.ask_depth_usdt)
            if snap.spread_bps > cfg.max_spread_bps or min_depth < cfg.min_depth_usdt:
                continue

            signal = Signal(
                symbol=snap.symbol,
                side=side,
                z_score_bps=snap.z_score_bps,
                mid_price=snap.mid_price,
                fast_ema=snap.fast_ema,
                slow_ema=snap.slow_ema,
                spread_bps=snap.spread_bps,
                depth_usdt=min_depth,
                confluence_score=confluence,
                weighted_score=weighted,
                indicator_votes=votes,
                rsi=indicators.rsi,
                macd_histogram=indicators.macd_histogram,
                macd_histogram_prev=indicators.macd_histogram_prev,
                bollinger_pct=indicators.bollinger_pct,
                trend_direction=indicators.trend_direction,
                higher_tf_trend=indicators.higher_tf_trend,
                atr=indicators.atr,
                adx=indicators.adx,
                plus_di=indicators.plus_di,
                minus_di=indicators.minus_di,
                funding_rate=snap.funding_rate,
                volume_ratio=indicators.volume_ratio,
                volume_spike=indicators.volume_spike,
                mode="SWING",
                trade_type="swing",
            )
            signal.accepted = True
            signals.append(signal)
            self._persist_signal(signal)
            swing_count += 1

        log.info(
            "swing signal generation",
            extra={"candidates": len(snapshots), "accepted": len(signals)},
        )
        return signals

    def set_swing_cooldown(self, symbol: str) -> None:
        """Set cooldown for swing trade."""
        self._cooldowns[f"swing_{symbol}"] = datetime.now(timezone.utc)

    def _persist_signal(self, signal: Signal) -> None:
        self.db.insert(
            "signals",
            {
                "ts": signal.ts,
                "symbol": signal.symbol,
                "side": signal.side,
                "z_score_bps": signal.z_score_bps,
                "mid_price": signal.mid_price,
                "fast_ema": signal.fast_ema,
                "slow_ema": signal.slow_ema,
                "spread_bps": signal.spread_bps,
                "depth_usdt": signal.depth_usdt,
                "accepted": 1 if signal.accepted else 0,
                "reject_reason": signal.reject_reason,
            },
        )
