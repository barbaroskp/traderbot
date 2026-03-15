"""9-indicator confluence strategy with smart filters.

Signal generation uses 9 independent indicators that each vote LONG, SHORT,
or NEUTRAL.  A trade is taken when enough indicators agree (confluence).

Each indicator measures a genuinely different aspect of the market:
  1. Trend (EMA + ADX)       – Is there a trend? Which direction? How strong?
  2. Momentum (RSI)          – Overbought/oversold mean-reversion signal
  3. Mean Reversion (BB)     – Price at statistical extremes (Bollinger Bands)
  4. Volume (OBV)            – Volume flow confirms or denies price direction
  5. Orderbook (Imbalance)   – Bid/ask pressure from market microstructure
  6. Multi-TF (15m Trend)    – Higher timeframe alignment
  7. MACD Momentum           – Histogram crossover + strengthening
  8. Funding Rate            – Contrarian signal from extreme leverage
  9. VWAP Deviation          – Price vs fair value anchor

Smart filters:
  - Spread/depth rejection (microstructure quality)
  - Correlation limiter (max same-direction positions)
  - Funding window avoidance (±15min around 00/08/16 UTC)
  - MACD confirmation at minimum confluence (reduces false entries)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from src.config import Settings
from src.logger import get_logger
from src.marketdata import Indicators, SymbolSnapshot
from src.storage import Storage

log = get_logger(__name__)


# ── Indicator weights (total = 150) ─────────────────────────────────────────
WEIGHTS: dict[str, float] = {
    "trend": 20,
    "momentum": 20,
    "mean_reversion": 15,
    "volume": 15,
    "orderbook": 15,
    "multi_tf": 15,
    "macd": 18,
    "funding": 17,
    "vwap": 15,
}


def _is_near_funding_time(now: datetime, window_minutes: int = 15) -> bool:
    """Check if current time is within ±window of funding times (00, 08, 16 UTC)."""
    funding_hours = [0, 8, 16]
    for h in funding_hours:
        funding_time = now.replace(hour=h, minute=0, second=0, microsecond=0)
        delta = abs((now - funding_time).total_seconds()) / 60
        # Handle wrap-around at midnight
        if delta > 12 * 60:
            delta = 24 * 60 - delta
        if delta <= window_minutes:
            return True
    return False


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class IndicatorVote:
    """A single indicator's vote."""
    name: str
    side: str           # LONG, SHORT, NEUTRAL
    weight: float = 0.0
    value: float = 0.0
    reason: str = ""


@dataclass
class Signal:
    """A trading signal with confluence details."""
    symbol: str
    side: str               # LONG or SHORT
    z_score_bps: float
    mid_price: float
    fast_ema: float
    slow_ema: float
    spread_bps: float
    depth_usdt: float
    accepted: bool = True
    reject_reason: str = ""
    ts: str = ""
    # ── Confluence ──
    confluence_score: int = 0
    weighted_score: float = 0.0
    indicator_votes: list[IndicatorVote] = field(default_factory=list)
    # ── Key values (for logging & execution layer) ──
    rsi: float = 50.0
    macd_histogram: float = 0.0
    macd_histogram_prev: float = 0.0
    bollinger_pct: float = 0.5
    trend_direction: str = "NEUTRAL"
    higher_tf_trend: str = "NEUTRAL"
    atr: float = 0.0
    adx: float = 0.0
    volume_ratio: float = 1.0
    # ── Extended metadata ──
    mode: str = "scalp"
    trade_type: str = "scalp"
    funding_rate: float = 0.0
    momentum_confirmed: bool = False

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = datetime.now(timezone.utc).isoformat()


# ── Strategy ─────────────────────────────────────────────────────────────────

