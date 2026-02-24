"""Lightweight backtest engine – reuses Strategy & RiskManager on historical klines.

Design goals:
  - Memory-efficient: streams candles, processes one symbol at a time (~20-50 MB)
  - No DB bloat: uses in-memory SQLite, only final report saved
  - CPU-friendly: single-threaded, runs as separate CLI command (not alongside live bot)
  - Reuses existing code: Strategy.generate_signals() + RiskManager unchanged

Usage:
  python -m src.backtest --symbols BTC-USDT,ETH-USDT --days 30
  python -m src.backtest --symbols BTC-USDT --days 90 --tp-bps 100 --sl-bps 50

Flow per symbol:
  1. Fetch historical 5m klines from BingX (paginated)
  2. Walk candles with a sliding window (100 candles → indicators)
  3. Each window → build SymbolSnapshot → Strategy.generate_signals()
  4. If signal accepted → open virtual position
  5. Each subsequent candle → check SL/TP/timeout
  6. After all candles → aggregate results
"""

from __future__ import annotations

import asyncio
import itertools
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from src.bingx_client import BingXClient
from src.config import Settings
from src.logger import get_logger
from src.marketdata import Indicators, MarketData, SymbolSnapshot
from src.risk import RiskManager, RiskState
from src.storage import Storage
from src.strategy import Strategy

log = get_logger(__name__)

# ── Virtual position for backtest ────────────────────────────────────────────


@dataclass
class VirtualPosition:
    """Simulated position during backtest."""

    symbol: str
    side: str  # LONG or SHORT
    entry_price: float
    qty: float
    notional: float
    sl_price: float
    tp_price: float
    opened_at: datetime
    sl_bps: float = 0.0
    tp_bps: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    breakeven_triggered: bool = False
    partial_tp_filled: bool = False


@dataclass
class TradeResult:
    """Result of a completed backtest trade."""

    symbol: str
    side: str
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    pnl_pct: float
    exit_reason: str  # TP, SL, TIMEOUT
    duration_minutes: float
    opened_at: datetime
    closed_at: datetime


@dataclass
class BacktestReport:
    """Aggregate report for a backtest run."""

    symbols: list[str]
    start_date: str
    end_date: str
    total_candles: int
    initial_capital: float
    final_capital: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    total_pnl_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    profit_factor: float
    avg_trade_pnl: float
    avg_win: float
    avg_loss: float
    avg_duration_minutes: float
    best_trade: float
    worst_trade: float
    longest_win_streak: int
    longest_loss_streak: int
    trades: list[TradeResult] = field(default_factory=list)
    # Per-symbol breakdown
    symbol_stats: dict[str, dict[str, Any]] = field(default_factory=dict)


# ── Backtest Engine ──────────────────────────────────────────────────────────


