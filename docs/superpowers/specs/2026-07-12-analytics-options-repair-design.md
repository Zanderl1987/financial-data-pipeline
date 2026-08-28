# Design: repair `analytics/options.py`

**Date:** 2026-07-12
**Status:** Approved (scope + design approved by Zander in session)
**Scope choice:** "Make it honest + working" — repair both functions against data that
actually exists. No new pipelines, no new tables, no promotion of the tmp contract CSVs.

## Problem

Both functions in `analytics/options.py` were written against camelCase yfinance-API
column names (`expirationDate`, `optionType`, `impliedVolatility`, `openInterest`,
`strike`) that `options_history` never had in any commit. The table's real columns are:

```
contract_symbol | symbol | contract_type | strike_price | expiration_date | date
| open | high | low | close | volume | fetched_at
```

with `contract_type` values `CALL` / `PUT` (uppercase). The table has **no implied
volatility and no open interest at all** — those exist only in the (currently empty)
Schwab chain tables. Result: `iv_summary` raises `KeyError: 'expirationDate'` and
`put_call_ratio` raises `KeyError` on `optionType`/`openInterest`. Found 2026-07-12
after the `underlying`→`symbol` rename fixed the `q.load` call but exposed this deeper
drift (work-notes/financial-data-pipeline/SESSION_NOTES_2026-07-12.md).

## Constraints

- Keep exported names and signatures: `iv_summary(symbol, date=None)`,
  `put_call_ratio(symbol=None, start=None, end=None)` —
  `tests/test_analytics.py::TestFunctionSignatures` pins these.
- Repo convention: return an empty `DataFrame` when no data, never raise
  (`TestEmptyDataBehavior`). `q.load()` already returns an empty frame when a CATALOG
  table has no files (`query.py` catches `duckdb.CatalogException`).
- Data reality (2026-07-12): `options_history` curated = 697,556 rows
  (AAPL/MSFT/NVDA/PLTR daily contract history); `schwab_options` and `options_chain`
  = NO DATA (both blocked on interactive Schwab OAuth).

## Design

### 1. `put_call_ratio(symbol=None, start=None, end=None)` — volume-based, from `options_history`

- `q.load("options_history", symbol=symbol, start=start, end=end)` (push-down filters
  work; the table has a proper `date` column).
- Group by `symbol`, `date`, `contract_type`; sum `volume`.
- Pivot `contract_type` (`CALL`/`PUT`) into columns; output columns:
  `symbol | date | call_volume | put_volume | put_call_ratio`.
- `put_call_ratio = put_volume / call_volume`, rounded 3dp, `NaN` when call volume
  is 0 (replace-0-with-NaN before dividing, as the old code did).
- Missing side (e.g. a day with only calls in the data) → fill 0, ratio follows from
  the same rule.
- **Semantics change, stated honestly:** this is a *volume* ratio, not the
  traditional open-interest ratio — `options_history` carries no OI. Docstring says so
  explicitly, keeps the <0.7 bullish / >1.0 bearish interpretation note (it applies to
  volume PCR too), and points to `options_metrics.put_call_ratio_oi` (Schwab pipeline)
  as the OI-based version once OAuth is done.

### 2. `iv_summary(symbol, date=None)` — wired to the real IV sources

- Source preference order: **`schwab_options` first** (`implied_volatility` + full
  greeks — the richer feed), fall back to **`options_chain`** (`volatility`). Use the
  first table that returns rows for the symbol; if both empty, return empty DataFrame.
- Internal normalizer (module-private helper) maps each source to a common shape:

  | canonical          | schwab_options       | options_chain   |
  |--------------------|----------------------|-----------------|
  | `contract_type`    | `put_call`           | `contract_type` |
  | `strike_price`     | `strike`             | `strike_price`  |
  | `iv`               | `implied_volatility` | `volatility`    |
  | `expiration_date`  | `expiration_date`    | `expiration_date` |
  | `date`             | derived: `fetched_at[:10]` (no date column in this table) | `date` |

- Because `schwab_options` has no `date` column, `q.load` is called with the `symbol`
  filter only; the `date` parameter filters in pandas after normalization
  (default: latest available date).
- Output: grouped by `expiration_date` × `contract_type` with
  `avg_iv | min_iv | max_iv | n_contracts`, rounded 4dp, sorted — same shape as the
  original intent, snake_case names.
- Returns empty today (both sources NO DATA) but the code path is real, unit-tested
  via synthetic frames, and starts working the day Schwab OAuth lands chain data.

### 3. Collateral edits

- Module docstring: correct the stale "Requires options_history (and optionally
  synthetic_options)" line to name the real per-function sources.
- No changes to `analytics/__init__.py` (exports unchanged), CATALOG, validate.py,
  curated.py, or any pipeline.

## Error handling

- Empty source → empty DataFrame out (both functions), no raise.
- `iv_summary` empty result should be a plain `pd.DataFrame()` (matching `q.load`'s
  empty return), consistent with the other analytics modules — callers already
  guard with `.empty`.

## Testing

Upgrade `tests/test_analytics.py` options coverage from signature-only to behavior
tests using small synthetic frames with `q.load` monkeypatched:

1. `put_call_ratio` math: known volumes → exact ratio; zero-call-volume day → NaN;
   CALL/PUT pivot produces `call_volume`/`put_volume` columns.
2. `iv_summary` from a synthetic `schwab_options`-shaped frame (put_call/strike/
   implied_volatility/fetched_at, no date column) → correct groups + derived date.
3. `iv_summary` from a synthetic `options_chain`-shaped frame (contract_type/
   strike_price/volatility/date) → same output shape (proves the normalizer).
4. Fallback order: schwab_options empty → options_chain used.
5. Empty-data path: both functions return empty DataFrame when all sources empty.
6. Existing signature tests unchanged (they must keep passing).

Live verification (manual, part of implementation): run `put_call_ratio("PLTR")`
against the real store and sanity-check output; run `iv_summary("AAPL")` and confirm
clean empty return. Full suite must stay green (273 passing baseline).
