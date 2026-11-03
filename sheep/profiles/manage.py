"""Profile and rule-version management: show, reset (paper only), clone (always into a
paper profile), create rule versions, switch a paper profile's rule version.

    python -m sheep.profiles.manage show main_paper
    python -m sheep.profiles.manage clone main_paper trial --version v2_candidate
    python -m sheep.profiles.manage reset trial --yes

Run with `uv run --env-file .env`. Functions never commit: the caller owns the transaction.
"""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb

from sheep.common.config import NAME_RE
from sheep.trader.strategy import validate_rules


def _profile(conn: psycopg.Connection, name: str) -> dict:
    row = conn.execute("SELECT * FROM profiles WHERE name = %s", (name,)).fetchone()
    if row is None:
        raise ValueError(f"unknown profile: {name}")
    return row


def reset(conn: psycopg.Connection, name: str) -> None:
    """Wipe a paper profile's orders, trades, tags and snapshots. Live history is never wiped."""
    p = _profile(conn, name)
    if p["mode"] != "paper":
        raise ValueError(f"refusing to reset {p['mode']} profile {name}")
    pid = p["id"]
    conn.execute(
        "DELETE FROM trade_tags WHERE trade_id IN (SELECT id FROM trades WHERE profile_id = %s)",
        (pid,),
    )
    for table in ("orders", "trades", "equity_snapshots"):  # orders reference trades
        conn.execute(f"DELETE FROM {table} WHERE profile_id = %s", (pid,))


def clone(
    conn: psycopg.Connection, src: str, new_name: str, strategy_version: str | None = None
) -> None:
    """Copy a profile's config into a new, empty paper profile (never a live one).

    The clone lives only in the DB; add it to config/profiles.yaml to keep it in config too.
    """
    if not NAME_RE.fullmatch(new_name):
        raise ValueError("name must be 1-64 letters, digits, _ or -")
    p = _profile(conn, src)
    version = strategy_version or p["strategy_version"]
    cfg = p["config_json"] | {
        "name": new_name, "mode": "paper", "broker": "paper", "strategy_version": version,
    }
    conn.execute(
        """INSERT INTO profiles
               (name, mode, broker, starting_capital, currency, strategy_version, config_json)
           VALUES (%s, 'paper', 'paper', %s, %s, %s, %s)""",
        (new_name, p["starting_capital"], p["currency"], version, Jsonb(cfg)),
    )


def create_rule_version(
    conn: psycopg.Connection, version: str, params: dict, notes: str | None = None,
    approved_by: str = "dashboard",
) -> None:
    """Save a new, immutable rule version. Existing versions are never edited."""
    if not NAME_RE.fullmatch(version):
        raise ValueError("version name must be 1-64 letters, digits, _ or -")
    validate_rules(params)
    if conn.execute("SELECT 1 FROM rule_versions WHERE version = %s", (version,)).fetchone():
        raise ValueError(f"rule version {version} already exists; versions can't be changed")
    conn.execute(
        "INSERT INTO rule_versions (version, params_json, approved_by, notes) "
        "VALUES (%s, %s, %s, %s)",
        (version, Jsonb(params), approved_by, notes),
    )


def set_rule_version(conn: psycopg.Connection, name: str, version: str) -> None:
    """Switch a paper profile to another rule version. Live profiles change only in config."""
    p = _profile(conn, name)
    if p["mode"] != "paper":
        raise ValueError(f"{name} is live; change its rules in config/profiles.yaml")
    if not conn.execute("SELECT 1 FROM rule_versions WHERE version = %s", (version,)).fetchone():
        raise ValueError(f"unknown rule version: {version}")
    conn.execute(
        "UPDATE profiles SET strategy_version = %s, "
        "config_json = config_json || jsonb_build_object('strategy_version', %s::text) "
        "WHERE id = %s",
        (version, version, p["id"]),
    )


if __name__ == "__main__":
    import argparse

    from sheep.broker.paper import PaperBroker
    from sheep.common.db import connect

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show").add_argument("name")
    r = sub.add_parser("reset")
    r.add_argument("name")
    r.add_argument("--yes", action="store_true", help="confirm wiping this profile's history")
    c = sub.add_parser("clone")
    c.add_argument("src")
    c.add_argument("new_name")
    c.add_argument("--version", help="strategy version for the clone (default: same as src)")
    args = ap.parse_args()

    with connect() as conn:
        if args.cmd == "reset":
            if not args.yes:
                ap.error("reset wipes all orders/trades/snapshots for the profile; pass --yes")
            reset(conn, args.name)
            print(f"reset {args.name}")
        elif args.cmd == "clone":
            clone(conn, args.src, args.new_name, args.version)
            print(f"cloned {args.src} -> {args.new_name} (paper)")
        else:
            p = _profile(conn, args.name)
            print({k: p[k] for k in ("name", "mode", "broker", "strategy_version")})
            if p["broker"] == "paper":
                b = PaperBroker(conn, args.name)
                print(f"cash {b.cash():.2f}  equity {b.equity():.2f}")
                print("positions", b.positions())
                print("open orders", [(o["id"], o["side"], o["qty"], o["symbol"], o["type"])
                                      for o in b.open_orders()])
