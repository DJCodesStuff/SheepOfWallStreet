import numpy as np
import pandas as pd
import pytest

from sheep.backtest.engine import Costs
from sheep.backtest.timemachine import history, simulate, verdict
from tests.test_backtest import NO_COSTS, RULES, rising_then_drop


def bars(o, h, lo, c, n=100):
    """n identical daily bars, each as (open, high, low, close) / the previous close."""
    close = 100 * c ** np.arange(n)
    prev = np.r_[100 / c, close[:-1]]
    idx = pd.bdate_range("2025-01-02", periods=n)
    return pd.DataFrame({"open": prev * o, "high": prev * h, "low": prev * lo, "close": close},
                        index=idx)


def test_same_seed_same_numbers_and_odds_sum_to_one():
    df = rising_then_drop(n=120, drop_at=120)["X"]
    df["close"] *= 1 + 0.02 * np.sin(np.arange(len(df)))  # some up and down days
    a, b = (simulate(df, 100, 97, 106, seed=7, costs=NO_COSTS) for _ in range(2))
    assert a["p_target"] == b["p_target"] and a["avg_r"] == b["avg_r"]
    assert a["p_target"] + a["p_stop"] + a["p_neither"] == pytest.approx(1)


def test_steady_rise_hits_target_at_its_r():
    mc = simulate(bars(1.0, 1.02, 1.0, 1.01), 100, 95, 110, costs=NO_COSTS)
    assert mc["p_target"] == 1 and mc["avg_r"] == pytest.approx(2)


def test_gap_through_stop_fills_at_open():
    mc = simulate(bars(0.9, 0.9, 0.88, 0.89), 100, 95, 110, costs=NO_COSTS)
    assert mc["p_stop"] == 1
    assert mc["avg_r"] == pytest.approx(-2)  # filled at 90, not 95: worse than -1R


def test_exits_pay_slippage():
    mc = simulate(bars(1.0, 1.02, 1.0, 1.01), 100, 95, 110, costs=Costs(100))
    assert mc["avg_r"] == pytest.approx((110 * 0.99 - 100) / 5)


def test_refuses_bad_plans():
    with pytest.raises(ValueError):
        simulate(bars(1, 1.01, 0.99, 1), 100, 101, 110)
    with pytest.raises(ValueError, match="usable daily bars"):
        simulate(bars(1, 1.01, 0.99, 1, n=20), 100, 95, 110)


@pytest.mark.parametrize("pt, ps, r, head", [
    (0.30, 0.30, 1.0, "Close to a coin flip"),
    (0.40, 0.30, 1.0, "Leans toward the target"),
    (0.10, 0.60, 1.0, "Strongly toward the stop"),
    (0.24, 0.50, 2.0, "Close to break-even"),  # needs 33% of hits at 2R and got 32%
])
def test_verdict(pt, ps, r, head):
    mc = {"p_target": pt, "p_stop": ps, "p_neither": 1 - pt - ps, "r_at_target": r,
          "avg_r": 0.0, "days": 20}
    assert verdict(mc)[0].startswith(head)


def test_history_replays_each_setup_day():
    h = history(rising_then_drop(n=60, drop_at=40)["X"], RULES, days=5, costs=NO_COSTS)
    assert h["fired"] > 0
    assert h["target"] + h["stop"] + h["neither"] == h["fired"]
    assert h["stop"] >= 1  # the setups just before the crash
