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
    assert dd.account_status(5, 0.0, BAND, True) == "stopped (hit your loss limit)"


def test_status_after_63_days():
    # h=63 row: p5=-0.63, p95=0.63; ret 0.5 is inside it (would be above at h<50)
    assert dd.account_status(80, 0.5, BAND, False) == "on track"
    assert dd.account_status(80, -0.7, BAND, False) == "behind"


def test_missed_days():
    days = [date(2026, 12, d) for d in (1, 2, 3, 4, 7, 8, 9, 10)]
    rows = [{"date": "2026-12-01", "account": "a", "status": "ok"},
            {"date": "2026-12-02", "account": "a", "status": "stale_data"},
            {"date": "2026-12-03", "account": "a", "status": "stopped"},
            {"date": "2026-12-04", "account": "a", "status": "dry_run"},
            {"date": "2026-12-07", "account": "b", "status": "ok"},
            {"date": "2026-12-07", "account": "a", "status": "error"},
            {"date": "2026-12-08", "account": "a", "status": "stale_data"},
            {"date": "2026-12-08", "account": "a", "status": "ok"},   # later run traded
            {"date": "2026-12-09", "account": "a", "status": "stale_data"},
            {"date": "2026-12-09", "account": "a", "status": "missed_cutoff"}]
    assert dd.missed_days(days, rows, "a") == [
        (date(2026, 12, 2), "stale_data"), (date(2026, 12, 4), "dry_run"),
        (date(2026, 12, 7), "error"), (date(2026, 12, 9), "missed_cutoff"),
        (date(2026, 12, 10), "no record")]


def test_verdict():
    assert dd.verdict(-0.7, (-0.63, 0.63)) == "FAIL"
    assert dd.verdict(0.0, (-0.63, 0.63)) == "PASS"
    assert dd.verdict(0.7, (-0.63, 0.63)) == "PASS (flagged: above range)"


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


def test_verdict_equity_fee_frozen_after_d63():
    days = _days(80)
    he = [5000.0] * 80
    d = days[0]
    lots = [("A", 36500.0, days[10], days[70]),    # closed after d63: capped at d63
            ("B", 36500.0, days[70], days[75]),    # opened after d63: ignored
            ("C", 36500.0, days[10], None)]        # still open: accrues to d63
    # A and C each accrue 52 days x 2.7 = 140.4
    assert dd.verdict_equity(days[:80], days, he, lots) == pytest.approx(5000.0 - 2 * 140.4)
    assert dd.verdict_equity(days[:63], days, he, lots) == pytest.approx(5000.0 - 2 * 140.4)


def test_dashboard_import_chain_needs_no_analytics_stack():
    """Streamlit Cloud installs only paper/requirements.txt: no duckdb, no scipy."""
    import subprocess
    import sys
    from pathlib import Path
    code = ("import sys; sys.modules['duckdb']=None; sys.modules['scipy']=None; "
            "import paper.dashboard_data, paper.decide, paper.broker")
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run([sys.executable, "-c", code], cwd=root,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]


# ---- I4: verdict order per spec 5 (loss limit is its own outcome) ----------

HIT = "HIT YOUR LOSS LIMIT"


def _fv(n_days, he, start=5000.0, lots=(), stops=(), band=BAND, hist_days=None):
    days = _days(n_days)
    ht = hist_days or _days(len(he))
    return dd.final_verdict(days, ht, he, list(lots), start, band, list(stops))


def test_pending_until_day_63_unless_limit_hit():
    assert _fv(62, [5000.0] * 62) is None
    r = _fv(63, [5000.0] * 63)
    assert r["verdict"] == "PASS" and r["day"] == 63 and r["return"] == 0.0


def test_close_breach_is_its_own_verdict_and_final():
    he = [5000.0] * 80
    he[9] = 4000.0                   # day 10 close at the limit, then recovers
    for n in (10, 40, 63, 80):
        r = _fv(n, he)
        assert r["verdict"] == HIT
        assert (r["day"], r["date"]) == (10, _days(10)[9])
        assert r["return"] == pytest.approx(-0.20)
        assert r["band"] == pytest.approx((-0.10, 0.10))
        assert r["vs_band"] == "below range"


