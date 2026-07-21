"""Front-office analytics dashboard (Streamlit).

Read-only view over the analytics DB. Run:
    streamlit run dashboard.py

The Ask tab needs GEMINI_API_KEY set. Every other tab works without it.
"""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st

from fo.analytics import portfolio as pa
from fo.analytics import sales as sa
from fo.config import get_settings
from fo.db.database import connect

st.set_page_config(page_title="Front-Office Analytics", layout="wide",
                   initial_sidebar_state="collapsed")

# --- dark quant-terminal styling ------------------------------------
st.markdown("""
<style>
  .stApp { background-color: #0d1117; color: #e6edf3; }
  section.main > div { padding-top: 1.5rem; }
  h1, h2, h3 { color: #e6edf3; font-family: 'SF Mono','Menlo',monospace; }
  h1 { font-size: 1.6rem; letter-spacing: .5px; }
  h3 { font-size: 1.05rem; color: #8b949e; text-transform: uppercase;
       letter-spacing: 1px; font-weight: 600; margin-top: 1.4rem; }
  .stTabs [data-baseweb="tab-list"] { gap: 4px; border-bottom: 1px solid #30363d; }
  .stTabs [data-baseweb="tab"] { background: #161b22; color: #8b949e;
       border-radius: 6px 6px 0 0; padding: 8px 18px; font-family: monospace; }
  .stTabs [aria-selected="true"] { background: #1f6feb22; color: #58a6ff;
       border-bottom: 2px solid #58a6ff; }
  [data-testid="stMetric"] { background: #161b22; border: 1px solid #30363d;
       border-radius: 8px; padding: 14px 16px; }
  [data-testid="stMetricLabel"] { color: #8b949e; font-family: monospace;
       font-size: .72rem; text-transform: uppercase; letter-spacing: 1px; }
  [data-testid="stMetricValue"] { font-family: monospace; font-size: 1.5rem; }
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def get_conn():
    return connect(get_settings().db_path)


def df(rows):
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def money(x):
    """Compact USD formatting: 1_244_535 -> $1.2M."""
    if x is None:
        return "-"
    a = abs(x)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"${x/div:,.1f}{suf}"
    return f"${x:,.0f}"


conn = get_conn()

try:
    latest = conn.execute("SELECT MAX(as_of_date) FROM positions_eod").fetchone()[0]
except Exception:
    latest = None
if not latest:
    st.error("No analytics data found. Build it first with `python run_all.py`.")
    st.stop()

start, end = conn.execute(
    "SELECT MIN(as_of_date), MAX(as_of_date) FROM positions_eod"
).fetchone()

st.title("FRONT-OFFICE ANALYTICS")
st.caption(f"Simulated trading book   ·   {start} to {end}   ·   "
           f"latest snapshot {latest}")

# --- headline KPIs --------------------------------------------------
pnl_ac = df(pa.pnl_summary(conn, group_by="asset_class"))
total_pnl = float(pnl_ac["total_pnl"].sum()) if not pnl_ac.empty else 0.0
n_clients = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
n_trades = conn.execute(
    "SELECT COUNT(*) FROM trades WHERE status != 'CANCELLED'").fetchone()[0]
n_dormant = len(sa.dormant_clients(conn, inactive_days=10))

k1, k2, k3, k4 = st.columns(4)
k1.metric("Total P&L", money(total_pnl))
k2.metric("Live Trades", f"{n_trades:,}")
k3.metric("Clients", f"{n_clients}")
k4.metric("Dormant", f"{n_dormant}", delta=f"-{n_dormant}", delta_color="inverse")

tab_pnl, tab_risk, tab_sales, tab_ask = st.tabs(
    ["  P&L  ", "  EXPOSURE & RISK  ", "  CLIENT ANALYTICS  ", "  ASK (AI)  "]
)

# ---------------------------------------------------------------- P&L
with tab_pnl:
    st.subheader("P&L by asset class")
    if not pnl_ac.empty:
        c1, c2 = st.columns([3, 2])
        with c1:
            show = pnl_ac.copy()
            for col in ("realized_pnl", "unrealized_pnl", "total_pnl"):
                show[col] = show[col].map(money)
            st.dataframe(show, use_container_width=True, hide_index=True)
        with c2:
            st.bar_chart(pnl_ac.set_index("asset_class")["total_pnl"],
                         color="#58a6ff")

    st.subheader("Top 10 clients by total P&L")
    cl = df(pa.pnl_summary(conn, group_by="client")).head(10)
    if not cl.empty:
        st.bar_chart(cl.set_index("client_id")["total_pnl"], color="#3fb950")

    st.subheader("Trading activity by asset class")
    br = df(pa.asset_class_breakdown(conn, start, end))
    if not br.empty:
        br["total_notional"] = br["total_notional"].map(money)
        st.dataframe(br, use_container_width=True, hide_index=True)

# --------------------------------------------------------- Exposure
with tab_risk:
    st.subheader("Net and gross exposure by client and asset class")
    ex = df(pa.exposure(conn))
    if not ex.empty:
        for col in ("net_exposure", "gross_exposure"):
            ex[col] = ex[col].map(money)
        st.dataframe(ex, use_container_width=True, hide_index=True)

    st.subheader("Concentration: largest instrument per client")
    co = df(pa.concentration(conn, top_n=10))
    if not co.empty:
        if "instrument_gross" in co:
            co["instrument_gross"] = co["instrument_gross"].map(money)
        st.dataframe(co, use_container_width=True, hide_index=True)

    st.subheader("Top movers, day over day")
    mv = df(pa.top_movers(conn, top_n=10))
    if not mv.empty:
        for col in ("prev_exposure", "curr_exposure", "change"):
            if col in mv:
                mv[col] = mv[col].map(money)
        st.dataframe(mv, use_container_width=True, hide_index=True)

# ----------------------------------------------------------- Sales
with tab_sales:
    st.subheader("Clients increasing activity, recent vs prior period")
    mid = conn.execute("SELECT date(?, '-9 day')", (end,)).fetchone()[0]
    act = df(sa.client_activity(conn, mid, end, compare_prior_period=True)).head(10)
    if not act.empty:
        for col in ("notional", "prior_notional", "change"):
            if col in act:
                act[col] = act[col].map(money)
        st.dataframe(act, use_container_width=True, hide_index=True)

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Dormant clients")
        st.dataframe(df(sa.dormant_clients(conn, inactive_days=10)),
                     use_container_width=True, hide_index=True)
    with c2:
        st.subheader("Opportunity flags")
        st.dataframe(df(sa.opportunity_flags(conn)),
                     use_container_width=True, hide_index=True)

# ------------------------------------------------------------- Ask
with tab_ask:
    st.subheader("Ask a question in plain English")
    st.caption("The model chooses which analytics function to run. "
               "The numbers come from the database, not the model.")

    examples = [
        "Who are my dormant clients?",
        "Which clients increased fixed-income activity this month?",
        "Show me P&L by asset class",
        "Any cross-sell opportunities?",
    ]
    picked = st.selectbox("Example questions", [""] + examples)
    question = st.text_input("Your question", value=picked)

    if st.button("Ask", type="primary") and question.strip():
        if not os.environ.get("GEMINI_API_KEY"):
            st.warning("Set GEMINI_API_KEY in your terminal before launching, "
                       "then restart the dashboard.")
        else:
            with st.spinner("Working..."):
                try:
                    from fo.ai.agent import ask
                    st.markdown(ask(conn, question))
                except Exception as e:
                    st.error(f"Query failed: {e}")