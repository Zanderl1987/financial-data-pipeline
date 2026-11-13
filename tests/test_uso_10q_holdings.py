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