class BacktestEngine:
    """Run strategy on historical kline data."""

    def __init__(
        self,
        cfg: Settings,
        symbols: list[str],
        days: int = 30,
        initial_capital: float | None = None,
    ) -> None:
        self.cfg = cfg
        self.symbols = symbols
        self.days = days
        self.initial_capital = initial_capital or cfg.initial_capital_usdt
        self.capital = self.initial_capital
        self.peak_capital = self.initial_capital

        # In-memory storage (no disk usage)
        self._db = Storage(":memory:")
        self._client = BingXClient(cfg)
        self._market = MarketData(cfg, self._client, self._db)
        self._strategy = Strategy(cfg, self._db)
        self._risk = RiskManager(cfg, self._db)

        # State
        self._positions: list[VirtualPosition] = []
        self._trades: list[TradeResult] = []
        self._equity_curve: list[float] = []
        self._total_candles = 0

    async def run(self) -> BacktestReport:
        """Run the full backtest across all symbols."""
        print(f"\n{'='*60}")
        print(f"  BACKTEST ENGINE")
        print(f"  Symbols: {', '.join(self.symbols)}")
        print(f"  Period: {self.days} days")
        print(f"  Capital: {self.initial_capital} USDT")
        print(f"  Strategy: EMA confluence (same as live)")
        print(f"{'='*60}\n")

        for symbol in self.symbols:
            await self._run_symbol(symbol)

        await self._client.close()
        return self._build_report()

    async def _run_symbol(self, symbol: str) -> None:
        """Backtest a single symbol."""
        print(f"[{symbol}] Fetching {self.days} days of 5m klines...")

        klines = await self._fetch_historical_klines(symbol)
        if len(klines) < 100:
            print(f"[{symbol}] Not enough data ({len(klines)} candles), skipping.")
            return

        print(f"[{symbol}] Got {len(klines)} candles. Running simulation...")
        self._total_candles += len(klines)

        window_size = max(self.cfg.kline_limit, 100)
        trades_before = len(self._trades)

        # Walk through candles with sliding window
        for i in range(window_size, len(klines)):
            window = klines[i - window_size : i]
            current = klines[i]

            # Parse current candle
            ts = self._parse_candle_time(current)
            close = float(current.get("close", current.get("c", 0)))
            high = float(current.get("high", current.get("h", 0)))
            low = float(current.get("low", current.get("l", 0)))

            if close <= 0:
                continue

            # Check exits first (using current candle's high/low/close)
            self._check_exits(symbol, close, high, low, ts)

            # Build snapshot from window (same as live bot)
            snap = self._build_snapshot(symbol, window, current)
            if snap is None:
                continue

            # Record equity
            self._equity_curve.append(self.capital)

            # Generate signals (only if no position open for this symbol)
            has_position = any(p.symbol == symbol for p in self._positions)
            if has_position:
                continue

            open_positions = self._get_open_positions_dict()
            signals = self._strategy.generate_signals(
                snapshots=[snap],
                open_positions=open_positions,
                risk_state=self._risk.state.value,
            )

            for sig in signals:
                if not sig.accepted:
                    continue
                self._open_position(sig, snap, ts)

        symbol_trades = len(self._trades) - trades_before
        print(f"[{symbol}] Done. {symbol_trades} trades completed.")

    async def _fetch_historical_klines(self, symbol: str) -> list[dict[str, Any]]:
        """Fetch historical klines from BingX in paginated chunks.

        BingX API returns max 1000 candles per request.
        For 30 days of 5m candles: 30 * 24 * 12 = 8640 candles → 9 requests.
        """
        all_klines: list[dict[str, Any]] = []
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(days=self.days)

        # 5m = 300 seconds per candle
        interval_seconds = 300
        max_per_request = 1000

        current_start = start_time
        request_count = 0

        while current_start < end_time:
            try:
                klines = await self._client.get_klines(
                    symbol,
                    interval=self.cfg.kline_interval,
                    limit=max_per_request,
                    start_time=int(current_start.timestamp() * 1000),
                )
                request_count += 1

                if not klines:
                    break

                all_klines.extend(klines)

                # Move start forward past last candle
                last_ts = self._parse_candle_time(klines[-1])
                if last_ts <= current_start:
                    # No progress, move forward by chunk
                    current_start += timedelta(seconds=interval_seconds * max_per_request)
                else:
                    current_start = last_ts + timedelta(seconds=interval_seconds)

                # Rate limit: don't hammer the API
                if request_count % 5 == 0:
                    await asyncio.sleep(1.0)
                else:
                    await asyncio.sleep(0.2)

            except Exception as exc:
                print(f"  [WARN] Kline fetch error: {exc}")
                await asyncio.sleep(2.0)
                current_start += timedelta(seconds=interval_seconds * max_per_request)

        # Sort by time and deduplicate
        seen_times: set[str] = set()
        unique: list[dict[str, Any]] = []
        for k in all_klines:
            t = str(k.get("time", k.get("t", "")))
            if t not in seen_times:
                seen_times.add(t)
                unique.append(k)

        unique.sort(key=lambda x: str(x.get("time", x.get("t", ""))))
        return unique

    def _build_snapshot(
        self,
        symbol: str,
        window: list[dict[str, Any]],
        current: dict[str, Any],
    ) -> SymbolSnapshot | None:
        """Build a SymbolSnapshot from a kline window (same indicator computation as live)."""
        indicators = self._market.compute_indicators(window)
        if not indicators.valid:
            return None

        close = float(current.get("close", current.get("c", 0)))
        high = float(current.get("high", current.get("h", 0)))
        low = float(current.get("low", current.get("l", 0)))
        volume = float(current.get("volume", current.get("v", 0)))

        if close <= 0:
            return None

        # Use kline-based EMA for z-score (same as live bot)
        fast_ema = indicators.kline_fast_ema
        slow_ema = indicators.kline_slow_ema
        z_score_bps = indicators.kline_z_score_bps

        # Simulate reasonable spread and depth (backtest doesn't have live orderbook)
        spread_bps = 5.0  # typical 5 bps spread
        bid_depth = 10000.0  # assume decent liquidity
        ask_depth = 10000.0

        snap = SymbolSnapshot(
            symbol=symbol,
            mid_price=close,
            mark_price=close,
            best_bid=close * (1 - spread_bps / 20_000),
            best_ask=close * (1 + spread_bps / 20_000),
            spread_bps=spread_bps,
            bid_depth_usdt=bid_depth,
            ask_depth_usdt=ask_depth,
            imbalance_ratio=0.5,
            fast_ema=fast_ema,
            slow_ema=slow_ema,
            z_score_bps=z_score_bps,
            indicators=indicators,
            funding_rate=0.0,
        )
        return snap

    def _open_position(
        self,
        signal: Any,
        snap: SymbolSnapshot,
        ts: datetime,
    ) -> None:
        """Open a virtual position."""
        # Size the position
        current_notional = sum(p.notional for p in self._positions)
        qty = self._risk.compute_position_size(
            price=snap.mid_price,
            current_total_notional=current_notional,
            atr=snap.indicators.atr if snap.indicators.valid else 0.0,
        )
        if qty <= 0:
            return

        entry_price = snap.mid_price
        notional = entry_price * qty

        # Compute SL/TP (same logic as execution.py)
        if self.cfg.use_dynamic_tp_sl and snap.indicators.atr > 0:
            atr_bps = (snap.indicators.atr / entry_price) * 10_000
            sl_bps = max(atr_bps * self.cfg.atr_sl_multiplier, self.cfg.min_sl_bps)
            tp_bps = max(atr_bps * self.cfg.atr_tp_multiplier, self.cfg.min_tp_bps)
        else:
            sl_bps = self.cfg.sl_bps
            tp_bps = self.cfg.tp_bps

        if signal.side == "LONG":
            sl_price = entry_price * (1 - sl_bps / 10_000)
            tp_price = entry_price * (1 + tp_bps / 10_000)
        else:
            sl_price = entry_price * (1 + sl_bps / 10_000)
            tp_price = entry_price * (1 - tp_bps / 10_000)

        # Apply fee on entry
        fee = notional * (self.cfg.fee_rate_bps / 10_000)
        self.capital -= fee

        pos = VirtualPosition(
            symbol=signal.symbol,
            side=signal.side,
            entry_price=entry_price,
            qty=qty,
            notional=notional,
            sl_price=sl_price,
            tp_price=tp_price,
            sl_bps=sl_bps,
            tp_bps=tp_bps,
            opened_at=ts,
            highest_price=entry_price,
            lowest_price=entry_price,
        )
        self._positions.append(pos)
        self._strategy.set_cooldown(signal.symbol)

    def _check_exits(
        self,
        symbol: str,
        close: float,
        high: float,
        low: float,
        ts: datetime,
    ) -> None:
        """Check all positions for this symbol against the current candle."""
        to_remove: list[VirtualPosition] = []

        for pos in self._positions:
            if pos.symbol != symbol:
                continue

            exit_reason = ""
            exit_price = close

            # Update high/low tracking
            if pos.side == "LONG":
                pos.highest_price = max(pos.highest_price, high)
                pos.lowest_price = min(pos.lowest_price, low)
            else:
                pos.highest_price = max(pos.highest_price, high)
                pos.lowest_price = min(pos.lowest_price, low)

            # Trailing stop update
            profit_bps = self._profit_bps(pos.side, pos.entry_price, close)
            if self.cfg.use_trailing_stop and profit_bps >= pos.tp_bps * self.cfg.trailing_activation_pct:
                trail_bps = pos.sl_bps * self.cfg.trailing_distance_pct
                if pos.side == "LONG":
                    new_sl = close * (1 - trail_bps / 10_000)
                    pos.sl_price = max(pos.sl_price, new_sl)
                else:
                    new_sl = close * (1 + trail_bps / 10_000)
                    pos.sl_price = min(pos.sl_price, new_sl)

            # Breakeven stop
            if self.cfg.use_breakeven_stop and not pos.breakeven_triggered:
                if profit_bps >= pos.tp_bps * self.cfg.breakeven_activation_pct:
                    if pos.side == "LONG":
                        be = pos.entry_price * (1 + self.cfg.breakeven_buffer_bps / 10_000)
                        pos.sl_price = max(pos.sl_price, be)
                    else:
                        be = pos.entry_price * (1 - self.cfg.breakeven_buffer_bps / 10_000)
                        pos.sl_price = min(pos.sl_price, be)
                    pos.breakeven_triggered = True

            # SL check (using candle low/high for realism)
            if pos.side == "LONG" and low <= pos.sl_price:
                exit_reason = "SL"
                exit_price = pos.sl_price  # assume fill at SL level
            elif pos.side == "SHORT" and high >= pos.sl_price:
                exit_reason = "SL"
                exit_price = pos.sl_price

            # TP check
            if not exit_reason:
                if pos.side == "LONG" and high >= pos.tp_price:
                    exit_reason = "TP"
                    exit_price = pos.tp_price
                elif pos.side == "SHORT" and low <= pos.tp_price:
                    exit_reason = "TP"
                    exit_price = pos.tp_price

            # Timeout check
            if not exit_reason:
                elapsed = (ts - pos.opened_at).total_seconds() / 60
                if elapsed >= self.cfg.max_hold_minutes:
                    exit_reason = "TIMEOUT"
                    exit_price = close

            # Time-decay SL tightening
            if not exit_reason and self.cfg.use_time_decay_sl:
                elapsed = (ts - pos.opened_at).total_seconds() / 60
                decay_start = self.cfg.max_hold_minutes * self.cfg.time_decay_start_pct
                if elapsed > decay_start and profit_bps < 0:
                    decay_progress = min(
                        1.0,
                        (elapsed - decay_start) / (self.cfg.max_hold_minutes - decay_start),
                    )
                    reduction = pos.sl_bps * self.cfg.time_decay_sl_reduction_pct * decay_progress
                    tightened = max(pos.sl_bps * 0.3, pos.sl_bps - reduction)
                    if pos.side == "LONG":
                        new_sl = pos.entry_price * (1 - tightened / 10_000)
                        pos.sl_price = max(pos.sl_price, new_sl)
                    else:
                        new_sl = pos.entry_price * (1 + tightened / 10_000)
                        pos.sl_price = min(pos.sl_price, new_sl)

            if exit_reason:
                self._close_position(pos, exit_price, exit_reason, ts)
                to_remove.append(pos)

        for p in to_remove:
            self._positions.remove(p)

    def _close_position(
        self,
        pos: VirtualPosition,
        exit_price: float,
        reason: str,
        ts: datetime,
    ) -> None:
        """Close a virtual position and record the trade."""
        if pos.side == "LONG":
            pnl = (exit_price - pos.entry_price) * pos.qty
        else:
            pnl = (pos.entry_price - exit_price) * pos.qty

        # Exit fee
        fee = exit_price * pos.qty * (self.cfg.fee_rate_bps / 10_000)
        pnl -= fee

        self.capital += pnl
        if self.capital > self.peak_capital:
            self.peak_capital = self.capital

        # Record trade result for risk manager
        self._risk.record_trade_result(pnl)
        self._risk.update_balance(self.capital)

        duration = (ts - pos.opened_at).total_seconds() / 60
        pnl_pct = (pnl / pos.notional) * 100 if pos.notional > 0 else 0.0

        trade = TradeResult(
            symbol=pos.symbol,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            qty=pos.qty,
            pnl=pnl,
            pnl_pct=pnl_pct,
            exit_reason=reason,
            duration_minutes=duration,
            opened_at=pos.opened_at,
            closed_at=ts,
        )
        self._trades.append(trade)

    def _get_open_positions_dict(self) -> list[dict[str, Any]]:
        """Convert virtual positions to dict format for Strategy.generate_signals()."""
        return [
            {
                "symbol": p.symbol,
                "side": p.side,
                "entry_price": p.entry_price,
                "qty": p.qty,
                "status": "OPEN",
            }
            for p in self._positions
        ]

    @staticmethod
    def _profit_bps(side: str, entry: float, current: float) -> float:
        if entry <= 0:
            return 0.0
        if side == "LONG":
            return ((current - entry) / entry) * 10_000
        return ((entry - current) / entry) * 10_000

    @staticmethod
    def _parse_candle_time(candle: dict[str, Any]) -> datetime:
        """Parse candle timestamp (BingX uses ms epoch or ISO)."""
        raw = candle.get("time", candle.get("t", 0))
        if isinstance(raw, (int, float)) and raw > 1_000_000_000_000:
            # Millisecond epoch
            return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
        elif isinstance(raw, (int, float)) and raw > 1_000_000_000:
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        elif isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        return datetime.now(timezone.utc)

    # ── Report ────────────────────────────────────────────────────────────────

    def _build_report(self) -> BacktestReport:
        """Build the final backtest report."""
        trades = self._trades
        total = len(trades)
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]

        # Max drawdown from equity curve
        max_dd = 0.0
        peak = self.initial_capital
        for eq in self._equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100 if peak > 0 else 0.0
            max_dd = max(max_dd, dd)

        # Also compute from trades
        running = self.initial_capital
        trade_peak = running
        for t in trades:
            running += t.pnl
            if running > trade_peak:
                trade_peak = running
            dd = (trade_peak - running) / trade_peak * 100 if trade_peak > 0 else 0.0
            max_dd = max(max_dd, dd)

        # Sharpe ratio (annualized, using daily returns approximation)
        if trades:
            returns = [t.pnl_pct for t in trades]
            avg_ret = sum(returns) / len(returns)
            if len(returns) > 1:
                var = sum((r - avg_ret) ** 2 for r in returns) / (len(returns) - 1)
                std_ret = math.sqrt(var)
            else:
                std_ret = 0.0
            # Approximate trades per day and annualize
            if trades[-1].closed_at > trades[0].opened_at:
                days_span = (trades[-1].closed_at - trades[0].opened_at).total_seconds() / 86400
                trades_per_day = total / max(days_span, 1)
            else:
                trades_per_day = 1
            sharpe = (avg_ret / std_ret * math.sqrt(trades_per_day * 365)) if std_ret > 0 else 0.0
        else:
            sharpe = 0.0

        # Profit factor
        gross_profit = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0

        # Win/loss streaks
        longest_win = longest_loss = current_win = current_loss = 0
        for t in trades:
            if t.pnl > 0:
                current_win += 1
                current_loss = 0
                longest_win = max(longest_win, current_win)
            else:
                current_loss += 1
                current_win = 0
                longest_loss = max(longest_loss, current_loss)

        # Per-symbol stats
        symbol_stats: dict[str, dict[str, Any]] = {}
        for sym in self.symbols:
            sym_trades = [t for t in trades if t.symbol == sym]
            sym_wins = [t for t in sym_trades if t.pnl > 0]
            sym_pnl = sum(t.pnl for t in sym_trades)
            symbol_stats[sym] = {
                "trades": len(sym_trades),
                "wins": len(sym_wins),
                "win_rate": len(sym_wins) / len(sym_trades) * 100 if sym_trades else 0,
                "total_pnl": round(sym_pnl, 4),
            }

        # Dates
        start_date = trades[0].opened_at.strftime("%Y-%m-%d") if trades else "N/A"
        end_date = trades[-1].closed_at.strftime("%Y-%m-%d") if trades else "N/A"

        report = BacktestReport(
            symbols=self.symbols,
            start_date=start_date,
            end_date=end_date,
            total_candles=self._total_candles,
            initial_capital=self.initial_capital,
            final_capital=round(self.capital, 4),
            total_trades=total,
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=len(wins) / total * 100 if total > 0 else 0.0,
            total_pnl=round(self.capital - self.initial_capital, 4),
            total_pnl_pct=round((self.capital - self.initial_capital) / self.initial_capital * 100, 2),
            max_drawdown_pct=round(max_dd, 2),
            sharpe_ratio=round(sharpe, 2),
            profit_factor=round(profit_factor, 2) if profit_factor != float("inf") else 999.0,
            avg_trade_pnl=round(sum(t.pnl for t in trades) / total, 4) if total > 0 else 0.0,
            avg_win=round(sum(t.pnl for t in wins) / len(wins), 4) if wins else 0.0,
            avg_loss=round(sum(t.pnl for t in losses) / len(losses), 4) if losses else 0.0,
            avg_duration_minutes=round(sum(t.duration_minutes for t in trades) / total, 1) if total > 0 else 0.0,
            best_trade=round(max((t.pnl for t in trades), default=0.0), 4),
            worst_trade=round(min((t.pnl for t in trades), default=0.0), 4),
            longest_win_streak=longest_win,
            longest_loss_streak=longest_loss,
            trades=trades,
            symbol_stats=symbol_stats,
        )
        return report


