"""Does the signal predict forward returns at all?

This is the question the project never answered. A PnL backtest cannot answer it
cleanly, because a bad result there confounds three separate things: the signal,
the TP/SL geometry, and the cost model. Here we isolate the first.

Method
------
For every bar we build a snapshot from CLOSED candles only, ask the strategy for
a direction, and then measure the realised return over the following N bars,
signed by that direction. If the signal carries information, the mean signed
return conditional on a signal must exceed both zero and the round-trip cost.

Alongside the full strategy we score each CLUSTER standalone, which localises any
edge to a specific family of indicators instead of judging the blend as a whole.

Statistics
----------
Windows overlap, so consecutive observations are correlated and a naive t-stat is
badly inflated. Sample size is deflated to n / horizon_bars, which is the
conservative reading. Treat |t| < 2 as "indistinguishable from noise".

Usage:
    python -m research.forward_returns --symbols BTC-USDT,ETH-USDT --days 90
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.config import Settings
from src.marketdata import Indicators, MarketData, SymbolSnapshot
from src.storage import Storage
from src.strategy import BONUS_ONLY_CLUSTERS, CLUSTER_MEMBERS, Strategy

from research.fetch_history import cache_path

HORIZONS = [6, 12, 24, 36]          # bars ahead (5m bars -> 30m, 1h, 2h, 3h)
WINDOW = 100                         # candles used to compute indicators
BAR_MINUTES = 5                      # for labelling only; set from --interval


@dataclass
class Bucket:
    """Signed forward returns (bps) collected for one configuration."""
    rets: dict[int, list[float]] = field(default_factory=lambda: {h: [] for h in HORIZONS})
    longs: int = 0
    shorts: int = 0

    def add(self, horizon: int, bps: float) -> None:
        self.rets[horizon].append(bps)

    def note_side(self, side: str) -> None:
        if side == "LONG":
            self.longs += 1
        else:
            self.shorts += 1

    @property
    def net_long_tilt(self) -> float:
        """(#long - #short) / total, in [-1, +1]."""
        tot = self.longs + self.shorts
        return (self.longs - self.shorts) / tot if tot else 0.0

    def stats(self, horizon: int, drift_bps: float = 0.0) -> tuple[int, float, float, float, float]:
        """(n, mean_bps, hit_rate_pct, deflated_t, alpha_bps)

        ``alpha`` removes the part of the return explained purely by being
        directionally tilted while the market drifted. Over this sample the
        market rose, so ANY long-biased rule earns the drift without predicting
        anything; comparing raw means against zero would credit that as skill.
        """
        xs = self.rets[horizon]
        n = len(xs)
        if n < 2:
            return n, 0.0, 0.0, 0.0, 0.0
        mean = sum(xs) / n
        var = sum((x - mean) ** 2 for x in xs) / (n - 1)
        sd = math.sqrt(var)
        hit = 100.0 * sum(1 for x in xs if x > 0) / n
        # Overlapping windows: deflate n by the horizon length.
        n_eff = max(1.0, n / horizon)
        t = (mean / (sd / math.sqrt(n_eff))) if sd > 0 else 0.0
        alpha = mean - self.net_long_tilt * drift_bps
        return n, mean, hit, t, alpha


def _f(candle: dict[str, Any], *keys: str) -> float:
    for k in keys:
        if k in candle:
            try:
                return float(candle[k])
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def build_snapshot(md: MarketData, symbol: str, window: list[dict], entry_px: float) -> SymbolSnapshot | None:
    ind = md.compute_indicators(window)
    if not ind.valid or entry_px <= 0:
        return None
    return SymbolSnapshot(
        symbol=symbol,
        mid_price=entry_px, mark_price=entry_px,
        best_bid=entry_px, best_ask=entry_px,
        spread_bps=1.0,                      # selector limits are tested elsewhere
        bid_depth_usdt=1e9, ask_depth_usdt=1e9,
        imbalance_ratio=0.5,                 # no historical order book
        fast_ema=ind.kline_fast_ema, slow_ema=ind.kline_slow_ema,
        z_score_bps=ind.kline_z_score_bps,
        indicators=ind, funding_rate=0.0,
    )


def run(symbols: list[str], days: int, interval: str) -> None:
    cfg = Settings(_env_file=None, db_path=":memory:", log_file="")
    db = Storage(":memory:")
    md = MarketData(cfg, None, db)  # type: ignore[arg-type]  # only compute_indicators used

    voting_clusters = [c for c in CLUSTER_MEMBERS if c not in BONUS_ONLY_CLUSTERS]

    # Configurations under test
    strategies: dict[str, Strategy] = {}
    for thesis in ("trend", "mean_revert"):
        c = cfg.model_copy(update={"signal_mode": "thesis", "primary_thesis": thesis})
        strategies[f"thesis:{thesis}"] = Strategy(c, db)
    c_vote = cfg.model_copy(update={"signal_mode": "vote"})
    strategies["vote(legacy 4/6)"] = Strategy(c_vote, db)
    c_old = cfg.model_copy(update={"signal_mode": "vote", "min_cluster_confluence": 2,
                                   "min_weighted_score_no_ema": 35.0})
    strategies["vote(old 2/6)"] = Strategy(c_old, db)

    buckets: dict[str, Bucket] = {k: Bucket() for k in strategies}
    buckets |= {f"cluster:{c}": Bucket() for c in voting_clusters}
    baseline = Bucket()
    cluster_seen = {c: 0 for c in voting_clusters}

    total_bars = 0
    for sym in symbols:
        path = cache_path(sym, interval, days)
        if not path.exists():
            print(f"  ! {sym}: no cache, skipping (run fetch_history first)")
            continue
        ks = json.loads(path.read_text())
        last = len(ks) - max(HORIZONS) - 1
        if last <= WINDOW:
            print(f"  ! {sym}: not enough data")
            continue

        for i in range(WINDOW, last):
            window = ks[i - WINDOW:i]
            entry = _f(ks[i], "open", "o") or _f(ks[i], "close", "c")
            snap = build_snapshot(md, sym, window, entry)
            if snap is None:
                continue
            total_bars += 1

            fwd = {}
            for h in HORIZONS:
                exit_px = _f(ks[i + h], "close", "c")
                fwd[h] = ((exit_px - entry) / entry) * 10_000 if entry > 0 else 0.0
                baseline.add(h, fwd[h])          # unconditional, LONG-signed

            probe = next(iter(strategies.values()))
            votes = probe._compute_votes(snap)
            cvs, _ = probe._compute_cluster_votes(votes)

            # Each cluster standalone
            for cv in cvs:
                if cv.name in BONUS_ONLY_CLUSTERS or cv.side == "NEUTRAL":
                    continue
                cluster_seen[cv.name] += 1
                b = buckets[f"cluster:{cv.name}"]
                b.note_side(cv.side)
                sign = 1.0 if cv.side == "LONG" else -1.0
                for h in HORIZONS:
                    b.add(h, sign * fwd[h])

            # Full configurations
            for name, strat in strategies.items():
                sigs = strat.generate_signals([snap], [])
                if not sigs:
                    continue
                b = buckets[name]
                b.note_side(sigs[0].side)
                sign = 1.0 if sigs[0].side == "LONG" else -1.0
                for h in HORIZONS:
                    b.add(h, sign * fwd[h])

        print(f"  {sym:14s} done")

    def cost_for(horizon_bars: int) -> float:
        """Round trip plus the funding that accrues while the position is held.

        Funding is negligible for a 30-minute scalp and dominant for a 7-day
        hold (21 funding events), so a single flat cost number would flatter
        long horizons exactly where we are trying to find an edge.
        """
        hold_hours = horizon_bars * BAR_MINUTES / 60.0
        intervals = hold_hours / cfg.funding_interval_hours
        return cfg.round_trip_cost_bps + cfg.default_funding_rate_bps * intervals

    print(f"\n{'='*84}")
    print(f"FORWARD RETURN ANALYSIS — {len(symbols)} symbols, {days}d, {total_bars:,} bars")
    print(f"cost = {cfg.round_trip_cost_bps:.0f} bps round trip + funding over the hold"
          f"   |   |t| < 2 => indistinguishable from noise")
    print("=" * 84)

    for h in HORIZONS:
        _n, drift, _hit, _t, _a = baseline.stats(h)
        mins = h * BAR_MINUTES
        span = f"{mins} min" if mins < 1440 else f"{mins/1440:.1f} days"
        cost = cost_for(h)
        print(f"\n── horizon {h} bars ({span}) ──   cost {cost:.0f} bps   "
              f"market drift {drift:+.2f} bps (free to a permanently-LONG rule)")
        print(f"   {'configuration':22s} {'n':>7s} {'tilt':>6s} {'mean':>8s} "
              f"{'alpha':>8s} {'hit%':>7s} {'t':>7s} {'alpha-cost':>11s}")
        print("   " + "-" * 82)
        rows = [(k, *buckets[k].stats(h, drift), buckets[k].net_long_tilt) for k in buckets]
        rows.sort(key=lambda r: -r[5])          # sort by alpha
        for name, n_, mean_, hit_, t_, alpha_, tilt_ in rows:
            if n_ < 30:
                continue
            net = alpha_ - cost
            flag = "  << EDGE" if (net > 0 and abs(t_) > 2) else ""
            print(f"   {name:22s} {n_:7d} {tilt_:+6.2f} {mean_:+8.2f} {alpha_:+8.2f} "
                  f"{hit_:6.1f}% {t_:+7.2f} {net:+11.2f}{flag}")

    print(f"\n{'='*84}")
    print("Clusters that could vote (no historical order book, so orderflow is absent):")
    for c, seen in sorted(cluster_seen.items(), key=lambda kv: -kv[1]):
        pct = 100.0 * seen / total_bars if total_bars else 0
        print(f"   {c:14s} voted on {seen:7d} bars ({pct:5.1f}%)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTC-USDT,ETH-USDT,SOL-USDT")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--horizons", default="", help="comma-separated bars ahead")
    a = ap.parse_args()

    BAR_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}[a.interval]
    globals()["BAR_MINUTES"] = BAR_MINUTES
    if a.horizons:
        globals()["HORIZONS"] = [int(x) for x in a.horizons.split(",")]

    run([s.strip() for s in a.symbols.split(",") if s.strip()], a.days, a.interval)
