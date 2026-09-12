# Unified Evaluation Framework

One framework to answer "does X predict returns?" for any signal, trade
rule, or event set — with the significance battery and PIT discipline
built in, and every result recorded to an append-only registry.

## Quickstart

```
# a repo-native source (adapter)
C:\ProgramData\anaconda3\python.exe evaluate.py --adapter sentiment
C:\ProgramData\anaconda3\python.exe evaluate.py --adapter signal-panel --factor momentum
C:\ProgramData\anaconda3\python.exe evaluate.py --adapter rating --signal-col rating_all
C:\ProgramData\anaconda3\python.exe evaluate.py --adapter rating-changes

# any custom signal: a parquet with [symbol, date, value]
C:\ProgramData\anaconda3\python.exe evaluate.py --input-parquet my_sig.parquet --name my_sig --lag-days 1

# report (reads artifacts only)
C:\ProgramData\anaconda3\python.exe generate_eval_report.py --latest my_sig

# dashboard (Streamlit)
streamlit run dashboard.py

# F1: YAML-driven evaluation (eval framework v2)
python -m evaluation.runner_yaml eval_spec.yaml
python -m evaluation.runner_yaml eval_spec.yaml --dry-run
```

## Contracts (evaluation/contracts.py)

- `Signal(name, frame[symbol,date,value], lag_days, direction)` — continuous
  daily signal. `lag_days` = business days between the data date and when it
  was PUBLIC. `direction=-1` marks a contrarian signal (evaluated on -value).
- `EventSet(name, frame[symbol,date,label], min_events)` — discrete events.
- `TradeRule(name, entries, exits, side, short_entries, short_exits,
  notional)` — callables mapping a per-symbol DataFrame to boolean flags.

## What a run produces

- `storage/reports/eval/<name>_<ts>/`: `results.json`, `run_meta.json`
  (universe, git commit, dropped symbols), `panel.parquet` / `trades.parquet`.
- Registry rows in `storage/eval_registry/results.parquet` — baselines for
  the next model to beat, and the honest trial count for deflated Sharpe.
- **Daily paper trade** (`evaluation=trades_daily`): forward P&L from live
  price refresh on survivor portfolio, registered with full Phase-1 hygiene
  (run_id, input_name, universe_hash, date_range, execution_hash). Run via
  `python -m strategies.portfolio --daily --confirm-run --register`.

## The battery

- Tier 1 (parametric): pooled + daily Spearman IC, cross-sectional
  top/bottom-20% bucket spread, per horizon (1/3/5/10/21d).
- Tier 2 (resampling): date-block bootstrap CI on the spread, moving-block
  Sharpe bootstrap, trade permutation null, Benjamini-Hochberg FDR across
  every p-value in the run.
- Tier 3 (research-grade): walk-forward IS/OOS, regime conditioning
  (SPY 200d SMA bull/bear + 21d realized-vol split), deflated Sharpe with
  the registry population as N-trials, registry percentile.

## PIT rules (enforced by the engine, not the caller)

- `lag_days` applied ONCE in `evaluation/data.py::apply_lag`.
- Entry = first trading close STRICTLY AFTER the (lagged) signal date.
- Forward returns are excess vs SPY; entry and exit closes must be finite
  and > 0 (degenerate-price guard).

## Reading results

|IC| < 0.02 is noise; 0.02-0.05 weak-but-real if t holds; > 0.05 on daily
data = hunt for a leak first. Null results are results — they stay in the
registry as the measured baseline.

## Adding a new signal

Write an adapter (tens of lines — see `evaluation/adapters.py`) or dump a
`[symbol, date, value]` parquet and use `--input-parquet`. Nothing else.

## Notable completed studies (registry keys)

| Study | Registry key | Result | Report |
|---|---|---|---|
| Earnings surprise (Alpha Vantage) | `earnings_surprise_beat` / `earnings_surprise_miss` | **Asymmetric signal**: beat +drift all h (p_adj~1e-4); miss null at low surprise, |surprise|≥5% → -drift (p_adj=0.011) | `experiments/2026-09-11_earnings-surprise-asymmetric-signal.md` |
| CA Form 700 A-1 holdings | `california_disclosures_holding` | NULL (448 events, best p_adj=0.80) | `experiments/2026-09-11_california-disclosures-null-result.md` |
| Congressional trades (PIT S&P 500) | `congressional_trades_sp500` | NULL (61.6% retention, best p_adj=0.355) | `experiments/2026-09-11_congressional-sp500-survivorship.md` |
| Survivor portfolio daily | `bollinger_bands_simple+optimized_doji_breakout_short+rsi_bb_inside_strategy` (trades_daily) | 264 trades, 61.7% WR, +$247k | — |

## F1: Eval Framework v2 (2026-09-12)

### YAML-Configurable Runner
Full evaluation specs in YAML (`evaluation/config.py::EvaluationSpec`), run via:
```bash
python -m evaluation.runner_yaml eval_spec.yaml
python -m evaluation.runner_yaml eval_spec.yaml --dry-run   # print runner kwargs
python -m evaluation.runner_yaml --print-example             # print template
```
Example: `eval_spec_f1_example.yaml` (all features enabled).

### Capital-Constrained Compounding
Fixes the equal-notional blind spot: tracks actual capital over time,
compounds P&L, enforces position/leverage limits at each rebalance.

```yaml
capital_constrained:
  enabled: true
  initial_capital: 1_000_000.0
  max_leverage: 1.0           # gross exposure / capital
  max_position_pct: 0.10      # single position limit
  max_sector_pct: 0.30        # sector concentration (requires sector map)
  compound_returns: true      # compound P&L into capital base
  rebalance_on_capital_change: true
  capital_change_threshold_pct: 5.0
```

### Price-Volume Signal Family
Volume-aware signal variants applied PIT-safe at signal construction:

```yaml
price_volume:
  enabled: true
  volume_window: 21
  volume_ma_window: 63
  min_volume_ratio: 1.5       # volume confirmation threshold
  volume_weighted: true       # scale signal by sqrt(volume/vol_MA)
  divergence_lookback: 5      # dampen on price-volume divergence
```

Three variants:
1. **Volume-weighted**: signal × √(volume/vol_MA), clipped [0.1, 5.0]
2. **Volume-confirmed**: zero signal where volume < min_volume_ratio × vol_MA
3. **Divergence detection**: halve signal when price trend ≠ volume trend
