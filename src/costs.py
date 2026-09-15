"""Transaction cost models, as first-class configuration.

WHY THIS IS ITS OWN MODULE
--------------------------
Every conclusion in RESEARCH-SYNTHESIS.md is governed by cost, not by signal
quality. The measured gross edges on BIST ranged from 4 to 56 basis points per
trade; the round trip ranges from 6 basis points at a zero-commission broker to
50 at a mainstream one. The same rule is profitable at one broker and ruinous
at another, so the cost assumption cannot live as a scattered constant — it has
to be a named, swappable object you can point at a real brokerage statement.

The governing relationship, which this module exists to evaluate:

    required win rate = 0.5 + cost / (2 * target)

A 1% target at a 0.41% round trip needs 70.5% accuracy. Measured intraday
direction accuracy on BIST was 46.5%. That single line is the whole project.

RATES ARE NOT FACTS
-------------------
The Turkish figures below were researched on 2026-09-07 from broker tariff
pages and are the *published retail* rates. They change, they vary by volume
tier, and the spread component is an ESTIMATE from the tick table rather than a
measurement. Anyone acting on these should replace them with numbers read off
their own contract note — which is exactly why they are parameters.
"""

from __future__ import annotations

from dataclasses import dataclass

# Borsa İstanbul price-step table (tiered since December 2024). The tick is a
# HARD FLOOR under the spread: no quote can be tighter than one step, so
# tick/price is the most optimistic spread assumption available and is what the
# research uses. Real spreads on mid caps run several ticks wide.
BIST_TICK_TABLE: tuple[tuple[float, float], ...] = (
    (20.0, 0.01),
    (50.0, 0.02),
    (100.0, 0.05),
    (float("inf"), 0.10),
)


def bist_tick(price: float) -> float:
    """Minimum price step for a BIST equity at ``price`` lira."""
    for ceiling, step in BIST_TICK_TABLE:
        if price < ceiling:
            return step
    return BIST_TICK_TABLE[-1][1]


def bist_min_spread_frac(price: float) -> float:
    """One tick as a fraction of price — the floor on the quoted spread."""
    return bist_tick(price) / price if price > 0 else 0.0


@dataclass(frozen=True)
class CostProfile:
    """What one open-and-close cycle actually costs, as a fraction of notional.

    ``commission_per_side`` and ``bsmv_on_commission`` are contractual and known.
    ``spread_frac`` is the part you cannot look up: it is the price you pay for
    demanding immediacy, and it is zero only if you are the one providing it.
    """

    name: str
    commission_per_side: float = 0.0     # fraction of notional, per side
    bsmv_on_commission: float = 0.05     # Turkish 5% levy ON THE COMMISSION
    exchange_fees_round_trip: float = 0.0002
    borrow_cost_per_day: float = 0.0     # short leg only; unknown for ÖPSP
    note: str = ""

    def round_trip(self, spread_frac: float = 0.0, hold_days: float = 0.0,
                   is_short: bool = False) -> float:
        """Total cost of a round trip, as a fraction of notional.

        ``spread_frac`` is charged in FULL, not halved: crossing to get in and
        crossing to get out costs one half-spread each. Passing
        ``bist_min_spread_frac(price)`` gives the optimistic floor.
        """
        commission = 2.0 * self.commission_per_side * (1.0 + self.bsmv_on_commission)
        borrow = self.borrow_cost_per_day * hold_days if is_short else 0.0
        return commission + self.exchange_fees_round_trip + spread_frac + borrow

    def required_win_rate(self, target: float, stop: float | None = None,
                          spread_frac: float = 0.0) -> float:
        """Break-even accuracy for a ``target``/``stop`` bracket after cost.

        With a symmetric bracket this reduces to 0.5 + cost/(2*target), which is
        the reason small targets are structurally unprofitable: the numerator is
        fixed by your broker and the denominator is your choice.
        """
        stop = target if stop is None else stop
        c = self.round_trip(spread_frac)
        net_win, net_loss = target - c, stop + c
        total = net_win + net_loss
        return net_loss / total if total > 0 else 1.0

    def breakeven_edge(self, spread_frac: float = 0.0) -> float:
        """Gross edge per trade a strategy must clear just to break even."""
        return self.round_trip(spread_frac)


# ── Turkish retail profiles, researched 2026-09-07 ────────────────
# Sources: broker tariff pages (Garanti BBVA Yatırım, İnfo Yatırım, Ziraat
# Yatırım, Midas) and Borsa İstanbul's published fee schedule.

MIDAS = CostProfile(
    name="midas",
    commission_per_side=0.0,
    exchange_fees_round_trip=0.0,
    note="0% commission on BIST equities; states it absorbs MKK/BIST fees. "
         "No short selling and no order-entry API, so it is cheap for a "
         "long-only, manually-executed book only.",
)

