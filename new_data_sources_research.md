# Financial Data Pipeline — New Data Sources Research
> **Last updated:** 2026-06-22  
> **Purpose:** Master reference for all candidate data sources to expand the pipeline beyond existing FRED/SEC/CFTC/EIA/Schwab feeds.  
> **Status key:** ✅ Free API/direct download · ⚠️ Registration required · 💰 Paid/commercial · 🔄 FRED proxy available

---

## Table of Contents
1. [Automotive Pricing](#1-automotive-pricing)
2. [EV & Battery Materials](#2-ev--battery-materials)
3. [Industrial Input Costs — Metals & Minerals](#3-industrial-input-costs--metals--minerals)
4. [Industrial Input Costs — Chemicals & Plastics](#4-industrial-input-costs--chemicals--plastics)
5. [Electronics & Semiconductor Components](#5-electronics--semiconductor-components)
6. [Packaging & Paper](#6-packaging--paper)
7. [Construction & Building Materials](#7-construction--building-materials)
8. [Supply Chain & Freight](#8-supply-chain--freight)
9. [Real Estate & Housing](#9-real-estate--housing)
10. [Global Commodity Benchmarks](#10-global-commodity-benchmarks)
11. [Factor Models & Academic Data](#11-factor-models--academic-data)
12. [FRED Series Master List — New Additions](#12-fred-series-master-list--new-additions)
13. [Implementation Priority Queue](#13-implementation-priority-queue)

---

## 1. Automotive Pricing

### 1.1 BLS CPI — New & Used Vehicles ✅ (already in pipeline)
| Field | Value |
|---|---|
| Source | Bureau of Labor Statistics via FRED |
| URL | `https://fred.stlouisfed.org` |
| Access | FRED API (`FRED_API_KEY`) |
| Cost | Free |
| Frequency | Monthly |
| FRED Series | `CUSR0000SETA01` (New Vehicles), `CUSR0000SETA02` (Used Cars & Trucks) |
| Fields | CPI index level |
| Notes | Best free proxy for retail vehicle price trends. Already in `commodity_macro_pipeline.py`. |

### 1.2 BLS PPI — Motor Vehicles & Parts ✅
| Field | Value |
|---|---|
| Source | BLS Producer Price Index via FRED |
| URL | `https://fred.stlouisfed.org` |
| Access | FRED API |
| Cost | Free |
| Frequency | Monthly |
| FRED Series | `WPU141` (Motor Vehicles & Equipment), `WPU1411` (Motor Vehicles — already in pipeline), `WPU1412` (Motor Vehicle Parts), `WPU141205` (Motor Vehicle Parts sub-index), `WPU14120502` (Steering & Suspension Parts), `WPU1413` (Truck & Bus Bodies) |
| Fields | PPI index level (not seasonally adjusted) |
| Notes | `WPU1411` already in pipeline. Add `WPU1412` and `WPU141` for broader coverage. |

### 1.3 BLS Auto Loan Rate ✅ (already in pipeline)
| Field | Value |
|---|---|
| FRED Series | `TERMCBCCALLNS` (48-month auto loan rate, monthly) |
| Notes | Already in `commodity_macro_pipeline.py`. |

### 1.4 Manheim Used Vehicle Value Index ⚠️ 💰
| Field | Value |
|---|---|
| Source | Cox Automotive / Manheim |
| URL | `https://www.coxautoinc.com/market-insights/` |
| Access | Proprietary — requires Cox Automotive business agreement |
| Cost | Commercial (not freely available) |
| Frequency | Monthly (headline index released publicly) |
| Notes | **Headline monthly value is published in press releases** and can be manually curated or scraped from Cox Automotive Insights. The MMR Valuations API requires a partnership. Use `CUSR0000SETA02` as FRED proxy. |

### 1.5 CarGurus Price Index ⚠️
| Field | Value |
|---|---|
| Source | CarGurus |
| URL | `https://www.cargurus.com/Cars/price-trends/` |
| Access | Web scrape or press release; no public API |
| Cost | Free (web) |
| Frequency | Monthly reports |
| Notes | CarGurus publishes Used Car Price Index reports monthly. No structured API but data can be extracted from published reports. |

### 1.6 Bureau of Economic Analysis — Motor Vehicle Output ✅
| Field | Value |
|---|---|
| Source | BEA via FRED |
| URL | `https://fred.stlouisfed.org` |
| Access | FRED API |
| Cost | Free |
| Frequency | Monthly/Quarterly |
| FRED Series | `DAUPSA` (Motor Vehicle Assemblies, monthly SAAR) |
| Fields | Vehicles assembled (millions, SAAR) |
| Notes | Tracks production volume — useful leading indicator for vehicle supply/pricing pressure. |

### 1.7 DOT/NHTSA Vehicle Sales Data ✅
| Field | Value |
|---|---|
| Source | Census Bureau via FRED |
| URL | `https://fred.stlouisfed.org` |
| Access | FRED API |
| Cost | Free |
| Frequency | Monthly |
| FRED Series | `TOTALSA` (Total Vehicle Sales, SAAR), `LAUTONSA` (Light Weight Vehicle Sales), `HTRUCKSSAAR` (Heavy Trucks) |
| Fields | Sales rate in millions, seasonally adjusted |

---

## 2. EV & Battery Materials

### 2.1 IMF Battery Raw Material Prices via FRED ✅
| Field | Value |
|---|---|
| Source | IMF Primary Commodity Prices via FRED |
| URL | `https://fred.stlouisfed.org` |
| Access | FRED API |
| Cost | Free |
| Frequency | Monthly |
| FRED Series | `PNICKUSDM` (Nickel, USD/MT), `PCOBAUSDM` (Cobalt, USD/MT), `PLITHIUMUSDM` (Lithium, USD/MT) |
| Fields | USD per metric ton, nominal |
| Notes | These are the best freely available proxies for EV battery input costs. All follow IMF PCPS naming convention. |

### 2.2 BLS PPI — Battery Manufacturing ✅
| Field | Value |
|---|---|
| Source | BLS via FRED |
| URL | `https://fred.stlouisfed.org` |
| Access | FRED API |
| Cost | Free |
| Frequency | Monthly |
| FRED Series | `PCU3359133591` (Storage Battery Manufacturing), `PCU335911335911` (Battery Manufacturing) |
| Fields | PPI index |
| Notes | Tracks downstream battery manufacturing costs, not raw materials directly. |

### 2.3 IEA Global EV Outlook Data ✅
| Field | Value |
|---|---|
| Source | International Energy Agency |
| URL | `https://www.iea.org/data-and-statistics/data-product/global-ev-outlook-2024` |
| Access | Free Excel/CSV download (no API) |
| Cost | Free |
| Frequency | Annual (April release) |
| Fields | EV sales by country, battery pack prices ($/kWh), charging infrastructure, battery demand |
| Notes | The **battery price series** ($/kWh over time) is the single best free source for long-run EV battery cost trajectory. Download annually and append to pipeline. |
| Python | `requests` + `openpyxl` to download and parse Excel |

### 2.4 BloombergNEF Battery Price Survey 💰
| Field | Value |
|---|---|
| Source | BloombergNEF |
| URL | `https://about.bnef.com/blog/lithium-ion-battery-pack-prices-rise-for-first-time-to-an-average-of-151-kwh-in-2022/` |
| Access | Annual headline number is public in press releases; full data requires subscription |
| Cost | Paid subscription |
| Notes | Widely cited $/kWh benchmark. Use IEA as free alternative. |

---

## 3. Industrial Input Costs — Metals & Minerals

### 3.1 IMF Global Metal Prices via FRED ✅
| Field | Value |
|---|---|
| Source | IMF Primary Commodity Prices via FRED |
| Access | FRED API |
| Cost | Free |
| Frequency | Monthly |
| FRED Series | `PNICKUSDM` (Nickel), `PCOBAUSDM` (Cobalt), `PLITHIUMUSDM` (Lithium), `PIORECRUSDM` (Iron Ore), `PTEAUSDM` (Tin), `PZINCUSDM` (Zinc), `PLEADUSDM` (Lead), `PBAUXUSDM` (Bauxite/Aluminum Ore) |
| Notes | All follow `P[COMMODITY]USDM` naming convention. Check FRED search for full IMF PCPS list. |

### 3.2 BLS PPI Metals ✅ (partially in pipeline)
| Field | Value |
|---|---|
| Source | BLS via FRED |
| Access | FRED API |
| Cost | Free |
| Frequency | Monthly |
| FRED Series | `WPU101` (Iron & Steel — in pipeline), `WPU1012` (Steel Mill Products — in pipeline), `WPU102` (Nonferrous Metals — in pipeline), `WPU102501` (Aluminum Mill Shapes — in pipeline), `WPU10250101` (Aluminum Ingot — in pipeline), `WPU1021` (Copper & Brass — in pipeline), `WPU102504` (Nickel & Nickel-Base Alloy Mill Shapes — **new**), `WPU10220101` (Zinc — **new**) |

### 3.3 USGS Mineral Resources Program ✅
| Field | Value |
|---|---|
| Source | US Geological Survey |
| URL | `https://www.usgs.gov/centers/national-minerals-information-center/commodity-statistics-and-information` |
| Access | Free CSV/Excel download (no API) |
| Cost | Free |
| Frequency | Annual (Mineral Commodity Summaries), Monthly (some) |
| Fields | Production, consumption, prices, trade data for 90+ minerals |
| Key Commodities | Lithium, cobalt, rare earths, graphite, platinum group metals |
| Notes | Best free source for rare earth elements and battery-critical minerals. No real-time data but annual summaries are authoritative. |
| Python | `requests` + `pandas.read_csv()` for bulk download |

### 3.4 LME (London Metal Exchange) 💰⚠️
| Field | Value |
|---|---|
| Source | LME / LSEG |
| URL | `https://www.lme.com/en/Market-Data/` |
| Access | Paid data feed; 15-min delayed quotes available free on website |
| Cost | Professional data requires subscription |
| Metals Covered | Aluminum, copper, nickel, zinc, lead, tin, cobalt, molybdenum |
| Notes | Delayed free data available via website scrape. For pipeline use, the IMF/FRED proxies are adequate alternatives. |

---

## 4. Industrial Input Costs — Chemicals & Plastics

### 4.1 BLS PPI Chemicals ✅ (partially in pipeline)
| Field | Value |
|---|---|
| Source | BLS via FRED |
| Access | FRED API |
| Cost | Free |
| Frequency | Monthly |
| FRED Series | `WPU06` (Chemicals & Allied Products — in pipeline), `WPU0911` (Industrial Chemicals — in pipeline), `WPU066` (Plastics Materials & Resins — in pipeline), `WPU091501` (Ethylene — in pipeline), `WPU091502` (Propylene — in pipeline), `WPU0916` (Industrial Gases — in pipeline) |
| New Additions | `WPU0916` (Industrial Gases), `WPU0932` (Synthetic Rubber), `WPU0714` (Industrial Organic Chemicals NEC) |

### 4.2 IMF Petrochemical Feedstock Prices via FRED ✅
| Field | Value |
|---|---|
| FRED Series | `POILWTIUSDM` (WTI as naphtha proxy), `DCOILWTICO` (daily WTI — already in pipeline) |
| Notes | Crude oil is the primary feedstock for ethylene, propylene, and most plastics. Pipeline already has WTI and Henry Hub; these are the key input cost drivers. |

### 4.3 ICIS Chemical Business 💰
| Field | Value |
|---|---|
| Source | ICIS |
| URL | `https://www.icis.com/explore/resources/news/` |
| Access | Subscription required for price data; news is partially free |
| Cost | Paid (thousands/year) |
| Covers | 300+ chemical commodity prices globally |
| Notes | Industry gold standard. Use BLS PPI WPU series as free proxy. |

### 4.4 ChemAnalyst ⚠️
| Field | Value |
|---|---|
| Source | ChemAnalyst |
| URL | `https://www.chemanalyst.com/Pricing-data/` |
| Access | Free registration for limited access; paid for full history |
| Cost | Freemium |
| Frequency | Weekly |
| Fields | Spot prices for ethylene, propylene, polyethylene (PE), polypropylene (PP), PVC, benzene, etc. |
| Notes | Useful for current price levels; limited historical depth on free tier. |

---

## 5. Electronics & Semiconductor Components

### 5.1 BLS Import Price Index — Semiconductors ✅
| Field | Value |
|---|---|
| Source | BLS via FRED |
| Access | FRED API |
| Cost | Free |
| Frequency | Monthly |
| FRED Series | `IZ3344` (Import Price Index: Semiconductor & Electronic Component Mfg), `IR21320` (Import Price Index: Semiconductors — end use) |
| Fields | Price index, not seasonally adjusted |
| Notes | Best freely available proxy for semiconductor price trends. Captures import cost of chips. |

### 5.2 BLS Import Price Index — Electronics (NAICS) ✅
| Field | Value |
|---|---|
| FRED Series | `COINDUSZ3344` (Import Price: Semiconductors from Industrialized Countries), `COOASZ3344` (Import Price: Semiconductors from Asian NICs) |
| Notes | Geographic breakdown of chip import prices — useful for supply chain risk modeling. |

### 5.3 BLS PPI — Semiconductor & Electronic Manufacturing ✅
| Field | Value |
|---|---|
| Source | BLS via FRED |
| Access | FRED API |
| Cost | Free |
| Frequency | Monthly |
| FRED Series | `PCU3344033440` (Semiconductor & Electronic Component Mfg), `PCU334413344` (Semiconductor Manufacturing) |
| Fields | PPI index for domestic production costs |

### 5.4 Taiwan Semiconductor Price Index (TAIEX Component) ⚠️
| Field | Value |
|---|---|
| Source | Taiwan Stock Exchange / Quandl |
| Notes | Taiwan dominates global foundry market (TSMC). The TAIEX Electronics sub-index tracks the sector broadly. No free API for individual component prices. |

### 5.5 USGS Rare Earth Data ✅
| Field | Value |
|---|---|
| Source | USGS National Minerals Information Center |
| URL | `https://www.usgs.gov/centers/national-minerals-information-center/rare-earths-statistics-and-information` |
| Access | Free PDF/XLS downloads |
| Covers | Neodymium, dysprosium, lanthanum, cerium (critical for EV motors, electronics) |
| Frequency | Annual |

---

## 6. Packaging & Paper

### 6.1 BLS PPI — Packaging Materials ✅
| Field | Value |
|---|---|
| Source | BLS via FRED |
| Access | FRED API |
| Cost | Free |
| Frequency | Monthly |
| FRED Series | `WPU0832` (Paper & Paperboard — in pipeline), `WPU0912` (Paperboard Containers — **new**), `WPU1023` (Aluminum Cans — **new**), `WPU091303` (PET Resin — **new**) |
| Notes | `WPU0832` already in pipeline. Add container/packaging sub-series. |

### 6.2 RISI / Fastmarkets RISI 💰
| Field | Value |
|---|---|
| Source | Fastmarkets RISI |
| URL | `https://www.fastmarkets.com/forest-products/` |
| Access | Subscription |
| Covers | Containerboard, kraft linerboard, corrugated medium, pulp, newsprint |
| Notes | Industry benchmark for paper/packaging. No free access. Use BLS PPI `WPU0832` as proxy. |

---

## 7. Construction & Building Materials

### 7.1 BLS PPI — Construction Materials ✅ (partially in pipeline)
| Field | Value |
|---|---|
| Source | BLS via FRED |
| Access | FRED API |
| Cost | Free |
| Frequency | Monthly |
| FRED Series | `WPU081` (Lumber & Wood Products — in pipeline), `WPU0811` (Softwood Lumber — in pipeline), `WPU0731` (Softwood Plywood — in pipeline), `WPU0561` (Ready-Mix Concrete — in pipeline) |
| New FRED Series | `WPU1321` (Gypsum Products — **new**), `WPU1322` (Gypsum Wallboard — **new**), `WPU1311` (Flat Glass — **new**), `WPU132` (Stone, Clay & Glass Products — **new**), `WPU136` (Concrete Ingredients — **new**) |

### 7.2 Random Lengths (Lumber Cash Prices) ⚠️
| Field | Value |
|---|---|
| Source | Random Lengths Publications |
| URL | `https://www.randomlengths.com` |
| Access | Subscription; weekly framing lumber price tables |
| Cost | Paid |
| Notes | Industry standard for cash lumber prices (not futures). Use CME Lumber futures (via yfinance: `LBS=F`) or BLS PPI `WPU0811` as free proxy. |

### 7.3 APA – The Engineered Wood Association ✅
| Field | Value |
|---|---|
| Source | APA |
| URL | `https://www.apawood.org/panel-guide` |
| Access | Free monthly price reports (PDF) |
| Cost | Free |
| Frequency | Monthly |
| Fields | Plywood and OSB panel prices by thickness/grade |
| Notes | APA publishes free monthly structural panel price reports. Can be parsed with `pdfplumber`. |

### 7.4 ENR Building Cost Index ⚠️
| Field | Value |
|---|---|
| Source | Engineering News-Record |
| URL | `https://www.enr.com/economics` |
| Access | Limited free access; some data behind paywall |
| Cost | Freemium |
| Frequency | Monthly |
| Fields | Building Cost Index (BCI), Construction Cost Index (CCI) |
| Notes | BCI = labor + materials composite. CCI = skilled labor intensive. Historical data requires subscription. Monthly current values are published publicly. |

### 7.5 Census Bureau — Construction Put in Place ✅
| Field | Value |
|---|---|
| Source | Census Bureau via FRED |
| Access | FRED API |
| Cost | Free |
| Frequency | Monthly |
| FRED Series | `TTLCONS` (Total Construction Spending), `PRRESCONS` (Private Residential), `PNRESCONS` (Private Non-Residential) |
| Notes | Tracks construction spending volume — correlates with materials demand. |

---

## 8. Supply Chain & Freight

### 8.1 NY Fed Global Supply Chain Pressure Index (GSCPI) ✅
| Field | Value |
|---|---|
| Source | Federal Reserve Bank of New York |
| URL | `https://www.newyorkfed.org/research/policy/gscpi` |
| **Direct Download** | `https://www.newyorkfed.org/medialibrary/research/interactives/gscpi/downloads/gscpi_data.xlsx` |
| Access | Free Excel download (no API key needed) |
| Cost | Free |
| Frequency | Monthly (released 4th business day of month) |
| Fields | GSCPI composite score (standard deviations from mean), sub-indices |
| History | Back to 1997 |
| Python | `pd.read_excel(url)` direct |
| Pipeline File | `gscpi_pipeline.py` (to be created) |
| Storage | `storage/raw/supply_chain/` |

### 8.2 Baltic Exchange Sub-Indices via FRED ✅
| Field | Value |
|---|---|
| Source | Baltic Exchange via FRED |
| Access | FRED API |
| Cost | Free |
| Frequency | Daily |
| FRED Series | `BALDRYINDEXP` (Baltic Dry Index — already in pipeline), `BCITCINDEX` (Baltic Capesize — **new**), `BPITCINDEX` (Baltic Panamax — **new**) |
| Notes | BDI already in pipeline. Capesize and Panamax sub-indices give more granular shipping cost signals. |

### 8.3 Cass Freight Index ✅
| Field | Value |
|---|---|
| Source | Cass Information Systems via FRED |
| Access | FRED API |
| Cost | Free |
| Frequency | Monthly |
| FRED Series | `CASSFREIGHTINDX` (Cass Freight Expenditures Index) |
| Fields | Total North American freight expenditures index |
| Notes | Broad freight spend indicator covering all modes (truck, rail, air, ocean). |

### 8.4 BLS Import/Export Price Indices ✅
| Field | Value |
|---|---|
| Source | BLS via FRED |
| Access | FRED API |
| Cost | Free |
| Frequency | Monthly |
| FRED Series | `IR` series for imports, `IX` series for exports |
| Key Series | `EIUIR` (All Imports), `EIUX` (All Exports), `IQ` (Import Price: Industrial Supplies), `IR21320` (Semiconductors — see §5.1) |
| Notes | Broad import/export price trends. Useful for identifying imported inflation. |

### 8.5 Census Bureau — Trade in Goods ✅
| Field | Value |
|---|---|
| Source | Census Bureau via FRED |
| Access | FRED API |
| Cost | Free |
| Frequency | Monthly |
| FRED Series | `BOPGSTB` (US Goods Trade Balance — already in pipeline), `IMPCH` (Imports from China), `EXPCH` (Exports to China) |

### 8.6 Freightos Baltic Index (FBX) ⚠️ 💰
| Field | Value |
|---|---|
| Source | Freightos |
| URL | `https://developer.freightos.com/` |
| Access | Commercial API (registration + approval required) |
| Cost | Paid (professional tier) |
| Frequency | Daily/Weekly |
| Fields | Container spot rates by trade lane (e.g., China-US West Coast, China-Europe) |
| Notes | Not freely available. **Alternative:** Use BDI (FRED) + GSCPI as free container shipping proxies. |

### 8.7 Drewry World Container Index ✅ (partial)
| Field | Value |
|---|---|
| Source | Drewry |
| URL | `https://www.drewry.co.uk/supply-chain-advisors/supply-chain-expertise/world-container-index-assessed-by-drewry` |
| Access | Weekly headline rate published free; historical data paid |
| Cost | Freemium |
| Frequency | Weekly |
| Fields | $/FEU composite and 8 trade lane breakdowns |
| Notes | Headline composite WCI is published weekly for free in press releases. Historical back-data requires subscription. |

---

## 9. Real Estate & Housing

### 9.1 Zillow Research Data ✅
| Field | Value |
|---|---|
| Source | Zillow Research |
| URL | `https://www.zillow.com/research/data/` |
| Access | Free bulk CSV downloads (no API key) |
| Cost | Free |
| Frequency | Monthly |
| Fields | Zillow Home Value Index (ZHVI) by metro/zip/type, Zillow Observed Rent Index (ZORI), days on market, inventory, list price cuts |
| Python | `pd.read_csv(url)` direct on Zillow CDN links |
| Key URLs | `https://files.zillowstatic.com/research/public_csvs/zhvi/Metro_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv` |
| Pipeline File | `zillow_pipeline.py` (to be created) |
| Storage | `storage/raw/real_estate/zillow/` |

### 9.2 Redfin Research Data ✅
| Field | Value |
|---|---|
| Source | Redfin Data Center |
| URL | `https://www.redfin.com/news/data-center/` |
| Access | Free TSV/CSV downloads |
| Cost | Free |
| Frequency | Weekly/Monthly |
| Fields | Median sale price, homes sold, days on market, inventory, price drops %, sale-to-list ratio |
| Notes | More granular/faster than Zillow (weekly data). National and metro-level. |
| Python | `pd.read_csv(url, sep='\t')` |

### 9.3 FRED — Mortgage & Housing ✅ (partially in pipeline)
| Field | Value |
|---|---|
| Source | Various via FRED |
| Access | FRED API |
| Cost | Free |
| Frequency | Weekly/Monthly |
| FRED Series (new) | `MORTGAGE30US` (30-yr fixed rate), `MORTGAGE15US` (15-yr fixed), `MBAVCHNG` (MBA Mortgage App Index), `RHORUSQ156N` (US Homeownership Rate), `EVACANTUSQ176N` (Vacant Housing Units) |
| Already in Pipeline | `CSUSHPINSA`, `HPIPONM226S`, `HOUST`, `PERMIT`, `EXHOSLUSM495S`, `MSPUS`, `COMRPIUSQ156N` |

### 9.4 HUD PD&R Housing Market Data ✅
| Field | Value |
|---|---|
| Source | HUD Office of Policy Development and Research |
| URL | `https://www.huduser.gov/portal/datasets/hads/hads.html` |
| Access | Free download |
| Cost | Free |
| Frequency | Annual |
| Fields | American Housing Survey, Fair Market Rents by metro, housing affordability index |
| Notes | Fair Market Rents (FMR) are a useful leading indicator for rental cost trends. |

### 9.5 NCREIF Property Index ⚠️
| Field | Value |
|---|---|
| Source | NCREIF |
| URL | `https://www.ncreif.org/data-products/` |
| Access | Member access required for full data; quarterly summaries published free |
| Cost | Membership (institutional) |
| Frequency | Quarterly |
| Fields | Total returns, income returns, appreciation for commercial RE by property type |

---

## 10. Global Commodity Benchmarks

### 10.1 World Bank Pink Sheet ✅
| Field | Value |
|---|---|
| Source | World Bank |
| **Direct Download URL** | `https://thedocs.worldbank.org/en/doc/5d903e848db1d1b83e0ec8f144e122b1-0350022026/original/CMO-Historical-Data-Monthly.xlsx` |
| Access | Free direct download (no API key) |
| Cost | Free |
| Frequency | Monthly (updated ~5th of month) |
| History | Back to 1960 |
| Coverage | 70+ commodities: energy, metals, agriculture, fertilizers |
| Format | Multi-sheet Excel; each sheet = one commodity |
| Python | `pd.read_excel(url, sheet_name=None)` |
| Pipeline File | `world_bank_pink_sheet_pipeline.py` (created) |
| Storage | `storage/raw/world_bank/` |

### 10.2 IMF Primary Commodity Prices (PCPS) ✅
| Field | Value |
|---|---|
| Source | International Monetary Fund |
| URL | `https://data.imf.org/?sk=471dddf8-d8a7-499a-81ba-5b332c01f8b9` |
| Access | Free SDMX 3.0 REST API (no key required) |
| Cost | Free |
| Frequency | Monthly |
| Python Library | `imfp` (`pip install imfp`) |
| Key API Call | `imfp.imf_data(database_id='PCPS', ...)` |
| Coverage | 60+ commodity price series |
| Notes | Overlaps with FRED IMF series but provides direct access without FRED API key; also offers more granular commodity breakdown. |

### 10.3 Kenneth French Data Library (Fama-French Factors) ✅
| Field | Value |
|---|---|
| Source | Dartmouth / Kenneth French |
| URL | `https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html` |
| Access | Free ZIP/CSV downloads; `pandas-datareader` support |
| Cost | Free |
| Frequency | Daily / Monthly |
| Key Download URLs | `https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_Factors_CSV.zip` (3-factor monthly), `https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_Factors_daily.zip` (daily) |
| Python | `pandas_datareader.data.DataReader('F-F_Research_Data_Factors', 'famafrench')` |
| Fields | Mkt-RF, SMB, HML, RF (monthly %), momentum (MOM), 5-factor, industry portfolios |
| Pipeline File | `french_factors_pipeline.py` (to be created) |
| Storage | `storage/raw/factors/` |

---

## 11. Factor Models & Academic Data

### 11.1 AQR Data Library ✅
| Field | Value |
|---|---|
| Source | AQR Capital Management |
| URL | `https://www.aqr.com/Insights/Datasets` |
| Access | Free Excel downloads |
| Cost | Free |
| Frequency | Monthly / Annual updates |
| Fields | Value, momentum, quality, low-beta factor returns; BAB (Betting Against Beta); QMJ (Quality Minus Junk) |
| Notes | Complements Fama-French. All freely downloadable. |

---

## 12. FRED Series Master List — New Additions

> Series already in `commodity_macro_pipeline.py` are marked (existing). New series to add are unmarked.

### Automotive
| FRED Series | Description | Freq |
|---|---|---|
| `WPU1412` | PPI: Motor Vehicle Parts & Accessories | Monthly |
| `WPU141` | PPI: Motor Vehicles & Equipment (broad) | Monthly |
| `DAUPSA` | Motor Vehicle Assemblies (SAAR) | Monthly |
| `TOTALSA` | Total Vehicle Sales (SAAR) | Monthly |
| `LAUTONSA` | Light Vehicle Sales | Monthly |

### EV / Battery Materials
| FRED Series | Description | Freq |
|---|---|---|
| `PNICKUSDM` | IMF: Global Price of Nickel | Monthly |
| `PCOBAUSDM` | IMF: Global Price of Cobalt | Monthly |
| `PLITHIUMUSDM` | IMF: Global Price of Lithium | Monthly |
| `PCU3359133591` | PPI: Storage Battery Manufacturing | Monthly |

### Metals (new)
| FRED Series | Description | Freq |
|---|---|---|
| `WPU102504` | PPI: Nickel & Nickel-Base Alloy Mill Shapes | Monthly |
| `PZINCUSDM` | IMF: Global Price of Zinc | Monthly |
| `PLEADUSDM` | IMF: Global Price of Lead | Monthly |
| `PTEAUSDM` | IMF: Global Price of Tin | Monthly |
| `PIORECRUSDM` | IMF: Global Price of Iron Ore | Monthly |

### Electronics / Semiconductors
| FRED Series | Description | Freq |
|---|---|---|
| `IZ3344` | Import Price: Semiconductor & Electronic Mfg | Monthly |
| `IR21320` | Import Price: Semiconductors (end use) | Monthly |
| `PCU3344033440` | PPI: Semiconductor & Electronic Component Mfg | Monthly |

### Chemicals / Plastics (new)
| FRED Series | Description | Freq |
|---|---|---|
| `WPU0932` | PPI: Synthetic Rubber | Monthly |
| `WPU0714` | PPI: Industrial Organic Chemicals NEC | Monthly |

### Construction (new)
| FRED Series | Description | Freq |
|---|---|---|
| `WPU1321` | PPI: Gypsum Products | Monthly |
| `WPU1322` | PPI: Gypsum Wallboard | Monthly |
| `WPU1311` | PPI: Flat Glass | Monthly |
| `TTLCONS` | Total Construction Spending | Monthly |

### Supply Chain / Freight
| FRED Series | Description | Freq |
|---|---|---|
| `CASSFREIGHTINDX` | Cass Freight Expenditures Index | Monthly |
| `BCITCINDEX` | Baltic Capesize Index | Daily |
| `BPITCINDEX` | Baltic Panamax Index | Daily |

### Mortgage / Housing (new)
| FRED Series | Description | Freq |
|---|---|---|
| `MORTGAGE30US` | 30-Year Fixed Mortgage Rate | Weekly |
| `MORTGAGE15US` | 15-Year Fixed Mortgage Rate | Weekly |
| `MBAVCHNG` | MBA Mortgage Application Volume | Weekly |
| `RHORUSQ156N` | US Homeownership Rate | Quarterly |

---

## 13. Implementation Priority Queue

> Ordered by: data value × pipeline complexity × cost (free-first)

| Priority | Source | Pipeline File | Access | Est. Effort |
|---|---|---|---|---|
| **P0** | NY Fed GSCPI | `gscpi_pipeline.py` | Free direct URL | Low — `pd.read_excel(url)` |
| **P0** | World Bank Pink Sheet | `world_bank_pink_sheet_pipeline.py` | Free direct URL | Low — `pd.read_excel(url)` |
| **P0** | New FRED series (approx. 25 series) | Extend `commodity_macro_pipeline.py` | FRED API | Low — add to SERIES dict |
| **P1** | Zillow Research Data | `zillow_pipeline.py` | Free CSV CDN | Low — `pd.read_csv(url)` |
| **P1** | Redfin Research Data | `redfin_pipeline.py` | Free TSV | Low — `pd.read_csv(url, sep='\t')` |
| **P1** | Kenneth French Factors | `french_factors_pipeline.py` | Free ZIP | Medium — parse non-standard CSV |
| **P1** | IEA EV Battery Prices | `iea_ev_pipeline.py` | Free Excel (annual) | Low — annual download + append |
| **P2** | IMF PCPS (via `imfp`) | `imf_pipeline.py` | Free API | Medium — SDMX query construction |
| **P2** | USGS Minerals Data | `usgs_minerals_pipeline.py` | Free PDF/XLS | Medium — format varies by commodity |
| **P3** | AQR Factor Data | `aqr_factors_pipeline.py` | Free Excel | Medium — parse Excel tables |
| **P3** | Redfin Weekly (granular) | Extend `redfin_pipeline.py` | Free TSV | Low |

---

## Notes & Constraints

- **Manheim (MUVVI):** Proprietary. Use `CUSR0000SETA02` (BLS CPI Used Cars) as free proxy.
- **Freightos FBX:** Commercial API only. Use BDI + GSCPI as free shipping proxies.
- **LME metals:** Delayed/paid. Use IMF PCPS via FRED (`PNICKUSDM`, etc.) as free proxy.
- **ICIS Chemicals:** Subscription. Use BLS PPI `WPU06` / `WPU066` as free proxy.
- **All FRED additions** flow through existing `commodity_macro_pipeline.py` — just add series IDs to the `SERIES` dict.
- **Non-FRED sources** (GSCPI, World Bank, Zillow, Redfin, French) require individual pipeline scripts with their own storage subdirectories.

---

*Research conducted 2026-06-22.*

---
## Appendix: Subagent Research Reports

### A. Automotive Pricing Research
The subagent has identified several valuable FRED Series IDs tracking automotive production, consumer prices, and producer prices:
- **`TOTALSA`**: Total Vehicle Sales (SAAR)
- **`DAUPSA`**: Domestic Auto Production
- **`WPU1411`**: PPI for Motor Vehicles (already in pipeline)
- **`WPU1412`**: PPI for Motor Vehicle Parts and Accessories
- **`PCU334413334413`**: PPI for Semiconductor and Related Device Manufacturing
- **`PNICKUSDM`**: IMF Global Price of Nickel (Monthly)
- **`PCOBAUSDM`**: IMF Global Price of Cobalt (Monthly)

### B. Industrial Components Research
The subagent has detailed several key industrial pricing series:
- **`PCU33441K33441K4`**: PPI for Capacitors for Electronic Circuitry
- **`PCU33441K33441K5`**: PPI for Resistors for Electronic Circuitry
- **`PCU334412334412`**: PPI for Bare Printed Circuit Board Manufacturing
- **`WPU0531`**: PPI for Natural Gas
- **`WPU0543`**: PPI for Industrial Electric Power
- **`WPU10260314`**: PPI for Copper Wire and Cable
- **`WPU1322`**: PPI for Hydraulic Cement

### C. Supply Chain & Trade Research
Key supply chain and freight series identified:
- **`FRGSHPUSM649NCIS`**: Cass Freight Shipments Index
- **`FRGEXPUSM649NCIS`**: Cass Freight Expenditures Index
- **`WPU3012`**: PPI for Truck Transportation of Freight
- **`WPU301401`**: PPI for Air Transportation of Freight
- **`GSCPI` (Direct Excel)**: Global Supply Chain Pressure Index from the New York Fed

### D. Housing & Real Estate Research
Key real estate and construction series:
- **`WPU13710102`**: PPI for Gypsum Building Materials (Drywall)
- **`MORTGAGE30US`**: 30-Year Fixed Mortgage Rate (Weekly)
- **`EVACANTUSQ176N`**: Vacant Housing Units in the US
- **`ZORI` (Zillow Observed Rent Index)**: Directly downloaded from Zillow Research (CSV format)
- **Redfin weekly market trackers** (Direct download link for TSV)
