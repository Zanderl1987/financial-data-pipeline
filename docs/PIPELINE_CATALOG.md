# Pipeline Catalog

Every `*_pipeline.py` at the repo root, grouped by domain, with what it
fetches and which `query.py` CATALOG table(s) it lands in. Run
`python run_all.py --dry-run` for the authoritative, always-current wiring;
this doc is the narrative version.

Legend: **Key** = required `.env` variable (blank = no key needed, either a
fully public API or a scrape). "Snapshot-only" = the source has no history
endpoint — the table only grows if the pipeline runs regularly (see
`EXPERT_BRIEF.md` roadmap item 2 on accumulator continuity).

## Equity prices & market history

| Pipeline | Tables | What it fetches | Key |
|---|---|---|---|
| `price_history_pipeline.py` | `prices` | Core daily OHLCV price history (equities) | — |
| `yfinance_pipeline.py` | `market_history` | Deep daily history for indices, front-month futures, FX pairs, rate/credit ETFs (`^GSPC` to 1927, `^DJI` to 1992, `CL=F`/`GC=F` to 2000, `TLT`/`HYG` to inception) | — |
| `futures_pipeline.py` | `futures`, `cot` | Continuous front-month OHLCV for 30 futures contracts + CFTC Commitments of Traders weekly positioning | — |
| `sector_etf_pipeline.py` | `sector_etfs` | Daily OHLCV for 11 SPDR sector ETFs + 4 broad index ETFs (Schwab) | Schwab OAuth |
| `tiingo_pipeline.py` | `tiingo_prices`, `tiingo_news` | Corporate-action-adjusted EOD prices + ticker-tagged news; works without OAuth | `TIINGO` key |

## Schwab (interactive OAuth, Stage 2)

| Pipeline | Tables | What it fetches | Notes |
|---|---|---|---|
| `schwab_quotes_pipeline.py` | `schwab_quotes` | Daily snapshot of real-time quotes + fundamentals (PE, EPS, div yield, 52wk range) for DJI + sector ETFs; batches 500 symbols/call | — |
| `schwab_intraday_pipeline.py` | `schwab_intraday` | Minute-level OHLCV (1-min bars retained ~48 days, 5/10/15/30-min ~9 months by Schwab) — run daily to accumulate beyond retention | Snapshot-only |
| `schwab_movers_pipeline.py` | `schwab_movers` | Daily top-10 movers per index (gainers/losers/volume) | Snapshot-only |
| `schwab_options_pipeline.py` | `schwab_options` | Options chains with full greeks (delta/gamma/theta/vega/rho), nearest N expirations | — |
| `schwab_portfolio_pipeline.py` | `schwab_positions`, `schwab_transactions` | Mirrors the real Schwab account(s): daily position snapshots + trade/dividend transaction history | — |

OAuth is interactive (auth code expires ~30s) — must be run in a real
terminal, never in an unattended/scheduled context. `schwabdev` 3.0.4 uses
`tokens_db=` (SQLite `tokens.db`), not `tokens_file=`. Trader API endpoints
(positions/transactions) need separately enabling at developer.schwab.com;
the Market Data API works out of the box. Schwab has no historical options —
chains are snapshot-only, hence accumulating `schwab_options` daily matters.

## Options

| Pipeline | Tables | What it fetches | Key |
|---|---|---|---|
| `options_chain_pipeline.py` | `options_metrics`, `options_chain` | Options chain + derived metrics | — |
| `yahoo_options_pipeline.py` | `options_history` | Full Yahoo chain (all expirations/strikes) then per-contract historical OHLCV in a resumable second phase | — |
| `synthetic_options_pipeline.py` | `synthetic_options` | Theoretical option prices/greeks over a moneyness × DTE grid, closed-form-priced from spot/rate/vol inputs sourced elsewhere in the store — gives deep history where real captured chains are shallow | — |

## Fundamentals

