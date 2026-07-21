"""Python wrapper around the C++ position engine (fo_engine).

Pulls live fills from SQLite in audit order, hands them to the compiled
C++ replay loop, and returns positions keyed by (client_id,
instrument_id). Results reconcile exactly against the Python snapshot
builder — see tests/test_engine.py.
"""
from __future__ import annotations

import json
import sqlite3

import fo_engine


def replay_from_db(conn: sqlite3.Connection) -> dict:
    """Replay all validated trade events through the C++ engine."""
    events = conn.execute(
        "SELECT event_type, payload FROM trade_events ORDER BY event_id"
    ).fetchall()

    # Flatten lifecycle events into signed fills, mirroring snapshots.py:
    # NEW  -> one fill; AMEND -> reverse old + apply new; CANCEL -> reverse.
    trades: dict[str, dict] = {}   # trade_id -> current {client, inst, signed, price}
    client_ids, inst_ids, signed_qtys, prices = [], [], [], []

    def emit(client, inst, signed, price):
        client_ids.append(client)
        inst_ids.append(inst)
        signed_qtys.append(signed)
        prices.append(price)

    for ev in events:
        p = json.loads(ev["payload"])
        tid = p["trade_id"]
        if ev["event_type"] == "NEW":
            signed = p["quantity"] if p["side"] == "BUY" else -p["quantity"]
            rec = {"client": p["client_id"], "inst": p["instrument_id"],
                   "signed": signed, "price": p["price"]}
            trades[tid] = rec
            emit(rec["client"], rec["inst"], rec["signed"], rec["price"])
        elif ev["event_type"] == "AMEND":
            rec = trades[tid]
            emit(rec["client"], rec["inst"], -rec["signed"], rec["price"])  # reverse
            ch = p.get("changes", {})
            if "quantity" in ch:
                sign = 1.0 if rec["signed"] >= 0 else -1.0
                rec["signed"] = sign * ch["quantity"]
            if "price" in ch:
                rec["price"] = ch["price"]
            emit(rec["client"], rec["inst"], rec["signed"], rec["price"])  # replay
        else:  # CANCEL
            rec = trades[tid]
            emit(rec["client"], rec["inst"], -rec["signed"], rec["price"])

    return fo_engine.replay_positions(client_ids, inst_ids, signed_qtys, prices)
