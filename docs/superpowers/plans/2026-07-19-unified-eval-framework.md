# Unified Evaluation Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `evaluation/` package + `evaluate.py` / `generate_eval_report.py` CLI pair: one framework that evaluates any Signal, TradeRule, or EventSet against forward returns with a three-tier significance battery and an append-only results registry.

**Architecture:** New `evaluation/` package wrapping existing battle-tested primitives (`event_backtest.load_close`/`load_close_matrix`/`event_study`, `backtest.backtest`, generalized `tv_rating_eval.evaluate_signal`/`simulate_trades`). Typed contracts (`Signal`/`EventSet`/`TradeRule`) validate loudly at construction; `data.py` is the ONE place point-in-time rules (lag_days, next-close entry) live; `stats.py` holds the whole significance battery (Tier 1 parametric, Tier 2 resampling, Tier 3 research-grade); `registry.py` accumulates baselines; `runner.py` + `evaluate.py` orchestrate; `generate_eval_report.py` renders a self-contained Plotly HTML from artifacts only. Existing scripts (`sentiment_eval.py`, `tv_rating_eval.py`, `backtest.py`, `event_backtest.py`) are NOT modified.

**Tech Stack:** pandas / numpy / scipy.stats (already in use), plotly.graph_objects (installed, v5.9.0). No new dependencies.

## Global Constraints

- Python: `C:\ProgramData\anaconda3\python.exe` — always the full path; bare `python` is a broken MS Store stub.
- Run all commands from the repo root: `C:\Users\zande\PycharmProjects\financial-data-pipeline`.
- ASCII-only in all CLI print output (Windows cp1252: no `═ ▶ ✓`; use `= >> + ! X`).
- Never name a DataFrame column `year` or `month` (Hive partition shadowing). Registry and artifacts use `date_range` strings, never year columns.
- Prices only via the query layer / `event_backtest.load_close*` (curated), never raw globs.
- Import repo modules (`event_backtest`, `backtest`, `query`, `analytics.*`) **locally inside the functions that call them**, not at module top of `evaluation/*` — this is the repo convention that lets tests monkeypatch them (see `tv_rating_eval.py`, `tests/test_event_backtest.py`).
- Do NOT wire anything into `run_all.py`, `curated.py`, `validate.py`, or the pipeline-catalog tests. This is an analysis tool, not an ingestion pipeline.
- Do NOT modify `sentiment_eval.py`, `tv_rating_eval.py`, `backtest.py`, `event_backtest.py`, `analytics/*` in this plan.
- All new tests go in `tests/test_evaluation.py` (spec-mandated single file).
- Horizons fixed at `(1, 3, 5, 10, 21)` trading days; benchmark default `SPY`; universe default = all `tiingo_prices` symbols.
- PIT invariant: `lag_days` is applied in exactly one place (`evaluation/data.py:apply_lag`); every evaluator gets entry at the NEXT trading close after the (lagged) signal date, enforced by the engine, never by the input.
- Statistics whose assumptions fail return `None` plus a `*_reason` string (the `sd > 0` bug class hit twice in the TV build — never divide by a zero/NaN sd).
- `storage/reports/` and `storage/eval_registry/` are gitignored (`storage/reports/` already is; Task 8 adds the registry dir to `.gitignore`).
- Spec reference: `docs/superpowers/specs/2026-07-18-unified-eval-framework-design.md`.

## File Structure

| File | Responsibility |
|---|---|
| `evaluation/__init__.py` | Re-export contracts + `run` |
| `evaluation/contracts.py` | `Signal`, `EventSet`, `TradeRule` dataclasses; ALL input validation |
| `evaluation/data.py` | `apply_lag`, `build_return_panel` — the one PIT place; forward excess returns |
| `evaluation/stats.py` | Tier 1 parametric, Tier 2 resampling (bootstrap/permutation/BH-FDR), Tier 3 (walk-forward, regimes, deflated Sharpe, registry percentile) |
| `evaluation/ic.py` | Level-IC evaluation of a `Signal` (panel + Tier 1) |
| `evaluation/portfolio.py` | Quantile-portfolio evaluation (wraps `backtest.backtest`) |
| `evaluation/events.py` | Event-study evaluation per label (wraps `event_backtest.event_study`) |
| `evaluation/trades.py` | Generic trade-simulation engine + permutation null |
| `evaluation/registry.py` | Append-only parquet results store; `baselines()`, `compare()`, `population()` |
| `evaluation/adapters.py` | `from_signal_panel`, `from_rating_history`, `from_sentiment`, `from_rating_changes`, `tv_threshold_rule`, `rating_cache` |
| `evaluation/runner.py` | Dispatch input → evaluations → registry rows + artifacts + `run_meta.json` |
| `evaluate.py` | Compute-stage CLI (repo root) |
| `generate_eval_report.py` | Report-stage CLI (repo root), reads artifacts only |
| `tests/test_evaluation.py` | Entire test suite for the package |

Task order: 1 contracts → 2 data → 3 Tier-1 stats + ic → 4 portfolio + events → 5 trades → 6 Tier-2 stats → 7 Tier-3 stats → 8 registry → 9 runner + evaluate CLI → 10 adapters → 11 report → 12 acceptance run + docs.

---

### Task 1: Package scaffold + input contracts

**Files:**
- Create: `evaluation/__init__.py`, `evaluation/contracts.py`
- Test: `tests/test_evaluation.py` (new file)

**Interfaces:**
- Produces: `Signal(name, frame, lag_days=0, direction=1, source="")` with `frame` columns exactly `[symbol, date, value]` after validation, sorted by (date, symbol), `date` tz-naive datetime64; `EventSet(name, frame, lag_days=0, min_events=5)` with frame columns `[symbol, date, label]` (+ optional `magnitude`); `TradeRule(name, entries, exits, side="long", short_entries=None, short_exits=None, notional=10000.0)` where `entries`/`exits` are callables `(pd.DataFrame) -> pd.Series[bool]` for the long side and the `short_*` pair is required iff `side="both"` (spec gives one entries/exits pair + side; with `side="both"` a single pair cannot distinguish long from short triggers, so the short side gets its own pair — for `side="short"`, `entries`/`exits` ARE the short rule).
- Constant: `MIN_DATES_WARN = 250`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_evaluation.py`:

```python
"""
test_evaluation.py -- unified evaluation framework (evaluation/ package).
All synthetic data; no stored data or API keys. Repo modules
(event_backtest, backtest, query, analytics.*) are monkeypatched where the
code under test imports them locally.
"""

import json
import math
import os
import sys
import warnings

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from evaluation.contracts import Signal, EventSet, TradeRule


def _sig_frame(n_dates=300, symbols=("AAA", "BBB", "CCC")):
    dates = pd.bdate_range("2023-01-02", periods=n_dates)
    rows = [{"symbol": s, "date": d, "value": float(i + j)}
            for j, s in enumerate(symbols) for i, d in enumerate(dates)]
    return pd.DataFrame(rows)


