"""The actual thing: run the bot over history and see what the account did.

WHY THIS EXISTS SEPARATELY FROM study_multi_tf
-----------------------------------------------
That study was an EVENT STUDY. It took every bar where the MACD signal sat in
the top decile and averaged the forward three-day excess return. That answers
"does the signal carry information", and it answered yes: +0.419% gross,
+0.259% net of cost, t=+5.25, positive in both halves, still positive after
dropping the ten best symbols.

It does NOT answer "what would the account have done", and the gap between
those two questions is where most strategies die:

  * an event study averages over every signal; a portfolio can only hold N
    positions and has to choose
  * it measured EXCESS return, relative to the cross-sectional mean. Capturing
    that requires being short the rest of the market, not just long the picks
  * signals overlap: a 3-day hold on daily bars means three generations of
    positions are open at once, so capital is split three ways
  * no leverage, no stop, no capital constraint, no compounding

This file closes all of those. It runs a book day by day, holds a bounded
number of positions, charges the same costs, compounds, and reports the equity
curve — long-only, market-neutral, and levered variants, so the difference
between "the signal has information" and "the account made money" is visible
rather than assumed.

Usage:
    python -m research.crypto.sim_macd
    python -m research.crypto.sim_macd --leverage 3 --stop 0.10
"""

from __future__ import annotations

import argparse
import pickle
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from research.crypto.study_multi_tf import signals

DATA = Path("research/crypto/data")


@dataclass
class SimConfig:
    signal: str = "macd"
    n_long: int = 10
    n_short: int = 0            # >0 makes it market-neutral
    hold_days: int = 3
    leverage: float = 1.0
    stop_loss: float | None = None      # fraction, on the levered position
    cost: float = 0.0016                # round trip, fraction of notional
    min_turnover: float = 5e6           # point-in-time liquidity filter


def build(store: dict[str, pd.DataFrame], cfg: SimConfig):
    """Aligned price / signal / turnover panels, signal already lagged."""
    px, sg, tv = {}, {}, {}
    for s, df in store.items():
        if len(df) < 120:
            continue
        d = df.drop_duplicates("t").set_index("t")
        f = signals(d.reset_index()).set_index(d.index)
        if cfg.signal not in f:
            continue
        px[s], sg[s] = d["c"], f[cfg.signal]
        tv[s] = (d["c"] * d.get("v", 0.0)).rolling(20).mean()
    P = pd.DataFrame(px).sort_index()
    return P, pd.DataFrame(sg).reindex(P.index), pd.DataFrame(tv).reindex(P.index)


def simulate(P: pd.DataFrame, S: pd.DataFrame, V: pd.DataFrame,
             cfg: SimConfig) -> dict:
    """Day-by-day book. Entries at the close of the decision bar.

    Positions are opened in `hold_days` staggered tranches so capital is split
    the way it actually would be: on any given day one tranche is being closed
    and one opened, and the rest are running.
    """
    R = P.pct_change().fillna(0.0)
    idx = P.index
    eq = 1.0
    curve, n_pos, turn_total = [], [], 0.0
    # each tranche: (exit_row, weights Series, entry_prices Series)
    tranches: list[tuple[int, pd.Series, pd.Series]] = []
    start = 60

    for i in range(start, len(P) - 1):
        # ── mark open tranches to market, apply stop ──────────
        step = 0.0
        alive = []
        for exit_i, w, entry_px in tranches:
            cur = P.iloc[i][w.index]
            move = (cur / entry_px - 1.0) * np.sign(w)
            if cfg.stop_loss is not None:
                hit = move * cfg.leverage <= -cfg.stop_loss
                if hit.any():
                    # stopped names leave the tranche and pay the exit cost
                    w = w[~hit]
                    entry_px = entry_px[~hit]
                    stopped = float(hit.sum()) / max(cfg.n_long, 1)
                    turn_total += stopped * cfg.leverage
                    eq *= (1 - stopped * cfg.cost / 2 * cfg.leverage)
            if len(w) and i < exit_i:
                step += float((w * R.iloc[i + 1][w.index].fillna(0.0)).sum())
                alive.append((exit_i, w, entry_px))
            elif len(w):
                # Cost scales with LEVERAGE: a 3x book trades three times the
                # notional and pays three times the commission and spread.
                # Levering the returns but not the costs made a -2.5% strategy
                # report +103.7% at 3x, which is how this bug was caught.
                notional = float(w.abs().sum()) * cfg.leverage
                turn_total += notional
                eq *= (1 - notional * cfg.cost / 2)
        tranches = alive
        eq *= (1 + step * cfg.leverage)

        # ── open a new tranche once per hold period ───────────
        if (i - start) % 1 == 0 and len(tranches) < cfg.hold_days:
            liq = V.iloc[i]
            ok = [c for c in P.columns
                  if np.isfinite(P.iloc[i][c]) and np.isfinite(S.iloc[i][c])
                  and np.isfinite(liq.get(c, np.nan))
                  and liq[c] >= cfg.min_turnover]
            if len(ok) >= cfg.n_long + cfg.n_short + 5:
                r = S.iloc[i][ok].sort_values(ascending=False)
                longs = list(r.index[:cfg.n_long])
                shorts = list(r.index[-cfg.n_short:]) if cfg.n_short else []
                size = 1.0 / cfg.hold_days / max(len(longs) + len(shorts), 1)
                w = pd.Series({**{s: size for s in longs},
                               **{s: -size for s in shorts}})
                notional = float(w.abs().sum()) * cfg.leverage
                turn_total += notional
                eq *= (1 - notional * cfg.cost / 2)
                tranches.append((i + cfg.hold_days, w, P.iloc[i][w.index]))

        curve.append(eq)
        n_pos.append(sum(len(w) for _, w, _ in tranches))
        if eq <= 0.01:
            break

    c = pd.Series(curve, index=idx[start + 1: start + 1 + len(curve)])
    yrs = len(c) / 365
    tot = float(c.iloc[-1] - 1)
    ret = c.pct_change().dropna()
    return {
        "curve": c,
        "cagr": (1 + tot) ** (1 / yrs) - 1 if tot > -1 else -1.0,
        "total": tot,
        "maxdd": float((c / c.cummax() - 1).min()),
        "vol": float(ret.std() * np.sqrt(365)),
        "turn_per_year": turn_total / yrs,
        "avg_positions": float(np.mean(n_pos)) if n_pos else 0.0,
        "wiped": eq <= 0.01,
    }


