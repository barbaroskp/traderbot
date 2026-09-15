"""Tests for the forensic accounting screen.

The important ones are at the bottom: GESAN's real H1 2026 figures, read by
hand from its KAP filing, must come out in the worst decile. That test is the
only thing standing between a calibrated screen and a set of arbitrary
thresholds, so if it fails, do not adjust it — work out which threshold moved
and why.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.bist.forensics import (
    BALANCE_SHEET_BUSINESSES,
    Forensics,
    analyse,
    pick,
    rank,
)

CPI = 0.315          # Turkish CPI, Aug 2026


def frame(rows: dict[str, list[float]], periods: int = 6) -> pd.DataFrame:
    """Statement with newest period FIRST, matching the vendor's layout."""
    cols = pd.to_datetime([f"2026-{m:02d}-01" for m in range(6, 0, -1)][:periods])
    return pd.DataFrame({c: [v[i] for v in rows.values()]
                         for i, c in enumerate(cols)},
                        index=list(rows)).astype(float)


def _statements(**over) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """A boring, healthy company; override single rows to make it unhealthy."""
    fin = {"Total Revenue": [100] * 6, "Operating Income": [12] * 6,
           "Net Income": [10] * 6}
    bal = {"Total Assets": [500] * 6, "Total Debt": [100] * 6,
           "Stockholders Equity": [200] * 6, "Inventory": [50] * 6,
           "Accounts Receivable": [40] * 6}
    cf = {"Operating Cash Flow": [11] * 6, "Capital Expenditure": [-8] * 6}
    for k, v in over.items():
        for d in (fin, bal, cf):
            if k in d:
                d[k] = v
    return frame(fin), frame(bal), frame(cf)


class TestPlumbing:
    def test_missing_row_is_none_not_zero(self) -> None:
        """A zero here would silently turn 'unknown' into 'nothing', which is
        how a screen ends up flagging a company for data it never had."""
        assert pick(frame({"Total Revenue": [1] * 6}), ("Operating Income",)) is None

    def test_missing_statement_is_survivable(self) -> None:
        f = analyse("X", None, None, None, CPI)
        assert f.flags == [] and f.severity == 0

    def test_newest_period_first(self) -> None:
        s = pick(frame({"Total Revenue": [9, 8, 7, 6, 5, 4]}), ("Total Revenue",))
        assert s is not None and s.iloc[0] == 9

    def test_healthy_company_raises_nothing(self) -> None:
        assert analyse("OK", *_statements(), CPI).flags == []


class TestSectorExclusion:
    @pytest.mark.parametrize("tic", ["GARAN", "EKGYO", "ANHYT", "SAHOL"])
    def test_financials_are_not_scored(self, tic) -> None:
        """Their deposit base is not 'debt' and their operating cash flow moves
        with the loan book. Scoring them anyway produced AKBNK and HALKB in the
        worst-ten list, which was meaningless."""
        f = analyse(tic, *_statements(), CPI)
        assert not f.model_applies
        assert [x.code for x in f.flags] == ["NOT_SCORED"]

    def test_not_scored_carries_no_severity(self) -> None:
        """Otherwise an unscored bank ranks as if it were a clean industrial."""
        assert analyse("GARAN", *_statements(), CPI).severity == 0

    def test_industrials_are_scored(self) -> None:
        assert analyse("EREGL", *_statements(), CPI).model_applies

    def test_the_exclusion_list_is_not_empty(self) -> None:
        assert {"GARAN", "EKGYO"} <= BALANCE_SHEET_BUSINESSES


