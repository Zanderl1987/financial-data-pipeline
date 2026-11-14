"""Parser tests for uso_10q_holdings_pipeline against a synthetic filing that
mirrors the real 10-Q layout: the current-period schedule, a prior year-end
comparison schedule that must be ignored, a swap header outside the table,
split '$' / '(' cells, and an inline-XBRL PartnersCapital fact."""
from datetime import date

import pytest

from uso_10q_holdings_pipeline import _merge_numeric_cells, parse_schedule, partners_capital

HTML = """<html><body>
<xbrli:context id="c1"><xbrli:entity/><xbrli:period><xbrli:instant>2026-09-30</xbrli:instant></xbrli:period></xbrli:context>
<xbrli:context id="c2"><xbrli:entity><xbrli:segment>x</xbrli:segment></xbrli:entity><xbrli:period><xbrli:instant>2026-09-30</xbrli:instant></xbrli:period></xbrli:context>
<p>Total partners' capital <ix:nonFraction name="us-gaap:PartnersCapital" contextRef="c2" scale="0">5</ix:nonFraction>
<ix:nonFraction name="us-gaap:PartnersCapital" contextRef="c1" scale="0">1,000,000,000</ix:nonFraction></p>
<p>United States Oil Fund, LP</p>
<p>Schedule of Investments (Unaudited)</p>
<p>At September 30, 2026</p>
<table>
<tr><td>Open Commodity Futures Contracts - Long</td></tr>
<tr><td>NYMEX WTI Crude Oil Futures CL November 2026 contracts, expiring October 2026 *</td>
    <td>$</td><td>900,000,000</td><td>15,000</td><td>$</td><td>(</td><td>4,000</td><td>)</td><td>0.30</td></tr>
<tr><td>Short-term U.S. Treasury Obligations</td></tr>
<tr><td>Matures on 10/08/2026, yield 3.85 %</td><td>$</td><td>30,000,000</td><td>$</td><td>29,978,635</td><td>3.00</td></tr>
<tr><td>Total Short-term U.S. Treasury Obligations</td><td>29,978,635</td><td>3.00</td></tr>
<tr><td>United States Money Market Funds</td></tr>
<tr><td>Dreyfus Government Money Market Fund - Institutional Shares, 3.82 % #</td><td>36,400,000</td><td>36,400,000</td><td>3.64</td></tr>
</table>
<p>Open OTC Commodity Swap Contracts</p>
<table>
<tr><td>MACQUARIE MQCP361E 07212026Index (a)</td><td>0.26</td><td>%</td><td>Macquarie Bank Ltd.</td>
    <td>monthly</td><td>01/21/2027</td><td>$</td><td>140,000,000</td><td>$</td><td>100,000,000</td><td>—</td><td>$</td><td>(</td><td>993</td><td>)</td></tr>
<tr><td>Total Open OTC Commodity Swap Contracts</td><td>$</td><td>140,000,000</td></tr>
</table>
<p>Schedule of Investments</p>
<p>At December 31, 2025</p>
<table>
<tr><td>Open Commodity Futures Contracts - Long</td></tr>
<tr><td>NYMEX WTI Crude Oil Futures CL February 2026 contracts, expiring January 2026 *</td><td>764,706,290</td><td>13,180</td><td>(7,910,690)</td><td>(0.89)</td></tr>
</table>
<p>Statements of Operations (Unaudited)</p>
</body></html>"""


@pytest.fixture(scope="module")
def df():
    return parse_schedule(HTML, date(2026, 9, 30), date(2026, 11, 9))


def test_partners_capital_ignores_dimensioned_contexts():
    assert partners_capital(HTML, date(2026, 9, 30)) == 1_000_000_000


def test_only_current_period_schedule(df):
    assert len(df) == 4
    assert not df["holding_name"].str.contains("February 2026").any()
    assert set(df["asset_category"]) == {"FUT", "GOVT", "MMF", "SWAP"}


def test_futures_weight_is_notional_exposure(df):
    fut = df[df.asset_category == "FUT"].iloc[0]
    assert fut.holding_ticker == "CL November 2026"
    assert fut.shares_held == 15_000
    assert fut.weight_pct == pytest.approx(90.0)


