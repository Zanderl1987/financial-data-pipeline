"""BlackRock equity-sheet parser: which rows count as holdings.

Foreign listings carry all-digit tickers (TSMC 2330, Samsung 005930, Toyota
7203) or letter+digit ones (DBS D05). An old ticker regex dropped them as
"non-securities", which cut 66-70% of the weight out of EEM/IEMG/ESGE and 13%
out of ACWI.
"""

import fund_holdings_pipeline as fhp

HEADER = ["Ticker", "Name", "Sector", "Asset Class", "Market Value", "Weight (%)"]


def _sheet(rows):
    return [["11/12/2026"], ["iShares MSCI ACWI ETF"], HEADER, *rows]


def _parse(monkeypatch, rows):
    monkeypatch.setattr(fhp, "_fetch_blackrock_holdings_rows", lambda *a, **k: _sheet(rows))
    return fhp.fetch_blackrock_etf_holdings("ACWI")


def test_keeps_numeric_foreign_tickers(monkeypatch):
    out = _parse(monkeypatch, [
        ["AAPL", "APPLE", "Information Technology", "Equity", "1", "4.0"],
        ["2330", "TAIWAN SEMICONDUCTOR MANUFACTURING", "Information Technology", "Equity", "1", "1.7"],
        ["005930", "SAMSUNG ELECTRONICS LTD", "Information Technology", "Equity", "1", "0.7"],
        ["7203", "TOYOTA MOTOR", "Consumer Discretionary", "Equity", "1", "0.2"],
        ["D05", "DBS GROUP HOLDINGS LTD", "Financials", "Equity", "1", "0.1"],
    ])
    assert list(out.holding_ticker) == ["AAPL", "2330", "005930", "7203", "D05"]
    assert round(out.weight_pct.sum(), 6) == 6.7


def test_keeps_tba_bond_rows(monkeypatch):
    out = _parse(monkeypatch, [
        ["UM30", "UMBS 30YR TBA(REG A)", "Securitized", "Fixed Income", "1", "0.3"],
    ])
    assert list(out.holding_ticker) == ["UM30"]


def test_drops_dash_placeholder_tickers(monkeypatch):
    out = _parse(monkeypatch, [
        ["--", "ARCELLX INC CVR", "Health Care", "Equity", "1", "0"],
        ["---", "BOWL AMERICA INC ESCROW", "Consumer Discretionary", "Equity", "1", "0"],
        ["MSFT", "MICROSOFT", "Information Technology", "Equity", "1", "4.0"],
    ])
    assert list(out.holding_ticker) == ["MSFT"]


def test_drops_cash_and_derivative_asset_classes(monkeypatch):
    out = _parse(monkeypatch, [
        ["USD", "USD CASH", "Cash and/or Derivatives", "Cash", "1", "0.02"],
        ["MARGIN_USD", "FUTURES USD MARGIN BALANCE", "Cash and/or Derivatives",
         "Cash Collateral and Margins", "1", "0.03"],
        ["IXCZ6", "E-MINI HEALTH CARE SECTOR DEC 26", "Cash and/or Derivatives", "Futures", "0", "0"],
        ["JPY", "JPY/USD", "Cash and/or Derivatives", "FX", "1", "0.00001"],
        ["FWD1", "EUR/USD FWD", "Cash and/or Derivatives", "Forwards", "1", "0.0"],
        ["SWP1", "IRS USD 3.5%", "Cash and/or Derivatives", "Swaps", "1", "0.0"],
        ["NVDA", "NVIDIA", "Information Technology", "Equity", "1", "4.0"],
    ])
    assert list(out.holding_ticker) == ["NVDA"]


def test_keeps_companies_whose_names_contain_derivative_words(monkeypatch):
    out = _parse(monkeypatch, [
        ["CAP", "CAPGEMINI", "Information Technology", "Equity", "1", "0.02"],
        ["FCFS", "FIRSTCASH HOLDINGS INC", "Financials", "Equity", "1", "0.01"],
        ["OPCH", "OPTION CARE HEALTH INC", "Health Care", "Equity", "1", "0.01"],
        ["LRE", "LANCASHIRE HOLDINGS", "Financials", "Equity", "1", "0.01"],
    ])
    assert list(out.holding_ticker) == ["CAP", "FCFS", "OPCH", "LRE"]


def test_name_filter_still_applies_without_asset_class(monkeypatch):
    sheet = [["Ticker", "Name", "Weight (%)"],
             ["ESZ6", "S&P500 EMINI DEC 26", "0"],
             ["USD", "USD CASH", "0.1"],
             ["AAPL", "APPLE", "7.0"]]
    monkeypatch.setattr(fhp, "_fetch_blackrock_holdings_rows", lambda *a, **k: sheet)
    out = fhp.fetch_blackrock_etf_holdings("ACWI")
    assert list(out.holding_ticker) == ["AAPL"]


def test_keeps_money_market_sweep(monkeypatch):
    # Unchanged behaviour: the cash-sweep fund has always been kept as a holding.
    out = _parse(monkeypatch, [
        ["XTSLA", "BLK CSH FND TREASURY SL AGENCY", "Cash and/or Derivatives", "Money Market", "1", "0.18"],
    ])
    assert list(out.holding_ticker) == ["XTSLA"]


def test_index_constituents_keeps_real_names_and_drops_derivatives(monkeypatch):
    import index_constituents_pipeline as icp

    def row(*cells):
        return "<Row>" + "".join(f'<Cell><Data ss:Type="String">{c}</Data></Cell>' for c in cells) + "</Row>"

    xml = (
        '<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
        'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Worksheet ss:Name="Holdings"><Table>'
        + row(*HEADER)
        + row("FCFS", "FIRSTCASH HOLDINGS INC", "Financials", "Equity", "1", "0.1")
        + row("OPCH", "OPTION CARE HEALTH INC", "Health Care", "Equity", "1", "0.1")
        + row("--", "ARCELLX INC CVR", "Health Care", "Equity", "1", "0")
        + row("RTYZ6", "RUSSELL 2000 EMINI CME DEC 26", "Cash and/or Derivatives", "Futures", "0", "0")
        + row("USD", "USD CASH", "Cash and/or Derivatives", "Cash", "1", "0.1")
        + "</Table></Worksheet></Workbook>"
    )

    class _Resp:
        content = xml.encode("utf-8")

        def raise_for_status(self):
            pass

    monkeypatch.setattr(icp.requests, "get", lambda *a, **k: _Resp())
    out = icp.fetch_blackrock_holdings("IWM")
    assert list(out.ticker) == ["FCFS", "OPCH"]
