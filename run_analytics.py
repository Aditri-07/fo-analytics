"""Build marks + EOD snapshots, then print a demo analytics report.

Usage:
    FO_ENV=dev python run_analytics.py
"""
import logging

from fo.analytics import portfolio as pa
from fo.analytics import sales as sa
from fo.analytics.marks import build_marks
from fo.analytics.snapshots import build_snapshots
from fo.config import get_settings
from fo.db.database import connect, init_schema
from fo.logging_setup import setup_logging

log = logging.getLogger("analytics.main")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> None:
    s = get_settings()
    setup_logging(s.log_level)
    conn = connect(s.db_path)
    init_schema(conn)   # idempotent — picks up new tables

    print("building marks + snapshots ...")
    print(build_marks(conn), build_snapshots(conn))

    start, end = conn.execute(
        "SELECT MIN(as_of_date), MAX(as_of_date) FROM positions_eod"
    ).fetchone()
    mid = conn.execute("SELECT date(?, '-9 day')", (end,)).fetchone()[0]

    section(f"P&L by asset class (as of {end})")
    for r in pa.pnl_summary(conn, group_by="asset_class"):
        print(f"  {r['asset_class']:<13} realized {r['realized_pnl']:>14,.0f}  "
              f"unrealized {r['unrealized_pnl']:>14,.0f}")

    section("Top 5 client P&L")
    for r in pa.pnl_summary(conn)[:5]:
        print(f"  {r['client_id']}  total {r['total_pnl']:>14,.0f}")

    section("Top movers (day over day)")
    for r in pa.top_movers(conn, top_n=5):
        print(f"  {r['client_id']} {r['instrument_id']:<8} "
              f"{r['prev_exposure']:>14,.0f} -> {r['curr_exposure']:>14,.0f}")

    section(f"Asset-class breakdown ({start}..{end})")
    for r in pa.asset_class_breakdown(conn, start, end):
        print(f"  {r['asset_class']:<13} {r['n_trades']:>5} trades  "
              f"{r['pct_of_notional']:>5}% of notional")

    section(f"Clients increasing activity ({mid}..{end} vs prior)")
    for r in sa.client_activity(conn, mid, end, compare_prior_period=True)[:5]:
        pct = f"{r['change_pct']:+.0f}%" if r["change_pct"] is not None else "new"
        print(f"  {r['client_id']} {r['name'][:28]:<28} change {r['change']:>14,.0f} ({pct})")

    section("Dormant clients")
    for r in sa.dormant_clients(conn, inactive_days=10):
        last = r["last_trade_date"] or "never"
        print(f"  {r['client_id']} {r['name'][:28]:<28} [{r['tier']}] last: {last}")

    section("Opportunity flags")
    for r in sa.opportunity_flags(conn)[:8]:
        extra = r.get("dominant_product", "")
        print(f"  {r['flag']:<15} {r['client_id']} {r['name'][:24]:<24} {extra}")

    conn.close()


if __name__ == "__main__":
    main()
