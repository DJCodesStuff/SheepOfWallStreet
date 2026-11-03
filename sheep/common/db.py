"""Postgres schema and config sync. Run `uv run --env-file .env python -m sheep.common.db`.

Connection comes from the DATABASE_URL env var (kept in the gitignored .env, never committed).
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from sheep.common.config import ConfigError, Profile, load_profiles, load_strategies

SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_versions (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version     TEXT NOT NULL UNIQUE,
    params_json JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_by TEXT,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS profiles (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,
    mode             TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    broker           TEXT NOT NULL CHECK (broker IN ('paper', 'alpaca_paper', 'alpaca_live')),
    starting_capital DOUBLE PRECISION NOT NULL CHECK (starting_capital > 0),
    currency         TEXT NOT NULL DEFAULT 'USD',
    strategy_version TEXT NOT NULL REFERENCES rule_versions(version),
    config_json      JSONB NOT NULL
);

-- stop, target, rule_version and reasoning are required: every trade must carry them.
CREATE TABLE IF NOT EXISTS trades (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    profile_id     BIGINT NOT NULL REFERENCES profiles(id),
    symbol         TEXT NOT NULL,
    entry_ts       TIMESTAMPTZ NOT NULL,
    exit_ts        TIMESTAMPTZ,
    entry_price    DOUBLE PRECISION NOT NULL,
    exit_price     DOUBLE PRECISION,
    qty            DOUBLE PRECISION NOT NULL CHECK (qty > 0),
    stop           DOUBLE PRECISION NOT NULL,
    target         DOUBLE PRECISION NOT NULL,
    pnl            DOUBLE PRECISION,
    rule_version   TEXT NOT NULL REFERENCES rule_versions(version),
    reasoning_json JSONB NOT NULL,
    exit_reason    TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    profile_id BIGINT NOT NULL REFERENCES profiles(id),
    symbol     TEXT NOT NULL,
    side       TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    qty        DOUBLE PRECISION NOT NULL CHECK (qty > 0),
    type       TEXT NOT NULL CHECK (type IN ('market', 'limit', 'stop')),
    limit_price DOUBLE PRECISION,
    stop_price DOUBLE PRECISION,
    oco_group  TEXT,  -- when one order in a group fills, the others are cancelled
    trade_id   BIGINT REFERENCES trades(id),
    intent_json JSONB,  -- entry orders: stop, target, rule version, reasoning
    status     TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'filled', 'cancelled', 'rejected')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    filled_at  TIMESTAMPTZ,
    fill_price DOUBLE PRECISION,
    fees       DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS trade_tags (
    id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trade_id  BIGINT NOT NULL REFERENCES trades(id),
    tag       TEXT NOT NULL,
    category  TEXT NOT NULL,
    notes     TEXT,
    tagged_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    profile_id BIGINT NOT NULL REFERENCES profiles(id),
    date       DATE NOT NULL,
    cash       DOUBLE PRECISION NOT NULL,
    equity     DOUBLE PRECISION NOT NULL,
    UNIQUE (profile_id, date)
);

CREATE TABLE IF NOT EXISTS proposals (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    profile_id           BIGINT REFERENCES profiles(id),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    description          TEXT NOT NULL,
    params_diff_json     JSONB NOT NULL,
    backtest_result_json JSONB,
    status               TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'approved', 'rejected'))
);
"""


def connect(url: str | None = None) -> psycopg.Connection:
    url = url or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set; put it in .env and use `uv run --env-file .env`")
    # Neon's free tier suspends the database after 5 idle minutes; the first connection wakes it
    # and can time out, so retry a couple of times before giving up.
    for attempt in range(3):
        try:
            return psycopg.connect(url, row_factory=dict_row, connect_timeout=15)
        except psycopg.OperationalError:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


# Sync functions never commit: the caller owns the transaction, so a failed sync changes nothing.

def sync_rule_versions(conn: psycopg.Connection, strategies: dict[str, dict]) -> None:
    """Insert new versions. Existing versions are immutable: changed params raise."""
    for version, params in strategies.items():
        row = conn.execute(
            "SELECT params_json FROM rule_versions WHERE version = %s", (version,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO rule_versions (version, params_json, approved_by) VALUES (%s, %s, %s)",
                (version, Jsonb(params), "config"),
            )
        elif row["params_json"] != params:
            raise ConfigError(f"rule version {version} is immutable; add a new version instead")


def sync_profiles(conn: psycopg.Connection, profiles: dict[str, Profile]) -> None:
    """Upsert profiles. Refuses to change an existing profile's mode (no auto-promotion)."""
    for p in profiles.values():
        row = conn.execute("SELECT mode FROM profiles WHERE name = %s", (p.name,)).fetchone()
        if row is not None and row["mode"] != p.mode:
            raise ConfigError(
                f"profile {p.name}: mode change {row['mode']} -> {p.mode} refused; "
                "create a new profile instead"
            )
        conn.execute(
            """INSERT INTO profiles
                   (name, mode, broker, starting_capital, currency, strategy_version, config_json)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (name) DO UPDATE SET
                   broker = excluded.broker,
                   starting_capital = excluded.starting_capital,
                   currency = excluded.currency,
                   strategy_version = excluded.strategy_version,
                   config_json = excluded.config_json""",
            (p.name, p.mode, p.broker, p.starting_capital, p.currency,
             p.strategy_version, Jsonb(asdict(p))),
        )


def init_db(conn: psycopg.Connection) -> None:
    strategies = load_strategies()
    profiles = load_profiles(strategies=strategies)
    conn.execute(SCHEMA)
    sync_rule_versions(conn, strategies)
    sync_profiles(conn, profiles)


if __name__ == "__main__":
    with connect() as conn:  # commits on success, rolls back on error
        init_db(conn)
        for r in conn.execute("SELECT name, mode, broker, strategy_version FROM profiles"):
            print(r)