class TestSignalContract:
    def test_valid_signal_constructs_and_sorts(self):
        f = _sig_frame().sample(frac=1.0, random_state=0)   # shuffled input
        s = Signal(name="toy", frame=f)
        assert list(s.frame.columns) == ["symbol", "date", "value"]
        assert s.frame["date"].is_monotonic_increasing
        assert str(s.frame["date"].dtype) == "datetime64[ns]"

    def test_missing_column_raises(self):
        with pytest.raises(ValueError, match="missing columns"):
            Signal(name="toy", frame=pd.DataFrame({"symbol": ["A"], "date": ["2024-01-02"]}))

    def test_duplicate_symbol_date_raises(self):
        f = pd.concat([_sig_frame(), _sig_frame().head(1)], ignore_index=True)
        with pytest.raises(ValueError, match="duplicate"):
            Signal(name="toy", frame=f)

    def test_nan_value_raises(self):
        f = _sig_frame()
        f.loc[0, "value"] = np.nan
        with pytest.raises(ValueError, match="NaN"):
            Signal(name="toy", frame=f)

    def test_tz_aware_dates_raise(self):
        f = _sig_frame()
        f["date"] = pd.to_datetime(f["date"]).dt.tz_localize("UTC")
        with pytest.raises(ValueError, match="tz-naive"):
            Signal(name="toy", frame=f)

    def test_bad_direction_raises(self):
        with pytest.raises(ValueError, match="direction"):
            Signal(name="toy", frame=_sig_frame(), direction=2)

    def test_short_history_warns_not_fails(self):
        with pytest.warns(UserWarning, match="distinct dates"):
            Signal(name="toy", frame=_sig_frame(n_dates=50))

    def test_long_history_no_warning(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            Signal(name="toy", frame=_sig_frame(n_dates=300))


class TestEventSetContract:
    def test_valid_event_set(self):
        f = pd.DataFrame({"symbol": ["AAA", "BBB"],
                          "date": ["2024-01-03", "2024-02-05"],
                          "label": ["up", "down"]})
        e = EventSet(name="toy_events", frame=f)
        assert e.min_events == 5
        assert str(e.frame["date"].dtype) == "datetime64[ns]"

    def test_missing_label_raises(self):
        f = pd.DataFrame({"symbol": ["AAA"], "date": ["2024-01-03"]})
        with pytest.raises(ValueError, match="missing columns"):
            EventSet(name="toy_events", frame=f)

    def test_nan_label_raises(self):
        f = pd.DataFrame({"symbol": ["AAA"], "date": ["2024-01-03"], "label": [None]})
        with pytest.raises(ValueError, match="NaN"):
            EventSet(name="toy_events", frame=f)


class TestTradeRuleContract:
    def test_valid_long_rule(self):
        r = TradeRule(name="toy_rule",
                      entries=lambda d: d["x"] > 0, exits=lambda d: d["x"] < 0)
        assert r.side == "long" and r.notional == 10_000.0

    def test_bad_side_raises(self):
        with pytest.raises(ValueError, match="side"):
            TradeRule(name="r", entries=lambda d: d, exits=lambda d: d, side="sideways")

    def test_both_requires_short_pair(self):
        with pytest.raises(ValueError, match="short_entries"):
            TradeRule(name="r", entries=lambda d: d, exits=lambda d: d, side="both")

    def test_non_callable_raises(self):
        with pytest.raises(ValueError, match="callable"):
            TradeRule(name="r", entries="not a function", exits=lambda d: d)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_evaluation.py -v`
Expected: collection error `ModuleNotFoundError: No module named 'evaluation'`.

- [ ] **Step 3: Implement `evaluation/contracts.py` and `evaluation/__init__.py`**

`evaluation/contracts.py`:

```python
"""
evaluation/contracts.py -- typed input contracts for the unified evaluation
framework.

Validation happens HERE, loudly, at construction. Everything downstream
(data.py, the evaluators, runner.py) trusts a constructed contract and never
re-validates. See docs/superpowers/specs/2026-07-18-unified-eval-framework-design.md.
"""

import warnings
from dataclasses import dataclass, field
from typing import Callable, Optional

import pandas as pd

MIN_DATES_WARN = 250


def _clean_dates(frame: pd.DataFrame, who: str) -> pd.DataFrame:
    out = frame.copy()
    dates = pd.to_datetime(out["date"])
    if getattr(dates.dt, "tz", None) is not None:
        raise ValueError(f"{who}: dates must be tz-naive")
    out["date"] = dates
    return out


@dataclass
class Signal:
    """
    Continuous score per (symbol, day).

    lag_days  : days after `date` the value became knowable (0 for
                price-derived signals; explicit and conservative for
                filed/published data). Applied ONLY by data.apply_lag().
    direction : +1 higher-is-better, -1 lower-is-better, 0 unknown --
                orients bucket definitions and expected IC sign in reports;
                0 reports raw signs with no orientation applied.
    """
    name: str
    frame: pd.DataFrame
    lag_days: int = 0
    direction: int = 1
    source: str = ""

    def __post_init__(self):
        who = f"Signal '{self.name}'"
        missing = {"symbol", "date", "value"} - set(self.frame.columns)
        if missing:
            raise ValueError(f"{who}: missing columns {sorted(missing)}")
        if self.direction not in (-1, 0, 1):
            raise ValueError(f"{who}: direction must be -1, 0, or +1")
        if self.lag_days < 0:
            raise ValueError(f"{who}: lag_days must be >= 0")
        f = _clean_dates(self.frame[["symbol", "date", "value"]], who)
        if f["value"].isna().any():
            raise ValueError(f"{who}: NaN values not allowed -- drop or fill upstream")
        if f.duplicated(["symbol", "date"]).any():
            raise ValueError(f"{who}: duplicate (symbol, date) rows -- the provider "
                             "must aggregate to one row per (symbol, date)")
        n_dates = f["date"].nunique()
        if n_dates < MIN_DATES_WARN:
            warnings.warn(f"{who}: only {n_dates} distinct dates (< {MIN_DATES_WARN}) "
                          "-- daily-IC t-stats will be unreliable")
        self.frame = f.sort_values(["date", "symbol"]).reset_index(drop=True)


@dataclass
class EventSet:
    """Discrete point-in-time occurrences, grouped by `label` for study."""
    name: str
    frame: pd.DataFrame
    lag_days: int = 0
    min_events: int = 5

    def __post_init__(self):
        who = f"EventSet '{self.name}'"
        missing = {"symbol", "date", "label"} - set(self.frame.columns)
        if missing:
            raise ValueError(f"{who}: missing columns {sorted(missing)}")
        if self.lag_days < 0:
            raise ValueError(f"{who}: lag_days must be >= 0")
        keep = ["symbol", "date", "label"] + (["magnitude"] if "magnitude" in self.frame.columns else [])
        f = _clean_dates(self.frame[keep], who)
        if f[["symbol", "date", "label"]].isna().any().any():
            raise ValueError(f"{who}: NaN in symbol/date/label not allowed")
        self.frame = f.sort_values(["date", "symbol"]).reset_index(drop=True)


@dataclass
class TradeRule:
    """
    A system producing discrete trades. entries/exits are callables
    (df) -> boolean Series over an OHLCV+signal frame; for side="long" or
    "short" they define that side's rule; for side="both" they define the
    LONG rule and short_entries/short_exits define the short rule.

    Rules see data up to and including day t; the ENGINE (trades.py)
    executes at the close of t+1. Entry timing is never trusted to the rule.
    """
    name: str
    entries: Callable
    exits: Callable
    side: str = "long"
    short_entries: Optional[Callable] = None
    short_exits: Optional[Callable] = None
    notional: float = 10_000.0

    def __post_init__(self):
        who = f"TradeRule '{self.name}'"
        if self.side not in ("long", "short", "both"):
            raise ValueError(f"{who}: side must be 'long', 'short', or 'both'")
        for label, fn in (("entries", self.entries), ("exits", self.exits)):
            if not callable(fn):
                raise ValueError(f"{who}: {label} must be callable")
        if self.side == "both":
            if not (callable(self.short_entries) and callable(self.short_exits)):
                raise ValueError(f"{who}: side='both' requires callable "
                                 "short_entries and short_exits")
        if self.notional <= 0:
            raise ValueError(f"{who}: notional must be positive")
```

`evaluation/__init__.py`:

```python
"""evaluation -- unified signal/trade/event evaluation framework (v1)."""

from evaluation.contracts import Signal, EventSet, TradeRule  # noqa: F401
```

(`runner.run` is added to this re-export in Task 9.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_evaluation.py -v`
Expected: all Task-1 tests PASS.

- [ ] **Step 5: Run the full suite to confirm nothing broke**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/ -q`
Expected: everything green (repo was 300+ passing; the pre-existing known failure state, if any, unchanged).

- [ ] **Step 6: Commit**

```bash
git add evaluation/__init__.py evaluation/contracts.py tests/test_evaluation.py
git commit -m "feat(evaluation): package scaffold + Signal/EventSet/TradeRule contracts"
```

---

### Task 2: Return-panel builder — the ONE point-in-time place

**Files:**
- Create: `evaluation/data.py`
- Test: `tests/test_evaluation.py` (append)

**Interfaces:**
- Consumes: `Signal.frame` layout from Task 1 (`symbol, date, value`).
- Produces: `HORIZONS = (1, 3, 5, 10, 21)`; `apply_lag(frame, lag_days) -> pd.DataFrame` (dates advanced by `lag_days` business days — THE one lag implementation); `load_closes(symbols, start=None, end=None, benchmark="SPY", price_table=None) -> pd.DataFrame` (wide date x symbol close matrix incl. benchmark, via `event_backtest.load_close_matrix`, local import); `build_return_panel(frame, closes, horizons=HORIZONS, benchmark="SPY") -> tuple[pd.DataFrame, dict]` — panel columns `symbol, date, value..., entry_date, fwd_1d, fwd_3d, fwd_5d, fwd_10d, fwd_21d`, plus a `dropped` dict `{symbol: reason}`. Entry = first trading close STRICTLY AFTER the (lagged) date; `fwd_{h}d` = entry-close to h-trading-days-later close, excess vs benchmark's matching path (benchmark reindexed+ffilled onto the symbol's own trading dates); benchmark symbol excluded from output; non-positive/NaN entry or exit closes masked to NaN.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_evaluation.py`:

```python
from evaluation.data import HORIZONS, apply_lag, build_return_panel


def _close_matrix(n=40):
    idx = pd.bdate_range("2024-01-02", periods=n)
    return pd.DataFrame({"AAA": 100.0 * (1.01 ** np.arange(n)),
                         "SPY": np.full(n, 100.0)}, index=idx)


class TestApplyLag:
    def test_zero_lag_returns_copy_unchanged(self):
        f = pd.DataFrame({"symbol": ["AAA"], "date": [pd.Timestamp("2024-01-05")],
                          "value": [1.0]})
        out = apply_lag(f, 0)
        assert out is not f
        assert out["date"].iloc[0] == pd.Timestamp("2024-01-05")

    def test_lag_moves_business_days(self):
        f = pd.DataFrame({"symbol": ["AAA"], "date": [pd.Timestamp("2024-01-05")],
                          "value": [1.0]})           # a Friday
        out = apply_lag(f, 2)
        assert out["date"].iloc[0] == pd.Timestamp("2024-01-09")   # Fri + 2bd = Tue


class TestBuildReturnPanel:
    def test_entry_is_strictly_next_close(self):
        closes = _close_matrix()
        idx = closes.index
        f = pd.DataFrame({"symbol": ["AAA"], "date": [idx[5]], "value": [1.0]})
        panel, dropped = build_return_panel(f, closes, benchmark=None)
        assert dropped == {}
        assert panel["entry_date"].iloc[0] == idx[6]
        expected = closes["AAA"].iloc[7] / closes["AAA"].iloc[6] - 1.0
        assert panel["fwd_1d"].iloc[0] == pytest.approx(expected)

    def test_flat_benchmark_equals_raw_return(self):
        closes = _close_matrix()          # SPY constant 100 -> excess == raw
        f = pd.DataFrame({"symbol": ["AAA"], "date": [closes.index[5]], "value": [1.0]})
        raw, _ = build_return_panel(f, closes, benchmark=None)
        exc, _ = build_return_panel(f, closes, benchmark="SPY")
        assert exc["fwd_5d"].iloc[0] == pytest.approx(raw["fwd_5d"].iloc[0])

    def test_excess_vs_identical_benchmark_is_zero(self):
        closes = _close_matrix()
        closes["SPY"] = closes["AAA"]
        f = pd.DataFrame({"symbol": ["AAA"], "date": [closes.index[5]], "value": [1.0]})
        panel, _ = build_return_panel(f, closes, benchmark="SPY")
        assert panel["fwd_5d"].iloc[0] == pytest.approx(0.0, abs=1e-12)

    def test_lag_pushes_entry_and_kills_tail(self):
        closes = _close_matrix()
        idx = closes.index
        f = pd.DataFrame({"symbol": ["AAA", "AAA"],
                          "date": [idx[5], idx[38]], "value": [1.0, 2.0]})
        panel, _ = build_return_panel(apply_lag(f, 3), closes, benchmark=None)
        # idx[5] + 3bd = idx[8] -> entry strictly after = idx[9]
        assert panel["entry_date"].iloc[0] == idx[9]
        # idx[38] + 3bd is past the data end -> no entry, all horizons NaN
        assert pd.isna(panel["entry_date"].iloc[1])
        assert panel[[f"fwd_{h}d" for h in HORIZONS]].iloc[1].isna().all()

    def test_benchmark_symbol_excluded(self):
        closes = _close_matrix()
        f = pd.DataFrame({"symbol": ["SPY"], "date": [closes.index[5]], "value": [1.0]})
        panel, dropped = build_return_panel(f, closes, benchmark="SPY")
        assert panel.empty
        assert "SPY" in dropped and "benchmark" in dropped["SPY"]

    def test_unknown_symbol_and_short_history_dropped(self):
        closes = _close_matrix()
        closes["SHT"] = np.nan
        closes.iloc[:10, closes.columns.get_loc("SHT")] = 50.0
        f = pd.DataFrame({"symbol": ["ZZZ", "SHT"],
                          "date": [closes.index[2]] * 2, "value": [1.0, 1.0]})
        panel, dropped = build_return_panel(f, closes, benchmark=None)
        assert dropped["ZZZ"] == "no price data"
        assert "history too short" in dropped["SHT"]

    def test_nonpositive_prices_masked(self):
        closes = _close_matrix()
        closes.iloc[9, closes.columns.get_loc("AAA")] = -1.0   # WTI-Apr-2020 class
        f = pd.DataFrame({"symbol": ["AAA"], "date": [closes.index[5]], "value": [1.0]})
        panel, _ = build_return_panel(f, closes, benchmark=None)
        # entry = idx[6]; h=3 exits at idx[9] (the bad close) -> masked to NaN
        assert pd.isna(panel["fwd_3d"].iloc[0])
        # h=1 exits at idx[7], untouched -> still a real return
        assert np.isfinite(panel["fwd_1d"].iloc[0])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_evaluation.py -v -k "ApplyLag or BuildReturnPanel"`
Expected: collection error `ModuleNotFoundError: No module named 'evaluation.data'`.

- [ ] **Step 3: Implement `evaluation/data.py`**

```python
"""
evaluation/data.py -- price/return panel builder. The ONE place point-in-time
rules live:

  * apply_lag()          -- the only implementation of publication lag.
  * build_return_panel() -- entry at the first trading close STRICTLY AFTER
                            the (lagged) signal date; forward returns excess
                            vs the benchmark's matching path; non-positive
                            prices masked (degenerate pct_change guard).

No other evaluation module ever shifts dates. See
docs/superpowers/specs/2026-07-18-unified-eval-framework-design.md.
"""

import numpy as np
import pandas as pd

HORIZONS = (1, 3, 5, 10, 21)


def apply_lag(frame: pd.DataFrame, lag_days: int) -> pd.DataFrame:
    """Advance `date` by lag_days business days (0 = no-op). Returns a copy."""
    out = frame.copy()
    if lag_days:
        out["date"] = pd.to_datetime(out["date"]) + pd.offsets.BDay(int(lag_days))
    return out


def load_closes(symbols, start=None, end=None, benchmark="SPY",
                price_table=None) -> pd.DataFrame:
    """Wide close matrix for symbols (+ benchmark), longest-series invariant."""
    import event_backtest as eb          # local import: repo test convention
    syms = list(dict.fromkeys(list(symbols) + ([benchmark] if benchmark else [])))
    return eb.load_close_matrix(syms, start=start, end=end, price_table=price_table)


def build_return_panel(frame: pd.DataFrame, closes: pd.DataFrame,
                       horizons=HORIZONS, benchmark="SPY"):
    """
    Tidy panel: one row per input signal row, with entry_date and fwd_{h}d
    forward EXCESS returns. Returns (panel, dropped) where dropped maps
    symbol -> reason for every symbol that produced no rows.
    """
    dropped = {}
    min_len = max(horizons) + 2
    bench = None
    if benchmark and benchmark in closes.columns:
        bench = closes[benchmark].dropna()

    frames = []
    for sym, grp in frame.groupby("symbol", sort=False):
        if benchmark and sym == benchmark:
            dropped[sym] = "benchmark symbol excluded (excess vs itself is 0)"
            continue
        if sym not in closes.columns:
            dropped[sym] = "no price data"
            continue
        s = closes[sym].dropna()
        if len(s) < min_len:
            dropped[sym] = f"history too short ({len(s)} closes < {min_len})"
            continue

        c = s.to_numpy(dtype=float)
        n = len(s)
        b = bench.reindex(s.index).ffill().to_numpy(dtype=float) if bench is not None else None

        out = grp.reset_index(drop=True).copy()
        out["date"] = pd.to_datetime(out["date"])
        entry_loc = s.index.searchsorted(out["date"].to_numpy(), side="right")
        ok = entry_loc < n
        entry_dates = pd.Series(pd.NaT, index=out.index, dtype="datetime64[ns]")
        entry_dates[ok] = s.index.to_numpy()[entry_loc[ok]]
        out["entry_date"] = entry_dates

        for h in horizons:
            exit_loc = entry_loc + h
            ret = np.full(len(out), np.nan)
            m = ok & (exit_loc < n)
            if m.any():
                e0, e1 = c[entry_loc[m]], c[exit_loc[m]]
                good = np.isfinite(e0) & np.isfinite(e1) & (e0 > 0) & (e1 > 0)
                r = np.where(good, np.divide(e1, e0, where=e0 != 0) - 1.0, np.nan)
                if b is not None:
                    b0, b1 = b[entry_loc[m]], b[exit_loc[m]]
                    bgood = np.isfinite(b0) & np.isfinite(b1) & (b0 > 0) & (b1 > 0)
                    br = np.where(bgood, np.divide(b1, b0, where=b0 != 0) - 1.0, np.nan)
                    r = r - br
                ret[m] = r
            out[f"fwd_{h}d"] = ret
        frames.append(out)

    if not frames:
        return pd.DataFrame(), dropped
    return pd.concat(frames, ignore_index=True), dropped
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_evaluation.py -v`
Expected: all tests PASS (Task 1 + Task 2).

- [ ] **Step 5: Commit**

```bash
git add evaluation/data.py tests/test_evaluation.py
git commit -m "feat(evaluation): return-panel builder -- the one PIT place (lag + next-close entry + excess returns)"
```

---

### Task 3: Tier-1 parametric stats + IC evaluator

**Files:**
- Create: `evaluation/stats.py` (Tier 1 section; Tiers 2/3 appended by Tasks 6/7)
- Create: `evaluation/ic.py`
- Test: `tests/test_evaluation.py` (append)

**Interfaces:**
- Consumes: panel layout from Task 2 (`symbol, date, value, fwd_{h}d`).
- Produces (stats.py): `pooled_ic(values, fwd) -> dict` (`pooled_ic, pooled_p, n` or None values + `pooled_reason`); `daily_ic(panel, value_col, fwd_col, min_names=5) -> dict` (`mean_daily_ic, ic_se, ic_t_stat, ic_days, ic_pct_positive` or None values + `daily_reason`); `quantile_spread(panel, value_col, fwd_col, q=0.2, min_side=6) -> dict` (`spread_pct, spread_t, spread_p, top_n, bottom_n, top_mean_pct, bottom_mean_pct` or None values + `spread_reason`); `t_to_p(t) -> float` (two-sided normal).
- Produces (ic.py): `evaluate_ic(panel, direction=1, horizons=HORIZONS, min_names=5, q=0.2) -> dict[int, dict]` — per horizon, the union of the three Tier-1 dicts plus `oriented` (the direction applied; `direction=-1` evaluates `-value`, `0` evaluates raw values).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_evaluation.py`:

```python
from evaluation import stats as ev_stats
from evaluation.ic import evaluate_ic


def _planted_panel(n_dates=300, n_syms=8, slope=0.01, noise=0.001, seed=0):
    """fwd_1d = slope * value + eps -- a signal that OBVIOUSLY works."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=n_dates)
    rows = []
    for d in dates:
        vals = rng.normal(size=n_syms)
        for k in range(n_syms):
            rows.append({"symbol": f"S{k}", "date": d, "value": float(vals[k]),
                         "fwd_1d": slope * float(vals[k]) + rng.normal(scale=noise)})
    return pd.DataFrame(rows)


def _noise_panel(n_dates=300, n_syms=8, seed=1):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=n_dates)
    rows = [{"symbol": f"S{k}", "date": d, "value": float(rng.normal()),
             "fwd_1d": float(rng.normal(scale=0.01))}
            for d in dates for k in range(n_syms)]
    return pd.DataFrame(rows)


class TestTier1:
    def test_planted_signal_detected_everywhere(self):
        p = _planted_panel()
        pool = ev_stats.pooled_ic(p["value"], p["fwd_1d"])
        assert pool["pooled_ic"] > 0.9
        d = ev_stats.daily_ic(p, "value", "fwd_1d")
        assert d["mean_daily_ic"] > 0.9 and d["ic_t_stat"] > 10
        s = ev_stats.quantile_spread(p, "value", "fwd_1d")
        assert s["spread_pct"] > 0 and s["spread_t"] > 10

    def test_noise_not_detected(self):
        p = _noise_panel()
        pool = ev_stats.pooled_ic(p["value"], p["fwd_1d"])
        assert abs(pool["pooled_ic"]) < 0.05
        s = ev_stats.quantile_spread(p, "value", "fwd_1d")
        assert abs(s["spread_t"]) < 3

    def test_pooled_too_few_pairs_reason(self):
        r = ev_stats.pooled_ic(pd.Series([1.0, 2.0]), pd.Series([0.1, 0.2]))
        assert r["pooled_ic"] is None and "fewer than" in r["pooled_reason"]

    def test_daily_zero_variance_guard(self):
        p = _planted_panel()
        p["value"] = 1.0                       # constant -> no per-day ranks
        d = ev_stats.daily_ic(p, "value", "fwd_1d")
        assert d["ic_t_stat"] is None and "daily_reason" in d

    def test_t_to_p_two_sided(self):
        assert ev_stats.t_to_p(0.0) == pytest.approx(1.0)
        assert ev_stats.t_to_p(1.96) == pytest.approx(0.05, abs=0.01)


class TestEvaluateIC:
    def test_direction_minus_one_orients(self):
        p = _planted_panel()
        p["value"] = -p["value"]               # now LOWER value = higher return
        res = evaluate_ic(p, direction=-1)
        assert res[1]["pooled_ic"] > 0.9       # orientation recovers the sign
        assert res[1]["oriented"] == -1

    def test_missing_horizon_columns_skipped(self):
        p = _planted_panel()                   # only fwd_1d exists
        res = evaluate_ic(p)
        assert list(res.keys()) == [1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_evaluation.py -v -k "Tier1 or EvaluateIC"`
Expected: collection error `ModuleNotFoundError: No module named 'evaluation.stats'`.

- [ ] **Step 3: Implement `evaluation/stats.py` (Tier 1) and `evaluation/ic.py`**

`evaluation/stats.py`:

```python
"""
evaluation/stats.py -- the entire significance battery, all three tiers.

Tier 1 (parametric)     -- this section (Task 3).
Tier 2 (resampling)     -- appended by Task 6.
Tier 3 (research-grade) -- appended by Task 7.

House rule: a statistic whose assumptions fail returns None plus a
'*_reason' string. NEVER divide by a zero/NaN sd (bug class hit twice
in the TV-rating build).
"""

import math

import numpy as np
import pandas as pd
from scipy import stats as sps

# --------------------------------------------------------------- Tier 1


def t_to_p(t: float) -> float:
    """Two-sided p from a t-statistic via the normal approximation."""
    return float(2.0 * (1.0 - sps.norm.cdf(abs(t))))


def pooled_ic(values, fwd) -> dict:
    x = pd.Series(values).reset_index(drop=True)
    y = pd.Series(fwd).reset_index(drop=True)
    m = x.notna() & y.notna()
    n = int(m.sum())
    if n < 10:
        return {"pooled_ic": None, "pooled_p": None, "n": n,
                "pooled_reason": f"fewer than 10 pairs (n={n})"}
    if x[m].nunique() < 2 or y[m].nunique() < 2:
        return {"pooled_ic": None, "pooled_p": None, "n": n,
                "pooled_reason": "no variance in values or returns"}
    rho, p = sps.spearmanr(x[m], y[m])
    return {"pooled_ic": round(float(rho), 4), "pooled_p": round(float(p), 4), "n": n}


def daily_ic(panel: pd.DataFrame, value_col: str, fwd_col: str,
             min_names: int = 5) -> dict:
    sub = panel.dropna(subset=[value_col, fwd_col])
    ics = []
    for _, day in sub.groupby("date"):
        if day["symbol"].nunique() >= min_names and day[value_col].nunique() > 1:
            r, _ = sps.spearmanr(day[value_col], day[fwd_col])
            if np.isfinite(r):
                ics.append(r)
    if len(ics) < 5:
        return {"mean_daily_ic": None, "ic_se": None, "ic_t_stat": None,
                "ic_days": len(ics),
                "daily_reason": f"only {len(ics)} usable days (< 5)"}
    a = np.asarray(ics)
    sd = a.std(ddof=1)
    out = {"mean_daily_ic": round(float(a.mean()), 4), "ic_days": int(len(a)),
           "ic_pct_positive": round(100 * float((a > 0).mean()), 1)}
    if sd > 0:
        se = sd / math.sqrt(len(a))
        out["ic_se"] = round(float(se), 5)
        out["ic_t_stat"] = round(float(a.mean() / se), 2)
    else:
        out["ic_se"] = None
        out["ic_t_stat"] = None
        out["daily_reason"] = "zero cross-day variance in daily ICs"
    return out


def quantile_spread(panel: pd.DataFrame, value_col: str, fwd_col: str,
                    q: float = 0.2, min_side: int = 6) -> dict:
    """
    Pooled top-q vs bottom-q cross-sectional bucket spread (per-date buckets,
    pooled returns, Welch t). Cross-sectional quantiles rather than absolute
    thresholds so arbitrary signal scales work.
    """
    sub = panel.dropna(subset=[value_col, fwd_col])
    tops, bots = [], []
    for _, day in sub.groupby("date"):
        if len(day) < 2 or day[value_col].nunique() < 2:
            continue
        k = max(1, int(round(len(day) * q)))
        r = day.sort_values(value_col)
        tops.append(r[fwd_col].tail(k))
        bots.append(r[fwd_col].head(k))
    top = pd.concat(tops) if tops else pd.Series(dtype=float)
    bot = pd.concat(bots) if bots else pd.Series(dtype=float)
    if len(top) <= min_side or len(bot) <= min_side:
        return {"spread_pct": None, "spread_t": None, "spread_p": None,
                "top_n": int(len(top)), "bottom_n": int(len(bot)),
                "spread_reason": f"bucket too small (top={len(top)}, bottom={len(bot)})"}
    sd_t, sd_b = top.std(ddof=1), bot.std(ddof=1)
    out = {"top_n": int(len(top)), "bottom_n": int(len(bot)),
           "top_mean_pct": round(100 * float(top.mean()), 3),
           "bottom_mean_pct": round(100 * float(bot.mean()), 3),
           "spread_pct": round(100 * float(top.mean() - bot.mean()), 3)}
    if (sd_t > 0 or sd_b > 0) and np.isfinite(sd_t) and np.isfinite(sd_b):
        t, p = sps.ttest_ind(top, bot, equal_var=False)
        out["spread_t"] = round(float(t), 2)
        out["spread_p"] = round(float(p), 4)
    else:
        out["spread_t"] = None
        out["spread_p"] = None
        out["spread_reason"] = "zero variance in both buckets"
    return out
```

`evaluation/ic.py`:

```python
"""
evaluation/ic.py -- level-IC evaluation of a continuous signal panel.
Generalizes tv_rating_eval.evaluate_signal: pooled + daily cross-sectional
IC and a cross-sectional quantile bucket spread, per horizon.
"""

import pandas as pd

from evaluation.data import HORIZONS
from evaluation import stats as ev_stats


def evaluate_ic(panel: pd.DataFrame, direction: int = 1, horizons=HORIZONS,
                min_names: int = 5, q: float = 0.2) -> dict:
    """
    direction=+1 evaluates `value` as higher-is-better; -1 evaluates -value
    (so a GOOD contrarian signal reports positive oriented IC); 0 evaluates
    raw values with no orientation.
    """
    work = panel.copy()
    vcol = "value"
    if direction == -1:
        work["_oriented_value"] = -work["value"]
        vcol = "_oriented_value"
    out = {}
    for h in horizons:
        fcol = f"fwd_{h}d"
        if fcol not in work.columns:
            continue
        res = {}
        sub = work.dropna(subset=[vcol, fcol])
        res.update(ev_stats.pooled_ic(sub[vcol], sub[fcol]))
        res.update(ev_stats.daily_ic(work, vcol, fcol, min_names=min_names))
        res.update(ev_stats.quantile_spread(work, vcol, fcol, q=q))
        res["oriented"] = int(direction)
        out[h] = res
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_evaluation.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add evaluation/stats.py evaluation/ic.py tests/test_evaluation.py
git commit -m "feat(evaluation): Tier-1 parametric battery (pooled/daily IC, quantile spread) + IC evaluator"
```

---

### Task 4: Portfolio + event-study evaluators (wrappers over battle-tested primitives)

**Files:**
- Create: `evaluation/portfolio.py`, `evaluation/events.py`
- Test: `tests/test_evaluation.py` (append)

**Interfaces:**
- Consumes: lag-applied signal/event frames (the RUNNER applies `data.apply_lag` once, Task 9 — these evaluators never shift dates).
- Produces (portfolio.py): `evaluate_portfolio(frame, direction=1, quantiles=5, rebalance="M", long_short=True, start=None, end=None, price_table=None, cost_bps=0.0) -> BacktestResult` (wraps `backtest.backtest` with `score="value"`, orienting `value` by direction first); `summarize_portfolio(res) -> dict` with keys `metrics`, `params` (JSON-safe).
- Produces (events.py): `evaluate_events(frame, min_events=5, benchmark="SPY", window=(0, 21), entry_lag=1, price_table=None) -> dict` with keys `labels` (`{label: {n_events, horizons: {h: rowdict}, mean_car_pct: {rel_day: pct}}}`) and `skipped` (`{label: reason-or-count}`). `entry_lag=1` is the engine-enforced next-close entry for events.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_evaluation.py`:

```python
from types import SimpleNamespace

import backtest as bt_module
import event_backtest as eb_module

from evaluation.portfolio import evaluate_portfolio, summarize_portfolio
from evaluation.events import evaluate_events


class TestEvaluatePortfolio:
    def _fake_result(self):
        return SimpleNamespace(metrics={"sharpe": 1.0, "cagr_pct": 5.0},
                               params={"score": "value", "n_symbols": 3},
                               returns=pd.Series([0.001, -0.002]))

    def test_wraps_backtest_with_value_score(self, monkeypatch):
        captured = {}

        def fake_backtest(signal, score="composite", **kw):
            captured["frame"] = signal.copy()
            captured["score"] = score
            captured["kw"] = kw
            return self._fake_result()

        monkeypatch.setattr(bt_module, "backtest", fake_backtest)
        f = _sig_frame(n_dates=30, symbols=("AAA", "BBB"))
        res = evaluate_portfolio(f, quantiles=4, rebalance="W")
        assert captured["score"] == "value"
        assert captured["kw"]["quantiles"] == 4
        assert captured["kw"]["rebalance"] == "W"
        assert res.metrics["sharpe"] == 1.0

    def test_direction_minus_one_flips_values(self, monkeypatch):
        captured = {}

        def fake_backtest(signal, score="composite", **kw):
            captured["values"] = signal["value"].copy()
            return self._fake_result()

        monkeypatch.setattr(bt_module, "backtest", fake_backtest)
        f = _sig_frame(n_dates=30, symbols=("AAA", "BBB"))
        evaluate_portfolio(f, direction=-1)
        assert (captured["values"] == -f["value"]).all()

    def test_summarize_is_json_safe(self):
        res = SimpleNamespace(metrics={"sharpe": float("nan"), "cagr_pct": 5.0},
                              params={"score": "value"})
        s = summarize_portfolio(res)
        assert s["metrics"]["sharpe"] is None       # NaN -> None for JSON
        assert s["metrics"]["cagr_pct"] == 5.0
        json.dumps(s)                               # must not raise


class TestEvaluateEvents:
    def _fake_study(self, n=7):
        horizons = pd.DataFrame({"n": [n, n], "mean_pct": [1.0, 2.0],
                                 "t_stat": [2.5, 3.0]}, index=[5, 21])
        horizons.index.name = "horizon_days"
        return SimpleNamespace(n_events=n, horizons=horizons,
                               mean_car=pd.Series([0.0, 0.01], index=[0, 1]))

    def _events_frame(self, label_counts):
        rows = []
        d0 = pd.Timestamp("2024-01-02")
        for label, cnt in label_counts.items():
            for i in range(cnt):
                rows.append({"symbol": f"S{i}", "date": d0 + pd.Timedelta(days=i),
                             "label": label})
        return pd.DataFrame(rows)

    def test_small_labels_skipped_large_studied(self, monkeypatch):
        calls = []

        def fake_event_study(events, **kw):
            calls.append(kw)
            return self._fake_study(n=len(events))

        monkeypatch.setattr(eb_module, "event_study", fake_event_study)
        f = self._events_frame({"big": 8, "tiny": 2})
        out = evaluate_events(f, min_events=5)
        assert "big" in out["labels"] and out["labels"]["big"]["n_events"] == 8
        assert out["skipped"]["tiny"] == 2
        assert calls[0]["entry_lag"] == 1           # engine-enforced next close

    def test_runtime_error_becomes_skip(self, monkeypatch):
        def fake_event_study(events, **kw):
            raise RuntimeError("No events had enough surrounding price history.")

        monkeypatch.setattr(eb_module, "event_study", fake_event_study)
        f = self._events_frame({"big": 8})
        out = evaluate_events(f, min_events=5)
        assert out["labels"] == {}
        assert "price history" in out["skipped"]["big"]

    def test_output_is_json_safe(self, monkeypatch):
        monkeypatch.setattr(eb_module, "event_study",
                            lambda events, **kw: self._fake_study())
        f = self._events_frame({"big": 8})
        json.dumps(evaluate_events(f, min_events=5))    # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_evaluation.py -v -k "EvaluatePortfolio or EvaluateEvents"`
Expected: collection error `ModuleNotFoundError: No module named 'evaluation.portfolio'`.

- [ ] **Step 3: Implement `evaluation/portfolio.py` and `evaluation/events.py`**

`evaluation/portfolio.py`:

```python
"""
evaluation/portfolio.py -- quantile-portfolio evaluation of a signal frame.
Thin wrapper over backtest.backtest (which already lags weights one day, so
weights set with info at t earn returns from t+1 -- PIT-safe by construction).
"""

import math

import pandas as pd


def evaluate_portfolio(frame: pd.DataFrame, direction: int = 1,
                       quantiles: int = 5, rebalance: str = "M",
                       long_short: bool = True, start=None, end=None,
                       price_table=None, cost_bps: float = 0.0):
    """frame: LAG-APPLIED signal frame (symbol, date, value). Returns BacktestResult."""
    import backtest as bt               # local import: repo test convention
    df = frame[["symbol", "date", "value"]].copy()
    if direction == -1:
        df["value"] = -df["value"]
    return bt.backtest(df, score="value", quantiles=quantiles,
                       rebalance=rebalance, long_short=long_short,
                       start=start, end=end, price_table=price_table,
                       cost_bps=cost_bps)


def _json_safe(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def summarize_portfolio(res) -> dict:
    """JSON-safe {metrics, params} from a BacktestResult."""
    return {"metrics": {k: _json_safe(v) for k, v in res.metrics.items()},
            "params": {k: _json_safe(v) for k, v in res.params.items()}}
```

`evaluation/events.py`:

```python
"""
evaluation/events.py -- event-study evaluation of an EventSet frame, one
study per label. Wraps event_backtest.event_study; entry_lag=1 keeps the
engine's next-close entry rule (day 0 is the first trading close AFTER the
lag-applied event date).
"""

import numpy as np
import pandas as pd


def _json_safe(v):
    if v is None:
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return f if np.isfinite(f) else None
    return v


def evaluate_events(frame: pd.DataFrame, min_events: int = 5,
                    benchmark: str = "SPY", window=(0, 21),
                    entry_lag: int = 1, price_table=None) -> dict:
    """frame: LAG-APPLIED event frame (symbol, date, label[, magnitude])."""
    import event_backtest as eb         # local import: repo test convention
    out = {"labels": {}, "skipped": {}}
    for label, grp in frame.groupby("label"):
        if len(grp) < min_events:
            out["skipped"][str(label)] = int(len(grp))
            continue
        try:
            res = eb.event_study(grp[["symbol", "date"]], window=window,
                                 benchmark=benchmark, entry_lag=entry_lag,
                                 price_table=price_table)
        except RuntimeError as exc:
            out["skipped"][str(label)] = str(exc)
            continue
        out["labels"][str(label)] = {
            "n_events": int(res.n_events),
            "horizons": {str(h): {k: _json_safe(v) for k, v in row.items()}
                         for h, row in res.horizons.iterrows()},
            "mean_car_pct": {str(int(d)): round(100 * float(v), 3)
                             for d, v in res.mean_car.items()
                             if np.isfinite(v)},
        }
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_evaluation.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add evaluation/portfolio.py evaluation/events.py tests/test_evaluation.py
git commit -m "feat(evaluation): portfolio + event-study evaluators wrapping backtest/event_backtest"
```

---

### Task 5: Generic trade-simulation engine

**Files:**
- Create: `evaluation/trades.py`
- Test: `tests/test_evaluation.py` (append)

**Interfaces:**
- Consumes: `TradeRule` from Task 1; `cache` = `{symbol: DataFrame indexed by date, with a 'close' column plus whatever columns the rule reads}`.
- Produces: `TRADE_COLS` (list, same schema as `tv_rating_eval._TRADE_COLS`); `rule_flags(rule, df) -> (long_entry, long_exit, short_entry, short_exit)` bool ndarrays; `simulate_symbol(index, close, long_entry, long_exit, short_entry, short_exit, symbol, notional) -> list[dict]` — the low-level flag-array engine (Task 6's permutation null re-enters HERE with permuted flags); `simulate(rule, cache, notional=None) -> pd.DataFrame` (TRADE_COLS); `trade_summary(trades) -> dict` (`n_trades, n_long, n_short, total_pnl_dollars, win_rate_pct, avg_pnl_pct, median_days_held, n_symbols` or `summary_reason`).
- Engine rules (identical to `tv_rating_eval.simulate_trades`, generalized): signals observed day t execute at close of t+1; one position per symbol (no pyramiding); a position with no qualifying exit before data end is dropped AND blocks later entries; non-finite/non-positive entry or exit prices skip the trade.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_evaluation.py`:

```python
from evaluation.trades import (TRADE_COLS, rule_flags, simulate, simulate_symbol,
                               trade_summary)


def _trade_frame(n=12):
    idx = pd.bdate_range("2024-01-02", periods=n)
    df = pd.DataFrame({"close": 100.0 + np.arange(n),
                       "ent": False, "ex": False}, index=idx)
    return df


def _flag_rule(side="long"):
    return TradeRule(name="flagrule",
                     entries=lambda d: d["ent"], exits=lambda d: d["ex"],
                     side=side)


class TestTradeEngine:
    def test_next_close_execution_and_no_pyramiding(self):
        df = _trade_frame()
        df.loc[df.index[2], "ent"] = True
        df.loc[df.index[4], "ent"] = True      # while in position -> ignored
        df.loc[df.index[5], "ex"] = True
        trades = simulate(_flag_rule(), {"AAA": df})
        assert len(trades) == 1
        t = trades.iloc[0]
        assert t["entry_signal_date"] == df.index[2]
        assert t["entry_date"] == df.index[3]          # next close
        assert t["entry_price"] == pytest.approx(103.0)
        assert t["exit_date"] == df.index[6]           # exit signal 5 -> close 6
        assert t["exit_price"] == pytest.approx(106.0)
        assert t["days_held"] == 3
        assert t["pnl_dollars"] == pytest.approx(10_000 * 3.0 / 103.0, abs=0.01)

    def test_short_side_flips_pnl_sign(self):
        df = _trade_frame()
        df.loc[df.index[2], "ent"] = True
        df.loc[df.index[5], "ex"] = True
        trades = simulate(_flag_rule(side="short"), {"AAA": df})
        assert trades.iloc[0]["side"] == "short"
        assert trades.iloc[0]["pnl_dollars"] == pytest.approx(-10_000 * 3.0 / 103.0, abs=0.01)

    def test_open_position_dropped_and_blocks_reentry(self):
        df = _trade_frame()
        df.loc[df.index[2], "ent"] = True      # never exits
        df.loc[df.index[8], "ent"] = True      # blocked: prior still open
        trades = simulate(_flag_rule(), {"AAA": df})
        assert trades.empty
        assert list(trades.columns) == TRADE_COLS

    def test_flag_length_mismatch_raises(self):
        df = _trade_frame()
        bad = TradeRule(name="bad", entries=lambda d: pd.Series([True]),
                        exits=lambda d: d["ex"])
        with pytest.raises(ValueError, match="flags"):
            rule_flags(bad, df)

    def test_trade_summary(self):
        df = _trade_frame()
        df.loc[df.index[2], "ent"] = True
        df.loc[df.index[5], "ex"] = True
        trades = simulate(_flag_rule(), {"AAA": df})
        s = trade_summary(trades)
        assert s["n_trades"] == 1 and s["n_long"] == 1 and s["n_short"] == 0
        assert s["win_rate_pct"] == 100.0
        assert trade_summary(trades.iloc[0:0])["summary_reason"] == "no realized trades"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_evaluation.py -v -k TradeEngine`
Expected: collection error `ModuleNotFoundError: No module named 'evaluation.trades'`.

- [ ] **Step 3: Implement `evaluation/trades.py`**

```python
"""
evaluation/trades.py -- generic next-close trade-simulation engine.
Generalizes tv_rating_eval.simulate_trades to arbitrary TradeRule callables.

