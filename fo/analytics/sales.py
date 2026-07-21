"""Sales & client analytics.

Same contract as portfolio analytics: connection + plain params in,
JSON-serializable rows out. These answer the questions salespeople
actually ask: who's active, who went quiet, what does each client
trade, where's the next conversation.
"""
from __future__ import annotations

import sqlite3

LIVE = "status != 'CANCELLED'"


def _bounds(conn: sqlite3.Connection) -> tuple[str, str]:
    r = conn.execute(
        f"SELECT MIN(date(trade_time)), MAX(date(trade_time)) FROM trades WHERE {LIVE}"
    ).fetchone()
    return r[0], r[1]


def client_activity(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    asset_class: str | None = None,
    compare_prior_period: bool = False,
) -> list[dict]:
    """Volumes and trade counts per client, optionally vs the prior
    period of equal length (this powers 'which clients increased X
    activity this month?')."""
    where, params = [LIVE, "date(trade_time) BETWEEN ? AND ?"], [start_date, end_date]
    if asset_class:
        where.append("asset_class = ?")
        params.append(asset_class)
    cur = {
        r["client_id"]: dict(r)
        for r in conn.execute(
            f"""SELECT t.client_id, c.name, c.tier, COUNT(*) AS n_trades,
                       ROUND(SUM(t.notional), 2) AS notional
                FROM trades t JOIN clients c USING (client_id)
                WHERE {' AND '.join(where)}
                GROUP BY t.client_id""",
            params,
        )
    }
    if not compare_prior_period:
        return sorted(cur.values(), key=lambda r: -r["notional"])

    span = conn.execute(
        "SELECT julianday(?) - julianday(?)", (end_date, start_date)
    ).fetchone()[0]
    p_end = conn.execute(
        "SELECT date(?, '-1 day')", (start_date,)
    ).fetchone()[0]
    p_start = conn.execute(
        "SELECT date(?, ?)", (p_end, f"-{int(span)} day")
    ).fetchone()[0]

    prior_params = [p_start, p_end] + ([asset_class] if asset_class else [])
    prior = {
        r["client_id"]: r["notional"]
        for r in conn.execute(
            f"""SELECT client_id, SUM(notional) AS notional FROM trades
                WHERE {LIVE} AND date(trade_time) BETWEEN ? AND ?
                {'AND asset_class = ?' if asset_class else ''}
                GROUP BY client_id""",
            prior_params,
        )
    }
    out = []
    for cid, row in cur.items():
        prev = prior.get(cid, 0.0) or 0.0
        row["prior_notional"] = round(prev, 2)
        row["change"] = round(row["notional"] - prev, 2)
        row["change_pct"] = (
            round(100.0 * (row["notional"] - prev) / prev, 1) if prev else None
        )
        row["prior_period"] = f"{p_start}..{p_end}"
        out.append(row)
    return sorted(out, key=lambda r: -r["change"])


def product_preferences(
    conn: sqlite3.Connection, client_id: str | None = None
) -> list[dict]:
    """Asset-class mix per client (% of that client's total notional)."""
    where, params = [LIVE], []
    if client_id:
        where.append("client_id = ?")
        params.append(client_id)
    rows = conn.execute(
        f"""SELECT client_id, asset_class, COUNT(*) AS n_trades,
                   ROUND(SUM(notional), 2) AS notional,
                   ROUND(100.0 * SUM(notional) /
                         SUM(SUM(notional)) OVER (PARTITION BY client_id), 1)
                       AS pct_of_client
            FROM trades WHERE {' AND '.join(where)}
            GROUP BY client_id, asset_class
            ORDER BY client_id, notional DESC""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def dormant_clients(
    conn: sqlite3.Connection, inactive_days: int = 10
) -> list[dict]:
    """Clients with no live trade in the last `inactive_days` business
    window (relative to the latest trade date in the book)."""
    _, latest = _bounds(conn)
    rows = conn.execute(
        f"""SELECT c.client_id, c.name, c.tier,
                   MAX(date(t.trade_time)) AS last_trade_date,
                   CAST(julianday(?) - julianday(MAX(date(t.trade_time))) AS INT)
                       AS days_inactive,
                   COUNT(t.trade_id) AS lifetime_trades
            FROM clients c LEFT JOIN trades t
              ON t.client_id = c.client_id AND t.{LIVE}
            GROUP BY c.client_id
            HAVING last_trade_date IS NULL
                OR days_inactive >= ?
            ORDER BY c.tier, days_inactive DESC""",
        (latest, inactive_days),
    ).fetchall()
    return [dict(r) for r in rows]


def activity_trend(
    conn: sqlite3.Connection, client_id: str | None = None
) -> list[dict]:
    """Weekly trade counts and notional (firm-wide or one client)."""
    where, params = [LIVE], []
    if client_id:
        where.append("client_id = ?")
        params.append(client_id)
    rows = conn.execute(
        f"""SELECT strftime('%Y-W%W', trade_time) AS week,
                   COUNT(*) AS n_trades,
                   ROUND(SUM(notional), 2) AS notional
            FROM trades WHERE {' AND '.join(where)}
            GROUP BY week ORDER BY week""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def opportunity_flags(conn: sqlite3.Connection) -> list[dict]:
    """Simple, explainable sales heuristics:
    - FADING:      active historically, notional in latest week < 40% of
                   their weekly average
    - SINGLE_PRODUCT: >= 85% of notional in one asset class (cross-sell
                   conversation)
    Heuristics are deliberately transparent — a salesperson must be able
    to see why a flag fired."""
    out: list[dict] = []

    fading = conn.execute(
        f"""WITH weekly AS (
              SELECT client_id, strftime('%Y-W%W', trade_time) AS week,
                     SUM(notional) AS notional
              FROM trades WHERE {LIVE}
              GROUP BY client_id, week),
            stats AS (
              SELECT client_id, AVG(notional) AS avg_w,
                     COUNT(*) AS n_weeks, MAX(week) AS last_week
              FROM weekly GROUP BY client_id HAVING n_weeks >= 3),
            latest AS (
              SELECT w.client_id, w.notional AS last_notional
              FROM weekly w JOIN stats s
                ON s.client_id = w.client_id AND w.week = s.last_week)
            SELECT s.client_id, c.name, c.tier,
                   ROUND(s.avg_w, 2) AS avg_weekly_notional,
                   ROUND(l.last_notional, 2) AS latest_week_notional
            FROM stats s JOIN latest l USING (client_id)
                         JOIN clients c USING (client_id)
            WHERE l.last_notional < 0.4 * s.avg_w
            ORDER BY s.avg_w DESC"""
    ).fetchall()
    out += [dict(r) | {"flag": "FADING",
                       "reason": "latest week < 40% of weekly average"}
            for r in fading]

    single = conn.execute(
        f"""WITH mix AS (
              SELECT client_id, asset_class, SUM(notional) AS notional,
                     100.0 * SUM(notional) /
                     SUM(SUM(notional)) OVER (PARTITION BY client_id) AS pct
              FROM trades WHERE {LIVE} GROUP BY client_id, asset_class)
            SELECT m.client_id, c.name, c.tier, m.asset_class AS dominant_product,
                   ROUND(m.pct, 1) AS pct_of_notional
            FROM mix m JOIN clients c USING (client_id)
            WHERE m.pct >= 85
            ORDER BY m.pct DESC"""
    ).fetchall()
    out += [dict(r) | {"flag": "SINGLE_PRODUCT",
                       "reason": ">=85% of notional in one asset class"}
            for r in single]
    return out