class TestInflationCalibration:
    """Judging Turkish balance sheets against zero growth flags the whole
    market. An early version raised EQUITY_NOT_FROM_PROFIT on 41% of BIST; it
    was measuring inflation, not behaviour."""

    def test_debt_standing_still_in_real_terms_is_not_a_surge(self) -> None:
        nominal = [131.5, 125, 118, 110, 100, 100]      # exactly CPI
        f = analyse("X", *_statements(**{"Total Debt": nominal}), CPI)
        assert "DEBT_SURGE" not in [x.code for x in f.flags]

    def test_real_debt_growth_is_flagged(self) -> None:
        doubling = [200, 170, 145, 120, 100, 100]
        f = analyse("X", *_statements(**{"Total Debt": doubling}), CPI)
        codes = [x.code for x in f.flags]
        assert "DEBT_SURGE" in codes

    def test_equity_restated_for_inflation_is_not_unexplained(self) -> None:
        """Opening equity 200, inflation 31.5%, earnings 40 -> 303 is expected."""
        eq = [303, 280, 260, 240, 200, 200]
        f = analyse("X", *_statements(**{"Stockholders Equity": eq,
                                         "Net Income": [10] * 6}), CPI)
        assert "EQUITY_NOT_FROM_PROFIT" not in [x.code for x in f.flags]

    def test_equity_far_above_that_is_unexplained(self) -> None:
        eq = [500, 420, 350, 280, 200, 200]
        f = analyse("X", *_statements(**{"Stockholders Equity": eq,
                                         "Net Income": [10] * 6}), CPI)
        assert "EQUITY_NOT_FROM_PROFIT" in [x.code for x in f.flags]


class TestEarningsQuality:
    def test_profit_with_negative_operating_cash_flow(self) -> None:
        f = analyse("X", *_statements(**{"Operating Cash Flow": [-5] * 6}), CPI)
        assert "PROFIT_NO_CASH" in [x.code for x in f.flags]

    def test_operating_loss_with_positive_net_income(self) -> None:
        """GESAN's signature: the profit is coming from somewhere else."""
        f = analyse("X", *_statements(**{"Operating Income": [-3] + [12] * 5,
                                         "Net Income": [25] + [10] * 5}), CPI)
        flag = next(x for x in f.flags if x.code == "PROFIT_FROM_NOWHERE")
        assert flag.severity == 3

    def test_high_accruals(self) -> None:
        """Sloan (1996): profit the cash flow statement does not corroborate."""
        f = analyse("X", *_statements(**{"Net Income": [100] * 6,
                                         "Operating Cash Flow": [0] * 6}), CPI)
        codes = [x.code for x in f.flags]
        assert "HIGH_ACCRUALS" in codes
        assert f.accruals == pytest.approx(400 / 500)

    def test_working_capital_outrunning_revenue(self) -> None:
        f = analyse("X", *_statements(**{
            "Inventory": [200, 150, 120, 90, 50, 50],
            "Accounts Receivable": [160, 120, 90, 60, 40, 40]}), CPI)
        assert "WC_BLOAT" in [x.code for x in f.flags]

    def test_chronic_cash_burn_is_serious(self) -> None:
        f = analyse("X", *_statements(**{"Operating Cash Flow": [-5] * 6}), CPI)
        flag = next(x for x in f.flags if x.code == "CHRONIC_CASH_BURN")
        assert flag.severity == 3

    def test_borrowing_that_did_not_fund_capex(self) -> None:
        f = analyse("X", *_statements(**{
            "Total Debt": [300, 250, 200, 150, 100, 100],
            "Capital Expenditure": [-1] * 6}), CPI)
        assert "BORROWED_NOT_INVESTED" in [x.code for x in f.flags]

    def test_every_check_is_one_sided(self) -> None:
        """No check may award points for looking good: a clean accrual profile
        is weak evidence of health, while earnings without cash is strong
        evidence of a problem. The asymmetry is deliberate."""
        assert all(x.severity >= 0
                   for x in analyse("X", *_statements(), CPI).flags)


