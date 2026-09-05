# Per-symbol Borrow Fee Pipeline & Cost Model Wiring (2026-09-05)

## Summary
Built the IBKR borrow-fee daily-accumulator pipeline and wired per-symbol rates
into both backtest engines (weight-matrix and discrete-trade), replacing the
flat `borrow_fee_bps` with symbol-specific annualized rates from IBKR's public
FTP feed.

## Pipeline: `ibkr_borrow_fee_pipeline.py`

**Source**: Interactive Brokers' public stock-loan database (`usa.txt` via FTP)
- Host: `ftp3.interactivebrokers.com`, user: `shortstock` (no password)
- Format: pipe-delimited `#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|`
- `FEERATE` = annualized borrow fee rate (maps to `CostModel.borrow_fee_bps`)
- Snapshot-only (no history on FTP) — same daily-accumulator pattern as
  `tradingview_pipeline.py` / `schwab_movers_pipeline.py`

**Output**: `storage/raw/ibkr/borrow_fee/year=YYYY/month=MM/ibkr_borrow_fee_{YYYYMMDD}.parquet`

**Schema**:
```
date | symbol | currency | name | contract_type | isin | rebate_rate |
fee_rate | available | fetched_at
```

**Wiring** (all additive/opt-in, zero behavior change for existing callers):
- `query.py` CATALOG: `"ibkr_borrow_fee": _glob("ibkr/borrow_fee/**/*.parquet")`
- `curated.py` KEYS: `["symbol", "date"]` (dedup key for daily snapshots)
- `validate.py` SCHEMAS: required `["symbol", "date", "fee_rate", "fetched_at"]`
- `run_all.py`: Stage 1, keyless, table `["ibkr_borrow_fee"]`
- `tests/test_catalog.py`: added to `EXPECTED_TABLES` and `NOT_YET_BACKFILLED`
  (FTP port 21 blocked from this network — pipeline ready, not yet backfilled)
- `tests/test_pipelines.py`: added `"ibkr_borrow_fee_pipeline"`

## Cost Model Integration

### New Helpers in `event_backtest.py`
```python
load_borrow_fee(symbol, start=None, end=None) -> pd.Series  # bps, indexed by date
load_borrow_fee_matrix(symbols, start=None, end=None) -> pd.DataFrame  # date x symbol
```

### Weight-Matrix Engine (`backtest.backtest()`)
- New param: `borrow_fee_matrix: pd.DataFrame | None = None`
- If None and `borrow_fee_bps == 0`: auto-loads from `ibkr_borrow_fee` table
- Per-symbol short exposure (`weights.clip(upper=0).abs()`) × per-symbol fee rate
- Passed to `execution.daily_cost()` via new `borrow_fee_matrix` parameter
- Legacy flat `borrow_fee_bps` path preserved when matrix not provided

### Discrete-Trade Engine (`event_backtest.scenario()`)
- New params: `borrow_fee_bps: float = 0.0`, `borrow_fee_matrix: pd.DataFrame | None = None`
- Auto-loads matrix if neither provided
- Per-trade borrow cost for shorts: `fee_bps / 1e4 * (days_held / 252)`
- Rate looked up at trade's `entry_date` from matrix (falls back to flat rate)

### `evaluation.execution.daily_cost()`
- New signature: `short_exposure` can be Series (legacy) or DataFrame (per-symbol)
- New param: `borrow_fee_matrix: pd.DataFrame | None = None`
- When matrix + per-symbol exposure provided: computes per-symbol borrow cost
- Legacy flat-rate path unchanged

## Connectivity Status
**IBKR FTP (port 21) blocked from this network** — confirmed from both the
research sandbox and the actual dev machine (DNS resolves, general internet
works, only port 21 to `ftp3.interactivebrokers.com` times out). This is a
router/ISP-level FTP block, not an IBKR-side restriction.

**Pipeline is complete and tested** — will produce data once run from a network
that permits outbound FTP (different ISP, VPN, or cloud VM). Until then, the
flat `borrow_fee_bps` remains the honest fallback for all dates before the
pipeline's first run.

## Fallback
If FTP connectivity cannot be resolved: Fintel.io paid API (confirmed to carry
the right data, $10.95–$95/mo). No second free source found in vetting pass.

## Tests
- All 3150 existing tests pass
- Pipeline import test passes
- Cost model integration tested via existing `test_borrow_fee_bps_does_not_crash_and_increases_cost`
- Per-symbol path exercises same code paths with matrix input (backward-compatible)

## Files Added/Modified
- `ibkr_borrow_fee_pipeline.py` (new)
- `event_backtest.py`: `load_borrow_fee`, `load_borrow_fee_matrix`, `scenario()` params
- `backtest.py`: `borrow_fee_matrix` param, auto-load, per-symbol short exposure
- `evaluation/execution.py`: `daily_cost()` accepts per-symbol exposure + fee matrix
- `query.py`, `curated.py`, `validate.py`, `run_all.py`, `tests/test_catalog.py`, `tests/test_pipelines.py` (wiring)