"""Which of the bot's own indicators actually carried information?

THE QUESTION
------------
The 41-day replay ended at -10.4% on the current config and -99.9% on the
original, with a 63% win rate. A 63% hit rate that still loses everything means
the error is structural, and there are only two places it can live: the
DIRECTION calls, or the exit geometry. This file examines the first.

It walks the same history the replay walked, calls the strategy's own
`_evaluate_snapshot` on every snapshot — not just the ones that became trades,
which is the whole point — and records what each of the ~25 indicators voted.
Then it measures the forward return that followed each vote.

The output is an attribution table: for every indicator, what happened next
when it said LONG, and what happened next when it said SHORT.

WHAT COUNTS AS "WORKING"
------------------------
An indicator earns its place only if, in the direction it voted, the subsequent
move beat the cost of acting on it. Vote counts and win rates are reported but
they do not decide anything: the replay already proved that a 63% win rate can
coexist with total loss, so hit rate is not evidence.

Returns are also reported MARKET-NEUTRAL (each bar's cross-sectional mean
removed). Crypto drifted upward over this window, so without that adjustment
every bullish indicator looks skilled and every bearish one looks broken, which
is a measurement of the market rather than of the indicator.

Usage:
    python -m research.replay.attribution --days 40 --horizon 36
"""

from __future__ import annotations

import argparse
import bisect
import logging
import pickle
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from research.replay.engine import Replay, _klines
from src.config import Settings

logging.disable(logging.CRITICAL)
DATA = Path("research/replay/data")


def collect(cfg: Settings, m5: dict, m15: dict, spreads: pd.DataFrame,
            universe: list[str], horizon_bars: int, stride: int):
    """Walk history, evaluate every snapshot, record votes and forward returns."""
    rp = Replay(cfg, "attribution")
    grid = sorted(set().union(*[set(m5[s].t) for s in universe if s in m5]))
    warm = max(cfg.kline_limit, 60) + 5
    grid = grid[warm:-horizon_bars - 1]
    idx5 = {s: {t: i for i, t in enumerate(m5[s].t)} for s in universe if s in m5}
    t15 = {s: list(m15[s].t) for s in universe if s in m15}

    rows = []
    now_dt = datetime.now(timezone.utc)
    for gi in range(0, len(grid), stride):
        now = grid[gi]
        fwd_by_sym = {}
        snaps = []
        for s in universe:
            i = idx5.get(s, {}).get(now)
            if i is None or i < cfg.kline_limit or i + horizon_bars >= len(m5[s]):
                continue
            j = bisect.bisect_right(t15.get(s, []), now)
            sp = float(spreads.spread_bps.get(s, 6.0))
            sn = rp.snapshot(s, m5[s], i, m15.get(s), j, sp, 0.0)
            if sn is None:
                continue
            px0 = float(m5[s].c.iloc[i - 1])
            px1 = float(m5[s].c.iloc[i - 1 + horizon_bars])
            if px0 <= 0:
                continue
            fwd_by_sym[s] = px1 / px0 - 1.0
            snaps.append(sn)
        if len(snaps) < 2:
            continue
        mkt = float(np.mean([fwd_by_sym[s.symbol] for s in snaps]))
        for sn in snaps:
            sig = rp._eval(sn, now_dt)
            if sig is None:
                continue
            ex = fwd_by_sym[sn.symbol] - mkt
            for v in sig.indicator_votes:
                if v.side in ("LONG", "SHORT"):
                    rows.append((v.name, v.side, ex, fwd_by_sym[sn.symbol]))
            for c in sig.cluster_votes:
                if c.side in ("LONG", "SHORT"):
                    rows.append((f"[{c.name}]", c.side, ex,
                                 fwd_by_sym[sn.symbol]))
    return pd.DataFrame(rows, columns=["name", "side", "excess", "raw"])


def table(df: pd.DataFrame, cost: float) -> pd.DataFrame:
    out = []
    for name, g in df.groupby("name"):
        # the vote's own P&L: long votes earn the move, short votes earn its negative
        pnl = np.where(g.side == "LONG", g.excess, -g.excess)
        raw = np.where(g.side == "LONG", g.raw, -g.raw)
        if len(pnl) < 200:
            continue
        t = pnl.mean() / (pnl.std(ddof=1) / np.sqrt(len(pnl))) if pnl.std() else 0.0
        nl = int((g.side == "LONG").sum())
        out.append({
            "indicator": name, "votes": len(pnl), "long%": 100 * nl / len(pnl),
            "excess": pnl.mean(), "raw": raw.mean(),
            "net": pnl.mean() - cost, "t": t,
            "hit%": 100 * float((pnl > 0).mean()),
        })
    return pd.DataFrame(out).sort_values("excess", ascending=False)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--horizon", type=int, default=36, help="bars held (5m each)")
    ap.add_argument("--stride", type=int, default=6, help="sample every N bars")
    ap.add_argument("--symbols", type=int, default=40)
    ap.add_argument("--cost", type=float, default=0.0016)
    a = ap.parse_args()

    m5 = pickle.loads((DATA / f"5m_{a.days}d.pkl").read_bytes())
    m15 = pickle.loads((DATA / f"15m_{a.days}d.pkl").read_bytes())
    spreads = pickle.loads((DATA / "spreads.pkl").read_bytes())

    order = [s for s in spreads.sort_values("quoteVolume", ascending=False).index
             if s in m5]
    universe = order[:a.symbols]

    cfg = Settings(_env_file=None, db_path=":memory:", log_file="",
                   min_volume_24h_usdt=0.0, max_spread_bps=1e9,
                   shortlist_size=a.symbols)
    print(f"{len(universe)} symbols · holding {a.horizon} bars "
          f"({a.horizon*5//60}h{a.horizon*5%60:02d}m) · sampling every "
          f"{a.stride*5} minutes · cost {a.cost*100:.2f}%")

    df = collect(cfg, m5, m15, spreads, universe, a.horizon, a.stride)
    if df.empty:
        print("no votes collected")
        return
    print(f"{len(df):,} directional votes recorded\n")

    t = table(df, a.cost)
    print("INDICATOR ATTRIBUTION — market-neutral forward return per vote")
    print("  [cluster] rows are the aggregated cluster votes\n")
    print(f"  {'':4s} {'indicator':22s} {'votes':>7s} {'long%':>6s} "
          f"{'excess':>8s} {'net':>8s} {'t':>7s} {'hit%':>6s}")
    print("  " + "-" * 72)
    for _, r in t.iterrows():
        ok = r["net"] > 0 and abs(r["t"]) >= 3.0
        print(f"  {'PASS' if ok else '    '} {r['indicator']:22s} "
              f"{int(r['votes']):7d} {r['long%']:5.0f}% {r['excess']*100:+7.3f}% "
              f"{r['net']*100:+7.3f}% {r['t']:+7.2f} {r['hit%']:5.1f}%")

    good = t[(t.net > 0) & (t.t.abs() >= 3.0)]
    print(f"\n  clearing cost with |t|>=3: {len(good)}/{len(t)}")
    if len(good):
        print("  " + ", ".join(good.indicator))
    worst = t.nsmallest(3, "excess")
    print(f"\n  most negative: " +
          ", ".join(f"{r['indicator']} ({r['excess']*100:+.3f}%)"
                    for _, r in worst.iterrows()))
    print("\n  a negative indicator is not useless — inverting it is a candidate,")
    print("  but only if it holds up out of sample, which this table cannot show.")


if __name__ == "__main__":
    main()
