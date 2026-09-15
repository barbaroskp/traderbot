# BingX Agent — trading bot, and the measurements that judge it

Autonomous trading bot for **BingX Perpetual Futures (Swap V2)**: 24 technical
indicators grouped into 7 voting clusters, adaptive risk management, and
paper/live dual-mode execution.

Alongside it, a research harness that replays the shipped decision engine over
real history and answers the only question that matters — **does it make
money?**

> **New here? Read [BOT-NASIL-CALISIR.md](BOT-NASIL-CALISIR.md)** (Turkish) for a
> plain-language explanation of how the bot works and what was measured.

## What the measurements say

The bot's own code, replayed on 89 BingX symbols over 41 days of real
5-minute and hourly prices, with fees, slippage and funding charged:

| configuration | 10,000 → |
|---|---|
| current config (post-refactor) | **8,956** (−10.4%) |
| config as first shipped | **34** (−99.7%) |

The second line is the one to sit with: that configuration won **29% of 4,122
trades** on its way to zero, and its trades were already losing *before*
commission (−5,798 gross, −2,778 fees).

Everything tried to rescue it, and the result of each:

- all **24 indicators** scored individually across **93,193 votes** — none
  clears its own transaction cost
- **16 parameter sets** fitted on one half of the sample and scored on the
  other — none profitable in both
- **1,008 independent rules** searched; correlation between in-sample and
  out-of-sample performance is **−0.037**, i.e. the winners could not have
  been picked in advance
- **7 timeframes** from 1-minute to daily — the only statistically solid
  signal was daily MACD, and it lived only in coins too illiquid to trade

What survived measurement was holding a basket and rebalancing rarely
(`src/allocator.py`). Full detail in
[RESEARCH-SYNTHESIS.md](RESEARCH-SYNTHESIS.md).

**Read the research before trusting any backtest in this repo.** Four separate
findings looked strong here and collapsed under scrutiny, two of them because
of bugs in my own analysis code. All four are documented.

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌──────────────────┐
│  Universe    │────▶│  Selector    │────▶│  Strategy        │
│  Discovery   │     │  (3-stage)   │     │  24 indicators   │
└─────────────┘     └──────────────┘     │  → 7 clusters    │
                                          └────────┬─────────┘
     ┌──────────────┐     ┌──────────────┐         ▼
     │  Risk Mgr    │◀───▶│  Execution   │◀── Signals
     │  (FSM)       │     │  Paper/Live  │
     └──────────────┘     └──────┬───────┘
                                 │
                    ┌────────────▼────────────┐
                    │  SQLite + Portfolio     │
                    │  (positions, PnL, logs) │
                    └─────────────────────────┘

        research/replay/  replays all of the above over history
```

## Quick Start

### 1. Clone & Install

```bash
cd bingx_agent
cp .env.example .env
# Edit .env with your API credentials

pip install -e ".[dev]"
```

### 2. Validate Configuration

```bash
python -m src.cli validate-config
```

### 3. Run Paper Trading (Default)

```bash
python -m src.cli run-paper
```

### 4. Run via Docker

```bash
cp .env.example .env
# Edit .env

