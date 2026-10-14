# trading-system

Automated trading for Kalshi and Polymarket US prediction markets: a shared core (exchange clients,
recorder, matching, execution, risk, alerts) plus pluggable strategies.

**Current stage: `paper` mode is running for the weather strategy.** Demo and live are not built yet.
Nothing in this codebase can place a real order: the venue trading methods raise `TradingDisabled`.

| Mode   | Status | What it does |
|--------|--------|--------------|
| record | ✅ working | Records markets + full rules, order books, forecasts, station observations and settlements to SQLite |
| paper  | ✅ working | Weather strategy on live prices; simulated immediate-or-cancel fills, positions, PnL, daily report |
| demo   | later | Real orders on the **Kalshi demo** only (Polymarket US has no sandbox) |
| live   | disabled | Needs `live_enabled: true` per venue *and* an interactive confirmation. Very low default limits |

## Setup

Requires Python 3.10+ (developed on 3.13).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env        # then edit; record mode needs only NWS_USER_AGENT
.venv/bin/python -m pytest
```

Set `NWS_USER_AGENT` to something identifying you (api.weather.gov asks for contact info in the User-Agent).

## Running

```bash
.venv/bin/python cli.py run                # recorder + strategies (paper), continuous. This is the main command
.venv/bin/python cli.py record             # recorder only, no strategies
.venv/bin/python cli.py report             # yesterday's summary (or --date YYYY-MM-DD)
.venv/bin/python cli.py kill               # kill switch: blocks every new order, cancels open ones
.venv/bin/python cli.py unkill
.venv/bin/python cli.py calibrate          # rebuild the weather model's history + print accuracy stats
.venv/bin/python cli.py status             # row counts, latest prices side by side, latest forecasts
.venv/bin/python cli.py pairs list --status pending
.venv/bin/python cli.py pairs show 12      # both venues' full resolution rules + warnings
.venv/bin/python cli.py pairs approve 12 --notes "checked TWC vs CLI history"
.venv/bin/python cli.py check-keys         # read-only balance calls; verifies credentials
```

### 24/7 on a VPS (e.g. AWS us-east-1)

```bash
docker compose up -d --build
docker compose logs -f recorder
```

The SQLite DB lives in `./db` on the host. Back it up; it's your backtest history.

## API keys

Record mode needs **no** venue keys: all market data endpoints used are public.

**Kalshi.** Log in at kalshi.com (or demo.kalshi.co for the sandbox; demo needs its own account and keys).
Go to Account & security → API Keys → Create Key. Ed25519 is the default key type; RSA also works, and the
client detects which from the key file. Save the private key file to `secrets/` (gitignored) and set
`KALSHI_{DEMO,PROD}_API_KEY_ID` and `KALSHI_{DEMO,PROD}_PRIVATE_KEY_PATH`.

**Polymarket US.** Complete KYC in the Polymarket US app, then visit https://polymarket.us/developer
(sign in with the *same* method as the app). Create a key and copy the Secret Key, which is shown once. Set
`POLYMARKET_KEY_ID` and `POLYMARKET_SECRET_KEY`. This uses the official `polymarket-us` SDK, not the
global Polymarket CLOB.

## What gets recorded

| Table | Contents |
|-------|----------|
| `markets` | Every weather market on both venues: title, **full rules text**, settlement source, bracket, status, result |
| `market_rules_history` | Every distinct version of each market's rules, in case a venue edits them |
| `orderbook_snapshots` | Kalshi: full depth for every market, every 60s (one batch call). Polymarket US: best bid/ask every 60s (`depth='top'`), plus full depth on rotation (`depth='full'`) |
| `forecasts` | NWS point + gridpoint highs; Open-Meteo GFS (31) and ECMWF (51) ensemble member daily highs |
| `observations` | NWS station obs (KNYC etc.) so same-day markets can see the high observed so far |
| `market_pairs` | Proposed Kalshi↔Polymarket US pairs, always `pending` until you approve them |
| `signals`, `orders`, `fills`, `daily_pnl` | Created now, populated from paper mode onward |

Ensemble daily highs are computed over the **local standard time** day (midnight to midnight LST),
because that's how the NWS climate report defines the daily max. During DST that's 1am to 1am local
clock time.

## Venue facts this code relies on (verified 2026-10-12)

**Kalshi**
- REST `https://external-api.kalshi.com/trade-api/v2` (prod) and `https://external-api.demo.kalshi.co/trade-api/v2` (demo; the older `demo-api.kalshi.co` still works)
- Signing: `base64(sign(timestamp_ms + METHOD + path_without_query))`. Ed25519 signs directly; RSA uses PSS/SHA-256
- Orderbook returns YES bids and NO bids only; YES ask = 1 − best NO bid. `GET /markets/orderbooks?tickers=…` batches up to 100, no auth
- Rate limits: token buckets, 10 tokens per request by default, Basic tier 200 read tokens/s. 429 has no Retry-After
- Fees: weather series are `fee_type: quadratic`, multiplier 1 → taker `ceil_to_cent(0.07·C·P·(1−P))`, no maker fee
- **Weather settlement: KXHIGH\* series now settle on The Weather Company** (weather.com/kalshi), referencing the CLI station (e.g. CLINYC). They do *not* settle on the NWS report directly

