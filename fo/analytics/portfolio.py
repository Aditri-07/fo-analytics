"""Portfolio analytics.

Every function takes a connection plus plain-typed parameters and
returns JSON-serializable dicts/lists — deliberately, because these
become the LLM's tool set in step 4. The model will pick and
parameterize these functions; it never computes numbers itself.

Live trades = status != 'CANCELLED' throughout.
"""
from __future__ import annotations

import sqlite3

LIVE = "status != 'CANCELLED'"


def _usd_expr(qty: str, mark: str = "p.mark_price") -> str:
    """SQL expression converting a position quantity to USD exposure,
    honouring per-asset-class conventions (FI /100, commodity contract
    size, USD-base FX pairs = base qty)."""
    return f"""CASE WHEN p.asset_class = 'FX' AND p.instrument_id LIKE 'USD%'
                    THEN {qty}
               ELSE {qty} * {mark} *
                    CASE p.asset_class WHEN 'FIXED_INCOME' THEN 0.01
                         WHEN 'COMMODITY' THEN i.contract_size ELSE 1 END
               END"""


def _latest_date(conn: sqlite3.Connection) -> str | None:
    return conn.execute("SELECT MAX(as_of_date) FROM positions_eod").fetchone()[0]


def pnl_summary(
    conn: sqlite3.Connection,
    as_of_date: str | None = None,
    group_by: str = "client",          # 'client' | 'asset_class'
) -> list[dict]:
    """Realized + unrealized P&L as of a snapshot date."""
    as_of_date = as_of_date or _latest_date(conn)
    key = {"client": "p.client_id", "asset_class": "p.asset_class"}[group_by]
    label = "client_id" if group_by == "client" else "asset_class"
    rows = conn.execute(
        f"""SELECT {key} AS k,
                   ROUND(SUM(p.realized_pnl), 2)  AS realized_pnl,
                   ROUND(SUM(COALESCE(p.unrealized_pnl, 0)), 2) AS unrealized_pnl,
                   ROUND(SUM(p.realized_pnl + COALESCE(p.unrealized_pnl, 0)), 2)
                       AS total_pnl
            FROM positions_eod p
            WHERE p.as_of_date = ?
            GROUP BY {key} ORDER BY total_pnl DESC""",
        (as_of_date,),
    ).fetchall()
    return [{"as_of_date": as_of_date, label: r["k"],
             "realized_pnl": r["realized_pnl"],
             "unrealized_pnl": r["unrealized_pnl"],
             "total_pnl": r["total_pnl"]} for r in rows]


def exposure(
    conn: sqlite3.Connection,
    as_of_date: str | None = None,
    client_id: str | None = None,
) -> list[dict]:
    """Net and gross exposure (mark * point value) by client x asset class."""
    as_of_date = as_of_date or _latest_date(conn)
    where, params = ["p.as_of_date = ?", "p.mark_price IS NOT NULL"], [as_of_date]
    if client_id:
        where.append("p.client_id = ?")
        params.append(client_id)
    rows = conn.execute(
        f"""SELECT p.client_id, p.asset_class,
                   ROUND(SUM({_usd_expr('p.net_quantity')}), 2) AS net_exposure,
                   ROUND(SUM({_usd_expr('ABS(p.net_quantity)')}), 2) AS gross_exposure
            FROM positions_eod p JOIN instruments i USING (instrument_id)
            WHERE {' AND '.join(where)}
            GROUP BY p.client_id, p.asset_class
            ORDER BY ABS(net_exposure) DESC""",
        params,
    ).fetchall()
    return [dict(r) | {"as_of_date": as_of_date} for r in rows]


def concentration(
    conn: sqlite3.Connection, as_of_date: str | None = None, top_n: int = 5
) -> list[dict]:
    """Largest single-instrument share of each client's gross exposure."""
    as_of_date = as_of_date or _latest_date(conn)
    rows = conn.execute(
        f"""WITH g AS (
             SELECT p.client_id, p.instrument_id,
                    {_usd_expr('ABS(p.net_quantity)')} AS gross
             FROM positions_eod p JOIN instruments i USING (instrument_id)
             WHERE p.as_of_date = ? AND p.mark_price IS NOT NULL
               AND p.net_quantity != 0)
           SELECT client_id, instrument_id AS top_instrument,
                  ROUND(gross, 2) AS instrument_gross,
                  ROUND(100.0 * gross / SUM(gross) OVER (PARTITION BY client_id), 1)
                      AS pct_of_client_gross
           FROM g
           ORDER BY pct_of_client_gross DESC""",
        (as_of_date,),
    ).fetchall()
    seen, out = set(), []
    for r in rows:                       # keep each client's single largest line
        if r["client_id"] in seen:
            continue
        seen.add(r["client_id"])
        out.append(dict(r) | {"as_of_date": as_of_date})
        if len(out) >= top_n:
            break
    return out


