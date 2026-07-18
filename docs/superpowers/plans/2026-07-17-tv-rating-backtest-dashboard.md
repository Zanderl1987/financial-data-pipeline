# TV Rating Backtest + Interactive Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a two-stage tool — `tv_rating_eval.py` (statistical evaluation + trade
simulation) and `generate_tv_rating_report.py` (interactive HTML dashboard) — that
measures whether `analytics.technical.tv_rating()` predicts forward returns, and shows
the results including a rule-based long/short trade simulation.

**Architecture:** Stage 1 builds a per-symbol cache of `rating_history()` frames (all 69
`tiingo_prices` symbols, full history), then derives three outputs from that cache: (a)
level-IC stats (pooled + daily cross-sectional Spearman IC, t-stats, bucket spreads) for
`rating_all`/`rating_ma`/`rating_osc`, generalizing `sentiment_eval.py`'s method; (b) a
transition event study, reusing `event_backtest.rating_changes()` + `event_study()`
directly rather than reimplementing event-window statistics; (c) a threshold-cross
trade simulation, which is genuinely new (no existing function supports a
signal-driven exit). Stage 1 writes 4 artifact files; Stage 2 reads only those files
and never recomputes indicators, so dashboard iteration doesn't re-run the slow
36-year indicator computation.

**Tech Stack:** pandas / numpy / scipy.stats (already in use across the repo),
plotly.graph_objects for the report (already installed, v5.9.0, no new dependency).

## Global Constraints

- Python: `C:\ProgramData\anaconda3\python.exe` — always use this full path, never bare `python`.
- Run all commands from the repo root: `C:\Users\zande\PycharmProjects\financial-data-pipeline`.
- ASCII-only in any CLI print output (Windows cp1252 terminal; no `═ ▶ ✓`, use `= >> + ! X`).
- Never name a DataFrame column `year` or `month` (Hive partitioning silently shadows
  them) — not applicable here since these scripts don't write partitioned raw data, but
  keep in mind if that changes.
- Read prices via the query layer (`analytics.technical.rating_history`, which itself
  reads curated `tiingo_prices` through `query.py`) — never raw globs.
