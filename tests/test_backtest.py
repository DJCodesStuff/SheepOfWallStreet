import numpy as np
import pandas as pd
import pytest

from sheep.backtest.engine import Costs, holdout_cutoff, run, walk_forward
from sheep.common.config import Risk
from sheep.trader.strategy import Rules

RULES = Rules(
    trend_ma_days=5, momentum_lookback_days=5, top_n=1, rsi_period=5, rsi_entry_below=0,
    breakout_lookback_days=5, atr_period=5, atr_stop_mult=2.0, target_r_multiple=10.0,
)
RISK = Risk(risk_per_trade_pct=1.0, max_open_positions=1, weekly_loss_limit_pct=100.0)
NO_COSTS = Costs(slippage_bps=0)


def rising_then_drop(n=30, drop_at=20, gap=False):
    """Steady 1%/day rise (a breakout every day), then one crash bar, then flat."""
    close = 100 * 1.01 ** np.arange(n)
    close[drop_at:] = close[drop_at - 1] * 0.85
    open_ = np.r_[close[0], close[:-1]]
    if gap:
        open_[drop_at] = close[drop_at]
    high, low = np.maximum(open_, close) * 1.005, np.minimum(open_, close) * 0.995
    idx = pd.bdate_range("2026-01-05", periods=n, name="date")
    return {"X": pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": 1e6}, index=idx
    )}


def test_sizing_risks_one_percent():
    t = run(rising_then_drop(), RULES, RISK, capital=100, costs=NO_COSTS).trades.iloc[0]
    assert (t.entry_price - t.initial_stop) * t.qty == pytest.approx(1.0)


def test_intraday_stop_fills_at_stop():
    t = run(rising_then_drop(), RULES, RISK, costs=NO_COSTS).trades.iloc[0]
    assert t.reason == "stop"
    assert t.exit_price == pytest.approx(t.stop)
    assert t.stop > t.initial_stop  # trailing stop ratcheted up during the rise


def test_gap_through_stop_fills_at_open():
    bars = rising_then_drop(gap=True)
    t = run(bars, RULES, RISK, costs=NO_COSTS).trades.iloc[0]
    assert t.reason == "stop_gap"
    assert t.exit_price == pytest.approx(bars["X"]["open"].iloc[20])


def test_costs_reduce_pnl():
    free = run(rising_then_drop(), RULES, RISK, costs=NO_COSTS).trades.pnl.sum()
    paid = run(rising_then_drop(), RULES, RISK, costs=Costs(50, 0.10)).trades.pnl.sum()
    assert paid < free


def test_equity_matches_trades_and_ends_flat():
    res = run(rising_then_drop(), RULES, RISK, capital=100, costs=Costs(10, 0.05))
    assert res.equity.iloc[-1] == pytest.approx(100 + res.trades.pnl.sum())


def test_walk_forward_never_touches_holdout():
    n = 252 * 8
    idx = pd.bdate_range("2010-01-01", periods=n, name="date")
    close = 100 * np.exp(np.cumsum(np.random.default_rng(0).normal(3e-4, 0.01, n)))
    df = pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                       "close": close, "volume": 1e6}, index=idx)
    bars = {"A": df, "B": df * 1.0}
    res, folds = walk_forward(bars, {"a": RULES, "b": RULES}, RISK, train_years=2, test_years=1)
    cutoff = holdout_cutoff(bars, 2)
    assert res.equity.index.max() < cutoff
    assert len(folds) >= 2
    for f, nxt in zip(folds, folds[1:], strict=False):
        assert f.test_end == nxt.test_start  # contiguous, non-overlapping test windows
