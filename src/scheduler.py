"""Main loop orchestrator – runs forever, never crashes.

Cycle (every scan_interval_minutes):
  1. Refresh universe (if needed)
  2. Select tradeable symbols
  3. Evaluate risk state
  4. Generate signals
  5. Execute signals
  6. Check exits (SL/TP/timeout)
  7. Update portfolio & PnL
  8. Log summary

The bot catches ALL exceptions and continues.
"""

from __future__ import annotations

import asyncio
import json
import signal
import traceback
from datetime import datetime, timezone
from typing import Any

from src.bingx_client import BingXClient
from src.config import Settings
from src.execution import ExecutionAdapter, LiveExecution, PaperExecution, _compute_tp_sl_bps
from src.logger import get_logger, setup_logging
from src.marketdata import MarketData, SymbolSnapshot
from src.portfolio import Portfolio
from src.risk import RiskManager
from src.selector import Selector
from src.storage import Storage
from src.strategy import Strategy
from src.universe import Universe

log = get_logger(__name__)


class _NoOpLLMAdvisor:
    """LLM kapaliyken kullanilir; tum sinyalleri oldugu gibi onaylar, llm_advisor.py gerekmez."""

    def reset_cycle(self) -> None:
        pass

    async def advise(self, signal: Any, context: dict[str, Any]) -> Any:
        class _Approve:
            action = "approve"
            reason = "llm disabled"
        return _Approve()


