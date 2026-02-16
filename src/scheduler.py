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

            # Sleep until next cycle
            await asyncio.sleep(self.cfg.scan_interval_minutes * 60)

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
            )
            if qty <= 0:
                log.warning("qty is zero after sizing", extra={"symbol": sig.symbol})
                continue

            # Dynamic leverage by confluence/score (optional)
            effective_leverage: int | None = self._dynamic_leverage(sig)

            # High-conviction: 5/5 confluence + high weighted score → larger position + higher leverage
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
