"""Point-in-time recorder for the BIST screen.

WHY THIS IS THE MOST IMPORTANT FILE IN THE BIST PACKAGE
-------------------------------------------------------
Every price rule in this project was validated by replaying history. The
fundamental screen cannot be: the vendor serves a CURRENT snapshot — four
annual periods and six quarters, with no publication dates — so there is no way
to reconstruct what a screen would have seen on an arbitrary past date. Worse,
under TMS 29 each filing restates its own comparatives, so even the historical
figures you can see are expressed in today's lira rather than the lira of the
day they were published.

That leaves exactly one honest path: start recording now, and be able to
measure this strategy properly in a year or two instead of arguing for it.

WHAT MAKES A RECORD TRUSTWORTHY
-------------------------------
Append-only, stamped at write time, and immutable afterwards. Three separate
bugs in this project came from recomputing history rather than recording it —
most recently a liquidity ranking that sorted dates instead of stocks because
Turkish lira turnover grows with inflation. A row written today and never
touched again cannot develop that class of fault.

Each row also carries a hash of the configuration that produced it. A changed
threshold is a different strategy, and comparing rows across a config change
without noticing would reproduce exactly the "best of forty rules" error the
price studies made. The hash makes that mistake visible rather than silent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from src.bist.screen import ScreenConfig
from src.logger import get_logger

log = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS bist_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of         TEXT NOT NULL,      -- trading date the screen describes
    recorded_at   TEXT NOT NULL,      -- when this row was written; never edited
    config_hash   TEXT NOT NULL,      -- a changed threshold is a changed strategy
    ticker        TEXT NOT NULL,
    eligible      INTEGER NOT NULL,
    reject        TEXT,
    rank_pos      INTEGER,
    total         REAL,
    value_score   REAL,
    quality_score REAL,
    momentum      REAL,
    market_cap    REAL,
    turnover      REAL,
    pb            REAL,
    pe            REAL,
    roe_nominal   REAL,
    roe_real      REAL,
    profit_margin REAL,
    debt_equity   REAL,
    forensic_sev  INTEGER,
    flag_codes    TEXT,
    price         REAL,               -- the close that day, for later scoring
    UNIQUE(as_of, config_hash, ticker)
);

CREATE INDEX IF NOT EXISTS ix_bist_snap_asof ON bist_snapshots(as_of);
CREATE INDEX IF NOT EXISTS ix_bist_snap_tic  ON bist_snapshots(ticker);

CREATE TABLE IF NOT EXISTS bist_portfolios (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of        TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    config_hash  TEXT NOT NULL,
    holdings     TEXT NOT NULL,       -- json {ticker: weight}
    turnover_pct REAL,                -- vs the previous recorded portfolio
    note         TEXT,
    UNIQUE(as_of, config_hash)
);

CREATE TABLE IF NOT EXISTS bist_configs (
    config_hash  TEXT PRIMARY KEY,
    first_seen   TEXT NOT NULL,
    config_json  TEXT NOT NULL
);
"""


