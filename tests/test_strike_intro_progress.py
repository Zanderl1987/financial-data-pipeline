"""Run-progress channel for the live study page."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strike_intro import progress


def test_stages_and_payloads_round_trip(tmp_path):
    p = progress.RunProgress(str(tmp_path))
    p.stage("excess_model", 3, 100, "fitting AAPL")
    p.put("loso_coefs", [{"symbol": "AAPL", "headroom": -2.1}])
    got = progress.read_progress(str(tmp_path))
    assert got["stages"]["excess_model"] == {"done": 3, "total": 100, "note": "fitting AAPL"}
    assert got["data"]["loso_coefs"][0]["symbol"] == "AAPL"


def test_read_before_any_write_is_empty(tmp_path):
    assert progress.read_progress(str(tmp_path)) == {"stages": {}, "data": {}, "updated_at": None}