docker-compose up -d
docker-compose logs -f bingx-agent
```

### 5. Check Status

```bash
python -m src.cli status
```

### 6. Export Data

```bash
python -m src.cli export-csv --table trades -o trades.csv
python -m src.cli export-csv --table pnl
python -m src.cli export-csv --table signals
python -m src.cli export-csv --table orders
```

## Configuration

All settings via environment variables or `.env` file:

| Variable | Default | Description |
|---|---|---|
| `BINGX_API_KEY` | (required) | BingX API key |
| `BINGX_API_SECRET` | (required) | BingX API secret |
| `PAPER_MODE` | `true` | Paper trading mode |
| `ALLOW_LIVE_TRADING` | `false` | Must be `true` for live |
| `INITIAL_CAPITAL_USDT` | `50` | Starting capital |
| `MAX_TOTAL_NOTIONAL_USDT` | `10` | Max total exposure |
| `MAX_TRADE_NOTIONAL_USDT` | `3` | Max per-trade size |
| `PER_TRADE_FRACTION` | `0.03` | Fraction of balance per trade |
| `LEVERAGE` | `1` | Default leverage |
| `MARGIN_MODE` | `ISOLATED` | Margin mode |
| `SCAN_INTERVAL_MINUTES` | `10` | Scan frequency |
| `FAST_EMA` / `SLOW_EMA` | `9` / `21` | EMA periods |
| `ENTRY_THRESHOLD_BPS` | `25` | Signal threshold (bps) |
| `TP_BPS` / `SL_BPS` | `40` / `30` | Take-profit / Stop-loss (bps) |
| `MAX_OPEN_POSITIONS` | `2` | Max concurrent positions |
| `MAX_HOLD_MINUTES` | `120` | Position timeout |
| `COOLDOWN_MINUTES` | `20` | Post-trade cooldown |

## Strategy

**Cluster confluence.** 24 indicators are grouped into 7 families; each family
must agree internally before it casts one vote, and enough families must agree
before a signal exists.

| cluster | what it reads | members |
|---|---|---|
| oscillator | overbought / oversold | rsi, stoch_rsi, williams_r, rsi_divergence |
| mean_revert | distance from a fair level | ema_zscore, bollinger, vwap, volume_profile |
| trend | direction and strength | trend, adx, macd, momentum |
| volatility | compression and breakout | squeeze, breakout, volume_spike, velocity, volume_profile_break |
| orderflow | book pressure | orderbook, taker_ratio, whale |
| flow | positioning | obv, open_interest, liq_cascade |
| contrarian | sentiment (bonus only) | sentiment |

Mean-reverting families (oscillator, mean_revert) and trend-following families
are deliberately in **separate clusters**: an earlier version mixed them, so
"price is stretched, expect reversion" and "price is moving, expect
continuation" could cancel inside one vote and the engine would take the
resulting noise as consensus.

Two resolution modes:

- `thesis` (default) — one family leads and the others may only confirm or veto
- `vote` — all families vote and the majority wins (the original behaviour)

Exit: **TP**, **SL** (both sized from ATR), or **timeout**
(`max_hold_minutes`). The TP floor is the full round-trip cost plus
`min_tp_net_bps`, so a take-profit cannot book a net loss.

**Measured result of all this: see the top of this file.** The architecture is
sound; the signal is not.

## Risk Management

Three-state risk machine that **never stops** the bot:

| State | Trigger | Effect |
|---|---|---|
| **NORMAL** | Default | Standard thresholds |
| **TIGHT** | 3 losses / 5% DD / high API errors | +50% entry threshold, 50% notional, +50% cooldown |
| **ULTRA_TIGHT** | 5 losses / 10% DD / very high errors | Top 10 only, 0.5-1 USDT max, low-vol only |

Automatic de-escalation when consecutive wins and stable cycles accumulate.

## Improving profitability — what was tried, and what happened

This section used to suggest tuning: stricter confluence, better risk–reward,
less overtrading. All of it was then measured, and none of it worked. Keeping
the original advice here would be misleading, so here are the results instead.

Each variant was fitted on the first half of the sample and scored on the
second:

| variant | fit | test |
|---|---|---|
| baseline | −3.51% | −4.71% |
| confluence 4 or 5 | −3.51% | −4.71% |
| entry threshold 60 / 100 bps | −5.20% | −7.55% / −8.94% |
| hold 60m / 360m / 720m | −4.66% … −3.31% | −4.35% … −3.69% |
| leverage 1x / 2x / 3x | −1.77% / −3.51% / −5.24% | −2.38% / −4.71% / −6.99% |
| wider TP, tighter SL, more slots | −2.91% … −3.77% | −4.27% … −4.62% |

**Positive in both windows: 0 of 16.**

Two patterns are worth extracting:

1. **Loss scales with trade count.** 23 trades → −0.19%; 79 → −4.71%; 166 →
   −8.94%. Each trade has negative expectancy, and parameters only change how
   many of them you take.
2. **Leverage is monotonically harmful.** It creates no edge; it multiplies
   the one that is already negative. The growth-optimal leverage for the
   surviving basket strategy is `L* = (μ−r)/σ² ≈ 1.05x`, and growth turns
   negative at 2.10x.

Run them yourself:

```bash
python -m research.replay.sweep --days 40        # the table above
python -m research.replay.attribution            # all 24 indicators scored
python -m research.replay.search --days 40       # 1,008 independent rules
python -m src.costs 0.10                         # does YOUR broker clear it?
```

### Optional LLM Advisor (Ollama)

Still available and still off by default. Note that it can only filter the
signals above, and the measurement says those signals carry no edge to filter.

### Optional LLM Advisor (Ollama)

Bot, her sinyali çalıştırmadan önce yerel bir LLM’e sorabilir (onay / red / küçült). Bu özellik kapalıyken davranış tamamen script ile aynıdır.

**1. Sunucuda Ollama kurulumu**

```bash
# Linux (curl ile)
curl -fsSL https://ollama.com/install.sh | sh

