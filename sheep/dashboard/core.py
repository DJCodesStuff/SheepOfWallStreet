"""Shared dashboard pieces: data access, the sidebar (profile picker + lock), formatting."""

import hmac
import os
import time
import warnings

import pandas as pd
import streamlit as st

from sheep.backtest.engine import Costs, buy_hold, stats, walk_forward
from sheep.common.config import Risk
from sheep.common.db import connect
from sheep.trader.data import load_bars
from sheep.trader.strategy import Rules

ss = st.session_state

# Hover explanations (the ⓘ tooltips), one place so wording stays consistent.
HELP = {
    "profile": "An isolated account: its own cash, positions, trade log, rule version and "
               "risk settings. Paper profiles use fake money.",
    "rules": "The versioned rule set this profile trades. Versions never change once saved; "
             "a change means a new version.",
    "password": "Viewing is open to anyone with the link. Running the Trader, backtests and "
                "profile changes need the dashboard password.",
    "equity": "Cash plus open positions valued at their latest closing price. The change is "
              "measured from the starting capital.",
    "cash": "Money not tied up in positions. The percentage is the share of equity in cash.",
    "open_positions": "Trades currently held, out of the profile's maximum open positions.",
    "closed_trades": "Trades that have exited through a stop, target or trend break.",
    "win_rate": "Share of closed trades that made money. Under 50% can still be profitable "
                "if winners are bigger than losers.",
    "expectancy": "Average profit or loss per closed trade, after costs.",
    "equity_curve": "Equity at each evening's close, recorded by the Trader's daily run.",
    "open_orders": "Orders waiting to fill: entries for the next open, and each trade's "
                   "stop and target.",
    "setup": "Why the trade was entered. Breakout: closed above the recent high. "
             "Pullback: dipped (low RSI) while in an uptrend.",
    "symbol": "Ticker of the stock or ETF.",
    "entry_ts": "When the entry order filled.",
    "exit_ts": "When the exit order filled.",
    "entry_price": "Fill price of the entry, including slippage.",
    "exit_price": "Fill price of the exit, including slippage.",
    "qty": "Shares held (fractional for stocks and ETFs) or whole contracts for futures.",
    "stop": "Stop-loss set at entry: entry price minus a multiple of ATR (average daily "
            "range). Defines the trade's risk.",
    "current_stop": "Trailing stop now in force. It only moves up as price rises, locking "
                    "in gains.",
    "target": "Take-profit set at entry: a multiple of the initial risk above the entry.",
    "pnl": "Profit or loss in dollars, after slippage and fees.",
    "r": "Profit or loss in units of the risk taken. +2R made twice what was risked; "
         "-1R lost exactly the planned risk.",
    "exit_reason": "stop: stop-loss hit · target: take-profit hit · trend_break: closed "
                   "below the 200-day average · *_gap: opened past the level.",
    "rule_version": "Rule set the trade was entered under.",
    "total_pnl": "Sum of profit and loss across the selected trades.",
    "avg_r": "Average result in R. Above 0 means the trades make money on average.",
    "by_setup": "Results grouped by why trades were entered and how they exited, to spot "
                "which patterns work. Needs about 20+ trades per group to mean much.",
    "walk_forward": "Each year is traded with rules chosen only from the 3 years before it, "
                    "so results are out-of-sample. The last 2 years are held out entirely.",
    "versions": "Rule versions to test. With more than one, each year trades whichever did "
                "best over the 3 years before it.",
    "slippage": "Assumed cost per fill, in basis points (1 bp = 0.01%), always against you. "
                "5 bps on a $100 fill is 5 cents.",
    "cagr": "Compound annual growth rate: the steady yearly return that gives the same end "
            "result.",
    "max_drawdown": "Largest drop from a peak to a later low. Closer to 0 is better.",
    "sharpe": "Return per unit of volatility, annualised. Higher is better; about 0.5 is "
              "typical for buying and holding the market.",
    "bt_trades": "Number of trades in the test, with win rate and average R.",
    "spy": "The benchmark: buying SPY at the start and holding it, with the same costs. "
           "The project rule is not to trade a strategy live unless it beats this.",
    "year_by_year": "Each test window, the rule version it traded, and how it did.",
    "run_trader": "The daily cycle: fill pending orders against the new bar, record exits, "
                  "raise trailing stops, and place new entries with their stop and target.",
    "clone": "Copy this profile's settings into a new, empty paper profile, e.g. to compare "
             "rule versions from identical starting conditions.",
    "new_profile": "Start a separate paper account with its own money and settings.",
    "capital": "Fake money the profile starts with.",
    "watchlist": "Tickers the Trader scans and ranks by momentum. Stocks and ETFs, plus listed "
                 "futures such as MES=F in paper profiles (see config/instruments.yaml). "
                 "Indexes like ^GSPC can't be traded; use an ETF or a future instead.",
    "benchmark": "What the strategy is compared with, bought at the start and held. SPY "
                 "includes dividends. An index such as ^GSPC is price-only (no dividends), so "
                 "it's a slightly easier bar to beat.",
    "options": "Read-only option chain from Yahoo (delayed). Options can't be traded or "
               "backtested here: there is no free history of option prices.",
    "underlying": "Stock or ETF whose options to show.",
    "expiry": "Date the options expire. Further-out expiries cost more but lose value more "
              "slowly.",
    "strike_window": "Only show strikes within this distance of the current price.",
    "calls": "Calls: the right to BUY 100 shares at the strike before expiry. They gain when "
             "the price rises.",
    "puts": "Puts: the right to SELL 100 shares at the strike before expiry. They gain when "
            "the price falls.",
    "strike": "Price at which the option lets you buy (call) or sell (put) the shares.",
    "last_price": "Last traded price per share. One contract covers 100 shares, so a 1.50 "
                  "option costs $150.",
    "bid": "Highest price a buyer is offering right now.",
    "ask": "Lowest price a seller is asking right now. A wide bid-ask gap means trading it "
           "is expensive.",
    "volume": "Contracts traded today.",
    "open_interest": "Contracts currently open. Higher means easier to trade.",
    "iv": "Implied volatility: the yearly price swing the option's price is betting on. "
          "Higher IV makes options more expensive.",
    "itm": "In the money. Call: strike below the current price. Put: strike above it.",
    "risk_per_trade": "Share of equity lost if a trade hits its stop. 1% of $100 is $1. "
                      "Position size is set from this.",
    "max_positions": "Most trades open at once.",
    "weekly_loss_limit": "If equity falls this much below the start of the week, no new "
                         "entries until next week.",
    "reset": "Deletes the profile's orders, trades and equity history, and starts again "
             "from its starting capital.",
}


