"""Run the actual bot's decision engine on BIST hourly bars.

WHAT IS THE BOT AND WHAT IS NOT
-------------------------------
The signals here come from the shipped code: `MarketData.compute_indicators`
builds all 24 indicators, `Strategy.generate_signals` runs the cluster voting
and every gate. Nothing about the decision logic is reimplemented.

What IS written here is the BIST wrapper: the exchange is different, so the
plumbing has to be. Specifically:

  * no funding rate — BIST has no perpetual funding, so that cost is zero
  * no order book history — same limitation as the crypto replay, so
    imbalance/whale/depth are neutral and the orderflow cluster abstains
  * no leverage — this is cash equity, and the bot's leverage is a futures
    concept that does not transfer
  * cost from the BIST tick table plus the broker's commission, rather than
    a perpetual taker fee

THE EXIT RULE IS THE USER'S, NOT THE BOT'S
------------------------------------------
The brief is explicit: buy on a signal, sell at +1%. So the take-profit is
pinned at +1% regardless of what `_compute_tp_sl_bps` would have chosen. The
stop and the timeout remain the bot's own, because "sell at +1%" specifies only
what to do when it goes right.

Usage:
    python -m research.replay.bist_run --days 40 --capital 10000
"""

from __future__ import annotations

import argparse
import logging
from datetime import timedelta

import numpy as np
import pandas as pd

from research.bist.universe import load_universe
from research.replay.engine import Replay
from src.config import Settings
from src.costs import bist_min_spread_frac

logging.disable(logging.CRITICAL)


