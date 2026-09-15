# What we learned, and what to do about it

A consolidation of everything measured and researched in this project: the
original crypto bot, the BIST equity study, the public bot landscape, the
market-mechanics research, and the methodology debt incurred along the way.

Data cut-off: 7 September 2026.

---

## The one-paragraph answer

Across two markets, four strategy families and roughly fifty tested rules,
**nothing that trades frequently survived its own transaction costs.** The
effects are not absent — several are real, statistically overwhelming, and
match published literature. They are simply smaller than the toll. What did
survive is unglamorous: holding assets, sized sensibly, rebalanced rarely or
not at all. Separately, the research established that Turkey currently offers
a genuinely unusual risk-free real return on TL deposits, which is a better
answer to "how do I grow my money" than any bot in this repository.

---

## 1. The governing arithmetic

Every result in this document is a consequence of one relationship:

```
required win rate  =  50%  +  cost / (2 × target)
```

| profit target | required accuracy (at 0.2% cost) |
|---|---|
| 1% | **60.0%** |
| 2% | 55.0% |
| 5% | 52.0% |
| 10% | 51.0% |
| 30% (hold for months) | 50.3% |

The cost is fixed; your target is not. This is why frequent small-target
trading loses and patient holding wins — **the same signal quality is
profitable at a large target and ruinous at a small one.** Measured accuracy
on BIST intraday direction was 46.5–47%. Required: 60%.

---

## 2. What was tested and what happened

### Crypto (BingX perpetual futures, the original bot)

| tested | result |
|---|---|
| 6-cluster indicator confluence, horizons 30 min → 14 days | no \|t\| > 2 anywhere; best cluster ≈ 1 bps alpha vs 26 bps cost |
| limit orders at 5 offsets instead of market | adverse selection ate the saving; +0.1%/yr |
| 5 volume tiers + 8 commodity/index instruments | no tier or asset class produced an edge |
| 114-symbol point-in-time universe | tier-C trend following t=+3.48 → **+0.96** once selection was point-in-time |
| shock reversion | year 1 t=+3.66, year 2 t=−0.71 |
| MA trend filters | look-ahead bug; corrected, 12/12 symbols lost to buy-and-hold |
| drawdown brake | re-entry bug; corrected, the brake **hurt** (+13.5% vs +55.5%) |
| **buy and hold BTC 0.7 / ETH 0.3, 2 years** | **+30.6%** |
| **the same, monthly rebalanced (shipped allocator)** | **+34.6%** (+16.0%/yr), maxDD −58.2%, 38 trades |

Total engineering contribution over simply holding: **~+2.1%/year.** Any
leverage would have been liquidated (−58.2% drawdown × 2 = −116%).

### BIST equities (136 tickers, 10 years daily + 2 years hourly + 60 days 5-minute)

| tested | gross | verdict |
|---|---|---|
| buy at open, take +1%, no stop | −0.10%/trade | the user's original plan: 10,000 TL → **228 TL**/yr |
| ±1% bracket, 6 TP/SL variants | all negative | stops did not rescue it |
| 12 technical entry rules (RSI, momentum, reversal, MA, volume) | best t=+1.35 | none moved the 46.5% hit rate |
| news proxy: buy the +3% gap-up | −0.686%, t=−8.79 | **strongly, consistently negative** |
| opening gap fade (short the gap-up) | +0.413%, t=+14.8 | died on inspection — see §3 |
| overnight (buy close, sell open) | +0.247%/day, t=+105 | died on the spread — see §3 |
| the literature's own 5-item shortlist | best +0.056%/trade | all below the cheapest possible cost |
| 12-1 month momentum, top 15, monthly | +49.8%/yr | beats buy-and-hold by +3.4pp at 5.9× turnover |
| **buy and hold, equal weight, 10 years** | **+46.4%/yr** | beat everything except momentum |
| equal weight, monthly rebalanced | +45.8%/yr | **rebalancing subtracts 0.6pp on BIST** |

All BIST levels are **optimistic**: delisted companies are absent from the data
source, so survivorship bias inflates every long-only number.

---

## 3. The two candidates that looked real, and why they died

These are the most instructive results in the project, because both were
statistically overwhelming and both were wrong for reasons no t-statistic could
have revealed.

