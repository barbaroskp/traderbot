# BIST research — findings

**Data.** 136 Turkish tickers. Daily bars 2016-09-05 → 2026-09-04 (330,397
ticker-days) and hourly bars for the most recent 2 years (68,104 ticker-days),
plus 5-minute bars for 60 days. Every study derives its signal and its outcome
from a SINGLE frame, because joining differently-adjusted series produced a
fake result on the first attempt.

**Three of my own bugs were found and fixed while producing this document.**
They are listed at the end, because the pattern matters more than any single
number: every one of them made a strategy look better than it was.

---

## Summary

| candidate | measured | verdict |
|---|---|---|
| opening gap fade (short gap-ups) | +0.41%/trade gross, t=+14.8 | **dead** — lives inside the opening print, −87% one hour later; shorting was banned for ~13 of the 24 months |
| overnight return (buy close, sell open) | +0.247%/day, t=+105, positive in all 10 years | **dead** — consumed by the bid-ask spread in every liquidity decile |
| intraday (open→close) | −0.078%/day, t=−16 | not a strategy, but a **headwind every intraday long fights** |
| 12-1m momentum, top 15, monthly | +49.8%/yr vs +46.4% buy-hold | +3.4pp/yr, survivorship-inflated, 5.9× turnover |
| rebalancing an equal-weight basket | −0.6pp/yr vs never rebalancing | **hurts** on BIST (unlike the crypto basket) |
| everything else tested | — | loses to buy-and-hold |

**Nothing tested is tradeable at Turkish retail costs.**

---

## 1. The opening gap fade

Stocks gapping up more than +3% fade during the day; gapping down, they
recover. On 136 names over 2 years the short-the-gap-up rule showed +0.413%
gross per trade at t=+14.80, 68.6% win rate, positive in both halves and in
73 of 102 tickers.

**Why it is not real money.**

*It lives in the opening print.* Delaying entry destroys it:

| entry | n | gross | t |
|---|---|---|---|
| first bar open | 1024 | +0.416% | +15.08 |
| +1 hour | 1228 | +0.054% | +2.02 |
| +2 hours | 1285 | −0.033% | −1.30 |

On |gap|>3% days the opening print is the day's high 13.5% of the time and the
day's low 14.3% — roughly double the 6.4%/7.1% base rate. The print sits
outside the real trading range far more often than a fair open should.

*The literature says the same, only faster.* Della Corte, Kosowski & Wang
measure the identical trade on US stocks: entry at 09:30:01 → 0.36%/day;
09:31 → 0.11%; 09:45 → 0.04%. **A 69% decay in one minute.** Berkman, Koch,
Tuttle & Zhang (JFQA 2012) document the effect and state explicitly that
retail implicit costs near the open usually exceed the effective half spread —
they describe a cost borne by buyers, not an alpha.

*The logical lock.* The gap is only known once the opening auction prints. At
the moment you learn a stock gapped +3%, the price you needed is history.

*And it could not have been traded anyway.* Short selling on BIST was fully
banned 6 Feb 2023 → 2 Jan 2025, again 23 Mar – 29 Aug 2025, and again
2 Mar – 26 Jun 2026 — roughly 13 of the 24 months in the sample, and BIST-50
only outside those windows.

*Unresolved.* On 60 days of 5-minute data the decay looks slower (+0.266% at
the open, +0.394% at +10 min, +0.195% at +30 min). n≈100 is far too small to
act on, and the better-powered 2-year estimate at +60 minutes is lower. Not
resolvable without paid intraday history.

## 2. The overnight return — real, and entirely the spread

The single strongest statistical result in the whole project:

| leg | mean/day | t | annualised |
|---|---|---|---|
| overnight (close→open) | +0.2476% | +105.1 | +86.5% |
| intraday (open→close) | −0.0779% | −16.1 | −17.8% |
| close→close | +0.1659% | +32.0 | +51.8% |

Positive in **every one of the ten years** (+0.156% to +0.344%/day). Not a
dividend-adjustment artefact: recomputed on raw unadjusted prices it is
+0.2614% vs +0.2726% adjusted.

**Then it dies on the spread.** Ranking stocks by turnover *within each day*
(the pooled ranking is contaminated — see bug 3):

| liquidity decile | gross overnight | spread (1 tick) | net | annualised |
|---|---|---|---|---|
| least liquid 20% | +0.2427% | 0.900% | −0.657% | −81.0% |
| 60–80% | +0.2457% | 0.525% | −0.280% | −50.6% |
| **most liquid 10%** | **+0.2287%** | **0.302%** | **−0.073%** | **−16.9%** |

The gross effect is nearly CONSTANT across liquidity (+0.229% to +0.259%).
What varies is the toll. Even in the most liquid decile, and even assuming the
spread is exactly one tick — the theoretical minimum — the strategy loses
16.9%/yr before any commission.

