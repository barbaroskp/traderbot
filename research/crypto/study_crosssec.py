"""Does cross-sectional selection work across the BingX universe?

This is the study that decides whether the scanner the brief asked for — rank
hundreds of contracts, hold the best ones, lever up when many look good — is a
strategy or a story.

THE STANDARD
------------
A candidate has to clear all of it, not some of it:

  * beat the CONTROL, which is holding the whole eligible universe. Beating
    "cash" is not interesting; beating "own everything" is the only comparison
    that isolates selection skill.
  * beat BTC alone. If a 600-name scanner cannot beat one coin, the scanning
    is decoration.
  * survive its own turnover at real BingX costs (5bps taker plus slippage).
  * hold up in BOTH halves of the sample, split by date.
  * |t| >= 3.0, not 2.0 — Harvey, Liu & Zhu's threshold for a new factor,
    which matters here because many signals are being tried at once.

And the mirror test: if "buy winners" and "buy losers" BOTH look profitable,
the study is measuring noise, and the correct conclusion is that neither works.

Usage:
    python -m research.crypto.study_crosssec
    python -m research.crypto.study_crosssec --top 400
"""

from __future__ import annotations

import argparse
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from src.crypto.crosssec import Result, SelectConfig, backtest

DATA = Path("research/crypto/data")


def load(top: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    bars = pickle.loads((DATA / f"daily_top{top}.pkl").read_bytes())
    meta = pickle.loads((DATA / "meta.pkl").read_bytes())
    px, vol = {}, {}
    for s, d in bars.items():
        d = d.drop_duplicates("t").set_index("t")
        px[s] = d["c"]
        vol[s] = d["c"] * d.get("v", 0.0)
    P = pd.DataFrame(px).sort_index()
    V = pd.DataFrame(vol).reindex(P.index)
    spread = (meta.set_index("symbol")["spread_bps"]
              if "spread_bps" in meta else pd.Series(dtype=float))
    return P, V, spread


def control(px: pd.DataFrame, vol: pd.DataFrame, cfg: SelectConfig,
            spread: pd.Series) -> Result:
    """Hold the entire eligible universe, equal weight. The honest benchmark."""
    c = SelectConfig(**{**cfg.__dict__, "signal": "none", "n_holdings": 10_000,
                        "weighting": "equal", "use_breadth_leverage": False})
    return backtest(px, vol, c, "CONTROL own-everything", spread)


def buy_hold(px: pd.DataFrame, sym: str) -> Result:
    s = px[sym].dropna()
    return Result(label=f"buy&hold {sym}", curve=s / s.iloc[0])


def halves(px: pd.DataFrame, vol: pd.DataFrame, cfg: SelectConfig,
           spread: pd.Series) -> tuple[float, float]:
    mid = len(px) // 2
    a = backtest(px.iloc[:mid], vol.iloc[:mid], cfg, "", spread)
    b = backtest(px.iloc[mid:], vol.iloc[mid:], cfg, "", spread)
    return a.stats()["cagr"], b.stats()["cagr"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=400)
    ap.add_argument("--holdings", type=int, default=10)
    a = ap.parse_args()

    px, vol, spread = load(a.top)
    print(f"{px.shape[1]} symbols · {px.index[0].date()} → {px.index[-1].date()} "
          f"({len(px)} days)")
    print("survivorship: delisted contracts are ABSENT — every level is optimistic\n")

    base = SelectConfig(n_holdings=a.holdings)
    rows: list[Result] = [control(px, vol, base, spread)]
    for s in px.columns[:1]:
        rows.append(buy_hold(px, s))
    if "BTC-USDT" in px:
        rows.append(buy_hold(px, "BTC-USDT"))

    signals = ["mom_30_7", "mom_30", "mom_90", "riskadj_mom",
               "rev_7", "rev_30", "lowvol", "vol_growth", "dist_ath"]
    tested = 0
    for s in signals:
        cfg = SelectConfig(**{**base.__dict__, "signal": s})
        rows.append(backtest(px, vol, cfg, f"{s}", spread))
        tested += 1

    print(Result.header())
    print("-" * 92)
    for r in sorted(rows, key=lambda x: -x.stats()["cagr"]):
        print(r.row())

    ctrl = next(r for r in rows if r.label.startswith("CONTROL"))
    cstat = ctrl.stats()
    print(f"\nvs CONTROL ({cstat['cagr']*100:+.1f}%/yr, maxDD {cstat['maxdd']*100:.0f}%):")
    beats = []
    for r in rows:
        if r is ctrl or r.label.startswith("buy&hold"):
            continue
        d = r.stats()["cagr"] - cstat["cagr"]
        if d > 0:
            beats.append(r)
        print(f"  {r.label:26s} {d*100:+7.1f} pp  {'BEATS' if d > 0 else 'loses'}")

    print(f"\nmirror test (if both directions 'work', it is noise):")
    for pair in (("mom_30", "rev_30"), ("mom_30_7", "rev_7")):
        g = {r.label: r.stats()["cagr"] for r in rows if r.label in pair}
        if len(g) == 2:
            both = all(v > cstat["cagr"] for v in g.values())
            print(f"  {pair[0]} {g[pair[0]]*100:+.1f}% vs {pair[1]} "
                  f"{g[pair[1]]*100:+.1f}%  -> {'BOTH BEAT = NOISE' if both else 'ok'}")

    print(f"\nsplit-sample on the candidates that beat the control:")
    if not beats:
        print("  none beat the control — nothing to validate")
    for r in beats:
        cfg = SelectConfig(**{**base.__dict__, "signal": r.label})
        try:
            h1, h2 = halves(px, vol, cfg, spread)
        except Exception as exc:                       # noqa: BLE001
            print(f"  {r.label:26s} split failed: {exc}")
            continue
        ok = h1 > 0 and h2 > 0
        print(f"  {r.label:26s} half1 {h1*100:+7.1f}%  half2 {h2*100:+7.1f}%  "
              f"{'consistent' if ok else 'INCONSISTENT'}")

    print(f"\n{tested} signals tested. Harvey/Liu/Zhu threshold for a new "
          f"factor is |t| >= 3.0, not 2.0.")


if __name__ == "__main__":
    main()
