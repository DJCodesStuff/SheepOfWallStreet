import pandas as pd
import streamlit as st

from sheep.common.config import SYMBOL_RE
from sheep.dashboard.core import HELP, load
from sheep.trader import news, scanner
from sheep.trader.data import load_bars

ss = st.session_state
EXTRA = ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB",
         "XLRE", "XLC", "GLD", "TLT"]


@st.cache_data(ttl=3600, show_spinner="Fetching daily closes, 20 symbols a request…")
def scan(symbols: tuple, min_price: float, min_volume: float):
    return scanner.run(list(symbols), min_price, min_volume)


@st.cache_data(ttl=900, show_spinner="Reading headlines and filings…")
def headlines(tickers: tuple, hours: int):
    return news.collect(list(tickers), hours, news.sec_user_agent())


def last_move(sym: str) -> float | None:
    try:
        c = load_bars(sym)["close"]
        return float(c.iloc[-1] / c.iloc[-2] - 1)
    except Exception:  # noqa: BLE001 - a missing chart just shows no move
        return None


def symbols(text: str) -> list[str]:
    out = list(dict.fromkeys(s.strip().upper() for s in text.replace(",", " ").split()))
    if bad := [s for s in out if not SYMBOL_RE.fullmatch(s)]:
        st.error(f"Not tickers: {', '.join(bad)}")
        st.stop()
    return out


st.title("Scanner", help=HELP["scanner"])
st.caption("Lists charts that match a rule on daily closes, to help build a watchlist. A match "
           "is not a buy. The whole US market (~10,000 symbols, a few minutes) runs from the "
           "command line: `python -m sheep.trader.scanner --universe`.")

watch = sorted({s for cfg in load()["profiles"]["config_json"] for s in cfg["watchlist"]
                if "=" not in s and not s.startswith("^")} | set(EXTRA))
with st.form("scan_form"):
    text = st.text_area("Symbols", " ".join(watch), help=HELP["scan_symbols"])
    c1, c2 = st.columns(2)
    min_price = c1.number_input("Min price ($)", 0.0, 1000.0, 5.0, step=1.0)
    min_volume = c2.number_input("Min avg volume (shares/day)", 0, 100_000_000, 1_000_000,
                                 step=100_000, help=HELP["min_volume"])
    if st.form_submit_button("Scan", type="primary", icon=":material/radar:"):
        ss["scan"] = scan(tuple(symbols(text)), min_price, min_volume)

if "scan" in ss:
    hits, n = ss["scan"]
    st.caption(f"Fetched {n['fetched']} of {n['requested']} charts, closes through "
               f"{n['as_of']}. {len(hits)} matched a rule.")
    for e in n["errors"][:3]:
        st.warning(f"Failed: {e}")
    st.dataframe(hits, hide_index=True, column_config={
        "setups": st.column_config.ListColumn(help="\n".join(
            f"{k}: {v}" for k, v in scanner.SETUPS.items())),
        "close": st.column_config.NumberColumn(format="$%.2f"),
        "rsi": st.column_config.NumberColumn("RSI", format="%.0f"),
        "above_200d": st.column_config.CheckboxColumn("above 200d", help=HELP["above_200d"]),
        "momentum_6m": st.column_config.NumberColumn("6m return", format="percent"),
        "avg_volume": st.column_config.NumberColumn("avg volume", format="compact"),
        "dollar_volume": st.column_config.NumberColumn("$ traded/day", format="compact"),
    })

st.header("News", help=HELP["news"])
default = list(ss["scan"][0]["symbol"][:5]) if "scan" in ss else watch[:5]
with st.form("news_form"):
    picked = st.multiselect("Tickers", sorted(set(watch) | set(default)), default,
                            accept_new_options=True)
    hours = st.slider("Last hours", 6, 168, 48, step=6)
    if st.form_submit_button("Load news", icon=":material/newspaper:"):
        ss["news"] = headlines(tuple(symbols(" ".join(picked))), hours)
        ss["moves"] = {s: last_move(s) for s in picked}

if "news" in ss:
    items, notes = ss["news"]
    for msg in notes:
        st.caption(msg)
    moves = pd.DataFrame([{"ticker": s, "last close move": m,
                           "moving": m is not None and abs(m) * 100 >= news.MOVE_PCT}
                          for s, m in ss["moves"].items()])
    st.dataframe(moves, hide_index=True, column_config={
        "last close move": st.column_config.NumberColumn(format="percent"),
        "moving": st.column_config.CheckboxColumn(help=f"{news.MOVE_PCT:g}% or more from the "
                                                       "close before"),
    })
    if items.empty:
        st.info("No headlines or filings in that window.", icon=":material/search_off:")
    else:
        st.dataframe(items, hide_index=True, column_config={
            "time": st.column_config.DatetimeColumn(format="ddd D MMM HH:mm",
                                                    timezone="America/New_York"),
            "tags": st.column_config.ListColumn(),
            "important": st.column_config.CheckboxColumn("!", help=HELP["important_8k"]),
            "link": st.column_config.LinkColumn(display_text="open"),
        })
    st.caption("Headlines are leads, not facts: open the story or filing before acting on it.")
