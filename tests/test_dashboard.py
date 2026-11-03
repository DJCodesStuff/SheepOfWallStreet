"""Renders the real dashboard against DATABASE_URL. Only reads and unlocks; never clicks writes."""

import os

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


def app(monkeypatch, password="1234"):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("DASHBOARD_PASSWORD", password)
    return AppTest.from_file("../streamlit_app.py", default_timeout=60).run()


def labels(at):
    return [b.label for b in at.button]


def test_renders_every_profile_locked(monkeypatch):
    at = app(monkeypatch)
    assert not at.exception
    for name in at.sidebar.selectbox[0].options:
        at.sidebar.selectbox[0].select(name).run()
        assert not at.exception, name
        assert at.title[0].value == name
        assert not any(label.startswith(("Run Trader", "Reset")) for label in labels(at))


def test_wrong_password_stays_locked(monkeypatch):
    at = app(monkeypatch)
    at.sidebar.text_input[0].input("0000")
    at.button[0].click().run()
    assert any("Wrong password" in e.value for e in at.sidebar.error)
    assert not any(label.startswith("Run Trader") for label in labels(at))


def test_right_password_unlocks_controls(monkeypatch):
    at = app(monkeypatch)
    at.sidebar.text_input[0].input("1234")
    at.button[0].click().run()
    assert not at.exception
    assert any(label.startswith("Run Trader") for label in labels(at))
    assert "Run walk-forward" in labels(at)
