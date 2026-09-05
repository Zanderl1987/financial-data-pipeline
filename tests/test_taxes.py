"""
tests/test_taxes.py -- evaluation/taxes.py: wash-sale basis accounting and
per-year short/long-term tax bucketing on realized trades.

Every fixture is hand-computed so the assertion is an exact identity, not a
round-trip echo of the module's own math. Conservation across wash
adjustment (sum(adj_pnl_dollars) == sum(pnl_dollars), unconditional) is
asserted on every wash scenario, and the cross-bucket offset rules in
_offset get dedicated known-answer cases below (both-sign, |loss| > |gain|,
and equality -- the residual-loss-into-leftover path that a gain>loss-only
assertion would miss).
"""

import numpy as np
import pandas as pd
import pytest

from evaluation import runner as ev_runner
from evaluation import taxes
from evaluation.taxes import TaxConfig, resolve, wash_adjust, wash_report
from evaluation.taxes import tax_summary, tax_year_summary, _offset


def _frame(rows):
    """rows: (symbol, side, entry, exit, entry_px, exit_px, shares, pnl).
    pnl is the exact accounting pnl_dollars for that lot."""
    cols = ["symbol", "side", "entry_date", "exit_date", "entry_price",
            "exit_price", "shares", "pnl_dollars"]
    df = pd.DataFrame(rows, columns=cols)
    df["pnl_pct"] = 100.0 * df.apply(
        lambda r: (r["exit_price"] / r["entry_price"] - 1.0)
        if r["side"] == "long" else (1.0 - r["exit_price"] / r["entry_price"]),
        axis=1)
    return df


def _assert_conserved(df_adj):
    assert round(float(df_adj["adj_pnl_dollars"].sum()), 6) == \
        round(float(df_adj["pnl_dollars"].sum()), 6)


