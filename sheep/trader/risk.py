"""Pre-trade risk gate: every new entry passes these checks or is blocked. Never resized.

Adapted from trader-risk in day-trader-skill (MIT, Jake Schincariol,
github.com/Jakeschincariol/day-trader-skill). Exits are never checked: getting out must always be
possible. Its day-trading checks (entry window, trades per day, PDT) don't apply to orders placed
after the close for the next open, so they are left out. The backtester calls the same function
at the actual fill, so a backtest blocks what the Trader would.
"""

from __future__ import annotations

from sheep.common.config import Instrument, Risk

_TOL = 1e-6  # float slack on the loss limit, which sizing hits exactly


def check_entry(symbol: str, qty: float, fill: float, stop: float, target: float,
                equity: float, risk: Risk, inst: Instrument) -> list[str]:
    """Reasons to block a long entry at `fill` (expected or actual); empty means PASS."""
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
    rr = (target - fill) / (fill - stop)
    if rr < risk.min_reward_risk:
        why.append(f"reward:risk {rr:.2f} is under the {risk.min_reward_risk:g} minimum")
    cost = qty * fill
    if not inst.is_future and cost > equity * risk.max_position_pct / 100 * (1 + _TOL):
        why.append(f"position ${cost:.2f} is {cost / equity:.0%} of equity, over the "
                   f"{risk.max_position_pct:g}% cap")
    return why
