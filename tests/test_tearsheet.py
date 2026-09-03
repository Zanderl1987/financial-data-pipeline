"""
tests/test_tearsheet.py -- W3 performance-analytics layer.

Known-answer tests wherever a closed form exists (a doubled series has beta 2;
a series against itself has beta 1 and zero alpha; a hand-built two-month
series has a computable YTD), plus the guards that matter: no inf Sharpe on a
flat window, no phantom recovery on a drawdown that never recovered, and the
realized-basis label that stops a trade curve being compared to a
mark-to-market one.
"""

import numpy as np
import pandas as pd
import pytest

from evaluation import tearsheet as ts


def _ret(values, start="2020-01-01"):
    return pd.Series(values, index=pd.bdate_range(start, periods=len(values)),
                     dtype=float)


def _noise(n=600, mu=0.0005, sd=0.01, seed=0):
    return _ret(np.random.default_rng(seed).normal(mu, sd, n))


# --------------------------------------------------------------- equity


class TestEquity:
    def test_compounds(self):
        eq = ts.to_equity(_ret([0.1, 0.1]))
        assert eq.iloc[-1] == pytest.approx(1.21)

    def test_empty_input(self):
        assert ts.to_equity(pd.Series(dtype=float)).empty


# --------------------------------------------------------------- trade bridge


def _trades(exit_dates, pnls):
    return pd.DataFrame({"exit_date": pd.to_datetime(exit_dates),
                         "pnl_dollars": pnls})


class TestTradeBridge:
    def test_realized_basis_is_labeled(self):
        out = ts.daily_returns_from_trades(
            _trades(["2020-01-06", "2020-01-08"], [500.0, -200.0]))
        assert out["basis"] == "realized"

    def test_pnl_lands_on_exit_dates(self):
        out = ts.daily_returns_from_trades(
            _trades(["2020-01-06", "2020-01-08"], [1000.0, -500.0]),
            starting_equity=100_000.0)
        r = out["returns"]
        assert out["final_equity"] == pytest.approx(100_500.0)
        assert r.loc["2020-01-07"] == pytest.approx(0.0)
        assert r.loc["2020-01-08"] < 0

    def test_multiple_trades_same_day_are_summed(self):
        out = ts.daily_returns_from_trades(
            _trades(["2020-01-06", "2020-01-06", "2020-01-08"],
                    [300.0, 200.0, 100.0]))
        assert out["final_equity"] == pytest.approx(100_600.0)

    def test_business_day_calendar_fills_gaps_with_zero(self):
        out = ts.daily_returns_from_trades(
            _trades(["2020-01-06", "2020-01-31"], [500.0, 500.0]))
        assert out["n_days"] == len(pd.bdate_range("2020-01-06", "2020-01-31"))
        assert (out["returns"] == 0.0).sum() > 15

    def test_empty_reason(self):
        out = ts.daily_returns_from_trades(pd.DataFrame())
        assert out["returns"] is None
        assert "no realized trades" in out["returns_reason"]

    def test_missing_columns_reason(self):
        out = ts.daily_returns_from_trades(pd.DataFrame({"foo": [1]}))
        assert out["returns"] is None
        assert "missing columns" in out["returns_reason"]

    def test_blown_up_equity_reason(self):
        out = ts.daily_returns_from_trades(
            _trades(["2020-01-06"], [-500_000.0]), starting_equity=100_000.0)
        assert out["returns"] is None
        assert "equity hit zero" in out["returns_reason"]

    def test_realized_drawdown_understates_mark_to_market(self):
        """The caveat the docstring makes, made executable: a position that goes
        deeply underwater and recovers before closing leaves NO trace in the
        realized curve. Nothing here is wrong -- but it is why the basis label
        exists, and why these numbers must not be compared to backtest.py's."""
        realized = ts.daily_returns_from_trades(
            _trades(["2020-01-06", "2020-02-03", "2020-03-02"],
                    [100.0, 100.0, 100.0]), starting_equity=100_000.0)
        assert ts.headline_metrics(realized["returns"])["max_drawdown_pct"] == 0.0


