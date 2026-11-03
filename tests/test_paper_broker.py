from datetime import datetime

import pandas as pd
import pytest

from sheep.backtest.engine import Costs
from sheep.broker.paper import PaperBroker
from sheep.common.config import load_profiles, load_strategies
from sheep.common.db import sync_profiles, sync_rule_versions
from sheep.profiles.manage import clone, reset
from sheep.trader.data import NY

NO_COSTS = Costs(0)
FRI_EVENING = datetime(2026, 1, 2, 18, tzinfo=NY)  # first bar below is Mon 2026-01-05
MON_EVENING = datetime(2026, 1, 5, 18, tzinfo=NY)


def frame(*ohlc):
    idx = pd.bdate_range("2026-01-05", periods=len(ohlc), name="date")
    return pd.DataFrame(ohlc, columns=["open", "high", "low", "close"], index=idx)


@pytest.fixture
def make(db):
    sync_rule_versions(db, load_strategies())
    sync_profiles(db, load_profiles())

    def _make(*ohlc, costs=NO_COSTS):
        return PaperBroker(db, "main_paper", costs, bars=lambda s: frame(*ohlc))

    return _make


def test_market_buy_fills_next_open_with_slippage(make):
    b = make((100, 101, 99, 100.5), costs=Costs(slippage_bps=10, fee_per_order=0.05))
    oid = b.submit("spy", "buy", 0.5, at=FRI_EVENING)
    assert b.refresh() == [oid]
    o = b.order(oid)
    assert o["fill_price"] == pytest.approx(100.1)
    assert b.cash() == pytest.approx(100 - 0.5 * 100.1 - 0.05)
    assert b.positions() == {"SPY": 0.5}
    assert b.equity() == pytest.approx(b.cash() + 0.5 * 100.5)


def test_order_waits_for_bar_after_placement(make):
    b = make((100, 101, 99, 100), (102, 103, 101, 102))
    oid = b.submit("SPY", "buy", 0.1, at=MON_EVENING)  # Monday's bar is already done
    b.refresh()
    assert b.order(oid)["fill_price"] == pytest.approx(102)


def test_oco_stop_fills_and_cancels_target(make):
    b = make((100, 101, 99, 100), (99, 99.5, 94, 96))
    b.submit("SPY", "buy", 0.5, at=FRI_EVENING)
    stop = b.submit("SPY", "sell", 0.5, "stop", stop_price=95, oco_group="t1", at=FRI_EVENING)
    target = b.submit("SPY", "sell", 0.5, "limit", limit_price=110, oco_group="t1",
                      at=FRI_EVENING)
    b.refresh()
    assert b.order(stop)["status"] == "filled"
    assert b.order(stop)["fill_price"] == pytest.approx(95)
    assert b.order(target)["status"] == "cancelled"
    assert b.positions() == {}


def test_stop_gap_fills_at_open(make):
    b = make((100, 101, 99, 100), (90, 91, 88, 89))
    b.submit("SPY", "buy", 0.5, at=FRI_EVENING)
    stop = b.submit("SPY", "sell", 0.5, "stop", stop_price=95, at=MON_EVENING)
    b.refresh()
    assert b.order(stop)["fill_price"] == pytest.approx(90)


def test_rejects_overspend_and_oversell(make):
    b = make((100, 101, 99, 100))
    big = b.submit("SPY", "buy", 2, at=FRI_EVENING)  # $200 with $100 cash
    short = b.submit("QQQ", "sell", 1, at=FRI_EVENING)  # nothing held
    assert b.refresh() == []
    assert b.order(big)["status"] == b.order(short)["status"] == "rejected"
    assert b.cash() == 100


def test_price_required_for_stop_and_limit(make):
    b = make((100, 101, 99, 100))
    with pytest.raises(ValueError):
        b.submit("SPY", "sell", 1, "stop")
    with pytest.raises(ValueError):
        b.submit("SPY", "sell", 1, "market", limit_price=5)


def test_live_profile_refused(make, db):
    with pytest.raises(ValueError, match="not a built-in paper"):
        PaperBroker(db, "live_small")


def test_reset_and_clone(make, db):
    b = make((100, 101, 99, 100))
    b.submit("SPY", "buy", 0.5, at=FRI_EVENING)
    b.refresh()
    reset(db, "main_paper")
    assert b.cash() == 100 and b.positions() == {}
    with pytest.raises(ValueError, match="refusing"):
        reset(db, "live_small")

    clone(db, "live_small", "live_copy", "v2_candidate")
    c = db.execute("SELECT * FROM profiles WHERE name = 'live_copy'").fetchone()
    assert (c["mode"], c["broker"], c["strategy_version"]) == ("paper", "paper", "v2_candidate")
    assert c["config_json"]["mode"] == "paper"


def test_create_and_switch_rule_versions(make, db):
    from sheep.profiles.manage import create_rule_version, set_rule_version

    params = load_strategies()["v1"] | {"atr_stop_mult": 3.0}
    create_rule_version(db, "v1_wide", params, "wider stop")
    row = db.execute("SELECT * FROM rule_versions WHERE version = 'v1_wide'").fetchone()
    assert row["params_json"]["atr_stop_mult"] == 3.0 and row["notes"] == "wider stop"
    with pytest.raises(ValueError, match="already exists"):
        create_rule_version(db, "v1_wide", params)

    set_rule_version(db, "main_paper", "v1_wide")
    p = db.execute("SELECT * FROM profiles WHERE name = 'main_paper'").fetchone()
    assert p["strategy_version"] == p["config_json"]["strategy_version"] == "v1_wide"
    with pytest.raises(ValueError, match="live"):
        set_rule_version(db, "live_small", "v1_wide")
    with pytest.raises(ValueError, match="unknown"):
        set_rule_version(db, "main_paper", "nope")
