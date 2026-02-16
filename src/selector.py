"""Tradeable symbol selector – multi-stage filter pipeline.

Pipeline:
  1. Pre-filter   → active contract with valid metadata
  2. Shortlist    → top N by basic price/mark data availability
  3. Final filter → spread, depth, volatility checks using order book
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.config import Settings
from src.logger import get_logger
from src.marketdata import MarketData, SymbolSnapshot
from src.universe import Universe

log = get_logger(__name__)


@dataclass
class FilterStats:
    """Diagnostics for each scan cycle."""
    universe_size: int = 0
    prefiltered: int = 0
    shortlisted: int = 0
    tradeable: int = 0
    rejected_spread: int = 0
    rejected_depth: int = 0
    rejected_vol: int = 0


class Selector:
    """Selects tradeable symbols from the universe."""

    def __init__(self, cfg: Settings, universe: Universe, market: MarketData) -> None:
        self.cfg = cfg
        self.universe = universe
        self.market = market

    async def select(self, risk_state: str = "NORMAL") -> tuple[list[SymbolSnapshot], FilterStats]:
        """Run the full selection pipeline.

        Args:
            risk_state: Current risk state – ULTRA_TIGHT narrows to top 10 liquid.

        Returns:
            Tuple of (tradeable snapshots, filter stats).
        """
        stats = FilterStats()
        stats.universe_size = self.universe.size

        # ── Stage 1: Pre-filter ─────────────────────────────────
        candidates = self._prefilter()
        stats.prefiltered = len(candidates)
        log.debug("prefilter done", extra={"count": len(candidates)})

        if not candidates:
            return [], stats

        # ── Stage 2: Shortlist (price data, top N) ──────────────
        shortlist = candidates[: self.cfg.shortlist_size]
        stats.shortlisted = len(shortlist)

        # ULTRA_TIGHT: still allow a reasonable pool (top 50 instead of 10)
        if risk_state == "ULTRA_TIGHT":
            shortlist = shortlist[:50]
            stats.shortlisted = len(shortlist)
        elif risk_state == "TIGHT":
            shortlist = shortlist[:200]
            stats.shortlisted = len(shortlist)

        # ── Stage 3: Fetch depth + final filter ─────────────────
        snapshots = await self.market.batch_snapshots(shortlist, fetch_depth=True, concurrency=5)

        tradeable: list[SymbolSnapshot] = []
        for snap in snapshots:
            if snap.mid_price <= 0:
                continue

            # Spread check
            if snap.spread_bps > self.cfg.max_spread_bps:
                stats.rejected_spread += 1
                continue

            # Depth check (use the smaller side)
            min_depth = min(snap.bid_depth_usdt, snap.ask_depth_usdt)
            if min_depth < self.cfg.min_depth_usdt:
                stats.rejected_depth += 1
                continue

            # Volatility guard (z_score as proxy)
            if abs(snap.z_score_bps) > self.cfg.vol_guard_bps:
                stats.rejected_vol += 1
                continue

            tradeable.append(snap)

        stats.tradeable = len(tradeable)
        log.info(
            "selection complete",
            extra={
                "universe": stats.universe_size,
                "prefiltered": stats.prefiltered,
                "shortlisted": stats.shortlisted,
                "tradeable": stats.tradeable,
                "rej_spread": stats.rejected_spread,
                "rej_depth": stats.rejected_depth,
                "rej_vol": stats.rejected_vol,
            },
        )
        return tradeable, stats

    def _prefilter(self) -> list[str]:
        """Stage 1: filter to active contracts with valid metadata."""
        result: list[str] = []
        for symbol in self.universe.symbols:
            contract = self.universe.get_contract(symbol)
            if contract is None:
                continue
            # Must have tick_size and step_size
            if not contract.get("tick_size") or not contract.get("step_size"):
                continue
            # All contract types accepted (crypto, commodities, etc.)
            result.append(symbol)
        return result
