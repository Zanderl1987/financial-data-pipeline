# Options Analytics — Comprehensive Build Plan

## Current state

- `analytics/options.py` has 2 functions: `put_call_ratio` (working, volume-based),
  `iv_summary` (working but returns empty — no IV data yet)
- `options_history`: 697,556 rows, 4 symbols (AAPL/MSFT/NVDA/PLTR), 2.5 years,
  columns: contract_symbol/symbol/contract_type/strike_price/expiration_date/date/
  open/high/low/close/volume/fetched_at
- `synthetic_options`: 648 rows, AAPL+MSFT, 1 day, has BSM Greeks (delta/gamma/
  theta/vega/rho) + moneyness + multiple vol methods (cc/yz/vix) and models (bsm/bs2002)
- `tiingo_prices`: 484K rows, 69 symbols including all 4 options symbols, full history
- `schwab_options`/`options_chain`/`options_metrics`: EMPTY (Schwab OAuth pending)
- No curated snapshots for schwab_options/options_chain/options_metrics

## Design principles

- All functions return `pd.DataFrame` (empty on no data, never None/exception)
- Follow existing patterns: `sys.path` + `import query as q`, `q.load()` + empty guard
- Module docstring with Usage examples; docstrings list return columns in `col | col` format
- No side effects — pure transforms on query.py data
- Functions accept `symbol: "str | None" = None` convention (None = all available)
- Group I functions: work NOW with options_history + synthetic_options + tiingo_prices
- Group II functions: work when Schwab OAuth lands (graceful empty returns today)
- Shared helpers prefixed `_` (like existing `_normalise_iv_source`)
- Tests: monkeypatched `q.load` behavior tests (pattern from existing test_analytics.py)

---

## Group I — Functions that work TODAY

### A. Volume analytics (from options_history)

#### 1. `volume_skew(symbol, start, end)`
Put vs call volume imbalance by date, at different rolling windows.

Returns:
```
symbol | date | call_volume | put_volume | pcr_1d | pcr_5d | pcr_21d
```
- `pcr_1d`: single-day put/call ratio (same as put_call_ratio output, included for convenience)
- `pcr_5d`: 5-day rolling average PCR
- `pcr_21d`: 21-day (1 month) rolling average PCR
- Readings: <0.7 bullish, 0.7-1.0 neutral, >1.0 bearish/hedging
- 5d vs 21d crossover = sentiment regime change signal

#### 2. `unusual_volume(symbol, threshold, lookback_days)`
Detect contracts with volume significantly above their own historical average.

Parameters:
- `threshold`: z-score threshold (default 2.0 = 2 standard deviations)
- `lookback_days`: rolling window for mean/std (default 30)

Returns:
```
symbol | date | contract_symbol | contract_type | strike_price |
expiration_date | volume | avg_volume | volume_zscore | pct_of_total
```
- `avg_volume`: rolling mean of this contract's daily volume
- `volume_zscore`: (today - mean) / std
- `pct_of_total`: this contract's volume as % of total daily volume across all strikes
- Flags institutional-sized flow (large volume in single strikes = possible hedging or positioning)

#### 3. `volume_by_strike(symbol, date, window_days)`
Aggregate volume by strike price for a given date or rolling window.

Returns:
```
symbol | strike_price | total_volume | call_volume | put_volume |
pct_of_total | put_call_ratio
```
- Useful for identifying strikes with concentrated activity (support/resistance signals)
- `window_days`: if provided, aggregate across N days centered on `date`

#### 4. `term_structure_volume(symbol, date)`
Volume distribution across expiration dates.

Returns:
```
symbol | expiration_date | total_volume | call_volume | put_volume |
pct_of_total | dte_bucket
```
- `dte_bucket`: labeled bins — 'week' (0-7d), 'month' (8-30d), 'quarter' (31-90d), 'leaps' (90d+)
- Shows where market expects near-term vs long-term action
- Heavy near-term = event-driven; heavy long-term = strategic positioning

#### 5. `volume_concentration(symbol, date)`
How concentrated is options volume across strikes and expirations — a proxy for conviction.

Returns (single row):
```
symbol | date | top5_strikes_pct | top10_strikes_pct | top3_expirations_pct |
total_contracts | hhi_strikes | hhi_expirations
```
- `top5_strikes_pct`: % of total volume in the top 5 strikes
- `hhi_strikes`: Herfindahl index across strikes (0-1; 1 = all volume in one strike)
- High concentration = institutional conviction; dispersed = retail noise

#### 6. `weighted_average_strike(symbol, date)`
Volume-weighted average strike — the "center of gravity" of options activity.

