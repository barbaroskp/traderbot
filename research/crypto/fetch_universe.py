"""Fetch the full BingX perpetual universe and its daily history.

SURVIVORSHIP WARNING
--------------------
The contract list served today contains the contracts that EXIST today.
Everything delisted — and in crypto that is a long list — is absent. Any
cross-sectional study built on this data is therefore measuring a universe
that was selected, with hindsight, for having survived. Momentum studies are
hit hardest: the names a loser portfolio would have held are exactly the ones
missing.

There is no fix available from a public endpoint. The mitigation is to state
the bias, prefer rules that do not depend on the tails, and treat every level
as optimistic.

Usage:
    python -m research.crypto.fetch_universe            # top 300 by turnover
    python -m research.crypto.fetch_universe --top 600
"""

from __future__ import annotations

import argparse
import pickle
import time
from pathlib import Path

import httpx
import pandas as pd

BASE = "https://open-api.bingx.com"
DATA = Path("research/crypto/data")
DATA.mkdir(parents=True, exist_ok=True)


def contracts() -> pd.DataFrame:
    r = httpx.get(f"{BASE}/openApi/swap/v2/quote/contracts", timeout=30).json()
    df = pd.DataFrame(r.get("data") or [])
    return df[df.get("status", 1) == 1] if "status" in df else df


def tickers() -> pd.DataFrame:
    r = httpx.get(f"{BASE}/openApi/swap/v2/quote/ticker", timeout=30).json()
    df = pd.DataFrame(r.get("data") or [])
    for c in ("lastPrice", "quoteVolume", "bidPrice", "askPrice", "priceChangePercent"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if {"bidPrice", "askPrice", "lastPrice"} <= set(df.columns):
        # The spread is the single most important number for a strategy that
        # rebalances often, and it is the one most backtests invent.
        mid = (df.bidPrice + df.askPrice) / 2
        df["spread_bps"] = (df.askPrice - df.bidPrice) / mid.where(mid > 0) * 1e4
    return df


def klines(symbol: str, interval: str = "1d", limit: int = 1000) -> pd.DataFrame | None:
    try:
        r = httpx.get(f"{BASE}/openApi/swap/v3/quote/klines",
                      params={"symbol": symbol, "interval": interval, "limit": limit},
                      timeout=30).json()
    except Exception:
        return None
    d = r.get("data")
    if not isinstance(d, list) or not d:
        return None
    df = pd.DataFrame(d)
    ren = {"time": "t", "open": "o", "high": "h", "low": "l", "close": "c",
           "volume": "v"}
    df = df.rename(columns={k: v for k, v in ren.items() if k in df})
    if "t" not in df:
        return None
    for c in ("o", "h", "l", "c", "v"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["t"] = pd.to_datetime(pd.to_numeric(df["t"], errors="coerce"), unit="ms")
    df = df.dropna(subset=["t", "c"]).sort_values("t")
    # The most recent candle is still forming. Including it makes every
    # indicator repaint — a bug this project already shipped once.
    return df.iloc[:-1].reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=300)
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()

    cache = DATA / f"daily_top{a.top}.pkl"
    meta_path = DATA / "meta.pkl"

    con, tick = contracts(), tickers()
    meta = con.merge(tick, on="symbol", how="inner") if "symbol" in tick else con
    meta = meta.sort_values("quoteVolume", ascending=False)
    meta_path.write_bytes(pickle.dumps(meta))
    print(f"{len(con)} contracts, {len(tick)} tickers, {len(meta)} joined")

    if cache.exists() and not a.refresh:
        bars = pickle.loads(cache.read_bytes())
        print(f"cached: {len(bars)} symbols")
    else:
        bars, syms = {}, list(meta.symbol.head(a.top))
        for i, s in enumerate(syms, 1):
            df = klines(s)
            if df is not None and len(df) >= 200:
                bars[s] = df
            if i % 25 == 0:
                print(f"  {i}/{len(syms)} fetched, {len(bars)} usable")
            time.sleep(0.08)
        cache.write_bytes(pickle.dumps(bars))

    spans = [(s, len(d), d.t.iloc[0].date(), d.t.iloc[-1].date())
             for s, d in bars.items()]
    lens = sorted(x[1] for x in spans)
    print(f"\n{len(bars)} symbols with >=200 daily bars")
    print(f"  bars: min {lens[0]}, median {lens[len(lens)//2]}, max {lens[-1]}")
    print(f"  earliest start {min(x[2] for x in spans)}")
    if "spread_bps" in meta:
        q = meta.head(a.top).spread_bps.dropna()
        print(f"  spread bps across top {a.top}: median {q.median():.1f}, "
              f"p90 {q.quantile(0.9):.1f}, p99 {q.quantile(0.99):.1f}")


if __name__ == "__main__":
    main()
