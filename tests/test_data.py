import os
from datetime import datetime

import pandas as pd
import pytest

from sheep.trader import data
from sheep.trader.data import NY, load_bars


def fake_bars(*days):
    idx = pd.DatetimeIndex(pd.to_datetime(days), name="date")
    return pd.DataFrame({c: 1.0 for c in data.COLUMNS}, index=idx)


@pytest.fixture
def fetches(monkeypatch):
    calls = []

    def fake_fetch(symbol):
        calls.append(symbol)
        # unsorted, with a duplicate, plus Tuesday's (possibly partial) bar
        return fake_bars("2026-11-02", "2026-10-30", "2026-11-02", "2026-11-03")

    monkeypatch.setattr(data, "_fetch", fake_fetch)
    return calls


TUE_MIDDAY = datetime(2026, 11, 3, 12, 0, tzinfo=NY)
TUE_EVENING = datetime(2026, 11, 3, 17, 0, tzinfo=NY)


def test_partial_bar_dropped_during_session(tmp_path, fetches):
    df = load_bars("spy", tmp_path, now=TUE_MIDDAY)
    assert list(df.index.strftime("%Y-%m-%d")) == ["2026-10-30", "2026-11-02"]


def test_final_bar_kept_after_close(tmp_path, fetches):
    df = load_bars("SPY", tmp_path, now=TUE_EVENING)
    assert df.index[-1] == pd.Timestamp("2026-11-03")


def test_cache_hit_and_stale_refetch(tmp_path, fetches):
    load_bars("SPY", tmp_path, now=TUE_MIDDAY)
    path = tmp_path / "SPY.csv"
    os.utime(path, (TUE_MIDDAY.timestamp(),) * 2)

    cached = load_bars("SPY", tmp_path, now=TUE_MIDDAY.replace(hour=15))
    assert len(fetches) == 1  # fetched after Monday's close: still fresh
    assert list(cached.columns) == data.COLUMNS

    load_bars("SPY", tmp_path, now=TUE_EVENING)
    assert len(fetches) == 2  # Tuesday's bar went final since the fetch: stale


def test_bad_symbol_rejected(tmp_path, fetches):
    with pytest.raises(ValueError):
        load_bars("../etc/passwd", tmp_path)


def test_nyse_holidays():
    from datetime import date

    from sheep.trader.data import is_session, prev_session

    closed = ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
              "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",  # July 4 2026: Saturday
              "2027-03-26", "2027-06-18", "2027-07-05", "2027-12-24"]  # Sat/Sun observances
    assert not any(is_session(date.fromisoformat(d)) for d in closed)
    assert is_session(date(2021, 12, 31))  # New Year's on a Saturday: no Friday closure
    assert is_session(date(2026, 11, 27))  # day after Thanksgiving: early close, still a session
    assert prev_session(date(2026, 1, 20)) == date(2026, 1, 16)  # Tue after MLK -> Fri


def test_last_final_close_skips_holidays_and_weekends():
    from sheep.trader.data import NY, last_final_close

    tue_morning = datetime(2026, 1, 20, 10, tzinfo=NY)  # Monday was MLK day
    assert last_final_close(tue_morning) == datetime(2026, 1, 16, 16, 30, tzinfo=NY)
    sat = datetime(2026, 1, 24, 18, tzinfo=NY)
    assert last_final_close(sat) == datetime(2026, 1, 23, 16, 30, tzinfo=NY)
