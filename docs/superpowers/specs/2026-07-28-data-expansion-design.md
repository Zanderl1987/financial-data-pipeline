# Data Pipeline Expansion — Finnhub Endpoints, FRED Labor Series, Indeed Hiring Lab

**Date:** 2026-07-28
**Status:** Draft for review

## Background

The pipeline already covers ~70 sources / 55+ CATALOG tables. `docs/new_data_sources_research.md`,
`docs/alternative_data_sources.md`, and `Downloads/New Financial Data Pipeline Additions.ods`
contain prior research identifying gaps that were never implemented. This spec turns three of
those gaps into buildable work, plus one source found via fresh web research (Indeed Hiring Lab).

A fourth candidate — the Opportunity Insights Economic Tracker — was investigated and **rejected**:
pulling the raw CSVs showed `Affinity - National - Daily.csv` (consumer spending) stops in June
2024 and `Employment - National - Weekly.csv` stops in May 2025. Those series were COVID-era
trackers that have since been discontinued. Its `Job Postings` series is still live but is
downstream of Indeed's own data, so it would duplicate workstream 3 below.

## Workstream 1 — Finnhub Endpoint Expansion

Extends `finnhub_pipeline.py`, which currently calls 7 endpoints (`stock/profile2`, `quote`,
`stock/metric`, `stock/recommendation`, `stock/price-target`, `stock/upgrade-downgrade`,
`company-news`) via the shared `get_with_backoff()` helper.

**New fetch functions, one per endpoint, added to the same file:**

| Function | Endpoint | Fields |
|---|---|---|
| `fetch_eps_estimate` | `stock/eps-estimate` | analyst EPS consensus, period, # analysts |
| `fetch_revenue_estimate` | `stock/revenue-estimate` | analyst revenue consensus |
| `fetch_ebitda_estimate` | `stock/ebitda-estimate` | analyst EBITDA consensus |
| `fetch_peers` | `stock/peers` | peer symbol list per symbol |
| `fetch_executive` | `stock/executive` | name, title, comp, age |
| `fetch_ownership` | `stock/ownership` | institutional holder %, shares, change |
| `fetch_fund_ownership` | `stock/fund-ownership` | fund-level holder %, shares, change |
| `fetch_revenue_breakdown` | `stock/revenue-breakdown` | segment/geo revenue split |
| `fetch_filings_sentiment` | `stock/filings-sentiment` | NLP sentiment score on 10-K/10-Q |
| `fetch_similarity_index` | `stock/similarity-index` | filing-over-filing material change score |
| `fetch_etf_holdings` / `_sector` / `_country` | `etf/holdings`, `etf/sector`, `etf/country` | ETF composition (separate symbol loop — DJI + sector ETF list already tracked) |

- **Symbol universe:** `dji_utils.get_dji_symbols()` (DJI-30) + the existing `ETF_UNIVERSE` dict
  (11 SPDR sector ETFs + 4 broad indexes) from `sector_etf_pipeline.py`, matching the convention
  already used by `dividend_pipeline.py` and `schwab_quotes_pipeline.py`.
- **Output:** one parquet per dataset under `storage/raw/finnhub/<dataset>/`, written via
  `write_partitioned` (Hive `year=/month=` layout), same as every other Finnhub table.
- **CATALOG (`catalog.py`):** 10 new keys — `finnhub_eps_estimate`, `finnhub_revenue_estimate`,
  `finnhub_ebitda_estimate`, `finnhub_peers`, `finnhub_executives`, `finnhub_ownership`,
  `finnhub_fund_ownership`, `finnhub_revenue_breakdown`, `finnhub_filings_sentiment`,
  `finnhub_etf_holdings` (+ `_sector`, `_country`).
- **`validate.py`:** one schema entry per new table (`required`, `critical_nn`, `date_col`),
  following the existing `finnhub_news` pattern.
- **`run_all.py`:** no new pipeline entry needed — these are new functions inside the existing
  `finnhub` pipeline registration, so Stage 1 wiring is unchanged; just expands what that one
  pipeline run writes.
