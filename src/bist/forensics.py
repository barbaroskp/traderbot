"""Forensic accounting scores for Turkish listed companies.

THE THESIS
----------
This project spent a long time looking for an edge in price data and did not
find one, in either crypto or Turkish equities, at any intraday horizon. The
edge that the evidence *does* support is slow, fundamental, and comes from
care rather than speed:

  1. Sloan (1996) and the accruals literature: firms whose reported earnings
     are not backed by cash flow subsequently underperform. This is one of the
     most replicated anomalies in accounting, it works at annual horizons, and
     it survives transaction costs precisely because it is low-turnover.
  2. Foucault, Hombert & Roşu (JF 2016): the fast trader profits *less* from
     long-run value; the slow trader's value-trading profit stays positive.
     Speed and long-horizon informational profit are substitutes.
  3. A Turkey-specific accident: TMS 29 inflation accounting restates every
     prior period into current purchasing power. Any screener that divides one
     as-filed period by another is dividing different currencies. Doing this
     correctly is not clever, it is just careful — and it appears most retail
     tools do not.

WHY A MACHINE DOES THIS BETTER THAN A PERSON
--------------------------------------------
Not because it is faster. Because it is exhaustive. A human analyst can pull
apart one company's filings the way GESAN's were pulled apart and find that
Q2 operating income was negative while net income rose, funded by a securities
disposal. A human cannot do that for 500 companies every quarter without
getting bored and skipping the boring ones — and the boring ones are where the
surprises are.

VALIDATION
----------
GESAN is the hand-verified test case. Its H1 2026 filing was read line by line
against KAP: revenue −5.5% in real terms, Q2 operating result −113m TL, net
profit +932m TL from a 1,120m TL securities disposal, operating cash flow
negative every period since 2023, debt +84% real year-on-year with 78% of it
FX-denominated and no hedging, related-party receivables up 1.5bn TL. Any
scoring function that does not rank GESAN in the worst decile of the market on
earnings quality is broken, and `tests/test_forensics.py` asserts that.

WHAT THIS MODULE CANNOT SEE
---------------------------
Related-party balances, hedging policy, auditor identity, and the composition
of "other securities" live in filing footnotes, not in any structured feed. The
scores below therefore catch the *shape* of a problem (earnings without cash,
debt without capex) but not its narrative. A flagged name still needs the
filing read. The point of the score is to tell you which twenty filings to read
out of five hundred.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

# Rows as they appear in the structured statements. Vendors rename these
# occasionally, so every lookup goes through `pick` and a missing row becomes
# NaN rather than an exception or, worse, a zero.
REVENUE = ("Total Revenue", "Operating Revenue")
OP_INCOME = ("Operating Income", "Operating Profit", "EBIT")
NET_INCOME = ("Net Income", "Net Income Common Stockholders",
              "Net Income Continuous Operations")
OCF = ("Operating Cash Flow", "Total Cash From Operating Activities",
       "Cash Flow From Continuing Operating Activities")
CAPEX = ("Capital Expenditure", "Capital Expenditures")
TOTAL_ASSETS = ("Total Assets",)
TOTAL_DEBT = ("Total Debt",)
EQUITY = ("Stockholders Equity", "Total Stockholder Equity",
          "Common Stock Equity")
CASH = ("Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments")
INVENTORY = ("Inventory",)
RECEIVABLES = ("Accounts Receivable", "Receivables", "Gross Accounts Receivable")


def pick(df: pd.DataFrame | None, names: tuple[str, ...]) -> pd.Series | None:
    """First matching row from a statement, or None. Never raises."""
    if df is None or df.empty:
        return None
    for n in names:
        if n in df.index:
            s = pd.to_numeric(df.loc[n], errors="coerce").dropna()
            if not s.empty:
                return s.sort_index(ascending=False)      # newest first
    return None


def _sum_recent(s: pd.Series | None, k: int = 4) -> float:
    """Trailing sum of the k most recent periods.

    Summing quarters is the right move under TMS 29 only when the quarters are
    stated in the same purchasing power. They are not: each filing restates its
    own comparatives, so quarters drawn from DIFFERENT filings are in different
    units. Summing them therefore understates older quarters by the inflation
    between filings. That biases trailing sums DOWN in a high-inflation regime,
    which makes every ratio built on them conservative rather than flattering —
    an acceptable direction for a screen whose job is to raise suspicion.
    """
    if s is None or s.empty:
        return float("nan")
    return float(s.iloc[:k].sum()) if len(s) >= max(2, k - 1) else float("nan")


def _growth(s: pd.Series | None, k: int = 4) -> float:
    """Change from k periods ago to now, as a fraction. NaN if unavailable."""
    if s is None or len(s) <= k:
        return float("nan")
    old, new = float(s.iloc[k]), float(s.iloc[0])
    if not np.isfinite(old) or old == 0:
        return float("nan")
    return new / old - 1.0


@dataclass
class Flag:
    """One red flag, with the number that produced it."""
    code: str
    severity: int          # 1 = note, 2 = concern, 3 = serious
    detail: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.code}: {self.detail}"


@dataclass
class Forensics:
    ticker: str
    accruals: float = float("nan")          # (net income - OCF) / assets
    ocf_to_ni: float = float("nan")         # cash backing of reported profit
    op_vs_net: float = float("nan")         # operating income / net income
    debt_growth: float = float("nan")
    debt_to_equity: float = float("nan")
    capex_to_debt_growth: float = float("nan")
    working_capital_bloat: float = float("nan")   # (inv+recv) growth - rev growth
    equity_growth_vs_earnings: float = float("nan")
    cash_burn_quarters: int = 0
    model_applies: bool = True
    flags: list[Flag] = field(default_factory=list)

    @property
    def severity(self) -> int:
        """Sum of flag weights. NOT_SCORED carries weight 0 deliberately, so an
        unscored financial cannot be mistaken for a clean industrial."""
        return sum(f.severity for f in self.flags)

    def summary(self) -> str:
        head = f"{self.ticker:8s} severity {self.severity:2d}"
        if not self.flags:
            return head + "  (no flags raised)"
        return head + "\n" + "\n".join(f"    {f}" for f in self.flags)


# ── business models these checks do not describe ────────────────
# Calibrating the screen on 134 BIST names made this unavoidable. A bank's
# "debt" is its deposit base — growing it is the business succeeding, not
# distress — and bank operating cash flow swings with loan-book changes, so
# PROFIT_NO_CASH fired on AKBNK, YKBNK and HALKB, which is meaningless. A REIT
# is levered property by construction and has no "capex" in the industrial
# sense, so BORROWED_NOT_INVESTED misfired on EKGYO, HLGYO and OZKGY. Insurers
# hold float, which breaks the same checks.
#
# The honest response is to say so rather than to score them anyway. Financials
# need their own model (NPLs, net interest margin, capital adequacy,
# cost/income) and that is a separate piece of work.
BALANCE_SHEET_BUSINESSES = {
    # banks and participation banks
    "GARAN", "AKBNK", "ISCTR", "YKBNK", "VAKBN", "HALKB", "TSKB", "ALBRK",
    "SKBNK", "ICBCT", "QNBFB",
    # insurance
    "AKGRT", "ANHYT", "ANSGR", "TURSG", "RAYSG",
    # REITs (gayrimenkul yatırım ortaklıkları)
    "EKGYO", "ISGYO", "TRGYO", "HLGYO", "KLGYO", "SNGYO", "AGYO", "OZKGY",
    "AKFGY", "TSGYO", "PEKGY",
    # holding companies consolidating financial subsidiaries
    "SAHOL", "KCHOL",
}


def analyse(ticker: str, financials: pd.DataFrame | None,
            balance: pd.DataFrame | None, cashflow: pd.DataFrame | None,
            cpi_yoy: float = 0.315) -> Forensics:
    """Score one company's earnings quality from its structured statements.

    Every check is one-sided: it can only ever raise suspicion. Nothing here
    awards points for looking good, because the asymmetry is real — a clean
    accrual profile is weak evidence of health, while earnings unbacked by cash
    is strong evidence of a problem.

    ``cpi_yoy`` is required because under TMS 29 every balance-sheet figure is
    restated into current purchasing power. Judging balance-sheet growth against
    zero in a 31.5% inflation regime flags the whole market: an early version of
    this function raised EQUITY_NOT_FROM_PROFIT on 41% of BIST, which was
    measuring Turkish inflation rather than any company's behaviour.
    """
    f = Forensics(ticker=ticker)
    f.model_applies = ticker not in BALANCE_SHEET_BUSINESSES
    if not f.model_applies:
        f.flags.append(Flag("NOT_SCORED", 0,
                            "bank, insurer, REIT or financial holding — these "
                            "checks do not describe this balance sheet and it "
                            "needs a sector-specific model"))
        return f

    rev = pick(financials, REVENUE)
    opi = pick(financials, OP_INCOME)
    ni = pick(financials, NET_INCOME)
    ocf = pick(cashflow, OCF)
    capex = pick(cashflow, CAPEX)
    assets = pick(balance, TOTAL_ASSETS)
    debt = pick(balance, TOTAL_DEBT)
    eq = pick(balance, EQUITY)
    inv = pick(balance, INVENTORY)
    recv = pick(balance, RECEIVABLES)

    ni_ttm, ocf_ttm = _sum_recent(ni), _sum_recent(ocf)
    assets_now = float(assets.iloc[0]) if assets is not None and len(assets) else float("nan")

    # ── 1. Accruals (Sloan 1996) ────────────────────────────────
    # Profit the cash flow statement does not corroborate. The single most
    # replicated earnings-quality signal there is.
    if np.isfinite(ni_ttm) and np.isfinite(ocf_ttm) and np.isfinite(assets_now) and assets_now > 0:
        f.accruals = (ni_ttm - ocf_ttm) / assets_now
        if f.accruals > 0.15:
            f.flags.append(Flag("HIGH_ACCRUALS", 3,
                                f"reported profit exceeds operating cash flow by "
                                f"{f.accruals*100:.1f}% of total assets"))
        elif f.accruals > 0.08:
            f.flags.append(Flag("ACCRUALS", 2,
                                f"accruals {f.accruals*100:.1f}% of assets"))

    if np.isfinite(ni_ttm) and np.isfinite(ocf_ttm) and ni_ttm > 0:
        f.ocf_to_ni = ocf_ttm / ni_ttm
        if f.ocf_to_ni < 0:
            f.flags.append(Flag("PROFIT_NO_CASH", 3,
                                f"profitable on paper but operating cash flow is "
                                f"negative (OCF/NI = {f.ocf_to_ni:+.2f})"))
        elif f.ocf_to_ni < 0.5:
            f.flags.append(Flag("WEAK_CASH_CONVERSION", 2,
                                f"only {f.ocf_to_ni*100:.0f}% of reported profit "
                                f"arrived as cash"))

    # ── 2. Operating result vs bottom line ──────────────────────
    # GESAN's signature: operations lost money, net income rose, and the gap was
    # a securities disposal. Any company whose profit is materially larger than
    # its operating profit is earning it somewhere other than its business.
    if opi is not None and ni is not None and len(opi) and len(ni):
        o, n = float(opi.iloc[0]), float(ni.iloc[0])
        if np.isfinite(o) and np.isfinite(n):
            if o < 0 < n:
                f.op_vs_net = float("-inf")
                f.flags.append(Flag("PROFIT_FROM_NOWHERE", 3,
                                    f"operating result {o/1e9:+.2f}bn but net income "
                                    f"{n/1e9:+.2f}bn — the profit is non-operating"))
            elif n > 0 and o > 0:
                f.op_vs_net = o / n
                if f.op_vs_net < 0.5:
                    f.flags.append(Flag("NON_OPERATING_PROFIT", 2,
                                        f"operating profit is only "
                                        f"{f.op_vs_net*100:.0f}% of net income"))

    # ── 3. Debt that did not buy anything ───────────────────────
    if debt is not None and len(debt) > 4:
        f.debt_growth = _growth(debt, 4)
        d_now, d_old = float(debt.iloc[0]), float(debt.iloc[4])
        delta = d_now - d_old
        cx = abs(_sum_recent(capex)) if capex is not None else float("nan")
        # Nominal debt growth is meaningless at 31.5% inflation: standing still
        # in real terms already shows as +31.5%. Judge the REAL change.
        real_debt_growth = (1.0 + f.debt_growth) / (1.0 + cpi_yoy) - 1.0 \
            if np.isfinite(f.debt_growth) else float("nan")
        if np.isfinite(real_debt_growth) and real_debt_growth > 0.30:
            sev = 3 if real_debt_growth > 0.75 else 2
            f.flags.append(Flag("DEBT_SURGE", sev,
                                f"financial debt +{real_debt_growth*100:.0f}% in REAL "
                                f"terms over four quarters "
                                f"(+{f.debt_growth*100:.0f}% nominal)"))
        if np.isfinite(cx) and delta > 0:
            f.capex_to_debt_growth = cx / delta
            if f.capex_to_debt_growth < 0.25 and delta / max(assets_now, 1) > 0.03:
                f.flags.append(Flag("BORROWED_NOT_INVESTED", 2,
                                    f"debt rose {delta/1e9:.2f}bn while capex was only "
                                    f"{cx/1e9:.2f}bn — borrowing funded something "
                                    f"other than capacity"))

    if debt is not None and eq is not None and len(debt) and len(eq):
        e = float(eq.iloc[0])
        if np.isfinite(e) and e > 0:
            f.debt_to_equity = float(debt.iloc[0]) / e
            if f.debt_to_equity > 1.5:
                f.flags.append(Flag("LEVERAGE", 2,
                                    f"debt/equity {f.debt_to_equity:.2f}"))

    # ── 4. Working capital growing faster than the business ─────
    # Inventory and receivables outrunning revenue is how revenue that has not
    # really been earned, or goods that will not really sell, shows up.
    rev_g = _growth(rev, 4)
    wc_parts = [g for g in (_growth(inv, 4), _growth(recv, 4)) if np.isfinite(g)]
    if wc_parts and np.isfinite(rev_g):
        f.working_capital_bloat = float(np.mean(wc_parts)) - rev_g
        if f.working_capital_bloat > 0.4:
            f.flags.append(Flag("WC_BLOAT", 2,
                                f"inventory/receivables grew "
                                f"{f.working_capital_bloat*100:.0f}pp faster than revenue"))

    # ── 5. Equity growing without earnings ──────────────────────
    # Either dilution or revaluation. Under TMS 29 a large part is the
    # measuring-unit change, which is why this is only a note: it flags a
    # question, not a finding.
    if eq is not None and len(eq) > 4 and np.isfinite(ni_ttm):
        f.equity_growth_vs_earnings = _growth(eq, 4)
        e_now, e_old = float(eq.iloc[0]), float(eq.iloc[4])
        if np.isfinite(e_old) and e_old > 0:
            # Expected equity = last year's, restated for inflation, plus
            # retained earnings. Only the excess over THAT is unexplained.
            expected = e_old * (1.0 + cpi_yoy) + ni_ttm
            unexplained = e_now - expected
            if unexplained > 0.25 * e_old:
                f.flags.append(Flag("EQUITY_NOT_FROM_PROFIT", 1,
                                    f"equity {e_now/1e9:.2f}bn vs {expected/1e9:.2f}bn "
                                    f"expected from inflation-restated opening equity "
                                    f"plus {ni_ttm/1e9:.2f}bn earnings — "
                                    f"{unexplained/1e9:+.2f}bn unexplained"))

    # ── 6. Persistent cash burn ─────────────────────────────────
    if ocf is not None and len(ocf):
        f.cash_burn_quarters = int((ocf.iloc[:6] < 0).sum())
        if f.cash_burn_quarters >= 4:
            f.flags.append(Flag("CHRONIC_CASH_BURN", 3,
                                f"operating cash flow negative in "
                                f"{f.cash_burn_quarters} of the last "
                                f"{min(6, len(ocf))} periods"))
        elif f.cash_burn_quarters >= 2:
            f.flags.append(Flag("CASH_BURN", 1,
                                f"operating cash flow negative in "
                                f"{f.cash_burn_quarters} recent periods"))

    return f


def rank(results: list[Forensics]) -> pd.DataFrame:
    """Cross-sectional table, worst first.

    Severity is deliberately a simple sum of flag weights rather than a fitted
    model. There is no labelled training set of "companies that later blew up"
    for BIST, so any weighting would be invented precision. A transparent count
    is honest about that and stays auditable.
    """
    rows = []
    for r in results:
        rows.append({
            "tic": r.ticker, "severity": r.severity,
            "accruals": r.accruals, "ocf_to_ni": r.ocf_to_ni,
            "debt_growth": r.debt_growth, "debt_to_equity": r.debt_to_equity,
            "wc_bloat": r.working_capital_bloat,
            "burn_q": r.cash_burn_quarters,
            "scored": r.model_applies,
            # NOT "flags": DataFrame.flags is a reserved pandas attribute and a
            # column of that name is unreachable by attribute access.
            "flag_codes": "; ".join(f.code for f in r.flags),
        })
    df = pd.DataFrame(rows)
    return df.sort_values("severity", ascending=False).reset_index(drop=True)
