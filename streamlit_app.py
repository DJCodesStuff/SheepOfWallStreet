"""Read-only per-profile dashboard: equity, open trades, trade journal, open orders.

Local:  uv run --env-file .env streamlit run streamlit_app.py
Cloud:  Streamlit Community Cloud, main file `streamlit_app.py`; put DATABASE_URL in the app's
        Secrets at the root level (Streamlit exposes root-level secrets as env vars).
"""

import pandas as pd
import streamlit as st

from sheep.common.db import connect

st.set_page_config(page_title="SheepOfWallStreet", layout="wide")


@st.cache_data(ttl=60)
def load(profile: str | None = None) -> dict[str, pd.DataFrame]:
    """All dashboard data in one short, read-only transaction."""
    with connect() as c:
        c.read_only = True  # BEGIN READ ONLY: the dashboard can never write

        def q(sql: str, *params) -> pd.DataFrame:
            return pd.DataFrame(c.execute(sql, params).fetchall())

        out = {"profiles": q("SELECT id, name, mode, broker, strategy_version, starting_capital "
                             "FROM profiles ORDER BY id")}
        if profile is not None:
            pid = int(out["profiles"].set_index("name").loc[profile, "id"])
            out["equity"] = q("SELECT date, cash, equity FROM equity_snapshots "
                              "WHERE profile_id = %s ORDER BY date", pid)
            out["trades"] = q(
                """SELECT t.id, t.symbol, t.reasoning_json->>'setup' AS setup, t.entry_ts,
                          t.entry_price, t.qty, t.stop, t.target, t.exit_ts, t.exit_price,
                          t.pnl, t.exit_reason, t.rule_version,
                          (SELECT o.stop_price FROM orders o WHERE o.trade_id = t.id
                             AND o.type = 'stop' AND o.status = 'pending' LIMIT 1) AS current_stop
                   FROM trades t WHERE t.profile_id = %s ORDER BY t.entry_ts DESC""", pid)
            out["orders"] = q("SELECT id, symbol, side, type, qty, limit_price, stop_price, "
                              "created_at FROM orders WHERE profile_id = %s AND status = 'pending' "
                              "ORDER BY id", pid)
        return out


profiles = load()["profiles"]
if profiles.empty:
    st.warning("No profiles in the database yet. Run `python -m sheep.common.db` first.")
    st.stop()

name = st.sidebar.selectbox("Profile", profiles["name"])
p = profiles.set_index("name").loc[name]
d = load(name)
eq, trades, orders = d["equity"], d["trades"], d["orders"]

st.title(name)
st.caption(f"{p['mode'].upper()} · broker `{p['broker']}` · rules `{p['strategy_version']}` · "
           "Not financial advice.")

capital = float(p["starting_capital"])
equity = float(eq["equity"].iloc[-1]) if len(eq) else capital
cash = float(eq["cash"].iloc[-1]) if len(eq) else capital
closed = trades[trades["exit_ts"].notna()] if len(trades) else trades
open_ = trades[trades["exit_ts"].isna()] if len(trades) else trades

m = st.columns(5)
m[0].metric("Equity", f"${equity:,.2f}", f"{equity / capital - 1:+.2%}")
m[1].metric("Cash", f"${cash:,.2f}")
m[2].metric("Open trades", len(open_))
m[3].metric("Closed trades", len(closed))
if len(closed):
    m[4].metric("Win rate", f"{(closed['pnl'] > 0).mean():.0%}",
                f"expectancy ${closed['pnl'].mean():+.2f}/trade")

st.subheader("Equity")
if len(eq):
    st.line_chart(eq.set_index("date")[["equity"]], y_label="USD")
else:
    st.caption("No snapshots yet. The Trader writes one each evening.")

st.subheader("Open trades")
if len(open_):
    st.dataframe(open_[["symbol", "setup", "entry_ts", "entry_price", "qty", "stop",
                        "current_stop", "target", "rule_version"]], hide_index=True)
else:
    st.caption("None.")

st.subheader("Trade journal")
if len(closed):
    j = closed.assign(r_multiple=closed["pnl"] / (closed["qty"] * (closed["entry_price"]
                                                                    - closed["stop"])))
    st.dataframe(j[["symbol", "setup", "entry_ts", "exit_ts", "entry_price", "exit_price",
                    "qty", "pnl", "r_multiple", "exit_reason", "rule_version"]],
                 hide_index=True)
else:
    st.caption("No closed trades yet.")

st.subheader("Open orders")
if len(orders):
    st.dataframe(orders, hide_index=True)
else:
    st.caption("None.")
