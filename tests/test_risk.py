from dataclasses import replace
from datetime import datetime, time

import pytest

from sheep.backtest.engine import run
from sheep.common.config import EQUITY, Profile
from sheep.common.db import sync_profiles, sync_rule_versions
from sheep.trader.data import NY
from sheep.trader.risk import check_entry
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


def test_backtest_applies_the_gate():
    bars = rising_then_drop()
    assert len(run(bars, RULES, RISK, 100, NO_COSTS).trades) >= 1
    assert run(bars, RULES, replace(RISK, blocked=("X",)), 100, NO_COSTS).trades.empty


def test_trader_blocks_stale_signal(db):
    sync_rule_versions(db, {"t": RULES.__dict__})
    sync_profiles(db, {"t": Profile("t", "paper", "paper", 100.0, "t", ("X",), RISK)})
    df = rising_then_drop()["X"]
    d = df.index[10]
    now = datetime.combine(d.date(), time(18), NY)
    log = run_profile(db, "t", NO_COSTS, bars_fn=lambda s: df.loc[:df.index[9]], now=now)
    assert any(line.startswith("BLOCK X breakout: stale signal") for line in log), log
    assert db.execute("SELECT count(*) AS n FROM orders").fetchone()["n"] == 0
