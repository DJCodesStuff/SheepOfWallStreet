import pandas as pd
import psycopg
import streamlit as st

from sheep.common.config import ConfigError, load_profiles
from sheep.dashboard.core import (
    HELP,
    PARAMS,
    current,
    flash,
    load,
    require_unlock,
    show_flash,
    write,
)
from sheep.profiles.manage import create_rule_version, set_rule_version
from sheep.trader.strategy import RULE_BOUNDS

data = load()
vers, profiles = data["versions"], data["profiles"]
names = list(vers["version"])
params = dict(zip(vers["version"], vers["params_json"], strict=True))

st.title("Rule versions", help=HELP["rules"])
st.caption("Versions can't be edited once saved, so every past trade points at the exact rules "
           "it used. To change the rules, create a new version from an existing one, backtest "
           "it, then switch a paper profile to it.")
show_flash()

# --- compare ----------------------------------------------------------------------------------
st.subheader("Compare", help="Every saved version side by side. Settings that differ between "
                             "versions are in bold.")
table = pd.DataFrame(params).reindex(list(RULE_BOUNDS))
table.index = [PARAMS[k][0] for k in table.index]
styled = table.style.format("{:g}", na_rep="–").apply(
    lambda row: ["font-weight: bold" if row.nunique() > 1 else ""] * len(row), axis=1)
st.dataframe(styled, column_config={"_index": st.column_config.TextColumn("setting")})

with st.expander("What each setting means", icon=":material/help:"):
    for label, meaning in PARAMS.values():
        st.markdown(f"**{label}**: {meaning}")

used_by = profiles.groupby("strategy_version")["name"].agg(", ".join)
details = vers[["version", "created_at", "approved_by", "notes"]].assign(
    used_by=vers["version"].map(used_by).fillna("–"))
st.dataframe(details, hide_index=True, column_config={
    "created_at": st.column_config.DatetimeColumn("created", format="YYYY-MM-DD"),
    "approved_by": st.column_config.TextColumn(
        "saved by", help="config: synced from strategy.yaml · dashboard: created here"),
    "notes": st.column_config.TextColumn("what changed and why", width="large"),
    "used_by": st.column_config.TextColumn("used by", help="Profiles trading this version"),
})

if not require_unlock():
    st.stop()

# --- create -----------------------------------------------------------------------------------
st.subheader("Create a version", help="Start from an existing version, adjust its settings, "
                                      "and save the result under a new name.")
with st.container(border=True):
    base = st.selectbox("Start from", names, index=names.index(current()["strategy_version"]),
                        help="Its settings are filled in below for you to adjust.")
    bp = params[base]
    with st.form("new_rule", border=False):
        new_name = st.text_input("New version name", placeholder=f"{base}_wide_stop",
                                 help="1-64 letters, digits, _ or -. Can't be renamed later.")
        grid = st.columns(3)
        values = {}
        for i, (k, (lo, hi)) in enumerate(RULE_BOUNDS.items()):
            label, meaning = PARAMS[k]
            if isinstance(lo, int):
                default = min(max(int(bp.get(k, lo)), lo), hi)
                values[k] = grid[i % 3].number_input(label, lo, hi, default, step=1,
                                                      help=meaning, key=f"{base}-{k}")
            else:
                default = min(max(float(bp.get(k, lo)), lo), hi)
                values[k] = grid[i % 3].number_input(label, lo, hi, default, step=0.1,
                                                      help=meaning, key=f"{base}-{k}")
        notes = st.text_area("What changed and why",
                             placeholder="e.g. Wider stop: v1 stops out too often on noise.",
                             help="Saved with the version so the reason isn't lost.")
        if st.form_submit_button("Save version", type="primary", icon=":material/save:"):
            changed = {k: (bp.get(k), v) for k, v in values.items() if bp.get(k) != v}
            if not changed:
                st.error(f"Nothing differs from {base}. Change at least one setting.")
            else:
                try:
                    name = new_name.strip()
                    write(lambda c: create_rule_version(c, name, values, notes.strip() or None))
                    diff = "; ".join(f"{PARAMS[k][0]} {a:g} → {b:g}"
                                     for k, (a, b) in changed.items())
                    flash("success", f"Saved **{name}** ({diff}). Backtest it before switching "
                                     "a profile to it.")
                except (ValueError, psycopg.Error) as e:
                    st.error(str(e))

# --- switch -----------------------------------------------------------------------------------
st.subheader("Switch a profile's version",
             help="Which rule version a paper profile trades from its next run.")
paper = list(profiles.loc[profiles["mode"] == "paper", "name"])
try:
    in_yaml = set(load_profiles())
except (ConfigError, OSError):
    in_yaml = set()
with st.container(border=True), st.form("assign", border=False):
    c1, c2 = st.columns(2)
    me = current().name
    who = c1.selectbox("Paper profile", paper, index=paper.index(me) if me in paper else 0)
    ver = c2.selectbox("Rule version", names)
    st.caption("Open trades keep their recorded entry rules, but their exits (trailing stop, "
               "trend break) follow the new version from the next run. Live profiles change "
               "only in config/profiles.yaml.")
    if st.form_submit_button("Switch", icon=":material/swap_horiz:"):
        try:
            write(lambda c: set_rule_version(c, who, ver))
            msg = f"**{who}** now trades **{ver}**."
            if who in in_yaml:
                msg += (f" {who} is also defined in config/profiles.yaml: change its "
                        "strategy_version there too, or the next config sync will switch it back.")
            flash("success", msg, select=who)
        except (ValueError, psycopg.Error) as e:
            st.error(str(e))