# Rule settings: (short label, what it does).
PARAMS = {
    "trend_ma_days": ("Trend filter (days)",
                      "Only buy when price is above its N-day average; 200 is the long-term "
                      "trend. A close below it also exits open trades."),
    "momentum_lookback_days": ("Momentum lookback (days)",
                               "The watchlist is ranked by return over this many trading days "
                               "(63 ≈ 3 months, 126 ≈ 6 months)."),
    "top_n": ("Top N by momentum", "Only the N strongest tickers can be entered."),
    "rsi_period": ("RSI period (days)",
                   "Days used for RSI, a 0-100 gauge of recent up moves versus down moves."),
    "rsi_entry_below": ("Pullback entry: RSI below",
                        "Buy a dip when RSI falls below this while in an uptrend. 0 turns "
                        "pullback entries off."),
    "breakout_lookback_days": ("Breakout lookback (days)",
                               "Buy when the close beats the highest high of the previous N "
                               "days."),
    "atr_period": ("ATR period (days)",
                   "Days used for ATR, the average daily price range (volatility)."),
    "atr_stop_mult": ("Stop distance (× ATR)",
                      "Stop sits this many ATRs below entry, and the trailing stop follows "
                      "this far below the close. Wider means fewer stop-outs but smaller "
                      "positions, since risk per trade is fixed."),
    "target_r_multiple": ("Target (× risk)",
                          "Take-profit this many times the risk above entry. 2 = twice as far "
                          "up as the stop is down. Very large values effectively turn the "
                          "target off and let the trailing stop exit."),
}


def num(key: str, label: str | None = None, fmt: str = "$%.2f"):
    return st.column_config.NumberColumn(label, format=fmt, help=HELP[key])


def when(key: str, label: str | None = None):
    return st.column_config.DatetimeColumn(label, format="YYYY-MM-DD", help=HELP[key])


def text(key: str, label: str | None = None):
    return st.column_config.TextColumn(label, help=HELP[key])