Returns:
```
symbol | date | vwap_strike | call_vwap_strike | put_vwap_strike |
underlying_price | distance_pct
```
- `vwap_strike`: overall volume-weighted avg strike
- `distance_pct`: (vwap_strike - underlying_price) / underlying_price * 100
- Positive distance = market skewing calls above spot (bullish); negative = puts below (bearish)

### B. Structural metrics (from options_history + tiingo_prices)

#### 7. `max_pain(symbol, date)`
Strike at which the most options expire worthless — a magnet for price at expiration.

Returns (single row per expiration):
```
symbol | expiration_date | max_pain_strike | underlying_price |
distance_pct | call_oi_at_mp | put_oi_at_mp
```
- Approximated using volume as OI proxy (options_history has no OI)
- `distance_pct`: (max_pain - spot) / spot * 100
- Market makers may pin price near max_pain at expiration (gamma exposure effect)

#### 8. `put_call_parity(symbol, date)`
Test put-call parity for near-term options — detect mispricings or early exercise signals.

Returns:
```
symbol | expiration_date | strike | call_price | put_price | fwd_price |
parity_diff | parity_pct | dividend_risk
```
- `fwd_price`: synthetic forward from put-call parity (C - P + K * exp(-rT))
- `parity_diff`: actual forward vs synthetic forward
- `dividend_risk`: True if call is deep ITM and close to ex-div date (early exercise risk)

### C. Realized volatility (from tiingo_prices)

#### 9. `realized_volatility(symbol, windows, method)`
Historical realized volatility at multiple lookback windows.

Parameters:
- `windows`: list of day windows (default [5, 10, 21, 63, 126, 252])
- `method`: 'close_to_close' (default) or 'yang_zhang'

Returns:
```
symbol | date | rv_5d | rv_10d | rv_21d | rv_63d | rv_126d | rv_252d
```
- Yang-Zhang estimator is more accurate (accounts for overnight gaps)
- Multiple windows show short-term vs long-term vol regime
- Short/long ratio (rv_5d / rv_21d) > 1 = vol expanding; < 1 = vol compressing

#### 10. `vol_regime(symbol, date)`
Classify the current volatility environment.

Returns (single row):
```
symbol | date | current_rv | rv_percentile_252d | iv_rv_ratio |
vol_state | vol_trend
```
- `rv_percentile_252d`: where current RV ranks vs trailing 1 year (0-100)
- `vol_state`: 'low' (<25th pct), 'normal' (25-75th), 'high' (75-90th), 'extreme' (>90th)
- `vol_trend`: 'expanding' (5d > 21d), 'stable' (within 10%), 'compressing' (5d < 21d)
- Only useful after Schwab data lands (needs IV for iv_rv_ratio); graceful empty today

### D. Greeks analytics (from synthetic_options)

#### 11. `portfolio_greeks(symbol, date, model, vol_method)`
Aggregate Greeks across all option chains for a symbol — net directional/time/vol exposure.

Parameters:
- `model`: 'bsm' (default) or 'bs2002'
- `vol_method`: 'cc' (default) or 'yz' or 'vix'

Returns (single row):
```
symbol | date | net_delta | net_gamma | net_theta | net_vega | net_rho |
total_calls | total_puts | delta_neutral_hedge
```
- `delta_neutral_hedge`: number of shares needed to delta-hedge the book (net_delta * 100 * contracts)
- Aggregate Greeks show market-maker hedging flows and directional exposure

#### 12. `gamma_exposure(symbol, date, model, vol_method)`
Gamma exposure by strike — shows where hedging pressure concentrates.

Returns:
```
symbol | strike_price | contract_type | gamma | gamma_x_notional | net_gamma
```
- `gamma_x_notional`: gamma * underlying_price * 100 (dollar gamma per contract)
- `net_gamma`: net long/short gamma across call+put at each strike
- Peak net gamma = area of heaviest dealer hedging activity

#### 13. `theo_vs_market(symbol, date, model, vol_method)`
Compare synthetic option prices against options_history market prices.

Returns:
```
symbol | date | contract_type | strike_price | expiration_date |
market_price | theo_price | diff | diff_pct | edge_direction
```
- `diff_pct`: (theo - market) / market * 100
- `edge_direction`: 'cheap' (market < theo, buy signal), 'rich' (market > theo, sell signal), 'fair'
- Only for symbols in both tables (currently AAPL and MSFT)
- Useful for identifying mispriced options before Schwab data arrives

---

## Group II — Functions for when Schwab OAuth lands

These return empty DataFrame today but become live on first Schwab chain pull.

#### 14. `iv_surface(symbol, date)`
Full implied volatility surface — IV by strike x expiration.

Returns:
```
symbol | date | strike_price | expiration_date | contract_type |
iv | delta | moneyness | dte
```
- Sources schwab_options (preferred) → options_chain (fallback)
- Foundation for all IV analytics (skew, term structure, smile)