# ── Parameter Optimizer ──────────────────────────────────────────────────────


@dataclass
class OptimizationResult:
    """Single parameter combination result."""
    params: dict[str, float]
    pnl_pct: float
    sharpe: float
    win_rate: float
    total_trades: int
    max_drawdown_pct: float
    score: float  # composite fitness score


class ParameterOptimizer:
    """Grid search optimizer over backtest engine.

    Usage:
        optimizer = ParameterOptimizer(
            base_cfg=Settings(),
            symbols=["BTC-USDT", "ETH-USDT"],
            days=30,
            param_grid={
                "tp_bps": [80, 100, 120, 150],
                "sl_bps": [40, 50, 60, 80],
                "min_confluence": [2, 3, 4],
            },
        )
        results = await optimizer.run()
    """

    def __init__(
        self,
        base_cfg: Settings,
        symbols: list[str],
        days: int = 30,
        initial_capital: float | None = None,
        param_grid: dict[str, list[float]] | None = None,
        metric: str = "score",  # score, pnl_pct, sharpe
    ) -> None:
        self.base_cfg = base_cfg
        self.symbols = symbols
        self.days = days
        self.initial_capital = initial_capital
        self.metric = metric

        # Default grid if none provided
        self.param_grid = param_grid or {
            "tp_bps": [80, 100, 120, 150],
            "sl_bps": [40, 50, 60, 80],
            "min_confluence": [2, 3, 4],
        }

    def _generate_combinations(self) -> list[dict[str, float]]:
        """Generate all parameter combinations from grid."""
        keys = list(self.param_grid.keys())
        values = list(self.param_grid.values())
        combos = []
        for combo in itertools.product(*values):
            combos.append(dict(zip(keys, combo)))
        return combos

    @staticmethod
    def _compute_score(report: BacktestReport) -> float:
        """Composite fitness: balance PnL, Sharpe, win rate, and drawdown."""
        if report.total_trades < 5:
            return -999.0  # not enough trades
        pnl_score = report.total_pnl_pct
        sharpe_score = report.sharpe_ratio * 10  # scale sharpe
        dd_penalty = report.max_drawdown_pct * 0.5  # penalize drawdown
        wr_bonus = (report.win_rate - 50) * 0.2  # bonus for >50% WR
        return pnl_score + sharpe_score - dd_penalty + wr_bonus

    async def run(self) -> list[OptimizationResult]:
        """Run grid search and return sorted results."""
        combos = self._generate_combinations()
        total = len(combos)
        print(f"\n{'='*60}")
        print(f"  PARAMETER OPTIMIZER")
        print(f"  Symbols: {', '.join(self.symbols)}")
        print(f"  Period: {self.days} days")
        print(f"  Parameters: {list(self.param_grid.keys())}")
        print(f"  Combinations: {total}")
        print(f"{'='*60}\n")

        results: list[OptimizationResult] = []
        for i, params in enumerate(combos, 1):
            cfg = self.base_cfg.model_copy(update=params)
            engine = BacktestEngine(
                cfg=cfg,
                symbols=self.symbols,
                days=self.days,
                initial_capital=self.initial_capital,
            )
            # Suppress print output during optimization
            import io
            import contextlib
            f = io.StringIO()
            with contextlib.redirect_stdout(f):
                report = await engine.run()

            score = self._compute_score(report)
            result = OptimizationResult(
                params=params,
                pnl_pct=report.total_pnl_pct,
                sharpe=report.sharpe_ratio,
                win_rate=report.win_rate,
                total_trades=report.total_trades,
                max_drawdown_pct=report.max_drawdown_pct,
                score=score,
            )
            results.append(result)

            # Progress
            marker = "***" if i <= 3 or score > 0 else "   "
            print(
                f"  [{i:3d}/{total}] {marker} "
                f"PnL={report.total_pnl_pct:+6.2f}% "
                f"Sharpe={report.sharpe_ratio:+5.2f} "
                f"WR={report.win_rate:5.1f}% "
                f"DD={report.max_drawdown_pct:5.1f}% "
                f"Trades={report.total_trades:3d} "
                f"Score={score:+7.2f} "
                f"| {params}"
            )

        # Sort by chosen metric
        sort_key = self.metric if self.metric != "score" else "score"
        results.sort(key=lambda r: getattr(r, sort_key), reverse=True)

        # Print top 5
        print(f"\n{'='*60}")
        print(f"  TOP 5 PARAMETER COMBINATIONS (by {self.metric})")
        print(f"{'='*60}")
        for rank, r in enumerate(results[:5], 1):
            print(f"\n  #{rank}: Score={r.score:+.2f}")
            print(f"    PnL: {r.pnl_pct:+.2f}% | Sharpe: {r.sharpe:+.2f} | WR: {r.win_rate:.1f}%")
            print(f"    DD: {r.max_drawdown_pct:.1f}% | Trades: {r.total_trades}")
            print(f"    Params: {r.params}")
        print()

        return results