The ENGINE owns execution timing: a signal observed on day t executes at the
close of day t+1 (never trusted to the rule); one position per symbol at a
time; realized trades only -- a position with no qualifying exit before the
data ends is dropped and blocks later entries for that symbol.
"""

import numpy as np
import pandas as pd

TRADE_COLS = ["symbol", "side", "entry_signal_date", "entry_date", "entry_price",
              "exit_signal_date", "exit_date", "exit_price", "days_held",
              "pnl_dollars", "pnl_pct"]


def _bool_array(flags, n: int, who: str) -> np.ndarray:
    a = pd.Series(flags).fillna(False).to_numpy(dtype=bool)
    if len(a) != n:
        raise ValueError(f"{who}: rule returned {len(a)} flags for {n} rows")
    return a


def rule_flags(rule, df: pd.DataFrame):
    """(long_entry, long_exit, short_entry, short_exit) bool arrays for one frame."""
    n = len(df)
    z = np.zeros(n, dtype=bool)
    le, lx, se, sx = z, z, z, z
    if rule.side in ("long", "both"):
        le = _bool_array(rule.entries(df), n, f"{rule.name} entries")
        lx = _bool_array(rule.exits(df), n, f"{rule.name} exits")
    if rule.side == "short":
        se = _bool_array(rule.entries(df), n, f"{rule.name} entries")
        sx = _bool_array(rule.exits(df), n, f"{rule.name} exits")
    elif rule.side == "both":
        se = _bool_array(rule.short_entries(df), n, f"{rule.name} short_entries")
        sx = _bool_array(rule.short_exits(df), n, f"{rule.name} short_exits")
    return le, lx, se, sx


def simulate_symbol(index, close, long_entry, long_exit, short_entry, short_exit,
                    symbol: str, notional: float) -> "list[dict]":
    """Low-level engine on flag arrays (Tier-2 permutation re-enters here)."""
    close = pd.Series(np.asarray(close, dtype=float), index=index)
    n = len(close)
    rows = []
    entry_positions = sorted(
        [(i, "long") for i in np.flatnonzero(long_entry)] +
        [(i, "short") for i in np.flatnonzero(short_entry)]
    )
    next_free = 0
    for sig_i, side in entry_positions:
        if sig_i < next_free:
            continue                        # already in a position
        entry_i = sig_i + 1                 # ENGINE: next-close execution
        if entry_i >= n:
            continue
        entry_price = close.iloc[entry_i]
        if not np.isfinite(entry_price) or entry_price <= 0:
            continue
        exit_cond = long_exit if side == "long" else short_exit
        exit_sig_i = None
        for j in range(entry_i + 1, n):
            if exit_cond[j]:
                exit_sig_i = j
                break
        if exit_sig_i is None:
            next_free = n                   # still open: blocks further entries
            continue
        exit_i = exit_sig_i + 1
        if exit_i >= n:
            next_free = n
            continue
        exit_price = close.iloc[exit_i]
        if not np.isfinite(exit_price) or exit_price <= 0:
            next_free = exit_i + 1
            continue
        pct = (exit_price / entry_price - 1.0) if side == "long" else \
              (1.0 - exit_price / entry_price)
        rows.append({
            "symbol": symbol, "side": side,
            "entry_signal_date": index[sig_i], "entry_date": index[entry_i],
            "entry_price": float(entry_price),
            "exit_signal_date": index[exit_sig_i], "exit_date": index[exit_i],
            "exit_price": float(exit_price), "days_held": int(exit_i - entry_i),
            "pnl_dollars": round(notional * pct, 2),
            "pnl_pct": round(100 * pct, 3),
        })
        next_free = exit_i + 1
    return rows


def simulate(rule, cache: dict, notional: "float | None" = None) -> pd.DataFrame:
    """One realized-trade row per closed position across all cache symbols."""
    notional = rule.notional if notional is None else notional
    rows = []
    for sym, df in cache.items():
        if df.empty or "close" not in df.columns:
            continue
        le, lx, se, sx = rule_flags(rule, df)
        rows.extend(simulate_symbol(df.index, df["close"], le, lx, se, sx,
                                    sym, notional))
    return pd.DataFrame(rows, columns=TRADE_COLS)


def trade_summary(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"n_trades": 0, "summary_reason": "no realized trades"}
    wins = trades["pnl_dollars"] > 0
    return {"n_trades": int(len(trades)),
            "n_long": int((trades["side"] == "long").sum()),
            "n_short": int((trades["side"] == "short").sum()),
            "total_pnl_dollars": round(float(trades["pnl_dollars"].sum()), 2),
            "win_rate_pct": round(100 * float(wins.mean()), 1),
            "avg_pnl_pct": round(float(trades["pnl_pct"].mean()), 3),
            "median_days_held": float(trades["days_held"].median()),
            "n_symbols": int(trades["symbol"].nunique())}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_evaluation.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add evaluation/trades.py tests/test_evaluation.py
git commit -m "feat(evaluation): generic next-close trade-simulation engine"
```

---

### Task 6: Tier-2 resampling battery (bootstrap, permutation null, BH-FDR)

**Files:**
- Modify: `evaluation/stats.py` (append a Tier-2 section)
- Test: `tests/test_evaluation.py` (append)

**Interfaces:**
- Consumes: panel layout (Task 2); `trades.rule_flags` / `trades.simulate_symbol` / `trades.simulate` / `TRADE_COLS` (Task 5).
- Produces: `block_bootstrap_spread(panel, value_col, fwd_col, q=0.2, n_boot=1000, seed=0, min_days=20) -> dict` (`spread_boot_mean_pct, spread_ci_lo_pct, spread_ci_hi_pct, n_boot, boot_days` or None values + `boot_reason`); `bootstrap_sharpe(returns, block_len=21, n_boot=1000, seed=0) -> dict` (`sharpe, sharpe_ci_lo, sharpe_ci_hi, n_boot` or None values + `sharpe_reason`); `permutation_trades(rule, cache, n_perm=200, seed=0) -> dict` (`obs_pnl_dollars, obs_win_rate_pct, pnl_p, win_rate_p, n_perm` or None values + `perm_reason`); `bh_fdr(records, alpha=0.10, p_key="p") -> pd.DataFrame` (input rows + `p_adj`, `reject`; None/NaN p excluded from m, never rejected).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_evaluation.py`:

```python
class TestTier2:
    def test_planted_spread_ci_excludes_zero(self):
        p = _planted_panel()
        r = ev_stats.block_bootstrap_spread(p, "value", "fwd_1d", n_boot=300, seed=0)
        assert r["spread_ci_lo_pct"] > 0

    def test_noise_spread_ci_straddles_zero(self):
        p = _noise_panel()
        r = ev_stats.block_bootstrap_spread(p, "value", "fwd_1d", n_boot=300, seed=0)
        assert r["spread_ci_lo_pct"] < 0 < r["spread_ci_hi_pct"]

    def test_bootstrap_spread_too_few_days_reason(self):
        p = _planted_panel(n_dates=10)
        r = ev_stats.block_bootstrap_spread(p, "value", "fwd_1d")
        assert r["spread_ci_lo_pct"] is None and "usable days" in r["boot_reason"]

    def test_bootstrap_sharpe_ci_and_guards(self):
        rng = np.random.default_rng(0)
        good = pd.Series(rng.normal(0.001, 0.01, size=500))
        r = ev_stats.bootstrap_sharpe(good, n_boot=300, seed=0)
        assert r["sharpe_ci_lo"] < r["sharpe"] < r["sharpe_ci_hi"]
        flat = pd.Series(np.zeros(500))
        assert "sharpe_reason" in ev_stats.bootstrap_sharpe(flat)
        short = pd.Series(rng.normal(size=10))
        assert "sharpe_reason" in ev_stats.bootstrap_sharpe(short)

    def test_permutation_null_rule_not_significant(self):
        # a rule whose entries are RANDOM days on a random walk must not
        # produce a tiny p (null true -> p ~ uniform; loose bound, fixed seeds)
        rng = np.random.default_rng(7)
        idx = pd.bdate_range("2020-01-02", periods=400)
        cache = {}
        for sym in ("AAA", "BBB", "CCC"):
            close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, size=400)))
            ent = np.zeros(400, dtype=bool)
            ent[rng.choice(400, size=12, replace=False)] = True
            ex = np.zeros(400, dtype=bool)
            ex[rng.choice(400, size=40, replace=False)] = True
            cache[sym] = pd.DataFrame({"close": close, "ent": ent, "ex": ex},
                                      index=idx)
        rule = TradeRule(name="nullrule", entries=lambda d: d["ent"],
                         exits=lambda d: d["ex"])
        r = ev_stats.permutation_trades(rule, cache, n_perm=99, seed=0)
        assert r["pnl_p"] > 0.01 and r["pnl_p"] <= 1.0
        assert r["n_perm"] > 20

    def test_permutation_no_trades_reason(self):
        idx = pd.bdate_range("2024-01-02", periods=30)
        cache = {"AAA": pd.DataFrame({"close": np.full(30, 100.0),
                                      "ent": False, "ex": False}, index=idx)}
        rule = TradeRule(name="never", entries=lambda d: d["ent"],
                         exits=lambda d: d["ex"])
        r = ev_stats.permutation_trades(rule, cache, n_perm=20, seed=0)
        assert r["pnl_p"] is None and "no realized trades" in r["perm_reason"]

    def test_bh_fdr_known_vector(self):
        recs = [{"id": i, "p": p} for i, p in
                enumerate([0.001, 0.008, 0.039, 0.041, 0.20, None])]
        out = ev_stats.bh_fdr(recs, alpha=0.05)
        # m=5 valid; BH thresholds 0.01,0.02,0.03,0.04,0.05 -> reject first 4
        assert out.loc[out["id"] == 0, "reject"].item() is np.True_ or \
               bool(out.loc[out["id"] == 0, "reject"].item())
        assert bool(out.loc[out["id"] == 3, "reject"].item())
        assert not bool(out.loc[out["id"] == 4, "reject"].item())
        assert not bool(out.loc[out["id"] == 5, "reject"].item())   # None p
        assert pd.isna(out.loc[out["id"] == 5, "p_adj"].item())

    def test_noise_grid_survives_nothing(self):
        # spec falsification: pure-noise stats must NOT survive FDR
        recs = []
        for seed in range(12):
            p = _noise_panel(seed=seed + 10)
            d = ev_stats.daily_ic(p, "value", "fwd_1d")
            if d["ic_t_stat"] is not None:
                recs.append({"id": seed, "p": ev_stats.t_to_p(d["ic_t_stat"])})
        out = ev_stats.bh_fdr(recs, alpha=0.10)
        assert int(out["reject"].sum()) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_evaluation.py -v -k Tier2`
Expected: FAIL with `AttributeError: ... has no attribute 'block_bootstrap_spread'`.

- [ ] **Step 3: Append the Tier-2 section to `evaluation/stats.py`**

```python
# --------------------------------------------------------------- Tier 2


def block_bootstrap_spread(panel: pd.DataFrame, value_col: str, fwd_col: str,
                           q: float = 0.2, n_boot: int = 1000, seed: int = 0,
                           min_days: int = 20) -> dict:
    """
    Bootstrap whole DATES (cross-sections) with replacement -> percentile CI
    on the top-q minus bottom-q spread. Resampling dates, not rows, preserves
    cross-sectional correlation (the block that matters for daily panels).
    """
    sub = panel.dropna(subset=[value_col, fwd_col])
    per_day = []
    for _, day in sub.groupby("date"):
        if len(day) < 2 or day[value_col].nunique() < 2:
            continue
        k = max(1, int(round(len(day) * q)))
        r = day.sort_values(value_col)
        per_day.append((float(r[fwd_col].tail(k).mean()),
                        float(r[fwd_col].head(k).mean())))
    if len(per_day) < min_days:
        return {"spread_ci_lo_pct": None, "spread_ci_hi_pct": None,
                "boot_reason": f"only {len(per_day)} usable days (< {min_days})"}
    arr = np.asarray(per_day)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(arr), size=(n_boot, len(arr)))
    boot = arr[idx].mean(axis=1)                       # (n_boot, 2)
    spreads = boot[:, 0] - boot[:, 1]
    lo, hi = np.percentile(spreads, [2.5, 97.5])
    return {"spread_boot_mean_pct": round(100 * float(spreads.mean()), 3),
            "spread_ci_lo_pct": round(100 * float(lo), 3),
            "spread_ci_hi_pct": round(100 * float(hi), 3),
            "n_boot": int(n_boot), "boot_days": int(len(arr))}


def bootstrap_sharpe(returns, block_len: int = 21, n_boot: int = 1000,
                     seed: int = 0) -> dict:
    """Moving-block bootstrap CI on the annualized Sharpe of daily returns."""
    r = pd.Series(returns).dropna().to_numpy(dtype=float)
    n = len(r)
    if n < 3 * block_len:
        return {"sharpe": None, "sharpe_ci_lo": None, "sharpe_ci_hi": None,
                "sharpe_reason": f"only {n} days (< {3 * block_len})"}
    sd = r.std(ddof=0)
    if not sd > 0:
        return {"sharpe": None, "sharpe_ci_lo": None, "sharpe_ci_hi": None,
                "sharpe_reason": "zero return variance"}
    ann = math.sqrt(252.0)
    obs = float(r.mean() / sd * ann)
    rng = np.random.default_rng(seed)
    n_blocks = int(math.ceil(n / block_len))
    sharpes = np.empty(n_boot)
    for i in range(n_boot):
        starts = rng.integers(0, n - block_len + 1, size=n_blocks)
        sample = np.concatenate([r[s:s + block_len] for s in starts])[:n]
        ssd = sample.std(ddof=0)
        sharpes[i] = sample.mean() / ssd * ann if ssd > 0 else np.nan
    sharpes = sharpes[np.isfinite(sharpes)]
    if len(sharpes) < n_boot // 2:
        return {"sharpe": round(obs, 2), "sharpe_ci_lo": None,
                "sharpe_ci_hi": None,
                "sharpe_reason": "bootstrap degenerate (zero-variance samples)"}
    lo, hi = np.percentile(sharpes, [2.5, 97.5])
    return {"sharpe": round(obs, 2), "sharpe_ci_lo": round(float(lo), 2),
            "sharpe_ci_hi": round(float(hi), 2), "n_boot": int(len(sharpes))}


def permutation_trades(rule, cache: dict, n_perm: int = 200,
                       seed: int = 0) -> dict:
    """
    Permutation null for a trade system: within each symbol, relocate the
    same NUMBER of entry signals to uniformly random days (exit rule kept
    as-is), re-simulate through the same engine, and compare total P&L and
    win rate. One-sided empirical p-values with the +1 correction.
    """
    from evaluation import trades as tr        # local import (no cycles)
    obs = tr.simulate(rule, cache)
    if obs.empty:
        return {"pnl_p": None, "win_rate_p": None,
                "perm_reason": "no realized trades"}
    obs_pnl = float(obs["pnl_dollars"].sum())
    obs_wr = float((obs["pnl_dollars"] > 0).mean())
    rng = np.random.default_rng(seed)
    flags = {}
    for sym, df in cache.items():
        if df.empty or "close" not in df.columns:
            continue
        flags[sym] = (df.index, df["close"].to_numpy(dtype=float),
                      tr.rule_flags(rule, df))
    pnl_ge = wr_ge = n_done = 0
    for _ in range(n_perm):
        rows = []
        for sym, (index, close, (le, lx, se, sx)) in flags.items():
            n = len(index)
            ple = np.zeros(n, dtype=bool)
            k = int(le.sum())
            if k:
                ple[rng.choice(n, size=k, replace=False)] = True
            pse = np.zeros(n, dtype=bool)
            k = int(se.sum())
            if k:
                pse[rng.choice(n, size=k, replace=False)] = True
            rows.extend(tr.simulate_symbol(index, close, ple, lx, pse, sx,
                                           sym, rule.notional))
        perm = pd.DataFrame(rows, columns=tr.TRADE_COLS)
        if perm.empty:
            continue
        n_done += 1
        if float(perm["pnl_dollars"].sum()) >= obs_pnl:
            pnl_ge += 1
        if float((perm["pnl_dollars"] > 0).mean()) >= obs_wr:
            wr_ge += 1
    if n_done < max(20, n_perm // 4):
        return {"pnl_p": None, "win_rate_p": None,
                "perm_reason": f"only {n_done} permutations produced trades"}
    return {"obs_pnl_dollars": round(obs_pnl, 2),
            "obs_win_rate_pct": round(100 * obs_wr, 1),
            "pnl_p": round((1 + pnl_ge) / (n_done + 1), 4),
            "win_rate_p": round((1 + wr_ge) / (n_done + 1), 4),
            "n_perm": int(n_done)}


def bh_fdr(records, alpha: float = 0.10, p_key: str = "p") -> pd.DataFrame:
    """
    Benjamini-Hochberg step-up across a run's full statistics grid.
    records: list of dicts each holding p_key (may be None) plus id fields.
    Returns the rows as a DataFrame with p_adj and reject added; None/NaN
    p-values are excluded from m and never rejected.
    """
    df = pd.DataFrame(records).copy()
    if df.empty:
        df["p_adj"] = pd.Series(dtype=float)
        df["reject"] = pd.Series(dtype=bool)
        return df
    p = pd.to_numeric(df[p_key], errors="coerce")
    df["p_adj"] = np.nan
    df["reject"] = False
    m = int(p.notna().sum())
    if m == 0:
        return df
    ps = p[p.notna()].sort_values()
    ranks = np.arange(1, m + 1)
    adj = ps.to_numpy() * m / ranks
    adj = np.minimum.accumulate(adj[::-1])[::-1]       # enforce monotonicity
    df.loc[ps.index, "p_adj"] = np.clip(adj, 0, 1)
    passed = ps.to_numpy() <= alpha * ranks / m
    k = int(np.max(np.nonzero(passed)[0]) + 1) if passed.any() else 0
    if k:
        df.loc[ps.index[:k], "reject"] = True
    return df
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_evaluation.py -v`
Expected: all tests PASS. (The permutation test simulates ~100 permutations x 3 symbols x 400 days; if it takes > ~30s, reduce n_perm in the TEST, not the default.)

- [ ] **Step 5: Commit**

```bash
git add evaluation/stats.py tests/test_evaluation.py
git commit -m "feat(evaluation): Tier-2 resampling battery (date bootstrap, trade permutation null, BH-FDR)"
```

---

### Task 7: Tier-3 research-grade battery (walk-forward, regimes, deflated Sharpe, registry percentile)

**Files:**
- Modify: `evaluation/stats.py` (append a Tier-3 section)
- Test: `tests/test_evaluation.py` (append)

**Interfaces:**
- Consumes: Tier-1 functions (`daily_ic`, `quantile_spread`) from Task 3.
- Produces: `walk_forward(panel, value_col, fwd_col, n_folds=4, min_train_days=126, min_names=5) -> dict` (`oos` = Tier-1 dict over all post-train dates, `folds` = list of per-fold dicts, `n_train_days`; or `wf_reason`); `regime_conditioning(panel, value_col, fwd_col, bench_close, min_names=5, sma_window=200, vol_window=21) -> dict` (keys `bull, bear, high_vol, low_vol` each holding `n_days` + Tier-1 stats; or `regime_reason`); `deflated_sharpe(sharpe_ann, n_days, trial_sharpes_ann, skew=0.0, kurt=3.0) -> dict` (`dsr_prob, sr0_ann, n_trials` or None + `dsr_reason` — Bailey/Lopez-de-Prado, with the registry's Sharpe population as the REAL number-of-trials denominator); `registry_percentile(value, population) -> dict` (`percentile, n_population` or None + `pct_reason`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_evaluation.py`:

```python
class TestTier3:
    def test_walk_forward_planted_oos_holds(self):
        p = _planted_panel(n_dates=400)
        r = ev_stats.walk_forward(p, "value", "fwd_1d", n_folds=4,
                                  min_train_days=126)
        assert len(r["folds"]) == 4
        assert r["oos"]["mean_daily_ic"] > 0.9
        assert all(f["mean_daily_ic"] > 0.9 for f in r["folds"])

    def test_walk_forward_too_short_reason(self):
        p = _planted_panel(n_dates=100)
        r = ev_stats.walk_forward(p, "value", "fwd_1d")
        assert r["oos"] is None and "dates" in r["wf_reason"]

    def test_regime_conditioning_partitions_days(self):
        # benchmark: 300 rising days (ends above SMA) then 200 falling days
        idx = pd.bdate_range("2022-01-03", periods=500)
        px = np.concatenate([100 * (1.004 ** np.arange(300)),
                             100 * (1.004 ** 299) * (0.996 ** np.arange(1, 201))])
        bench = pd.Series(px, index=idx)
        rng = np.random.default_rng(0)
        rows = [{"symbol": f"S{k}", "date": d, "value": float(rng.normal()),
                 "fwd_1d": float(rng.normal(scale=0.01))}
                for d in idx for k in range(6)]
        panel = pd.DataFrame(rows)
        r = ev_stats.regime_conditioning(panel, "value", "fwd_1d", bench)
        assert set(r) == {"bull", "bear", "high_vol", "low_vol"}
        assert r["bull"]["n_days"] > 0 and r["bear"]["n_days"] > 0
        # bull+bear cover exactly the SMA-defined dates
        assert r["bull"]["n_days"] + r["bear"]["n_days"] == 500 - 199

    def test_regime_short_benchmark_reason(self):
        bench = pd.Series(np.arange(50, dtype=float),
                          index=pd.bdate_range("2024-01-02", periods=50))
        r = ev_stats.regime_conditioning(_planted_panel(), "value", "fwd_1d", bench)
        assert "regime_reason" in r

    def test_deflated_sharpe_monotone_in_trial_dispersion(self):
        tight = ev_stats.deflated_sharpe(2.0, 500, [0.2, 0.3, 0.1, -0.1])
        wide = ev_stats.deflated_sharpe(2.0, 500, [3.0, -3.0, 2.5, -2.5])
        assert tight["dsr_prob"] > 0.9
        assert wide["dsr_prob"] < tight["dsr_prob"]

    def test_deflated_sharpe_small_population_reason(self):
        r = ev_stats.deflated_sharpe(2.0, 500, [1.0])
        assert r["dsr_prob"] is None and "population too small" in r["dsr_reason"]

    def test_registry_percentile(self):
        r = ev_stats.registry_percentile(0.5, [0.1, 0.2, 0.6, 0.9])
        assert r["percentile"] == 50.0 and r["n_population"] == 4
        assert "pct_reason" in ev_stats.registry_percentile(0.5, [0.1])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_evaluation.py -v -k Tier3`
Expected: FAIL with `AttributeError: ... has no attribute 'walk_forward'`.

- [ ] **Step 3: Append the Tier-3 section to `evaluation/stats.py`**

```python
# --------------------------------------------------------------- Tier 3


def walk_forward(panel: pd.DataFrame, value_col: str, fwd_col: str,
                 n_folds: int = 4, min_train_days: int = 126,
                 min_names: int = 5) -> dict:
    """
    Expanding-window walk-forward: the first min_train_days distinct dates
    are the initial in-sample block; the remaining dates split into n_folds
    sequential OOS folds. For unfitted signals this measures out-of-sample
    STABILITY; headline numbers are the OOS aggregate (spec rule).
    """
    sub = panel.dropna(subset=[value_col, fwd_col])
    dates = np.array(sorted(sub["date"].unique()))
    need = min_train_days + n_folds * 21
    if len(dates) < need:
        return {"oos": None, "folds": [],
                "wf_reason": f"only {len(dates)} dates (< {need})"}
    oos_dates = dates[min_train_days:]
    folds = []
    for i, chunk in enumerate(np.array_split(oos_dates, n_folds)):
        fsub = sub[sub["date"].isin(chunk)]
        d = daily_ic(fsub, value_col, fwd_col, min_names=min_names)
        folds.append({"fold": i + 1,
                      "date_range": f"{pd.Timestamp(chunk[0]).date()}"
                                    f"..{pd.Timestamp(chunk[-1]).date()}",
                      "mean_daily_ic": d.get("mean_daily_ic"),
                      "ic_t_stat": d.get("ic_t_stat"),
                      "ic_days": d.get("ic_days")})
    osub = sub[sub["date"].isin(oos_dates)]
    oos = daily_ic(osub, value_col, fwd_col, min_names=min_names)
    oos.update(quantile_spread(osub, value_col, fwd_col))
    return {"oos": oos, "folds": folds, "n_train_days": int(min_train_days)}


def regime_conditioning(panel: pd.DataFrame, value_col: str, fwd_col: str,
                        bench_close: pd.Series, min_names: int = 5,
                        sma_window: int = 200, vol_window: int = 21) -> dict:
    """
    Per-regime Tier-1 stats. Bull/bear: benchmark close >= its sma_window SMA.
    High/low vol: benchmark vol_window realized vol vs its own median.
    Regimes are assigned by SIGNAL date (info available at signal time).
    """
    b = pd.Series(bench_close).dropna()
    if len(b) < sma_window + vol_window:
        return {"regime_reason": f"benchmark history too short ({len(b)} days)"}
    sma = b.rolling(sma_window).mean()
    vol = b.pct_change().rolling(vol_window).std() * math.sqrt(252.0)
    med = vol.median()

    sub = panel.dropna(subset=[value_col, fwd_col]).copy()
    dates = pd.DatetimeIndex(pd.to_datetime(sub["date"]))
    sma_at = sma.reindex(dates).to_numpy()
    close_at = b.reindex(dates).to_numpy()
    vol_at = vol.reindex(dates).to_numpy()
    sma_ok = np.isfinite(sma_at) & np.isfinite(close_at)
    vol_ok = np.isfinite(vol_at)

    masks = {"bull": sma_ok & (close_at >= sma_at),
             "bear": sma_ok & (close_at < sma_at),
             "high_vol": vol_ok & (vol_at > med),
             "low_vol": vol_ok & (vol_at <= med)}
    out = {}
    for name, mask in masks.items():
        fsub = sub[mask]
        res = {"n_days": int(fsub["date"].nunique())}
        res.update(daily_ic(fsub, value_col, fwd_col, min_names=min_names))
        res.update(quantile_spread(fsub, value_col, fwd_col))
        out[name] = res
    return out


def deflated_sharpe(sharpe_ann, n_days: int, trial_sharpes_ann,
                    skew: float = 0.0, kurt: float = 3.0) -> dict:
    """
    Bailey & Lopez de Prado deflated Sharpe ratio. trial_sharpes_ann is the
    registry's population of previously recorded annualized Sharpes -- a
    REAL 'number of things tried' denominator instead of a guess. Returns
    dsr_prob ~ P(true SR > expected max of N null trials).
    """
    if sharpe_ann is None or not np.isfinite(sharpe_ann):
        return {"dsr_prob": None, "dsr_reason": "no observed Sharpe"}
    trials = np.asarray([s for s in trial_sharpes_ann
                         if s is not None and np.isfinite(s)], dtype=float)
    N = len(trials)
    if N < 2:
        return {"dsr_prob": None,
                "dsr_reason": f"registry population too small (n={N} < 2)"}
    if n_days < 30:
        return {"dsr_prob": None, "dsr_reason": f"only {n_days} days (< 30)"}
    daily = 1.0 / math.sqrt(252.0)
    sr = float(sharpe_ann) * daily
    var_tr = float(np.var(trials * daily, ddof=1))
    if not var_tr > 0:
        return {"dsr_prob": None,
                "dsr_reason": "zero variance across trial Sharpes"}
    gamma = 0.5772156649015329
    z = sps.norm.ppf
    sr0 = math.sqrt(var_tr) * ((1 - gamma) * z(1 - 1.0 / N)
                               + gamma * z(1 - 1.0 / (N * math.e)))
    denom_sq = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if not denom_sq > 0:
        return {"dsr_prob": None, "dsr_reason": "invalid skew/kurtosis adjustment"}
    stat = (sr - sr0) * math.sqrt(n_days - 1) / math.sqrt(denom_sq)
    return {"dsr_prob": round(float(sps.norm.cdf(stat)), 4),
            "sr0_ann": round(float(sr0 / daily), 3), "n_trials": int(N)}


def registry_percentile(value, population) -> dict:
    """Where does `value` sit in the registry's population of the same stat?"""
    pop = np.asarray([v for v in population
                      if v is not None and np.isfinite(v)], dtype=float)
    if len(pop) < 2:
        return {"percentile": None,
                "pct_reason": f"population too small (n={len(pop)})"}
    return {"percentile": round(100.0 * float((pop <= value).mean()), 1),
            "n_population": int(len(pop))}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_evaluation.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add evaluation/stats.py tests/test_evaluation.py
