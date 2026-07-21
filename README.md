# Front-Office Workflow & Analytics System

Trade lifecycle management, portfolio/sales analytics, ETL, and an LLM
query layer over simulated front-office data.

## Status

- [x] Step 1 — Data model, feed simulator, SQL schema
- [x] Step 2 — ETL + validation
- [x] **Step 3 — Analytics layer** (this commit)
- [ ] Step 4 — LLM natural-language query layer
- [ ] Step 5 — C++ analytics engine + pybind11 bindings
- [ ] Step 6 — Dashboard

## Quick start

```bash
pip install -r requirements.txt
python run_all.py            # simulate -> ETL -> analytics, one command
python -m pytest tests/ -q   # 45 tests
```

`python run_all.py --fresh` wipes `data/` and rebuilds from scratch
(do this after changing any logic that affects stored values).
Stages can also be run individually, in order:

```bash
python run_simulator.py --days 20 --clients 25 --seed 42   # 1. generate feeds
python run_etl.py                                          # 2. load -> validate -> promote
python run_analytics.py                                    # 3. marks + snapshots + report
```

Outputs:
- `data/feeds/trade_events_YYYY-MM-DD.jsonl` — daily lifecycle event feeds
- `data/feeds/clients.csv`, `instruments.csv` — reference data
- `data/fo_analytics.db` — SQLite DB with schema + reference data loaded

## Architecture (step 1)

```
FeedGenerator ──► JSONL event feeds (NEW/AMEND/CANCEL, ~4% dirty rows)
                        │
                        ▼            (step 2: ETL validates + promotes)
              raw_trade_events ──► trade_events (audit) ──► trades (state)
                                                        └─► positions_eod
```

Design decisions:
- **Event-sourced trades.** The feed carries lifecycle events; `trades`
  is derived current state, `trade_events` is the immutable audit trail.
- **Simulator writes files, not the DB.** Mirrors real feed handling and
  gives the ETL (step 2) a genuine ingestion boundary to validate at.
- **Dirty data by design.** Duplicates, missing fields, bad values,
  unknown references, stale timestamps, malformed JSON — injected at
  configurable rates so validation has real work to do.
- **Client profiles drive behaviour.** Tiered activity levels,
  asset-class preferences, dormancy — patterns the sales analytics in
  step 3 should rediscover from the data alone.
- **WAC position keeping** with realized/unrealized P&L split and
  correct flip-through-zero handling; amendments/cancels replay
  economics so positions stay consistent.

## ETL (step 2)

```bash
FO_ENV=dev python run_etl.py     # load -> validate -> promote
```

- **Landing zone first.** Raw lines are stored verbatim in
  `raw_trade_events`; parsing and validation never happen at ingestion.
- **12 REJECT rules + 2 WARN rules** with stable codes
  (`DUPLICATE_EVENT`, `ORPHAN_EVENT`, `VERSION_OUT_OF_SEQUENCE`,
  `AMEND_AFTER_CANCEL`, ...). Rejects go to `dq_issues` and are never
  promoted; warns (e.g. `STALE_TIMESTAMP`) are flagged but promoted.
- **Lifecycle-aware validation.** The validator tracks per-trade
  version/status, so it catches orphaned amends, amend-after-cancel,
  version gaps, and duplicate lifecycle events — including *cascade*
  rejections (a rejected NEW orphans all its later events).
- **Idempotent + transactional.** Re-running loads nothing twice and
  re-promotes nothing; the whole run is one transaction, so a crash
  leaves the DB untouched.
- **Environments as config.** `FO_ENV=dev|uat|prod` selects DB/feed
  paths and log level (`fo/config.py`); rotating-file logging in
  `fo/logging_setup.py`.

## Analytics (step 3)

- **EOD snapshots** replay the `trade_events` audit log through the
  same `Position` class as the OOP model (WAC, flip-through-zero,
  amend/cancel reverse-and-replay) and write `positions_eod` per day.
  Replay is in audit order with day-clamping: backdated (stale-WARN)
  events apply on the processing day, never retroactively.
- **Marks** = last traded price per instrument/day with carry-forward
  (`marks_eod`, source column records carried days). A proxy for
  vendor closes; swap in real EOD data later without touching callers.
- **Portfolio analytics** (`fo/analytics/portfolio.py`): P&L summary,
  net/gross exposure, concentration, turnover, top movers, asset-class
  breakdown, daily P&L series.
- **Sales analytics** (`fo/analytics/sales.py`): client activity with
  prior-period comparison, product preferences, dormant clients,
  weekly trends, opportunity flags (FADING, SINGLE_PRODUCT) with
  human-readable reasons.
- All functions take `(conn, plain params)` and return JSON-serializable
  rows — they are the LLM's tool set in step 4.
- **Conventions:** notional/exposure in USD per asset-class convention
  (FI clean/100, commodity contract size, USD-base FX = base qty);
  USD-base FX P&L converted from quote ccy at the day's mark.
  Known simplifications: no accrued interest on bonds, no funding or
  fees, marks are trade prints not vendor closes.
- **Ground-truth test:** dormancy detection provably recovers the
  simulator's hidden dormant-client profiles from the data alone.

## Layout

```
fo/
  models/    enums.py, trade.py (Trade + 4 asset-class subclasses),
             portfolio.py (Client, Order, Position, Portfolio)
  db/        schema.sql, database.py (bootstrap + reference load)
  simulator/ feed_generator.py
tests/       test_models.py (13 tests)
run_simulator.py
```
