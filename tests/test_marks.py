import pandas as pd
import pytest

from sheep.dashboard import core


def test_mark_values_open_trades_at_last_close(monkeypatch):
    idx = pd.bdate_range("2026-01-05", periods=4, name="date")
    monkeypatch.setattr(core, "load_bars", lambda s: pd.DataFrame({"close": [99, 100, 101, 104]},
                                                                  index=idx))
    t = pd.DataFrame([{"symbol": "SPY", "entry_ts": pd.Timestamp("2026-01-06 16:00", tz="UTC"),
                       "entry_price": 100.0, "qty": 0.5, "stop": 95.0, "current_stop": 98.0,
                       "target": 110.0}])
    m = core.mark.__wrapped__(t).iloc[0]
    assert m["sessions"] == 3  # Jan 6, 7, 8
    assert m["open_pnl"] == pytest.approx(2.0) and m["open_r"] == pytest.approx(0.8)
    assert m["if_stopped"] == pytest.approx(-1.0)  # trailing stop 98, not the original 95
    assert m["progress"] == pytest.approx(0.5)  # 104 is halfway from 98 to 110


def test_mark_leaves_row_blank_when_bars_fail(monkeypatch):
    monkeypatch.setattr(core, "load_bars", lambda s: (_ for _ in ()).throw(OSError("offline")))
    t = pd.DataFrame([{"symbol": "SPY", "entry_ts": pd.Timestamp("2026-01-06", tz="UTC"),
                       "entry_price": 100.0, "qty": 1.0, "stop": 95.0, "current_stop": None,
                       "target": 110.0}])
    assert core.mark.__wrapped__(t)["close"].isna().all()