| Pipeline | Tables | What it fetches | Key |
|---|---|---|---|
| `fundamentals_pipeline.py` / `sec_edgar_pipeline.py` | `fundamentals_annual`, `fundamentals_quarterly`, `sec_edgar_submissions`, `sec_edgar_xbrl_fundamentals`, `sec_edgar_efts_search` | SEC EDGAR company filings metadata + XBRL fundamentals + full-text search for DOW 30 | — (EDGAR_USER_AGENT required) |
| `simfin_pipeline.py` | `simfin_income`, `simfin_balance`, `simfin_cashflow` | Income/balance/cash-flow statements, 10+ yrs, 4,000+ US stocks (12-month delay on free tier) | `SIMFIN_API_KEY` |
| `tiingo_fundamentals_pipeline.py` | (Tiingo daily metrics + statements) | marketCap/enterpriseVal/PE/PB/PEG daily + quarterly/annual statements, 5,500+ tickers, 20+ yrs | `TIINGO` key |
| `alpha_vantage_fundamentals_pipeline.py` | `alpha_vantage_overview`, `alpha_vantage_income_statement`, `alpha_vantage_balance_sheet`, `alpha_vantage_cash_flow`, `alpha_vantage_earnings`, `alpha_vantage_earnings_calendar`, `alpha_vantage_dividends`, `alpha_vantage_insider_transactions`, `alpha_vantage_news_sentiment`, `alpha_vantage_top_gainers_losers` | Company overview, statements, earnings history for DOW 30 — rotates a subset of symbols daily to stay under 25 req/day free tier | `ALPHA_VANTAGE_API_KEY` |
| `stockanalysis_pipeline.py` | `sa_income`, `sa_balance`, `sa_cashflow`, `sa_ratios`, `sa_movers`, `sa_ipos`, `sa_ipo_calendar`, `sa_ipo_stats`, `sa_corporate_actions`, `sa_stock_list`, `sa_etf_list` | Scraped statements, ratios, movers, IPO history/calendar/stats, corporate actions | — (scrape) |
| `finviz_pipeline.py` | `finviz_movers`, `finviz_screener`, `finviz_financials`, `finviz_insider`, `finviz_sector_perf`, `finviz_industry_perf`, `finviz_country_perf`, `finviz_group_valuation` | 15 scraped datasets: movers, S&P 500 overview/financials, insider transactions, sector/industry/country performance | — (scrape) |
| `dividend_pipeline.py` / `dividend_research.py` | `dividends` | Per-symbol cash dividend history (ex/pay/record/declaration dates, amount) via Finnhub | `FINNHUB_API_KEY` |
| `tiingo_corporate_actions_pipeline.py` | (dividends/splits, 80,000+ tickers) | Comprehensive dividend + split history, 60+ yrs | `TIINGO` key |

## Finnhub (company data, events, alt-data)

| Pipeline | Tables | What it fetches | Key |
|---|---|---|---|
| `finnhub_pipeline.py` | `finnhub_profile`, `finnhub_quotes`, `finnhub_metrics`, `finnhub_recommendations`, `finnhub_price_targets`, `finnhub_upgrades`, `finnhub_news` | Company profile, real-time quotes, financial metrics, recommendation trends, price targets, upgrades/downgrades, company news | `FINNHUB_API_KEY` |
| `finnhub_events_pipeline.py` | `earnings_calendar`, `insider_transactions`, `ipo_calendar` | Market-wide earnings calendar (1 call/run — date-range query, not per-symbol), per-symbol insider Form 3/4/5 filings, IPO calendar | `FINNHUB_API_KEY` |
| `finnhub_expansion_pipeline.py` | (ESG, congressional trading, supply chain, insider sentiment, social sentiment, +more) | ~12 additional Finnhub categories: ESG scores, congressional trading, supply chain, insider/social sentiment | `FINNHUB_API_KEY` |
| `finnhub_fundamentals_pipeline.py` | (earnings history, EPS/revenue estimates, ownership, splits, peers, executives, filing sentiment, transcripts, news sentiment) | ~10 more Finnhub categories layered on the above three | `FINNHUB_API_KEY` |
| `institutional_pipeline.py` | `institutional_holdings` | SEC 13F-HR quarterly filings for a curated list of major institutional investors (via EDGAR EFTS, not Finnhub) | EDGAR_USER_AGENT |