def config_hash(cfg: ScreenConfig) -> str:
    """Stable digest of every threshold that affects a decision."""
    blob = json.dumps(asdict(cfg), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


class SnapshotStore:
    """Append-only store. There is deliberately no update or delete method."""

    def __init__(self, db_path: str | Path = "data/bist.db") -> None:
        import sqlite3
        self.path = Path(db_path)
        if str(db_path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ── writing ─────────────────────────────────────────────────

    def record(self, ranked: pd.DataFrame, cfg: ScreenConfig,
               as_of: date | str | None = None,
               prices: dict[str, float] | None = None) -> int:
        """Write one day's complete screen. Returns rows inserted.

        Re-running on the same date with the same config is a no-op rather than
        an error or an overwrite: the first observation of a day is the one that
        was actually available, and a later re-run may see revised vendor data.
        """
        h = config_hash(cfg)
        as_of = str(as_of or date.today())
        now = datetime.now(timezone.utc).isoformat()
        prices = prices or {}

        self.conn.execute(
            "INSERT OR IGNORE INTO bist_configs VALUES (?,?,?)",
            (h, now, json.dumps(asdict(cfg), sort_keys=True)),
        )

        rows = []
        for pos, (_, r) in enumerate(ranked.iterrows(), start=1):
            tic = str(r["tic"])
            rows.append((
                as_of, now, h, tic,
                1 if bool(r.get("eligible")) else 0,
                (str(r.get("reject")) or None) if r.get("reject") else None,
                pos if bool(r.get("eligible")) else None,
                _f(r.get("total")), _f(r.get("value")), _f(r.get("quality")),
                _f(r.get("momentum")), _f(r.get("mcap")), _f(r.get("turnover")),
                _f(r.get("pb")), _f(r.get("pe")), _f(r.get("roe_nom")),
                _f(r.get("roe_real")), _f(r.get("margin")), _f(r.get("de")),
                int(r.get("sev") or 0), str(r.get("flag_codes") or ""),
                _f(prices.get(tic)),
            ))
        cur = self.conn.executemany(
            "INSERT OR IGNORE INTO bist_snapshots "
            "(as_of, recorded_at, config_hash, ticker, eligible, reject, "
            " rank_pos, total, value_score, quality_score, momentum, "
            " market_cap, turnover, pb, pe, roe_nominal, roe_real, "
            " profit_margin, debt_equity, forensic_sev, flag_codes, price) "
            "VALUES (" + ",".join("?" * 22) + ")", rows)
        self.conn.commit()
        n = cur.rowcount or 0
        log.info("bist snapshot recorded",
                 extra={"as_of": as_of, "rows": n, "config": h})
        return n

    def record_portfolio(self, holdings: dict[str, float], cfg: ScreenConfig,
                         as_of: date | str | None = None,
                         note: str = "") -> float:
        """Write the target book and return turnover vs the previous record."""
        h = config_hash(cfg)
        as_of = str(as_of or date.today())
        prev = self.latest_portfolio(h)
        turnover = _turnover(prev, holdings)
        self.conn.execute(
            "INSERT OR IGNORE INTO bist_portfolios "
            "(as_of, recorded_at, config_hash, holdings, turnover_pct, note) "
            "VALUES (?,?,?,?,?,?)",
            (as_of, datetime.now(timezone.utc).isoformat(), h,
             json.dumps(holdings, sort_keys=True), turnover, note),
        )
        self.conn.commit()
        return turnover

    # ── reading ─────────────────────────────────────────────────

    def latest_portfolio(self, cfg_hash: str) -> dict[str, float]:
        row = self.conn.execute(
            "SELECT holdings FROM bist_portfolios WHERE config_hash=? "
            "ORDER BY as_of DESC, id DESC LIMIT 1", (cfg_hash,)).fetchone()
        return json.loads(row["holdings"]) if row else {}

    def history(self, ticker: str | None = None) -> pd.DataFrame:
        q = "SELECT * FROM bist_snapshots"
        args: tuple = ()
        if ticker:
            q += " WHERE ticker=?"
            args = (ticker,)
        return pd.read_sql_query(q + " ORDER BY as_of", self.conn, params=args)

    def coverage(self) -> str:
        r = self.conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT as_of) d, COUNT(DISTINCT ticker) t,"
            " MIN(as_of) a, MAX(as_of) b, COUNT(DISTINCT config_hash) c "
            "FROM bist_snapshots").fetchone()
        if not r or not r["n"]:
            return "no snapshots recorded yet"
        span = ""
        try:
            days = (pd.Timestamp(r["b"]) - pd.Timestamp(r["a"])).days
            span = f", spanning {days} days"
        except Exception:
            pass
        return (f"{r['n']:,} rows · {r['d']} dates · {r['t']} tickers · "
                f"{r['a']} to {r['b']}{span} · {r['c']} config version(s)")

    def forward_returns(self, horizon_days: int = 63) -> pd.DataFrame:
        """Join each recorded row to the price ``horizon_days`` later.

        This is the payoff of recording: once enough dates accumulate, the
        screen can finally be judged the way every price rule in this project
        was — did the names it ranked highest actually outperform, net of cost,
        with a t-statistic that clears 3.0. Returns empty until there is enough
        history, and saying so is the correct answer for now.
        """
        df = self.history()
        if df.empty:
            return df
        df["as_of"] = pd.to_datetime(df["as_of"])
        import numpy as np

        out = []
        for _tic, g in df.groupby("ticker"):
            g = g.sort_values("as_of").reset_index(drop=True)
            px = pd.to_numeric(g["price"], errors="coerce")
            fwd = px.shift(-1)                  # next recorded observation
            # A zero or missing price must not become an infinite return.
            # (pandas 3 removed the `mode.use_inf_as_na` option that used to
            # handle this implicitly, so it is done explicitly here.)
            g["fwd_return"] = (fwd / px.where(px > 0) - 1.0).replace(
                [np.inf, -np.inf], np.nan)
            out.append(g)
        res = pd.concat(out, ignore_index=True)
        return res.dropna(subset=["fwd_return"])

    def close(self) -> None:
        self.conn.close()


def _f(v) -> float | None:
    try:
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _turnover(old: dict[str, float], new: dict[str, float]) -> float:
    """One-sided turnover between two books, as a percentage.

    Reported because Novy-Marx & Velikov's threshold is the operative
    constraint on this whole approach: under 50% one-sided monthly turnover a
    strategy still delivers net returns; above roughly 100% the costs exceed
    most anomalies. A screen that quietly drifts past that has stopped working
    regardless of how good its rankings look.
    """
    if not old:
        return 100.0 if new else 0.0
    keys = set(old) | set(new)
    return 100.0 * sum(abs(new.get(k, 0.0) - old.get(k, 0.0)) for k in keys) / 2.0
