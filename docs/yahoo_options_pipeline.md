# Yahoo Options Pipeline — Reference

`yahoo_options_pipeline.py` — fetch full Yahoo Finance options chains and per-contract daily OHLCV history.

---

## What it does

**Phase 1 — Chain snapshot.** Pulls every expiration, every strike, both calls and puts for each symbol using Yahoo's v7 options API. Requires a crumb+cookie session (handled automatically). Saves a dated CSV of all contract identifiers plus current market data (bid, ask, IV, OI, last price, etc.).

**Phase 2 — Historical OHLCV.** For each contract in the list, fetches daily open/high/low/close/volume bars via Yahoo's v8 chart API. No auth needed for this endpoint. Saves a Parquet file of all bars across all contracts.

---

## Outputs

| File | Location | Description |
|------|----------|-------------|
| `options_contracts_{SYMBOL}_{YYYYMMDD}.csv` | `storage/tmp/` | Full chain snapshot — contract identifiers + chain metadata |
| `options_history_{SYMBOL}_{YYYYMMDD}.parquet` | `storage/raw/options_history/` | Daily OHLCV bars for every contract that had trade history |

### History parquet schema

| Column | Type | Notes |
|--------|------|-------|
| `contract_symbol` | str | OSI format, e.g. `PLTR260618C00047000` |
| `underlying` | str | Ticker, e.g. `PLTR` |
| `contract_type` | str | `CALL` or `PUT` |
| `strike_price` | float | |
| `expiration_date` | str | `YYYY-MM-DD` |
| `date` | str | `YYYY-MM-DD` bar date |
| `open` | float | |
| `high` | float | |
| `low` | float | |
| `close` | float | |
| `volume` | int | |
| `fetched_at` | str | ISO timestamp of fetch |

---

## Usage

> **Tip:** Run with `python -u` (unbuffered) when launching in the background so progress lines flush immediately rather than batching.

```bash
# Single symbol, default 1y lookback
python yahoo_options_pipeline.py --symbols PLTR

# Multiple symbols
python yahoo_options_pipeline.py --symbols PLTR,AAPL,MSFT

# Full available history (recommended — typically 18+ months for large caps)
python yahoo_options_pipeline.py --symbols PLTR --range max

# Filter to contracts with meaningful open interest (faster for large chains like NVDA)
python yahoo_options_pipeline.py --symbols NVDA --range max --min-oi 100

# Phase 1 only — save chain CSV, skip history fetch
python yahoo_options_pipeline.py --symbols PLTR --skip-history

# Resume a history run from an existing contract list (skip Phase 1)
python yahoo_options_pipeline.py --resume storage/tmp/options_contracts_PLTR_20260616.csv

# Resume with different range
python yahoo_options_pipeline.py --resume storage/tmp/options_contracts_PLTR_20260616.csv --range max
```

**Always run with `--range max`** to capture the full available history (~18 months for large-cap names, shorter for recently listed contracts). The default `1y` is a safe starting point for testing.

---

## Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--symbols` | required* | Comma-separated tickers, e.g. `PLTR,AAPL` |
| `--resume CSV_PATH` | required* | Skip Phase 1; resume history fetch from an existing CSV |
| `--range` | `1y` | Yahoo chart range: `1d 5d 1mo 3mo 6mo 1y 2y 5y 10y ytd max` |
| `--skip-history` | off | Phase 1 only — chain snapshot, no history |
| `--min-oi N` | `0` | Skip history fetch for contracts with open interest below N. Useful for large chains — e.g. `--min-oi 100` cuts ~30% of NVDA contracts. 0 = fetch all. |

*`--symbols` and `--resume` are mutually exclusive; one is required.

---

## Authentication

The chain endpoint (`query2.finance.yahoo.com/v7/finance/options/`) requires a crumb token. The script handles this automatically on startup:

1. Visits `finance.yahoo.com` with browser headers to obtain session cookies.
2. Exchanges cookies for a crumb token at Yahoo's crumb API.
3. Appends `?crumb=...` to all chain requests.

The history endpoint (`query1.finance.yahoo.com/v8/finance/chart/`) is public — no session needed.

---

## Rate limiting

- 250ms between every request (`REQUEST_INTERVAL = 0.25`)
- HTTP 429 triggers exponential backoff: `30s × attempt` (up to 3 retries)
- HTTP 404 on a contract = no history; skipped silently

---

## Crash recovery

If the history fetch dies partway through (network drop, 429 exhaustion, keyboard interrupt), use `--resume` to restart from the saved CSV without re-fetching the chain:

```bash
python yahoo_options_pipeline.py --resume storage/tmp/options_contracts_PLTR_20260616.csv --range max
```

The CSV filename encodes the fetch date — match it to the parquet you want to populate or overwrite.

---

## Notes

- OSI contract symbols returned directly from Yahoo's chain response as `contractSymbol`.
- Contracts with no close prices (all-None bars) are skipped in the history output.
- CSV files are gitignored (`storage/**/*.csv`). Parquet files are gitignored (`storage/**/*.parquet`). Only `.gitkeep` markers are tracked.
- The script is zero-dependency beyond `pandas` and `pyarrow` (both already present). All HTTP is stdlib `urllib`.
