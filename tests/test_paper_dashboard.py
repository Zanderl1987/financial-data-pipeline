from datetime import date

import pandas as pd
import pytest

from paper import dashboard_data as dd

BAND = {"h": list(range(1, 64)),
        "p5": [-0.01 * h for h in range(1, 64)],
        "p50": [0.0] * 63,
        "p95": [0.01 * h for h in range(1, 64)]}


def test_status_bands():
    assert dd.account_status(5, -0.06, BAND, False) == "behind"
    assert dd.account_status(5, 0.0, BAND, False) == "on track"
    assert dd.account_status(5, 0.06, BAND, False) == "above"
    assert dd.account_status(5, 0.0, BAND, True) == "stopped"


def test_status_after_63_days():
    # h=63 row: p5=-0.63, p95=0.63; ret 0.5 is inside it (would be above at h<50)
    assert dd.account_status(80, 0.5, BAND, False) == "on track"
    assert dd.account_status(80, -0.7, BAND, False) == "behind"


def test_missed_days():
    days = [date(2026, 12, d) for d in (1, 2, 3, 4, 7)]
    rows = [{"date": "2026-12-01", "account": "a", "status": "ok"},
            {"date": "2026-12-02", "account": "a", "status": "stale_data"},
            {"date": "2026-12-03", "account": "a", "status": "stopped"},
            {"date": "2026-12-04", "account": "a", "status": "dry_run"},
            {"date": "2026-12-07", "account": "b", "status": "ok"},
            {"date": "2026-12-07", "account": "a", "status": "error"}]
    assert dd.missed_days(days, rows, "a") == [date(2026, 12, 4), date(2026, 12, 7)]


def test_verdict():
    assert dd.verdict(-0.7, (-0.63, 0.63), False) == "FAIL"
    assert dd.verdict(0.0, (-0.63, 0.63), False) == "PASS"
    assert dd.verdict(0.7, (-0.63, 0.63), False) == "PASS (flagged: above range)"
    assert dd.verdict(0.0, (-0.63, 0.63), True) == "FAIL"


def test_trade_cost_sign():
    fills = pd.DataFrame({"symbol": ["A", "B"], "side": ["buy", "sell"],
                          "date": [date(2026, 12, 1)] * 2,
                          "price": [10.01, 9.99], "qty": [10, 10]})
    closes = pd.DataFrame({"A": [10.0], "B": [10.0]}, index=[date(2026, 12, 1)])
    out = dd.trade_costs(fills, closes)
    assert out["cost_bps"].tolist() == pytest.approx([10.0, 10.0])


def test_parse_log_and_launch_secret():
    text = '{"date":"2026-12-01","account":"a","status":"ok"}\n\nbad\n'
    assert dd.parse_runlog(text) == [{"date": "2026-12-01", "account": "a", "status": "ok"}]
    assert dd.parse_launch("2026-12-01") == date(2026, 12, 1)
    assert dd.parse_launch(None) is None and dd.parse_launch("nope") is None


def test_day_index_and_fee_adjusted_equity():
    days = [date(2026, 12, d) for d in (1, 2, 3)]
    assert dd.day_index(days, date(2026, 12, 1), date(2026, 12, 2)) == 2
    assert dd.day_index(days, None, date(2026, 12, 2)) == 0
    # short of 36500 notional at 270bps/yr for 10 days -> 27.0
    assert dd.fee_adjusted(5000.0, [("S", 36500.0, date(2026, 12, 1), None)],
                           date(2026, 12, 11)) == pytest.approx(5000 - 27.0)


def test_limit_distance():
    assert dd.limit_distance(5000.0, 4200.0) == pytest.approx(200.0)


from datetime import datetime, time  # noqa: E402

LOTS = [("S", 36500.0, date(2026, 12, 1), None)]


def _days(n):
    return [date(2026, 12, 1) + pd.Timedelta(days=i).to_pytimedelta() for i in range(n)]


def test_verdict_equity_frozen():
    days = _days(80)
    ht = days
    he = [5000.0 + i for i in range(80)]
    assert dd.verdict_equity(days[:62], ht, he, []) is None
    v63 = dd.verdict_equity(days[:63], ht, he, [])
    assert v63 == 5062.0
    assert dd.verdict_equity(days[:80], ht, he, []) == v63
    # fee accrued only up to day 63 (62 days x 36500*270bps/365 per day = 2.7/day)
    assert dd.verdict_equity(days[:80], ht, he, LOTS) == pytest.approx(5062.0 - 62 * 2.7)


def test_net_position_pnl():
    pos = [{"symbol": "S", "side": "short", "pnl": 100.0},
           {"symbol": "L", "side": "long", "pnl": 50.0}]
    out = dd.net_position_pnl(pos, LOTS, date(2026, 12, 11))
    assert out[0]["pnl_net"] == pytest.approx(100.0 - 27.0)
    assert out[1]["pnl_net"] == 50.0


def test_scale_band_and_complete_days():
    b = dd.scale_band(BAND, 5000.0, 2)
    assert b["p95"] == pytest.approx([5050.0, 5100.0]) and b["p5"][0] == pytest.approx(4950.0)
    ds = [date(2026, 12, 1), date(2026, 12, 2)]
    early = datetime(2026, 12, 2, 12, 0)
    late = datetime(2026, 12, 2, 16, 30)
    assert dd.complete_days(ds, early) == ds[:1]
    assert dd.complete_days(ds, late) == ds
