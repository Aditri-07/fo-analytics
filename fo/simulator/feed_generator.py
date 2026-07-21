"""Simulated trade-event feed generator.

Produces what an upstream booking system would drop on a desk each day:
- one JSONL file per business day of NEW / AMEND / CANCEL events
- reference-data CSVs (clients, instruments)

Realism features
----------------
- Client profiles drive behaviour: activity level (Poisson intensity),
  asset-class preferences, and dormancy. Sales analytics in step 3 will
  rediscover these patterns from the data.
- Instrument prices follow a lognormal random walk day to day, and
  intraday fills are jittered around the daily level.
- A configurable fraction of trades are later amended or cancelled.

Dirty-data injection (for the ETL to catch in step 2)
-----------------------------------------------------
- exact duplicate rows
- missing required fields
- non-positive quantity/price
- unknown client or instrument ids
- stale timestamps (events dated days in the past)
- malformed JSON lines

Everything is seeded, so runs are reproducible.
"""
from __future__ import annotations

import csv
import json
import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from fo.models.enums import AssetClass, ClientTier

# ----------------------------------------------------------------------
# Static universes
# ----------------------------------------------------------------------
INSTRUMENTS: dict[str, dict] = {
    # equities
    "AAPL":  {"asset_class": "EQUITY", "price": 232.0, "vol": 0.018, "description": "Apple Inc"},
    "MSFT":  {"asset_class": "EQUITY", "price": 415.0, "vol": 0.016, "description": "Microsoft Corp"},
    "NVDA":  {"asset_class": "EQUITY", "price": 128.0, "vol": 0.030, "description": "NVIDIA Corp"},
    "JPM":   {"asset_class": "EQUITY", "price": 248.0, "vol": 0.014, "description": "JPMorgan Chase"},
    "XOM":   {"asset_class": "EQUITY", "price": 118.0, "vol": 0.015, "description": "Exxon Mobil"},
    # fixed income (clean price per 100 face)
    "UST10Y":   {"asset_class": "FIXED_INCOME", "price": 98.4,  "vol": 0.004, "description": "US Treasury 10Y"},
    "UST2Y":    {"asset_class": "FIXED_INCOME", "price": 99.6,  "vol": 0.002, "description": "US Treasury 2Y"},
    "CORP_IG5": {"asset_class": "FIXED_INCOME", "price": 101.2, "vol": 0.005, "description": "IG Corporate 5Y"},
    "CORP_HY7": {"asset_class": "FIXED_INCOME", "price": 96.8,  "vol": 0.009, "description": "HY Corporate 7Y"},
    # fx (rate)
    "EURUSD": {"asset_class": "FX", "price": 1.085, "vol": 0.006, "description": "EUR/USD spot"},
    "USDJPY": {"asset_class": "FX", "price": 154.2, "vol": 0.007, "description": "USD/JPY spot"},
    "GBPUSD": {"asset_class": "FX", "price": 1.268, "vol": 0.006, "description": "GBP/USD spot"},
    # commodities (futures-style, with contract sizes)
    "CL_F": {"asset_class": "COMMODITY", "price": 78.5,   "vol": 0.022, "contract_size": 1000.0, "description": "WTI Crude future"},
    "GC_F": {"asset_class": "COMMODITY", "price": 2380.0, "vol": 0.012, "contract_size": 100.0,  "description": "Gold future"},
    "NG_F": {"asset_class": "COMMODITY", "price": 2.85,   "vol": 0.035, "contract_size": 10000.0,"description": "Henry Hub NatGas future"},
}

SECTORS = ["Hedge Fund", "Asset Manager", "Pension", "Insurance", "Corporate", "Bank"]
REGIONS = ["AMER", "EMEA", "APAC"]
TRADERS = ["akumar", "jsmith", "mchen", "tpatel", "rlopez"]

# Typical order-of-magnitude quantity per asset class.
QTY_SCALE = {
    "EQUITY": 2_000,        # shares
    "FIXED_INCOME": 1_500_000,  # face
    "FX": 3_000_000,        # base ccy
    "COMMODITY": 25,        # contracts
}


@dataclass
class ClientProfile:
    client_id: str
    name: str
    tier: ClientTier
    sector: str
    region: str
    onboarded: str
    daily_intensity: float                  # expected trades/day when active
    ac_weights: dict[str, float]            # asset-class preference
    dormant: bool = False