class TestWashAdjust:
    def test_no_wash_when_reentry_over_30d(self):
        tr = _frame([
            ("AAPL", "long", "2024-01-02", "2024-01-05", 100.0, 95.0, 100, -500.0),
            ("AAPL", "long", "2024-03-01", "2024-03-05", 95.0, 101.0, 100, 600.0),
        ])
        df = wash_adjust(tr)
        assert list(df["adj_pnl_dollars"]) == [-500.0, 600.0]
        assert list(df["wash_disallowed"]) == [0.0, 0.0]
        assert (df["wash_disallowed"] > 0).sum() == 0
        _assert_conserved(df)

    def test_simple_full_wash_defers_loss(self):
        tr = _frame([
            ("AAPL", "long", "2024-01-02", "2024-01-05", 100.0, 95.0, 100, -500.0),
            ("AAPL", "long", "2024-01-15", "2024-01-25", 95.0, 101.0, 100, 600.0),
        ])
        df = wash_adjust(tr)
        # Loss fully deferred into the replacement's basis: first lot
        # recognizes 0, replacement recognizes 100 (600 - 500 deferred).
        assert list(df["adj_pnl_dollars"]) == [0.0, 100.0]
        assert list(df["wash_disallowed"]) == [500.0, 0.0]
        _assert_conserved(df)
        wr = wash_report(tr)
        assert len(wr) == 1
        assert wr.iloc[0]["loss_deferred"] == pytest.approx(500.0)
        assert wr.iloc[0]["loss_total"] == pytest.approx(-500.0)

    def test_partial_replacement_prorates_deferral(self):
        tr = _frame([
            ("AAPL", "long", "2024-01-02", "2024-01-05", 100.0, 95.0, 100, -500.0),
            ("AAPL", "long", "2024-01-15", "2024-01-25", 95.0, 101.0, 50, 300.0),
        ])
        df = wash_adjust(tr)
        # Ratio 50/100 -> half the loss deferred (250); remainder recognized.
        assert list(df["adj_pnl_dollars"]) == [pytest.approx(-250.0),
                                               pytest.approx(50.0)]
        assert list(df["wash_disallowed"]) == [250.0, 0.0]
        _assert_conserved(df)

    def test_wash_chain_rolls_until_reentry_gap(self):
        tr = _frame([
            ("AAPL", "long", "2024-01-02", "2024-01-05", 100.0, 99.0, 100, -100.0),
            ("AAPL", "long", "2024-01-15", "2024-01-20", 99.0, 98.5, 100, -50.0),
            ("AAPL", "long", "2024-02-01", "2024-02-10", 98.5, 98.9, 100, 40.0),
        ])
        df = wash_adjust(tr)
        # A: -100 deferred -> recognizes 0. B: -50 -100 basis = -150 deferred
        # -> recognizes 0. C: +40 -150 basis = -110 recognized (gap to data
        # end is a terminal wash exit, so the deferred loss surfaces here).
        assert list(df["adj_pnl_dollars"]) == [0.0, 0.0, -110.0]
        assert list(df["wash_disallowed"]) == [100.0, 150.0, 0.0]
        _assert_conserved(df)

    def test_short_sale_symmetric_wash(self):
        tr = _frame([
            ("TSLA", "short", "2024-01-02", "2024-01-05", 100.0, 105.0, 100, -500.0),
            ("TSLA", "short", "2024-01-15", "2024-01-25", 104.0, 103.0, 100, 100.0),
        ])
        df = wash_adjust(tr)
        assert list(df["adj_pnl_dollars"]) == [0.0, -400.0]
        assert list(df["wash_disallowed"]) == [500.0, 0.0]
        _assert_conserved(df)

    def test_apply_to_shorts_false_rejects_short_trades(self):
        tr = _frame([
            ("TSLA", "short", "2024-01-02", "2024-01-05", 100.0, 105.0, 100, -500.0),
        ])
        with pytest.raises(ValueError, match="apply_to_shorts"):
            wash_adjust(tr, TaxConfig(apply_to_shorts=False))

    def test_wash_window_zero_disables_rule(self):
        tr = _frame([
            ("AAPL", "long", "2024-01-02", "2024-01-05", 100.0, 95.0, 100, -500.0),
            ("AAPL", "long", "2024-01-15", "2024-01-25", 95.0, 101.0, 100, 600.0),
        ])
        df = wash_adjust(tr, TaxConfig(wash_window_days=0))
        assert list(df["adj_pnl_dollars"]) == [-500.0, 600.0]
        assert (df["wash_disallowed"] > 0).sum() == 0

    def test_missing_columns_raise(self):
        with pytest.raises(ValueError, match="missing required columns"):
            wash_adjust(pd.DataFrame({"symbol": ["A"]}))

    def test_row_order_independence(self):
        rows = [
            ("AAPL", "long", "2024-01-02", "2024-01-05", 100.0, 95.0, 100, -500.0),
            ("AAPL", "long", "2024-01-15", "2024-01-25", 95.0, 101.0, 100, 600.0),
            ("IBM", "long", "2024-02-01", "2024-02-10", 50.0, 52.0, 100, 200.0),
        ]
        forward = wash_adjust(_frame(rows))
        back = wash_adjust(_frame(list(reversed(rows))))
        assert list(forward["adj_pnl_dollars"]) == list(back["adj_pnl_dollars"])
        assert list(forward["gross_pnl_dollars"]) == list(back["gross_pnl_dollars"])

    def test_shares_inferred_from_pnl_pct(self):
        tr = _frame([
            ("AAPL", "long", "2024-01-02", "2024-01-05", 100.0, 110.0, 100, 1000.0),
        ]).drop(columns=["shares"])
        df = wash_adjust(tr)
        assert df["shares"].iloc[0] == pytest.approx(100.0)

    def test_wash_window_gap_boundary_30_vs_31(self):
        # Gap of exactly 30 days in [exit, exit+window] -> wash; 31 -> none.
        at = ("AAPL", "long", "2024-01-02", "2024-01-05", 100.0, 95.0, 100, -500.0)
        in30 = _frame([at,
                       ("AAPL", "long", "2024-02-04", "2024-02-14", 95.0, 101.0, 100, 600.0)])
        in31 = _frame([at,
                       ("AAPL", "long", "2024-02-05", "2024-02-15", 95.0, 101.0, 100, 600.0)])
        d30 = wash_adjust(in30)
        d31 = wash_adjust(in31)
        assert list(d30["adj_pnl_dollars"]) == [0.0, 100.0]   # washed
        assert list(d31["adj_pnl_dollars"]) == [-500.0, 600.0]  # not washed
        assert d30["wash_disallowed"].iloc[0] == pytest.approx(500.0)
        assert d31["wash_disallowed"].iloc[0] == pytest.approx(0.0)

    def test_wash_window_gap_zero_same_day_reentry(self):
        tr = _frame([
            ("AAPL", "long", "2024-01-02", "2024-01-05", 100.0, 95.0, 100, -500.0),
            ("AAPL", "long", "2024-01-05", "2024-01-12", 95.0, 101.0, 100, 600.0),
        ])
        df = wash_adjust(tr)
        assert list(df["adj_pnl_dollars"]) == [0.0, 100.0]
        assert df["wash_disallowed"].iloc[0] == pytest.approx(500.0)

    def test_cross_year_wash_surfaces_in_replacement_year(self):
        # Loss sold 2024-12-28, replacement bought 2025-01-10 (13d gap).
        # The deferred loss is recognized in the REPLACEMENT lot's year
        # (2025), and per-year sums still conserve to total accounting P&L.
        tr = _frame([
            ("AAPL", "long", "2024-11-01", "2024-12-28", 100.0, 95.0, 100, -500.0),
            ("AAPL", "long", "2025-01-10", "2025-02-10", 95.0, 101.0, 100, 600.0),
            ("MSFT", "long", "2025-03-01", "2025-04-01", 50.0, 55.0, 100, 500.0),
        ])
        df = wash_adjust(tr)
        years = tax_year_summary(tr)

        # The AAPL 2024 lot recognizes 0; the 2025 replacement absorbs -500.
        r2024 = years[years["year"] == 2024].iloc[0]
        r2025 = years[years["year"] == 2025].iloc[0]
        assert r2024["realized_pnl_tax"] == pytest.approx(0.0)
        assert r2025["realized_pnl_tax"] == pytest.approx(600.0 - 500.0 + 500.0)
        # Cross-year conservation: the wash only moves WHEN, not the total.
        assert years["realized_pnl_tax"].sum() == \
            pytest.approx(float(tr["pnl_dollars"].sum()))
        assert df["adj_pnl_dollars"].sum() == pytest.approx(float(tr["pnl_dollars"].sum()))
        wr = wash_report(tr)
        assert wr.iloc[0]["sold_exit_date"].year == 2024
        assert wr.iloc[0]["replacement_entry_date"].year == 2025

    def test_engine_faithful_shares_inference_in_wash(self):
        # Engine output has NO shares column; pnl_pct is rounded to 3
        # decimals and pnl_dollars to cents, exactly as trades.py emits.
        # Both lots infer >= the sold lot's 100 shares, so the ratio caps at
        # 1.0 and the loss defers in full even though no shares were passed.
        tr = pd.DataFrame([
            {"symbol": "AAPL", "side": "long", "entry_date": "2024-01-02",
             "exit_date": "2024-01-05", "entry_price": 100.0, "exit_price": 95.0,
             "pnl_dollars": -500.00, "pnl_pct": -5.000},
            {"symbol": "AAPL", "side": "long", "entry_date": "2024-01-15",
             "exit_date": "2024-01-25", "entry_price": 95.0, "exit_price": 101.0,
             "pnl_dollars": 631.58, "pnl_pct": 6.316},
        ])
        df = wash_adjust(tr)
        assert df["shares"].iloc[1] == pytest.approx(10000.0 / 95.0, rel=1e-3)
        assert list(df["adj_pnl_dollars"]) == [pytest.approx(0.0),
                                               pytest.approx(631.58 - 500.0)]
        _assert_conserved(df)

    def test_nan_pnl_raises(self):
        tr = _frame([("AAPL", "long", "2024-01-02", "2024-01-05",
                      100.0, 95.0, 100, np.nan)])
        with pytest.raises(ValueError, match="finite"):
            wash_adjust(tr)

    def test_nat_date_raises(self):
        tr = _frame([("AAPL", "long", "2024-01-02", "2024-01-05",
                      100.0, 95.0, 100, -500.0)])
        tr.loc[0, "entry_date"] = pd.NaT
        with pytest.raises(ValueError, match="NaT"):
            wash_adjust(tr)

    def test_nonpositive_price_raises(self):
        tr = _frame([("AAPL", "long", "2024-01-02", "2024-01-05",
                      100.0, 0.0, 100, -500.0)])
        with pytest.raises(ValueError, match="entry_price/exit_price"):
            wash_adjust(tr)