### The opening gap fade

Stocks gapping up more than +3% fade intraday: +0.413% per trade, t=+14.80,
68.6% win rate, positive in both halves of the sample and in 73 of 102 tickers.

**It lives inside the opening print.** Delaying entry destroys it:

| entry | gross | t |
|---|---|---|
| the opening print | +0.416% | +15.08 |
| +1 hour | +0.054% | +2.02 |
| +2 hours | −0.033% | −1.30 |

On gap days the opening print is the day's high 13.5% of the time and the day's
low 14.3% — double the 6.4%/7.1% base rate. The price we were measuring from
sits outside the real trading range far more often than a fair open should.

Della Corte, Kosowski & Wang measure the identical trade on US stocks and find
the same decay, only faster: entry at 09:30:01 → 0.36%/day, 09:31 → 0.11%,
09:45 → 0.04%. **A 69% decay in one minute.**

**And it is logically untradeable.** The gap is only known once the opening
auction prints. The moment you learn a stock gapped +3%, the price you needed
is history.

**And it could not have been traded anyway.** Turkish short selling was fully
banned 6 Feb 2023 → 2 Jan 2025, again 23 Mar – 29 Aug 2025, and again
2 Mar – 26 Jun 2026 — roughly 13 of the sample's 24 months.

### The overnight return

The strongest statistical result in the whole project:

| leg | mean/day | t | annualised |
|---|---|---|---|
| overnight (close→open) | +0.2476% | **+105.1** | +86.5% |
| intraday (open→close) | −0.0779% | −16.1 | −17.8% |
| close→close | +0.1659% | +32.0 | +51.8% |

Positive in **all ten years**. Not a dividend-adjustment artefact (raw prices:
+0.2614%). Survives the delay test that killed the gap fade — 106% of it
remains one hour after the open, so it is not an opening-print artefact either.

**It dies on the spread.** Ranking by turnover *within each day*:

| liquidity decile | gross | spread (1 tick) | net | annualised |
|---|---|---|---|---|
| least liquid 20% | +0.2427% | 0.900% | −0.657% | −81.0% |
| 60–80% | +0.2457% | 0.525% | −0.280% | −50.6% |
| **most liquid 10%** | **+0.2287%** | **0.302%** | **−0.073%** | **−16.9%** |

The gross effect is nearly constant across liquidity; only the toll varies. Even
in the most liquid decile, assuming the spread is exactly one tick — the
theoretical minimum — it loses 16.9%/yr before any commission. A live basket
simulation of the 20 most liquid names at **zero commission** returns
**−25.2%/yr** against **+31.7%/yr** for simply holding them.

This is what the literature means when it calls the overnight return a
microstructure artefact: the open prints on the offer side, and the "return" is
the spread that morning buyers pay. Berkman et al. (JFQA 2012) say so
explicitly — they document a **cost borne by buyers**, not an alpha.

---

## 4. Why speed cannot be the answer

The intuition that a fast, tireless bot reading news should be able to catch a
1% move fails on measured facts, not opinion.

**The news is priced before it is news.** Kurov et al. (JFQA 2019): prices move
in the correct direction ~30 minutes *before* official macro releases, and
pre-announcement drift accounts for **~40% of the total adjustment**. The rest
completes in seconds (Brogaard/Hendershott/Riordan, RFS 2014).

**The speed race has a 5–10 microsecond winning margin.** Aquilina, Budish &
O'Neill (QJE 2022), from LSE message data at 100 ns resolution:

| quantity | value |
|---|---|
| minimum viable reaction (HFT + matching engine) | **29 µs** |
| modal margin by which the winner beats the first loser | **5–10 µs** |
| races per symbol per day | 537 |
| share of races won by the top 6 firms | **82%** |

A retail bot on a broker REST path reacts in ~100–500 ms. At 250 ms that is
**~8,600× the minimum reaction time and ~30,000× the winning margin.** This is
not "slower" — it is outside the decision window by four orders of magnitude.

**And retail flow is the preferred counterparty.** Baron, Brogaard, Hagströmer
& Kirilenko (JFQA 2019), E-mini S&P 500: aggressive HFTs earn **90.67%
annualised alpha**, and *"Fundamental traders incur the least cost to HFTs,
while Small traders incur the most."* A retail speed bot is not a marginal
loser in the race; it is what the race feeds on.