git commit -m "feat(evaluation): Tier-3 battery (walk-forward, regime conditioning, deflated Sharpe, registry percentile)"
```

---

### Task 8: Append-only results registry

**Files:**
- Create: `evaluation/registry.py`
- Modify: `.gitignore` (add `storage/eval_registry/`)
- Test: `tests/test_evaluation.py` (append)

**Interfaces:**
- Consumes: nothing from earlier tasks (pure storage layer; pandas/parquet only).
- Produces: `REG_PATH = "storage/eval_registry/results.parquet"` (repo-root-relative); `COLUMNS = ["run_id", "input_name", "input_type", "evaluation", "horizon", "statistic", "value", "n", "universe_hash", "date_range", "created_at"]`; `universe_hash(symbols) -> str` (12-hex, order/case-insensitive); `new_run_id() -> str`; `append(rows: pd.DataFrame, path=REG_PATH) -> int` (atomic temp+`os.replace`, returns rows appended); `load(path=REG_PATH) -> pd.DataFrame` (empty frame with COLUMNS when file absent); `baselines(input_name=None, path=REG_PATH) -> pd.DataFrame` (latest row per `(input_name, evaluation, horizon, statistic)`); `compare(rows, path=REG_PATH, tol=0.005, allow_universe_mismatch=False) -> pd.DataFrame` (adds `baseline`, `diff`, `within_tol`; raises `ValueError` on universe_hash mismatch unless allowed); `population(statistic, path=REG_PATH) -> list[float]` (latest value per input_name — the trial population for `deflated_sharpe` / `registry_percentile`); `summary(path=REG_PATH) -> str` (ASCII one-screen summary; `python -m evaluation.registry` prints it — the spec's CLI summary export). Convention: `horizon = -1` for statistics without a horizon (portfolio, trades).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_evaluation.py`:

```python
from evaluation import registry as ev_registry


def _reg_rows(run_id="r1", name="sig_a", value=0.02, created="2026-07-19T10:00:00",
              uhash="abc123", statistic="pooled_ic", horizon=1):
    return pd.DataFrame([{
        "run_id": run_id, "input_name": name, "input_type": "signal",
        "evaluation": "ic", "horizon": horizon, "statistic": statistic,
        "value": value, "n": 100, "universe_hash": uhash,
        "date_range": "2024-01-02..2025-01-31", "created_at": created,
    }])


class TestRegistry:
    def test_roundtrip_and_missing_file(self, tmp_path):
        path = str(tmp_path / "reg" / "results.parquet")
        assert ev_registry.load(path).empty
        assert list(ev_registry.load(path).columns) == ev_registry.COLUMNS
        n = ev_registry.append(_reg_rows(), path)
        assert n == 1
        reg = ev_registry.load(path)
        assert len(reg) == 1
        assert list(reg.columns) == ev_registry.COLUMNS
        assert not os.path.exists(path + ".tmp")

    def test_append_is_additive(self, tmp_path):
        path = str(tmp_path / "results.parquet")
        ev_registry.append(_reg_rows(run_id="r1"), path)
        ev_registry.append(_reg_rows(run_id="r2"), path)
        assert len(ev_registry.load(path)) == 2

    def test_append_rejects_missing_columns(self, tmp_path):
        path = str(tmp_path / "results.parquet")
        bad = _reg_rows().drop(columns=["statistic"])
        with pytest.raises(ValueError, match="statistic"):
            ev_registry.append(bad, path)

    def test_baselines_latest_wins(self, tmp_path):
        path = str(tmp_path / "results.parquet")
        ev_registry.append(_reg_rows(run_id="r1", value=0.01,
                                     created="2026-07-18T10:00:00"), path)
        ev_registry.append(_reg_rows(run_id="r2", value=0.03,
                                     created="2026-07-19T10:00:00"), path)
        base = ev_registry.baselines(path=path)
        assert len(base) == 1
        assert base.iloc[0]["value"] == pytest.approx(0.03)
        assert base.iloc[0]["run_id"] == "r2"

    def test_compare_within_tol_and_universe_guard(self, tmp_path):
        path = str(tmp_path / "results.parquet")
        ev_registry.append(_reg_rows(run_id="r1", value=0.010, uhash="aaa"), path)
        fresh_ok = _reg_rows(run_id="r2", value=0.012, uhash="aaa")
        cmp = ev_registry.compare(fresh_ok, path=path, tol=0.005)
        assert bool(cmp.iloc[0]["within_tol"]) is True
        assert cmp.iloc[0]["baseline"] == pytest.approx(0.010)
        fresh_far = _reg_rows(run_id="r3", value=0.030, uhash="aaa")
        cmp2 = ev_registry.compare(fresh_far, path=path, tol=0.005)
        assert bool(cmp2.iloc[0]["within_tol"]) is False
        fresh_mismatch = _reg_rows(run_id="r4", value=0.011, uhash="bbb")
        with pytest.raises(ValueError, match="universe"):
            ev_registry.compare(fresh_mismatch, path=path)
        cmp3 = ev_registry.compare(fresh_mismatch, path=path,
                                   allow_universe_mismatch=True)
        assert len(cmp3) == 1

    def test_population_latest_per_input(self, tmp_path):
        path = str(tmp_path / "results.parquet")
        ev_registry.append(_reg_rows(run_id="r1", name="sig_a", value=1.0,
                                     statistic="sharpe", horizon=-1,
                                     created="2026-07-18T10:00:00"), path)
        ev_registry.append(_reg_rows(run_id="r2", name="sig_a", value=1.5,
                                     statistic="sharpe", horizon=-1,
                                     created="2026-07-19T10:00:00"), path)
        ev_registry.append(_reg_rows(run_id="r3", name="sig_b", value=-0.2,
                                     statistic="sharpe", horizon=-1), path)
        pop = ev_registry.population("sharpe", path=path)
        assert sorted(pop) == [pytest.approx(-0.2), pytest.approx(1.5)]
        assert ev_registry.population("nope", path=path) == []

    def test_universe_hash_order_and_case_invariant(self):
        h1 = ev_registry.universe_hash(["AAPL", "MSFT", "SPY"])
        h2 = ev_registry.universe_hash(["spy", "msft", "aapl"])
        h3 = ev_registry.universe_hash(["AAPL", "MSFT"])
        assert h1 == h2
        assert h1 != h3
        assert len(h1) == 12

    def test_summary_is_ascii(self, tmp_path):
        path = str(tmp_path / "results.parquet")
        assert "empty" in ev_registry.summary(path)
        ev_registry.append(_reg_rows(), path)
        s = ev_registry.summary(path)
        assert s.isascii()
        assert "sig_a" in s and "1 rows" in s
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_evaluation.py -k Registry -v`
Expected: collection error `ModuleNotFoundError: No module named 'evaluation.registry'`.

- [ ] **Step 3: Implement `evaluation/registry.py`**

