"""Progress math behind the live Massive backfill dashboard (no network)."""
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import massive_backfill_progress as mbp

DAYS = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]


def _state(root, symbol, last):
    d = root / "state" / "massive_listings"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{symbol}.json").write_text(json.dumps({"last_date": last}), encoding="utf-8")


def test_progress_counts_days_up_to_checkpoint(tmp_path):
    _state(tmp_path, "AAPL", "2026-09-04")
    _state(tmp_path, "MSFT", "2026-09-02")
    p = mbp.load_progress(str(tmp_path), ["AAPL", "MSFT", "NVDA"], DAYS).set_index("symbol")
    assert p.loc["AAPL", "days_done"] == 4 and p.loc["AAPL", "status"] == "done"
    assert p.loc["MSFT", "days_done"] == 2 and p.loc["MSFT", "status"] == "in progress"
    assert p.loc["NVDA", "days_done"] == 0 and p.loc["NVDA", "status"] == "queued"
    assert p.loc["MSFT", "pct"] == 50.0


def test_eta_from_observed_rate():
    progress = pd.DataFrame({"days_done": [4, 2, 0], "days_total": [4, 4, 4]})
    started = pd.Timestamp("2026-10-01 00:00")
    now = pd.Timestamp("2026-10-01 06:00")          # 6 days done in 6h -> 1 day/h
    eta = mbp.estimate_eta(progress, started, now)
    assert eta["remaining_days"] == 6
    assert eta["hours_left"] == 6.0


def test_eta_is_none_before_any_progress():
    progress = pd.DataFrame({"days_done": [0], "days_total": [4]})
    eta = mbp.estimate_eta(progress, pd.Timestamp("2026-10-01"), pd.Timestamp("2026-10-01 01:00"))
    assert eta["hours_left"] is None


def test_dashboard_renders_progress_and_charts(tmp_path, monkeypatch):
    """The live dashboard runs end to end on synthetic backfill output."""
    import pytest
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    import massive_option_listings_pipeline as mol

    def _c(t, strike):
        return {"ticker": t, "strike_price": strike, "expiration_date": "2027-01-15",
                "contract_type": "call", "shares_per_contract": 100}
    lists = {"2026-09-01": ["O:AAPL270115C00200000"],
             "2026-09-02": ["O:AAPL270115C00200000", "O:AAPL270115C00250000"]}
    fetch = lambda client, sym, d: mol.standard_frame(
        [_c(t, float(t[-8:]) / 1000) for t in lists[d]], sym)
    mol.advance_symbol(None, "AAPL", list(lists), str(tmp_path), fetch=fetch)
    universe = tmp_path / "u.csv"
    pd.DataFrame({"symbol": ["AAPL", "MSFT"]}).to_csv(universe, index=False)
    monkeypatch.setenv("MASSIVE_PROGRESS_ROOT", str(tmp_path))
    monkeypatch.setenv("MASSIVE_PROGRESS_UNIVERSE", str(universe))

    app = AppTest.from_file(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "scripts", "massive_backfill_dashboard.py"),
        default_timeout=120).run()
    assert not app.exception, app.exception
    labels = [m.label for m in app.metric]
    assert "Symbols complete" in labels
    assert app.selectbox[0].options == ["AAPL"]
