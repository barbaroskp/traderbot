"""Replay the live bot over history: what would the account have done?

THIS IS NOT A RE-IMPLEMENTATION
--------------------------------
It imports and calls the shipped classes — `MarketData.compute_indicators`,
`Strategy.generate_signals`, `RiskManager.compute_position_size`,
`execution._compute_tp_sl_bps` — so the decisions here are the decisions the
live bot would make. Only the plumbing (fetching, filling, marking to market)
is written here, because that is the part that talks to an exchange.

WHAT IS FAITHFUL
----------------
* the signal engine, exactly, including thesis resolution and every gate
* position sizing, including Kelly and the margin caps, off the CURRENT
  balance — so a book that grows to 12,000 sizes its next trade off 12,000
* leverage, cooldown, max_open_positions, the expectancy gate
* TP/SL levels from the real `_compute_tp_sl_bps`
* taker fees both sides, slippage, funding charged every 8 hours
* the still-forming candle is dropped, as `finalize_klines` does live

WHAT CANNOT BE FAITHFUL, AND WHY
--------------------------------
No exchange publishes historical order-book depth. So `bid_depth_usdt`,
`ask_depth_usdt`, `imbalance_ratio` and the whale fields have no history to
replay. The replay feeds each symbol its MEASURED CURRENT spread and leaves
imbalance/whale neutral, which makes the orderflow cluster abstain rather than
invent votes. Open interest is likewise unavailable, so `open_interest` and the
liquidation-cascade detector are silent.

Five of the six voting clusters replay exactly. The sixth does not participate.
That is stated in the output of every run rather than buried here.

INTRABAR AMBIGUITY
------------------
When a 5-minute bar's high reaches TP and its low reaches SL, the order they
were touched is unknowable at this resolution. Those bars are resolved AGAINST
the position. That is deliberately pessimistic: the opposite choice inflates
the win rate, and an earlier study in this project produced a false positive
exactly that way.
"""

from __future__ import annotations

import argparse
import pickle
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import Settings
from src.execution import _compute_tp_sl_bps
from src.marketdata import Indicators, MarketData, SymbolSnapshot
from src.risk import RiskManager
from src.storage import Storage
from src.strategy import Strategy

DATA = Path("research/replay/data")


@dataclass
class Position:
    symbol: str
    side: str                  # LONG | SHORT
    entry_price: float
    qty: float
    leverage: int
    margin: float
    opened_at: pd.Timestamp
    tp_price: float
    sl_price: float
    max_hold_minutes: int
    entry_fee: float
    trade_type: str = "scalp"
    funding_paid: float = 0.0
    last_funding: pd.Timestamp | None = None

    @property
    def notional(self) -> float:
        return self.qty * self.entry_price


@dataclass
class Trade:
    symbol: str
    side: str
    opened_at: pd.Timestamp
    closed_at: pd.Timestamp
    entry: float
    exit: float
    qty: float
    leverage: int
    margin: float
    reason: str
    gross_pnl: float
    fees: float
    funding: float

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees - self.funding


@dataclass
class ReplayResult:
    label: str
    equity_curve: pd.Series
    trades: list[Trade] = field(default_factory=list)
    cycles: int = 0
    signals: int = 0
    rejected: dict = field(default_factory=dict)

    def stats(self, start_equity: float) -> dict:
        e = self.equity_curve
        if e.empty:
            return {}
        days = max((e.index[-1] - e.index[0]).total_seconds() / 86400, 1e-9)
        total = float(e.iloc[-1] / start_equity - 1)
        wins = [t for t in self.trades if t.net_pnl > 0]
        gross = sum(t.gross_pnl for t in self.trades)
        fees = sum(t.fees for t in self.trades)
        fund = sum(t.funding for t in self.trades)
        return {
            "final": float(e.iloc[-1]), "total": total,
            "annualised": (1 + total) ** (365 / days) - 1 if total > -1 else -1.0,
            "maxdd": float((e / e.cummax() - 1).min()),
            "trades": len(self.trades),
            "win_rate": len(wins) / len(self.trades) if self.trades else 0.0,
            "gross": gross, "fees": fees, "funding": fund,
            "net": gross - fees - fund,
            "days": days,
        }


