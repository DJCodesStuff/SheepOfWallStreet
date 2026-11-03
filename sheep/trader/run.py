"""Daily after-close run, per profile:

1. refresh the broker (fill/reject pending orders against the new bar)
2. reconcile: entry fills open trades, exit fills close them (pnl, exit reason)
3. manage open trades: trend break -> exit at next open, else ratchet the trailing stop
4. scan for entries and size them; each entry is a bracket: market buy + OCO stop/target sells
5. snapshot equity

Every entry order carries its intent (stop, target, rule version, signal reasoning), and every
trade stores it, so the Reviewer can judge process. Re-running on the same evening is a no-op.

    uv run --env-file .env python -m sheep.trader.run [profile ...]   # default: all paper profiles
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from sheep.backtest.engine import DEFAULT_COSTS, Costs
from sheep.broker.paper import PaperBroker
from sheep.common.config import Risk
from sheep.trader.data import BAR_FINAL_AT, NY, last_final_close, load_bars
from sheep.trader.strategy import Rules, scan, signals, trail_stop

_EXIT_REASON = {"stop": "stop", "limit": "target"}


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

    # Exit fills -> close the trade.
    for o in conn.execute(
        """SELECT o.*, t.entry_price, e.fees AS entry_fees FROM orders o
           JOIN trades t ON t.id = o.trade_id
           JOIN orders e ON e.trade_id = t.id AND e.side = 'buy'
           WHERE o.profile_id = %s AND o.side = 'sell' AND o.status = 'filled'
             AND t.exit_ts IS NULL""",
        (pid,),
    ).fetchall():
        intent = o["intent_json"] or {}
        reason = _EXIT_REASON.get(o["type"]) or intent.get("exit_reason", "manual")
        pnl = o["qty"] * (o["fill_price"] - o["entry_price"]) - o["fees"] - o["entry_fees"]
        conn.execute(
            "UPDATE trades SET exit_ts = %s, exit_price = %s, pnl = %s, exit_reason = %s "
            "WHERE id = %s",
            (o["filled_at"], o["fill_price"], pnl, reason, o["trade_id"]),
        )
        log.append(f"closed trade {o['trade_id']} ({reason}): pnl {pnl:+.2f}")


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

    committed = sum(o["qty"] * costs.buy(o["intent_json"]["ref_price"]) for o in pending)
    cash = broker.cash() - committed
    for c in scan(bars, rules):
        if c.symbol in busy:
            continue
        if slots <= 0:
            log.append(f"skip {c.symbol} {c.setup}: max open positions")
            continue
        est_fill = costs.buy(c.ref_price)
        qty = min(
            equity * risk.risk_per_trade_pct / 100 / (est_fill - c.stop),
            (cash - costs.fee_per_order) / est_fill,
        )
        if qty <= 0:
            log.append(f"skip {c.symbol} {c.setup}: no cash")
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
        cash -= qty * est_fill + costs.fee_per_order
        slots -= 1
        log.append(f"entry {c.symbol} {c.setup}: buy {qty:.4f} ~{c.ref_price:.2f}, "
                   f"stop {c.stop:.2f}, target {c.target:.2f}")


if __name__ == "__main__":
    import sys

    from sheep.common.db import connect

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
