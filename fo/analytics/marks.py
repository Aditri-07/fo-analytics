"""Build EOD marks from trade prints.

Mark = last traded price per instrument per business day, carried
forward on days with no prints. This is an honest proxy for vendor
closes; the `source` column records which days are carried.
"""
from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger("analytics.marks")


def build_marks(conn: sqlite3.Connection) -> dict:
    # Business-day calendar from the feed files themselves — event
    # timestamps can be backdated (stale WARNs), the calendar cannot.
    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(feed_file, 14, 10) FROM raw_trade_events ORDER BY 1"
    )]
    instruments = [r[0] for r in conn.execute(
        "SELECT instrument_id FROM instruments"
    )]

    # Last NEW/AMEND print per instrument per day (cancels carry no price).
    last_px: dict[tuple[str, str], float] = {}
    for r in conn.execute(
        """SELECT date(t.event_time) d, json_extract(t.payload,'$.instrument_id') iid,
                  COALESCE(json_extract(t.payload,'$.changes.price'),
                           json_extract(t.payload,'$.price')) px
           FROM trade_events t
           WHERE COALESCE(json_extract(t.payload,'$.changes.price'),
                          json_extract(t.payload,'$.price')) IS NOT NULL
           ORDER BY t.event_time"""
    ):
        if r["iid"] is not None:
            last_px[(r["d"], r["iid"])] = r["px"]

    rows, carried = [], 0
    prev: dict[str, float] = {}
    for d in days:
        for iid in instruments:
            px = last_px.get((d, iid))
            if px is None:
                if iid not in prev:
                    continue                  # never traded yet -> no mark
                px, src = prev[iid], "CARRY_FORWARD"
                carried += 1
            else:
                src = "LAST_TRADE"
            prev[iid] = px
            rows.append((d, iid, px, src))

    with conn:
        conn.execute("DELETE FROM marks_eod")
        conn.executemany(
            "INSERT INTO marks_eod (as_of_date, instrument_id, mark_price, source) "
            "VALUES (?, ?, ?, ?)", rows,
        )
    log.info("marks built: %d rows (%d carried) over %d days",
             len(rows), carried, len(days))
    return {"days": len(days), "marks": len(rows), "carried": carried}
