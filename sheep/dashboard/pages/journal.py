import streamlit as st

from sheep.dashboard.core import HELP, cols, current, load, split_trades

p = current()
_, closed = split_trades(load(p.name)["trades"])

st.title("Trade journal")
st.caption(f"Closed trades for {p.name}. Each one keeps the rule version and signal it was "
           "entered on.")

if closed.empty:
    st.info("No closed trades yet. They appear here once a stop, target or trend-break exit "
            "fills.", icon=":material/hourglass_empty:")
    st.stop()

f1, f2, f3 = st.columns(3)
syms = f1.multiselect("Symbol", sorted(closed["symbol"].unique()), help=HELP["symbol"])
setups = f2.multiselect("Setup", sorted(closed["setup"].dropna().unique()), help=HELP["setup"])
reasons = f3.multiselect("Exit reason", sorted(closed["exit_reason"].dropna().unique()),
                         help=HELP["exit_reason"])
view = closed
for col, picked in (("symbol", syms), ("setup", setups), ("exit_reason", reasons)):
    if picked:
        view = view[view[col].isin(picked)]

with st.container(horizontal=True):
    st.metric("Trades", len(view), border=True, help=HELP["closed_trades"])
    st.metric("Win rate", f"{(view['pnl'] > 0).mean():.0%}" if len(view) else "–", border=True,
              help=HELP["win_rate"])
    st.metric("Total P&L", f"${view['pnl'].sum():+,.2f}", border=True, help=HELP["total_pnl"])
    st.metric("Average R", f"{view['r'].mean():+.2f}" if len(view) else "–", border=True,
              help=HELP["avg_r"])

st.subheader("By setup and exit", help=HELP["by_setup"])
by = (view.assign(win=view["pnl"] > 0)
      .groupby(["setup", "exit_reason"], dropna=False)
      .agg(trades=("pnl", "size"), win_rate=("win", "mean"), total_pnl=("pnl", "sum"),
           avg_r=("r", "mean"))
      .reset_index())
st.dataframe(by, hide_index=True, column_config=cols(by))

st.subheader("All trades")
table = view[["symbol", "setup", "entry_ts", "exit_ts", "entry_price", "exit_price", "qty",
              "pnl", "r", "exit_reason", "rule_version"]]
st.dataframe(table, hide_index=True, column_config=cols(table))
