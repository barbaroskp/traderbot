"""The referee: one standard every candidate signal must pass.

This exists because four separate findings in this project looked strong and
died under scrutiny — shock reversion (t=+3.66 in year one, -0.71 in year two),
moving-average filters (a look-ahead bug), the drawdown brake (an equity-vs-
price re-entry bug), and tier-C trend following (t=+3.48 collapsing to +0.96
once the universe was selected point-in-time). Every one of them would have
been shipped by a harness that only printed a mean and a t-statistic.

So a candidate is reported as SURVIVING only if all of the following hold:

  net > 0                 profitable AFTER a realistic round-trip cost
  |t| >= 3.0              not 2.0 — we are testing many rules, see below
  both halves same sign   the sample split by date, not shuffled
  breadth                 not carried by one ticker or one quarter
  outlier-robust          still positive with the best 5 trades removed
  n >= 100                enough trades that the mean means anything

MULTIPLE TESTING
----------------
Testing k independent rules and keeping the best inflates the apparent
t-statistic. With ~30-60 rules under consideration the Bonferroni-corrected
5% threshold sits near |t| = 3.0-3.3, so 3.0 is the floor here and the count
of rules tested is printed alongside every table. This is the single most
common way a backtest lies, and it is free to guard against.

AMBIGUITY
---------
Hourly bars cannot order a touch of both bracket levels inside one bar. Those
observations are EXCLUDED and their share is reported. Assigning them to the
loss (the earlier approach) manufactures a negative result; assigning them to
the win manufactures a positive one. Reporting the share lets the reader see
how much of the answer is unknowable at this resolution.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

T_FLOOR = 3.0
MIN_N = 100


@dataclass
class Verdict:
    label: str
    n: int
    hit: float
    gross: float
    net: float
    t: float
    half1: float
    half2: float
    breakeven_cost: float
    tickers_profitable: str
    quarters_positive: str
    ex_top5: float
    ambiguous_pct: float
    survives: bool
    reasons: list[str] = field(default_factory=list)

    def row(self) -> str:
        flag = "PASS" if self.survives else "    "
        return (f"{flag} {self.label:34s} {self.n:5d} {self.hit*100:6.1f}% "
                f"{self.net*100:+8.3f}% {self.t:+6.2f} {self.half1*100:+7.2f} "
                f"{self.half2*100:+7.2f} {self.breakeven_cost*100:6.3f}% "
                f"{self.tickers_profitable:>6s} {self.quarters_positive:>5s} "
                f"{self.ex_top5*100:+7.3f}%")

    @staticmethod
    def header() -> str:
        return (f"{'':4s} {'rule':34s} {'n':>5s} {'hit':>7s} {'net':>9s} "
                f"{'t':>6s} {'half1':>7s} {'half2':>7s} {'b/e':>7s} "
                f"{'tics':>6s} {'qtr':>5s} {'ex-top5':>8s}")


def judge(df: pd.DataFrame, mask, outcome: str, cost: float, label: str,
          how_col: str | None = None) -> Verdict | None:
    """Evaluate one rule. Returns None if there is too little to say."""
    x = df[mask].copy()
    amb_pct = 0.0
    if how_col and how_col in x.columns:
        amb = (x[how_col] == "ambiguous")
        amb_pct = float(amb.mean()) if len(x) else 0.0
        x = x[~amb]
    x = x.dropna(subset=[outcome])
    n = len(x)
    if n < 40:
        return None

    a = x[outcome].to_numpy(dtype=float)
    gross = float(a.mean())
    net = gross - cost
    sd = float(a.std(ddof=1))
    t = gross / (sd / np.sqrt(n)) if sd > 0 else 0.0

    dates = pd.Series(pd.to_datetime(x["date"].astype(str)).to_numpy())
    cut = dates.quantile(0.5)
    h1 = x[dates.to_numpy() <= cut][outcome]
    h2 = x[dates.to_numpy() > cut][outcome]
    n1 = float(h1.mean() - cost) if len(h1) >= 20 else np.nan
    n2 = float(h2.mean() - cost) if len(h2) >= 20 else np.nan

    by_tic = x.groupby("tic")[outcome].agg(["count", "mean"])
    by_tic = by_tic[by_tic["count"] >= 5]
    tic_ok = int((by_tic["mean"] > cost).sum())
    tic_tot = int(len(by_tic))

    q = pd.qcut(dates.rank(method="first"), 4, labels=False, duplicates="drop")
    qmeans = x.groupby(q.to_numpy())[outcome].mean()
    q_ok = int((qmeans > cost).sum())
    q_tot = int(len(qmeans))

    srt = np.sort(a)[::-1]
    ex5 = float(srt[5:].mean() - cost) if n > 25 else np.nan

    reasons: list[str] = []
    if net <= 0:
        reasons.append("net<=0")
    if abs(t) < T_FLOOR:
        reasons.append(f"|t|<{T_FLOOR}")
    if n < MIN_N:
        reasons.append(f"n<{MIN_N}")
    if not (np.isfinite(n1) and np.isfinite(n2) and n1 > 0 and n2 > 0):
        reasons.append("half fails")
    if tic_tot >= 4 and tic_ok / tic_tot < 0.5:
        reasons.append("breadth")
    if q_tot >= 3 and q_ok < q_tot - 1:
        reasons.append("time-concentrated")
    if np.isfinite(ex5) and ex5 <= 0:
        reasons.append("outlier-driven")

    return Verdict(
        label=label, n=n, hit=float((a > 0).mean()), gross=gross, net=net, t=t,
        half1=n1, half2=n2, breakeven_cost=gross,
        tickers_profitable=f"{tic_ok}/{tic_tot}",
        quarters_positive=f"{q_ok}/{q_tot}", ex_top5=ex5,
        ambiguous_pct=amb_pct, survives=not reasons, reasons=reasons,
    )


def report(verdicts: list[Verdict | None], title: str, n_rules_tested: int,
           cost: float) -> str:
    vs = [v for v in verdicts if v is not None]
    lines = [
        f"\n{title}",
        f"cost charged {cost*100:.2f}% round trip | {n_rules_tested} rules tested "
        f"| survival needs net>0, |t|>={T_FLOOR}, both halves>0, breadth, "
        f"outlier-robust, n>={MIN_N}",
        Verdict.header(), "-" * 118,
    ]
    lines += [v.row() for v in sorted(vs, key=lambda v: -v.net)]
    surv = [v for v in vs if v.survives]
    lines.append("")
    if surv:
        lines.append(f"SURVIVING: {len(surv)}/{len(vs)}")
        for v in surv:
            lines.append(f"  {v.label}: net {v.net*100:+.3f}%/trade, t={v.t:+.2f}, "
                         f"break-even cost {v.breakeven_cost*100:.3f}%, n={v.n}")
    else:
        lines.append(f"SURVIVING: 0/{len(vs)} — nothing cleared the bar.")
        near = sorted(vs, key=lambda v: len(v.reasons))[:3]
        for v in near:
            lines.append(f"  closest: {v.label} — failed on {', '.join(v.reasons)}")
    return "\n".join(lines)
