# Commodity & Supply Chain Data Sources — Research Notes
*Researched 2026-06-22. Identifies free data sources for raw materials, inputs, and trade flows not yet in the pipeline.*

---

## Tier 1 — Real APIs, Free, High Value

### USDA NASS QuickStats
- **URL**: https://quickstats.nass.usda.gov/api
- **Key**: Free registration at https://quickstats.nass.usda.gov/api
- **What**: ~65 agricultural commodities — corn, wheat, soybeans, cotton, sugar, coffee, cocoa prices, production, yield by state. Monthly and annual. Decades of history.
- **Format**: JSON/CSV
- **Notes**: Gold standard for US crop data. Includes 35+ market year averages.

### USDA Fertilizer Prices (Open Ag Transport)
- **URL**: https://agtransport.usda.gov
- **Key**: None required
- **What**: Monthly fertilizer prices by region — potash, phosphate, urea, DAP, MAP, anhydrous ammonia. Critical for agriculture input cost tracking.
- **Format**: CSV/Excel downloads

### FAO FAOSTAT
- **URL**: https://www.fao.org/faostat / http://api.data.fao.org/1.0/docs/data_access.html
- **Key**: None required
- **What**: UN food/agriculture statistics for 245+ countries from 1961. Production, trade, prices, emissions. Global counterpart to USDA NASS.
- **Format**: JSON via REST API; also `faodata` Python package on PyPI
- **Notes**: New developer API portal launched recently.

### Census Bureau International Trade API (HTS Codes)
- **URL**: https://api.census.gov/data/timeseries/intltrade/imports/hs and .../exports/hs
- **Key**: Free at https://api.census.gov/data/key_signup.html
- **What**: Monthly US imports and exports by **HTS (Harmonized Tariff Schedule) code**. Can query "imports of polysilicon from China" or "exports of integrated circuits to Taiwan" by specific product code. 2010–present.
- **Format**: JSON
- **Docs**: https://www.census.gov/foreign-trade/reference/guides/Guide_to_International_Trade_Datasets.pdf
- **Notes**: 56 export + 85 import parameters. Multiple classification systems (HS, SIC, SITC, NAICS). HIGH priority — tracks actual component-level flows.

### UN Comtrade
- **URL**: https://comtrade.un.org
- **Key**: Free registration
- **Limits**: 500 API calls/day, 100K records per call
- **What**: International trade by HS code, ~200 countries, annual 1988+, monthly 2000+. International version of Census trade data.
- **Format**: JSON; Python library `comtradeapicall`, R package `comtradr`
- **Notes**: Complements Census API for non-US bilateral trade flows.

### IMF Primary Commodity Prices (PCPS)
- **URL**: https://data.imf.org/en/Resource-Pages/IMF-API
- **Key**: None required (SDMX 2.1 / 3.0 API)
- **What**: 45+ commodity prices (energy, metals, food, fertilizers) for 182 economies from 1962. Also CTOT (Commodity Terms of Trade).
- **Format**: SDMX/JSON
- **Notes**: Covers many things FRED and World Bank miss. Contact: DataHelp@imf.org

### BEA API (Bureau of Economic Analysis)
- **URL**: https://www.bea.gov/resources/for-developers
- **Key**: Free (name + email only)
- **What**: Trade in goods/services by industry, input-output accounts (what industries buy from each other), international investment position.
- **Datasets**: IntlServTrade, IntlInvPosFA, ITA, MNE
- **Format**: XML and JSON
- **Notes**: Input-output tables are useful for tracing component dependencies across industries.

### NOAA Climate Data Online
- **URL**: https://www.ncei.noaa.gov/cdo-web / https://www.ncei.noaa.gov/access/services/data/v1
- **Key**: Free token at https://www.ncdc.noaa.gov/cdo-web/token
- **Limits**: 5 req/sec, 10K req/day
- **What**: Daily/monthly temperature, precipitation, wind, degree days, AGRMET agricultural meteorological data. Historical back to 1800s for some stations.
- **Format**: JSON
- **Notes**: Important for agricultural yield context. Older endpoint deprecated — use v1 above.

