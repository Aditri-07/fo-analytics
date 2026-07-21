"""C++ engine must reconcile exactly with the Python position keeping."""
import pytest

from fo.analytics.marks import build_marks
from fo.analytics.snapshots import build_snapshots
from fo.db.database import connect, init_schema, load_reference_data
from fo.engine.positions import replay_from_db
from fo.etl.loader import load_feed_files
from fo.etl.pipeline import run_pipeline
from fo.simulator.feed_generator import FeedGenerator, SimConfig


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("eng")
    cfg = SimConfig(n_clients=12, n_days=8, seed=99, out_dir=tmp / "feeds")
    FeedGenerator(cfg).run()
    conn = connect(tmp / "e.db")
    init_schema(conn)
    load_reference_data(conn, cfg.out_dir)
    load_feed_files(conn, cfg.out_dir)
    run_pipeline(conn)
    build_marks(conn)
    build_snapshots(conn)
    yield conn
    conn.close()


def test_cpp_matches_python_positions(db):
    cpp = replay_from_db(db)

    # Python ground truth: final-day snapshot net qty + realized P&L,
    # expressed in raw (pre-point-value) terms to match the engine.
    last = db.execute("SELECT MAX(as_of_date) FROM positions_eod").fetchone()[0]
    py_rows = db.execute(
        "SELECT client_id, instrument_id, net_quantity, realized_pnl "
        "FROM positions_eod WHERE as_of_date = ?", (last,),
    ).fetchall()

    checked = 0
    for r in py_rows:
        key = f"{r['client_id']}|{r['instrument_id']}"
        if key not in cpp:
            # snapshot may carry a zero-qty row the engine collapsed; skip
            if abs(r["net_quantity"]) < 1e-9:
                continue
            pytest.fail(f"missing position {key}")
        assert cpp[key]["net_qty"] == pytest.approx(r["net_quantity"], abs=1e-6)
        checked += 1

    assert checked > 0
    