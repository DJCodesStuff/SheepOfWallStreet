"""Per-profile dashboard. Viewing is open; controls unlock with DASHBOARD_PASSWORD.

Local:  uv run --env-file .env streamlit run streamlit_app.py
Cloud:  Streamlit Community Cloud, main file `streamlit_app.py`. In the app's Secrets, at root
        level (Streamlit exposes those as env vars):
            DATABASE_URL = "postgresql://..."
            DASHBOARD_PASSWORD = "..."

Controls only ever act on paper profiles: the Trader and reset refuse live profiles, and
create/clone always make paper ones.
"""

import hmac
import os
import time

import pandas as pd
import psycopg
import streamlit as st

from sheep.backtest.engine import Costs, buy_hold, stats, walk_forward
from sheep.common.config import ConfigError, Risk, parse_profile
from sheep.common.db import connect, sync_profiles
from sheep.profiles.manage import clone, reset
from sheep.trader.data import load_bars
from sheep.trader.run import run_profile
from sheep.trader.strategy import Rules

st.set_page_config(page_title="SheepOfWallStreet", layout="wide")
ss = st.session_state


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
            "versions": q("SELECT version, params_json FROM rule_versions ORDER BY id"),
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


def write(fn):
    """Run fn(conn) in one transaction (committed on success), then refresh cached views."""
    with connect() as c:
        out = fn(c)
    load.clear()
    return out


def flash(kind: str, msg: str) -> None:
    ss["flash"] = (kind, msg)
    st.rerun()


@st.cache_data(ttl=3600, show_spinner="Running walk-forward backtest…")
def backtest(watchlist: tuple, risk: dict, capital: float, params: dict, slippage: float):
    costs = Costs(slippage)
    bars = {s: load_bars(s) for s in watchlist}
    res, folds = walk_forward(bars, {v: Rules(**p) for v, p in params.items()}, Risk(**risk),
                              capital, costs)
    end = res.equity.index[-1] + pd.Timedelta(days=1)
    bench = buy_hold(load_bars("SPY"), capital, costs, res.equity.index[0], end)
    folds_df = pd.DataFrame([{
        "test from": f.test_start.date(), "test to": f.test_end.date(), "version": f.version,
        "trades": f.stats["n_trades"], "return": f.stats["total_return"],
        "max drawdown": f.stats["max_drawdown"],
    } for f in folds])
    curve = pd.DataFrame({"strategy": res.equity, "SPY buy-and-hold": bench}).ffill()
    return stats(res, bench), folds_df, curve


# --- password gate ----------------------------------------------------------------------------

@st.cache_resource
def _failures() -> list[float]:
    """Failed unlock times, shared by every session on this server (not per browser tab)."""
    return []


MAX_FAILURES, WINDOW_S = 10, 15 * 60


def unlocked() -> bool:
    secret = os.environ.get("DASHBOARD_PASSWORD")
    if not secret:
        st.sidebar.caption("Controls disabled: DASHBOARD_PASSWORD is not set.")
        return False
    if ss.get("unlocked"):
        if st.sidebar.button("Lock controls"):
            ss["unlocked"] = False
            st.rerun()
        return True
    with st.sidebar.form("unlock"):
        pw = st.text_input("Password to unlock controls", type="password")
        if not st.form_submit_button("Unlock"):
            return False
    fails = _failures()
    fails[:] = [t for t in fails if t > time.time() - WINDOW_S]
    if len(fails) >= MAX_FAILURES:
        st.sidebar.error("Too many failed attempts. Try again in 15 minutes.")
        return False
    if hmac.compare_digest(pw.encode(), secret.encode()):
        ss["unlocked"] = True
        st.rerun()
    fails.append(time.time())
    time.sleep(1)
    st.sidebar.error("Wrong password.")
    return False


# --- page -------------------------------------------------------------------------------------

base = load()
profiles, versions = base["profiles"], base["versions"]
all_versions = list(versions["version"])
if profiles.empty:
    st.warning("No profiles in the database yet. Run `python -m sheep.common.db` first.")
    st.stop()

name = st.sidebar.selectbox("Profile", profiles["name"], key="profile")
can = unlocked()
p = profiles.set_index("name").loc[name]
d = load(name)
eq, trades, orders = d["equity"], d["trades"], d["orders"]
is_paper = p["broker"] == "paper"

st.title(name)
st.caption(f"{p['mode'].upper()} · broker `{p['broker']}` · rules `{p['strategy_version']}` · "
           "Not financial advice.")
if "flash" in ss:
    kind, msg = ss.pop("flash")
    getattr(st, kind)(msg)

overview, bt_tab, manage = st.tabs(["Overview", "Backtest", "Manage"])