### WSTS (World Semiconductor Trade Statistics)
- **URL**: https://www.wsts.org
- **Key**: None (free download)
- **What**: Four decades of monthly semiconductor shipment data by product type (logic, memory, analog, discrete, etc.), region, and end-use. Value, units, ASP.
- **Format**: Excel downloads
- **Notes**: No REST API; structured Excel files. Best free source for semiconductor market volume data.

### BLS PPI — Expanded Series (beyond current pipeline)
- **URL**: https://www.bls.gov/ppi/databases / accessed via existing FRED API
- **What**: 10,000+ PPI series. Key series NOT currently in pipeline:
  - `PCU325211325211` — Plastics material & resin manufacturing
  - `PCU325311325311A` — Nitrogenous fertilizer manufacturing
  - `WPU0652013A` — Synthetic ammonia, nitric acid, ammonium compounds, urea
  - `WPU061` — Industrial chemicals
  - `WPU10` — Metals and metal products (detailed)
  - `PCU3334` — Computer and electronic product manufacturing inputs
  - `WPU0571` — Softwood lumber
  - `WPU0561` — Copper and copper products
- **Notes**: All accessible via existing FRED API key. Just add series codes.

---

## Tier 2 — Free with Caveats

### Metals-API / Metals.Dev / api.metals.live
- **URLs**: https://metals.dev (fully free), https://api.metals.live (free), https://www.metals-api.com (free tier)
- **What**: Real-time and historical spot prices — steel (HRC, scrap, rebar), aluminum, copper, nickel, zinc, tin, lead.
- **Notes**: Metals.Dev requires no credit card; api.metals.live is public. Cover base metals beyond what FRED provides. Evaluate which has best historical depth.

### World Bank Pink Sheet
- **URL**: https://thedocs.worldbank.org/en/doc/18675f1d1639c7a34d463f59263ba0a2-0050012025/world-bank-commodities-price-data-the-pink-sheet
- **Key**: None
- **What**: Monthly commodity prices across all categories (energy, metals, agriculture, fertilizers, beverages) going back to 1960.
- **Format**: Excel files (CMO-Historical-Data-Monthly.xlsx, CMO-Historical-Data-Annual.xlsx)
- **Notes**: No REST API — download and parse Excel monthly. High historical value. World Bank API (already in pipeline) may expose some of this data via indicator codes.

### USITC DataWeb
- **URL**: https://dataweb.usitc.gov
- **Key**: Account required for API
- **What**: US international trade statistics and tariff data, 1989–present. Classification: HTS, SIC, SITC, NAICS.
- **Notes**: Complements Census Trade API. Web interface is free.

### Quandl / Nasdaq Data Link (COM dataset)
- **URL**: https://data.nasdaq.com/data/COM-wiki-commodity-prices/documentation
- **Key**: Free NASDAQ Data Link key
- **What**: Historical commodity prices going back to 1950s on some series — coal, uranium, natural gas, and others.
- **Format**: CSV, JSON, XML
- **Notes**: Already have NASDAQ_DATA_LINK_API_KEY in .env — may just need to add series.

### Benchmark Mineral Intelligence (free tier)
- **URL**: https://www.benchmarkminerals.com
- **Key**: Free registration
- **Limits**: 3 pieces of content/month on free tier
- **What**: Lithium carbonate/hydroxide, cobalt, nickel, manganese, rare earths (neodymium, dysprosium, terbium). Battery supply chain focus.
- **Notes**: Free tier too limited for automated pipeline; better as manual reference. Paid tier has LME futures data.

### Manheim MMR Valuations API
- **URL**: https://developer.manheim.com
- **Key**: Account/API registration required
- **What**: Wholesale used vehicle prices by VIN or year/make/model/trim. 5M+ annual auction transactions. Nightly refresh, back to 2018.
- **Notes**: Likely free for research but gated. Best free source for vehicle price tracking.

