"""Download everything a faithful replay of the live bot needs.

WHAT THE BOT CONSUMES EACH CYCLE, AND WHETHER HISTORY EXISTS
-------------------------------------------------------------
From klines — fully replayable:
    RSI, MACD, Bollinger, EMA, ATR, ADX, VWAP, volume ratio, z-score,
    higher-timeframe trend, swing indicators, mid/mark price
From the funding endpoint — replayable:
    funding_rate
From the order book — NOT replayable, no public history anywhere:
    best_bid, best_ask, spread_bps, bid/ask depth, imbalance_ratio,
    whale_bid_usdt, whale_ask_usdt, whale_imbalance
From open interest — NOT replayable:
    open_interest and the liquidation-cascade detector built on it

So five of the strategy's six clusters (oscillator, mean_revert, trend,
volatility, flow) replay exactly. The orderflow cluster cannot, and the
replay feeds it today's MEASURED per-symbol spread as a constant while
leaving imbalance and whale fields neutral — so that cluster abstains
rather than inventing votes it could not have had.

Intervals and depths are taken from the live config so the replay sees
exactly what the scheduler would have fetched:
    kline_interval 5m,  kline_limit 100
    higher_tf 15m,      higher_tf_limit 60
    swing 1h,           swing_kline_limit 100

Usage:
    python -m research.replay.fetch --days 40 --symbols 100
"""

from __future__ import annotations

import argparse
import pickle
import time
from pathlib import Path

import httpx
import pandas as pd

BASE = "https://open-api.bingx.com/openApi/swap/v3/quote/klines"
FUND = "https://open-api.bingx.com/openApi/swap/v2/quote/fundingRate"
DATA = Path("research/replay/data")
DATA.mkdir(parents=True, exist_ok=True)

BARS_PER_DAY = {"5m": 288, "15m": 96, "1h": 24}
WARMUP = {"5m": 120, "15m": 80, "1h": 120}


def _page(symbol: str, interval: str, end: int | None) -> pd.DataFrame | None:
    p = {"symbol": symbol, "interval": interval, "limit": 1000}
    if end:
        p["endTime"] = end
    try:
        r = httpx.get(BASE, params=p, timeout=30).json()
    except Exception:
        return None
    d = r.get("data")
    if not isinstance(d, list) or not d:
        return None
    df = pd.DataFrame(d).rename(columns={"time": "t", "open": "o", "high": "h",
                                         "low": "l", "close": "c", "volume": "v"})
    if "t" not in df:
        return None
    for c in ("o", "h", "l", "c", "v"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["t"] = pd.to_datetime(pd.to_numeric(df["t"], errors="coerce"), unit="ms")
    return df.dropna(subset=["t", "c"]).sort_values("t")


def series(symbol: str, interval: str, need: int) -> pd.DataFrame | None:
    parts, end, pages = [], None, 0
    while sum(len(p) for p in parts) < need and pages < 15:
        d = _page(symbol, interval, end)
        pages += 1
        if d is None or d.empty:
            break
        parts.append(d)
        end = int(d.t.iloc[0].timestamp() * 1000) - 1
        if len(d) < 1000:
            break
        time.sleep(0.04)
    if not parts:
        return None
    out = pd.concat(parts).drop_duplicates("t").sort_values("t")
    # The last bar is still forming; the live path drops it (finalize_klines)
    # and so must the replay, or every indicator repaints.
    return out.iloc[:-1].reset_index(drop=True)


def funding(symbol: str) -> pd.DataFrame | None:
    try:
        r = httpx.get(FUND, params={"symbol": symbol, "limit": 1000},
                      timeout=30).json()
    except Exception:
        return None
    d = r.get("data")
    if not isinstance(d, list) or not d:
        return None
    df = pd.DataFrame(d)
    tcol = "fundingTime" if "fundingTime" in df else "time"
    if tcol not in df or "fundingRate" not in df:
        return None
    df["t"] = pd.to_datetime(pd.to_numeric(df[tcol], errors="coerce"), unit="ms")
    df["rate"] = pd.to_numeric(df.fundingRate, errors="coerce")
    return df[["t", "rate"]].dropna().sort_values("t").reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--symbols", type=int, default=100)
    a = ap.parse_args()

    meta = pickle.loads(
        (Path("research/crypto/data/meta.pkl")).read_bytes())
    meta["quoteVolume"] = pd.to_numeric(meta.quoteVolume, errors="coerce")
    meta["spread_bps"] = pd.to_numeric(meta.spread_bps, errors="coerce")
    meta = meta.sort_values("quoteVolume", ascending=False)
    syms = list(meta.symbol.head(a.symbols))

    # Per-symbol measured spread, standing in for the order book the replay
    # cannot have. Saved alongside the bars so the assumption is auditable.
    (DATA / "spreads.pkl").write_bytes(pickle.dumps(
        meta.set_index("symbol")[["spread_bps", "quoteVolume",
                                  "takerFeeRate", "makerFeeRate"]]))

    for interval in ("5m", "15m", "1h"):
        out = DATA / f"{interval}_{a.days}d.pkl"
        if out.exists():
            print(f"{interval}: cached", flush=True)
            continue
        need = BARS_PER_DAY[interval] * a.days + WARMUP[interval]
        store: dict[str, pd.DataFrame] = {}
        for i, s in enumerate(syms, 1):
            d = series(s, interval, need)
            if d is not None and len(d) >= need * 0.6:
                store[s] = d
            if i % 20 == 0:
                print(f"  {interval} {i}/{len(syms)} · usable {len(store)}",
                      flush=True)
        out.write_bytes(pickle.dumps(store))
        if store:
            k = next(iter(store))
            span = store[k].t.iloc[-1] - store[k].t.iloc[0]
            lens = sorted(len(v) for v in store.values())
            print(f"{interval}: {len(store)} symbols · median {lens[len(lens)//2]} "
                  f"bars · span {span.days}d", flush=True)

    fout = DATA / "funding.pkl"
    if not fout.exists():
        fstore = {}
        for i, s in enumerate(syms, 1):
            f = funding(s)
            if f is not None and len(f) > 10:
                fstore[s] = f
            if i % 40 == 0:
                print(f"  funding {i}/{len(syms)}", flush=True)
            time.sleep(0.03)
        fout.write_bytes(pickle.dumps(fstore))
        print(f"funding: {len(fstore)} symbols", flush=True)


if __name__ == "__main__":
    main()
