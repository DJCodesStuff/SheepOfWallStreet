from datetime import date

import streamlit as st

from sheep.common.config import SYMBOL_RE
from sheep.dashboard.core import HELP, option_chain, option_expiries

st.title("Options", help=HELP["options"])
st.caption("Today's option chain, read-only and delayed. Options can't be traded or backtested "
           "here.")

c1, c2 = st.columns([1, 2])
sym = c1.text_input("Underlying", "SPY", help=HELP["underlying"]).strip().upper()
if not SYMBOL_RE.fullmatch(sym) or sym.startswith("^") or "=" in sym:
    st.error(f"{sym!r} isn't a stock or ETF ticker.")
    st.stop()
try:
    expiries = option_expiries(sym)
except Exception as e:  # noqa: BLE001 - network/data errors shown in the UI
    st.error(f"Couldn't load options for {sym}: {e}")
    st.stop()
if not expiries:
    st.info(f"No listed options for {sym}.", icon=":material/search_off:")
    st.stop()


def days_left(expiry: str) -> int:
    return (date.fromisoformat(expiry) - date.today()).days


exp = c2.selectbox("Expiry", expiries, help=HELP["expiry"],
                   format_func=lambda e: f"{e}  ({days_left(e)} days)")
try:
    calls, puts, spot = option_chain(sym, exp)
except Exception as e:  # noqa: BLE001
    st.error(f"Couldn't load the {exp} chain for {sym}: {e}")
    st.stop()

with st.container(horizontal=True):
    st.metric(f"{sym} price", f"${spot:,.2f}" if spot else "–", border=True,
              help="Latest price of the underlying, delayed.")
    st.metric("Days to expiry", days_left(exp), border=True, help=HELP["expiry"])
    st.metric("Contracts listed", f"{len(calls)} calls · {len(puts)} puts", border=True)

window = st.slider("Strikes within ±% of the price", 1, 50, 10, help=HELP["strike_window"],
                   disabled=not spot)

COLUMNS = {
    "strike": st.column_config.NumberColumn(format="$%.2f", help=HELP["strike"]),
    "lastPrice": st.column_config.NumberColumn("last", format="$%.2f", help=HELP["last_price"]),
    "bid": st.column_config.NumberColumn(format="$%.2f", help=HELP["bid"]),
    "ask": st.column_config.NumberColumn(format="$%.2f", help=HELP["ask"]),
    "volume": st.column_config.NumberColumn(format="%d", help=HELP["volume"]),
    "openInterest": st.column_config.NumberColumn("open interest", format="%d",
                                                  help=HELP["open_interest"]),
    "impliedVolatility": st.column_config.NumberColumn("IV", format="percent", help=HELP["iv"]),
    "inTheMoney": st.column_config.CheckboxColumn("ITM", help=HELP["itm"]),
}


def table(df):
    if spot:
        df = df[(df["strike"] - spot).abs() <= spot * window / 100]
    st.dataframe(df[[c for c in COLUMNS if c in df.columns]], hide_index=True,
                 column_config=COLUMNS)


left, right = st.columns(2, gap="large")
with left:
    st.subheader("Calls", help=HELP["calls"])
    table(calls)
with right:
    st.subheader("Puts", help=HELP["puts"])
    table(puts)
