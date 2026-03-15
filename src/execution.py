"""Execution layer – Paper & Live adapters.

Both adapters share the same interface so the scheduler is mode-agnostic.

Order flow:
  1. Compute qty & price (marketable limit with slippage guard)
  2. Place entry order
  3. On fill → place SL (stop-market) + TP (limit, reduceOnly)
  4. Manage partial fills: cancel remainder, flatten or manage

No raw market orders – always marketable limit.
"""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.bingx_client import BingXClient, BingXClientError, generate_client_order_id
from src.config import Settings
from src.logger import get_logger
from src.marketdata import MarketData, SymbolSnapshot
from src.risk import RiskManager
from src.storage import Storage
from src.strategy import Signal
from src.universe import Universe

log = get_logger(__name__)


def _compute_tp_sl_bps(
    cfg: Settings, snap: SymbolSnapshot, entry_price: float,
    trade_type: str = "scalp",
) -> tuple[float, float]:
    """Compute SL/TP bps (ATR-based when enabled). Swing uses wider values.
    TP is floored so that after fees we have at least min_tp_net_bps net profit.
    """
    is_swing = trade_type == "swing"
    base_sl = cfg.swing_sl_bps if is_swing else cfg.sl_bps
    base_tp = cfg.swing_tp_bps if is_swing else cfg.tp_bps
    atr_sl_mult = cfg.swing_atr_sl_multiplier if is_swing else cfg.atr_sl_multiplier
    atr_tp_mult = cfg.swing_atr_tp_multiplier if is_swing else cfg.atr_tp_multiplier

    if cfg.use_dynamic_tp_sl and snap.indicators.atr > 0 and entry_price > 0:
        atr_bps = (snap.indicators.atr / entry_price) * 10_000
        sl_bps = max(atr_bps * atr_sl_mult, cfg.min_sl_bps)
        tp_bps = max(atr_bps * atr_tp_mult, cfg.min_tp_bps)
    else:
        sl_bps = base_sl
        tp_bps = base_tp

    # Floor TP so that after round-trip fees we have at least min_tp_net_bps net profit
    round_trip_fee_bps = 2.0 * cfg.fee_rate_bps
    tp_floor = round_trip_fee_bps + getattr(cfg, "min_tp_net_bps", 10.0)
    tp_bps = max(tp_bps, tp_floor)
    return sl_bps, tp_bps


def _estimate_liquidation_price(
    side: str, entry: float, leverage: int, maintenance_margin_pct: float = 0.5
) -> float:
    """Estimate liquidation price for isolated margin.

    Formula: For LONG  → liq = entry * (1 - (1/leverage) + maintenance_margin_pct/100)
             For SHORT → liq = entry * (1 + (1/leverage) - maintenance_margin_pct/100)
    """
    if leverage <= 0 or entry <= 0:
        return 0.0
    margin_ratio = 1.0 / leverage
    maint = maintenance_margin_pct / 100.0
    if side == "LONG":
        return entry * (1.0 - margin_ratio + maint)
    else:
        return entry * (1.0 + margin_ratio - maint)


def _is_near_liquidation(
    side: str, mark: float, liq_price: float, safety_margin_pct: float
) -> bool:
    """Check if mark price is within safety_margin_pct of liquidation price."""
    if liq_price <= 0 or mark <= 0:
        return False
    distance_pct = abs(mark - liq_price) / mark * 100
    return distance_pct <= safety_margin_pct


def _profit_bps(side: str, entry: float, mark: float) -> float:
    if entry <= 0:
        return 0.0
    if side == "LONG":
        return ((mark - entry) / entry) * 10_000
    return ((entry - mark) / entry) * 10_000


@dataclass
class OrderResult:
    """Standardised order result (paper or live)."""
    client_order_id: str
    exchange_order_id: str = ""
    symbol: str = ""
    side: str = ""
    order_type: str = ""
    price: float = 0.0
    qty: float = 0.0
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    status: str = "PENDING"  # PENDING | FILLED | PARTIAL | CANCELLED | REJECTED
    is_paper: bool = True
    error: str = ""


