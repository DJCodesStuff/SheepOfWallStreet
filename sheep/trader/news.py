"""News monitor: Yahoo Finance headlines and SEC 8-K filings per ticker, tagged by what they touch.

Adapted from trader-news in day-trader-skill (MIT, Jake Schincariol,
github.com/Jakeschincariol/day-trader-skill). Kept: the feeds, the tags, the 8-K item labels and
the important-item flags. Left out: the seen-items state file (this lists the last `hours`
instead) and the intraday price check (the dashboard shows the last close's move).

Headlines are leads, not facts: read the story or the filing before acting on one. If an 8-K
and a headline disagree, the 8-K is what the company told the SEC. ETFs have no 8-Ks.

SEC asks every client to identify itself: set SEC_USER_AGENT="Your Name you@example.com" in
.env, or filings are skipped with a note.

    uv run --env-file .env python -m sheep.trader.news SPY XLF JPM [--hours 48]
"""

from __future__ import annotations

import email.utils
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from functools import cache

import pandas as pd

from sheep.trader.data import NY
from sheep.trader.scanner import _get

YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"
SEC_FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form}"
            "&dateb=&owner=include&count={count}&output=atom")
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
PAUSE = 0.15  # seconds between requests; SEC allows 10 a second, stay far below
MOVE_PCT = 3.0  # a last close this far from the one before is flagged as moving

ITEMS_8K = {
    "1.01": "material agreement", "1.02": "agreement ended", "1.03": "bankruptcy",
    "1.05": "cybersecurity incident", "2.01": "acquisition or sale of assets",
    "2.02": "earnings results", "2.03": "new debt", "2.04": "debt acceleration",
    "2.05": "restructuring costs", "2.06": "impairment", "3.01": "delisting notice",
    "3.02": "unregistered share sale", "3.03": "shareholder rights change",
    "4.01": "auditor change", "4.02": "past financials no longer reliable",
    "5.01": "change in control", "5.02": "director or officer change",
    "5.03": "bylaws or fiscal year change", "5.07": "shareholder vote results",
    "7.01": "Regulation FD disclosure", "8.01": "other events", "9.01": "exhibits",
}
IMPORTANT_ITEMS = {"1.03", "1.05", "2.02", "2.04", "2.06", "3.01", "4.02", "5.01", "5.02"}
KEYWORDS = [(tag, re.compile(rx, re.I)) for tag, rx in [
    ("earnings", r"\bearnings\b|\bresults\b|\bEPS\b|\bguidance\b|\bforecast\b"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|price target|\boutperform\b"
                r"|\bunderperform\b"),
    ("legal", r"\blawsuit\b|\bsued\b|\bprobe\b|\binvestigation\b|\bSEC charges\b|\bantitrust\b"
              r"|\bjudge\b|\bcourt\b"),
    ("deal", r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|\bbuyout\b|\btakeover\b|\bdeal\b"),
    ("capital", r"\bdividend\b|\bbuyback\b|\brepurchase\b|\bstock split\b|\boffering\b"),
    ("people", r"\bCEO\b|\bCFO\b|\bresign|\bsteps down\b|\bappoint"),
    ("risk", r"\brecall\b|\bbankrupt|\bdefault\b|\bdelist|\bhack|\bbreach\b|\boutage\b"),
    ("halt", r"\bhalt(?:s|ed)?\b|\btrading (?:pause|suspen)"),
    ("regulatory", r"\bFDA\b|\bapproval\b|\bapproves\b|\bclinical\b|\bFTC\b|\bDOJ\b"),
    ("macro", r"\bFed\b|\bFOMC\b|\bCPI\b|\binflation\b|\bjobs report\b|\bpayrolls\b"
              r"|\btariffs?\b|\bfutures\b"),
]]
COLUMNS = ["time", "ticker", "source", "title", "tags", "important", "link"]


def sec_user_agent() -> str | None:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    return ua if "@" in ua and "example.com" not in ua else None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(el: ET.Element, name: str) -> str:
    return next(((c.text or "").strip() for c in el.iter() if _local(c.tag) == name), "")


def parse_time(value: str) -> datetime | None:
    """RFC 822 (RSS) or ISO 8601 (Atom) -> aware UTC datetime, or None."""
    value = (value or "").strip()
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).astimezone(UTC)


