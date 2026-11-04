"""Market scanner on daily closes: lists charts that match a rule, to help build a watchlist.

Adapted from trader-scanner in day-trader-skill (MIT, Jake Schincariol,
github.com/Jakeschincariol/day-trader-skill). Kept: the universe filter over Nasdaq Trader's
symbol directories and the five setups on daily closes. Changed for swing trading: ETFs are in
by default (leveraged/inverse ones out), each hit also shows the Phase 1 trend filter and
momentum, and the intraday opening-range scan is left out. A hit matched a rule; it is not a buy.

Data: Yahoo's spark endpoint (free, unofficial, 20 symbols a request, closes only), then one
yfinance download of just the hits for their 20-day average volume. Run it after the close or
before the open; a bar is used only once it is final.

    uv run python -m sheep.trader.scanner SPY QQQ XLK    # these symbols
    uv run python -m sheep.trader.scanner --universe     # ~6,000 US stocks + ETFs, a few minutes
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
import warnings
from datetime import datetime

import numpy as np
import pandas as pd

from sheep.trader.data import NY, last_final_close
from sheep.trader.strategy import rsi, sma

UA = "Mozilla/5.0 (compatible; SheepOfWallStreet/0.1)"
NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/"
NASDAQ_FILES = ("nasdaqlisted.txt", "otherlisted.txt")
SPARK_URL = "https://{host}.finance.yahoo.com/v8/finance/spark?symbols={syms}&range=1y&interval=1d"
SPARK_MAX = 20  # Yahoo answers 400 above 20 symbols
PAUSE = 0.3  # seconds between requests: be polite to free endpoints
MIN_BARS = 30
SETUPS = {
    "breakout_20d": "closed above every close of the 20 sessions before",
    "bull_flag": "up over 10% in the 10 sessions before the last 5, then 5 closes within 5%",
    "reclaim_ma20": "closed back above its 20-day average",
    "oversold": "RSI(14) under 30",
    "oversold_bounce": "RSI(14) was under 30 yesterday and today closed up",
}


def _get(url: str, ua: str = UA, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# --- universe ---------------------------------------------------------------------------------

TICKER = re.compile(r"^[A-Z]{1,5}$|^[A-Z]{1,4}\.[A-Z]$")
ALWAYS_OUT = [
    ("warrant", re.compile(r"\bwarrants?\b")),
    ("unit or right",
     re.compile(r"\b(units?|rights?),? (each|that|entitling)\b|(?<!the )\brights? to\b")),
    ("note", re.compile(r"\b(notes?|etns?|debentures?)\b")),
    ("coupon", re.compile(r"%")),
    ("depositary", re.compile(r"(?<!american )(?<!global )\bdepositary shares?\b")),
]
COMMON = re.compile(
    r"\bcommon (stock|shares?|units?)\b|\bord(inary)? shares?\b|\bshares? of beneficial interest\b"
    r"|\b(american|global) deposit[ao]ry\b|\bads[s]?\b|\badrs?\b"
    r"|\b(limited partnership|partnership|trust) units?\b|\bl\.?p\.? units?\b"
    r"|\bunits? (of|representing)\b.*\b(limited partner|partnership|beneficial interest"
    r"|limited liability)")
SOFT_OUT = [
    ("unit", re.compile(r"\bunits?\b")),
    ("right", re.compile(r"\brights?\b")),
    ("bond", re.compile(r"\bbonds?\b")),
    ("preferred", re.compile(r"\bpreferred\b|\bpreference\b")),
]
# Leveraged and inverse ETFs are a project non-goal. ponytail: by name, so a fund that doesn't
# say so slips through.
GEARED = re.compile(r"\b(ultra\w*|[1-5](\.\d+)?x|leveraged|inverse|short|bear|daily)\b")


def classify(row: dict, etfs: bool = True) -> str:
    """One symbol-directory row -> 'keep' or the rule that drops it."""
    sym = (row.get("Symbol") or row.get("ACT Symbol") or "").strip()
    name = " ".join((row.get("Security Name") or "").lower().split())
    if (row.get("Test Issue") or "").strip() != "N":
        return "test issue"
    if (row.get("NextShares") or "N").strip() != "N":
        return "NextShares"
    if not TICKER.match(sym):
        return "not a plain ticker"
    if (row.get("ETF") or "").strip() != "N":
        if not etfs:
            return "ETF"
        return "leveraged or inverse ETF" if GEARED.search(name) else "keep"
    if re.search(r"\.[UWR]$", sym):
        return "unit / warrant / right suffix"
    for tag, rx in ALWAYS_OUT:
        if rx.search(name):
            return tag
    if COMMON.search(name):
        return "keep"
    for tag, rx in SOFT_OUT:
        if rx.search(name):
            return tag
    return "keep"


def parse_symdir(text: str) -> list[dict]:
    """A Nasdaq Trader pipe file -> rows. The last line is 'File Creation Time: ...'."""
    lines = [ln for ln in text.replace("\r", "").split("\n") if ln.strip()]
    hdr = [h.strip() for h in lines[0].split("|")] if lines else []
    if "Security Name" not in hdr:
        raise ValueError("not a Nasdaq Trader symbol directory")
    return [dict(zip(hdr, ln.split("|"), strict=False)) for ln in lines[1:]
            if not ln.startswith("File Creation Time")]


def universe(etfs: bool = True) -> list[str]:
    """US-listed common stocks, ADRs (and plain ETFs), as Yahoo symbols (BRK.B -> BRK-B)."""
    out = set()
    for f in NASDAQ_FILES:
        for row in parse_symdir(_get(NASDAQ_URL + f).decode()):
            if classify(row, etfs) == "keep":
                out.add((row.get("Symbol") or row.get("ACT Symbol")).strip().replace(".", "-"))
    return sorted(out)


# --- data -------------------------------------------------------------------------------------

def parse_spark(doc: dict, now: datetime) -> dict[str, pd.Series]:
    """Spark JSON -> {symbol: final daily closes}. Today's bar is dropped until it's final."""
    cutoff = pd.Timestamp(last_final_close(now).date())
    out = {}
    for sym, d in doc.items():
        ts, closes = d.get("timestamp") or [], d.get("close") or []
        if not ts or len(ts) != len(closes):
            continue
        idx = pd.to_datetime(ts, unit="s", utc=True).tz_convert(NY).tz_localize(None).normalize()
        s = pd.Series(closes, index=idx, dtype=float).dropna()
        out[sym] = s[~s.index.duplicated(keep="last")].loc[:cutoff]
    return out


def closes(symbols: list[str], now: datetime | None = None,
           progress=None) -> tuple[dict[str, pd.Series], list[str]]:
    """({symbol: daily closes}, errors), 20 symbols a request."""
    now = now or datetime.now(NY)
    out, errors = {}, []
    chunks = [symbols[i:i + SPARK_MAX] for i in range(0, len(symbols), SPARK_MAX)]
    for n, chunk in enumerate(chunks):
        for host in ("query2", "query1"):  # query1 is the fallback on a 429 or 5xx
            try:
                doc = json.loads(_get(SPARK_URL.format(host=host, syms=",".join(chunk))))
                out |= parse_spark(doc, now)
                break
            except (OSError, ValueError) as e:  # URLError, timeouts, bad JSON
                if host == "query1":
                    errors.append(f"{','.join(chunk)}: {e}")
        if progress:
            progress((n + 1) / len(chunks))
        time.sleep(PAUSE)
    return out, errors


def avg_volume(symbols: list[str], days: int = 20) -> dict[str, float]:
    """20-day average daily volume per symbol (one yfinance download)."""
    if not symbols:
        return {}
    import yfinance as yf

    with warnings.catch_warnings():  # yfinance 1.7 trips pandas 3.1 deprecations internally
        warnings.filterwarnings("ignore", module="yfinance")
        df = yf.download(symbols, period="2mo", interval="1d", group_by="ticker",
                         progress=False, threads=True, auto_adjust=True)
    out = {}
    for s in symbols:
        try:
            out[s] = float(df[s]["Volume"].dropna().tail(days).mean())
        except KeyError:
            continue
    return out


# --- setups -----------------------------------------------------------------------------------

def find_setups(c: pd.Series) -> list[str]:
    """Setup keys for one symbol's daily closes (oldest first, at least MIN_BARS)."""
    v, r = c.to_numpy(), rsi(c, 14).to_numpy()
    tags = []
    if v[-1] > v[-21:-1].max():
        tags.append("breakout_20d")
    if v[-6] / v[-16] - 1 > 0.10 and (v[-5:].max() - v[-5:].min()) / v[-1] < 0.05:
        tags.append("bull_flag")
    if v[-2] < v[-21:-1].mean() and v[-1] > v[-20:].mean():
        tags.append("reclaim_ma20")
    if r[-1] < 30:
        tags.append("oversold")
    if r[-2] < 30 and v[-1] > v[-2]:
        tags.append("oversold_bounce")
    return tags


