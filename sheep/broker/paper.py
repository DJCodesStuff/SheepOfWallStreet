"""Built-in paper broker: simulated fills from daily bars, all state in the `orders` table.

Fill model (same as the backtester):
- An order becomes eligible on the first bar dated after the NY day it was placed, so an order
  placed after Monday's close can first fill at Tuesday's open.
- market: the open. stop/limit: the open if it gapped through the price, else the price itself if
  the bar's range touched it. Stops are checked before limits on the same bar (conservative).
- Slippage (bps, against you) on every fill, plus the flat fee per order.
- Buys that cost more than the cash on hand, and sells of more than is held, are rejected.

Cash and positions are derived from filled orders, so the orders table is the single source of
truth. Methods never commit: the caller owns the transaction.

AlpacaBroker (step 10) must expose the same public methods.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, time

import pandas as pd
import psycopg

from sheep.backtest.engine import DEFAULT_COSTS, Costs
from sheep.trader.data import NY, last_final_close, load_bars

_PRIORITY = {"market": 0, "stop": 1, "limit": 2}
_EPS = 1e-9


def _trigger(o: dict, bar: pd.Series) -> float | None:
    """Price this bar fills the order at, or None if it doesn't trigger."""
    if o["type"] == "market":
        return bar["open"]
    sell = o["side"] == "sell"
    if o["type"] == "stop":  # sell stop triggers on the way down, buy stop on the way up
        p = o["stop_price"]
        if sell:
            return bar["open"] if bar["open"] <= p else p if bar["low"] <= p else None
        return bar["open"] if bar["open"] >= p else p if bar["high"] >= p else None
    p = o["limit_price"]  # sell limit triggers on the way up, buy limit on the way down
    if sell:
        return bar["open"] if bar["open"] >= p else p if bar["high"] >= p else None
    return bar["open"] if bar["open"] <= p else p if bar["low"] <= p else None


