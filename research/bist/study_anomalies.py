"""Study 3 — do the documented equity anomalies show up on BIST, and what
would simply holding a basket have done?

SURVIVORSHIP WARNING (read before believing any number here)
------------------------------------------------------------
The universe is the set of tickers that still resolve at the vendor TODAY.
Companies that were delisted, merged, or collapsed over the sample are absent.
That biases every long-only result UPWARD, and it biases momentum upward too
(the names that went to zero are precisely the ones a loser portfolio would
have held). The numbers below are therefore OPTIMISTIC by an unknown margin.
They are still useful for RELATIVE comparisons — buy-and-hold vs rebalanced vs
a timing rule all inherit the same bias — but the absolute level should not be
taken as an achievable return.

The one comparison that is nearly bias-free is a strategy against buy-and-hold
on the SAME universe, which is how every result here is framed.

Usage:  python -m research.bist.study_anomalies
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.bist.universe import load_universe

COST = 0.002          # round-trip, charged on turnover


def panel(bars: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Wide close-price panel, business-day indexed."""
    ser = {}
    for t, df in bars.items():
        s = df["Close"].copy()
        s.index = pd.to_datetime([d.date() for d in s.index])
        ser[t] = s[~s.index.duplicated(keep="last")]
    px = pd.DataFrame(ser).sort_index()
    return px[px.index >= px.index[0]]


def ann(total: float, years: float) -> float:
    return (1 + total) ** (1 / years) - 1 if total > -1 else -1.0


def maxdd(curve: np.ndarray) -> float:
    peak = np.maximum.accumulate(curve)
    return float((curve / peak - 1).min())


def run_portfolio(px: pd.DataFrame, weights_fn, rebal_days: int,
                  cost: float = COST, label: str = "") -> dict:
    """Replay a monthly-ish rebalanced long-only portfolio.

    weights_fn(history_df) -> dict[ticker, weight] using ONLY data strictly
    before the rebalance date. Returns equity curve stats.
    """
    dates = px.index
    equity = 1.0
    curve, turn = [], 0.0
    held: dict[str, float] = {}
    last_rebal = -10**9

    for i in range(252, len(dates)):
        d = dates[i]
        # mark to market
        if held:
            r = 0.0
            for t, w in held.items():
                p0, p1 = px[t].iloc[i - 1], px[t].iloc[i]
                if np.isfinite(p0) and np.isfinite(p1) and p0 > 0:
                    r += w * (p1 / p0 - 1)
            equity *= (1 + r)
        curve.append(equity)

        if i - last_rebal >= rebal_days:
            hist = px.iloc[:i]
            new = weights_fn(hist)
            if new:
                tv = sum(abs(new.get(t, 0.0) - held.get(t, 0.0))
                         for t in set(new) | set(held))
                equity *= (1 - tv * cost / 2)
                turn += tv
                held = new
                last_rebal = i

    c = np.array(curve)
    years = len(c) / 252
    total = c[-1] - 1
    return {"label": label, "total": total, "cagr": ann(total, years),
            "maxdd": maxdd(c), "turnover_yr": turn / years, "years": years,
            "curve": c}


def w_equal(hist: pd.DataFrame) -> dict:
    live = [t for t in hist.columns if np.isfinite(hist[t].iloc[-1])
            and hist[t].iloc[-252:].notna().sum() > 200]
    return {t: 1 / len(live) for t in live} if live else {}


def _rank_weights(hist: pd.DataFrame, score_fn, n: int, ascending: bool) -> dict:
    live = [t for t in hist.columns if np.isfinite(hist[t].iloc[-1])
            and hist[t].iloc[-252:].notna().sum() > 200]
    if len(live) < n * 2:
        return {}
    sc = {}
    for t in live:
        v = score_fn(hist[t])
        if np.isfinite(v):
            sc[t] = v
    if len(sc) < n * 2:
        return {}
    picks = sorted(sc, key=lambda t: sc[t], reverse=not ascending)[:n]
    return {t: 1 / n for t in picks}


