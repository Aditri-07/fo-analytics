"""Database bootstrap.

Step 1 responsibilities only: create the schema and load reference data
(clients, instruments). Trade-event loading belongs to the ETL (step 2).
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()


def load_reference_data(conn: sqlite3.Connection, feed_dir: str | Path) -> dict:
    feed_dir = Path(feed_dir)
    counts = {}
    with open(feed_dir / "clients.csv", newline="") as f:
        rows = list(csv.DictReader(f))
        conn.executemany(
            """INSERT OR REPLACE INTO clients
               (client_id, name, tier, sector, region, onboarded)
               VALUES (:client_id, :name, :tier, :sector, :region, :onboarded)""",
            rows,
        )
        counts["clients"] = len(rows)
    with open(feed_dir / "instruments.csv", newline="") as f:
        rows = list(csv.DictReader(f))
        conn.executemany(
            """INSERT OR REPLACE INTO instruments
               (instrument_id, asset_class, description, currency, contract_size)
               VALUES (:instrument_id, :asset_class, :description,
                       :currency, :contract_size)""",
            rows,
        )
        counts["instruments"] = len(rows)
    conn.commit()
    return counts