## Short interest & positioning

| Pipeline | Tables | What it fetches | Key |
|---|---|---|---|
| `short_interest_pipeline.py` | `short_interest`, `finra_short_interest` | yfinance snapshot (default, daily-runnable: shares short, % float, days to cover) + FINRA biweekly Reg SHO short interest (`--source finra`, currently 403ing — see Known-broken) | — |
| `congressional_trades_pipeline.py` | `congressional_trades` | US House/Senate stock trade disclosures via community-maintained S3 aggregators of official eFD/Clerk filings | — |
| `sec_ftd` (fails-to-deliver) | `sec_ftd` | SEC fails-to-deliver data (used by `analytics/short_interest.py ftd_pressure`) | — |

## News, sentiment & attention

| Pipeline | Tables | What it fetches | Key |
|---|---|---|---|
| `news_sentiment_pipeline.py` | `news_sentiment` | Scores existing `finnhub_news` headlines/summaries with local VADER + a finance lexicon — offline, free, deterministic, incremental | — |
| `fed_sentiment_pipeline.py` | `fed_speeches`, `fed_sentiment` | FOMC statements + Fed official speeches (RSS, no key) scored hawkish/dovish by Claude (claude-haiku) | `ANTHROPIC_API_KEY` |
| `reddit_pipeline.py` | `reddit_posts`, `reddit_mentions` | Post volume/engagement across finance subreddits (PRAW) | `REDDIT_CLIENT_ID/SECRET` |
| `wikipedia_pipeline.py` | `wikipedia_pageviews` | Daily pageviews for DJI company pages + sector pages — attention spikes correlate with pre-earnings IV/volume | — |
| `google_trends_pipeline.py` | `google_trends_economic`, `google_trends_market`, `google_trends_sector` | Normalized (0-100) search-interest time series for financial keywords (pytrends) | — |
| `fear_greed_pipeline.py` | `fear_greed` | Crypto Fear & Greed Index, 0-100 composite, full history in one call | — |
| `tv_rating_eval.py` / `tradingview_pipeline.py` | `tv_ratings` | TradingView aggregate Technical Rating (Strong Buy…Strong Sell) — current value only, no history endpoint | Snapshot-only |

## Macro — US

