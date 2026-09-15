"""Study 4 — the literature's shortlist, tested on BIST.

WHY THESE FIVE AND NOT MY OWN IDEAS
-----------------------------------
Earlier studies here ran 40+ self-invented rules on a 2-year sample. Bailey &
López de Prado's minimum-backtest-length result says that budget allows roughly
7 independent configurations before an in-sample winner with zero out-of-sample
value is essentially guaranteed. Those studies are therefore exploratory at
best.

These five are different in kind: each is a PRE-REGISTERED hypothesis taken
from published, replicated work, with the rule specified before looking at
Turkish data. A pre-specified test of an external prior is not the same
statistical object as the best of forty self-generated rules.

  1. Closing-auction reversal      Bogousslavsky 2021; "Who Trades at the Close"
  2. Intraday periodicity          Heston, Korajczyk & Sadka, JF 2010
  3. Vol-conditioned reversal      Nagel, RFS 2012 ("Evaporating Liquidity")
  4. Market intraday momentum      Gao, Han, Li & Zhou, JFE 2018 (index level)
  5. Opening range breakout        Zarattini & Aziz 2023 — tested to FALSIFY

Each is judged on net return after cost, |t| >= 3.0 (Harvey/Liu/Zhu), and
consistency across both halves of the sample.

Usage:  python -m research.bist.study_literature [cost_pct]
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from research.bist import events
from research.bist.universe import load_universe


def tick_pct(p: float) -> float:
    t = 0.01 if p < 20 else 0.02 if p < 50 else 0.05 if p < 100 else 0.10
    return t / p


def report(name: str, rets: np.ndarray, dates: np.ndarray, cost: float,
           note: str = "") -> None:
    rets = np.asarray(rets, dtype=float)
    r = rets[np.isfinite(rets)]
    if len(r) < 60:
        print(f"  {name:38s}  too few observations ({len(r)})")
        return
    m = r.mean()
    t = m / (r.std(ddof=1) / np.sqrt(len(r)))
    d = np.asarray(dates, dtype=float)[np.isfinite(rets)]
    cut = np.median(d)
    h1, h2 = r[d <= cut], r[d > cut]
    ok = (m - cost > 0) and abs(t) >= 3.0 and \
         (h1.mean() - cost > 0) and (h2.mean() - cost > 0)
    print(f"  {'PASS' if ok else '    '} {name:34s} {len(r):6d} "
          f"{m*100:+8.4f}% {t:+6.2f} {(m-cost)*100:+9.4f}% "
          f"{(h1.mean()-cost)*100:+8.4f} {(h2.mean()-cost)*100:+8.4f}  {note}")


def main() -> None:
    cost = float(sys.argv[1]) / 100 if len(sys.argv) > 1 else 0.004
    bars = load_universe(interval="60m", period="2y")

    # ── assemble a per-ticker day list with the intraday bar path ──
    days: dict[str, list] = {}
    for tic, df in bars.items():
        d = events.to_days(df, min_bars=6)
        if len(d) >= 200:
            days[tic] = d
    print(f"{len(days)} tickers with >=200 clean days, "
          f"cost charged {cost*100:.2f}% round trip")
    print(f"  survival: net>0, |t|>=3.0 (Harvey/Liu/Zhu), both halves net>0\n")
    print(f"  {'':4s} {'test':34s} {'n':>6s} {'gross':>9s} {'t':>6s} "
          f"{'net':>9s} {'half1':>8s} {'half2':>8s}")
    print("  " + "-" * 96)

    # ── 1. CLOSING-AUCTION REVERSAL ────────────────────────────
    # signal: last-bar move into the close (the auction leg), standardised.
    # trade:  next day, enter at first-bar close, exit at bar 3 close.
    rows = []
    for tic, dl in days.items():
        press = []
        for d in dl:
            c = d.closes
            press.append((c[-1] - c[-2]) / c[-2] if len(c) > 1 and c[-2] > 0 else np.nan)
        press = np.array(press)
        for i in range(21, len(dl) - 1):
            s = np.nanstd(press[i - 21:i])
            if not np.isfinite(press[i]) or s <= 0:
                continue
            z = press[i] / s
            nxt = dl[i + 1]
            if len(nxt.closes) < 4:
                continue
            e, x = float(nxt.closes[0]), float(nxt.closes[3])
            if e <= 0:
                continue
            rows.append((nxt.date, z, (x - e) / e))
    R = pd.DataFrame(rows, columns=["date", "z", "r"]).dropna()
    dd = pd.to_datetime(R.date.astype(str)).astype("int64").to_numpy()
    # reversal: buy the most-pressured-down, sell the most-pressured-up
    lo, hi = R.z.quantile(0.1), R.z.quantile(0.9)
    report("1 close-auction rev: LONG bottom10%", R[R.z <= lo].r.to_numpy(),
           dd[R.z.to_numpy() <= lo], cost)
    report("1 close-auction rev: SHORT top10%", -R[R.z >= hi].r.to_numpy(),
           dd[R.z.to_numpy() >= hi], cost)
    report("1 close-auction: LONG top10% (cont.)", R[R.z >= hi].r.to_numpy(),
           dd[R.z.to_numpy() >= hi], cost)

    # ── 2. INTRADAY PERIODICITY (Heston-Korajczyk-Sadka) ───────
    # same clock hour on prior days predicts this hour today
    for bar in (0, 3, 7):
        rows = []
        for tic, dl in days.items():
            hr = []
            for d in dl:
                c = d.closes
                if len(c) > bar + 1 and c[bar] > 0:
                    hr.append((c[bar + 1] - c[bar]) / c[bar])
                else:
                    hr.append(np.nan)
            hr = np.array(hr)
            for i in range(6, len(hr)):
                past = hr[i - 5:i]
                if np.isfinite(past).sum() < 4 or not np.isfinite(hr[i]):
                    continue
                rows.append((dl[i].date, np.nanmean(past), hr[i]))
        P = pd.DataFrame(rows, columns=["date", "sig", "r"]).dropna()
        if len(P) < 500:
            continue
        d2 = pd.to_datetime(P.date.astype(str)).astype("int64").to_numpy()
        top = P.sig >= P.sig.quantile(0.9)
        report(f"2 periodicity bar{bar}: LONG top10%", P[top].r.to_numpy(),
               d2[top.to_numpy()], cost)

    # ── 3. VOL-CONDITIONED SHORT-TERM REVERSAL (Nagel) ─────────
    rows = []
    for tic, dl in days.items():
        oc = np.array([(d.close - d.open) / d.open if d.open > 0 else np.nan
                       for d in dl])
        for i in range(21, len(dl)):
            v = np.nanstd(oc[i - 21:i])
            if not np.isfinite(oc[i - 1]) or not np.isfinite(v):
                continue
            rows.append((dl[i].date, oc[i - 1], v, oc[i]))
    V = pd.DataFrame(rows, columns=["date", "prev", "vol", "r"]).dropna()
    V["d"] = V.groupby("date").prev.transform(lambda s: s - s.mean())
    hv = V.vol >= V.vol.quantile(2 / 3)
    lo10 = V.d <= V.d.quantile(0.1)
    d3 = pd.to_datetime(V.date.astype(str)).astype("int64").to_numpy()
    report("3 reversal LONG losers (all vol)", V[lo10].r.to_numpy(),
           d3[lo10.to_numpy()], cost)
    m = (lo10 & hv)
    report("3 reversal LONG losers (HIGH vol)", V[m].r.to_numpy(),
           d3[m.to_numpy()], cost, "Nagel condition")

    # ── 4. MARKET INTRADAY MOMENTUM (index level) ──────────────
    # equal-weight index of the traded universe
    alld = sorted({d.date for dl in days.values() for d in dl})
    idx = {}
    for dt in alld:
        h = [d for dl in days.values() for d in dl if d.date == dt and len(d.closes) >= 8]
        if len(h) >= 30:
            idx[dt] = np.array([np.mean([(x.closes[k] - x.open) / x.open for x in h])
                                for k in range(8)])
    keys = sorted(idx)
    rows = []
    for i in range(1, len(keys)):
        a = idx[keys[i]]
        first = a[0]                      # open → first bar close
        pen = a[6] - a[5]                 # penultimate hour
        last = a[7] - a[6]                # final hour
        sig = np.sign(first) + np.sign(pen)
        if sig != 0:
            rows.append((keys[i], np.sign(sig) * last))
    M = pd.DataFrame(rows, columns=["date", "r"]).dropna()
    d4 = pd.to_datetime(M.date.astype(str)).astype("int64").to_numpy()
    report("4 intraday momentum (index)", M.r.to_numpy(), d4, cost,
           f"{len(M)} index-days")

    # ── 5. OPENING RANGE BREAKOUT (falsification) ──────────────
    rows = []
    for tic, dl in days.items():
        for d in dl:
            if len(d.highs) < 8:
                continue
            # opening range = bar 0; the breakout must be judged on a LATER
            # bar, otherwise closes[0] <= highs[0] makes the test unsatisfiable
            orh, orl = d.highs[0], d.lows[0]
            p = float(d.closes[1])
            if p <= 0 or orh <= 0:
                continue
            if p > orh:
                rows.append((d.date, (d.close - p) / p))
            elif p < orl:
                rows.append((d.date, -(d.close - p) / p))
    B = pd.DataFrame(rows, columns=["date", "r"]).dropna()
    d5 = pd.to_datetime(B.date.astype(str)).astype("int64").to_numpy()
    report("5 opening range breakout", B.r.to_numpy(), d5, cost, "expect FAIL")

    print(f"\n  cost note: {cost*100:.2f}% is commission+BSMV at a mainstream")
    print(f"  Turkish broker plus one tick of spread. At Midas (0% commission)")
    print(f"  the hurdle is the spread alone, roughly 0.10-0.30%.")


if __name__ == "__main__":
    main()
