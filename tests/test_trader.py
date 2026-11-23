from dataclasses import replace
from datetime import datetime, time

import pytest

from sheep.backtest.engine import Costs, run
from sheep.common.config import Profile, Risk
from sheep.common.db import sync_profiles, sync_rule_versions
from sheep.trader.data import NY
from sheep.trader.run import run_profile
from tests.test_backtest import RULES, rising_then_drop

NO_COSTS = Costs(0)
RISK = Risk(risk_per_trade_pct=1.0, max_open_positions=1, weekly_loss_limit_pct=100.0)


def run_days(db, name, bars, days=None):
    """Run the open and evening runs of each day over synthetic bars, like the two crons."""
    idx = next(iter(bars.values())).index
    logs = []
    for i, d in enumerate(idx[:days]):
        for hour in (10, 18) if i else (18,):
            upto = idx[i - 1] if hour < 16 else d  # before the close only earlier bars are final
            logs += run_profile(db, name, NO_COSTS, bars_fn=lambda s, u=upto: bars[s].loc[:u],
                                open_fn=lambda s, now, d=d: bars[s].loc[d, "open"],
                                now=datetime.combine(d.date(), time(hour), NY))
    return logs


def setup_profile(db, bars, risk=RISK, name="t", capital=100.0, rules=RULES):
    sync_rule_versions(db, {"t": rules.__dict__})
    sync_profiles(db, {name: Profile(name, "paper", "paper", capital, "t", tuple(bars), risk)})


def gapped(gap):
    """rising_then_drop with every open `gap` times the previous close."""
    df = rising_then_drop()["X"]
    df["open"] *= gap
    df["high"] = df[["open", "high"]].max(axis=1)
    return {"X": df}


TWO = {**rising_then_drop(), **rising_then_drop(symbol="Y")}  # same signal, same days
TWO_RULES = replace(RULES, top_n=2)
TIGHT = replace(RISK, max_open_positions=2, weekly_loss_limit_pct=1.5)  # room for one entry


@pytest.mark.parametrize("bars, risk, rules", [
    (rising_then_drop(), RISK, RULES),
    (gapped(1.01), RISK, RULES),  # entries gap up: sizing at the open must still risk 1%
    (TWO, TIGHT, TWO_RULES),
], ids=["plain", "gap_up", "weekly_room"])
def test_daily_runs_match_backtest(db, bars, risk, rules):
    setup_profile(db, bars, risk, rules=rules)
    run_days(db, "t", bars)
    trades = db.execute("SELECT * FROM trades ORDER BY entry_ts, id").fetchall()
    bt = run(bars, rules, risk, 100.0, NO_COSTS).trades
    assert len(trades) == len(bt) >= 1
    for t, b in zip(trades, bt.itertuples(), strict=True):
        assert (t["symbol"], t["exit_reason"]) == (b.symbol, b.reason)
        assert t["entry_ts"].astimezone(NY).date() == b.entry_date.date()
        assert t["entry_price"] == pytest.approx(b.entry_price)
        assert t["exit_price"] == pytest.approx(b.exit_price)
        assert t["pnl"] == pytest.approx(b.pnl)
        assert t["reasoning_json"]["rule_version"] == "t"
    t = trades[0]
    assert t["qty"] * (t["entry_price"] - t["stop"]) == pytest.approx(1.0)  # 1% of $100

    eq = db.execute("SELECT equity FROM equity_snapshots ORDER BY date DESC LIMIT 1").fetchone()
    assert eq["equity"] == pytest.approx(100 + sum(t["pnl"] for t in trades))


def test_weekly_room_blocks_second_same_day_entry(db):
    setup_profile(db, TWO, TIGHT, rules=TWO_RULES)
    logs = run_days(db, "t", TWO, days=8)
    assert any(line.startswith("BLOCK Y") and "weekly loss limit" in line for line in logs), logs


def test_rerun_is_noop(db):
    bars = rising_then_drop()
    setup_profile(db, bars)
    run_days(db, "t", bars, days=12)
    before = db.execute("SELECT id, status, stop_price FROM orders ORDER BY id").fetchall()
    d = bars["X"].index[11]
    for hour in (10, 18):
        log = run_profile(db, "t", NO_COSTS, bars_fn=lambda s: bars[s].loc[:d],
                          open_fn=lambda s, now: bars[s].loc[d, "open"],
                          now=datetime.combine(d.date(), time(hour), NY))
        assert all(line.startswith("signal") for line in log), log
    assert db.execute("SELECT id, status, stop_price FROM orders ORDER BY id").fetchall() == before


@pytest.mark.parametrize("hm", [(8, 0), (16, 15)])
def test_skips_outside_run_windows(db, hm):
    setup_profile(db, rising_then_drop())
    monday = datetime(2026, 1, 12, *hm, tzinfo=NY)
    assert run_profile(db, "t", now=monday)[0].startswith("skipped")
    assert db.execute("SELECT count(*) AS n FROM orders").fetchone()["n"] == 0


def test_no_opening_price_skips_entry(db):
    bars = rising_then_drop()
    setup_profile(db, bars)
    run_days(db, "t", bars, days=6)  # first signal: index 5's close
    d = bars["X"].index[6]
    log = run_profile(db, "t", NO_COSTS, bars_fn=lambda s: bars[s].loc[:bars[s].index[5]],
                      open_fn=lambda s, now: None, now=datetime.combine(d.date(), time(10), NY))
    assert any("no opening price" in line for line in log), log
    assert db.execute("SELECT count(*) AS n FROM orders").fetchone()["n"] == 0


def test_trailing_stop_ratchets_open_trade(db):
    bars = rising_then_drop()
    setup_profile(db, bars)
    run_days(db, "t", bars, days=15)
    (t,) = db.execute("SELECT * FROM trades").fetchall()
    stop = db.execute(
        "SELECT stop_price FROM orders WHERE trade_id = %s AND type = 'stop' "
        "AND status = 'pending'", (t["id"],)
    ).fetchone()["stop_price"]
    assert stop > t["stop"]  # trades.stop keeps the stop at entry; the order trails


def test_futures_daily_runs_match_backtest_through_roll(db):
    from tests.test_backtest import mes_bars

    bars = mes_bars()
    setup_profile(db, bars, name="f", capital=61_000.0)
    logs = run_days(db, "f", bars)

    (t,) = db.execute("SELECT * FROM trades").fetchall()
    b = run(bars, RULES, RISK, 61_000.0, NO_COSTS).trades.iloc[0]
    assert any("roll to next contract" in line for line in logs)
    assert t["qty"] == b.qty == 24
    assert t["exit_reason"] == "stop" and b.reason == "stop"
    assert t["pnl"] == pytest.approx(b.pnl)
    eq = db.execute("SELECT equity FROM equity_snapshots ORDER BY date DESC LIMIT 1").fetchone()
    assert eq["equity"] == pytest.approx(61_000 + t["pnl"])
