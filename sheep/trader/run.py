"""The Trader, per profile. It runs twice a weekday; which run it is depends on the NY time:

- evening (after 16:30, once the day's bar is final; any hour on weekends and NYSE holidays):
  1. refresh the broker (fill/reject pending orders against the new bar)
  2. reconcile: entry fills open trades, exit fills close them (pnl, exit reason)
  3. manage open trades: trend break -> exit at next open; futures held over a roll date ->
     roll (sell + buy) at the next open; else ratchet the trailing stop
  4. log tonight's signals, snapshot equity
- open (09:30-16:00 on trading days): steps 1-3 again (a no-op after a normal evening;
  catches up a missed one), then enter: re-scan the previous session's final bars, read
  today's opening price, size each entry at it (so the stop risks exactly
  risk_per_trade_pct even after a gap) and pass it through the risk
  gate (sheep/trader/risk.py), then, last, the backtest gate: the symbol's own backtest of that
  setup, trades closed before today, must show a positive average R. Each entry is a
  bracket: market buy + OCO stop/target sells, filled at today's open. This is what
  the backtester does, so the two agree.

Every entry order carries its intent (stop, target, rule version, signal reasoning), and every
trade stores it, so the Reviewer can judge process. Re-running either run is a no-op.

    uv run --env-file .env python -m sheep.trader.run [profile ...]   # default: all paper profiles
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timedelta

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from sheep.backtest.engine import DEFAULT_COSTS, Costs, symbol_history
from sheep.broker.paper import PaperBroker
from sheep.common.config import Risk, instrument
from sheep.trader.data import (
    BAR_FINAL_AT,
    MARKET_CLOSE,
    MARKET_OPEN,
    NY,
    is_session,
    last_final_close,
    load_bars,
    prev_session,
    today_open,
)
from sheep.trader.risk import check_entry, track_record
from sheep.trader.strategy import Rules, scan, signals, trail_stop

_EXIT_REASON = {"stop": "stop", "limit": "target"}
_EPS = 1e-9


def run_profile(
    conn: psycopg.Connection,
    name: str,
    costs: Costs = DEFAULT_COSTS,
    bars_fn: Callable[[str], pd.DataFrame] = load_bars,
    open_fn: Callable[[str, datetime], float | None] = today_open,
    now: datetime | None = None,
) -> list[str]:
    """Run one profile's open or evening run (see module doc). Returns a log of decisions."""
    now = (now or datetime.now(NY)).astimezone(NY)
    session = is_session(now.date())  # weekends and NYSE holidays: evening run at any hour
    at_open = session and MARKET_OPEN <= now.time() < MARKET_CLOSE
    if session and not at_open and now.time() < BAR_FINAL_AT:
        return [f"skipped: entries run {MARKET_OPEN:%H:%M}-{MARKET_CLOSE:%H:%M} NY on trading "
                f"days, fills and exits after {BAR_FINAL_AT:%H:%M}"]
    p = conn.execute("SELECT * FROM profiles WHERE name = %s", (name,)).fetchone()
    if p is None:
        raise ValueError(f"unknown profile: {name}")
    # One run per profile at a time (cron + dashboard button): held until the transaction ends.
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (p["id"],))
    cfg = p["config_json"]
    risk = Risk(**cfg["risk"])
    params = conn.execute(
        "SELECT params_json FROM rule_versions WHERE version = %s", (p["strategy_version"],)
    ).fetchone()["params_json"]
    rules = Rules(**params)
    broker = PaperBroker(conn, name, costs, bars_fn)  # raises for non-paper profiles
    log: list[str] = []

    broker.refresh()
    _reconcile(conn, p["id"], log)
    symbols = set(cfg["watchlist"]) | set(broker.positions())
    bars = {s: bars_fn(s) for s in symbols}
    _manage(conn, broker, p["id"], rules, bars, now, log)
    watch = {s: bars[s] for s in cfg["watchlist"]}
    if at_open:
        _enter(conn, broker, p, rules, risk, bars, watch, costs, open_fn, now, log)
    else:
        log += [f"signal {c.symbol} {c.setup} at {c.ref_price:.2f} (stop {c.stop:.2f}): sized "
                "and checked at the next open" for c in scan(watch, rules)]
    broker.snapshot(now)
    return log


