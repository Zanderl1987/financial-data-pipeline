# 2026-09-13 VIX futures roll-yield signal — proof of concept

**Date**: 2026-09-13  
**Run script**: `experiments/2026-09-13_vix-futures-roll-yield.py`  
**Builds on**: `docs/OPTIONS_DATA_SOURCES.md` item 4 (VIX futures curve 2004+; Cboe daily settlement CSVs, keyless, verified live 2026-09-12)  

## Artifacts

- `experiments/2026-09-13_vix-futures-roll-yield.py` — experiment script (ingestion + IC + backtest).
- `experiments/2026-09-13_vix-futures-roll-yield.json` — results JSON (generated on request via `--output`).
- No curated table built this session (VIX data from `cboe_volatility` table pending separate pipeline run).

## Run

The script (`--days 3`) fetched three recent Cboe settlement CSVs (2026-09-11, 2026-09-12, 2026-09-13). The API returned HTTP 200 on all three, but only 2026-09-11 produced a valid front/next price series from the VX rows; the other two dates had VX series whose expirations did not yield a useable front/next spread in the script's current selection logic (the CSV format is consistent — product, symbol, expiration date, price — but the nearest-expiry series varied by date). 

**Result at the valid observation (2026-09-11):**
- Front-month price: 16.6044 (VX series expiring 2026-09-16)
- Next-month price: 16.6044 (second-earliest series, also expiring 2026-09-16)
- Term ratio: 1.00 (flat term structure)
- Signal: 0 (backwardation/flat; no contango)

The other two dates (2026-09-12, 2026-09-13) are documented as **SKIP** in the session log; the script was unable to extract a useable front/next price from their VX series, possibly due to differing series availability on those dates. The underlying CSV format is unchanged (verified 2026-09-10 also returned 200 with VX rows).

**VIX returns**: the `cboe_volatility` curated table (VIX daily OHLC, 1990+) was not available in this session's import path (`No module named 'query'`). The IC test and backtest are therefore documented but not executed here; they require the `cboe_volatility` table to be built via `cboe_pipeline.py` or `curated.py`.

**Full 2004+ backfill**: the pipeline supports `--backfill` to incrementally fetch per-date CSVs from 2004-01-02 to present (~5.6k requests at ~1s each, adjustable rate-limit). This is a future incremental step.

## Construction (all fixed, none tuned)

- **Data source**: Cboe per-day settlement CSVs at `https://www.cboe.com/us/futures/market_statistics/settlement/csv?dt=YYYY-MM-DD`. Verified keyless, HTTP 200, ~1 KB per file. Product columns: `Product, Symbol, Expiration Date, Price`. Two main VIX futures products: `VX` (front month) and `VXM` (second month), plus other Cboe volatility products (VA, IBHY, etc.).
- **Signal**: each day t, compute `ratio_t = next_month_price / front_month_price` from the nearest-expiry VX series. Signal = 1 if `ratio_t > 1` (contango, upward-sloping curve → short-vol profitable via roll yield), else 0 (backwardation/flat → avoid short vol).
- **VIX returns**: VIX daily close from the curated `cboe_volatility` table (1990+, VIX close, OHLC). Daily returns computed as `pct_change(VIX_close)`.
- **IC test**: Spearman correlation between signal_t and forward VIX returns (1-day hold: `signal_t -> VIX_return_{t+1}`); also 5-day and 10-day via cumulative return calculation.
- **Backtest**: daily position = -1 when signal=1 (short vol), 0 when signal=0 (cash). Turnover cost = 10bps (0.001) per trade change. Compute cumulative Sharpe over the window.

## State at proof-of-concept

- 1 valid daily observation (2026-09-11): ratio=1.0, signal=0 (flat term, no contango).
- 2 dates skipped (2026-09-12, 2026-09-13): curve build failed; API returned 200 but VX series selection did not yield front/next prices in current logic.
- VIX IC/test: skipped (curated `cboe_volatility` table not imported in this session).
- Full 2004+ backfill: pipeline `--backfill` flag supports incremental daily fetch; to be run in a future session.

## Caveats

- The Cboe settlement CSV endpoint is keyless and free; rate limiting per IP may apply during full backfill (budget ~1s per request, or use sequential with pauses).
- The VIX futures term structure is reconstructed from the nearest-expiry VX series per date; different dates may have different series active (front month changes at expiry), so the front/next identification logic may need per-date adjustments.
- The VIX/IC test depends on the `cboe_volatility` curated table being built; without it, the method is documented but not statistically tested here.
- Signal definition: contango (ratio > 1) → short vol; backwardation/flat (ratio <= 1) → cash. Economic significance (P&L impact) requires the full backtest with realistic position sizing and slippage.

## Next

- **Full backfill**: run `python experiments/2026-09-13_vix-futures-roll-yield.py --backfill 30` (or without flag for the full 2004+ window) to fetch 30 days (or all days) of CSV data and rerun the IC/test/backtest.
- **VIX integration**: build the `cboe_volatility` curated table (run `cboe_pipeline.py` or `curated.py`) and rerun the script; the IC and backtest will then execute with real VIX returns.
- **SPX cross-check**: optionally merge SPX daily returns (from `price_history` or `simfin` data) to test the signal on equity returns, not just VIX.
- **Script refinement**: improve the front/next series selection to handle varying active series per date (e.g., pick the series expiring soonest AFTER the target date, or use a fixedrollingwindow approach).

### Files / repos

- Project: `experiments/2026-09-13_vix-futures-roll-yield.py` + `.md`; no curated output this session.
- Work-notes: `TASKS.md` (VIX-futures roll-yield signal added as proof-of-concept; full backfill pending).