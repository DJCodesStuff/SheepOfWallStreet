"""Load and validate config/profiles.yaml, strategy.yaml and instruments.yaml."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"

# Which brokers each mode may use. A paper profile can never point at a live broker.
BROKERS_BY_MODE = {"paper": {"paper", "alpaca_paper"}, "live": {"alpaca_live"}}
SYMBOL_RE = re.compile(r"[A-Z0-9.^=-]{1,15}")
NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Risk:
    risk_per_trade_pct: float
    max_open_positions: int
    weekly_loss_limit_pct: float
    # Pre-trade gate (sheep/trader/risk.py). The defaults pass every entry the sizing allows.
    min_reward_risk: float = 1.0  # (target - entry) / (entry - stop)
    max_position_pct: float = 100.0  # one stock/ETF position's cost, as % of equity
    blocked: tuple[str, ...] = ()  # never enter these (e.g. ahead of earnings)


@dataclass(frozen=True)
class Instrument:
    """How a symbol trades. The default is a stock/ETF: fractional units, paid in full."""

    name: str = ""
    multiplier: float = 1.0  # dollars per 1.00 point of price
    margin: float | None = None  # per contract; None = paid in full (stocks, ETFs)
    fee: float = 0.0  # per unit per side, on top of Costs.fee_per_order
    roll_months: tuple[int, ...] = ()
    roll_day: int = 10

    @property
    def is_future(self) -> bool:  # futures: whole contracts, margin, P&L x multiplier, rolls
        return self.margin is not None

    def rolls_between(self, after: date, upto: date) -> bool:
        """True if a roll date falls in (after, upto]."""
        return any(
            after < date(y, m, self.roll_day) <= upto
            for y in range(after.year, upto.year + 1)
            for m in self.roll_months
        )


EQUITY = Instrument()


def asset_class(symbol: str) -> str:
    if symbol.endswith("=F"):
        return "future"
    if symbol.startswith("^"):
        return "index"
    return "other" if "=" in symbol else "equity"


@cache
def load_futures(path: Path = CONFIG_DIR / "instruments.yaml") -> dict[str, Instrument]:
    raw = (yaml.safe_load(Path(path).read_text()) or {}).get("futures") or {}
    return {
        s: Instrument(**(spec | {"roll_months": tuple(spec.get("roll_months", ()))}))
        for s, spec in raw.items()
    }


def instrument(symbol: str) -> Instrument:
    """Trading terms for a symbol; raises for anything that can't be traded."""
    kind = asset_class(symbol)
    if kind == "equity":
        return EQUITY
    if kind == "future":
        if symbol not in load_futures():
            raise ConfigError(f"{symbol}: no contract spec; add it to config/instruments.yaml")
        return load_futures()[symbol]
    if kind == "index":
        raise ConfigError(f"{symbol} is an index and can't be bought directly; trade an ETF "
                          "(e.g. SPY for ^GSPC) or a future (e.g. MES=F) instead")
    raise ConfigError(f"{symbol}: only stocks, ETFs and listed futures (=F) are supported")


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
        p = parse_profile(raw, strategies)
        if p.name in profiles:
            raise ConfigError(f"duplicate profile name: {p.name}")
        profiles[p.name] = p
    return profiles


def parse_profile(raw: dict, strategies: dict[str, dict]) -> Profile:
    name = raw.get("name") or "<unnamed>"
    try:
        p = Profile(
            name=raw["name"],
            mode=raw["mode"],
            broker=raw["broker"],
            starting_capital=float(raw["starting_capital"]),
            strategy_version=raw["strategy_version"],
            watchlist=tuple(str(s).upper() for s in raw["watchlist"]),
            risk=Risk(**raw["risk"] | {
                "blocked": tuple(str(s).upper() for s in raw["risk"].get("blocked") or ())}),
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
    check(bool(NAME_RE.fullmatch(p.name)), "name must be 1-64 letters, digits, _ or -")
    check(len(p.watchlist) > 0, "watchlist is empty")
    bad = [s for s in p.watchlist if not SYMBOL_RE.fullmatch(s)]
    check(not bad, f"bad symbols: {bad}")
    for s in p.watchlist:
        try:
            inst = instrument(s)
        except ConfigError as e:
            check(False, str(e))
        check(not (inst.is_future and p.mode == "live"),
              f"{s}: futures are paper-only (no live futures broker)")
    check(0 < p.risk.risk_per_trade_pct <= 100, "risk_per_trade_pct must be in (0, 100]")
    check(0 < p.risk.weekly_loss_limit_pct <= 100, "weekly_loss_limit_pct must be in (0, 100]")
    check(p.risk.max_open_positions >= 1, "max_open_positions must be >= 1")
    check(p.risk.min_reward_risk >= 0, "min_reward_risk must be >= 0")
    check(0 < p.risk.max_position_pct <= 100, "max_position_pct must be in (0, 100]")
    return p
