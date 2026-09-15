"""Multi-asset allocation engine, built for an investor who lives in lira.

WHY THIS REPLACES THE SINGLE-MARKET ALLOCATOR
----------------------------------------------
`src/allocator.py` holds a fixed crypto basket and rebalances it. Measured over
two years that returned +16%/yr with a −58% drawdown. The return is real; the
drawdown makes it something almost nobody actually holds through, and a
strategy you abandon at the bottom has a negative expected return no matter
what the backtest says.

The fix is not a better forecast — fifty rules were tested and none predicted
anything. It is diversification and position sizing, which are the two levers
that work without requiring you to be right about direction.

The opportunity is visible in the correlation matrix of Turkish-lira returns,
2017–2026:

        BTC    ETH  XU100  GOLD  SP500  SILVER   USD
  BTC  1.00   0.79   0.07  0.23   0.35    0.21  0.23
  XU100 .07   0.08   1.00  0.06   0.17    0.12  0.02
  GOLD  .23   0.17   0.06  1.00   0.55    0.80  0.72

Turkish equity is essentially uncorrelated with everything else a Turkish
investor can buy (0.02–0.17). Gold and the S&P are highly correlated *with each
other and with the dollar* (0.55–0.72) because for a lira investor they are all
partly the same trade: short the lira. BTC and ETH are 0.79 — one position, not
two. Gold and silver are 0.80 — likewise.

So the genuinely independent bets are roughly: Turkish equity, crypto, and
hard-currency assets. Three, not seven.

THREE MECHANISMS, IN ORDER OF HOW MUCH THEY MATTER
---------------------------------------------------
1. DIVERSIFICATION across those independent bets. Free, and the only thing in
   finance that is.
2. VOLATILITY TARGETING. Scale total exposure so the portfolio hits a chosen
   risk level. This survives out-of-sample scrutiny for a single long position
   (Moreira & Muir 2017; Cederburg et al. 2020 show the factor-timing version
   largely does not) because volatility is strongly autocorrelated while
   returns are not. It does not add alpha. It makes the drawdown survivable,
   which is what actually determines whether the money compounds.
3. INFLATION AS THE HURDLE. Everything is measured in real lira. A 22% nominal
   return against 35% inflation is a 13% loss, and any engine that reports
   nominal returns to a Turkish investor is lying by omission.

WHAT THIS ENGINE WILL NOT DO
----------------------------
Predict. There is no signal here, no momentum overlay on the asset mix, no
regime model. Weights come from volatility and correlation, which are
estimable, rather than from expected returns, which are not — estimation error
in expected returns is the classic way mean-variance optimisation destroys
portfolios.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# Turkish CPI, year-on-year, December-to-December (TÜİK). 2026 is the August
# print annualised. The engine needs this because a nominal return means
# nothing here: over this sample inflation averaged well above 30%.
TURKISH_CPI_YOY = {
    2016: 0.0853, 2017: 0.1192, 2018: 0.2030, 2019: 0.1184, 2020: 0.1460,
    2021: 0.3608, 2022: 0.6427, 2023: 0.6477, 2024: 0.4438, 2025: 0.3089,
    2026: 0.3151,
}

# Overnight lira deposit rate available to a retail saver, net of the 5%
# withholding on 1-year deposits. Approximated by the policy rate, which is what
# deposit pricing tracks. Cash is NOT a zero-return asset in Turkey — in 2026 it
# pays a positive real rate, and an engine that models it as zero will
# systematically over-allocate to risk.
TL_DEPOSIT_RATE = {
    2016: 0.08, 2017: 0.09, 2018: 0.18, 2019: 0.16, 2020: 0.12, 2021: 0.16,
    2022: 0.14, 2023: 0.32, 2024: 0.48, 2025: 0.44, 2026: 0.37,
}


def daily_inflation(index: pd.DatetimeIndex) -> pd.Series:
    """Daily compounding rate implied by each year's CPI print."""
    y = pd.Series(index.year, index=index)
    ann = y.map(TURKISH_CPI_YOY).fillna(0.30)
    return (1.0 + ann) ** (1.0 / 252.0) - 1.0


def daily_cash_rate(index: pd.DatetimeIndex) -> pd.Series:
    y = pd.Series(index.year, index=index)
    ann = y.map(TL_DEPOSIT_RATE).fillna(0.30)
    return (1.0 + ann) ** (1.0 / 252.0) - 1.0


