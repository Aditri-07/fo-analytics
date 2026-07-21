"""ETL pipeline: raw_trade_events -> trade_events (audit) -> trades (state).

Flow per raw row (processed in feed_file, line order):
  1. validate against reference data + current trade state
  2. REJECT -> dq_issues, mark raw processed=2, never promoted
     WARN   -> dq_issues, still promoted
  3. promote: insert into trade_events, apply to trades current state
     (NEW inserts; AMEND updates fields + recomputes notional;
      CANCEL flips status), mark raw processed=1

The whole run is one transaction: a crash mid-run leaves the DB as it
was before the run started.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from datetime import datetime

from fo.etl.validator import RefData, validate_event

log = logging.getLogger("etl.pipeline")

# Notional conventions per asset class (matches the OOP model).
def _notional(asset_class: str, qty: float, price: float,
              contract_size: float, instrument_id: str = "") -> float:
    if asset_class == "FIXED_INCOME":
        return qty * price / 100.0
    if asset_class == "COMMODITY":
        return qty * price * contract_size
    if asset_class == "FX" and instrument_id.startswith("USD"):
        return qty                      # USD-base pair: USD leg = base qty
    return qty * price


def _load_refdata(conn: sqlite3.Connection) -> RefData:
    clients = {r["client_id"] for r in conn.execute("SELECT client_id FROM clients")}
    instruments = {
        r["instrument_id"]: {
            "asset_class": r["asset_class"],
            "contract_size": r["contract_size"],
        }
        for r in conn.execute(
            "SELECT instrument_id, asset_class, contract_size FROM instruments"
        )
    }
    state = {
        r["trade_id"]: {"version": r["version"], "status": r["status"]}
        for r in conn.execute("SELECT trade_id, version, status FROM trades")
    }
    return RefData(clients, instruments, state)


def _feed_date(feed_file: str) -> datetime:
    # trade_events_YYYY-MM-DD.jsonl
    return datetime.fromisoformat(feed_file.removeprefix("trade_events_")
                                  .removesuffix(".jsonl"))


def run_pipeline(conn: sqlite3.Connection, stale_days_warn: int = 3) -> dict:
    ref = _load_refdata(conn)
    rows = conn.execute(
        "SELECT raw_id, feed_file, line_no, payload FROM raw_trade_events "
        "WHERE processed = 0 ORDER BY feed_file, line_no"
    ).fetchall()

    stats: Counter = Counter()
    rule_counts: Counter = Counter()

    with conn:  # single transaction for the whole run
        for row in rows:
            res = validate_event(
                row["payload"], ref, _feed_date(row["feed_file"]),
                stale_days_warn,
            )
            for issue in res.issues:
                conn.execute(
                    "INSERT INTO dq_issues (raw_id, severity, rule, detail) "
                    "VALUES (?, ?, ?, ?)",
                    (row["raw_id"], issue.severity, issue.rule, issue.detail),
                )
                rule_counts[issue.rule] += 1

            if res.rejected:
                conn.execute(
                    "UPDATE raw_trade_events SET processed = 2 WHERE raw_id = ?",
                    (row["raw_id"],),
                )
                stats["rejected"] += 1
                continue

            _promote(conn, ref, res.event, row["raw_id"])
            conn.execute(
                "UPDATE raw_trade_events SET processed = 1 WHERE raw_id = ?",
                (row["raw_id"],),
            )
            stats["promoted"] += 1
            stats["warned"] += any(i.severity == "WARN" for i in res.issues)

    stats["raw_rows"] = len(rows)
    summary = dict(stats)
    summary["by_rule"] = dict(rule_counts)
    log.info("pipeline run: %s", summary)
    return summary


def _promote(conn: sqlite3.Connection, ref: RefData, ev: dict, raw_id: int) -> None:
    etype, tid, version = ev["event_type"], ev["trade_id"], ev["version"]
    conn.execute(
        "INSERT INTO trade_events (trade_id, event_type, event_time, version, "
        "payload, raw_id) VALUES (?, ?, ?, ?, ?, ?)",
        (tid, etype, ev["event_time"], version, json.dumps(ev), raw_id),
    )

    if etype == "NEW":
        inst = ref.instruments[ev["instrument_id"]]
        notional = _notional(ev["asset_class"], ev["quantity"], ev["price"],
                             inst["contract_size"], ev["instrument_id"])
        conn.execute(
            """INSERT INTO trades
               (trade_id, client_id, instrument_id, asset_class, side,
                quantity, price, notional, currency, trade_time,
                settlement_date, trader, status, version, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))""",
            (tid, ev["client_id"], ev["instrument_id"], ev["asset_class"],
             ev["side"], ev["quantity"], ev["price"], notional,
             ev["currency"], ev["event_time"], ev.get("settlement_date"),
             ev["trader"], "NEW", version),
        )
        ref.trade_state[tid] = {"version": version, "status": "NEW"}

    elif etype == "AMEND":
        cur = conn.execute(
            "SELECT t.quantity, t.price, t.asset_class, t.instrument_id, "
            "i.contract_size "
            "FROM trades t JOIN instruments i USING (instrument_id) "
            "WHERE t.trade_id = ?", (tid,),
        ).fetchone()
        qty = ev["changes"].get("quantity", cur["quantity"])
        px = ev["changes"].get("price", cur["price"])
        notional = _notional(cur["asset_class"], qty, px,
                             cur["contract_size"], cur["instrument_id"])
        sets, params = ["status='AMENDED'", "version=?", "quantity=?",
                        "price=?", "notional=?", "updated_at=datetime('now')"], \
                       [version, qty, px, notional]
        for f in ("trader", "settlement_date"):
            if f in ev["changes"]:
                sets.append(f"{f}=?")
                params.append(ev["changes"][f])
        params.append(tid)
        conn.execute(f"UPDATE trades SET {', '.join(sets)} WHERE trade_id=?",
                     params)
        ref.trade_state[tid] = {"version": version, "status": "AMENDED"}

    else:  # CANCEL
        conn.execute(
            "UPDATE trades SET status='CANCELLED', version=?, "
            "updated_at=datetime('now') WHERE trade_id=?",
            (version, tid),
        )
        ref.trade_state[tid] = {"version": version, "status": "CANCELLED"}
