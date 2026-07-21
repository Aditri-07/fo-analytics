"""Validation of raw trade events.

Each raw row is checked against a rule set; the outcome is either
REJECT (never promoted, logged to dq_issues) or WARN (promoted, but
flagged in dq_issues). Rules carry stable codes so DQ reporting can
aggregate by rule.

Rule codes
----------
REJECT: MALFORMED_JSON, INVALID_EVENT_TYPE, MISSING_FIELD, BAD_VALUE,
        UNKNOWN_CLIENT, UNKNOWN_INSTRUMENT, ASSET_CLASS_MISMATCH,
        ILLEGAL_AMEND_FIELD, DUPLICATE_EVENT, ORPHAN_EVENT,
        AMEND_AFTER_CANCEL, VERSION_OUT_OF_SEQUENCE
WARN:   STALE_TIMESTAMP, MISSING_SETTLEMENT_DATE
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fo.models.trade import AMENDABLE_FIELDS

REQUIRED_NEW = (
    "trade_id", "version", "event_time", "client_id", "instrument_id",
    "asset_class", "side", "quantity", "price", "currency", "trader",
)
REQUIRED_AMEND = ("trade_id", "version", "event_time", "changes")
REQUIRED_CANCEL = ("trade_id", "version", "event_time")


@dataclass
class Issue:
    severity: str      # 'REJECT' | 'WARN'
    rule: str
    detail: str


@dataclass
class ValidationResult:
    event: dict | None                       # parsed event if usable
    issues: list[Issue] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return any(i.severity == "REJECT" for i in self.issues)


@dataclass
class RefData:
    """Reference/state context the validator checks against."""
    client_ids: set[str]
    instruments: dict[str, dict]             # instrument_id -> {asset_class, contract_size}
    # live trade state: trade_id -> {"version": int, "status": str}
    trade_state: dict[str, dict]


def validate_event(
    payload: str,
    ref: RefData,
    feed_date: datetime,
    stale_days_warn: int = 3,
) -> ValidationResult:
    res = ValidationResult(event=None)

    # ---- parse ---------------------------------------------------------
    try:
        ev = json.loads(payload)
    except json.JSONDecodeError as e:
        res.issues.append(Issue("REJECT", "MALFORMED_JSON", str(e)))
        return res
    if not isinstance(ev, dict):
        res.issues.append(Issue("REJECT", "MALFORMED_JSON", "not an object"))
        return res
    res.event = ev

    etype = ev.get("event_type")
    if etype not in ("NEW", "AMEND", "CANCEL"):
        res.issues.append(Issue("REJECT", "INVALID_EVENT_TYPE", f"{etype!r}"))
        return res

    # ---- required fields ----------------------------------------------
    required = {"NEW": REQUIRED_NEW, "AMEND": REQUIRED_AMEND,
                "CANCEL": REQUIRED_CANCEL}[etype]
    missing = [f for f in required if f not in ev or ev[f] in (None, "")]
    if missing:
        res.issues.append(Issue("REJECT", "MISSING_FIELD", ",".join(missing)))
        return res

    # ---- value checks --------------------------------------------------
    if etype == "NEW":
        if ev["side"] not in ("BUY", "SELL"):
            res.issues.append(Issue("REJECT", "BAD_VALUE", f"side={ev['side']!r}"))
        if not _positive(ev["quantity"]):
            res.issues.append(Issue("REJECT", "BAD_VALUE", f"quantity={ev['quantity']!r}"))
        if not _positive(ev["price"]):
            res.issues.append(Issue("REJECT", "BAD_VALUE", f"price={ev['price']!r}"))
        if res.rejected:
            return res

        if ev["client_id"] not in ref.client_ids:
            res.issues.append(Issue("REJECT", "UNKNOWN_CLIENT", ev["client_id"]))
        inst = ref.instruments.get(ev["instrument_id"])
        if inst is None:
            res.issues.append(Issue("REJECT", "UNKNOWN_INSTRUMENT", ev["instrument_id"]))
        elif inst["asset_class"] != ev["asset_class"]:
            res.issues.append(Issue(
                "REJECT", "ASSET_CLASS_MISMATCH",
                f"event={ev['asset_class']} ref={inst['asset_class']}",
            ))
        if not ev.get("settlement_date"):
            res.issues.append(Issue("WARN", "MISSING_SETTLEMENT_DATE", ev["trade_id"]))

    elif etype == "AMEND":
        changes = ev["changes"]
        if not isinstance(changes, dict) or not changes:
            res.issues.append(Issue("REJECT", "BAD_VALUE", "empty changes"))
            return res
        illegal = set(changes) - AMENDABLE_FIELDS
        if illegal:
            res.issues.append(Issue(
                "REJECT", "ILLEGAL_AMEND_FIELD", ",".join(sorted(illegal))
            ))
        for f in ("quantity", "price"):
            if f in changes and not _positive(changes[f]):
                res.issues.append(Issue("REJECT", "BAD_VALUE", f"{f}={changes[f]!r}"))

    if res.rejected:
        return res

    # ---- lifecycle / sequencing ---------------------------------------
    tid = ev["trade_id"]
    state = ref.trade_state.get(tid)
    if etype == "NEW":
        if state is not None:
            res.issues.append(Issue("REJECT", "DUPLICATE_EVENT",
                                    f"{tid} v{ev['version']}"))
    else:
        if state is None:
            res.issues.append(Issue("REJECT", "ORPHAN_EVENT",
                                    f"{etype} for unknown {tid}"))
        elif state["status"] == "CANCELLED":
            res.issues.append(Issue("REJECT", "AMEND_AFTER_CANCEL", tid))
        elif ev["version"] != state["version"] + 1:
            res.issues.append(Issue(
                "REJECT", "VERSION_OUT_OF_SEQUENCE",
                f"{tid}: got v{ev['version']}, have v{state['version']}",
            ))
    if res.rejected:
        return res

    # ---- staleness (WARN only) ----------------------------------------
    try:
        ev_time = datetime.fromisoformat(ev["event_time"])
    except (ValueError, TypeError):
        res.issues.append(Issue("REJECT", "BAD_VALUE",
                                f"event_time={ev['event_time']!r}"))
        return res
    if feed_date - ev_time.replace(tzinfo=None) > timedelta(days=stale_days_warn):
        res.issues.append(Issue(
            "WARN", "STALE_TIMESTAMP",
            f"{tid}: event {ev['event_time']} vs feed {feed_date.date()}",
        ))
    return res


def _positive(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0
