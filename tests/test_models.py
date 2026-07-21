import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fo.models.enums import AssetClass, ClientTier, Side, TradeStatus
from fo.models.portfolio import Client, Portfolio, Position
from fo.models.trade import BondTrade, CommodityTrade, EquityTrade

NOW = datetime(2026, 6, 22, 14, 0, tzinfo=timezone.utc)


def eq_trade(tid="T1", qty=100, px=50.0, side=Side.BUY):
    return EquityTrade(
        trade_id=tid, client_id="C001", instrument_id="AAPL",
        side=side, quantity=qty, price=px, trade_time=NOW, trader="akumar",
    )


def client():
    return Client("C001", "Alpine Capital", ClientTier.GOLD,
                  "Hedge Fund", "AMER", "2024-01-01")


# ----------------------------------------------------------------------
# Trade lifecycle
# ----------------------------------------------------------------------
def test_new_trade_has_audit_event():
    t = eq_trade()
    assert t.status is TradeStatus.NEW
    assert len(t.events) == 1 and t.version == 1


def test_amend_bumps_version_and_records_changes():
    t = eq_trade()
    t.apply_amendment({"quantity": 150})
    assert t.quantity == 150
    assert t.status is TradeStatus.AMENDED
    assert t.version == 2
    assert t.events[-1].changes == {"quantity": 150}


def test_amend_rejects_illegal_fields_and_values():
    t = eq_trade()
    with pytest.raises(ValueError):
        t.apply_amendment({"client_id": "C999"})
    with pytest.raises(ValueError):
        t.apply_amendment({"price": -1})


def test_cancel_is_terminal():
    t = eq_trade()
    t.cancel()
    assert not t.is_live
    with pytest.raises(ValueError):
        t.apply_amendment({"price": 60})
    with pytest.raises(ValueError):
        t.cancel()


def test_invalid_construction_rejected():
    with pytest.raises(ValueError):
        eq_trade(qty=-5)
    with pytest.raises(ValueError):
        eq_trade(px=0)


# ----------------------------------------------------------------------
# Asset-class notional conventions
# ----------------------------------------------------------------------
def test_bond_notional_per_100_face():
    b = BondTrade(trade_id="B1", client_id="C001", instrument_id="UST10Y",
                  side=Side.BUY, quantity=1_000_000, price=98.5,
                  trade_time=NOW, trader="akumar")
    assert b.notional() == pytest.approx(985_000)
    assert b.asset_class is AssetClass.FIXED_INCOME


def test_commodity_notional_uses_contract_size():
    c = CommodityTrade(trade_id="X1", client_id="C001", instrument_id="CL_F",
                       side=Side.SELL, quantity=10, price=80.0,
                       trade_time=NOW, trader="akumar", contract_size=1000)
    assert c.notional() == pytest.approx(800_000)
    assert c.signed_quantity() == -10


# ----------------------------------------------------------------------
# Position keeping
# ----------------------------------------------------------------------
def test_wac_and_realized_pnl():
    p = Position("AAPL", AssetClass.EQUITY)
    p.apply_fill(100, 50.0)
    p.apply_fill(100, 60.0)
    assert p.avg_cost == pytest.approx(55.0)
    p.apply_fill(-150, 70.0)          # sell 150 @ 70 vs cost 55
    assert p.realized_pnl == pytest.approx(150 * 15.0)
    assert p.net_quantity == pytest.approx(50)
    assert p.avg_cost == pytest.approx(55.0)


def test_position_flip_through_zero():
    p = Position("AAPL", AssetClass.EQUITY)
    p.apply_fill(100, 50.0)
    p.apply_fill(-160, 55.0)          # close 100, open 60 short @ 55
    assert p.realized_pnl == pytest.approx(500.0)
    assert p.net_quantity == pytest.approx(-60)
    assert p.avg_cost == pytest.approx(55.0)


def test_portfolio_cancel_reverses_position_exactly():
    pf = Portfolio(client())
    t = eq_trade(qty=200, px=40.0)
    pf.book(t)
    assert pf.positions["AAPL"].net_quantity == 200
    pf.cancel("T1")
    assert pf.positions["AAPL"].net_quantity == 0
    assert pf.positions["AAPL"].realized_pnl == pytest.approx(0.0)


def test_portfolio_amend_replays_economics():
    pf = Portfolio(client())
    pf.book(eq_trade(qty=100, px=50.0))
    pf.amend("T1", {"quantity": 250, "price": 52.0})
    pos = pf.positions["AAPL"]
    assert pos.net_quantity == pytest.approx(250)
    assert pos.avg_cost == pytest.approx(52.0)


def test_portfolio_rejects_duplicates_and_wrong_client():
    pf = Portfolio(client())
    pf.book(eq_trade())
    with pytest.raises(ValueError):
        pf.book(eq_trade())           # duplicate trade_id
    bad = eq_trade(tid="T2")
    bad.client_id = "C999"
    with pytest.raises(ValueError):
        pf.book(bad)


# ----------------------------------------------------------------------
# Simulator smoke test
# ----------------------------------------------------------------------
def test_simulator_is_deterministic_and_dirty(tmp_path: Path):
    from fo.simulator.feed_generator import FeedGenerator, SimConfig

    cfg = SimConfig(n_clients=10, n_days=3, seed=7, out_dir=tmp_path)
    stats = FeedGenerator(cfg).run()
    assert stats["days"] == 3
    assert stats["news"] > 0 and stats["dirty"] > 0

    files = sorted(tmp_path.glob("trade_events_*.jsonl"))
    assert len(files) == 3

    # At least one malformed or invalid line exists; valid ones parse.
    parsed = bad = 0
    for f in files:
        for line in f.read_text().splitlines():
            try:
                json.loads(line)
                parsed += 1
            except json.JSONDecodeError:
                bad += 1
    assert parsed > 0

    # Same seed -> identical output.
    cfg2 = SimConfig(n_clients=10, n_days=3, seed=7, out_dir=tmp_path / "b")
    FeedGenerator(cfg2).run()
    f1 = (tmp_path / files[0].name).read_text()
    f2 = (tmp_path / "b" / files[0].name).read_text()
    assert f1 == f2