# ── Report printer ───────────────────────────────────────────────────────────


def print_report(report: BacktestReport) -> None:
    """Pretty-print backtest results to console."""
    print(f"\n{'='*60}")
    print(f"  BACKTEST REPORT")
    print(f"{'='*60}")
    print(f"  Period       : {report.start_date} → {report.end_date}")
    print(f"  Symbols      : {', '.join(report.symbols)}")
    print(f"  Total candles: {report.total_candles:,}")
    print(f"{'─'*60}")
    print(f"  Initial capital : {report.initial_capital:.2f} USDT")
    print(f"  Final capital   : {report.final_capital:.2f} USDT")
    print(f"  Total PnL       : {report.total_pnl:+.4f} USDT ({report.total_pnl_pct:+.2f}%)")
    print(f"{'─'*60}")
    print(f"  Total trades    : {report.total_trades}")
    print(f"  Winning         : {report.winning_trades} ({report.win_rate:.1f}%)")
    print(f"  Losing          : {report.losing_trades}")
    print(f"  Avg trade PnL   : {report.avg_trade_pnl:+.4f} USDT")
    print(f"  Avg win         : {report.avg_win:+.4f} USDT")
    print(f"  Avg loss        : {report.avg_loss:+.4f} USDT")
    print(f"  Best trade      : {report.best_trade:+.4f} USDT")
    print(f"  Worst trade     : {report.worst_trade:+.4f} USDT")
    print(f"{'─'*60}")
    print(f"  Max drawdown    : {report.max_drawdown_pct:.2f}%")
    print(f"  Sharpe ratio    : {report.sharpe_ratio:.2f}")
    print(f"  Profit factor   : {report.profit_factor:.2f}")
    print(f"  Avg duration    : {report.avg_duration_minutes:.1f} min")
    print(f"  Win streak      : {report.longest_win_streak}")
    print(f"  Loss streak     : {report.longest_loss_streak}")

    if report.symbol_stats:
        print(f"{'─'*60}")
        print(f"  PER-SYMBOL BREAKDOWN:")
        for sym, stats in report.symbol_stats.items():
            print(
                f"    {sym:15s}  trades={stats['trades']:3d}  "
                f"wins={stats['wins']:3d}  "
                f"wr={stats['win_rate']:5.1f}%  "
                f"pnl={stats['total_pnl']:+.4f}"
            )

    # Exit reason breakdown
    if report.trades:
        reasons: dict[str, int] = {}
        for t in report.trades:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        print(f"{'─'*60}")
        print(f"  EXIT REASONS:")
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            pct = count / report.total_trades * 100
            print(f"    {reason:15s}: {count:4d} ({pct:5.1f}%)")

    print(f"{'='*60}\n")


