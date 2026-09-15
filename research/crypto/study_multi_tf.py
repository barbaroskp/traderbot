"""Which analysis works, at which timeframe? The map.

THE FRAMING THAT MATTERS
------------------------
A round trip on a liquid BingX perpetual costs about 16 basis points and that
number does not care what chart you are looking at. What changes across
timeframes is how far price moves in a bar. So the quantity that decides
whether ANY signal can pay for itself is:

    cost / typical move

which runs from roughly 150% at one minute to a few percent at one day. A
signal on the one-minute chart must be many times more accurate than the same
signal on the daily chart just to break even. Every result below is therefore
reported both in raw basis points AND as a fraction of the move available,
because only the second number is comparable across frames.

MARKET NEUTRAL, ALWAYS
----------------------
Every forward return is demeaned cross-sectionally at its own timestamp. Crypto
rose over this sample; without that adjustment every long signal looks
profitable and every short signal looks broken, which is a measurement of the
market rather than of the signal. Skipping this step nearly produced a false
positive earlier in this project.

WHAT THE SHORT FRAMES CANNOT PROVE
----------------------------------
The vendor caps klines at 1000 bars per request. Even paginated, the 1-minute
series covers days rather than months, so a result there is measured over a
single market regime. That is a real limit and it is printed with the results
rather than hidden.

Usage:
    python -m research.crypto.study_multi_tf
"""

from __future__ import annotations

import argparse
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path("research/crypto/data")

# Bars held, per timeframe. Chosen so the holding period is comparable in
# clock time across frames rather than in bar count.
HORIZONS = {
    "1m": (5, 30, 120),
    "5m": (3, 12, 48),
    "15m": (2, 8, 32),
    "30m": (2, 8, 24),
    "1h": (4, 12, 24),
    "4h": (1, 3, 6),
    "1d": (1, 3, 7),
}

BAR_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60,
               "4h": 240, "1d": 1440}