# --------------------------------------------------------------- monthly


class TestMonthly:
    def test_known_answer(self):
        """Jan +10% then Feb +10% compounds to a 21% YTD."""
        r = pd.Series(
            [0.10, 0.10],
            index=pd.to_datetime(["2020-01-15", "2020-02-14"]))
        out = ts.monthly_returns_table(r)
        assert out["table"].loc[2020, "Jan"] == pytest.approx(10.0)
        assert out["table"].loc[2020, "Feb"] == pytest.approx(10.0)
        assert out["table"].loc[2020, "YTD"] == pytest.approx(21.0)

    def test_all_twelve_month_columns_present(self):
        out = ts.monthly_returns_table(_noise(600))
        assert list(out["table"].columns) == ts.MONTH_NAMES + ["YTD"]

    def test_reports_real_month_count(self):
        """A short backtest spanning a year boundary must not look like a year:
        two months of data reports n_months == 2, not 12."""
        r = pd.Series([0.01, 0.01],
                      index=pd.to_datetime(["2020-12-15", "2021-01-15"]))
        out = ts.monthly_returns_table(r)
        assert out["n_months"] == 2
        assert len(out["table"]) == 2                # two year rows

    def test_summary_stats(self):
        out = ts.monthly_returns_table(_noise(600))
        assert out["worst_month_pct"] <= out["best_month_pct"]
        assert 0.0 <= out["pct_positive_months"] <= 100.0

    def test_empty_reason(self):
        out = ts.monthly_returns_table(pd.Series(dtype=float))
        assert out["table"] is None
        assert "no returns" in out["monthly_reason"]


# --------------------------------------------------------------- rolling


class TestRolling:
    def test_shape_and_columns(self):
        out = ts.rolling_metrics(_noise(400), window=63)
        assert list(out["frame"].columns) == [
            "rolling_sharpe", "rolling_sortino", "rolling_vol_pct"]
        assert out["n_windows"] > 0

    def test_flat_window_gives_nan_not_inf(self):
        """A zero-sd stretch must not produce an infinite Sharpe -- one inf
        value would dominate every chart it lands in."""
        r = _ret([0.0] * 100 + list(np.random.default_rng(0).normal(0, 0.01, 100)))
        out = ts.rolling_metrics(r, window=20)
        vals = out["frame"]["rolling_sharpe"]
        assert not np.isinf(vals.dropna()).any()
        assert vals.isna().any()

    def test_vol_is_annualized(self):
        """Constant-magnitude alternating returns have a known daily sd."""
        r = _ret([0.01, -0.01] * 200)
        out = ts.rolling_metrics(r, window=50)
        expected = 0.01 * np.sqrt(252) * 100 * np.sqrt(50 / 49)
        assert out["frame"]["rolling_vol_pct"].dropna().iloc[-1] == pytest.approx(
            expected, rel=0.01)

    def test_too_short_reason(self):
        out = ts.rolling_metrics(_noise(50), window=63)
        assert out["frame"] is None
        assert "only 50 days" in out["rolling_reason"]

    def test_bad_window_reason(self):
        out = ts.rolling_metrics(_noise(400), window=2)
        assert out["frame"] is None
        assert ">= 5" in out["rolling_reason"]


# --------------------------------------------------------------- drawdown