class ExecutionAdapter(abc.ABC):
    """Abstract execution interface."""

    @abc.abstractmethod
    async def execute_signal(
        self,
        signal: Signal,
        snap: SymbolSnapshot,
        qty: float,
        current_total_margin: float,
        leverage_override: int | None = None,
    ) -> OrderResult | None:
        """Execute a signal: entry + SL + TP. leverage_override: use when high-conviction (e.g. 5x)."""

    @abc.abstractmethod
    async def check_exits(self, positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Check if any SL/TP/timeout exits triggered. Returns closed position dicts."""

    @abc.abstractmethod
    async def cancel_stale_orders(self, max_age_minutes: int = 30) -> int:
        """Cancel orders older than max_age. Returns count cancelled."""


# ── Paper Execution ─────────────────────────────────────────────────────────

class PaperExecution(ExecutionAdapter):
    """Simulates order fills using depth data + slippage assumption."""

    def __init__(
        self,
        cfg: Settings,
        db: Storage,
        market: MarketData,
        risk: RiskManager,
        universe: Universe,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.market = market
        self.risk = risk
        self.universe = universe

    async def execute_signal(
        self,
        signal: Signal,
        snap: SymbolSnapshot,
        qty: float,
        current_total_margin: float,
        leverage_override: int | None = None,
    ) -> OrderResult | None:
        """Simulate entry fill + place SL/TP. leverage_override unused in paper (qty already scaled)."""
        if qty <= 0:
            return None

        now = datetime.now(timezone.utc).isoformat()
        client_oid = generate_client_order_id()

        # ── Round qty to contract spec ──────────────────────────
        contract = self.universe.get_contract(signal.symbol)
        step_size = contract.get("step_size", 0.001) if contract else 0.001
        qty = self._round_qty(qty, step_size)
        if qty <= 0:
            return None

        # ── Simulate fill price with dynamic slippage ─────────────
        # Use the larger of: half the actual spread, or the configured assumption.
        # Wide-spread coins get realistic higher slippage; tight coins stay low.
        effective_slippage_bps = max(
            snap.spread_bps * 0.5,
            self.cfg.slippage_assumption_bps,
        ) if snap.spread_bps > 0 else self.cfg.slippage_assumption_bps
        slippage_mult = effective_slippage_bps / 10_000
        if signal.side == "LONG":
            fill_price = snap.best_ask * (1 + slippage_mult)
        else:
            fill_price = snap.best_bid * (1 - slippage_mult)

        # ── Slippage guard: reject if fill too far from mid ─────
        price_deviation_bps = abs(fill_price - snap.mid_price) / snap.mid_price * 10_000
        if price_deviation_bps > self.cfg.max_spread_bps * 2:
            log.warning(
                "paper: slippage guard triggered",
                extra={"symbol": signal.symbol, "deviation_bps": price_deviation_bps},
            )
            return OrderResult(
                client_order_id=client_oid,
                symbol=signal.symbol,
                side=signal.side,
                status="REJECTED",
                error="slippage_guard",
                is_paper=True,
            )

        notional = fill_price * qty

        # ── Persist entry order ─────────────────────────────────
        self.db.insert(
            "orders",
            {
                "client_order_id": client_oid,
                "exchange_order_id": f"paper_{client_oid}",
                "ts": now,
                "symbol": signal.symbol,
                "side": signal.side,
                "order_type": "LIMIT",
                "price": fill_price,
                "qty": qty,
                "status": "FILLED",
                "filled_qty": qty,
                "avg_fill_price": fill_price,
                "is_paper": 1,
                "reduce_only": 0,
                "updated_at": now,
            },
        )

        # ── Persist fill ────────────────────────────────────────
        order_row = self.db.fetch_one(
            "SELECT id FROM orders WHERE client_order_id=?", (client_oid,)
        )
        order_db_id = order_row["id"] if order_row else 0
        self.db.insert(
            "fills",
            {
                "order_id": order_db_id,
                "ts": now,
                "symbol": signal.symbol,
                "side": signal.side,
                "price": fill_price,
                "qty": qty,
                "fee": notional * (self.cfg.fee_rate_bps / 10_000),
                "is_paper": 1,
            },
        )

        # ── Compute SL / TP prices ──────────────────────────────
        sl_bps, tp_bps = _compute_tp_sl_bps(self.cfg, snap, fill_price, getattr(signal, "trade_type", "scalp"))
        if signal.side == "LONG":
            sl_price = fill_price * (1 - sl_bps / 10_000)
            tp_price = fill_price * (1 + tp_bps / 10_000)
        else:
            sl_price = fill_price * (1 + sl_bps / 10_000)
            tp_price = fill_price * (1 - tp_bps / 10_000)

        # Partial TP
        tp1_oid = ""
        tp1_price = 0.0
        tp1_qty = 0.0
        tp2_qty = qty
        if self.cfg.use_partial_tp and 0 < self.cfg.partial_tp_fraction < 1:
            tp1_qty = qty * self.cfg.partial_tp_fraction
            tp2_qty = qty - tp1_qty
            tp1_bps = tp_bps * self.cfg.partial_tp_trigger_pct
            if signal.side == "LONG":
                tp1_price = fill_price * (1 + tp1_bps / 10_000)
            else:
                tp1_price = fill_price * (1 - tp1_bps / 10_000)
            if tp1_qty > 0 and tp2_qty > 0:
                tp1_oid = generate_client_order_id()
            else:
                tp1_qty = 0.0
                tp2_qty = qty

        sl_oid = generate_client_order_id()
        tp_oid = generate_client_order_id()

        # ── Persist SL order ────────────────────────────────────
        self.db.insert(
            "orders",
            {
                "client_order_id": sl_oid,
                "exchange_order_id": f"paper_{sl_oid}",
                "ts": now,
                "symbol": signal.symbol,
                "side": "SHORT" if signal.side == "LONG" else "LONG",
                "order_type": "STOP_MARKET",
                "price": sl_price,
                "qty": qty,
                "status": "PENDING",
                "filled_qty": 0,
                "avg_fill_price": 0,
                "is_paper": 1,
                "parent_order_id": client_oid,
                "reduce_only": 1,
                "updated_at": now,
            },
        )

        # ── Persist TP order(s) ─────────────────────────────────
        if tp1_oid:
            self.db.insert(
                "orders",
                {
                    "client_order_id": tp1_oid,
                    "exchange_order_id": f"paper_{tp1_oid}",
                    "ts": now,
                    "symbol": signal.symbol,
                    "side": "SHORT" if signal.side == "LONG" else "LONG",
                    "order_type": "TAKE_PROFIT",
                    "price": tp1_price,
                    "qty": tp1_qty,
                    "status": "PENDING",
                    "filled_qty": 0,
                    "avg_fill_price": 0,
                    "is_paper": 1,
                    "parent_order_id": client_oid,
                    "reduce_only": 1,
                    "tp_level": 1,
                    "updated_at": now,
                },
            )

        self.db.insert(
            "orders",
            {
                "client_order_id": tp_oid,
                "exchange_order_id": f"paper_{tp_oid}",
                "ts": now,
                "symbol": signal.symbol,
                "side": "SHORT" if signal.side == "LONG" else "LONG",
                "order_type": "TAKE_PROFIT",
                "price": tp_price,
                "qty": tp2_qty,
                "status": "PENDING",
                "filled_qty": 0,
                "avg_fill_price": 0,
                "is_paper": 1,
                "parent_order_id": client_oid,
                "reduce_only": 1,
                "tp_level": 2 if tp1_oid else 1,
                "updated_at": now,
            },
        )

        # ── Open position ──────────────────────────────────────
        self.db.insert(
            "positions",
            {
                "symbol": signal.symbol,
                "side": signal.side,
                "entry_price": fill_price,
                "qty": qty,
                "original_qty": qty,
                "remaining_qty": qty,
                "notional": notional,
                "unrealised_pnl": 0,
                "realised_pnl": 0,
                "sl_order_id": sl_oid,
                "tp1_order_id": tp1_oid,
                "tp_order_id": tp_oid,
                "sl_bps": sl_bps,
                "tp_bps": tp_bps,
                "leverage": leverage_override or self.cfg.leverage,
                "breakeven_triggered": 0,
                "partial_tp_filled": 0,
                "highest_price": fill_price,
                "lowest_price": fill_price,
                "opened_at": now,
                "status": "OPEN",
                "is_paper": 1,
                "trade_type": getattr(signal, "trade_type", "scalp"),
                "max_hold_minutes": (
                    self.cfg.swing_max_hold_minutes if getattr(signal, "trade_type", "scalp") == "swing"
                    else self.cfg.max_hold_minutes
                ),
            },
        )

        log.info(
            "paper: position opened",
            extra={
                "symbol": signal.symbol,
                "side": signal.side,
                "qty": qty,
                "fill_price": fill_price,
                "notional": round(notional, 2),
                "sl": round(sl_price, 6),
                "tp": round(tp_price, 6),
            },
        )

        return OrderResult(
            client_order_id=client_oid,
            symbol=signal.symbol,
            side=signal.side,
            order_type="LIMIT",
            price=fill_price,
            qty=qty,
            filled_qty=qty,
            avg_fill_price=fill_price,
            status="FILLED",
            is_paper=True,
        )

    async def check_exits(self, positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Check SL/TP/timeout against current mark price."""
        closed: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)

        for pos in positions:
            if pos["status"] != "OPEN":
                continue

            symbol = pos["symbol"]
            mark = await self.market.fetch_mark_price(symbol)
            if mark <= 0:
                continue

            entry = pos["entry_price"]
            side = pos["side"]
            qty = float(pos.get("remaining_qty", pos["qty"]))
            if qty <= 0:
                continue
            sl_bps = float(pos.get("sl_bps") or self.cfg.sl_bps)
            tp_bps = float(pos.get("tp_bps") or self.cfg.tp_bps)

            # ── PnL calculation ─────────────────────────────────
            if side == "LONG":
                unrealised_pnl = (mark - entry) * qty
            else:
                unrealised_pnl = (entry - mark) * qty

            # Update unrealised PnL
            self.db.execute(
                "UPDATE positions SET unrealised_pnl=? WHERE id=?",
                (unrealised_pnl, pos["id"]),
            )

            # ── Anti-liquidation check ─────────────────────────
            if self.cfg.anti_liquidation_enabled:
                leverage = int(pos.get("leverage", self.cfg.leverage) or self.cfg.leverage)
                liq_price = _estimate_liquidation_price(side, entry, leverage)
                if liq_price > 0 and _is_near_liquidation(
                    side, mark, liq_price, self.cfg.liquidation_safety_margin_pct
                ):
                    exit_reason = "ANTI_LIQUIDATION"
                    log.warning(
                        "anti-liquidation triggered",
                        extra={
                            "symbol": symbol, "side": side, "mark": mark,
                            "liq_price": round(liq_price, 6), "leverage": leverage,
                        },
                    )

            # Track highs/lows for trailing
            highest = float(pos.get("highest_price") or entry)
            lowest = float(pos.get("lowest_price") or entry)
            if side == "LONG" and mark > highest:
                highest = mark
            if side == "SHORT" and mark < lowest:
                lowest = mark
            self.db.execute(
                "UPDATE positions SET highest_price=?, lowest_price=? WHERE id=?",
                (highest, lowest, pos["id"]),
            )

            profit_bps = _profit_bps(side, entry, mark)

            exit_reason = ""

            # ── Partial TP check ─────────────────────────────────
            if (
                self.cfg.use_partial_tp
                and not pos.get("partial_tp_filled", 0)
                and pos.get("tp1_order_id")
            ):
                tp1_order = self.db.fetch_one(
                    "SELECT * FROM orders WHERE client_order_id=? AND status='PENDING'",
                    (pos.get("tp1_order_id", ""),),
                )
                if tp1_order:
                    tp1_price = tp1_order["price"]
                    hit_tp1 = (side == "LONG" and mark >= tp1_price) or (
                        side == "SHORT" and mark <= tp1_price
                    )
                    if hit_tp1:
                        remaining = self._apply_partial_tp(pos, mark, tp1_order["qty"], tp1_order["client_order_id"])
                        qty = remaining
                        pos["partial_tp_filled"] = 1
                        pos["remaining_qty"] = remaining
                        pos["qty"] = remaining

                        # Move SL to breakeven after partial TP
                        if self.cfg.use_breakeven_stop:
                            if side == "LONG":
                                be_price = entry * (1 + self.cfg.breakeven_buffer_bps / 10_000)
                            else:
                                be_price = entry * (1 - self.cfg.breakeven_buffer_bps / 10_000)
                            self._update_sl_order_price(pos, side, be_price)
                            self.db.execute(
                                "UPDATE positions SET breakeven_triggered=1 WHERE id=?",
                                (pos["id"],),
                            )

            # ── Breakeven check ─────────────────────────────────
            if self.cfg.use_breakeven_stop and not pos.get("breakeven_triggered", 0):
                if profit_bps >= tp_bps * self.cfg.breakeven_activation_pct:
                    if side == "LONG":
                        be_price = entry * (1 + self.cfg.breakeven_buffer_bps / 10_000)
                    else:
                        be_price = entry * (1 - self.cfg.breakeven_buffer_bps / 10_000)
                    self._update_sl_order_price(pos, side, be_price)
                    self.db.execute(
                        "UPDATE positions SET breakeven_triggered=1 WHERE id=?",
                        (pos["id"],),
                    )

            # ── Trailing stop update ────────────────────────────
            if self.cfg.use_trailing_stop and profit_bps >= tp_bps * self.cfg.trailing_activation_pct:
                trail_bps = sl_bps * self.cfg.trailing_distance_pct
                if side == "LONG":
                    new_sl = mark * (1 - trail_bps / 10_000)
                else:
                    new_sl = mark * (1 + trail_bps / 10_000)
                self._update_sl_order_price(pos, side, new_sl)

            # ── NEW: Time-decay SL tightening ────────────────────
            pos_max_hold = int(pos.get("max_hold_minutes") or 0) or (
                self.cfg.swing_max_hold_minutes if pos.get("trade_type") == "swing"
                else self.cfg.max_hold_minutes
            )
            if self.cfg.use_time_decay_sl and not exit_reason:
                opened = datetime.fromisoformat(pos["opened_at"])
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=timezone.utc)
                elapsed_min = (now - opened).total_seconds() / 60
                decay_start = pos_max_hold * self.cfg.time_decay_start_pct
                if elapsed_min > decay_start and profit_bps < 0:
                    # Progressively tighten SL as position ages while in loss
                    decay_progress = min(1.0, (elapsed_min - decay_start) / (pos_max_hold - decay_start))
                    reduction = sl_bps * self.cfg.time_decay_sl_reduction_pct * decay_progress
                    tightened_sl_bps = max(sl_bps * 0.3, sl_bps - reduction)  # never less than 30% of original
                    if side == "LONG":
                        new_sl = entry * (1 - tightened_sl_bps / 10_000)
                    else:
                        new_sl = entry * (1 + tightened_sl_bps / 10_000)
                    self._update_sl_order_price(pos, side, new_sl)

            # ── NEW: Momentum reversal exit ──────────────────────
            if self.cfg.use_momentum_exit and not exit_reason and profit_bps < 0:
                try:
                    snap = await self.market.snapshot_symbol(symbol)
                    if snap and snap.indicators.valid:
                        hist = snap.indicators.macd_histogram
                        prev = snap.indicators.macd_histogram_prev
                        # MACD crossed zero against position direction while in loss
                        if side == "LONG" and hist < 0 and prev >= 0:
                            exit_reason = "MOMENTUM_EXIT"
                        elif side == "SHORT" and hist > 0 and prev <= 0:
                            exit_reason = "MOMENTUM_EXIT"
                except Exception:
                    pass  # don't block exit check on indicator fetch failure

            # ── SL check ────────────────────────────────────────
            sl_order = self.db.fetch_one(
                "SELECT * FROM orders WHERE client_order_id=? AND status='PENDING'",
                (pos.get("sl_order_id", ""),),
            )
            if sl_order:
                sl_price = sl_order["price"]
                if side == "LONG" and mark <= sl_price:
                    exit_reason = "SL"
                elif side == "SHORT" and mark >= sl_price:
                    exit_reason = "SL"

            # ── TP check ────────────────────────────────────────
            if not exit_reason:
                tp_order = self.db.fetch_one(
                    "SELECT * FROM orders WHERE client_order_id=? AND status='PENDING'",
                    (pos.get("tp_order_id", ""),),
                )
                if tp_order:
                    tp_price = tp_order["price"]
                    if side == "LONG" and mark >= tp_price:
                        exit_reason = "TP"
                    elif side == "SHORT" and mark <= tp_price:
                        exit_reason = "TP"

            # ── Timeout check ───────────────────────────────────
            if not exit_reason:
                opened = datetime.fromisoformat(pos["opened_at"])
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=timezone.utc)
                elapsed_min = (now - opened).total_seconds() / 60
                if elapsed_min >= pos_max_hold:
                    exit_reason = "TIMEOUT"

            if exit_reason:
                closed.append(
                    await self._close_position(pos, mark, exit_reason)
                )

        return closed

    async def _close_position(
        self, pos: dict[str, Any], exit_price: float, reason: str
    ) -> dict[str, Any]:
        """Close a paper position."""
        now = datetime.now(timezone.utc).isoformat()
        entry = pos["entry_price"]
        qty = pos["qty"]
        side = pos["side"]

        if side == "LONG":
            pnl = (exit_price - entry) * qty
        else:
            pnl = (entry - exit_price) * qty

        # Fees: entry (on notional at open) + exit (on notional at close)
        entry_fee = entry * qty * (self.cfg.fee_rate_bps / 10_000)
        exit_fee = exit_price * qty * (self.cfg.fee_rate_bps / 10_000)
        pnl -= entry_fee + exit_fee

        # Update position (store exit_reason for analytics)
        self.db.execute(
            "UPDATE positions SET status='CLOSED', realised_pnl=?, closed_at=?, exit_reason=? WHERE id=?",
            (pnl, now, reason, pos["id"]),
        )

        # Mark SL/TP orders as cancelled (the one that didn't trigger)
        for oid_key in ("sl_order_id", "tp_order_id", "tp1_order_id"):
            oid = pos.get(oid_key, "")
            if oid:
                self.db.execute(
                    "UPDATE orders SET status='CANCELLED', updated_at=? "
                    "WHERE client_order_id=? AND status='PENDING'",
                    (now, oid),
                )

        log.info(
            "paper: position closed",
            extra={
                "symbol": pos["symbol"],
                "side": side,
                "reason": reason,
                "entry": entry,
                "exit": exit_price,
                "pnl": round(pnl, 4),
                "qty": qty,
            },
        )

        return {**pos, "realised_pnl": pnl, "exit_price": exit_price, "exit_reason": reason}

    def _apply_partial_tp(
        self,
        pos: dict[str, Any],
        exit_price: float,
        qty_to_close: float,
        tp1_oid: str,
    ) -> float:
        """Apply partial take-profit in paper mode. Returns remaining qty."""
        now = datetime.now(timezone.utc).isoformat()
        entry = pos["entry_price"]
        side = pos["side"]
        remaining = max(0.0, float(pos.get("remaining_qty", pos["qty"])) - qty_to_close)

        if side == "LONG":
            pnl = (exit_price - entry) * qty_to_close
        else:
            pnl = (entry - exit_price) * qty_to_close

        entry_fee = entry * qty_to_close * (self.cfg.fee_rate_bps / 10_000)
        exit_fee = exit_price * qty_to_close * (self.cfg.fee_rate_bps / 10_000)
        pnl -= entry_fee + exit_fee

        self.db.execute(
            "UPDATE positions SET realised_pnl=realised_pnl + ?, qty=?, remaining_qty=?, "
            "partial_tp_filled=1 WHERE id=?",
            (pnl, remaining, remaining, pos["id"]),
        )
        self.db.execute(
            "UPDATE orders SET status='FILLED', filled_qty=?, avg_fill_price=?, updated_at=? "
            "WHERE client_order_id=?",
            (qty_to_close, exit_price, now, tp1_oid),
        )

        log.info(
            "paper: partial tp filled",
            extra={
                "symbol": pos["symbol"],
                "side": side,
                "exit_price": exit_price,
                "qty_closed": qty_to_close,
                "remaining_qty": remaining,
                "pnl": round(pnl, 4),
            },
        )
        return remaining

    def _update_sl_order_price(self, pos: dict[str, Any], side: str, new_price: float) -> None:
        """Update SL order price in paper mode (only if more protective)."""
        sl_oid = pos.get("sl_order_id", "")
        if not sl_oid:
            return
        sl_order = self.db.fetch_one(
            "SELECT * FROM orders WHERE client_order_id=? AND status='PENDING'",
            (sl_oid,),
        )
        if not sl_order:
            return
        current_price = sl_order["price"]
        if side == "LONG" and new_price <= current_price:
            return
        if side == "SHORT" and new_price >= current_price:
            return
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "UPDATE orders SET price=?, updated_at=? WHERE client_order_id=? AND status='PENDING'",
            (new_price, now, sl_oid),
        )

    async def cancel_stale_orders(self, max_age_minutes: int = 30) -> int:
        """Cancel paper orders older than max_age that are still PENDING."""
        now = datetime.now(timezone.utc)
        pending = self.db.fetch_all(
            "SELECT * FROM orders WHERE status='PENDING' AND is_paper=1"
        )
        cancelled = 0
        for o in pending:
            ts = datetime.fromisoformat(o["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if (now - ts).total_seconds() / 60 > max_age_minutes:
                self.db.execute(
                    "UPDATE orders SET status='CANCELLED', updated_at=? WHERE id=?",
                    (now.isoformat(), o["id"]),
                )
                cancelled += 1
        return cancelled

    @staticmethod
    def _round_qty(qty: float, step_size: float) -> float:
        if step_size <= 0:
            return qty
        return round(qty - (qty % step_size), 10)


# ── Live Execution ──────────────────────────────────────────────────────────

class LiveExecution(ExecutionAdapter):
    """Real execution via BingX API.

    Extra safety:
    - Idempotent client_order_id
    - Pre-retry order status check
    - SL/TP placement verification
    - Balance & margin pre-check
    """

    def __init__(
        self,
        cfg: Settings,
        client: BingXClient,
        db: Storage,
        market: MarketData,
        risk: RiskManager,
        universe: Universe,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.db = db
        self.market = market
        self.risk = risk
        self.universe = universe

    async def execute_signal(
        self,
        signal: Signal,
        snap: SymbolSnapshot,
        qty: float,
        current_total_margin: float,
        leverage_override: int | None = None,
    ) -> OrderResult | None:
        """Place a live entry order + SL + TP. leverage_override: e.g. 5 for high-conviction."""
        if qty <= 0:
            return None

        # Guard: live trading must be allowed
        if not self.cfg.is_live():
            log.error("live execution called but live trading disabled!")
            return None

        now = datetime.now(timezone.utc).isoformat()
        client_oid = generate_client_order_id()

        # Round qty
        contract = self.universe.get_contract(signal.symbol)
        step_size = contract.get("step_size", 0.001) if contract else 0.001
        qty = self._round_qty(qty, step_size)
        if qty <= 0:
            return None

        # Set leverage + margin mode (use override when high-conviction signal)
        leverage = leverage_override if leverage_override is not None else self.cfg.leverage
        leverage = min(leverage, self.cfg.max_leverage_allowed)
        try:
            await self.client.set_margin_mode(signal.symbol, self.cfg.margin_mode.value)
            await self.client.set_leverage(
                signal.symbol, "LONG" if signal.side == "LONG" else "SHORT", leverage
            )
        except BingXClientError as exc:
            log.warning("margin/leverage setup error", extra={"error": str(exc)})
            # Non-fatal – may already be set

        # Determine BingX side
        if signal.side == "LONG":
            bingx_side = "BUY"
        else:
            bingx_side = "SELL"

        # Use mid price as reference for DB record
        ref_price = snap.mid_price

        # Live slippage guard: avoid entering when book is suddenly too wide
        try:
            depth = await self.market.fetch_depth(signal.symbol)
        except Exception:
            depth = None

        guard_mid = 0.0
        expected_fill = 0.0
        if depth is not None and depth.mid_price > 0:
            guard_mid = depth.mid_price
            expected_fill = depth.best_ask if signal.side == "LONG" else depth.best_bid
        elif snap.mid_price > 0:
            guard_mid = snap.mid_price
            expected_fill = snap.best_ask if signal.side == "LONG" else snap.best_bid

        if guard_mid > 0 and expected_fill > 0:
            entry_slippage_bps = abs((expected_fill - guard_mid) / guard_mid) * 10_000
            if entry_slippage_bps > self.cfg.live_slippage_guard_bps:
                log.warning(
                    "live: slippage guard blocked entry",
                    extra={
                        "symbol": signal.symbol,
                        "side": signal.side,
                        "mid": guard_mid,
                        "expected_fill": expected_fill,
                        "slippage_bps": round(entry_slippage_bps, 2),
                        "guard_bps": self.cfg.live_slippage_guard_bps,
                    },
                )
                self.db.insert(
                    "orders",
                    {
                        "client_order_id": client_oid,
                        "ts": now,
                        "symbol": signal.symbol,
                        "side": signal.side,
                        "order_type": "MARKET",
                        "price": guard_mid,
                        "qty": qty,
                        "status": "REJECTED",
                        "is_paper": 0,
                        "reduce_only": 0,
                        "updated_at": now,
                    },
                )
                return OrderResult(
                    client_order_id=client_oid,
                    symbol=signal.symbol,
                    side=signal.side,
                    status="REJECTED",
                    error="live_slippage_guard",
                    is_paper=False,
                )

        # ── Place entry order (MARKET for instant fill) ────────
        try:
            result = await self.client.place_order(
                symbol=signal.symbol,
                side=bingx_side,
                position_side=signal.side,  # LONG or SHORT
                order_type="MARKET",
                quantity=qty,
                client_order_id=client_oid,
            )
        except BingXClientError as exc:
            log.error(
                "live: entry order failed",
                extra={"symbol": signal.symbol, "error": str(exc)},
            )
            self.db.insert(
                "orders",
                {
                    "client_order_id": client_oid,
                    "ts": now,
                    "symbol": signal.symbol,
                    "side": signal.side,
                    "order_type": "MARKET",
                    "price": ref_price,
                    "qty": qty,
                    "status": "REJECTED",
                    "is_paper": 0,
                    "reduce_only": 0,
                    "updated_at": now,
                },
            )
            return OrderResult(
                client_order_id=client_oid,
                symbol=signal.symbol,
                side=signal.side,
                status="REJECTED",
                error=str(exc),
                is_paper=False,
            )

        exchange_oid = str(result.get("orderId", "") or result.get("order", {}).get("orderId", "") or "")
        log.info("live: order placed", extra={
            "symbol": signal.symbol, "exchange_oid": exchange_oid,
            "client_oid": client_oid, "raw_keys": list(result.keys()),
        })

        # ── Persist entry order ─────────────────────────────────
        self.db.insert(
            "orders",
            {
                "client_order_id": client_oid,
                "exchange_order_id": exchange_oid,
                "ts": now,
                "symbol": signal.symbol,
                "side": signal.side,
                "order_type": "MARKET",
                "price": ref_price,
                "qty": qty,
                "status": "PENDING",
                "is_paper": 0,
                "reduce_only": 0,
                "updated_at": now,
            },
        )

        # ── Check fill status (MARKET should fill instantly) ───
        filled_qty = 0.0
        avg_price = ref_price
        for _wait in range(3):
            try:
                status_resp = await self.client.get_order(
                    signal.symbol, order_id=exchange_oid, client_order_id=client_oid
                )
                # BingX may nest under "order" key
                order_data = status_resp.get("order", status_resp)
                fill_status = order_data.get("status", "NEW")
                filled_qty = float(order_data.get("executedQty", 0))
                avg_price = float(order_data.get("avgPrice", 0)) or ref_price
                log.info("live: fill check", extra={
                    "symbol": signal.symbol, "attempt": _wait + 1,
                    "status": fill_status, "filled_qty": filled_qty,
                    "avg_price": avg_price, "resp_keys": list(status_resp.keys()),
                })
                if filled_qty > 0:
                    break
            except BingXClientError as exc:
                log.warning("live: fill check error", extra={"error": str(exc)})
            await asyncio.sleep(0.5)

        db_status = "FILLED" if filled_qty > 0 else "PENDING"
        self.db.execute(
            "UPDATE orders SET status=?, filled_qty=?, avg_fill_price=?, updated_at=? "
            "WHERE client_order_id=?",
            (db_status, filled_qty, avg_price, now, client_oid),
        )

        if filled_qty <= 0:
            log.warning("live: market order not filled after retries", extra={"oid": client_oid})
            return OrderResult(
                client_order_id=client_oid,
                exchange_order_id=exchange_oid,
                symbol=signal.symbol,
                side=signal.side,
                status="PENDING",
                is_paper=False,
            )

        # ── Place SL + TP ──────────────────────────────────────
        notional = avg_price * filled_qty
        close_side = "SELL" if signal.side == "LONG" else "BUY"

        sl_bps, tp_bps = _compute_tp_sl_bps(self.cfg, snap, avg_price, getattr(signal, "trade_type", "scalp"))
        if signal.side == "LONG":
            sl_price = avg_price * (1 - sl_bps / 10_000)
            tp_price = avg_price * (1 + tp_bps / 10_000)
        else:
            sl_price = avg_price * (1 + sl_bps / 10_000)
            tp_price = avg_price * (1 - tp_bps / 10_000)

        sl_oid = generate_client_order_id()
        tp_oid = generate_client_order_id()
        tp1_oid = ""
        tp1_price = 0.0
        tp1_qty = 0.0
        tp2_qty = filled_qty
        if self.cfg.use_partial_tp and 0 < self.cfg.partial_tp_fraction < 1:
            tp1_qty = filled_qty * self.cfg.partial_tp_fraction
            tp2_qty = filled_qty - tp1_qty
            tp1_bps = tp_bps * self.cfg.partial_tp_trigger_pct
            if signal.side == "LONG":
                tp1_price = avg_price * (1 + tp1_bps / 10_000)
            else:
                tp1_price = avg_price * (1 - tp1_bps / 10_000)
            if tp1_qty > 0 and tp2_qty > 0:
                tp1_oid = generate_client_order_id()
            else:
                tp1_qty = 0.0
                tp2_qty = filled_qty

        # SL (stop-market) – with verification loop + fallback market close
        close_position_side = signal.side  # same position side for closing
        sl_placed = False
        for sl_attempt in range(3):
            try:
                await self.client.place_order(
                    symbol=signal.symbol,
                    side=close_side,
                    position_side=close_position_side,
                    order_type="STOP_MARKET",
                    quantity=filled_qty,
                    stop_price=sl_price,
                    client_order_id=sl_oid,
                )
                sl_placed = True
                break
            except BingXClientError as exc:
                log.warning(
                    "live: SL placement attempt failed",
                    extra={"attempt": sl_attempt + 1, "error": str(exc)},
                )
                if sl_attempt < 2:
                    await asyncio.sleep(0.5 * (sl_attempt + 1))

        if not sl_placed:
            # CRITICAL: SL failed after retries → emergency market close
            log.error(
                "live: SL placement failed after 3 attempts, emergency closing position",
                extra={"symbol": signal.symbol, "side": signal.side},
            )
            try:
                await self.client.place_order(
                    symbol=signal.symbol,
                    side=close_side,
                    position_side=close_position_side,
                    order_type="MARKET",
                    quantity=filled_qty,
                )
                # Position closed, return as rejected (no SL protection)
                pnl_est = 0.0  # roughly break-even since just opened
                self.db.execute(
                    "UPDATE orders SET status='CANCELLED', updated_at=? WHERE client_order_id=?",
                    (now, client_oid),
                )
                return OrderResult(
                    client_order_id=client_oid,
                    exchange_order_id=exchange_oid,
                    symbol=signal.symbol,
                    side=signal.side,
                    status="REJECTED",
                    error="sl_placement_failed_emergency_close",
                    is_paper=False,
                )
            except BingXClientError as exc2:
                log.error(
                    "live: CRITICAL - emergency close also failed, position UNPROTECTED",
                    extra={"symbol": signal.symbol, "error": str(exc2)},
                )

        # TP1 (partial) – take-profit market
        if tp1_oid:
            try:
                await self.client.place_order(
                    symbol=signal.symbol,
                    side=close_side,
                    position_side=close_position_side,
                    order_type="TAKE_PROFIT_MARKET",
                    quantity=tp1_qty,
                    stop_price=tp1_price,
                    client_order_id=tp1_oid,
                )
            except BingXClientError as exc:
                log.error("live: TP1 placement failed", extra={"error": str(exc)})

        # TP2 (final) – take-profit market
        try:
            await self.client.place_order(
                symbol=signal.symbol,
                side=close_side,
                position_side=close_position_side,
                order_type="TAKE_PROFIT_MARKET",
                quantity=tp2_qty,
                stop_price=tp_price,
                client_order_id=tp_oid,
            )
        except BingXClientError as exc:
            log.error("live: TP placement failed", extra={"error": str(exc)})

        # ── Persist SL/TP orders ───────────────────────────────
        self.db.insert(
            "orders",
            {
                "client_order_id": sl_oid,
                "exchange_order_id": "",
                "ts": now,
                "symbol": signal.symbol,
                "side": "SHORT" if signal.side == "LONG" else "LONG",
                "order_type": "STOP_MARKET",
                "price": sl_price,
                "qty": filled_qty,
                "status": "PENDING",
                "filled_qty": 0,
                "avg_fill_price": 0,
                "is_paper": 0,
                "parent_order_id": client_oid,
                "reduce_only": 1,
                "tp_level": 0,
                "updated_at": now,
            },
        )

        if tp1_oid:
            self.db.insert(
                "orders",
                {
                    "client_order_id": tp1_oid,
                    "exchange_order_id": "",
                    "ts": now,
                    "symbol": signal.symbol,
                    "side": "SHORT" if signal.side == "LONG" else "LONG",
                    "order_type": "TAKE_PROFIT_MARKET",
                    "price": tp1_price,
                    "qty": tp1_qty,
                    "status": "PENDING",
                    "filled_qty": 0,
                    "avg_fill_price": 0,
                    "is_paper": 0,
                    "parent_order_id": client_oid,
                    "reduce_only": 1,
                    "tp_level": 1,
                    "updated_at": now,
                },
            )

        self.db.insert(
            "orders",
            {
                "client_order_id": tp_oid,
                "exchange_order_id": "",
                "ts": now,
                "symbol": signal.symbol,
                "side": "SHORT" if signal.side == "LONG" else "LONG",
                "order_type": "TAKE_PROFIT_MARKET",
                "price": tp_price,
                "qty": tp2_qty,
                "status": "PENDING",
                "filled_qty": 0,
                "avg_fill_price": 0,
                "is_paper": 0,
                "parent_order_id": client_oid,
                "reduce_only": 1,
                "tp_level": 2 if tp1_oid else 1,
                "updated_at": now,
            },
        )

        # ── Open position record ────────────────────────────────
        self.db.insert(
            "positions",
            {
                "symbol": signal.symbol,
                "side": signal.side,
                "entry_price": avg_price,
                "qty": filled_qty,
                "notional": notional,
                "sl_order_id": sl_oid,
                "tp1_order_id": tp1_oid,
                "tp_order_id": tp_oid,
                "sl_bps": sl_bps,
                "tp_bps": tp_bps,
                "original_qty": filled_qty,
                "remaining_qty": filled_qty,
                "leverage": leverage,
                "breakeven_triggered": 0,
                "partial_tp_filled": 0,
                "highest_price": avg_price,
                "lowest_price": avg_price,
                "opened_at": now,
                "status": "OPEN",
                "is_paper": 0,
                "trade_type": getattr(signal, "trade_type", "scalp"),
                "max_hold_minutes": (
                    self.cfg.swing_max_hold_minutes if getattr(signal, "trade_type", "scalp") == "swing"
                    else self.cfg.max_hold_minutes
                ),
            },
        )

        log.info(
            "live: position opened",
            extra={
                "symbol": signal.symbol,
                "side": signal.side,
                "qty": filled_qty,
                "avg_price": avg_price,
                "notional": round(notional, 2),
            },
        )

        return OrderResult(
            client_order_id=client_oid,
            exchange_order_id=exchange_oid,
            symbol=signal.symbol,
            side=signal.side,
            order_type="LIMIT",
            price=avg_price,
            qty=filled_qty,
            filled_qty=filled_qty,
            avg_fill_price=avg_price,
            status="FILLED",
            is_paper=False,
        )

    async def check_exits(self, positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Check live positions for SL/TP fills, partials, trailing, or timeout."""
        closed: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)

        for pos in positions:
            if pos["status"] != "OPEN" or pos.get("is_paper", 1):
                continue

            symbol = pos["symbol"]
            side = pos["side"]
            entry = pos["entry_price"]
            sl_bps = float(pos.get("sl_bps") or self.cfg.sl_bps)
            tp_bps = float(pos.get("tp_bps") or self.cfg.tp_bps)

            # Check exchange position + qty
            try:
                exch_positions = await self.client.get_positions(symbol)
            except BingXClientError:
                continue

            exch_qty = 0.0
            for p in exch_positions:
                amt = float(p.get("positionAmt", 0))
                if amt != 0:
                    pos_side = p.get("positionSide")
                    if pos_side in (side, "BOTH", None, ""):
                        exch_qty = abs(amt)
                        break

            if exch_qty <= 0:
                # Position was closed by SL/TP on exchange
                mark = await self.market.fetch_mark_price(symbol)
                qty = float(pos.get("remaining_qty", pos["qty"]))
                pnl = (mark - entry) * qty if side == "LONG" else (entry - mark) * qty
                entry_fee = entry * qty * (self.cfg.fee_rate_bps / 10_000)
                exit_fee = mark * qty * (self.cfg.fee_rate_bps / 10_000)
                pnl -= entry_fee + exit_fee
                self.db.execute(
                    "UPDATE positions SET status='CLOSED', realised_pnl=?, closed_at=?, exit_reason=? WHERE id=?",
                    (pnl, now.isoformat(), "exchange_close", pos["id"]),
                )
                closed.append({**pos, "realised_pnl": pnl, "exit_reason": "exchange_close"})
                continue

            # Refresh qty if partial filled on exchange
            if abs(exch_qty - float(pos.get("remaining_qty", pos["qty"]))) > 1e-8:
                self.db.execute(
                    "UPDATE positions SET qty=?, remaining_qty=? WHERE id=?",
                    (exch_qty, exch_qty, pos["id"]),
                )

            mark = await self.market.fetch_mark_price(symbol)
            if mark <= 0:
                continue

            # ── Anti-liquidation check (live) ─────────────────
            if self.cfg.anti_liquidation_enabled:
                leverage = int(pos.get("leverage", self.cfg.leverage) or self.cfg.leverage)
                liq_price = _estimate_liquidation_price(side, entry, leverage)
                if liq_price > 0 and _is_near_liquidation(
                    side, mark, liq_price, self.cfg.liquidation_safety_margin_pct
                ):
                    log.warning(
                        "live: anti-liquidation emergency close",
                        extra={
                            "symbol": symbol, "side": side, "mark": mark,
                            "liq_price": round(liq_price, 6), "leverage": leverage,
                        },
                    )
                    try:
                        close_side = "SELL" if side == "LONG" else "BUY"
                        await self.client.place_order(
                            symbol=symbol, side=close_side, position_side=side,
                            order_type="MARKET", quantity=exch_qty,
                        )
                        mark = await self.market.fetch_mark_price(symbol)
                        pnl = (mark - entry) * exch_qty if side == "LONG" else (entry - mark) * exch_qty
                        entry_fee = entry * exch_qty * (self.cfg.fee_rate_bps / 10_000)
                        exit_fee = mark * exch_qty * (self.cfg.fee_rate_bps / 10_000)
                        pnl -= entry_fee + exit_fee
                        self.db.execute(
                            "UPDATE positions SET status='CLOSED', realised_pnl=?, closed_at=?, exit_reason=? WHERE id=?",
                            (pnl, now.isoformat(), "ANTI_LIQUIDATION", pos["id"]),
                        )
                        closed.append({**pos, "realised_pnl": pnl, "exit_reason": "ANTI_LIQUIDATION"})
                        continue
                    except BingXClientError as exc:
                        log.error("live: anti-liquidation close failed", extra={"error": str(exc)})

            profit_bps = _profit_bps(side, entry, mark)

            # Track highs/lows for trailing
            highest = float(pos.get("highest_price") or entry)
            lowest = float(pos.get("lowest_price") or entry)
            if side == "LONG" and mark > highest:
                highest = mark
            if side == "SHORT" and mark < lowest:
                lowest = mark
            self.db.execute(
                "UPDATE positions SET highest_price=?, lowest_price=? WHERE id=?",
                (highest, lowest, pos["id"]),
            )

            # Partial TP check (TP1 filled?)
            if (
                self.cfg.use_partial_tp
                and not pos.get("partial_tp_filled", 0)
                and pos.get("tp1_order_id")
            ):
                try:
                    tp1_resp = await self.client.get_order(
                        symbol, client_order_id=pos.get("tp1_order_id", "")
                    )
                    tp1_data = tp1_resp.get("order", tp1_resp)
                    tp1_status = tp1_data.get("status", "")
                    tp1_filled = float(tp1_data.get("executedQty", 0))
                    if tp1_status in ("FILLED", "PARTIALLY_FILLED") and tp1_filled > 0:
                        self.db.execute(
                            "UPDATE orders SET status='FILLED', filled_qty=?, avg_fill_price=?, updated_at=? "
                            "WHERE client_order_id=?",
                            (tp1_filled, mark, now.isoformat(), pos.get("tp1_order_id", "")),
                        )
                        self.db.execute(
                            "UPDATE positions SET partial_tp_filled=1 WHERE id=?",
                            (pos["id"],),
                        )
                        # Move SL to breakeven on partial fill
                        if self.cfg.use_breakeven_stop:
                            if side == "LONG":
                                be_price = entry * (1 + self.cfg.breakeven_buffer_bps / 10_000)
                            else:
                                be_price = entry * (1 - self.cfg.breakeven_buffer_bps / 10_000)
                            await self._update_live_sl(pos, side, exch_qty, be_price)
                            self.db.execute(
                                "UPDATE positions SET breakeven_triggered=1 WHERE id=?",
                                (pos["id"],),
                            )
                except BingXClientError as exc:
                    log.warning("live: tp1 status check failed", extra={"error": str(exc)})

            # Breakeven update
            if self.cfg.use_breakeven_stop and not pos.get("breakeven_triggered", 0):
                if profit_bps >= tp_bps * self.cfg.breakeven_activation_pct:
                    if side == "LONG":
                        be_price = entry * (1 + self.cfg.breakeven_buffer_bps / 10_000)
                    else:
                        be_price = entry * (1 - self.cfg.breakeven_buffer_bps / 10_000)
                    await self._update_live_sl(pos, side, exch_qty, be_price)
                    self.db.execute(
                        "UPDATE positions SET breakeven_triggered=1 WHERE id=?",
                        (pos["id"],),
                    )

            # Trailing stop update
            if self.cfg.use_trailing_stop and profit_bps >= tp_bps * self.cfg.trailing_activation_pct:
                trail_bps = sl_bps * self.cfg.trailing_distance_pct
                if side == "LONG":
                    new_sl = mark * (1 - trail_bps / 10_000)
                else:
                    new_sl = mark * (1 + trail_bps / 10_000)
                await self._update_live_sl(pos, side, exch_qty, new_sl)

            # Time-decay SL tightening (live)
            live_max_hold = int(pos.get("max_hold_minutes") or 0) or (
                self.cfg.swing_max_hold_minutes if pos.get("trade_type") == "swing"
                else self.cfg.max_hold_minutes
            )
            if self.cfg.use_time_decay_sl:
                opened_td = datetime.fromisoformat(pos["opened_at"])
                if opened_td.tzinfo is None:
                    opened_td = opened_td.replace(tzinfo=timezone.utc)
                elapsed_td = (now - opened_td).total_seconds() / 60
                decay_start = live_max_hold * self.cfg.time_decay_start_pct
                if elapsed_td > decay_start and profit_bps < 0:
                    decay_progress = min(1.0, (elapsed_td - decay_start) / (live_max_hold - decay_start))
                    reduction = sl_bps * self.cfg.time_decay_sl_reduction_pct * decay_progress
                    tightened_sl_bps = max(sl_bps * 0.3, sl_bps - reduction)
                    if side == "LONG":
                        new_sl = entry * (1 - tightened_sl_bps / 10_000)
                    else:
                        new_sl = entry * (1 + tightened_sl_bps / 10_000)
                    await self._update_live_sl(pos, side, exch_qty, new_sl)

            # Momentum reversal exit (live)
            should_momentum_exit = False
            if self.cfg.use_momentum_exit and profit_bps < 0:
                try:
                    snap = await self.market.snapshot_symbol(symbol)
                    if snap and snap.indicators.valid:
                        hist = snap.indicators.macd_histogram
                        prev = snap.indicators.macd_histogram_prev
                        if side == "LONG" and hist < 0 and prev >= 0:
                            should_momentum_exit = True
                        elif side == "SHORT" and hist > 0 and prev <= 0:
                            should_momentum_exit = True
                except Exception:
                    pass

            if should_momentum_exit:
                try:
                    close_side = "SELL" if side == "LONG" else "BUY"
                    await self.client.place_order(
                        symbol=symbol,
                        side=close_side,
                        position_side=side,
                        order_type="MARKET",
                        quantity=exch_qty,
                    )
                    mark = await self.market.fetch_mark_price(symbol)
                    pnl = (mark - entry) * exch_qty if side == "LONG" else (entry - mark) * exch_qty
                    entry_fee = entry * exch_qty * (self.cfg.fee_rate_bps / 10_000)
                    exit_fee = mark * exch_qty * (self.cfg.fee_rate_bps / 10_000)
                    pnl -= entry_fee + exit_fee
                    self.db.execute(
                        "UPDATE positions SET status='CLOSED', realised_pnl=?, closed_at=?, exit_reason=? WHERE id=?",
                        (pnl, now.isoformat(), "MOMENTUM_EXIT", pos["id"]),
                    )
                    closed.append({**pos, "realised_pnl": pnl, "exit_reason": "MOMENTUM_EXIT"})
                    continue
                except BingXClientError as exc:
                    log.error("live: momentum exit failed", extra={"error": str(exc)})

            # Timeout check
            opened = datetime.fromisoformat(pos["opened_at"])
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            if (now - opened).total_seconds() / 60 >= live_max_hold:
                try:
                    close_side = "SELL" if side == "LONG" else "BUY"
                    await self.client.place_order(
                        symbol=symbol,
                        side=close_side,
                        position_side=side,
                        order_type="MARKET",
                        quantity=exch_qty,
                    )
                    mark = await self.market.fetch_mark_price(symbol)
                    pnl = (mark - entry) * exch_qty if side == "LONG" else (entry - mark) * exch_qty
                    entry_fee = entry * exch_qty * (self.cfg.fee_rate_bps / 10_000)
                    exit_fee = mark * exch_qty * (self.cfg.fee_rate_bps / 10_000)
                    pnl -= entry_fee + exit_fee
                    self.db.execute(
                        "UPDATE positions SET status='CLOSED', realised_pnl=?, closed_at=?, exit_reason=? "
                        "WHERE id=?",
                        (pnl, now.isoformat(), "TIMEOUT", pos["id"]),
                    )
                    closed.append({**pos, "realised_pnl": pnl, "exit_reason": "TIMEOUT"})
                except BingXClientError as exc:
                    log.error("live: timeout close failed", extra={"error": str(exc)})

        return closed

    async def _update_live_sl(
        self,
        pos: dict[str, Any],
        side: str,
        qty: float,
        new_price: float,
    ) -> None:
        """Cancel & replace SL order if new price is more protective."""
        sl_oid = pos.get("sl_order_id", "")
        if not sl_oid or qty <= 0:
            return
        sl_order = self.db.fetch_one(
            "SELECT * FROM orders WHERE client_order_id=? AND status='PENDING'",
            (sl_oid,),
        )
        if not sl_order:
            return
        current_price = sl_order["price"]
        if side == "LONG" and new_price <= current_price:
            return
        if side == "SHORT" and new_price >= current_price:
            return

        try:
            await self.client.cancel_order(pos["symbol"], client_order_id=sl_oid)
        except BingXClientError as exc:
            log.warning("live: SL cancel failed", extra={"error": str(exc)})

        new_oid = generate_client_order_id()
        close_side = "SELL" if side == "LONG" else "BUY"
        try:
            await self.client.place_order(
                symbol=pos["symbol"],
                side=close_side,
                position_side=side,
                order_type="STOP_MARKET",
                quantity=qty,
                stop_price=new_price,
                client_order_id=new_oid,
            )
        except BingXClientError as exc:
            log.error("live: SL replace failed", extra={"error": str(exc)})
            return

        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "UPDATE orders SET status='CANCELLED', updated_at=? WHERE client_order_id=?",
            (now, sl_oid),
        )
        self.db.insert(
            "orders",
            {
                "client_order_id": new_oid,
                "exchange_order_id": "",
                "ts": now,
                "symbol": pos["symbol"],
                "side": "SHORT" if side == "LONG" else "LONG",
                "order_type": "STOP_MARKET",
                "price": new_price,
                "qty": qty,
                "status": "PENDING",
                "filled_qty": 0,
                "avg_fill_price": 0,
                "is_paper": 0,
                "parent_order_id": sl_order.get("parent_order_id", ""),
                "reduce_only": 1,
                "tp_level": 0,
                "updated_at": now,
            },
        )
        self.db.execute(
            "UPDATE positions SET sl_order_id=? WHERE id=?",
            (new_oid, pos["id"]),
        )

    async def cancel_stale_orders(self, max_age_minutes: int = 30) -> int:
        """Cancel live orders that are stale.

        FIX: Previously, failed cancel API calls left DB rows as PENDING,
        causing infinite cancel spam on every cycle. Now we mark them as
        CANCELLED in DB regardless of API result (order likely already
        filled/cancelled on exchange). Also cap at 10 cancels per cycle
        to avoid API rate limit issues.
        """
        # Only cancel orders that are NOT linked to an open position's SL/TP
        # (those are managed by check_exits). Cancel orphaned stale orders.
        pending = self.db.fetch_all(
            "SELECT * FROM orders WHERE status='PENDING' AND is_paper=0"
        )
        now = datetime.now(timezone.utc)
        cancelled = 0
        max_cancels_per_cycle = 10  # avoid API rate limit spam

        # Collect SL/TP order IDs for currently open positions so we don't cancel them
        open_positions = self.db.fetch_all(
            "SELECT sl_order_id, tp_order_id, tp1_order_id FROM positions WHERE status='OPEN' AND is_paper=0"
        )
        protected_oids: set[str] = set()
        for pos in open_positions:
            for key in ("sl_order_id", "tp_order_id", "tp1_order_id"):
                oid = pos.get(key, "")
                if oid:
                    protected_oids.add(oid)

        for o in pending:
            if cancelled >= max_cancels_per_cycle:
                break

            # Don't cancel SL/TP orders for open positions
            if o["client_order_id"] in protected_oids:
                continue

            ts = datetime.fromisoformat(o["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if (now - ts).total_seconds() / 60 > max_age_minutes:
                try:
                    await self.client.cancel_order(
                        o["symbol"], client_order_id=o["client_order_id"]
                    )
                except BingXClientError as exc:
                    # Order likely already filled/cancelled on exchange - that's fine
                    log.debug(
                        "stale order cancel failed (likely already gone)",
                        extra={"oid": o["client_order_id"], "error": str(exc)},
                    )
                # Always mark as CANCELLED in DB to prevent infinite retry spam
                self.db.execute(
                    "UPDATE orders SET status='CANCELLED', updated_at=? WHERE id=?",
                    (now.isoformat(), o["id"]),
                )
                cancelled += 1
        return cancelled

    @staticmethod
    def _round_qty(qty: float, step_size: float) -> float:
        if step_size <= 0:
            return qty
        return round(qty - (qty % step_size), 10)
