"""One-command run: simulate -> ETL -> analytics.

Usage:
    python run_all.py                 # full pipeline, default settings
    python run_all.py --fresh        # wipe data/ first, rebuild from scratch
    python run_all.py --days 30 --clients 40 --seed 7
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str]) -> None:
    print(f"\n>>> {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(result.returncode)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--clients", type=int, default=25)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--fresh", action="store_true",
                    help="delete data/ and logs/ before running")
    args = ap.parse_args()

    if args.fresh:
        for d in ("data", "logs"):
            shutil.rmtree(d, ignore_errors=True)
        print("wiped data/ and logs/")

    py = sys.executable
    run([py, "run_simulator.py", "--days", str(args.days),
         "--clients", str(args.clients), "--seed", str(args.seed)])
    run([py, "run_etl.py"])
    run([py, "run_analytics.py"])
    print("\nAll stages complete.")


if __name__ == "__main__":
    main()