def _reconcile(conn: psycopg.Connection, pid: int, log: list[str]) -> None:
    # Entry fills -> open a trade and attach its bracket exits to it.
    for o in conn.execute(
        "SELECT * FROM orders WHERE profile_id = %s AND side = 'buy' AND status = 'filled' "
        "AND intent_json IS NOT NULL AND trade_id IS NULL ORDER BY id",
        (pid,),
    ).fetchall():
        it = o["intent_json"]
        tid = conn.execute(
            """INSERT INTO trades (profile_id, symbol, entry_ts, entry_price, qty, stop, target,
                                   rule_version, reasoning_json)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (pid, o["symbol"], o["filled_at"], o["fill_price"], o["qty"], it["stop"],
             it["target"], it["rule_version"], Jsonb(it | {"entry_order_id": o["id"]})),
        ).fetchone()["id"]
        conn.execute(
            "UPDATE orders SET trade_id = %s WHERE profile_id = %s AND (id = %s OR oco_group = %s)",
            (tid, pid, o["id"], it["bracket"]),
        )
        log.append(f"opened trade {tid}: {o['qty']:.4f} {o['symbol']} @ {o['fill_price']:.2f}")

    # Entry rejected/cancelled -> its bracket exits are meaningless.
    conn.execute(
        """UPDATE orders SET status = 'cancelled' WHERE profile_id = %s AND status = 'pending'
           AND oco_group IN (SELECT intent_json->>'bracket' FROM orders WHERE profile_id = %s
                             AND side = 'buy' AND status IN ('rejected', 'cancelled'))""",
        (pid, pid),
    )

    # A trade whose position is back to zero is closed. Its P&L is the cash flow of every fill
    # (entry, rolls, exit), so roll costs and fees are included.
    for t in conn.execute(
        "SELECT * FROM trades WHERE profile_id = %s AND exit_ts IS NULL ORDER BY id", (pid,)
    ).fetchall():
        fills = conn.execute(
            "SELECT * FROM orders WHERE trade_id = %s AND status = 'filled' "
            "ORDER BY filled_at, id", (t["id"],),
        ).fetchall()
        if sum(o["qty"] if o["side"] == "buy" else -o["qty"] for o in fills) > _EPS:
            continue
        mult = instrument(t["symbol"]).multiplier
        pnl = sum((o["qty"] if o["side"] == "sell" else -o["qty"]) * o["fill_price"] * mult
                  - o["fees"] for o in fills)
        last = fills[-1]
        intent = last["intent_json"] or {}
        reason = ("roll_failed" if intent.get("roll")
                  else _EXIT_REASON.get(last["type"]) or intent.get("exit_reason", "manual"))
        conn.execute("UPDATE orders SET status = 'cancelled' WHERE trade_id = %s "
                     "AND status = 'pending'", (t["id"],))
        conn.execute(
            "UPDATE trades SET exit_ts = %s, exit_price = %s, pnl = %s, exit_reason = %s "
            "WHERE id = %s",
            (last["filled_at"], last["fill_price"], pnl, reason, t["id"]),
        )
        log.append(f"closed trade {t['id']} ({reason}): pnl {pnl:+.2f}")


def _manage(conn, broker: PaperBroker, pid: int, rules: Rules, bars, now, log) -> None:
    for t in conn.execute(
        "SELECT * FROM trades WHERE profile_id = %s AND exit_ts IS NULL ORDER BY id", (pid,)
    ).fetchall():
        exits = conn.execute(
            "SELECT * FROM orders WHERE trade_id = %s AND side = 'sell' AND status = 'pending'",
            (t["id"],),
        ).fetchall()
        if any(o["type"] == "market" for o in exits):
            continue  # exit already queued for the next open
        sig = signals(bars[t["symbol"]], rules).iloc[-1]
        group = t["reasoning_json"]["bracket"]

        if sig["trend_break"]:
            for o in exits:
                broker.cancel(o["id"])
            oid = broker.submit(t["symbol"], "sell", t["qty"], at=now)
            conn.execute(
                "UPDATE orders SET trade_id = %s, intent_json = %s WHERE id = %s",
                (t["id"], Jsonb({"exit_reason": "trend_break"}), oid),
            )
            log.append(f"trade {t['id']} {t['symbol']}: trend break, exit at next open")
            continue

        inst = instrument(t["symbol"])
        if inst.roll_months:
            last_roll = conn.execute(  # entry fill or the latest roll's buy
                "SELECT max(filled_at) AS at FROM orders WHERE trade_id = %s AND side = 'buy' "
                "AND status = 'filled'", (t["id"],),
            ).fetchone()["at"].astimezone(NY).date()
            if inst.rolls_between(last_roll, last_final_close(now).date()):
                for side in ("sell", "buy"):  # same bar, sell first: close then reopen
                    oid = broker.submit(t["symbol"], side, t["qty"], at=now)
                    conn.execute(
                        "UPDATE orders SET trade_id = %s, intent_json = %s WHERE id = %s",
                        (t["id"], Jsonb({"roll": True}), oid),
                    )
                log.append(f"trade {t['id']} {t['symbol']}: roll to next contract at next open")

        stop_orders = [o for o in exits if o["type"] == "stop"]
        current = stop_orders[0]["stop_price"] if stop_orders else t["stop"]
        new = trail_stop(current, sig["close"], sig["atr"], rules.atr_stop_mult)
        if stop_orders and new <= current:
            continue
        for o in stop_orders:
            broker.cancel(o["id"])
        oid = broker.submit(t["symbol"], "sell", t["qty"], "stop", stop_price=new,
                            oco_group=group, at=now)
        conn.execute("UPDATE orders SET trade_id = %s WHERE id = %s", (t["id"], oid))
        log.append(f"trade {t['id']} {t['symbol']}: stop {current:.2f} -> {new:.2f}")


def _enter(conn, broker: PaperBroker, p, rules: Rules, risk: Risk, bars, watch, costs: Costs,
           open_fn, now: datetime, log: list[str]) -> None:
    pid = p["id"]
    pending = conn.execute(
        "SELECT * FROM orders WHERE profile_id = %s AND side = 'buy' AND status = 'pending' "
        "AND intent_json IS NOT NULL",
        (pid,),
    ).fetchall()
    open_trades = conn.execute(
        "SELECT * FROM trades WHERE profile_id = %s AND exit_ts IS NULL", (pid,)
    ).fetchall()
    busy = {t["symbol"] for t in open_trades} | {o["symbol"] for o in pending}
    slots = risk.max_open_positions - len(busy)

    equity = broker.equity()  # positions marked at yesterday's close
    today = now.date()
    expected = prev_session(today)  # signals come from the previous trading day's close
    monday = today - timedelta(days=today.weekday())
    row = conn.execute(
        "SELECT equity FROM equity_snapshots WHERE profile_id = %s AND date < %s "
        "ORDER BY date DESC LIMIT 1",
        (pid, monday),
    ).fetchone()
    week_start = row["equity"] if row else p["starting_capital"]
    floor = week_start * (1 - risk.weekly_loss_limit_pct / 100)
    if equity < floor:
        log.append(f"weekly loss limit hit (equity {equity:.2f} vs {week_start:.2f}): no entries")
        return

    def is_future(sym: str) -> bool:
        return instrument(sym).is_future

    entries = [o for o in pending if "bracket" in o["intent_json"]]  # not futures rolls

    def entry_px(o) -> float:  # an unfilled entry's expected fill
        return costs.buy(o["intent_json"].get("open", o["intent_json"]["ref_price"]))

    # Loss still allowed this week if every stop in force (the latest one per trade) fills
    stops = {r["trade_id"]: r["stop_price"] for r in conn.execute(
        "SELECT DISTINCT ON (trade_id) trade_id, stop_price FROM orders WHERE profile_id = %s "
        "AND type = 'stop' AND trade_id IS NOT NULL ORDER BY trade_id, id DESC", (pid,),
    ).fetchall()}
    room = equity - floor - sum(
        max(0.0, bars[t["symbol"]]["close"].iloc[-1] - stops.get(t["id"], t["stop"]))
        * t["qty"] * instrument(t["symbol"]).multiplier for t in open_trades
    ) - sum(max(0.0, entry_px(o) - o["intent_json"]["stop"]) * o["qty"]
            * instrument(o["symbol"]).multiplier for o in entries)

    cash = broker.cash() - sum(o["qty"] * entry_px(o) for o in entries
                               if not is_future(o["symbol"]))
    free_margin = equity - broker.margin_in_use() - sum(
        o["qty"] * instrument(o["symbol"]).margin for o in pending if is_future(o["symbol"]))
    for c in scan(watch, rules):
        if c.symbol in busy:
            continue
        if slots <= 0:
            log.append(f"skip {c.symbol} {c.setup}: max open positions")
            continue
        if c.date.date() < expected:  # the data feed missed a bar: don't act on old prices
            log.append(f"BLOCK {c.symbol} {c.setup}: stale signal from {c.date.date()}, "
                       f"expected {expected}")
            continue
        try:
            px = open_fn(c.symbol, now)
        except Exception as e:  # noqa: BLE001 - one symbol's feed failing skips only it
            px, err = None, f" ({e})"
        else:
            err = ""
        if px is None:
            log.append(f"skip {c.symbol} {c.setup}: no opening price for {today} yet{err}")
            continue
        inst = instrument(c.symbol)
        est_fill = costs.buy(px)
        if not c.stop < est_fill < c.target:
            log.append(f"skip {c.symbol} {c.setup}: opened at {px:.2f}, past the stop "
                       f"{c.stop:.2f} or target {c.target:.2f}; the setup is void")
            continue
        budget = equity * risk.risk_per_trade_pct / 100
        risk_per_unit = (est_fill - c.stop) * inst.multiplier
        if inst.is_future:
            qty = math.floor(min(budget / risk_per_unit, free_margin / inst.margin))
            if qty < 1:
                log.append(f"skip {c.symbol} {c.setup}: 1 contract risks ${risk_per_unit:,.0f} "
                           f"and needs ${inst.margin:,.0f} margin; budget ${budget:,.2f} risk, "
                           f"${free_margin:,.2f} free margin")
                continue
        else:
            qty = min(budget / risk_per_unit, (cash - costs.fee_per_order) / est_fill)
            if qty <= 0:
                log.append(f"skip {c.symbol} {c.setup}: no cash")
                continue
        why = check_entry(c.symbol, qty, est_fill, c.stop, c.target, equity, risk, inst, room)
        if why:
            log.append(f"BLOCK {c.symbol} {c.setup}: " + "; ".join(why))
            continue
        record = None
        if risk.backtest_min_trades:  # final check: the same backtest the backtester gates on
            block, record = track_record(
                symbol_history(c.symbol, bars[c.symbol], rules, risk, costs), c.setup,
                pd.Timestamp(today), risk.backtest_min_trades)
            if block:
                log.append(f"BLOCK {c.symbol} {c.setup}: {block}")
                continue
        buy = broker.submit(c.symbol, "buy", qty, at=now)
        group = f"bracket-{buy}"
        conn.execute(
            "UPDATE orders SET intent_json = %s WHERE id = %s",
            (Jsonb({
                "stop": c.stop, "target": c.target, "ref_price": c.ref_price, "open": px,
                "setup": c.setup, "signal_date": str(c.date.date()),
                "rule_version": p["strategy_version"], "bracket": group, "signal": c.reasoning,
                "backtest": record,
            }), buy),
        )
        broker.submit(c.symbol, "sell", qty, "stop", stop_price=c.stop, oco_group=group, at=now)
        broker.submit(c.symbol, "sell", qty, "limit", limit_price=c.target, oco_group=group,
                      at=now)
        if inst.is_future:
            free_margin -= qty * inst.margin
        else:
            cash -= qty * est_fill + costs.fee_per_order
        room -= qty * risk_per_unit
        slots -= 1
        log.append(f"entry {c.symbol} {c.setup}: buy {qty:.4f} at the {px:.2f} open "
                   f"(signal close {c.ref_price:.2f}), stop {c.stop:.2f}, target {c.target:.2f}")


if __name__ == "__main__":
    import sys

    from sheep.common.db import connect

    failed = []
    with connect() as conn:
        names = sys.argv[1:] or [r["name"] for r in conn.execute(
            "SELECT name FROM profiles WHERE broker = 'paper' ORDER BY id"
        ).fetchall()]
        for name in names:
            try:
                with conn.transaction():  # one profile failing doesn't block the others
                    lines = run_profile(conn, name)
                print(f"[{name}]", *lines or ["no action"], sep="\n  ")
            except Exception as e:  # noqa: BLE001 - report and keep going
                print(f"[{name}] FAILED: {e}")
                failed.append(name)
    sys.exit(1 if failed else 0)  # non-zero so the scheduler flags the run
