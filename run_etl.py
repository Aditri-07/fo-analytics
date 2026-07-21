"""Run the ETL: load feed files, validate, promote.

Usage:
    FO_ENV=dev python run_etl.py [--feeds data/feeds] [--db data/dev/fo_analytics.db]
"""
import argparse
import logging
from pathlib import Path

from fo.config import get_settings
from fo.db.database import connect, init_schema, load_reference_data
from fo.etl.loader import load_feed_files
from fo.etl.pipeline import run_pipeline
from fo.logging_setup import setup_logging

log = logging.getLogger("etl.main")


def main() -> None:
    s = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--feeds", type=Path, default=s.feed_dir)
    ap.add_argument("--db", type=Path, default=s.db_path)
    args = ap.parse_args()

    setup_logging(s.log_level)
    log.info("env=%s db=%s feeds=%s", s.env, args.db, args.feeds)

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(args.db)
    init_schema(conn)
    load_reference_data(conn, args.feeds)

    load_stats = load_feed_files(conn, args.feeds)
    log.info("loader: %s", load_stats)

    summary = run_pipeline(conn, stale_days_warn=s.stale_days_warn)
    log.info("done: promoted=%s rejected=%s warned=%s",
             summary.get("promoted", 0), summary.get("rejected", 0),
             summary.get("warned", 0))

    print("\n=== ETL summary ===")
    for k in ("raw_rows", "promoted", "rejected", "warned"):
        print(f"{k:>10}: {summary.get(k, 0)}")
    print("by rule:")
    for rule, n in sorted(summary["by_rule"].items(), key=lambda x: -x[1]):
        print(f"  {rule:<26} {n}")
    conn.close()


if __name__ == "__main__":
    main()
