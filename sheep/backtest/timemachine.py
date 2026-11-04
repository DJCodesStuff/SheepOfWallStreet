"""Time Machine: replay a planned swing trade thousands of times from the symbol's own daily bars.

Adapted from trader-timemachine in day-trader-skill (MIT, Jake Schincariol,
github.com/Jakeschincariol/day-trader-skill), moved from 5-minute bars within one session to
daily bars over a holding period of `days` sessions.

- Pool: the last `lookback` daily bars, each as (open, high, low, close) / the previous close.
- One replay: start at the entry, draw `days` bars from the pool at random (with replacement)
  and scale each to the running close. Fills follow the backtester: a gap through a level fills
  at the open, a bar touching both counts as the stop, and exits pay sell slippage.
- History: every past day this symbol's setup fired under `rules`, traded with the same bracket
  (stop = close - atr_stop_mult x ATR, target at target_r_multiple R, entry at the next open)
  for up to `days` sessions. That is what really happened, not a simulation. The momentum rank
  across a watchlist is ignored, and back-to-back signal days each count.
- Verdict: of the replays that hit a side, how often the target came first, against the
  break-even share 1 / (1 + R) at the target's R.

A simulation, not a prediction: earnings, news and regime changes can do what the pool never did.
ponytail: days are drawn independently, so volatility clustering and trends are lost (a block
bootstrap would keep them); the stop is fixed, so the Trader's trailing stop and trend-break exit
aren't modelled.

    uv run python -m sheep.backtest.timemachine SPY [--entry 763.37 --stop 747.47 --target 792.31]
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from sheep.backtest.engine import DEFAULT_COSTS, Costs
from sheep.trader.strategy import Rules, signals

COIN_FLIP, STRONG, MANY_NEITHER = 0.05, 0.15, 0.40
BANDS = (0.05, 0.25, 0.5, 0.75, 0.95)
MIN_POOL = 60


def _outcomes(o, h, lo, c, entry: float, stop: float, target: float, costs: Costs):
    """Per path ((paths, days) price arrays): outcome 'target' | 'stop' | 'neither', and R."""
    hit = (lo <= stop) | (h >= target)
    first = hit.argmax(axis=1)
    rows = np.arange(len(o))
    fo, fl = o[rows, first], lo[rows, first]
    any_hit = hit.any(axis=1)
    is_stop = any_hit & (fl <= stop)
    is_target = any_hit & ~is_stop
    exit_px = np.where(is_stop, np.minimum(fo, stop),
                       np.where(is_target, np.maximum(fo, target), c[:, -1]))
    r = (costs.sell(exit_px) - entry) / (entry - stop)
    outcome = np.where(is_stop, "stop", np.where(is_target, "target", "neither"))
    return outcome, r


def simulate(bars: pd.DataFrame, entry: float, stop: float, target: float, days: int = 20,
             replays: int = 10_000, seed: int = 19, lookback: int = 504,
             costs: Costs = DEFAULT_COSTS) -> dict:
    """Odds for a long bracket over the next `days` sessions. Same inputs + seed, same numbers."""
    if not 0 < stop < entry < target:
        raise ValueError("a long plan needs stop < entry < target")
    if not (1 <= days <= 250 and 1 <= replays <= 100_000):
        raise ValueError("days must be 1-250 and replays 1-100,000")
    recent = bars.iloc[-(lookback + 1):]
    pool = recent[["open", "high", "low", "close"]].div(recent["close"].shift(), axis=0).dropna()
    if len(pool) < MIN_POOL:
        raise ValueError(f"only {len(pool)} usable daily bars; need {MIN_POOL}")
    draw = pool.to_numpy()[np.random.default_rng(seed).integers(0, len(pool), (replays, days))]
    c = entry * np.cumprod(draw[..., 3], axis=1)
    prev = np.hstack([np.full((replays, 1), entry), c[:, :-1]])
    outcome, r = _outcomes(prev * draw[..., 0], prev * draw[..., 1], prev * draw[..., 2], c,
                           entry, stop, target, costs)
    final = c[:, -1]
    path = np.hstack([np.full((replays, 1), entry), c])
    return {
        "replays": replays, "seed": seed, "days": days, "pool_days": len(pool),
        "pool_from": pool.index[0].date(), "pool_to": pool.index[-1].date(),
        "r_at_target": (target - entry) / (entry - stop),
        "p_target": float(np.mean(outcome == "target")),
        "p_stop": float(np.mean(outcome == "stop")),
        "p_neither": float(np.mean(outcome == "neither")),
        "p_green": float(np.mean((outcome == "target")
                                 | ((outcome == "neither") & (final > entry)))),
        "avg_r": float(r.mean()),
        "final": {q: float(np.quantile(final, q)) for q in (0.05, 0.5, 0.95)},
        "bands": pd.DataFrame(np.quantile(path, BANDS, axis=0).T,
                              columns=[f"p{int(q * 100)}" for q in BANDS]),
    }


def history(bars: pd.DataFrame, rules: Rules, days: int = 20,
            costs: Costs = DEFAULT_COSTS) -> dict:
    """The rule's real record on this symbol: every setup day with `days` sessions after it."""
    sig = signals(bars, rules)
    o, h, lo, c = (bars[k].to_numpy() for k in ("open", "high", "low", "close"))
    fired = np.flatnonzero((sig["breakout"] | sig["pullback"]).to_numpy())
    out, rs = {"target": 0, "stop": 0, "neither": 0}, []
    for j in fired[fired + days < len(bars)]:
        stop = c[j] - rules.atr_stop_mult * sig["atr"].iat[j]
        target = c[j] + rules.target_r_multiple * (c[j] - stop)
        entry = costs.buy(o[j + 1])
        if not 0 < stop < entry < target:  # gapped past a level: void, as in the backtester
            continue
        w = slice(j + 1, j + 1 + days)
        outcome, r = _outcomes(o[None, w], h[None, w], lo[None, w], c[None, w], entry, stop,
                               target, costs)
        out[str(outcome[0])] += 1
        rs.append(float(r[0]))
    return out | {"fired": len(rs), "avg_r": float(np.mean(rs)) if rs else None}


