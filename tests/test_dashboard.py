"""Renders the real dashboard against DATABASE_URL. Only reads and unlocks; never clicks writes."""

import os

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")

PAGES = "sheep/dashboard/pages/"


def app(monkeypatch, password="1234"):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("DASHBOARD_PASSWORD", password)
    return AppTest.from_file("../streamlit_app.py", default_timeout=60).run()


def unlock(at, password):
    at.sidebar.text_input[0].input(password)
    next(b for b in at.button if b.label == "Unlock").click().run()
    return at


def labels(at):
    return [b.label for b in at.button]


def test_every_page_renders_for_every_profile(monkeypatch):
    at = app(monkeypatch)
    assert not at.exception
    for name in at.sidebar.selectbox[0].options:
        at.sidebar.selectbox[0].select(name).run()
        for page in ("overview", "journal", "rules", "scanner", "timemachine", "backtest",
                     "manage"):
            at.switch_page(f"{PAGES}{page}.py").run()
            assert not at.exception, (name, page)


def test_controls_locked_by_default(monkeypatch):
    at = app(monkeypatch)
    for page in ("rules", "backtest", "manage"):
        at.switch_page(f"{PAGES}{page}.py").run()
        assert any("Unlock controls" in i.value for i in at.info), page
        assert "Save version" not in labels(at)
        assert not any(label.startswith(("Run Trader", "Run backtest")) for label in labels(at))


def test_wrong_password_stays_locked(monkeypatch):
    at = unlock(app(monkeypatch), "0000")
    assert any("Wrong password" in e.value for e in at.sidebar.error)
    at.switch_page(f"{PAGES}manage.py").run()
    assert not any(label.startswith("Run Trader") for label in labels(at))


def test_right_password_unlocks_controls(monkeypatch):
    at = unlock(app(monkeypatch), "1234")
    at.switch_page(f"{PAGES}manage.py").run()
    assert not at.exception
    assert any(label.startswith("Run Trader") for label in labels(at))
    at.switch_page(f"{PAGES}backtest.py").run()
    assert "Run backtest" in labels(at)
    at.switch_page(f"{PAGES}rules.py").run()
    assert not at.exception
    assert {"Save version", "Switch"} <= set(labels(at))


def test_pages_render_with_trades(monkeypatch):
    """The live DB may be empty, so feed every page synthetic trades, equity and orders."""
    from datetime import UTC, date, datetime

    import pandas as pd

    from sheep.dashboard import core

    ts = lambda d: datetime(2026, 10, d, 20, tzinfo=UTC)  # noqa: E731
    trade = dict(symbol="XLV", setup="breakout", entry_price=180.0, qty=0.2, stop=175.0,
                 target=190.0, rule_version="v1", current_stop=None)
    fake = {
        "profiles": pd.DataFrame([dict(
            id=1, name="demo", mode="paper", broker="paper", strategy_version="v1",
            starting_capital=100.0,
            config_json={"watchlist": ["XLV", "XLE"], "risk": {
                "risk_per_trade_pct": 1.0, "max_open_positions": 3,
                "weekly_loss_limit_pct": 3.0}},
        )]),
        "versions": pd.DataFrame([dict(version="v1", params_json={})]),
        "equity": pd.DataFrame([dict(date=date(2026, 10, d), cash=60.0, equity=100.0 + d / 10)
                                for d in (1, 2, 3)]),
        "trades": pd.DataFrame([
            trade | dict(id=1, entry_ts=ts(1), exit_ts=ts(5), exit_price=190.0, pnl=2.0,
                         exit_reason="target"),
            trade | dict(id=2, entry_ts=ts(6), exit_ts=ts(8), exit_price=175.0, pnl=-1.0,
                         exit_reason="stop"),
            trade | dict(id=3, symbol="XLE", entry_ts=ts(9), exit_ts=None, exit_price=None,
                         pnl=None, exit_reason=None, current_stop=177.0),
        ]),
        "orders": pd.DataFrame([dict(id=9, symbol="XLE", side="sell", type="stop", qty=0.2,
                                     limit_price=None, stop_price=177.0, created_at=ts(9))]),
    }
    monkeypatch.setattr(core, "load", lambda profile=None: fake)

    at = app(monkeypatch)
    for page in ("overview", "journal"):
        at.switch_page(f"{PAGES}{page}.py").run()
        assert not at.exception, page
        assert len(at.dataframe) >= 1, page
    assert at.metric[0].value == "2"  # journal: 2 closed trades
    assert all(m.proto.help for m in at.metric)  # every metric has a hover explanation


def test_options_page_renders_chain(monkeypatch):
    import pandas as pd

    from sheep.dashboard import core

    def fake_chain(symbol, expiry):
        rows = [dict(strike=k, lastPrice=1.0, bid=0.9, ask=1.1, volume=10, openInterest=100,
                     impliedVolatility=0.2, inTheMoney=k < 500) for k in (400, 495, 505, 600)]
        return pd.DataFrame(rows), pd.DataFrame(rows), 500.0

    monkeypatch.setattr(core, "option_expiries", lambda s: ["2099-01-15"])
    monkeypatch.setattr(core, "option_chain", fake_chain)
    at = app(monkeypatch)
    at.switch_page(f"{PAGES}options.py").run()
    assert not at.exception
    calls = at.dataframe[0].value
    assert list(calls["strike"]) == [495, 505]  # ±10% window around $500 by default
    at.text_input[0].input("^GSPC").run()
    assert any("isn't a stock or ETF" in e.value for e in at.error)