class TestDrawdown:
    def test_underwater_is_non_positive_and_zero_at_peaks(self):
        u = ts.drawdown_series(_ret([0.1, -0.05, 0.2]))
        assert (u <= 1e-12).all()
        assert u.iloc[0] == pytest.approx(0.0)

    def test_known_depth(self):
        """+10% then -20% is a 20% drawdown from the peak."""
        out = ts.drawdown_periods(_ret([0.10, -0.20, 0.01]))
        assert out["max_drawdown_pct"] == pytest.approx(-20.0, abs=0.01)

    def test_recovered_period_has_dates(self):
        out = ts.drawdown_periods(_ret([0.10, -0.05, 0.10]))
        row = out["table"].iloc[0]
        assert bool(row["recovered"]) is True
        assert row["recovery_date"] is not None
        assert row["days_to_recovery"] is not None

    def test_unrecovered_drawdown_is_not_silently_closed(self):
        """A drawdown still open at the end of the sample must report
        recovered=False and recovery_date=None -- closing it off at the last
        bar would invent a recovery that never happened."""
        out = ts.drawdown_periods(_ret([0.10, -0.30, 0.01, 0.01]))
        row = out["table"].iloc[0]
        assert bool(row["recovered"]) is False
        assert pd.isna(row["recovery_date"])
        assert pd.isna(row["days_to_recovery"])
        assert out["n_unrecovered"] == 1

    def test_column_types_are_stable_across_mixed_tables(self):
        """The bug real data found and synthetic data hid: left to pandas'
        inference, an all-unrecovered table keeps Python None in an object
        column while a MIXED table coerces to NaT. A downstream `is None` check
        then passes every synthetic test and renders "NaT" in production. Types
        are pinned so pd.isna works uniformly in both cases."""
        all_open = ts.drawdown_periods(_ret([0.10, -0.30, 0.01]))["table"]
        mixed = ts.drawdown_periods(
            _ret([0.10, -0.05, 0.10, 0.10, -0.30, 0.01]))["table"]
        assert bool(mixed["recovered"].any()) and not bool(mixed["recovered"].all())
        for t in (all_open, mixed):
            assert t["recovery_date"].dtype == "datetime64[ns]"
            assert str(t["days_to_recovery"].dtype) == "Int64"
            assert pd.isna(t.loc[~t["recovered"], "recovery_date"]).all()

    def test_monotonic_rise_has_no_drawdowns(self):
        out = ts.drawdown_periods(_ret([0.01] * 20))
        assert out["n_periods"] == 0
        assert out["max_drawdown_pct"] == 0.0
        assert out["table"].empty

    def test_deepest_first_and_top_n_respected(self):
        r = _ret([0.10, -0.02, 0.05, 0.10, -0.30, 0.40, 0.05, -0.01, 0.05])
        out = ts.drawdown_periods(r, top_n=2)
        assert len(out["table"]) == 2
        depths = out["table"]["depth_pct"].tolist()
        assert depths == sorted(depths)              # most negative first

    def test_matches_headline_max_drawdown(self):
        r = _noise(400)
        assert (ts.drawdown_periods(r)["max_drawdown_pct"]
                == pytest.approx(ts.headline_metrics(r)["max_drawdown_pct"],
                                 abs=0.02))

    def test_empty_reason(self):
        out = ts.drawdown_periods(pd.Series(dtype=float))
        assert out["table"] is None
        assert "no returns" in out["dd_reason"]


# --------------------------------------------------------------- benchmark