class PaperBroker:
    def __init__(
        self,
        conn: psycopg.Connection,
        profile: str,
        costs: Costs = DEFAULT_COSTS,
        bars: Callable[[str], pd.DataFrame] = load_bars,
    ):
        row = conn.execute(
            "SELECT id, mode, broker, starting_capital FROM profiles WHERE name = %s", (profile,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown profile: {profile}")
        if row["mode"] != "paper" or row["broker"] != "paper":
            raise ValueError(f"profile {profile} is not a built-in paper profile")
        self.conn, self.profile_id, self.capital = conn, row["id"], row["starting_capital"]
        self.costs, self.bars = costs, bars

    # --- orders ---------------------------------------------------------------------------

    def submit(
        self,
        symbol: str,
        side: str,
        qty: float,
        type: str = "market",
        limit_price: float | None = None,
        stop_price: float | None = None,
        oco_group: str | None = None,
        at: datetime | None = None,
    ) -> int:
        if (type == "limit") != (limit_price is not None):
            raise ValueError("limit_price is required for, and only for, limit orders")
        if (type == "stop") != (stop_price is not None):
            raise ValueError("stop_price is required for, and only for, stop orders")
        return self.conn.execute(
            """INSERT INTO orders (profile_id, symbol, side, qty, type, limit_price, stop_price,
                                   oco_group, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now())) RETURNING id""",
            (self.profile_id, symbol.upper(), side, qty, type, limit_price, stop_price,
             oco_group, at),
        ).fetchone()["id"]

    def cancel(self, order_id: int) -> None:
        self.conn.execute(
            "UPDATE orders SET status = 'cancelled' "
            "WHERE id = %s AND profile_id = %s AND status = 'pending'",
            (order_id, self.profile_id),
        )

    def open_orders(self) -> list[dict]:
        return self.conn.execute(
            "SELECT * FROM orders WHERE profile_id = %s AND status = 'pending' ORDER BY id",
            (self.profile_id,),
        ).fetchall()

    def order(self, order_id: int) -> dict:
        return self.conn.execute(
            "SELECT * FROM orders WHERE id = %s AND profile_id = %s", (order_id, self.profile_id)
        ).fetchone()

    # --- account --------------------------------------------------------------------------

    def cash(self) -> float:
        flow = self.conn.execute(
            """SELECT COALESCE(SUM(CASE side WHEN 'buy' THEN -qty * fill_price
                                             ELSE qty * fill_price END - fees), 0) AS flow
               FROM orders WHERE profile_id = %s AND status = 'filled'""",
            (self.profile_id,),
        ).fetchone()["flow"]
        return self.capital + flow

    def positions(self) -> dict[str, float]:
        rows = self.conn.execute(
            """SELECT symbol, SUM(CASE side WHEN 'buy' THEN qty ELSE -qty END) AS qty
               FROM orders WHERE profile_id = %s AND status = 'filled' GROUP BY symbol""",
            (self.profile_id,),
        ).fetchall()
        return {r["symbol"]: r["qty"] for r in rows if abs(r["qty"]) > _EPS}

    def equity(self) -> float:
        """Cash plus positions marked at their latest final close."""
        return self.cash() + sum(
            q * self.bars(s)["close"].iloc[-1] for s, q in self.positions().items()
        )

    def snapshot(self, now: datetime | None = None) -> None:
        """Record the latest final session's cash and equity."""
        self.conn.execute(
            """INSERT INTO equity_snapshots (profile_id, date, cash, equity)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (profile_id, date)
               DO UPDATE SET cash = excluded.cash, equity = excluded.equity""",
            (self.profile_id, last_final_close(now or datetime.now(NY)).date(),
             self.cash(), self.equity()),
        )

    # --- simulation -----------------------------------------------------------------------

    def refresh(self) -> list[int]:
        """Fill or reject pending orders against every final bar since they were placed.

        Returns the ids of orders that filled.

        ponytail: processes one symbol at a time, so a cash check can see a later day's sell
        from another symbol before an earlier buy. Fine at a few orders a day; interleave all
        symbols by date if that ever matters.
        """
        by_symbol: dict[str, list[dict]] = defaultdict(list)
        for o in self.open_orders():
            o["placed"] = o["created_at"].astimezone(NY).date()
            by_symbol[o["symbol"]].append(o)

        filled = []
        for sym, orders in by_symbol.items():
            df = self.bars(sym)
            for d, bar in df[df.index.date > min(o["placed"] for o in orders)].iterrows():
                live = [o for o in orders if o["status"] == "pending" and d.date() > o["placed"]]
                for o in sorted(live, key=lambda o: _PRIORITY[o["type"]]):
                    if o["status"] != "pending":  # cancelled by an OCO sibling on this bar
                        continue
                    px = _trigger(o, bar)
                    if px is None or not self._fill(o, d, px):
                        continue
                    filled.append(o["id"])
                    if o["oco_group"]:
                        self.conn.execute(
                            "UPDATE orders SET status = 'cancelled' WHERE profile_id = %s "
                            "AND oco_group = %s AND status = 'pending'",
                            (self.profile_id, o["oco_group"]),
                        )
                        for sib in orders:
                            if sib["oco_group"] == o["oco_group"] and sib["status"] == "pending":
                                sib["status"] = "cancelled"
        return filled

    def _fill(self, o: dict, d: pd.Timestamp, px: float) -> bool:
        buy = o["side"] == "buy"
        fill = self.costs.buy(px) if buy else self.costs.sell(px)
        fee = self.costs.fee_per_order
        if (buy and o["qty"] * fill + fee > self.cash() + _EPS) or (
            not buy and o["qty"] > self.positions().get(o["symbol"], 0) + _EPS
        ):
            self._set_status(o, "rejected")
            return False
        self.conn.execute(
            "UPDATE orders SET status = 'filled', filled_at = %s, fill_price = %s, fees = %s "
            "WHERE id = %s",
            (datetime.combine(d.date(), time(16), NY), fill, fee, o["id"]),
        )
        o["status"] = "filled"
        return True

    def _set_status(self, o: dict, status: str) -> None:
        self.conn.execute("UPDATE orders SET status = %s WHERE id = %s", (status, o["id"]))
        o["status"] = status