def parse_rss(data: bytes, ticker: str) -> list[dict]:
    return [{"ticker": ticker, "source": "Yahoo Finance", "kind": "headline",
             "title": _text(it, "title"), "summary": _text(it, "description"),
             "link": _text(it, "link"), "time": parse_time(_text(it, "pubDate")), "items": []}
            for it in ET.fromstring(data).iter("item")]


def parse_sec_atom(data: bytes, ticker: str) -> list[dict]:
    root = ET.fromstring(data)
    company = _text(root, "conformed-name") or ticker
    out = []
    for e in (x for x in root.iter() if _local(x.tag) == "entry"):
        items = re.findall(r"\d+\.\d+", _text(e, "items-desc"))
        labels = [ITEMS_8K.get(i, "item " + i) for i in items if i != "9.01"]
        link = next((c.get("href", "") for c in e.iter() if _local(c.tag) == "link"), "")
        title = f"{company} filed a {_text(e, 'filing-type')}"
        out.append({"ticker": ticker, "source": "SEC EDGAR", "kind": "filing",
                    "title": title + (": " + ", ".join(labels) if labels else ""),
                    "summary": _text(e, "form-name"), "link": link,
                    "time": parse_time(_text(e, "updated")), "items": items})
    return out


def tag(it: dict) -> dict:
    """Add `tags` (what kind of news) and `important` (a material 8-K item)."""
    hay = f"{it['title']} {it.get('summary') or ''}"
    tags = [t for t, rx in KEYWORDS if rx.search(hay)]
    if "2.02" in it["items"] and "earnings" not in tags:
        tags.insert(0, "earnings")
    return it | {"tags": tags, "important": bool(set(it["items"]) & IMPORTANT_ITEMS)}


@cache
def cik_map(ua: str) -> dict[str, str]:
    """Ticker (Yahoo form, BRK-B) -> 10-digit CIK, from SEC's company_tickers.json."""
    data = json.loads(_get(SEC_TICKERS, ua))
    return {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in data.values()}


def collect(tickers: list[str], hours: float = 48, sec_ua: str | None = None,
            forms: str = "8-K", now: datetime | None = None) -> tuple[pd.DataFrame, list[str]]:
    """(items newest first, notes). One request per feed per ticker, with a pause between."""
    now = now or datetime.now(UTC)
    items, notes = [], []
    if sec_ua is None:
        notes.append("SEC filings skipped: set SEC_USER_AGENT=\"Your Name you@example.com\"")
    for t in tickers:
        try:
            items += parse_rss(_get(YAHOO_RSS.format(sym=t)), t)
        except (OSError, ET.ParseError) as e:
            notes.append(f"headlines for {t}: {e}")
        time.sleep(PAUSE)
        if sec_ua is None:
            continue
        try:
            cik = cik_map(sec_ua).get(t)
            if cik:  # ETFs and funds usually have none
                items += parse_sec_atom(_get(SEC_FEED.format(cik=cik, form=forms, count=10),
                                                 sec_ua), t)
        except (OSError, ValueError, ET.ParseError) as e:
            notes.append(f"filings for {t}: {e}")
        time.sleep(PAUSE)
    cutoff = now - timedelta(hours=hours)
    df = pd.DataFrame([tag(i) for i in items if i["time"] and i["time"] >= cutoff],
                      columns=COLUMNS + ["kind", "summary", "items"])
    if df.empty:
        return df[COLUMNS], notes
    # One row per story: a headline in several tickers' feeds lists them all.
    df = (df.assign(key=df["link"].where(df["link"] != "", df["title"]))
            .groupby("key", sort=False)
            .agg({"time": "first", "ticker": lambda s: ", ".join(sorted(set(s))),
                  "source": "first", "title": "first", "link": "first", "tags": "first",
                  "important": "first"}))
    return df.sort_values("time", ascending=False)[COLUMNS].reset_index(drop=True), notes


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Headlines and 8-Ks for some tickers.")
    ap.add_argument("tickers", nargs="+")
    ap.add_argument("--hours", type=float, default=48)
    a = ap.parse_args()
    df, notes = collect([t.upper() for t in a.tickers], a.hours, sec_user_agent())
    print(*notes, sep="\n")
    for r in df.itertuples():
        flag = "!" if r.important else " "
        when = r.time.astimezone(NY)
        print(f"{flag} {when:%a %d %b %H:%M} {r.ticker:10} {r.title}"
              + (f"  [{', '.join(r.tags)}]" if r.tags else ""))
        print(f"    {r.link}")
    print(f"{len(df)} items in the last {a.hours:g} hours. Headlines are leads, not facts.")
