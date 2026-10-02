"""The live study page renders from a progress.json mid-run."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_page_renders_stages_coefficients_and_null(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    from strike_intro.progress import RunProgress
    run = tmp_path / "20261002_120000"
    p = RunProgress(str(run))
    p.stage("excess_model", 2, 10, "MSFT")
    p.put("loso_coefs", [{"symbol": "AAPL", "const": 0.1, "headroom": -5.0},
                         {"symbol": "MSFT", "const": 0.2, "headroom": -4.5}])
    p.put("perm_null", {"label": "day|ret_21|all", "observed": 0.01, "sample": [0.0, 0.001, -0.002]})
    monkeypatch.setenv("STRIKE_INTRO_RUNS", str(tmp_path))
    app = AppTest.from_file(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "scripts", "strike_intro_study_dashboard.py"), default_timeout=60).run()
    assert not app.exception, app.exception
    assert any("excess_model" in str(t.value) for t in [*app.markdown, *app.caption])
