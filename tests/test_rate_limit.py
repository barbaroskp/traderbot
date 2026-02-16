"""Tests for rate limiter and exponential backoff."""

from __future__ import annotations

import asyncio
import time

import pytest

from src.bingx_client import RateLimiter, _Bucket


class TestBucket:
    @pytest.mark.asyncio
    async def test_basic_acquire(self) -> None:
        bucket = _Bucket(capacity=5, refill_per_sec=5.0)
        # Should be able to acquire immediately (bucket starts full)
        for _ in range(5):
            await bucket.acquire()
        # tokens should be ~0 now

    @pytest.mark.asyncio
    async def test_refill(self) -> None:
        bucket = _Bucket(capacity=2, refill_per_sec=100.0)
        # Drain
        await bucket.acquire()
        await bucket.acquire()
        # Wait for refill
        await asyncio.sleep(0.05)  # 50ms → ~5 tokens refilled
        await bucket.acquire()  # Should succeed

    @pytest.mark.asyncio
    async def test_capacity_not_exceeded(self) -> None:
        bucket = _Bucket(capacity=3, refill_per_sec=100.0)
        await asyncio.sleep(0.1)  # way more than needed to refill
        # tokens should be capped at capacity
        assert bucket.tokens <= bucket.capacity + 1  # small tolerance


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_acquire_does_not_error(self) -> None:
        rl = RateLimiter()
        await rl.acquire("/openApi/swap/v2/quote/contracts")

    @pytest.mark.asyncio
    async def test_trade_endpoint_has_stricter_bucket(self) -> None:
        rl = RateLimiter()
        trade_path = "/openApi/swap/v2/trade/order"
        bucket = rl._get_endpoint_bucket(trade_path)
        assert bucket.capacity == 5

    @pytest.mark.asyncio
    async def test_non_trade_endpoint_bucket(self) -> None:
        rl = RateLimiter()
        path = "/openApi/swap/v2/quote/depth"
        bucket = rl._get_endpoint_bucket(path)
        assert bucket.capacity == 10

    @pytest.mark.asyncio
    async def test_throughput_within_limits(self) -> None:
        """Ensure rate limiter doesn't block too aggressively."""
        rl = RateLimiter()
        t0 = time.monotonic()
        for _ in range(5):
            await rl.acquire("/openApi/swap/v2/quote/contracts")
        elapsed = time.monotonic() - t0
        # 5 requests should complete quickly (bucket starts full at 10)
        assert elapsed < 2.0
