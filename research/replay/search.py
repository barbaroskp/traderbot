"""What algorithm WOULD have been profitable over these 41 days?

A DIFFERENT QUESTION FROM EVERYTHING BEFORE IT
-----------------------------------------------
Every earlier study tested the bot's OWN rules and found them worthless: 22
indicators across 93,193 votes, none clearing cost; 16 parameter configurations,
none profitable in either half of the sample. Those answer "is the bot any
good". They do not answer "was there anything to find".

This asks the second question directly. It searches a large space of simple,
mechanical rules — entry signal, threshold, direction, holding period, take
profit, stop loss, liquidity tier — and reports which ones would have made
money over the window, with the bot's real cost structure charged.

THE ANSWER IS GUARANTEED TO BE "YES", AND THAT IS THE POINT
-----------------------------------------------------------
Search a few thousand rules over 41 days and some will be profitable. That is
arithmetic, not discovery: with ~3,000 trials, the best in-sample result is
what the extreme tail of a null distribution looks like. So the output is
deliberately structured in two columns — what won the FIT window, and what that
same rule did in the TEST window — and the honest finding is the relationship
between them, not the first column.

If the best fit-window rules cluster near zero out of sample, then the correct
answer to "what would have worked" is "nothing that could have been known in
advance", which is a different and more useful statement than "nothing worked".

Usage:
    python -m research.replay.search --days 40
"""

from __future__ import annotations

import argparse
import itertools
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path("research/replay/data")


