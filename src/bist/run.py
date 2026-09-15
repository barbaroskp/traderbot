"""Daily runner for the BIST screen.

    python -m src.bist.run              # screen, record, print the book
    python -m src.bist.run --no-record  # look without writing
    python -m src.bist.run --explain GESAN
    python -m src.bist.run --coverage   # how much point-in-time history exists

This writes nothing but a record. It places no orders, and there is no live
execution path in this package on purpose: the screen has never been validated
on out-of-sample Turkish data, because none exists to validate it on. Every run
adds one day to the record that will eventually make that validation possible.
Until `--coverage` shows a couple of years, treat the output as a research
artefact rather than a set of instructions.
"""

from __future__ import annotations

import argparse
import pickle
import warnings
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from src.bist.forensics import analyse
from src.bist.screen import (
    Candidate,
    ScreenConfig,
    build_portfolio,
    explain,
    score,
)
from src.bist.snapshot import SnapshotStore, config_hash

warnings.filterwarnings("ignore")

CACHE = Path("research/bist/data")
EXTRA = {"GESAN", "EUPWR", "ASTOR", "SMRTG", "REEDR", "KONTR", "ENERY", "TERA"}


def universe() -> list[str]:
    from research.bist.universe import TICKERS
    return sorted(set(TICKERS) | EXTRA)


def load(fresh: bool) -> tuple[dict, dict, dict]:
    """Statements, key ratios and prices — from cache unless ``fresh``.

    The cache exists so a re-run is reproducible and does not hammer the vendor.
    A live run for the record should pass ``--fresh``: a snapshot stamped today
    but built from week-old cached fundamentals would corrupt exactly the
    point-in-time property the record exists to establish.
    """
    paths = {k: CACHE / f"{k}.pkl" for k in ("stmts2", "info2", "px2")}
    if not fresh and all(p.exists() for p in paths.values()):
        return tuple(pickle.loads(p.read_bytes()) for p in paths.values())  # type: ignore

    import yfinance as yf
    stmts, info, px = {}, {}, {}
    for t in universe():
        try:
            k = yf.Ticker(f"{t}.IS")
            stmts[t] = (k.quarterly_financials, k.quarterly_balance_sheet,
                        k.quarterly_cashflow)
            info[t] = k.info
            d = k.history(period="2y", interval="1d")
            if d is not None and len(d) > 300:
                px[t] = d
        except Exception:
            continue
    CACHE.mkdir(parents=True, exist_ok=True)
    for name, obj in (("stmts2", stmts), ("info2", info), ("px2", px)):
        (CACHE / f"{name}.pkl").write_bytes(pickle.dumps(obj))
    return stmts, info, px