A live basket simulation confirms it: the 20 most liquid names, rebalanced
daily, held overnight only, at **zero commission**, returns −25.2%/yr against
+31.7%/yr for simply holding the same names.

This is what Lachance and others mean when they call the overnight return a
microstructure artefact: the open prints on the offer side, and the "return"
is the spread that morning buyers pay.

## 3. What the intraday leg means for any bot

Open→close is **−0.078%/day, −17.8% annualised, t=−16**. Turkish equities
drift down during the session and up overnight. Every intraday long strategy
starts roughly 18 percentage points a year behind before a single cost is
charged. This is the quantitative reason the "+1% intraday" idea fails: it is
not that +1% is hard to reach, it is that the session's drift is against you
and the toll is charged on top.

## 4. Longer-horizon strategies (10 years, daily)

| strategy | CAGR | maxDD | turnover/yr |
|---|---|---|---|
| buy & hold, equal weight | +46.4% | −38.1% | 0.1× |
| momentum 12-1m, top 15 | +49.8% | −44.8% | 5.9× |
| equal weight, monthly rebalance | +45.8% | −38.0% | 0.1× |
| low volatility, bottom 15 | +39.5% | −28.5% | 3.1× |
| momentum 6-1m, top 15 | +41.2% | −49.2% | 9.4× |
| short-term reversal, bottom 15 | +29.8% | −52.1% | 21.0× |
| contrarian 12-1m, bottom 15 | +21.9% | −41.2% | 6.6× |

Only 12-1 month momentum beats buy-and-hold, by +3.4pp/yr, at 5.9× turnover
and a worse drawdown. Given survivorship bias and the cost of that turnover it
is not a reliable edge. **Rebalancing subtracts 0.6pp/yr** — the opposite of
what it did on the crypto basket, and a reminder that the rebalancing premium
is a property of the return process, not a free lunch.

All levels here are **optimistic**: delisted companies are absent from the
data source.

## 5. Turkish market mechanics (researched, Sept 2026)

* **Costs.** Mainstream brokers 0.15–0.20% per side plus 5% BSMV on the
  commission → ~0.31–0.41% round trip, plus spread. **Midas charges 0%
  commission** on BIST equities. Exchange/settlement fees are immaterial
  (≤0.02%).
* **Short selling.** Permitted again since 26 Jun 2026, **BIST-50 only**,
  requiring a margin agreement and ÖPSP/internal borrow at an undisclosed fee.
  The **uptick rule** activates whenever BIST-100 falls >2% and was active as
  recently as **2 Sep 2026** — it forbids shorting on a downtick, i.e. exactly
  when a short signal fires.
* **Execution APIs.** AlgoLab (DenizBank), the only documented retail
  order-entry API, **closed 31 Dec 2025**. No public retail API found at İş
  Yatırım, Garanti, Gedik, QNB, or Midas. Matriks offers FIX but as an
  institutional product. **There is currently no documented free retail
  execution API in Turkey.**
* **VIOP single-stock futures** (~0.18% round trip, no borrow, no uptick rule)
  are the only structurally coherent short route; liquidity per name is
  unverified.
* **KAP** has a REST API but access is via commercial arrangement; the public
  site refreshes roughly every 3 minutes.

## 6. Methodological debt — read this before trusting anything above

**The trial budget is blown.** Bailey & López de Prado's minimum-backtest-
length result implies that with 2 years of data and a target Sharpe of 1, only
about **7 independent configurations** may be tried before an in-sample
success with zero out-of-sample value becomes essentially certain (~45 with 10
years). This study ran well over 40. Every number here should be read as a
hypothesis to be validated forward, not a measurement.

Harvey, Liu & Zhu's threshold for a new factor is **t > 3.0**, not 2.0. The
referee in `referee.py` uses 3.0 for that reason.

### The three bugs found in my own work this session

1. **Joined two differently-adjusted series.** The gap was computed from daily
   bars and the outcome from hourly bars; the vendor adjusts them differently,
   and they disagreed by >1% on 44.6% of days. Fixed by deriving everything
   from one frame per ticker.
2. **Assigned unresolvable outcomes to the loss.** When both bracket levels
   fall inside one bar the order is unknowable; forcing them to losses
   manufactured the original negative result, and forcing them to wins would
   manufacture a positive one. Now excluded and the share reported.
3. **Ranked liquidity across a decade of nominal Turkish lira.** TL turnover
   grows with inflation, so a pooled turnover ranking sorted *dates*, not
   stocks: the "most liquid quintile" was simply recent days. This inverted the
   conclusion — the pooled version said the overnight trade cleared costs in
   liquid names (+0.126%/day); ranking within each day says it loses in every
   decile. Fixed by ranking cross-sectionally per day.

Twice in this session an automated pass/fail line printed a verdict that the
numbers on the same line contradicted. Both times the number was right and the
verdict was wrong.