| Pipeline | Tables | What it fetches | Key |
|---|---|---|---|
| `commodity_macro_pipeline.py` | `commodities`, `macro` | Core FRED macro/commodity series | `FRED_API_KEY` |
| `fred_macro_pipeline.py` | `fred_macro_housing`, `fred_macro_sentiment`, `fred_macro_industrial`, `fred_macro_consumer`, `fred_macro_trade` | ~37 more FRED series: housing, sentiment, industrial production, retail sales, PCE, trade balance, consumer credit, durable goods | `FRED_API_KEY` |
| `fred_rates_gdp_pipeline.py` | `fred_rates_gdp_interest_rates`, `..._money_supply`, `..._gdp`, `..._inflation`, `..._mortgage`, `..._commodities`, `..._exchange_rates`, `..._markets`, `..._federal_debt` | ~60 more FRED series: rates, yield curve, money supply, GDP, inflation, mortgage rates, federal debt | `FRED_API_KEY` |
| `bea_pipeline.py` | `bea_gdp`, `bea_income`, `bea_profits` | NIPA GDP components, personal income, corporate profits by industry | `BEA_API_KEY` |
| `bls_pipeline.py` | `bls_cpi`, `bls_ppi`, `bls_employment`, `bls_jolts`, `bls_unemployment` | CPI, PPI, nonfarm payrolls, JOLTS, unemployment (U-3/U-6) | `BLS_API_KEY` (optional, raises limit) |
| `bls_expansion_pipeline.py` | `bls_import_export_prices`, `bls_eci`, `bls_productivity` | Import/export price indexes, Employment Cost Index, productivity | `BLS_API_KEY` (optional) |
| `bls_oes_qcew_pipeline.py` | `bls_oes`, `bls_qcew`, `bls_ecec`, `bls_cps_demographics` | Occupational wages (800+ occupations), county/MSA employment & wages, employer costs, CPS demographics | `BLS_API_KEY` (optional) |
| `treasury_pipeline.py` / `treasury_fiscal_pipeline.py` | `treasury_debt`, `treasury_auctions` | Debt to the Penny, average interest rates by security type, Treasury auctions, MTS/DTS | — |
| `treasury_tic_pipeline.py` | `treasury_tic_holders`, `treasury_tic_slt` | Major foreign holders of US Treasuries + broader foreign portfolio holdings (annual) | — |
| `fed_soma_pipeline.py` | `fed_soma` | NY Fed weekly balance sheet holdings (Treasuries, Agency MBS/debt), ~2002+ in backfill | — |
| `nasdaq_data_link_pipeline.py` | `market_valuation`, `treasury_yield_curve` | S&P 500 Shiller CAPE/PE/dividend yield/earnings yield, Treasury yield curve | **Known-broken** — Incapsula WAF 403s everything |
| `shiller_pipeline.py` | `shiller_cape` | Robert Shiller's monthly CAPE/price/earnings/dividends back to 1871 (Yale) | — |
| `fama_french_pipeline.py` | `ff_factors`, `ff_industry` | 5-factor model (Mkt-RF, SMB, HML, RMW, CMA) + momentum + 48 industry portfolios | — |
| `cboe_pipeline.py` | `cboe_volatility` | VIX, VIX9D, VIX3M, VIX6M, VVIX, SKEW daily OHLC | — |
| `tsa_pipeline.py` | `tsa_checkpoint` | Daily TSA checkpoint traveler counts — leading indicator of travel demand | — |
| `real_estate_pipeline.py` | `fhfa_hpi`, `zillow_zhvi`, `zillow_zori` | FHFA House Price Index (national/state/MSA) + Zillow home value/rent indices | — |
| `redfin_pipeline.py` | `redfin_market_tracker` | Redfin housing market tracker — median sale/list price, homes sold, inventory, months of supply, price drops (national/metro/state) | — |
| `aqr_factors_pipeline.py` | `aqr_factors` | AQR factor library — Value & Momentum Everywhere, Quality-Minus-Junk, Time-Series Momentum monthly factor returns | — |
| `etf_holdings_pipeline.py` | `etf_holdings` | ETF holdings with per-holding quant scores (Piotroski F, Altman Z, market cap, sector) from SecuritiesDB (top-100 by weight) | — |
| `shipping_pipeline.py` | `shipping_gscpi`, `shipping_freight_ppi` | NY Fed Global Supply Chain Pressure Index + FRED freight PPI series | `FRED_API_KEY` |

## Macro — global

| Pipeline | Tables | What it fetches | Key |
|---|---|---|---|
| `world_bank_pipeline.py` | `world_bank` | GDP, growth, inflation, trade, employment, debt for 200+ countries (annual) | — |
| `oecd_pipeline.py` | `oecd_macro` | Unemployment, CPI, industrial production, long/short rates for 14 major economies | — |
| `ecb_pipeline.py` | `ecb_rates` | ECB policy rates, Euribor, EUR FX rates, HICP, Eurozone yield curve | — |
| `worldbank_pink_sheet.py` | `wb_commodities` | World Bank Pink Sheet commodity prices | — |
| `imf_commodities_pipeline.py` | `imf_commodities` | IMF PCPS commodity prices (base metals, coal, LNG, silver, fertilizers, ag) via FRED mirror | `FRED_API_KEY` |