**Polymarket US**
- Public data `https://gateway.polymarket.us` (no key). Trading `https://api.polymarket.us`, Ed25519 signed (`X-PM-Access-Key`, `X-PM-Timestamp`, `X-PM-Signature`)
- **No sandbox**, so the path is record → paper → live
- One instrument per market: buy = YES, short = NO. Book is bids/offers on that instrument
- Fees: taker `θ·C·p·(1−p)`, θ = 0.0695 (0.10 table tennis), banker's rounding to the cent. Maker rebate −0.0125 until 2026-10-19 13:00 ET, then volume tiers (none below $10M/month). Modeled as 0
- Rate limits: the Retail API documents 25 req/s per IP. The public gateway's `/book` and `/bbo` measured at only **~5 requests per 10s**, then 429 with `Retry-After: 10`. Hence the rotation. The authenticated markets WebSocket is the better long-term feed
- **Weather settles on the NWS Daily Climate Report (CLI)**, with stations KNYC, KMDW, KMIA, KLAX, KSFO
- Slugs are misleading: `…-gte71lt72f` is described as "between 71F and 72F" (inclusive). Brackets are parsed from rules text only

**Why every cross-venue pair needs your approval:** the same NYC daily-high bracket settles on The
Weather Company at Kalshi and on the NWS CLI at Polymarket US. They usually agree, but rounding,
preliminary vs. final data and revisions can make them disagree.

## Weather model (strategies/weather_model.py)

The day's high is modeled as Normal(mu, sigma), rounded to whole degrees F like the NWS climate report.
`mu` = average of the GFS and ECMWF deterministic forecasts plus a bias, and `bias`/`sigma` come from
~2.5 years of those same models' past forecasts vs. official CLI highs, for the same city, lead time
and season (±30 days of the year). Walk-forward tests: 80% ranges contain the actual high 78–84% of
the time. Same-day markets treat the high observed so far as a floor, and stop trading at noon local
standard time (the model can't tell when the day's peak has passed). Signals need ≥5¢/contract edge
after exact fees and ≥5 contracts available; size is 0.1 Kelly within the risk caps in `config.yaml`.

## Layout

```
core/exchanges/   base.py (common interface), kalshi.py, polymarket_us.py
core/data/        recorder.py, forecasts.py (NWS + Open-Meteo)
core/matching/    weather.py (conservative pairing; human approval required)
core/alerts/      notifier.py (stdout + Telegram/Discord)
core/fees.py      both venues' fee models;  core/sizing.py  edge + Kelly sizing
core/risk/        pre-trade checks, exposure caps, daily loss halt, kill switch
core/execution/   paper broker (simulated IOC fills), portfolio/PnL from fills
core/engine.py    wires everything;  core/reporting.py  daily summary
strategies/       weather_model.py (probabilities), weather_fair_value.py (the strategy)
backtest/         next
```
