"""Tests for the transaction cost model.

Several of these pin the arithmetic that decided the whole BIST study, so that
a future change to a rate cannot silently flip a conclusion.
"""

from __future__ import annotations

import pytest

from src.costs import (
    BIST_MAINSTREAM,
    BINGX_PERP,
    MIDAS,
    VIOP_SINGLE_STOCK,
    CostProfile,
    bist_min_spread_frac,
    bist_tick,
    get_profile,
    verdict,
)


class TestTickTable:
    @pytest.mark.parametrize("price,step", [
        (5.0, 0.01), (19.99, 0.01), (20.0, 0.02), (49.98, 0.02),
        (50.0, 0.05), (99.95, 0.05), (100.0, 0.10), (350.0, 0.10),
    ])
    def test_steps(self, price, step) -> None:
        assert bist_tick(price) == step

    def test_relative_tick_shrinks_with_price(self) -> None:
        """A low-priced share has a structurally wider spread floor. This is why
        the overnight study had to be run per liquidity decile."""
        assert bist_min_spread_frac(5.0) > bist_min_spread_frac(300.0)

    def test_zero_price_is_not_a_division_error(self) -> None:
        assert bist_min_spread_frac(0.0) == 0.0


class TestRoundTrip:
    def test_bsmv_is_levied_on_commission_not_notional(self) -> None:
        """0.195% per side, twice, plus 5% of that — not 5% of the trade."""
        c = BIST_MAINSTREAM.round_trip(spread_frac=0.0)
        assert c == pytest.approx(2 * 0.00195 * 1.05 + 0.0002)
        assert c < 0.005          # nowhere near 5% of notional

    def test_spread_is_charged_in_full_not_halved(self) -> None:
        """One half-spread to get in, one to get out."""
        base = MIDAS.round_trip(0.0)
        assert MIDAS.round_trip(0.002) == pytest.approx(base + 0.002)

    def test_borrow_only_applies_to_shorts(self) -> None:
        p = CostProfile(name="x", borrow_cost_per_day=0.001)
        assert p.round_trip(hold_days=10, is_short=False) == \
            pytest.approx(p.round_trip(hold_days=0, is_short=False))
        assert p.round_trip(hold_days=10, is_short=True) > p.round_trip(is_short=True)

    def test_midas_is_the_cheapest_bist_route(self) -> None:
        s = bist_min_spread_frac(100.0)
        assert MIDAS.round_trip(s) < VIOP_SINGLE_STOCK.round_trip(s) \
            < BIST_MAINSTREAM.round_trip(s)


class TestRequiredWinRate:
    def test_the_formula_that_governs_the_project(self) -> None:
        """required = 0.5 + cost/(2*target) for a symmetric bracket."""
        p = CostProfile(name="flat", commission_per_side=0.001,
                        bsmv_on_commission=0.0, exchange_fees_round_trip=0.0)
        cost = p.round_trip()                       # 0.2%
        assert p.required_win_rate(0.01) == pytest.approx(0.5 + cost / 0.02)
        assert p.required_win_rate(0.01) == pytest.approx(0.60)

    def test_bigger_targets_need_less_accuracy(self) -> None:
        """The reason patient holding beats scalping with the SAME signal."""
        p = BIST_MAINSTREAM
        rates = [p.required_win_rate(t) for t in (0.01, 0.02, 0.05, 0.10, 0.30)]
        assert rates == sorted(rates, reverse=True)
        assert rates[0] > 0.68            # 1% target: nearly 70% accuracy
        assert rates[-1] < 0.52           # 30% target: barely above a coin flip

    def test_zero_cost_needs_exactly_a_coin_flip(self) -> None:
        free = CostProfile(name="free", bsmv_on_commission=0.0,
                           exchange_fees_round_trip=0.0)
        assert free.required_win_rate(0.01) == pytest.approx(0.5)


class TestMeasuredEdgesAgainstRealBrokers:
    """The BIST study's actual numbers, pinned. If a rate below is edited and
    one of these flips, the synthesis document is out of date."""

    GAP_FADE_GROSS = 0.00413          # short gap-ups, at the opening print
    GAP_FADE_AT_1H = 0.00054          # the same rule one hour later
    OVERNIGHT_LIQUID = 0.002287       # most liquid decile, per day
    BEST_LITERATURE_RULE = 0.00056    # opening-range breakout, gross

    def test_gap_fade_clears_only_at_zero_commission(self) -> None:
        s = bist_min_spread_frac(100.0)
        assert self.GAP_FADE_GROSS > MIDAS.round_trip(s)
        assert self.GAP_FADE_GROSS < BIST_MAINSTREAM.round_trip(s)

    def test_gap_fade_one_hour_later_clears_nothing(self) -> None:
        """87% of the edge is gone by then, which is what killed it."""
        s = bist_min_spread_frac(100.0)
        for p in (MIDAS, BIST_MAINSTREAM, VIOP_SINGLE_STOCK):
            assert self.GAP_FADE_AT_1H < p.round_trip(s)

    def test_overnight_loses_even_at_one_tick_and_zero_commission(self) -> None:
        """The most liquid decile still pays 0.302% of spread against a
        0.229% gross return — this is the result that closed the study."""
        assert self.OVERNIGHT_LIQUID < MIDAS.round_trip(0.00302)

    def test_no_literature_rule_clears_any_broker(self) -> None:
        s = bist_min_spread_frac(100.0)
        for p in (MIDAS, BIST_MAINSTREAM, VIOP_SINGLE_STOCK):
            assert self.BEST_LITERATURE_RULE < p.round_trip(s)


class TestProfileRegistry:
    def test_lookup(self) -> None:
        assert get_profile("midas") is MIDAS

    def test_unknown_profile_names_the_alternatives(self) -> None:
        with pytest.raises(ValueError, match="known:"):
            get_profile("nope")

    def test_crypto_profile_has_no_bsmv(self) -> None:
        assert BINGX_PERP.bsmv_on_commission == 0.0

    def test_verdict_line_states_the_outcome(self) -> None:
        assert "CLEARS" in verdict(0.05, MIDAS)
        assert "fails" in verdict(0.0001, BIST_MAINSTREAM)