# Model indir (hafif: 3B, daha iyi mantık: 7B)
ollama pull llama3.2:3b
# veya: ollama pull qwen2.5:7b
```

**2. Servisi çalıştır**

```bash
ollama serve   # arka planda veya systemd ile
```

**3. Bot config**

`.env` içinde:

```
USE_LLM_ADVISOR=true
OLLAMA_MODEL=llama3.2:3b
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_TIMEOUT_SECONDS=8
OLLAMA_MAX_CALLS_PER_CYCLE=3
```

**4. Davranış**

- Her cycle’da en fazla `OLLAMA_MAX_CALLS_PER_CYCLE` sinyal LLM’e gider (döngü süresini uzatmamak için).
- Cevap **APPROVE** → işlem aynen açılır.
- **REJECT** → o sinyal atlanır.
- **REDUCE** → pozisyon büyüklüğü yarıya iner.
- Zaman aşımı veya hata → APPROVE kabul edilir (bot bloke olmaz).

LLM’e giden bağlam: sembol, yön, z-score, RSI, MACD, BB, trend, confluence, açık pozisyon sayısı, bakiye, realise PnL özeti.

## Live Trading Checklist

Before enabling live trading:

1. Run paper mode for at least 24-48 hours
2. Review trades in the CSV export
3. Verify API credentials work: `python -m src.cli validate-config`
4. Set in `.env`:
   ```
   PAPER_MODE=false
   ALLOW_LIVE_TRADING=true
   ```
5. Start with minimal capital and conservative settings
6. Monitor logs: `tail -f data/bingx_agent.log | python -m json.tool`

## Testing

```bash
pytest -v
pytest --cov=src
```

Test coverage includes:
- HMAC-SHA256 signature generation
- Rate limiter backoff behavior
- Universe discovery & caching
- Selector filter pipeline
- Strategy signal generation
- Paper fill simulation (SL/TP/timeout)
- Risk state transitions
- Idempotent order IDs
- Storage schema & queries

## Project Structure

```
traderbot/
├── src/
│   ├── config.py        # Pydantic settings (extra="forbid" — see POSTMORTEM)
│   ├── logger.py        # JSON structured logging
│   ├── bingx_client.py  # API client (auth + rate limit)
│   ├── universe.py      # Contract discovery
│   ├── marketdata.py    # Indicators, klines, depth
│   ├── selector.py      # 3-stage tradeable filter
│   ├── strategy.py      # 24 indicators → 7 clusters → signal
│   ├── risk.py          # Risk state machine, Kelly sizing
│   ├── execution.py     # Paper + Live execution
│   ├── portfolio.py     # Position & PnL tracking
│   ├── storage.py       # SQLite persistence
│   ├── scheduler.py     # Main 24/7 loop
│   ├── llm_advisor.py   # Optional Ollama signal advisor
│   ├── cli.py           # CLI commands
│   │
│   ├── allocator.py     # basket + rebalance — the one thing that measured positive
│   ├── allocator_runner.py  # wires the allocator to a live account
│   ├── costs.py         # broker cost profiles; every conclusion is cost-governed
│   ├── portfolio/       # multi-asset engine, measured in real lira
│   ├── bist/            # forensic accounting screen for Turkish equities
│   └── crypto/          # cross-sectional selection over the perp universe
│
├── research/
│   ├── replay/          # replays the shipped engine over history
│   │   ├── engine.py    #   the replay itself
│   │   ├── run.py       #   what the account would have done
│   │   ├── attribution.py   # scores all 24 indicators
│   │   ├── sweep.py     #   16 configs, fit/test split
│   │   └── search.py    #   1,008 independent rules
│   ├── crypto/          # universe, multi-timeframe and cross-sectional studies
│   └── bist/            # the same discipline on 136 Turkish equities
│
├── tests/               # 309 tests
├── data/                # SQLite DB + logs (gitignored)
├── BOT-NASIL-CALISIR.md # plain-language explanation (Turkish)
├── RESEARCH-SYNTHESIS.md# every measurement, crypto and BIST
├── POSTMORTEM.md        # why the first version lost money
├── CHANGELOG-FIXES.md   # the six bugs, one by one
└── README.md
```

Cached market data (`*.pkl`, ~150 MB) is gitignored: the fetchers reproduce it
on demand, and committing it would pin a vendor snapshot the scripts exist to
refresh.

## Monitoring

Logs are JSON-structured. Every cycle logs:
- Universe size, tradeable count
- Open positions, total exposure
- Realised/unrealised PnL
- Risk state
- API latency, rate limit hits
- Cycle duration (ms)

```bash
# Pretty-print latest logs
tail -20 data/bingx_agent.log | python -m json.tool

# Filter for trades only
grep '"paper: position' data/bingx_agent.log | python -m json.tool
```

## Troubleshooting

| Issue | Solution |
|---|---|
| `Config validation FAILED` | Check `.env` file exists and values are valid |
| `rate limited (429)` | Reduce `SCAN_INTERVAL_MINUTES` or reduce shortlist size |
| `universe refresh failed` | Check network / API status; bot uses cached data |
| `0 tradeable symbols` | Normal during low-liquidity periods; bot continues |
| `ULTRA_TIGHT state` | Bot is protecting capital; will auto-recover |
| Docker healthcheck failing | Run `validate-config` manually to diagnose |

## License

Private / Internal Use.