- Follow the existing codebase convention of importing `from analytics.technical import
  rating_history` **locally inside the function that calls it**, not at module top
  level (see `event_backtest.py`'s `rating_changes()`/`technical_events()`). This is
  what lets tests monkeypatch `"analytics.technical.rating_history"` the same way
  `tests/test_event_backtest.py` already does — a module-level import would bind the
  name before the patch and break that pattern.
- These are analysis/report scripts over already-curated data, not data-ingestion
  pipelines — do **not** wire them into `run_all.py`, `curated.py`, or the
  pipeline-catalog tests (`tests/test_catalog.py` / `tests/test_pipelines.py`).
- No same-day look-ahead anywhere: any action driven by day T's rating executes at day
  T+1's close at the earliest.
- Spec reference: `docs/superpowers/specs/2026-07-17-tv-rating-backtest-dashboard-design.md`.

---

### Task 1: Signal cache + return panel (`tv_rating_eval.py`, part 1)

**Files:**
- Create: `tv_rating_eval.py`
- Test: `tests/test_tv_rating_eval.py`

**Interfaces:**
- Produces: `universe(price_table=PRICE_TABLE) -> list[str]`,
  `build_signal_cache(symbols, price_table=PRICE_TABLE, start=None, end=None) ->
  dict[str, pd.DataFrame]` (keyed by symbol, each value is a `rating_history()` frame —
  columns include at least `close`, `rating_all`, `rating_ma`, `rating_osc`,
  `rating_label`, DatetimeIndex ascending), `build_return_panel(cache, horizons=HORIZONS,
  benchmark=BENCHMARK) -> pd.DataFrame` with columns `symbol, date, close, rating_all,
  rating_ma, rating_osc, rating_label, fwd_1d, fwd_3d, fwd_5d, fwd_10d, fwd_21d`.
- Module constants later tasks depend on: `SIGNALS = ("rating_all", "rating_ma",
  "rating_osc")`, `HORIZONS = (1, 3, 5, 10, 21)`, `BENCHMARK = "SPY"`, `PRICE_TABLE =
  "tiingo_prices"`, `OUT_DIR = "storage/reports/tv_rating_eval"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tv_rating_eval.py
"""
test_tv_rating_eval.py — TV rating backtest: signal cache, return panel,
level-IC stats, transition study, trade simulation. No API keys or stored
data required; analytics.technical.rating_history / event_backtest are
monkeypatched with synthetic frames.
"""

import json
import math
import os
import sys

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import tv_rating_eval as tve
import event_backtest as eb


class TestUniverseAndCache:
    def test_universe_calls_query_symbols(self, monkeypatch):
        monkeypatch.setattr(tve.q, "symbols", lambda table: ["AAPL", "MSFT"])
        assert tve.universe() == ["AAPL", "MSFT"]

    def test_cache_skips_empty_symbols(self, monkeypatch):
        good = pd.DataFrame({"close": [1.0, 2.0]},
                            index=pd.bdate_range("2024-01-01", periods=2))

        def fake_rating_history(sym, **kw):
            return good if sym == "GOOD" else pd.DataFrame()

        monkeypatch.setattr("analytics.technical.rating_history", fake_rating_history)
        cache = tve.build_signal_cache(["GOOD", "BAD"])
        assert list(cache.keys()) == ["GOOD"]


class TestReturnPanel:
    def _cache(self):
        dates = pd.bdate_range("2024-01-01", periods=7)
        x = pd.DataFrame({
            "close": [100, 101, 102, 103, 104, 105, 106],
            "rating_all": [0.6] * 7, "rating_ma": [0.5] * 7,
            "rating_osc": [0.7] * 7, "rating_label": ["strong_buy"] * 7,
        }, index=dates)
        bench = pd.DataFrame({
            "close": [200.0] * 7, "rating_all": [0.0] * 7, "rating_ma": [0.0] * 7,
            "rating_osc": [0.0] * 7, "rating_label": ["neutral"] * 7,
        }, index=dates)
        return {"X": x, "SPY": bench}

    def test_next_close_entry_and_benchmark_excluded(self):
        panel = tve.build_return_panel(self._cache(), horizons=(1, 2), benchmark="SPY")
        assert list(panel["symbol"].unique()) == ["X"]
        row0 = panel.iloc[0]
        assert row0["fwd_1d"] == pytest.approx(102 / 101 - 1.0)
        assert row0["fwd_2d"] == pytest.approx(103 / 101 - 1.0)

    def test_insufficient_future_data_is_nan(self):
        panel = tve.build_return_panel(self._cache(), horizons=(2,), benchmark="SPY")
        assert pd.isna(panel.iloc[-1]["fwd_2d"])

    def test_no_benchmark_gives_raw_return(self):
        panel = tve.build_return_panel(self._cache(), horizons=(1,), benchmark=None)
        assert panel.iloc[0]["fwd_1d"] == pytest.approx(102 / 101 - 1.0)

    def test_excess_vs_moving_benchmark(self):
        dates = pd.bdate_range("2024-01-01", periods=5)
        x = pd.DataFrame({"close": [100, 102, 104, 106, 108],
                          "rating_all": [0.6] * 5, "rating_ma": [0.5] * 5,
                          "rating_osc": [0.7] * 5, "rating_label": ["strong_buy"] * 5},
                         index=dates)
        bench = pd.DataFrame({"close": [200, 200, 202, 204, 206],
                              "rating_all": [0.0] * 5, "rating_ma": [0.0] * 5,
                              "rating_osc": [0.0] * 5, "rating_label": ["neutral"] * 5},
                             index=dates)
        panel = tve.build_return_panel({"X": x, "SPY": bench}, horizons=(1,),
                                       benchmark="SPY")
        raw = 104 / 102 - 1.0
        bench_ret = 202 / 200 - 1.0
        assert panel.iloc[0]["fwd_1d"] == pytest.approx(raw - bench_ret)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_tv_rating_eval.py -v`
Expected: FAIL / collection error (`tv_rating_eval` module doesn't exist yet).

- [ ] **Step 3: Write `tv_rating_eval.py` (part 1 — imports, constants, cache, panel)**

```python
"""
tv_rating_eval.py — statistical evaluation of the local TradingView Technical
Rating replica (analytics.technical.tv_rating) against forward returns, plus
a rule-based trade simulation.

Point-in-time safe: the rating for day T is computed from day T's own OHLCV
close (no external publication lag to model), but every simulated action
(entering/exiting a position, measuring forward returns) executes at day
T+1's close at the earliest -- never same-day, since intraday you don't yet
have the close the rating depends on.

Method
------
1. Build a per-symbol cache of analytics.technical.rating_history() frames
   (all 69 tiingo_prices symbols by default, full available history).
2. Level-IC evaluation: for rating_all/rating_ma/rating_osc, measure forward
   returns (excess vs SPY) at 1/3/5/10/21 trading days; report pooled +
   daily cross-sectional Spearman IC with t-stats, and a strong_buy vs
   strong_sell bucket spread. Same method as sentiment_eval.py, generalized
   to 3 signal columns.
3. Transition study: reuse event_backtest.rating_changes() to find every
   rating_label change, then event_backtest.event_study() per (from, to)
   pair for the average cumulative-return path and its significance.
4. Trade simulation: threshold-cross long/short rule on rating_all (see
   simulate_trades docstring), fixed $10k notional per trade.

Usage
-----
  python tv_rating_eval.py                      # full 69-symbol universe
  python tv_rating_eval.py --symbols AAPL,MSFT  # faster iteration subset
  python tv_rating_eval.py --start 2015-01-01

Output (storage/reports/tv_rating_eval/):
  ic_stats.json       -- level-IC + transition significance stats
  panel.parquet       -- symbol-day signal + forward-return panel (+ close)
  transitions.parquet -- mean cumulative-return path per transition type
  trades.parquet      -- one row per simulated trade

Interpreting: mean daily IC > ~0.02 with |t| > 2 is a real (if modest)
signal; |IC| > 0.05 on daily data is suspicious, hunt for a leak. t-stat
needs >= ~250 days to trust. Sign flips across horizons = noise.
"""

import argparse
import json
import math
import os
import sys

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import query as q
import event_backtest as eb

SIGNALS = ("rating_all", "rating_ma", "rating_osc")
HORIZONS = (1, 3, 5, 10, 21)
BENCHMARK = "SPY"
PRICE_TABLE = "tiingo_prices"
BULL_MIN = 0.5
BEAR_MAX = -0.5
EXIT_LONG_MAX = 0.1
EXIT_SHORT_MIN = -0.1
NOTIONAL = 10_000.0
OUT_DIR = "storage/reports/tv_rating_eval"


def universe(price_table: str = PRICE_TABLE) -> "list[str]":
    """All symbols available in the given price table, sorted."""
    return q.symbols(price_table)


def build_signal_cache(symbols, price_table: str = PRICE_TABLE,
                       start: "str | None" = None,
                       end: "str | None" = None) -> "dict[str, pd.DataFrame]":
    """
    One analytics.technical.rating_history() frame per symbol, keyed by
    symbol. Symbols with no usable price data are skipped.
    """
    from analytics.technical import rating_history
    cache = {}
    for sym in symbols:
        d = rating_history(sym, price_table=price_table, start=start, end=end)
        if not d.empty:
            cache[sym] = d
    return cache


def build_return_panel(cache: "dict[str, pd.DataFrame]",
                       horizons=HORIZONS,
                       benchmark: "str | None" = BENCHMARK) -> pd.DataFrame:
    """
    Tidy (symbol, date) panel: close, rating_all/rating_ma/rating_osc/
    rating_label, plus fwd_{h}d forward excess returns for each horizon.

    Entry executes at the close of the day AFTER the signal date (no
    same-day look-ahead). fwd_{h}d is the return from that entry close to
    h trading days later, excess vs `benchmark`'s matching path (reindexed
    onto the symbol's own dates). The benchmark symbol itself is excluded
    from the output panel, since its excess return vs itself is identically
    zero and would only dilute downstream stats.
    """
    bench_close = None
    if benchmark and benchmark in cache:
        bench_close = cache[benchmark]["close"]

    frames = []
    for sym, d in cache.items():
        if sym == benchmark:
            continue
        c = d["close"]
        entry = c.shift(-1)
        bench_reidx = bench_close.reindex(d.index) if bench_close is not None else None
        bench_entry = bench_reidx.shift(-1) if bench_reidx is not None else None

        out = pd.DataFrame({
            "symbol": sym,
            "date": d.index,
            "close": c.values,
            "rating_all": d["rating_all"].values,
            "rating_ma": d["rating_ma"].values,
            "rating_osc": d["rating_osc"].values,
            "rating_label": d["rating_label"].values,
        })
        for h in horizons:
            exit_ = c.shift(-(1 + h))
            ret = exit_ / entry - 1.0
            if bench_reidx is not None:
                bench_exit = bench_reidx.shift(-(1 + h))
                ret = ret - (bench_exit / bench_entry - 1.0)
            out[f"fwd_{h}d"] = ret.values
        frames.append(out)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_tv_rating_eval.py -v`
Expected: PASS (all tests in `TestUniverseAndCache` and `TestReturnPanel`).

- [ ] **Step 5: Commit**

```bash
git add tv_rating_eval.py tests/test_tv_rating_eval.py
git commit -m "Add TV rating eval: signal cache + forward-return panel"
```

---

### Task 2: Level-IC evaluation stats

**Files:**
- Modify: `tv_rating_eval.py` (append)
- Test: `tests/test_tv_rating_eval.py` (append)

**Interfaces:**
- Consumes: the return panel from Task 1 (`symbol, date, fwd_{h}d, <signal columns>`).
- Produces: `evaluate_signal(panel, signal_col, horizons=HORIZONS, min_names=5,
  bull_min=BULL_MIN, bear_max=BEAR_MAX) -> dict[int, dict]` — keyed by horizon, each
  value has keys `n, pooled_ic, pooled_p, mean_daily_ic, ic_se, ic_t_stat, ic_days,
  ic_pct_positive, bull_n, bear_n, bull_mean_pct, bear_mean_pct, spread_pct, spread_t,
  spread_p` (later keys only present when enough data exists — mirrors
  `sentiment_eval.evaluate()`'s partial-result behavior).

- [ ] **Step 1: Write the failing tests**

```python
def _synthetic_panel(n_days=30, n_syms=10, noise=0.0, seed=7):
    """Panel where fwd_1d is a monotone function of rating_all (+ noise)."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2025-01-02", periods=n_days)
    rows = []
    for d in dates:
        for i in range(n_syms):
            score = rng.uniform(-1, 1)
            rows.append({
                "symbol": f"S{i}", "date": d, "rating_all": score,
                "fwd_1d": 0.01 * score + noise * rng.normal(),
            })
    return pd.DataFrame(rows)


class TestEvaluateSignal:
    def test_recovers_positive_signal(self):
        panel = _synthetic_panel()
        res = tve.evaluate_signal(panel, "rating_all", horizons=(1,))
        assert 1 in res
        r = res[1]
        assert r["pooled_ic"] > 0.9
        assert r["mean_daily_ic"] > 0.9
        assert r["ic_days"] == 30
        assert r["spread_pct"] > 0
        # noise=0.0 makes fwd_1d an exact positive-scalar multiple of rating_all,
        # so every single day's Spearman rho is exactly 1.0 -- zero cross-day
        # variance, so ic_se/ic_t_stat are correctly None (same sd>0 guard
        # sentiment_eval.evaluate() already uses for its own t-stat).
        assert r["ic_se"] is None

    def test_ic_se_positive_with_noisy_signal(self):
        # noise=0.05 breaks the exact-rho-1.0-every-day degeneracy above, so
        # ic_se's sd>0 branch is actually exercised and produces a real value.
        panel = _synthetic_panel(noise=0.05)
        res = tve.evaluate_signal(panel, "rating_all", horizons=(1,))
        r = res[1]
        assert r["ic_se"] is not None
        assert r["ic_se"] > 0

    def test_insufficient_rows_skipped(self):
        panel = _synthetic_panel(n_days=1, n_syms=5)
        res = tve.evaluate_signal(panel, "rating_all", horizons=(1,))
        assert res == {} or "mean_daily_ic" not in res.get(1, {})

    def test_daily_ic_withheld_below_min_names(self):
        panel = _synthetic_panel(n_days=30, n_syms=3)
        res = tve.evaluate_signal(panel, "rating_all", horizons=(1,), min_names=5)
        assert "mean_daily_ic" not in res[1]

    def test_missing_horizon_column_ignored(self):
        panel = _synthetic_panel()
        res = tve.evaluate_signal(panel, "rating_all", horizons=(1, 21))
        assert 1 in res and 21 not in res

    def test_works_on_any_signal_column_name(self):
        panel = _synthetic_panel().rename(columns={"rating_all": "rating_osc"})
        res = tve.evaluate_signal(panel, "rating_osc", horizons=(1,))
        assert res[1]["pooled_ic"] > 0.9
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_tv_rating_eval.py::TestEvaluateSignal -v`
Expected: FAIL (`AttributeError: module 'tv_rating_eval' has no attribute 'evaluate_signal'`).

- [ ] **Step 3: Append `evaluate_signal` to `tv_rating_eval.py`**

```python
def evaluate_signal(panel: pd.DataFrame, signal_col: str, horizons=HORIZONS,
                    min_names: int = 5, bull_min: float = BULL_MIN,
                    bear_max: float = BEAR_MAX) -> dict:
    """
    Pooled + daily cross-sectional IC and a bullish/bearish bucket spread
    for one signal column, at each horizon. Same method as
    sentiment_eval.evaluate(), generalized to an arbitrary signal column
    and configurable bull/bear thresholds (default +-0.5, TV's own
    strong_buy/strong_sell cutoffs, since rating_all/ma/osc share the
    [-1, 1] scale).
    """
    out = {}
    for h in horizons:
        col = f"fwd_{h}d"
        if col not in panel.columns:
            continue
        sub = panel.dropna(subset=[col, signal_col])
        if len(sub) < 10:
            continue
        res = {"n": len(sub)}

        rho, p = stats.spearmanr(sub[signal_col], sub[col])
        res["pooled_ic"] = round(float(rho), 4)
        res["pooled_p"] = round(float(p), 4)

        ics = []
        for _, day in sub.groupby("date"):
            if day["symbol"].nunique() >= min_names and day[signal_col].nunique() > 1:
                r, _ = stats.spearmanr(day[signal_col], day[col])
                if np.isfinite(r):
                    ics.append(r)
        if len(ics) >= 5:
            ics = np.array(ics)
            sd = ics.std(ddof=1)
            se = sd / math.sqrt(len(ics))
            res["mean_daily_ic"] = round(float(ics.mean()), 4)
            res["ic_se"] = round(float(se), 5) if sd > 0 else None
            res["ic_t_stat"] = round(float(ics.mean() / se), 2) if sd > 0 else None
            res["ic_days"] = len(ics)
            res["ic_pct_positive"] = round(100 * float((ics > 0).mean()), 1)

        bull = sub.loc[sub[signal_col] >= bull_min, col]
        bear = sub.loc[sub[signal_col] <= bear_max, col]
        res["bull_n"], res["bear_n"] = len(bull), len(bear)
        res["bull_mean_pct"] = round(100 * float(bull.mean()), 3) if len(bull) else None
        res["bear_mean_pct"] = round(100 * float(bear.mean()), 3) if len(bear) else None
        if len(bull) > 5 and len(bear) > 5:
            spread = float(bull.mean() - bear.mean())
            t, p2 = stats.ttest_ind(bull, bear, equal_var=False)
            res["spread_pct"] = round(100 * spread, 3)
            res["spread_t"] = round(float(t), 2)
            res["spread_p"] = round(float(p2), 4)
        out[h] = res
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_tv_rating_eval.py::TestEvaluateSignal -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add tv_rating_eval.py tests/test_tv_rating_eval.py
git commit -m "Add level-IC evaluation stats to tv_rating_eval"
```

---

### Task 3: Transition event study

**Files:**
- Modify: `tv_rating_eval.py` (append)
- Test: `tests/test_tv_rating_eval.py` (append)

**Interfaces:**
- Consumes: `event_backtest.rating_changes(symbols, start, end, price_table) ->
  DataFrame[symbol, date, from_label, to_label, from_score, to_score, step,
  direction]`; `event_backtest.event_study(events, window, benchmark, entry_lag,
  price_table) -> EventStudyResult` (has `.mean_car: pd.Series[rel_day -> float]` and
  `.horizons: pd.DataFrame` indexed by `horizon_days` with columns `n, mean_pct,
  median_pct, hit_rate_pct, t_stat, baseline_pct, edge_pct`).
- Produces: `run_transition_study(symbols, start="1990-01-01", end=None,
  benchmark=BENCHMARK, window=(0, 21), entry_lag=1, min_events=5, price_table=
  PRICE_TABLE) -> tuple[pd.DataFrame, dict]` — `paths` has columns `from_label,
  to_label, rel_day, mean_car_pct, n`; `summary` is `{"from->to": {horizon_str:
  {...horizons row...}}}`.

**Gotcha to be aware of:** `event_backtest.rating_changes()` has a "latest mode" that
activates when `date`, `start`, AND `end` are all `None` — it then returns only each
symbol's most recent transition, not full history. `run_transition_study` must always
pass an explicit `start` (default `"1990-01-01"`, not `None`) to avoid silently
falling into that mode.

- [ ] **Step 1: Write the failing test**

```python
class TestTransitionStudy:
    def test_skips_groups_below_min_events(self, monkeypatch):
        changes = pd.DataFrame({
            "symbol": ["A", "B", "C"],
            "date": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]),
            "from_label": ["neutral", "neutral", "buy"],
            "to_label": ["buy", "buy", "strong_buy"],
            "from_score": [0.0, 0.0, 0.3], "to_score": [0.3, 0.3, 0.6],
            "step": [1, 1, 1], "direction": ["upgrade"] * 3,
        })
        monkeypatch.setattr(tve.eb, "rating_changes", lambda *a, **k: changes)

        called = {}

        def fake_event_study(events, **kw):
            called["n"] = len(events)
            return eb.EventStudyResult(
                car=pd.DataFrame(), mean_car=pd.Series({0: 0.0, 21: 0.01}),
                horizons=pd.DataFrame({"n": [len(events)], "mean_pct": [1.0],
                                      "median_pct": [1.0], "hit_rate_pct": [60.0],
                                      "t_stat": [2.5], "baseline_pct": [0.5],
                                      "edge_pct": [0.5]}, index=[21]),
                events=events, baseline=pd.Series(dtype=float), params={})

        monkeypatch.setattr(tve.eb, "event_study", fake_event_study)

        paths, summary = tve.run_transition_study(["A", "B", "C"], min_events=2)
        assert called["n"] == 2                       # only neutral->buy qualifies
        assert "neutral->buy" in summary
        assert "buy->strong_buy" not in summary        # only 1 event, below min_events
        assert set(paths["from_label"]) == {"neutral"}

    def test_empty_changes_returns_empty(self, monkeypatch):
        monkeypatch.setattr(tve.eb, "rating_changes",
                            lambda *a, **k: pd.DataFrame(columns=eb._CHANGE_COLS))
        paths, summary = tve.run_transition_study(["A"])
        assert paths.empty
        assert summary == {}

    def test_always_passes_explicit_start(self, monkeypatch):
        captured = {}

        def fake_rating_changes(symbols, start=None, end=None, price_table=None):
            captured["start"] = start
            return pd.DataFrame(columns=eb._CHANGE_COLS)

        monkeypatch.setattr(tve.eb, "rating_changes", fake_rating_changes)
        tve.run_transition_study(["A"])
        assert captured["start"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_tv_rating_eval.py::TestTransitionStudy -v`
Expected: FAIL (`AttributeError: ... has no attribute 'run_transition_study'`).

- [ ] **Step 3: Append `run_transition_study` to `tv_rating_eval.py`**

```python
def run_transition_study(symbols, start: str = "1990-01-01",
                         end: "str | None" = None,
                         benchmark: "str | None" = BENCHMARK,
                         window: "tuple[int, int]" = (0, 21),
                         entry_lag: int = 1, min_events: int = 5,
                         price_table: str = PRICE_TABLE):
    """
    For every distinct (from_label, to_label) rating transition seen across
    `symbols`' full history, compute the average cumulative-return path via
    event_backtest.event_study(). Transition types with fewer than
    `min_events` occurrences are skipped (too little data to trust a mean).

    `start` defaults to "1990-01-01" (NOT None) deliberately:
    event_backtest.rating_changes() only scans full history when at least
    one of date/start/end is given: passing all-None triggers its
    "latest transition only" mode, which would silently return one row per
    symbol instead of the full transition history this study needs.

    Returns (paths, summary):
      paths   -- tidy DataFrame: from_label, to_label, rel_day, mean_car_pct, n
      summary -- {"from_label->to_label": {horizon_str: {...event_study
                 horizons row...}}}
    """
    changes = eb.rating_changes(symbols, start=start, end=end, price_table=price_table)
    path_rows = []
    summary = {}
    if changes.empty:
        return pd.DataFrame(columns=["from_label", "to_label", "rel_day",
                                     "mean_car_pct", "n"]), summary

    for (frm, to), grp in changes.groupby(["from_label", "to_label"]):
        if len(grp) < min_events:
            continue
        res = eb.event_study(grp[["symbol", "date"]], window=window,
                             benchmark=benchmark, entry_lag=entry_lag,
                             price_table=price_table)
        key = f"{frm}->{to}"
        for rel_day, val in res.mean_car.items():
            path_rows.append({"from_label": frm, "to_label": to,
                              "rel_day": int(rel_day),
                              "mean_car_pct": round(100 * float(val), 3),
                              "n": res.n_events})
        summary[key] = {str(h): row.to_dict() for h, row in res.horizons.iterrows()}

    paths = pd.DataFrame(path_rows, columns=["from_label", "to_label", "rel_day",
                                             "mean_car_pct", "n"])
    return paths, summary
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_tv_rating_eval.py::TestTransitionStudy -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add tv_rating_eval.py tests/test_tv_rating_eval.py
git commit -m "Add rating-transition event study to tv_rating_eval"
```

---

### Task 4: Trade simulation

**Files:**
- Modify: `tv_rating_eval.py` (append)
- Test: `tests/test_tv_rating_eval.py` (append)

**Interfaces:**
- Consumes: the signal cache from Task 1 (`dict[str, pd.DataFrame]` with `rating_all`,
  `close` columns).
- Produces: `simulate_trades(cache, notional=NOTIONAL) -> pd.DataFrame` with columns
  `symbol, side, entry_signal_date, entry_date, entry_price, exit_signal_date,
  exit_date, exit_price, days_held, pnl_dollars, pnl_pct`.

- [ ] **Step 1: Write the failing tests**

```python
class TestSimulateTrades:
    def _cache(self):
        dates = pd.bdate_range("2024-01-01", periods=12)
        rating = [0.0, 0.6, 0.6, 0.05, 0.05, -0.6, -0.6, -0.6, -0.05, -0.05, 0.0, 0.0]
        close = [100, 101, 102, 103, 104, 105, 90, 91, 92, 93, 94, 95]
        d = pd.DataFrame({"rating_all": rating, "close": close}, index=dates)
        return {"X": d}, dates

    def test_long_and_short_trade_pnl(self):
        cache, dates = self._cache()
        trades = tve.simulate_trades(cache)
        assert len(trades) == 2

        long_t = trades.iloc[0]
        assert long_t["side"] == "long"
        assert long_t["entry_price"] == 102
        assert long_t["exit_price"] == 104
        assert long_t["days_held"] == 2
        assert long_t["pnl_pct"] == pytest.approx(100 * (104 / 102 - 1), abs=1e-3)
        assert long_t["pnl_dollars"] == pytest.approx(10000 * (104 / 102 - 1), abs=0.5)

        short_t = trades.iloc[1]
        assert short_t["side"] == "short"
        assert short_t["entry_price"] == 90
        assert short_t["exit_price"] == 93
        assert short_t["pnl_pct"] == pytest.approx(100 * (1 - 93 / 90), abs=1e-3)
        assert short_t["pnl_dollars"] < 0

    def test_entry_executes_next_close_not_same_day(self):
        cache, dates = self._cache()
        trades = tve.simulate_trades(cache)
        assert trades.iloc[0]["entry_signal_date"] == dates[1]
        assert trades.iloc[0]["entry_date"] == dates[2]

    def test_signal_while_in_position_is_ignored(self):
        dates = pd.bdate_range("2024-01-01", periods=6)
        rating = [0.0, 0.6, 0.4, 0.6, 0.05, 0.05]   # re-crosses 0.5 while still long
        close = [100, 101, 102, 103, 104, 105]
        cache = {"X": pd.DataFrame({"rating_all": rating, "close": close}, index=dates)}
        trades = tve.simulate_trades(cache)
        assert len(trades) == 1

    def test_unresolved_position_produces_no_trade(self):
        dates = pd.bdate_range("2024-01-01", periods=5)
        rating = [0.0, 0.6, 0.6, 0.6, 0.6]           # never drops back below 0.1
        close = [100, 101, 102, 103, 104]
        cache = {"X": pd.DataFrame({"rating_all": rating, "close": close}, index=dates)}
        trades = tve.simulate_trades(cache)
        assert trades.empty
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_tv_rating_eval.py::TestSimulateTrades -v`
Expected: FAIL (`AttributeError: ... has no attribute 'simulate_trades'`).

- [ ] **Step 3: Append `simulate_trades` (and its crossing helpers) to `tv_rating_eval.py`**

```python
def _crossed_up(s: pd.Series, level: float) -> pd.Series:
    return (s >= level) & (s.shift(1) < level)


def _crossed_down(s: pd.Series, level: float) -> pd.Series:
    return (s <= level) & (s.shift(1) > level)


_TRADE_COLS = ["symbol", "side", "entry_signal_date", "entry_date", "entry_price",
              "exit_signal_date", "exit_date", "exit_price", "days_held",
              "pnl_dollars", "pnl_pct"]


def simulate_trades(cache: "dict[str, pd.DataFrame]",
                    notional: float = NOTIONAL) -> pd.DataFrame:
    """
    Rule-based long/short simulation on rating_all, one position per symbol
    at a time (no pyramiding; an entry signal while already in a position
    is ignored -- a position only closes via its own exit condition, and a
    new entry cannot start before the day after the previous trade's exit
    execution):

      long entry:  rating_all crosses UP through BULL_MIN (+0.5)
      long exit:   first later day rating_all < EXIT_LONG_MAX (+0.1)
      short entry: rating_all crosses DOWN through BEAR_MAX (-0.5)
      short exit:  first later day rating_all > EXIT_SHORT_MIN (-0.1)

    Both entry and exit execute at the NEXT trading day's close after the
    signal is observed (no same-day action). A position with no qualifying
    exit before the data ends is dropped (still open, not a realized P&L) --
    and blocks any further entries for that symbol, since it's still
    (unrealizedly) open.

    Returns one row per realized trade -- see _TRADE_COLS.
    """
    rows = []
    for sym, d in cache.items():
        rating = d["rating_all"]
        close = d["close"]
        n = len(d)

        long_entries = _crossed_up(rating, BULL_MIN).to_numpy()
        short_entries = _crossed_down(rating, BEAR_MAX).to_numpy()
        exit_long_cond = (rating < EXIT_LONG_MAX).to_numpy()
        exit_short_cond = (rating > EXIT_SHORT_MIN).to_numpy()

        entry_positions = sorted(
            [(i, "long") for i in np.flatnonzero(long_entries)] +
            [(i, "short") for i in np.flatnonzero(short_entries)]
        )

        next_free = 0
        for sig_i, side in entry_positions:
            if sig_i < next_free:
                continue                       # already in a position
            entry_i = sig_i + 1
            if entry_i >= n:
                continue                       # no next close to enter at
            entry_price = close.iloc[entry_i]
            if not np.isfinite(entry_price) or entry_price <= 0:
                continue

            exit_cond = exit_long_cond if side == "long" else exit_short_cond
            exit_sig_i = None
            for j in range(entry_i + 1, n):
                if exit_cond[j]:
                    exit_sig_i = j
                    break
            if exit_sig_i is None:
                next_free = n                  # rest of history: still open
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
                "symbol": sym, "side": side,
                "entry_signal_date": d.index[sig_i], "entry_date": d.index[entry_i],
                "entry_price": float(entry_price),
                "exit_signal_date": d.index[exit_sig_i], "exit_date": d.index[exit_i],
                "exit_price": float(exit_price), "days_held": exit_i - entry_i,
                "pnl_dollars": round(notional * pct, 2), "pnl_pct": round(100 * pct, 3),
            })
            next_free = exit_i + 1

    return pd.DataFrame(rows, columns=_TRADE_COLS)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_tv_rating_eval.py::TestSimulateTrades -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add tv_rating_eval.py tests/test_tv_rating_eval.py
git commit -m "Add threshold-cross trade simulation to tv_rating_eval"
```

---

### Task 5: CLI wiring + output artifacts

**Files:**
- Modify: `tv_rating_eval.py` (append `main()`)
- Test: `tests/test_tv_rating_eval.py` (append)

**Interfaces:**
- Consumes: everything from Tasks 1-4.
- Produces: `main()` (CLI entry point); writes `ic_stats.json`, `panel.parquet`,
  `transitions.parquet`, `trades.parquet` under `OUT_DIR`.

- [ ] **Step 1: Write the failing test**

```python
class TestMainArtifacts:
    def test_writes_all_four_artifacts(self, tmp_path, monkeypatch):
        dates = pd.bdate_range("2024-01-01", periods=40)
        rng = np.random.default_rng(1)

        def make(seed, drift):
            close = 100 * np.exp(np.cumsum(rng.normal(drift, 0.01, 40)))
            rating = np.clip(np.cumsum(rng.normal(0, 0.1, 40)), -1, 1)
            label = pd.cut(rating, bins=[-1.01, -0.5, -0.1, 0.1, 0.5, 1.01],
                           labels=["strong_sell", "sell", "neutral", "buy", "strong_buy"])
            return pd.DataFrame({"close": close, "rating_all": rating,
                                 "rating_ma": rating, "rating_osc": rating,
                                 "rating_label": label}, index=dates)

        fake_cache = {"AAPL": make(1, 0.001), "MSFT": make(2, -0.001),
                     "SPY": make(3, 0.0005)}
        monkeypatch.setattr(tve, "universe", lambda: ["AAPL", "MSFT", "SPY"])
        monkeypatch.setattr(tve, "build_signal_cache", lambda *a, **k: fake_cache)
        monkeypatch.setattr(tve.eb, "rating_changes",
                            lambda *a, **k: pd.DataFrame(columns=eb._CHANGE_COLS))
        monkeypatch.setattr(tve, "OUT_DIR", str(tmp_path))
        monkeypatch.setattr(sys, "argv", ["tv_rating_eval.py"])

        tve.main()

        for fname in ("ic_stats.json", "panel.parquet", "transitions.parquet",
                     "trades.parquet"):
            assert (tmp_path / fname).exists(), fname
        with open(tmp_path / "ic_stats.json") as f:
            stats_json = json.load(f)
        assert "level_ic" in stats_json
        assert "transition_stats" in stats_json
```

- [ ] **Step 2: Run test to verify it fails**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_tv_rating_eval.py::TestMainArtifacts -v`
Expected: FAIL (`AttributeError: ... has no attribute 'main'`).

- [ ] **Step 3: Append `main()` to `tv_rating_eval.py`**

```python
def main():
    parser = argparse.ArgumentParser(
        description="Evaluate the TV rating replica vs forward returns")
    parser.add_argument("--symbols", default=None,
                        help="Comma-separated symbol subset (default: full "
                             "tiingo_prices universe)")
    parser.add_argument("--start", default="1990-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--benchmark", default=BENCHMARK)
    parser.add_argument("--min-names", type=int, default=5)
    args = parser.parse_args()

    syms = args.symbols.split(",") if args.symbols else universe()
    print(f"[tv_rating_eval] building signal cache for {len(syms)} symbols...")
    cache = build_signal_cache(syms, start=args.start, end=args.end)
    print(f"  {len(cache)} symbols had usable price history")
    if not cache:
        print("No usable symbols. Check price_table / date range.")
        return

    print("[tv_rating_eval] building return panel...")
    panel = build_return_panel(cache, benchmark=args.benchmark)
    print(f"  {len(panel):,} symbol-day rows")

    print("[tv_rating_eval] level-IC evaluation...")
    level_ic = {sig: evaluate_signal(panel, sig, min_names=args.min_names)
               for sig in SIGNALS}
    for sig, results in level_ic.items():
        print(f"\n=== {sig} ===")
        hdr = (f"{'h':>3} {'n':>6} {'pooledIC':>9} {'p':>7} {'dailyIC':>8} "
              f"{'t':>6} {'days':>5} {'bull%':>7} {'bear%':>7} {'spread%':>8} {'t':>6}")
        print(hdr)
        for h, r in results.items():
            print(f"{h:>3} {r['n']:>6} {r.get('pooled_ic', float('nan')):>9} "
                  f"{r.get('pooled_p', float('nan')):>7} "
                  f"{str(r.get('mean_daily_ic', '-')):>8} {str(r.get('ic_t_stat', '-')):>6} "
                  f"{str(r.get('ic_days', '-')):>5} "
                  f"{str(r.get('bull_mean_pct', '-')):>7} {str(r.get('bear_mean_pct', '-')):>7} "
                  f"{str(r.get('spread_pct', '-')):>8} {str(r.get('spread_t', '-')):>6}")

    print("\n[tv_rating_eval] transition study...")
    paths, transition_summary = run_transition_study(
        syms, start=args.start, end=args.end, benchmark=args.benchmark)
    print(f"  {len(transition_summary)} transition types qualified")

    print("[tv_rating_eval] trade simulation...")
    trades = simulate_trades(cache)
    if len(trades):
        win_rate = 100 * (trades["pnl_dollars"] > 0).mean()
        print(f"  {len(trades)} realized trades | win rate {win_rate:.1f}%")
    else:
        print("  0 realized trades")

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "ic_stats.json"), "w") as f:
        json.dump({"level_ic": level_ic, "transition_stats": transition_summary}, f,
                  indent=2, default=str)
    panel.to_parquet(os.path.join(OUT_DIR, "panel.parquet"), index=False)
    paths.to_parquet(os.path.join(OUT_DIR, "transitions.parquet"), index=False)
    trades.to_parquet(os.path.join(OUT_DIR, "trades.parquet"), index=False)
    print(f"\n-> wrote artifacts to {OUT_DIR}/")

    print("\nGuide: |IC| < 0.02 = noise; 0.02-0.05 weak-but-real if t>=2; "
         ">0.05 on daily data is suspicious (hunt for a leak). Need t-stat >= 2 "
         "across >= ~250 days to call anything significant. Sign flips across "
         "horizons = noise, not momentum-then-reversal.")


if __name__ == "__main__":
    main()
```

**Note:** this step references `OUT_DIR` as a module global at call time (not
captured as a default argument), so the test's `monkeypatch.setattr(tve, "OUT_DIR",
...)` takes effect — do not rewrite `main()` to capture `OUT_DIR` into a local default
parameter value, which would bind the original path at function-definition time.

- [ ] **Step 4: Run tests to verify they pass**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_tv_rating_eval.py -v`
Expected: PASS (all tests in the file, ~18 tests total across Tasks 1-5).

- [ ] **Step 5: Commit**

```bash
git add tv_rating_eval.py tests/test_tv_rating_eval.py
git commit -m "Wire tv_rating_eval CLI + output artifacts"
```

---

### Task 6: Report data loading + pure helper functions

**Files:**
- Create: `generate_tv_rating_report.py`
- Test: `tests/test_generate_tv_rating_report.py`

**Interfaces:**
- Consumes: the 4 artifact files Task 5 writes (`ic_stats.json` shape:
  `{"level_ic": {signal: {horizon_str: {...evaluate_signal stats...}}}, "transition_stats":
  {...}}`; `panel.parquet` shape from Task 1; `trades.parquet` shape from Task 4).
- Produces: `load_artifacts(out_dir=tve.OUT_DIR) -> tuple[dict, pd.DataFrame,
  pd.DataFrame, pd.DataFrame]` (ic_stats, panel, transitions, trades);
  `classify_significance(mean_daily_ic, ic_t_stat) -> str` (`"noise" | "weak" |
  "significant"`); `build_headline_rows(ic_stats) -> list[dict]`; `build_symbol_table(
  panel, signal="rating_all", horizons=tve.HORIZONS) -> pd.DataFrame` (columns
  `symbol, n_signals, best_horizon, best_ic, worst_horizon, worst_ic`).
- Module constants later tasks depend on (validated dataviz palette — see
  `docs/superpowers/specs/2026-07-17-tv-rating-backtest-dashboard-design.md` and the
  `dataviz` skill for why these specific hex values / role assignments were chosen):
  `COLOR_SERIES = {"rating_all": "#2a78d6", "rating_ma": "#008300", "rating_osc":
  "#e87ba4"}` (categorical identity, fixed order), `COLOR_GOOD = "#0ca30c"`,
  `COLOR_WARNING = "#fab219"`, `COLOR_CRITICAL = "#d03b3b"`, `COLOR_MUTED = "#898781"`
  (status/state colors — good/warning reserved for significance tiers; good/critical
  reused for win/loss and bull/bear sign encoding, never for identity),
  `TIER_COLOR = {"significant": COLOR_GOOD, "weak": COLOR_WARNING, "noise": COLOR_MUTED}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_generate_tv_rating_report.py
"""
test_generate_tv_rating_report.py — TV rating dashboard report builder.
Pure data-prep/classification functions are unit tested directly; chart
builders (Task 7) are tested for structural correctness (trace counts,
visibility arrays), not pixel output; assemble_report (Task 8) is tested
end-to-end against synthetic artifacts written to tmp_path.
"""

import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import generate_tv_rating_report as gr


class TestClassifySignificance:
    def test_noise_below_ic_floor(self):
        assert gr.classify_significance(0.01, 3.0) == "noise"

    def test_noise_below_t_floor(self):
        assert gr.classify_significance(0.03, 1.5) == "noise"

    def test_weak_band(self):
        assert gr.classify_significance(0.03, 2.5) == "weak"

    def test_significant_band(self):
        assert gr.classify_significance(0.07, 3.0) == "significant"

    def test_none_inputs_are_noise(self):
        assert gr.classify_significance(None, None) == "noise"


class TestHeadlineRows:
    def test_builds_rows_from_nested_json(self):
        ic_stats = {"level_ic": {"rating_all": {"1": {
            "n": 100, "pooled_ic": 0.05, "pooled_p": 0.01, "mean_daily_ic": 0.04,
            "ic_t_stat": 2.5, "ic_se": 0.016, "ic_days": 300,
            "spread_pct": 1.2, "spread_t": 2.1}}}}
        rows = gr.build_headline_rows(ic_stats)
        assert len(rows) == 1
        assert rows[0]["signal"] == "rating_all"
        assert rows[0]["horizon"] == 1
        assert rows[0]["tier"] == "weak"


class TestSymbolTable:
    def test_best_worst_horizon_identified(self):
        # NOTE: fwd_1d/fwd_5d must NOT both be clean positive-scalar multiples
        # of rating_all -- Spearman rho is scale-invariant, so two such columns
        # tie at rho=1.0 exactly and "best horizon" becomes undecidable. fwd_1d
        # gets heavy noise (weak relation); fwd_5d stays a clean transform
        # (rho=1.0) so the two are unambiguously, deterministically different.
        dates = pd.bdate_range("2024-01-01", periods=60)
        rng = np.random.default_rng(3)
        signal = np.linspace(-1, 1, 60)
        panel = pd.DataFrame({
            "symbol": "X", "date": dates, "rating_all": signal,
            "fwd_1d": signal * 0.001 + rng.normal(0, 0.5, 60),  # weak relation
            "fwd_5d": signal * 0.05,                            # strong relation
        })
        out = gr.build_symbol_table(panel, signal="rating_all", horizons=(1, 5))
        row = out.iloc[0]
        assert row["symbol"] == "X"
        assert row["best_horizon"] == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_generate_tv_rating_report.py -v`
Expected: FAIL (collection error — `generate_tv_rating_report` doesn't exist yet).

- [ ] **Step 3: Write `generate_tv_rating_report.py` (part 1)**

```python
"""
generate_tv_rating_report.py — self-contained interactive HTML dashboard for
the tv_rating_eval.py backtest results.

Reads ONLY the artifacts tv_rating_eval.py writes (storage/reports/tv_rating_eval/)
-- never recomputes indicators -- and writes a single HTML file with embedded
Plotly.js (no server, no external requests).

Usage
-----
  python tv_rating_eval.py                    # (once) produce the artifacts
  python generate_tv_rating_report.py         # build the report from them

Output: storage/reports/tv_rating_backtest.html
"""

import json
import os
import sys

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from scipy import stats

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tv_rating_eval as tve

SIGNALS = tve.SIGNALS
HORIZONS = tve.HORIZONS

# Categorical identity (fixed order, dataviz reference palette slots 1/2/3).
COLOR_SERIES = {"rating_all": "#2a78d6", "rating_ma": "#008300", "rating_osc": "#e87ba4"}
# Status/state colors -- reserved for significance tiers and win/loss/bull-bear
# sign, never used for series identity.
COLOR_GOOD = "#0ca30c"
COLOR_WARNING = "#fab219"
COLOR_CRITICAL = "#d03b3b"
COLOR_MUTED = "#898781"
TIER_COLOR = {"significant": COLOR_GOOD, "weak": COLOR_WARNING, "noise": COLOR_MUTED}


def load_artifacts(out_dir: "str | None" = None):
    out_dir = out_dir or tve.OUT_DIR
    with open(os.path.join(out_dir, "ic_stats.json")) as f:
        ic_stats = json.load(f)
    panel = pd.read_parquet(os.path.join(out_dir, "panel.parquet"))
    transitions = pd.read_parquet(os.path.join(out_dir, "transitions.parquet"))
    trades = pd.read_parquet(os.path.join(out_dir, "trades.parquet"))
    return ic_stats, panel, transitions, trades


def classify_significance(mean_daily_ic, ic_t_stat) -> str:
    """
    Skepticism-default tiers (see signal-eval skill / how-to-read panel):
    |IC| < 0.02 or |t| < 2 -> noise; 0.02 <= |IC| < 0.05 with |t| >= 2 -> weak;
    |IC| >= 0.05 with |t| >= 2 -> significant (report text flags this band as
    worth a leak check, not an automatic celebration).
    """
    if mean_daily_ic is None or ic_t_stat is None:
        return "noise"
    ic, t = abs(mean_daily_ic), abs(ic_t_stat)
    if ic < 0.02 or t < 2:
        return "noise"
    if ic < 0.05:
        return "weak"
    return "significant"


def build_headline_rows(ic_stats: dict) -> "list[dict]":
    rows = []
    for signal, by_h in ic_stats.get("level_ic", {}).items():
        for h_str, r in by_h.items():
            tier = classify_significance(r.get("mean_daily_ic"), r.get("ic_t_stat"))
            rows.append({
                "signal": signal, "horizon": int(h_str), "n": r.get("n"),
                "pooled_ic": r.get("pooled_ic"), "pooled_p": r.get("pooled_p"),
                "mean_daily_ic": r.get("mean_daily_ic"),
                "ic_t_stat": r.get("ic_t_stat"), "ic_days": r.get("ic_days"),
                "spread_pct": r.get("spread_pct"), "spread_t": r.get("spread_t"),
                "tier": tier,
            })
    return sorted(rows, key=lambda r: (r["signal"], r["horizon"]))


def build_symbol_table(panel: pd.DataFrame, signal: str = "rating_all",
                       horizons=HORIZONS) -> pd.DataFrame:
    """Per-symbol pooled IC at each horizon; reports the best/worst horizon."""
    rows = []
    for sym, grp in panel.groupby("symbol"):
        ics = {}
        for h in horizons:
            col = f"fwd_{h}d"
            if col not in grp.columns:
                continue
            sub = grp.dropna(subset=[col, signal])
            if len(sub) >= 10:
                rho, _ = stats.spearmanr(sub[signal], sub[col])
                if np.isfinite(rho):
                    ics[h] = rho
        if not ics:
            continue
        best_h = max(ics, key=ics.get)
        worst_h = min(ics, key=ics.get)
        rows.append({
            "symbol": sym, "n_signals": len(grp),
            "best_horizon": best_h, "best_ic": round(ics[best_h], 4),
            "worst_horizon": worst_h, "worst_ic": round(ics[worst_h], 4),
        })
    return pd.DataFrame(rows).sort_values("best_ic", ascending=False).reset_index(drop=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_generate_tv_rating_report.py -v`
Expected: PASS (`TestClassifySignificance`, `TestHeadlineRows`, `TestSymbolTable`).

- [ ] **Step 5: Commit**

```bash
git add generate_tv_rating_report.py tests/test_generate_tv_rating_report.py
git commit -m "Add report data-loading + significance/symbol-table helpers"
```

---

### Task 7: Chart builders

**Files:**
- Modify: `generate_tv_rating_report.py` (append)
- Test: `tests/test_generate_tv_rating_report.py` (append)

**Interfaces:**
- Consumes: `ic_stats` dict, `panel` DataFrame, `transitions` DataFrame, `trades`
  DataFrame (all from Task 6/Task 1-5 shapes); `COLOR_SERIES`/`COLOR_GOOD`/
  `COLOR_CRITICAL`/`COLOR_MUTED` from Task 6.
- Produces: `build_ic_bar_chart(ic_stats) -> go.Figure`, `build_spread_chart(ic_stats)
  -> go.Figure`, `build_scatter_section(panel, signals=SIGNALS, horizons=HORIZONS,
  sample_n=5000, seed=42) -> go.Figure`, `build_transition_chart(transitions_df) ->
  go.Figure`, `build_price_trades_chart(panel, trades, symbols=None) -> go.Figure`,
  `build_cumulative_pnl_chart(trades) -> go.Figure`. Each of these is consumed by
  `assemble_report()` in Task 8.

- [ ] **Step 1: Write the failing tests**

```python
class TestICBarChart:
    def test_three_signal_traces(self):
        ic_stats = {"level_ic": {sig: {"1": {"mean_daily_ic": 0.03, "ic_t_stat": 2.0,
                    "ic_se": 0.015, "ic_days": 100}} for sig in gr.COLOR_SERIES}}
        fig = gr.build_ic_bar_chart(ic_stats)
        assert len(fig.data) == 3
        assert {tr.name for tr in fig.data} == set(gr.COLOR_SERIES)


class TestSpreadChart:
    def test_three_subplots_no_legend(self):
        ic_stats = {"level_ic": {sig: {"1": {"spread_pct": 0.5}}
                    for sig in gr.COLOR_SERIES}}
        fig = gr.build_spread_chart(ic_stats)
        assert len(fig.data) == 3
        assert all(tr.showlegend is False for tr in fig.data)


class TestScatterSection:
    def test_dropdown_has_one_button_per_combo(self):
        dates = pd.bdate_range("2024-01-01", periods=20)
        panel = pd.DataFrame({
            "symbol": "X", "date": dates, "rating_all": np.linspace(-1, 1, 20),
            "rating_ma": np.linspace(-1, 1, 20), "rating_osc": np.linspace(-1, 1, 20),
            **{f"fwd_{h}d": np.linspace(-0.05, 0.05, 20) for h in gr.HORIZONS},
        })
        fig = gr.build_scatter_section(panel)
        assert len(fig.data) == len(gr.SIGNALS) * len(gr.HORIZONS)
        assert len(fig.layout.updatemenus[0].buttons) == len(gr.SIGNALS) * len(gr.HORIZONS)
        assert fig.data[0].visible is True
        assert fig.data[1].visible is False


class TestPriceTradesChart:
    def test_visibility_toggles_per_symbol(self):
        dates = pd.bdate_range("2024-01-01", periods=5)
        panel = pd.DataFrame({"symbol": ["A"] * 5 + ["B"] * 5,
                              "date": list(dates) * 2,
                              "close": [10, 11, 12, 13, 14, 20, 21, 22, 23, 24]})
        trades = pd.DataFrame(columns=["symbol", "side", "entry_date", "entry_price",
                                       "exit_date", "exit_price", "pnl_dollars", "pnl_pct"])
        fig = gr.build_price_trades_chart(panel, trades, symbols=["A", "B"])
        assert len(fig.data) == 10          # 5 traces x 2 symbols
        assert len(fig.layout.updatemenus[0].buttons) == 2
        vis0 = fig.layout.updatemenus[0].buttons[0].args[0]["visible"]
        assert vis0 == [True] * 5 + [False] * 5


class TestCumulativePnlChart:
    def test_cumulative_sum_matches_manual(self):
        trades = pd.DataFrame({
            "symbol": ["A", "B"], "side": ["long", "short"],
            "exit_date": pd.to_datetime(["2024-01-05", "2024-01-03"]),
            "pnl_dollars": [200.0, -50.0], "pnl_pct": [2.0, -0.5],
        })
        fig = gr.build_cumulative_pnl_chart(trades)
        y = list(fig.data[0].y)
        assert y == [-50.0, 150.0]     # sorted by exit_date: B(-50) then A(+200)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_generate_tv_rating_report.py -v`
Expected: FAIL (`AttributeError` for each missing `build_*` function).

- [ ] **Step 3: Append chart builders to `generate_tv_rating_report.py`**

```python
def build_ic_bar_chart(ic_stats: dict) -> go.Figure:
    fig = go.Figure()
    for signal, color in COLOR_SERIES.items():
        by_h = ic_stats.get("level_ic", {}).get(signal, {})
        hs = sorted((int(h) for h in by_h), key=int)
        y = [by_h[str(h)].get("mean_daily_ic") for h in hs]
        err = [by_h[str(h)].get("ic_se") or 0 for h in hs]
        fig.add_trace(go.Bar(name=signal, x=[f"{h}d" for h in hs], y=y,
                             error_y=dict(type="data", array=err, visible=True),
                             marker_color=color))
    fig.update_layout(barmode="group", title="Mean daily cross-sectional IC by horizon",
                      yaxis_title="Mean daily IC", template="plotly_white",
                      legend_title_text="Signal")
    return fig


def build_spread_chart(ic_stats: dict) -> go.Figure:
    """
    Small multiples (one subplot per signal) rather than grouped bars colored
    by sign -- keeps signal identity (the subplot title) and bull/bear sign
    (bar color) as two separate encodings instead of overloading one color
    channel with both identity and state.
    """
    from plotly.subplots import make_subplots

    signals = list(COLOR_SERIES)
    horizons_all = sorted({int(h) for sig in ic_stats.get("level_ic", {}).values()
                          for h in sig})
    fig = make_subplots(rows=1, cols=len(signals), subplot_titles=signals,
                        shared_yaxes=True)
    for i, signal in enumerate(signals, start=1):
        by_h = ic_stats.get("level_ic", {}).get(signal, {})
        y = [by_h.get(str(h), {}).get("spread_pct") for h in horizons_all]
        colors = [COLOR_GOOD if (v or 0) >= 0 else COLOR_CRITICAL for v in y]
        fig.add_trace(go.Bar(x=[f"{h}d" for h in horizons_all], y=y,
                             marker_color=colors, showlegend=False, name=signal),
                      row=1, col=i)
    fig.update_layout(title="Bullish minus bearish mean excess return by horizon",
                      template="plotly_white")
    fig.update_yaxes(title_text="Spread (%)", row=1, col=1)
    return fig


def build_scatter_section(panel: pd.DataFrame, signals=SIGNALS, horizons=HORIZONS,
                          sample_n: int = 5000, seed: int = 42) -> go.Figure:
    """
    Rating-vs-forward-return scatter with a single flat "signal @ horizon"
    dropdown (15 options for 3 signals x 5 horizons) -- not two independent
    dropdowns, since Plotly updatemenu buttons fully replace visibility state
    and two independently-stateful dropdowns can't be combined without
    custom JS. Each combo is downsampled to `sample_n` points (deterministic)
    for render performance; panel.parquet retains full data.
    """
    rng = np.random.default_rng(seed)
    combos = [(sig, h) for sig in signals for h in horizons]
    fig = go.Figure()
    for i, (sig, h) in enumerate(combos):
        col = f"fwd_{h}d"
        sub = panel.dropna(subset=[sig, col])
        if len(sub) > sample_n:
            sub = sub.iloc[rng.choice(len(sub), sample_n, replace=False)]
        fig.add_trace(go.Scattergl(
            x=sub[sig], y=100 * sub[col], mode="markers",
            marker=dict(size=5, color=COLOR_SERIES[sig], opacity=0.4),
            text=sub["symbol"].astype(str) + " " + sub["date"].astype(str),
            hovertemplate="%{text}<br>signal=%{x:.3f}<br>fwd return=%{y:.2f}%<extra></extra>",
            visible=(i == 0), showlegend=False, name=f"{sig} @ {h}d"))

    buttons = []
    for i, (sig, h) in enumerate(combos):
        vis = [j == i for j in range(len(combos))]
        buttons.append(dict(label=f"{sig} @ {h}d", method="update",
                            args=[{"visible": vis},
                                  {"xaxis.title.text": sig,
                                   "yaxis.title.text": f"Forward {h}d excess return (%)"}]))
    fig.update_layout(
        updatemenus=[dict(buttons=buttons, x=0, y=1.15, xanchor="left")],
        title="Rating level vs forward excess return",
        xaxis_title=combos[0][0],
        yaxis_title=f"Forward {combos[0][1]}d excess return (%)",
        template="plotly_white")
    return fig


def build_transition_chart(transitions_df: pd.DataFrame) -> go.Figure:
    """One line per transition type; toggle via the default Plotly legend
    click behavior (no custom JS needed for a same-y-scale multi-line toggle)."""
    fig = go.Figure()
    if transitions_df.empty:
        fig.update_layout(title="Rating-transition event study (no qualifying transitions)")
        return fig
    for (frm, to), grp in transitions_df.groupby(["from_label", "to_label"]):
        grp = grp.sort_values("rel_day")
        fig.add_trace(go.Scatter(
            x=grp["rel_day"], y=grp["mean_car_pct"], mode="lines",
            name=f"{frm} -> {to}  (n={int(grp['n'].iloc[0])})"))
    fig.update_layout(title="Average cumulative return after a rating transition "
                            "(click legend entries to toggle)",
                      xaxis_title="Trading days after transition",
                      yaxis_title="Mean cumulative excess return (%)",
                      template="plotly_white", hovermode="x unified")
    fig.add_hline(y=0, line_color=COLOR_MUTED, line_width=1)
    return fig


def build_price_trades_chart(panel: pd.DataFrame, trades: pd.DataFrame,
                             symbols: "list[str] | None" = None) -> go.Figure:
    """
    Symbol dropdown (one option per symbol) over a fixed 5-trace-per-symbol
    layout: price line, win-entry markers, win-exit markers, loss-entry
    markers, loss-exit markers -- kept fixed-width (even when a symbol has
    zero wins or losses) so the dropdown's visibility-array indexing stays
    simple and correct regardless of trade counts. Direction is shape
    (triangle-up=long, triangle-down=short); outcome is color (state, not
    identity: COLOR_GOOD/COLOR_CRITICAL).
    """
    symbols = symbols or sorted(panel["symbol"].unique())
    traces_per_symbol = 5
    fig = go.Figure()
    for i, sym in enumerate(symbols):
        p = panel[panel["symbol"] == sym].sort_values("date")
        fig.add_trace(go.Scatter(x=p["date"], y=p["close"], mode="lines",
                                 line=dict(color="#52514e", width=1.5),
                                 name=f"{sym} price", visible=(i == 0),
                                 showlegend=False))
        t = trades[trades["symbol"] == sym] if not trades.empty else trades
        wins = t[t["pnl_dollars"] > 0] if len(t) else t
        losses = t[t["pnl_dollars"] <= 0] if len(t) else t
        for side_df, tag in ((wins, "win"), (losses, "loss")):
            color = COLOR_GOOD if tag == "win" else COLOR_CRITICAL
            shapes = side_df["side"].map({"long": "triangle-up",
                                          "short": "triangle-down"}) if len(side_df) else []
            fig.add_trace(go.Scatter(
                x=side_df["entry_date"], y=side_df["entry_price"], mode="markers",
                marker=dict(symbol=list(shapes), size=11, color=color,
                           line=dict(width=1, color="#0b0b0b")),
                name=f"{sym} entry ({tag})", visible=(i == 0), showlegend=False,
                hovertemplate="entry %{x}<br>$%{y:.2f}<extra></extra>"))
            fig.add_trace(go.Scatter(
                x=side_df["exit_date"], y=side_df["exit_price"], mode="markers",
                marker=dict(symbol="x", size=9, color=color,
                           line=dict(width=1, color="#0b0b0b")),
                name=f"{sym} exit ({tag})", visible=(i == 0), showlegend=False,
                hovertemplate="exit %{x}<br>$%{y:.2f}<extra></extra>"))

    buttons = []
    for i, sym in enumerate(symbols):
        vis = [False] * (len(symbols) * traces_per_symbol)
        for j in range(traces_per_symbol):
            vis[i * traces_per_symbol + j] = True
        buttons.append(dict(label=sym, method="update", args=[{"visible": vis}]))

    fig.update_layout(
        updatemenus=[dict(buttons=buttons, x=0, y=1.15, xanchor="left")],
        title=f"Price with simulated trades -- {symbols[0] if symbols else ''}",
        yaxis_title="Price ($)", template="plotly_white")
    return fig


def build_cumulative_pnl_chart(trades: pd.DataFrame) -> go.Figure:
    if trades.empty:
        fig = go.Figure()
        fig.update_layout(title="Cumulative realized P&L (no trades)")
        return fig
    t = trades.sort_values("exit_date").reset_index(drop=True)
    t["cum_pnl"] = t["pnl_dollars"].cumsum()
    colors = [COLOR_GOOD if v > 0 else COLOR_CRITICAL for v in t["pnl_dollars"]]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=t["exit_date"], y=t["cum_pnl"], mode="lines+markers",
        line=dict(color="#2a78d6", width=2),
        marker=dict(size=7, color=colors, line=dict(width=1, color="#0b0b0b")),
        text=[f"{r.symbol} {r.side}: ${r.pnl_dollars:,.0f} ({r.pnl_pct:+.2f}%)"
             for r in t.itertuples()],
        hovertemplate="%{x}<br>%{text}<br>cumulative: $%{y:,.0f}<extra></extra>",
        name="Cumulative realized P&L"))

    show_annotations = len(t) <= 200
    annotations = [dict(x=r.exit_date, y=r.cum_pnl,
                        text=f"${r.pnl_dollars:,.0f} ({r.pnl_pct:+.1f}%)",
                        showarrow=True, arrowhead=2, ax=0, ay=-30,
                        font=dict(size=9, color="#52514e"))
                  for r in t.itertuples()] if show_annotations else []
    fig.update_layout(
        title="Cumulative realized P&L -- sum of independently-sized $10k trades "
             "(not a capital-constrained portfolio curve)",
        yaxis_title="Cumulative P&L ($)", template="plotly_white",
        annotations=annotations, hovermode="closest")
    if not show_annotations:
        fig.add_annotation(text="Per-trade $ / % labels hidden above 200 trades -- "
                                "hover each point for its P&L.",
                           xref="paper", yref="paper", x=0, y=1.08, showarrow=False,
                           font=dict(size=11, color=COLOR_MUTED))
    return fig
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_generate_tv_rating_report.py -v`
Expected: PASS (all chart-builder tests).

- [ ] **Step 5: Commit**

```bash
git add generate_tv_rating_report.py tests/test_generate_tv_rating_report.py
git commit -m "Add chart builders for TV rating dashboard"
```

---

### Task 8: Report assembly + CLI

**Files:**
- Modify: `generate_tv_rating_report.py` (append)
- Test: `tests/test_generate_tv_rating_report.py` (append)

**Interfaces:**
- Consumes: `load_artifacts`, `build_headline_rows`, `build_symbol_table` (Task 6);
  all `build_*_chart`/`build_scatter_section` functions (Task 7).
- Produces: `build_headline_table(ic_stats) -> go.Figure`, `assemble_report(out_dir=
  None, report_path="storage/reports/tv_rating_backtest.html") -> str` (returns the
  written path), `main()` (CLI entry point).

- [ ] **Step 1: Write the failing test**

```python
class TestAssembleReport:
    def test_writes_html_file_with_expected_sections(self, tmp_path):
        out_dir = tmp_path / "artifacts"
        out_dir.mkdir()
        ic_stats = {"level_ic": {sig: {"1": {
            "n": 50, "pooled_ic": 0.03, "pooled_p": 0.02, "mean_daily_ic": 0.025,
            "ic_t_stat": 2.2, "ic_se": 0.011, "ic_days": 40,
            "spread_pct": 0.4, "spread_t": 1.8}} for sig in gr.COLOR_SERIES},
            "transition_stats": {}}
        with open(out_dir / "ic_stats.json", "w") as f:
            json.dump(ic_stats, f)

        dates = pd.bdate_range("2024-01-01", periods=10)
        panel = pd.DataFrame({
            "symbol": "AAPL", "date": dates, "close": np.linspace(100, 110, 10),
            "rating_all": np.linspace(-1, 1, 10), "rating_ma": np.linspace(-1, 1, 10),
            "rating_osc": np.linspace(-1, 1, 10),
            **{f"fwd_{h}d": np.linspace(-0.02, 0.02, 10) for h in gr.HORIZONS},
        })
        panel.to_parquet(out_dir / "panel.parquet", index=False)
        pd.DataFrame(columns=["from_label", "to_label", "rel_day", "mean_car_pct", "n"]
                    ).to_parquet(out_dir / "transitions.parquet", index=False)
        pd.DataFrame(columns=["symbol", "side", "entry_date", "entry_price",
                              "exit_date", "exit_price", "pnl_dollars", "pnl_pct"]
                    ).to_parquet(out_dir / "trades.parquet", index=False)

        report_path = tmp_path / "report.html"
        path = gr.assemble_report(str(out_dir), str(report_path))
        content = report_path.read_text(encoding="utf-8")

        assert path == str(report_path)
        assert "TradingView Rating Backtest" in content
        assert "How to read this report" in content
        assert content.lower().count("plotly") > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_generate_tv_rating_report.py::TestAssembleReport -v`
Expected: FAIL (`AttributeError: ... has no attribute 'assemble_report'`).

- [ ] **Step 3: Append `build_headline_table`, `HOW_TO_READ`, `assemble_report`, `main` to `generate_tv_rating_report.py`**

```python
def build_headline_table(ic_stats: dict) -> go.Figure:
    rows = build_headline_rows(ic_stats)
    header = ["Signal", "Horizon", "n", "Pooled IC", "Pooled p", "Daily IC",
             "IC t-stat", "IC days", "Spread %", "Spread t"]
    if rows:
        cols = list(zip(*[[r["signal"], f"{r['horizon']}d", r["n"], r["pooled_ic"],
                          r["pooled_p"], r["mean_daily_ic"], r["ic_t_stat"],
                          r["ic_days"], r["spread_pct"], r["spread_t"]]
                         for r in rows]))
        fill_colors = [TIER_COLOR[r["tier"]] for r in rows]
    else:
        cols = [[] for _ in header]
        fill_colors = []

    fig = go.Figure(data=[go.Table(
        header=dict(values=header, fill_color="#e1e0d9", align="left"),
        cells=dict(values=cols,
                  fill_color=[["#fcfcfb"] * len(rows)] * (len(header) - 1) + [fill_colors],
                  align="left"))])
    fig.update_layout(title="Headline IC / significance by signal x horizon "
                            "(Spread t column: grey=noise, yellow=weak, green=significant)")
    return fig


HOW_TO_READ = """
<div style="max-width:900px;margin:24px auto;padding:16px;
           border:1px solid #e1e0d9;border-radius:8px;
           font-family:system-ui,-apple-system,'Segoe UI',sans-serif;
           color:#52514e;">
<h3 style="color:#0b0b0b;margin-top:0;">How to read this report</h3>
<ul>
  <li><b>|IC| &lt; 0.02</b> is noise. <b>0.02-0.05</b> is weak-but-real only if the
      t-stat also holds (|t| &ge; 2). <b>|IC| &gt; 0.05</b> on daily data is
      suspicious -- before celebrating, re-check for a look-ahead leak.</li>
  <li>A t-stat needs roughly <b>250+ days</b> of daily IC observations before
      it's trustworthy, and this report tests 3 signals x 5 horizons x 2 stats
      -- one marginal t of about 2 among ~30 numbers is expected by chance alone.</li>
  <li><b>Sign flips across horizons</b> (e.g. positive at 1 day, negative at 3
      days) mean the signal is noise, not "momentum then reversal," unless that
      flip was predicted before looking.</li>
  <li>Universe is a fixed 69-symbol list (mega-caps + sector/bond/commodity
      ETFs) -- not a broad-market sample. Returns are excess vs SPY.</li>
  <li>The cumulative P&amp;L chart sums independently-sized $10,000 trades in
      exit-date order -- it is <b>not</b> a capital-constrained portfolio
      equity curve, since trades can overlap in time.</li>
</ul>
</div>
"""


def assemble_report(out_dir: "str | None" = None,
                    report_path: str = "storage/reports/tv_rating_backtest.html") -> str:
    ic_stats, panel, transitions, trades = load_artifacts(out_dir)
    symbols = sorted(panel["symbol"].unique())

    symbol_table = build_symbol_table(panel)
    table_fig = go.Figure(data=[go.Table(
        header=dict(values=list(symbol_table.columns), fill_color="#e1e0d9", align="left"),
        cells=dict(values=[symbol_table[c] for c in symbol_table.columns],
                  fill_color="#fcfcfb", align="left"))])
    table_fig.update_layout(title="Per-symbol best/worst horizon IC (rating_all)")

    figs = [
        build_headline_table(ic_stats),
        build_ic_bar_chart(ic_stats),
        build_spread_chart(ic_stats),
        build_scatter_section(panel),
        build_transition_chart(transitions),
        table_fig,
        build_price_trades_chart(panel, trades, symbols),
        build_cumulative_pnl_chart(trades),
    ]

    html_parts = ['<html><head><title>TV Rating Backtest</title></head><body>',
                 '<h1 style="font-family:system-ui,sans-serif;">'
                 'TradingView Rating Backtest</h1>']
    for i, fig in enumerate(figs):
        html_parts.append(fig.to_html(full_html=False, include_plotlyjs=(i == 0)))
    html_parts.append(HOW_TO_READ)
    html_parts.append("</body></html>")

    os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(html_parts))
    return report_path


def main():
    parser = argparse.ArgumentParser(description="Build the TV rating backtest HTML report")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--report-path", default="storage/reports/tv_rating_backtest.html")
    args = parser.parse_args()
    path = assemble_report(args.out_dir, args.report_path)
    print(f"-> wrote {path}")


if __name__ == "__main__":
    main()
```

**Note:** add `import argparse` to the existing import block at the top of
`generate_tv_rating_report.py` (needed by `main()`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_generate_tv_rating_report.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add generate_tv_rating_report.py tests/test_generate_tv_rating_report.py
git commit -m "Assemble TV rating dashboard HTML report + CLI"
```

---

### Task 9: End-to-end verification against real data

**Files:** none (verification only — no code changes expected unless a real-data bug
surfaces, in which case fix it in the relevant file from Tasks 1-8 and re-run this task).

- [ ] **Step 1: Run the full test suite**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/ -v`
Expected: PASS (existing 273+ tests, plus the new tests from Tasks 1-8).

- [ ] **Step 2: Run the compute stage against real data**

Run: `"C:\ProgramData\anaconda3\python.exe" tv_rating_eval.py`
Expected: prints the per-signal IC tables, transition-study count, and trade-simulation
summary; exits 0; creates `storage/reports/tv_rating_eval/{ic_stats.json,panel.parquet,
transitions.parquet,trades.parquet}`.

- [ ] **Step 3: Spot-check one symbol's trade log against its own price data**

Run:
```
"C:\ProgramData\anaconda3\python.exe" -c "
import pandas as pd
trades = pd.read_parquet('storage/reports/tv_rating_eval/trades.parquet')
aapl = trades[trades['symbol'] == 'AAPL']
print(aapl.to_string())
"
```
Manually verify a couple of rows: `entry_date` should be a trading day strictly after
`entry_signal_date`; `exit_date` strictly after `exit_signal_date`; `pnl_dollars`
consistent with `10000 * (exit_price/entry_price - 1)` (long) or the mirrored short
formula.

- [ ] **Step 4: Run the report stage and open it in a real browser**

Run: `"C:\ProgramData\anaconda3\python.exe" generate_tv_rating_report.py`
Then open `storage/reports/tv_rating_backtest.html` in a browser (e.g. via `! start
storage/reports/tv_rating_backtest.html` from the session, or open the file directly).
Confirm: headline table renders with color-coded significance cells; IC bar chart and
spread subplots render; the scatter's 15-option dropdown switches signal/horizon and
redraws; the transition chart's legend toggles lines on click; the price+trades
symbol dropdown switches symbols and redraws price line + markers; the cumulative
P&L chart shows a running total with hoverable per-trade detail. Report back any
chart that fails to render or a dropdown that doesn't redraw — do not declare this
step done without having actually opened the file and interacted with each control.

- [ ] **Step 5: Walk the point-in-time checklist explicitly**

Confirm and note the result of each:
- Join lag: n/a (rating computed from the same day's own close, no external
  publication lag to model).
- Entry timing: next trading day's close, confirmed in `build_return_panel` (Task 1)
  and `simulate_trades` (Task 4) — both shift by `+1` before reading a price.
- Excess vs benchmark: SPY, confirmed in `build_return_panel`'s benchmark subtraction
  and `run_transition_study`'s `benchmark=BENCHMARK` pass-through.
- Universe caveat: stated in the report's "How to read this report" panel (Task 8).

- [ ] **Step 6: Record the outcome**

Add a short entry to a new `SESSION_NOTES_<today's date>.md` (or append to the current
day's session notes if one already exists) noting: whether the level-IC results showed
anything above the noise floor for any signal/horizon, how many transition types
qualified, how many realized trades the simulation produced and its aggregate P&L, and
any real-data surprise found during Step 4's manual browser check. This is a
reportable result even if the finding is "no significant IC anywhere" — a null result
here is valid and worth recording (see the signal-eval skill's guidance on recording
baselines, not just positive findings).
