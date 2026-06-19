# Financial Data Pipeline Sources

| Data Source | Financial Instrument(s) | Action | Financial Data Type | Description |
| :--- | :--- | :--- | :--- | :--- |
| **FRED (Federal Reserve Economic Data)** | Spot Commodities, Treasury Yields, and Macroeconomic Indicators | Directly Pulling | Macroeconomic & Spot Commodity Data | Daily, weekly, and monthly time series for macro metrics (CPI, GDP, VIX, Money Supply) and spot prices for commodities, metals, and the Treasury yield curve. |
| **SEC EDGAR API** | U.S. Public Equities (DJI components default, or ~15k full-market) | Directly Pulling | Fundamental Accounting Data | Extracts XBRL financial metrics (Revenue, Net Income, EPS, Assets, Cash Flow, etc.) from 10-K and 10-Q corporate filings. |
| **Yahoo Finance (`yfinance`)** | Continuous Front-Month Futures | Directly Pulling | Futures OHLCV | Daily Open-High-Low-Close-Volume price history for 28 major futures contracts spanning Energy, Metals, Agriculture, Equity Indices, Treasuries, and FX. |
| **CFTC (`cot_reports`)** | Futures & Options Contracts | Directly Pulling & Synthetically Generating | Commitments of Traders (COT) | Weekly open interest and trader positioning. The pipeline **synthetically derives** net positioning metrics (`net_noncomm`, `net_comm`) for speculators and commercial hedgers. |
| **U.S. EIA API v2** | Spot & Retail Petroleum Products | Directly Pulling | Energy Cash Prices | Daily wholesale spot prices at major trading hubs (e.g., NY Harbor, Gulf Coast) and weekly retail consumer pump prices by U.S. region. |
| **Charles Schwab API** | U.S. Public Equities (DJI components) | Directly Pulling & Synthetically Generating | Options Chain & Greeks | Pulls full options chains (Bid/Ask, Implied Volatility, Greeks). The pipeline **synthetically generates** aggregate metrics like Open Interest Put/Call ratio, Volume Put/Call ratio, and average call delta. |
| **Charles Schwab API** | U.S. Public Equities (DJI components) | Directly Pulling & Synthetically Generating | Equity OHLCV & Technicals | Daily equity price history. The pipeline **synthetically generates** derived technical columns including log returns, percentage change, intraday change/range, and VWAP. |

*(Note: **Wikipedia** is also used across several of these pipelines as a lightweight data source to dynamically scrape the active list of Dow Jones Industrial Average ticker symbols).*
