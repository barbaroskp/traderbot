"""Study 1 — does the opening-gap fade survive a wider universe?

The effect was first seen on 30 BIST-30 names with the ambiguous bracket cases
assigned against us. Both of those choices are suspect: 30 large caps is a
narrow, hindsight-flavoured slice of the tape, and forcing ambiguous days to
losses biases the LONG side down and the SHORT side up — which is exactly the
direction of the original result. This re-runs it on 136 names with ambiguity
excluded and reported.

Usage:  python -m research.bist.study_gap [cost_pct]
"""

from __future__ import annotations

import pickle
import sys
from pathlib import Path

import pandas as pd

from research.bist import events, referee
from research.bist.universe import DATA, load_universe

CACHE = DATA / "events_60m_2y.pkl"


def event_table(refresh: bool = False) -> pd.DataFrame:
    if CACHE.exists() and not refresh:
        return pickle.loads(CACHE.read_bytes())
    bars = load_universe(interval="60m", period="2y")
    df = events.build(bars)
    CACHE.write_bytes(pickle.dumps(df))
    return df


def main() -> None:
    cost = float(sys.argv[1]) / 100 if len(sys.argv) > 1 else 0.002
    df = event_table()
    print(f"{len(df):,} ticker-days, {df.tic.nunique()} tickers, "
          f"{df.date.min()} → {df.date.max()}")

    amb = (df["L1_1_how"] == "ambiguous").mean()
    print(f"ambiguous (both bracket levels inside one hourly bar): {amb*100:.1f}% "
          f"— excluded from every test below, not assigned to either side\n")

    rules: list[tuple[str, object, str, str]] = []
    for g in (0.02, 0.03, 0.04, 0.05):
        m = df.gap > g
        rules.append((f"gap>+{g*100:.0f}% SHORT ±1%", m, "S1_1", "S1_1_how"))
        rules.append((f"gap>+{g*100:.0f}% LONG  ±1%", m, "L1_1", "L1_1_how"))
        rules.append((f"gap>+{g*100:.0f}% SHORT open→close", m, "oc_short", None))
    for g in (0.02, 0.03, 0.04):
        m = df.gap < -g
        rules.append((f"gap<-{g*100:.0f}% LONG  ±1%", m, "L1_1", "L1_1_how"))
        rules.append((f"gap<-{g*100:.0f}% SHORT ±1%", m, "S1_1", "S1_1_how"))
        rules.append((f"gap<-{g*100:.0f}% LONG  open→close", m, "oc", None))

    df["oc_short"] = -df["oc"]

    # liquidity split: is the effect only in illiquid names (untradeable)?
    liq = df.turnover21.quantile(0.5)
    rules += [
        ("gap>+3% SHORT, liquid half", (df.gap > 0.03) & (df.turnover21 >= liq),
         "S1_1", "S1_1_how"),
        ("gap>+3% SHORT, illiquid half", (df.gap > 0.03) & (df.turnover21 < liq),
         "S1_1", "S1_1_how"),
        ("gap>+3% SHORT, prev-day volume x2", (df.gap > 0.03) & (df.vol_ratio_prev > 2),
         "S1_1", "S1_1_how"),
        ("gap>+3% SHORT, ±2% bracket", df.gap > 0.03, "S2_2", "S2_2_how"),
        ("gap>+3% SHORT, TP2/SL1", df.gap > 0.03, "S2_1", "S2_1_how"),
        ("gap>+3% + downtrend r21<0 SHORT", (df.gap > 0.03) & (df.r21 < 0),
         "S1_1", "S1_1_how"),
        ("gap>+3% + uptrend r21>0 SHORT", (df.gap > 0.03) & (df.r21 > 0),
         "S1_1", "S1_1_how"),
        ("baseline: every day LONG ±1%", pd.Series(True, index=df.index),
         "L1_1", "L1_1_how"),
        ("baseline: every day SHORT ±1%", pd.Series(True, index=df.index),
         "S1_1", "S1_1_how"),
    ]

    verdicts = [referee.judge(df, m, col, cost, lbl, how) for lbl, m, col, how in rules]
    print(referee.report(verdicts, "STUDY 1 — OPENING GAP, 136 BIST NAMES, 2 YEARS",
                         len(rules), cost))

    # cost sensitivity on the headline rule
    v = referee.judge(df, df.gap > 0.03, "S1_1", 0.0, "x", "S1_1_how")
    if v:
        print(f"\ncost sensitivity, gap>+3% SHORT (gross {v.gross*100:+.3f}%, n={v.n}):")
        for c in (0.001, 0.002, 0.003, 0.004, 0.005):
            per = v.gross - c
            yr = (1 + per) ** (v.n / 2)
            print(f"   {c*100:.1f}% → {per*100:+.3f}%/trade   "
                  f"{v.n/2:.0f} trades/yr → {(yr-1)*100:+7.1f}%/yr")


if __name__ == "__main__":
    main()
