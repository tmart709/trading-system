-- SQLite schema, written to port to Postgres with minimal changes:
--   * timestamps are ISO-8601 UTC TEXT (-> TIMESTAMPTZ)
--   * "INTEGER PRIMARY KEY" ids (-> BIGINT GENERATED ALWAYS AS IDENTITY)
--   * JSON blobs are TEXT (-> JSONB)
--   * prices/sizes are TEXT decimal strings to keep exact values (-> NUMERIC)

CREATE TABLE IF NOT EXISTS markets (
    venue             TEXT NOT NULL,
    market_id         TEXT NOT NULL,
    event_id          TEXT,
    event_key         TEXT,              -- venue-neutral real-world event, e.g. weather:NYC:2026-10-13
    series_id         TEXT,
    title             TEXT,
    rules             TEXT,
    settlement_source TEXT,
    status            TEXT,
    close_time        TEXT,
    bracket_low       INTEGER,
    bracket_high      INTEGER,
    result            TEXT,
    settled_at        TEXT,              -- when we first saw the result
    raw_json          TEXT,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    PRIMARY KEY (venue, market_id)
);
CREATE INDEX IF NOT EXISTS idx_markets_event ON markets (venue, event_id);

-- Every distinct version of a market's resolution text. Rules can be edited after listing.
CREATE TABLE IF NOT EXISTS market_rules_history (
    id                INTEGER PRIMARY KEY,
    venue             TEXT NOT NULL,
    market_id         TEXT NOT NULL,
    rules             TEXT NOT NULL,
    settlement_source TEXT,
    seen_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rules_hist ON market_rules_history (venue, market_id, seen_at);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id                INTEGER PRIMARY KEY,
    venue             TEXT NOT NULL,
    market_id         TEXT NOT NULL,
    ts                TEXT NOT NULL,       -- when we fetched it (UTC)
    exchange_ts       TEXT,
    depth             TEXT NOT NULL DEFAULT 'full',  -- full | top (best prices only, sizes unknown)
    best_yes_bid      TEXT,
    best_yes_bid_size TEXT,
    best_yes_ask      TEXT,
    best_yes_ask_size TEXT,
    yes_bids_json     TEXT NOT NULL,       -- [[price, size], ...] best first
    yes_asks_json     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_market_ts ON orderbook_snapshots (venue, market_id, ts);

CREATE TABLE IF NOT EXISTS forecasts (
    id            INTEGER PRIMARY KEY,
    fetched_at    TEXT NOT NULL,
    city          TEXT NOT NULL,
    source        TEXT NOT NULL,      -- nws | open_meteo_ensemble
    model         TEXT NOT NULL,      -- nws_point | nws_grid | gfs_seamless | ...
    variable      TEXT NOT NULL DEFAULT 'high',   -- high | low
    target_date   TEXT NOT NULL,      -- LST date of the high/low (YYYY-MM-DD)
    issued_at     TEXT,
    point_f       REAL,               -- deterministic value / ensemble mean
    members_json  TEXT,               -- ensemble member values (deg F)
    raw_json      TEXT
);
CREATE INDEX IF NOT EXISTS idx_fc_city_date ON forecasts (city, target_date, fetched_at);

-- Cross-venue pairs. Nothing may trade a pair unless status = 'approved' (set only by a human).
CREATE TABLE IF NOT EXISTS market_pairs (
    id                 INTEGER PRIMARY KEY,
    kalshi_market_id   TEXT NOT NULL,
    pm_market_id       TEXT NOT NULL,
    match_reason       TEXT,
    warnings           TEXT,
    kalshi_rules       TEXT,
    pm_rules           TEXT,
    kalshi_source      TEXT,
    pm_source          TEXT,
    status             TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    notes              TEXT,
    created_at         TEXT NOT NULL,
    reviewed_at        TEXT,
    UNIQUE (kalshi_market_id, pm_market_id)
);

-- Populated from the paper phase onward.
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    venue         TEXT NOT NULL,
    market_id     TEXT NOT NULL,
    side          TEXT NOT NULL,
    model_prob    TEXT,
    market_price  TEXT,
    fee_per_contract TEXT,
    edge          TEXT,
    size          TEXT,
    mode          TEXT NOT NULL,
    acted         INTEGER NOT NULL DEFAULT 0,
    details_json  TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id               INTEGER PRIMARY KEY,
    client_order_id  TEXT NOT NULL UNIQUE,
    venue_order_id   TEXT,
    ts               TEXT NOT NULL,
    strategy         TEXT NOT NULL,
    venue            TEXT NOT NULL,
    market_id        TEXT NOT NULL,
    side             TEXT NOT NULL,
    contracts        TEXT NOT NULL,
    limit_price      TEXT NOT NULL,
    mode             TEXT NOT NULL,
    status           TEXT NOT NULL,
    signal_id        INTEGER,
    details_json     TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    id             INTEGER PRIMARY KEY,
    order_id       INTEGER,
    venue_fill_id  TEXT,
    ts             TEXT NOT NULL,
    venue          TEXT NOT NULL,
    market_id      TEXT NOT NULL,
    side           TEXT NOT NULL,
    contracts      TEXT NOT NULL,
    price          TEXT NOT NULL,
    fee            TEXT NOT NULL,
    mode           TEXT NOT NULL,
    UNIQUE (venue, venue_fill_id)
);

CREATE TABLE IF NOT EXISTS daily_pnl (
    day          TEXT NOT NULL,
    strategy     TEXT NOT NULL,
    venue        TEXT NOT NULL,
    mode         TEXT NOT NULL,
    realized     TEXT NOT NULL,
    unrealized   TEXT NOT NULL,
    fees         TEXT NOT NULL,
    PRIMARY KEY (day, strategy, venue, mode)
);

CREATE TABLE IF NOT EXISTS system_events (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    level     TEXT NOT NULL,
    component TEXT NOT NULL,
    message   TEXT NOT NULL
);

-- Station observations (NWS / METAR) so same-day markets know the high observed so far.
CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY,
    fetched_at  TEXT NOT NULL,
    city        TEXT NOT NULL,
    station     TEXT NOT NULL,
    obs_time    TEXT NOT NULL,
    temp_f      REAL,
    raw_json    TEXT,
    UNIQUE (station, obs_time)
);

-- Historical forecast-vs-actual pairs for calibrating the weather model.
-- forecast = LST-day max from a model run issued `lag_days` before; actual = NWS CLI high.
CREATE TABLE IF NOT EXISTS calibration_samples (
    city         TEXT NOT NULL,
    variable     TEXT NOT NULL,       -- high | low
    target_date  TEXT NOT NULL,
    lag_days     INTEGER NOT NULL,
    model        TEXT NOT NULL,
    forecast_f   REAL NOT NULL,
    PRIMARY KEY (city, variable, target_date, lag_days, model)
);
CREATE TABLE IF NOT EXISTS cli_actuals (
    city         TEXT NOT NULL,
    target_date  TEXT NOT NULL,
    high_f       INTEGER NOT NULL,
    low_f        INTEGER,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (city, target_date)
);

-- Every model probability the strategy computes (hourly per market), for calibration reporting
-- against settled results, signal or not.
CREATE TABLE IF NOT EXISTS model_predictions (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    venue         TEXT NOT NULL,
    market_id     TEXT NOT NULL,
    model_prob    REAL NOT NULL,
    market_mid    REAL,
    details_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_pred_market ON model_predictions (venue, market_id, ts);

-- Operator controls: kill switch and daily halts. Read by the risk layer before every order.
CREATE TABLE IF NOT EXISTS controls (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reason     TEXT
);
