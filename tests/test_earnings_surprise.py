"""
tests/test_earnings_surprise.py -- earnings surprise event study unit tests.
"""

import pandas as pd
import pytest

from experiments.earnings_surprise_event_study import build_events


class TestEarningsBuildEvents:
    """Test the event builder with injected data (no live store)."""

    def _fake_av_earnings(self):
        # Minimal columns the builder needs
        return pd.DataFrame({
            "ticker": ["AAPL", "AAPL", "MSFT", "MSFT", "GOOGL"],
            "report_type": ["quarterly", "quarterly", "quarterly", "annual", "quarterly"],
            "reportedDate": ["2024-01-25", "2024-04-25", "2024-01-30", "2024-01-30", "2024-01-31"],
            "surprisePercentage": ["5.2", "-3.1", "10.0", "2.0", "0.0"],
            "reportTime": ["pre-market", "post-market", "pre-market", "post-market", "pre-market"],
            "reportedEPS": ["2.1", "1.5", "3.0", "2.8", "1.8"],
            "estimatedEPS": ["2.0", "1.55", "2.7", "2.75", "1.8"],
        })

    def test_quarterly_only(self):
        # Monkeypatch q.load to return fake data
        import experiments.earnings_surprise_event_study as ese
        import query as q
        orig_load = q.load
        try:
            q.load = lambda name: self._fake_av_earnings() if name == "alpha_vantage_earnings" else orig_load(name)
            events = ese.build_events()
        finally:
            q.load = orig_load

        # Only quarterly, has surprise, has date -> 4 rows (1 annual dropped)
        assert len(events) == 4
        # 5.2, 10.0 -> beat; -3.1, 0.0 -> miss (0 is not > 0)
        assert (events["direction"] == "beat").sum() == 2
        assert (events["direction"] == "miss").sum() == 2

    def test_entry_lag_mapping(self):
        import experiments.earnings_surprise_event_study as ese
        import query as q
        orig_load = q.load
        try:
            q.load = lambda name: self._fake_av_earnings() if name == "alpha_vantage_earnings" else orig_load(name)
            events = ese.build_events()
        finally:
            q.load = orig_load

        # pre-market -> 0, post-market -> 1
        aapl_pre = events[(events["symbol"] == "AAPL") & (events["entry_lag"] == 0)]
        aapl_post = events[(events["symbol"] == "AAPL") & (events["entry_lag"] == 1)]
        assert len(aapl_pre) == 1
        assert len(aapl_post) == 1

    def test_min_surprise_filter(self):
        import experiments.earnings_surprise_event_study as ese
        import query as q
        orig_load = q.load
        try:
            q.load = lambda name: self._fake_av_earnings() if name == "alpha_vantage_earnings" else orig_load(name)
            events = ese.build_events(min_surprise_pct=4.0)
        finally:
            q.load = orig_load

        # Only |surprise| >= 4 -> 5.2, -3.1(no), 10.0, 0.0(no) -> 2 rows
        assert len(events) == 2
        assert (events["surprisePct"].abs() >= 4).all()

    def test_dedupe_same_symbol_date_direction(self):
        df = pd.DataFrame({
            "ticker": ["AAPL", "AAPL"],
            "report_type": ["quarterly", "quarterly"],
            "reportedDate": ["2024-01-25", "2024-01-25"],
            "surprisePercentage": ["5.0", "3.0"],
            "reportTime": ["pre-market", "pre-market"],
            "reportedEPS": ["2.1", "2.05"],
            "estimatedEPS": ["2.0", "2.0"],
        })
        import experiments.earnings_surprise_event_study as ese
        import query as q
        orig_load = q.load
        try:
            q.load = lambda name: df if name == "alpha_vantage_earnings" else orig_load(name)
            events = ese.build_events()
        finally:
            q.load = orig_load

        # Same symbol/date/direction -> keep larger |surprise|
        assert len(events) == 1
        assert events.iloc[0]["surprisePct"] == 5.0