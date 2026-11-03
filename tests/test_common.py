import psycopg
import pytest
import yaml

from sheep.common.config import ConfigError, load_profiles, load_strategies
from sheep.common.db import sync_profiles, sync_rule_versions


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
    {"watchlist": ["../etc"]},
    {"name": "has space"},
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


@pytest.mark.parametrize("watchlist, mode, ok", [
    (["MES=F"], "paper", True),
    (["^GSPC"], "paper", False),  # indexes can't be traded
    (["ZZZ=F"], "paper", False),  # no contract spec
    (["EURUSD=X"], "paper", False),
    (["MES=F"], "live", False),  # futures are paper-only
])
def test_watchlist_instruments(tmp_path, watchlist, mode, ok):
    broker = "alpaca_live" if mode == "live" else "paper"
    path = write_profiles(tmp_path, watchlist=watchlist, mode=mode, broker=broker)
    if ok:
        load_profiles(path, strategies={"v1": {}})
    else:
        with pytest.raises(ConfigError):
            load_profiles(path, strategies={"v1": {}})


def test_connect_retries_while_database_wakes(monkeypatch):
    from sheep.common import db

    calls = []

    def flaky(*a, **kw):
        calls.append(kw["connect_timeout"])
        if len(calls) < 3:
            raise psycopg.OperationalError("compute is starting")
        return "conn"

    monkeypatch.setattr(db.psycopg, "connect", flaky)
    monkeypatch.setattr(db.time, "sleep", lambda s: None)
    assert db.connect("postgresql://x") == "conn" and len(calls) == 3

    def down(*a, **kw):
        calls.append(1)
        raise psycopg.OperationalError("down")

    calls.clear()
    monkeypatch.setattr(db.psycopg, "connect", down)
    with pytest.raises(psycopg.OperationalError):
        db.connect("postgresql://x")
    assert len(calls) == 3  # gives up after three tries
