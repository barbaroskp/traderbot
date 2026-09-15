"""Download and cache historical klines from BingX public endpoints.

The quote endpoints are unsigned, so no API credentials are needed for research.
Data is cached under research/data/ so repeated experiments are fast and, more
importantly, reproducible: every run of an experiment sees byte-identical input.

Usage:
    python -m research.fetch_history --symbols BTC-USDT,ETH-USDT --days 60
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.bingx_client import BingXClient
from src.config import Settings

CACHE_DIR = Path(__file__).resolve().parent / "data"
INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}


def cache_path(symbol: str, interval: str, days: int) -> Path:
    return CACHE_DIR / f"{symbol}_{interval}_{days}d.json"


def _ts(candle: dict[str, Any]) -> int:
    try:
        return int(candle.get("time", candle.get("t", 0)) or 0)
    except (TypeError, ValueError):
        return 0


async def fetch_symbol(
    client: BingXClient, symbol: str, interval: str, days: int
) -> list[dict[str, Any]]:
    """Page backwards-compatibly through history, de-duplicated and time-sorted."""
    step = INTERVAL_SECONDS[interval]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    out: list[dict[str, Any]] = []
    cursor = start
    chunk = timedelta(seconds=step * 1000)
    empty_streak = 0
    seen_any = False

    # BingX only serves a limited window of intraday history (~30-40 days for
    # 5m). Requesting 90 days lands the cursor in a pre-history region that
    # returns empty batches. Those must NOT be treated as "done" — keep walking
    # forward until real data appears, and only stop on empty once we have some.
    while cursor < end:
        try:
            batch = await client.get_klines(
                symbol, interval=interval, limit=1000,
                start_time=int(cursor.timestamp() * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - research script, keep going
            print(f"    ! {symbol}: {exc}")
            await asyncio.sleep(2.0)
            cursor += chunk
            continue

        if not batch:
            empty_streak += 1
            if seen_any and empty_streak >= 3:
                break                      # reached the end of available data
            if empty_streak > 120:
                break                      # nothing anywhere; give up
            cursor += chunk
            continue

        seen_any = True
        empty_streak = 0
        out.extend(batch)
        newest = max(_ts(k) for k in batch)
        nxt = datetime.fromtimestamp(newest / 1000, timezone.utc) + timedelta(seconds=step)
        cursor = nxt if nxt > cursor else cursor + chunk
        await asyncio.sleep(0.15)

    seen: dict[int, dict[str, Any]] = {}
    for k in out:
        t = _ts(k)
        if t:
            seen[t] = k
    return [seen[t] for t in sorted(seen)]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTC-USDT,ETH-USDT,SOL-USDT")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--force", action="store_true", help="re-download even if cached")
    args = ap.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    client = BingXClient(Settings(_env_file=None, db_path=":memory:", log_file=""))

    for sym in symbols:
        path = cache_path(sym, args.interval, args.days)
        if path.exists() and not args.force:
            n = len(json.loads(path.read_text()))
            print(f"  {sym:14s} cached ({n} candles)")
            continue
        print(f"  {sym:14s} downloading {args.days}d of {args.interval} ...", flush=True)
        candles = await fetch_symbol(client, sym, args.interval, args.days)
        path.write_text(json.dumps(candles))
        if candles:
            span = (_ts(candles[-1]) - _ts(candles[0])) / 86_400_000
            print(f"  {sym:14s} {len(candles)} candles, {span:.1f} days")
        else:
            print(f"  {sym:14s} NO DATA")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
