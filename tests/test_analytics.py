import json
from pathlib import Path

import pytest

from fo.analytics import portfolio as pa
from fo.analytics import sales as sa
from fo.analytics.marks import build_marks
from fo.analytics.snapshots import build_snapshots, point_value
from fo.db.database import connect, init_schema, load_reference_data
from fo.etl.loader import load_feed_files
from fo.etl.pipeline import run_pipeline
from fo.simulator.feed_generator import FeedGenerator, SimConfig

SEED = 11


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("an")
    cfg = SimConfig(n_clients=15, n_days=15, seed=SEED, out_dir=tmp / "feeds")
    gen = FeedGenerator(cfg)
    gen.run()
    conn = connect(tmp / "t.db")
    init_schema(conn)
    load_reference_data(conn, cfg.out_dir)
    load_feed_files(conn, cfg.out_dir)
    run_pipeline(conn)
    build_marks(conn)
    build_snapshots(conn)
    yield {"conn": conn, "gen": gen}
    conn.close()


@pytest.fixture()
def db(env):
    return env["conn"]


@pytest.fixture()
def gen(env):
    return env["gen"]


# ----------------------------------------------------------------------
# Marks
# ----------------------------------------------------------------------
def test_marks_cover_all_days_with_carry_forward(db):
    days = db.execute("SELECT COUNT(DISTINCT as_of_date) FROM marks_eod").fetchone()[0]
    assert days == 15
    # Every traded instrument eventually has a mark every later day.
    gaps = db.execute(
        """SELECT COUNT(*) FROM (SELECT DISTINCT instrument_id FROM trades) i
           CROSS JOIN (SELECT DISTINCT as_of_date FROM marks_eod) d
           LEFT JOIN marks_eod m
             ON m.instrument_id = i.instrument_id AND m.as_of_date = d.as_of_date
           WHERE m.mark_price IS NULL
             AND d.as_of_date >= (SELECT MIN(date(trade_time)) FROM trades t2
                                  WHERE t2.instrument_id = i.instrument_id)"""
    ).fetchone()[0]
    assert gaps == 0


# ----------------------------------------------------------------------
# Snapshots
# ----------------------------------------------------------------------
def test_point_values():
    assert point_value("EQUITY", 1) == 1
    assert point_value("FIXED_INCOME", 1) == 0.01
    assert point_value("COMMODITY", 1000) == 1000


def test_snapshot_net_qty_matches_trade_state(db):
    """Final snapshot net qty must equal signed sum of live trades."""
    last = db.execute("SELECT MAX(as_of_date) FROM positions_eod").fetchone()[0]
    mismatches = db.execute(
        """WITH state AS (
             SELECT client_id, instrument_id,
                    SUM(CASE side WHEN 'BUY' THEN quantity ELSE -quantity END) q
             FROM trades WHERE status != 'CANCELLED'
             GROUP BY client_id, instrument_id),
           snap AS (
             SELECT client_id, instrument_id, net_quantity q
             FROM positions_eod WHERE as_of_date = ?)
           SELECT COUNT(*) FROM state s JOIN snap p USING (client_id, instrument_id)
           WHERE ABS(s.q - p.q) > 1e-6""",
        (last,),
    ).fetchone()[0]
    assert mismatches == 0


def test_cancelled_trades_leave_no_position(db):
    """A client+instrument whose only trades were cancelled nets to zero."""
    row = db.execute(
        """SELECT t.client_id, t.instrument_id FROM trades t
           GROUP BY t.client_id, t.instrument_id
           HAVING SUM(t.status != 'CANCELLED') = 0 AND COUNT(*) > 0
           LIMIT 1"""
    ).fetchone()
    if row is None:
        pytest.skip("no fully-cancelled client+instrument in this seed")
    last = db.execute("SELECT MAX(as_of_date) FROM positions_eod").fetchone()[0]
    q = db.execute(
        "SELECT net_quantity FROM positions_eod "
        "WHERE as_of_date=? AND client_id=? AND instrument_id=?",
        (last, row["client_id"], row["instrument_id"]),
    ).fetchone()
    assert q is None or abs(q[0]) < 1e-9


# ----------------------------------------------------------------------
# Portfolio analytics
# ----------------------------------------------------------------------
def test_pnl_summary_groupings_reconcile(db):
    by_client = pa.pnl_summary(db, group_by="client")
    by_ac = pa.pnl_summary(db, group_by="asset_class")
    assert by_client and by_ac
    assert sum(r["total_pnl"] for r in by_client) == pytest.approx(
        sum(r["total_pnl"] for r in by_ac), abs=1.0
    )


def test_exposure_gross_at_least_abs_net(db):
    for r in pa.exposure(db):
        assert r["gross_exposure"] + 1e-6 >= abs(r["net_exposure"])


def test_concentration_pct_bounded(db):
    for r in pa.concentration(db, top_n=10):
        assert 0 < r["pct_of_client_gross"] <= 100.0


def test_turnover_positive(db):
    start, end = db.execute(
        "SELECT MIN(as_of_date), MAX(as_of_date) FROM positions_eod"
    ).fetchone()
    rows = pa.turnover(db, start, end)
    assert rows and all(r["turnover_ratio"] is None or r["turnover_ratio"] >= 0
                        for r in rows)


def test_top_movers_sorted_by_abs_change(db):
    rows = pa.top_movers(db, top_n=8)
    changes = [abs(r["change"]) for r in rows]
    assert changes == sorted(changes, reverse=True)


# ----------------------------------------------------------------------
# Sales analytics — including ground-truth recovery from the simulator
# ----------------------------------------------------------------------
def test_dormant_detection_recovers_simulated_dormant_clients(db, gen):
    truth = {c.client_id for c in gen.clients if c.dormant}
    detected = {r["client_id"] for r in sa.dormant_clients(db, inactive_days=8)}
    if not truth:
        pytest.skip("seed produced no dormant clients")
    # Every truly dormant client must be detected (they trade ~4% of days).
    assert truth <= detected


def test_product_preferences_sum_to_100(db):
    from collections import defaultdict
    totals = defaultdict(float)
    for r in sa.product_preferences(db):
        totals[r["client_id"]] += r["pct_of_client"]
    for cid, pct in totals.items():
        assert pct == pytest.approx(100.0, abs=0.5), cid


def test_client_activity_comparison_math(db):
    start, end = db.execute(
        "SELECT date(MAX(as_of_date), '-6 day'), MAX(as_of_date) FROM positions_eod"
    ).fetchone()
    for r in sa.client_activity(db, start, end, compare_prior_period=True):
        assert r["change"] == pytest.approx(
            r["notional"] - r["prior_notional"], abs=0.05
        )


def test_opportunity_flags_have_reasons(db):
    for r in sa.opportunity_flags(db):
        assert r["flag"] in ("FADING", "SINGLE_PRODUCT") and r["reason"]
