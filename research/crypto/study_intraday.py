"""Do intraday technical signals predict anything across the BingX universe?

THE BRIEF THIS TESTS
--------------------
"Read the short-term moves with technical analysis, and when the signal is
strong, go in with high leverage." That is a specific, testable claim with two
separate parts, and they are tested separately here:

  1. DIRECTION — does the signal predict the sign of the next move, net of the
     real BingX cost of acting on it?
  2. STRENGTH — does a STRONGER signal predict a BIGGER move? This is the part
     that decides whether "strong signal, high leverage" is a strategy or a
     superstition. If forward return is flat across signal-strength deciles,
     then sizing by strength adds variance and nothing else, and the leverage
     rule is actively harmful regardless of whether the direction works.

WHY THIS IS NOT A REPEAT
------------------------
The original bot's confluence engine was tested on 10-12 symbols over 40 days
of 5-minute bars. That is a thin sample and a fair objection to the conclusion.
This runs on ~200 symbols over ~160 days of hourly bars — roughly two orders of
magnitude more observations — with costs taken from BingX's live fee schedule
and measured per-symbol spreads rather than an assumed number.

COSTS ARE NOT A DETAIL
----------------------
Measured on the live BingX book today: taker 5bp per side, median spread 6.1bp
in the most-traded quintile and 32.5bp in the least. A round trip is therefore
16bp at best. A signal acted on hourly pays that toll every time, so an edge of
10bp per trade is not "small" — it is negative.

THE REFEREE
-----------
Net of cost > 0; |t| >= 3.0 (Harvey/Liu/Zhu, because many signals are tried at
once); the same sign in both halves of the sample split by date; and breadth
across symbols rather than a result carried by two or three names.

Usage:
    python -m research.crypto.study_intraday
"""

from __future__ import annotations

import argparse
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path("research/crypto/data")
TAKER = 0.0005


