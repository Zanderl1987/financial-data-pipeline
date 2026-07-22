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