class Scheduler:
    """24/7 autonomous trading loop."""

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self._running = False
        self._cycle_count = 0
        self._soft_kill_cycles_left = 0

        # ── Wire components ─────────────────────────────────────
        self.db = Storage(cfg.db_path)
        self.client = BingXClient(cfg)
        self.universe = Universe(cfg, self.client, self.db)
        self.market = MarketData(cfg, self.client, self.db)
        self.selector = Selector(cfg, self.universe, self.market)
        self.strategy = Strategy(cfg, self.db)
        self.risk = RiskManager(cfg, self.db)
        self.portfolio = Portfolio(cfg, self.db)

        # ── Execution adapter (paper vs live) ───────────────────
        self.execution: ExecutionAdapter
        if cfg.is_live():
            self.execution = LiveExecution(
                cfg, self.client, self.db, self.market, self.risk, self.universe
            )
        else:
            self.execution = PaperExecution(cfg, self.db, self.market, self.risk, self.universe)

        # LLM advisor: sadece aciksa yukle (llm_advisor.py yoksa veya hata varsa NoOp, bot yine calisir)
        if getattr(cfg, "use_llm_advisor", False):
            try:
                from src.llm_advisor import LLMAdvisor
                self.llm_advisor = LLMAdvisor(cfg)
            except Exception as e:
                log.warning("llm_advisor not available, using no-op", extra={"error": str(e)})
                self.llm_advisor = _NoOpLLMAdvisor()
        else:
            self.llm_advisor = _NoOpLLMAdvisor()

    async def start(self) -> None:
        """Start the main loop – runs forever."""
        setup_logging(self.cfg.log_level, self.cfg.log_file)

        mode = "LIVE" if self.cfg.is_live() else "PAPER"
        log.info(
            "scheduler starting",
            extra={
                "mode": mode,
                "capital": self.cfg.initial_capital_usdt,
                "scan_interval_min": self.cfg.scan_interval_minutes,
            },
        )

        # Record run
        self.db.insert(
            "runs",
            {
                "started_at": datetime.now(timezone.utc).isoformat(),
                "mode": mode.lower(),
                "config_json": json.dumps(self.cfg.model_dump(), default=str),
            },
        )

        # Dashboard risk_state_log'dan son durumu okur; restart'ta bellek NORMAL olur ama DB eski kalir.
        # Baslangicta mevcut risk state'i yaz ki dashboard restart sonrasi dogru gostersin.
        self.db.log_risk_state(
            state=self.risk.state_name,
            reason="startup",
            consecutive_losses=self.risk.get_diagnostics().get("consecutive_losses", 0),
            drawdown_pct=self.risk.get_diagnostics().get("drawdown_pct", 0),
        )

        self._running = True

        # ── Signal handlers for graceful shutdown ───────────────
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._shutdown)

        # ── Cleanup orphaned PENDING orders from previous runs ───
        # Orders stuck as PENDING with no matching OPEN position cause
        # cancel_stale_orders to spam the exchange API every cycle.
        self._cleanup_orphaned_orders()

        # ── Initial universe load ───────────────────────────────
        await self._safe_universe_refresh()

        # ── Main loop ───────────────────────────────────────────
        while self._running:
            try:
                await self._run_cycle()
            except Exception as exc:
                log.error(
                    "cycle error (non-fatal)",
                    extra={"error": str(exc), "tb": traceback.format_exc()},
                )
                self.db.log_error(
                    component="scheduler",
                    error_type=type(exc).__name__,
                    message=str(exc),
                    tb=traceback.format_exc(),
                )

            # Adaptive scan: faster when positions are open
            if self.cfg.use_adaptive_scan and self.portfolio.get_open_positions(self.cfg.paper_mode):
                interval = self.cfg.scan_interval_active_minutes
            else:
                interval = self.cfg.scan_interval_minutes
            await asyncio.sleep(interval * 60)

        # ── Cleanup ─────────────────────────────────────────────
        await self.client.close()
        self.db.close()
        log.info("scheduler stopped gracefully")

    def _shutdown(self) -> None:
        log.info("shutdown signal received")
        self._running = False

    async def _run_cycle(self) -> None:
        """Single scan → signal → execute → exit-check cycle."""
        self._cycle_count += 1
        cycle_start = datetime.now(timezone.utc)
        log.info("cycle start", extra={"cycle": self._cycle_count})

        # ── 1. Universe refresh ─────────────────────────────────
        if self.universe.needs_refresh():
            await self._safe_universe_refresh()

        if self.cfg.is_live():
            await self._sync_live_balance()
            if self._cycle_count % max(1, self.cfg.reconcile_interval_cycles) == 0:
                await self._reconcile_live_state()

        # ── 2. Evaluate risk state ──────────────────────────────
        api_stats = self.client.stats
        risk_state = self.risk.evaluate(api_error_rate=api_stats["api_error_rate"])

        # ── 3. Select tradeable symbols ─────────────────────────
        tradeable, filter_stats = await self.selector.select(risk_state=risk_state.value)

        # ── 4. Check existing positions (exits) ─────────────────
        is_paper = self.cfg.paper_mode
        open_positions = self.portfolio.get_open_positions(is_paper)
        closed = await self.execution.check_exits(open_positions)

        if closed:
            self.portfolio.apply_closed_trades(closed)
            for c in closed:
                pnl = c.get("realised_pnl", 0)
                self.risk.record_trade_result(pnl)
                self.strategy.set_cooldown(c.get("symbol", ""), pnl=pnl)

        # ── 5. Generate signals ─────────────────────────────────
        # Refresh open positions after exits
        open_positions = self.portfolio.get_open_positions(is_paper)
        signals = self.strategy.generate_signals(
            snapshots=tradeable,
            open_positions=open_positions,
            risk_state=risk_state.value,
        )

        # Net gorunur log: neden islem acilmadi?
        if tradeable and not signals:
            log.warning(
                "cycle produced no signals",
                extra={
                    "tradeable": len(tradeable),
                    "risk_state": risk_state.value,
                    "hint": "check selection complete (rej_spread/depth/vol) and signal generation (reject_reasons)",
                },
            )

        soft_kill_active = self._update_soft_kill(api_stats)

        # ── Daily loss circuit breaker ────────────────────────────
        if self.risk.daily_kill_active:
            # Kill switch: close ALL open positions immediately
            remaining_open = self.portfolio.get_open_positions(is_paper)
            if remaining_open:
                log.warning(
                    "DAILY LOSS KILL: closing all positions",
                    extra={"positions": len(remaining_open), "daily_pnl": round(self.risk.daily_pnl, 4)},
                )
                kill_closed = await self.execution.check_exits(
                    [{**p, "force_close": True} for p in remaining_open]
                )
                # Force close by setting timeout to 0 won't work, use _close_position directly
                for pos in remaining_open:
                    if pos["status"] == "OPEN":
                        mark = await self.market.fetch_mark_price(pos["symbol"])
                        if mark > 0:
                            c = await self.execution._close_position(pos, mark, "DAILY_KILL")
                            self.portfolio.apply_closed_trades([c])
                            self.risk.record_trade_result(c.get("realised_pnl", 0))

        # ── 5b. LLM advisor (optional) ───────────────────────────
        self.llm_advisor.reset_cycle()
        summary = self.portfolio.get_summary()
        llm_context = {
            "open_positions": len(open_positions),
            "balance": summary["balance"],
            "recent_pnl_summary": f"realised {summary['realised_pnl']:.2f}",
        }

        # ── 5c. Swing signal generation (every N cycles) ──────────
        swing_signals = []
        if self.cfg.swing_enabled and self._cycle_count % self.cfg.swing_scan_every_n_cycles == 0:
            swing_signals = await self._run_swing_cycle(open_positions, risk_state)

        # ── 6. Execute signals (best first by weighted score) ─────
        all_signals = signals + swing_signals
        signals_sorted = sorted(all_signals, key=lambda s: (s.confluence_score, s.weighted_score), reverse=True)
        current_margin = self.portfolio.get_total_margin(is_paper)
        executed = 0

        daily_blocked = not self.risk.can_open_new_trade()
        sharpe_can_trade, sharpe_mult = self.risk.can_trade_by_sharpe()
        entries_blocked = soft_kill_active or daily_blocked or not sharpe_can_trade

        if soft_kill_active:
            log.warning(
                "soft kill-switch active: new entries paused",
                extra={
                    "cycles_left": self._soft_kill_cycles_left,
                    "risk_state": risk_state.value,
                },
            )
        elif daily_blocked:
            log.warning(
                "daily loss circuit breaker: new entries paused",
                extra={
                    "daily_pnl": round(self.risk.daily_pnl, 4),
                    "kill_active": self.risk.daily_kill_active,
                    "stop_active": self.risk.daily_stop_active,
                },
            )
        elif not sharpe_can_trade:
            log.warning(
                "rolling sharpe pause: new entries blocked",
                extra={"sharpe": self.risk.get_rolling_sharpe()},
            )

        if entries_blocked:
            pass  # all blockers logged above
        else:
            for sig in signals_sorted:
                # Find matching snapshot
                snap = next((s for s in tradeable if s.symbol == sig.symbol), None)
                if snap is None:
                    log.warning("signal has no matching snapshot", extra={"symbol": sig.symbol})
                    continue

                # Optional: ask LLM before executing
                advice = await self.llm_advisor.advise(sig, llm_context)
                if advice.action == "reject":
                    log.info("llm rejected signal", extra={"symbol": sig.symbol, "reason": advice.reason})
                    continue

                # ── 1. Determine leverage FIRST (needed for margin-based sizing) ──
                if getattr(sig, "trade_type", "scalp") == "swing":
                    effective_leverage: int | None = self.cfg.swing_leverage
                else:
                    effective_leverage: int | None = self._dynamic_leverage(sig)

                # High-conviction check (affects both leverage and margin)
                is_high_conviction = (
                    sig.confluence_score >= self.cfg.high_conviction_min_confluence
                    and sig.weighted_score >= self.cfg.high_conviction_min_weighted_score
                )
                if is_high_conviction:
                    effective_leverage = max(
                        effective_leverage or 0,
                        self.cfg.leverage_high_conviction,
                    ) or None

                if effective_leverage is not None:
                    effective_leverage = min(effective_leverage, self.cfg.max_leverage_allowed)

                final_leverage = effective_leverage or self.cfg.leverage

                # Faz 4: Drawdown-based leverage scaling
                dd_mult = self.risk.get_drawdown_leverage_mult()
                if dd_mult < 1.0:
                    final_leverage = max(1, int(final_leverage * dd_mult))

                # ── 2. Compute position size (margin-based: margin × leverage = notional) ──
                qty = self.risk.compute_position_size(
                    price=snap.mid_price,
                    leverage=final_leverage,
                    current_total_margin=current_margin,
                    atr=snap.indicators.atr if snap.indicators.valid else 0.0,
                )
                if qty <= 0:
                    log.warning("qty is zero after sizing", extra={"symbol": sig.symbol})
                    continue

                # High-conviction: increase margin allocation (multiplier on margin)
                if is_high_conviction:
                    qty = qty * self.cfg.high_conviction_margin_multiplier
                    # Cap by max margin for high conviction (margin cap, not notional)
                    hc_balance = self.portfolio.balance or self.cfg.initial_capital_usdt
                    max_notional_high = hc_balance * self.cfg.max_margin_high_conviction_pct * final_leverage
                    max_qty_high = max_notional_high / snap.mid_price
                    qty = min(qty, max_qty_high)
                    log.info(
                        "high conviction signal: larger margin + higher leverage",
                        extra={
                            "symbol": sig.symbol,
                            "confluence": sig.confluence_score,
                            "weighted_score": round(sig.weighted_score, 1),
                            "leverage": final_leverage,
                            "margin_multiplier": self.cfg.high_conviction_margin_multiplier,
                            "margin": round(qty * snap.mid_price / final_leverage, 2),
                            "notional": round(qty * snap.mid_price, 2),
                        },
                    )

                # Daily loss reduce: halve position sizes
                daily_mult = self.risk.get_daily_size_multiplier()
                if daily_mult < 1.0:
                    qty = qty * daily_mult

                # Session-aware sizing (dead zone → smaller positions)
                session_mult = getattr(sig, "session_size_mult", 1.0)
                if session_mult < 1.0:
                    qty = qty * session_mult

                # Faz 4: Portfolio heat reduction
                heat_mult = self.risk.get_portfolio_heat_mult(current_margin)
                if heat_mult <= 0:
                    log.warning("portfolio heat max: skipping new entry", extra={"symbol": sig.symbol})
                    continue
                if heat_mult < 1.0:
                    qty = qty * heat_mult

                # Faz 6: Sharpe-based sizing
                if sharpe_mult < 1.0:
                    qty = qty * sharpe_mult

                # Faz 6: Strategy decay reduction
                decay_mult = self.risk.get_decay_size_mult()
                if decay_mult <= 0:
                    log.warning("strategy decay pause: skipping entry", extra={"symbol": sig.symbol})
                    continue
                if decay_mult < 1.0:
                    qty = qty * decay_mult

                # Faz 5: Liquidity-adjusted sizing cap
                if self.cfg.use_liquidity_sizing:
                    side_depth = snap.bid_depth_usdt if sig.side == "LONG" else snap.ask_depth_usdt
                    effective_depth = max(side_depth, self.cfg.liquidity_sizing_depth_floor_usdt)
                    max_notional_liq = effective_depth * (self.cfg.liquidity_sizing_max_pct / 100.0)
                    max_qty_liq = max_notional_liq / snap.mid_price
                    if qty > max_qty_liq:
                        qty = max_qty_liq

                if advice.action == "reduce":
                    qty = qty * 0.5
                    log.info("llm reduced size", extra={"symbol": sig.symbol, "reason": advice.reason})

                # ── Fee-adjusted expectancy guard ──
                # Skip trade if TP after fees doesn't provide positive expectancy
                trade_type = getattr(sig, "trade_type", "scalp")
                sl_bps, tp_bps = _compute_tp_sl_bps(self.cfg, snap, snap.mid_price, trade_type)
                round_trip_fee_bps = 2.0 * self.cfg.fee_rate_bps
                net_tp_bps = tp_bps - round_trip_fee_bps
                net_sl_bps = sl_bps + round_trip_fee_bps
                # Need net_tp / net_sl > 1.0 for positive expectancy (1.5 cok katiydi, cok sinyal engelliyordu)
                if net_tp_bps <= 0 or (net_sl_bps > 0 and net_tp_bps / net_sl_bps < 1.0):
                    log.warning(
                        "fee-adjusted expectancy guard: skipping low-expectancy trade",
                        extra={
                            "symbol": sig.symbol,
                            "tp_bps": round(tp_bps, 1),
                            "sl_bps": round(sl_bps, 1),
                            "net_tp_bps": round(net_tp_bps, 1),
                            "net_sl_bps": round(net_sl_bps, 1),
                            "ratio": round(net_tp_bps / net_sl_bps, 2) if net_sl_bps > 0 else 0,
                        },
                    )
                    continue

                notional = qty * snap.mid_price
                margin = notional / final_leverage
                log.info(
                    "executing signal",
                    extra={
                        "symbol": sig.symbol,
                        "side": sig.side,
                        "z_bps": round(sig.z_score_bps, 1),
                        "confluence": sig.confluence_score,
                        "weighted_score": round(sig.weighted_score, 1),
                        "rsi": round(sig.rsi, 1),
                        "macd_hist": round(sig.macd_histogram, 6),
                        "bb_pct": round(sig.bollinger_pct, 2),
                        "trend": sig.trend_direction,
                        "price": snap.mid_price,
                        "qty": qty,
                        "margin": round(margin, 2),
                        "notional": round(notional, 2),
                        "leverage": final_leverage,
                    },
                )

                result = await self.execution.execute_signal(
                    signal=sig,
                    snap=snap,
                    qty=qty,
                    current_total_margin=current_margin,
                    leverage_override=effective_leverage,
                )
                if result:
                    log.info(
                        "execution result",
                        extra={
                            "symbol": sig.symbol,
                            "status": result.status,
                            "error": result.error,
                            "filled_qty": result.filled_qty,
                        },
                    )
                if result and result.status == "FILLED":
                    executed += 1
                    # Track margin usage: margin = notional / leverage
                    fill_notional = result.avg_fill_price * result.filled_qty
                    current_margin += fill_notional / final_leverage
                    if getattr(sig, "trade_type", "scalp") == "swing":
                        self.strategy.set_swing_cooldown(sig.symbol)
                    else:
                        self.strategy.set_cooldown(sig.symbol)

        # ── 7. Cancel stale orders ──────────────────────────────
        cancelled = await self.execution.cancel_stale_orders(max_age_minutes=30)

        # ── 8. Update portfolio ─────────────────────────────────
        if self._cycle_count % max(1, self.cfg.db_maintenance_interval_cycles) == 0:
            await self._run_db_maintenance()

        self.risk.update_balance(self.portfolio.balance)
        self.portfolio.record_daily_pnl()

        # ── 9. Log cycle summary ────────────────────────────────
        summary = self.portfolio.get_summary()
        cycle_ms = (datetime.now(timezone.utc) - cycle_start).total_seconds() * 1000

        log.info(
            "cycle complete",
            extra={
                "cycle": self._cycle_count,
                "cycle_ms": round(cycle_ms, 1),
                "universe_size": filter_stats.universe_size,
                "tradeable": filter_stats.tradeable,
                "shortlisted": filter_stats.shortlisted,
                "signals_scalp": len(signals),
                "signals_swing": len(swing_signals),
                "executed": executed,
                "exits": len(closed),
                "cancelled_orders": cancelled,
                "open_positions": summary["open_positions"],
                "total_exposure": summary["total_exposure"],
                "balance": summary["balance"],
                "unrealised_pnl": summary["unrealised_pnl"],
                "realised_pnl": summary["realised_pnl"],
                "risk_state": risk_state.value,
                "api_requests": api_stats["total_requests"],
                "rate_limit_hits": api_stats["rate_limit_hits"],
            },
        )

    def _cleanup_orphaned_orders(self) -> None:
        """Mark orphaned PENDING orders as CANCELLED on startup.

        Orders left as PENDING from a previous crash/restart that have no
        matching OPEN position cause cancel_stale_orders to spam the exchange
        API every cycle trying to cancel already-gone orders.
        """
        try:
            is_paper = 1 if self.cfg.paper_mode else 0
            # Get all order IDs referenced by OPEN positions
            open_positions = self.db.fetch_all(
                "SELECT sl_order_id, tp_order_id, tp1_order_id FROM positions WHERE status='OPEN' AND is_paper=?",
                (is_paper,),
            )
            protected_oids: set[str] = set()
            for pos in open_positions:
                for key in ("sl_order_id", "tp_order_id", "tp1_order_id"):
                    oid = pos.get(key, "")
                    if oid:
                        protected_oids.add(oid)

            # Find all PENDING orders
            pending = self.db.fetch_all(
                "SELECT id, client_order_id FROM orders WHERE status='PENDING' AND is_paper=?",
                (is_paper,),
            )

            now = datetime.now(timezone.utc).isoformat()
            orphaned = 0
            for o in pending:
                if o["client_order_id"] not in protected_oids:
                    self.db.execute(
                        "UPDATE orders SET status='CANCELLED', updated_at=? WHERE id=?",
                        (now, o["id"]),
                    )
                    orphaned += 1

            if orphaned:
                log.info(
                    "startup: cleaned orphaned PENDING orders",
                    extra={"orphaned": orphaned, "protected": len(protected_oids)},
                )
        except Exception as exc:
            log.warning("startup: orphaned order cleanup failed", extra={"error": str(exc)})

    async def _safe_universe_refresh(self) -> None:
        """Refresh universe, swallowing errors."""
        try:
            count = await self.universe.refresh()
            log.info("universe refresh ok", extra={"contracts": count})
        except Exception as exc:
            log.error("universe refresh failed", extra={"error": str(exc)})
            self.db.log_error(
                component="universe",
                error_type=type(exc).__name__,
                message=str(exc),
                tb=traceback.format_exc(),
            )

    async def _sync_live_balance(self) -> None:
        """Sync portfolio/risk balance from exchange in live mode."""
        try:
            raw_balance = await self.client.get_balance()
            balance = self._extract_live_balance(raw_balance)
            if balance <= 0:
                log.warning(
                    "live balance sync skipped: non-positive balance",
                    extra={"balance": balance},
                )
                return
            self.portfolio.sync_balance(balance)
            self.risk.update_balance(balance)
            log.debug("live balance synced", extra={"balance": round(balance, 4)})
        except Exception as exc:
            log.warning("live balance sync failed", extra={"error": str(exc)})

    @staticmethod
    def _extract_live_balance(raw: Any) -> float:
        """Extract a usable USDT-equity figure from BingX balance response."""
        priority_keys = (
            "balance",
            "equity",
            "walletBalance",
            "marginBalance",
            "accountEquity",
            "availableBalance",
            "availableMargin",
        )

        def _as_float(v: Any) -> float:
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        if isinstance(raw, dict):
            for key in priority_keys:
                val = _as_float(raw.get(key))
                if val > 0:
                    return val
            nested = raw.get("balance")
            if isinstance(nested, dict):
                for key in priority_keys:
                    val = _as_float(nested.get(key))
                    if val > 0:
                        return val

        if isinstance(raw, list):
            # Prefer explicit USDT row if present
            for row in raw:
                if not isinstance(row, dict):
                    continue
                asset = str(row.get("asset") or row.get("currency") or "").upper()
                if asset == "USDT":
                    for key in priority_keys:
                        val = _as_float(row.get(key))
                        if val > 0:
                            return val
            # Fallback: first positive balance-like field
            for row in raw:
                if not isinstance(row, dict):
                    continue
                for key in priority_keys:
                    val = _as_float(row.get(key))
                    if val > 0:
                        return val

        return 0.0

    def _update_soft_kill(self, api_stats: dict[str, Any]) -> bool:
        """Pause new entries for a few cycles under extreme stress conditions."""
        if not self.cfg.soft_kill_switch_enabled:
            return False

        if self._soft_kill_cycles_left > 0:
            self._soft_kill_cycles_left -= 1
            return True

        diag = self.risk.get_diagnostics()
        drawdown = float(diag.get("drawdown_pct", 0.0))
        balance = float(diag.get("balance", self.portfolio.balance))
        min_balance = self.cfg.initial_capital_usdt * self.cfg.soft_kill_min_balance_ratio

        triggers: list[str] = []
        if api_stats.get("api_error_rate", 0.0) >= self.cfg.soft_kill_api_error_rate:
            triggers.append("api_error_rate")
        if drawdown >= self.cfg.soft_kill_drawdown_pct:
            triggers.append("drawdown")
        if balance <= min_balance:
            triggers.append("low_balance")

        if not triggers:
            return False

        self._soft_kill_cycles_left = max(0, self.cfg.soft_kill_cooldown_cycles - 1)
        log.warning(
            "soft kill-switch triggered",
            extra={
                "triggers": triggers,
                "cooldown_cycles": self.cfg.soft_kill_cooldown_cycles,
                "drawdown_pct": drawdown,
                "balance": round(balance, 4),
                "api_error_rate": round(api_stats.get("api_error_rate", 0.0), 4),
            },
        )
        self.db.log_risk_state(
            state="SOFT_KILL",
            reason=",".join(triggers),
            consecutive_losses=int(diag.get("consecutive_losses", 0)),
            drawdown_pct=drawdown,
            api_error_rate=float(api_stats.get("api_error_rate", 0.0)),
        )
        return True

    async def _reconcile_live_state(self) -> None:
        """Reconcile live exchange positions with local DB open positions."""
        try:
            exch_positions = await self.client.get_positions()
        except Exception as exc:
            log.warning("live reconcile failed to fetch exchange positions", extra={"error": str(exc)})
            return

        db_open = self.db.get_open_positions(is_paper=False)

        exch_map: dict[tuple[str, str], dict[str, float]] = {}

        def _detect_side(position: dict[str, Any]) -> str | None:
            side = str(position.get("positionSide") or "").upper()
            if side in ("LONG", "SHORT"):
                return side
            amt = float(position.get("positionAmt", 0) or 0)
            if amt > 0:
                return "LONG"
            if amt < 0:
                return "SHORT"
            return None

        for p in exch_positions:
            try:
                qty_raw = float(p.get("positionAmt", 0) or 0)
            except (TypeError, ValueError):
                continue
            qty = abs(qty_raw)
            if qty <= 0:
                continue
            symbol = str(p.get("symbol") or "").upper()
            side = _detect_side(p)
            if not symbol or side is None:
                continue
            entry = float(p.get("avgPrice", 0) or 0)
            exch_map[(symbol, side)] = {"qty": qty, "entry": entry}

        db_map: dict[tuple[str, str], dict[str, Any]] = {
            (str(p.get("symbol", "")).upper(), str(p.get("side", "")).upper()): p
            for p in db_open
        }

        reconciled_closed = 0
        reconciled_resized = 0
        reconciled_created = 0

        # 1) DB open but no exchange position -> close stale DB row
        for key, pos in db_map.items():
            exch = exch_map.get(key)
            if exch is None:
                symbol = str(pos.get("symbol", ""))
                side = str(pos.get("side", ""))
                entry = float(pos.get("entry_price", 0) or 0)
                qty = float(pos.get("remaining_qty", pos.get("qty", 0)) or 0)
                mark = await self.market.fetch_mark_price(symbol)
                if mark <= 0:
                    mark = entry
                pnl = (mark - entry) * qty if side == "LONG" else (entry - mark) * qty
                # Deduct fees for consistency with execution layer
                entry_fee = entry * qty * (self.cfg.fee_rate_bps / 10_000)
                exit_fee = mark * qty * (self.cfg.fee_rate_bps / 10_000)
                pnl -= entry_fee + exit_fee
                self.db.execute(
                    "UPDATE positions SET status='CLOSED', realised_pnl=?, closed_at=? WHERE id=?",
                    (pnl, datetime.now(timezone.utc).isoformat(), pos["id"]),
                )
                self.portfolio.apply_closed_trades([{**pos, "realised_pnl": pnl}])
                self.risk.record_trade_result(pnl)
                reconciled_closed += 1
                continue

            db_qty = float(pos.get("remaining_qty", pos.get("qty", 0)) or 0)
            exch_qty = float(exch["qty"])
            if db_qty > 0 and abs(exch_qty - db_qty) / db_qty > 0.05:
                self.db.execute(
                    "UPDATE positions SET qty=?, remaining_qty=?, notional=? WHERE id=?",
                    (exch_qty, exch_qty, exch_qty * float(pos.get("entry_price", 0) or 0), pos["id"]),
                )
                reconciled_resized += 1

        # 2) Exchange position exists but DB row missing -> create placeholder
        for key, exch in exch_map.items():
            if key in db_map:
                continue
            symbol, side = key
            entry = float(exch.get("entry", 0) or 0)
            if entry <= 0:
                entry = await self.market.fetch_mark_price(symbol)
            qty = float(exch["qty"])
            notional = max(entry, 0) * qty
            now_iso = datetime.now(timezone.utc).isoformat()
            self.db.insert(
                "positions",
                {
                    "symbol": symbol,
                    "side": side,
                    "entry_price": entry,
                    "qty": qty,
                    "notional": notional,
                    "sl_order_id": "",
                    "tp1_order_id": "",
                    "tp_order_id": "",
                    "sl_bps": self.cfg.sl_bps,
                    "tp_bps": self.cfg.tp_bps,
                    "original_qty": qty,
                    "remaining_qty": qty,
                    "breakeven_triggered": 0,
                    "partial_tp_filled": 0,
                    "highest_price": entry,
                    "lowest_price": entry,
                    "opened_at": now_iso,
                    "status": "OPEN",
                    "is_paper": 0,
                },
            )
            reconciled_created += 1

        if reconciled_closed or reconciled_resized or reconciled_created:
            log.warning(
                "live reconcile applied",
                extra={
                    "closed": reconciled_closed,
                    "resized": reconciled_resized,
                    "created": reconciled_created,
                },
            )

    async def _run_db_maintenance(self) -> None:
        """Prune old runtime rows and compact WAL/DB periodically."""
        try:
            deleted = self.db.prune_runtime_data(self.cfg.db_retention_days)
            do_vacuum = (
                self._cycle_count % max(1, self.cfg.db_vacuum_interval_cycles) == 0
            )
            self.db.checkpoint_and_vacuum(vacuum=do_vacuum)
            log.info(
                "db maintenance complete",
                extra={
                    "deleted": deleted,
                    "retention_days": self.cfg.db_retention_days,
                    "vacuum": do_vacuum,
                    "db_size_mb": round(self.db.db_size_mb(), 2),
                },
            )
        except Exception as exc:
            log.warning("db maintenance failed", extra={"error": str(exc)})

    async def get_status(self) -> dict[str, Any]:
        """Get current bot status (for CLI / healthcheck)."""
        return {
            "running": self._running,
            "cycle_count": self._cycle_count,
            "mode": "live" if self.cfg.is_live() else "paper",
            "risk": self.risk.get_diagnostics(),
            "portfolio": self.portfolio.get_summary(),
            "universe_size": self.universe.size,
            "api_stats": self.client.stats,
        }

    async def _run_swing_cycle(
        self,
        open_positions: list[dict[str, Any]],
        risk_state: Any,
    ) -> list:
        """Run swing signal generation on 1h klines with 4h trend filter."""
        try:
            # Re-use the selector's shortlist (already filtered by spread/depth/vol)
            shortlisted = self.selector.last_shortlist
            if not shortlisted:
                return []

            # Limit scan to top symbols to control API usage
            scan_symbols = shortlisted[:50]

            swing_snaps = await self.market.batch_snapshots_swing(
                symbols=scan_symbols,
                swing_interval=self.cfg.swing_interval,
                swing_limit=self.cfg.swing_kline_limit,
                trend_interval=self.cfg.swing_trend_interval,
                trend_limit=self.cfg.swing_trend_limit,
                concurrency=3,
            )

            swing_signals = self.strategy.generate_swing_signals(
                snapshots=swing_snaps,
                open_positions=open_positions,
                risk_state=risk_state.value,
            )

            log.info(
                "swing cycle",
                extra={
                    "scanned": len(scan_symbols),
                    "snapshots": len(swing_snaps),
                    "signals": len(swing_signals),
                },
            )
            return swing_signals
        except Exception as exc:
            log.warning("swing cycle error", extra={"error": str(exc)})
            return []

    def _dynamic_leverage(self, sig: Any) -> int | None:
        """Compute leverage tier based on confluence/score."""
        if not getattr(self.cfg, "dynamic_leverage_enabled", False):
            return None

        if (
            sig.confluence_score >= self.cfg.dyn_leverage_tier3_confluence
            and sig.weighted_score >= self.cfg.dyn_leverage_tier3_weighted_score
        ):
            return self.cfg.dyn_leverage_tier3
        if (
            sig.confluence_score >= self.cfg.dyn_leverage_tier2_confluence
            and sig.weighted_score >= self.cfg.dyn_leverage_tier2_weighted_score
        ):
            return self.cfg.dyn_leverage_tier2
        if (
            sig.confluence_score >= self.cfg.dyn_leverage_tier1_confluence
            and sig.weighted_score >= self.cfg.dyn_leverage_tier1_weighted_score
        ):
            return self.cfg.dyn_leverage_tier1
        return None
