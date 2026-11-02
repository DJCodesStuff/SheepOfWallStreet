import os

import pytest


def test_dashboard_renders_every_profile():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("../streamlit_app.py", default_timeout=60).run()
    assert not at.exception
    for name in at.sidebar.selectbox[0].options:
        at.sidebar.selectbox[0].select(name).run()
        assert not at.exception, name
        assert at.title[0].value == name
