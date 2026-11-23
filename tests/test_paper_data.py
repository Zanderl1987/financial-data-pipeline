from datetime import date

import numpy as np
import pandas as pd
import pytest

import paper.data as pdata

TODAY = date(2026, 11, 23)
COLS = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]


def _frame(days):
    idx = pd.DatetimeIndex(pd.to_datetime(days), name="Date")
    return pd.DataFrame(1.0, index=idx, columns=COLS)


def _multi(frames):
    return pd.concat(frames, axis=1)


@pytest.fixture(autouse=True)
def _nosleep(monkeypatch):
    monkeypatch.setattr(pdata.time, "sleep", lambda s: None)


def test_drops_todays_partial_bar(monkeypatch):
    f = _frame(["2026-11-19", "2026-11-20", "2026-11-23"])
    raw = _multi({"AAA": f})
    monkeypatch.setattr(pdata.yf, "download", lambda *a, **k: raw)
    out = pdata.fetch_daily_bars(["AAA", "BBB"], date(2026, 1, 1), TODAY, today=TODAY)
    df = out["AAA"]
    assert df.index[-1] == pd.Timestamp("2026-11-20")
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.tz is None
    assert "BBB" not in out


def test_single_symbol_shape(monkeypatch):
    raw = _frame(["2026-11-19", "2026-11-20"])
    monkeypatch.setattr(pdata.yf, "download", lambda *a, **k: raw)
    out = pdata.fetch_daily_bars(["AAA"], date(2026, 1, 1), TODAY, today=TODAY)
    assert list(out["AAA"].columns) == ["open", "high", "low", "close", "volume"]
    assert len(out["AAA"]) == 2


def test_batches_and_omits_empty(monkeypatch):
    syms = [f"S{i}" for i in range(450)]
    calls = []

    def fake(batch_syms, **k):
        calls.append(list(batch_syms))
        good = _frame(["2026-11-19", "2026-11-20"])
        bad = good.copy()
        bad[:] = np.nan
        return _multi({s: (bad if s == "S5" else good) for s in batch_syms})

    monkeypatch.setattr(pdata.yf, "download", fake)
    out = pdata.fetch_daily_bars(syms, date(2026, 1, 1), TODAY, today=TODAY)
    assert len(calls) == 3
    assert "S5" not in out
    assert len(out) == 449