def ema(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy()


def rsi(c: np.ndarray, n: int = 14) -> np.ndarray:
    d = np.diff(c, prepend=c[0])
    up = pd.Series(np.where(d > 0, d, 0.0)).ewm(alpha=1 / n, adjust=False).mean()
    dn = pd.Series(np.where(d < 0, -d, 0.0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50).to_numpy()


def signals(df: pd.DataFrame) -> pd.DataFrame:
    """The battery. Every column is signed so positive means 'expect up'."""
    c = df.c.to_numpy(float)
    h = df.h.to_numpy(float)
    lo = df.l.to_numpy(float)
    v = df.v.to_numpy(float)
    s = pd.Series(c)
    r = s.pct_change()
    ma20, sd20 = s.rolling(20).mean(), s.rolling(20).std()
    ma50 = s.rolling(50).mean()
    macd = ema(c, 12) - ema(c, 26)
    sigl = pd.Series(macd).ewm(span=9, adjust=False).mean().to_numpy()
    atr = (pd.Series(h - lo)).rolling(14).mean()
    vm = pd.Series(v).rolling(20).mean()
    hi24, lo24 = pd.Series(h).rolling(24).max(), pd.Series(lo).rolling(24).min()

    out = pd.DataFrame({
        "trend_ma":    (s - ma50) / ma50.replace(0, np.nan),
        "trend_cross": (ma20 - ma50) / ma50.replace(0, np.nan),
        "macd":        (macd - sigl) / s.where(s > 0),
        "rsi_rev":     (50 - rsi(c)) / 50,
        "bb_rev":      -((s - ma20) / sd20.replace(0, np.nan)),
        "bb_break":    ((s - ma20) / sd20.replace(0, np.nan)),
        "breakout":    (s - hi24.shift(1)) / s.where(s > 0),
        "breakdown":   -(s - lo24.shift(1)) / s.where(s > 0),
        "mom_short":   s / s.shift(6) - 1,
        "mom_long":    s / s.shift(48) - 1,
        "rev_short":   -(s / s.shift(6) - 1),
        "vol_spike":   (pd.Series(v) / vm.replace(0, np.nan) - 1) * np.sign(r.fillna(0)),
        "range_pos":   (s - lo24) / (hi24 - lo24).replace(0, np.nan) - 0.5,
        "atr_squeeze": -(atr / s.where(s > 0)),
    })
    # Decide at bar i using only bars < i.
    return out.shift(1)


NAMES = ["trend_ma", "trend_cross", "macd", "rsi_rev", "bb_rev", "bb_break",
         "breakout", "breakdown", "mom_short", "mom_long", "rev_short",
         "vol_spike", "range_pos", "atr_squeeze"]


def panel(store: dict[str, pd.DataFrame], horizons: tuple[int, ...]) -> pd.DataFrame:
    rows = []
    for sym, df in store.items():
        if len(df) < 200:
            continue
        f = signals(df)
        c = df.c.to_numpy(float)
        n = len(df)
        for H in horizons:
            fwd = np.full(n, np.nan)
            fwd[:n - H] = c[H:] / c[:n - H] - 1.0
            f[f"fwd{H}"] = fwd
        f["sym"] = sym
        f["t"] = df.t.to_numpy()
        rows.append(f)
    if not rows:
        return pd.DataFrame()
    d = pd.concat(rows, ignore_index=True).replace([np.inf, -np.inf], np.nan)
    for H in horizons:
        d[f"ex{H}"] = d[f"fwd{H}"] - d.groupby("t")[f"fwd{H}"].transform("mean")
    return d


def evaluate(d: pd.DataFrame, sig: str, H: int, cost: float, q: float = 0.10):
    x = d[[sig, f"ex{H}", f"fwd{H}", "t", "sym"]].dropna()
    if len(x) < 3000:
        return None
    hi, lo = x[sig].quantile(1 - q), x[sig].quantile(q)
    long_, short_ = x[x[sig] >= hi], x[x[sig] <= lo]
    pnl = np.concatenate([long_[f"ex{H}"].to_numpy(),
                          -short_[f"ex{H}"].to_numpy()])
    if len(pnl) < 1000:
        return None
    m = float(pnl.mean())
    t = m / (pnl.std(ddof=1) / np.sqrt(len(pnl))) if pnl.std() > 0 else 0.0
    dts = np.concatenate([long_["t"].astype("int64").to_numpy(),
                          short_["t"].astype("int64").to_numpy()])
    mid = np.median(dts)
    a, b = pnl[dts <= mid], pnl[dts > mid]
    syms = np.concatenate([long_["sym"].to_numpy(), short_["sym"].to_numpy()])
    by = pd.Series(pnl).groupby(syms).mean()
    top10 = by.nlargest(10).index
    ex_top = float(pd.Series(pnl)[~pd.Series(syms).isin(top10).to_numpy()].mean())
    return {"sig": sig, "H": H, "n": len(pnl), "gross": m, "net": m - cost, "t": t,
            "h1": a.mean() - cost if len(a) > 200 else np.nan,
            "h2": b.mean() - cost if len(b) > 200 else np.nan,
            "ex_top10": ex_top - cost,
            "move": float(x[f"fwd{H}"].abs().mean())}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, default=0.0016)
    a = ap.parse_args()

    frames = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
    print("COST-TO-MOVE: the ratio that decides whether any signal can pay\n")
    print(f"  {'frame':6s} {'symbols':>8s} {'bars':>7s} {'span':>10s} "
          f"{'typ move':>10s} {'cost/move':>10s}")
    print("  " + "-" * 58)

    loaded: dict[str, pd.DataFrame] = {}
    for tf in frames:
        p = DATA / (f"tf_{tf}.pkl" if tf not in ("1h", "1d")
                    else ("hourly.pkl" if tf == "1h" else "daily_top400.pkl"))
        if not p.exists():
            continue
        store = pickle.loads(p.read_bytes())
        if not store:
            continue
        d = panel(store, HORIZONS[tf])
        if d.empty:
            continue
        loaded[tf] = d
        k = next(iter(store))
        span = store[k].t.iloc[-1] - store[k].t.iloc[0]
        lens = sorted(len(v) for v in store.values())
        h1 = HORIZONS[tf][1]
        move = float(d[f"fwd{h1}"].abs().mean())
        print(f"  {tf:6s} {len(store):8d} {lens[len(lens)//2]:7d} "
              f"{span.days:6d}d    {move*100:8.2f}% "
              f"{a.cost/move*100:9.0f}%")

    print(f"\n\nSIGNAL MAP — net edge per trade, market-neutral, "
          f"cost {a.cost*100:.2f}%")
    print("  PASS needs: net>0, |t|>=3, both halves>0, survives dropping "
          "the best 10 symbols\n")
    survivors = []
    for tf, d in loaded.items():
        H = HORIZONS[tf][1]
        rows = []
        for sig in NAMES:
            r = evaluate(d, sig, H, a.cost)
            if r:
                rows.append(r)
        if not rows:
            continue
        rows.sort(key=lambda r: -r["net"])
        hold = H * BAR_MINUTES[tf]
        print(f"  {tf}  (hold {H} bars = {hold//60}h{hold%60:02d}m, "
              f"typ move {rows[0]['move']*100:.2f}%)")
        print(f"    {'':4s} {'signal':12s} {'gross':>8s} {'net':>8s} {'t':>6s} "
              f"{'half1':>8s} {'half2':>8s} {'ex-top10':>9s}")
        for r in rows[:5]:
            ok = (r["net"] > 0 and abs(r["t"]) >= 3.0
                  and r["h1"] > 0 and r["h2"] > 0 and r["ex_top10"] > 0)
            if ok:
                survivors.append((tf, r))
            print(f"    {'PASS' if ok else '    '} {r['sig']:12s} "
                  f"{r['gross']*100:+7.3f}% {r['net']*100:+7.3f}% {r['t']:+6.2f} "
                  f"{r['h1']*100:+7.3f}% {r['h2']*100:+7.3f}% "
                  f"{r['ex_top10']*100:+8.3f}%")
        print()

    print(f"SURVIVORS: {len(survivors)}")
    for tf, r in survivors:
        print(f"  {tf} {r['sig']} H={r['H']} net {r['net']*100:+.3f}%/trade "
              f"t={r['t']:+.2f}")
    if not survivors:
        print("  none cleared all four checks")


if __name__ == "__main__":
    main()
