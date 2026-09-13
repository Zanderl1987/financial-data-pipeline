# Basis momentum — data-availability check: DOCUMENTED DATA-BLOCKED

**Date:** 2026-09-13
**Run script:** `experiments/2026-09-13_basis-momentum-data-check.py`
**Artifact:** `storage/reports/eval/basis_momentum_data_check_20260913.json`
**Run:** `C:\ProgramData\anaconda3\python.exe experiments/2026-09-13_basis-momentum-data-check.py` from repo root

## What was asked

TASKS.md's basis-momentum item says: *"Verify a buildable proxy exists (e.g.
multi-commodity quote tables in `futures` raw) before scoping; if no proxy, document as
data-blocked and move on."* This pass is that verification — measured against the live
store, not assumed.

## What the store actually holds (all hands-on evidence)

- **`futures` (raw + curated, 263,182 curated rows, 44 symbols): continuous
  front-month only.** Every symbol is a yfinance continuous `*=F` contract
  (`CL=F`, `GC=F`, `ES=F`, `6E=F`, …). The raw schema is `date, symbol, name,
  category, open/high/low/close, volume, fetched_at, month, year` — and the only
  `month`-named column is the Hive **fetch** partition, not contract maturity.
  There is **no expiry / contract code / maturity column anywhere**, zero
  maturity-like identifiers (checked, and every symbol ends `=F`).
- **`eia_petroleum_futures`**: two front-month products only (NYMEX Heating Oil,
  RBOB Gasoline), both series stale since 2024-04 (matching AUTOMATION.md's
  permanently-stale note). Not a curve.
- **`cot`** (273,217 rows): CFTC **positioning** — no prices, no terms.
- **`omkar_commodity`**: no data files at all.
- **`market_history`** indices/cash prices, `metals_spot` (front-month proxy), etc.:
  none carry a term structure.

## Verdict

**Basis momentum is DATA-BLOCKED on this store, confirmed by measurement.** The strategy
(roll-yield / cross-maturity basis persistence) requires near-vs-deferred price curves
per commodity; the entire store is built on front-month continuous series. There is no
free source gap to close either — the one curve-priced free feed we found and verified
live (Cboe VX/VXM daily settlement CSVs, 2004+, keyless) is the **VIX-futures term
structure**: a genuine multi-maturity curve of *vol* futures. That supports a
**VIX-futures roll-yield** signal (structurally distinct from the VTSL index-curve
overlay already published — actual near-vs-next VX roll economics, not a slope) but NOT
cross-commodity basis momentum.

## Resolution (matching the TASKS.md instruction to "move on")

- Basis-momentum stays listed as data-blocked in TASKS.md; the only true reopen is a
  same paid gate as the equity delisting panel (curve-grade futures data —
  Barchart/CME-class). Do not re-check the `futures` store for this again.
- The Cboe VIX-futures curve build (OPTIONS_DATA_SOURCES.md item 4,
  `vix_tsl_pipeline.py` → curated `vix_futures_curve`) remains on the build menu as its
  own catalog item — it is now the *only* structurally distinct short-vol signal
  candidate the free stack can support beyond the published index-level VTSL overlay.

**File road-map:** script + write-up committed with this pass; JSON artifact is
gitignored storage.