def test_tbill_and_mmf(df):
    tb = df[df.asset_category == "GOVT"].iloc[0]
    assert tb.maturity_date == date(2026, 10, 8)
    assert tb.par_value == 30_000_000 and tb.market_value_usd == 29_978_635
    assert tb.ytm_pct == 3.85 and tb.weight_pct == 3.00
    mmf = df[df.asset_category == "MMF"].iloc[0]
    assert mmf.holding_name == "Dreyfus Government Money Market Fund - Institutional Shares"
    assert mmf.ytm_pct == 3.82 and mmf.weight_pct == 3.64


def test_swap_header_outside_table(df):
    sw = df[df.asset_category == "SWAP"].iloc[0]
    assert sw.holding_name == "MACQUARIE MQCP361E 07212026Index"
    assert sw.par_value == 140_000_000 and sw.market_value_usd == 100_000_000
    assert sw.weight_pct == pytest.approx(10.0)
    assert sw.maturity_date == date(2027, 1, 21)
    assert sw.issuer_name == "Macquarie Bank Ltd."


def test_rows_carry_period_and_source(df):
    assert (df.snapshot_date == date(2026, 9, 30)).all()
    assert (df.reporting_period_end == date(2026, 9, 30)).all()
    assert (df.source == "edgar_10q:USO").all()


def test_merge_numeric_cells():
    assert _merge_numeric_cells(["x", "$", "(", "993", ")"]) == ["x", "$(993)"]


def test_write_puts_each_quarter_in_its_own_file(tmp_path, monkeypatch):
    """A later one-quarter overwrite must retire files whole, never rewrite one.
    A rewrite copies the untouched quarters into a new file with their old
    fetched_at, and curated read both copies (USO 2026-11-14)."""
    import pandas as pd
    from pyiceberg.catalog import load_catalog
    from pyiceberg.partitioning import PartitionField, PartitionSpec
    from pyiceberg.schema import Schema
    from pyiceberg.transforms import IdentityTransform
    from pyiceberg.types import (DateType, DoubleType, NestedField, StringType,
                                 TimestamptzType)

    import uso_10q_holdings_pipeline as uso

    monkeypatch.setattr(uso, "ICEBERG_WAREHOUSE", tmp_path)
    monkeypatch.setattr(uso, "CATALOG_DB", tmp_path / "cat.db")
    cat = load_catalog("constituents", type="sql",
                       uri=f"sqlite:///{(tmp_path / 'cat.db').as_posix()}",
                       warehouse=f"file://{tmp_path.as_posix()}")
    cat.create_namespace("constituents")
    schema = Schema(
        NestedField(1, "snapshot_date", DateType(), required=True),
        NestedField(2, "fund_ticker", StringType(), required=True),
        NestedField(3, "holding_name", StringType()),
        NestedField(4, "weight_pct", DoubleType()),
        NestedField(5, "filing_date", DateType()),
        NestedField(6, "reporting_period_end", DateType()),
        NestedField(7, "maturity_date", DateType()),
        NestedField(8, "source", StringType(), required=True),
        NestedField(9, "fetched_at", TimestamptzType(), required=True),
    )
    table = cat.create_table(
        "constituents.fund_holdings", schema,
        partition_spec=PartitionSpec(PartitionField(2, 1000, IdentityTransform(), "fund_ticker")))

    def quarters(*periods):
        return pd.DataFrame([{
            "snapshot_date": p, "fund_ticker": "USO", "holding_name": name,
            "weight_pct": 50.0, "filing_date": p, "reporting_period_end": p,
            "maturity_date": None, "source": uso.SOURCE,
        } for p in periods for name in ("CL futures", "T-bill")])

    uso.write_to_iceberg(quarters("2026-03-31", "2026-06-30", "2026-09-30"))
    uso.write_to_iceberg(quarters("2026-09-30"))

    table.refresh()
    live = [t.file.file_path for t in table.scan().plan_files()]
    per_file = [pd.read_parquet(p.replace("file://", "")).reporting_period_end.nunique()
                for p in live]
    assert per_file == [1, 1, 1]
    rows = table.scan().to_pandas()
    assert len(rows) == 6
    on_disk = list(tmp_path.rglob("*.parquet"))
    assert len(on_disk) == 4   # 3 from the backfill + 1 replacement, no rewrite
