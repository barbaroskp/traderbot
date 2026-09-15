"""Replay src/allocator.py over cached history.

This is the acceptance test for the allocation engine: it drives the SAME code
that would run live, hour by hour, charging costs on every trade. If the engine
cannot reproduce the numbers that motivated it, the engine is wrong.

Usage:
    python -m research.backtest_allocator --days 730 --interval 1h
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.allocator import Allocator, AllocatorState, Stance
from src.config import Settings
from research.fetch_history import cache_path
from research.forward_returns import _f

ONE_WAY_COST = 0.0013          # 26 bps round trip -> 13 bps per side


def load_prices(symbols: list[str], interval: str, days: int):
    series, times = {}, None
    for s in symbols:
        p = cache_path(s, interval, days)
        if not p.exists():
            raise SystemExit(f"missing cache for {s}; run research.fetch_history first")
        ks = json.loads(p.read_text())
        series[s] = [_f(k, "close", "c") for k in ks]
        ts = [int(k["time"]) for k in ks]
        times = ts if times is None else times
    n = min(len(v) for v in series.values())
    return {s: v[:n] for s, v in series.items()}, times[:n]


def simulate(cfg: Settings, prices: dict[str, list[float]], times: list[int],
             start_equity: float = 1000.0, verbose: bool = False):
    symbols = list(prices)
    alloc = Allocator(cfg, AllocatorState())
    units = {s: 0.0 for s in symbols}
    cash = start_equity

    equity_curve, peak, max_dd = [], start_equity, 0.0
    trades = turnover = fees = 0
    base_px = {s: prices[s][0] for s in symbols}

    for i in range(len(times)):
        px = {s: prices[s][i] for s in symbols}
        now = datetime.fromtimestamp(times[i] / 1000, timezone.utc)
        holdings = {s: units[s] * px[s] for s in symbols}
        equity = cash + sum(holdings.values())
        # Equal-weight basket index. The brake's re-entry test reads this rather
        # than equity, because equity does not move while the book is in stables.
        index = sum(px[s] / base_px[s] for s in symbols) / len(symbols)

        d = alloc.decide(equity, holdings, index, now)
        if d.rebalance:
            for it in d.intents:
                p = px[it.symbol]
                if p <= 0:
                    continue
                cost = abs(it.delta_quote) * ONE_WAY_COST
                units[it.symbol] += it.delta_quote / p
                cash -= it.delta_quote + cost
                fees += cost
                turnover += abs(it.delta_quote)
                trades += 1
            if verbose:
                print(f"  {now:%Y-%m-%d} {d.reason.value:10s} "
                      f"equity={equity:8.0f} dd={d.drawdown_pct:5.1f}% {d.note}")

        equity = cash + sum(units[s] * px[s] for s in symbols)
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1.0)
        equity_curve.append(equity)

    final = equity_curve[-1]
    return {
        "return_pct": (final / start_equity - 1) * 100,
        "max_drawdown_pct": max_dd * 100,
        "trades": trades,
        "turnover_x": turnover / start_equity,
        "fees_pct": fees / start_equity * 100,
        "final_stance": alloc.state.stance.value,
        "curve": equity_curve,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    base = Settings(_env_file=None, db_path=":memory:", log_file="")
    symbols = [s.strip() for s in base.allocation_basket.split(",") if s.strip()]
    prices, times = load_prices(symbols, a.interval, a.days)
    half = len(times) // 2

    print(f"Basket: {', '.join(symbols)}")
    print(f"Sample: {len(times)} bars of {a.interval} ({a.days}d), cost {ONE_WAY_COST*2*1e4:.0f}bps round trip\n")

    variants = {
        "buy & hold (no rebalance, no brake)": {"rebalance_days": 0, "rebalance_drift_pct": 0,
                                                "max_portfolio_drawdown_pct": 0},
        "monthly rebalance": {"rebalance_days": 30, "rebalance_drift_pct": 0,
                              "max_portfolio_drawdown_pct": 0},
        "monthly + drift trigger": {"rebalance_days": 30, "rebalance_drift_pct": 30,
                                    "max_portfolio_drawdown_pct": 0},
        "SHIPPED DEFAULTS": {},
        "  ... brake at 20%": {"max_portfolio_drawdown_pct": 20},
        "  ... brake at 35%": {"max_portfolio_drawdown_pct": 35},
    }

    print(f"{'variant':38s} {'1st half':>9s} {'2nd half':>9s} {'full':>9s} "
          f"{'maxDD':>8s} {'trades':>7s} {'fees':>6s}")
    print("-" * 92)
    for name, over in variants.items():
        cfg = base.model_copy(update=over)
        a1 = simulate(cfg, {s: v[:half] for s, v in prices.items()}, times[:half])
        a2 = simulate(cfg, {s: v[half:] for s, v in prices.items()}, times[half:])
        af = simulate(cfg, prices, times, verbose=a.verbose and name == "SHIPPED DEFAULTS")
        print(f"{name:38s} {a1['return_pct']:+8.1f}% {a2['return_pct']:+8.1f}% "
              f"{af['return_pct']:+8.1f}% {af['max_drawdown_pct']:+7.1f}% "
              f"{af['trades']:7d} {af['fees_pct']:5.1f}%")


if __name__ == "__main__":
    main()