**The direction is wrong too.** The tradable short-horizon news effect is
*reversal*, not continuation: 31% of an extreme one-minute move reverses in the
next minute, strongest in the most liquid names. Our own −0.686% for buying
gap-ups is the literature reproducing itself.

**On the LLM claims.** Lopez-Lira & Tang's widely-cited paper reports 93.3%
overnight direction accuracy — but the authors themselves label that *"the
non-tradable initial reaction."* The tradable drift is 34 bps/day **before
costs**, and they write it is *"only feasible for market participants whose
transaction costs are sufficiently low, such as market makers."* Their own
gross Sharpe decays **6.54 (2021Q4) → 3.68 → 2.33 → 1.22 (2024)**. A 2026
replication with strict annual training cutoffs (DatedGPT) measured the
contamination directly: **26.4 bps per standard deviation of pure look-ahead
leakage**, significant at 1%.

---

## 5. A Turkish-specific trap: VBTS

BIST's Volatility Based Measures System applies escalating restrictions, each
for **one month**: short-sale and margin ban, gross settlement (no same-day
round trip), market-order ban, and finally **single-price call-auction-only
trading**.

A strategy that hunts +1% intraday moves selects precisely the names that trip
VBTS. **The strategy's own success criterion is correlated with its own
operational shutdown.** This is not a cost to model; it is a structural
incompatibility.

Also relevant: daily price limits (±20% Stars, ±15% Main, ±10% Sub), an
instrument circuit breaker at 10/7.5/5% deviation, and a market-wide 10-minute
halt when BIST-100 falls 6%.

---

## 6. The public bot landscape

Twelve frameworks surveyed with live GitHub API data (7 Sep 2026).