@dataclass
class SimConfig:
    n_clients: int = 25
    n_days: int = 20
    start_date: date = date(2026, 6, 22)    # a Monday
    seed: int = 42
    amend_prob: float = 0.06
    cancel_prob: float = 0.03
    dormant_frac: float = 0.15
    # dirty-data rates (per emitted event)
    err_duplicate: float = 0.010
    err_missing_field: float = 0.008
    err_bad_value: float = 0.006
    err_unknown_ref: float = 0.005
    err_stale_ts: float = 0.005
    err_malformed: float = 0.003
    out_dir: Path = field(default_factory=lambda: Path("data/feeds"))


class FeedGenerator:
    def __init__(self, cfg: SimConfig):
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        self.clients = self._make_clients()
        self.prices = {k: v["price"] for k, v in INSTRUMENTS.items()}
        self._trade_seq = 0
        self._booked: list[dict] = []  # live NEW events eligible for amend/cancel

    # ------------------------------------------------------------------
    def _make_clients(self) -> list[ClientProfile]:
        rng = self.rng
        tiers = [ClientTier.PLATINUM] * 4 + [ClientTier.GOLD] * 8
        tiers += [ClientTier.SILVER] * (self.cfg.n_clients - len(tiers))
        clients = []
        for i, tier in enumerate(tiers[: self.cfg.n_clients], start=1):
            # Preference: draw Dirichlet-ish weights, spiky so clients
            # have a recognisable product bias.
            raw = [rng.expovariate(1.0) ** 2 for _ in AssetClass]
            total = sum(raw)
            weights = {ac.value: w / total for ac, w in zip(AssetClass, raw)}
            intensity = {
                ClientTier.PLATINUM: rng.uniform(8, 15),
                ClientTier.GOLD: rng.uniform(3, 8),
                ClientTier.SILVER: rng.uniform(0.5, 3),
            }[tier]
            clients.append(
                ClientProfile(
                    client_id=f"C{i:03d}",
                    name=f"{rng.choice(['Alpine','Meridian','Vantage','Harbor','Quarry','Citadelle','Northgate','Blueline'])} "
                         f"{rng.choice(['Capital','Partners','Advisors','Asset Mgmt','Investments'])} {i}",
                    tier=tier,
                    sector=rng.choice(SECTORS),
                    region=rng.choice(REGIONS),
                    onboarded=(self.cfg.start_date - timedelta(days=rng.randint(90, 2000))).isoformat(),
                    daily_intensity=intensity,
                    ac_weights=weights,
                    dormant=rng.random() < self.cfg.dormant_frac,
                )
            )
        return clients

    # ------------------------------------------------------------------
    def run(self) -> dict:
        out = self.cfg.out_dir
        out.mkdir(parents=True, exist_ok=True)
        self._write_reference_data(out)

        stats = {"days": 0, "events": 0, "news": 0, "amends": 0,
                 "cancels": 0, "dirty": 0}
        d = self.cfg.start_date
        days_done = 0
        while days_done < self.cfg.n_days:
            if d.weekday() < 5:  # business days only
                n = self._write_day(out, d, stats)
                stats["events"] += n
                days_done += 1
                stats["days"] += 1
            d += timedelta(days=1)
        return stats

    # ------------------------------------------------------------------
    def _write_reference_data(self, out: Path) -> None:
        with open(out / "clients.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["client_id", "name", "tier", "sector", "region", "onboarded"])
            for c in self.clients:
                w.writerow([c.client_id, c.name, c.tier.value, c.sector,
                            c.region, c.onboarded])
        with open(out / "instruments.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["instrument_id", "asset_class", "description",
                        "currency", "contract_size"])
            for iid, meta in INSTRUMENTS.items():
                w.writerow([iid, meta["asset_class"], meta["description"],
                            "USD", meta.get("contract_size", 1.0)])

    # ------------------------------------------------------------------
    def _write_day(self, out: Path, d: date, stats: dict) -> int:
        rng = self.rng
        self._evolve_prices()
        events: list[tuple[datetime, dict]] = []

        for c in self.clients:
            if c.dormant and rng.random() > 0.04:  # dormant ≈ rare one-offs
                continue
            n_trades = self._poisson(c.daily_intensity)
            for _ in range(n_trades):
                ev = self._new_trade_event(c, d)
                events.append((self._parse_ts(ev["event_time"]), ev))
                stats["news"] += 1

        # Amend/cancel a slice of previously booked trades.
        for booked in list(self._booked):
            r = rng.random()
            if r < self.cfg.cancel_prob:
                ev = self._lifecycle_event(booked, "CANCEL", d)
                events.append((self._parse_ts(ev["event_time"]), ev))
                self._booked.remove(booked)
                stats["cancels"] += 1
            elif r < self.cfg.cancel_prob + self.cfg.amend_prob:
                ev = self._lifecycle_event(booked, "AMEND", d)
                events.append((self._parse_ts(ev["event_time"]), ev))
                booked["version"] += 1
                stats["amends"] += 1

        events.sort(key=lambda t: t[0])
        path = out / f"trade_events_{d.isoformat()}.jsonl"
        with open(path, "w") as f:
            for _, ev in events:
                for line in self._maybe_dirty(ev, stats):
                    f.write(line + "\n")
        return len(events)

    # ------------------------------------------------------------------
    def _new_trade_event(self, c: ClientProfile, d: date) -> dict:
        rng = self.rng
        ac = rng.choices(list(c.ac_weights), weights=list(c.ac_weights.values()))[0]
        candidates = [k for k, v in INSTRUMENTS.items() if v["asset_class"] == ac]
        iid = rng.choice(candidates)
        base_px = self.prices[iid]
        px = base_px * math.exp(rng.gauss(0, INSTRUMENTS[iid]["vol"] * 0.5))
        qty = max(1.0, rng.lognormvariate(0, 0.6) * QTY_SCALE[ac])
        if ac in ("EQUITY", "COMMODITY"):
            qty = float(int(qty))
        self._trade_seq += 1
        ts = self._intraday_ts(d)
        ev = {
            "event_type": "NEW",
            "trade_id": f"T{self._trade_seq:07d}",
            "version": 1,
            "event_time": ts,
            "client_id": c.client_id,
            "instrument_id": iid,
            "asset_class": ac,
            "side": rng.choice(["BUY", "SELL"]),
            "quantity": round(qty, 2),
            "price": round(px, 6),
            "currency": "USD",
            "settlement_date": (d + timedelta(days=2)).isoformat(),
            "trader": rng.choice(TRADERS),
        }
        # Keep a small pool eligible for later amend/cancel.
        if len(self._booked) < 400:
            self._booked.append(dict(ev))
        return ev

    def _lifecycle_event(self, booked: dict, kind: str, d: date) -> dict:
        rng = self.rng
        ev = {
            "event_type": kind,
            "trade_id": booked["trade_id"],
            "version": booked["version"] + 1,
            "event_time": self._intraday_ts(d),
        }
        if kind == "AMEND":
            fld = rng.choice(["quantity", "price"])
            factor = rng.uniform(0.85, 1.15)
            ev["changes"] = {fld: round(booked[fld] * factor, 6)}
            booked[fld] = ev["changes"][fld]
        return ev

    # ------------------------------------------------------------------
    # Dirty-data injection
    # ------------------------------------------------------------------
    def _maybe_dirty(self, ev: dict, stats: dict) -> list[str]:
        rng, cfg = self.rng, self.cfg
        line = json.dumps(ev)
        out = [line]
        r = rng.random()
        t = 0.0
        if r < (t := t + cfg.err_duplicate):
            out.append(line)                                   # exact dup
        elif r < (t := t + cfg.err_missing_field):
            bad = dict(ev)
            bad.pop(rng.choice(["price", "quantity", "client_id", "trader"]), None)
            out = [json.dumps(bad)]
        elif r < (t := t + cfg.err_bad_value):
            bad = dict(ev)
            if "price" in bad:
                bad["price"] = rng.choice([0, -abs(bad["price"])])
            out = [json.dumps(bad)]
        elif r < (t := t + cfg.err_unknown_ref):
            bad = dict(ev)
            key = rng.choice(["client_id", "instrument_id"])
            if key in bad:
                bad[key] = "ZZ_UNKNOWN"
            out = [json.dumps(bad)]
        elif r < (t := t + cfg.err_stale_ts):
            bad = dict(ev)
            ts = self._parse_ts(bad["event_time"]) - timedelta(days=rng.randint(5, 30))
            bad["event_time"] = ts.isoformat()
            out = [json.dumps(bad)]
        elif r < (t := t + cfg.err_malformed):
            out = [line[: max(10, len(line) // 2)]]            # truncated JSON
        else:
            return out
        stats["dirty"] += 1
        return out

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _evolve_prices(self) -> None:
        for iid, meta in INSTRUMENTS.items():
            self.prices[iid] *= math.exp(self.rng.gauss(0, meta["vol"]))

    def _poisson(self, lam: float) -> int:
        # Knuth's algorithm; fine for small lambda.
        L, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= L:
                return k
            k += 1

    def _intraday_ts(self, d: date) -> str:
        secs = self.rng.randint(0, int(6.5 * 3600))  # 09:30–16:00 ET as UTC-4
        t = (datetime.combine(d, time(13, 30), tzinfo=timezone.utc)
             + timedelta(seconds=secs))
        return t.isoformat()

    @staticmethod
    def _parse_ts(s: str) -> datetime:
        return datetime.fromisoformat(s)