class TestTaxBucketsAndYears:
    def test_bucket_split_and_rate_multiplication(self):
        # 2024: +1000 short (held 59d), +3000 long (held >365d). No losses.
        tr = _frame([
            ("AAPL", "long", "2024-01-02", "2024-03-01", 100.0, 110.0, 100, 1000.0),
            ("MSFT", "long", "2023-01-10", "2024-03-01", 100.0, 130.0, 100, 3000.0),
        ])
        years = tax_year_summary(tr)
        assert len(years) == 1
        row = years.iloc[0]
        assert row["year"] == 2024
        assert row["realized_pnl_tax"] == pytest.approx(4000.0)
        assert row["net_short"] == pytest.approx(1000.0)
        assert row["net_long"] == pytest.approx(3000.0)
        # 1000 * 0.24 + 3000 * 0.15 = 240 + 450 = 690.
        assert row["tax_due"] == pytest.approx(690.0)
        assert row["after_tax_pnl"] == pytest.approx(4000.0 - 690.0)
        assert row["tax_drag_pct"] == pytest.approx(17.25)

    def test_long_term_loss_offsets_short_term_gain(self):
        # 2025: short +2000, long -1500 (cross-bucket offset -> 500 short).
        tr = _frame([
            ("AAPL", "long", "2024-12-01", "2025-03-01", 100.0, 120.0, 100, 2000.0),
            ("XOM", "long", "2023-06-01", "2025-03-01", 100.0, 85.0, 100, -1500.0),
        ])
        row = tax_year_summary(tr).iloc[0]
        assert row["realized_pnl_tax"] == pytest.approx(500.0)
        assert row["taxable_short"] == pytest.approx(500.0)
        assert row["taxable_long"] == pytest.approx(0.0)
        assert row["tax_due"] == pytest.approx(500.0 * 0.24)

    def test_short_loss_exceeding_long_gain_reports_residual_deduction(self):
        # The _offset residual bug guard: short -2000 vs long +1500 must
        # leave a 500 leftover loss (deductible vs ordinary income, reported
        # loss_deducted in full since it is under the 3000 cap), NOT vanish.
        tr = _frame([
            ("AAPL", "long", "2024-11-01", "2025-03-01", 100.0, 80.0, 100, -2000.0),
            ("MSFT", "long", "2023-06-01", "2025-03-01", 100.0, 130.0, 100, 1500.0),
        ])
        row = tax_year_summary(tr).iloc[0]
        assert row["taxable_short"] == pytest.approx(0.0)
        assert row["taxable_long"] == pytest.approx(0.0)
        assert row["loss_deducted"] == pytest.approx(500.0)
        assert row["tax_due"] == pytest.approx(0.0)

    def test_short_loss_above_cap_caps_deduction(self):
        # short -4000 vs long +1500 -> leftover 2500, still under cap here;
        # push past: short -6000 -> leftover 4500 -> report only 3000.
        tr = _frame([
            ("AAPL", "long", "2024-11-01", "2025-03-01", 100.0, 40.0, 100, -6000.0),
            ("MSFT", "long", "2023-06-01", "2025-03-01", 100.0, 130.0, 100, 1500.0),
        ])
        row = tax_year_summary(tr).iloc[0]
        assert row["loss_deducted"] == pytest.approx(3000.0)
        assert row["tax_due"] == pytest.approx(0.0)

    def test_loss_deduction_floor_caps_excess(self):
        # 2026: short loss -5000, no gains: 3000 is reportable as deductible
        # against ORDINARY income. Capital tax is 0; nothing is netted off
        # tax_due because ordinary income is out of this module's scope --
        # the deduction's benefit is reported, not subtracted.
        tr = _frame([
            ("AAPL", "long", "2026-01-05", "2026-03-01", 100.0, 50.0, 100, -5000.0),
        ])
        row = tax_year_summary(tr).iloc[0]
        assert row["loss_deducted"] == pytest.approx(3000.0)
        assert row["tax_due"] == pytest.approx(0.0)
        assert row["after_tax_pnl"] == pytest.approx(-5000.0)
        assert row["tax_drag_pct"] is None

    def test_multiyear_rows_sorted(self):
        tr = _frame([
            ("A", "long", "2024-01-02", "2024-03-01", 100.0, 110.0, 100, 1000.0),
            ("B", "long", "2025-01-02", "2025-03-01", 100.0, 105.0, 100, 500.0),
        ])
        years = tax_year_summary(tr)
        assert list(years["year"]) == [2024, 2025]
        assert list(years["n_trades"]) == [1, 1]

    def test_bucket_days_boundary(self):
        cfg = TaxConfig()
        assert taxes._bucket_days("2024-01-01", "2024-12-31", cfg.short_term_days) == "short"
        assert taxes._bucket_days("2023-01-01", "2024-01-02", cfg.short_term_days) == "long"

    def test_bucket_days_leap_year_exact_one_year_is_short(self):
        # 2024 is a leap year: 2024-01-01 -> 2025-01-01 is 366 calendar days,
        # but the hold is EXACTLY one year, hence short-term under the IRS
        # rule. A plain 365-day-count threshold would misbucket this.
        cfg = TaxConfig()
        assert taxes._bucket_days("2024-01-01", "2025-01-01", cfg.short_term_days) == "short"

    def test_bucket_days_custom_day_cutoff_is_literal(self):
        # A non-365 value is a plain max-day threshold, calendar days as-is.
        cfg = TaxConfig()
        assert taxes._bucket_days("2023-01-01", "2024-01-01", 364) == "long"
        assert taxes._bucket_days("2023-01-01", "2023-12-31", 364) == "short"


