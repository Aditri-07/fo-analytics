-- ============================================================
-- Front-office analytics schema (SQLite dialect; portable to
-- Postgres with minor type changes).
--
-- Layering:
--   raw_trade_events  : landing zone, feed rows as-received (dirty)
--   trade_events      : validated, immutable event log (audit trail)
--   trades            : current state per trade (derived from events)
--   positions_eod     : end-of-day position snapshots
--   dq_issues         : data-quality rejections/flags from ETL
-- ============================================================

PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------
-- Reference data
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clients (
    client_id    TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    tier         TEXT NOT NULL CHECK (tier IN ('PLATINUM','GOLD','SILVER')),
    sector       TEXT NOT NULL,
    region       TEXT NOT NULL,
    onboarded    TEXT NOT NULL              -- ISO date
);

CREATE TABLE IF NOT EXISTS instruments (
    instrument_id TEXT PRIMARY KEY,
    asset_class   TEXT NOT NULL CHECK
        (asset_class IN ('EQUITY','FIXED_INCOME','FX','COMMODITY')),
    description   TEXT,
    currency      TEXT NOT NULL DEFAULT 'USD',
    contract_size REAL NOT NULL DEFAULT 1.0
);

-- ------------------------------------------------------------
-- Landing zone: raw feed rows, no constraints beyond a surrogate
-- key. ETL (step 2) reads from here, validates, promotes.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_trade_events (
    raw_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_file    TEXT NOT NULL,
    line_no      INTEGER NOT NULL,
    payload      TEXT NOT NULL,             -- original JSON line
    loaded_at    TEXT NOT NULL DEFAULT (datetime('now')),
    processed    INTEGER NOT NULL DEFAULT 0 -- 0=pending 1=promoted 2=rejected
);

-- ------------------------------------------------------------
-- Validated, immutable event log (the audit trail).
-- One row per lifecycle event; trades table is derived state.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trade_events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id     TEXT NOT NULL,
    event_type   TEXT NOT NULL CHECK (event_type IN ('NEW','AMEND','CANCEL')),
    event_time   TEXT NOT NULL,             -- ISO timestamp (UTC)
    version      INTEGER NOT NULL,
    payload      TEXT NOT NULL,             -- validated JSON of the event
    raw_id       INTEGER REFERENCES raw_trade_events(raw_id),
    UNIQUE (trade_id, version)
);
CREATE INDEX IF NOT EXISTS ix_trade_events_trade
    ON trade_events (trade_id, version);

-- ------------------------------------------------------------
-- Current state per trade (what analytics query most).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trades (
    trade_id        TEXT PRIMARY KEY,
    client_id       TEXT NOT NULL REFERENCES clients(client_id),
    instrument_id   TEXT NOT NULL REFERENCES instruments(instrument_id),
    asset_class     TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    quantity        REAL NOT NULL CHECK (quantity > 0),
    price           REAL NOT NULL CHECK (price > 0),
    notional        REAL NOT NULL,
    currency        TEXT NOT NULL,
    trade_time      TEXT NOT NULL,
    settlement_date TEXT,
    trader          TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('NEW','AMENDED','CANCELLED')),
    version         INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_trades_client   ON trades (client_id, trade_time);
CREATE INDEX IF NOT EXISTS ix_trades_ac_time  ON trades (asset_class, trade_time);
CREATE INDEX IF NOT EXISTS ix_trades_status   ON trades (status);

-- ------------------------------------------------------------
-- End-of-day position snapshots (per client x instrument x date).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS positions_eod (
    as_of_date    TEXT NOT NULL,
    client_id     TEXT NOT NULL REFERENCES clients(client_id),
    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
    asset_class   TEXT NOT NULL,
    net_quantity  REAL NOT NULL,
    avg_cost      REAL NOT NULL,
    mark_price    REAL,
    realized_pnl  REAL NOT NULL DEFAULT 0,
    unrealized_pnl REAL,
    PRIMARY KEY (as_of_date, client_id, instrument_id)
);

-- ------------------------------------------------------------
-- End-of-day marks. In this project marks are derived from the
-- last traded price per instrument/day with carry-forward (a
-- proxy; a real desk would use vendor closes).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS marks_eod (
    as_of_date    TEXT NOT NULL,
    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
    mark_price    REAL NOT NULL,
    source        TEXT NOT NULL DEFAULT 'LAST_TRADE',
    PRIMARY KEY (as_of_date, instrument_id)
);

-- ------------------------------------------------------------
-- Data-quality issues raised by ETL validation.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dq_issues (
    issue_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_id      INTEGER REFERENCES raw_trade_events(raw_id),
    severity    TEXT NOT NULL CHECK (severity IN ('WARN','REJECT')),
    rule        TEXT NOT NULL,              -- e.g. 'DUPLICATE_TRADE_ID'
    detail      TEXT,
    detected_at TEXT NOT NULL DEFAULT (datetime('now'))
);