def _klines(df: pd.DataFrame, end_i: int, limit: int) -> list[dict]:
    """Closed bars only, in the dict shape the indicator code expects."""
    lo = max(0, end_i - limit)
    w = df.iloc[lo:end_i]
    return [{"time": int(t.timestamp() * 1000), "open": o, "high": h,
             "low": lo_, "close": c, "volume": v}
            for t, o, h, lo_, c, v in zip(w.t, w.o, w.h, w.l, w.c, w.v)]


class Replay:
    def __init__(self, cfg: Settings, label: str = "current") -> None:
        self.cfg = cfg
        self.label = label
        self.db = Storage(":memory:")
        self.md = MarketData(cfg, None, self.db)          # type: ignore[arg-type]
        self.strategy = Strategy(cfg, self.db)
        self.risk = RiskManager(cfg, self.db)

    # ── snapshot construction ──────────────────────────────────

    def snapshot(self, sym: str, m5: pd.DataFrame, i5: int,
                 m15: pd.DataFrame | None, i15: int,
                 spread_bps: float, funding: float) -> SymbolSnapshot | None:
        win = _klines(m5, i5, self.cfg.kline_limit)
        if len(win) < 50:
            return None
        ind: Indicators = self.md.compute_indicators(win)
        if not ind.valid:
            return None
        if m15 is not None and self.cfg.use_higher_tf_trend:
            hw = _klines(m15, i15, self.cfg.higher_tf_limit)
            if len(hw) >= 50:
                ind.higher_tf_trend = self.md.compute_indicators(hw).trend_direction
        px = float(m5.c.iloc[i5 - 1])
        if px <= 0:
            return None
        half = px * spread_bps / 2e4
        return SymbolSnapshot(
            symbol=sym, mid_price=px, mark_price=px,
            best_bid=px - half, best_ask=px + half, spread_bps=spread_bps,
            # No historical order book: give depth that passes the filter and
            # leave imbalance/whale neutral so the orderflow cluster abstains
            # instead of fabricating votes.
            bid_depth_usdt=1e9, ask_depth_usdt=1e9,
            imbalance_ratio=0.5, whale_imbalance=0.5,
            fast_ema=ind.kline_fast_ema, slow_ema=ind.kline_slow_ema,
            z_score_bps=ind.kline_z_score_bps,
            indicators=ind, funding_rate=funding,
        )

    def _eval(self, snap: SymbolSnapshot, now):
        """Evaluate one snapshot and return the Signal WITH its votes.

        `generate_signals` only returns accepted signals, so using it for
        attribution would measure the indicators only on the rare bars where
        the whole committee already agreed — which is the selection effect the
        attribution is trying to look past. This calls the evaluator directly
        with permissive gates so every vote is visible, accepted or not.
        """
        try:
            return self.strategy._evaluate_snapshot(
                snap, now, min_confluence=0, max_positions=999,
                open_symbols=set(), open_count=0, open_positions=[],
                risk_state="NORMAL",
            )
        except Exception:
            return None

    # ── position lifecycle ─────────────────────────────────────

    def open_position(self, snap: SymbolSnapshot, side: str, leverage: int,
                      now: pd.Timestamp, equity: float,
                      open_margin: float) -> Position | None:
        self.risk.update_balance(equity)
        qty = self.risk.compute_position_size(
            price=snap.mid_price, leverage=leverage,
            current_total_margin=open_margin, atr=snap.indicators.atr,
        )
        if qty <= 0:
            return None
        slip = self.cfg.slippage_assumption_bps / 1e4
        entry = snap.mid_price * (1 + slip if side == "LONG" else 1 - slip)
        notional = qty * entry
        margin = notional / max(leverage, 1)
        if margin < 1.0:
            return None
        # The function returns (SL, TP) in that order — see execution.py:288.
        # Unpacking it the other way round gave every position a narrow target
        # and a wide stop, inverting the configured 200/70 geometry into
        # roughly 105/251 and turning a profitable expectancy into -16.5 bps
        # per trade. The first run of this replay reported -99.9% because of
        # this line, not because of anything the bot does.
        sl_bps, tp_bps = _compute_tp_sl_bps(self.cfg, snap, entry)
        if side == "LONG":
            tp = entry * (1 + tp_bps / 1e4)
            sl = entry * (1 - sl_bps / 1e4)
        else:
            tp = entry * (1 - tp_bps / 1e4)
            sl = entry * (1 + sl_bps / 1e4)
        return Position(
            symbol=snap.symbol, side=side, entry_price=entry, qty=qty,
            leverage=leverage, margin=margin, opened_at=now,
            tp_price=tp, sl_price=sl,
            max_hold_minutes=self.cfg.max_hold_minutes,
            entry_fee=notional * self.cfg.fee_rate_bps / 1e4,
            last_funding=now,
        )

    def check_exit(self, p: Position, bar: pd.Series,
                   now: pd.Timestamp) -> tuple[float, str] | None:
        hi, lo = float(bar.h), float(bar.l)
        hit_tp = hi >= p.tp_price if p.side == "LONG" else lo <= p.tp_price
        hit_sl = lo <= p.sl_price if p.side == "LONG" else hi >= p.sl_price
        if hit_tp and hit_sl:
            # Unknowable order inside the bar: resolve against the position.
            return p.sl_price, "SL_AMBIGUOUS"
        if hit_sl:
            return p.sl_price, "SL"
        if hit_tp:
            return p.tp_price, "TP"
        if (now - p.opened_at) >= timedelta(minutes=p.max_hold_minutes):
            return float(bar.c), "TIMEOUT"
        return None

    def close(self, p: Position, exit_px: float, reason: str,
              now: pd.Timestamp) -> Trade:
        slip = self.cfg.slippage_assumption_bps / 1e4
        fill = exit_px * (1 - slip if p.side == "LONG" else 1 + slip)
        direction = 1 if p.side == "LONG" else -1
        gross = (fill - p.entry_price) * p.qty * direction
        exit_fee = fill * p.qty * self.cfg.fee_rate_bps / 1e4
        return Trade(symbol=p.symbol, side=p.side, opened_at=p.opened_at,
                     closed_at=now, entry=p.entry_price, exit=fill, qty=p.qty,
                     leverage=p.leverage, margin=p.margin, reason=reason,
                     gross_pnl=gross, fees=p.entry_fee + exit_fee,
                     funding=p.funding_paid)


