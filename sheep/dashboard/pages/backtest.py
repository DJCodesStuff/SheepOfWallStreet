import streamlit as st

from sheep.common.config import SYMBOL_RE
from sheep.dashboard.core import (
    HELP,
    backtest,
    current,
    load,
    num,
    require_unlock,
    ss,
    versions,
)

p = current()
st.title("Backtest", help=HELP["walk_forward"])
st.caption(f"Walk-forward test on {p.name}'s watchlist and risk settings, after slippage, "
           "against buying and holding a benchmark. The last 2 years are held out and never "
           "used here.")

if not require_unlock():
    st.stop()

with st.form("backtest"):
    c1, c2, c3 = st.columns([3, 1, 1])
    chosen = c1.multiselect("Rule versions", versions(), default=[p["strategy_version"]],
                            help=HELP["versions"])
    slip = c2.number_input("Slippage (bps per fill)", 0.0, 100.0, 5.0, step=1.0,
                           help=HELP["slippage"])
    bench = c3.text_input("Benchmark", "SPY", help=HELP["benchmark"]).strip().upper()
    run = st.form_submit_button("Run backtest", type="primary", icon=":material/play_arrow:")

if run:
    if not chosen:
        st.error("Pick at least one rule version.")
    elif not SYMBOL_RE.fullmatch(bench):
        st.error(f"{bench!r} isn't a valid ticker.")
    else:
        cfg = p["config_json"]
        params = load()["versions"].set_index("version").loc[chosen, "params_json"].to_dict()
        try:
            out = backtest(tuple(cfg["watchlist"]), cfg["risk"], float(p["starting_capital"]),
                           params, slip, bench)
            ss["bt"] = {"profile": p.name, "label": " + ".join(chosen), "slip": slip,
                        "bench": bench, "out": out}
        except Exception as e:  # noqa: BLE001 - surface data/fit errors in the UI
            st.error(f"Backtest failed: {e}")

bt = ss.get("bt")
if not bt or bt["profile"] != p.name:
    st.stop()

s, folds, curve = bt["out"]
b = bt["bench"]
st.subheader(f"{bt['label']} · {bt['slip']:g} bps slippage", help=HELP["spy"])
if s["cagr"] < s["bench_cagr"]:
    st.warning(f"Doesn't beat {b} buy-and-hold after costs. By the project's rules, don't trade "
               "this live.", icon=":material/trending_down:")
else:
    st.success(f"Beats {b} buy-and-hold after costs on this test. Confirm on the holdout before "
               "trusting it.", icon=":material/trending_up:")

with st.container(horizontal=True):
    st.metric("Yearly return", f"{s['cagr']:.1%}", f"{s['cagr'] - s['bench_cagr']:+.1%} vs {b}",
              border=True, help=HELP["cagr"])
    st.metric("Max drawdown", f"{s['max_drawdown']:.1%}",
              f"{s['max_drawdown'] - s['bench_max_drawdown']:+.1%} vs {b}", border=True,
              help=HELP["max_drawdown"])
    st.metric("Sharpe", f"{s['sharpe']:.2f}", f"{s['sharpe'] - s['bench_sharpe']:+.2f} vs {b}",
              border=True, help=HELP["sharpe"])
    st.metric("Trades", s["n_trades"],
              f"win {s['win_rate']:.0%} · avg {s['avg_r']:+.2f}R", delta_color="off",
              border=True, help=HELP["bt_trades"])

st.line_chart(curve, y_label="USD", height=320)

with st.expander("Year by year", icon=":material/calendar_month:"):
    st.caption(HELP["year_by_year"])
    st.dataframe(folds, hide_index=True, column_config={
        "version": st.column_config.TextColumn(help=HELP["rule_version"]),
        "trades": st.column_config.NumberColumn(help=HELP["closed_trades"]),
        "return": num("cagr", "return", "percent"),
        "max drawdown": num("max_drawdown", "max drawdown", "percent"),
    })
