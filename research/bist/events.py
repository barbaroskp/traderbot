"""Turn BIST hourly bars into a day-level event table.

DESIGN NOTE — ONE SOURCE, ALWAYS
--------------------------------
An earlier version of this study computed the opening gap from DAILY bars and
the intraday outcome from HOURLY bars. Both series are individually correct,
but the vendor applies dividend/split adjustment differently across intervals,
so on 44.6% of days the two disagreed by more than 1%. The measured "signal"
was partly an artefact of joining two differently-adjusted series on date.

Everything here is therefore derived from a SINGLE hourly frame per ticker:
the gap compares today's first bar open to yesterday's last bar close, and the
outcome is resolved from the same day's bars. A mismatch is not merely unlikely,
it is impossible by construction.

OUTCOME RESOLUTION
------------------
For a ±X% bracket we need to know which side was touched FIRST. Daily OHLC
cannot tell us (both are touched on ~21% of days), and resolving those against
ourselves is not conservatism, it is a fabricated result. Hourly bars narrow
the ambiguity to within a single hour; when both levels fall inside the SAME
hour we still cannot order them, so those cases are counted separately and
reported, rather than silently assigned.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Day:
    """One trading day for one ticker, built only from that day's bars."""
    date: object
    open: float
    high: float
    low: float
    close: float
    volume: float
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray


def to_days(df: pd.DataFrame, min_bars: int = 4) -> list[Day]:
    """Collapse an hourly frame into per-day records, keeping the bar path."""
    out: list[Day] = []
    g = df.groupby(df.index.date)
    for date, d in g:
        if len(d) < min_bars:
            continue
        o = float(d["Open"].values[0])
        if not np.isfinite(o) or o <= 0:
            continue
        out.append(Day(
            date=date, open=o,
            high=float(d["High"].max()), low=float(d["Low"].min()),
            close=float(d["Close"].values[-1]), volume=float(d["Volume"].sum()),
            highs=d["High"].values.astype(float),
            lows=d["Low"].values.astype(float),
            closes=d["Close"].values.astype(float),
        ))
    return out


def bracket(day: Day, tp: float, sl: float, side: str = "LONG"
            ) -> tuple[float, str]:
    """Resolve a ±bracket from the open. Returns (return, how).

    ``how`` is one of "tp", "sl", "close", or "ambiguous" — the last meaning
    both levels were touched inside the same hourly bar so the order is
    unknowable at this resolution. Callers must decide what to do with those
    and must report how many there were.
    """
    o = day.open
    up_lvl = o * (1 + tp) if side == "LONG" else o * (1 + sl)
    dn_lvl = o * (1 - sl) if side == "LONG" else o * (1 - tp)

    for hi, lo in zip(day.highs, day.lows):
        up, dn = hi >= up_lvl, lo <= dn_lvl
        if up and dn:
            return (0.0, "ambiguous")
        if up:
            return (tp if side == "LONG" else -sl, "tp" if side == "LONG" else "sl")
        if dn:
            return (-sl if side == "LONG" else tp, "sl" if side == "LONG" else "tp")

    r = (day.close - o) / o
    return (r if side == "LONG" else -r, "close")


def build(bars: dict[str, pd.DataFrame], lookback: int = 21) -> pd.DataFrame:
    """Build the event table.

    Every feature is computed from bars STRICTLY BEFORE the decision point.
    The decision point is today's open; features may therefore use everything
    up to and including yesterday's close, plus today's open itself (which is
    observable before you trade the rest of the day).
    """
    rows: list[dict] = []
    for tic, df in bars.items():
        days = to_days(df)
        if len(days) < lookback + 10:
            continue
        closes = np.array([d.close for d in days])
        vols = np.array([d.volume for d in days])
        rng = np.array([(d.high - d.low) / d.open for d in days])

        for i in range(lookback, len(days)):
            d = days[i]
            prev = days[i - 1]
            if prev.close <= 0:
                continue
            gap = (d.open - prev.close) / prev.close
            if abs(gap) > 0.30:          # split/dividend residue, not a gap
                continue

            hist_v = vols[i - lookback:i]
            hist_c = closes[i - lookback:i]
            vmean = hist_v.mean() if hist_v.size else 0.0

            row = {
                "tic": tic, "date": d.date, "open": d.open,
                # ── features (information available at the open) ──
                "gap": gap,
                "vol_ratio_prev": prev.volume / vmean if vmean > 0 else 1.0,
                "r1": (closes[i - 1] - closes[i - 2]) / closes[i - 2] if i >= 2 else np.nan,
                "r5": (closes[i - 1] - closes[i - 6]) / closes[i - 6] if i >= 6 else np.nan,
                "r21": (closes[i - 1] - closes[i - 21]) / closes[i - 21] if i >= 21 else np.nan,
                "vola21": pd.Series(hist_c).pct_change().std(),
                "range21": rng[i - lookback:i].mean(),
                "px": prev.close,
                "turnover21": float((hist_v * hist_c).mean()),
                # ── outcomes ──
                "oc": (d.close - d.open) / d.open,           # open→close
                "co_next": np.nan,                            # filled below
                "cc_next": np.nan,
                "cc_next3": np.nan,
            }
            if i + 1 < len(days):
                row["co_next"] = (days[i + 1].open - d.close) / d.close
                row["cc_next"] = (days[i + 1].close - d.close) / d.close
            if i + 3 < len(days):
                row["cc_next3"] = (days[i + 3].close - d.close) / d.close

            for tag, (tp, sl) in {"1_1": (0.01, 0.01), "2_1": (0.02, 0.01),
                                  "1_2": (0.01, 0.02), "2_2": (0.02, 0.02)}.items():
                rl, hl = bracket(d, tp, sl, "LONG")
                rs, hs = bracket(d, tp, sl, "SHORT")
                row[f"L{tag}"], row[f"L{tag}_how"] = rl, hl
                row[f"S{tag}"], row[f"S{tag}_how"] = rs, hs
            rows.append(row)

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values(["date", "tic"]).reset_index(drop=True)