def test_breach_inside_band_reports_where_it_stood():
    he = [10000.0] * 63
    he[19] = 9000.0                  # 10k account: -10% on day 20, band p5 = -20%
    r = _fv(63, he, start=10000.0)
    assert r["verdict"] == HIT and r["vs_band"] == "inside range"


def test_frozen_day_63_close_at_limit_is_hit_not_pass():
    he = [10000.0] * 62 + [9000.0]   # -10%: inside the 10k band, but the limit
    assert _fv(63, he, start=10000.0)["verdict"] == HIT


def test_breach_counts_borrow_fee():
    # 36500 short notional accrues 2.7/day; raw 4010 on day 6 (5 days) -> 3996.5
    he = [5000.0] * 63
    he[5] = 4010.0
    lots = [("S", 36500.0, _days(1)[0], None)]
    r = _fv(63, he, lots=lots)
    assert r["verdict"] == HIT and r["day"] == 6
    assert r["return"] == pytest.approx((4010.0 - 13.5 - 5000.0) / 5000.0)


def test_stop_order_on_or_before_day_63_is_a_hit():
    # intraday stop on day 20; no close ever breached
    he = [5000.0] * 70
    r = _fv(70, he, stops=[_days(20)[19]])
    assert r["verdict"] == HIT and r["day"] == 20 and r["return"] == 0.0
    assert r["vs_band"] == "inside range"
    # still running (day 30): already final
    assert _fv(30, he, stops=[_days(20)[19]])["verdict"] == HIT


def test_nothing_after_day_63_changes_the_verdict():
    he = [5000.0] * 63 + [3000.0] * 17          # crash after day 63
    r = _fv(80, he, stops=[_days(70)[69]])      # and a stop on day 70
    assert r["verdict"] == "PASS" and r["day"] == 63


def test_frozen_return_judged_against_day_63_band():
    assert _fv(63, [5000.0] * 62 + [5000.0 * 1.70])["verdict"] ==         "PASS (flagged: above range)"
    r = _fv(63, [5000.0] * 62 + [5000.0 * 0.40], start=5000.0,
            band={**BAND, "p5": [-0.9] * 63})
    assert r["verdict"] == HIT                  # -60% is past the $1,000 limit
    wide = {**BAND, "p5": [-0.01] * 63, "p95": [0.01] * 63}
    r = _fv(63, [10000.0] * 62 + [9500.0], start=10000.0, band=wide)
    assert r["verdict"] == "FAIL" and r["vs_band"] == "below range"


def test_incomplete_today_point_is_not_a_close():
    # history has an intraday point for an incomplete day 31 below the limit
    he = [5000.0] * 30 + [3900.0]
    assert _fv(30, he) is None


def test_describe_verdict():
    he = [5000.0] * 80
    he[9] = 4000.0
    s = dd.describe_verdict(_fv(80, he))
    assert s == "day 10 (2026-12-10): -20.00%, below range [-10.00%, +10.00%]"
    assert dd.describe_verdict(None) == ""


# ---- M-c: portfolio-history timestamps dated in New York -------------------

def test_history_dates_utc_midnight_and_ny_midnight():
    from datetime import timezone
    def ts(*a):
        return int(datetime(*a, tzinfo=timezone.utc).timestamp())
    got = dd.history_dates([ts(2026, 12, 1, 0, 0),     # 00:00 UTC = 19:00 ET Nov 30
                            ts(2026, 12, 2, 5, 0),     # 00:00 EST
                            ts(2026, 12, 3, 14, 30),   # 09:30 EST
                            ts(2026, 12, 3, 21, 0),    # 16:00 EST
                            ts(2026, 6, 1, 4, 0),      # 00:00 EDT
                            ts(2026, 6, 2, 0, 0)])     # 00:00 UTC = 20:00 EDT Jun 1
    assert got == [date(2026, 12, 1), date(2026, 12, 2), date(2026, 12, 3),
                   date(2026, 12, 3), date(2026, 6, 1), date(2026, 6, 2)]


def test_day_63_from_utc_midnight_history():
    from datetime import timezone
    days = _days(80)
    ts = [int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
          for d in days]
    he = [5000.0 + i for i in range(80)]
    # dated naively in NY, each point slides back a day and day 64's close
    # would be read as day 63's
    assert dd.verdict_equity(days, dd.history_dates(ts), he, []) == 5062.0