#### 15. `iv_skew(symbol, date, ref_delta)`
Measure the slope of the volatility smile — OTM put IV vs ATM IV vs OTM call IV.

Parameters:
- `ref_delta`: reference delta for ATM (default 0.5)

Returns:
```
symbol | date | expiration_date | atm_iv | put_25d_iv | call_25d_iv |
skew_slope | put_call_skew | wing_skew
```
- `skew_slope`: (25d_put_iv - 25d_call_iv) / ATM_iv — standardized skew measure
- `put_call_skew`: 25d_put_iv / ATM_iv — put wing premium
- `wing_skew`: (25d_put_iv + 25d_call_iv) / 2 / ATM_iv — smile convexity

#### 16. `iv_term_structure(symbol, date)`
IV across expiration dates — how the market prices future vol.

Returns:
```
symbol | date | expiration_date | dte | atm_iv | term_slope | term_convexity
```
- `term_slope`: near-term IV - far-term IV (backwardation = fear, contango = complacency)
- `term_convexity`: short-term IV relative to medium-term — kink detection

#### 17. `iv_rv_spread(symbol, date)`
Compare implied vs realized volatility — options rich/cheap relative to actual moves.

Returns:
```
symbol | date | expiration_date | iv | rv_21d | rv_63d | iv_rv_21d |
iv_rv_63d | iv_percentile | rv_percentile
```
- `iv_rv_21d`: IV minus 21-day realized vol — positive = options rich, negative = options cheap
- `iv_percentile`: where IV ranks vs trailing 1 year
- Core vol trading signal: sell vol when IV >> RV, buy when IV << RV

#### 18. `unusual_activity(symbols, volume_threshold, iv_threshold)`
Cross-symbol scanner for unusual options activity combining volume + IV signals.

Returns:
```
symbol | date | contract_type | strike_price | expiration_date |
volume | volume_zscore | iv | iv_change_1d | iv_change_5d |
estimated_premium | activity_score
```
- `activity_score`: composite 0-100 score combining volume spike + IV spike + premium size
- Flags potential institutional flow (high volume + IV spike = someone paying up for options)

#### 19. `vertical_spread_pricing(symbol, date, spread_width)`
Price vertical spreads and compare to theoretical fair value.

Parameters:
- `spread_width`: dollar width between strikes (default 5.0)

Returns:
```
symbol | expiration_date | spread_type | long_strike | short_strike |
market_debit | theoretical_debit | edge | edge_pct | max_profit | max_loss
```
- `spread_type`: 'bull_call' or 'bear_put'
- `edge`: theoretical - market debit (positive = underpriced spread)

---

## File changes

### `analytics/options.py`
- Keep existing `put_call_ratio` and `iv_summary` unchanged
- Add `_normalise_iv_source` (existing, unchanged)
- Add `_load_prices(symbol, start, end)` helper — loads tiingo_prices, returns close-indexed Series
- Add `_load_options_history(symbol, start, end)` helper — loads and validates options_history
- Add all 19 functions above
- Update module docstring with full list of functions and their data sources

### `analytics/__init__.py`
- Add all new function names to imports and `__all__`

### `tests/test_analytics.py`
- New test class per function group (e.g., `TestVolumeSkewBehaviour`, `TestRealizedVolatility`)
- Monkeypatched `q.load` with synthetic DataFrames for each function
- Edge cases: empty data, single-row, missing columns, zero volume
- Signature tests for new parameters
- Live data tests (where data exists) for spot-checking

### Storage
- No new tables needed — all functions read from existing CATALOG tables
- No new pipelines needed — data sources already wired

### `CLAUDE.md`
- Update `analytics/options.py` line to reflect expanded analytics suite

---

## Verification

After implementation:
1. `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_analytics.py -v` — all new + existing tests pass
2. `C:\ProgramData\anaconda3\python.exe -m pytest tests/ -v` — full suite (309+ tests)
3. Live spot-check: call each function with real data (AAPL/PLTR), verify non-empty output and correct columns
4. Group II functions: call with real data, verify clean empty DataFrame returns
5. `C:\ProgramData\anaconda3\python.exe validate.py` — no regressions

---

## Priority order

1. **Volume analytics** (1-6) — highest value, data exists, immediately useful
2. **Realized volatility** (9-10) — needed for vol regime context, tiingo_prices available
3. **Greeks analytics** (11-13) — synthetic_options available, unique edge
4. **Structural metrics** (7-8) — max pain + put-call parity, uses existing data
5. **Group II** (14-19) — graceful empty today, live on OAuth

Total: 19 new functions + existing 2 = 21 options analytics functions.
