"""Load feed files into the raw_trade_events landing zone.

Idempotent: a feed file already present in raw_trade_events is skipped,
so re-running the pipeline never double-loads. Raw lines are stored
verbatim — parsing/validation happens downstream, never at ingestion.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("etl.loader")


def load_feed_files(conn: sqlite3.Connection, feed_dir: Path) -> dict:
    files = sorted(feed_dir.glob("trade_events_*.jsonl"))
    stats = {"files_loaded": 0, "files_skipped": 0, "lines": 0}

    already = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT feed_file FROM raw_trade_events"
        )
    }
    for path in files:
        if path.name in already:
            stats["files_skipped"] += 1
            log.debug("skip %s (already loaded)", path.name)
            continue
        rows = [
            (path.name, i, line)
            for i, line in enumerate(path.read_text().splitlines(), start=1)
            if line.strip()
        ]
        with conn:  # one transaction per file
            conn.executemany(
                "INSERT INTO raw_trade_events (feed_file, line_no, payload) "
                "VALUES (?, ?, ?)",
                rows,
            )
        stats["files_loaded"] += 1
        stats["lines"] += len(rows)
        log.info("loaded %s (%d lines)", path.name, len(rows))
    return stats
