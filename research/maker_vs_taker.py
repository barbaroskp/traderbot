"""Would resting limit orders beat crossing the spread?

Market orders pay ~26bps round trip (5bps taker each side plus ~8bps of
slippage each side). A resting limit order pays the maker fee and no slippage,
which on paper cuts that to single digits — enough to move the break-even hit
rate from ~69% to ~54% at a 3-hour horizon.

The catch is adverse selection, and it is not a footnote. A buy limit resting
below the market fills when price comes DOWN to it — which is exactly when the
trade is going against you. When price runs away in your favour, you are simply
not filled and you miss the winner. So the fills you get are a biased sample of
the trades you wanted.

This script measures the whole trade-off end to end on real data:

  * fill rate at each offset,
  * the return of the trades that actually filled (not the ones you wanted),
  * net of realistic maker/taker costs.

Usage:
    python -m research.maker_vs_taker
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from pathlib import Path

from src.config import Settings
from src.marketdata import MarketData
from src.storage import Storage
from src.strategy import Strategy

from research.forward_returns import build_snapshot, _f

logging.disable(logging.CRITICAL)

SYMBOLS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT",
           "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "LINK-USDT", "LTC-USDT"]

HOLD_BARS = 36          # 3 hours on 5m candles
FILL_WINDOW = 12        # how long the limit order rests (12 bars = 1 hour)

# Cost model, bps. Entry via market crosses the book AND pays taker.
TAKER_FEE, MAKER_FEE, SLIPPAGE = 5.0, 2.0, 8.0


def load(days: int) -> dict[str, list[dict]]:
    out = {}
    for s in SYMBOLS:
        p = Path(f"research/data/{s}_5m_{days}d.json")
        if p.exists():
            out[s] = json.loads(p.read_text())
    return out


def collect_signals(cfg: Settings, candles: dict[str, list[dict]]):
    """Replay the strategy and yield (symbol, bar index, side, reference price)."""
    db = Storage(":memory:")
    md = MarketData(cfg, None, db)          # type: ignore[arg-type]
    strat = Strategy(cfg, db)
    for sym, ks in candles.items():
        for i in range(100, len(ks) - HOLD_BARS - FILL_WINDOW - 1):
            ref = _f(ks[i], "open", "o") or _f(ks[i], "close", "c")
            snap = build_snapshot(md, sym, ks[i - 100:i], ref)
            if snap is None:
                continue
            sigs = strat.generate_signals([snap], [])
            if sigs:
                yield sym, i, sigs[0].side, ref


def stats(xs: list[float], hold: int) -> tuple[float, float, float]:
    """(mean, hit rate %, t deflated for overlapping windows)"""
    n = len(xs)
    if n < 2:
        return 0.0, 0.0, 0.0
    mean = sum(xs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1))
    hit = 100.0 * sum(1 for x in xs if x > 0) / n
    n_eff = max(1.0, n / hold)
    return mean, hit, (mean / (sd / math.sqrt(n_eff)) if sd > 0 else 0.0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=40)
    a = ap.parse_args()

    candles = load(a.days)
    if not candles:
        raise SystemExit("no cached 5m data; run research.fetch_history first")

    base = Settings(_env_file=None, db_path=":memory:", log_file="")
    # Legacy 2-of-6 voting: the configuration actually run live, and the only
    # one that produces enough signals here to say anything.
    cfg = base.model_copy(update={"signal_mode": "vote", "min_cluster_confluence": 2,
                                  "min_weighted_score_no_ema": 35.0})

    signals = list(collect_signals(cfg, candles))
    print(f"{len(signals)} signals over {a.days}d on {len(candles)} symbols "
          f"(hold {HOLD_BARS} bars = {HOLD_BARS*5//60}h, limit rests {FILL_WINDOW*5}min)\n")

    # ── baseline: cross the spread ────────────────────────────
    market_cost = 2 * TAKER_FEE + 2 * SLIPPAGE
    gross_mkt = []
    for sym, i, side, ref in signals:
        ks = candles[sym]
        exit_px = _f(ks[i + HOLD_BARS], "close", "c")
        r = (exit_px - ref) / ref * 1e4 * (1 if side == "LONG" else -1)
        gross_mkt.append(r)
    m, hit, t = stats(gross_mkt, HOLD_BARS)
    print(f"{'execution':28s} {'fills':>7s} {'fill%':>7s} {'gross':>9s} "
          f"{'cost':>7s} {'NET':>9s} {'hit%':>7s} {'t':>7s}")
    print("-" * 86)
    print(f"{'MARKET (cross the spread)':28s} {len(gross_mkt):7d} {100.0:6.0f}% "
          f"{m:+8.1f} {market_cost:6.0f} {m - market_cost:+8.1f} {hit:6.1f}% {t:+7.2f}")

    # ── resting limit at a range of offsets ───────────────────
    for offset in (2.0, 5.0, 10.0, 20.0, 40.0):
        filled, missed = [], 0
        for sym, i, side, ref in signals:
            ks = candles[sym]
            limit = ref * (1 - offset / 1e4) if side == "LONG" else ref * (1 + offset / 1e4)

            hit_bar = None
            for j in range(i, i + FILL_WINDOW):
                lo, hi = _f(ks[j], "low", "l"), _f(ks[j], "high", "h")
                if (side == "LONG" and lo <= limit) or (side == "SHORT" and hi >= limit):
                    hit_bar = j
                    break
            if hit_bar is None:
                missed += 1
                continue

            exit_px = _f(ks[hit_bar + HOLD_BARS], "close", "c")
            r = (exit_px - limit) / limit * 1e4 * (1 if side == "LONG" else -1)
            filled.append(r)

        n = len(filled)
        if n < 30:
            continue
        m, hit, t = stats(filled, HOLD_BARS)
        # Entry rests as maker; the exit still has to cross (a stop cannot be
        # guaranteed passive), so only one side of the friction is saved.
        cost = MAKER_FEE + TAKER_FEE + SLIPPAGE
        print(f"{f'LIMIT resting {offset:.0f}bps away':28s} {n:7d} "
              f"{100*n/len(signals):6.0f}% {m:+8.1f} {cost:6.0f} {m - cost:+8.1f} "
              f"{hit:6.1f}% {t:+7.2f}")

    print(f"\ncosts: taker {TAKER_FEE:.0f} + slippage {SLIPPAGE:.0f} per crossing side; "
          f"maker {MAKER_FEE:.0f} and no slippage when resting")
    print("'gross' is measured from the price actually obtained, so a better fill "
          "already shows up there.")


if __name__ == "__main__":
    main()
