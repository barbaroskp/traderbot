"""Cross-sectional selection across a wide perpetual universe.

WHAT CHANGED FROM THE FAILED APPROACH
--------------------------------------
The original bot asked "will THIS symbol go up?" for each symbol in turn, using
six clusters of indicators. That question was tested to death — around fifty
rule variants, horizons from 30 minutes to 14 days, five volume tiers, eight
asset classes — and never produced |t| > 2 net of costs.

This asks a different question: "of the six hundred things listed, which are
the most attractive RIGHT NOW relative to each other?" That is a ranking
problem rather than a forecasting one, and it is the form in which momentum and
its relatives actually survive in the literature. It may also fail. The point
of this module is that it fails visibly, under a referee, instead of quietly.

SURVIVORSHIP
------------
The universe comes from the contracts that exist today, so everything delisted
is missing — and in crypto, delisting usually means the price went to zero.
That biases a long-only cross-sectional study upward, momentum most of all,
because the names a loser portfolio would have held are precisely the ones the
exchange removed. Every number produced here is optimistic by an unmeasurable
amount. It is stated rather than hidden because there is no public endpoint
that would fix it.

POINT-IN-TIME DISCIPLINE
------------------------
A symbol enters the investable set on date t only if it already had
`min_history` bars before t and cleared the liquidity filter using data from
before t. An earlier study in this project ranked assets using a filter applied
over the whole sample and turned a t-statistic of +3.48 into +0.96 the moment
that was fixed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class SelectConfig:
    """Everything that changes a decision."""

    n_holdings: int = 10
    # Ranking signal. Every one of these is a HYPOTHESIS, not a belief; the
    # backtest decides. "none" is the control: hold the whole eligible universe.
    signal: str = "mom_30_7"
    min_history: int = 120
    min_turnover_usdt: float = 5e6      # 20-day average, point in time
    max_spread_bps: float = 20.0

    rebalance_days: int = 7
    # An incumbent is only displaced by a challenger ranking this many places
    # higher, which is what keeps turnover — and therefore cost — bounded.
    replace_margin_ranks: int = 3

    # Position sizing. Inverse volatility spreads risk; equal weight
    # concentrates it in whatever is most volatile, which in crypto is
    # whatever just ran.
    weighting: str = "invvol"           # "invvol" | "equal"
    vol_lookback: int = 30

    # Leverage responds to BREADTH: how much of the universe is participating.
    # The brief asked for leverage when there are many opportunities. Breadth
    # is the honest measure of that, but note it is also just a bull-market
    # detector, so the backtest has to show it earns its keep.
    use_breadth_leverage: bool = False
    breadth_lookback: int = 30
    max_leverage: float = 1.0
    min_leverage: float = 1.0

    taker_fee: float = 0.0005
    slippage_bps: float = 5.0
    funding_annual: float = 0.10        # paid on the levered portion only


def _returns(px: pd.DataFrame) -> pd.DataFrame:
    return px.pct_change()


def signal_values(px: pd.DataFrame, vol_usdt: pd.DataFrame, i: int,
                  cfg: SelectConfig) -> pd.Series:
    """Rank score for every symbol, using ONLY data strictly before row i.

    Higher is better. NaN means "no opinion" and is excluded from ranking
    rather than filled, because filling it invents a view.
    """
    hist = px.iloc[:i]
    if len(hist) < cfg.min_history:
        return pd.Series(dtype=float)
    c = hist.iloc[-1]
    s = cfg.signal

    if s == "none":
        return pd.Series(0.0, index=px.columns)
    if s == "mom_30":
        return c / hist.iloc[-31] - 1.0
    if s == "mom_90":
        return c / hist.iloc[-91] - 1.0
    if s == "mom_30_7":
        # 30-day momentum skipping the last week: the recent window is where
        # short-term reversal lives, and including it mixes two opposing
        # effects into one number.
        return hist.iloc[-8] / hist.iloc[-31] - 1.0
    if s == "rev_7":
        return -(c / hist.iloc[-8] - 1.0)
    if s == "rev_30":
        return -(c / hist.iloc[-31] - 1.0)
    if s == "lowvol":
        return -_returns(hist).iloc[-cfg.vol_lookback:].std()
    if s == "riskadj_mom":
        r = _returns(hist).iloc[-31:-8]
        sd = r.std()
        return (hist.iloc[-8] / hist.iloc[-31] - 1.0) / sd.where(sd > 0)
    if s == "vol_growth":
        recent = vol_usdt.iloc[max(0, i - 7):i].mean()
        base = vol_usdt.iloc[max(0, i - 60):i - 7].mean()
        return recent / base.where(base > 0) - 1.0
    if s == "dist_ath":
        # Furthest below its own high — a value proxy, and the mirror of
        # momentum. If both "buy winners" and "buy losers" appear to work, the
        # study is measuring noise.
        return -(c / hist.max() - 1.0)
    raise ValueError(f"unknown signal {s!r}")


def eligible(px: pd.DataFrame, vol_usdt: pd.DataFrame, i: int,
             cfg: SelectConfig, spread_bps: pd.Series | None = None) -> list[str]:
    """Investable set at row i, decided only on prior data."""
    hist = px.iloc[:i]
    out = []
    for sym in px.columns:
        s = hist[sym].dropna()
        if len(s) < cfg.min_history or not np.isfinite(px[sym].iloc[i - 1]):
            continue
        turn = vol_usdt[sym].iloc[max(0, i - 20):i].mean()
        if not np.isfinite(turn) or turn < cfg.min_turnover_usdt:
            continue
        if spread_bps is not None:
            sp = spread_bps.get(sym, np.nan)
            if np.isfinite(sp) and sp > cfg.max_spread_bps:
                continue
        out.append(sym)
    return out


def breadth(px: pd.DataFrame, i: int, cfg: SelectConfig) -> float:
    """Fraction of the universe above its own trailing average.

    Used to scale leverage. It is deliberately a simple, hard-to-overfit
    statistic; if this does not work, a cleverer one almost certainly is not
    working either, it is just hiding the failure better.
    """
    hist = px.iloc[max(0, i - cfg.breadth_lookback):i]
    if len(hist) < 5:
        return 0.0
    last, mean = hist.iloc[-1], hist.mean()
    ok = (last > mean) & np.isfinite(last) & np.isfinite(mean)
    n = int(np.isfinite(last).sum())
    return float(ok.sum() / n) if n else 0.0


def weights_for(px: pd.DataFrame, i: int, picks: list[str],
                cfg: SelectConfig) -> pd.Series:
    if not picks:
        return pd.Series(dtype=float)
    if cfg.weighting == "equal":
        w = pd.Series(1.0 / len(picks), index=picks)
    else:
        r = _returns(px.iloc[max(0, i - cfg.vol_lookback):i][picks])
        vol = r.std().replace(0, np.nan)
        inv = (1.0 / vol).replace([np.inf, -np.inf], np.nan).dropna()
        if inv.empty:
            w = pd.Series(1.0 / len(picks), index=picks)
        else:
            w = inv / inv.sum()
            w = w.reindex(picks).fillna(0.0)
            if w.sum() > 0:
                w = w / w.sum()
    lev = 1.0
    if cfg.use_breadth_leverage:
        b = breadth(px, i, cfg)
        lev = cfg.min_leverage + (cfg.max_leverage - cfg.min_leverage) * b
    return w * lev


@dataclass
class Result:
    label: str
    curve: pd.Series
    turnover_per_year: float = 0.0
    avg_holdings: float = 0.0
    avg_leverage: float = 1.0
    picks_log: list = field(default_factory=list)

    def stats(self) -> dict:
        s = self.curve
        yrs = len(s) / 365
        tot = float(s.iloc[-1] / s.iloc[0] - 1)
        cagr = (1 + tot) ** (1 / yrs) - 1 if tot > -1 else -1.0
        r = s.pct_change().dropna()
        vol = float(r.std() * np.sqrt(365))
        dd = float((s / s.cummax() - 1).min())
        # Deflated for the fact that many rules are being tried: with k trials
        # the expected max t-stat rises even under a null of no skill.
        t = float(r.mean() / (r.std() / np.sqrt(len(r)))) if r.std() > 0 else 0.0
        return {"cagr": cagr, "vol": vol, "maxdd": dd, "total": tot,
                "calmar": cagr / abs(dd) if dd < 0 else 0.0, "t": t}

    def row(self) -> str:
        s = self.stats()
        return (f"{self.label:26s} {s['cagr']*100:+8.1f}% {s['maxdd']*100:7.1f}% "
                f"{s['vol']*100:6.0f}% {s['calmar']:7.3f} {s['t']:+6.2f} "
                f"{self.turnover_per_year*100:7.0f}% {self.avg_holdings:5.1f} "
                f"{self.avg_leverage:6.2f}x")

    @staticmethod
    def header() -> str:
        return (f"{'strategy':26s} {'CAGR':>9s} {'maxDD':>8s} {'vol':>7s} "
                f"{'calmar':>7s} {'t':>6s} {'turn/yr':>8s} {'held':>5s} {'lev':>7s}")


def backtest(px: pd.DataFrame, vol_usdt: pd.DataFrame, cfg: SelectConfig,
             label: str = "", spread_bps: pd.Series | None = None) -> Result:
    """Replay selection over history.

    Decisions at row i use rows < i and are applied to the return from i to
    i+1. That ordering is not a detail: an earlier script in this project
    decided with the same bar it measured and reported +2,473,980%.
    """
    R = _returns(px).fillna(0.0)
    idx = px.index
    eq = 1.0
    held = pd.Series(dtype=float)
    curve, turn_total, hold_counts, levs = [], 0.0, [], []
    last_rebal = -10 ** 9
    start = max(cfg.min_history, cfg.vol_lookback) + 5
    cost_per_unit = cfg.taker_fee + cfg.slippage_bps / 1e4
    fund_daily = cfg.funding_annual / 365

    for i in range(start, len(px) - 1):
        if i - last_rebal >= cfg.rebalance_days:
            elig = eligible(px, vol_usdt, i, cfg, spread_bps)
            if elig:
                sig = signal_values(px, vol_usdt, i, cfg).reindex(elig).dropna()
                if not sig.empty:
                    ranked = sig.sort_values(ascending=False)
                    picks = list(ranked.index[:cfg.n_holdings])
                    # Incumbency: only displace a holding if the challenger is
                    # clearly better, or turnover eats the edge.
                    if len(held):
                        order = {s: r for r, s in enumerate(ranked.index)}
                        for h in held.index:
                            if h in picks or h not in order:
                                continue
                            worst = max(picks, key=lambda p: order.get(p, 10**9))
                            if order[h] - order.get(worst, 10**9) < cfg.replace_margin_ranks:
                                picks[picks.index(worst)] = h
                    target = weights_for(px, i, picks, cfg)
                    allsym = held.index.union(target.index)
                    t = float((target.reindex(allsym).fillna(0.0)
                               - held.reindex(allsym).fillna(0.0)).abs().sum())
                    if t > 0.02:
                        eq *= (1 - t * cost_per_unit)
                        turn_total += t
                        held = target
                        last_rebal = i

        if len(held):
            step = float((held * R.iloc[i + 1].reindex(held.index).fillna(0.0)).sum())
            step -= max(0.0, held.sum() - 1.0) * fund_daily
            eq *= (1 + step)
            hold_counts.append(len(held))
            levs.append(float(held.sum()))
        curve.append(eq)

    years = len(curve) / 365
    return Result(
        label=label or f"{cfg.signal}/{cfg.n_holdings}",
        curve=pd.Series(curve, index=idx[start + 1: start + 1 + len(curve)]),
        turnover_per_year=turn_total / years if years else 0.0,
        avg_holdings=float(np.mean(hold_counts)) if hold_counts else 0.0,
        avg_leverage=float(np.mean(levs)) if levs else 1.0,
    )