def turnover(
    conn: sqlite3.Connection, start_date: str, end_date: str
) -> list[dict]:
    """Traded notional / average gross exposure per client over a period."""
    rows = conn.execute(
        f"""WITH traded AS (
             SELECT client_id, SUM(notional) AS traded_notional
             FROM trades
             WHERE {LIVE} AND date(trade_time) BETWEEN ? AND ?
             GROUP BY client_id),
           gross AS (
             SELECT p.client_id, p.as_of_date,
                    SUM({_usd_expr('ABS(p.net_quantity)')}) AS gross
             FROM positions_eod p JOIN instruments i USING (instrument_id)
             WHERE p.as_of_date BETWEEN ? AND ? AND p.mark_price IS NOT NULL
             GROUP BY p.client_id, p.as_of_date),
           avg_gross AS (
             SELECT client_id, AVG(gross) AS avg_gross FROM gross
             GROUP BY client_id)
           SELECT t.client_id,
                  ROUND(t.traded_notional, 2) AS traded_notional,
                  ROUND(a.avg_gross, 2) AS avg_gross_exposure,
                  ROUND(t.traded_notional / NULLIF(a.avg_gross, 0), 2)
                      AS turnover_ratio
           FROM traded t JOIN avg_gross a USING (client_id)
           ORDER BY turnover_ratio DESC""",
        (start_date, end_date, start_date, end_date),
    ).fetchall()
    return [dict(r) | {"start_date": start_date, "end_date": end_date}
            for r in rows]


def top_movers(
    conn: sqlite3.Connection,
    as_of_date: str | None = None,
    top_n: int = 10,
) -> list[dict]:
    """Largest day-over-day changes in net exposure (client x instrument)."""
    as_of_date = as_of_date or _latest_date(conn)
    prev = conn.execute(
        "SELECT MAX(as_of_date) FROM positions_eod WHERE as_of_date < ?",
        (as_of_date,),
    ).fetchone()[0]
    if prev is None:
        return []
    rows = conn.execute(
        f"""WITH e AS (
             SELECT p.as_of_date, p.client_id, p.instrument_id,
                    {_usd_expr('p.net_quantity', 'COALESCE(p.mark_price, p.avg_cost)')} AS net_exp
             FROM positions_eod p JOIN instruments i USING (instrument_id)
             WHERE p.as_of_date IN (?, ?))
           SELECT cur.client_id, cur.instrument_id,
                  ROUND(COALESCE(prv.net_exp, 0), 2) AS prev_exposure,
                  ROUND(cur.net_exp, 2) AS curr_exposure,
                  ROUND(cur.net_exp - COALESCE(prv.net_exp, 0), 2) AS change
           FROM e cur LEFT JOIN e prv
             ON prv.client_id = cur.client_id
            AND prv.instrument_id = cur.instrument_id
            AND prv.as_of_date = ?
           WHERE cur.as_of_date = ?
           ORDER BY ABS(change) DESC LIMIT ?""",
        (prev, as_of_date, prev, as_of_date, top_n),
    ).fetchall()
    return [dict(r) | {"as_of_date": as_of_date, "prev_date": prev}
            for r in rows]


def asset_class_breakdown(
    conn: sqlite3.Connection, start_date: str, end_date: str
) -> list[dict]:
    """Trade count and notional by asset class over a period."""
    rows = conn.execute(
        f"""SELECT asset_class, COUNT(*) AS n_trades,
                   ROUND(SUM(notional), 2) AS total_notional,
                   ROUND(100.0 * SUM(notional) /
                         SUM(SUM(notional)) OVER (), 1) AS pct_of_notional
            FROM trades
            WHERE {LIVE} AND date(trade_time) BETWEEN ? AND ?
            GROUP BY asset_class ORDER BY total_notional DESC""",
        (start_date, end_date),
    ).fetchall()
    return [dict(r) | {"start_date": start_date, "end_date": end_date}
            for r in rows]


def daily_pnl_series(
    conn: sqlite3.Connection, client_id: str | None = None
) -> list[dict]:
    """Firm (or one client's) total P&L per snapshot date — a history."""
    where, params = ["mark_price IS NOT NULL OR realized_pnl != 0"], []
    if client_id:
        where.append("client_id = ?")
        params.append(client_id)
    rows = conn.execute(
        f"""SELECT as_of_date,
                   ROUND(SUM(realized_pnl), 2) AS realized_pnl,
                   ROUND(SUM(COALESCE(unrealized_pnl, 0)), 2) AS unrealized_pnl,
                   ROUND(SUM(realized_pnl + COALESCE(unrealized_pnl, 0)), 2)
                       AS total_pnl
            FROM positions_eod
            WHERE {' AND '.join(where)}
            GROUP BY as_of_date ORDER BY as_of_date""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]
