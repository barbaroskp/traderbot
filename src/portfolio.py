"""Portfolio tracker – positions, PnL, balance management.

Aggregates position data from SQLite and provides summary metrics
consumed by the scheduler, risk manager, and CLI.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.config import Settings
from src.logger import get_logger
from src.storage import Storage

log = get_logger(__name__)


class Portfolio:
    """Portfolio state: positions, PnL, balance."""

    def __init__(self, cfg: Settings, db: Storage) -> None:
        self.cfg = cfg
        self.db = db
        self._paper_balance: float = cfg.initial_capital_usdt

    @property
    def balance(self) -> float:
        return self._paper_balance

    def sync_balance(self, balance: float) -> None:
        """Sync balance from external source (live API) or paper tracking."""
        self._paper_balance = balance

    def get_open_positions(self, is_paper: bool = True) -> list[dict[str, Any]]:
        return self.db.get_open_positions(is_paper=is_paper)

    def get_open_count(self, is_paper: bool = True) -> int:
        return len(self.get_open_positions(is_paper))

    def get_total_notional(self, is_paper: bool = True) -> float:
        positions = self.get_open_positions(is_paper)
        return sum(p.get("notional", 0) for p in positions)

    def get_total_unrealised_pnl(self, is_paper: bool = True) -> float:
        positions = self.get_open_positions(is_paper)
        return sum(p.get("unrealised_pnl", 0) for p in positions)

    def apply_closed_trades(self, closed: list[dict[str, Any]]) -> None:
        """Update paper balance with realised PnL from closed trades."""
        for c in closed:
            pnl = c.get("realised_pnl", 0)
            self._paper_balance += pnl
            log.info(
                "portfolio: trade closed",
                extra={
                    "symbol": c.get("symbol"),
                    "pnl": round(pnl, 4),
                    "balance": round(self._paper_balance, 2),
                },
            )

    def record_daily_pnl(self) -> None:
        """Snapshot daily PnL to pnl_daily table."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        positions = self.get_open_positions()
        unrealised = sum(p.get("unrealised_pnl", 0) for p in positions)

        # Count today's trades
        today_start = f"{today}T00:00:00"
        today_trades = self.db.fetch_all(
            "SELECT * FROM positions WHERE closed_at >= ? AND is_paper=1",
            (today_start,),
        )
        total_trades = len(today_trades)
        winning = sum(1 for t in today_trades if t.get("realised_pnl", 0) > 0)
        realised = sum(t.get("realised_pnl", 0) for t in today_trades)
        total_notional = sum(t.get("notional", 0) for t in today_trades)

        # Upsert daily record
        existing = self.db.fetch_one("SELECT * FROM pnl_daily WHERE date=?", (today,))
        if existing:
            self.db.execute(
                "UPDATE pnl_daily SET realised_pnl=?, unrealised_pnl=?, total_trades=?, "
                "winning_trades=?, total_notional=?, balance_usdt=? WHERE date=?",
                (realised, unrealised, total_trades, winning, total_notional, self._paper_balance, today),
            )
        else:
            self.db.insert(
                "pnl_daily",
                {
                    "date": today,
                    "realised_pnl": realised,
                    "unrealised_pnl": unrealised,
                    "total_trades": total_trades,
                    "winning_trades": winning,
                    "total_notional": total_notional,
                    "balance_usdt": self._paper_balance,
                },
            )

    def get_summary(self) -> dict[str, Any]:
        """Generate portfolio summary for logging / CLI."""
        is_paper = self.cfg.paper_mode
        positions = self.get_open_positions(is_paper)
        total_notional = sum(p.get("notional", 0) for p in positions)
        total_unrealised = sum(p.get("unrealised_pnl", 0) for p in positions)

        # All-time stats
        all_closed = self.db.fetch_all(
            "SELECT * FROM positions WHERE status='CLOSED' AND is_paper=?",
            (1 if is_paper else 0,),
        )
        total_realised = sum(t.get("realised_pnl", 0) for t in all_closed)
        total_trades = len(all_closed)
        winning = sum(1 for t in all_closed if t.get("realised_pnl", 0) > 0)
        win_rate = (winning / total_trades * 100) if total_trades > 0 else 0

        return {
            "mode": "paper" if is_paper else "live",
            "balance": round(self._paper_balance, 2),
            "open_positions": len(positions),
            "total_exposure": round(total_notional, 2),
            "unrealised_pnl": round(total_unrealised, 4),
            "realised_pnl": round(total_realised, 4),
            "total_trades": total_trades,
            "win_rate_pct": round(win_rate, 1),
            "positions": [
                {
                    "symbol": p["symbol"],
                    "side": p["side"],
                    "entry": p["entry_price"],
                    "qty": p["qty"],
                    "notional": round(p.get("notional", 0), 2),
                    "upnl": round(p.get("unrealised_pnl", 0), 4),
                }
                for p in positions
            ],
        }
