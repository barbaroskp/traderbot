# Correctness pass — what changed and why

Companion to `POSTMORTEM.md`. Every item below is a defect fix or a
cost-accounting correction, not a strategy opinion. Behavioural thresholds are
marked as provisional: they are meant to be calibrated against measured forward
returns, not tuned to hit a trade count.

Test suite: **172 passing**, including 28 new regression tests in
`tests/test_regressions.py` that pin each fix.

---

## 1. Config integrity — the defect that actually caused the loss

| Change | File |
|---|---|
| `extra="ignore"` → `extra="forbid"` | `src/config.py` |
| `.env` files rewritten to the real field names (`*_PCT`, not `*_USDT`) | `.env.example`, `.env.optimized` |

Commit `00ee744` renamed the exposure caps from fixed USDT amounts to fractions
of balance, but both `.env` files kept the old names. Pydantic silently dropped
them and applied the defaults, so an intended **8 USDT total exposure became 80%
of balance** — up to ~8x balance in notional at 10x leverage. Nothing errored
and nothing logged.

`extra="forbid"` now rejects any unrecognised variable at startup, and
`TestEnvConfigSync` fails the build if a `.env` file drifts from `config.py`
again.

## 2. Cost accounting — every layer was optimistic

| Change | File |
|---|---|
| `fee_rate_bps` 4.0 → **5.0** (BingX taker; both entry and exit are taker) | `src/config.py` |
| `slippage_assumption_bps` 3.0 → **8.0** | `src/config.py` |
| New `use_funding_cost` / `funding_interval_hours` / `default_funding_rate_bps` | `src/config.py` |
| New `round_trip_cost_bps` property (2×fee + 2×slippage) | `src/config.py` |
| Funding deducted on **every** close path (paper, 4 live paths, reconcile, backtest) | `execution.py`, `scheduler.py`, `backtest.py` |
| Exit slippage now charged (stop/TP orders cross the book too) | `execution.py`, `backtest.py` |
| Backtest entry filled at mid **with slippage** instead of at mid exactly | `backtest.py` |
| Backtest spread from config instead of a hardcoded 5bps | `backtest.py`, `config.py` |
| TP floor must clear the **full** round trip, not just fees | `execution.py` |
| `positions` gains `funding_rate`, `entry_fee`, `exit_fee`, `funding_fee`, `exit_price` | `storage.py` |
| Live fills now written to the `fills` table (was paper-only) | `execution.py` |

Real friction is ~26 bps per round trip; the bot assumed 8 bps. Funding was not
deducted anywhere at all, despite holds of up to 3 hours (scalp) and 24 hours
(swing — three funding events).

## 3. Signal correctness

| Change | File |
|---|---|
| `finalize_klines()` drops the in-progress candle; applied to all three fetchers | `marketdata.py` |
| `higher_tf_limit` / `swing_trend_limit` 50 → 60 | `config.py` |
| `_normalize_weighted_score` counts opposing votes in numerator **and** denominator | `strategy.py` |
| Breadth factor: score scaled by share of clusters agreeing | `strategy.py` |
| Clusters regrouped so mean-reversion and trend-following are separable | `strategy.py` |
| `sentiment` demoted to bonus-only | `strategy.py` |
| `_get_min_confluence` returns the gate actually enforced, and risk states raise it | `strategy.py` |

**Repainting:** indicators were computed over the still-forming candle, so
`closes[-1]` and `volumes[-1]` changed on every poll. `volume_ratio` was
structurally understated early in a bar and only spiked near its close. The
backtest walks closed candles, which is a large part of why backtest results
never transferred to live.

**Inverted conviction:** the score divided by the max weight of the *winning*
side only, so two full-weight indicators scored **81.8** while eight indicators
(three partial) scored **77.7**. Leverage tiers and the high-conviction margin
multiplier keyed off that number, so the bot sized **up** into its **thinnest**
evidence. Opposing votes now reduce the score, and breadth raises it.

**Contradiction:** the `trend` cluster contained both `ema_zscore` (mean
reversion: price below the fast EMA is a BUY) and `trend`/`adx` (trend
following: price above EMA50 is a BUY). Two incompatible theories of price,
resolved by an arbitrary internal majority that hid the disagreement from the
confluence count. Verified against live data after the fix: `oscillator` votes
LONG while `trend` votes SHORT, the disagreement is now visible, and the trade
is correctly blocked.