## Commodities, energy & agriculture

| Pipeline | Tables | What it fetches | Key |
|---|---|---|---|
| `gas_price_pipeline.py` | `gas_spot`, `gas_retail` | Spot + retail gas/diesel prices | `EIA_API_KEY` |
| `eia_pipeline.py` | `eia_petroleum_stocks`, `eia_natgas_storage`, `eia_crude_production`, `eia_refinery_activity`, `eia_crude_trade`, `eia_hourly_grid` | Weekly petroleum inventories, weekly natgas storage, monthly crude production by state | `EIA_API_KEY` |
| `eia_expansion_pipeline.py` | `eia_electricity_generation`, `eia_electricity_sales`, `eia_nuclear_outages`, `eia_coal_production`, `eia_coal_trade`, `eia_international`, `eia_seds` | Electricity generation/sales, nuclear outages, coal production/trade, international energy, state-level SEDS | `EIA_API_KEY` |
| `eia_hourly_grid_pipeline.py` | `eia_hourly_grid` | Hourly demand, demand forecast, net generation, interchange — 65+ balancing authorities (EIA-930) | `EIA_API_KEY` |
| `eia_petng_prices_pipeline.py` | `eia_petroleum_spot_prices`, `eia_petroleum_futures`, `eia_refiner_margins`, `eia_petroleum_supply_demand`, `eia_natural_gas_consumption` | Spot/futures petroleum prices, refiner margins, supply/demand balance, natgas consumption, LNG | `EIA_API_KEY` |
| `metals_pipeline.py` | `metals_spot` | Real-time precious metals spot (api.metals.live) + FRED base metals history | `FRED_API_KEY` (base metals only) |
| `omkar_commodity_pipeline.py` | `omkar_commodity` | CME/NYMEX commodity futures — 30 commodities incl. lumber (100 queries/month free) | `OMKAR_API_KEY` |
| `fao_pipeline.py` | `fao_production`, `fao_prices` | Global crop production quantities/area + producer prices (major crops: wheat, maize, rice, soy, cotton, sugar) | — |
| `usda_pipeline.py` | `usda_crops`, `usda_fertilizers` | US crop production statistics + fertilizer prices (QuickStats API) | `USDA_NASS_API_KEY` (**currently 401ing, needs fresh key**) |
| `usgs_minerals_pipeline.py` | `usgs_minerals` | Monthly/annual import-export volumes and production for cobalt, manganese, lithium, graphite, nickel, rare earths, silicon | — |
| `comtrade_pipeline.py` | `comtrade_trade` | US imports/exports of battery-materials & advanced-manufacturing HS codes | `COMTRADE_API_KEY` (optional, extends history to 1988) |
| `trade_pipeline.py` | `us_imports_hs`, `us_exports_hs` | Monthly US agricultural imports/exports by HTS chapter (cereals, oilseeds, fats/oils, feed) | `CENSUS_API_KEY` |
| `noaa_climate_pipeline.py` | `noaa_climate` | Monthly weather summaries for US agricultural regions (NCEI GSOM) | — |
| `open_meteo_pipeline.py` | `open_meteo_weather` | Daily weather history (temp, precip, wind, solar, degree-days) for 25 economically significant locations, batched 5-at-a-time | — |

## Crypto & FX