class Strategy:
    """9-indicator confluence signal generator with smart filters."""

    def __init__(self, cfg: Settings, db: Storage) -> None:
        self.cfg = cfg
        self.db = db
        self._cooldowns: dict[str, datetime] = {}
        self._symbol_results: dict[str, list[float]] = {}

    # ── Cooldown management ──────────────────────────────────────────────────

    def set_cooldown(self, symbol: str, pnl: float = 0.0) -> None:
        """Set cooldown for a symbol. Longer cooldown after a loss."""
        minutes = self.cfg.cooldown_minutes
        if pnl < 0:
            # Smart cooldown: scale with consecutive losses
            recent = self._symbol_results.get(symbol, [])
            consec_losses = 0
            for r in reversed(recent):
                if r < 0:
                    consec_losses += 1
                else:
                    break
            minutes = int(minutes * min(4, 1.5 + consec_losses * 0.5))
        # Track results
        if symbol not in self._symbol_results:
            self._symbol_results[symbol] = []
        self._symbol_results[symbol].append(pnl)
        if len(self._symbol_results[symbol]) > 20:
            self._symbol_results[symbol] = self._symbol_results[symbol][-20:]
        self._cooldowns[symbol] = datetime.now(timezone.utc) + timedelta(minutes=minutes)

    def _is_on_cooldown(self, symbol: str) -> bool:
        cd = self._cooldowns.get(symbol)
        if cd is None:
            return False
        if datetime.now(timezone.utc) >= cd:
            del self._cooldowns[symbol]
            return False
        return True

    # ── Signal generation ────────────────────────────────────────────────────

    def generate_signals(
        self,
        snapshots: list[SymbolSnapshot],
        open_positions: list[dict[str, Any]],
        risk_state: str = "NORMAL",
    ) -> list[Signal]:
        """Score all snapshots and return accepted signals."""
        open_symbols = {p["symbol"] for p in open_positions}
        signals: list[Signal] = []
        new_this_cycle = 0
        available_slots = max(0, self.cfg.max_open_positions - len(open_symbols))

        for snap in snapshots:
            if new_this_cycle >= available_slots:
                break
            signal = self._score_symbol(snap, open_symbols, open_positions, risk_state)
            if signal is not None and signal.accepted:
                signals.append(signal)
                new_this_cycle += 1
                open_symbols.add(snap.symbol)

        return signals

    def generate_swing_signals(
        self,
        snapshots: list[SymbolSnapshot],
        open_positions: list[dict[str, Any]],
        risk_state: str = "NORMAL",
    ) -> list[Signal]:
        """Generate swing trade signals (higher timeframe, stricter filters)."""
        swing_max = getattr(self.cfg, "swing_max_positions", 3)
        swing_min_conf = getattr(self.cfg, "swing_min_confluence", 4)
        require_trend = getattr(self.cfg, "swing_require_trend_alignment", True)

        swing_positions = [p for p in open_positions if p.get("trade_type") == "swing"]
        open_symbols = {p["symbol"] for p in open_positions}
        signals: list[Signal] = []

        if len(swing_positions) >= swing_max:
            return signals

        for snap in snapshots:
            if snap.symbol in open_symbols:
                continue
            if len(swing_positions) + len(signals) >= swing_max:
                break

            ind = snap.indicators
            if not ind.valid or snap.mid_price <= 0:
                continue

            # Collect votes (same indicators)
            votes = self._collect_votes(ind, snap)
            long_count = sum(1 for v in votes if v.side == "LONG")
            short_count = sum(1 for v in votes if v.side == "SHORT")
            long_score = sum(v.weight for v in votes if v.side == "LONG")
            short_score = sum(v.weight for v in votes if v.side == "SHORT")

            if long_score > short_score and long_count >= swing_min_conf:
                side, confluence, weighted = "LONG", long_count, long_score
            elif short_score > long_score and short_count >= swing_min_conf:
                side, confluence, weighted = "SHORT", short_count, short_score
            else:
                continue

            # Require higher TF alignment for swing
            if require_trend:
                if side == "LONG" and ind.higher_tf_trend != "UP":
                    continue
                if side == "SHORT" and ind.higher_tf_trend != "DOWN":
                    continue

            # Spread/depth checks
            if snap.spread_bps > self.cfg.max_spread_bps:
                continue
            depth = min(snap.bid_depth_usdt, snap.ask_depth_usdt)
            if depth < self.cfg.min_depth_usdt:
                continue

            signal = Signal(
                symbol=snap.symbol,
                side=side,
                z_score_bps=snap.z_score_bps,
                mid_price=snap.mid_price,
                fast_ema=snap.fast_ema,
                slow_ema=snap.slow_ema,
                spread_bps=snap.spread_bps,
                depth_usdt=depth,
                confluence_score=confluence,
                weighted_score=weighted,
                indicator_votes=votes,
                rsi=ind.rsi,
                macd_histogram=ind.macd_histogram,
                macd_histogram_prev=ind.macd_histogram_prev,
                bollinger_pct=ind.bollinger_pct,
                trend_direction=ind.trend_direction,
                higher_tf_trend=ind.higher_tf_trend,
                atr=ind.atr,
                adx=ind.adx,
                volume_ratio=ind.volume_ratio,
                trade_type="swing",
                mode="SWING",
            )
            signals.append(signal)

        return signals

    def _collect_votes(self, ind: Indicators, snap: SymbolSnapshot) -> list[IndicatorVote]:
        """Collect all indicator votes."""
        votes: list[IndicatorVote] = [
            self._vote_trend(ind),
            self._vote_momentum(ind),
            self._vote_mean_reversion(ind),
            self._vote_volume(ind),
            self._vote_orderbook(snap),
            self._vote_multi_tf(ind),
            self._vote_macd(ind),
            self._vote_funding(snap),
            self._vote_vwap(ind),
        ]
        return votes

    def _score_symbol(
        self,
        snap: SymbolSnapshot,
        open_symbols: set[str],
        open_positions: list[dict[str, Any]],
        risk_state: str,
    ) -> Signal | None:
        """Score a single symbol using 9 independent indicators + smart filters."""
        symbol = snap.symbol
        ind = snap.indicators

        # ── Pre-checks ───────────────────────────────────────────
        if symbol in open_symbols:
            return None
        if self._is_on_cooldown(symbol):
            return None
        if not ind.valid:
            return None
        if snap.mid_price <= 0:
            return None

        # ── Spread / depth quality filter ─────────────────────────
        if snap.spread_bps > self.cfg.max_spread_bps:
            return None
        depth = min(snap.bid_depth_usdt, snap.ask_depth_usdt)
        if depth < self.cfg.min_depth_usdt:
            return None

        # ── Funding window avoidance ─────────────────────────────
        if getattr(self.cfg, "avoid_funding_window", True):
            now = datetime.now(timezone.utc)
            if _is_near_funding_time(now, window_minutes=15):
                return None

        # ── Collect votes ────────────────────────────────────────
        votes = self._collect_votes(ind, snap)

        # ── Tally ────────────────────────────────────────────────
        long_score = sum(v.weight for v in votes if v.side == "LONG")
        short_score = sum(v.weight for v in votes if v.side == "SHORT")
        long_count = sum(1 for v in votes if v.side == "LONG")
        short_count = sum(1 for v in votes if v.side == "SHORT")

        if long_score > short_score:
            side = "LONG"
            weighted_score = long_score
            confluence = long_count
        elif short_score > long_score:
            side = "SHORT"
            weighted_score = short_score
            confluence = short_count
        else:
            return None  # no clear direction

        # ── Conflict filter: trend vs mean-reversion disagreement ──
        # If trend says one direction but mean_reversion says opposite, skip
        trend_vote = next((v for v in votes if v.name == "trend"), None)
        mr_vote = next((v for v in votes if v.name == "mean_reversion"), None)
        if (
            trend_vote and mr_vote
            and trend_vote.side != "NEUTRAL" and mr_vote.side != "NEUTRAL"
            and trend_vote.side != mr_vote.side
        ):
            log.debug(
                "signal rejected",
                extra={"symbol": symbol, "reason": "trend_mr_conflict",
                       "trend": trend_vote.side, "mr": mr_vote.side},
            )
            return None

        # ── Higher TF alignment filter ──────────────────────────
        if getattr(self.cfg, "require_higher_tf_alignment", False):
            htf = ind.higher_tf_trend
            if side == "LONG" and htf == "DOWN":
                log.debug(
                    "signal rejected",
                    extra={"symbol": symbol, "reason": "htf_against",
                           "side": side, "htf": htf},
                )
                return None
            if side == "SHORT" and htf == "UP":
                log.debug(
                    "signal rejected",
                    extra={"symbol": symbol, "reason": "htf_against",
                           "side": side, "htf": htf},
                )
                return None

        # ── Threshold checks ─────────────────────────────────────
        min_confluence = self.cfg.min_confluence_score
        min_score = self.cfg.min_weighted_score

        # Tighter thresholds under elevated risk
        if risk_state == "TIGHT":
            min_score = max(min_score, 65.0)
            min_confluence = max(min_confluence, 6)
        elif risk_state == "ULTRA_TIGHT":
            min_score = max(min_score, 80.0)
            min_confluence = max(min_confluence, 7)

        if confluence < min_confluence:
            log.debug(
                "signal rejected",
                extra={"symbol": symbol, "reason": "low_confluence",
                       "confluence": confluence, "score": weighted_score},
            )
            return None

        if weighted_score < min_score:
            log.debug(
                "signal rejected",
                extra={"symbol": symbol, "reason": "low_score",
                       "confluence": confluence, "score": weighted_score},
            )
            return None

        # ── Z-score guard (extreme deviation filter) ─────────────
        if abs(snap.z_score_bps) > self.cfg.max_z_score_bps:
            return None

        # ── MACD momentum confirmation at min confluence ─────────
        # At exactly min confluence, require MACD to agree (reduces false entries)
        if getattr(self.cfg, "require_momentum_confirmation", False):
            if confluence == min_confluence:
                if not self._check_momentum(ind, side):
                    log.debug(
                        "signal rejected",
                        extra={"symbol": symbol, "reason": "no_momentum",
                               "confluence": confluence},
                    )
                    return None

        # ── Correlation filter ────────────────────────────────────
        if getattr(self.cfg, "use_correlation_filter", False):
            max_same_dir = getattr(self.cfg, "max_same_direction_positions", 3)
            same_dir_count = sum(
                1 for p in open_positions if p.get("side") == side
            )
            if same_dir_count >= max_same_dir:
                log.debug(
                    "signal rejected",
                    extra={"symbol": symbol, "reason": "correlation_limit",
                           "same_dir": same_dir_count, "max": max_same_dir},
                )
                return None

        # ── Determine momentum confirmation ──────────────────────
        momentum_confirmed = self._check_momentum(ind, side)

        # ── Build signal ─────────────────────────────────────────
        signal = Signal(
            symbol=symbol,
            side=side,
            z_score_bps=snap.z_score_bps,
            mid_price=snap.mid_price,
            fast_ema=snap.fast_ema,
            slow_ema=snap.slow_ema,
            spread_bps=snap.spread_bps,
            depth_usdt=depth,
            confluence_score=confluence,
            weighted_score=weighted_score,
            indicator_votes=votes,
            rsi=ind.rsi,
            macd_histogram=ind.macd_histogram,
            macd_histogram_prev=ind.macd_histogram_prev,
            bollinger_pct=ind.bollinger_pct,
            trend_direction=ind.trend_direction,
            higher_tf_trend=ind.higher_tf_trend,
            atr=ind.atr,
            adx=ind.adx,
            volume_ratio=ind.volume_ratio,
            trade_type="scalp",
            funding_rate=snap.funding_rate,
            momentum_confirmed=momentum_confirmed,
        )

        # Persist to DB
        self.db.insert("signals", {
            "ts": signal.ts,
            "symbol": symbol,
            "side": side,
            "z_score_bps": snap.z_score_bps,
            "mid_price": snap.mid_price,
            "fast_ema": snap.fast_ema,
            "slow_ema": snap.slow_ema,
            "spread_bps": snap.spread_bps,
            "depth_usdt": signal.depth_usdt,
            "accepted": 1,
            "reject_reason": "",
        })

        log.info(
            "signal generated",
            extra={
                "symbol": symbol,
                "side": side,
                "confluence": confluence,
                "weighted_score": round(weighted_score, 1),
                "votes": "|".join(
                    f"{v.name}:{v.side}" for v in votes if v.side != "NEUTRAL"
                ),
                "rsi": round(ind.rsi, 1),
                "bb_pct": round(ind.bollinger_pct, 2),
                "trend": ind.trend_direction,
                "adx": round(ind.adx, 1),
                "obv_slope": round(ind.obv_slope, 3),
                "macd_h": round(ind.macd_histogram, 6),
                "funding": round(snap.funding_rate, 6),
                "momentum": momentum_confirmed,
            },
        )

        return signal

    # ── Momentum check ───────────────────────────────────────────────────────

    def _check_momentum(self, ind: Indicators, side: str) -> bool:
        """Check if MACD momentum aligns with signal direction."""
        if not ind.valid:
            return True  # no data → don't block
        if not ind.macd_strengthening:
            return False
        hist = ind.macd_histogram
        if side == "LONG" and hist > 0:
            return True
        if side == "SHORT" and hist < 0:
            return True
        # Crossover case
        prev = ind.macd_histogram_prev
        if side == "LONG" and hist > 0 and prev <= 0:
            return True
        if side == "SHORT" and hist < 0 and prev >= 0:
            return True
        return False

    # ── Indicator Voters ─────────────────────────────────────────────────────
    #
    # Each voter returns IndicatorVote with side LONG/SHORT/NEUTRAL.
    # Weight is fixed per indicator (from WEIGHTS dict).
    # No gradations – a vote is a vote.  Strength comes from confluence count.

    def _vote_trend(self, ind: Indicators) -> IndicatorVote:
        """Trend: Price vs EMA50 + ADX strength + DI direction.

        Requires both trend direction (price vs EMA) AND trend strength (ADX)
        AND directional confirmation (DI+/DI-) to vote.
        """
        w = WEIGHTS["trend"]
        if ind.ema_trend <= 0:
            return IndicatorVote("trend", "NEUTRAL", 0.0)

        price_above = ind.kline_fast_ema > ind.ema_trend
        price_below = ind.kline_fast_ema < ind.ema_trend
        strong = ind.adx >= self.cfg.adx_trend_threshold

        if price_above and strong and ind.plus_di > ind.minus_di:
            return IndicatorVote(
                "trend", "LONG", w,
                value=ind.adx,
                reason=f"adx={ind.adx:.0f} +di={ind.plus_di:.0f}>{ind.minus_di:.0f}",
            )
        if price_below and strong and ind.minus_di > ind.plus_di:
            return IndicatorVote(
                "trend", "SHORT", w,
                value=ind.adx,
                reason=f"adx={ind.adx:.0f} -di={ind.minus_di:.0f}>{ind.plus_di:.0f}",
            )
        return IndicatorVote("trend", "NEUTRAL", 0.0)

    def _vote_momentum(self, ind: Indicators) -> IndicatorVote:
        """Momentum: RSI overbought/oversold.

        RSI < oversold → LONG (oversold, expect bounce)
        RSI > overbought → SHORT (overbought, expect pullback)
        """
        w = WEIGHTS["momentum"]
        if ind.rsi < self.cfg.rsi_oversold:
            return IndicatorVote(
                "momentum", "LONG", w, value=ind.rsi,
                reason=f"rsi={ind.rsi:.1f}<{self.cfg.rsi_oversold}",
            )
        if ind.rsi > self.cfg.rsi_overbought:
            return IndicatorVote(
                "momentum", "SHORT", w, value=ind.rsi,
                reason=f"rsi={ind.rsi:.1f}>{self.cfg.rsi_overbought}",
            )
        return IndicatorVote("momentum", "NEUTRAL", 0.0)

    def _vote_mean_reversion(self, ind: Indicators) -> IndicatorVote:
        """Mean Reversion: Bollinger Band position.

        Price near lower band (bb_pct < 0.15) → LONG (expect reversion up)
        Price near upper band (bb_pct > 0.85) → SHORT (expect reversion down)
        """
        w = WEIGHTS["mean_reversion"]
        if ind.bollinger_pct < 0.15:
            return IndicatorVote(
                "mean_reversion", "LONG", w, value=ind.bollinger_pct,
                reason=f"bb_pct={ind.bollinger_pct:.2f}",
            )
        if ind.bollinger_pct > 0.85:
            return IndicatorVote(
                "mean_reversion", "SHORT", w, value=ind.bollinger_pct,
                reason=f"bb_pct={ind.bollinger_pct:.2f}",
            )
        return IndicatorVote("mean_reversion", "NEUTRAL", 0.0)

    def _vote_volume(self, ind: Indicators) -> IndicatorVote:
        """Volume: OBV slope (accumulation vs distribution).

        Positive slope → accumulation → LONG
        Negative slope → distribution → SHORT
        Very small slope (noise) → NEUTRAL
        """
        w = WEIGHTS["volume"]
        min_slope = 0.03  # higher threshold: ignore noise (was 0.01)
        if ind.obv_slope > min_slope:
            return IndicatorVote(
                "volume", "LONG", w, value=ind.obv_slope,
                reason=f"obv_slope={ind.obv_slope:.3f}",
            )
        if ind.obv_slope < -min_slope:
            return IndicatorVote(
                "volume", "SHORT", w, value=ind.obv_slope,
                reason=f"obv_slope={ind.obv_slope:.3f}",
            )
        return IndicatorVote("volume", "NEUTRAL", 0.0)

    def _vote_orderbook(self, snap: SymbolSnapshot) -> IndicatorVote:
        """Orderbook: Depth imbalance (bid vs ask pressure).

        Bid-heavy (imbalance > 0.60) → LONG (buying pressure)
        Ask-heavy (imbalance < 0.40) → SHORT (selling pressure)
        """
        w = WEIGHTS["orderbook"]
        imb = snap.imbalance_ratio
        if imb > self.cfg.orderbook_imbalance_long:
            return IndicatorVote(
                "orderbook", "LONG", w, value=imb,
                reason=f"imb={imb:.2f}",
            )
        if imb < self.cfg.orderbook_imbalance_short:
            return IndicatorVote(
                "orderbook", "SHORT", w, value=imb,
                reason=f"imb={imb:.2f}",
            )
        return IndicatorVote("orderbook", "NEUTRAL", 0.0)

    def _vote_multi_tf(self, ind: Indicators) -> IndicatorVote:
        """Multi-Timeframe: Higher TF (15m) trend confirmation.

        Aligns with the bigger picture – reduces false signals from noise.
        """
        w = WEIGHTS["multi_tf"]
        if ind.higher_tf_trend == "UP":
            return IndicatorVote("multi_tf", "LONG", w, reason="15m_up")
        if ind.higher_tf_trend == "DOWN":
            return IndicatorVote("multi_tf", "SHORT", w, reason="15m_down")
        return IndicatorVote("multi_tf", "NEUTRAL", 0.0)

    def _vote_macd(self, ind: Indicators) -> IndicatorVote:
        """MACD: Histogram crossover + momentum strength.

        Positive histogram + strengthening → LONG
        Negative histogram + strengthening → SHORT
        Crossover bonus: higher weight when crossing zero
        """
        w = WEIGHTS["macd"]
        hist = ind.macd_histogram
        prev = ind.macd_histogram_prev

        if hist == 0:
            return IndicatorVote("macd", "NEUTRAL", 0.0)

        # Bullish: positive histogram
        if hist > 0:
            # Crossover from negative → stronger signal
            if prev <= 0:
                return IndicatorVote(
                    "macd", "LONG", w * 1.3,
                    value=hist, reason=f"macd_crossover_up h={hist:.6f}",
                )
            # Strengthening (histogram growing)
            if hist > prev:
                return IndicatorVote(
                    "macd", "LONG", w,
                    value=hist, reason=f"macd_rising h={hist:.6f}",
                )
            # Weakening (histogram shrinking but still positive) → NEUTRAL
            # Don't vote when momentum is fading
            return IndicatorVote("macd", "NEUTRAL", 0.0,
                value=hist, reason=f"macd_fading h={hist:.6f}")

        # Bearish: negative histogram
        if prev >= 0:
            return IndicatorVote(
                "macd", "SHORT", w * 1.3,
                value=hist, reason=f"macd_crossover_down h={hist:.6f}",
            )
        if hist < prev:
            return IndicatorVote(
                "macd", "SHORT", w,
                value=hist, reason=f"macd_falling h={hist:.6f}",
            )
        # Weakening bearish → NEUTRAL
        return IndicatorVote("macd", "NEUTRAL", 0.0,
            value=hist, reason=f"macd_fading h={hist:.6f}")

    def _vote_funding(self, snap: SymbolSnapshot) -> IndicatorVote:
        """Funding Rate: Contrarian signal from extreme leverage positioning.

        High positive funding → longs overleveraged → SHORT (contrarian)
        High negative funding → shorts overleveraged → LONG (contrarian)
        """
        w = WEIGHTS["funding"]
        funding = snap.funding_rate
        threshold = getattr(self.cfg, "funding_rate_threshold", 0.0005)

        if abs(funding) < threshold:
            return IndicatorVote("funding", "NEUTRAL", 0.0)

        # Intensity scales weight: more extreme = stronger conviction
        intensity = min(3.0, abs(funding) / threshold)
        scaled_w = w * (0.5 + intensity * 0.25)

        if funding > threshold:
            return IndicatorVote(
                "funding", "SHORT", scaled_w,
                value=funding, reason=f"funding={funding:.6f} (longs overleveraged)",
            )
        if funding < -threshold:
            return IndicatorVote(
                "funding", "LONG", scaled_w,
                value=funding, reason=f"funding={funding:.6f} (shorts overleveraged)",
            )
        return IndicatorVote("funding", "NEUTRAL", 0.0)

    def _vote_vwap(self, ind: Indicators) -> IndicatorVote:
        """VWAP: Price deviation from volume-weighted average price.

        Below VWAP → undervalued → LONG
        Above VWAP → overvalued → SHORT
        """
        w = WEIGHTS["vwap"]
        dev = ind.vwap_deviation_bps
        threshold = getattr(self.cfg, "entry_threshold_bps", 15.0)

        if ind.vwap <= 0 or abs(dev) < threshold:
            return IndicatorVote("vwap", "NEUTRAL", 0.0)

        if dev < -threshold:
            return IndicatorVote(
                "vwap", "LONG", w,
                value=dev, reason=f"vwap_dev={dev:.1f}bps (below vwap)",
            )
        if dev > threshold:
            return IndicatorVote(
                "vwap", "SHORT", w,
                value=dev, reason=f"vwap_dev={dev:.1f}bps (above vwap)",
            )
        return IndicatorVote("vwap", "NEUTRAL", 0.0)
