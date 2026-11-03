import psycopg
import streamlit as st

from sheep.common.config import ConfigError, parse_profile
from sheep.common.db import sync_profiles
from sheep.dashboard.core import (
    HELP,
    current,
    flash,
    load,
    require_unlock,
    show_flash,
    versions,
    write,
)
from sheep.profiles.manage import clone, reset
from sheep.trader.run import run_profile

p = current()
name = p.name
all_versions = versions()

st.title("Manage")
show_flash()
if not require_unlock():
    st.stop()

left, right = st.columns(2, gap="large")

with left:
    with st.container(border=True):
        st.markdown("#### :material/play_arrow: Run Trader")
        st.caption(f"Today's cycle for **{name}**: fill orders, manage exits, place new entries. "
                   "Refuses before 4:30pm New York time on weekdays.")
        if p["broker"] != "paper":
            st.caption(":material/block: Only built-in paper profiles can run here.")
        elif st.button(f"Run Trader for {name}", type="primary", help=HELP["run_trader"]):
            try:
                lines = write(lambda c: run_profile(c, name))
                flash("success", "\n\n".join(lines) or "Ran: no action needed.")
            except (ValueError, psycopg.Error) as e:
                flash("error", f"Trader failed: {e}")

    with st.container(border=True):
        st.markdown(f"#### :material/content_copy: Clone {name}")
        with st.form("clone", border=False):
            new_name = st.text_input("New profile name", help="1-64 letters, digits, _ or -.")
            ver = st.selectbox("Rule version", all_versions,
                               index=all_versions.index(p["strategy_version"]),
                               help=HELP["rules"])
            st.caption("Same capital, watchlist and risk; starts empty; always paper.")
            if st.form_submit_button("Clone", help=HELP["clone"]):
                try:
                    write(lambda c: clone(c, name, new_name.strip(), ver))
                    flash("success", f"Cloned {name} → {new_name.strip()}.",
                          select=new_name.strip())
                except (ValueError, psycopg.Error) as e:
                    st.error(f"Clone failed: {e}")

with right, st.container(border=True):
    st.markdown("#### :material/add: New paper profile")
    with st.form("create", border=False):
        new = st.text_input("Name", placeholder="e.g. etf_momentum_test",
                            help="1-64 letters, digits, _ or -.")
        c1, c2 = st.columns(2)
        ver = c1.selectbox("Rule version", all_versions, help=HELP["rules"])
        capital = c2.number_input("Starting capital (USD)", 1.0, 1e6, 100.0, help=HELP["capital"])
        watch = st.text_input("Watchlist (comma-separated)", "SPY, QQQ, IWM",
                              help=HELP["watchlist"])
        c1, c2, c3 = st.columns(3)
        rpt = c1.number_input("Risk per trade %", 0.1, 5.0, 1.0, step=0.1,
                              help=HELP["risk_per_trade"])
        maxpos = c2.number_input("Max positions", 1, 10, 3, help=HELP["max_positions"])
        wll = c3.number_input("Weekly loss limit %", 0.5, 20.0, 3.0, step=0.5,
                              help=HELP["weekly_loss_limit"])
        if st.form_submit_button("Create profile", type="primary", help=HELP["new_profile"]):
            raw = {
                "name": new.strip(), "mode": "paper", "broker": "paper",
                "starting_capital": capital, "strategy_version": ver,
                "watchlist": [s.strip() for s in watch.split(",") if s.strip()],
                "risk": {"risk_per_trade_pct": rpt, "max_open_positions": int(maxpos),
                         "weekly_loss_limit_pct": wll},
            }
            try:
                prof = parse_profile(raw, dict.fromkeys(all_versions))
                if prof.name in set(load()["profiles"]["name"]):
                    raise ConfigError(f"profile {prof.name} already exists")
                write(lambda c: sync_profiles(c, {prof.name: prof}))
                flash("success", f"Created paper profile {prof.name}.", select=prof.name)
            except ConfigError as e:
                st.error(str(e))

with st.expander(f"Danger zone: reset {name}", icon=":material/warning:"):
    if p["mode"] != "paper":
        st.caption("Live profiles can't be reset.")
    else:
        st.caption(f"Deletes all of {name}'s orders, trades, tags and equity snapshots and "
                   "starts again from its starting capital. Cannot be undone.")
        confirm = st.text_input("Type the profile name to confirm", key=f"reset-{name}")
        if st.button(f"Reset {name}", disabled=confirm != name, icon=":material/restart_alt:",
                     help=HELP["reset"]):
            write(lambda c: reset(c, name))
            flash("success", f"Reset {name}.")
