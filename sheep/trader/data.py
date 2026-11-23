"""Daily OHLCV loader: yfinance, cached as one CSV per symbol in data/.

Run `python -m sheep.trader.data` to warm the cache for every profile's watchlist.
"""

from __future__ import annotations

import warnings
from datetime import date, datetime, time, timedelta
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from pandas.tseries.holiday import (
    AbstractHolidayCalendar,
    GoodFriday,
    Holiday,
    USLaborDay,
    USMartinLutherKingJr,
    USMemorialDay,
    USPresidentsDay,
    USThanksgivingDay,
    nearest_workday,
    sunday_to_monday,
)

from sheep.common.config import ROOT, SYMBOL_RE

DATA_DIR = ROOT / "data"
NY = ZoneInfo("America/New_York")
MARKET_OPEN, MARKET_CLOSE = time(9, 30), time(16)
BAR_FINAL_AT = time(16, 30)  # treat a daily bar as final 30 min after the 16:00 close
COLUMNS = ["open", "high", "low", "close", "volume"]


class NYSEHolidays(AbstractHolidayCalendar):
    """NYSE full-day closures. A Saturday holiday closes the Friday before, a Sunday one the
    Monday after, except New Year's Day: the NYSE stays open on a Friday Dec 31.

    ponytail: no one-off closures (e.g. national days of mourning, announced days ahead); add
    them as Holiday(name, year=, month=, day=) when announced. Early 13:00 closes count as
    full sessions.
    """

    rules = [
        Holiday("New Year's Day", month=1, day=1, observance=sunday_to_monday),
        USMartinLutherKingJr, USPresidentsDay, GoodFriday, USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, start_date="2022-01-01",
                observance=nearest_workday),
        Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
        USLaborDay, USThanksgivingDay,
        Holiday("Christmas Day", month=12, day=25, observance=nearest_workday),
    ]


@cache
def _holidays(year: int) -> frozenset[date]:
    return frozenset(NYSEHolidays().holidays(f"{year}-01-01", f"{year}-12-31").date)


def is_session(d: date) -> bool:
    """True if the NYSE trades on day d."""
    return d.weekday() < 5 and d not in _holidays(d.year)


def prev_session(d: date) -> date:
    """The last trading day before d."""
    d -= timedelta(days=1)
    while not is_session(d):
        d -= timedelta(days=1)
    return d


def today_open(symbol: str, now: datetime | None = None) -> float | None:
    """Today's opening price, or None before the open or on a day with no session."""
    import yfinance as yf

    now = (now or datetime.now(NY)).astimezone(NY)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", module="yfinance")
        df = yf.Ticker(symbol).history(period="5d", interval="1d", auto_adjust=True)
    if df.empty or df.index[-1].date() != now.date():
        return None
    px = float(df["Open"].iloc[-1])
    return px if px > 0 else None  # also rejects NaN


def last_final_close(now: datetime) -> datetime:
    """Most recent moment a session's daily bar became final. Bars after this date are partial."""
    now = now.astimezone(NY)
    d = now.date()
    if not (is_session(d) and now.time() >= BAR_FINAL_AT):
        d = prev_session(d)
    return datetime.combine(d, BAR_FINAL_AT, NY)


def _fetch(symbol: str) -> pd.DataFrame:
    import yfinance as yf

    with warnings.catch_warnings():  # yfinance 1.7 trips pandas 3.1 deprecations internally
        warnings.filterwarnings("ignore", module="yfinance")
        df = yf.Ticker(symbol).history(period="max", interval="1d", auto_adjust=True)
    if df.empty:
        raise ValueError(f"yfinance returned no data for {symbol}")
    df = df.rename(columns=str.lower)[COLUMNS]
    df.index = pd.DatetimeIndex(df.index.tz_localize(None).normalize(), name="date")
    return df


def _clean(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Sort, dedupe, drop NaNs, and drop today's bar if it isn't final yet (no look-ahead)."""
    df = df[~df.index.duplicated(keep="last")].sort_index().dropna()
    return df.loc[: pd.Timestamp(last_final_close(now).date())]


def load_bars(
    symbol: str, cache_dir: Path = DATA_DIR, refresh: bool = False, now: datetime | None = None
) -> pd.DataFrame:
    """Full split/dividend-adjusted daily history, indexed by date, lowercase OHLCV columns.

    Refetches the whole history when stale rather than appending: adjusted prices shift after
    every dividend/split, so appending would mix adjustment bases.
    """
    symbol = symbol.upper()
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError(f"bad symbol: {symbol!r}")
    now = now or datetime.now(NY)
    path = Path(cache_dir) / f"{symbol}.csv"

    if not refresh and path.exists():
        fetched_at = datetime.fromtimestamp(path.stat().st_mtime, NY)
        if fetched_at >= last_final_close(now):
            return pd.read_csv(path, index_col="date", parse_dates=["date"])

    df = _clean(_fetch(symbol), now)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    df.to_csv(tmp)
    tmp.replace(path)  # atomic: a crash mid-write never leaves a corrupt cache
    return df


if __name__ == "__main__":
    from sheep.common.config import load_profiles

    for sym in sorted({s for p in load_profiles().values() for s in p.watchlist}):
        bars = load_bars(sym)
        print(f"{sym:6} {len(bars):6} bars  {bars.index[0].date()} -> {bars.index[-1].date()}")