@dataclass
class EngineConfig:
    """Every number that changes a decision, in one place."""

    # Target annualised volatility of the whole book. This is the main dial:
    # it sets the drawdown you are signing up for, roughly 2–3x the target in a
    # bad year. 20% is chosen so a −50% year is unlikely rather than expected.
    target_vol: float = 0.20

    # Exposure is capped because volatility targeting levers UP when markets are
    # calm, and calm markets are exactly where leverage gets people killed. The
    # arithmetic: growth is maximised at L* = (mu-r)/sigma^2 and turns negative
    # near 2L*. For an asset with 30% excess return and 50% vol, L* = 1.2, so
    # anything above ~2x is destroying growth even with a positive edge.
    max_gross_exposure: float = 1.0
    min_gross_exposure: float = 0.0

    # Per-asset cap. Without it, inverse-volatility weighting concentrates in
    # whatever is quiet this month, which is how "diversified" books end up
    # 70% in one position.
    max_weight: float = 0.35

    vol_lookback: int = 63          # ~3 months: long enough to be stable,
    corr_lookback: int = 252        # short enough to react to a regime change
    rebalance_days: int = 21
    # Only trade when a weight has drifted this far from target, RELATIVE to
    # the target. Threshold banding rather than calendar rebalancing is what
    # keeps one-sided turnover under the ~50%/month that Novy-Marx & Velikov
    # find strategies need to stay net-positive.
    drift_band: float = 0.25
    cost_per_trade: float = 0.002   # round trip, fraction of notional traded

    assets: tuple[str, ...] = ("BTC", "XU100", "GOLD")
    use_cash_asset: bool = True


def inverse_vol_weights(returns: pd.DataFrame, cfg: EngineConfig) -> pd.Series:
    """Risk-balanced weights: each asset contributes similar risk.

    Deliberately NOT mean-variance. Expected returns cannot be estimated well
    enough to optimise on — the errors are larger than the differences — and
    the optimiser responds by loading everything onto whichever asset had the
    best recent run. Volatility and correlation are estimable; that is the
    whole reason weights come from them.
    """
    vol = returns.tail(cfg.vol_lookback).std() * np.sqrt(252)
    vol = vol.replace(0, np.nan).dropna()
    if vol.empty:
        return pd.Series(dtype=float)

    w = 1.0 / vol
    w = w / w.sum()

    # Correlation haircut: an asset that moves with the rest of the book is
    # bringing less diversification than its own volatility suggests.
    if len(returns.columns) > 1 and len(returns) >= cfg.corr_lookback // 2:
        c = returns.tail(cfg.corr_lookback).corr()
        avg_corr = (c.sum() - 1.0) / max(len(c) - 1, 1)
        haircut = (1.0 - avg_corr.clip(lower=0.0, upper=0.95)).reindex(w.index)
        w = (w * haircut).fillna(0.0)
        if w.sum() > 0:
            w = w / w.sum()

    # Cap, then redistribute the excess, iterating because capping one asset
    # can push another over the line.
    for _ in range(10):
        over = w > cfg.max_weight
        if not over.any():
            break
        excess = (w[over] - cfg.max_weight).sum()
        w[over] = cfg.max_weight
        room = ~over
        if not room.any() or w[room].sum() <= 0:
            break
        w[room] += excess * (w[room] / w[room].sum())
    return w


def vol_scalar(returns: pd.DataFrame, weights: pd.Series,
               cfg: EngineConfig) -> float:
    """How much of the book to put at risk, to hit the volatility target.

    Uses the full covariance rather than a weighted average of volatilities,
    because with correlations from 0.02 to 0.80 those two numbers differ by a
    lot — and the low-correlation case is precisely where naive scaling
    under-invests.
    """
    r = returns.tail(cfg.vol_lookback)[weights.index].dropna()
    if len(r) < 20:
        return cfg.min_gross_exposure
    cov = r.cov() * 252
    var = float(weights.values @ cov.values @ weights.values)
    if var <= 0:
        return cfg.min_gross_exposure
    realised = np.sqrt(var)
    return float(np.clip(cfg.target_vol / realised,
                         cfg.min_gross_exposure, cfg.max_gross_exposure))


@dataclass
class BacktestResult:
    label: str
    curve: pd.Series                      # nominal lira
    real_curve: pd.Series                 # inflation-adjusted
    turnover_per_year: float = 0.0
    trades: int = 0
    weights: pd.DataFrame = field(default_factory=pd.DataFrame)

    def _stats(self, s: pd.Series) -> dict:
        years = len(s) / 252
        total = float(s.iloc[-1] / s.iloc[0] - 1)
        cagr = (1 + total) ** (1 / years) - 1 if total > -1 else -1.0
        r = s.pct_change().dropna()
        vol = float(r.std() * np.sqrt(252))
        dd = float((s / s.cummax() - 1).min())
        return {"total": total, "cagr": cagr, "vol": vol, "maxdd": dd,
                "sharpe": cagr / vol if vol > 0 else 0.0,
                "calmar": cagr / abs(dd) if dd < 0 else 0.0}

    @property
    def nominal(self) -> dict:
        return self._stats(self.curve)

    @property
    def real(self) -> dict:
        return self._stats(self.real_curve)

    def row(self) -> str:
        n, r = self.nominal, self.real
        return (f"{self.label:34s} {n['cagr']*100:+7.1f}% {r['cagr']*100:+8.1f}% "
                f"{r['vol']*100:7.1f}% {r['maxdd']*100:7.1f}% "
                f"{r['calmar']:6.2f} {self.turnover_per_year*100:7.0f}%")

    @staticmethod
    def header() -> str:
        return (f"{'strategy':34s} {'nominal':>8s} {'REAL':>9s} {'vol':>8s} "
                f"{'maxDD':>8s} {'calmar':>6s} {'turn/yr':>8s}")