### Fastmarkets / IEA Battery Materials
- **Fastmarkets**: https://www.fastmarkets.com/metals-and-mining/battery-raw-materials/price-data — IOSCO-compliant benchmark prices for lithium, cobalt, nickel. Paid.
- **IEA**: https://www.iea.org/data-and-statistics — Historical price data 2015–2024 for battery materials and lithium-ion cells. Free charts/downloads.
- **Notes**: IEA data is free but not API-accessible in structured form.

---

## Tier 3 — Industry Sources (No Clean API / Mostly Paid)

### Petrochemical Feedstocks
- **Intratec.us**: Ethylene, propylene, benzene, toluene, xylene (BTX), ammonia, methanol prices. US, China, SE Asia, Europe. Free preview only; Excel/API exports paid.
- **ICIS**: 300+ chemical markets. Free exploration, data behind paywall.
- **ChemAnalyst**: 1000+ global commodities. Free trial only.
- **Alternative**: BLS FRED series cover ammonia (`WPU0652013A`) and industrial chemicals (`WPU061`) as PPI proxies.

### Rare Earth Elements
- **Trading Economics**: https://tradingeconomics.com/commodity/neodymium — Free charts for neodymium, other REEs.
- **Shanghai Metals Market (SMM)**: https://www.metal.com/Rare-Earth-Oxides — Current and historical rare earth oxide prices. China-focused.
- **Asian Metal**: https://www.asianmetal.com/RareEarthsPrice — Free pricing + production/inventory stats.
- **Notes**: No clean API for any of these. Best available free data for REEs.