## 4. Bugs

| Bug | File |
|---|---|
| `exit_reason = "ANTI_LIQUIDATION"` was overwritten by an unconditional `exit_reason = ""` 23 lines later — anti-liquidation never fired in paper mode | `execution.py` |
| Paper SL/TP compared only the latest mark price, missing any breach that reverted between polls (a live exchange stop would have filled) | `execution.py`, `marketdata.py` |
| Rejected signals returned `None`, so `reject_reasons` was always empty and the operator could never see **why** no trades happened | `strategy.py` |
| Backtest filled a full bar after the signal, at a price that already contained that bar's move | `backtest.py` |
| Kelly clamped up to `kelly_min_fraction` on a negative edge, so the bot kept betting while its own history said it was losing | `risk.py` |
| Expectancy guard only checked R:R ≥ 1.0, which says nothing about expectancy | `scheduler.py` |

The empty `reject_reasons` histogram matters more than it looks: it is why the
filters were loosened blind. `SL/TP` detection now uses the interval high/low
via `fetch_recent_extremes()`, and resolves ambiguous cases (both levels touched
in one interval) against us.

## 5. Defaults — provisional, to be calibrated with data

Frequency is the dominant cost driver: at ~26 bps per round trip, 80 trades/day
burns 9-22% of balance per day before the strategy contributes anything.

| Setting | Was | Now |
|---|---|---|
| `max_trade_margin_pct` / `max_total_margin_pct` | 0.20 / 0.80 | 0.10 / 0.30 |
| `leverage` / `max_leverage_allowed` | 3 / 7 | 2 / 3 |
| `dynamic_leverage_enabled` | true | **false** |
| `max_open_positions` | 5 | 2 |
| `cooldown_minutes` | 10 | 60 |
| `scan_interval_minutes` | 3 | 5 (matches the 5m candle) |
| `shortlist_size` | 300 | 25 |
| `min_volume_24h_usdt` | 1M | 50M |
| `max_spread_bps` | 30 | 8 |
| `min_depth_usdt` | 1,000 | 25,000 |
| `min_cluster_confluence` | 2 of 6 | 4 of 6 |
| `entry_threshold_bps` | 15 | 30 |
| `risk_state_disabled` | true | **false** |
| `soft_kill_switch_enabled` | false | **true** (15% drawdown) |
| `selector_lenient_enabled` | true | **false** |
| `kelly_min_fraction` | 0.05 | **0.0** |

Scanning 300 symbols × 23 indicators × 480 cycles/day is ~3M hypothesis tests a
day; at that scale "signals" are found whether or not they exist.

## 6. Tests

The old suite asserted the settings that lost the money (`leverage == 3`,
`max_spread_bps == 30.0`, `min_cluster_confluence` effectively 2). Those
assertions were updated, and the per-indicator vote tests now pin their own
thresholds explicitly so they test **vote logic** rather than the production
entry bar.

New in `tests/test_regressions.py`:

- `.env` ↔ `config.py` field-name sync, and rejection of unknown variables
- retired `*_USDT` names cannot come back
- `finalize_klines` drops the in-progress candle
- weighted score falls when opposition appears, never goes negative
- no cluster mixes `ema_zscore` with `trend`/`adx`; every weighted indicator is
  assigned; market-wide sentiment cannot vote; confluence needs a majority;
  risk states raise the bar
- funding charged per interval, signed by side, treated as a cost when unknown,
  disable-able
- TP floor clears the full round trip; `round_trip_cost_bps` counts both sides
- anti-liquidation survives to the close; stop touched between polls is
  detected; both-levels-touched resolves against us; close charges funding and
  exit slippage
- Kelly returns 0.0 and blocks sizing on a proven negative edge
- expectancy gate uses full cost, not fees alone

---

## What this does and does not do

These changes make the bot **measure honestly and lose slowly**. They do not
create an edge. The strategy's gross predictive power is still unknown, because
until now no component measured it correctly: the backtest paid no slippage, the
indicators repainted, and paper mode forgave stops that live would have taken.

The next step is the experiment, not more tuning: replay the corrected signal
over historical data and measure forward returns conditional on a signal against
the unconditional baseline. If the edge does not exceed ~26 bps per round trip,
no parameter set will save it and the strategy needs replacing rather than
adjusting.
