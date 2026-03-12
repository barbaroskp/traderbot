"""Risk management – softer state machine + position scaler.

Risk States:
  NORMAL      → standard thresholds, full notional
  TIGHT       → slightly higher entry bar, 70% notional, moderate cooldown
  ULTRA_TIGHT → top picks only, 40% notional, longer cooldown

KEY DESIGN PRINCIPLE:
  The bot should NEVER completely stop trading. Even in ULTRA_TIGHT,
  it continues to trade with reduced exposure. The risk system is a
  "volume knob" not an "off switch".

Transitions are driven by:
  - consecutive_losses (higher thresholds than before)
  - short-term drawdown
  - api error rate

De-escalation is FAST:
  - Even 1-2 wins can start recovery
  - Time-based auto-recovery prevents permanent lockout
  - Maximum time in any restricted state before forced recovery
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from src.config import Settings
from src.logger import get_logger
from src.storage import Storage

log = get_logger(__name__)


class RiskState(str, Enum):
    NORMAL = "NORMAL"
    TIGHT = "TIGHT"
    ULTRA_TIGHT = "ULTRA_TIGHT"


# ── Thresholds for state transitions ────────────────────────────────────────

@dataclass
class RiskThresholds:
    """When to escalate / de-escalate risk.

    All values are now loaded from Settings (config.py / .env).
    """
    consec_losses_tight: int = 8
    drawdown_pct_tight: float = 8.0
    drawdown_min_for_consec_tight: float = 2.0
    api_error_rate_tight: float = 0.3
    consec_losses_ultra: int = 14
    drawdown_pct_ultra: float = 18.0
    drawdown_min_for_consec_ultra: float = 5.0
    api_error_rate_ultra: float = 0.5
    consec_wins_recover: int = 2
    stable_cycles_recover: int = 3
    max_minutes_in_tight: int = 20
    max_minutes_in_ultra: int = 45

    @classmethod
    def from_config(cls, cfg: "Settings") -> "RiskThresholds":
        return cls(
            consec_losses_tight=cfg.risk_consec_losses_tight,
            drawdown_pct_tight=cfg.risk_drawdown_pct_tight,
            drawdown_min_for_consec_tight=cfg.risk_drawdown_min_for_consec_tight,
            api_error_rate_tight=cfg.risk_api_error_rate_tight,
            consec_losses_ultra=cfg.risk_consec_losses_ultra,
            drawdown_pct_ultra=cfg.risk_drawdown_pct_ultra,
            drawdown_min_for_consec_ultra=cfg.risk_drawdown_min_for_consec_ultra,
            api_error_rate_ultra=cfg.risk_api_error_rate_ultra,
            consec_wins_recover=cfg.risk_consec_wins_recover,
            stable_cycles_recover=cfg.risk_stable_cycles_recover,
            max_minutes_in_tight=cfg.risk_max_minutes_in_tight,
            max_minutes_in_ultra=cfg.risk_max_minutes_in_ultra,
        )


class RiskManager:
    """Softer risk state machine that auto-scales exposure.

    Key improvements over original:
    1. Much harder to escalate (needs more losses / deeper drawdown)
    2. Much easier to de-escalate (fewer wins, time-based recovery)
    3. Even ULTRA_TIGHT still allows 2 positions with decent sizing
    4. Never stops trading completely
    """

    def __init__(self, cfg: Settings, db: Storage) -> None:
        self.cfg = cfg
        self.db = db
        self.thresholds = RiskThresholds.from_config(cfg)
        self._state = RiskState.NORMAL
        self._consecutive_losses = 0
        self._consecutive_wins = 0
        self._stable_cycles = 0
        self._peak_balance = cfg.initial_capital_usdt
        self._current_balance = cfg.initial_capital_usdt
        self._state_entered_at: datetime = datetime.now(timezone.utc)

        # Daily loss circuit breaker state
        self._daily_pnl: float = 0.0
        self._daily_start_balance: float = cfg.initial_capital_usdt
        self._daily_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._daily_kill_until: datetime | None = None
        self._daily_reduce_active: bool = False
        self._daily_stop_active: bool = False

    @property
    def state(self) -> RiskState:
        return self._state

    @property
    def state_name(self) -> str:
        return self._state.value

    def record_trade_result(self, pnl: float) -> None:
        """Update win/loss counters after a trade closes."""
        if pnl >= 0:
            self._consecutive_wins += 1
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            self._consecutive_wins = 0

        self._current_balance += pnl
        if self._current_balance > self._peak_balance:
            self._peak_balance = self._current_balance

        # Track daily P&L for circuit breaker
        self._check_daily_reset()
        self._daily_pnl += pnl
        self._evaluate_daily_loss()

    def update_balance(self, balance: float) -> None:
        """Sync balance from actual account / paper portfolio."""
        self._current_balance = balance
        if balance > self._peak_balance:
            self._peak_balance = balance

    @property
    def drawdown_pct(self) -> float:
        if self._peak_balance <= 0:
            return 0.0
        return ((self._peak_balance - self._current_balance) / self._peak_balance) * 100

    def evaluate(self, api_error_rate: float = 0.0) -> RiskState:
        """Evaluate and possibly transition risk state.

        Call this once per scan cycle.
        Returns the new state.

        When ``cfg.risk_state_disabled`` is True, the state machine is
        completely bypassed and the manager always stays in NORMAL.
        """
        if self.cfg.risk_state_disabled:
            self._state = RiskState.NORMAL
            return self._state

        prev = self._state
        dd = self.drawdown_pct
        now = datetime.now(timezone.utc)

        # ── Time-based auto-recovery (ANTI-LOCKOUT) ────────────
        minutes_in_state = (now - self._state_entered_at).total_seconds() / 60

        if (
            self._state == RiskState.ULTRA_TIGHT
            and minutes_in_state >= self.thresholds.max_minutes_in_ultra
        ):
            self._state = RiskState.TIGHT
            self._state_entered_at = now
            self._stable_cycles = 0
            self._consecutive_losses = min(self._consecutive_losses, self.thresholds.consec_losses_tight - 1)
            log.warning(
                "time-based recovery from ULTRA_TIGHT",
                extra={"minutes_in_state": round(minutes_in_state, 1)},
            )
        elif (
            self._state == RiskState.TIGHT
            and minutes_in_state >= self.thresholds.max_minutes_in_tight
        ):
            self._state = RiskState.NORMAL
            self._state_entered_at = now
            self._stable_cycles = 0
            # Reset loss count so we don't immediately re-enter TIGHT (catch-22: no trades → no wins → stuck)
            self._consecutive_losses = 0
            log.warning(
                "time-based recovery from TIGHT",
                extra={"minutes_in_state": round(minutes_in_state, 1), "consecutive_losses_reset": True},
            )

        # ── Win-based de-escalation (check BEFORE escalation) ──
        if self._state == RiskState.ULTRA_TIGHT:
            if self._consecutive_wins >= self.thresholds.consec_wins_recover:
                self._state = RiskState.TIGHT
                self._state_entered_at = now
                self._stable_cycles = 0
        elif self._state == RiskState.TIGHT:
            self._stable_cycles += 1
            if (
                self._consecutive_wins >= self.thresholds.consec_wins_recover
                or self._stable_cycles >= self.thresholds.stable_cycles_recover
            ):
                self._state = RiskState.NORMAL
                self._state_entered_at = now
                self._stable_cycles = 0

        # ── Escalation: consec losses alone don't trigger unless drawdown is meaningful ──
        to_ultra = (
            (self._consecutive_losses >= self.thresholds.consec_losses_ultra and dd >= self.thresholds.drawdown_min_for_consec_ultra)
            or dd >= self.thresholds.drawdown_pct_ultra
            or api_error_rate >= self.thresholds.api_error_rate_ultra
        )
        to_tight = (
            (self._consecutive_losses >= self.thresholds.consec_losses_tight and dd >= self.thresholds.drawdown_min_for_consec_tight)
            or dd >= self.thresholds.drawdown_pct_tight
            or api_error_rate >= self.thresholds.api_error_rate_tight
        )
        if to_ultra:
            if self._state != RiskState.ULTRA_TIGHT:
                self._state = RiskState.ULTRA_TIGHT
                self._state_entered_at = now
                self._stable_cycles = 0
        elif to_tight:
            if self._state == RiskState.NORMAL:
                self._state = RiskState.TIGHT
                self._state_entered_at = now
                self._stable_cycles = 0

        if self._state != prev:
            reason = self._transition_reason(prev, api_error_rate)
            log.warning(
                "risk state transition",
                extra={
                    "from": prev.value,
                    "to": self._state.value,
                    "reason": reason,
                    "consec_losses": self._consecutive_losses,
                    "consec_wins": self._consecutive_wins,
                    "drawdown_pct": round(dd, 2),
                    "api_error_rate": round(api_error_rate, 3),
                    "minutes_in_prev_state": round(minutes_in_state, 1),
                },
            )
            self.db.log_risk_state(
                state=self._state.value,
                reason=reason,
                consecutive_losses=self._consecutive_losses,
                drawdown_pct=dd,
                api_error_rate=api_error_rate,
            )

        return self._state

    def get_max_trade_margin(self) -> float:
        """Max MARGIN per trade – percentage of current balance, scales with account size.

        Even in ULTRA_TIGHT, allows reasonable trade sizes.
        """
        base = self._current_balance * self.cfg.max_trade_margin_pct
        base = max(base, 1.0)  # minimum 1$ margin
        if self._state == RiskState.TIGHT:
            return base * 0.7   # 70% of max margin
        elif self._state == RiskState.ULTRA_TIGHT:
            return base * 0.4   # 40% of max margin — still tradeable
        return base

    def get_max_total_margin(self) -> float:
        """Max total MARGIN across all positions – percentage of current balance."""
        base = self._current_balance * self.cfg.max_total_margin_pct
        base = max(base, 2.0)  # minimum 2$ total
        if self._state == RiskState.TIGHT:
            return base * 0.7
        elif self._state == RiskState.ULTRA_TIGHT:
            return base * 0.4
        return base

    def get_cooldown_minutes(self) -> int:
        """Cooldown between trades for the same symbol."""
        base = self.cfg.cooldown_minutes
        if self._state == RiskState.TIGHT:
            return int(base * 1.3)   # was 1.5x, now 1.3x
        elif self._state == RiskState.ULTRA_TIGHT:
            return base * 2          # was 3x, now 2x
        return base

    def _compute_kelly_fraction(self) -> float | None:
        """Compute Kelly Criterion optimal fraction from recent trade history.

        Kelly formula: f* = (W/L) * p - q  where:
          p = win probability, q = 1 - p
          W = average win, L = average loss (absolute)
          f* = fraction * kelly_fraction (half-Kelly for safety)

        Returns None if insufficient data.
        """
        if not self.cfg.use_kelly_sizing:
            return None

        trades = self.db.fetch_all(
            "SELECT realised_pnl FROM positions WHERE status='CLOSED' "
            "ORDER BY closed_at DESC LIMIT 100"
        )
        if len(trades) < self.cfg.kelly_min_trades:
            return None

        wins = [t["realised_pnl"] for t in trades if t["realised_pnl"] > 0]
        losses = [abs(t["realised_pnl"]) for t in trades if t["realised_pnl"] < 0]

        if not wins or not losses:
            return None

        p = len(wins) / len(trades)  # win probability
        q = 1.0 - p
        avg_win = sum(wins) / len(wins)
        avg_loss = sum(losses) / len(losses)

        if avg_loss <= 0:
            return None

        win_loss_ratio = avg_win / avg_loss
        kelly = (win_loss_ratio * p - q) / win_loss_ratio

        # Apply fractional Kelly (half-Kelly default)
        kelly *= self.cfg.kelly_fraction

        # Clamp to configured bounds
        kelly = max(self.cfg.kelly_min_fraction, min(self.cfg.kelly_max_fraction, kelly))

        if kelly <= 0:
            return self.cfg.kelly_min_fraction

        log.debug(
            "kelly sizing",
            extra={
                "win_rate": round(p, 3),
                "avg_win": round(avg_win, 4),
                "avg_loss": round(avg_loss, 4),
                "raw_kelly": round(kelly / self.cfg.kelly_fraction, 4),
                "half_kelly": round(kelly, 4),
                "trades_used": len(trades),
            },
        )
        return kelly

    def compute_position_size(
        self,
        price: float,
        leverage: int,
        current_total_margin: float,
        atr: float = 0.0,
    ) -> float:
        """Compute order quantity in base asset using margin-based futures sizing.

        Sizing model:
            margin  = balance × fraction  (Kelly / Volatility / Fraction)
            notional = margin × leverage
            qty      = notional / price

        Priority: Kelly → Volatility → Fraction-based sizing.
        All three methods compute MARGIN first, then leverage amplifies it.
        """
        max_trade_margin = self.get_max_trade_margin()
        max_total_margin = self.get_max_total_margin()
        remaining_margin = max(0, max_total_margin - current_total_margin)
        min_margin = 1.0  # minimum viable margin: 1$ (2$ cok yuksekti dusuk bakiye icin)

        # Hard stop: never open new position if margin budget is exhausted
        if price <= 0 or remaining_margin < min_margin:
            return 0.0

        leverage = max(1, leverage)  # safety: never zero/negative leverage

        # ── Kelly Criterion sizing (highest priority when available) ──
        kelly_f = self._compute_kelly_fraction()
        if kelly_f is not None:
            kelly_margin = max(min_margin, self._current_balance * kelly_f)
            kelly_margin = min(kelly_margin, max_trade_margin, remaining_margin)
            notional = kelly_margin * leverage
            qty = notional / price
            log.debug(
                "kelly position size (margin-based)",
                extra={
                    "kelly_fraction": round(kelly_f, 4),
                    "margin": round(kelly_margin, 4),
                    "leverage": leverage,
                    "notional": round(notional, 4),
                    "qty": qty,
                    "balance": self._current_balance,
                },
            )
            return qty

        # ── Volatility-adjusted sizing: target a fixed dollar risk as margin ──
        if self.cfg.use_volatility_sizing and atr > 0:
            # risk_amount = balance * target_risk_pct → margin allocation
            risk_amount = self._current_balance * self.cfg.target_risk_pct
            atr_risk = atr * self.cfg.volatility_sizing_atr_mult
            if atr_risk > 0:
                # vol_qty is based on ATR risk, convert to margin equivalent
                vol_qty = risk_amount / atr_risk
                vol_margin = vol_qty * price  # margin needed for this qty (before leverage)
                # Cap by margin limits
                vol_margin = max(min_margin, vol_margin)
                vol_margin = min(vol_margin, max_trade_margin, remaining_margin)
                notional = vol_margin * leverage
                qty = notional / price
                log.debug(
                    "volatility-adjusted sizing (margin-based)",
                    extra={
                        "price": price,
                        "atr": round(atr, 6),
                        "risk_amount": round(risk_amount, 4),
                        "margin": round(vol_margin, 4),
                        "leverage": leverage,
                        "notional": round(notional, 4),
                        "qty": qty,
                        "balance": self._current_balance,
                        "risk_state": self._state.value,
                    },
                )
                return qty

        # ── Fallback: fraction-based sizing ──
        fraction_margin = self._current_balance * self.cfg.per_trade_fraction
        margin = max(min_margin, fraction_margin)
        margin = min(margin, max_trade_margin, remaining_margin)

        if margin < min_margin:
            return 0.0

        notional = margin * leverage
        qty = notional / price
        log.debug(
            "position size computed (margin-based)",
            extra={
                "price": price,
                "margin": round(margin, 4),
                "leverage": leverage,
                "notional": round(notional, 4),
                "qty": qty,
                "balance": self._current_balance,
                "remaining_margin": round(remaining_margin, 2),
                "risk_state": self._state.value,
            },
        )
        return qty

    # ── Daily Loss Circuit Breaker ─────────────────────────────────────────

    def _check_daily_reset(self) -> None:
        """Reset daily P&L tracking at midnight UTC."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily_date:
            self._daily_date = today
            self._daily_pnl = 0.0
            self._daily_start_balance = self._current_balance
            self._daily_reduce_active = False
            self._daily_stop_active = False
            # Don't reset kill cooldown - it persists across days
            log.info("daily loss tracker reset", extra={"date": today, "balance": self._current_balance})

    def _evaluate_daily_loss(self) -> None:
        """Evaluate daily loss thresholds and activate circuit breakers."""
        if not self.cfg.use_daily_loss_limit:
            return
        if self._daily_start_balance <= 0:
            return

        daily_loss_pct = abs(min(0, self._daily_pnl)) / self._daily_start_balance

        # Kill threshold: close everything + cooldown
        if daily_loss_pct >= self.cfg.daily_loss_kill_pct:
            if self._daily_kill_until is None:
                self._daily_kill_until = datetime.now(timezone.utc) + timedelta(
                    minutes=self.cfg.daily_loss_cooldown_minutes
                )
                log.warning(
                    "DAILY LOSS KILL activated",
                    extra={
                        "daily_pnl": round(self._daily_pnl, 4),
                        "daily_loss_pct": round(daily_loss_pct * 100, 2),
                        "cooldown_until": self._daily_kill_until.isoformat(),
                    },
                )
        # Stop threshold: no new trades
        elif daily_loss_pct >= self.cfg.daily_loss_limit_pct:
            if not self._daily_stop_active:
                self._daily_stop_active = True
                log.warning(
                    "DAILY LOSS STOP activated - no new trades",
                    extra={
                        "daily_pnl": round(self._daily_pnl, 4),
                        "daily_loss_pct": round(daily_loss_pct * 100, 2),
                    },
                )
        # Reduce threshold: halve position sizes
        elif daily_loss_pct >= self.cfg.daily_loss_reduce_pct:
            if not self._daily_reduce_active:
                self._daily_reduce_active = True
                log.warning(
                    "DAILY LOSS REDUCE activated - position sizes halved",
                    extra={
                        "daily_pnl": round(self._daily_pnl, 4),
                        "daily_loss_pct": round(daily_loss_pct * 100, 2),
                    },
                )

    @property
    def daily_kill_active(self) -> bool:
        """True if kill switch is active (all positions should be closed, no new trades)."""
        if self._daily_kill_until is None:
            return False
        if datetime.now(timezone.utc) >= self._daily_kill_until:
            self._daily_kill_until = None
            log.info("daily loss kill cooldown expired, trading resumed")
            return False
        return True

    @property
    def daily_stop_active(self) -> bool:
        """True if daily loss stop is active (no new trades, existing positions stay)."""
        return self._daily_stop_active and not self.daily_kill_active

    @property
    def daily_reduce_active(self) -> bool:
        """True if daily loss reduce is active (halve new position sizes)."""
        return self._daily_reduce_active and not self._daily_stop_active and not self.daily_kill_active

    def can_open_new_trade(self) -> bool:
        """Check if circuit breaker allows opening new trades."""
        if not self.cfg.use_daily_loss_limit:
            return True
        self._check_daily_reset()
        if self.daily_kill_active:
            return False
        if self.daily_stop_active:
            return False
        return True

    def get_daily_size_multiplier(self) -> float:
        """Return position size multiplier based on daily loss state."""
        if not self.cfg.use_daily_loss_limit:
            return 1.0
        if self.daily_reduce_active:
            return 0.5
        return 1.0

    @property
    def daily_pnl(self) -> float:
        self._check_daily_reset()
        return self._daily_pnl

    # ── Faz 4: Drawdown-Based Leverage Scaling ──────────────────────

    def get_drawdown_leverage_mult(self) -> float:
        """Scale down leverage as drawdown deepens.

        Linear interpolation between start and full drawdown thresholds.
        Returns 1.0 when no scaling, down to drawdown_leverage_min_mult at full threshold.
        """
        if not self.cfg.use_drawdown_leverage_scaling:
            return 1.0
        dd = self.drawdown_pct
        start = self.cfg.drawdown_leverage_start_pct
        full = self.cfg.drawdown_leverage_full_pct
        min_mult = self.cfg.drawdown_leverage_min_mult

        if dd <= start:
            return 1.0
        if dd >= full:
            return min_mult

        # Linear interpolation
        ratio = (dd - start) / (full - start)
        return 1.0 - ratio * (1.0 - min_mult)

    # ── Faz 4: Portfolio Heat Monitor ──────────────────────────────

    def get_portfolio_heat_mult(self, current_total_margin: float) -> float:
        """Reduce new position sizes when portfolio heat is high.

        Heat = current_total_margin / balance as percentage.
        Returns 1.0 when below reduce threshold, reduce_mult when above,
        and 0.0 when at max threshold (no new positions).
        """
        if not self.cfg.use_portfolio_heat:
            return 1.0
        if self._current_balance <= 0:
            return 0.0

        heat_pct = (current_total_margin / self._current_balance) * 100
        if heat_pct >= self.cfg.portfolio_heat_max_pct:
            return 0.0
        if heat_pct >= self.cfg.portfolio_heat_reduce_at_pct:
            return self.cfg.portfolio_heat_reduce_mult
        return 1.0

    # ── Faz 6: Rolling Sharpe Ratio ────────────────────────────────

    def get_rolling_sharpe(self) -> float | None:
        """Compute rolling Sharpe ratio from recent closed trades.

        Returns None if insufficient data.
        """
        if not self.cfg.use_rolling_sharpe:
            return None

        try:
            trades = self.db.fetch_all(
                "SELECT realised_pnl FROM positions WHERE status='CLOSED' "
                "ORDER BY closed_at DESC LIMIT ?",
                (self.cfg.rolling_sharpe_lookback,),
            )
        except Exception:
            return None

        if len(trades) < self.cfg.rolling_sharpe_min_trades:
            return None

        pnls = [t["realised_pnl"] for t in trades]
        mean_pnl = sum(pnls) / len(pnls)
        if len(pnls) < 2:
            return None
        variance = sum((p - mean_pnl) ** 2 for p in pnls) / (len(pnls) - 1)
        std_pnl = variance ** 0.5
        if std_pnl <= 0:
            return 0.0

        # Annualized-ish: multiply by sqrt(trades_per_day * 365)
        # For simplicity, just return raw Sharpe-like ratio
        sharpe = mean_pnl / std_pnl
        return sharpe

    def can_trade_by_sharpe(self) -> tuple[bool, float]:
        """Check if Sharpe allows trading. Returns (can_trade, size_multiplier)."""
        sharpe = self.get_rolling_sharpe()
        if sharpe is None:
            return True, 1.0  # insufficient data, allow trading

        if sharpe < self.cfg.rolling_sharpe_pause_threshold:
            return False, 0.0  # Sharpe too negative, pause
        if sharpe < self.cfg.rolling_sharpe_reduce_threshold:
            return True, 0.5  # Sharpe negative but not terrible, reduce size
        return True, 1.0

    # ── Faz 6: Strategy Decay Detection ────────────────────────────

    def detect_strategy_decay(self) -> tuple[bool, str]:
        """Compare recent vs baseline performance to detect strategy decay.

        Returns (is_decaying, reason).
        """
        if not self.cfg.use_decay_detection:
            return False, ""

        try:
            all_trades = self.db.fetch_all(
                "SELECT realised_pnl FROM positions WHERE status='CLOSED' "
                "ORDER BY closed_at DESC LIMIT ?",
                (self.cfg.decay_lookback_baseline,),
            )
        except Exception:
            return False, ""

        if len(all_trades) < self.cfg.decay_min_trades:
            return False, ""

        recent = all_trades[:self.cfg.decay_lookback_recent]
        baseline = all_trades[self.cfg.decay_lookback_recent:]

        if not baseline or not recent:
            return False, ""

        # Win rate comparison
        recent_wins = sum(1 for t in recent if t["realised_pnl"] > 0)
        baseline_wins = sum(1 for t in baseline if t["realised_pnl"] > 0)
        recent_wr = recent_wins / len(recent) * 100
        baseline_wr = baseline_wins / len(baseline) * 100

        # Profit factor comparison
        def _pf(trades):
            gross_profit = sum(t["realised_pnl"] for t in trades if t["realised_pnl"] > 0)
            gross_loss = abs(sum(t["realised_pnl"] for t in trades if t["realised_pnl"] < 0))
            return gross_profit / gross_loss if gross_loss > 0 else 99.0

        recent_pf = _pf(recent)
        baseline_pf = _pf(baseline)

        reasons = []
        if baseline_wr > 0:
            wr_drop = baseline_wr - recent_wr
            if wr_drop >= self.cfg.decay_winrate_drop_pct:
                reasons.append(f"winrate_drop:{wr_drop:.1f}%({recent_wr:.0f}vs{baseline_wr:.0f})")

        if baseline_pf > 0 and baseline_pf < 90:
            pf_drop_pct = ((baseline_pf - recent_pf) / baseline_pf) * 100
            if pf_drop_pct >= self.cfg.decay_pf_drop_pct:
                reasons.append(f"pf_drop:{pf_drop_pct:.0f}%({recent_pf:.2f}vs{baseline_pf:.2f})")

        if reasons:
            return True, "; ".join(reasons)
        return False, ""

    def get_decay_size_mult(self) -> float:
        """Return size multiplier if strategy is decaying."""
        is_decaying, reason = self.detect_strategy_decay()
        if is_decaying:
            if self.cfg.decay_action == "pause":
                return 0.0
            log.warning("strategy decay detected", extra={"reason": reason})
            return self.cfg.decay_size_mult
        return 1.0

    def get_diagnostics(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        minutes_in_state = (now - self._state_entered_at).total_seconds() / 60
        return {
            "state": self._state.value,
            "consecutive_losses": self._consecutive_losses,
            "consecutive_wins": self._consecutive_wins,
            "drawdown_pct": round(self.drawdown_pct, 2),
            "balance": round(self._current_balance, 2),
            "peak_balance": round(self._peak_balance, 2),
            "max_trade_margin": self.get_max_trade_margin(),
            "max_total_margin": self.get_max_total_margin(),
            "minutes_in_current_state": round(minutes_in_state, 1),
            "auto_recovery_in_minutes": self._time_to_recovery(),
            "daily_pnl": round(self._daily_pnl, 4),
            "daily_kill_active": self.daily_kill_active,
            "daily_stop_active": self.daily_stop_active,
            "daily_reduce_active": self.daily_reduce_active,
            "drawdown_leverage_mult": round(self.get_drawdown_leverage_mult(), 3),
            "rolling_sharpe": self.get_rolling_sharpe(),
            "strategy_decaying": self.detect_strategy_decay()[0],
        }

    def _time_to_recovery(self) -> float:
        """Minutes until time-based auto-recovery kicks in."""
        now = datetime.now(timezone.utc)
        minutes_in_state = (now - self._state_entered_at).total_seconds() / 60
        if self._state == RiskState.ULTRA_TIGHT:
            return max(0, self.thresholds.max_minutes_in_ultra - minutes_in_state)
        elif self._state == RiskState.TIGHT:
            return max(0, self.thresholds.max_minutes_in_tight - minutes_in_state)
        return 0.0

    def _transition_reason(self, prev: RiskState, api_error_rate: float) -> str:
        parts: list[str] = []
        if self._consecutive_losses >= self.thresholds.consec_losses_tight:
            parts.append(f"consec_losses={self._consecutive_losses}")
        if self.drawdown_pct >= self.thresholds.drawdown_pct_tight:
            parts.append(f"drawdown={self.drawdown_pct:.1f}%")
        if api_error_rate >= self.thresholds.api_error_rate_tight:
            parts.append(f"api_errors={api_error_rate:.2f}")
        if self._consecutive_wins >= self.thresholds.consec_wins_recover:
            parts.append(f"consec_wins={self._consecutive_wins}")
        if self._stable_cycles >= self.thresholds.stable_cycles_recover:
            parts.append(f"stable_cycles={self._stable_cycles}")

        now = datetime.now(timezone.utc)
        minutes_in_state = (now - self._state_entered_at).total_seconds() / 60
        if (
            prev == RiskState.ULTRA_TIGHT
            and minutes_in_state >= self.thresholds.max_minutes_in_ultra
        ):
            parts.append(f"time_recovery={minutes_in_state:.0f}min")
        elif (
            prev == RiskState.TIGHT
            and minutes_in_state >= self.thresholds.max_minutes_in_tight
        ):
            parts.append(f"time_recovery={minutes_in_state:.0f}min")

        return "; ".join(parts) if parts else "auto"
