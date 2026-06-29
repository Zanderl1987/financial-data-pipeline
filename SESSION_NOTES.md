# Session Notes — 2026-06-29

## What We Built This Session

### 7 new alternative data pipelines (all Stage 1, free/keyless unless noted)

| Pipeline | File | Tables | Notes |
|---|---|---|---|
| Open-Meteo weather | `open_meteo_pipeline.py` | `open_meteo_weather` | 25 US locations, 11 daily vars, 1990+ |
| Wikipedia pageviews | `wikipedia_pipeline.py` | `wikipedia_pageviews` | 46 articles (DJI + macro), 2015+ |
| OpenFDA | `openfda_pipeline.py` | `openfda_approvals`, `openfda_recalls` | Drug approvals + enforcement recalls, 2010+ |
| Treasury TIC | `treasury_tic_pipeline.py` | `treasury_tic_holders`, `treasury_tic_slt` | Foreign holdings of US Treasuries by country |
| Google Trends | `google_trends_pipeline.py` | `google_trends_economic/market/sector` | 45 keywords, 3 groups, 5yr weekly backfill |
| Reddit sentiment | `reddit_pipeline.py` | `reddit_posts`, `reddit_mentions` | **Needs API keys** (see below) |
| AIS vessel tracking | `ais_pipeline.py` | `ais_positions`, `ais_zone_summary` | **Needs API key** (see below) |

All tables registered in `query.py` CATALOG and `validate.py` SCHEMAS.

---

## Backfill Status

### New pipelines
| Pipeline | Status | Rows |
|---|---|---|
| wikipedia | COMPLETE | 180,582 |
| openfda | COMPLETE | 18,104 approvals + 5,000 recalls |
| treasury_tic | COMPLETE | 12,355 holders + 8,664 SLT |
| google_trends | COMPLETE | 11,790 (3 groups x 3,930) |
| open_meteo | **IN PROGRESS** — batched rewrite running (background task `bo2t02z5d`) |
| reddit | SKIP — needs API keys |
| ais | SKIP — needs API key |

### Original pipelines (Run 2, completed)
26 PASS / 9 FAIL / 5 SKIP over 67 minutes. Failures:

| Pipeline | Failure | Investigated? |
|---|---|---|
| `futures` | exit 1 | No |
| `short_interest` | exit 1 | No |
| `coingecko` | timed out 600s | No — probably needs longer timeout |
| `prices`, `sector_etfs`, `schwab_*`, `options_chain` | exit 1 | Expected — no Schwab credentials |
| `synthetic_options` | timed out 1200s | Expected — depends on prices table |

---

## Open Issues / Next Steps

### 1. Confirm open_meteo batched run (immediate)
Background task `bo2t02z5d` is running the rewritten pipeline.
Old design: 25 API calls (blew quota). New design: 5 batched calls of 5 locations each, 45s pause between batches.
Once complete: verify 25 locations wrote, then `git pull` on D drive.

Check output:
```
C:\Users\Zander\AppData\Local\Temp\claude\C--Users-Zander\20eee7bc-4ce2-4758-affe-6bcd1da40cab\tasks\bo2t02z5d.output
```

### 2. Get Reddit + AIS credentials
**Reddit** — register a free "script" app:
1. Go to https://www.reddit.com/prefs/apps → "create another app" → type: script
2. Redirect URI: `http://localhost:8080`
3. Add to `.env`:
   ```
   REDDIT_CLIENT_ID=...
   REDDIT_CLIENT_SECRET=...
   REDDIT_USER_AGENT=financial-data-pipeline/1.0 (by u/your_username)
   ```
4. Test: `python reddit_pipeline.py --backfill`

**AIS** — free real-time vessel tracking:
1. Register at https://aisstream.io/ (no credit card)
2. Add to `.env`:
   ```
   AISSTREAM_API_KEY=...
   ```
3. Test: `python ais_pipeline.py --minutes 10`

### 3. Investigate futures + short_interest failures
Both exit 1 with no timeout. Not looked at this session. Run individually to see the error:
```
python futures_pipeline.py --backfill
python short_interest_pipeline.py --source all
```

### 4. Fix coingecko timeout
`coingecko_pipeline.py` timed out at 600s during backfill. Either:
- Increase its timeout in `run_all.py` (currently 600)
- Or run directly: `python coingecko_pipeline.py --backfill`

---

## Bugs Fixed This Session

| Bug | Root Cause | Fix |
|---|---|---|
| `open_meteo` blowing quota | 25 individual API calls | Batched 5 locations per call; 5 calls total |
| `open_meteo` 300s timeout in `run_all.py` | Underestimated rate-limit wait time | Raised to 1800s |
| OpenFDA `parse_exception` HTTP 500 | `+TO+` in query string double-encoded by requests | Use plain spaces; requests encodes them correctly |
| Treasury TIC wrong URL | Used `mfhhis.txt` → 404 | Correct URL: `mfhhis01.txt` |
| Treasury TIC SLT wrong approach | SHL survey URLs all 404 | Switched to `slt_table1.txt` (long-form monthly) |
| Treasury TIC parser failure | Used regex sep on tab-delimited file | Rewrote parser to split on `\t` |
| pytrends urllib3 error | `method_whitelist` renamed in urllib3 >= 2.0 | Removed `retries`/`backoff_factor` from `TrendReq()` |
| `UnicodeEncodeError` on Windows | `→` (U+2192) in print statements; terminal is cp1252 | Replaced with `->` ASCII |
| New tables `not in CATALOG` | Tables missing from `query.py` and `validate.py` | Added all 14 new tables to both |

---

## Repo State

- **GitHub**: up to date (`master`, latest commit: open_meteo batching rewrite)
- **C: drive** (`C:\Users\Zander\financial-data-pipeline`): working copy, up to date
- **D: drive** (`D:\Claude Main\Projects\financial-data-pipeline`): synced this session; needs one more pull after open_meteo run completes

D drive has some extra untracked local files (not in `.gitignore`):
```
alternative_data_sources.md   backfill_symbols.py    data_sources.csv
data_sources.md               patch_banks.py         validate_synthetic_options.py
Schwab API Exploration.ipynb  SchwabDev1.py          (etc.)
```
These are safe to ignore — they pre-date the current pipeline structure.
