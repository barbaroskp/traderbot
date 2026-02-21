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
from src.execution import ExecutionAdapter, LiveExecution, PaperExecution
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
                self.risk.record_trade_result(c.get("realised_pnl", 0))
                self.strategy.set_cooldown(c.get("symbol", ""))

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

        # ── 5b. LLM advisor (optional) ───────────────────────────
        self.llm_advisor.reset_cycle()
        summary = self.portfolio.get_summary()
        llm_context = {
            "open_positions": len(open_positions),
            "balance": summary["balance"],
            "recent_pnl_summary": f"realised {summary['realised_pnl']:.2f}",
        }

        # ── 6. Execute signals (best first by weighted score) ─────
        signals_sorted = sorted(signals, key=lambda s: (s.confluence_score, s.weighted_score), reverse=True)
        current_notional = self.portfolio.get_total_notional(is_paper)
        executed = 0

        if soft_kill_active:
            log.warning(
                "soft kill-switch active: new entries paused",
                extra={
                    "cycles_left": self._soft_kill_cycles_left,
                    "risk_state": risk_state.value,
                },
            )
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

                qty = self.risk.compute_position_size(
                    price=snap.mid_price,
                    current_total_notional=current_notional,
                    atr=snap.indicators.atr if snap.indicators.valid else 0.0,
                )
                if qty <= 0:
                    log.warning("qty is zero after sizing", extra={"symbol": sig.symbol})
                    continue

                # Dynamic leverage by confluence/score (optional)
                effective_leverage: int | None = self._dynamic_leverage(sig)

                # High-conviction: 5/5 confluence + high weighted score -> larger position + higher leverage
                is_high_conviction = (
                    sig.confluence_score >= self.cfg.high_conviction_min_confluence
                    and sig.weighted_score >= self.cfg.high_conviction_min_weighted_score
                )
                if is_high_conviction:
                    qty = qty * self.cfg.high_conviction_size_multiplier
                    max_qty_high = self.cfg.max_trade_notional_high_conviction_usdt / snap.mid_price
                    qty = min(qty, max_qty_high)
                    effective_leverage = max(
                        effective_leverage or 0,
                        self.cfg.leverage_high_conviction,
                    ) or None
                    log.info(
                        "high conviction signal: larger size + higher leverage",
                        extra={
                            "symbol": sig.symbol,
                            "confluence": sig.confluence_score,
                            "weighted_score": round(sig.weighted_score, 1),
                            "leverage": effective_leverage or self.cfg.leverage,
                            "size_multiplier": self.cfg.high_conviction_size_multiplier,
                        },
                    )

                if effective_leverage is not None:
                    effective_leverage = min(effective_leverage, self.cfg.max_leverage_allowed)

                if advice.action == "reduce":
                    qty = qty * 0.5
                    log.info("llm reduced size", extra={"symbol": sig.symbol, "reason": advice.reason})

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
                        "notional": round(qty * snap.mid_price, 2),
                        "leverage": effective_leverage or self.cfg.leverage,
                    },
                )

                result = await self.execution.execute_signal(
                    signal=sig,
                    snap=snap,
                    qty=qty,
                    current_total_notional=current_notional,
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
                    current_notional += result.avg_fill_price * result.filled_qty
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
                "signals": len(signals),
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