class TestOffset:
    def test_both_gains_no_offset(self):
        assert _offset(1000.0, 2000.0) == (1000.0, 2000.0, 0.0)

    def test_both_losses_all_leftover(self):
        assert _offset(-100.0, -200.0) == (0.0, 0.0, 300.0)

    def test_short_gain_offsets_long_loss(self):
        assert _offset(1000.0, -400.0) == (600.0, 0.0, 0.0)

    def test_long_gain_offsets_short_loss(self):
        assert _offset(-400.0, 1000.0) == (0.0, 600.0, 0.0)

    def test_short_loss_exceeds_long_gain_residual(self):
        assert _offset(-2000.0, 1500.0) == (0.0, 0.0, 500.0)

    def test_long_loss_exceeds_short_gain_residual(self):
        assert _offset(1500.0, -2000.0) == (0.0, 0.0, 500.0)

    def test_equal_gain_loss_wash(self):
        assert _offset(1000.0, -1000.0) == (0.0, 0.0, 0.0)

    def test_zero_short_with_long_loss(self):
        assert _offset(0.0, -500.0) == (0.0, 0.0, 500.0)

    def test_zero_short_with_long_gain(self):
        assert _offset(0.0, 500.0) == (0.0, 500.0, 0.0)


class TestWiring:
    def test_trades_tax_registration_skips_metadata_and_none(self):
        # Feedback-loop guard (runner.py): count/flag leaves registered under
        # the trades_tax evaluation are metadata, not diffable statistics --
        # n_wash_events joins the module's _METADATA_KEYS, tax_drag_pct (None
        # on a loss year) is dropped entirely, and n_trades is the row's n.
        d = {"n_trades": 3, "tax_due_total": 12.34, "tax_drag_pct": None,
             "n_wash_events": 1, "wash_disallowed_total": 500.0,
             "after_tax_pnl_dollars": 99.9}
        rows = ev_runner._stat_rows("trades_tax", -1, d, n_key="n_trades")
        stats = [r["statistic"] for r in rows]
        assert "n_wash_events" not in stats
        assert "tax_drag_pct" not in stats
        assert "n_trades" not in stats
        assert "tax_due_total" in stats
        assert "wash_disallowed_total" in stats
        assert all(r["n"] == 3 for r in rows)