def scan(data: dict[str, pd.Series], volume: dict[str, float] | None = None,
         min_price: float = 5.0, min_volume: float = 1e6) -> pd.DataFrame:
    """Hits, most dollars traded first. `volume` (avg shares/day) None = skip the volume floor."""
    rows = []
    for sym, c in data.items():
        if len(c) < MIN_BARS or c.iloc[-1] < min_price:
            continue
        tags = find_setups(c)
        if not tags:
            continue
        ma200 = sma(c, 200).iloc[-1]
        rows.append({
            "symbol": sym, "setups": tags, "close": float(c.iloc[-1]), "date": c.index[-1].date(),
            "rsi": float(rsi(c, 14).iloc[-1]),
            "above_200d": bool(c.iloc[-1] > ma200) if pd.notna(ma200) else None,
            "momentum_6m": float(c.iloc[-1] / c.iloc[-127] - 1) if len(c) > 126 else np.nan,
        })
    hits = pd.DataFrame(rows, columns=["symbol", "setups", "close", "date", "rsi", "above_200d",
                                       "momentum_6m"])
    if volume is not None:
        hits["avg_volume"] = hits["symbol"].map(volume)
        hits = hits[hits["avg_volume"] >= min_volume]
        hits = hits.assign(dollar_volume=hits["close"] * hits["avg_volume"])
        hits = hits.sort_values("dollar_volume", ascending=False)
    return hits.reset_index(drop=True)


