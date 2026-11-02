"""Phase 1 rules: trend filter, momentum rank, pullback/breakout entry, ATR stop, R-multiple target.

Every indicator at row t uses only bars <= t, so a signal on day t is known at t's close and
acted on at the next session.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Rules:
    """One rule version's params (a `versions.<v>` block of config/strategy.yaml)."""

    trend_ma_days: int
    momentum_lookback_days: int
    top_n: int
    rsi_period: int
    rsi_entry_below: float
    breakout_lookback_days: int
    atr_period: int
    atr_stop_mult: float
    target_r_multiple: float


@dataclass(frozen=True)
class Candidate:
    symbol: str
    date: pd.Timestamp
    setup: str  # "breakout" | "pullback"
    ref_price: float  # signal-day close; the actual fill happens next session
    stop: float
    target: float
    reasoning: dict


# --- indicators -------------------------------------------------------------------------------

def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int) -> pd.Series:
    delta = close.diff()
    gain, loss = _wilder(delta.clip(lower=0), n), _wilder(-delta.clip(upper=0), n)
    return 100 - 100 / (1 + gain / loss)


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    prev = df["close"].shift()
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1
    ).max(axis=1)
    return _wilder(tr, n)


# --- signals ----------------------------------------------------------------------------------

def signals(df: pd.DataFrame, r: Rules) -> pd.DataFrame:
    """Per-day indicator values and boolean setups for one symbol's OHLCV bars."""
    close = df["close"]
    out = pd.DataFrame(index=df.index)
    out["close"] = close
    out["sma_trend"] = sma(close, r.trend_ma_days)
    out["momentum"] = close.pct_change(r.momentum_lookback_days)
    out["rsi"] = rsi(close, r.rsi_period)
    out["atr"] = atr(df, r.atr_period)
    out["prior_high"] = df["high"].rolling(r.breakout_lookback_days).max().shift(1)
    out["trend_ok"] = close > out["sma_trend"]  # NaN (too little history) -> False
    out["pullback"] = out["trend_ok"] & (out["rsi"] < r.rsi_entry_below)
    out["breakout"] = out["trend_ok"] & (close > out["prior_high"])
    out["trend_break"] = close < out["sma_trend"]  # exit signal for open positions
    return out


def pick(day_rows: dict[str, pd.Series], r: Rules) -> list[Candidate]:
    """Rank one day's signal rows by momentum, keep the top_n, emit those with an entry setup.

    Symbols outside the top_n are never entered, even if the leaders have no setup that day.
    """
    ranked = sorted(
        (s for s, row in day_rows.items() if pd.notna(row["momentum"])),
        key=lambda s: day_rows[s]["momentum"],
        reverse=True,
    )
    out = []
    for rank, sym in enumerate(ranked[: r.top_n], start=1):
        row = day_rows[sym]
        setup = "breakout" if row["breakout"] else "pullback" if row["pullback"] else None
        close = float(row["close"])
        stop = close - r.atr_stop_mult * float(row["atr"])
        if setup is None or not 0 < stop < close:  # also rejects NaN ATR
            continue
        reasoning = {"rank": rank, "setup": setup} | {
            k: bool(v) if isinstance(v, (bool, np.bool_)) else float(v) for k, v in row.items()
        }
        out.append(
            Candidate(
                symbol=sym,
                date=row.name,
                setup=setup,
                ref_price=close,
                stop=stop,
                target=close + r.target_r_multiple * (close - stop),
                reasoning=reasoning,
            )
        )
    return out


def scan(bars: dict[str, pd.DataFrame], r: Rules) -> list[Candidate]:
    """Candidates as of the latest date in `bars`. Symbols without a bar that day are skipped."""
    sigs = {s: signals(df, r) for s, df in bars.items()}
    day = max(sig.index[-1] for sig in sigs.values())
    return pick({s: sig.loc[day] for s, sig in sigs.items() if day in sig.index}, r)


def trail_stop(stop: float, close: float, atr_value: float, mult: float) -> float:
    """Chandelier-style trailing stop: ratchets up with price, never down."""
    return max(stop, close - mult * atr_value)
