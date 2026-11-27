from dataclasses import replace
from datetime import datetime, time

import pandas as pd
import pytest

from sheep.backtest.engine import run
from sheep.common.config import EQUITY, Profile
from sheep.common.db import sync_profiles, sync_rule_versions
from sheep.trader.data import NY
from sheep.trader.risk import check_entry, track_record
from sheep.trader.run import run_profile
from tests.test_backtest import NO_COSTS, RISK, RULES, rising_then_drop


def test_sized_entry_passes():
    # $100 equity, 1% risk: 0.5 shares x $2 to the stop = $1.
    assert check_entry("X", 0.5, 100, 98, 104, 100, RISK, EQUITY) == []


@pytest.mark.parametrize("qty, stop, target, risk, reason", [
    (0.6, 98, 104, RISK, "over the $1.00 max"),
    (0.5, 98, 101, RISK, "reward:risk 0.50"),
    (0.5, 98, 104, replace(RISK, max_position_pct=40), "over the 40% cap"),
    (0.5, 98, 104, replace(RISK, blocked=("X",)), "blocked list"),
    (0.5, 101, 104, RISK, "levels out of order"),
])
def test_entry_blocked(qty, stop, target, risk, reason):
    why = check_entry("X", qty, 100, stop, target, 100, risk, EQUITY)
    assert any(reason in w for w in why), why


def test_weekly_room_blocks_entry():
    why = check_entry("X", 0.5, 100, 98, 104, 100, RISK, EQUITY, room=0.5)
    assert any("weekly loss limit" in w for w in why), why
    assert check_entry("X", 0.5, 100, 98, 104, 100, RISK, EQUITY, room=1.5) == []


def test_backtest_applies_the_gate():
    bars = rising_then_drop()
    assert len(run(bars, RULES, RISK, 100, NO_COSTS).trades) >= 1
    assert run(bars, RULES, replace(RISK, blocked=("X",)), 100, NO_COSTS).trades.empty


def test_trader_blocks_stale_signal(db):
    sync_rule_versions(db, {"t": RULES.__dict__})
    sync_profiles(db, {"t": Profile("t", "paper", "paper", 100.0, "t", ("X",), RISK)})
    df = rising_then_drop()["X"]
    d = df.index[11]  # the open run after a day whose bar (index 10) the feed missed
    now = datetime.combine(d.date(), time(10), NY)
    log = run_profile(db, "t", NO_COSTS, bars_fn=lambda s: df.loc[:df.index[9]],
                      open_fn=lambda s, now: df.loc[d, "open"], now=now)
    assert any(line.startswith("BLOCK X breakout: stale signal") for line in log), log
    assert db.execute("SELECT count(*) AS n FROM orders").fetchone()["n"] == 0


def test_track_record_uses_only_closed_past_trades_of_the_setup():
    t = pd.DataFrame({
        "setup": ["breakout", "breakout", "pullback", "breakout", "breakout"],
        "exit_date": pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07",
                                     "2026-01-09"]),
        "r_multiple": [1.0, -0.5, -9.0, -5.0, 2.0],
        "reason": ["target", "stop", "stop", "stop", "end"],
    })
    at = pd.Timestamp("2026-01-07")  # the -5R trade closes today: not known at the open
    assert track_record(t, "breakout", at, 2) == (None, {"trades": 2, "need": 2, "avg_r": 0.25})
    assert "need 3" in track_record(t, "breakout", at, 3)[0]
    later = pd.Timestamp("2026-01-12")  # the "end" trade was only force-closed by the backtest
    assert "-2.75R" in track_record(t, "breakout", later, 2)[0]
    assert "0 past breakout trades" in track_record(pd.DataFrame(), "breakout", at, 1)[0]


def test_backtest_gate_needs_history_then_follows_it():
    bars = rising_then_drop(n=80, drop_at=70)  # +10R, +10R, then a stop
    off = run(bars, RULES, RISK, 100, NO_COSTS).trades
    on = run(bars, RULES, replace(RISK, backtest_min_trades=1), 100, NO_COSTS).trades
    assert len(off) == 3
    assert list(on["entry_date"]) == list(off["entry_date"][1:])  # first: no history yet