def run(symbols: list[str], min_price: float = 5.0, min_volume: float = 1e6,
        progress=None) -> tuple[pd.DataFrame, dict]:
    """Fetch, scan, apply the volume floor to the hits. Returns (hits, counts incl. errors)."""
    data, errors = closes(symbols, progress=progress)
    pre = scan(data, None, min_price)
    hits = scan({s: data[s] for s in pre["symbol"]}, avg_volume(list(pre["symbol"])), min_price,
                min_volume)
    dates = pd.Series([c.index[-1].date() for c in data.values() if len(c)])
    return hits, {"requested": len(symbols), "fetched": len(data),
                  "as_of": dates.mode().iat[0] if len(dates) else None, "errors": errors}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Scan daily closes for setups.")
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--universe", action="store_true", help="every US stock and plain ETF")
    ap.add_argument("--no-etfs", action="store_true")
    ap.add_argument("--min-price", type=float, default=5.0)
    ap.add_argument("--min-volume", type=float, default=1e6, help="20-day avg shares a day")
    ap.add_argument("--show", type=int, default=40)
    a = ap.parse_args()
    syms = universe(not a.no_etfs) if a.universe else [s.upper() for s in a.symbols]
    if not syms:
        ap.error("give symbols or --universe")
    hits, n = run(syms, a.min_price, a.min_volume)
    print(f"Fetched {n['fetched']} of {n['requested']} charts, closes through {n['as_of']}; "
          f"{len(hits)} matched a rule.")
    for e in n["errors"][:5]:
        print("  failed:", e)
    with pd.option_context("display.width", 140, "display.max_colwidth", 50):
        print(hits.head(a.show).to_string(index=False))
    print("A scan lists charts that match a rule, not stocks to buy.")
