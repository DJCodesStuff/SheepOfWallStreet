"""Pre-trade risk gate: every new entry passes these checks or is blocked. Never resized.

Adapted from trader-risk in day-trader-skill (MIT, Jake Schincariol,
github.com/Jakeschincariol/day-trader-skill). Exits are never checked: getting out must always be
possible. Its day-trading checks (entry window, trades per day, PDT) don't apply to one entry
run a day at the open, so they are left out. The Trader and the backtester both call this at
the opening price the entry fills at, so a backtest blocks what the Trader would.
"""

from __future__ import annotations

import math

import pandas as pd

from sheep.common.config import Instrument, Risk

_TOL = 1e-6  # float slack on the loss limit, which sizing hits exactly


def check_entry(symbol: str, qty: float, fill: float, stop: float, target: float,
                equity: float, risk: Risk, inst: Instrument, room: float = math.inf) -> list[str]:
    """Reasons to block a long entry at `fill`; empty means PASS.

    `room`: how much more can be lost this week before the weekly loss limit, after every open
    position's stop fills. Entries whose own stop would breach it are blocked, so positions
    opened together can't plan to lose more than the limit (gaps through stops still can).
    """
    why = []
    if symbol in risk.blocked:
        why.append(f"{symbol} is on the blocked list")
    if not 0 < stop < fill < target:
        return why + [f"levels out of order: stop {stop:.2f}, entry {fill:.2f}, "
                      f"target {target:.2f}"]
    loss = qty * (fill - stop) * inst.multiplier
    budget = equity * risk.risk_per_trade_pct / 100
    if loss > budget * (1 + _TOL):
        why.append(f"loss at the stop ${loss:.2f} is over the ${budget:.2f} max per trade")
    if loss > room:
        why.append(f"loss at the stop ${loss:.2f} is over the ${max(room, 0):.2f} left before "
                   f"the {risk.weekly_loss_limit_pct:g}% weekly loss limit, counting open "
                   "positions' stops")
    rr = (target - fill) / (fill - stop)
    if rr < risk.min_reward_risk:
        why.append(f"reward:risk {rr:.2f} is under the {risk.min_reward_risk:g} minimum")
    cost = qty * fill
    if not inst.is_future and cost > equity * risk.max_position_pct / 100 * (1 + _TOL):
        why.append(f"position ${cost:.2f} is {cost / equity:.0%} of equity, over the "
                   f"{risk.max_position_pct:g}% cap")
    return why


def track_record(trades: pd.DataFrame, setup: str, asof: pd.Timestamp, n: int
                 ) -> tuple[str | None, dict]:
    """Backtest gate, the last check before an entry: (reason to block or None, summary).

    `trades`: one symbol's backtest (engine.symbol_history). Only trades of this setup closed
    before `asof` count, so there is no look-ahead; the last `n` must average a positive R.
    Positions the backtest force-closed at the end of its data (reason "end") never really
    closed, so they don't count (the Trader's history ends yesterday; the backtester's doesn't).
    """
    past = trades[(trades["setup"] == setup) & (trades["exit_date"] < asof)
                  & (trades["reason"] != "end")].tail(n) if len(trades) else trades
    r = float(past["r_multiple"].mean()) if len(past) else float("nan")
    summary = {"trades": len(past), "need": n, "avg_r": r}
    if len(past) < n:
        return f"backtest has {len(past)} past {setup} trades, need {n}", summary
    if not r > 0:
        return f"backtest: last {n} {setup} trades averaged {r:+.2f}R", summary
    return None, summary