```python
"""
evaluation/registry.py -- append-only parquet store of evaluation results.

One row per (run, evaluation, horizon, statistic). horizon=-1 means "no
horizon" (portfolio- or trade-level statistics). The registry is the memory
of every signal ever evaluated: baselines() answers "what did this signal
score last time", population() answers "how many trials has this research
program run" (the honest N for deflated Sharpe).

NOTE: no `year`/`month` columns ever (Hive partition shadowing) -- the date
range lives in the `date_range` string.
"""

import hashlib
import os
import uuid

import pandas as pd

REG_PATH = os.path.join("storage", "eval_registry", "results.parquet")

COLUMNS = [
    "run_id", "input_name", "input_type", "evaluation", "horizon",
    "statistic", "value", "n", "universe_hash", "date_range", "created_at",
]

_KEY = ["input_name", "evaluation", "horizon", "statistic"]


def universe_hash(symbols) -> str:
    """Order- and case-insensitive 12-hex digest of a symbol universe."""
    joined = ",".join(sorted({str(s).upper() for s in symbols}))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _normalize(rows: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in COLUMNS if c not in rows.columns]
    if missing:
        raise ValueError(f"registry rows missing columns: {missing}")
    out = rows[COLUMNS].copy()
    out["horizon"] = pd.to_numeric(out["horizon"]).fillna(-1).astype("int64")
    out["value"] = pd.to_numeric(out["value"], errors="coerce").astype(float)
    out["n"] = pd.to_numeric(out["n"]).fillna(0).astype("int64")
    for col in ("run_id", "input_name", "input_type", "evaluation",
                "statistic", "universe_hash", "date_range", "created_at"):
        out[col] = out[col].astype(str)
    return out


def load(path: str = REG_PATH) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_parquet(path)


def append(rows: pd.DataFrame, path: str = REG_PATH) -> int:
    """Append rows atomically (write temp, os.replace). Returns rows added."""
    rows = _normalize(rows)
    existing = load(path)
    combined = (pd.concat([existing, rows], ignore_index=True)
                if not existing.empty else rows)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    combined.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    return len(rows)


def baselines(input_name=None, path: str = REG_PATH) -> pd.DataFrame:
    """Latest row per (input_name, evaluation, horizon, statistic)."""
    reg = load(path)
    if reg.empty:
        return reg
    if input_name is not None:
        reg = reg[reg["input_name"] == input_name]
    if reg.empty:
        return reg
    return (reg.sort_values("created_at")
               .groupby(_KEY, as_index=False)
               .tail(1)
               .reset_index(drop=True))


def compare(rows: pd.DataFrame, path: str = REG_PATH, tol: float = 0.005,
            allow_universe_mismatch: bool = False) -> pd.DataFrame:
    """
    Compare fresh rows against stored baselines on the same key. Refuses to
    compare across different universes (a coverage difference masquerades as
    a skill difference) unless allow_universe_mismatch=True.
    """
    rows = _normalize(rows)
    base = baselines(path=path)
    if base.empty:
        out = rows.copy()
        out["baseline"] = float("nan")
        out["diff"] = float("nan")
        out["within_tol"] = False
        return out
    b = base[_KEY + ["value", "universe_hash"]].rename(
        columns={"value": "baseline", "universe_hash": "baseline_universe_hash"})
    out = rows.merge(b, on=_KEY, how="left")
    matched = out["baseline_universe_hash"].notna()
    mismatch = matched & (out["universe_hash"] != out["baseline_universe_hash"])
    if mismatch.any() and not allow_universe_mismatch:
        bad = out.loc[mismatch, _KEY].to_dict("records")[:3]
        raise ValueError(
            f"universe_hash mismatch vs baseline for {bad} -- results are not "
            "comparable across universes; pass allow_universe_mismatch=True "
            "to override")
    out["diff"] = out["value"] - out["baseline"]
    out["within_tol"] = out["diff"].abs() <= tol
    out.loc[out["baseline"].isna(), "within_tol"] = False
    return out.drop(columns=["baseline_universe_hash"])


def population(statistic: str, path: str = REG_PATH) -> list:
    """Latest value per input_name for one statistic (deflated-Sharpe trials)."""
    reg = load(path)
    if reg.empty:
        return []
    sub = reg[(reg["statistic"] == statistic) & reg["value"].notna()]
    if sub.empty:
        return []
    latest = (sub.sort_values("created_at")
                 .groupby("input_name", as_index=False)
                 .tail(1))
    return [float(v) for v in latest["value"]]


def summary(path: str = REG_PATH) -> str:
    """One-screen ASCII summary (the registry's CLI export)."""
    reg = load(path)
    if reg.empty:
        return f"registry empty ({path})"
    lines = [f"{len(reg)} rows, {reg['input_name'].nunique()} inputs, "
             f"{reg['run_id'].nunique()} runs ({path})"]
    per = (reg.groupby("input_name")
              .agg(rows=("run_id", "size"), runs=("run_id", "nunique"),
                   latest=("created_at", "max")))
    for name, r in per.iterrows():
        lines.append(f"  {name}: {r['rows']} rows, {r['runs']} runs, "
                     f"latest {str(r['latest'])[:19]}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    print(summary(sys.argv[1] if len(sys.argv) > 1 else REG_PATH))
```

- [ ] **Step 4: Add the registry dir to `.gitignore`**

Append this line to the repo-root `.gitignore` (keep existing content untouched):

```
storage/eval_registry/
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_evaluation.py -v`
Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add evaluation/registry.py tests/test_evaluation.py .gitignore
git commit -m "feat(evaluation): append-only parquet results registry (baselines, compare, population)"
```

---

### Task 9: Runner + generic `evaluate.py` CLI

**Files:**
- Create: `evaluation/runner.py`, `evaluate.py` (repo root)
- Modify: `evaluation/__init__.py` (re-export `run`)
- Test: `tests/test_evaluation.py` (append)

**Interfaces:**
- Consumes: everything — `contracts.Signal/EventSet/TradeRule` (Task 1), `data.apply_lag/load_closes/build_return_panel/HORIZONS` (Task 2), `ic.evaluate_ic` (Task 3), `portfolio.evaluate_portfolio/summarize_portfolio` (Task 4), `events.evaluate_events` (Task 4), `trades.simulate/trade_summary` (Task 5), `stats.block_bootstrap_spread/bootstrap_sharpe/permutation_trades/bh_fdr/t_to_p` (Task 6), `stats.walk_forward/regime_conditioning/deflated_sharpe/registry_percentile` (Task 7), `registry.*` (Task 8).
- Produces: `run(obj, universe=None, start=None, end=None, benchmark="SPY", price_table=None, quantiles=5, rebalance="M", long_short=True, out_root="storage/reports/eval", registry_path=None, write_registry=True, n_boot=1000, n_perm=200, seed=0, cache=None) -> dict` with keys `name, input_type, run_id, out_dir, n_evaluations, results, rows_written`. Dispatch: `Signal` → apply_lag once → panel → IC + Tier 2 + Tier 3 + quantile portfolio; `EventSet` → apply_lag once → `evaluate_events` (engine `entry_lag=1`); `TradeRule` → `simulate` + `trade_summary` + `permutation_trades` (requires `cache`, raises `ValueError` without it). Artifacts written to `out_dir = <out_root>/<name>_<UTC yyyymmdd_hhmmss>/`: `results.json`, `run_meta.json`, plus `panel.parquet` (Signal) or `trades.parquet` (TradeRule). `registry_path=None` means `registry.REG_PATH`.
- Produces (CLI): `evaluate.py` with `main(argv=None) -> int`; flags `--input-parquet PATH` (required in Task 9; Task 10 adds adapter alternatives), `--input-type {signal,events}` (default signal), `--name` (required), `--lag-days INT` (default 0), `--direction {1,-1,0}` (default 1), `--universe SYM [SYM ...]`, `--start/--end YYYY-MM-DD`, `--price-table`, `--n-boot` (default 1000), `--n-perm` (default 200), `--no-registry`; exit 0 on success, 1 when zero evaluations were produced.
- Registry-row convention (relied on by Tasks 11–12): every numeric leaf of each result dict becomes a row; key statistics are `pooled_ic`/`mean_daily_ic`/`ic_t_stat`/`spread_pct` (evaluation=`ic`, horizon=h), `spread_ci_lo_pct`/`spread_ci_hi_pct` (evaluation=`ic_boot`, horizon=h), `sharpe`/`sharpe_ci_lo`/`sharpe_ci_hi` (evaluation=`portfolio_boot`, horizon=-1), `dsr_prob` (evaluation=`tier3`, horizon=-1), event rows as evaluation=`events:<label>`, trade rows as evaluation=`trades`/`trades_perm` (horizon=-1). `deflated_sharpe`'s trial population = `registry.population("sharpe")` + this run.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_evaluation.py`:

```python
import types

from evaluation import runner as ev_runner
import evaluate as ev_cli


def _fake_price_world(n=320, syms=("AAA", "BBB", "CCC", "DDD", "EEE", "FFF"),
                      seed=3):
    idx = pd.bdate_range("2024-01-02", periods=n)
    rng = np.random.default_rng(seed)
    data = {s: 100.0 * np.cumprod(1 + rng.normal(0.0003, 0.01, n))
            for s in syms}
    data["SPY"] = 100.0 * np.cumprod(1 + rng.normal(0.0003, 0.008, n))
    return pd.DataFrame(data, index=idx)


def _runner_signal(closes, n_sig_dates=260, seed=7):
    syms = [c for c in closes.columns if c != "SPY"]
    dates = closes.index[:n_sig_dates]
    rng = np.random.default_rng(seed)
    rows = [{"symbol": s, "date": d, "value": float(rng.normal())}
            for d in dates for s in syms]
    return Signal(name="test_sig", frame=pd.DataFrame(rows), lag_days=1)


def _install_fake_market(monkeypatch, closes):
    """Fake the two repo modules the evaluation package imports locally."""
    fake_eb = types.SimpleNamespace(
        load_close_matrix=lambda syms, start=None, end=None, price_table=None:
            closes[[s for s in syms if s in closes.columns]])
    monkeypatch.setitem(sys.modules, "event_backtest", fake_eb)

    ls = closes.drop(columns=["SPY"]).pct_change().mean(axis=1).fillna(0.0)
    fake_res = types.SimpleNamespace(
        returns=ls, equity=(1 + ls).cumprod(),
        benchmark=closes["SPY"].pct_change().fillna(0.0),
        weights=None,
        metrics={"sharpe": 0.9, "cagr_pct": 7.5, "max_drawdown_pct": -12.0},
        params={"quantiles": 5, "rebalance": "M"})
    fake_bt = types.SimpleNamespace(backtest=lambda *a, **kw: fake_res)
    monkeypatch.setitem(sys.modules, "backtest", fake_bt)
    return fake_eb, fake_bt


class TestRunner:
    def test_signal_end_to_end(self, tmp_path, monkeypatch):
        closes = _fake_price_world()
        _install_fake_market(monkeypatch, closes)
        sig = _runner_signal(closes)
        reg_path = str(tmp_path / "reg" / "results.parquet")
        res = ev_runner.run(sig, out_root=str(tmp_path / "reports"),
                            registry_path=reg_path,
                            n_boot=50, n_perm=10, seed=0)
        assert res["input_type"] == "signal"
        assert res["n_evaluations"] >= 2          # ic + portfolio at minimum
        out_dir = res["out_dir"]
        for fname in ("results.json", "run_meta.json", "panel.parquet"):
            assert os.path.exists(os.path.join(out_dir, fname))
        with open(os.path.join(out_dir, "run_meta.json")) as fh:
            meta = json.load(fh)
        assert meta["input_name"] == "test_sig"
        assert meta["universe_hash"]
        assert "dropped" in meta and "git_commit" in meta
        assert ".." in meta["date_range"]
        ic1 = res["results"]["ic"][1]
        assert ic1["pooled_ic"] is not None
        assert "tier2" in res["results"] and "tier3" in res["results"]
        assert "fdr" in res["results"]
        reg = ev_registry.load(reg_path)
        assert res["rows_written"] == len(reg) > 0
        assert (reg["statistic"] == "pooled_ic").any()
        assert (reg["statistic"] == "sharpe").any()
        assert reg["run_id"].nunique() == 1

    def test_signal_no_registry_write(self, tmp_path, monkeypatch):
        closes = _fake_price_world()
        _install_fake_market(monkeypatch, closes)
        sig = _runner_signal(closes)
        reg_path = str(tmp_path / "reg.parquet")
        res = ev_runner.run(sig, out_root=str(tmp_path / "reports"),
                            registry_path=reg_path, write_registry=False,
                            n_boot=20, n_perm=5)
        assert res["rows_written"] == 0
        assert not os.path.exists(reg_path)

    def test_events_dispatch(self, tmp_path, monkeypatch):
        fake_events_result = {
            "labels": {"up": {"n_events": 6,
                              "horizons": {1: {"n": 6, "mean_pct": 0.5,
                                               "t_stat": 1.2},
                                           5: {"n": 6, "mean_pct": 1.1,
                                               "t_stat": 1.8}},
                              "mean_car_pct": {0: 0.0, 1: 0.4}}},
            "skipped": {"tiny": "3 events < min_events=5"},
        }
        seen = {}

        def fake_evaluate_events(frame, **kw):
            seen["frame"] = frame
            seen["kw"] = kw
            return fake_events_result

        import evaluation.events as ev_events_mod
        monkeypatch.setattr(ev_events_mod, "evaluate_events",
                            fake_evaluate_events)
        ev = EventSet(name="test_ev", frame=pd.DataFrame({
            "symbol": ["AAA"] * 6, "label": ["up"] * 6,
            "date": pd.bdate_range("2024-02-01", periods=6)}), lag_days=1)
        reg_path = str(tmp_path / "reg.parquet")
        res = ev_runner.run(ev, out_root=str(tmp_path / "reports"),
                            registry_path=reg_path)
        # runner applied the 1-BDay lag before handing the frame over
        assert (pd.to_datetime(seen["frame"]["date"]).min()
                > pd.Timestamp("2024-02-01"))
        assert seen["kw"]["entry_lag"] == 1
        assert res["results"]["events"] == fake_events_result
        reg = ev_registry.load(reg_path)
        assert (reg["evaluation"] == "events:up").any()
        row = reg[(reg["evaluation"] == "events:up")
                  & (reg["horizon"] == 5) & (reg["statistic"] == "mean_pct")]
        assert row.iloc[0]["value"] == pytest.approx(1.1)

    def test_trade_rule_dispatch(self, tmp_path):
        idx = pd.bdate_range("2024-01-02", periods=40)
        close = pd.Series(np.linspace(100, 120, 40), index=idx)
        ent = np.zeros(40, dtype=bool)
        ent[[5, 20]] = True
        exi = np.zeros(40, dtype=bool)
        exi[[10, 25]] = True
        df = pd.DataFrame({"close": close, "ent": ent, "exi": exi}, index=idx)
        rule = TradeRule(name="test_rule",
                         entries=lambda d: d["ent"], exits=lambda d: d["exi"])
        reg_path = str(tmp_path / "reg.parquet")
        res = ev_runner.run(rule, cache={"AAA": df},
                            out_root=str(tmp_path / "reports"),
                            registry_path=reg_path, n_perm=20, seed=0)
        assert res["input_type"] == "trade_rule"
        assert res["results"]["summary"]["n_trades"] == 2
        assert os.path.exists(os.path.join(res["out_dir"], "trades.parquet"))
        perm = res["results"]["permutation"]
        assert perm["pnl_p"] is None or 0.0 <= perm["pnl_p"] <= 1.0
        reg = ev_registry.load(reg_path)
        assert (reg["evaluation"] == "trades").any()

    def test_trade_rule_requires_cache(self, tmp_path):
        rule = TradeRule(name="r", entries=lambda d: d["close"] > 0,
                         exits=lambda d: d["close"] < 0)
        with pytest.raises(ValueError, match="cache"):
            ev_runner.run(rule, out_root=str(tmp_path / "reports"))


class TestCli:
    def _write_signal_parquet(self, tmp_path, closes):
        sig = _runner_signal(closes)
        p = str(tmp_path / "sig.parquet")
        sig.frame.to_parquet(p, index=False)
        return p

    def test_cli_signal_happy_path(self, tmp_path, monkeypatch, capsys):
        closes = _fake_price_world()
        _install_fake_market(monkeypatch, closes)
        p = self._write_signal_parquet(tmp_path, closes)
        rc = ev_cli.main([
            "--input-parquet", p, "--input-type", "signal",
            "--name", "cli_sig", "--lag-days", "1",
            "--out-root", str(tmp_path / "reports"),
            "--registry-path", str(tmp_path / "reg.parquet"),
            "--n-boot", "20", "--n-perm", "5"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "cli_sig" in out
        assert out.isascii()

    def test_cli_zero_evaluations_exits_nonzero(self, tmp_path, monkeypatch,
                                                capsys):
        closes = _fake_price_world()
        _install_fake_market(monkeypatch, closes)
        frame = pd.DataFrame({"symbol": ["ZZZ"] * 5,
                              "date": pd.bdate_range("2024-02-01", periods=5),
                              "value": [1.0, 2.0, 3.0, 4.0, 5.0]})
        p = str(tmp_path / "zzz.parquet")
        frame.to_parquet(p, index=False)
        rc = ev_cli.main([
            "--input-parquet", p, "--name", "no_prices",
            "--out-root", str(tmp_path / "reports"), "--no-registry"])
        assert rc == 1

    def test_cli_rejects_bad_input_type(self, tmp_path):
        with pytest.raises(SystemExit):
            ev_cli.main(["--input-parquet", "x.parquet",
                         "--input-type", "bogus", "--name", "n"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_evaluation.py -k "Runner or Cli" -v`
Expected: collection error `ModuleNotFoundError: No module named 'evaluation.runner'`.

- [ ] **Step 3: Implement `evaluation/runner.py`**