def row(label: str, r: dict) -> str:
    return (f"{label:34s} {r['cagr']*100:+8.1f}% {r['total']*100:+9.0f}% "
            f"{r['maxdd']*100:7.1f}% {r['vol']*100:6.0f}% "
            f"{r['turn_per_year']*100:7.0f}% {r['avg_positions']:5.1f}"
            f"{'  WIPED OUT' if r['wiped'] else ''}")


def header() -> str:
    return (f"{'variant':34s} {'CAGR':>9s} {'total':>10s} {'maxDD':>8s} "
            f"{'vol':>7s} {'turn/yr':>8s} {'pos':>5s}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leverage", type=float, default=None)
    ap.add_argument("--stop", type=float, default=None)
    a = ap.parse_args()

    store = pickle.loads((DATA / "daily_top400.pkl").read_bytes())
    base = SimConfig()
    P, S, V = build(store, base)
    print(f"{P.shape[1]} symbols · {P.index[0].date()} → {P.index[-1].date()} "
          f"({len(P)} days)\nsignal: {base.signal}, hold {base.hold_days}d, "
          f"cost {base.cost*100:.2f}% round trip\n")

    # benchmark
    btc = P["BTC-USDT"].dropna() if "BTC-USDT" in P else None
    variants = []
    if btc is not None:
        yrs = len(btc) / 365
        tot = float(btc.iloc[-1] / btc.iloc[0] - 1)
        variants.append(("buy & hold BTC", {
            "curve": btc / btc.iloc[0],
            "cagr": (1 + tot) ** (1 / yrs) - 1, "total": tot,
            "maxdd": float((btc / btc.cummax() - 1).min()),
            "vol": float(btc.pct_change().std() * np.sqrt(365)),
            "turn_per_year": 0.0, "avg_positions": 1.0, "wiped": False}))

    specs = [
        ("long-only 10, 1x", dict(n_long=10, n_short=0, leverage=1)),
        ("long-only 5, 1x", dict(n_long=5, n_short=0, leverage=1)),
        ("long-only 20, 1x", dict(n_long=20, n_short=0, leverage=1)),
        ("market-neutral 10/10, 1x", dict(n_long=10, n_short=10, leverage=1)),
        ("market-neutral 10/10, 2x", dict(n_long=10, n_short=10, leverage=2)),
        ("market-neutral 10/10, 3x", dict(n_long=10, n_short=10, leverage=3)),
        ("long-only 10, 3x", dict(n_long=10, n_short=0, leverage=3)),
        ("long-only 10, 3x + 10% stop",
         dict(n_long=10, n_short=0, leverage=3, stop_loss=0.10)),
        ("m-neutral 10/10, 3x + 10% stop",
         dict(n_long=10, n_short=10, leverage=3, stop_loss=0.10)),
    ]
    if a.leverage or a.stop:
        specs = [(f"custom {a.leverage}x stop {a.stop}",
                  dict(n_long=10, n_short=10, leverage=a.leverage or 1,
                       stop_loss=a.stop))]

    for label, over in specs:
        cfg = SimConfig(**{**base.__dict__, **over})
        variants.append((label, simulate(P, S, V, cfg)))

    print(header())
    print("-" * 86)
    for label, r in sorted(variants, key=lambda x: -x[1]["cagr"]):
        print(row(label, r))

    print("\nnote: 'excess return' in the event study required shorting the rest")
    print("of the market to capture. The market-neutral rows are the honest")
    print("implementation of that; the long-only rows also carry market beta.")


if __name__ == "__main__":
    main()