### Semiconductor Component Pricing
- **What exists**: WSTS for market volumes (see Tier 1). ETO ChipExplorer (https://eto.tech/dataset-docs/chipexplorer) maps supply chain tools/materials by country/firm.
- **What doesn't exist free**: Photoresist prices, specialty gas (NF3, WF6) pricing, CMP slurry costs, silicon wafer spot prices. These are proprietary B2B transactions.
- **Proxy approach**: Track BLS PPI `PCU3334` (computer/electronic product manufacturing) + Census HTS imports for HS codes covering semiconductor chemicals.

### Plastics Resin Prices
- **Plastics Technology**: https://www.ptonline.com/topics/resin-pricing — PE, PP, PVC, PET monthly pricing as editorial content, not API.
- **Better alternative**: FRED `PCU325211325211` (Plastics material & resin manufacturing PPI) — programmatic, monthly.

### USGS Minerals Information
- **URL**: https://mrdata.usgs.gov / https://www.usgs.gov/centers/gggsc/science/usmin-mineral-deposit-database
- **What**: Geospatial data on US mineral deposits, mines, production statistics for critical minerals (cobalt, lithium, rare earths). Annual Mineral Commodity Summaries.
- **Notes**: Data downloads available; API via MRData portal. More useful for supply-side capacity than pricing.

---

## Key HTS Codes for Component Tracking (Census Trade API)

| HTS Code | Description |
|---|---|
| 2804.61 | Silicon (>99.99% pure) — polysilicon |
| 2804.69 | Silicon (other) |
| 8541.10 | Diodes |
| 8541.21–29 | Transistors |
| 8542.31–39 | Electronic integrated circuits (processors, memory, etc.) |
| 7601 | Unwrought aluminum |
| 7403 | Refined copper, unwrought |
| 2804.50 | Boron, tellurium (semiconductor dopants) |
| 2809–2811 | Acids (hydrofluoric, nitric, phosphoric — chip fab chemicals) |
| 2903.39 | Halogenated hydrocarbons (specialty gases) |
| 8486 | Semiconductor manufacturing equipment (lithography, etc.) |
| 3901–3904 | Polyethylene, PP, PVC resins |
| 3102–3105 | Fertilizers (nitrogenous, phosphate, potassium) |
| 2606 | Aluminum ores and concentrates |
| 2603 | Copper ores and concentrates |
| 2602 | Manganese ores |
| 2825.30 | Vanadium oxides (battery materials) |
| 2846 | Rare earth compounds |

---

## Semiconductor Bill of Materials (Reference)

Key inputs tracked by supply chain analysts for a leading-edge chip:

| Material | Role | Best Free Data Source |
|---|---|---|
| Silicon wafer (300mm) | Substrate | WSTS volumes; no spot price API |
| Polysilicon | Wafer feedstock | Census HTS 2804.61; PPI proxies |
| Photoresist | Lithography | No free data; industry press |
| Specialty gases (NF3, WF6, SiH4) | Etch/deposition | No free API; BLS proxies |
| CMP slurries | Planarization | No free data |
| Copper (for interconnects) | Wiring | Metals-API, LME, FRED |
| Cobalt (barrier layer) | Interconnects | Benchmark Minerals (limited free) |
| Rare earths (neodymium) | Magnets in equipment | SMM, Trading Economics |
| HF acid | Cleaning/etch | BLS industrial chemicals PPI |
| Ultra-pure water | Fab process | N/A (utility cost, not commodity) |

---

## EV / Battery Bill of Materials (Reference)

| Material | Role | Best Free Data Source |
|---|---|---|
| Lithium carbonate / hydroxide | Cathode | Benchmark Minerals (limited); IEA historical |
| Cobalt sulfate | Cathode (NMC) | Benchmark Minerals; LME spot |
| Nickel sulfate | Cathode (NMC/NCA) | LME; Metals-API |
| Manganese sulfate | Cathode (LFP/NMC) | SMM; Trading Economics |
| Graphite (natural/synthetic) | Anode | No clean free API |
| Copper foil | Current collector | LME copper + processing premium |
| Aluminum foil | Current collector | LME aluminum |
| Electrolyte salts (LiPF6) | Electrolyte | No free API; PPI proxies |
| Separator (PE/PP film) | Ionic separation | FRED PCU325211 (plastics PPI) |

---

## Automotive Supply Chain (Reference)

| Component | Material | Best Free Data Source |
|---|---|---|
| Body panels | Steel (HRC/CRC) | Metals-API; BLS PPI WPU101 |
| Engine block | Aluminum, cast iron | LME aluminum; Metals-API |
| Wiring harness | Copper | LME; FRED; Metals-API |
| Tires | Natural rubber, carbon black | World Bank Pink Sheet; FAO |
| Glass | Silica sand, soda ash | BLS PPI |
| Interior plastics | PP, ABS | FRED PCU325211 |
| Catalytic converter | Platinum, palladium, rhodium | FRED (PPLATUSDUSDM, PPALMUSDUSDM) |
| EV battery pack | See EV BOM above | See above |
| Vehicle prices | Manheim MMR (wholesale), CPI used cars series | Manheim API; FRED CUSR0000SETA02 |

---

## Priority Build Order for Pipeline

| # | Pipeline | Source | New Tables | Est. Rows/Run |
|---|---|---|---|---|
| 1 | `usda_pipeline.py` | USDA NASS QuickStats + Fertilizer | `usda_crops`, `usda_fertilizers` | ~50K |
| 2 | `trade_pipeline.py` | Census HTS + UN Comtrade | `us_imports_hs`, `us_exports_hs`, `comtrade` | ~100K+ |
| 3 | `imf_commodities_pipeline.py` | IMF PCPS + CTOT | `imf_commodities` | ~20K |
| 4 | `metals_pipeline.py` | Metals.Dev + api.metals.live | `metals_spot` | ~5K |
| 5 | `bls_ppi_extended_pipeline.py` | BLS FRED (new series) | Extends existing `bls_ppi` | ~10K |
| 6 | `fao_pipeline.py` | FAO FAOSTAT | `fao_production`, `fao_trade_prices` | ~200K |
| 7 | `noaa_climate_pipeline.py` | NOAA CDO | `noaa_climate` | ~50K |
| 8 | `worldbank_pink_sheet.py` | World Bank Pink Sheet Excel | `wb_commodities` | ~5K |