```python
"""
evaluation/runner.py -- dispatch an input contract to its evaluations,
write artifacts (results.json / run_meta.json / parquet), append registry
rows. THE place lag_days is applied (via data.apply_lag, exactly once);
evaluators downstream never shift dates.
"""

import json
import os
import subprocess
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from evaluation import data as ev_data
from evaluation import events as ev_events
from evaluation import ic as ev_ic
from evaluation import portfolio as ev_portfolio
from evaluation import registry as ev_registry
from evaluation import stats as ev_stats
from evaluation import trades as ev_trades
from evaluation.contracts import EventSet, Signal, TradeRule


def _git_commit() -> str:
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return subprocess.check_output(["git", "rev-parse", "HEAD"],
                                       cwd=root, text=True).strip()
    except Exception:
        return "unknown"


def _json_safe(v):
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if isinstance(v, (np.floating, np.integer)):
        return v.item()
    if isinstance(v, (pd.Timestamp, datetime)):
        return str(v)
    if isinstance(v, pd.Series):
        return {str(k): _json_safe(x) for k, x in v.items()}
    if isinstance(v, pd.DataFrame):
        return v.to_dict("records")
    if isinstance(v, float) and not np.isfinite(v):
        return None
    return v


def _stat_rows(evaluation: str, horizon: int, d: dict, n_key=None) -> list:
    """One registry row per numeric (non-bool) leaf of a flat result dict."""
    n = 0
    if n_key is not None and d.get(n_key) is not None:
        n = int(d[n_key])
    rows = []
    for k, v in d.items():
        if isinstance(v, bool) or not isinstance(v, (int, float, np.floating,
                                                     np.integer)):
            continue
        rows.append({"evaluation": evaluation, "horizon": int(horizon),
                     "statistic": str(k), "value": float(v), "n": n})
    return rows


def _oriented(panel: pd.DataFrame, direction: int) -> pd.DataFrame:
    work = panel.copy()
    if direction == -1:
        work["value"] = -work["value"]
    return work


def _returns_series(res):
    rets = res.returns
    if isinstance(rets, pd.DataFrame):
        col = "long_short" if "long_short" in rets.columns else rets.columns[0]
        rets = rets[col]
    return pd.Series(rets).dropna()


def _run_signal(obj: Signal, universe, start, end, benchmark, price_table,
                quantiles, rebalance, long_short, n_boot, n_perm, seed,
                registry_path):
    lagged = ev_data.apply_lag(obj.frame, obj.lag_days)
    symbols = (sorted(universe) if universe
               else sorted(lagged["symbol"].unique()))
    closes = ev_data.load_closes(symbols, start=start, end=end,
                                 benchmark=benchmark, price_table=price_table)
    panel, dropped = ev_data.build_return_panel(lagged, closes,
                                                ev_data.HORIZONS, benchmark)
    results = {}
    rows = []
    if panel.empty:
        return results, rows, panel, dropped, symbols

    # Tier 1: pooled/daily IC + cross-sectional bucket spread, per horizon
    ic_res = ev_ic.evaluate_ic(panel, direction=obj.direction)
    results["ic"] = ic_res
    for h, d in ic_res.items():
        rows += _stat_rows("ic", h, d, n_key="n")

    work = _oriented(panel, obj.direction)

    # Tier 2: date-block bootstrap CI on the bucket spread, per horizon
    tier2 = {}
    for h in ev_data.HORIZONS:
        fcol = f"fwd_{h}d"
        if fcol not in work.columns:
            continue
        boot = ev_stats.block_bootstrap_spread(work, "value", fcol,
                                               n_boot=n_boot, seed=seed)
        tier2[h] = boot
        rows += _stat_rows("ic_boot", h, boot, n_key="boot_days")
    results["tier2"] = tier2

    # Tier 3: walk-forward OOS + regime conditioning on the 5d horizon
    tier3 = {}
    ref_col = "fwd_5d" if "fwd_5d" in work.columns else None
    if ref_col is not None:
        tier3["walk_forward"] = ev_stats.walk_forward(work, "value", ref_col)
        oos = tier3["walk_forward"].get("oos")
        if isinstance(oos, dict):
            rows += _stat_rows("tier3_wf_oos", 5, oos, n_key="ic_days")
        if benchmark and benchmark in closes.columns:
            tier3["regimes"] = ev_stats.regime_conditioning(
                work, "value", ref_col, closes[benchmark].dropna())
            for regime, d in tier3["regimes"].items():
                if isinstance(d, dict):
                    rows += _stat_rows(f"tier3_regime_{regime}", 5, d,
                                       n_key="n_days")

    # Quantile portfolio (wraps backtest.backtest) + Sharpe bootstrap + DSR
    portfolio = None
    try:
        res = ev_portfolio.evaluate_portfolio(
            lagged, direction=obj.direction, quantiles=quantiles,
            rebalance=rebalance, long_short=long_short, start=start, end=end,
            price_table=price_table)
        portfolio = ev_portfolio.summarize_portfolio(res)
        rets = _returns_series(res)
        boot_sharpe = ev_stats.bootstrap_sharpe(rets, n_boot=n_boot, seed=seed)
        portfolio["sharpe_bootstrap"] = boot_sharpe
        rows += _stat_rows("portfolio_boot", -1, boot_sharpe, n_key="n_boot")
        sharpe_now = boot_sharpe.get("sharpe")
        if sharpe_now is not None:
            trials = ev_registry.population("sharpe", path=registry_path)
            trials = trials + [sharpe_now]
            dsr = ev_stats.deflated_sharpe(sharpe_now, len(rets), trials)
            pct = ev_stats.registry_percentile(sharpe_now, trials)
            tier3["deflated_sharpe"] = dsr
            tier3["registry_percentile"] = pct
            rows += _stat_rows("tier3", -1, dsr, n_key="n_trials")
            rows += _stat_rows("tier3", -1, pct, n_key="n_population")
    except Exception as exc:  # portfolio eval is best-effort, never fatal
        portfolio = {"portfolio_reason": f"{type(exc).__name__}: {exc}"}
    results["portfolio"] = portfolio
    results["tier3"] = tier3

    # BH-FDR across every p-value this run produced
    records = []
    for h, d in ic_res.items():
        for stat, p in (("pooled_p", d.get("pooled_p")),
                        ("spread_p", d.get("spread_p"))):
            records.append({"evaluation": "ic", "horizon": h,
                            "statistic": stat, "p": p})
        t = d.get("ic_t_stat")
        records.append({"evaluation": "ic", "horizon": h,
                        "statistic": "daily_ic_p",
                        "p": ev_stats.t_to_p(t) if t is not None else None})
    if records:
        fdr = ev_stats.bh_fdr(pd.DataFrame(records))
        results["fdr"] = fdr.to_dict("records")

    return results, rows, panel, dropped, symbols


def run(obj, universe=None, start=None, end=None, benchmark="SPY",
        price_table=None, quantiles=5, rebalance="M", long_short=True,
        out_root=os.path.join("storage", "reports", "eval"),
        registry_path=None, write_registry=True,
        n_boot=1000, n_perm=200, seed=0, cache=None) -> dict:
    registry_path = registry_path or ev_registry.REG_PATH
    panel = trades_df = None
    dropped = {}

    if isinstance(obj, Signal):
        input_type = "signal"
        results, rows, panel, dropped, symbols = _run_signal(
            obj, universe, start, end, benchmark, price_table, quantiles,
            rebalance, long_short, n_boot, n_perm, seed, registry_path)
    elif isinstance(obj, EventSet):
        input_type = "event_set"
        lagged = ev_data.apply_lag(obj.frame, obj.lag_days)
        symbols = (sorted(universe) if universe
                   else sorted(lagged["symbol"].unique()))
        ev_res = ev_events.evaluate_events(
            lagged, min_events=obj.min_events, benchmark=benchmark,
            window=(0, 21), entry_lag=1, price_table=price_table)
        results = {"events": ev_res}
        rows = []
        for label, d in ev_res.get("labels", {}).items():
            for h, rowdict in d.get("horizons", {}).items():
                rows += _stat_rows(f"events:{label}", int(h), rowdict,
                                   n_key="n")
    elif isinstance(obj, TradeRule):
        input_type = "trade_rule"
        if cache is None:
            raise ValueError(
                "TradeRule evaluation needs cache={symbol: DataFrame} -- "
                "pass the per-symbol frames the rule's callables read")
        symbols = sorted(cache.keys())
        trades_df = ev_trades.simulate(obj, cache)
        summary = ev_trades.trade_summary(trades_df)
        perm = ev_stats.permutation_trades(obj, cache, n_perm=n_perm,
                                           seed=seed)
        results = {"summary": summary, "permutation": perm}
        rows = (_stat_rows("trades", -1, summary, n_key="n_trades")
                + _stat_rows("trades_perm", -1, perm, n_key="n_perm"))
    else:
        raise TypeError(f"cannot evaluate object of type {type(obj).__name__}"
                        " -- expected Signal, EventSet, or TradeRule")

    # --- artifacts -------------------------------------------------------
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(out_root, f"{obj.name}_{ts}")
    os.makedirs(out_dir, exist_ok=True)

    if input_type in ("signal", "event_set"):
        d = pd.to_datetime(obj.frame["date"])
        date_range = f"{d.min():%Y-%m-%d}..{d.max():%Y-%m-%d}"
    elif trades_df is not None and not trades_df.empty:
        date_range = (f"{trades_df['entry_date'].min():%Y-%m-%d}.."
                      f"{trades_df['exit_date'].max():%Y-%m-%d}")
    else:
        date_range = ".."
    uhash = ev_registry.universe_hash(symbols)
    run_id = ev_registry.new_run_id()
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    n_evaluations = sum(1 for v in results.values() if v)
    meta = {"run_id": run_id, "input_name": obj.name,
            "input_type": input_type, "created_at": created_at,
            "git_commit": _git_commit(), "universe": list(symbols),
            "universe_hash": uhash, "date_range": date_range,
            "dropped": dropped, "n_evaluations": n_evaluations,
            "params": {"lag_days": getattr(obj, "lag_days", None),
                       "direction": getattr(obj, "direction", None),
                       "benchmark": benchmark, "price_table": price_table,
                       "quantiles": quantiles, "rebalance": rebalance,
                       "long_short": long_short, "n_boot": n_boot,
                       "n_perm": n_perm, "seed": seed,
                       "start": start, "end": end}}
    with open(os.path.join(out_dir, "run_meta.json"), "w",
              encoding="utf-8") as fh:
        json.dump(_json_safe(meta), fh, indent=2, default=str)
    with open(os.path.join(out_dir, "results.json"), "w",
              encoding="utf-8") as fh:
        json.dump(_json_safe(results), fh, indent=2, default=str)
    if panel is not None and not panel.empty:
        panel.to_parquet(os.path.join(out_dir, "panel.parquet"), index=False)
    if trades_df is not None:
        trades_df.to_parquet(os.path.join(out_dir, "trades.parquet"),
                             index=False)

    # --- registry --------------------------------------------------------
    rows_written = 0
    if write_registry and rows:
        frame = pd.DataFrame(rows)
        frame["run_id"] = run_id
        frame["input_name"] = obj.name
        frame["input_type"] = input_type
        frame["universe_hash"] = uhash
        frame["date_range"] = date_range
        frame["created_at"] = created_at
        rows_written = ev_registry.append(frame, path=registry_path)

    return {"name": obj.name, "input_type": input_type, "run_id": run_id,
            "out_dir": out_dir, "n_evaluations": n_evaluations,
            "results": results, "rows_written": rows_written}
```

- [ ] **Step 4: Re-export `run` in `evaluation/__init__.py`**

Replace the file's contents with:

```python
"""evaluation -- unified signal/trade/event evaluation framework (v1)."""

from evaluation.contracts import Signal, EventSet, TradeRule  # noqa: F401
from evaluation.runner import run  # noqa: F401
```

- [ ] **Step 5: Implement `evaluate.py` (repo root)**

```python
r"""
evaluate.py -- compute-stage CLI for the unified evaluation framework.

Evaluates a Signal or EventSet parquet against forward returns with the
three-tier significance battery, writes artifacts under
storage/reports/eval/, and appends baselines to the results registry.

Usage:
  C:\ProgramData\anaconda3\python.exe evaluate.py --input-parquet sig.parquet
      --input-type signal --name my_signal --lag-days 1 --direction 1
  C:\ProgramData\anaconda3\python.exe evaluate.py --input-parquet ev.parquet
      --input-type events --name fda_approvals

Input parquet layout: signal = [symbol, date, value]; events =
[symbol, date, label]. Adapter flags for repo-native sources (factor panel,
sentiment, TV ratings) are added by evaluation/adapters.py (Task 10).
"""

import argparse
import sys


def _fmt(v):
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def _print_signal_summary(res):
    ic = res["results"].get("ic", {})
    print(f"== {res['name']} (signal) run {res['run_id']} ==")
    print(f"{'h':>3} {'pooled_ic':>10} {'daily_ic':>9} {'t':>6} "
          f"{'spread_pct':>10}")
    for h in sorted(ic):
        d = ic[h]
        print(f"{h:>3} {_fmt(d.get('pooled_ic')):>10} "
              f"{_fmt(d.get('mean_daily_ic')):>9} "
              f"{_fmt(d.get('ic_t_stat')):>6} {_fmt(d.get('spread_pct')):>10}")
    port = res["results"].get("portfolio") or {}
    boot = port.get("sharpe_bootstrap") or {}
    if boot.get("sharpe") is not None:
        print(f"portfolio sharpe {_fmt(boot['sharpe'])} "
              f"[{_fmt(boot.get('sharpe_ci_lo'))}, "
              f"{_fmt(boot.get('sharpe_ci_hi'))}]")
    t3 = res["results"].get("tier3", {})
    dsr = t3.get("deflated_sharpe") or {}
    if dsr.get("dsr_prob") is not None:
        print(f"deflated sharpe prob {_fmt(dsr['dsr_prob'])} "
              f"(n_trials={dsr.get('n_trials')})")


def _print_events_summary(res):
    ev = res["results"].get("events", {})
    print(f"== {res['name']} (events) run {res['run_id']} ==")
    for label, d in ev.get("labels", {}).items():
        print(f"label {label}: n_events={d.get('n_events')}")
        for h, row in sorted(d.get("horizons", {}).items()):
            print(f"  h={h:>3} mean={_fmt(row.get('mean_pct'))}% "
                  f"t={_fmt(row.get('t_stat'))} n={row.get('n')}")
    for label, why in ev.get("skipped", {}).items():
        print(f"skipped {label}: {why}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Unified evaluation framework -- compute stage")
    ap.add_argument("--input-parquet", required=True,
                    help="parquet with [symbol, date, value] or "
                         "[symbol, date, label]")
    ap.add_argument("--input-type", choices=["signal", "events"],
                    default="signal")
    ap.add_argument("--name", required=True,
                    help="registry name for this input")
    ap.add_argument("--lag-days", type=int, default=0,
                    help="business days between data date and availability")
    ap.add_argument("--direction", type=int, choices=[1, -1, 0], default=1)
    ap.add_argument("--universe", nargs="*", default=None)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--price-table", default=None)
    ap.add_argument("--quantiles", type=int, default=5)
    ap.add_argument("--rebalance", default="M")
    ap.add_argument("--n-boot", type=int, default=1000)
    ap.add_argument("--n-perm", type=int, default=200)
    ap.add_argument("--out-root", default=None)
    ap.add_argument("--registry-path", default=None)
    ap.add_argument("--no-registry", action="store_true",
                    help="do not append this run to the results registry")
    args = ap.parse_args(argv)

    import pandas as pd

    from evaluation import runner
    from evaluation.contracts import EventSet, Signal

    frame = pd.read_parquet(args.input_parquet)
    if args.input_type == "signal":
        obj = Signal(name=args.name, frame=frame, lag_days=args.lag_days,
                     direction=args.direction, source=args.input_parquet)
    else:
        obj = EventSet(name=args.name, frame=frame, lag_days=args.lag_days)

    kwargs = dict(universe=args.universe, start=args.start, end=args.end,
                  benchmark=args.benchmark, price_table=args.price_table,
                  quantiles=args.quantiles, rebalance=args.rebalance,
                  write_registry=not args.no_registry,
                  n_boot=args.n_boot, n_perm=args.n_perm)
    if args.out_root:
        kwargs["out_root"] = args.out_root
    if args.registry_path:
        kwargs["registry_path"] = args.registry_path
    res = runner.run(obj, **kwargs)

    if res["n_evaluations"] == 0:
        print(f"X no evaluations produced for {args.name} -- every symbol "
              "was dropped (no prices / history too short). See run_meta.json"
              f" in {res['out_dir']}")
        return 1
    if args.input_type == "signal":
        _print_signal_summary(res)
    else:
        _print_events_summary(res)
    print(f"artifacts: {res['out_dir']}")
    print(f"registry rows written: {res['rows_written']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_evaluation.py -v`
Expected: all tests PASS.

- [ ] **Step 7: Run the full suite to confirm nothing broke**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/ -q`
Expected: no NEW failures vs the pre-existing state (unrelated in-flight constituents work may already affect other files).

- [ ] **Step 8: Commit**

```bash
git add evaluation/runner.py evaluation/__init__.py evaluate.py tests/test_evaluation.py
git commit -m "feat(evaluation): runner dispatch + generic evaluate.py CLI (artifacts, registry rows, FDR)"
```

---

### Task 10: Adapters for repo-native sources + adapter CLI flags

**Files:**
- Create: `evaluation/adapters.py`
- Modify: `evaluate.py` (add `--adapter` flags)
- Test: `tests/test_evaluation.py` (append)

**Interfaces:**
- Consumes: `Signal`/`EventSet`/`TradeRule` (Task 1); repo modules `analytics.signals.signal_panel` (tidy frame `symbol|date|momentum|...|composite`), `sentiment_eval.daily_signals` (`symbol, date, sent_score, n_articles`), `tv_rating_eval.build_signal_cache`/`universe` + constants `BULL_MIN, BEAR_MAX, EXIT_LONG_MAX, EXIT_SHORT_MIN, NOTIONAL`, `event_backtest.rating_changes` (columns `symbol, date, from_label, to_label, from_score, to_score, step, direction`). All imported LOCALLY inside each adapter (monkeypatch convention).
- Produces: `from_signal_panel(factor="composite", symbols=None, start=None, end=None) -> Signal` (name `factor_<factor>`); `from_sentiment(start=None, end=None, min_articles=1) -> Signal` (name `news_sentiment`); `rating_cache(symbols=None, price_table=None, start=None, end=None) -> dict[str, pd.DataFrame]`; `from_rating_history(signal_col="rating_all", cache=None, symbols=None, price_table=None, start=None, end=None) -> Signal` (name `tv_<signal_col>`); `from_rating_changes(symbols=None, start="2000-01-01", end=None, min_step=1, price_table=None) -> EventSet` (name `tv_rating_changes`, label = the `direction` column, `up`/`down`); `tv_threshold_rule() -> TradeRule` (name `tv_threshold`, `side="both"`, thresholds imported from `tv_rating_eval` so constants stay single-sourced). CLI gains `--adapter {signal-panel, sentiment, rating, rating-changes, tv-rule}` — `tv-rule` builds `tv_threshold_rule()` plus `rating_cache(...)` and passes the cache to the runner, so ALL THREE input types evaluate through one CLI invocation each (spec success criterion 1).
- ALL adapters return `lag_days=0`, `direction=1`: the legacy harnesses (`sentiment_eval`, `tv_rating_eval`) enter at the next close after the signal date with no extra lag, and `build_return_panel`'s strictly-after entry reproduces exactly that — so registry baselines stay comparable (Task 12 acceptance depends on this; TV's IC baseline is recorded as raw mildly-NEGATIVE values, not orientation-corrected).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_evaluation.py`:

```python
from evaluation import adapters as ev_adapters


def _fake_panel_frame():
    dates = pd.bdate_range("2024-01-02", periods=10)
    rows = []
    for d in dates:
        for s in ("AAA", "BBB"):
            rows.append({"symbol": s, "date": d,
                         "momentum": 0.1, "value": -0.2, "composite": 0.05})
    return pd.DataFrame(rows)


class TestAdapters:
    def test_from_signal_panel(self, monkeypatch):
        import analytics.signals as sig_mod
        monkeypatch.setattr(sig_mod, "signal_panel",
                            lambda symbols=None, start=None, end=None:
                            _fake_panel_frame())
        s = ev_adapters.from_signal_panel(factor="momentum")
        assert isinstance(s, Signal)
        assert s.name == "factor_momentum"
        assert s.lag_days == 0 and s.direction == 1
        assert list(s.frame.columns) == ["symbol", "date", "value"]
        assert (s.frame["value"] == 0.1).all()

    def test_from_signal_panel_value_factor_no_collision(self, monkeypatch):
        # the FACTOR named "value" must land in the contract column "value"
        import analytics.signals as sig_mod
        monkeypatch.setattr(sig_mod, "signal_panel",
                            lambda symbols=None, start=None, end=None:
                            _fake_panel_frame())
        s = ev_adapters.from_signal_panel(factor="value")
        assert (s.frame["value"] == -0.2).all()

    def test_from_signal_panel_unknown_factor(self, monkeypatch):
        import analytics.signals as sig_mod
        monkeypatch.setattr(sig_mod, "signal_panel",
                            lambda symbols=None, start=None, end=None:
                            _fake_panel_frame())
        with pytest.raises(ValueError, match="factor"):
            ev_adapters.from_signal_panel(factor="nope")

    def test_from_sentiment(self, monkeypatch):
        import sentiment_eval as se_mod
        fake = pd.DataFrame({"symbol": ["AAA", "BBB"],
                             "date": pd.to_datetime(["2024-01-02",
                                                     "2024-01-02"]),
                             "sent_score": [0.3, -0.1],
                             "n_articles": [4, 2]})
        monkeypatch.setattr(se_mod, "daily_signals",
                            lambda min_articles=1, start=None, end=None: fake)
        s = ev_adapters.from_sentiment()
        assert s.name == "news_sentiment"
        assert list(s.frame.columns) == ["symbol", "date", "value"]
        assert s.frame["value"].tolist() == [0.3, -0.1]

    def test_from_rating_history(self):
        idx = pd.bdate_range("2024-01-02", periods=6)
        cache = {"AAA": pd.DataFrame({"close": 100.0, "rating_all": 0.4,
                                      "rating_ma": 0.2}, index=idx),
                 "BBB": pd.DataFrame({"close": 50.0, "rating_all": -0.3,
                                      "rating_ma": -0.1}, index=idx)}
        s = ev_adapters.from_rating_history(signal_col="rating_all",
                                            cache=cache)
        assert s.name == "tv_rating_all"
        assert len(s.frame) == 12
        assert set(s.frame["symbol"]) == {"AAA", "BBB"}
        aaa = s.frame[s.frame["symbol"] == "AAA"]
        assert (aaa["value"] == 0.4).all()

    def test_from_rating_changes(self, monkeypatch):
        import event_backtest as eb_mod
        fake = pd.DataFrame({"symbol": ["AAA", "BBB"],
                             "date": pd.to_datetime(["2024-03-01",
                                                     "2024-03-04"]),
                             "from_label": ["neutral", "buy"],
                             "to_label": ["buy", "neutral"],
                             "from_score": [0.0, 0.5], "to_score": [0.5, 0.0],
                             "step": [1, 1], "direction": ["up", "down"]})
        seen = {}

        def fake_changes(symbols, start=None, end=None, min_step=1,
                         price_table=None, **kw):
            seen["start"] = start
            return fake

        monkeypatch.setattr(eb_mod, "rating_changes", fake_changes)
        ev = ev_adapters.from_rating_changes(symbols=["AAA", "BBB"],
                                             min_events=1)
        assert isinstance(ev, EventSet)
        assert ev.name == "tv_rating_changes"
        assert sorted(ev.frame["label"].unique()) == ["down", "up"]
        assert seen["start"] is not None      # full-history scan needs start

    def test_tv_threshold_rule_matches_legacy_semantics(self):
        import tv_rating_eval as tv
        rule = ev_adapters.tv_threshold_rule()
        assert isinstance(rule, TradeRule)
        assert rule.side == "both"
        assert rule.notional == tv.NOTIONAL
        idx = pd.bdate_range("2024-01-02", periods=6)
        df = pd.DataFrame({"close": 100.0,
                           "rating_all": [0.0, 0.6, 0.6, 0.05, 0.6, -0.6]},
                          index=idx)
        le = rule.entries(df).to_numpy()
        lx = rule.exits(df).to_numpy()
        se_ = rule.short_entries(df).to_numpy()
        sx = rule.short_exits(df).to_numpy()
        # long entry only on the CROSS up through +0.5 (days 1 and 4)
        assert le.tolist() == [False, True, False, False, True, False]
        # long exit whenever rating < +0.1 (days 0, 3, 5)
        assert lx.tolist() == [True, False, False, True, False, True]
        # short entry on the cross down through -0.5 (day 5)
        assert se_.tolist() == [False, False, False, False, False, True]
        # short exit whenever rating > -0.1 (days 0..4)
        assert sx.tolist() == [True, True, True, True, True, False]


class TestCliAdapters:
    def test_cli_adapter_signal_panel(self, tmp_path, monkeypatch, capsys):
        closes = _fake_price_world()
        _install_fake_market(monkeypatch, closes)
        import analytics.signals as sig_mod
        dates = closes.index[:260]
        syms = [c for c in closes.columns if c != "SPY"]
        rng = np.random.default_rng(11)
        rows = [{"symbol": s, "date": d, "composite": float(rng.normal())}
                for d in dates for s in syms]
        monkeypatch.setattr(sig_mod, "signal_panel",
                            lambda symbols=None, start=None, end=None:
                            pd.DataFrame(rows))
        rc = ev_cli.main(["--adapter", "signal-panel", "--factor", "composite",
                          "--out-root", str(tmp_path / "reports"),
                          "--registry-path", str(tmp_path / "reg.parquet"),
                          "--n-boot", "20", "--n-perm", "5"])
        assert rc == 0
        assert "factor_composite" in capsys.readouterr().out

    def test_cli_requires_exactly_one_source(self):
        with pytest.raises(SystemExit):
            ev_cli.main(["--name", "x"])                       # neither
        with pytest.raises(SystemExit):
            ev_cli.main(["--input-parquet", "a.parquet",
                         "--adapter", "sentiment", "--name", "x"])  # both

    def test_cli_adapter_tv_rule(self, tmp_path, monkeypatch, capsys):
        import evaluation.adapters as ad_mod
        idx = pd.bdate_range("2024-01-02", periods=30)
        df = pd.DataFrame({"close": np.linspace(100, 110, 30),
                           "rating_all": [0.0] * 5 + [0.6] * 5 + [0.0] * 20},
                          index=idx)
        monkeypatch.setattr(ad_mod, "rating_cache",
                            lambda **kw: {"AAA": df})
        rc = ev_cli.main(["--adapter", "tv-rule",
                          "--out-root", str(tmp_path / "reports"),
                          "--registry-path", str(tmp_path / "reg.parquet"),
                          "--n-perm", "10"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "tv_threshold" in out
        assert out.isascii()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_evaluation.py -k Adapter -v`
