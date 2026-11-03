import streamlit as st

from sheep.dashboard.core import HELP, cols, current, load, num, split_trades, when

p = current()
d = load(p.name)
eq, orders = d["equity"], d["orders"]
open_, closed = split_trades(d["trades"])
risk = p["config_json"]["risk"]

st.title(p.name)
st.caption(f"{p['mode'].upper()} · watchlist: {', '.join(p['config_json']['watchlist'])}",
           help=HELP["watchlist"])

capital = float(p["starting_capital"])
equity = float(eq["equity"].iloc[-1]) if len(eq) else capital
cash = float(eq["cash"].iloc[-1]) if len(eq) else capital

with st.container(horizontal=True):
    st.metric("Equity", f"${equity:,.2f}", f"{equity / capital - 1:+.2%} since start",
              border=True, help=HELP["equity"])
    st.metric("Cash", f"${cash:,.2f}", f"{cash / equity:.0%} of equity", delta_color="off",
              border=True, help=HELP["cash"])
    st.metric("Open positions", f"{len(open_)} / {risk['max_open_positions']}", border=True,
              help=HELP["open_positions"])
    st.metric("Closed trades", len(closed), border=True, help=HELP["closed_trades"])
    if len(closed):
        st.metric("Win rate", f"{(closed['pnl'] > 0).mean():.0%}",
                  f"${closed['pnl'].mean():+.2f} per trade", border=True,
                  help=f"{HELP['win_rate']} The figure below it is the expectancy: "
                       f"{HELP['expectancy'].lower()}")

st.subheader("Equity", help=HELP["equity_curve"])
if len(eq) > 1:
    st.line_chart(eq.set_index("date")[["equity"]], y_label="USD", height=280)
else:
    st.caption("The curve starts once the Trader has run on two evenings "
               "(after 4:30pm New York time, or with **Manage → Run Trader**).")

st.subheader("Open positions", help=HELP["open_positions"])
if len(open_):
    view = open_[["symbol", "setup", "entry_ts", "entry_price", "qty", "stop", "current_stop",
                  "target"]]
    st.dataframe(view, hide_index=True, column_config=cols(view))
else:
    st.caption("No open positions.")

with st.expander(f"Open orders ({len(orders)})", icon=":material/pending_actions:"):
    st.caption(HELP["open_orders"])
    if len(orders):
        st.dataframe(orders, hide_index=True, column_config=cols(
            orders, limit_price=num("target", "limit price"),
            stop_price=num("current_stop", "stop price"),
            created_at=when("entry_ts", "placed"),
        ))
