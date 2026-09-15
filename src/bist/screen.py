"""The portfolio selector.

WHAT THIS DOES AND DOES NOT DO
------------------------------
It does not forecast prices. Fifty rules were tested against BIST and crypto
price history and none survived its own transaction costs, so nothing here
tries to predict a move. What it does instead is rank companies on things that
are knowable today — how cheap they are, whether they earn a real return, and
whether their accounting is telling a coherent story — and hold the best ones
until the ranking changes.

FOUR SCORES, IN ORDER OF HOW MUCH I TRUST THEM
----------------------------------------------
1. FORENSICS (a veto, not a score). From `forensics.py`. Anything above the
   severity threshold is removed from the universe regardless of how cheap it
   looks. This is the highest-conviction component because it is the one where
   a machine genuinely outperforms a person: reading 500 cash-flow statements
   every quarter and noticing the eleven companies whose profit did not arrive
   as cash. Sloan (1996) and the accruals literature back the direction.

2. QUALITY, measured in REAL terms. A Turkish company earning 7.6% on equity
   while inflation runs 31.5% is destroying about a quarter of its capital a
   year, and every screener that ranks on nominal ROE will call it profitable.
   Subtracting CPI is the single highest-value line in this file.

3. VALUE. Cheapness on P/B and earnings yield. Decades of out-of-sample
   evidence across dozens of markets — but in Turkey it is also the fastest
   route into a value trap, which is why forensics vetoes first and quality is
   weighted equally.

4. MOMENTUM (12-1 month). The only rule that beat equal-weight buy-and-hold in
   our own ten-year BIST test (+49.8%/yr vs +46.4%). The margin was 3.4pp at
   5.9x turnover, so it is a tilt, not a thesis.

WHAT IS HONESTLY WEAK HERE
--------------------------
The fundamental data is a CURRENT snapshot: four annual periods and six
quarters, with no publication dates. So this cannot be backtested on Turkish
data the way the price rules were — there is no point-in-time history to replay
against. Confidence comes from the external factor literature, not from a local
measurement, and that is a weaker footing than anything else in this
repository. `collect_snapshot()` exists to start accumulating the point-in-time
record forward so that in a few years this file can be tested rather than
argued for.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.bist.forensics import Forensics, analyse

TURKISH_CPI_YOY = 0.315          # Aug 2026, TÜİK


@dataclass
class ScreenConfig:
    """Every threshold, in one place, so a change is visible in a diff."""
    n_holdings: int = 15
    min_market_cap: float = 5e9          # TL
    min_daily_turnover: float = 20e6     # TL, 21-day average
    forensic_veto_severity: int = 6      # at or above this, excluded outright
    cpi_yoy: float = TURKISH_CPI_YOY
    # Weights on the three scored components. Value and quality are equal by
    # design: value alone finds traps, quality alone finds expensive
    # compounders. Momentum is a tilt.
    w_value: float = 0.40
    w_quality: float = 0.40
    w_momentum: float = 0.20
    # Turnover control. Novy-Marx & Velikov: strategies under 50% one-sided
    # monthly turnover still deliver net returns; above 100%/month the costs
    # exceed most anomalies. A name must beat the worst holding by this margin
    # before it displaces it, which stops the portfolio churning on noise.
    replace_margin: float = 0.05
    exclude_financials_from_scoring: bool = True


@dataclass
class Candidate:
    ticker: str
    market_cap: float = float("nan")
    turnover: float = float("nan")
    pb: float = float("nan")
    pe: float = float("nan")
    roe_nominal: float = float("nan")
    profit_margin: float = float("nan")
    debt_to_equity: float = float("nan")
    mom_12_1: float = float("nan")
    forensics: Forensics | None = None

    @property
    def roe_real(self) -> float:
        """Nominal ROE minus inflation. The number that decides whether the
        business is actually creating value in a 31.5% CPI regime."""
        if not np.isfinite(self.roe_nominal):
            return float("nan")
        return self.roe_nominal - TURKISH_CPI_YOY

    @property
    def earnings_yield(self) -> float:
        if not np.isfinite(self.pe) or self.pe <= 0:
            return float("nan")
        return 1.0 / self.pe

    @property
    def forensic_severity(self) -> int:
        return self.forensics.severity if self.forensics else 0


def _pct_rank(s: pd.Series, ascending: bool) -> pd.Series:
    """Percentile rank in [0,1], NaN-safe, filled at the median.

    Missing data must not score as best OR worst: a company with no reported
    debt figure is unknown, not debt-free, and either extreme would be a
    fabricated opinion.
    """
    r = s.rank(pct=True, ascending=ascending)
    return r.fillna(0.5)


def score(candidates: list[Candidate], cfg: ScreenConfig | None = None
          ) -> pd.DataFrame:
    """Rank the universe. Returns every candidate with its scores and status."""
    cfg = cfg or ScreenConfig()
    rows = [{
        "tic": c.ticker, "mcap": c.market_cap, "turnover": c.turnover,
        "pb": c.pb, "pe": c.pe, "ey": c.earnings_yield,
        "roe_nom": c.roe_nominal, "roe_real": c.roe_real,
        "margin": c.profit_margin, "de": c.debt_to_equity,
        "mom": c.mom_12_1, "sev": c.forensic_severity,
        "scored": c.forensics.model_applies if c.forensics else True,
    } for c in candidates]
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # ── eligibility, before any scoring ─────────────────────────
    df["eligible"] = True
    df["reject"] = ""

    def _reject(mask: pd.Series, why: str) -> None:
        hit = mask & df["eligible"]
        df.loc[hit, "eligible"] = False
        df.loc[hit, "reject"] = why

    _reject(df.mcap.fillna(0) < cfg.min_market_cap, "too small")
    _reject(df.turnover.fillna(0) < cfg.min_daily_turnover, "illiquid")
    _reject(df.sev >= cfg.forensic_veto_severity, "forensic veto")
    # A loss-making company has no earnings yield; ranking it on cheapness would
    # put the most distressed names at the top.
    #
    # Note what is NOT vetoed here. Requiring a POSITIVE real return on equity —
    # nominal ROE above 31.5% inflation — is the economically correct test of
    # whether a business creates value, and applying it left 3 eligible names
    # out of 142. That is not a bug in the filter; it is the measurement.
    # Turkish equity is an inflation hedge rather than a compounder, which is
    # why BIST's twenty-year real price return is roughly +0.6%/yr.
    #
    # So real ROE does its work as a RANKING input inside the quality score,
    # where "least bad" is a meaningful answer, rather than as a veto, where it
    # empties the universe. The absolute number is still printed for every name
    # because a portfolio of companies all losing real ground is a fact the
    # owner should see, not one the screen should hide.
    _reject(df.roe_nom.fillna(-9) <= 0, "loss-making")
    _reject(df.pe.isna() | (df.pe <= 0), "no positive earnings")

    # Sanity bounds on the vendor's own ratios. This is not a view about
    # valuation, it is a data check: the first run of this screen ranked ENKAI
    # ninth on a reported price/book of 59.7, which is not a plausible figure
    # for a large Turkish contractor and is almost certainly a units error in
    # the book-value field. A ratio outside these bounds means the input is
    # wrong, and a wrong input must not be scored — least of all when the
    # scoring is percentile-based, where one absurd value distorts every rank
    # around it.
    _reject(df.pb.notna() & ((df.pb <= 0) | (df.pb > 25)), "implausible P/B")
    _reject(df.pe.notna() & (df.pe > 150), "implausible P/E")
    if cfg.exclude_financials_from_scoring:
        _reject(~df.scored.astype(bool), "financial: needs its own model")

    e = df[df.eligible].copy()
    if e.empty:
        df["value"] = df["quality"] = df["momentum"] = df["total"] = np.nan
        return df.sort_values("sev", ascending=False)

    # ── the three scores, computed only over eligible names ─────
    e["value"] = (_pct_rank(e.pb, ascending=False)
                  + _pct_rank(e.ey, ascending=True)) / 2
    e["quality"] = (_pct_rank(e.roe_real, ascending=True)
                    + _pct_rank(e.margin, ascending=True)
                    + _pct_rank(e.de, ascending=False)) / 3
    e["momentum"] = _pct_rank(e.mom, ascending=True)
    e["total"] = (cfg.w_value * e.value + cfg.w_quality * e.quality
                  + cfg.w_momentum * e.momentum)

    out = df.merge(e[["tic", "value", "quality", "momentum", "total"]],
                   on="tic", how="left")
    return out.sort_values("total", ascending=False, na_position="last"
                           ).reset_index(drop=True)


def build_portfolio(ranked: pd.DataFrame, current: dict[str, float] | None = None,
                    cfg: ScreenConfig | None = None) -> dict[str, float]:
    """Target weights, respecting the incumbency margin.

    A pure top-N rule rebalances whenever two names swap places by a hair,
    which is how a low-turnover strategy quietly becomes a high-turnover one.
    An incumbent is only displaced by a challenger scoring `replace_margin`
    higher.
    """
    cfg = cfg or ScreenConfig()
    current = current or {}
    ok = ranked[ranked.eligible & ranked.total.notna()]
    if ok.empty:
        return {}

    picks = list(ok.tic.head(cfg.n_holdings))
    held = [t for t in current if t in set(ok.tic)]

    for t in held:
        if t in picks:
            continue
        t_score = float(ok.loc[ok.tic == t, "total"].iloc[0])
        # the weakest name we would otherwise buy
        weakest = min(picks, key=lambda p: float(ok.loc[ok.tic == p, "total"].iloc[0]))
        w_score = float(ok.loc[ok.tic == weakest, "total"].iloc[0])
        if w_score - t_score < cfg.replace_margin:
            picks[picks.index(weakest)] = t          # incumbent survives

    return {t: 1.0 / len(picks) for t in picks} if picks else {}


def explain(ranked: pd.DataFrame, ticker: str) -> str:
    """Why one name was picked or rejected, in plain terms."""
    row = ranked[ranked.tic == ticker]
    if row.empty:
        return f"{ticker}: not in the universe"
    r = row.iloc[0]
    if not r.eligible:
        return f"{ticker}: EXCLUDED — {r.reject} (forensic severity {r.sev:.0f})"
    pos = int(row.index[0]) + 1
    return (f"{ticker}: rank {pos}/{len(ranked)} · total {r.total:.3f} "
            f"(value {r.value:.2f}, quality {r.quality:.2f}, mom {r.momentum:.2f}) · "
            f"P/B {r.pb:.2f}, real ROE {r.roe_real*100:+.1f}%, "
            f"forensic severity {r.sev:.0f}")
