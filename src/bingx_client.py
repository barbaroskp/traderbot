"""BingX Swap V2 REST client.

Features:
- HMAC-SHA256 authentication
- Global + per-endpoint rate limiting
- Exponential backoff with jitter on 429
- Idempotent client_order_id generation
- Full typing
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from src.config import Settings
from src.logger import get_logger

log = get_logger(__name__)


# ── Rate-limiter ────────────────────────────────────────────────────────────

@dataclass
class _Bucket:
    """Token-bucket rate limiter."""
    capacity: int
    refill_per_sec: float
    tokens: float = field(init=False)
    last_refill: float = field(init=False)

    def __post_init__(self) -> None:
        self.tokens = float(self.capacity)
        self.last_refill = time.monotonic()

    async def acquire(self) -> None:
        while True:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
            self.last_refill = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            wait = (1 - self.tokens) / self.refill_per_sec
            await asyncio.sleep(wait)


class RateLimiter:
    """Hierarchical rate limiter: global + per-endpoint."""

    def __init__(self) -> None:
        # BingX default: ~10 req/s global, trade endpoints stricter
        self._global = _Bucket(capacity=10, refill_per_sec=10)
        self._endpoint_buckets: dict[str, _Bucket] = {}
        self._trade_paths = {
            "/openApi/swap/v2/trade/order",
            "/openApi/swap/v2/trade/batchOrders",
            "/openApi/swap/v2/trade/cancelOrder",
            "/openApi/swap/v2/trade/cancelOrders",
        }

    def _get_endpoint_bucket(self, path: str) -> _Bucket:
        if path not in self._endpoint_buckets:
            if path in self._trade_paths:
                self._endpoint_buckets[path] = _Bucket(capacity=5, refill_per_sec=2)
            else:
                self._endpoint_buckets[path] = _Bucket(capacity=10, refill_per_sec=5)
        return self._endpoint_buckets[path]

    async def acquire(self, path: str) -> None:
        await self._global.acquire()
        bucket = self._get_endpoint_bucket(path)
        await bucket.acquire()


# ── Signature ───────────────────────────────────────────────────────────────

def build_query_string(params: dict[str, Any]) -> str:
    """Build sorted query string exactly like BingX official demo.

    Format: sorted key=value pairs joined with &, then &timestamp=... appended.
    NO url-encoding of values.
    """
    sorted_keys = sorted(params.keys())
    return "&".join(f"{k}={params[k]}" for k in sorted_keys)


def build_signature(query_string: str, secret: str) -> str:
    """HMAC-SHA256 signature over the full query string (including timestamp)."""
    return hmac.new(
        secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def generate_client_order_id() -> str:
    """Idempotent, collision-free order ID."""
    return f"bxa_{uuid.uuid4().hex[:20]}"


# ── Client ──────────────────────────────────────────────────────────────────

class BingXClientError(Exception):
    """Raised on non-retriable API errors."""

    def __init__(self, code: int, msg: str, raw: dict[str, Any] | None = None) -> None:
        self.code = code
        self.msg = msg
        self.raw = raw
        super().__init__(f"BingX error {code}: {msg}")


class BingXClient:
    """Async BingX Swap V2 REST client."""

    MAX_RETRIES = 4
    BASE_BACKOFF = 0.5  # seconds

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.base_url = cfg.bingx_base_url.rstrip("/")
        self._api_key = cfg.bingx_api_key
        self._api_secret = cfg.bingx_api_secret
        self._rate_limiter = RateLimiter()
        self._client: httpx.AsyncClient | None = None
        self._rate_limit_hits = 0
        self._total_requests = 0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            # Force IPv4 to avoid IPv6 IP whitelist issues
            transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(30, connect=10),
                headers={"X-BX-APIKEY": self._api_key},
                transport=transport,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ── Core request ────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = True,
    ) -> dict[str, Any]:
        params = dict(params or {})
        if signed:
            # BingX official format: sorted params, then append timestamp, then sign
            qs = build_query_string(params)
            qs += "&timestamp=" + str(int(time.time() * 1000))
            sig = build_signature(qs, self._api_secret)
            full_url = f"{path}?{qs}&signature={sig}"
        else:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            full_url = f"{path}?{qs}" if qs else path

        await self._rate_limiter.acquire(path)
        client = await self._get_client()

        for attempt in range(1, self.MAX_RETRIES + 1):
            self._total_requests += 1
            t0 = time.monotonic()
            try:
                if method.upper() == "GET":
                    resp = await client.get(full_url)
                else:
                    resp = await client.post(full_url, data={})

                latency_ms = (time.monotonic() - t0) * 1000
                log.debug(
                    "api call",
                    extra={
                        "method": method,
                        "path": path,
                        "status": resp.status_code,
                        "latency_ms": round(latency_ms, 1),
                        "attempt": attempt,
                    },
                )

                if resp.status_code == 429:
                    self._rate_limit_hits += 1
                    backoff = self.BASE_BACKOFF * (2 ** (attempt - 1))
                    jitter = backoff * 0.3 * (hash(time.monotonic()) % 100 / 100)
                    wait = backoff + jitter
                    log.warning(
                        "rate limited (429), backing off",
                        extra={"wait_s": round(wait, 2), "attempt": attempt},
                    )
                    await asyncio.sleep(wait)
                    continue

                resp.raise_for_status()
                data = resp.json()

                # BingX wraps responses: {"code": 0, "data": {...}}
                code = data.get("code", 0)
                if code != 0:
                    raise BingXClientError(code, data.get("msg", "unknown"), data)

                return data

            except httpx.HTTPStatusError as exc:
                if exc.response.status_code >= 500 and attempt < self.MAX_RETRIES:
                    backoff = self.BASE_BACKOFF * (2 ** (attempt - 1))
                    log.warning(
                        "server error, retrying",
                        extra={"status": exc.response.status_code, "attempt": attempt},
                    )
                    await asyncio.sleep(backoff)
                    continue
                raise BingXClientError(
                    exc.response.status_code, str(exc), None
                ) from exc
            except httpx.RequestError as exc:
                if attempt < self.MAX_RETRIES:
                    backoff = self.BASE_BACKOFF * (2 ** (attempt - 1))
                    log.warning(
                        "request error, retrying",
                        extra={"error": str(exc), "attempt": attempt},
                    )
                    await asyncio.sleep(backoff)
                    continue
                raise BingXClientError(-1, str(exc), None) from exc

        raise BingXClientError(-1, f"max retries ({self.MAX_RETRIES}) exhausted for {path}")

    # ── Contract Discovery ──────────────────────────────────────

    async def get_contracts(self) -> list[dict[str, Any]]:
        """Get all perpetual swap contracts."""
        data = await self._request("GET", "/openApi/swap/v2/quote/contracts", signed=False)
        return data.get("data", [])

    # ── Market Data ─────────────────────────────────────────────

    async def get_ticker(self, symbol: str) -> dict[str, Any]:
        """24hr ticker for a symbol."""
        data = await self._request(
            "GET", "/openApi/swap/v2/quote/ticker", params={"symbol": symbol}, signed=False
        )
        return data.get("data", {})

    async def get_all_tickers(self) -> list[dict[str, Any]]:
        """24hr tickers for ALL symbols (single API call)."""
        data = await self._request(
            "GET", "/openApi/swap/v2/quote/ticker", params={}, signed=False
        )
        result = data.get("data", [])
        if isinstance(result, dict):
            return [result]
        return result if isinstance(result, list) else []

    async def get_depth(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        """Order book depth."""
        data = await self._request(
            "GET",
            "/openApi/swap/v2/quote/depth",
            params={"symbol": symbol, "limit": limit},
            signed=False,
        )
        return data.get("data", {})

    async def get_open_interest(self, symbol: str) -> dict[str, Any]:
        """Open interest for a symbol."""
        data = await self._request(
            "GET",
            "/openApi/swap/v2/quote/openInterest",
            params={"symbol": symbol},
            signed=False,
        )
        return data.get("data", {})

    async def get_mark_price(self, symbol: str) -> dict[str, Any]:
        """Mark price / premium index."""
        data = await self._request(
            "GET",
            "/openApi/swap/v2/quote/premiumIndex",
            params={"symbol": symbol},
            signed=False,
        )
        return data.get("data", {})

    async def get_klines(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 100,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[dict[str, Any]]:
        """Kline / candlestick data.

        Args:
            start_time: Start time in milliseconds (optional, for historical fetch).
            end_time: End time in milliseconds (optional).
        """
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        data = await self._request(
            "GET",
            "/openApi/swap/v2/quote/klines",
            params=params,
            signed=False,
        )
        return data.get("data", [])

    async def get_recent_trades(self, symbol: str, limit: int = 200) -> list[dict[str, Any]]:
        """Recent public trades for a symbol.

        Note: endpoint support can vary by exchange deployment; callers should handle
        BingXClientError and gracefully fall back.
        """
        data = await self._request(
            "GET",
            "/openApi/swap/v2/quote/trades",
            params={"symbol": symbol, "limit": limit},
            signed=False,
        )
        result = data.get("data", [])
        return result if isinstance(result, list) else []

    # ── Trading ─────────────────────────────────────────────────

    async def place_order(
        self,
        symbol: str,
        side: str,
        position_side: str,
        order_type: str,
        quantity: float,
        price: float | None = None,
        stop_price: float | None = None,
        client_order_id: str | None = None,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        """Place a new order.

        Args:
            side: BUY or SELL (order direction).
            position_side: LONG or SHORT (position direction, required by BingX).
        """
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "positionSide": position_side,
            "type": order_type,
            "quantity": quantity,
        }
        if price is not None:
            params["price"] = price
        if stop_price is not None:
            params["stopPrice"] = stop_price
        if client_order_id:
            params["clientOrderId"] = client_order_id
        if reduce_only:
            params["reduceOnly"] = "true"

        data = await self._request("POST", "/openApi/swap/v2/trade/order", params=params)
        return data.get("data", {})

    async def cancel_order(
        self, symbol: str, order_id: str | None = None, client_order_id: str | None = None
    ) -> dict[str, Any]:
        """Cancel an open order."""
        params: dict[str, Any] = {"symbol": symbol}
        if order_id:
            params["orderId"] = order_id
        if client_order_id:
            params["clientOrderId"] = client_order_id
        data = await self._request("POST", "/openApi/swap/v2/trade/cancelOrder", params=params)
        return data.get("data", {})

    async def get_order(
        self, symbol: str, order_id: str | None = None, client_order_id: str | None = None
    ) -> dict[str, Any]:
        """Query order status."""
        params: dict[str, Any] = {"symbol": symbol}
        if order_id:
            params["orderId"] = order_id
        if client_order_id:
            params["clientOrderId"] = client_order_id
        data = await self._request("GET", "/openApi/swap/v2/trade/order", params=params)
        return data.get("data", {})

    async def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Get all open orders."""
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        data = await self._request("GET", "/openApi/swap/v2/trade/openOrders", params=params)
        return data.get("data", {}).get("orders", [])

    # ── Account ─────────────────────────────────────────────────

    async def get_balance(self) -> dict[str, Any]:
        """Account balance."""
        data = await self._request("GET", "/openApi/swap/v2/user/balance")
        return data.get("data", {})

    async def get_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Current open positions."""
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        data = await self._request("GET", "/openApi/swap/v2/user/positions", params=params)
        return data.get("data", [])

    async def set_leverage(self, symbol: str, side: str, leverage: int) -> dict[str, Any]:
        """Set leverage for a symbol."""
        data = await self._request(
            "POST",
            "/openApi/swap/v2/trade/leverage",
            params={"symbol": symbol, "side": side, "leverage": leverage},
        )
        return data.get("data", {})

    async def set_margin_mode(self, symbol: str, margin_mode: str) -> dict[str, Any]:
        """Set margin mode (ISOLATED / CROSS)."""
        data = await self._request(
            "POST",
            "/openApi/swap/v2/trade/marginType",
            params={"symbol": symbol, "marginType": margin_mode},
        )
        return data.get("data", {})

    # ── Diagnostics ─────────────────────────────────────────────

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "total_requests": self._total_requests,
            "rate_limit_hits": self._rate_limit_hits,
            "api_error_rate": (
                self._rate_limit_hits / max(self._total_requests, 1)
            ),
        }
