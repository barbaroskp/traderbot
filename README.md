# BingX Agent

Autonomous trading bot for **BingX Perpetual Futures (Swap V2)** — 24 technical
indicators grouped into seven voting clusters, adaptive risk management, and
paper/live dual-mode execution — together with the research harness used to
evaluate it.

The harness replays the shipped decision engine over historical market data and
measures the result. Its findings are summarised below and detailed in
[RESEARCH-SYNTHESIS.md](RESEARCH-SYNTHESIS.md). A plain-language walkthrough in
Turkish is in [BOT-NASIL-CALISIR.md](BOT-NASIL-CALISIR.md).

## Results

The bot's own decision engine, replayed across 89 BingX symbols over 41 days of
5-minute and hourly data, with fees, slippage and funding charged:

| configuration | starting 10,000 | ends at | trades | win rate |
|---|---|---|---|---|
| current (post-refactor) | 10,000 | **8,956** (−10.4%) | 171 | 43% |
| as originally shipped | 10,000 | **34** (−99.7%) | 4,122 | 29% |

The second configuration lost money on the trades themselves before commission
was applied: −5,798 gross, −2,778 in fees.

Four further studies were run to establish whether any configuration performs
better:

| study | scope | result |
|---|---|---|
| indicator attribution | 24 indicators, 93,193 votes | none clears its transaction cost |
| parameter sweep | 16 configurations, fit/test split | none profitable in both halves |
| rule search | 1,008 independent rules | fit↔test correlation −0.037 |
| timeframe map | 14 signals × 7 timeframes | only daily MACD is statistically solid, and only in coins too illiquid to trade |

The one approach that measured positive was holding a basket with infrequent
rebalancing, implemented in `src/allocator.py`.

### Data and methodology

All figures are derived from data downloaded and replayed in this repository.

**Market data**

| market | breadth | depth | observations |
|---|---|---|---|
| BingX perpetuals | 1,216 contracts discovered, 967 with live books | 372 symbols × 999 daily bars (2.7 yr) | 371,628 |
| BingX perpetuals | 196 symbols | 3,996 hourly bars each (166 d) | 783,216 |
| BingX perpetuals | 89 symbols | 11,988 five-minute bars each (41 d) | 1,066,932 |
| BingX perpetuals | 150 symbols | 1m / 5m / 15m / 30m / 4h panels | ~2,100,000 |
| Borsa İstanbul | 136 tickers | 2,543 daily bars (10 yr) | 345,848 |
| Borsa İstanbul | 136 tickers | hourly, 2 yr | 68,257 |
| multi-asset, in lira | BTC ETH XU100 GOLD SP500 SILVER USDTRY | 10 yr daily | 22,603 |

Roughly **4.7 million bars**, all cached to disk and gitignored.

**Studies run on it**

| study | what was tested | sample |
|---|---|---|
| indicator attribution | all 24 shipped indicators | 93,193 directional votes |
| intraday signal map | 9 signals × 4 horizons | 772,150 hourly observations |
| confluence test | vote-count → forward return | 767,446 observations |
| multi-timeframe map | 14 signals × 7 timeframes | 1m through daily |
| rule search | 1,008 independent rules | fit/test split |
| parameter sweep | 16 configurations | fit/test split |
| opening-gap study | 30 rules, then an artefact audit | 68,104 ticker-days |
| BIST technical rules | 12 rules, then 5 from the literature | 326,040 ticker-days |
| cross-sectional selection | 9 signals × 5 turnover settings | 372 symbols |
| full bot replay | the shipped engine, bar by bar | 11,885 cycles |

**Methodology**

- Forward returns are cross-sectionally demeaned. Both crypto and BIST rose
  over their sample windows; without this adjustment every long signal appears
  skilled and every short signal broken.
- Significance threshold is |t| ≥ 3.0 rather than 2.0, following Harvey, Liu &
  Zhu, because many rules are tested against the same data.
- Candidates are fitted on one half of the sample and scored on the other, and
  both figures are always reported.
- Survivors are then tested for breadth (results recomputed with the ten best
  symbols removed) and stability (month by month).
- Costs are taken from live BingX order books and the BIST tick table.

This standard is why the conclusion is negative. An earlier, looser pass
produced four apparent findings, each of which failed on closer inspection:
shock reversion (t = +3.66 in year one, −0.71 in year two), moving-average
filters (look-ahead bias), the drawdown brake (an equity-versus-price re-entry
error), and tier-C trend following (t = +3.48 falling to +0.96 once the
universe was selected point-in-time).

Two errors in the analysis code are documented rather than silently corrected:
a TP/SL unpacking inversion that caused the replay to report −99.9%, and a
liquidity ranking distorted by lira inflation that reversed a conclusion
outright. Both are described in
[RESEARCH-SYNTHESIS.md](RESEARCH-SYNTHESIS.md).

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

Measured performance of this engine is reported under [Results](#results)
above.

## Risk Management

Three-state risk machine that **never stops** the bot:

| State | Trigger | Effect |
|---|---|---|
| **NORMAL** | Default | Standard thresholds |
| **TIGHT** | 3 losses / 5% DD / high API errors | +50% entry threshold, 50% notional, +50% cooldown |
| **ULTRA_TIGHT** | 5 losses / 10% DD / very high errors | Top 10 only, 0.5-1 USDT max, low-vol only |

Automatic de-escalation when consecutive wins and stable cycles accumulate.

## Parameter tuning

This section previously recommended stricter confluence thresholds, wider
risk–reward ratios and reduced trade frequency as routes to higher returns.
Each was subsequently tested and none improved the result, so the
recommendations have been replaced by the measurements.

Every variant was fitted on the first half of the sample and scored on the
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

Two relationships hold across the variants:

1. **Losses scale with trade count** — 23 trades give −0.19%, 79 give −4.71%,
   166 give −8.94%. Per-trade expectancy is negative, and the parameters
   determine only how many such trades are taken.
2. **Leverage compounds the loss rather than creating an edge.** For the
   basket strategy the growth-optimal level is `L* = (μ−r)/σ² ≈ 1.05x`, and
   the growth rate turns negative at 2.10x.

To reproduce:

```bash
python -m research.replay.sweep --days 40        # the table above
python -m research.replay.attribution            # all 24 indicators scored
python -m research.replay.search --days 40       # 1,008 independent rules
python -m src.costs 0.10                         # break-even against your own broker
```

### Optional LLM Advisor (Ollama)

Available and disabled by default. Note that the advisor filters the signals
described above, and those signals were measured to carry no edge.

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
