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

    Designed so the bot does NOT get stuck in TIGHT:
    - Escalate only when BOTH consec losses AND meaningful drawdown (avoids streak-only lockout)
    - Higher consec loss counts before TIGHT/ULTRA
    - Short time-based recovery so we return to NORMAL quickly
    """
    # NORMAL → TIGHT: need BOTH consec losses AND at least this drawdown (avoids bad streak with flat PnL)
    consec_losses_tight: int = 8
    drawdown_pct_tight: float = 8.0
    drawdown_min_for_consec_tight: float = 2.0   # only escalate on consec losses if dd >= this %
    api_error_rate_tight: float = 0.3

    # TIGHT → ULTRA_TIGHT
    consec_losses_ultra: int = 14
    drawdown_pct_ultra: float = 18.0
    drawdown_min_for_consec_ultra: float = 5.0
    api_error_rate_ultra: float = 0.5

    # De-escalation: easy recovery
    consec_wins_recover: int = 2
    stable_cycles_recover: int = 3

    # Time-based auto-recovery: SHORT so we don't sit in TIGHT forever
    max_minutes_in_tight: int = 20     # force NORMAL after 20 min
    max_minutes_in_ultra: int = 45     # force TIGHT after 45 min


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
        self.thresholds = RiskThresholds()
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
        """
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

    def get_max_trade_notional(self) -> float:
        """Max notional per trade, adjusted by risk state.

        Even in ULTRA_TIGHT, allows reasonable trade sizes.
        """
        base = self.cfg.max_trade_notional_usdt
        if self._state == RiskState.TIGHT:
            return base * 0.7   # was 0.5, now 70%
        elif self._state == RiskState.ULTRA_TIGHT:
            return base * 0.4   # was 0.2, now 40% — still tradeable
        return base

    def get_max_total_notional(self) -> float:
        """Max total notional across all positions."""
        base = self.cfg.max_total_notional_usdt
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

    def compute_position_size(
        self,
        price: float,
        current_total_notional: float,
        atr: float = 0.0,
    ) -> float:
        """Compute order quantity in base asset.

        Respects per-trade fraction, max notional, and total exposure limits.
        Uses at least min_notional (2 USDT) to ensure orders aren't too small.

        When use_volatility_sizing is enabled and ATR is available, sizes positions
        inversely proportional to volatility: smaller in high-vol, larger in low-vol.
        """
        max_trade = self.get_max_trade_notional()
        max_total = self.get_max_total_notional()
        remaining = max(0, max_total - current_total_notional)

        # Volatility-adjusted sizing: target a fixed dollar risk per trade
        if self.cfg.use_volatility_sizing and atr > 0 and price > 0:
            # risk_amount = balance * target_risk_pct (e.g., 1% of 50 USDT = 0.5 USDT)
            risk_amount = self._current_balance * self.cfg.target_risk_pct
            # qty = risk_amount / (ATR * multiplier) → fewer contracts when ATR is high
            atr_risk = atr * self.cfg.volatility_sizing_atr_mult
            if atr_risk > 0:
                vol_qty = risk_amount / atr_risk
                vol_notional = vol_qty * price
                # Cap by regular limits
                vol_notional = min(vol_notional, max_trade, remaining)
                vol_notional = max(2.0, vol_notional)  # min 2 USDT
                qty = vol_notional / price
                log.debug(
                    "volatility-adjusted sizing",
                    extra={
                        "price": price,
                        "atr": round(atr, 6),
                        "risk_amount": round(risk_amount, 4),
                        "vol_notional": round(vol_notional, 4),
                        "qty": qty,
                        "balance": self._current_balance,
                        "risk_state": self._state.value,
                    },
                )
                return qty

        # Fallback: fraction-based sizing
        fraction_notional = self._current_balance * self.cfg.per_trade_fraction
        # Ensure minimum viable notional (at least 2 USDT)
        min_notional = 2.0
        notional = max(min_notional, fraction_notional)
        notional = min(notional, max_trade, remaining)

        if notional <= 0 or price <= 0:
            return 0.0

        qty = notional / price
        log.debug(
            "position size computed",
            extra={
                "price": price,
                "notional": round(notional, 4),
                "qty": qty,
                "balance": self._current_balance,
                "remaining_capacity": round(remaining, 2),
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
            "max_trade_notional": self.get_max_trade_notional(),
            "max_total_notional": self.get_max_total_notional(),
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
