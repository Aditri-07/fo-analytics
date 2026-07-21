import json
from datetime import datetime
from pathlib import Path

import pytest

from fo.db.database import connect, init_schema, load_reference_data
from fo.etl.loader import load_feed_files
from fo.etl.pipeline import run_pipeline
from fo.etl.validator import RefData, validate_event
from fo.simulator.feed_generator import FeedGenerator, SimConfig

FEED_DATE = datetime(2026, 6, 22)


def ref(state=None):
    return RefData(
        client_ids={"C001"},
        instruments={"AAPL": {"asset_class": "EQUITY", "contract_size": 1.0}},
        trade_state=state or {},
    )


def new_event(**over):
    ev = {
        "event_type": "NEW", "trade_id": "T1", "version": 1,
        "event_time": "2026-06-22T14:00:00+00:00", "client_id": "C001",
        "instrument_id": "AAPL", "asset_class": "EQUITY", "side": "BUY",
        "quantity": 100, "price": 50.0, "currency": "USD",
        "settlement_date": "2026-06-24", "trader": "akumar",
    }
    ev.update(over)
    return json.dumps(ev)


# ----------------------------------------------------------------------
# Validator unit tests: one per rule
# ----------------------------------------------------------------------
@pytest.mark.parametrize("payload,rule", [
    ('{"event_type": "NEW", "trunc', "MALFORMED_JSON"),
    (json.dumps({"event_type": "NOPE"}), "INVALID_EVENT_TYPE"),
    (new_event(price=None), "MISSING_FIELD"),
    (new_event(price=-5), "BAD_VALUE"),
    (new_event(quantity=0), "BAD_VALUE"),
    (new_event(side="HOLD"), "BAD_VALUE"),
    (new_event(client_id="ZZ_UNKNOWN"), "UNKNOWN_CLIENT"),
    (new_event(instrument_id="ZZ_UNKNOWN"), "UNKNOWN_INSTRUMENT"),
    (new_event(asset_class="FX"), "ASSET_CLASS_MISMATCH"),
])
def test_reject_rules(payload, rule):
    res = validate_event(payload, ref(), FEED_DATE)
    assert res.rejected
    assert rule in {i.rule for i in res.issues}


def test_duplicate_new_rejected():
    state = {"T1": {"version": 1, "status": "NEW"}}
    res = validate_event(new_event(), ref(state), FEED_DATE)
    assert res.rejected and res.issues[0].rule == "DUPLICATE_EVENT"


def test_orphan_amend_rejected():
    ev = json.dumps({"event_type": "AMEND", "trade_id": "TX", "version": 2,
                     "event_time": "2026-06-22T14:00:00+00:00",
                     "changes": {"price": 51.0}})
    res = validate_event(ev, ref(), FEED_DATE)
    assert res.rejected and res.issues[0].rule == "ORPHAN_EVENT"


def test_amend_after_cancel_rejected():
    state = {"T1": {"version": 2, "status": "CANCELLED"}}
    ev = json.dumps({"event_type": "AMEND", "trade_id": "T1", "version": 3,
                     "event_time": "2026-06-22T14:00:00+00:00",
                     "changes": {"price": 51.0}})
    res = validate_event(ev, ref(state), FEED_DATE)
    assert res.rejected and res.issues[0].rule == "AMEND_AFTER_CANCEL"


def test_version_gap_rejected():
    state = {"T1": {"version": 1, "status": "NEW"}}
    ev = json.dumps({"event_type": "CANCEL", "trade_id": "T1", "version": 5,
                     "event_time": "2026-06-22T14:00:00+00:00"})
    res = validate_event(ev, ref(state), FEED_DATE)
    assert res.rejected and res.issues[0].rule == "VERSION_OUT_OF_SEQUENCE"


def test_illegal_amend_field_rejected():
    state = {"T1": {"version": 1, "status": "NEW"}}
    ev = json.dumps({"event_type": "AMEND", "trade_id": "T1", "version": 2,
                     "event_time": "2026-06-22T14:00:00+00:00",
                     "changes": {"client_id": "C999"}})
    res = validate_event(ev, ref(state), FEED_DATE)
    assert res.rejected and res.issues[0].rule == "ILLEGAL_AMEND_FIELD"


def test_stale_timestamp_warns_but_promotes():
    res = validate_event(
        new_event(event_time="2026-06-01T14:00:00+00:00"), ref(), FEED_DATE
    )
    assert not res.rejected
    assert {i.rule for i in res.issues} == {"STALE_TIMESTAMP"}


def test_clean_event_passes():
    res = validate_event(new_event(), ref(), FEED_DATE)
    assert not res.rejected and not res.issues


# ----------------------------------------------------------------------
# End-to-end pipeline
# ----------------------------------------------------------------------
@pytest.fixture()
def db(tmp_path: Path):
    cfg = SimConfig(n_clients=12, n_days=5, seed=11, out_dir=tmp_path / "feeds")
    FeedGenerator(cfg).run()
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    load_reference_data(conn, cfg.out_dir)
    load_feed_files(conn, cfg.out_dir)
    yield conn
    conn.close()


def test_pipeline_end_to_end(db):
    summary = run_pipeline(db)
    assert summary["promoted"] > 0
    assert summary["rejected"] > 0           # dirty rows were injected
    assert summary["raw_rows"] == summary["promoted"] + summary["rejected"]

    # No trade in current state violates basic invariants.
    bad = db.execute(
        "SELECT COUNT(*) FROM trades WHERE quantity <= 0 OR price <= 0"
    ).fetchone()[0]
    assert bad == 0

    # Every promoted event has an audit row; versions are unique per trade.
    dup = db.execute(
        "SELECT trade_id, version, COUNT(*) c FROM trade_events "
        "GROUP BY trade_id, version HAVING c > 1"
    ).fetchall()
    assert dup == []

    # Cancelled trades in state match CANCEL events in the audit log.
    n_cancel_state = db.execute(
        "SELECT COUNT(*) FROM trades WHERE status='CANCELLED'"
    ).fetchone()[0]
    n_cancel_events = db.execute(
        "SELECT COUNT(*) FROM trade_events WHERE event_type='CANCEL'"
    ).fetchone()[0]
    assert n_cancel_state == n_cancel_events

    # dq_issues recorded rejects with rule codes.
    rules = {r[0] for r in db.execute("SELECT DISTINCT rule FROM dq_issues")}
    assert rules & {"MALFORMED_JSON", "MISSING_FIELD", "BAD_VALUE",
                    "UNKNOWN_CLIENT", "UNKNOWN_INSTRUMENT",
                    "DUPLICATE_EVENT"}


def test_pipeline_is_idempotent(db):
    first = run_pipeline(db)
    n_trades = db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    second = run_pipeline(db)               # nothing left to process
    assert second["raw_rows"] == 0
    assert db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == n_trades
    assert first["promoted"] > 0


def test_loader_skips_already_loaded_files(db, tmp_path):
    from fo.etl.loader import load_feed_files as load_again
    stats = load_again(db, tmp_path / "feeds")
    assert stats["files_loaded"] == 0
    assert stats["files_skipped"] == 5