def rsi(c: np.ndarray, n: int = 14) -> np.ndarray:
    d = np.diff(c, prepend=c[0])
    up = pd.Series(np.where(d > 0, d, 0.0)).ewm(alpha=1 / n, adjust=False).mean()
    dn = pd.Series(np.where(d < 0, -d, 0.0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50).to_numpy()


def ema(c: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(c).ewm(span=n, adjust=False).mean().to_numpy()


def features(df: pd.DataFrame) -> pd.DataFrame:
    """Signals computed from bars STRICTLY BEFORE the decision bar.

    Everything is shifted by one at the end. The single most common way a
    backtest lies is deciding with the bar it measures, and this project has
    already produced a +2,473,980% return that way.
    """
    c = df.c.to_numpy(dtype=float)
    h = df.h.to_numpy(dtype=float)
    low = df.l.to_numpy(dtype=float)
    v = df.v.to_numpy(dtype=float)
    s = pd.Series(c)

    r = s.pct_change()
    sd20 = r.rolling(20).std()
    ma20 = s.rolling(20).mean()
    sdp20 = s.rolling(20).std()
    macd = ema(c, 12) - ema(c, 26)
    sig = pd.Series(macd).ewm(span=9, adjust=False).mean().to_numpy()
    vm = pd.Series(v).rolling(20).mean()

    out = pd.DataFrame({
        # each is signed: positive means "expect up"
        "rsi_rev": (50 - rsi(c)) / 50,                    # oversold -> long
        "zscore_rev": -((s - ma20) / sdp20.replace(0, np.nan)),
        "macd": (macd - sig) / s.where(s > 0),
        "mom_24": s / s.shift(24) - 1,
        "mom_4": s / s.shift(4) - 1,
        "breakout": (s - pd.Series(h).rolling(24).max().shift(1))
                    / s.where(s > 0),
        "bb_break": ((s - ma20) / sdp20.replace(0, np.nan)),
        "vol_spike": (pd.Series(v) / vm.replace(0, np.nan) - 1)
                     * np.sign(r.fillna(0)),
        "range_pos": (s - pd.Series(low).rolling(24).min())
                     / (pd.Series(h).rolling(24).max()
                        - pd.Series(low).rolling(24).min()).replace(0, np.nan) - 0.5,
    })
    out["vola"] = sd20
    # Decide on bar i using information available at i-1.
    return out.shift(1)


def build(hourly: dict[str, pd.DataFrame], horizons=(1, 4, 12, 24)) -> pd.DataFrame:
    rows = []
    for sym, df in hourly.items():
        if len(df) < 300:
            continue
        f = features(df)
        c = df.c.to_numpy(dtype=float)
        n = len(df)
        fwd = {H: np.full(n, np.nan) for H in horizons}
        for H in horizons:
            fwd[H][:n - H] = c[H:] / c[:n - H] - 1.0
        f = f.assign(sym=sym, t=df.t.to_numpy(),
                     **{f"fwd{H}": fwd[H] for H in horizons})
        rows.append(f)
    out = pd.concat(rows, ignore_index=True)
    return out.replace([np.inf, -np.inf], np.nan)


SIGNALS = ["rsi_rev", "zscore_rev", "macd", "mom_24", "mom_4",
           "breakout", "bb_break", "vol_spike", "range_pos"]


def judge(d: pd.DataFrame, sig: str, H: int, cost: float, q: float = 0.10):
    """Trade the extreme decile in the signal's own direction."""
    x = d[[sig, f"fwd{H}", "t", "sym"]].dropna()
    if len(x) < 500:
        return None
    hi, lo = x[sig].quantile(1 - q), x[sig].quantile(q)
    longs = x[x[sig] >= hi][f"fwd{H}"]
    shorts = -x[x[sig] <= lo][f"fwd{H}"]
    a = pd.concat([longs, shorts]).to_numpy()
    if len(a) < 200:
        return None
    gross = a.mean()
    t = gross / (a.std(ddof=1) / np.sqrt(len(a))) if a.std() > 0 else 0.0
    # int64 nanoseconds: numpy cannot take a median of datetime64 directly.
    dts = pd.concat([x[x[sig] >= hi]["t"], x[x[sig] <= lo]["t"]]).astype(
        "int64").to_numpy()
    mid = np.median(dts)
    h1, h2 = a[dts <= mid], a[dts > mid]
    return {"signal": sig, "H": H, "n": len(a), "gross": gross,
            "net": gross - cost, "t": t,
            "h1": h1.mean() - cost if len(h1) > 50 else np.nan,
            "h2": h2.mean() - cost if len(h2) > 50 else np.nan}


def strength_monotonicity(d: pd.DataFrame, sig: str, H: int) -> list[float]:
    """Mean forward return by signal-strength decile, in the signal's direction.

    This is the leverage question. If these are flat, sizing by strength buys
    nothing but variance.
    """
    x = d[[sig, f"fwd{H}"]].dropna()
    if len(x) < 2000:
        return []
    x = x.assign(b=pd.qcut(x[sig].rank(method="first"), 10, labels=False))
    means = x.groupby("b")[f"fwd{H}"].mean()
    return [float(v) for v in means]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, default=0.0016,
                    help="round trip: 2x5bp taker + 6bp spread")
    a = ap.parse_args()

    hourly = pickle.loads((DATA / "hourly.pkl").read_bytes())
    d = build(hourly)
    print(f"{d.sym.nunique()} symbols · {len(d):,} hourly observations · "
          f"{d.t.min()} → {d.t.max()}")
    print(f"cost charged {a.cost*100:.2f}% round trip "
          f"(BingX taker 5bp x2 + 6bp median spread)\n")

    print("PART 1 — DOES DIRECTION WORK? (extreme decile, traded in signal direction)")
    print(f"  {'':4s} {'signal':12s} {'H':>3s} {'n':>7s} {'gross':>9s} {'net':>9s} "
          f"{'t':>7s} {'half1':>8s} {'half2':>8s}")
    print("  " + "-" * 76)
    results = []
    for sig in SIGNALS:
        for H in (1, 4, 12, 24):
            r = judge(d, sig, H, a.cost)
            if r is None:
                continue
            results.append(r)
            ok = (r["net"] > 0 and abs(r["t"]) >= 3.0
                  and r["h1"] > 0 and r["h2"] > 0)
            print(f"  {'PASS' if ok else '    '} {sig:12s} {H:3d} {r['n']:7d} "
                  f"{r['gross']*100:+8.3f}% {r['net']*100:+8.3f}% {r['t']:+7.2f} "
                  f"{r['h1']*100:+7.3f}% {r['h2']*100:+7.3f}%")

    surv = [r for r in results if r["net"] > 0 and abs(r["t"]) >= 3.0
            and r["h1"] > 0 and r["h2"] > 0]
    print(f"\n  surviving: {len(surv)}/{len(results)}")
    best = max(results, key=lambda r: r["gross"]) if results else None
    if best:
        print(f"  largest GROSS edge found: {best['signal']} at {best['H']}h = "
              f"{best['gross']*100:+.3f}% vs {a.cost*100:.2f}% cost")

    print("\n\nPART 2 — DOES SIGNAL STRENGTH PREDICT MOVE SIZE?")
    print("  (mean forward return by strength decile — the leverage question)")
    print(f"  {'signal':12s} {'H':>3s} " + " ".join(f"{i+1:>6d}" for i in range(10))
          + "   monotone?")
    print("  " + "-" * 88)
    for sig in SIGNALS:
        for H in (4, 24):
            m = strength_monotonicity(d, sig, H)
            if not m:
                continue
            sp = np.corrcoef(np.arange(10), m)[0, 1]
            cells = " ".join(f"{v*100:+6.2f}" for v in m)
            print(f"  {sig:12s} {H:3d} {cells}  r={sp:+.2f}")
    print("\n  r near 0 means strength carries no information about size, and")
    print("  sizing by strength therefore adds variance without adding return.")


if __name__ == "__main__":
    main()