| Pipeline | Tables | What it fetches | Key |
|---|---|---|---|
| `coingecko_pipeline.py` | `crypto_market`, `crypto_history` | Top-250 coin snapshot + daily OHLCV history for top 50 | `COINGECKO_API_KEY` (optional) |
| `coingecko_expansion_pipeline.py` | `coingecko_global_market`, `coingecko_coins_markets`, `coingecko_trending`, `coingecko_categories`, `coingecko_derivatives`, `coingecko_exchange_rates` | Global market cap, top coins, trending, categories, derivatives, exchange rates | `COINGECKO_DEMO_API_KEY` (optional) |
| `forex_pipeline.py` | `forex_rates` | Historical FX rates for 19 currencies vs USD (Frankfurter/ECB) | — |
| `alpha_vantage_pipeline.py` | `alpha_vantage_technical`, `alpha_vantage_forex` | Pre-computed technical indicators (RSI/MACD/SMA/EMA/Bollinger) + forex rates — unique to this source | `ALPHA_VANTAGE_API_KEY` |

## Corporate structure & reference data (Iceberg)

| Pipeline | Tables (Iceberg) | What it fetches | Key |
|---|---|---|---|
| `index_constituents_pipeline.py` | `index_members` | S&P 500, Nasdaq-100, Russell 3000/2000, Wilshire 5000 constituents (Wikipedia + stockanalysis.com + BlackRock varnish API) | — |
| `securities_reference_pipeline.py` | `securities` | Unified securities reference: EDGAR ticker/CIK universe + index membership + Finnhub profile enrichment | — |
| `fund_holdings_pipeline.py` | `fund_holdings` | ETF holdings (BlackRock varnish API) + mutual fund holdings (EdgarTools N-PORT) | — |
| `openfigi_pipeline.py` | `identifier_map` | Resolves tickers to FIGI/Composite FIGI/security type/exchange for tickers already in the store | `OPENFIGI_API_KEY` (optional, raises rate limit) |
| `sec_filings_pipeline.py` | `sec_filings` | EDGAR daily form indexes (8-K, 10-K/Q, S-1, SC 13D/G, DEF 14A) mapped CIK→ticker — the event stream for "what happens after a company files X" | EDGAR_USER_AGENT |
| `patents_pipeline.py` | `patents` | USPTO patent grants (AI/ML, semiconductors, biotech, wireless, power/EV) as an R&D-activity proxy | — |
| `openfda_pipeline.py` | `openfda_approvals`, `openfda_recalls` | Drug approvals (NDA/BLA/ANDA) + enforcement actions (recalls/warnings) | — |
| `fdic_pipeline.py` | `fdic_institutions`, `fdic_financials`, `fdic_failures` | All FDIC-insured institutions, quarterly call-report financials (back to 1992), bank failures since 1934 | — |

## Alternative / attention data

| Pipeline | Tables | What it fetches | Key |
|---|---|---|---|
| `ais_pipeline.py` | `ais_positions`, `ais_zone_summary` | Real-time vessel positions (cargo/tanker) across 10 maritime chokepoints via AISStream.io WebSocket | `AISSTREAM_API_KEY` |

## Signal health

| Pipeline | Tables | What it fetches | Notes |
|---|---|---|---|
| `signal_monitor.py` | `signal_health` | Re-scores configured TA signals over trailing windows via `event_backtest`, appends run history for drift detection | Not a data-ingestion pipeline — reads the store, doesn't add to it |

## Known-broken / dead ends

Don't re-attempt these without a genuinely new access path — see
`CLAUDE.md` and `EXPERT_BRIEF.md` for the full reasoning:

- `nasdaq_data_link_pipeline.py` — Incapsula WAF 403s everything.
- `usda_pipeline.py` — `USDA_NASS_API_KEY` returns 401 (needs a fresh key); `CENSUS_API_KEY` not set.
- `short_interest_pipeline.py --source finra` — FINRA moved the dataset behind the FINRA Query API, which needs registered OAuth client credentials from developer.finra.org; the 20-char `FINRA_API_KEY` in `.env` does not work for this.
- Baker Hughes rig count (JS SPA), AAR rail traffic (member-gated), Stooq (JS proof-of-work), Motley Fool transcripts (ToS prohibits scraping) — ruled out, no pipeline exists.
- `IEX_CLOUD_API_KEY` — service shut down 2025, dead.
