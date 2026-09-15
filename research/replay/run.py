"""Run the bot over history and report what the account did.

    python -m research.replay.run --days 40 --capital 10000

Two configurations are replayed side by side:

  CURRENT   the config as it stands after the correctness refactor —
            2x leverage, 30% total margin, 60-minute cooldown, 2 open
            positions, spread <= 8bp, 24h volume >= $50m
  ORIGINAL  the config as originally shipped — 3x leverage (up to 5x on
            high conviction), 80% total margin, 10-minute cooldown, 5 open
            positions, spread <= 30bp, 24h volume >= $1m

The second is included because the first filter collapses BingX's actual
liquidity to about five tradeable symbols, and that fact is itself a result:
a bot configured for costs it can survive has almost nothing to trade, while
a bot configured to have plenty to trade is paying spreads it cannot survive.
Seeing both numbers is the point.
"""

from __future__ import annotations

import argparse
import logging
import pickle
from pathlib import Path

import pandas as pd

# The live logger writes a line per rejected candidate. Over ~12,000 cycles x
# 25 symbols that is millions of lines and it drowns the result.
logging.disable(logging.CRITICAL)

from research.replay.engine import ReplayResult, run
from src.config import Settings

DATA = Path("research/replay/data")


def load(days: int):
    m5 = pickle.loads((DATA / f"5m_{days}d.pkl").read_bytes())
    m15 = pickle.loads((DATA / f"15m_{days}d.pkl").read_bytes())
    spreads = pickle.loads((DATA / "spreads.pkl").read_bytes())
    fpath = DATA / "funding.pkl"
    funding = pickle.loads(fpath.read_bytes()) if fpath.exists() else {}
    return m5, m15, spreads, funding


def universe_for(cfg: Settings, spreads: pd.DataFrame, m5: dict) -> list[str]:
    """Apply the selector's liquidity filters, as the live bot would."""
    ok = []
    for s in m5:
        row = spreads.loc[s] if s in spreads.index else None
        if row is None:
            continue
        sp = float(row.spread_bps) if pd.notna(row.spread_bps) else 999.0
        vol = float(row.quoteVolume) if pd.notna(row.quoteVolume) else 0.0
        if sp <= cfg.max_spread_bps and vol >= cfg.min_volume_24h_usdt:
            ok.append((s, vol))
    ok.sort(key=lambda x: -x[1])
    return [s for s, _ in ok[:cfg.shortlist_size]]


def report(res: ReplayResult, start: float) -> None:
    s = res.stats(start)
    if not s:
        print(f"  {res.label}: no data")
        return
    print(f"\n  ── {res.label} ──")
    print(f"  {start:,.0f} TL  →  {s['final']:,.0f} TL   "
          f"({s['total']*100:+.1f}% over {s['days']:.0f} days, "
          f"{s['annualised']*100:+.0f}%/yr)")
    print(f"  max drawdown {s['maxdd']*100:.1f}%  ·  {s['trades']} trades  ·  "
          f"win rate {s['win_rate']*100:.0f}%")
    print(f"  gross P&L {s['gross']:+,.0f}  fees {-s['fees']:,.0f}  "
          f"funding {-s['funding']:+,.0f}  →  net {s['net']:+,.0f}")
    print(f"  cycles {res.cycles:,}  ·  signals accepted {res.signals}")
    if res.trades:
        by = {}
        for t in res.trades:
            by.setdefault(t.reason, []).append(t.net_pnl)
        bits = [f"{k} {len(v)} ({sum(v):+,.0f})" for k, v in sorted(by.items())]
        print("  exits: " + " · ".join(bits))
    if res.rejected:
        top = sorted(res.rejected.items(), key=lambda kv: -kv[1])[:6]
        print("  top gate rejections: " +
              " · ".join(f"{k} {v}" for k, v in top))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--capital", type=float, default=10_000.0)
    ap.add_argument("--only", choices=["current", "original"], default=None)
    a = ap.parse_args()

    m5, m15, spreads, funding = load(a.days)
    any_sym = next(iter(m5))
    span = m5[any_sym].t
    print(f"data: {len(m5)} symbols · {span.iloc[0]:%Y-%m-%d %H:%M} → "
          f"{span.iloc[-1]:%Y-%m-%d %H:%M} · {len(span):,} 5-minute bars")
    print("orderflow cluster is silent: no historical order book exists, so "
          "imbalance and whale fields are neutral")

    base = dict(_env_file=None, db_path=":memory:", log_file="",
                initial_capital_usdt=a.capital, risk_state_disabled=False)

    configs = {
        "CURRENT (post-refactor)": Settings(**base),
        "ORIGINAL (as first shipped)": Settings(**{
            **base,
            "leverage": 3, "leverage_high_conviction": 5,
            "max_total_margin_pct": 0.80, "max_trade_margin_pct": 0.20,
            "per_trade_fraction": 0.15, "max_open_positions": 5,
            "cooldown_minutes": 10, "entry_threshold_bps": 15.0,
            "max_spread_bps": 30.0, "min_depth_usdt": 1000.0,
            "min_volume_24h_usdt": 1_000_000.0, "shortlist_size": 300,
            "signal_mode": "vote", "min_cluster_confluence": 2,
            "min_weighted_score_no_ema": 35.0,
            "dynamic_leverage_enabled": True,
            "kelly_min_fraction": 0.05, "kelly_block_on_negative_edge": False,
        }),
    }

    for label, cfg in configs.items():
        if a.only == "current" and "ORIGINAL" in label:
            continue
        if a.only == "original" and "CURRENT" in label:
            continue
        uni = universe_for(cfg, spreads, m5)
        print(f"\n{label}: universe {len(uni)} symbols "
              f"(spread<={cfg.max_spread_bps}bp, vol>=${cfg.min_volume_24h_usdt/1e6:.0f}m, "
              f"lev {cfg.leverage}x, {cfg.max_open_positions} slots)")
        if not uni:
            print("  nothing passes the liquidity filter — the bot would never trade")
            continue
        print(f"  {', '.join(uni[:12])}{' …' if len(uni) > 12 else ''}")
        res = run(cfg, m5, m15, spreads, funding, a.capital, label, uni)
        report(res, a.capital)


if __name__ == "__main__":
    main()
