import os
import uuid

import psycopg
import pytest
import yaml

from sheep.common.config import ConfigError, load_profiles, load_strategies
from sheep.common.db import SCHEMA, connect, sync_profiles, sync_rule_versions


@pytest.fixture
def db():
    """A throwaway schema inside one transaction that is always rolled back."""
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set (run with `uv run --env-file .env pytest`)")
    with connect() as conn:
        schema = f"test_{uuid.uuid4().hex[:12]}"
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"SET LOCAL search_path TO {schema}")  # LOCAL: safe with Neon's pooler
        conn.execute(SCHEMA)
        yield conn
        conn.rollback()


def write_profiles(tmp_path, **overrides):
    p = {
        "name": "p1", "mode": "paper", "broker": "paper", "starting_capital": 100,
        "strategy_version": "v1", "watchlist": ["spy"],
        "risk": {"risk_per_trade_pct": 1, "max_open_positions": 2, "weekly_loss_limit_pct": 3},
    } | overrides
    path = tmp_path / "profiles.yaml"
    path.write_text(yaml.safe_dump({"profiles": [p]}))
    return path


def test_repo_config_loads():
    profiles = load_profiles()
    assert {"main_paper", "experiment_momentum_fast", "live_small"} <= profiles.keys()


@pytest.mark.parametrize("overrides", [
    {"mode": "paper", "broker": "alpaca_live"},  # paper profile on live broker
    {"mode": "live", "broker": "paper"},
    {"mode": "yolo"},
    {"strategy_version": "nope"},
    {"starting_capital": 0},
    {"watchlist": []},
    {"risk": {"risk_per_trade_pct": 1}},  # missing risk fields
])
def test_bad_profiles_rejected(tmp_path, overrides):
    with pytest.raises(ConfigError):
        load_profiles(write_profiles(tmp_path, **overrides), strategies={"v1": {}})


def test_watchlist_uppercased(tmp_path):
    p = load_profiles(write_profiles(tmp_path), strategies={"v1": {}})["p1"]
    assert p.watchlist == ("SPY",)


def test_rule_versions_immutable(db):
    conn = db
    sync_rule_versions(conn, {"v1": {"atr_stop_mult": 2.0}})
    sync_rule_versions(conn, {"v1": {"atr_stop_mult": 2.0}})  # unchanged is fine
    with pytest.raises(ConfigError, match="immutable"):
        sync_rule_versions(conn, {"v1": {"atr_stop_mult": 3.0}})


def test_profile_mode_change_refused(tmp_path, db):
    conn = db
    sync_rule_versions(conn, {"v1": {}})
    sync_profiles(conn, load_profiles(write_profiles(tmp_path), strategies={"v1": {}}))
    live = write_profiles(tmp_path, mode="live", broker="alpaca_live")
    with pytest.raises(ConfigError, match="mode change"):
        sync_profiles(conn, load_profiles(live, strategies={"v1": {}}))


def test_trade_requires_stop_and_reasoning(db):
    conn = db
    sync_rule_versions(conn, load_strategies())
    sync_profiles(conn, load_profiles())
    with pytest.raises(psycopg.IntegrityError):
        conn.execute(
            "INSERT INTO trades (profile_id, symbol, entry_ts, entry_price, qty, target, "
            "rule_version, reasoning_json) "
            "VALUES ((SELECT min(id) FROM profiles), 'SPY', '2026-01-02', 500, 0.1, 520, "
            "'v1', '{}')"
        )
