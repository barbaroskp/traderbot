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
from datetime import datetime, timezone
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
        raw_kelly = (win_loss_ratio * p - q) / win_loss_ratio

        # A non-positive Kelly means the measured edge is NEGATIVE and the
        # optimal bet size is zero. This used to clamp up to kelly_min_fraction,
        # so the bot carried on sizing positions while its own closed-trade
        # history said it was losing — the exact failure Kelly exists to prevent.
        if raw_kelly <= 0:
            log.warning(
                "kelly: measured edge is negative — sizing to zero",
                extra={
                    "win_rate": round(p, 3),
                    "avg_win": round(avg_win, 4),
                    "avg_loss": round(avg_loss, 4),
                    "raw_kelly": round(raw_kelly, 4),
                    "trades_used": len(trades),
                },
            )
            if self.cfg.kelly_block_on_negative_edge:
                return 0.0
            return self.cfg.kelly_min_fraction

        # Apply fractional Kelly
        kelly = raw_kelly * self.cfg.kelly_fraction

        # Clamp to configured bounds
        kelly = max(self.cfg.kelly_min_fraction, min(self.cfg.kelly_max_fraction, kelly))

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
            # Zero fraction = proven negative edge. Refuse the trade outright;
            # falling through to min_margin here would silently reinstate a
            # position size Kelly just told us not to take.
            if kelly_f <= 0:
                log.warning(
                    "position sizing blocked: kelly fraction is zero (negative measured edge)",
                    extra={"balance": self._current_balance},
                )
                return 0.0
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
