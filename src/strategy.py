"""Multi-indicator confluence strategy – optimized for maximum profitability.

Signal generation uses 22 independent indicators that each "vote" for LONG, SHORT, or NEUTRAL.
A trade is only taken when enough indicators agree (confluence).

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
    session: str = ""  # trading session name
    session_size_mult: float = 1.0  # session-based size multiplier

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
        self._symbol_results: dict[str, list[float]] = {}  # symbol → recent PnL list
        self._adaptive_score_cache: float | None = None
        self._adaptive_score_ts: datetime | None = None

    def generate_signals(
        self,
        snapshots: list[SymbolSnapshot],
        open_positions: list[dict[str, Any]],
        risk_state: str = "NORMAL",
    ) -> list[Signal]:
        """Score all snapshots and return accepted signals."""
        now = datetime.now(timezone.utc)
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
            if signal:
                signals.append(signal)
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
                "min_confluence": min_confluence,
                **({"reject_reasons": reject_counts} if reject_counts else {}),
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
        # Skip extreme z-score (breakout, not mean reversion)
        if abs(z) > self.cfg.max_z_score_bps:
            return None

        # ── Compute indicator votes ───────────────────────────
        indicators = snap.indicators
        mode = self._determine_mode(indicators)
        votes = self._compute_votes(snap)

        # Count agreeing indicators per side
        long_votes = [v for v in votes if v.side == "LONG"]
        short_votes = [v for v in votes if v.side == "SHORT"]
        long_score = len(long_votes)
        short_score = len(short_votes)
        long_weighted = sum(v.weight for v in long_votes)
        short_weighted = sum(v.weight for v in short_votes)

        # Pre-compute momentum for both sides so it influences confluence
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

        if require_ema:
            if not ema_vote or ema_vote.side == "NEUTRAL":
                return None
            # EMA voted LONG or SHORT; need at least one other indicator agreeing
            if ema_vote.side == "LONG" and long_score >= 2 and long_weighted > short_weighted:
                side = "LONG"
                confluence_score = long_score
                weighted_score = long_weighted
            elif ema_vote.side == "SHORT" and short_score >= 2 and short_weighted > long_weighted:
                side = "SHORT"
                confluence_score = short_score
                weighted_score = short_weighted
            else:
                return None

        effective_min_confluence = (
            2
            if require_ema
            else max(min_confluence, getattr(self.cfg, "min_confluence_no_ema", 3))
        )

        if not require_ema:
            if long_score >= effective_min_confluence and long_weighted > short_weighted:
                side = "LONG"
                confluence_score = long_score
                weighted_score = long_weighted
            elif short_score >= effective_min_confluence and short_weighted > long_weighted:
                side = "SHORT"
                confluence_score = short_score
                weighted_score = short_weighted
            else:
                return None

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
        # ── Faz 3: Session Awareness bonus ─────────────────
        session_info = self._get_session_info(now)
        weighted_score += session_info["score_bonus"]

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
            session=session_info["session"],
            session_size_mult=session_info["size_mult"],
        )

        # ── Rejection checks ───────────────────────────────────

        # Already have a position in this symbol
        if symbol in open_symbols:
            signal.accepted = False
            signal.reject_reason = "position_exists"
            self._persist_signal(signal)
            return None

        # Max positions reached
        if open_count >= max_positions:
            signal.accepted = False
            signal.reject_reason = "max_positions"
            self._persist_signal(signal)
            return None

        # Cooldown
        if self._in_cooldown(symbol, now):
            signal.accepted = False
            signal.reject_reason = "cooldown"
            self._persist_signal(signal)
            return None

        # ── NEW: Funding time avoidance ──────────────────────
        if self.cfg.avoid_funding_window and _is_near_funding_time(now, self.cfg.funding_window_minutes):
            signal.accepted = False
            signal.reject_reason = "funding_window"
            self._persist_signal(signal)
            return None

        # ── NEW: Correlation filter ──────────────────────────
        if self.cfg.use_correlation_filter:
            same_dir_count = sum(
                1 for p in open_positions if p.get("side") == side
            )
            if same_dir_count >= self.cfg.max_same_direction_positions:
                signal.accepted = False
                signal.reject_reason = "correlation_limit"
                self._persist_signal(signal)
                return None

        # ── Faz 3: Anti-Manipulation Detection ─────────────
        manipulation_reason = self._detect_manipulation(snap)
        if manipulation_reason:
            signal.accepted = False
            signal.reject_reason = f"manipulation:{manipulation_reason}"
            self._persist_signal(signal)
            return None

        # Spread too wide
        if snap.spread_bps > self.cfg.max_spread_bps:
            signal.accepted = False
            signal.reject_reason = "spread_wide"
            self._persist_signal(signal)
            return None

        # Depth too thin
        if min_depth < self.cfg.min_depth_usdt:
            signal.accepted = False
            signal.reject_reason = "depth_thin"
            self._persist_signal(signal)
            return None

        # Funding rate filter
        if self.cfg.use_funding_filter and abs(snap.funding_rate) >= self.cfg.funding_rate_threshold:
            if side == "LONG" and snap.funding_rate > self.cfg.funding_rate_threshold:
                signal.accepted = False
                signal.reject_reason = "high_funding_long"
                self._persist_signal(signal)
                return None
            if side == "SHORT" and snap.funding_rate < -self.cfg.funding_rate_threshold:
                signal.accepted = False
                signal.reject_reason = "high_funding_short"
                self._persist_signal(signal)
                return None

        # Regime filter (trend-follow / breakout)
        if self.cfg.use_regime_filter:
            if mode == "TREND_FOLLOW":
                if indicators.trend_direction == "UP" and side != "LONG":
                    signal.accepted = False
                    signal.reject_reason = "trend_follow_block"
                    self._persist_signal(signal)
                    return None
                if indicators.trend_direction == "DOWN" and side != "SHORT":
                    signal.accepted = False
                    signal.reject_reason = "trend_follow_block"
                    self._persist_signal(signal)
                    return None
            elif mode == "BREAKOUT_WATCH":
                if not breakout_confirmed:
                    signal.accepted = False
                    signal.reject_reason = "breakout_not_confirmed"
                    self._persist_signal(signal)
                    return None
                if breakout_up and side != "LONG":
                    signal.accepted = False
                    signal.reject_reason = "breakout_mismatch"
                    self._persist_signal(signal)
                    return None
                if breakout_down and side != "SHORT":
                    signal.accepted = False
                    signal.reject_reason = "breakout_mismatch"
                    self._persist_signal(signal)
                    return None

        # Higher TF trend alignment
        if self.cfg.require_higher_tf_alignment and indicators.higher_tf_trend != "NEUTRAL":
            if side == "LONG" and indicators.higher_tf_trend != "UP":
                signal.accepted = False
                signal.reject_reason = "higher_tf_mismatch"
                self._persist_signal(signal)
                return None
            if side == "SHORT" and indicators.higher_tf_trend != "DOWN":
                signal.accepted = False
                signal.reject_reason = "higher_tf_mismatch"
                self._persist_signal(signal)
                return None

        # Trend filter: when we have exactly min confluence, don't trade against the trend
        if self.cfg.require_trend_not_against and confluence_score == effective_min_confluence:
            trend = indicators.trend_direction
            if side == "LONG" and trend == "DOWN":
                signal.accepted = False
                signal.reject_reason = "trend_against"
                self._persist_signal(signal)
                return None
            if side == "SHORT" and trend == "UP":
                signal.accepted = False
                signal.reject_reason = "trend_against"
                self._persist_signal(signal)
                return None

        # Pullback mode: only trade WITH the trend
        if self.cfg.trade_with_trend_only:
            trend = indicators.trend_direction
            if side == "LONG" and trend not in ("UP", "NEUTRAL"):
                signal.accepted = False
                signal.reject_reason = "trend_not_up"
                self._persist_signal(signal)
                return None
            if side == "SHORT" and trend not in ("DOWN", "NEUTRAL"):
                signal.accepted = False
                signal.reject_reason = "trend_not_down"
                self._persist_signal(signal)
                return None

        # Volume filter: skip very low volume (dead market)
        if self.cfg.min_volume_ratio > 0 and indicators.volume_ratio < self.cfg.min_volume_ratio:
            signal.accepted = False
            signal.reject_reason = "low_volume"
            self._persist_signal(signal)
            return None

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
            return None

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
            return None

        # TIGHT/ULTRA: require minimum weighted score
        if risk_state == "TIGHT":
            min_score = getattr(self.cfg, "risk_tight_min_weighted_score", 55.0)
            if weighted_score < min_score:
                signal.accepted = False
                signal.reject_reason = f"tight_score_{weighted_score:.0f}<{min_score}"
                self._persist_signal(signal)
                return None
        elif risk_state == "ULTRA_TIGHT":
            min_score = getattr(self.cfg, "risk_ultra_min_weighted_score", 65.0)
            if weighted_score < min_score:
                signal.accepted = False
                signal.reject_reason = f"ultra_score_{weighted_score:.0f}<{min_score}"
                self._persist_signal(signal)
                return None

        # ── Faz 3: Adaptive Quality Gate ─────────────────
        adaptive_min = self._get_adaptive_min_score()
        if weighted_score < adaptive_min:
            signal.accepted = False
            signal.reject_reason = f"adaptive_quality_{weighted_score:.0f}<{adaptive_min:.0f}"
            self._persist_signal(signal)
            return None

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
        bb_pct = indicators.bollinger_pct
        if bb_pct <= 0.15:
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

        # 4b. Breakout vote (BB band break + volume spike)
        if cfg.use_breakout_mode and indicators.volume_spike:
            if bb_pct >= cfg.breakout_bb_pct_high:
                votes.append(IndicatorVote(
                    name="breakout", side="LONG", weight=cfg.weight_breakout,
                    value=bb_pct, reason="BB breakout up + volume spike",
                ))
            elif bb_pct <= cfg.breakout_bb_pct_low:
                votes.append(IndicatorVote(
                    name="breakout", side="SHORT", weight=cfg.weight_breakout,
                    value=bb_pct, reason="BB breakout down + volume spike",
                ))

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

        # 6b. Order Flow Imbalance (depth ratio signal)
        total_depth = snap.bid_depth_usdt + snap.ask_depth_usdt
        if total_depth > 0:
            bid_ratio = snap.bid_depth_usdt / total_depth
            if bid_ratio >= cfg.orderflow_strong_threshold:
                votes.append(IndicatorVote(
                    name="orderflow", side="LONG", weight=cfg.weight_orderflow,
                    value=bid_ratio, reason=f"orderflow bid dominant {bid_ratio:.2f}",
                ))
            elif bid_ratio <= cfg.orderflow_weak_threshold:
                votes.append(IndicatorVote(
                    name="orderflow", side="SHORT", weight=cfg.weight_orderflow,
                    value=bid_ratio, reason=f"orderflow ask dominant {bid_ratio:.2f}",
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
                # Outside value area → breakout or rejection
                if indicators.price_vs_poc_bps > 100:
                    # Well above VA → strong breakout bullish
                    votes.append(IndicatorVote(
                        name="volume_profile", side="LONG",
                        weight=cfg.weight_volume_profile,
                        value=indicators.price_vs_poc_bps,
                        reason=f"price broke above value area (+{indicators.price_vs_poc_bps:.0f}bps)",
                    ))
                elif indicators.price_vs_poc_bps < -100:
                    # Well below VA → strong breakout bearish
                    votes.append(IndicatorVote(
                        name="volume_profile", side="SHORT",
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

        # ── Alpha Signal: Funding Rate Mean Reversion ─────────
        # Extreme positive funding = longs overleveraged → SHORT
        # Extreme negative funding = shorts overleveraged → LONG
        funding = snap.funding_rate
        fr_threshold = cfg.funding_rate_threshold
        if abs(funding) >= fr_threshold:
            fr_side = "SHORT" if funding > 0 else "LONG"
            # Scale weight by how extreme the funding is (1x-3x threshold)
            fr_intensity = min(3.0, abs(funding) / fr_threshold)
            fr_weight = getattr(cfg, "weight_funding_rate", 15.0) * (0.5 + fr_intensity * 0.25)
            votes.append(IndicatorVote(
                name="funding_rate", side=fr_side,
                weight=fr_weight,
                value=funding,
                reason=f"funding={funding:+.6f} ({fr_intensity:.1f}x threshold, contrarian)",
            ))

        # ── Alpha Signal: Multi-TF Momentum Alignment ────────
        # When higher timeframe trend aligns with signal direction
        htf = indicators.higher_tf_trend
        if htf != "NEUTRAL":
            htf_side = "LONG" if htf == "UP" else "SHORT"
            htf_weight = getattr(cfg, "weight_multi_tf", 15.0)
            votes.append(IndicatorVote(
                name="multi_tf", side=htf_side,
                weight=htf_weight,
                value=1.0 if htf == "UP" else -1.0,
                reason=f"higher TF trend={htf}",
            ))

        # ── Alpha Signal: Volatility Regime ───────────────────
        # LOW vol → tighter SL/TP, smaller moves expected
        # HIGH vol → wider SL/TP, bigger moves, more opportunity
        if indicators.atr > 0 and snap.mid_price > 0:
            atr_bps = (indicators.atr / snap.mid_price) * 10_000
            # Classify regime: squeeze_on=True → low vol breakout imminent
            if indicators.squeeze_on:
                vol_regime = "SQUEEZE"
                # Squeeze breakout: vote in direction of momentum
                if indicators.macd_histogram > 0:
                    vol_side = "LONG"
                elif indicators.macd_histogram < 0:
                    vol_side = "SHORT"
                else:
                    vol_side = "NEUTRAL"
                if vol_side != "NEUTRAL":
                    votes.append(IndicatorVote(
                        name="vol_regime", side=vol_side,
                        weight=getattr(cfg, "weight_vol_regime", 12.0),
                        value=atr_bps,
                        reason=f"squeeze breakout → {vol_side} (ATR={atr_bps:.1f}bps)",
                    ))

        # ── Filter to active indicators only ──────────────────
        active_set = set(cfg.active_indicators.split(","))
        if active_set:
            votes = [v for v in votes if v.name in active_set]

        return votes

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
        "sentiment":       "weight_sentiment",
        "orderflow":       "weight_orderflow",
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
        """Normalize raw weighted score to 0-100 scale.

        Returns (normalized_score, raw_score).

        Normalization = (sum of winning-side weights) / (max possible for
        those indicator names) × 100.

        This way the score genuinely represents "what percentage of maximum
        possible conviction did the winning indicators achieve?"
        """
        side_votes = [v for v in votes if v.side == side]
        if not side_votes:
            return 0.0, 0.0

        raw = sum(v.weight for v in side_votes)

        # Compute max possible for participating indicator names
        max_possible = 0.0
        seen: set[str] = set()
        for v in side_votes:
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
            return 0.0, raw

        normalized = (raw / max_possible) * 100.0
        return min(normalized, 100.0), raw

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

    def set_cooldown(self, symbol: str, pnl: float = 0.0) -> None:
        """Set cooldown after a trade. Smart cooldown adjusts based on result."""
        self._cooldowns[symbol] = datetime.now(timezone.utc)
        # Track per-symbol results for smart cooldown
        if symbol not in self._symbol_results:
            self._symbol_results[symbol] = []
        self._symbol_results[symbol].append(pnl)
        # Keep last 20 results per symbol
        if len(self._symbol_results[symbol]) > 20:
            self._symbol_results[symbol] = self._symbol_results[symbol][-20:]

    def _in_cooldown(self, symbol: str, now: datetime) -> bool:
        last = self._cooldowns.get(symbol)
        if last is None:
            return False
        cooldown_min = self.cfg.cooldown_minutes

        # Smart cooldown: adjust based on recent results for this symbol
        if self.cfg.use_smart_cooldown and symbol in self._symbol_results:
            recent = self._symbol_results[symbol][-self.cfg.smart_cooldown_streak_cap:]
            if recent:
                last_pnl = recent[-1]
                if last_pnl < 0:
                    # Consecutive losses → longer cooldown
                    loss_streak = 0
                    for r in reversed(recent):
                        if r < 0:
                            loss_streak += 1
                        else:
                            break
                    loss_streak = min(loss_streak, self.cfg.smart_cooldown_streak_cap)
                    cooldown_min *= self.cfg.smart_cooldown_loss_multiplier ** loss_streak
                elif last_pnl > 0:
                    # Win → shorter cooldown
                    cooldown_min *= self.cfg.smart_cooldown_win_multiplier

        elapsed_min = (now - last).total_seconds() / 60
        return elapsed_min < cooldown_min

    # ── Faz 3: Adaptive Quality Gate ──────────────────────────

    def _get_adaptive_min_score(self) -> float:
        """Compute adaptive minimum weighted score based on recent performance."""
        if not self.cfg.use_adaptive_quality:
            return self.cfg.adaptive_quality_base_score

        # Cache for 1 minute to avoid DB hits every snapshot
        now = datetime.now(timezone.utc)
        if (
            self._adaptive_score_cache is not None
            and self._adaptive_score_ts is not None
            and (now - self._adaptive_score_ts).total_seconds() < 60
        ):
            return self._adaptive_score_cache

        try:
            trades = self.db.fetch_all(
                "SELECT realised_pnl FROM positions WHERE status='CLOSED' "
                "ORDER BY closed_at DESC LIMIT ?",
                (self.cfg.adaptive_quality_lookback,),
            )
        except Exception:
            trades = []

        if len(trades) < self.cfg.adaptive_quality_min_trades:
            self._adaptive_score_cache = self.cfg.adaptive_quality_base_score
            self._adaptive_score_ts = now
            return self._adaptive_score_cache

        wins = sum(1 for t in trades if t["realised_pnl"] > 0)
        losses = len(trades) - wins
        win_rate = wins / len(trades) if trades else 0.5

        # High win rate → lower threshold (be more aggressive)
        # Low win rate → higher threshold (be more selective)
        score = self.cfg.adaptive_quality_base_score
        if win_rate >= 0.55:
            # Winning → relax threshold
            score += self.cfg.adaptive_quality_win_adjust * (win_rate - 0.5) * 20
        elif win_rate <= 0.45:
            # Losing → tighten threshold
            score += self.cfg.adaptive_quality_loss_adjust * (0.5 - win_rate) * 20

        # Check recent streak
        streak = 0
        if trades:
            last_positive = trades[0]["realised_pnl"] > 0
            for t in trades:
                if (t["realised_pnl"] > 0) == last_positive:
                    streak += 1
                else:
                    break
            if not last_positive:
                # Loss streak → tighten more aggressively
                score += min(streak, 5) * 2.0

        score = max(self.cfg.adaptive_quality_min_score,
                    min(self.cfg.adaptive_quality_max_score, score))

        self._adaptive_score_cache = score
        self._adaptive_score_ts = now
        return score

    # ── Faz 3: Anti-Manipulation Detection ────────────────────

    def _detect_manipulation(self, snap: SymbolSnapshot) -> str | None:
        """Detect potential manipulation patterns. Returns reason string or None."""
        if not self.cfg.use_anti_manipulation:
            return None

        ind = snap.indicators
        if not ind.valid:
            return None

        # Wick ratio check: large wicks relative to body = stop hunting
        if ind.recent_high > 0 and ind.recent_low > 0 and snap.mid_price > 0:
            recent_range = ind.recent_high - ind.recent_low
            if recent_range > 0:
                range_bps = (recent_range / snap.mid_price) * 10_000
                # If range is huge but price ended near where it started → wick manipulation
                if range_bps > self.cfg.rapid_reversal_bps:
                    body_bps = abs(ind.price_velocity_bps) * self.cfg.wick_lookback_bars
                    if body_bps > 0 and range_bps / body_bps > self.cfg.wick_ratio_threshold:
                        return f"wick_manipulation (range={range_bps:.0f}bps, body={body_bps:.0f}bps)"

        # Rapid velocity reversal = possible pump & dump
        if abs(ind.price_acceleration) > 0 and abs(ind.price_velocity_bps) > self.cfg.rapid_reversal_bps:
            # Acceleration opposite to velocity = reversal in progress
            if (ind.price_velocity_bps > 0 and ind.price_acceleration < -0.5) or \
               (ind.price_velocity_bps < 0 and ind.price_acceleration > 0.5):
                return f"rapid_reversal (vel={ind.price_velocity_bps:.1f}bps, acc={ind.price_acceleration:.2f})"

        return None

    # ── Faz 3: Session Awareness ──────────────────────────────

    def _get_session_info(self, now: datetime) -> dict[str, Any]:
        """Determine current trading session and return multipliers."""
        if not self.cfg.use_session_awareness:
            return {"session": "any", "size_mult": 1.0, "score_bonus": 0.0}

        hour = now.hour
        session = "off_hours"
        size_mult = 1.0
        score_bonus = 0.0

        # EU/US overlap (13-16 UTC) = best liquidity
        if self.cfg.session_us_start <= hour < self.cfg.session_eu_end:
            session = "eu_us_overlap"
            score_bonus = self.cfg.session_overlap_bonus_score

        # US session
        elif self.cfg.session_us_start <= hour < self.cfg.session_us_end:
            session = "us"

        # EU session
        elif self.cfg.session_eu_start <= hour < self.cfg.session_eu_end:
            session = "eu"

        # Asian session (lower vol, tighter ranges)
        elif self.cfg.session_asian_start <= hour < self.cfg.session_asian_end:
            session = "asian"

        # Dead zone (22-00 UTC)
        if hour >= self.cfg.session_dead_zone_start or hour < self.cfg.session_dead_zone_end:
            session = "dead_zone"
            size_mult = self.cfg.session_dead_zone_size_mult

        return {"session": session, "size_mult": size_mult, "score_bonus": score_bonus}

    def _get_min_confluence(self, risk_state: str) -> int:
        """How many indicators must agree."""
        return self.cfg.min_confluence_score

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
            long_votes = [v for v in votes if v.side == "LONG"]
            short_votes = [v for v in votes if v.side == "SHORT"]

            if len(long_votes) >= cfg.swing_min_confluence and len(long_votes) > len(short_votes):
                side = "LONG"
                confluence = len(long_votes)
            elif len(short_votes) >= cfg.swing_min_confluence and len(short_votes) > len(long_votes):
                side = "SHORT"
                confluence = len(short_votes)
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
