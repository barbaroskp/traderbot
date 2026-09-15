"""Fetch several timeframes for a wide BingX universe.

WHY MULTIPLE TIMEFRAMES IS THE RIGHT QUESTION
----------------------------------------------
The cost of a round trip is roughly fixed — about 16 basis points on liquid
BingX perpetuals (5bp taker each side plus a ~6bp spread). What is NOT fixed is
how far price travels in a bar. So the ratio that decides whether any signal
can pay for itself changes by two orders of magnitude across timeframes:

    1 minute   typical move ~0.1%   cost is ~160% of the move
    1 day      typical move ~3.6%   cost is ~4% of the move

A signal at one minute has to be enormously more accurate than the same signal
at one day just to break even. That is why "which analysis works" cannot be
answered without saying "at what timeframe", and why this fetcher exists.

The vendor caps klines at 1000 bars per request, so history per timeframe is
bounded and shorter timeframes cover less calendar time even with pagination.
That is a real limit on what the shortest frames can prove, and it is reported
rather than papered over.
"""

from __future__ import annotations

import argparse
import pickle
import time
from pathlib import Path

import httpx
import pandas as pd

BASE = "https://open-api.bingx.com/openApi/swap/v3/quote/klines"
DATA = Path("research/crypto/data")

# timeframe -> (pages, minutes per bar). Pages chosen so each frame covers a
# usable span without spending the whole session on the one-minute series.
PLAN = {
    "1m": (6, 1),
    "5m": (6, 5),
    "15m": (5, 15),
    "30m": (5, 30),
    "4h": (3, 240),
    "1d": (2, 1440),
}


def page(symbol: str, interval: str, end: int | None) -> pd.DataFrame | None:
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


def series(symbol: str, interval: str, pages: int) -> pd.DataFrame | None:
    parts, end = [], None
    for _ in range(pages):
        d = page(symbol, interval, end)
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
    # Drop the final bar: it is still forming, and including it makes every
    # indicator repaint. This project shipped that bug once already.
    return out.iloc[:-1].reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=150)
    ap.add_argument("--frames", default="1m,5m,15m,30m,4h,1d")
    a = ap.parse_args()

    meta = pickle.loads((DATA / "meta.pkl").read_bytes())
    meta["quoteVolume"] = pd.to_numeric(meta.quoteVolume, errors="coerce")
    syms = list(meta.sort_values("quoteVolume", ascending=False)
                .symbol.head(a.symbols))

    for tf in a.frames.split(","):
        tf = tf.strip()
        if tf not in PLAN:
            continue
        out_path = DATA / f"tf_{tf}.pkl"
        if out_path.exists():
            print(f"{tf}: cached")
            continue
        pages, _ = PLAN[tf]
        store: dict[str, pd.DataFrame] = {}
        for i, s in enumerate(syms, 1):
            d = series(s, tf, pages)
            if d is not None and len(d) >= 400:
                store[s] = d
            if i % 50 == 0:
                print(f"  {tf} {i}/{len(syms)} · usable {len(store)}", flush=True)
        out_path.write_bytes(pickle.dumps(store))
        if store:
            k = next(iter(store))
            lens = sorted(len(v) for v in store.values())
            span = store[k].t.iloc[-1] - store[k].t.iloc[0]
            print(f"{tf}: {len(store)} symbols · median {lens[len(lens)//2]} bars "
                  f"· span {span.days}d {span.seconds//3600}h", flush=True)


if __name__ == "__main__":
    main()