def _lean(mc: dict) -> tuple[float, float, float] | None:
    """(gap, share, break_even): the target's share of replays that hit a side, minus break-even."""
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    share, even = pt / (pt + ps), 1 / (1 + mc["r_at_target"])
    return share - even, share, even


def verdict(mc: dict) -> list[str]:
    """The verdict line, then any notes that keep it from reading one-sided."""
    lv, by = _lean(mc), f"day {mc['days']}"
    if lv is None:
        return [f"No replay reached the target or the stop by {by}: the price then decides "
                "every one."]
    d, share, even = lv
    r = mc["r_at_target"]
    one_r = abs(r - 1) < 0.005
    if abs(d) < COIN_FLIP:
        head = "Close to a coin flip" if one_r else "Close to break-even"
    elif abs(d) < STRONG:
        head = "Leans toward the " + ("target" if d > 0 else "stop")
    else:
        head = "Strongly toward the " + ("target" if d > 0 else "stop")
    odds = f"target first {mc['p_target']:.1%} vs stop first {mc['p_stop']:.1%}"
    lines = [f"{head}: {odds}." if one_r else
             f"{head}: at {r:.2g}R the target has to come first in {even:.0%} of the replays "
             f"that hit a side; it did in {share:.0%} ({odds})."]
    if mc["p_neither"] > MANY_NEITHER:
        lines.append(f"{mc['p_neither']:.1%} of the replays hit neither by {by}; the price then "
                     "decides those.")
    if abs(d) >= COIN_FLIP and mc["avg_r"] != 0 and (mc["avg_r"] > 0) != (d > 0):
        lines.append(f"Average R is {mc['avg_r']:+.3f} all the same: the replays that hit "
                     "neither pull it the other way.")
    return lines


def plan_from_rules(bars: pd.DataFrame, rules: Rules) -> tuple[float, float, float]:
    """(entry, stop, target) the rules would set from the latest close."""
    last = signals(bars, rules).iloc[-1]
    stop = last["close"] - rules.atr_stop_mult * last["atr"]
    return float(last["close"]), float(stop), float(last["close"] + rules.target_r_multiple
                                                    * (last["close"] - stop))


if __name__ == "__main__":
    import argparse

    from sheep.common.config import load_strategies
    from sheep.trader.data import load_bars

    ap = argparse.ArgumentParser(description="Replay a long swing trade from its own daily bars.")
    ap.add_argument("symbol")
    ap.add_argument("--entry", type=float)
    ap.add_argument("--stop", type=float)
    ap.add_argument("--target", type=float)
    ap.add_argument("--version", default="v1", help="rule version for the default plan/history")
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--replays", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=19)
    a = ap.parse_args()
    sym = a.symbol.upper()
    bars, rules = load_bars(sym), Rules(**load_strategies()[a.version])
    entry, stop, target = plan_from_rules(bars, rules)
    entry, stop, target = a.entry or entry, a.stop or stop, a.target or target
    mc = simulate(bars, entry, stop, target, a.days, a.replays, a.seed)
    hist = history(bars, rules, a.days)
    f = mc["final"]
    print(f"{sym}: entry {entry:.2f}, stop {stop:.2f}, target {target:.2f} "
          f"({mc['r_at_target']:.2f}R). {mc['replays']:,} replays of {mc['days']} sessions "
          f"from {mc['pool_days']} daily bars ({mc['pool_from']} to {mc['pool_to']}), "
          f"seed {mc['seed']}.")
    for k in ("target", "stop", "neither", "green"):
        print(f"  {k:8} {mc['p_' + k]:6.1%}")
    print(f"  Price on day {mc['days']}: median {f[0.5]:.2f}, 90% between {f[0.05]:.2f} and "
          f"{f[0.95]:.2f}. Average R {mc['avg_r']:+.3f}.")
    print(f"History ({a.version} setups on {sym}): fired {hist['fired']} times: "
          f"{hist['target']} target, {hist['stop']} stop, {hist['neither']} neither"
          + (f". Average R {hist['avg_r']:+.3f}." if hist["fired"] else "."))
    print(*verdict(mc), sep="\n")
