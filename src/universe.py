"""Universe discovery – fetch & cache all BingX perpetual contracts.

Refreshes every ``cfg.universe_refresh_hours`` hours.
Stores contract metadata in SQLite for use by selector/strategy.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.bingx_client import BingXClient, BingXClientError
from src.config import Settings
from src.logger import get_logger
from src.storage import Storage

log = get_logger(__name__)


class Universe:
    """Manages the set of known BingX perpetual contracts."""

    def __init__(self, cfg: Settings, client: BingXClient, db: Storage) -> None:
        self.cfg = cfg
        self.client = client
        self.db = db
        self._contracts: dict[str, dict[str, Any]] = {}
        self._last_refresh: datetime | None = None

    @property
    def size(self) -> int:
        return len(self._contracts)

    @property
    def symbols(self) -> list[str]:
        return list(self._contracts.keys())

    @property
    def symbols_by_volume(self) -> list[str]:
        """Return symbols sorted by 24h volume (descending). Most liquid first."""
        return sorted(
            self._contracts.keys(),
            key=lambda s: self._contracts[s].get("volume_24h", 0),
            reverse=True,
        )

    def get_contract(self, symbol: str) -> dict[str, Any] | None:
        return self._contracts.get(symbol)

    async def refresh(self) -> int:
        """Pull all contracts from BingX and persist to SQLite.

        Returns number of active contracts discovered.
        """
        try:
            raw_contracts = await self.client.get_contracts()
        except BingXClientError as exc:
            log.error("universe refresh failed", extra={"error": str(exc)})
            # Fall back to cached data in SQLite
            return self._load_from_db()

        if not raw_contracts:
            log.warning("empty contract list from API, keeping cache")
            return self.size

        # Fetch 24h volume for all symbols in a single API call
        volume_map: dict[str, float] = {}
        try:
            tickers = await self.client.get_all_tickers()
            for t in tickers:
                sym = t.get("symbol", "")
                vol = _safe_float(t.get("quoteVolume", t.get("volume", 0)))
                if sym and vol > 0:
                    volume_map[sym] = vol
        except Exception as exc:
            log.warning("volume fetch failed, continuing without", extra={"error": str(exc)})

        now = datetime.now(timezone.utc).isoformat()
        count = 0
        self._contracts.clear()

        for c in raw_contracts:
            symbol = c.get("symbol", "")
            raw_status = c.get("status", "")
            if not symbol:
                continue

            # BingX returns status as int (1=active) or string ("TRADING")
            status_str = str(raw_status).strip()

            contract = {
                "symbol": symbol,
                "base_asset": c.get("asset", c.get("baseAsset", "")),
                "quote_asset": c.get("currency", c.get("quoteAsset", "USDT")),
                "status": status_str if status_str else "TRADING",
                "tick_size": _safe_float(c.get("tickSize", c.get("pricePrecision"))),
                "step_size": _safe_float(c.get("stepSize", c.get("quantityPrecision"))),
                "min_qty": _safe_float(c.get("minQty", c.get("tradeMinQuantity", 0))),
                "max_leverage": _safe_int(c.get("maxLongLeverage", c.get("maxLeverage", 125))),
                "volume_24h": volume_map.get(symbol, 0.0),
                "updated_at": now,
            }

            self.db.upsert_contract(contract)

            if status_str.upper() in ("TRADING", "1", "ONLINE"):
                self._contracts[symbol] = contract
                count += 1

        self._last_refresh = datetime.now(timezone.utc)
        log.info(
            "universe refreshed",
            extra={"total_raw": len(raw_contracts), "active": count},
        )
        return count

    def _load_from_db(self) -> int:
        """Fallback: load contracts from SQLite cache."""
        rows = self.db.get_contracts()
        self._contracts.clear()
        for r in rows:
            self._contracts[r["symbol"]] = r
        log.info("universe loaded from cache", extra={"count": len(self._contracts)})
        return len(self._contracts)

    def needs_refresh(self) -> bool:
        """True if we've never refreshed or interval elapsed."""
        if self._last_refresh is None:
            return True
        elapsed_hours = (
            datetime.now(timezone.utc) - self._last_refresh
        ).total_seconds() / 3600
        return elapsed_hours >= self.cfg.universe_refresh_hours


def _safe_float(val: Any) -> float:
    """Convert value to float, default 0.0 on failure."""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _safe_int(val: Any) -> int:
    """Convert value to int, default 125 on failure."""
    if val is None:
        return 125
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return 125
