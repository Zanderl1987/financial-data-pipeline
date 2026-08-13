# financial-data-pipeline

An alpha-research data platform: ~85 free/public-source pipelines feed a
partitioned Parquet store, deduplicated into 130+ curated tables queryable
through a DuckDB layer, on top of which cross-sectional factor signals and
event-study backtests run.

```
*_pipeline.py → storage/raw (Parquet, Hive-partitioned)
             → curated.py dedup → storage/curated (one file per table)
                  → query.py (DuckDB CATALOG)
                       → analytics/ (PIT feature matrix, z-scored factors, TA)
                            → backtest.py / event_backtest.py
                                 → signal_monitor.py (signal-health tracking)
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the layers fit
together and [docs/PIPELINE_CATALOG.md](docs/PIPELINE_CATALOG.md) for what
every pipeline pulls and which table it lands in.

## What's in here

- **130+ curated tables** across equities, options, fundamentals, macro,
  rates, commodities, crypto, labor market, trade, and alternative data
  (satellite/AIS shipping, Wikipedia attention, Reddit/news sentiment,
  weather, patents, congressional trades).
- **Point-in-time-correct features** (`analytics/features.py`) — joined on
  *filing/publication* date with explicit lags, not observation date. This is
  what makes the backtests meaningful; see
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#point-in-time-correctness) before
  changing anything in that module.
- **9 live cross-sectional factors** (`analytics/signals.py`): momentum,
  value, quality, low-volatility, short-pressure, insider-flow, sentiment.
- **Two backtest engines**: `backtest.py` (quantile portfolios from a
  cross-sectional score) and `event_backtest.py` (event studies — earnings
  beats, filings, drawdowns, threshold crosses, TA signals).
- **A signal-health monitor** (`signal_monitor.py`) that re-scores configured
  TA signals on a rolling basis and flags degradation.

## Setup

Requires Python 3.10+ (dataclasses, `dict[str, str]` type hints).

```
pip install -r requirements.txt
```

`requirements.txt` is generated from the actual imports in the repo — the
core stack (`pandas`, `pyarrow`, `duckdb`, `python-dotenv`, `requests`,
`numpy`, `scipy`) is needed everywhere; the rest are pipeline-specific
extras (`yfinance`, `schwabdev`, `pyiceberg`, `torch`/`transformers` for
FinBERT eval, `anthropic` for Fed-speech sentiment, etc. — see the comments
in the file for which pipeline needs which). Three packages (`pytrends`,
`praw`, `cot_reports`) are used but weren't pinned because they weren't
installed anywhere to check a version against — install and pin them before
relying on `google_trends_pipeline.py`, `reddit_pipeline.py`, or the COT
positioning data in `futures_pipeline.py`.

1. Copy `.env.example` to `.env` and fill in the free API keys for the
   sources you want (most pipelines work keyless or with a free-tier key;
   see [docs/PIPELINE_CATALOG.md](docs/PIPELINE_CATALOG.md) for which key
   each pipeline needs).
2. Run the test suite to confirm your environment works:
   ```
   python -m pytest tests/ -v
   ```
3. See what a full run would do without executing anything:
   ```
   python run_all.py --dry-run
   ```

## Running it

```
python run_all.py                 # incremental run, all 3 dependency stages
python run_all.py --backfill      # full available history where supported
python run_all.py --stage 1       # free/public sources only (skip Schwab)
python run_all.py --only commodity_macro,gas_prices,finnhub
python validate.py                # data health check (schema/null/range checks)
python curated.py                 # rebuild deduped curated snapshots
```

`run_all.py` rebuilds curated automatically after each run. If you run a
pipeline script directly instead, run `curated.py` afterward — otherwise
`query.py` and everything downstream reads stale/duplicated data.

## Querying the data

```python
import query as q

df = q.load("prices", symbol="NVDA", start="2025-01-01")
q.tables()          # every table with row counts
q.schema("prices")  # columns + types
q.date_range()      # min/max dates across all tables

from analytics import signal_panel, upcoming_earnings
signal_panel()                  # all 9 factors, z-scored, per symbol/date
upcoming_earnings(days_ahead=14)
```

Always query through `query.py` (which prefers the deduplicated curated
snapshot), never glob `storage/raw/` directly — raw files contain overlapping
re-fetches and have measured up to ~42% duplicate rows on some tables.

## Adding a new pipeline

See the wiring checklist in [CLAUDE.md](CLAUDE.md#adding-a-new-pipeline--wiring-checklist)
— every new pipeline must be wired into `query.py` CATALOG, `validate.py`
SCHEMAS, `run_all.py` PipelineSpec, `curated.py` KEYS, and both catalog/
pipeline test files, or it's invisible to the rest of the stack.

## Testing

```
python -m pytest tests/ -v
```

761 tests. Beyond unit coverage, guard tests enforce the wiring checklist above —
a pipeline that isn't registered in `query.py` CATALOG, `validate.py` SCHEMAS,
`run_all.py`, and `curated.py` KEYS fails the suite rather than silently going
missing from the query layer.

## License

MIT — see [LICENSE](LICENSE).

## Further reading

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — how the layers fit together,
  storage layout, PIT correctness, look-ahead safety in the backtesters.
- [docs/PIPELINE_CATALOG.md](docs/PIPELINE_CATALOG.md) — every pipeline,
  grouped by domain, with what it fetches and its required API key (if any).
- [CLAUDE.md](CLAUDE.md) — operating manual: environment, commands, wiring
  checklist, hard-won gotchas, known-broken sources.
- [docs/EXPERT_BRIEF.md](docs/EXPERT_BRIEF.md) — the judgment layer: prioritized
  roadmap and the reasoning behind it.
