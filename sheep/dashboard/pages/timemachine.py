import altair as alt
import pandas as pd
import streamlit as st

from sheep.backtest.timemachine import history, plan_from_rules, simulate, verdict
from sheep.dashboard.core import HELP, current, load, split_trades
from sheep.trader.data import load_bars
from sheep.trader.strategy import Rules

ss = st.session_state
HUE = "#3b6fd4"  # one hue for the bands and median: it's one series

p = current()
rules = Rules(**load()["versions"].set_index("version").loc[p.strategy_version, "params_json"])
open_trades, _ = split_trades(load(p.name)["trades"])
held = list(open_trades["symbol"]) if not open_trades.empty else []

st.title("Time machine", help=HELP["timemachine"])
st.caption(f"Replays a long trade thousands of times from the symbol's own last 2 years of daily "
           f"bars, and checks how {p.strategy_version}'s setups really did on it. A simulation, "
           "not a prediction: earnings and news can do what those 2 years never did.")

options = list(dict.fromkeys(held + list(p.config_json["watchlist"])))
sym = st.selectbox("Symbol", options, accept_new_options=True, help=HELP["tm_symbol"])
sym = sym.strip().upper()
try:
    bars = load_bars(sym)
except Exception as e:  # noqa: BLE001 - network/data errors shown in the UI
    st.error(f"Couldn't load {sym}: {e}")
    st.stop()

if sym in held:  # an open trade: its fill, the stop in force now, and its target
    t = open_trades[open_trades["symbol"] == sym].iloc[0]
    plan = (t.entry_price, t.current_stop if pd.notna(t.current_stop) else t.stop, t.target)
    st.caption(f"Prefilled from open trade {t.id}.")
else:
    plan = plan_from_rules(bars, rules)
    st.caption(f"Prefilled with what {p.strategy_version} would set from the "
               f"{bars.index[-1].date()} close.")

with st.form("tm_form"):
    c = st.columns(3)
    entry = c[0].number_input("Entry", 0.0, value=float(plan[0]), format="%.2f")
    stop = c[1].number_input("Stop", 0.0, value=float(plan[1]), format="%.2f",
                             help=HELP["stop"])
    target = c[2].number_input("Target", 0.0, value=float(plan[2]), format="%.2f",
                               help=HELP["target"])
    c = st.columns(3)
    days = c[0].slider("Holding period (sessions)", 5, 60, 20, help=HELP["tm_days"])
    replays = c[1].select_slider("Replays", [1_000, 5_000, 10_000, 50_000], 10_000)
    seed = c[2].number_input("Seed", 0, 10**6, 19, help=HELP["tm_seed"])
    if st.form_submit_button("Run", type="primary", icon=":material/history:"):
        try:
            ss["tm"] = (sym, entry, stop, target,
                        simulate(bars, entry, stop, target, days, replays, int(seed)),
                        history(bars, rules, days))
        except ValueError as e:
            st.error(str(e))

if "tm" not in ss or ss["tm"][0] != sym:
    st.stop()
_, entry, stop, target, mc, hist = ss["tm"]

st.subheader(f"{sym}: {mc['replays']:,} replays of {mc['days']} sessions, seed {mc['seed']}")
with st.container(horizontal=True):
    st.metric("Target first", f"{mc['p_target']:.1%}", border=True)
    st.metric("Stop first", f"{mc['p_stop']:.1%}", border=True)
    st.metric("Neither", f"{mc['p_neither']:.1%}", border=True,
              help=f"Still open after {mc['days']} sessions.")
    st.metric("Green", f"{mc['p_green']:.1%}", border=True, help=HELP["tm_green"])
    st.metric("Average R", f"{mc['avg_r']:+.2f}", border=True, help=HELP["r"])
lines = verdict(mc)
st.info(lines[0], icon=":material/balance:")
for note in lines[1:]:
    st.caption(note)

b = mc["bands"].reset_index(names="day")
x = alt.X("day:Q", title="Sessions after entry")
tip = [alt.Tooltip("day:Q", title="Session")] + [
    alt.Tooltip(f"{k}:Q", title=label, format="$.2f")
    for k, label in [("p95", "95th pct"), ("p75", "75th pct"), ("p50", "Median"),
                     ("p25", "25th pct"), ("p5", "5th pct")]]
base = alt.Chart(b).encode(x=x)
levels = pd.DataFrame({"label": [f"stop {stop:.2f}", f"entry {entry:.2f}",
                                 f"target {target:.2f}"], "price": [stop, entry, target]})
chart = alt.layer(
    base.mark_area(color=HUE, opacity=0.18).encode(
        y=alt.Y("p5:Q", title="Price", scale=alt.Scale(zero=False)), y2="p95:Q"),
    base.mark_area(color=HUE, opacity=0.35).encode(y="p25:Q", y2="p75:Q"),
    base.mark_line(color=HUE, strokeWidth=2).encode(y="p50:Q"),
    alt.Chart(levels).mark_rule(color="gray", strokeDash=[4, 4]).encode(y="price:Q"),
    alt.Chart(levels).mark_text(color="gray", align="left", dx=4, dy=-6).encode(
        x=alt.value(0), y="price:Q", text="label:N"),
    base.mark_rule(opacity=0, strokeWidth=12).encode(tooltip=tip),  # hover target per session
).properties(height=320)
st.altair_chart(chart)
f = mc["final"]
st.caption(f"Shaded: the middle 50% and 90% of replayed prices; line: the median. Price after "
           f"{mc['days']} sessions: median {f[0.5]:.2f}, 90% between {f[0.05]:.2f} and "
           f"{f[0.95]:.2f}. Pool: {mc['pool_days']} daily bars, {mc['pool_from']} to "
           f"{mc['pool_to']}.")
with st.expander("Table"):
    st.dataframe(b, hide_index=True)

st.subheader("History", help=HELP["tm_history"])
if hist["fired"]:
    st.write(f"{p.strategy_version}'s setups fired on {sym} **{hist['fired']}** times with "
             f"{mc['days']} sessions after: **{hist['target']}** hit the target, "
             f"**{hist['stop']}** the stop, **{hist['neither']}** neither. "
             f"Average R **{hist['avg_r']:+.2f}**.")
else:
    st.write(f"{p.strategy_version}'s setups never fired on {sym} with {mc['days']} sessions "
             "of data after.")
