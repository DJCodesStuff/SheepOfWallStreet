import json

import numpy as np
import pandas as pd
import pytest

from sheep.common.config import load_strategies
from sheep.trader.strategy import Rules, atr, pick, rsi, scan, signals, trail_stop

RULES = Rules(
    trend_ma_days=50, momentum_lookback_days=30, top_n=1, rsi_period=14, rsi_entry_below=40,
    breakout_lookback_days=20, atr_period=14, atr_stop_mult=2.0, target_r_multiple=2.0,
)


def bars(daily_growth, n=120, last_jump=1.0):
    close = 100 * (1 + daily_growth) ** np.arange(n)
    close[-1] *= last_jump
    idx = pd.bdate_range("2026-01-01", periods=n, name="date")
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1e6},
        index=idx,
    )


def test_every_config_version_parses():
    for params in load_strategies().values():
        Rules(**params)


def test_rsi_extremes():
    up = pd.Series(np.arange(1.0, 40.0))
    assert rsi(up, 14).iloc[-1] == 100
    assert rsi(up[::-1].reset_index(drop=True), 14).iloc[-1] == 0


def test_atr_constant_range():
    df = pd.DataFrame({"high": [102.0] * 30, "low": [98.0] * 30, "close": [100.0] * 30})
    assert atr(df, 14).iloc[-1] == pytest.approx(4.0)


def test_no_look_ahead():
    df = bars(0.002)
    changed = df.copy()
    changed.iloc[100:] *= 3  # rewrite the future
    a, b = signals(df, RULES).iloc[:100], signals(changed, RULES).iloc[:100]
    pd.testing.assert_frame_equal(a, b)


def test_scan_picks_top_momentum_breakout():
    universe = {
        "STRONG": bars(0.004, last_jump=1.05),  # uptrend, breaks out on the last bar
        "WEAK": bars(0.001, last_jump=1.05),  # also breaks out, but lower momentum
        "DOWN": bars(-0.003),
    }
    (c,) = scan(universe, RULES)
    assert (c.symbol, c.setup) == ("STRONG", "breakout")
    risk = c.ref_price - c.stop
    assert risk == pytest.approx(2.0 * c.reasoning["atr"])
    assert c.target == pytest.approx(c.ref_price + 2.0 * risk)
    json.dumps(c.reasoning)  # must be storable in trades.reasoning_json


def test_no_entry_without_setup_or_history():
    assert scan({"DOWN": bars(-0.003)}, RULES) == []
    assert scan({"SHORT": bars(0.004, n=40, last_jump=1.05)}, RULES) == []  # < 50 bars for SMA


def test_leader_without_setup_blocks_others():
    universe = {"STRONG": bars(0.004), "WEAK": bars(0.001, last_jump=1.05)}
    sigs = {s: signals(df, RULES).iloc[-1] for s, df in universe.items()}
    assert pick(sigs, RULES) == []


def test_trail_stop_only_ratchets_up():
    assert trail_stop(95, close=110, atr_value=2, mult=2) == 106
    assert trail_stop(106, close=100, atr_value=2, mult=2) == 106


@pytest.mark.parametrize("change", [
    {"top_n": 0},  # below range
    {"atr_stop_mult": 50},  # above range
    {"trend_ma_days": 200.5},  # must be whole
    {"rsi_period": "14"},  # must be a number
    {"extra": 1},  # unknown key
])
def test_validate_rules_rejects(change):
    from sheep.trader.strategy import validate_rules

    params = load_strategies()["v1"] | change
    with pytest.raises(ValueError):
        validate_rules(params)


def test_validate_rules_accepts_config_versions():
    from sheep.trader.strategy import validate_rules

    for params in load_strategies().values():
        validate_rules(params)
