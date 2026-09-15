"""The barbell: a defined-loss structure for a high-multiple goal.

THE PROBLEM THIS SOLVES
-----------------------
The stated goal was 100,000 TL to 1,000,000 TL in nine months — a 10x, which is
29.2% per month compounded. Measured against 3,040 overlapping nine-month
windows of actual Turkish-lira prices, 2017-2026:

  * Unleveraged, BTC reached 10x in ZERO windows. Its best nine months were
    +529%. ETH managed it in 3 windows out of 3,040 (0.10%).
  * With leverage on BTC, daily marked to market as a perpetual actually is:

        leverage   reached 10x   LIQUIDATED   median outcome
              3x          9.6%         6.3%            1.15x
              5x         13.0%        27.5%            0.33x
             10x          4.8%        76.9%            0.00x
             20x          0.0%       100.0%            0.00x

    5x gives the best shot at the target and still ends with the median
    investor down 67%. 10x is WORSE than 5x at the same goal because the
    liquidation probability grows faster than the payoff.

So the goal cannot be reached reliably. What CAN be built is a structure where
the downside is known in advance and the upside is still live.

THE STRUCTURE
-------------
Put enough in a lira time deposit that its interest alone covers the entire
risk leg, then lever the remainder hard.

    deposit 80,000 TL at 37% for 9 months  ->  101,305 TL
    risk     20,000 TL at 5x on BTC        ->  0 to ~1,100,000 TL

The deposit leg's arithmetic is the whole point: 0.8 x 1.266 = 1.013. Even if
the risk leg goes to zero, nine months end above the starting capital. Across
all 3,040 historical windows the worst outcome was 101,305 TL, and the
probability of finishing below 100,000 TL was 0.0%.

    allocation          median      5th pct     95th pct   reached 1m   below start
    90/10 at 5x        117,293      113,968      662,545        2.86%          0.0%
    80/20 at 5x        107,955      101,305    1,198,459        5.59%          0.0%
    70/30 at 5x         98,616       88,642    1,734,374        7.40%         51.3%

80/20 is the last allocation where the deposit still covers the risk leg. At
70/30 the probability of ending below the starting capital jumps to 51.3% — a
coin flip — for 1.8 percentage points more upside. That is the line.

WHAT THIS IS NOT
----------------
The 5.59% is a count of what happened in a period when BTC compounded at 47% a
year. It is not a forecast. If the next nine months are flat, that number is
near zero. The structure does not depend on the estimate — the deposit leg is
contractual and the risk leg is capped — but the probability attached to the
upside very much does.

Nothing in this project predicts direction. Fifty-odd rules were tested across
two markets and none survived its own costs. This module does not try; it only
shapes the distribution of outcomes so the bad tail is survivable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BarbellPlan:
    """A concrete, sized plan. Every number is derivable from the four inputs."""

    capital: float = 100_000.0
    horizon_months: float = 9.0
    deposit_rate: float = 0.37          # TCMB policy rate, Sept 2026
    deposit_tax: float = 0.05           # withholding on a 1-year TL deposit
    leverage: float = 5.0               # 3x survives more; 10x is strictly worse
    risk_fraction: float = 0.20         # the line where the deposit still covers it

    @property
    def deposit(self) -> float:
        return self.capital * (1.0 - self.risk_fraction)

    @property
    def risk_capital(self) -> float:
        return self.capital * self.risk_fraction

    @property
    def net_deposit_rate(self) -> float:
        return self.deposit_rate * (1.0 - self.deposit_tax)

    @property
    def deposit_value_at_horizon(self) -> float:
        return self.deposit * (1.0 + self.net_deposit_rate) ** (self.horizon_months / 12.0)

    @property
    def floor(self) -> float:
        """Worst case: the risk leg is liquidated in full."""
        return self.deposit_value_at_horizon

    @property
    def covers_the_risk_leg(self) -> bool:
        """The property the whole structure rests on."""
        return self.floor >= self.capital

    @property
    def liquidation_move(self) -> float:
        """Adverse move in the underlying that wipes the risk leg out.

        At 5x this is 20%. BTC has moved 20% against a position inside a week
        more than once in the sample, so this is not a remote scenario — it is
        the expected cost of the structure, which is exactly why it is sized to
        be affordable.
        """
        return 1.0 / self.leverage

    def upside(self, underlying_return: float) -> float:
        """Total value if the underlying moves by ``underlying_return``.

        Linear in the underlying above liquidation, floored below it. Real
        perpetual futures also bleed funding, which is modelled in the research
        script but deliberately not here — this is the plan, not the simulator.
        """
        if underlying_return <= -self.liquidation_move:
            levered = 0.0
        else:
            levered = self.risk_capital * (1.0 + self.leverage * underlying_return)
        return self.deposit_value_at_horizon + levered

    def summary(self) -> str:
        lines = [
            f"capital {self.capital:,.0f} TL over {self.horizon_months:.0f} months",
            f"  deposit    {self.deposit:,.0f} TL at {self.deposit_rate*100:.0f}% "
            f"(net {self.net_deposit_rate*100:.1f}% after withholding)",
            f"             -> {self.deposit_value_at_horizon:,.0f} TL at horizon",
            f"  risk leg   {self.risk_capital:,.0f} TL at {self.leverage:.0f}x",
            f"             wiped out by a {self.liquidation_move*100:.0f}% adverse move",
            "",
            f"  FLOOR      {self.floor:,.0f} TL "
            f"({'above' if self.covers_the_risk_leg else 'BELOW'} starting capital)",
        ]
        for move in (-0.20, 0.0, 0.50, 1.00, 2.00, 4.00):
            lines.append(f"  BTC {move*100:+5.0f}%  ->  {self.upside(move):12,.0f} TL")
        return "\n".join(lines)


def largest_safe_risk_fraction(capital: float = 100_000.0,
                               horizon_months: float = 9.0,
                               deposit_rate: float = 0.37,
                               deposit_tax: float = 0.05) -> float:
    """The most you can put at risk while the deposit still covers it.

    Solves ``(1-f) * (1+r_net)^t >= 1`` for f. At 37% gross, 5% withholding and
    nine months that is 0.201 — which is where the 20% in the default plan
    comes from. It is not a preference, it is the constraint.
    """
    net = deposit_rate * (1.0 - deposit_tax)
    growth = (1.0 + net) ** (horizon_months / 12.0)
    return max(0.0, 1.0 - 1.0 / growth)


if __name__ == "__main__":
    plan = BarbellPlan()
    print(plan.summary())
    f = largest_safe_risk_fraction()
    print(f"\nlargest risk fraction the deposit still covers: {f*100:.1f}%")
    print(f"plan uses {plan.risk_fraction*100:.0f}% — "
          f"{'within' if plan.risk_fraction <= f else 'ABOVE'} the constraint")