| framework | stars | last commit | assets | note |
|---|---|---|---|---|
| Freqtrade | 54,094 | active | **crypto only** | docs disclaim slippage; only one shipping a look-ahead detector |
| NautilusTrader | 28,517 | active | crypto + IB equities | same engine in backtest and live; most honest architecture |
| Backtrader | 23,150 | **Apr 2023 — dead** | any | do not start new work on it |
| QuantConnect LEAN | 21,499 | active | equities/options/futures | **best reality modelling**; survivorship-free US data |
| Hummingbot | 19,872 | active | crypto only | market making |
| backtesting.py | 8,937 | weak | single asset | **open look-ahead bug in its own ATR example (#963)** |

**What the claims look like up close.** Freqtrade's documentation states
verbatim that *"all orders are filled at the requested price (no slippage)"*
and that *"stoploss exits happen exactly at stoploss price, even if low was
lower."* Both flatter results. NostalgiaForInfinity, the most-run public
strategy, publishes **no returns, no backtest period, no cost assumptions** and
is patched dozens of times a day with numbered "protections" — continuous
in-sample refitting. Hummingbot's headline "10–50% from liquidity mining" is
**their own simulation**, with no observed returns and no discussion of adverse
selection. The two highest-starred trading repos created since 2024 are LLM
demos with **165,000 combined stars and no track record**.

**Verified fraud patterns** (CFTC releases 8524-22, 8693-23): promises of
*"8 to 25% per month"* with *"minimal risk"*, fabricated account statements,
Ponzi payments — and in one action, **twelve entities using the identical fake
NFA registration number.**

### Red flags for any bot claim

1. Costs not itemised — the number is gross and meaningless.
2. Sample under a year, or one that ends today, or one wholly inside a bull run.
3. No out-of-sample split and no live track record.
4. Continuous parameter patching — every "fix" is an in-sample refit.
5. **The vendor is selling the bot rather than trading it.** A real 34%/quarter
   edge is never for sale.
6. Monthly return percentages ("8–25% per month") — the exact CFTC phrasing.
7. Backtest universe = today's listed assets (survivorship bias).
8. Simulation cited as evidence, including by the framework's own vendor.
9. GitHub stars treated as validation.

---

## 7. Where a small account genuinely has an advantage

This is the constructive half, and it is well documented.

**Capacity.** Frazzini, Israel & Moskowitz computed break-even fund sizes from
~$1trn of live AQR executions: size $103bn, value $83bn, momentum $52bn
(US; higher globally). A retail account is seven to nine orders of magnitude
below these — it earns the **gross** return where an institution earns the net.

**Impact.** Their *average* trade is 1.2% of daily volume. Below ~0.1% of ADV
the square-root impact law puts market impact in the low single-digit basis
points. For a BIST-100 name with ~$20m ADV, that ceiling is roughly $20k–200k
per name.

**No career, benchmark or redemption risk.** Shleifer & Vishny's limits of
arbitrage bind professionals precisely when mispricing is widest; Scharfstein &
Stein show herding is individually rational under career concerns. You cannot
be fired for tracking error and no investor can pull your capital at the bottom.
This is the least replicable retail advantage that exists.

**Slow information beats fast information — for you.** Foucault, Hombert & Roşu
(JF 2016) prove the fast speculator trades *less* aggressively on long-run value
and makes *less* profit from it; the slow speculator's value-trading profit
remains strictly positive. Speed and long-horizon informational profit are
substitutes.

**BIST is less crowded than developed markets.** HFT participates in ~6% of
BIST orders versus 24–43% of EU value traded. The latency race is materially
less contested here — which matters only for strategies that are not latency
races to begin with.

**Turnover is the binding constraint.** Novy-Marx & Velikov: strategies with
**under 50% one-sided monthly turnover** still deliver significant net returns;
high-turnover strategies cost over 1% per month. Short-term reversal alone costs
48.4 bps/month to trade, against 5.5–5.7 bps for value and size.

---

## 8. Turkey, September 2026 — the numbers that actually matter

| | value | source |
|---|---|---|
| CPI inflation, Aug 2026 | **31.51% y/y** | TÜİK via TCMB |
| TCMB 1-week repo | **37.00%** (unchanged since Jan 2026) | TCMB |
| USD/TRY | 48.23 (4 Sep 2026) | TCMB |
| TL depreciation, last 12 months | **17.4%** — *below* inflation | TCMB |

**Real after-tax returns by instrument:**

| instrument | net nominal | tax | **real** |
|---|---|---|---|
| **TL deposit, 1 year** | ~35–38% | 5% stopaj | **+3% to +5%** |
| TL deposit, 3 month | ~36–38% | 10% stopaj | +4% to +5% |
| USD deposit | 3–4% USD + FX | **25%** on interest | **≈ −11% last 12m** |
| **BIST-100 equity** | — | **0% capital gains** | **~+3%/yr long run** |
| foreign equity (Midas etc.) | USD ~8% + FX | **progressive to 40%** | best long-run, worst tax |

Two facts deserve emphasis:

**BIST-100 has barely beaten inflation over 20 years** (+19.9%/yr nominal vs
~19%/yr inflation; ~+3%/yr real including dividends) and in **USD terms it has
gone nowhere for two decades** — 259 in 2006, 291 in 2026. Turkish equity is an
inflation hedge, not a compounder.

**TCMB is currently running a positive real policy rate of about 4%**, which
Turkey almost never offers. A 1-year TL deposit nets roughly +3–5% real,
essentially risk-free. That is a better expected outcome than every strategy in
this repository, and it required no code. It is also fragile — it evaporates
the moment the FX regime changes.

**Turkey's tax code pays you to hold domestic equity**: 0% capital gains on
BIST-listed shares for residents, against up to 40% on foreign shares and 25%
withholding on FX deposit interest.

---

## 9. Sizing and risk — what the evidence supports

**Kelly.** `f* = (μ − r)/σ²`. Growth `g(f) = r + f(μ−r) − f²σ²/2` is zero at
`2f*` and negative beyond: overbetting by 2× turns a positive edge into
guaranteed ruin. Because μ is estimated with standard error `σ/√T`, `f*`
inherits that error linearly while the penalty is quadratic — hence half-Kelly
or less in practice. **With a measured edge indistinguishable from zero, Kelly's
answer to the trading bot is to size it at zero.**

**Leverage.** Same parabola: growth is maximised at `L* = (μ−r)/σ²` and turns
negative near `2L*`. For crypto (σ ≈ 70%, μ−r ≈ 30%) that gives `L* = 0.6×` —
**less than unlevered is growth-optimal.** This is why levered crypto decays.

**Rebalancing.** Diversification return `DR = ½(Σwᵢσᵢ² − σ²ₚ)`. It **adds**
when assets share similar drifts with high individual volatility and low
correlation; it **subtracts** when one asset has a persistently higher drift,
because you systematically sell the compounder to buy the laggard. This exactly
explains our two results: crypto assets share a drift with huge idiosyncratic
volatility (rebalancing helped, +2.0%/yr), while Turkish equities carry a large
persistent inflation-driven drift (rebalancing hurt, −0.6pp/yr). **Rebalance on
a threshold band, not a calendar, and never rebalance a high-drift asset
against a low-drift one.**

**Stop losses** (Kaminski & Lo). Under an i.i.d. random walk stops strictly
subtract. They add value only under positive autocorrelation or persistent bad
regimes, and are actively destructive under mean reversion. On a long-term
holding, a stop is a tax.

**Volatility targeting.** Moreira & Muir's alphas come from an in-sample
spanning regression; Cederburg et al. show the out-of-sample gains largely
vanish for most factors. What survives is vol targeting on a single long
position — it works because volatility is autocorrelated while returns are not.
It reduces drawdowns; it does not add alpha.

**Diversification.** `σ²ₚ → σ̄²[ρ̄ + (1−ρ̄)/N]`. At ρ̄ = 0.3, twenty names
capture ~87% of the achievable variance reduction. The binding constraint for a
Turkish investor is not the number of stocks — every BIST name shares one
country and currency factor, and a TL salary is already a 100% long position in
Turkey.

---

## 10. Methodology debt — read before trusting anything above

**The backtest budget is spent.** Bailey & López de Prado's minimum-backtest-
length result implies roughly **7 independent configurations** on a two-year
sample before an in-sample winner with zero out-of-sample value is essentially
guaranteed (~45 on ten years). This project ran well past that. Harvey, Liu &
Zhu's threshold for a new factor is **t > 3.0**, not 2.0 — `research/bist/
referee.py` enforces 3.0 for that reason.

**More backtesting on these samples cannot produce more knowledge.** Anything
further must be validated forward, in real time.

### Bugs found in our own analysis, all of them flattering

1. **Joined two differently-adjusted price series.** The gap was computed from
   daily bars and the outcome from hourly bars; the vendor adjusts them
   differently and they disagreed by >1% on 44.6% of days.
2. **Assigned unresolvable outcomes to the loss.** When both bracket levels fall
   inside one bar the order is unknowable; forcing them to losses manufactured
   the original negative result. Now excluded and the share reported.
3. **Ranked liquidity across a decade of nominal lira.** TL turnover grows with
   inflation, so a pooled turnover ranking sorted *dates*, not stocks — the
   "most liquid quintile" was simply recent days. **This inverted a
   conclusion**: pooled, the overnight trade appeared to clear costs in liquid
   names (+0.126%/day); ranked within each day, it loses in every decile.
4. **Two look-ahead bugs** in exploratory scripts, one of which produced a
   +2,473,980% return before correction.

Twice in this session an automated pass/fail line printed a verdict the numbers
on the same line contradicted. Both times the number was right.

---

## 11. What follows from all this

**Do not build.** Any intraday BIST strategy. Anything requiring Turkish short
selling. Anything whose edge is measured in single basis points against a
0.1–0.45% toll. Any bot whose thesis is speed.

**Do build.** A forward paper-trading validator — the only instrument that can
add information now — and finish the operational gaps in the allocator, the one
component that survived measurement. See the plan file for specifics.

**Consider not building at all.** The honest comparison, as of September 2026:

| option | expected real return | effort | risk |
|---|---|---|---|
| TL deposit, 1 year | **+3% to +5%** | none | FX regime change |
| BIST buy & hold (0% CGT) | ~+3%/yr | none | −38% drawdowns |
| the crypto allocator | +16%/yr nominal | built | −58% drawdown |
| any intraday bot in this repo | **negative** | very high | total loss |

The most valuable thing this research produced is not a strategy. It is the
ability to tell, quickly and cheaply, that a proposed strategy will not work —
before any money is at risk.
