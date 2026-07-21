"""Tool definitions + dispatch for the natural-language query layer.

Gemini reads the schemas below to decide WHICH analytics function to
call and with WHAT parameters. It never computes numbers itself — it
picks a function; our code runs it against SQLite and returns real rows.
"""
from __future__ import annotations

import sqlite3

from fo.analytics import portfolio as pa
from fo.analytics import sales as sa

# --- date helpers so the model can say "this month" etc. -------------
def _date_bounds(conn: sqlite3.Connection) -> tuple[str, str]:
    r = conn.execute(
        "SELECT MIN(as_of_date), MAX(as_of_date) FROM positions_eod"
    ).fetchone()
    return r[0], r[1]


# --- the functions the model is allowed to call ----------------------
# Each maps a tool name -> (python callable, how to supply conn/params).
def dispatch(conn: sqlite3.Connection, name: str, args: dict):
    start, end = _date_bounds(conn)
    args = args or {}

    if name == "pnl_summary":
        return pa.pnl_summary(conn, group_by=args.get("group_by", "client"))
    if name == "exposure":
        return pa.exposure(conn, client_id=args.get("client_id"))
    if name == "concentration":
        return pa.concentration(conn, top_n=args.get("top_n", 5))
    if name == "top_movers":
        return pa.top_movers(conn, top_n=args.get("top_n", 10))
    if name == "asset_class_breakdown":
        return pa.asset_class_breakdown(conn, start, end)
    if name == "turnover":
        return pa.turnover(conn, start, end)
    if name == "client_activity":
        # default window = last 10 snapshot days ("this month"-ish)
        s = args.get("start_date") or conn.execute(
            "SELECT date(?, '-9 day')", (end,)
        ).fetchone()[0]
        e = args.get("end_date") or end
        return sa.client_activity(
            conn, s, e,
            asset_class=args.get("asset_class"),
            compare_prior_period=args.get("compare_prior_period", False),
        )
    if name == "product_preferences":
        return sa.product_preferences(conn, client_id=args.get("client_id"))
    if name == "dormant_clients":
        return sa.dormant_clients(conn, inactive_days=args.get("inactive_days", 10))
    if name == "opportunity_flags":
        return sa.opportunity_flags(conn)

    raise ValueError(f"unknown tool: {name}")


# --- schemas handed to Gemini ----------------------------------------
TOOL_SCHEMAS = [
    {
        "name": "pnl_summary",
        "description": "Realized and unrealized P&L as of the latest date. "
                       "group_by is 'client' or 'asset_class'.",
        "parameters": {
            "type": "object",
            "properties": {
                "group_by": {"type": "string",
                             "enum": ["client", "asset_class"]},
            },
        },
    },
    {
        "name": "exposure",
        "description": "Net and gross USD exposure by client and asset "
                       "class. Optional client_id to filter to one client.",
        "parameters": {
            "type": "object",
            "properties": {"client_id": {"type": "string"}},
        },
    },
    {
        "name": "concentration",
        "description": "Each client's single largest instrument as a % of "
                       "their gross exposure. top_n limits how many clients.",
        "parameters": {
            "type": "object",
            "properties": {"top_n": {"type": "integer"}},
        },
    },
    {
        "name": "top_movers",
        "description": "Largest day-over-day changes in net exposure "
                       "(client x instrument). top_n limits rows.",
        "parameters": {
            "type": "object",
            "properties": {"top_n": {"type": "integer"}},
        },
    },
    {
        "name": "asset_class_breakdown",
        "description": "Trade count and notional by asset class over the "
                       "whole period.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "turnover",
        "description": "Traded notional divided by average gross exposure "
                       "per client, over the whole period.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "client_activity",
        "description": "Trading volume and trade counts per client over a "
                       "period. Set compare_prior_period=true to compare "
                       "against the previous equal-length window (use this "
                       "for questions about who INCREASED or DECREASED "
                       "activity). asset_class optionally filters to one of "
                       "EQUITY, FIXED_INCOME, FX, COMMODITY.",
        "parameters": {
            "type": "object",
            "properties": {
                "asset_class": {"type": "string",
                                "enum": ["EQUITY", "FIXED_INCOME",
                                         "FX", "COMMODITY"]},
                "compare_prior_period": {"type": "boolean"},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
            },
        },
    },
    {
        "name": "product_preferences",
        "description": "Asset-class mix per client (% of each client's "
                       "notional). Optional client_id for one client.",
        "parameters": {
            "type": "object",
            "properties": {"client_id": {"type": "string"}},
        },
    },
    {
        "name": "dormant_clients",
        "description": "Clients with no live trade in the last "
                       "inactive_days (relative to the latest trade date).",
        "parameters": {
            "type": "object",
            "properties": {"inactive_days": {"type": "integer"}},
        },
    },
    {
        "name": "opportunity_flags",
        "description": "Sales heuristics: FADING clients (activity dropping) "
                       "and SINGLE_PRODUCT clients (cross-sell candidates).",
        "parameters": {"type": "object", "properties": {}},
    },
]