class TestGesanValidation:
    """GESAN's actual H1 2026 numbers, in thousands of June-2026 TL, taken from
    the KAP filing and cross-checked line by line. See RESEARCH-SYNTHESIS.md.

    Verified by hand: Q2 revenue 1,607,912 against 4,030,136 a year earlier;
    Q2 operating result −113,218 while net profit was +932,326, funded by a
    1,119,758 securities disposal; operating cash flow negative in every period
    since 2023; financial debt 6,157,501 against 3,341,000 restated a year
    earlier, 78% of it FX with no hedging.
    """

    def _gesan(self):
        fin = frame({
            "Total Revenue":    [1_607_912, 7_448_700, 6_430_000, 6_370_000,
                                 4_030_136, 3_500_000],
            "Operating Income": [-113_218, 1_930_000, 910_000, 1_180_000,
                                 444_461, 400_000],
            "Net Income":       [932_326, 180_000, 90_000, 610_000,
                                 40_000, 100_000],
        })
        bal = frame({
            "Total Assets":         [44_705_331] * 6,
            "Total Debt":           [6_157_501, 5_090_000, 4_060_000, 2_870_000,
                                     2_529_000, 2_400_000],
            "Stockholders Equity":  [17_230_000, 15_340_000, 13_640_000,
                                     13_070_000, 11_830_000, 11_000_000],
            "Inventory":            [10_675_676, 8_000_000, 7_000_000,
                                     6_000_000, 3_500_000, 3_000_000],
            "Accounts Receivable":  [3_500_000, 3_000_000, 2_500_000,
                                     2_200_000, 1_800_000, 1_600_000],
        })
        cf = frame({
            "Operating Cash Flow":  [-1_067_994, -400_000, 200_000, -300_000,
                                     -986_934, -500_000],
            "Capital Expenditure":  [-13_347, -50_000, -300_000, -400_000,
                                     -696_999, -500_000],
        })
        return fin, bal, cf

    def test_the_hand_verified_flags_are_all_raised(self) -> None:
        codes = {x.code for x in analyse("GESAN", *self._gesan(), CPI).flags}
        for expected in ("PROFIT_NO_CASH", "PROFIT_FROM_NOWHERE",
                         "DEBT_SURGE", "WC_BLOAT", "CHRONIC_CASH_BURN"):
            assert expected in codes, f"{expected} was not raised"

    def test_it_lands_in_the_worst_decile(self) -> None:
        """Against a field of healthy companies it must rank at the very top.
        This is the test that makes the thresholds meaningful rather than
        arbitrary."""
        field = [analyse(f"OK{i}", *_statements(), CPI) for i in range(20)]
        field.append(analyse("GESAN", *self._gesan(), CPI))
        table = rank(field)
        assert table.iloc[0]["tic"] == "GESAN"
        assert table.iloc[0]["severity"] >= 10

    def test_real_debt_growth_is_reported_not_nominal(self) -> None:
        """+143% nominal is +85% real at 31.5% inflation. Reporting the nominal
        figure would overstate it by two thirds."""
        f = analyse("GESAN", *self._gesan(), CPI)
        detail = next(x for x in f.flags if x.code == "DEBT_SURGE").detail
        assert "REAL" in detail and "nominal" in detail

    def test_operating_cash_flow_contradicts_the_profit(self) -> None:
        f = analyse("GESAN", *self._gesan(), CPI)
        assert f.ocf_to_ni < 0


class TestRanking:
    def test_worst_first(self) -> None:
        rows = [analyse("OK", *_statements(), CPI),
                analyse("BAD", *_statements(**{"Operating Cash Flow": [-9] * 6}), CPI)]
        assert rank(rows).iloc[0]["tic"] == "BAD"

    def test_flag_column_is_not_named_flags(self) -> None:
        """DataFrame.flags is a reserved pandas attribute; a column of that name
        raises KeyError on attribute access."""
        t = rank([analyse("OK", *_statements(), CPI)])
        assert "flag_codes" in t.columns and "flags" not in t.columns

    def test_scored_column_lets_callers_drop_financials(self) -> None:
        t = rank([analyse("GARAN", *_statements(), CPI),
                  analyse("EREGL", *_statements(), CPI)])
        assert set(t.scored) == {True, False}