class TestTaxSummary:
    def test_shape_and_conservation(self):
        tr = _frame([
            ("AAPL", "long", "2024-01-02", "2024-01-05", 100.0, 95.0, 100, -500.0),
            ("AAPL", "long", "2024-01-15", "2024-01-25", 95.0, 101.0, 100, 600.0),
            ("MSFT", "long", "2023-01-10", "2024-03-01", 100.0, 130.0, 100, 3000.0),
        ])
        s = tax_summary(tr)
        assert s["n_trades"] == 3
        assert s["total_pnl_dollars"] == pytest.approx(100.0 + 3000.0)
        # Wash-deferred 500 short-term loss flips the AAPL pair to +100; the
        # total recognized equals the accounting total.
        assert s["wash_disallowed_total"] == pytest.approx(500.0)
        assert s["n_wash_events"] == 1
        realized = 100.0 + 3000.0
        tax = 100.0 * 0.24 + 3000.0 * 0.15     # short-term now +100, long +3000
        assert s["after_tax_pnl_dollars"] == pytest.approx(realized - tax)
        assert s["tax_due_total"] == pytest.approx(tax)

    def test_empty_frame(self):
        empty = _frame([])
        s = tax_summary(empty)
        assert s["n_trades"] == 0
        assert s["after_tax_pnl_dollars"] == 0.0
        assert s["tax_due_total"] == 0.0
        assert s["n_wash_events"] == 0

    def test_resolve_none_gives_defaults(self):
        assert resolve(None) == TaxConfig()
        assert resolve(TaxConfig(st_rate=0.5)).st_rate == 0.5