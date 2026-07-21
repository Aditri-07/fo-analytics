"""EOD position snapshots.

Replays the validated audit log (`trade_events`) chronologically through
the same `Position` class used by the OOP model — one Position per
(client, instrument) — and writes a snapshot at each day boundary.

P&L conventions: `Position` works in raw price terms; dollar P&L is
scaled by point value at write time:
    EQUITY / FX     : 1
    FIXED_INCOME    : 1/100        (face * clean-price move / 100)
    COMMODITY       : contract_size
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

from fo.models.enums import AssetClass
from fo.models.portfolio import Position

log = logging.getLogger("analytics.snapshots")


def point_value(asset_class: str, contract_size: float) -> float:
    if asset_class == "FIXED_INCOME":
        return 0.01
    if asset_class == "COMMODITY":
        return contract_size
    return 1.0


@dataclass
class _TradeRec:
    client_id: str
    instrument_id: str
    signed_qty: float
    price: float
    live: bool = True


def build_snapshots(conn: sqlite3.Connection) -> dict:
    contract_size = {
        r["instrument_id"]: r["contract_size"]
        for r in conn.execute("SELECT instrument_id, contract_size FROM instruments")
    }
    marks = {
        (r["as_of_date"], r["instrument_id"]): r["mark_price"]
        for r in conn.execute("SELECT * FROM marks_eod")
    }

    # Replay in audit (promotion) order, NOT event_time order: stale
    # backdated events (WARN-promoted) may be timestamped before their
    # trade's NEW. Audit order is the order the ETL validated state in,
    # so it is consistent by construction.
    events = conn.execute(
        "SELECT event_type, event_time, payload FROM trade_events "
        "ORDER BY event_id"
    ).fetchall()

    positions: dict[tuple[str, str], Position] = {}
    trades: dict[str, _TradeRec] = {}
    rows: list[tuple] = []
    current_day: str | None = None

    def pos_for(client_id: str, iid: str, ac: str) -> Position:
        return positions.setdefault(
            (client_id, iid), Position(iid, AssetClass(ac))
        )

    def flush_day(day: str) -> None:
        for (client_id, iid), pos in positions.items():
            pv = point_value(pos.asset_class.value, contract_size.get(iid, 1.0))
            mark = marks.get((day, iid))
            # USD-base FX (e.g. USDJPY): raw P&L is in quote ccy;
            # convert to USD at the day's mark (simple treatment).
            if pos.asset_class is AssetClass.FX and iid.startswith("USD"):
                rate = mark if mark else (pos.avg_cost or 1.0)
                pv = 1.0 / rate
            unreal = pos.unrealized_pnl(mark) * pv if mark is not None else None
            rows.append((
                day, client_id, iid, pos.asset_class.value,
                pos.net_quantity, pos.avg_cost, mark,
                pos.realized_pnl * pv, unreal,
            ))

    for ev in events:
        # Backdated (stale) events apply on the processing day — clamp
        # so day boundaries only move forward (no retroactive restatement).
        day = ev["event_time"][:10]
        if current_day is not None:
            day = max(day, current_day)
            if day != current_day:
                flush_day(current_day)
        current_day = day

        p = json.loads(ev["payload"])
        tid = p["trade_id"]
        if ev["event_type"] == "NEW":
            signed = p["quantity"] if p["side"] == "BUY" else -p["quantity"]
            rec = _TradeRec(p["client_id"], p["instrument_id"], signed, p["price"])
            trades[tid] = rec
            pos_for(rec.client_id, rec.instrument_id, p["asset_class"]).apply_fill(
                rec.signed_qty, rec.price
            )
        elif ev["event_type"] == "AMEND":
            rec = trades[tid]
            pos = positions[(rec.client_id, rec.instrument_id)]
            pos.apply_fill(-rec.signed_qty, rec.price)      # reverse old
            ch = p.get("changes", {})
            if "quantity" in ch:
                sign = 1.0 if rec.signed_qty >= 0 else -1.0
                rec.signed_qty = sign * ch["quantity"]
            if "price" in ch:
                rec.price = ch["price"]
            pos.apply_fill(rec.signed_qty, rec.price)       # replay new
        else:  # CANCEL
            rec = trades[tid]
            positions[(rec.client_id, rec.instrument_id)].apply_fill(
                -rec.signed_qty, rec.price
            )
            rec.live = False

    if current_day is not None:
        flush_day(current_day)

    with conn:
        conn.execute("DELETE FROM positions_eod")
        conn.executemany(
            """INSERT INTO positions_eod
               (as_of_date, client_id, instrument_id, asset_class,
                net_quantity, avg_cost, mark_price, realized_pnl, unrealized_pnl)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            rows,
        )
    log.info("snapshots built: %d rows", len(rows))
    return {"snapshot_rows": len(rows)}