class TestBenchmark:
    def test_series_against_itself(self):
        b = _noise(400)
        out = ts.benchmark_stats(b, b)
        assert out["beta"] == pytest.approx(1.0, abs=1e-6)
        assert out["alpha_ann_pct"] == pytest.approx(0.0, abs=1e-6)
        assert out["correlation"] == pytest.approx(1.0, abs=1e-6)
        assert out["tracking_error_pct"] == pytest.approx(0.0, abs=1e-9)

    def test_doubled_series_has_beta_two(self):
        b = _noise(400)
        out = ts.benchmark_stats(b * 2.0, b)
        assert out["beta"] == pytest.approx(2.0, abs=1e-6)
        assert out["up_capture_pct"] == pytest.approx(200.0, abs=0.1)
        assert out["down_capture_pct"] == pytest.approx(200.0, abs=0.1)

    def test_constant_alpha_is_recovered(self):
        b = _noise(400)
        out = ts.benchmark_stats(b + 0.001, b)
        assert out["beta"] == pytest.approx(1.0, abs=1e-6)
        assert out["alpha_ann_pct"] == pytest.approx(0.001 * 252 * 100, abs=0.01)

    def test_market_neutral_series_has_near_zero_beta(self):
        b = _noise(600, seed=1)
        s = _noise(600, seed=2)
        out = ts.benchmark_stats(s, b)
        assert abs(out["beta"]) < 0.2

    def test_partial_overlap_is_visible(self):
        """Half a benchmark must report half the overlap, not be forward-filled
        into looking complete."""
        b = _noise(400, seed=1)
        out = ts.benchmark_stats(b, b.iloc[:200])
        assert out["n_overlap"] == 200

    def test_too_little_overlap_reason(self):
        b = _noise(400, seed=1)
        out = ts.benchmark_stats(b, b.iloc[:10])
        assert out["beta"] is None
        assert "overlapping days" in out["bench_reason"]

    def test_zero_variance_benchmark_reason(self):
        b = _noise(400, seed=1)
        flat = pd.Series(0.0, index=b.index)
        out = ts.benchmark_stats(b, flat)
        assert out["beta"] is None
        assert "zero benchmark variance" in out["bench_reason"]

    def test_empty_reason(self):
        out = ts.benchmark_stats(pd.Series(dtype=float), _noise(100))
        assert out["beta"] is None
        assert "empty" in out["bench_reason"]


# --------------------------------------------------------------- headline


class TestHeadline:
    def test_known_cagr_on_a_flat_year(self):
        r = _ret([0.0] * 252)
        out = ts.headline_metrics(r)
        assert out["cagr_pct"] == pytest.approx(0.0)
        assert out["max_drawdown_pct"] == pytest.approx(0.0)

    def test_constant_series_does_not_produce_a_float_noise_sharpe(self):
        """`sd > 0` is not a sufficient guard, and this is the case that proves
        it. A constant series of 0.001 has an arithmetically-zero sd, but in
        float64 it lands near 6e-19 -- positive, finite, and enough to yield a
        Sharpe of 2.4e16 that would render as a plausible huge number rather
        than as the degenerate input it is. SD_FLOOR exists for this."""
        out = ts.headline_metrics(_ret([0.001] * 100))
        assert out["sharpe"] is None
        assert out["sortino"] is None                # no negative days
        assert out["calmar"] is None                 # no drawdown
        assert out["headline_reason"] == "zero return variance"

    def test_exactly_zero_series_also_guarded(self):
        out = ts.headline_metrics(_ret([0.0] * 100))
        assert out["sharpe"] is None

    def test_rolling_sharpe_is_guarded_too(self):
        """Same hazard, same fix, different code path."""
        r = _ret([0.001] * 120 + [0.02, -0.01] * 60)
        vals = ts.rolling_metrics(r, window=30)["frame"]["rolling_sharpe"]
        assert vals.abs().max() < 1e6

    def test_near_constant_benchmark_is_guarded(self):
        b = _noise(400, seed=1)
        out = ts.benchmark_stats(b, pd.Series(0.001, index=b.index))
        assert out["beta"] is None
        assert "zero benchmark variance" in out["bench_reason"]

    def test_hit_rate(self):
        out = ts.headline_metrics(_ret([0.01, -0.01, 0.01, -0.01, 0.01, 0.01]))
        assert out["hit_rate_pct"] == pytest.approx(66.7, abs=0.1)

    def test_too_short_reason(self):
        out = ts.headline_metrics(_ret([0.01, 0.02]))
        assert out["sharpe"] is None
        assert "(< 5)" in out["headline_reason"]


# --------------------------------------------------------------- tail risk