- **Rate limits:** Finnhub free tier is 60 calls/min. 11 new endpoints × ~40 symbols (30 DJI + ~11
  sector ETFs, some endpoints DJI-only) is a meaningful jump in call volume. `get_with_backoff`
  already handles 429s; no new throttling logic needed, but full incremental runs will take longer.
- **Skipped:** `stock/financials` / `financials-reported` (redundant with existing SEC EDGAR
  `fundamentals_pipeline.py` + `simfin_pipeline.py`) and `stock/transcripts` (redundant with the
  separate `earnings_sentiment_tool` project).

## Workstream 2 — FRED Labor-Market Gap-Fill

Extends the `SERIES` dict in `commodity_macro_pipeline.py`. No new code path — the existing FRED
fetch loop already handles any series ID in the dict.

**New entries, tagged `"labor"`:**

| Series ID | Description | Frequency |
|---|---|---|
| `PAYEMS` | Nonfarm Payrolls | Monthly |
| `ICSA` | Initial Jobless Claims | Weekly |
| `CCSA` | Continued Jobless Claims | Weekly |
| `CIVPART` | Labor Force Participation Rate | Monthly |
| `CPILFESL` | Core CPI (ex food/energy) | Monthly |
| `M1SL` | M1 Money Supply | Monthly |
| `WALCL` | Fed Balance Sheet (Total Assets) | Weekly |

No CATALOG change needed — these land in the existing `macro` table (same glob).
`validate.py`'s existing `macro` schema entry already covers arbitrary series rows.

## Workstream 3 — Indeed Hiring Lab (new `indeed_hiringlab_pipeline.py`)

New pipeline file, modeled on `worldbank_pink_sheet.py` (direct CSV download, no API key, no auth,
`requests` + `pandas.read_csv`).

**Source URLs** (verified live 2026-07-28, current through 2026-07-17):
- `https://raw.githubusercontent.com/hiring-lab/job_postings_tracker/master/US/aggregate_job_postings_US.csv`
  — national daily index, SA/NSA, split by `variable` (total postings / new postings)
- `.../US/job_postings_by_sector_US.csv` — same index broken out by sector
- `.../US/state_job_postings_us.csv` — same index broken out by state

Metro-level (`metro_job_postings_us.csv`) is skipped — too granular for this pipeline's existing
scale (state is the finest geography used elsewhere, e.g. Redfin/Zillow-style sources).

- **Output:** `storage/raw/indeed_hiringlab/{national,sector,state}/` via `write_partitioned`.
- **CATALOG:** `indeed_job_postings_national`, `indeed_job_postings_sector`,
  `indeed_job_postings_state`.
- **`validate.py`:** new schema entries — `required: ["date", "indeed_job_postings_index_sa",
  "indeed_job_postings_index_nsa", "fetched_at"]`, `date_col: "date"`.
- **`run_all.py`:** new Stage 1 entry (`name="indeed_hiringlab"`, `file="indeed_hiringlab_pipeline.py"`,
  free/public, no env var required — never auto-skips).
- **`analytics/events.py`** (or new `analytics/labor.py`): `hiring_trend(sector=None, state=None)` —
  latest index level + WoW/MoM % change, mirroring the shape of `sentiment_summary()`.
- **No `--backfill` distinction needed** — the source CSVs always contain full history since
  2020-02-01; every run just re-downloads and overwrites, same as `worldbank_pink_sheet.py`.

## Testing

- `tests/test_finnhub_pipeline.py` (or extend existing finnhub test file): one test per new fetch
  function using a mocked response, following the existing test pattern for `fetch_profile` etc.
- `tests/test_commodity_macro_pipeline.py`: extend existing SERIES-dict coverage test (if one
  exists) to assert new labor series are present.
- `tests/test_indeed_hiringlab_pipeline.py`: new file — mocked CSV response, schema assertions,
  CATALOG registration test (matching `tests/test_catalog.py` pattern).
- Full suite run at the end (`pytest`) to confirm no regressions in the 400+ existing tests.

## Out of Scope

- Opportunity Insights Economic Tracker (rejected above).
- Indeed's wage tracker and remote-work tracker repos — not verified for currency in this pass;
  can be a future addition if wanted.
- Backfill/historical depth beyond what each source already provides by default (Indeed CSVs are
  full-history; FRED/Finnhub incremental windows follow each pipeline's existing default).
