"""Load and validate config/profiles.yaml and config/strategy.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"

# Which brokers each mode may use. A paper profile can never point at a live broker.
BROKERS_BY_MODE = {"paper": {"paper", "alpaca_paper"}, "live": {"alpaca_live"}}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Risk:
    risk_per_trade_pct: float
    max_open_positions: int
    weekly_loss_limit_pct: float


@dataclass(frozen=True)
class Profile:
    name: str
    mode: str
    broker: str
    starting_capital: float
    strategy_version: str
    watchlist: tuple[str, ...]
    risk: Risk
    currency: str = "USD"


def load_strategies(path: Path = CONFIG_DIR / "strategy.yaml") -> dict[str, dict]:
    versions = (yaml.safe_load(Path(path).read_text()) or {}).get("versions") or {}
    if not versions:
        raise ConfigError(f"{path}: no strategy versions defined")
    return versions


def load_profiles(
    path: Path = CONFIG_DIR / "profiles.yaml", strategies: dict[str, dict] | None = None
) -> dict[str, Profile]:
    strategies = load_strategies() if strategies is None else strategies
    profiles: dict[str, Profile] = {}
    for raw in (yaml.safe_load(Path(path).read_text()) or {}).get("profiles") or []:
        p = _parse_profile(raw, strategies)
        if p.name in profiles:
            raise ConfigError(f"duplicate profile name: {p.name}")
        profiles[p.name] = p
    return profiles


def _parse_profile(raw: dict, strategies: dict[str, dict]) -> Profile:
    name = raw.get("name") or "<unnamed>"
    try:
        p = Profile(
            name=raw["name"],
            mode=raw["mode"],
            broker=raw["broker"],
            starting_capital=float(raw["starting_capital"]),
            strategy_version=raw["strategy_version"],
            watchlist=tuple(str(s).upper() for s in raw["watchlist"]),
            risk=Risk(**raw["risk"]),
            currency=raw.get("currency", "USD"),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise ConfigError(f"profile {name}: bad or missing field ({e})") from e

    def check(ok: bool, msg: str) -> None:
        if not ok:
            raise ConfigError(f"profile {name}: {msg}")

    check(p.mode in BROKERS_BY_MODE, f"mode must be one of {sorted(BROKERS_BY_MODE)}")
    check(p.broker in BROKERS_BY_MODE[p.mode], f"broker {p.broker!r} not allowed in {p.mode} mode")
    check(p.starting_capital > 0, "starting_capital must be > 0")
    check(p.strategy_version in strategies, f"unknown strategy_version {p.strategy_version!r}")
    check(len(p.watchlist) > 0, "watchlist is empty")
    check(0 < p.risk.risk_per_trade_pct <= 100, "risk_per_trade_pct must be in (0, 100]")
    check(0 < p.risk.weekly_loss_limit_pct <= 100, "weekly_loss_limit_pct must be in (0, 100]")
    check(p.risk.max_open_positions >= 1, "max_open_positions must be >= 1")
    return p