def main() -> None:
    bars = load_universe(interval="1d", period="10y", min_bars=500)
    px = panel(bars)
    print(f"{px.shape[1]} tickers, {px.index[0].date()} → {px.index[-1].date()}, "
          f"{len(px)} days")
    print("NOTE: delisted names are absent — every level below is optimistic.\n")

    N = 15
    tests = [
        ("buy & hold, equal weight (no rebal)", w_equal, 10**9),
        ("equal weight, monthly rebalance", w_equal, 21),
        ("equal weight, quarterly rebalance", w_equal, 63),
        (f"momentum 12-1m, top {N}",
         lambda h: _rank_weights(h, lambda s: s.iloc[-21] / s.iloc[-252] - 1
                                 if len(s) > 252 and np.isfinite(s.iloc[-252]) and s.iloc[-252] > 0
                                 else np.nan, N, False), 21),
        (f"momentum 6-1m, top {N}",
         lambda h: _rank_weights(h, lambda s: s.iloc[-21] / s.iloc[-126] - 1
                                 if len(s) > 126 and np.isfinite(s.iloc[-126]) and s.iloc[-126] > 0
                                 else np.nan, N, False), 21),
        (f"contrarian 12-1m, bottom {N}",
         lambda h: _rank_weights(h, lambda s: s.iloc[-21] / s.iloc[-252] - 1
                                 if len(s) > 252 and np.isfinite(s.iloc[-252]) and s.iloc[-252] > 0
                                 else np.nan, N, True), 21),
        (f"short-term reversal 1m, bottom {N}",
         lambda h: _rank_weights(h, lambda s: s.iloc[-1] / s.iloc[-21] - 1
                                 if len(s) > 21 and np.isfinite(s.iloc[-21]) and s.iloc[-21] > 0
                                 else np.nan, N, True), 21),
        (f"low volatility, bottom {N}",
         lambda h: _rank_weights(h, lambda s: s.pct_change().iloc[-252:].std(),
                                 N, True), 21),
        (f"high volatility, top {N}",
         lambda h: _rank_weights(h, lambda s: s.pct_change().iloc[-252:].std(),
                                 N, False), 21),
    ]

    print(f"{'strategy':38s} {'CAGR':>8s} {'total':>10s} {'maxDD':>8s} {'turn/yr':>8s}")
    print("-" * 78)
    res = []
    for label, fn, rd in tests:
        r = run_portfolio(px, fn, rd, label=label)
        res.append(r)
        print(f"{label:38s} {r['cagr']*100:+7.1f}% {r['total']*100:+9.0f}% "
              f"{r['maxdd']*100:7.1f}% {r['turnover_yr']:7.1f}x")

    base = next(r for r in res if r["label"].startswith("buy & hold"))
    print(f"\nrelative to buy & hold ({base['cagr']*100:+.1f}%/yr over "
          f"{base['years']:.1f} years):")
    for r in res:
        if r is base:
            continue
        d = (r["cagr"] - base["cagr"]) * 100
        print(f"  {r['label']:38s} {d:+6.2f} pp/yr  "
              f"{'BEATS' if d > 0 else 'loses'}")

    # ── overnight vs intraday, on the 2y hourly set ──
    print("\n\nOVERNIGHT vs INTRADAY (2y hourly set)")
    hb = load_universe(interval="60m", period="2y")
    on, intra = [], []
    for t, df in hb.items():
        g = df.groupby(df.index.date)
        o = g["Open"].first()
        c = g["Close"].last()
        if len(o) < 100:
            continue
        intra.extend(((c - o) / o).dropna().tolist())
        on.extend(((o.values[1:] - c.values[:-1]) / c.values[:-1]).tolist())
    on = np.array([x for x in on if np.isfinite(x) and abs(x) < 0.3])
    intra = np.array([x for x in intra if np.isfinite(x) and abs(x) < 0.3])
    for nm, a in (("overnight (close→open)", on), ("intraday (open→close)", intra)):
        t = a.mean() / (a.std(ddof=1) / np.sqrt(len(a)))
        print(f"  {nm:26s} mean {a.mean()*100:+.4f}%/day  t={t:+6.2f}  "
              f"annualised {((1+a.mean())**252-1)*100:+7.1f}%")
    print("  (if the whole return accrues overnight, any intraday LONG strategy")
    print("   is fighting a headwind before costs are even charged)")


if __name__ == "__main__":
    main()
