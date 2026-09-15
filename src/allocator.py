"""Allocation & risk engine.

WHY THIS EXISTS
---------------
Every directional hypothesis in this project was tested and failed (see
FINDINGS.md): indicator confluence across 6 clusters at horizons from 30 minutes
to 14 days (no |t| > 2 anywhere), moving-average trend filters at 1h and 1d (all
lost to buy-and-hold once switching costs were charged), and shock-reversion
(strong in year one, gone in year two).

What survived measurement was holding, and holding with discipline beat holding
alone. So this engine does not predict direction. It answers three mechanical
questions instead:

  1. WHAT to hold      -> a fixed target basket
  2. WHEN to rebalance -> on schedule, or when a weight drifts far from target
  3. WHEN to stand aside -> when portfolio drawdown breaches a limit

Rebalancing is how sharp moves get harvested without forecasting them: a coin
that spikes is sold back to target, one that collapses is bought back up. The
drawdown brake is what turns a -64% peak-to-trough hold into something a person
can actually stay invested through.

Measured on 6 majors, 2 years, costs charged (research/):

    buy and hold                     +43.5%   (max drawdown -64%)
    monthly rebalance                +58.6%
    monthly + 20% drawdown brake     +96.6%   (bad year -8% instead of -38%)

The engine is deliberately pure: it takes prices and holdings and returns
intents. No I/O, no exchange calls, so it can be replayed over history exactly
as it runs live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable

from src.config import Settings
from src.logger import get_logger

log = get_logger(__name__)


class Stance(str, Enum):
    """Whether the book is invested or parked in stables."""
    INVESTED = "INVESTED"
    DEFENSIVE = "DEFENSIVE"


class RebalanceReason(str, Enum):
    SCHEDULED = "scheduled"       # calendar cadence reached
    DRIFT = "drift"               # a weight moved too far from target
    DE_RISK = "de_risk"           # drawdown brake tripped: exit to stables
    RE_ENTER = "re_enter"         # recovered enough to go back in
    INITIAL = "initial"           # first deployment of capital


@dataclass(frozen=True)
class TradeIntent:
    """A desired change in one symbol, in quote currency (USDT).

    Positive delta = buy. The engine never emits leverage or short intents:
    the evidence supports holding a basket, not expressing a directional view.
    """
    symbol: str
    delta_quote: float
    target_quote: float
    current_quote: float

    @property
    def side(self) -> str:
        return "BUY" if self.delta_quote > 0 else "SELL"


@dataclass
class AllocationDecision:
    rebalance: bool
    reason: RebalanceReason | None
    stance: Stance
    intents: list[TradeIntent] = field(default_factory=list)
    turnover_quote: float = 0.0
    drawdown_pct: float = 0.0
    note: str = ""

    @property
    def is_noop(self) -> bool:
        return not self.rebalance or not self.intents


@dataclass
class AllocatorState:
    """Everything the engine must remember between cycles.

    Persisted rather than recomputed so a restart cannot silently reset the
    drawdown brake — the peak has to survive process death, otherwise every
    restart re-arms the brake at the current (already depressed) equity.
    """
    stance: Stance = Stance.INVESTED
    peak_equity: float = 0.0
    # Low of the MARKET index since going defensive — deliberately not equity.
    # Equity is frozen while the book sits in stables, so an equity-based
    # recovery test can never fire and the brake becomes a one-way door: sell
    # once, hold cash forever. Re-entry has to watch prices, not the balance.
    defensive_index_low: float = 0.0
    last_rebalance_at: datetime | None = None
    deployed: bool = False


class Allocator:
    """Decides what the book should hold. Stateless w.r.t. the exchange."""

    def __init__(self, cfg: Settings, state: AllocatorState | None = None) -> None:
        self.cfg = cfg
        self.state = state or AllocatorState()

    # ── targets ────────────────────────────────────────────────

    def target_weights(self) -> dict[str, float]:
        """Parse the configured basket into normalised target weights.

        Accepts either ``"BTC-USDT,ETH-USDT"`` (equal weight) or explicit
        weights, ``"BTC-USDT:0.7,ETH-USDT:0.3"``. Weights are normalised, so
        they need not sum to 1.

        Basket membership is a deliberate CONFIGURATION decision, never a model
        output, and that is a finding rather than a simplification. Over two
        years of measurement:

          * which assets you hold dominated every timing rule tested
            (6 majors +58.6% vs 12 coins +13.5%)
          * but no mechanical selection rule beat simply holding everything:
            momentum (hold recent winners) and contrarian (hold recent losers)
            were tested at 30/90/180-day lookbacks holding 3 or 5 names — all
            twelve variants LOST to the passive basket, several badly (-58%).

        So selection cannot be automated from price data, and any basket that
        looks clever is probably hindsight. The default below is deliberately
        the boring, obvious choice that required no foresight to pick in 2024.
        """
        targets: dict[str, float] = {}
        for part in self.cfg.allocation_basket.split(","):
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                sym, _, raw = part.partition(":")
                try:
                    weight = float(raw)
                except ValueError:
                    log.warning("bad basket weight, skipping", extra={"entry": part})
                    continue
            else:
                sym, weight = part, 1.0
            sym = sym.strip()
            if sym and weight > 0:
                targets[sym] = targets.get(sym, 0.0) + weight

        total = sum(targets.values())
        if total <= 0:
            return {}
        return {s: w / total for s, w in targets.items()}

    # ── main entry point ───────────────────────────────────────

    def decide(
        self,
        equity_quote: float,
        holdings_quote: dict[str, float],
        market_index: float,
        now: datetime | None = None,
    ) -> AllocationDecision:
        """Given current equity and per-symbol value, decide what to do.

        ``holdings_quote`` is the market value of each held symbol in USDT;
        anything not listed is treated as zero. Cash is implied by
        ``equity_quote - sum(holdings)``.

        ``market_index`` is a price level for the basket (any consistent scale,
        e.g. an equal-weight index). It exists because re-entry must be judged
        on the MARKET, not on equity: while defensive the book is in stables and
        its equity does not move, so an equity-based recovery test can never
        become true.
        """
        now = now or datetime.now(timezone.utc)
        targets = self.target_weights()
        if equity_quote <= 0 or not targets:
            return AllocationDecision(False, None, self.state.stance, note="no equity or empty basket")

        # Peak tracks the high-water mark of equity, which is what the brake
        # measures against. It must never be reset by a restart.
        self.state.peak_equity = max(self.state.peak_equity, equity_quote)
        drawdown = (equity_quote / self.state.peak_equity) - 1.0 if self.state.peak_equity > 0 else 0.0
        dd_pct = -drawdown * 100.0

        # ── defensive: wait for the MARKET to recover before redeploying ──
        if self.state.stance is Stance.DEFENSIVE:
            if market_index > 0:
                self.state.defensive_index_low = min(
                    self.state.defensive_index_low or market_index, market_index
                )
            low = self.state.defensive_index_low or market_index
            recovery = (market_index / low - 1.0) * 100.0 if low > 0 else 0.0
            if recovery >= self.cfg.reentry_recovery_pct:
                log.info(
                    "re-entering: market recovered off the low",
                    extra={"recovery_pct": round(recovery, 2), "index": market_index},
                )
                return self._rebalance_to_target(
                    equity_quote, holdings_quote, targets, now,
                    RebalanceReason.RE_ENTER, Stance.INVESTED, dd_pct,
                    note=f"market recovered {recovery:.1f}% off the low",
                )
            return AllocationDecision(
                False, None, Stance.DEFENSIVE, drawdown_pct=dd_pct,
                note=f"defensive; market {recovery:.1f}% off low, "
                     f"need {self.cfg.reentry_recovery_pct:.1f}%",
            )

        # ── drawdown brake ────────────────────────────────────
        if self.cfg.max_portfolio_drawdown_pct > 0 and dd_pct >= self.cfg.max_portfolio_drawdown_pct:
            # Sell everything. This is the one place the engine reduces exposure
            # on price action alone, and it is a risk limit, not a forecast.
            intents = [
                TradeIntent(sym, -val, 0.0, val)
                for sym, val in sorted(holdings_quote.items()) if val > 0
            ]
            self.state.stance = Stance.DEFENSIVE
            self.state.defensive_index_low = market_index
            self.state.last_rebalance_at = now
            log.warning(
                "drawdown brake tripped: moving to stables",
                extra={"drawdown_pct": round(dd_pct, 2),
                       "limit_pct": self.cfg.max_portfolio_drawdown_pct,
                       "equity": round(equity_quote, 2)},
            )
            return AllocationDecision(
                True, RebalanceReason.DE_RISK, Stance.DEFENSIVE, intents,
                turnover_quote=sum(abs(i.delta_quote) for i in intents),
                drawdown_pct=dd_pct,
                note=f"drawdown {dd_pct:.1f}% >= {self.cfg.max_portfolio_drawdown_pct:.1f}%",
            )

        # ── first deployment ──────────────────────────────────
        if not self.state.deployed:
            return self._rebalance_to_target(
                equity_quote, holdings_quote, targets, now,
                RebalanceReason.INITIAL, Stance.INVESTED, dd_pct, note="initial deployment",
            )

        # ── scheduled cadence ─────────────────────────────────
        due = False
        if self.cfg.rebalance_days > 0 and self.state.last_rebalance_at is not None:
            due = now - self.state.last_rebalance_at >= timedelta(days=self.cfg.rebalance_days)

        # ── drift trigger: this is the "sharp move" response ──
        # A coin that rips is trimmed back to target; one that collapses is
        # topped up. No forecast is involved — only the distance from target.
        worst_drift = 0.0
        for sym, tgt_w in targets.items():
            cur_w = holdings_quote.get(sym, 0.0) / equity_quote
            if tgt_w > 0:
                worst_drift = max(worst_drift, abs(cur_w - tgt_w) / tgt_w)
        drifted = self.cfg.rebalance_drift_pct > 0 and worst_drift * 100.0 >= self.cfg.rebalance_drift_pct

        if not (due or drifted):
            return AllocationDecision(
                False, None, Stance.INVESTED, drawdown_pct=dd_pct,
                note=f"in tolerance (worst drift {worst_drift*100:.1f}%)",
            )

        return self._rebalance_to_target(
            equity_quote, holdings_quote, targets, now,
            RebalanceReason.SCHEDULED if due else RebalanceReason.DRIFT,
            Stance.INVESTED, dd_pct,
            note=f"worst drift {worst_drift*100:.1f}%",
        )

    # ── helpers ────────────────────────────────────────────────

    def _rebalance_to_target(
        self,
        equity: float,
        holdings: dict[str, float],
        targets: dict[str, float],
        now: datetime,
        reason: RebalanceReason,
        stance: Stance,
        dd_pct: float,
        note: str = "",
    ) -> AllocationDecision:
        intents: list[TradeIntent] = []
        symbols: Iterable[str] = sorted(set(targets) | set(holdings))
        for sym in symbols:
            cur = holdings.get(sym, 0.0)
            tgt = equity * targets.get(sym, 0.0)
            delta = tgt - cur
            # Skip dust: a trade smaller than this costs more in fees and
            # minimum-size rejections than the tracking error it removes.
            if abs(delta) < max(self.cfg.min_rebalance_trade_quote, equity * 1e-4):
                continue
            intents.append(TradeIntent(sym, delta, tgt, cur))

        self.state.stance = stance
        self.state.deployed = True
        self.state.last_rebalance_at = now
        if stance is Stance.INVESTED:
            self.state.defensive_index_low = 0.0
            if reason is RebalanceReason.RE_ENTER:
                # Re-arm the brake against the equity we are re-entering with.
                # Leaving the old high-water mark in place would leave drawdown
                # still past the limit on the very next cycle, so the brake would
                # fire again immediately and the book would oscillate between
                # stables and the basket, paying fees each way.
                self.state.peak_equity = equity

        return AllocationDecision(
            bool(intents), reason if intents else None, stance, intents,
            turnover_quote=sum(abs(i.delta_quote) for i in intents),
            drawdown_pct=dd_pct, note=note,
        )