def to_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Vendor frame -> the t/o/h/l/c/v shape the replay helpers expect."""
    out = pd.DataFrame({
        "t": pd.to_datetime(df.index),
        "o": df["Open"].to_numpy(float), "h": df["High"].to_numpy(float),
        "l": df["Low"].to_numpy(float), "c": df["Close"].to_numpy(float),
        "v": df["Volume"].to_numpy(float),
    })
    return out.dropna(subset=["c"]).reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--capital", type=float, default=10_000.0)
    ap.add_argument("--tp", type=float, default=0.01)
    ap.add_argument("--commission", type=float, default=0.0,
                    help="per side, fraction. 0 = Midas, 0.00195 = mainstream")
    ap.add_argument("--slots", type=int, default=3)
    ap.add_argument("--pick", choices=["strongest", "weakest", "random"],
                    default="strongest",
                    help="which candidate to take when several fire. 'random' "
                         "is the control: if it beats 'strongest', the "
                         "strength ranking is not just useless but harmful.")
    ap.add_argument("--mode", default="vote")
    ap.add_argument("--confluence", type=int, default=2)
    ap.add_argument("--threshold", type=float, default=15.0)
    ap.add_argument("--minscore", type=float, default=35.0)
    ap.add_argument("--cooldown", type=int, default=60)
    a = ap.parse_args()

    raw = load_universe(interval="60m", period="2y", min_bars=300)
    bars = {t: to_bars(d) for t, d in raw.items()}
    cutoff = max(b.t.iloc[-1] for b in bars.values()) - timedelta(days=a.days)
    # Keep warm-up history before the window: the indicators need 100 bars and
    # slicing to the window first would start the bot blind.
    hist = {t: b for t, b in bars.items() if len(b[b.t >= cutoff]) > 50}
    print(f"{len(hist)} BIST symbols · window from {cutoff:%Y-%m-%d} · "
          f"TP fixed at +{a.tp*100:.1f}% · commission {a.commission*100:.3f}%/side")

    cfg = Settings(_env_file=None, db_path=":memory:", log_file="",
                   initial_capital_usdt=a.capital, risk_state_disabled=False,
                   max_open_positions=a.slots, max_spread_bps=1e9,
                   min_volume_24h_usdt=0.0, min_depth_usdt=0.0,
                   shortlist_size=200, use_funding_cost=False,
                   leverage=1, leverage_high_conviction=1,
                   # Loosened so MANY candidates fire at once. With one slot
                   # and the whole book on a single name, the brief is that the
                   # ranking does the work — but the shipped gates emit roughly
                   # one signal at a time, so there is nothing to rank. Opening
                   # the gates is what makes the ranking testable at all.
                   signal_mode=a.mode, min_cluster_confluence=a.confluence,
                   entry_threshold_bps=a.threshold,
                   min_weighted_score_no_ema=a.minscore,
                   cooldown_minutes=a.cooldown)
    rp = Replay(cfg, "bist")
    rng = np.random.default_rng(7)

    grid = sorted({t for b in hist.values() for t in b.t if t >= cutoff})
    idx = {s: {t: i for i, t in enumerate(b.t)} for s, b in hist.items()}

    equity = a.capital
    open_pos: list[dict] = []
    last_entry: dict[str, pd.Timestamp] = {}
    trades: list[dict] = []
    curve = []
    cycles = signals_seen = 0

    for now in grid:
        # ── manage open positions ──────────────────────────────
        still = []
        for p in open_pos:
            i = idx[p["sym"]].get(now)
            if i is None:
                still.append(p)
                continue
            bar = hist[p["sym"]].iloc[i]
            hi, lo, cl = float(bar.h), float(bar.l), float(bar.c)
            exit_px = reason = None
            if lo <= p["sl"]:
                exit_px, reason = p["sl"], "SL"
            elif hi >= p["tp"]:
                exit_px, reason = p["tp"], "TP"
            elif (now - p["t0"]) >= timedelta(minutes=cfg.max_hold_minutes):
                exit_px, reason = cl, "TIMEOUT"
            if exit_px is None:
                still.append(p)
                continue
            gross = (exit_px - p["entry"]) * p["qty"]
            fee = exit_px * p["qty"] * a.commission
            equity += gross - fee
            trades.append({"sym": p["sym"], "reason": reason,
                           "ret": exit_px / p["entry"] - 1,
                           "pnl": gross - fee - p["fee_in"], "t": now})
        open_pos = still

        # ── scan ───────────────────────────────────────────────
        if len(open_pos) < cfg.max_open_positions:
            held = {p["sym"] for p in open_pos}
            snaps = []
            for s, b in hist.items():
                if s in held:
                    continue
                i = idx[s].get(now)
                if i is None or i < cfg.kline_limit:
                    continue
                ce = last_entry.get(s)
                if ce is not None and (now - ce) < timedelta(minutes=cfg.cooldown_minutes):
                    continue
                px = float(b.c.iloc[i - 1])
                if px <= 0:
                    continue
                sp = bist_min_spread_frac(px) * 1e4
                sn = rp.snapshot(s, b, i, b, i, sp, 0.0)
                if sn is not None:
                    snaps.append(sn)
            if snaps:
                sigs = rp.strategy.generate_signals(
                    snaps, [{"symbol": s} for s in held])
                signals_seen += len(sigs)
                # Take the STRONGEST signal, not the first one the engine
                # happened to emit. With a single slot and the whole book on
                # one name, which candidate gets picked is the entire strategy.
                if a.pick == "random":
                    rng.shuffle(sigs)
                else:
                    sigs = sorted(sigs, key=lambda s: (
                        getattr(s, "weighted_score", 0.0),
                        getattr(s, "confluence_score", 0)),
                        reverse=(a.pick == "strongest"))
                for sig in sigs:
                    if len(open_pos) >= cfg.max_open_positions:
                        break
                    if sig.side != "LONG":          # cash equity: no shorts
                        continue
                    sn = next((x for x in snaps if x.symbol == sig.symbol), None)
                    if sn is None:
                        continue
                    stake = equity / cfg.max_open_positions
                    half = bist_min_spread_frac(sn.mid_price) / 2
                    entry = sn.mid_price * (1 + half)
                    qty = stake / entry
                    fee_in = stake * a.commission
                    equity -= fee_in
                    open_pos.append({
                        "sym": sig.symbol, "entry": entry, "qty": qty,
                        "tp": entry * (1 + a.tp),
                        "sl": entry * (1 - cfg.sl_bps / 1e4),
                        "t0": now, "fee_in": fee_in,
                    })
                    last_entry[sig.symbol] = now
        unreal = 0.0
        for p in open_pos:
            i = idx[p["sym"]].get(now)
            if i is not None:
                unreal += (float(hist[p["sym"]].c.iloc[i]) - p["entry"]) * p["qty"]
        curve.append(equity + unreal)
        cycles += 1

    final = curve[-1] if curve else a.capital
    T = pd.DataFrame(trades)
    print(f"\n  {a.capital:,.0f} TL  →  {final:,.0f} TL   "
          f"({final/a.capital-1:+.1%} over {a.days} days)")
    print(f"  {len(T)} trades · {cycles} hourly cycles · {signals_seen} signals "
          f"({signals_seen/max(cycles,1):.1f} candidates per cycle)")
    if len(T):
        w = T[T.pnl > 0]
        print(f"  win rate {len(w)/len(T)*100:.0f}% · "
              f"avg win {w.pnl.mean() if len(w) else 0:+,.0f} TL · "
              f"avg loss {T[T.pnl<=0].pnl.mean():+,.0f} TL")
        for r, g in T.groupby("reason"):
            print(f"    {r:8s} {len(g):4d} trades  {g.pnl.sum():+9,.0f} TL")
        e = pd.Series(curve)
        print(f"  max drawdown {(e/e.cummax()-1).min()*100:.1f}%")


if __name__ == "__main__":
    main()
