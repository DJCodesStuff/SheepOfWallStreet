"""Daily OHLCV loader: yfinance, cached as one CSV per symbol in data/.

Run `python -m sheep.trader.data` to warm the cache for every profile's watchlist.
"""

from __future__ import annotations

import re
import warnings
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from sheep.common.config import ROOT

DATA_DIR = ROOT / "data"
NY = ZoneInfo("America/New_York")
BAR_FINAL_AT = time(16, 30)  # treat a daily bar as final 30 min after the 16:00 close
COLUMNS = ["open", "high", "low", "close", "volume"]
_SYMBOL_RE = re.compile(r"[A-Z0-9.^=-]{1,15}")


def last_final_close(now: datetime) -> datetime:
    """Most recent moment a daily bar became final. Bars after this date are partial.

    ponytail: ignores weekends/holidays; worst case is one unneeded refetch on a non-trading day.
    """
    now = now.astimezone(NY)
    cutoff = datetime.combine(now.date(), BAR_FINAL_AT, NY)
    return cutoff if now >= cutoff else cutoff - timedelta(days=1)


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
    if not _SYMBOL_RE.fullmatch(symbol):
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
