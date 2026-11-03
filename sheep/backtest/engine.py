"""Daily event-driven backtester and walk-forward runner.

Mirrors live behaviour: signals at day t's close (strategy.pick), orders fill at t+1's open.
- Costs: slippage (bps, always against you) on every fill, plus a flat fee per order.
- Exits: a gap through stop/target fills at the open. Otherwise, if a bar touches both stop and
  target, the stop is assumed to hit first (conservative). Trend break exits at the next open.
- Sizing: risk `risk_per_trade_pct` of equity between fill and stop, capped by cash (no leverage).

    uv run python -m sheep.backtest.engine main_paper [--versions v1 v2_candidate] [--holdout]
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from sheep.common.config import Risk, instrument
from sheep.trader.strategy import Rules, pick, signals, trail_stop


@dataclass(frozen=True)
class Costs:
    slippage_bps: float = 5.0
    fee_per_order: float = 0.0  # Alpaca is commission-free; set this for other brokers

    def buy(self, px: float) -> float:
        return px * (1 + self.slippage_bps / 1e4)

    def sell(self, px: float) -> float:
        return px * (1 - self.slippage_bps / 1e4)


DEFAULT_COSTS = Costs()


@dataclass
class Result:
    equity: pd.Series  # end-of-day equity
    trades: pd.DataFrame
    capital: float


@dataclass
class _Pos:
    symbol: str
    setup: str
    entry_date: pd.Timestamp
    entry_price: float
    qty: float
    initial_stop: float
    stop: float
    target: float
    reasoning: dict = field(repr=False)
    costs: float = 0.0  # entry fee + roll costs so far
    last_roll: date | None = None  # entry or latest roll date


def run(
    bars: dict[str, pd.DataFrame],
    rules: Rules,
    risk: Risk,
    capital: float = 100.0,
    costs: Costs = DEFAULT_COSTS,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> Result:
    """Trade days in [start, end). Bars before `start` are used only to warm up indicators.

    Futures (see config/instruments.yaml): whole contracts, sized within free margin, P&L times
    the multiplier, no cash paid for the notional, and positions held over a roll date are
    closed and reopened at the next open (paying slippage and fees twice).
    """
    sigs = {s: signals(df, rules).join(df[["open", "high", "low"]]) for s, df in bars.items()}
    inst = {s: instrument(s) for s in bars}
    days = sorted(set().union(*(sig.index for sig in sigs.values())))
    days = [d for d in days if (start is None or d >= start) and (end is None or d < end)]

    cash, prev_eq = capital, capital
    positions: dict[str, _Pos] = {}
    entries: list = []  # candidates from yesterday's close
    exits: set[str] = set()  # trend breaks from yesterday's close
    trades: list[dict] = []
    equity: dict[pd.Timestamp, float] = {}
    last_close: dict[str, float] = {}
    week, week_start_eq = None, capital
    prev_day: pd.Timestamp | None = None

    def fee(sym: str, qty: float) -> float:
        return costs.fee_per_order + inst[sym].fee * qty

    def value(p: _Pos, px: float) -> float:
        """What the position adds to equity at price px (futures: open P&L only)."""
        i = inst[p.symbol]
        return p.qty * i.multiplier * (px - p.entry_price) if i.is_future else p.qty * px

    def close(sym: str, d: pd.Timestamp, px: float, reason: str) -> None:
        nonlocal cash
        p = positions.pop(sym)
        mult = inst[sym].multiplier
        fill, f = costs.sell(px), fee(sym, p.qty)
        cash += value(p, fill) - f
        pnl = p.qty * mult * (fill - p.entry_price) - p.costs - f
        trades.append({
            "symbol": sym, "setup": p.setup, "entry_date": p.entry_date, "exit_date": d,
            "entry_price": p.entry_price, "exit_price": fill, "qty": p.qty,
            "initial_stop": p.initial_stop, "stop": p.stop, "target": p.target, "pnl": pnl,
            "r_multiple": pnl / (p.qty * mult * (p.entry_price - p.initial_stop)),
            "reason": reason, "reasoning": p.reasoning,
        })

    for d in days:
        today = {s: sig.loc[d] for s, sig in sigs.items() if d in sig.index}
        if (wk := d.isocalendar()[:2]) != week:
            week, week_start_eq = wk, prev_eq

        # 1. at the open: queued exits, futures rolls, then queued entries
        for s in [s for s in exits if s in today and s in positions]:
            close(s, d, today[s]["open"], "trend_break")
        exits &= positions.keys()
        for s, p in positions.items():
            i = inst[s]
            if i.roll_months and s in today and prev_day is not None \
                    and i.rolls_between(p.last_roll, prev_day.date()):
                # ponytail: no margin re-check on the roll; the Trader closes the trade instead
                px = today[s]["open"]
                cost = p.qty * i.multiplier * (costs.buy(px) - costs.sell(px)) + 2 * fee(s, p.qty)
                cash -= cost
                p.costs += cost
                p.last_roll = d.date()
        for c in entries:
            if c.symbol in positions or c.symbol not in today:
                continue
            if len(positions) >= risk.max_open_positions:
                break
            i = inst[c.symbol]
            fill = costs.buy(today[c.symbol]["open"])
            if not c.stop < fill < c.target:  # gapped past a level: setup is void
                continue
            qty = prev_eq * risk.risk_per_trade_pct / 100 / ((fill - c.stop) * i.multiplier)
            if i.is_future:
                in_use = sum(q.qty * inst[s].margin for s, q in positions.items()
                             if inst[s].is_future)
                qty = math.floor(min(qty, (prev_eq - in_use) / i.margin))
            else:
                qty = min(qty, (cash - costs.fee_per_order) / fill)
            if qty <= 0:
                continue
            f = fee(c.symbol, qty)
            cash -= f + (0 if i.is_future else qty * fill)
            positions[c.symbol] = _Pos(c.symbol, c.setup, d, fill, qty, c.stop, c.stop,
                                       c.target, c.reasoning, costs=f, last_roll=d.date())

        # 2. during the session: stops and targets
        for s, p in list(positions.items()):
            if s not in today:
                continue
            bar = today[s]
            if bar["open"] <= p.stop:
                close(s, d, bar["open"], "stop_gap")
            elif bar["open"] >= p.target:
                close(s, d, bar["open"], "target_gap")
            elif bar["low"] <= p.stop:
                close(s, d, p.stop, "stop")
            elif bar["high"] >= p.target:
                close(s, d, p.target, "target")

        # 3. at the close: trail stops, flag trend breaks, mark equity, scan for tomorrow
        for s, p in positions.items():
            if s in today:
                row = today[s]
                p.stop = trail_stop(p.stop, row["close"], row["atr"], rules.atr_stop_mult)
                if row["trend_break"]:
                    exits.add(s)
        last_close.update({s: row["close"] for s, row in today.items()})
        prev_eq = equity[d] = cash + sum(value(p, last_close[s]) for s, p in positions.items())
        weekly_limit_hit = prev_eq < week_start_eq * (1 - risk.weekly_loss_limit_pct / 100)
        entries = [] if weekly_limit_hit else pick(today, rules)
        prev_day = d

    if days:  # flatten at the end so every trade is closed and costed
        for s in list(positions):
            close(s, days[-1], last_close[s], "end")
        equity[days[-1]] = cash
    return Result(pd.Series(equity, dtype=float), pd.DataFrame(trades), capital)


def buy_hold(
    df: pd.DataFrame, capital: float, costs: Costs = DEFAULT_COSTS, start=None, end=None
) -> pd.Series:
    close = df["close"].loc[start:end]
    if end is not None:
        close = close[close.index < end]
    shares = (capital - costs.fee_per_order) / costs.buy(close.iloc[0])
    eq = shares * close
    eq.iloc[-1] = shares * costs.sell(close.iloc[-1]) - costs.fee_per_order
    return eq


def _curve_stats(eq: pd.Series, capital: float) -> dict:
    if eq.empty:
        return {"total_return": 0.0, "cagr": 0.0, "max_drawdown": 0.0, "sharpe": math.nan}
    rets = eq / eq.shift().fillna(capital) - 1
    peak = eq.cummax().clip(lower=capital)
    return {
        "total_return": eq.iloc[-1] / capital - 1,
        "cagr": (eq.iloc[-1] / capital) ** (252 / len(eq)) - 1,
        "max_drawdown": (eq / peak - 1).min(),
        "sharpe": rets.mean() / rets.std() * math.sqrt(252) if rets.std() > 0 else math.nan,
    }


def stats(res: Result, benchmark: pd.Series | None = None) -> dict:
    out = _curve_stats(res.equity, res.capital)
    t = res.trades
    pnl = t["pnl"] if len(t) else pd.Series(dtype=float)
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    out |= {
        "n_trades": len(t),
        "win_rate": len(wins) / len(t) if len(t) else math.nan,
        "avg_win": wins.mean() if len(wins) else math.nan,
        "avg_loss": losses.mean() if len(losses) else math.nan,
        "expectancy": pnl.mean() if len(t) else math.nan,
        "avg_r": t["r_multiple"].mean() if len(t) else math.nan,
    }
    if benchmark is not None:
        b = _curve_stats(benchmark, benchmark.iloc[0])
        out |= {f"bench_{k}": v for k, v in b.items()}
    return out


@dataclass
class Fold:
    train_start: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    version: str
    stats: dict


def holdout_cutoff(bars: dict[str, pd.DataFrame], holdout_years: int) -> pd.Timestamp:
    return max(df.index[-1] for df in bars.values()) - pd.DateOffset(years=holdout_years)


def walk_forward(
    bars: dict[str, pd.DataFrame],
    candidates: dict[str, Rules],
    risk: Risk,
    capital: float = 100.0,
    costs: Costs = DEFAULT_COSTS,
    train_years: int = 3,
    test_years: int = 1,
    holdout_years: int = 2,
) -> tuple[Result, list[Fold]]:
    """Roll train/test windows forward. Each fold picks the best candidate (by Sharpe) on its
    train window and trades it on the next, unseen test window. Test windows are stitched into
    one out-of-sample curve. The last `holdout_years` are cut off before anything runs.
    """
    cutoff = holdout_cutoff(bars, holdout_years)
    bars = {s: df.loc[: cutoff - pd.Timedelta(days=1)] for s, df in bars.items()}
    t = min(df.index[0] for df in bars.values()) + pd.DateOffset(years=1)  # indicator warm-up
    train, test = pd.DateOffset(years=train_years), pd.DateOffset(years=test_years)

    def score(res: Result) -> float:
        s = stats(res)["sharpe"]
        return -math.inf if math.isnan(s) else s

    eq_parts, trade_parts, folds, equity = [], [], [], capital
    while t + train + test <= cutoff:
        test_start, test_end = t + train, t + train + test
        version = next(iter(candidates)) if len(candidates) == 1 else max(
            candidates,
            key=lambda v: score(run(bars, candidates[v], risk, capital, costs, t, test_start)),
        )
        res = run(bars, candidates[version], risk, equity, costs, test_start, test_end)
        folds.append(Fold(t, test_start, test_end, version, stats(res)))
        eq_parts.append(res.equity)
        trade_parts.append(res.trades)
        if len(res.equity):
            equity = res.equity.iloc[-1]
        t += test
    if not eq_parts:
        raise ValueError("not enough history for one train+test fold before the holdout")
    return Result(pd.concat(eq_parts), pd.concat(trade_parts, ignore_index=True), capital), folds


def _print_stats(title: str, s: dict) -> None:
    print(f"\n{title}")
    for k, v in s.items():
        print(f"  {k:24} {v:10.4f}" if isinstance(v, float) else f"  {k:24} {v:10}")


if __name__ == "__main__":
    import argparse

    from sheep.common.config import load_profiles, load_strategies
    from sheep.trader.data import load_bars

    ap = argparse.ArgumentParser()
    ap.add_argument("profile")
    ap.add_argument("--versions", nargs="+", help="candidate rule versions (default: profile's)")
    ap.add_argument("--slippage-bps", type=float, default=5.0)
    ap.add_argument("--fee", type=float, default=0.0, help="flat fee per order")
    ap.add_argument("--holdout-years", type=int, default=2)
    ap.add_argument("--holdout", action="store_true",
                    help="ALSO run the profile's version on the untouched holdout. Do this rarely.")
    args = ap.parse_args()

    profile = load_profiles()[args.profile]
    strategies = load_strategies()
    costs = Costs(args.slippage_bps, args.fee)
    bars = {s: load_bars(s) for s in profile.watchlist}
    spy = load_bars("SPY")
    cands = {v: Rules(**strategies[v]) for v in args.versions or [profile.strategy_version]}

    res, folds = walk_forward(bars, cands, profile.risk, profile.starting_capital, costs,
                              holdout_years=args.holdout_years)
    print(f"{'test window':25} {'version':14} {'trades':>6} {'return':>8} {'max_dd':>8}")
    for f in folds:
        print(f"{f.test_start.date()} -> {f.test_end.date()}  {f.version:14} "
              f"{f.stats['n_trades']:6} {f.stats['total_return']:8.2%} "
              f"{f.stats['max_drawdown']:8.2%}")
    end = res.equity.index[-1] + pd.Timedelta(days=1)
    bench = buy_hold(spy, profile.starting_capital, costs, res.equity.index[0], end)
    _print_stats("walk-forward (out-of-sample) vs SPY buy-and-hold", stats(res, bench))

    if args.holdout:
        cutoff = holdout_cutoff(bars, args.holdout_years)
        rules = Rules(**strategies[profile.strategy_version])
        h = run(bars, rules, profile.risk, profile.starting_capital, costs, start=cutoff)
        hb = buy_hold(spy, profile.starting_capital, costs, h.equity.index[0])
        _print_stats(f"HOLDOUT from {cutoff.date()} ({profile.strategy_version})", stats(h, hb))