def ema(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy()


def rsi(c: np.ndarray, n: int = 14) -> np.ndarray:
    d = np.diff(c, prepend=c[0])
    up = pd.Series(np.where(d > 0, d, 0.0)).ewm(alpha=1 / n, adjust=False).mean()
    dn = pd.Series(np.where(d < 0, -d, 0.0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50).to_numpy()


def features(df: pd.DataFrame) -> pd.DataFrame:
    """Signals from bars strictly before the decision bar."""
    c = df.c.to_numpy(float)
    h = df.h.to_numpy(float)
    lo = df.l.to_numpy(float)
    v = df.v.to_numpy(float)
    s = pd.Series(c)
    r = s.pct_change()
    ma20, sd20 = s.rolling(20).mean(), s.rolling(20).std()
    ma100 = s.rolling(100).mean()
    macd = ema(c, 12) - ema(c, 26)
    sig = pd.Series(macd).ewm(span=9, adjust=False).mean().to_numpy()
    vm = pd.Series(v).rolling(48).mean()
    hi48 = pd.Series(h).rolling(48).max()
    lo48 = pd.Series(lo).rolling(48).min()
    out = pd.DataFrame({
        "rsi": rsi(c),
        "z20": (s - ma20) / sd20.replace(0, np.nan),
        "trend100": (s - ma100) / ma100.replace(0, np.nan),
        "macdh": (macd - sig) / s.where(s > 0),
        "mom12": s / s.shift(12) - 1,
        "mom48": s / s.shift(48) - 1,
        "mom288": s / s.shift(288) - 1,
        "volr": pd.Series(v) / vm.replace(0, np.nan),
        "rngpos": (s - lo48) / (hi48 - lo48).replace(0, np.nan),
        "vola": r.rolling(48).std(),
    })
    return out.shift(1)


SPECS = {
    "rsi_low":     ("rsi", "<", [25, 30, 35], +1),
    "rsi_high":    ("rsi", ">", [65, 70, 75], -1),
    "rsi_high_L":  ("rsi", ">", [65, 70, 75], +1),
    "rsi_low_S":   ("rsi", "<", [25, 30, 35], -1),
    "z_low":       ("z20", "<", [-1.5, -2.0, -2.5], +1),
    "z_high":      ("z20", ">", [1.5, 2.0, 2.5], -1),
    "z_high_L":    ("z20", ">", [1.5, 2.0, 2.5], +1),
    "z_low_S":     ("z20", "<", [-1.5, -2.0, -2.5], -1),
    "trend_up":    ("trend100", ">", [0.02, 0.05, 0.10], +1),
    "trend_dn":    ("trend100", "<", [-0.02, -0.05, -0.10], -1),
    "trend_up_S":  ("trend100", ">", [0.02, 0.05, 0.10], -1),
    "trend_dn_L":  ("trend100", "<", [-0.02, -0.05, -0.10], +1),
    "mom12_up":    ("mom12", ">", [0.01, 0.02, 0.04], +1),
    "mom12_dn":    ("mom12", "<", [-0.01, -0.02, -0.04], +1),
    "mom48_up":    ("mom48", ">", [0.02, 0.05, 0.10], +1),
    "mom48_dn":    ("mom48", "<", [-0.02, -0.05, -0.10], +1),
    "mom288_up":   ("mom288", ">", [0.05, 0.10, 0.20], +1),
    "mom288_dn":   ("mom288", "<", [-0.05, -0.10, -0.20], +1),
    "volspike":    ("volr", ">", [2.0, 3.0, 5.0], +1),
    "volspike_S":  ("volr", ">", [2.0, 3.0, 5.0], -1),
    "breakout":    ("rngpos", ">", [0.95, 0.99], +1),
    "breakdown":   ("rngpos", "<", [0.05, 0.01], +1),
    "breakout_S":  ("rngpos", ">", [0.95, 0.99], -1),
    "macd_pos":    ("macdh", ">", [0.0, 0.001], +1),
    "macd_neg":    ("macdh", "<", [0.0, -0.001], +1),
}

HOLDS = [12, 36, 96, 288]          # 1h, 3h, 8h, 24h on 5m bars
BRACKETS = [(None, None), (0.02, 0.01), (0.03, 0.015), (0.05, 0.02)]


def evaluate(F: dict, PX: dict, mask_fn, side: int, hold: int,
             tp, sl, cost: float, cut_i: int):
    """Return (fit_mean, fit_n, test_mean, test_n) in fraction-of-notional."""
    fit, test = [], []
    for sym, f in F.items():
        px = PX[sym]
        c = px["c"]
        h = px["h"]
        lo = px["l"]
        n = len(c)
        m = mask_fn(f)
        idx = np.flatnonzero(m.to_numpy(dtype=bool))
        idx = idx[(idx > 300) & (idx < n - hold - 1)]
        if idx.size == 0:
            continue
        # Enforce one position per symbol at a time: skip entries that fall
        # inside an existing hold, or the same move is counted many times.
        kept, last_exit = [], -1
        for i in idx:
            if i > last_exit:
                kept.append(i)
                last_exit = i + hold
        for i in kept:
            entry = c[i]
            if not np.isfinite(entry) or entry <= 0:
                continue
            seg_h, seg_l = h[i + 1:i + 1 + hold], lo[i + 1:i + 1 + hold]
            if tp is None:
                ret = (c[i + hold] / entry - 1.0) * side
            else:
                up = entry * (1 + (tp if side > 0 else sl))
                dn = entry * (1 - (sl if side > 0 else tp))
                hit_u = np.flatnonzero(seg_h >= up)
                hit_d = np.flatnonzero(seg_l <= dn)
                fu = hit_u[0] if hit_u.size else 10 ** 9
                fd = hit_d[0] if hit_d.size else 10 ** 9
                if fu == fd == 10 ** 9:
                    ret = (c[i + hold] / entry - 1.0) * side
                elif fu == fd:
                    ret = -sl            # same bar: resolve against us
                elif fu < fd:
                    ret = tp if side > 0 else -sl
                else:
                    ret = -sl if side > 0 else tp
            (fit if i <= cut_i else test).append(ret - cost)
    return (float(np.mean(fit)) if fit else np.nan, len(fit),
            float(np.mean(test)) if test else np.nan, len(test))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--cost", type=float, default=0.0016)
    ap.add_argument("--symbols", type=int, default=40)
    ap.add_argument("--min-trades", type=int, default=60)
    a = ap.parse_args()

    m5 = pickle.loads((DATA / f"5m_{a.days}d.pkl").read_bytes())
    spreads = pickle.loads((DATA / "spreads.pkl").read_bytes())
    order = [s for s in spreads.sort_values("quoteVolume", ascending=False).index
             if s in m5][:a.symbols]

    F, PX = {}, {}
    for s in order:
        d = m5[s].reset_index(drop=True)
        if len(d) < 1000:
            continue
        F[s] = features(d)
        PX[s] = {"c": d.c.to_numpy(float), "h": d.h.to_numpy(float),
                 "l": d.l.to_numpy(float)}
    any_len = len(next(iter(PX.values()))["c"])
    cut_i = any_len // 2
    print(f"{len(F)} symbols · {any_len:,} 5-minute bars · "
          f"fit = first half, test = second half · cost {a.cost*100:.2f}%\n")

    results = []
    for name, (col, op, thresholds, side) in SPECS.items():
        for thr, hold, (tp, sl) in itertools.product(thresholds, HOLDS, BRACKETS):
            if op == "<":
                fn = lambda f, c=col, t=thr: f[c] < t
            else:
                fn = lambda f, c=col, t=thr: f[c] > t
            fm, fn_, tm, tn = evaluate(F, PX, fn, side, hold, tp, sl,
                                       a.cost, cut_i)
            if fn_ < a.min_trades or tn < a.min_trades:
                continue
            results.append({
                "rule": f"{name} {op}{thr} h={hold} "
                        f"{'tp/sl ' + str(tp) + '/' + str(sl) if tp else 'no bracket'}",
                "fit": fm, "fit_n": fn_, "test": tm, "test_n": tn,
            })

    R = pd.DataFrame(results)
    if R.empty:
        print("nothing had enough trades")
        return
    R = R.sort_values("fit", ascending=False)
    print(f"{len(R):,} rules tested\n")

    print("TOP 15 BY FIT WINDOW — and what they then did in the test window")
    print(f"  {'rule':52s} {'FIT':>9s} {'n':>6s} {'TEST':>9s} {'n':>6s}")
    print("  " + "-" * 86)
    for _, r in R.head(15).iterrows():
        print(f"  {r['rule']:52s} {r['fit']*100:+8.3f}% {r['fit_n']:6d} "
              f"{r['test']*100:+8.3f}% {r['test_n']:6d}")

    good_fit = R[R.fit > 0]
    both = R[(R.fit > 0) & (R.test > 0)]
    print(f"\n  profitable in FIT:  {len(good_fit):,}/{len(R):,} "
          f"({len(good_fit)/len(R)*100:.0f}%)")
    print(f"  profitable in BOTH: {len(both):,}/{len(R):,} "
          f"({len(both)/len(R)*100:.0f}%)")

    corr = R[["fit", "test"]].dropna().corr().iloc[0, 1]
    print(f"\n  correlation between fit and test performance: {corr:+.3f}")
    print("  this is the number that matters: near zero means the fit-window")
    print("  winners were noise and could not have been picked in advance.")

    top50 = R.head(50)
    print(f"\n  the 50 best fit-window rules averaged "
          f"{top50.fit.mean()*100:+.3f}% in fit and "
          f"{top50.test.mean()*100:+.3f}% in test")
    print(f"  all {len(R):,} rules averaged {R.fit.mean()*100:+.3f}% / "
          f"{R.test.mean()*100:+.3f}%")

    if len(both):
        print(f"\n  rules positive in both windows, best by TEST:")
        for _, r in both.nlargest(8, "test").iterrows():
            print(f"    {r['rule']:52s} fit {r['fit']*100:+.3f}%  "
                  f"test {r['test']*100:+.3f}%")


if __name__ == "__main__":
    main()
