"""Generate simulated feeds and bootstrap the analytics database.

Usage:
    python run_simulator.py [--days 20] [--clients 25] [--seed 42]
"""
import argparse
from pathlib import Path

from fo.db.database import connect, init_schema, load_reference_data
from fo.simulator.feed_generator import FeedGenerator, SimConfig


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--clients", type=int, default=25)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, default=Path("data/feeds"))
    ap.add_argument("--db", type=Path, default=Path("data/fo_analytics.db"))
    args = ap.parse_args()

    cfg = SimConfig(n_clients=args.clients, n_days=args.days,
                    seed=args.seed, out_dir=args.out)
    stats = FeedGenerator(cfg).run()
    print(f"feed generated: {stats}")

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(args.db)
    init_schema(conn)
    counts = load_reference_data(conn, args.out)
    print(f"db bootstrapped at {args.db}: {counts}")
    conn.close()


if __name__ == "__main__":
    main()
