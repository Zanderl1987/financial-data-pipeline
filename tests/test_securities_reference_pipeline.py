import pandas as pd

from securities_reference_pipeline import _aggregate_index_flags


def _row(ticker, index_code, company_name, cik=None, sector=None, sub_industry=None,
         snapshot_date="2026-07-23"):
    return {
        "ticker": ticker, "index_code": index_code, "company_name": company_name,
        "cik": cik, "gics_sector": sector, "gics_sub_industry": sub_industry,
        "snapshot_date": snapshot_date,
    }


def test_ticker_in_multiple_indexes_with_differing_metadata_gets_all_flags():
    # Reproduces the real AAPL/MSFT/NVDA bug: each index source reports its own
    # company_name casing, and only the SPX/Wikipedia source carries cik/gics_sector.
    members = pd.DataFrame([
        _row("AAPL", "NDX", "Apple Inc."),
        _row("AAPL", "RUT3000", "APPLE INC"),
        _row("AAPL", "SPX", "Apple Inc.", cik=320193, sector="Information Technology",
             sub_industry="Technology Hardware, Storage & Peripherals"),
        _row("AAPL", "W5000", "APPLE INC"),
    ])
    out = _aggregate_index_flags(members)

    assert len(out) == 1
    row = out.iloc[0]
    assert row["is_sp500"] and row["is_nasdaq100"] and row["is_russell3000"] and row["is_wilshire5000"]
    assert not row["is_russell2000"]
    assert row["cik"] == 320193
    assert row["gics_sector"] == "Information Technology"


def test_only_latest_snapshot_date_is_used():
    members = pd.DataFrame([
        _row("XYZ", "SPX", "Old Co", snapshot_date="2026-07-01"),
        _row("XYZ", "RUT2000", "New Co", snapshot_date="2026-07-23"),
    ])
    out = _aggregate_index_flags(members)

    assert len(out) == 1
    row = out.iloc[0]
    assert not row["is_sp500"]
    assert row["is_russell2000"]
    assert row["company_name"] == "New Co"


def test_metadata_prefers_spx_source_when_available():
    members = pd.DataFrame([
        _row("ABC", "NDX", "ABC Nasdaq Name"),
        _row("ABC", "SPX", "ABC SP500 Name", cik=42, sector="Health Care"),
    ])
    out = _aggregate_index_flags(members)

    row = out.iloc[0]
    assert row["company_name"] == "ABC SP500 Name"
    assert row["cik"] == 42