BIST_MAINSTREAM = CostProfile(
    name="bist_mainstream",
    commission_per_side=0.00195,
    note="Garanti BBVA Yatırım published fixed rate; İnfo Yatırım is 0.20%, "
         "Ziraat from 0.15%. ~0.41% round trip before spread.",
)

BIST_CHEAP = CostProfile(
    name="bist_cheap",
    commission_per_side=0.0015,
    note="Best mainstream online tariff found (Ziraat Yatırım).",
)

VIOP_SINGLE_STOCK = CostProfile(
    name="viop_single_stock",
    commission_per_side=0.00084,
    exchange_fees_round_trip=0.00008,
    note="Single-stock futures: no borrow, no uptick rule, and it survived the "
         "2026 cash-equity short ban. Liquidity per contract is UNVERIFIED and "
         "the effective spread is likely wider than the cash market.",
)

# The crypto venue the original bot traded, for comparison on the same scale.
BINGX_PERP = CostProfile(
    name="bingx_perp",
    commission_per_side=0.0005,
    bsmv_on_commission=0.0,
    exchange_fees_round_trip=0.0,
    note="Taker both sides; entries are MARKET and SL/TP are STOP_MARKET. "
         "Add ~8bps slippage per side and funding while held.",
)

PROFILES: dict[str, CostProfile] = {
    p.name: p for p in
    (MIDAS, BIST_CHEAP, BIST_MAINSTREAM, VIOP_SINGLE_STOCK, BINGX_PERP)
}


def get_profile(name: str) -> CostProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(
            f"unknown cost profile {name!r}; known: {sorted(PROFILES)}"
        ) from None


def verdict(gross_edge: float, profile: CostProfile, price: float = 100.0,
            spread_ticks: float = 1.0) -> str:
    """One line on whether a measured gross edge clears a broker's toll.

    ``spread_ticks`` defaults to 1 — the theoretical floor. If a rule fails at
    one tick it cannot be rescued by better execution, which is the finding
    that closed out the BIST study.
    """
    spread = bist_min_spread_frac(price) * spread_ticks
    cost = profile.round_trip(spread)
    net = gross_edge - cost
    return (f"{profile.name:18s} cost {cost*100:6.3f}%  "
            f"gross {gross_edge*100:+6.3f}%  net {net*100:+7.3f}%  "
            f"{'CLEARS' if net > 0 else 'fails'}")


# ── measured gross edges, from research/bist/FINDINGS-BIST.md ─────
# Kept here so the decision table below cannot drift from the study.
MEASURED_EDGES: tuple[tuple[str, float], ...] = (
    ("gap fade, at the opening print", 0.00413),
    ("gap fade, entered +1 hour later", 0.00054),
    ("overnight, most liquid decile", 0.002287),
    ("opening range breakout (lit.)", 0.00056),
    ("intraday periodicity (lit.)", 0.000427),
    ("buy +1% target, no signal", -0.0010),
)


def _main() -> None:
    """Decision table: does a measured edge survive a given broker?

    Usage:
        python -m src.costs                    # all researched profiles
        python -m src.costs 0.10               # your own commission, % per side
        python -m src.costs 0.10 350           # ... and your typical share price
    """
    import sys

    price = float(sys.argv[2]) if len(sys.argv) > 2 else 100.0
    if len(sys.argv) > 1:
        own = CostProfile(name=f"yours({sys.argv[1]}%/side)",
                          commission_per_side=float(sys.argv[1]) / 100)
        profiles = [own, MIDAS, BIST_MAINSTREAM]
    else:
        profiles = [MIDAS, BIST_CHEAP, BIST_MAINSTREAM, VIOP_SINGLE_STOCK]

    spread = bist_min_spread_frac(price)
    print(f"share price {price:.2f} TL -> one tick = {spread*100:.3f}% "
          f"(the FLOOR on the spread; real quotes are wider)\n")
    print(f"{'profile':26s} {'round trip':>11s}   " +
          "  ".join(f"{n[:14]:>14s}" for n, _ in MEASURED_EDGES[:3]))
    print("-" * 78)
    for p in profiles:
        cost = p.round_trip(spread)
        cells = "  ".join(
            f"{(e - cost)*100:+13.3f}%" for _, e in MEASURED_EDGES[:3])
        print(f"{p.name:26s} {cost*100:10.3f}%  {cells}")

    print(f"\nrequired accuracy by profit target (spread at {price:.0f} TL):")
    print(f"  {'profile':26s} " + "  ".join(f"{t:>7s}" for t in
                                            ("1%", "2%", "5%", "10%", "30%")))
    for p in profiles:
        row = "  ".join(f"{p.required_win_rate(t, spread_frac=spread)*100:6.1f}%"
                        for t in (0.01, 0.02, 0.05, 0.10, 0.30))
        print(f"  {p.name:26s} {row}")
    print("\n  measured BIST intraday direction accuracy: 46.5%")


if __name__ == "__main__":
    _main()