# Column settings for every table, keyed by column name.
COLUMNS = {
    "symbol": text("symbol"), "setup": text("setup"), "exit_reason": text("exit_reason"),
    "rule_version": text("rule_version", "rules"),
    "entry_ts": when("entry_ts", "entered"), "exit_ts": when("exit_ts", "exited"),
    "entry_price": num("entry_price", "entry"), "exit_price": num("exit_price", "exit"),
    "qty": num("qty", fmt="%.4f"), "stop": num("stop", "stop at entry"),
    "current_stop": num("current_stop", "trailing stop"), "target": num("target"),
    "pnl": num("pnl", "P&L"), "r": num("r", "R", "%+.2f"),
    "trades": st.column_config.NumberColumn(help=HELP["closed_trades"]),
    "win_rate": num("win_rate", "win rate", "percent"),
    "total_pnl": num("total_pnl", "total P&L"), "avg_r": num("avg_r", "avg R", "%+.2f"),
}


def cols(df: pd.DataFrame, **extra) -> dict:
    """Column settings for the columns `df` actually has."""
    return {k: v for k, v in (COLUMNS | extra).items() if k in df.columns}


# --- data -------------------------------------------------------------------------------------

@st.cache_data(ttl=60)
def load(profile: str | None = None) -> dict[str, pd.DataFrame]:
    """All dashboard data in one short, read-only transaction."""
    with connect() as c:
        c.read_only = True  # BEGIN READ ONLY: viewing can never write

        def q(sql: str, *params) -> pd.DataFrame:
            return pd.DataFrame(c.execute(sql, params).fetchall())

        out = {
            "profiles": q("SELECT id, name, mode, broker, strategy_version, starting_capital, "
                          "config_json FROM profiles ORDER BY id"),
            "versions": q("SELECT version, params_json, created_at, approved_by, notes "
                          "FROM rule_versions ORDER BY id"),
        }
        if profile is not None:
            pid = int(out["profiles"].set_index("name").loc[profile, "id"])
            out["equity"] = q("SELECT date, cash, equity FROM equity_snapshots "
                              "WHERE profile_id = %s ORDER BY date", pid)
            out["trades"] = q(
                """SELECT t.id, t.symbol, t.reasoning_json->>'setup' AS setup, t.entry_ts,
                          t.entry_price, t.qty, t.stop, t.target, t.exit_ts, t.exit_price,
                          t.pnl, t.exit_reason, t.rule_version,
                          (SELECT o.stop_price FROM orders o WHERE o.trade_id = t.id
                             AND o.type = 'stop' AND o.status = 'pending' LIMIT 1) AS current_stop
                   FROM trades t WHERE t.profile_id = %s ORDER BY t.entry_ts DESC""", pid)
            out["orders"] = q("SELECT id, symbol, side, type, qty, limit_price, stop_price, "
                              "created_at FROM orders WHERE profile_id = %s AND status = 'pending' "
                              "ORDER BY id", pid)
        return out


def current() -> pd.Series:
    """The profile picked in the sidebar (its name is the Series' .name)."""
    return load()["profiles"].set_index("name").loc[ss["profile"]]


def versions() -> list[str]:
    return list(load()["versions"]["version"])