Expected: collection error `ModuleNotFoundError: No module named 'evaluation.adapters'`.

- [ ] **Step 3: Implement `evaluation/adapters.py`**

```python
"""
evaluation/adapters.py -- turn repo-native data sources into evaluation
contracts. Each adapter is tens of lines; adding a new signal to the
framework should never require more than a function like these.

All adapters return lag_days=0 / direction=1: the legacy harnesses enter at
the next close after the signal date with no extra lag, and the engine's
strictly-after entry reproduces that -- keeping registry baselines
comparable with the historical sentiment/TV numbers.
"""

import pandas as pd

from evaluation.contracts import EventSet, Signal, TradeRule


def from_signal_panel(factor: str = "composite", symbols=None,
                      start=None, end=None) -> Signal:
    """One factor column of analytics.signals.signal_panel as a Signal."""
    from analytics import signals as _signals   # local import: monkeypatchable
    panel = _signals.signal_panel(symbols=symbols, start=start, end=end)
    if panel.empty:
        raise ValueError("signal_panel returned no rows -- run pipelines / "
                         "curated.py first")
    if factor not in panel.columns:
        have = [c for c in panel.columns if c not in ("symbol", "date")]
        raise ValueError(f"unknown factor {factor!r}; available: {have}")
    frame = panel[["symbol", "date"]].copy()
    frame["value"] = panel[factor].to_numpy()   # works for factor="value" too
    frame = frame.dropna(subset=["value"])
    return Signal(name=f"factor_{factor}", frame=frame, lag_days=0,
                  direction=1, source="analytics.signals.signal_panel")


def from_sentiment(start=None, end=None, min_articles: int = 1) -> Signal:
    """Confidence-weighted daily news sentiment (sentiment_eval's exact
    aggregation, imported -- not reimplemented)."""
    import sentiment_eval as _se
    daily = _se.daily_signals(min_articles=min_articles, start=start, end=end)
    if daily.empty:
        raise ValueError("no news_sentiment rows -- run "
                         "news_sentiment_pipeline.py first")
    frame = daily.rename(columns={"sent_score": "value"})[
        ["symbol", "date", "value"]]
    return Signal(name="news_sentiment", frame=frame, lag_days=0,
                  direction=1, source="sentiment_eval.daily_signals")


def rating_cache(symbols=None, price_table=None, start=None,
                 end=None) -> dict:
    """tv_rating_eval's per-symbol rating_history cache (for TradeRules)."""
    import tv_rating_eval as _tv
    kw = {"start": start, "end": end}
    if price_table is not None:
        kw["price_table"] = price_table
    return _tv.build_signal_cache(symbols or _tv.universe(), **kw)


def from_rating_history(signal_col: str = "rating_all", cache=None,
                        symbols=None, price_table=None, start=None,
                        end=None) -> Signal:
    """A TV rating column across the universe as a continuous Signal."""
    if cache is None:
        cache = rating_cache(symbols=symbols, price_table=price_table,
                             start=start, end=end)
    frames = []
    for sym, d in cache.items():
        if signal_col not in d.columns:
            continue
        f = pd.DataFrame({"symbol": sym, "date": d.index,
                          "value": d[signal_col].to_numpy()})
        frames.append(f)
    if not frames:
        raise ValueError(f"no {signal_col!r} data in the rating cache")
    frame = pd.concat(frames, ignore_index=True).dropna(subset=["value"])
    return Signal(name=f"tv_{signal_col}", frame=frame, lag_days=0,
                  direction=1, source="tv_rating_eval.build_signal_cache")


def from_rating_changes(symbols=None, start: str = "2000-01-01", end=None,
                        min_step: int = 1, price_table=None,
                        min_events: int = 5) -> EventSet:
    """TA rating-bucket transitions as an EventSet (label = up / down)."""
    import event_backtest as _eb
    import tv_rating_eval as _tv
    changes = _eb.rating_changes(symbols or _tv.universe(),
                                 start=start, end=end, min_step=min_step,
                                 price_table=price_table)
    if changes.empty:
        raise ValueError("rating_changes returned no transitions")
    frame = changes.rename(columns={"direction": "label"})[
        ["symbol", "date", "label"]]
    return EventSet(name="tv_rating_changes", frame=frame, lag_days=0,
                    min_events=min_events)


def tv_threshold_rule() -> TradeRule:
    """The TV threshold strategy as a TradeRule. Thresholds are imported
    from tv_rating_eval (single source of truth), and the flag semantics
    replicate its simulate_trades: entries on the CROSS through the level,
    exits on the level condition itself; the engine adds next-close
    execution and one-position-at-a-time."""
    import tv_rating_eval as _tv

    def _crossed_up(s, level):
        return (s >= level) & (s.shift(1) < level)

    def _crossed_down(s, level):
        return (s <= level) & (s.shift(1) > level)

    return TradeRule(
        name="tv_threshold",
        entries=lambda d: _crossed_up(d["rating_all"], _tv.BULL_MIN),
        exits=lambda d: d["rating_all"] < _tv.EXIT_LONG_MAX,
        side="both",
        short_entries=lambda d: _crossed_down(d["rating_all"], _tv.BEAR_MAX),
        short_exits=lambda d: d["rating_all"] > _tv.EXIT_SHORT_MIN,
        notional=_tv.NOTIONAL)
```

- [ ] **Step 4: Add adapter flags to `evaluate.py`**

In `evaluate.py`, make these three edits.

1. Change the `--input-parquet` argument to optional and add the adapter flags (replace the single `ap.add_argument("--input-parquet", ...)` block):

```python
    ap.add_argument("--input-parquet", default=None,
                    help="parquet with [symbol, date, value] or "
                         "[symbol, date, label]")
    ap.add_argument("--adapter",
                    choices=["signal-panel", "sentiment", "rating",
                             "rating-changes", "tv-rule"], default=None,
                    help="evaluate a repo-native source instead of a parquet")
    ap.add_argument("--factor", default="composite",
                    help="signal-panel adapter: which factor column")
    ap.add_argument("--signal-col", default="rating_all",
                    help="rating adapter: which rating column")
    ap.add_argument("--min-step", type=int, default=1,
                    help="rating-changes adapter: minimum bucket jump")
```

2. Change `--name` from `required=True` to `default=None` (adapters name themselves):

```python
    ap.add_argument("--name", default=None,
                    help="registry name (required with --input-parquet; "
                         "adapters name themselves)")
```

3. Replace the input-construction block (`frame = pd.read_parquet(...)` through the `EventSet(...)` line) with:

```python
    if bool(args.input_parquet) == bool(args.adapter):
        ap.error("pass exactly one of --input-parquet or --adapter")

    from evaluation import runner
    from evaluation.contracts import EventSet, Signal

    cache = None
    if args.adapter:
        from evaluation import adapters
        if args.adapter == "signal-panel":
            obj = adapters.from_signal_panel(factor=args.factor,
                                             symbols=args.universe,
                                             start=args.start, end=args.end)
        elif args.adapter == "sentiment":
            obj = adapters.from_sentiment(start=args.start, end=args.end)
        elif args.adapter == "rating":
            obj = adapters.from_rating_history(signal_col=args.signal_col,
                                               symbols=args.universe,
                                               price_table=args.price_table,
                                               start=args.start, end=args.end)
        elif args.adapter == "tv-rule":
            obj = adapters.tv_threshold_rule()
            cache = adapters.rating_cache(symbols=args.universe,
                                          price_table=args.price_table,
                                          start=args.start, end=args.end)
        else:                                   # rating-changes
            obj = adapters.from_rating_changes(symbols=args.universe,
                                               start=args.start or "2000-01-01",
                                               end=args.end,
                                               min_step=args.min_step,
                                               price_table=args.price_table)
        if args.name:
            obj.name = args.name
        from evaluation.contracts import TradeRule
        if isinstance(obj, TradeRule):
            args.input_type = "trades"
        elif isinstance(obj, EventSet):
            args.input_type = "events"
        else:
            args.input_type = "signal"
    else:
        if not args.name:
            ap.error("--name is required with --input-parquet")
        import pandas as pd
        frame = pd.read_parquet(args.input_parquet)
        if args.input_type == "signal":
            obj = Signal(name=args.name, frame=frame, lag_days=args.lag_days,
                         direction=args.direction, source=args.input_parquet)
        else:
            obj = EventSet(name=args.name, frame=frame,
                           lag_days=args.lag_days)
```

(The stray `import pandas as pd` and duplicate `from evaluation import runner` left above this block from Task 9 should be removed — the imports now live inside this block.)

4. Pass the cache through to the runner — in the `kwargs = dict(...)` block, add after `n_boot=args.n_boot, n_perm=args.n_perm)`:

```python
    if cache is not None:
        kwargs["cache"] = cache
```

5. Add a trades printer next to `_print_events_summary` and make the final print dispatch three-way (replace the `if args.input_type == "signal": ... else: _print_events_summary(res)` block):

```python
def _print_trades_summary(res):
    s = res["results"].get("summary", {})
    p = res["results"].get("permutation", {})
    print(f"== {res['name']} (trade rule) run {res['run_id']} ==")
    print(f"trades {s.get('n_trades', 0)} "
          f"(long {s.get('n_long', 0)} / short {s.get('n_short', 0)}) "
          f"win_rate {_fmt(s.get('win_rate_pct'))}% "
          f"pnl ${_fmt(s.get('total_pnl_dollars'))}")
    print(f"permutation null: pnl_p {_fmt(p.get('pnl_p'))} "
          f"win_rate_p {_fmt(p.get('win_rate_p'))} "
          f"(n_perm={p.get('n_perm')})")
```

```python
    if args.input_type == "signal":
        _print_signal_summary(res)
    elif args.input_type == "events":
        _print_events_summary(res)
    else:
        _print_trades_summary(res)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_evaluation.py -v`
Expected: all tests PASS (including the Task-9 CLI tests, which must still pass with `--input-parquet` now optional-but-validated).

- [ ] **Step 6: Commit**

```bash
git add evaluation/adapters.py evaluate.py tests/test_evaluation.py
git commit -m "feat(evaluation): adapters for factor panel, sentiment, TV ratings + adapter CLI flags"
```

---

### Task 11: Report stage — `generate_eval_report.py`

**Files:**
- Create: `generate_eval_report.py` (repo root)
- Test: `tests/test_evaluation.py` (append)

