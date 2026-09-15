"""Reverse-engineer the bot: what setting would have turned it profitable?

THE TRAP THIS AVOIDS
--------------------
Searching 41 days of history for the best parameter and reporting it is the
single most reliable way to produce a number that never repeats. This project
has already killed four findings that way. So every candidate is fitted on the
FIRST portion of the window and scored on the SECOND, and both numbers are
printed. A config that wins in-sample and loses out-of-sample is not a finding,
it is the search overfitting, and it is shown rather than hidden.

WHAT IS WORTH SWEEPING, AND WHY
-------------------------------
The attribution study measured every one of the bot's 22 indicators across
93,193 votes: none cleared its cost, and every hit rate sat between 46% and
53%. So changing WHICH indicators vote cannot be the answer — there is no
subset to promote. What remains is the shape of the trade:

  * how long a position is allowed to run before it is abandoned
  * how strict the committee has to be before a trade is taken
  * how much leverage the result is multiplied by
  * whether one thesis leads (thesis mode) or the clusters vote (vote mode)

The corrected replay leaves the current config needing a 35.4% hit rate and
delivering 26%. These are the levers that could close that 9-point gap, if
anything can.

Usage:
    python -m research.replay.sweep --days 40
"""

from __future__ import annotations

import argparse
import logging
import pickle
from pathlib import Path

import pandas as pd

from research.replay.engine import run
from research.replay.run import load, universe_for
from src.config import Settings

logging.disable(logging.CRITICAL)


def split_run(cfg: Settings, m5: dict, m15: dict, spreads, funding,
              universe: list[str], capital: float, cut: pd.Timestamp):
    """Fit window and test window, replayed separately."""
    m5_a = {s: d[d.t <= cut].reset_index(drop=True) for s, d in m5.items()}
    m5_b = {s: d[d.t > cut].reset_index(drop=True) for s, d in m5.items()}
    m15_a = {s: d[d.t <= cut].reset_index(drop=True) for s, d in m15.items()}
    m15_b = {s: d[d.t > cut].reset_index(drop=True) for s, d in m15.items()}
    a = run(cfg, m5_a, m15_a, spreads, funding, capital, "in", universe,
            progress=False)
    b = run(cfg, m5_b, m15_b, spreads, funding, capital, "out", universe,
            progress=False)
    return a, b


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--capital", type=float, default=10_000.0)
    a = ap.parse_args()

    m5, m15, spreads, funding = load(a.days)
    times = next(iter(m5.values())).t
    cut = times.iloc[len(times) // 2]
    print(f"fit  window: {times.iloc[0]:%Y-%m-%d} → {cut:%Y-%m-%d}")
    print(f"test window: {cut:%Y-%m-%d} → {times.iloc[-1]:%Y-%m-%d}\n")

    base = dict(_env_file=None, db_path=":memory:", log_file="",
                initial_capital_usdt=a.capital, risk_state_disabled=False)

    variants: list[tuple[str, dict]] = [
        ("baseline (current)", {}),
        ("hold 60m", {"max_hold_minutes": 60}),
        ("hold 360m", {"max_hold_minutes": 360}),
        ("hold 720m", {"max_hold_minutes": 720}),
        ("confluence 4", {"min_cluster_confluence": 4}),
        ("confluence 5", {"min_cluster_confluence": 5}),
        ("threshold 60bps", {"entry_threshold_bps": 60.0}),
        ("threshold 100bps", {"entry_threshold_bps": 100.0}),
        ("leverage 1x", {"leverage": 1, "leverage_high_conviction": 1}),
        ("leverage 3x", {"leverage": 3, "leverage_high_conviction": 3}),
        ("thesis mean_revert", {"primary_thesis": "mean_revert"}),
        ("vote mode 3/6", {"signal_mode": "vote", "min_cluster_confluence": 3}),
        ("wider TP (atr x4)", {"atr_tp_multiplier": 4.0}),
        ("tighter SL (atr x0.8)", {"atr_sl_multiplier": 0.8}),
        ("wider SL (atr x2)", {"atr_sl_multiplier": 2.0}),
        ("4 slots", {"max_open_positions": 4}),
    ]

    cfg0 = Settings(**base)
    uni = universe_for(cfg0, spreads, m5)
    print(f"universe: {len(uni)} symbols — {', '.join(uni)}\n")

    print(f"  {'variant':24s} {'FIT':>9s} {'trades':>7s} {'TEST':>9s} "
          f"{'trades':>7s} {'win%':>6s}")
    print("  " + "-" * 68)
    rows = []
    for label, over in variants:
        cfg = Settings(**{**base, **over})
        try:
            fit, test = split_run(cfg, m5, m15, spreads, funding, uni,
                                  a.capital, cut)
        except Exception as exc:                       # noqa: BLE001
            print(f"  {label:24s} failed: {exc}")
            continue
        sf, st = fit.stats(a.capital), test.stats(a.capital)
        rows.append((label, sf.get("total", 0), st.get("total", 0)))
        print(f"  {label:24s} {sf.get('total', 0)*100:+8.2f}% "
              f"{sf.get('trades', 0):7d} {st.get('total', 0)*100:+8.2f}% "
              f"{st.get('trades', 0):7d} {st.get('win_rate', 0)*100:5.0f}%")

    if rows:
        best_fit = max(rows, key=lambda r: r[1])
        print(f"\n  best in FIT window:  {best_fit[0]} ({best_fit[1]*100:+.2f}%)")
        print(f"    its TEST result:   {best_fit[2]*100:+.2f}%")
        both = [r for r in rows if r[1] > 0 and r[2] > 0]
        print(f"\n  positive in BOTH windows: {len(both)}/{len(rows)}")
        for r in both:
            print(f"    {r[0]:24s} fit {r[1]*100:+.2f}%  test {r[2]*100:+.2f}%")
        if not both:
            print("    none — every candidate that won the fit window lost the test")


if __name__ == "__main__":
    main()