class TestTailRisk:
    def test_var_and_cvar_known_answer(self):
        # 19 flat +1% days and one -20% day. quantile(0.05) on 20 sorted
        # points (linear interpolation) lands just below 0, between the
        # -20% and +1% values, so VaR is tiny -- but CVaR (mean of the tail
        # AT OR BELOW that quantile) is the full -20% outlier: exactly the
        # "VaR hides the fat tail" case the docstring describes.
        out = ts.tail_risk_metrics(_ret([-0.20] + [0.01] * 19))
        assert out["var_pct"] == pytest.approx(0.05, abs=0.01)
        assert out["cvar_pct"] == pytest.approx(20.0, abs=0.01)
        assert out["n_tail_days"] == 1
        assert out["alpha"] == 0.95

    def test_cvar_at_least_as_severe_as_var(self):
        rng = np.random.default_rng(3)
        out = ts.tail_risk_metrics(_ret(rng.normal(0.0003, 0.015, 300).tolist()))
        assert out["cvar_pct"] >= out["var_pct"]

    def test_too_short_reason(self):
        out = ts.tail_risk_metrics(_ret([0.01] * 10))
        assert out["cvar_pct"] is None
        assert "(< 20)" in out["tail_risk_reason"]


# --------------------------------------------------------------- assembly


class TestTearsheet:
    def test_all_sections_present(self):
        out = ts.tearsheet(_noise(600), bench_returns=_noise(600, seed=9))
        assert set(out) == {"headline", "monthly", "rolling", "drawdowns",
                            "underwater", "benchmark", "tail_risk"}
        assert out["benchmark"]["beta"] is not None
        assert out["tail_risk"]["cvar_pct"] is not None

    def test_missing_benchmark_says_so(self):
        out = ts.tearsheet(_noise(600))
        assert out["benchmark"]["beta"] is None
        assert "no benchmark supplied" in out["benchmark"]["bench_reason"]

    def test_survives_a_degenerate_series(self):
        """Every section must degrade to a reason string rather than raise."""
        out = ts.tearsheet(_ret([0.01, 0.02]))
        assert out["headline"]["sharpe"] is None
        assert out["rolling"]["frame"] is None
        assert out["tail_risk"]["cvar_pct"] is None

    def test_renderer_is_self_contained(self):
        """The report must work with no network. Plotly is inlined, so the file
        is large; what matters is that it pulls in nothing at load time.

        (The bundled plotly source contains map-tile attribution URLs in string
        literals. Those are never fetched for the trace types used here, which
        is why this asserts on resource TAGS rather than on the absence of the
        substring 'http'.)"""
        import re

        import generate_tearsheet as gt

        r = _noise(600)
        html = gt.build_html(r, title="Test", bench_returns=_noise(600, seed=4))
        assert html.startswith("<!DOCTYPE html>")
        assert html.rstrip().endswith("</html>")
        assert not re.search(r"<script[^>]+src=", html, re.I)
        assert not re.search(r"<link[^>]+href=", html, re.I)
        assert not re.search(r"<(img|iframe)\b", html, re.I)

    def test_renderer_labels_an_unrecovered_drawdown_in_a_mixed_table(self):
        import generate_tearsheet as gt

        r = _ret([0.10, -0.05, 0.10, 0.10, -0.30, 0.01])
        html = gt._drawdown_table(ts.drawdown_periods(r))
        assert "still under water" in html
        assert "NaT" not in html

    def test_renderer_degrades_without_a_benchmark(self):
        import generate_tearsheet as gt

        html = gt.build_html(_noise(600), title="Test")
        assert "no benchmark supplied" in html

    def test_renderer_survives_a_degenerate_series(self):
        """Too little data must render a reason, not raise."""
        import generate_tearsheet as gt

        html = gt.build_html(_ret([0.01, 0.02]), title="Test")
        assert "<!DOCTYPE html>" in html

    def test_end_to_end_from_trades(self):
        rng = np.random.default_rng(0)
        dates = pd.bdate_range("2020-01-01", periods=300)
        trades = pd.DataFrame({
            "exit_date": rng.choice(dates, 150),
            "pnl_dollars": rng.normal(50, 500, 150)})
        bridged = ts.daily_returns_from_trades(trades)
        out = ts.tearsheet(bridged["returns"])
        assert out["headline"]["n_days"] > 100
        assert out["drawdowns"]["table"] is not None
