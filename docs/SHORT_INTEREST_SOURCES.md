# Short Interest Data Sources

## Overview

Short interest measures how many shares of a stock have been sold short but not yet
covered. It is one of the strongest contrarian / squeeze-risk signals available for free.
This pipeline combines three complementary sources to build a comprehensive picture.

---

## Source 1 — Yahoo Finance Snapshot (`--source yfinance`, default)

**What it is:** Per-symbol snapshot pulled from Yahoo Finance via `yfinance.Ticker.info`.
Exchange filings (NYSE/NASDAQ report short interest twice monthly); Yahoo surfaces the
most recent filing date in each response.

**Run frequency:** Daily. Each run creates a new dated Parquet file. Running regularly
accumulates a time series.

**Fields captured:**

| Field | Column | Notes |
|-------|--------|-------|
| Shares short (current) | `shares_short` | Raw count of shares sold short |
| Shares short (prior month) | `shares_short_prior_month` | Previous filing period |
| Short % of float | `short_pct_float` | **Most-watched signal**; e.g. 0.20 = 20% |
| Days to cover | `days_to_cover` | Short interest ÷ avg daily volume |
| Float shares | `float_shares` | Publicly tradeable share count |
| Shares outstanding | `shares_outstanding` | Total incl. insider/restricted |
| Filing date | `filing_date` | As-of date of the exchange filing |
| Snapshot date | `snapshot_date` | Date this pipeline run was executed |

**Output:** `storage/raw/short_interest/short_interest_snapshot_{YYYYMMDD}.parquet`
**CATALOG key:** `short_interest`

**Key thresholds:**
- `short_pct_float > 0.20` → heavily shorted; possible squeeze or fundamental short thesis
- `short_pct_float > 0.30` → extreme; meme-stock territory (GME peaked ~140%)
- `days_to_cover > 5`      → shorts can't exit quickly; amplifies squeeze risk
- `days_to_cover > 10`     → very high; shorts trapped if price rises

---

## Source 2 — FINRA Regulation SHO Biweekly Short Interest (`--source finra`)

**What it is:** Official regulatory data published by FINRA under SEC Rule 10a-1 and
Regulation SHO. Covers all NMS (National Market System) securities across NYSE, NASDAQ,
and regional exchanges. Published ~24 times/year (around the 15th and last business day
of each month). Full market coverage — not just DJI/ETFs.

**Data format:** Pipe-delimited text files from FINRA's CDN at:
`https://cdn.finra.org/equity/regsho/biweekly/CNMSshvol{YYYYMMDD}.txt`

**Fields captured:**

| Field | Column | Notes |
|-------|--------|-------|
| Ticker | `symbol` | Exchange symbol |
| Company name | `company` | Issue name |
| Exchange | `market` | NYSE, NASDAQ, OTC, etc. |
| Shares short | `shares_short` | Aggregate reported short position |
| Days to cover | `days_to_cover` | Calculated from exchange avg volume |
| Change from prior | `change_shares` | Delta vs prior settlement period |
| Settlement date | `settlement_date` | Official as-of date |

**Output:** `storage/raw/finra_short_interest/finra_short_{YYYYMMDD}.parquet`
**CATALOG key:** `finra_short_interest`

**Advantage over yfinance:** Covers all ~6,000+ US-listed securities (not just a watch
list). Useful for screening the entire market for extreme short interest levels.

---

## Source 3 — SEC Fails-to-Deliver (`--source ftd`)

**What it is:** When a short seller (or any seller) cannot deliver shares by the
settlement date (T+2), it results in a "fail to deliver." The SEC publishes these
twice monthly. Unusually high FTD relative to float or average volume suggests
potential naked short selling or severe settlement stress.

**Historical precedent:** GME (Jan 2021) had FTD counts in the tens of millions of
shares before the squeeze, signaling settlement pressure before it became public.

**Data format:** Zipped pipe-delimited CSV from SEC at:
`https://www.sec.gov/files/data/fails-deliver-data/cnsfails{YYYY}{MM}{a|b}.zip`
(a = first half of month, b = second half)

**Fields captured:**

| Field | Column | Notes |
|-------|--------|-------|
| Settlement date | `settlement_date` | Date of the failed delivery |
| CUSIP | `cusip` | Security identifier |
| Symbol | `symbol` | Ticker |
| Shares failed | `shares_failed` | Shares that failed to settle |
| Company name | `description` | SEC description field |
| Price | `price` | Closing price on settlement date |

**Output:** `storage/raw/sec_ftd/sec_ftd_{YYYYMMDD}.parquet`
**CATALOG key:** `sec_ftd`

**Advantage:** Different angle from short interest — a stock can have high FTD even if
reported short interest looks manageable (naked shorts may not be reported correctly).

---

## Analytics Functions (`analytics/short_interest.py`)

```python
from analytics.short_interest import (
    squeeze_candidates,   # high short % + manageable DTC
    short_change,         # trend: are shorts covering or piling in?
    ftd_pressure,         # top FTD by total shares failed
    short_vs_ftd,         # joined view: short interest + FTD per symbol
)

# Find stocks with >15% of float short and DTC <= 10
squeeze_candidates(min_short_pct=0.15, max_days_to_cover=10)

# Has short interest grown or shrunk across recent snapshots?
short_change(symbols=["GME", "AMC", "NVDA"])

# Top 20 symbols by total FTD (potential naked short pressure)
ftd_pressure(top_n=20)

# Combined squeeze scorecard
short_vs_ftd(symbols=["AAPL", "NVDA", "TSLA"])
```

---

## Running the Pipeline

```bash
# Daily snapshot (yfinance — recommended for regular runs)
python short_interest_pipeline.py

# Specific symbols only
python short_interest_pipeline.py --symbols AAPL NVDA TSLA GME

# FINRA full-market biweekly data
python short_interest_pipeline.py --source finra

# SEC Fails-to-Deliver
python short_interest_pipeline.py --source ftd

# All three sources in one run
python short_interest_pipeline.py --source all
```

---

## Signal Combinations

| Condition | Interpretation |
|-----------|---------------|
| High `short_pct_float` + low `days_to_cover` | Explosive squeeze potential; shorts can't exit fast |
| Rising `shares_short` + falling price | Bears confirmed; trend continuation |
| Falling `shares_short` + rising price | Short covering rally; upside momentum |
| High FTD + high `short_pct_float` | Strongest squeeze signal; settlement stress + heavy short |
| High FTD + stable `short_pct_float` | Possible naked short / settlement issue without broad short thesis |