def save_report_csv(report: BacktestReport, path: str = "data/backtest_report.csv") -> None:
    """Save trade list to CSV for analysis."""
    import csv
    from pathlib import Path

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "symbol", "side", "entry_price", "exit_price", "qty",
            "pnl", "pnl_pct", "exit_reason", "duration_min",
            "opened_at", "closed_at",
        ])
        for t in report.trades:
            writer.writerow([
                t.symbol, t.side, f"{t.entry_price:.6f}", f"{t.exit_price:.6f}",
                f"{t.qty:.6f}", f"{t.pnl:.4f}", f"{t.pnl_pct:.2f}",
                t.exit_reason, f"{t.duration_minutes:.1f}",
                t.opened_at.isoformat(), t.closed_at.isoformat(),
            ])
    print(f"  Trade log saved to: {path}")


# ── CLI entry point ──────────────────────────────────────────────────────────


async def main() -> None:
    """CLI entry point for backtest."""
    import argparse

    parser = argparse.ArgumentParser(description="TraderBot Backtest Engine")
    parser.add_argument(
        "--symbols",
        type=str,
        default="BTC-USDT",
        help="Comma-separated symbols (e.g. BTC-USDT,ETH-USDT,SOL-USDT)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of days to backtest (default: 30)",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=None,
        help="Initial capital in USDT (default: from config)",
    )
    parser.add_argument(
        "--tp-bps",
        type=float,
        default=None,
        help="Override TP in bps (default: from config)",
    )
    parser.add_argument(
        "--sl-bps",
        type=float,
        default=None,
        help="Override SL in bps (default: from config)",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="Save trade log as CSV",
    )
    parser.add_argument(
        "--optimize",
        action="store_true",
        help="Run parameter optimization grid search",
    )
    parser.add_argument(
        "--opt-metric",
        type=str,
        default="score",
        choices=["score", "pnl_pct", "sharpe", "win_rate"],
        help="Metric to optimize for (default: composite score)",
    )

    args = parser.parse_args()

    # Load config
    cfg = Settings()

    # Apply overrides
    if args.tp_bps is not None:
        cfg = cfg.model_copy(update={"tp_bps": args.tp_bps})
    if args.sl_bps is not None:
        cfg = cfg.model_copy(update={"sl_bps": args.sl_bps})

    symbols = [s.strip() for s in args.symbols.split(",")]

    if args.optimize:
        optimizer = ParameterOptimizer(
            base_cfg=cfg,
            symbols=symbols,
            days=args.days,
            initial_capital=args.capital,
            metric=args.opt_metric,
        )
        await optimizer.run()
    else:
        engine = BacktestEngine(
            cfg=cfg,
            symbols=symbols,
            days=args.days,
            initial_capital=args.capital,
        )
        report = await engine.run()
        print_report(report)

        if args.csv:
            save_report_csv(report)


if __name__ == "__main__":
    asyncio.run(main())
