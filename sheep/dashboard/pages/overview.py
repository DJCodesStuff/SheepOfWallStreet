import altair as alt
import pandas as pd
import streamlit as st

from sheep.dashboard.core import HELP, cols, current, load, mark, num, split_trades, when
from sheep.trader.data import NY, load_bars

HUE = "#3b6fd4"

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
    m = mark(open_)
    with st.container(horizontal=True):
        st.metric("Open P&L", f"${m['open_pnl'].sum():+,.2f}",
                  f"{m['open_pnl'].sum() / equity:+.2%} of equity", border=True,
                  help=HELP["open_pnl"])
        st.metric("If every stop fills", f"${m['if_stopped'].sum():+,.2f}",
                  f"{m['if_stopped'].sum() / equity:+.2%} of equity", border=True,
                  help=HELP["if_stopped"])
    if m["close"].isna().any():
        st.warning("Couldn't load prices for " + ", ".join(m[m["close"].isna()]["symbol"]))
    view = m[["symbol", "setup", "sessions", "qty", "entry_price", "close", "open_pnl",
              "open_pct", "open_r", "current_stop", "to_stop", "target", "to_target",
              "progress", "if_stopped"]]
    st.dataframe(view, hide_index=True, column_config=cols(view))
    st.caption(f"Valued at the {m['as_of'].max()} close.")

    for tab, t in zip(st.tabs(list(m["symbol"])), m.itertuples(), strict=True):
        with tab:
            entered = pd.Timestamp(t.entry_ts).tz_convert(NY).tz_localize(None).normalize()
            try:
                c = load_bars(t.symbol)["close"]
            except Exception as e:  # noqa: BLE001 - network/data errors shown in the UI
                st.caption(f"No chart: {e}")
                continue
            c = c[c.index >= entered - pd.Timedelta(days=30)].rename("close")
            stop = t.current_stop if pd.notna(t.current_stop) else t.stop
            levels = pd.DataFrame({
                "label": [f"target {t.target:.2f}", f"entry {t.entry_price:.2f}",
                          f"stop now {stop:.2f}"] + (
                    [f"stop at entry {t.stop:.2f}"] if stop != t.stop else []),
                "price": [t.target, t.entry_price, stop] + ([t.stop] if stop != t.stop else []),
            })
            line = alt.Chart(c.reset_index()).mark_line(color=HUE, strokeWidth=2).encode(
                x=alt.X("date:T", title=None),
                y=alt.Y("close:Q", title="Close", scale=alt.Scale(zero=False)),
                tooltip=[alt.Tooltip("date:T", format="%a %d %b"),
                         alt.Tooltip("close:Q", format="$.2f")])
            st.altair_chart(alt.layer(
                line,
                alt.Chart(levels).mark_rule(color="gray", strokeDash=[4, 4]).encode(y="price:Q"),
                alt.Chart(levels).mark_text(color="gray", align="left", dx=4, dy=-6).encode(
                    x=alt.value(0), y="price:Q", text="label:N"),
                alt.Chart(pd.DataFrame({"date": [entered]})).mark_rule(color="gray").encode(
                    x="date:T"),
            ).properties(height=260))
            st.caption(f"Entered {entered.date()} on a {t.setup} setup "
                       f"({t.sessions} trading days ago). Solid vertical line: entry day.")
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
