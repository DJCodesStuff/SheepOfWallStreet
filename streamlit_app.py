"""Dashboard entry point. Viewing is open; Backtest and Manage unlock with DASHBOARD_PASSWORD.

Local:  uv run --env-file .env streamlit run streamlit_app.py
Cloud:  Streamlit Community Cloud, main file `streamlit_app.py`. In the app's Secrets, at root
        level (Streamlit exposes those as env vars):
            DATABASE_URL = "postgresql://..."
            DASHBOARD_PASSWORD = "..."

Controls only ever act on paper profiles: the Trader and reset refuse live profiles, and
create/clone always make paper ones.
"""

import streamlit as st

from sheep.dashboard.core import sidebar

st.set_page_config(page_title="SheepOfWallStreet", page_icon=":material/savings:",
                   layout="wide")

pages = "sheep/dashboard/pages/"
nav = st.navigation([
    st.Page(pages + "overview.py", title="Overview", icon=":material/dashboard:", default=True),
    st.Page(pages + "journal.py", title="Trade journal", icon=":material/receipt_long:"),
    st.Page(pages + "rules.py", title="Rules", icon=":material/rule:"),
    st.Page(pages + "backtest.py", title="Backtest", icon=":material/query_stats:"),
    st.Page(pages + "options.py", title="Options", icon=":material/candlestick_chart:"),
    st.Page(pages + "manage.py", title="Manage", icon=":material/tune:"),
], position="top")
sidebar()
nav.run()
