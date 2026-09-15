"""BIST universe and cached data access.

WHY A SEPARATE MODULE
---------------------
The crypto research harness (research/fetch_history.py) talks to BingX. BIST
needs a different source, a different session structure (a fixed daily open
rather than 24/7 tape) and, critically, a different bias to guard against:
Turkish tickers change names and get delisted, so a list of "today's BIST 100"
applied to two years of history is survivorship bias by construction.

The list below is therefore deliberately WIDE — roughly the liquid Turkish
large/mid-cap tape including names that have since lagged out of the index —
and every study that ranks or filters must do so point-in-time. Symbols that
no longer resolve are simply skipped, which is the honest treatment: they were
tradeable then even if the data is gone now.
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path

import pandas as pd

DATA = Path(__file__).parent / "data"
DATA.mkdir(parents=True, exist_ok=True)

# Liquid BIST names. Wider than BIST-30, roughly BIST-100 scale, plus a few
# that have drifted out of the index over the sample.
TICKERS: list[str] = [
    # banks & holdings
    "GARAN", "AKBNK", "ISCTR", "YKBNK", "VAKBN", "HALKB", "TSKB", "ALBRK",
    "SKBNK", "ICBCT", "KCHOL", "SAHOL", "AGHOL", "ENKAI", "GLYHO", "DOHOL",
    "TKFEN", "ALARK", "GSDHO", "IHLAS",
    # industrials & autos
    "EREGL", "KRDMD", "KRDMA", "TOASO", "FROTO", "ARCLK", "OTKAR", "TTRAK",
    "DOAS", "ASELS", "VESTL", "VESBE", "KARSN", "PARSN", "BFREN", "CEMTS",
    "BRSAN", "BURCE", "IZMDC",
    # energy, chemicals, refining
    "TUPRS", "PETKM", "AKSA", "SASA", "GUBRF", "BAGFS", "HEKTS", "AKSEN",
    "ZOREN", "ODAS", "AYDEM", "ENJSA", "CANTE", "NATEN", "BIOEN", "SMRTG",
    # materials, cement, glass, paper
    "SISE", "TRKCM", "ANACM", "CIMSA", "AKCNS", "OYAKC", "BOLUC", "BUCIM",
    "NUHCM", "KONYA", "GOLTS", "KARTN", "BRISA", "EGEEN",
    # consumer, retail, food
    "BIMAS", "MGROS", "SOKM", "ULKER", "CCOLA", "AEFES", "TATGD", "BANVT",
    "PNSUT", "KERVT", "PETUN", "TUKAS", "KNFRT", "SELEC", "MAVI", "BIZIM",
    "ADESE", "CRFSA",
    # telecom, tech, media
    "TCELL", "TTKOM", "LOGO", "NETAS", "INDES", "ARENA", "DGATE", "ESCOM",
    "KAREL", "ALCTL", "PKART", "LINK", "MIATK", "SMART",
    # transport, tourism, aviation
    "THYAO", "PGSUS", "TAVHL", "CLEBI", "RYSAS", "GSDDE", "MARTI", "AYCES",
    "TEKTU", "UTPYA",
    # REITs & construction
    "EKGYO", "ISGYO", "TRGYO", "HLGYO", "KLGYO", "SNGYO", "AGYO", "OZKGY",
    "YYAPI", "ENKA",
    # healthcare, pharma, misc
    "ECILC", "DEVA", "TRILC", "LKMNH", "MPARK", "RTALB",
    # metals & mining
    "KOZAL", "KOZAA", "IPEKE", "CVKMD", "SARKY", "BRKSN",
    # other liquid names
    "FENER", "GSRAY", "BJKAS", "TSPOR", "AKGRT", "ANHYT", "ANSGR", "TURSG",
    "AKFGY", "VAKKO", "DERIM", "YATAS", "DGKLB",
]


def yf_symbol(t: str) -> str:
    return f"{t}.IS"


def _cache_path(ticker: str, interval: str, period: str) -> Path:
    return DATA / f"{ticker}_{interval}_{period}.pkl"


def load_bars(ticker: str, interval: str = "60m", period: str = "2y",
              refresh: bool = False) -> pd.DataFrame | None:
    """Fetch OHLCV for one ticker, cached on disk.

    Cached because a full universe sweep is ~150 requests and the study gets
    re-run dozens of times; hammering the source on every iteration would both
    be rude and make results non-reproducible if the vendor rate-limits midway
    and silently returns partial data.
    """
    p = _cache_path(ticker, interval, period)
    if p.exists() and not refresh:
        try:
            return pickle.loads(p.read_bytes())
        except Exception:
            pass
    import yfinance as yf
    try:
        df = yf.Ticker(yf_symbol(ticker)).history(period=period, interval=interval)
    except Exception:
        return None
    if df is None or df.empty:
        p.write_bytes(pickle.dumps(None))
        return None
    df = df[df["Open"] > 0].copy()
    p.write_bytes(pickle.dumps(df))
    return df


def load_universe(interval: str = "60m", period: str = "2y",
                  min_bars: int = 300, refresh: bool = False,
                  tickers: list[str] | None = None) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for t in (tickers or TICKERS):
        df = load_bars(t, interval, period, refresh)
        if df is not None and len(df) >= min_bars:
            out[t] = df
    return out


def coverage_report(bars: dict[str, pd.DataFrame]) -> str:
    if not bars:
        return "no data"
    n = len(bars)
    spans = {t: (df.index[0].date(), df.index[-1].date()) for t, df in bars.items()}
    starts = sorted(s for s, _ in spans.values())
    ends = sorted(e for _, e in spans.values())
    return (f"{n} tickers, bars/ticker median "
            f"{int(pd.Series([len(d) for d in bars.values()]).median())}, "
            f"earliest start {starts[0]}, latest start {starts[-1]}, "
            f"earliest end {ends[0]}, latest end {ends[-1]}")


if __name__ == "__main__":
    import sys
    iv = sys.argv[1] if len(sys.argv) > 1 else "60m"
    bars = load_universe(interval=iv)
    print(coverage_report(bars))
    print(f"missing: {sorted(set(TICKERS) - set(bars))}")