**Interfaces:**
- Consumes: ONLY the artifacts one `runner.run()` call wrote (`run_meta.json`, `results.json`, optional `trades.parquet`). Never recomputes statistics. JSON round-trip note: dict keys that were ints in memory (horizons, CAR rel-days) come back as STRINGS — every loop over them must `int(k)`.
- Produces: `main(argv=None) -> int` CLI with `--run-dir PATH` or `--latest NAME` (newest `storage/reports/eval/<NAME>_*` dir), `--out PATH` (default `<run_dir>/report.html` — the spec's per-run location), `--registry-path` (baseline table source; default `registry.REG_PATH`); `find_latest(name, root) -> str|None`; `classify_significance(mean_daily_ic, ic_t_stat) -> str` (`noise`/`weak`/`significant`, same thresholds as `generate_tv_rating_report.py`). Output: ONE self-contained HTML file, Plotly.js embedded, no external requests. Sections per spec: tiles + IC-by-horizon + spread-with-CI + regimes + FDR table (signal); per-label edge bars + CAR curves (events); P&L histogram + permutation-p tile (trades — the permutation NULL DISTRIBUTION overlay is deliberately out of v1: Tier 2 returns only the p-values, not the null draws); plus a baseline-comparison table read from the registry (reading stored rows is not recomputing).
- Style contract (dataviz skill, mirrors `generate_tv_rating_report.py`): categorical palette slots in FIXED order for series identity (`#2a78d6` blue, `#008300` green, `#e87ba4` magenta, `#eda100` yellow — never cycled, never re-ranked); status colors reserved for state only (good `#0ca30c` = significant tier / wins / bull regime, warning `#fab219` = weak tier, critical `#d03b3b` = losses / bear regime, muted `#898781` = noise tier / vol regimes); one y-axis per chart (never dual-axis); a zero reference line on every IC/spread chart; legend present for >=2 series; text in ink colors (`#0b0b0b` / `#52514e`), never series colors; ASCII-only console output.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_evaluation.py`:

```python
import generate_eval_report as ev_report


def _write_fake_run(root, name="fake_sig", ts="20260719_120000"):
    d = os.path.join(str(root), f"{name}_{ts}")
    os.makedirs(d, exist_ok=True)
    results = {
        "ic": {"1": {"pooled_ic": 0.011, "pooled_p": 0.2, "n": 900,
                     "mean_daily_ic": 0.010, "ic_t_stat": 1.1,
                     "ic_days": 250, "spread_pct": 0.05, "spread_t": 0.8,
                     "spread_p": 0.4, "top_n": 200, "bottom_n": 200,
                     "oriented": 1},
               "5": {"pooled_ic": 0.031, "pooled_p": 0.01, "n": 880,
                     "mean_daily_ic": 0.028, "ic_t_stat": 2.4,
                     "ic_days": 248, "spread_pct": 0.22, "spread_t": 2.1,
                     "spread_p": 0.04, "top_n": 190, "bottom_n": 190,
                     "oriented": 1}},
        "tier2": {"1": {"spread_boot_mean_pct": 0.05,
                        "spread_ci_lo_pct": -0.1, "spread_ci_hi_pct": 0.2,
                        "n_boot": 50, "boot_days": 250},
                  "5": {"spread_boot_mean_pct": 0.22,
                        "spread_ci_lo_pct": 0.02, "spread_ci_hi_pct": 0.4,
                        "n_boot": 50, "boot_days": 248}},
        "tier3": {"walk_forward": {"oos": {"mean_daily_ic": 0.02,
                                           "ic_t_stat": 1.5, "ic_days": 60},
                                   "n_train_days": 126},
                  "regimes": {"bull": {"n_days": 150, "mean_daily_ic": 0.03},
                              "bear": {"n_days": 100, "mean_daily_ic": -0.01},
                              "high_vol": {"n_days": 125,
                                           "mean_daily_ic": 0.02},
                              "low_vol": {"n_days": 125,
                                          "mean_daily_ic": 0.01}},
                  "deflated_sharpe": {"dsr_prob": 0.62, "sr0_ann": 0.8,
                                      "n_trials": 3}},
        "portfolio": {"metrics": {"sharpe": 0.9},
                      "sharpe_bootstrap": {"sharpe": 0.9,
                                           "sharpe_ci_lo": 0.1,
                                           "sharpe_ci_hi": 1.6,
                                           "n_boot": 50}},
        "fdr": [{"evaluation": "ic", "horizon": 5, "statistic": "pooled_p",
                 "p": 0.01, "p_adj": 0.05, "reject": True}],
    }
    meta = {"run_id": "abc123", "input_name": name, "input_type": "signal",
            "created_at": "2026-07-19T12:00:00+00:00", "git_commit": "deadbeef",
            "universe": ["AAA", "BBB"], "universe_hash": "aaa111bbb222",
            "date_range": "2024-01-02..2025-01-31",
            "dropped": {"ZZZ": "no price data"}, "n_evaluations": 4,
            "params": {"lag_days": 0, "direction": 1, "benchmark": "SPY"}}
    with open(os.path.join(d, "results.json"), "w") as fh:
        json.dump(results, fh)
    with open(os.path.join(d, "run_meta.json"), "w") as fh:
        json.dump(meta, fh)
    return d


class TestReport:
    def test_signal_report_end_to_end(self, tmp_path, capsys):
        d = _write_fake_run(tmp_path)
        out = str(tmp_path / "report.html")
        rc = ev_report.main(["--run-dir", d, "--out", out])
        assert rc == 0
        assert capsys.readouterr().out.isascii()
        html = open(out, encoding="utf-8").read()
        assert "plotly" in html.lower()
        assert "fake_sig" in html
        assert "deadbeef" in html          # provenance in the header

    def test_latest_picks_newest(self, tmp_path):
        _write_fake_run(tmp_path, ts="20260101_000000")
        d2 = _write_fake_run(tmp_path, ts="20260301_000000")
        assert ev_report.find_latest("fake_sig", root=str(tmp_path)) == d2
        assert ev_report.find_latest("nope", root=str(tmp_path)) is None

    def test_missing_run_dir_fails_cleanly(self, tmp_path, capsys):
        rc = ev_report.main(["--run-dir", str(tmp_path / "absent")])
        assert rc == 1
        assert "X" in capsys.readouterr().out

    def test_classify_significance_tiers(self):
        assert ev_report.classify_significance(0.01, 3.0) == "noise"
        assert ev_report.classify_significance(0.03, 1.0) == "noise"
        assert ev_report.classify_significance(0.03, 2.5) == "weak"
        assert ev_report.classify_significance(0.06, 2.5) == "significant"
        assert ev_report.classify_significance(None, None) == "noise"

    def test_trade_report(self, tmp_path):
        d = os.path.join(str(tmp_path), "rule_20260719_120000")
        os.makedirs(d)
        results = {"summary": {"n_trades": 2, "n_long": 2, "n_short": 0,
                               "total_pnl_dollars": 350.0,
                               "win_rate_pct": 100.0, "avg_pnl_pct": 1.7,
                               "median_days_held": 5.0, "n_symbols": 1},
                   "permutation": {"obs_pnl_dollars": 350.0,
                                   "obs_win_rate_pct": 100.0,
                                   "pnl_p": 0.2, "win_rate_p": 0.3,
                                   "n_perm": 20}}
        meta = {"run_id": "r", "input_name": "rule", "input_type":
                "trade_rule", "created_at": "2026-07-19", "git_commit": "x",
                "universe": ["AAA"], "universe_hash": "h",
                "date_range": "2024-01-02..2024-03-01", "dropped": {},
                "n_evaluations": 2, "params": {}}
        with open(os.path.join(d, "results.json"), "w") as fh:
            json.dump(results, fh)
        with open(os.path.join(d, "run_meta.json"), "w") as fh:
            json.dump(meta, fh)
        pd.DataFrame({"symbol": ["AAA", "AAA"], "side": ["long", "long"],
                      "pnl_pct": [2.0, 1.4], "pnl_dollars": [200.0, 150.0],
                      "days_held": [5, 6]}).to_parquet(
            os.path.join(d, "trades.parquet"), index=False)
        out = str(tmp_path / "rule.html")
        assert ev_report.main(["--run-dir", d, "--out", out]) == 0
        assert "rule" in open(out, encoding="utf-8").read()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_evaluation.py -k Report -v`
Expected: collection error `ModuleNotFoundError: No module named 'generate_eval_report'`.

- [ ] **Step 3: Implement `generate_eval_report.py`**

```python
r"""
generate_eval_report.py -- self-contained interactive HTML report for one
unified-evaluation run (the artifacts evaluate.py / evaluation.runner.run
writes under storage/reports/eval/<name>_<ts>/).

Reads ONLY the artifacts (results.json, run_meta.json, trades.parquet) --
never recomputes statistics -- and writes a single HTML file with embedded
Plotly.js (no server, no external requests).

Usage
-----
  C:\ProgramData\anaconda3\python.exe evaluate.py --adapter sentiment
  C:\ProgramData\anaconda3\python.exe generate_eval_report.py --latest news_sentiment
  C:\ProgramData\anaconda3\python.exe generate_eval_report.py --run-dir storage/reports/eval/news_sentiment_20260719_120000

Output: <run_dir>/report.html (override with --out)
"""

import argparse
import glob
import json
import os
import sys

import pandas as pd
import plotly.graph_objects as go

EVAL_ROOT = os.path.join("storage", "reports", "eval")

# Categorical identity (dataviz reference palette, FIXED slot order -- never
# cycled, never reassigned when a series is filtered out).
SLOT = ["#2a78d6", "#008300", "#e87ba4", "#eda100"]
# Status/state colors -- reserved for significance tiers, win/loss and
# bull/bear regime state, never for series identity.
COLOR_GOOD = "#0ca30c"
COLOR_WARNING = "#fab219"
COLOR_CRITICAL = "#d03b3b"
COLOR_MUTED = "#898781"
TIER_COLOR = {"significant": COLOR_GOOD, "weak": COLOR_WARNING,
              "noise": COLOR_MUTED}
SURFACE, PAGE = "#fcfcfb", "#f9f9f7"
INK, INK2, GRID, BASE = "#0b0b0b", "#52514e", "#e1e0d9", "#c3c2b7"


def classify_significance(mean_daily_ic, ic_t_stat) -> str:
    """Same skepticism tiers as generate_tv_rating_report.py: |IC|<0.02 or
    |t|<2 -> noise; |IC|<0.05 -> weak; else significant (leak-check band)."""
    if mean_daily_ic is None or ic_t_stat is None:
        return "noise"
    ic, t = abs(mean_daily_ic), abs(ic_t_stat)
    if ic < 0.02 or t < 2:
        return "noise"
    if ic < 0.05:
        return "weak"
    return "significant"


def find_latest(name: str, root: str = EVAL_ROOT):
    dirs = sorted(d for d in glob.glob(os.path.join(root, f"{name}_*"))
                  if os.path.isdir(d))
    return dirs[-1] if dirs else None


def load_run(run_dir: str):
    with open(os.path.join(run_dir, "results.json"), encoding="utf-8") as fh:
        results = json.load(fh)
    with open(os.path.join(run_dir, "run_meta.json"), encoding="utf-8") as fh:
        meta = json.load(fh)
    trades = None
    tp = os.path.join(run_dir, "trades.parquet")
    if os.path.exists(tp):
        trades = pd.read_parquet(tp)
    return results, meta, trades


def _layout(title: str, ytitle: str = "", height: int = 360) -> dict:
    return dict(
        title=dict(text=title, font=dict(color=INK, size=15)),
        height=height, paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        font=dict(family='system-ui, "Segoe UI", sans-serif', color=INK2,
                  size=12),
        margin=dict(l=60, r=30, t=50, b=40),
        xaxis=dict(gridcolor=GRID, linecolor=BASE, zeroline=False),
        yaxis=dict(title=ytitle, gridcolor=GRID, linecolor=BASE,
                   zeroline=True, zerolinecolor=BASE, zerolinewidth=1),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        bargap=0.35)


def _ic_by_horizon(ic: dict) -> "go.Figure | None":
    hs = sorted(int(k) for k in ic)
    if not hs:
        return None
    pooled = [ic[str(h)].get("pooled_ic") for h in hs]
    daily = [ic[str(h)].get("mean_daily_ic") for h in hs]
    fig = go.Figure()
    fig.add_bar(x=[f"{h}d" for h in hs], y=pooled, name="pooled IC",
                marker_color=SLOT[0],
                hovertemplate="pooled IC %{y:.4f}<extra>%{x}</extra>")
    fig.add_bar(x=[f"{h}d" for h in hs], y=daily, name="mean daily IC",
                marker_color=SLOT[1],
                hovertemplate="mean daily IC %{y:.4f}<extra>%{x}</extra>")
    fig.update_layout(**_layout("Spearman IC by horizon (oriented)",
                                "IC"))
    return fig


def _spread_with_ci(ic: dict, tier2: dict) -> "go.Figure | None":
    hs = sorted(int(k) for k in ic)
    if not hs:
        return None
    spread = [ic[str(h)].get("spread_pct") for h in hs]
    lo, hi = [], []
    for h in hs:
        b = tier2.get(str(h), {})
        m, l_, h_ = (b.get("spread_boot_mean_pct"),
                     b.get("spread_ci_lo_pct"), b.get("spread_ci_hi_pct"))
        s = spread[hs.index(h)]
        if None in (m, l_, h_, s):
            lo.append(None)
            hi.append(None)
        else:
            lo.append(s - l_)
            hi.append(h_ - s)
    fig = go.Figure()
    fig.add_bar(x=[f"{h}d" for h in hs], y=spread,
                name="top-bottom quintile spread", marker_color=SLOT[0],
                error_y=dict(type="data",
                             array=[v if v is not None else 0 for v in hi],
                             arrayminus=[v if v is not None else 0
                                         for v in lo],
                             color=INK2),
                hovertemplate="spread %{y:.3f}%<extra>%{x}</extra>")
    fig.update_layout(**_layout(
        "Bucket spread with bootstrap 95% CI (Tier 2)", "excess return %"))
    return fig


def _regimes(tier3: dict) -> "go.Figure | None":
    reg = tier3.get("regimes") or {}
    order = [k for k in ("bull", "bear", "high_vol", "low_vol")
             if isinstance(reg.get(k), dict)]
    if not order:
        return None
    # bull/bear are STATE -> status colors; vol regimes stay muted grays
    color = {"bull": COLOR_GOOD, "bear": COLOR_CRITICAL,
             "high_vol": COLOR_MUTED, "low_vol": BASE}
    fig = go.Figure()
    fig.add_bar(x=order, y=[reg[k].get("mean_daily_ic") for k in order],
                marker_color=[color[k] for k in order], showlegend=False,
                text=[f"n={reg[k].get('n_days')}" for k in order],
                textposition="outside",
                hovertemplate="mean daily IC %{y:.4f}<extra>%{x}</extra>")
    fig.update_layout(**_layout("Regime conditioning, 5d horizon (Tier 3)",
                                "mean daily IC"))
    return fig


def _events_fig(ev: dict) -> "go.Figure | None":
    labels = list(ev.get("labels", {}))
    if not labels:
        return None
    fig = go.Figure()
    for i, label in enumerate(labels[:4]):        # slot cap; rest in table
        d = ev["labels"][label]
        hs = sorted(int(k) for k in d.get("horizons", {}))
        fig.add_bar(x=[f"{h}d" for h in hs],
                    y=[d["horizons"][str(h)].get("edge_pct",
                       d["horizons"][str(h)].get("mean_pct"))
                       for h in hs],
                    name=f"{label} (n={d.get('n_events')})",
                    marker_color=SLOT[i],
                    hovertemplate="%{y:.3f}%<extra>%{x}</extra>")
    fig.update_layout(**_layout("Event edge vs baseline by horizon",
                                "edge %"))
    return fig


def _car_fig(ev: dict) -> "go.Figure | None":
    labels = list(ev.get("labels", {}))
    fig = go.Figure()
    added = False
    for i, label in enumerate(labels[:4]):        # same slots as the bars
        car = ev["labels"][label].get("mean_car_pct") or {}
        if not car:
            continue
        days = sorted(int(k) for k in car)
        fig.add_scatter(x=days, y=[car[str(d)] for d in days], mode="lines",
                        name=label, line=dict(color=SLOT[i], width=2),
                        hovertemplate="day %{x}: %{y:.3f}%<extra></extra>")
        added = True
    if not added:
        return None
    fig.update_layout(**_layout(
        "Mean cumulative abnormal return by relative day", "CAR %"))
    return fig


def _trades_fig(trades: "pd.DataFrame | None") -> "go.Figure | None":
    if trades is None or trades.empty:
        return None
    wins = trades[trades["pnl_pct"] > 0]["pnl_pct"]
    losses = trades[trades["pnl_pct"] <= 0]["pnl_pct"]
    fig = go.Figure()
    fig.add_histogram(x=wins, name="wins", marker_color=COLOR_GOOD,
                      nbinsx=30)
    fig.add_histogram(x=losses, name="losses", marker_color=COLOR_CRITICAL,
                      nbinsx=30)
    fig.update_layout(barmode="overlay", **_layout(
        "Realized trade P&L distribution", "trades"))
    fig.update_traces(opacity=0.85)
    return fig


def _tile(label: str, value: str, sub: str = "") -> str:
    return (f'<div style="background:{SURFACE};border:1px solid {GRID};'
            'border-radius:8px;padding:14px 18px;min-width:150px">'
            f'<div style="color:{INK2};font-size:12px">{label}</div>'
            f'<div style="color:{INK};font-size:24px;font-weight:600">'
            f'{value}</div>'
            f'<div style="color:{INK2};font-size:11px">{sub}</div></div>')


def _fmt(v, nd=3):
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _signal_tiles(results: dict, meta: dict) -> str:
    tiles = []
    ic = results.get("ic", {})
    if ic:
        best_h = max(ic, key=lambda k: abs(ic[k].get("mean_daily_ic") or 0))
        d = ic[best_h]
        tier = classify_significance(d.get("mean_daily_ic"),
                                     d.get("ic_t_stat"))
        tiles.append(_tile(f"daily IC ({best_h}d, best)",
                           _fmt(d.get("mean_daily_ic"), 4),
                           f"t={_fmt(d.get('ic_t_stat'), 2)} - "
                           f"verdict: {tier}"))
    port = results.get("portfolio") or {}
    boot = port.get("sharpe_bootstrap") or {}
    if boot.get("sharpe") is not None:
        tiles.append(_tile("portfolio Sharpe", _fmt(boot["sharpe"], 2),
                           f"95% CI [{_fmt(boot.get('sharpe_ci_lo'), 2)}, "
                           f"{_fmt(boot.get('sharpe_ci_hi'), 2)}]"))
    dsr = (results.get("tier3") or {}).get("deflated_sharpe") or {}
    if dsr.get("dsr_prob") is not None:
        tiles.append(_tile("deflated Sharpe prob", _fmt(dsr["dsr_prob"], 2),
                           f"{dsr.get('n_trials')} registry trials"))
    tiles.append(_tile("universe", str(len(meta.get("universe", []))),
                       f"{len(meta.get('dropped', {}))} dropped"))
    return ('<div style="display:flex;gap:12px;flex-wrap:wrap;'
            'margin:10px 0 18px">' + "".join(tiles) + "</div>")


def _fdr_table(results: dict) -> str:
    fdr = results.get("fdr") or []
    if not fdr:
        return ""
    rows = "".join(
        f'<tr><td>{r.get("evaluation")}</td><td>{r.get("horizon")}</td>'
        f'<td>{r.get("statistic")}</td><td>{_fmt(r.get("p"), 4)}</td>'
        f'<td>{_fmt(r.get("p_adj"), 4)}</td>'
        f'<td>{"yes" if r.get("reject") else "no"}</td></tr>'
        for r in fdr)
    return ('<h3 style="color:' + INK + '">Benjamini-Hochberg FDR '
            '(all p-values this run)</h3>'
            f'<table style="border-collapse:collapse;color:{INK2};'
            'font-size:12px"><tr><th>evaluation</th><th>horizon</th>'
            '<th>statistic</th><th>p</th><th>p_adj</th>'
            '<th>reject@10%</th></tr>' + rows + "</table>")


def _baseline_table(baselines) -> str:
    """Registry baselines for this input (read, never recomputed)."""
    if baselines is None or getattr(baselines, "empty", True):
        return ""
    keep = baselines[baselines["statistic"].isin(
        ["pooled_ic", "mean_daily_ic", "spread_pct", "sharpe"])]
    if keep.empty:
        return ""
    keep = keep.sort_values(["evaluation", "horizon", "statistic"])
    rows = "".join(
        f'<tr><td>{r.evaluation}</td><td>{r.horizon}</td>'
        f'<td>{r.statistic}</td><td>{_fmt(r.value, 4)}</td>'
        f'<td>{str(r.created_at)[:19]}</td></tr>'
        for r in keep.itertuples())
    return ('<h3 style="color:' + INK + '">Registry baselines '
            '(latest per statistic)</h3>'
            f'<table style="border-collapse:collapse;color:{INK2};'
            'font-size:12px"><tr><th>evaluation</th><th>horizon</th>'
            '<th>statistic</th><th>value</th><th>recorded</th></tr>'
            + rows + "</table>")


def build_html(results: dict, meta: dict, trades, baselines=None) -> str:
    figs = []
    if meta.get("input_type") == "signal":
        figs = [_ic_by_horizon(results.get("ic", {})),
                _spread_with_ci(results.get("ic", {}),
                                results.get("tier2", {})),
                _regimes(results.get("tier3", {}))]
    elif meta.get("input_type") == "event_set":
        figs = [_events_fig(results.get("events", {})),
                _car_fig(results.get("events", {}))]
    elif meta.get("input_type") == "trade_rule":
        figs = [_trades_fig(trades)]
    figs = [f for f in figs if f is not None]

    parts = [f'<body style="background:{PAGE};font-family:system-ui,'
             '\'Segoe UI\',sans-serif;margin:24px">',
             f'<h2 style="color:{INK}">Evaluation report: '
             f'{meta.get("input_name")}</h2>',
             f'<div style="color:{INK2};font-size:12px">'
             f'{meta.get("input_type")} - run {meta.get("run_id")} - '
             f'{meta.get("date_range")} - commit {meta.get("git_commit")} - '
             f'{meta.get("created_at")}</div>',
             _signal_tiles(results, meta)
             if meta.get("input_type") == "signal" else ""]
    for i, fig in enumerate(figs):
        parts.append(fig.to_html(full_html=False,
                                 include_plotlyjs=(i == 0)))
    if meta.get("input_type") == "trade_rule":
        s = results.get("summary", {})
        p = results.get("permutation", {})
        parts.append(_tile("trades", str(s.get("n_trades", 0)),
                           f"win rate {_fmt(s.get('win_rate_pct'), 1)}% - "
                           f"P&L ${_fmt(s.get('total_pnl_dollars'), 0)} - "
                           f"perm p={_fmt(p.get('pnl_p'), 3)}"))
    parts.append(_fdr_table(results))
    parts.append(_baseline_table(baselines))
    if meta.get("dropped"):
        drops = "; ".join(f"{k}: {v}" for k, v in
                          list(meta["dropped"].items())[:20])
        parts.append(f'<p style="color:{INK2};font-size:11px">Dropped '
                     f'symbols: {drops}</p>')
    parts.append("</body>")
    return ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>eval: {meta.get('input_name')}</title></head>"
            + "".join(parts) + "</html>")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Unified evaluation framework -- report stage")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run-dir", help="one run's artifact directory")
    g.add_argument("--latest",
                   help="newest run dir for this input name under "
                        "storage/reports/eval/")
    ap.add_argument("--out", default=None)
    ap.add_argument("--registry-path", default=None)
    args = ap.parse_args(argv)

    run_dir = args.run_dir or find_latest(args.latest)
    if not run_dir or not os.path.isdir(run_dir):
        print(f"X run directory not found: {run_dir or args.latest}")
        return 1
    if not os.path.exists(os.path.join(run_dir, "results.json")):
        print(f"X {run_dir} has no results.json -- not a run directory")
        return 1

    results, meta, trades = load_run(run_dir)
    baselines = None
    try:
        from evaluation import registry as ev_registry
        baselines = ev_registry.baselines(
            input_name=meta.get("input_name"),
            path=args.registry_path or ev_registry.REG_PATH)
    except Exception:
        baselines = None                # report still renders without it
    out = args.out or os.path.join(run_dir, "report.html")
    html = build_html(results, meta, trades, baselines=baselines)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"+ report written: {out} ({len(html) // 1024} KB, "
          f"from {run_dir})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_evaluation.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Eyeball one rendered report**

Generate a report from any test-produced or synthetic run dir and open it:

```powershell
C:\ProgramData\anaconda3\python.exe generate_eval_report.py --run-dir <a run dir produced while testing> --out storage\reports\eval_report_smoke.html
Start-Process storage\reports\eval_report_smoke.html
```

Check: no label collisions, zero-line visible on IC/spread charts, legend present, nothing overflows. (The color part was validated by construction — the palette is the dataviz reference instance.)

- [ ] **Step 6: Commit**

```bash
git add generate_eval_report.py tests/test_evaluation.py
git commit -m "feat(evaluation): self-contained Plotly report stage (generate_eval_report.py)"
```

---

### Task 12: Acceptance run — reproduce the recorded baselines + docs

No new framework code. This task runs the framework on REAL curated data and
verifies it reproduces the numbers the legacy harnesses recorded, then
documents the framework. All runs are local (curated parquet) — no API
quota is spent.

**Files:**
- Create: `docs/EVALUATION.md`
- Modify: `CLAUDE.md` (commands + architecture pointer), `work-notes/financial-data-pipeline/SESSION_NOTES_2026-07-19-eval-framework.md` (outcome)

**Recorded baselines being reproduced (tolerance: IC within +/-0.005 and the same noise/weak/significant verdicts):**
- VADER news sentiment (2026-07-07 session): pooled IC ~ +0.01, no significant horizon — verdict noise everywhere.
- TV `rating_all` (2026-07-17/18 sessions): raw ICs mildly NEGATIVE, ~ -0.005..-0.012 across horizons (mildly contrarian) — verdict noise. Adapters use `direction=1`, so the framework must report these same raw negative values.
- The 9 factor signals (8 + composite): FIRST framework baselines — record whatever comes out, null results included.

- [ ] **Step 1: Full test suite green**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/ -q`
Expected: `tests/test_evaluation.py` fully green; no NEW failures elsewhere (the working tree carries unrelated in-flight constituents work — judge against the failure set from before Task 1, recorded in the Task-1 commit message or session notes).

- [ ] **Step 2: Sentiment acceptance run**

```powershell
C:\ProgramData\anaconda3\python.exe evaluate.py --adapter sentiment --n-boot 500 --n-perm 100
```

Expected: exit 0; printed pooled_ic within [0.005, 0.015] at the horizons the 2026-07-07 eval recorded (~ +0.01); every horizon verdict noise (|daily IC| < 0.02 or |t| < 2). If outside tolerance: STOP — do not tune. Re-audit with the signal-eval leak checklist (entry timing, benchmark, join lag) and compare `storage/reports/eval/news_sentiment_*/results.json` horizon-by-horizon against `sentiment_eval.py`'s own output before touching any framework code.

- [ ] **Step 3: TV rating acceptance run**

```powershell
C:\ProgramData\anaconda3\python.exe evaluate.py --adapter rating --signal-col rating_all --n-boot 500 --n-perm 100
```

Expected: exit 0; raw pooled/daily ICs in [-0.017, 0.0] (recorded band -0.005..-0.012 +/- 0.005); verdict noise at all horizons.

- [ ] **Step 4: Factor-panel first baselines (9 runs)**

```powershell
foreach ($f in @("momentum","value","quality","low_vol","growth","short_pressure","insider_flow","sentiment","composite")) {
  C:\ProgramData\anaconda3\python.exe evaluate.py --adapter signal-panel --factor $f --n-boot 500 --n-perm 100
  if ($LASTEXITCODE -ne 0) { Write-Output "! factor $f produced no evaluations" }
}
```

Expected: runs complete (a factor with no data may exit 1 — note it, don't fail the task); registry now holds a first baseline row set per factor. Null results are valid results — record them.

- [ ] **Step 5: Event + trade-rule smoke runs**

```powershell
C:\ProgramData\anaconda3\python.exe evaluate.py --adapter rating-changes --start 2024-01-01
C:\ProgramData\anaconda3\python.exe evaluate.py --adapter tv-rule --n-perm 200
```

Expected: both exit cleanly. Sanity-check the trade counts/win rate against `tv_rating_eval.py`'s recorded trade summary (same thresholds, same engine semantics — numbers should be close; small diffs from engine boundary handling are acceptable, LARGE diffs mean a semantics bug in trades.py).

- [ ] **Step 6: Registry + report spot-check**

```powershell
C:\ProgramData\anaconda3\python.exe -c "import sys; sys.path.insert(0, '.'); from evaluation import registry; reg = registry.load(); print(len(reg), 'rows,', reg['input_name'].nunique(), 'inputs'); print(reg.groupby('input_name').size().to_string())"
C:\ProgramData\anaconda3\python.exe generate_eval_report.py --latest news_sentiment --out storage\reports\eval_report_news_sentiment.html
Start-Process storage\reports\eval_report_news_sentiment.html
```

Expected: >= 11 distinct input_names in the registry (sentiment, tv_rating_all, 9 factors, plus events/trades); report opens and reads correctly.

- [ ] **Step 7: Write `docs/EVALUATION.md`**

````markdown
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
````

- [ ] **Step 8: Update `CLAUDE.md`**

In the Commands section, after the `curated.py` line, add:

```
C:\ProgramData\anaconda3\python.exe evaluate.py --adapter sentiment   # unified eval framework (see docs/EVALUATION.md)
C:\ProgramData\anaconda3\python.exe generate_eval_report.py --latest <name>   # HTML report from eval artifacts
```

In the Architecture diagram, extend the backtest line:

```
                      └─ backtest.py (quantile portfolios) / event_backtest.py (event studies)
                           └─ evaluation/ + evaluate.py (unified eval framework: 3-tier significance battery, append-only registry — docs/EVALUATION.md)
                           └─ signal_monitor.py (maintained signal-health table, DEGRADED flags)
```

- [ ] **Step 9: Update session notes**

Append to `work-notes/financial-data-pipeline/SESSION_NOTES_2026-07-19-eval-framework.md`: plan executed
through Task 12; the acceptance numbers actually observed (sentiment IC,
TV rating IC per horizon, the 9 factor first-baseline ICs, trade-rule
summary vs legacy); any deviations from the recorded baselines and how they
were resolved; registry row count.

- [ ] **Step 10: Final commit**

```bash
git add docs/EVALUATION.md CLAUDE.md work-notes/financial-data-pipeline/SESSION_NOTES_2026-07-19-eval-framework.md
git commit -m "docs(evaluation): acceptance run reproduced recorded baselines; framework usage guide"
```

---

## Plan complete

Execution order is strictly Task 1 → 12 (each task's Consumes block names
what it needs from earlier tasks). Every task ends with `tests/test_evaluation.py`
green and a commit. Task 12 is the acceptance gate: the framework must
reproduce the recorded sentiment and TV-rating baselines within +/-0.005 IC
with the same verdicts before the plan is done.