with overview:
    capital = float(p["starting_capital"])
    equity = float(eq["equity"].iloc[-1]) if len(eq) else capital
    cash = float(eq["cash"].iloc[-1]) if len(eq) else capital
    closed = trades[trades["exit_ts"].notna()] if len(trades) else trades
    open_ = trades[trades["exit_ts"].isna()] if len(trades) else trades

    m = st.columns(5)
    m[0].metric("Equity", f"${equity:,.2f}", f"{equity / capital - 1:+.2%}")
    m[1].metric("Cash", f"${cash:,.2f}")
    m[2].metric("Open trades", len(open_))
    m[3].metric("Closed trades", len(closed))
    if len(closed):
        m[4].metric("Win rate", f"{(closed['pnl'] > 0).mean():.0%}",
                    f"expectancy ${closed['pnl'].mean():+.2f}/trade")

    st.subheader("Equity")
    if len(eq):
        st.line_chart(eq.set_index("date")[["equity"]], y_label="USD")
    else:
        st.caption("No snapshots yet. The Trader writes one each evening.")

    st.subheader("Open trades")
    if len(open_):
        st.dataframe(open_[["symbol", "setup", "entry_ts", "entry_price", "qty", "stop",
                            "current_stop", "target", "rule_version"]], hide_index=True)
    else:
        st.caption("None.")

    st.subheader("Trade journal")
    if len(closed):
        risk_usd = closed["qty"] * (closed["entry_price"] - closed["stop"])
        j = closed.assign(r_multiple=closed["pnl"] / risk_usd)
        st.dataframe(j[["symbol", "setup", "entry_ts", "exit_ts", "entry_price", "exit_price",
                        "qty", "pnl", "r_multiple", "exit_reason", "rule_version"]],
                     hide_index=True)
    else:
        st.caption("No closed trades yet.")

    st.subheader("Open orders")
    if len(orders):
        st.dataframe(orders, hide_index=True)
    else:
        st.caption("None.")

with bt_tab:
    if not can:
        st.info("Unlock controls in the sidebar to run backtests.")
    else:
        st.caption("Walk-forward: each year trades the version that did best on the 3 years "
                   "before it. The last 2 years are held out and never used here.")
        chosen = st.multiselect("Rule versions", all_versions, default=[p["strategy_version"]])
        slip = st.number_input("Slippage (bps per fill)", 0.0, 100.0, 5.0, step=1.0)
        if st.button("Run walk-forward", disabled=not chosen):
            cfg = p["config_json"]
            params = versions.set_index("version").loc[chosen, "params_json"].to_dict()
            try:
                s, folds, curve = backtest(tuple(cfg["watchlist"]), cfg["risk"], capital,
                                           params, slip)
            except Exception as e:  # noqa: BLE001 - surface data/fit errors in the UI
                st.error(f"Backtest failed: {e}")
            else:
                rows = ["total_return", "cagr", "max_drawdown", "sharpe"]
                st.dataframe(pd.DataFrame({
                    "strategy": [s[k] for k in rows],
                    "SPY buy-and-hold": [s[f"bench_{k}"] for k in rows],
                }, index=rows), width="content")
                st.caption(f"{s['n_trades']} trades · win rate {s['win_rate']:.0%} · "
                           f"avg R {s['avg_r']:+.2f}")
                st.line_chart(curve, y_label="USD")
                st.dataframe(folds, hide_index=True)

with manage:
    if not can:
        st.info("Unlock controls in the sidebar to manage profiles.")
    else:
        st.subheader("Run Trader now")
        st.caption("Daily cycle for this profile: fills, exits, new entries. Built-in paper "
                   "profiles only. Refuses before 16:30 New York time on weekdays.")
        if st.button(f"Run Trader for {name}", disabled=not is_paper):
            try:
                lines = write(lambda c: run_profile(c, name))
                flash("success", "\n\n".join(lines) or "No action.")
            except (ValueError, psycopg.Error) as e:
                flash("error", f"Trader failed: {e}")

        st.subheader("Create paper profile")
        with st.form("create"):
            c1, c2 = st.columns(2)
            new = c1.text_input("Name")
            ver = c2.selectbox("Rule version", all_versions)
            capital_in = c1.number_input("Starting capital (USD)", 1.0, 1e6, 100.0)
            watch = c2.text_input("Watchlist (comma-separated)", "SPY, QQQ, IWM")
            rpt = c1.number_input("Risk per trade %", 0.1, 5.0, 1.0, step=0.1)
            maxpos = c2.number_input("Max open positions", 1, 10, 3)
            wll = c1.number_input("Weekly loss limit %", 0.5, 20.0, 3.0, step=0.5)
            if st.form_submit_button("Create"):
                raw = {
                    "name": new.strip(), "mode": "paper", "broker": "paper",
                    "starting_capital": capital_in, "strategy_version": ver,
                    "watchlist": [s.strip() for s in watch.split(",") if s.strip()],
                    "risk": {"risk_per_trade_pct": rpt, "max_open_positions": int(maxpos),
                             "weekly_loss_limit_pct": wll},
                }
                try:
                    prof = parse_profile(raw, dict.fromkeys(all_versions))
                    if prof.name in set(profiles["name"]):
                        raise ConfigError(f"profile {prof.name} already exists")
                    write(lambda c: sync_profiles(c, {prof.name: prof}))
                    flash("success", f"Created paper profile {prof.name}.")
                except ConfigError as e:
                    st.error(str(e))

        st.subheader("Clone profile")
        with st.form("clone"):
            c1, c2 = st.columns(2)
            clone_name = c1.text_input("New name")
            clone_ver = c2.selectbox("Rule version", all_versions,
                                     index=all_versions.index(p["strategy_version"]))
            st.caption(f"Copies {name}'s settings into a new, empty paper profile.")
            if st.form_submit_button("Clone"):
                try:
                    write(lambda c: clone(c, name, clone_name.strip(), clone_ver))
                    flash("success", f"Cloned {name} → {clone_name.strip()} (paper).")
                except (ValueError, psycopg.Error) as e:
                    st.error(f"Clone failed: {e}")

        st.subheader("Reset profile")
        if p["mode"] != "paper":
            st.caption("Live profiles can't be reset.")
        else:
            st.caption(f"Deletes all of {name}'s orders, trades, tags and equity snapshots. "
                       "Cannot be undone.")
            confirm = st.text_input("Type the profile name to confirm", key=f"reset-{name}")
            if st.button(f"Reset {name}", type="primary", disabled=confirm != name):
                write(lambda c: reset(c, name))
                flash("success", f"Reset {name}.")
