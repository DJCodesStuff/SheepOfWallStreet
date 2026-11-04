from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from sheep.trader import news
from sheep.trader.data import NY
from sheep.trader.scanner import classify, find_setups, parse_spark, scan


def closes(values):
    return pd.Series(values, index=pd.bdate_range("2026-01-02", periods=len(values)), dtype=float)


@pytest.mark.parametrize("values, setup", [
    (100 + np.arange(40.0), "breakout_20d"),
    (np.r_[np.full(25, 100.0), 100 * 1.015 ** np.arange(1, 11), np.full(5, 116.0)], "bull_flag"),
    (100 - np.arange(40.0), "oversold"),
    (np.r_[100 - np.arange(39.0), 62.5], "oversold_bounce"),
    (np.r_[np.full(30, 100.0), 99.0, 101.0], "reclaim_ma20"),
])
def test_setups(values, setup):
    assert setup in find_setups(closes(values))


def test_scan_skips_cheap_and_short_charts():
    data = {"UP": closes(100 + np.arange(40.0)), "PENNY": closes(1 + np.arange(40) / 100),
            "NEW": closes(100 + np.arange(10.0))}
    hits = scan(data, {"UP": 2e6, "PENNY": 2e6}, min_price=5, min_volume=1e6)
    assert list(hits["symbol"]) == ["UP"]
    assert scan(data, {"UP": 5e5}, min_volume=1e6).empty


@pytest.mark.parametrize("row, verdict", [
    ({"Symbol": "AAPL", "Security Name": "Apple Inc. - Common Stock", "Test Issue": "N",
      "ETF": "N"}, "keep"),
    ({"ACT Symbol": "BRK.B", "Security Name": "Berkshire Hathaway Inc. Class B",
      "Test Issue": "N", "ETF": "N"}, "keep"),
    ({"Symbol": "ABCDW", "Security Name": "ABC Corp - Warrants", "Test Issue": "N",
      "ETF": "N"}, "warrant"),
    ({"Symbol": "SPY", "Security Name": "SPDR S&P 500 ETF Trust", "Test Issue": "N",
      "ETF": "Y"}, "keep"),
    ({"Symbol": "TQQQ", "Security Name": "ProShares UltraPro QQQ", "Test Issue": "N",
      "ETF": "Y"}, "leveraged or inverse ETF"),
    ({"Symbol": "ZVZZT", "Security Name": "Test", "Test Issue": "Y", "ETF": "N"}, "test issue"),
])
def test_universe_filter(row, verdict):
    assert classify(row) == verdict


def test_spark_drops_todays_partial_bar():
    days = pd.bdate_range("2026-11-02", periods=3)  # Mon-Wed
    ts = [int(pd.Timestamp(d, tz=NY).replace(hour=9, minute=30).timestamp()) for d in days]
    doc = {"SPY": {"timestamp": ts, "close": [1.0, 2.0, 3.0]}}
    wed_noon = datetime(2026, 11, 4, 12, tzinfo=NY)
    assert list(parse_spark(doc, wed_noon)["SPY"]) == [1.0, 2.0]
    assert list(parse_spark(doc, wed_noon.replace(hour=17))["SPY"]) == [1.0, 2.0, 3.0]


RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Acme beats on earnings, raises guidance</title><link>https://x/1</link>
<pubDate>Wed, 04 Nov 2026 13:00:00 GMT</pubDate></item>
<item><title>Old story</title><link>https://x/2</link>
<pubDate>Sun, 01 Nov 2026 13:00:00 GMT</pubDate></item>
</channel></rss>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<company-info><conformed-name>Acme Inc.</conformed-name></company-info>
<entry><content><filing-type>8-K</filing-type><items-desc>items 2.02 and 9.01</items-desc>
</content><link href="https://sec/1"/><updated>2026-11-04T08:00:00-05:00</updated></entry>
</feed>"""


def test_news_tags_dedupes_and_windows(monkeypatch):
    def fake_get(url, ua=None):
        if "company_tickers" in url:
            return b'{"0": {"ticker": "ACME", "cik_str": 1}}'
        return ATOM if "sec.gov" in url else RSS

    monkeypatch.setattr(news, "_get", fake_get)
    monkeypatch.setattr(news.time, "sleep", lambda s: None)
    news.cik_map.cache_clear()
    now = datetime(2026, 11, 4, 18, tzinfo=UTC)
    df, notes = news.collect(["ACME", "XLF"], hours=24, sec_ua="Test test@test.dev", now=now)
    assert notes == []
    head = df[df["link"] == "https://x/1"].iloc[0]
    assert head["ticker"] == "ACME, XLF"  # one row for a story in both feeds
    assert head["tags"] == ["earnings"]
    filing = df[df["source"] == "SEC EDGAR"].iloc[0]
    assert filing["title"] == "Acme Inc. filed a 8-K: earnings results" and filing["important"]
    assert "https://x/2" not in set(df["link"])  # older than 24 hours
