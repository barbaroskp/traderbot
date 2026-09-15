"""Study 2 — is the gap "edge" real, or an artefact of the opening print?

WHY THIS STUDY EXISTS
---------------------
Study 1 reported an effect that got STRONGER when the universe widened, is
SYMMETRIC (gaps up fade, gaps down recover), and reaches t=+16. In this project
that combination has meant a bug four times out of four. A symmetric reversion
measured FROM a price is the signature of that price being noisy: if the open
print is stale, an odd lot, or an auction imbalance, then every large apparent
gap is partly measurement error, and measurement error always "reverts".

THE DECISIVE TEST
-----------------
Enter later. Real intraday drift is a property of the day and survives a delay;
a bad opening print is a property of one tick and cannot.

  t0 entry: first bar open      <- contaminated by the print, if it is bad
  t1 entry: second bar open     <- one full hour of trading later
  t2 entry: third bar open

If the edge decays to nothing by t1, the "signal" was the price we measured
from, not the market. If it survives, we have something.

SECOND TEST — clean reference
-----------------------------
Recompute the same rules using yesterday's close → today's CLOSE. That path
never touches the suspect open. An effect that exists on the clean path is
economics; one that only exists on the open path is measurement.

Usage:  python -m research.bist.study_artifact
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.bist import events
from research.bist.universe import load_universe


def resolve(highs, lows, closes, entry_idx: int, entry_px: float,
            tp: float, sl: float, side: str) -> tuple[float, str]:
    """Bracket from a chosen bar onward, entering at entry_px."""
    up = entry_px * (1 + (tp if side == "LONG" else sl))
    dn = entry_px * (1 - (sl if side == "LONG" else tp))
    for j in range(entry_idx, len(highs)):
        u, d = highs[j] >= up, lows[j] <= dn
        if u and d:
            return 0.0, "ambiguous"
        if u:
            return (tp if side == "LONG" else -sl), "hit"
        if d:
            return (-sl if side == "LONG" else tp), "hit"
    r = (closes[-1] - entry_px) / entry_px
    return (r if side == "LONG" else -r), "close"


def main() -> None:
    bars = load_universe(interval="60m", period="2y")
    rows = []
    for tic, df in bars.items():
        days = events.to_days(df)
        for i in range(1, len(days)):
            d, prev = days[i], days[i - 1]
            if prev.close <= 0 or d.open <= 0 or len(d.highs) < 4:
                continue
            gap = (d.open - prev.close) / prev.close
            if abs(gap) > 0.30:
                continue
            row = {"tic": tic, "date": d.date, "gap": gap,
                   "gap_cc": (d.close - prev.close) / prev.close}
            for k in (0, 1, 2):
                if k >= len(d.highs) - 1:
                    row[f"s{k}"] = np.nan
                    row[f"oc{k}"] = np.nan
                    continue
                # entering at bar k's open == previous bar's close for k>0
                px = d.open if k == 0 else float(d.closes[k - 1])
                if px <= 0:
                    row[f"s{k}"] = np.nan
                    row[f"oc{k}"] = np.nan
                    continue
                r, how = resolve(d.highs, d.lows, d.closes, k, px, 0.01, 0.01, "SHORT")
                row[f"s{k}"] = np.nan if how == "ambiguous" else r
                row[f"oc{k}"] = -(float(d.closes[-1]) - px) / px   # short open→close
            rows.append(row)
    D = pd.DataFrame(rows)
    print(f"{len(D):,} ticker-days, {D.tic.nunique()} tickers\n")

    def stat(x):
        x = x.dropna()
        if len(x) < 30:
            return None
        m = x.mean()
        return len(x), m, m / (x.std(ddof=1) / np.sqrt(len(x)))

    print("TEST A — DELAY THE ENTRY (gap>+3%, SHORT, ±1% bracket)")
    print("  a real intraday drift survives an hour; a bad opening print does not\n")
    print(f"  {'entry':28s} {'n':>6s} {'gross':>9s} {'t':>7s}")
    print("  " + "-" * 54)
    m = D.gap > 0.03
    for k, lbl in ((0, "t0: first bar OPEN"), (1, "t1: +1 hour"), (2, "t2: +2 hours")):
        s = stat(D[m][f"s{k}"])
        if s:
            print(f"  {lbl:28s} {s[0]:6d} {s[1]*100:+8.3f}% {s[2]:+7.2f}")
    print()
    print(f"  {'entry (open→close leg)':28s} {'n':>6s} {'gross':>9s} {'t':>7s}")
    print("  " + "-" * 54)
    for k, lbl in ((0, "t0: first bar OPEN"), (1, "t1: +1 hour"), (2, "t2: +2 hours")):
        s = stat(D[m][f"oc{k}"])
        if s:
            print(f"  {lbl:28s} {s[0]:6d} {s[1]*100:+8.3f}% {s[2]:+7.2f}")

    print("\n\nTEST B — DEFINE THE GAP WITHOUT THE OPEN PRINT")
    print("  'gap_cc' = yesterday close → today CLOSE. Never touches the open.")
    print("  If reversion is real it should show up on the NEXT day too.\n")
    D = D.sort_values(["tic", "date"])
    D["next_oc0"] = D.groupby("tic")["oc0"].shift(-1)
    D["next_s0"] = D.groupby("tic")["s0"].shift(-1)
    print(f"  {'rule':34s} {'n':>6s} {'gross':>9s} {'t':>7s}")
    print("  " + "-" * 60)
    for lbl, mm, col in [
        ("cc-gap>+3% → next day SHORT ±1%", D.gap_cc > 0.03, "next_s0"),
        ("cc-gap>+3% → next day SHORT o→c", D.gap_cc > 0.03, "next_oc0"),
        ("cc-gap<-3% → next day LONG ±1%", D.gap_cc < -0.03, "next_s0"),
        ("open-gap>+3% → SAME day SHORT ±1%", D.gap > 0.03, "s0"),
    ]:
        x = D[mm][col].dropna()
        if lbl.startswith("cc-gap<"):
            x = -x
        s = stat(x)
        if s:
            print(f"  {lbl:34s} {s[0]:6d} {s[1]*100:+8.3f}% {s[2]:+7.2f}")

    print("\n\nTEST C — HOW ODD IS THE OPENING PRINT?")
    D["o_is_extreme"] = np.nan
    ex = []
    for tic, df in bars.items():
        for d in events.to_days(df):
            if len(d.highs) < 4 or d.open <= 0:
                continue
            # is the open the day's high or low? a fair open rarely is
            ex.append((abs(d.open - d.high) < 1e-9, abs(d.open - d.low) < 1e-9))
    ex = np.array(ex)
    print(f"  open == day HIGH on {ex[:,0].mean()*100:5.1f}% of days")
    print(f"  open == day LOW  on {ex[:,1].mean()*100:5.1f}% of days")
    print(f"  (a well-behaved open is an extreme of the day only rarely;")
    print(f"   high numbers mean the print sits outside the real trading range)")

    big = D[D.gap.abs() > 0.03]
    print(f"\n  on |gap|>3% days specifically:")
    rows2 = []
    for tic, df in bars.items():
        days = events.to_days(df)
        for i in range(1, len(days)):
            d, prev = days[i], days[i - 1]
            if prev.close <= 0 or d.open <= 0 or len(d.highs) < 4:
                continue
            g = (d.open - prev.close) / prev.close
            if abs(g) > 0.30 or abs(g) < 0.03:
                continue
            rows2.append((abs(d.open - d.high) < 1e-9, abs(d.open - d.low) < 1e-9))
    r2 = np.array(rows2)
    print(f"    open == day HIGH: {r2[:,0].mean()*100:5.1f}%   "
          f"open == day LOW: {r2[:,1].mean()*100:5.1f}%")


if __name__ == "__main__":
    main()
