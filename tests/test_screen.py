"""Tests for the portfolio selector.

Two groups matter most. `TestEligibility` pins the rejection rules, several of
which were written in response to a specific wrong answer the screen gave on
live data. `TestTurnoverControl` pins the incumbency margin, which is the only
thing stopping a low-turnover strategy from quietly becoming a high-turnover
one.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.bist.forensics import Flag, Forensics
from src.bist.screen import (
    TURKISH_CPI_YOY,
    Candidate,
    ScreenConfig,
    build_portfolio,
    explain,
    score,
)


def cand(tic: str, **over) -> Candidate:
    """A plainly investable company; override fields to make it not one."""
    base = dict(
        market_cap=5e10, turnover=1e8, pb=1.2, pe=8.0,
        roe_nominal=0.40, profit_margin=0.12, debt_to_equity=40.0,
        mom_12_1=0.20, forensics=Forensics(ticker=tic),
    )
    base.update(over)
    return Candidate(ticker=tic, **base)


def universe(n: int = 20, **over) -> list[Candidate]:
    """A field of similar companies, so one altered name stands out."""
    out = []
    for i in range(n):
        out.append(cand(f"CO{i:02d}", pb=1.0 + i * 0.1, pe=6.0 + i * 0.5,
                        roe_nominal=0.50 - i * 0.01, mom_12_1=0.30 - i * 0.02))
    out.extend(cand(t, **over) for t in over.pop("_extra", []))
    return out


class TestRealReturnOnEquity:
    def test_real_roe_subtracts_inflation(self) -> None:
        """The single highest-value line in the module: a Turkish company
        earning 7.6% while CPI runs 31.5% is destroying capital, and every
        screener ranking on nominal ROE calls it profitable."""
        c = cand("X", roe_nominal=0.076)
        assert c.roe_real == pytest.approx(0.076 - TURKISH_CPI_YOY)
        assert c.roe_real < 0

    def test_real_roe_is_nan_when_roe_is_unknown(self) -> None:
        assert np.isnan(cand("X", roe_nominal=float("nan")).roe_real)

    def test_earnings_yield_inverts_pe(self) -> None:
        assert cand("X", pe=10.0).earnings_yield == pytest.approx(0.10)

    def test_negative_pe_has_no_earnings_yield(self) -> None:
        assert np.isnan(cand("X", pe=-4.0).earnings_yield)


class TestEligibility:
    def _reason(self, c: Candidate, cfg: ScreenConfig | None = None) -> str:
        r = score(universe() + [c], cfg or ScreenConfig())
        return str(r[r.tic == c.ticker].iloc[0]["reject"])

    def test_forensic_veto_overrides_cheapness(self) -> None:
        """The whole point of the veto: a company can look like the best value
        on the board and still be uninvestable. GESAN screened cheap."""
        f = Forensics(ticker="BAD", flags=[Flag("PROFIT_NO_CASH", 3, ""),
                                           Flag("DEBT_SURGE", 3, "")])
        assert self._reason(cand("BAD", pb=0.2, pe=2.0, forensics=f)) == "forensic veto"

    def test_loss_making_is_rejected(self) -> None:
        assert self._reason(cand("X", roe_nominal=-0.05)) == "loss-making"

    def test_no_positive_earnings_is_rejected(self) -> None:
        assert self._reason(cand("X", pe=float("nan"))) == "no positive earnings"

    def test_too_small(self) -> None:
        assert self._reason(cand("X", market_cap=1e8)) == "too small"

    def test_illiquid(self) -> None:
        assert self._reason(cand("X", turnover=1e5)) == "illiquid"

    def test_implausible_price_to_book_is_a_data_check(self) -> None:
        """Not a valuation view. The first live run ranked ENKAI ninth on a
        reported P/B of 59.7, which is a units error, and percentile scoring
        lets one absurd value distort every rank around it."""
        assert self._reason(cand("X", pb=59.7)) == "implausible P/B"

    def test_financials_are_excluded_with_a_named_reason(self) -> None:
        f = Forensics(ticker="BANK", model_applies=False,
                      flags=[Flag("NOT_SCORED", 0, "")])
        assert "financial" in self._reason(cand("BANK", forensics=f))

    def test_negative_real_roe_is_NOT_a_veto(self) -> None:
        """Requiring nominal ROE above 31.5% inflation is the economically
        correct test and leaves 3 eligible names out of 142. Real ROE therefore
        ranks inside quality rather than vetoing, or the universe is empty."""
        c = cand("X", roe_nominal=0.10)          # real ROE -21.5%
        assert c.roe_real < 0
        assert self._reason(c) == ""


class TestScoring:
    def test_cheaper_scores_higher_on_value(self) -> None:
        r = score([cand("CHEAP", pb=0.4, pe=4.0), cand("RICH", pb=6.0, pe=40.0)]
                  + universe())
        v = r.set_index("tic")["value"]
        assert v["CHEAP"] > v["RICH"]

    def test_higher_real_roe_scores_higher_on_quality(self) -> None:
        r = score([cand("GOOD", roe_nominal=0.60), cand("POOR", roe_nominal=0.05)]
                  + universe())
        q = r.set_index("tic")["quality"]
        assert q["GOOD"] > q["POOR"]

    def test_missing_data_ranks_at_the_median_not_the_extreme(self) -> None:
        """A company with no reported debt figure is unknown, not debt-free.
        Scoring it as either extreme would be a fabricated opinion."""
        r = score(universe() + [cand("NODATA", debt_to_equity=float("nan"))])
        q = float(r.set_index("tic").loc["NODATA", "quality"])
        assert 0.2 < q < 0.8

    def test_weights_are_applied(self) -> None:
        cfg = ScreenConfig(w_value=1.0, w_quality=0.0, w_momentum=0.0)
        r = score(universe(), cfg)
        e = r[r.eligible]
        assert e.total.corr(e.value) == pytest.approx(1.0, abs=1e-9)

    def test_ineligible_names_get_no_score(self) -> None:
        r = score(universe() + [cand("TINY", market_cap=1e7)])
        assert pd.isna(r[r.tic == "TINY"].iloc[0]["total"])

    def test_empty_universe_does_not_raise(self) -> None:
        assert score([]).empty


class TestPortfolioConstruction:
    def test_holds_n_equally_weighted(self) -> None:
        pf = build_portfolio(score(universe(30)), cfg=ScreenConfig(n_holdings=15))
        assert len(pf) == 15
        assert all(w == pytest.approx(1 / 15) for w in pf.values())
        assert sum(pf.values()) == pytest.approx(1.0)

    def test_never_holds_a_vetoed_name(self) -> None:
        f = Forensics(ticker="BAD", flags=[Flag("PROFIT_NO_CASH", 3, ""),
                                           Flag("CHRONIC_CASH_BURN", 3, "")])
        r = score(universe(20) + [cand("BAD", pb=0.1, pe=1.5, forensics=f)])
        assert "BAD" not in build_portfolio(r)

    def test_empty_when_nothing_qualifies(self) -> None:
        r = score([cand("X", market_cap=1.0)])
        assert build_portfolio(r) == {}


class TestTurnoverControl:
    """Without an incumbency margin a top-N rule rebalances whenever two names
    swap by a hair — which is how a low-turnover strategy silently becomes a
    high-turnover one and gives its return away in costs."""

    def _ranked(self):
        return score(universe(30))

    def test_marginal_challenger_does_not_displace_an_incumbent(self) -> None:
        r = self._ranked()
        top = list(r[r.eligible].tic.head(15))
        incumbent = list(r[r.eligible].tic)[15]        # just outside the cut
        held = {t: 1 / 15 for t in top[:-1]} | {incumbent: 1 / 15}
        pf = build_portfolio(r, current=held,
                             cfg=ScreenConfig(n_holdings=15, replace_margin=0.5))
        assert incumbent in pf

    def test_a_clear_improvement_does_displace(self) -> None:
        r = self._ranked()
        ranked_tics = list(r[r.eligible].tic)
        held = {t: 1 / 15 for t in ranked_tics[:14]} | {ranked_tics[-1]: 1 / 15}
        pf = build_portfolio(r, current=held,
                             cfg=ScreenConfig(n_holdings=15, replace_margin=0.01))
        assert ranked_tics[-1] not in pf

    def test_holding_size_is_stable_across_rebalances(self) -> None:
        r = self._ranked()
        first = build_portfolio(r)
        second = build_portfolio(r, current=first)
        assert len(first) == len(second) == 15


class TestExplain:
    def test_explains_a_rejection(self) -> None:
        f = Forensics(ticker="BAD", flags=[Flag("DEBT_SURGE", 3, ""),
                                           Flag("PROFIT_NO_CASH", 3, "")])
        r = score(universe() + [cand("BAD", forensics=f)])
        assert "EXCLUDED" in explain(r, "BAD") and "forensic" in explain(r, "BAD")

    def test_explains_a_selection(self) -> None:
        r = score(universe())
        out = explain(r, "CO00")
        assert "rank" in out and "value" in out and "real ROE" in out

    def test_unknown_ticker_is_not_an_error(self) -> None:
        assert "not in the universe" in explain(score(universe()), "NOPE")
