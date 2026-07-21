# Sales & Trading Analytics Platform

A front-office trade analytics platform: full trade lifecycle modeling, a validated
data pipeline, portfolio and sales analytics, an LLM natural-language query layer,
and a performance-critical engine ported to C++ — built on simulated market data.

> **Note on data:** All data is simulated. Real front-office trade flow is
> confidential, so a seeded generator produces realistic trading activity (tiered
> clients, asset-class preferences, dirty feeds) to run the system against. The
> pipeline, analytics, and engine are production-shaped and would run unchanged on
> real feeds.

## What it does

The system models how a trading desk's analytics infrastructure is organized, in
five layers:

- **Trade lifecycle** — event-sourced model of trades, orders, positions,
  portfolios, and clients across equities, fixed income, FX, and commodities.
  Booking, amendments, and cancellations flow as events to an immutable audit log;
  current positions are derived from the event stream.
- **Data pipeline (ETL)** — raw feeds land in a staging area, pass through 14
  data-quality checks, and only clean records are promoted. Idempotent and
  crash-safe.
- **Analytics** — deterministic, unit-tested functions over the database: P&L,
  exposure/netting, concentration, turnover, client activity, and dormancy.
- **AI query layer** — plain-English questions are routed by an LLM to the correct
  analytics function. The model selects and parameterizes; all numbers come from
  the data, never the model.
- **C++ engine** — the performance-critical position/P&L loop, ported to C++ via
  pybind11 and validated to match the Python implementation exactly.

## Architecture

```
FeedGenerator ──► JSONL feeds (NEW / AMEND / CANCEL, ~4% dirty rows)
                        │
                        ▼  ETL: validate + promote
              raw_trade_events ──► trade_events (audit) ──► trades (state)
                        │                                      │
                        ▼                                      ▼
                   dq_issues                            positions_eod ◄── marks
                        │                                      │
                        └──────────► analytics layer ◄─────────┘
                                          │
                        ┌─────────────────┼─────────────────┐
                        ▼                 ▼                 ▼
                   C++ engine        LLM query          dashboard
                   (pybind11)         layer
```

## Quick start

```bash
pip install -r requirements.txt
python run_all.py            # simulate → ETL → analytics, one command
python -m pytest tests/ -q   # 46 tests
```

Individual stages, in order:

```bash
python run_simulator.py --days 20 --clients 25 --seed 42   # generate feeds + DB
python run_etl.py                                          # load → validate → promote
python run_analytics.py                                    # marks, snapshots, report
```

### Natural-language queries (optional)

Requires a Gemini API key. Everything else runs without it.

```bash
export GEMINI_API_KEY=your-key
python run_query.py "which clients increased fixed-income activity this month?"
python run_query.py "who are my dormant clients?"
```

### C++ engine

Requires a C++ compiler.

```bash
pip install pybind11
pip install -e .              # compiles the C++ module
python run_benchmark.py       # Python vs C++ on identical data
```

The engine is validated to match the Python implementation exactly
(`tests/test_engine.py`).

### Dashboard

```bash
streamlit run dashboard.py    # opens localhost:8501
```

Interactive views for P&L, exposure, client analytics, and the natural-language
query box. Set `GEMINI_API_KEY` before launching for the query tab.

## Design decisions worth knowing

- **Event-sourced trades.** The feed carries lifecycle events; `trades` is derived
  current state and `trade_events` is the immutable audit trail (unique on
  trade-id + version). Amendments and cancellations are applied as
  reverse-and-replay, so positions stay exact.
- **The simulator writes files, not the database.** This creates a real ingestion
  boundary for the ETL to validate at — mirroring how feed handling actually works.
- **Dirty data by design.** The generator injects duplicates, missing fields, bad
  values, unknown references, stale timestamps, and malformed JSON at configurable
  rates, so validation has genuine work to do. Rejections cascade correctly — a
  rejected NEW event orphans its later amendments and cancellations.
- **Weighted-average-cost position keeping** with a realized/unrealized P&L split
  and correct flip-through-zero handling (a position crossing zero re-opens at the
  fill price).
- **Asset-class-specific notional conventions** — bond clean price × face / 100,
  commodity contract size, USD-base FX where the USD leg is the base quantity.
- **The LLM never computes.** It chooses which analytics function to call and with
  what parameters; the Python code produces every number. This keeps output
  validated and reproducible.

## Testing

46 unit tests with CI (GitHub Actions), including:

- Trade lifecycle and position-keeping edge cases (flips, amends, cancels)
- Every data-quality validation rule
- End-to-end pipeline reconciliation
- A ground-truth test that recovers 100% of the simulator's hidden dormant-client
  profiles from the data alone
- C++ / Python reconciliation to machine precision

## Layout

```
fo/
  models/     enums, trade hierarchy, client/order/position/portfolio
  db/         schema.sql, database bootstrap
  simulator/  feed generator
  etl/        loader, validator (14 rules), pipeline
  analytics/  marks, snapshots, portfolio, sales
  ai/         LLM tool definitions + query agent
  engine/     Python wrapper for the C++ module
cpp/          engine.cpp (pybind11)
tests/        46 tests
dashboard.py  Streamlit dashboard
run_*.py      simulator, ETL, analytics, query, benchmark, all
```

## Known simplifications

Simulated data throughout. No accrued interest on bonds; FX quoted against USD;
marks are trade prints rather than vendor closes; DEV/UAT/PROD are config profiles,
not real infrastructure. These are scoped deliberately — the goal is correct
machinery, not a production trading system.
