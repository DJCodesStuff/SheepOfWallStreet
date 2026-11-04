"""Daily after-close run, per profile:

1. refresh the broker (fill/reject pending orders against the new bar)
2. reconcile: entry fills open trades, exit fills close them (pnl, exit reason)
3. manage open trades: trend break -> exit at next open; futures held over a roll date ->
   roll (sell + buy) at the next open; else ratchet the trailing stop
4. scan for entries, size them, and pass each through the risk gate (sheep/trader/risk.py);
   each entry is a bracket: market buy + OCO stop/target sells
5. snapshot equity

Every entry order carries its intent (stop, target, rule version, signal reasoning), and every
trade stores it, so the Reviewer can judge process. Re-running on the same evening is a no-op.

    uv run --env-file .env python -m sheep.trader.run [profile ...]   # default: all paper profiles
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timedelta

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from sheep.backtest.engine import DEFAULT_COSTS, Costs
from sheep.broker.paper import PaperBroker
from sheep.common.config import Risk, instrument
from sheep.trader.data import BAR_FINAL_AT, NY, last_final_close, load_bars
from sheep.trader.risk import check_entry
from sheep.trader.strategy import Rules, scan, signals, trail_stop

_EXIT_REASON = {"stop": "stop", "limit": "target"}
_EPS = 1e-9


def run_profile(
    conn: psycopg.Connection,
    name: str,
    costs: Costs = DEFAULT_COSTS,
    bars_fn: Callable[[str], pd.DataFrame] = load_bars,
    now: datetime | None = None,
) -> list[str]:
    """Run one profile's daily cycle. Returns a human-readable log of decisions."""
    now = (now or datetime.now(NY)).astimezone(NY)
    if now.weekday() < 5 and now.time() < BAR_FINAL_AT:
        # Orders fill on the first bar after the day they're placed, so placing them before
        # today's bar is final would act a day late on yesterday's signal.
        return [f"skipped: run after {BAR_FINAL_AT:%H:%M} NY on weekdays"]
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
    _enter(conn, broker, p, rules, risk, {s: bars[s] for s in cfg["watchlist"]}, costs, now, log)
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


def _enter(conn, broker: PaperBroker, p, rules: Rules, risk: Risk, bars, costs: Costs,
           now: datetime, log: list[str]) -> None:
    pid = p["id"]
    pending = conn.execute(
        "SELECT * FROM orders WHERE profile_id = %s AND side = 'buy' AND status = 'pending' "
        "AND intent_json IS NOT NULL",
        (pid,),
    ).fetchall()
    open_syms = {r["symbol"] for r in conn.execute(
        "SELECT symbol FROM trades WHERE profile_id = %s AND exit_ts IS NULL", (pid,)
    ).fetchall()}
    busy = open_syms | {o["symbol"] for o in pending}
    slots = risk.max_open_positions - len(busy)

    equity = broker.equity()
    today = last_final_close(now).date()
    # Latest weekday on or before today. ponytail: no holiday calendar, so on a market holiday
    # every signal reads as stale and nothing is entered (the evening before already acted).
    expected = today - timedelta(days=max(0, today.weekday() - 4))
    monday = today - timedelta(days=today.weekday())
    row = conn.execute(
        "SELECT equity FROM equity_snapshots WHERE profile_id = %s AND date < %s "
        "ORDER BY date DESC LIMIT 1",
        (pid, monday),
    ).fetchone()
    week_start = row["equity"] if row else p["starting_capital"]
    if equity < week_start * (1 - risk.weekly_loss_limit_pct / 100):
        log.append(f"weekly loss limit hit (equity {equity:.2f} vs {week_start:.2f}): no entries")
        return

    def is_future(sym: str) -> bool:
        return instrument(sym).is_future

    cash = broker.cash() - sum(o["qty"] * costs.buy(o["intent_json"]["ref_price"])
                               for o in pending if not is_future(o["symbol"]))
    free_margin = equity - broker.margin_in_use() - sum(
        o["qty"] * instrument(o["symbol"]).margin for o in pending if is_future(o["symbol"]))
    for c in scan(bars, rules):
        if c.symbol in busy:
            continue
        if slots <= 0:
            log.append(f"skip {c.symbol} {c.setup}: max open positions")
            continue
        inst = instrument(c.symbol)
        est_fill = costs.buy(c.ref_price)
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
        why = check_entry(c.symbol, qty, est_fill, c.stop, c.target, equity, risk, inst)
        if c.date.date() < expected:  # the data feed missed a bar: don't act on old prices
            why.insert(0, f"stale signal from {c.date.date()}, expected {expected}")
        if why:
            log.append(f"BLOCK {c.symbol} {c.setup}: " + "; ".join(why))
            continue
        buy = broker.submit(c.symbol, "buy", qty, at=now)
        group = f"bracket-{buy}"
        conn.execute(
            "UPDATE orders SET intent_json = %s WHERE id = %s",
            (Jsonb({
                "stop": c.stop, "target": c.target, "ref_price": c.ref_price, "setup": c.setup,
                "signal_date": str(c.date.date()), "rule_version": p["strategy_version"],
                "bracket": group, "signal": c.reasoning,
            }), buy),
        )
        broker.submit(c.symbol, "sell", qty, "stop", stop_price=c.stop, oco_group=group, at=now)
        broker.submit(c.symbol, "sell", qty, "limit", limit_price=c.target, oco_group=group,
                      at=now)
        if inst.is_future:
            free_margin -= qty * inst.margin
        else:
            cash -= qty * est_fill + costs.fee_per_order
        slots -= 1
        log.append(f"entry {c.symbol} {c.setup}: buy {qty:.4f} ~{c.ref_price:.2f}, "
                   f"stop {c.stop:.2f}, target {c.target:.2f}")


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
