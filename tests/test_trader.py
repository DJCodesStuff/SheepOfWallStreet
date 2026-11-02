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


@pytest.fixture
def trader(db):
    """Run the 't' profile one evening at a time over synthetic bars, like the live cron would."""
    sync_rule_versions(db, {"t": RULES.__dict__})
    sync_profiles(db, {"t": Profile("t", "paper", "paper", 100.0, "t", ("X",), RISK)})
    df = rising_then_drop()["X"]

    def evening(i):
        d = df.index[i]
        now = datetime.combine(d.date(), time(18), NY)
        return run_profile(db, "t", NO_COSTS, bars_fn=lambda s: df.loc[:d], now=now)

    return db, df, evening


def test_daily_runs_match_backtest(trader):
    db, df, evening = trader
    for i in range(len(df)):
        evening(i)
    trades = db.execute("SELECT * FROM trades ORDER BY id").fetchall()
    bt = run({"X": df}, RULES, RISK, 100.0, NO_COSTS).trades
    assert len(trades) == len(bt) >= 1
    t, b = trades[0], bt.iloc[0]
    assert t["exit_reason"] == "stop" and b.reason == "stop"
    assert t["entry_price"] == pytest.approx(b.entry_price)
    assert t["exit_price"] == pytest.approx(b.exit_price)
    assert t["pnl"] == pytest.approx(b.pnl)
    assert t["reasoning_json"]["rule_version"] == "t"

    eq = db.execute("SELECT equity FROM equity_snapshots ORDER BY date DESC LIMIT 1").fetchone()
    assert eq["equity"] == pytest.approx(100 + sum(t["pnl"] for t in trades))


def test_rerun_same_evening_is_noop(trader):
    db, df, evening = trader
    for i in range(12):
        evening(i)
    before = db.execute("SELECT id, status, stop_price FROM orders ORDER BY id").fetchall()
    assert evening(11) == []
    assert db.execute("SELECT id, status, stop_price FROM orders ORDER BY id").fetchall() == before


def test_refuses_to_run_before_bar_is_final(trader):
    db, df, _ = trader
    monday_noon = datetime(2026, 1, 12, 12, tzinfo=NY)
    assert run_profile(db, "t", now=monday_noon)[0].startswith("skipped")
    assert db.execute("SELECT count(*) AS n FROM orders").fetchone()["n"] == 0


def test_trailing_stop_ratchets_open_trade(trader):
    db, df, evening = trader
    for i in range(15):
        evening(i)
    (t,) = db.execute("SELECT * FROM trades").fetchall()
    stop = db.execute(
        "SELECT stop_price FROM orders WHERE trade_id = %s AND type = 'stop' "
        "AND status = 'pending'", (t["id"],)
    ).fetchone()["stop_price"]
    assert stop > t["stop"]  # trades.stop keeps the stop at entry; the order trails