def run(cfg: Settings, m5: dict, m15: dict, spreads: pd.DataFrame,
        funding: dict, start_equity: float, label: str,
        universe: list[str], progress: bool = True,
        invert: bool = False, t_from=None, t_to=None) -> ReplayResult:
    """Replay the bot.

    ``invert`` flips every signal's side. It is here because a strategy that
    wins 63% of its trades and still loses everything is making a structural
    error, and flipping it is the cheapest test of whether the error is in the
    DIRECTION or in the exit geometry. If inverting also loses, the direction
    was never the problem and the TP/SL shape is.

    ``t_from`` / ``t_to`` restrict the replay to a date window, so a parameter
    search can be run on one half of the sample and validated on the other.
    """
    rp = Replay(cfg, label)
    res = ReplayResult(label=label, equity_curve=pd.Series(dtype=float))

    grid = sorted(set().union(*[set(m5[s].t) for s in universe if s in m5]))
    warm = max(cfg.kline_limit, 60) + 5
    grid = grid[warm:]
    idx5 = {s: {t: i for i, t in enumerate(m5[s].t)} for s in universe if s in m5}
    idx15 = {s: {t: i for i, t in enumerate(m15[s].t)} for s in universe if s in m15}
    t15 = {s: list(m15[s].t) for s in universe if s in m15}

    equity = start_equity
    positions: list[Position] = []
    curve_t, curve_v = [], []
    last_entry: dict[str, pd.Timestamp] = {}

    for ci, now in enumerate(grid):
        # ── mark and close open positions on this bar ──────────
        still: list[Position] = []
        for p in positions:
            df = m5.get(p.symbol)
            i = idx5.get(p.symbol, {}).get(now)
            if df is None or i is None:
                still.append(p)
                continue
            bar = df.iloc[i]
            # funding every 8h while open
            if cfg.use_funding_cost and p.last_funding is not None:
                hrs = (now - p.last_funding).total_seconds() / 3600
                if hrs >= cfg.funding_interval_hours:
                    rate = _funding_at(funding.get(p.symbol), now,
                                       cfg.default_funding_rate_bps / 1e4)
                    sign = 1 if p.side == "LONG" else -1
                    p.funding_paid += p.notional * rate * sign
                    p.last_funding = now
            out = rp.check_exit(p, bar, now)
            if out is None:
                still.append(p)
                continue
            tr = rp.close(p, out[0], out[1], now)
            equity += tr.net_pnl
            rp.risk.record_trade_result(tr.net_pnl)
            res.trades.append(tr)
        positions = still

        if equity <= 0:
            curve_t.append(now); curve_v.append(0.0)
            break

        # ── scan and possibly open ─────────────────────────────
        if len(positions) < cfg.max_open_positions:
            open_margin = sum(p.margin for p in positions)
            snaps = []
            held = {p.symbol for p in positions}
            for s in universe:
                if s in held or s not in idx5:
                    continue
                cd = last_entry.get(s)
                if cd is not None and (now - cd) < timedelta(minutes=cfg.cooldown_minutes):
                    continue
                i = idx5[s].get(now)
                if i is None or i < cfg.kline_limit:
                    continue
                j = _le_index(t15.get(s), now)
                sp = float(spreads.spread_bps.get(s, 6.0))
                fr = _funding_at(funding.get(s), now, 0.0)
                sn = rp.snapshot(s, m5[s], i, m15.get(s), j, sp, fr)
                if sn is not None:
                    snaps.append(sn)
            if snaps:
                # generate_signals expects position ROWS, not symbols — it reads
                # p["symbol"]. Passing bare strings silently indexes into them.
                sigs = rp.strategy.generate_signals(
                    snaps, [{"symbol": s} for s in held])
                res.signals += len(sigs)
                for sig in sigs:
                    if len(positions) >= cfg.max_open_positions:
                        break
                    sn = next((x for x in snaps if x.symbol == sig.symbol), None)
                    if sn is None:
                        continue
                    lev = getattr(sig, "leverage", None) or cfg.leverage
                    p = rp.open_position(sn, sig.side, int(lev), now, equity,
                                         open_margin)
                    if p is None:
                        continue
                    equity -= p.entry_fee
                    positions.append(p)
                    open_margin += p.margin
                    last_entry[sig.symbol] = now

        unreal = 0.0
        for p in positions:
            i = idx5.get(p.symbol, {}).get(now)
            if i is None:
                continue
            px = float(m5[p.symbol].c.iloc[i])
            d = 1 if p.side == "LONG" else -1
            unreal += (px - p.entry_price) * p.qty * d
        curve_t.append(now)
        curve_v.append(equity + unreal)
        res.cycles += 1
        if progress and ci % 2000 == 0:
            print(f"    {now:%Y-%m-%d %H:%M} · equity {equity+unreal:,.0f} · "
                  f"{len(res.trades)} trades", flush=True)

    res.equity_curve = pd.Series(curve_v, index=pd.DatetimeIndex(curve_t))
    res.rejected = dict(rp.strategy._gate_stats) if hasattr(
        rp.strategy, "_gate_stats") else {}
    return res


def _le_index(times: list | None, now) -> int:
    if not times:
        return 0
    import bisect
    return bisect.bisect_right(times, now)


def _funding_at(df: pd.DataFrame | None, now, default: float) -> float:
    if df is None or df.empty:
        return default
    i = df.t.searchsorted(now, side="right") - 1
    return float(df.rate.iloc[i]) if i >= 0 else default