def build_candidates(stmts: dict, info: dict, px: dict) -> tuple[list[Candidate],
                                                                 dict[str, float]]:
    cands, prices = [], {}
    for t in universe():
        i = info.get(t) or {}
        if not i.get("marketCap"):
            continue
        fs = stmts.get(t)
        f = (analyse(t, *fs) if fs and fs[0] is not None and not fs[0].empty
             else None)

        mom = turnover = np.nan
        d = px.get(t)
        if d is not None and len(d) > 270:
            c = d["Close"].values
            # 12-1 month: skip the most recent month, which is the short-term
            # reversal window rather than momentum.
            mom = c[-21] / c[-252] - 1
            turnover = float((d["Volume"] * d["Close"]).tail(21).mean())
            prices[t] = float(c[-1])

        def _n(v):
            return float(v) if v is not None else np.nan

        cands.append(Candidate(
            ticker=t, market_cap=_n(i.get("marketCap")), turnover=turnover,
            pb=_n(i.get("priceToBook")), pe=_n(i.get("trailingPE")),
            roe_nominal=_n(i.get("returnOnEquity")),
            profit_margin=_n(i.get("profitMargins")),
            debt_to_equity=_n(i.get("debtToEquity")),
            mom_12_1=mom, forensics=f))
    return cands, prices


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-record", action="store_true")
    ap.add_argument("--fresh", action="store_true", help="refetch, don't use cache")
    ap.add_argument("--explain", metavar="TICKER")
    ap.add_argument("--coverage", action="store_true")
    ap.add_argument("--db", default="data/bist.db")
    ap.add_argument("--holdings", type=int, default=15)
    a = ap.parse_args()

    store = SnapshotStore(a.db)
    if a.coverage:
        print(store.coverage())
        fr = store.forward_returns()
        print(f"scoreable observations: {len(fr)}")
        if len(fr) > 200:
            top = fr[fr.rank_pos.notna() & (fr.rank_pos <= 15)]
            rest = fr[fr.rank_pos.isna() | (fr.rank_pos > 15)]
            if len(top) > 30 and len(rest) > 30:
                d = top.fwd_return.mean() - rest.fwd_return.mean()
                se = np.sqrt(top.fwd_return.var() / len(top)
                             + rest.fwd_return.var() / len(rest))
                print(f"held vs rest: {d*100:+.2f}% per period, "
                      f"t={d/se:+.2f} (needs |t|>=3.0)")
        else:
            print("not enough history to judge the screen yet — this is the "
                  "expected answer for a while")
        return

    cfg = ScreenConfig(n_holdings=a.holdings)
    cands, prices = build_candidates(*load(a.fresh))
    ranked = score(cands, cfg)

    if a.explain:
        print(explain(ranked, a.explain.upper()))
        return

    current = store.latest_portfolio(config_hash(cfg))
    book = build_portfolio(ranked, current=current, cfg=cfg)
    eligible = ranked[ranked.eligible]

    print(f"as of {date.today()} · {len(cands)} candidates · "
          f"{len(eligible)} eligible · config {config_hash(cfg)}")
    rejects = ranked[~ranked.eligible].reject.value_counts()
    print("excluded: " + " · ".join(f"{k} {v}" for k, v in rejects.items()))

    print(f"\n{'#':>2} {'ticker':7} {'score':>6} {'value':>6} {'qual':>6} "
          f"{'mom':>5} {'P/B':>5} {'P/E':>6} {'realROE':>8} {'sev':>4}")
    for n, (_, r) in enumerate(eligible.head(cfg.n_holdings).iterrows(), 1):
        mark = " " if r.tic in current else "+"
        print(f"{n:2d}{mark}{r.tic:7} {r.total:6.3f} {r.value:6.2f} "
              f"{r.quality:6.2f} {r.momentum:5.2f} {r.pb:5.2f} {r.pe:6.1f} "
              f"{r.roe_real*100:+7.1f}% {r.sev:4.0f}")

    dropped = sorted(set(current) - set(book))
    if dropped:
        print(f"dropped: {', '.join(dropped)}")

    vetoed = ranked[ranked.reject == "forensic veto"].sort_values(
        "sev", ascending=False)
    if len(vetoed):
        print(f"\nforensic veto ({len(vetoed)}): " +
              ", ".join(f"{r.tic}({r.sev:.0f})" for _, r in vetoed.iterrows()))

    h = eligible.head(cfg.n_holdings)
    print(f"\nbook median: P/E {h.pe.median():.1f} · P/B {h.pb.median():.2f} · "
          f"real ROE {h.roe_real.median()*100:+.1f}% · severity {h.sev.median():.0f}")
    pos = int((h.roe_real > 0).sum())
    print(f"{pos} of {len(h)} holdings out-earn inflation; "
          f"eligible-universe median real ROE {eligible.roe_real.median()*100:+.1f}%")

    if not a.no_record:
        rows = store.record(ranked, cfg, prices=prices)
        turn = store.record_portfolio(book, cfg)
        print(f"\nrecorded {rows} rows · one-sided turnover {turn:.0f}% "
              f"(keep under 50%/month)")
        print(store.coverage())
    store.close()


if __name__ == "__main__":
    main()