def split_trades(trades: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(open, closed); closed trades get an `r` column (P&L in multiples of initial risk)."""
    if trades.empty:
        return trades, trades
    closed = trades[trades["exit_ts"].notna()]
    closed = closed.assign(r=closed["pnl"] / (closed["qty"] * (closed["entry_price"]
                                                                - closed["stop"])))
    return trades[trades["exit_ts"].isna()], closed


def write(fn):
    """Run fn(conn) in one transaction (committed on success), then refresh cached views."""
    with connect() as c:
        out = fn(c)
    load.clear()
    return out


def flash(kind: str, msg: str, select: str | None = None) -> None:
    """Show `msg` after a rerun; optionally switch the sidebar to profile `select`."""
    ss["flash"] = (kind, msg)
    if select:
        ss["next_profile"] = select
    st.rerun()


def show_flash() -> None:
    if "flash" in ss:
        kind, msg = ss.pop("flash")
        getattr(st, kind)(msg)


@st.cache_data(ttl=3600, show_spinner="Running walk-forward backtest…")
def backtest(watchlist: tuple, risk: dict, capital: float, params: dict, slippage: float,
             benchmark: str = "SPY"):
    costs = Costs(slippage)
    bars = {s: load_bars(s) for s in watchlist}
    res, folds = walk_forward(bars, {v: Rules(**p) for v, p in params.items()}, Risk(**risk),
                              capital, costs)
    end = res.equity.index[-1] + pd.Timedelta(days=1)
    bench = buy_hold(load_bars(benchmark), capital, costs, res.equity.index[0], end)
    folds_df = pd.DataFrame([{
        "test from": f.test_start.date(), "test to": f.test_end.date(), "version": f.version,
        "trades": f.stats["n_trades"], "return": f.stats["total_return"],
        "max drawdown": f.stats["max_drawdown"],
    } for f in folds])
    curve = pd.DataFrame({"Strategy": res.equity, f"{benchmark} buy-and-hold": bench}).ffill()
    return stats(res, bench), folds_df, curve


@st.cache_data(ttl=300, show_spinner="Loading expiries…")
def option_expiries(symbol: str) -> list[str]:
    import yfinance as yf

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", module="yfinance")
        return list(yf.Ticker(symbol).options)


@st.cache_data(ttl=300, show_spinner="Loading option chain…")
def option_chain(symbol: str, expiry: str) -> tuple[pd.DataFrame, pd.DataFrame, float | None]:
    """(calls, puts, underlying price) for one expiry."""
    import yfinance as yf

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", module="yfinance")
        c = yf.Ticker(symbol).option_chain(expiry)
    return c.calls, c.puts, (c.underlying or {}).get("regularMarketPrice")


# --- password gate ----------------------------------------------------------------------------

MAX_FAILURES, WINDOW_S = 10, 15 * 60


@st.cache_resource
def _failures() -> list[float]:
    """Failed unlock times, shared by every session on this server (not per browser tab)."""
    return []


def is_unlocked() -> bool:
    return bool(os.environ.get("DASHBOARD_PASSWORD")) and ss.get("unlocked", False)


def require_unlock() -> bool:
    if is_unlocked():
        return True
    st.info("Unlock controls in the sidebar to use this page.", icon=":material/lock:")
    return False


def _lock_panel() -> None:
    secret = os.environ.get("DASHBOARD_PASSWORD")
    if not secret:
        st.caption(":material/lock: Controls disabled (DASHBOARD_PASSWORD not set).")
        return
    if ss.get("unlocked"):
        st.caption(":material/lock_open: Controls unlocked")
        if st.button("Lock", icon=":material/lock:"):
            ss["unlocked"] = False
            st.rerun()
        return
    with st.form("unlock", border=False):
        pw = st.text_input("Password", type="password", placeholder="Unlock controls",
                           help=HELP["password"])
        if not st.form_submit_button("Unlock", icon=":material/key:"):
            return
    fails = _failures()
    fails[:] = [t for t in fails if t > time.time() - WINDOW_S]
    if len(fails) >= MAX_FAILURES:
        st.error("Too many failed attempts. Try again in 15 minutes.")
        return
    if hmac.compare_digest(pw.encode(), secret.encode()):
        ss["unlocked"] = True
        st.rerun()
    fails.append(time.time())
    time.sleep(1)
    st.error("Wrong password.")


def sidebar() -> None:
    profiles = load()["profiles"]
    with st.sidebar:
        st.markdown("### SheepOfWallStreet")
        if profiles.empty:
            st.warning("No profiles yet. Run `python -m sheep.common.db` first.")
            st.stop()
        if "next_profile" in ss:  # set by create/clone; must land before the widget exists
            ss["profile"] = ss.pop("next_profile")
        if ss.get("profile") not in set(profiles["name"]):
            ss.pop("profile", None)
        modes = dict(zip(profiles["name"], profiles["mode"], strict=True))
        st.selectbox("Profile", profiles["name"], key="profile",
                     format_func=lambda n: f"{n}  ·  {modes[n]}", help=HELP["profile"])
        p = current()
        st.caption(f"Rules `{p['strategy_version']}` · broker `{p['broker']}` · "
                   f"started with ${p['starting_capital']:,.0f}", help=HELP["rules"])
        st.divider()
        _lock_panel()
        st.divider()
        st.caption("Paper trading and research tool. Not financial advice.")
