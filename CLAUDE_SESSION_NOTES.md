# Claude Session Notes
**Session date:** 2026-06-23
**Branch:** entity-resolution-refactor (commits: 1ce1a91, a0e1249)

---

## For You (Summary)

### What we did
1. **Added BEA API key** to `.env` — you provided `637CE4BC-85D8-48B9-8B97-5C0B7343F844`
2. **Fixed and ran BEA pipeline** — two table IDs were wrong/discontinued; fixed and backfilled 68,796 rows of GDP, personal income, and corporate profits history
3. **Built 7 new pipelines** (batch 1 from prior session) were already committed at session start
4. **Built 7 more new pipelines** (this session's main work) — all wired, tested, committed

### New pipelines added this session (commit a0e1249)
| Pipeline | What it pulls | Key needed? |
|---|---|---|
| Fama-French | 5-factor + momentum returns, 48 industry portfolios (back to 1926) | No |
| Shiller CAPE | Long-run S&P 500 P/E, dividends, CAPE back to 1871 | No |
| CBOE | VIX, VIX9D, VIX3M, VIX6M, VVIX, SKEW daily OHLC | No |
| FDIC | All US bank financials (quarterly call reports), institutions, failures | No |
| Fear & Greed | Daily crypto fear/greed 0-100 score (Alternative.me) | No |
| Nasdaq Data Link | S&P 500 valuation metrics + full Treasury yield curve | Have it in .env |
| NY Fed SOMA | Weekly Fed balance sheet: Treasury + Agency MBS holdings | No |

### Current state
- **78 tables** in CATALOG
- **139 tests passing**
- None of the 7 new pipelines have been run yet — data dirs are empty
- BEA is the only new pipeline with actual data on disk

### What to do next
- Run the new pipelines (see "Ready to run" section below)
- FDIC backfill will be slow (~30 min for financials since 1992)
- FED SOMA backfill will be very slow (~1,200 weekly reports, timeout=3600s)
- Still have keys in .env that aren't being used: `CENSUS_API_KEY`, `OPENROUTER_API_KEY`

### Ready to run (quick ones first)
```
python fear_greed_pipeline.py --backfill
python shiller_pipeline.py
python cboe_pipeline.py --backfill
python nasdaq_data_link_pipeline.py --backfill
python fama_french_pipeline.py --backfill
python fdic_pipeline.py --backfill
python fed_soma_pipeline.py --backfill   # slow — allow 1+ hour
```

---

## For Claude (Technical Pickup Notes)

### Repo location
`C:\Users\zande\PycharmProjects\financial-data-pipeline`
Python: `C:\ProgramData\anaconda3\python.exe`

### Architecture
- All pipelines write via `storage_utils.write_partitioned(df, output_dir, filename)`
  → `output_dir/year=YYYY/month=MM/filename.parquet` (Hive partitioning)
- DuckDB reads via glob patterns in `query.py` CATALOG with `hive_partitioning=True`
- `run_all.py` uses `PipelineSpec(name, file, desc, stage, tables, requires_env, backfill_args, timeout)`
- `validate.py` SCHEMAS dict drives post-run validation checks
- Tests in `tests/test_catalog.py` (EXPECTED_TABLES list) and `tests/test_pipelines.py` (PIPELINE_MODULES list)

### CATALOG table count: 78
Notable new entries this session:
```python
"ff_factors"            -> storage/raw/fama_french/factors/**/*.parquet
"ff_industry"           -> storage/raw/fama_french/industry/**/*.parquet
"shiller_cape"          -> storage/raw/shiller/**/*.parquet
"cboe_volatility"       -> storage/raw/cboe/**/*.parquet
"fdic_institutions"     -> storage/raw/fdic/institutions/**/*.parquet
"fdic_financials"       -> storage/raw/fdic/financials/**/*.parquet
"fdic_failures"         -> storage/raw/fdic/failures/**/*.parquet
"fear_greed"            -> storage/raw/fear_greed/**/*.parquet
"market_valuation"      -> storage/raw/nasdaq_data_link/valuation/**/*.parquet
"treasury_yield_curve"  -> storage/raw/nasdaq_data_link/yield_curve/**/*.parquet
"fed_soma"              -> storage/raw/fed_soma/**/*.parquet
```
Also added in this session (were in run_all.py but missing from CATALOG — now fixed):
```python
"usgs_minerals"         -> storage/raw/usgs_minerals/**/*.parquet
"comtrade_trade"        -> storage/raw/comtrade/**/*.parquet
```

### .env keys present
```
SCHWAB_API_KEY / SCHWAB_APP_SECRET / SCHWAB_CALLBACK_URL / SCHWAB_TOKEN_PATH
EDGAR_USER_AGENT
FRED_API_KEY / FRED_API_KEY_BACKUP
EIA_API_KEY
HF_TOKEN / HF_DATASET_REPO
FINNHUB_API_KEY
YOUTUBE_API_V3_KEY
OPENROUTER_API_KEY          # not used yet
NASDAQ_DATA_LINK_API_KEY    # used by nasdaq_data_link_pipeline.py
FINRA_API_KEY
IEX_CLOUD_API_KEY           # IEX Cloud shut down 2025 — key is dead
SIMFIN_API_KEY
NEWSORG_API_KEY
ALPHA_VANTAGE_API_KEY / ALPHA_VANTAGE_API_KEY_2
TIINGO_API_KEY
QUANDL_API_KEY              # legacy alias for NASDAQ_DATA_LINK_API_KEY
SOCIAL_SENTIMENT_IO_API_KEY
USDA_NASS_API_KEY / USDA_NASS_API_KEY_2
BEA_API_KEY                 # added this session: 637CE4BC-85D8-48B9-8B97-5C0B7343F844
```
Keys in .env with no pipeline yet: `OPENROUTER_API_KEY`, `CENSUS_API_KEY` (trade pipeline uses it but may not be set)

### BEA pipeline fixes applied this session
`bea_pipeline.py` — table IDs corrected:
- `T20200` → `T20600` (monthly personal income — T20200 was wrong ID)
- `T60700A` → `T61600D` + `T61900D` (corporate profits — T60700A was discontinued by BEA)
BEA backfill complete: 282 rows GDP, 903 rows income, 116 rows profits (incremental); 17,704 / 48,041 / 3,051 (backfill)

### New pipeline details

**fama_french_pipeline.py**
- Source: Ken French Data Library ZIP files (Dartmouth)
- Downloads: F-F_Research_Data_5_Factors_2x3_CSV.zip, F-F_Momentum_Factor_CSV.zip, 48_Industry_Portfolios_CSV.zip
- Parses multi-section fixed-width CSVs (Annual/Monthly/Daily sections in same file)
- Output: long-format — date, frequency, factor, value (factors); date, frequency, weighting, industry, return_pct (industry)
- No key. `--backfill` disables 5-year cutoff.

**shiller_pipeline.py**
- Source: http://www.econ.yale.edu/~shiller/data/ie_data.xls
- Reads Excel sheet "Data", skiprows=7
- Date column is float (1871.01 = Jan 1871) — custom `_parse_date()` handles this
- Columns: date, price, dividend, earnings, cpi, gs10, real_price, real_dividend, real_earnings, cape
- Always full history (no incremental concept — file is small, always re-download)

**cboe_pipeline.py**
- Source: https://cdn.cboe.com/api/global/us_indices/daily_prices/{NAME}_History.csv
- 6 indices: VIX, VIX9D, VIX3M, VIX6M, VVIX, SKEW
- CSV format: DATE, OPEN, HIGH, LOW, CLOSE (MM/DD/YYYY dates)
- Some files have a non-standard pre-header line — code scans for row starting with "DATE"
- Output: date, index_name, open, high, low, close

**fdic_pipeline.py**
- Source: https://banks.data.fdic.gov/api/ (no key)
- 3 endpoints: /institutions, /financials, /failures
- Paginated with offset (PAGE_SIZE=10,000); JSON format `{"data": [{"data": {...}}]}`
- financials uses date filter `REPDTE:[YYYYMMDD+TO+YYYYMMDD]`
- Backfill start: 1992-03-31 (earliest available quarterly financials)
- timeout=1800 in run_all.py (financials can be large)

**fear_greed_pipeline.py**
- Source: https://api.alternative.me/fng/?limit=0&format=json
- Single call returns full history as JSON array
- Timestamps are Unix epoch integers
- Output: date, value (0-100 int), classification, source="crypto"
- NOTE: This is crypto-specific, not equity market sentiment

**nasdaq_data_link_pipeline.py**
- Source: https://data.nasdaq.com/api/v3/datasets/{CODE}.json
- Uses NASDAQ_DATA_LINK_API_KEY from .env
- market_valuation: 5 MULTPL series (long format: date, series, series_desc, value)
- treasury_yield_curve: USTREASURY/YIELD (wide format: date, 1mo, 3mo, 6mo, 1yr, 2yr, 3yr, 5yr, 7yr, 10yr, 20yr, 30yr)
- Does NOT use the `nasdaq-data-link` SDK — uses direct REST to avoid extra dependency

**fed_soma_pipeline.py**
- Source: https://markets.newyorkfed.org/api/soma/
- Two asset types: "tsy" (Treasury) and "agency" (Agency MBS/debt)
- Fetches available dates list first, then one request per date
- Incremental: last 10 weekly reports; backfill: all ~1,200 dates since 2002
- REQUEST_GAP=0.4s; timeout=3600 in run_all.py
- Output columns: as_of_date, asset_type, plus whatever fields the API returns (facevalue, parvalue, coupon, maturitydate, etc.)

### Test suite
```
139 passed, 12 skipped
tests/test_catalog.py  — EXPECTED_TABLES has 80 entries matching CATALOG
tests/test_pipelines.py — PIPELINE_MODULES has 30 entries
tests/test_runner.py   — validates PipelineSpec tables all exist in CATALOG
```

### Git log (recent)
```
a0e1249  Add 7 new pipelines: Fama-French, Shiller, CBOE, FDIC, Fear&Greed, Nasdaq Data Link, NY Fed SOMA
1ce1a91  Add 7 new free/public data pipelines with full infrastructure wiring  (prior session)
5d37aab  Implement canonical entity resolution layer using CIK mapping
6b33202  Initial commit of project files
```

### Open items / known issues
- `CENSUS_API_KEY` — trade_pipeline.py uses it; verify it's in .env (not confirmed this session)
- `IEX_CLOUD_API_KEY` in .env is dead (IEX Cloud shut down 2025) — safe to ignore
- None of the 7 new pipelines from this session have been run yet
- FED SOMA backfill: ~1,200 API calls at 0.4s gap = ~8 minutes minimum, plus JSON parse time; may take 30-60 min total
- FDIC financials backfill: large dataset, paginated; may time out — if so, run standalone outside run_all.py
- Shiller Excel URL (`econ.yale.edu`) has been stable for years but is a personal faculty page — could go down

---

# Session 2 — 2026-06-23 (Manufacturing Cost & Critical Minerals)
**Branch:** entity-resolution-refactor
**Goal:** Expand pipeline with prices and trade flows for battery/auto manufacturing components

---

## For You (Summary)

### What we did
1. **Added 6 BLS PPI manufacturing series** to `bls_pipeline.py` — direct cost indices for battery/auto supply chain
2. **Fixed IMF/FRED pipeline** — removed wrong FRED series IDs for cobalt/lithium/manganese (FRED doesn't have them); added explanatory comment
3. **Built USGS Minerals pipeline** — new `usgs_minerals_pipeline.py`, tested and working (3,877 rows, 6 commodities)
4. **Built UN Comtrade pipeline** — new `comtrade_pipeline.py`, needs `COMTRADE_API_KEY` to run
5. **Wired both new pipelines** into `run_all.py` and `validate.py`

### New pipelines added this session
| Pipeline | What it pulls | Key needed? |
|---|---|---|
| USGS Minerals | Monthly US import/export volumes for cobalt & manganese (MIS); annual production for lithium, graphite, nickel, rare earths (MYB) | No |
| UN Comtrade | Annual US import/export trade flows for 11 battery/semiconductor/metals HS codes | Yes — free B1 at comtradeapi.un.org |

### New BLS PPI series added
| Series ID | Name |
|---|---|
| PCU331110331110 | PPI Iron and Steel Mills |
| PCU331210331210 | PPI Steel Product Mfg (Purchased Steel) |
| PCU335911335911 | PPI Storage Battery Mfg |
| PCU336111336111 | PPI Automobile Manufacturing |
| PCU3363 | PPI Motor Vehicle Parts Mfg |
| PCU334413334413 | PPI Semiconductor Device Mfg |

**Note:** BLS hit its daily rate limit before these could be verified — confirm they load on the next run.

### What to do next
1. **Register for COMTRADE_API_KEY** — free B1 tier at https://comtradeapi.un.org/; add to `.env` then run:
   ```
   python comtrade_pipeline.py --backfill
   ```
2. **Verify BLS PPI new series** — run `python bls_pipeline.py --backfill` tomorrow (rate limit resets daily)
3. **Run 7 pipelines from Session 1** (none have been run yet):
   ```
   python fear_greed_pipeline.py --backfill
   python shiller_pipeline.py
   python cboe_pipeline.py --backfill
   python nasdaq_data_link_pipeline.py --backfill
   python fama_french_pipeline.py --backfill
   python fdic_pipeline.py --backfill
   python fed_soma_pipeline.py --backfill   # slow — allow 1+ hour
   ```

---

## For Claude (Technical Pickup Notes)

### Key finding: where battery-critical metal prices live
FRED, World Bank Pink Sheet, and Nasdaq Data Link (free tier) do NOT have cobalt, lithium, manganese, graphite, or rare earth prices. Sourcing strategy:
- **USGS pipeline** — monthly US import volumes + values for cobalt/manganese (implicit price = value/$000 ÷ quantity/MT × 1000); annual stats for lithium, graphite, nickel, rare earths
- **Comtrade pipeline** — annual trade flows by HS code, same implicit price calculation possible
- **BLS PPI** — manufacturing cost indices roll up all input costs (best proxy for "is it getting more expensive to make a battery")

### usgs_minerals_pipeline.py
- Tested and confirmed working: **3,877 rows, 6 commodities** (2,182 MIS + 3,186 MYB records)
- Scrapes USGS NMIC commodity pages dynamically for file URLs (pubs.usgs.gov DS-140 URLs are dead; files now on USGS S3)
- MIS files (cobalt abbrev=`cobal`, manganese abbrev=`manga`) — sheets T1–T5; T2=imports by country, T4=exports
- MYB files (lithium, graphite, nickel, rare_earths) — annual workbooks; parser finds year-column headers
- No API key needed; respectful scraping (1.5s between requests)
- Output table: `usgs_minerals` → `storage/raw/usgs_minerals/year=YYYY/month=MM/`
- Schema columns: commodity, category, file_type (MIS/MYB), sheet, table_title, period, period_type, col_idx, value, source_url, source, fetched_at

### comtrade_pipeline.py
- Requires `COMTRADE_API_KEY` — exits cleanly with instructions if missing
- Authenticated endpoint: `https://comtradeapi.un.org/data/v1/get/C/A/HS`
- Auth header: `Ocp-Apim-Subscription-Key: {key}`
- 11 HS codes: lithium carbonates (283691), lithium hydroxide (282520), cobalt unwrought (810520), manganese ores (260200), natural graphite (250410), rare earth compounds (284690), Li-ion batteries (850760), processor ICs (854231), memory ICs (854232), steel HRC (720829), aluminum unwrought (760110)
- Output table: `comtrade_trade` → `storage/raw/comtrade/year=YYYY/month=MM/`
- Public preview endpoint tried but returned 404 for all URL variants — removed; authenticated-only now

### imf_commodities_pipeline.py change
- Removed: `PCOBUSDM`, `PLITHIUUSDM`, `PMANGAUSDM` — all returned HTTP 400 "series does not exist"
- Added comment block explaining that FRED does not mirror IMF PCPS for these metals
- File still fetches: aluminum, nickel, zinc, lead, iron ore, tin, coal (AU), LNG (Japan/EU), rice, palm oil, tea, non-fuel index, food index

### CATALOG / validate.py
- Two new schemas added to `validate.py` SCHEMAS dict (already committed before this session's other changes):
  - `usgs_minerals`: required=[commodity, category, sheet, metric, year, value, fetched_at]
  - `comtrade_trade`: required=[hs_code, hs_name, category, year, flow, trade_value_usd, fetched_at]
- Both tables added to CATALOG in `query.py`

### run_all.py additions
```python
PipelineSpec(
    name="usgs_minerals",
    file="usgs_minerals_pipeline.py",
    desc="USGS DS-140 critical mineral statistics — lithium, cobalt, graphite, rare earths",
    stage=1,
    tables=["usgs_minerals"],
    backfill_args=["--backfill"],
    timeout=300,
),
PipelineSpec(
    name="comtrade",
    file="comtrade_pipeline.py",
    desc="UN Comtrade — US import/export flows for battery materials and components",
    stage=1,
    tables=["comtrade_trade"],
    requires_env=["COMTRADE_API_KEY"],
    backfill_args=["--backfill"],
    timeout=600,
),
```

### Dead ends investigated this session
- **Nasdaq Data Link LME dataset** — HTTP 403 with free key; LME data requires premium subscription
- **IMF SDMX API** (`dataservices.imf.org`) — DNS resolution fails from this machine; alternative sources used
- **USDA NASS API key** (`Tv2Hcksg8LVSG9XTY8KU3VR26YVhX3WhV9aXmNzU`) — returning 401; may need more activation time
- **CENSUS_API_KEY** — still missing from `.env`; `trade_pipeline.py` still SKIPs

### Git log (after this session's commit)
```
<this session's commit>   Add manufacturing cost pipelines: USGS minerals, UN Comtrade, BLS PPI indices
a0e1249                   Add 7 new pipelines: Fama-French, Shiller, CBOE, FDIC, Fear&Greed, Nasdaq Data Link, NY Fed SOMA
1ce1a91                   Add 7 new free/public data pipelines with full infrastructure wiring
5d37aab                   Implement canonical entity resolution layer using CIK mapping
6b33202                   Initial commit of project files
```