def backtest(prices: pd.DataFrame, cfg: EngineConfig,
             label: str = "engine") -> BacktestResult:
    """Replay the engine over lira price history.

    Decisions on day i use returns strictly BEFORE day i and are applied to the
    return FROM i to i+1. That ordering is the whole game: an earlier study in
    this project decided with the same bar it measured and produced a
    +2,473,980% return before the bug was found.
    """
    px = prices[list(cfg.assets)].dropna()
    if px.empty or len(px) < cfg.corr_lookback + 30:
        raise ValueError("not enough history")

    rets = px.pct_change()
    cash = daily_cash_rate(px.index)
    infl = daily_inflation(px.index)

    # The hot loop runs on numpy. The pandas version was ~100x slower and a
    # sweep of a dozen configurations did not finish; nothing about the logic
    # changed, only the container.
    cols = list(px.columns)
    R = np.array(rets.to_numpy(dtype=float), copy=True)
    R[~np.isfinite(R)] = 0.0
    CASH = cash.to_numpy(dtype=float)
    INFL = infl.to_numpy(dtype=float)

    equity = real_equity = 1.0
    held = np.zeros(len(cols))
    curve, real_curve, wlog = [], [], []
    turnover_total = 0.0
    trades = 0
    start = cfg.corr_lookback
    last_rebal = -10**9

    for i in range(start, len(px) - 1):
        if i - last_rebal >= cfg.rebalance_days:
            # Fixed-width window, not rets.iloc[:i]. Slicing the whole history
            # and calling dropna() on it every rebalance copies a frame that
            # grows to thousands of rows, and a sweep of a dozen configurations
            # would not finish. Nothing downstream looks further back than
            # corr_lookback anyway.
            lo = max(0, i - cfg.corr_lookback)
            hist = rets.iloc[lo:i].dropna()
            target_s = inverse_vol_weights(hist, cfg)
            if not target_s.empty:
                scale = vol_scalar(hist, target_s, cfg)
                target_s = target_s * scale
                target = target_s.reindex(cols).fillna(0.0).to_numpy()
                drift = float(np.abs(held - target).sum())
                # Always deploy from flat; afterwards only when meaningfully off
                if held.sum() == 0 or drift > cfg.drift_band * max(target.sum(), 1e-9):
                    equity *= (1 - drift * cfg.cost_per_trade)
                    real_equity *= (1 - drift * cfg.cost_per_trade)
                    turnover_total += drift
                    trades += 1
                    held = target
                    last_rebal = i

        r = float(held @ R[i + 1])
        if cfg.use_cash_asset:
            r += max(0.0, 1.0 - held.sum()) * CASH[i + 1]
        equity *= (1 + r)
        real_equity *= (1 + r) / (1 + INFL[i + 1])
        curve.append(equity)
        real_curve.append(real_equity)
        wlog.append(held.copy())

    idx = px.index[start + 1:len(px)]
    years = len(curve) / 252
    return BacktestResult(
        label=label,
        curve=pd.Series(curve, index=idx),
        real_curve=pd.Series(real_curve, index=idx),
        turnover_per_year=turnover_total / years if years else 0.0,
        trades=trades,
        weights=pd.DataFrame(wlog, index=idx, columns=cols),
    )


def buy_and_hold(prices: pd.DataFrame, assets: tuple[str, ...],
                 label: str) -> BacktestResult:
    """Equal-weight, bought once, never touched. The benchmark that any engine
    has to beat before it has earned its complexity."""
    px = prices[list(assets)].dropna()
    norm = px / px.iloc[0]
    curve = norm.mean(axis=1)
    infl = daily_inflation(px.index)
    real = curve / (1 + infl).cumprod()
    return BacktestResult(label=label, curve=curve,
                          real_curve=real / real.iloc[0])


def cash_only(index: pd.DatetimeIndex, label: str = "TL deposit") -> BacktestResult:
    """The option that requires no code, no risk and no attention. In 2026 it
    pays a positive real rate, which makes it a serious benchmark rather than a
    formality."""
    c = daily_cash_rate(index)
    infl = daily_inflation(index)
    curve = (1 + c).cumprod()
    real = ((1 + c) / (1 + infl)).cumprod()
    return BacktestResult(label=label, curve=curve, real_curve=real)
