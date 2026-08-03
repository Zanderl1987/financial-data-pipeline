# Interactive Backtest Explorer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `backtest_app.py`, a live Dash app that lets a user pick an evaluated signal from the registry, drag threshold sliders to re-run the `tv_threshold` trade rule live, and explore the resulting trades on a per-symbol price chart and a cumulative P&L chart -- all on top of the existing, already-tested `evaluation/` engine (no new backtest math).

**Architecture:** One new root-level file, `backtest_app.py`, split internally into (a) pure functions that load registry/artifact data, build a parameterized `TradeRule`, run `evaluation.trades.simulate()`, and build Plotly figures, and (b) a thin Dash layout + callback layer that wires those pure functions to UI events. The pure functions are unit-tested directly with pytest; the Dash wiring gets one layout-construction smoke test plus a manual browser verification pass (no Selenium, per spec).

**Tech Stack:** Dash (Plotly), reusing `evaluation.trades.simulate`, `evaluation.contracts.TradeRule`, `evaluation.adapters.rating_cache`, `evaluation.registry`, and `generate_eval_report.find_latest` / `load_run`.

## Global Constraints

- Python: always invoke via the full path `C:\ProgramData\anaconda3\python.exe` -- bare `python` is a broken MS Store stub on this machine.
- ASCII-only output in any script's print statements (Windows cp1252 terminal). Use `= >> + ! X`, never Unicode box/check glyphs.
- Never name a DataFrame column `year` or `month` (Hive partition-column shadowing).
- `backtest_app.py` is an analysis tool over already-curated/already-evaluated data -- it must NOT be wired into `run_all.py`, `curated.py`, or any pipeline-catalog test.
- Never read prices via raw file globs -- go through the existing query layer / already-loaded caches only (this app never touches raw storage directly; it consumes `evaluation.adapters.rating_cache()`, which already does this correctly).
- No new backtest/statistics logic: every number in this app must come from `evaluation.trades.simulate()` / `trade_summary()` or from artifacts already written by `evaluate.py`.

---

## Task 1: Add and verify the Dash dependency

**Files:**
- Modify: `requirements.txt:33` (after the `plotly==5.9.0` line)

**Interfaces:**
- Produces: a working `import dash` / `from dash import dcc, html, Input, Output` in the installed environment, which every later task depends on.

- [ ] **Step 1: Install dash and note the resolved version**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pip install dash`

Read the resolved version from the install output (e.g. `Successfully installed dash-2.18.2 ...`).

- [ ] **Step 2: Pin the resolved version in requirements.txt**

Edit `requirements.txt`, adding a new line immediately after the `plotly==5.9.0` line (line 33):

```
dash==2.18.2                   # backtest_app.py (live interactive explorer)
```

(Use whatever exact version pip actually resolved in Step 1 -- do not guess ahead of the install output.)

- [ ] **Step 3: Verify the import**

Run: `"C:\ProgramData\anaconda3\python.exe" -c "import dash; from dash import dcc, html, Input, Output; print('ok', dash.__version__)"`

Expected: prints `ok <version>` with no traceback.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "Add dash dependency for the interactive backtest explorer"
```

---

## Task 2: Backfill tests for generate_eval_report.py's reused functions

`backtest_app.py` will call `generate_eval_report.find_latest` and `generate_eval_report.load_run` directly, and neither currently has any test coverage (confirmed: no `tests/test_generate_eval_report.py` exists). Write regression tests for exactly the functions this app depends on before building on top of them. Because these functions are pre-existing and already correct, each test is expected to **pass on first run** -- there is no red/green cycle here, just a coverage backfill. Say so explicitly when running each test the first time.

**Files:**
- Create: `tests/test_generate_eval_report.py`

**Interfaces:**
- Consumes: `generate_eval_report.find_latest(name, root=EVAL_ROOT) -> str | None`, `generate_eval_report.load_run(run_dir) -> (results: dict, meta: dict, trades: pd.DataFrame | None)`, `generate_eval_report.classify_significance(mean_daily_ic, ic_t_stat) -> str`.

- [ ] **Step 1: Write the test file**

```python
"""
tests/test_generate_eval_report.py -- coverage backfill for the functions
backtest_app.py depends on directly: find_latest, load_run,
classify_significance. generate_eval_report.py itself is unchanged and
already working in production (it produces the static HTML report); these
tests exist because nothing exercised these functions before, not because
anything was broken.
"""

import json
import os
import sys

import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import generate_eval_report as ger


class TestFindLatest:
    def test_picks_newest_by_sorted_suffix(self, tmp_path):
        root = tmp_path / "eval"
        root.mkdir()
        (root / "sig_20260101_000000").mkdir()
        (root / "sig_20260301_000000").mkdir()
        (root / "sig_20260215_000000").mkdir()
        assert ger.find_latest("sig", root=str(root)) == str(
            root / "sig_20260301_000000")

    def test_returns_none_when_no_match(self, tmp_path):
        root = tmp_path / "eval"
        root.mkdir()
        assert ger.find_latest("missing_signal", root=str(root)) is None

    def test_ignores_files_only_matches_dirs(self, tmp_path):
        root = tmp_path / "eval"
        root.mkdir()
        (root / "sig_20260101_000000").write_text("not a dir")
        assert ger.find_latest("sig", root=str(root)) is None


class TestLoadRun:
    def _make_run_dir(self, tmp_path, with_trades=True):
        run_dir = tmp_path / "sig_20260101_000000"
        run_dir.mkdir()
        with open(run_dir / "results.json", "w", encoding="utf-8") as fh:
            json.dump({"summary": {"n_trades": 2}}, fh)
        with open(run_dir / "run_meta.json", "w", encoding="utf-8") as fh:
            json.dump({"input_name": "sig", "input_type": "trade_rule"}, fh)
        if with_trades:
            pd.DataFrame({"symbol": ["AAPL"], "pnl_dollars": [10.0]}).to_parquet(
                run_dir / "trades.parquet", index=False)
        return str(run_dir)

    def test_loads_results_and_meta(self, tmp_path):
        run_dir = self._make_run_dir(tmp_path)
        results, meta, trades = ger.load_run(run_dir)
        assert results == {"summary": {"n_trades": 2}}
        assert meta == {"input_name": "sig", "input_type": "trade_rule"}
        assert list(trades["symbol"]) == ["AAPL"]

    def test_trades_none_when_no_trades_parquet(self, tmp_path):
        run_dir = self._make_run_dir(tmp_path, with_trades=False)
        _, _, trades = ger.load_run(run_dir)
        assert trades is None


class TestClassifySignificance:
    def test_none_inputs_are_noise(self):
        assert ger.classify_significance(None, None) == "noise"

    def test_low_ic_or_low_t_is_noise(self):
        assert ger.classify_significance(0.01, 5.0) == "noise"
        assert ger.classify_significance(0.03, 1.0) == "noise"

    def test_mid_ic_is_weak(self):
        assert ger.classify_significance(0.03, 3.0) == "weak"

    def test_high_ic_is_significant(self):
        assert ger.classify_significance(0.08, 4.0) == "significant"
```

- [ ] **Step 2: Run the tests and confirm they pass immediately**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_generate_eval_report.py -v`
Expected: all tests PASS (this backfills coverage for existing, already-correct code -- no implementation change needed).

- [ ] **Step 3: Commit**

```bash
git add tests/test_generate_eval_report.py
git commit -m "Backfill tests for generate_eval_report's find_latest/load_run/classify_significance"
```

---

## Task 3: Module skeleton + list_evaluated_signals()

**Files:**
- Create: `backtest_app.py`
- Test: `tests/test_backtest_app.py`

**Interfaces:**
- Consumes: `evaluation.registry.load(path=REG_PATH) -> pd.DataFrame` (columns include `input_name`), `generate_eval_report.find_latest(name)`.
- Produces: `list_evaluated_signals() -> list[dict]`, each dict `{"name": str, "has_local_artifacts": bool}`, sorted by name. Later tasks (layout) consume this exact shape.

- [ ] **Step 1: Write the failing test**

```python
"""
tests/test_backtest_app.py -- unit tests for backtest_app.py's pure logic
(registry/artifact loading, live trade-rule simulation, chart data prep).
Dash callback wiring itself is smoke-tested (layout construction only) --
no Selenium/browser harness, per docs/superpowers/specs/
2026-08-03-interactive-backtest-explorer-design.md.
"""

import os
import sys

import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import backtest_app as ba


class TestListEvaluatedSignals:
    def test_empty_registry_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(ba.ev_registry, "load",
                            lambda path=None: pd.DataFrame(columns=ba.ev_registry.COLUMNS))
        assert ba.list_evaluated_signals() == []

    def test_lists_unique_sorted_names_with_artifact_flag(self, monkeypatch):
        reg = pd.DataFrame({"input_name": ["tv_threshold", "factor_value",
                                           "tv_threshold"]})
        monkeypatch.setattr(ba.ev_registry, "load", lambda path=None: reg)
        monkeypatch.setattr(ba, "find_latest",
                            lambda name: "/some/dir" if name == "tv_threshold" else None)
        assert ba.list_evaluated_signals() == [
            {"name": "factor_value", "has_local_artifacts": False},
            {"name": "tv_threshold", "has_local_artifacts": True},
        ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_backtest_app.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backtest_app'`

- [ ] **Step 3: Write minimal implementation**

```python
r"""
backtest_app.py -- live interactive explorer for the unified evaluation
framework's results. Reads registry + run artifacts, and for signals with a
recognized TradeRule shape, re-runs evaluation.trades.simulate() live as the
user drags threshold sliders -- no new backtest math, everything here
delegates to evaluation/ and generate_eval_report's existing loaders.

See docs/superpowers/specs/2026-08-03-interactive-backtest-explorer-design.md.

Usage
-----
  C:\ProgramData\anaconda3\python.exe backtest_app.py
  (opens http://127.0.0.1:8050)
"""

import dash
from dash import dcc, html, Input, Output
import plotly.graph_objects as go

from evaluation import registry as ev_registry
from evaluation import trades as ev_trades
from evaluation import adapters as ev_adapters
from evaluation.contracts import TradeRule
from generate_eval_report import find_latest, load_run

# Signals whose TradeRule shape we know how to rebuild live: name -> cache
# builder. Only tv_threshold exists today (adapters.tv_threshold_rule() /
# adapters.rating_cache()); a signal not in this dict still shows its IC &
# Significance panel, just not the Live Trade Rule / Symbol Explorer / P&L
# panels (see has_trade_rule()).
KNOWN_TRADE_RULE_SIGNALS = {
    "tv_threshold": ev_adapters.rating_cache,
}

_CACHE: dict = {}   # signal name -> dict[symbol -> DataFrame], built lazily


def list_evaluated_signals() -> "list[dict]":
    """Registry input_names, deduped/sorted, flagged for missing local artifacts."""
    reg = ev_registry.load()
    if reg.empty:
        return []
    names = sorted(reg["input_name"].unique())
    return [{"name": n, "has_local_artifacts": find_latest(n) is not None}
            for n in names]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_backtest_app.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backtest_app.py tests/test_backtest_app.py
git commit -m "Scaffold backtest_app.py with list_evaluated_signals()"
```

---

## Task 4: load_signal()

**Files:**
- Modify: `backtest_app.py`
- Test: `tests/test_backtest_app.py`

**Interfaces:**
- Consumes: `find_latest(name)`, `load_run(run_dir) -> (results, meta, trades)`.
- Produces: `load_signal(name: str) -> dict`. On success:
  `{"run_dir": str, "results": dict, "meta": dict, "trades": pd.DataFrame | None}`.
  On missing artifacts: `{"error": str}` (a single `"error"` key is how every
  later caller -- layout and callbacks -- detects the "registry-listed but
  no local artifacts" case from the spec's error handling section).

- [ ] **Step 1: Write the failing test**

```python
class TestLoadSignal:
    def test_missing_artifacts_returns_error_dict(self, monkeypatch):
        monkeypatch.setattr(ba, "find_latest", lambda name: None)
        out = ba.load_signal("ghost_signal")
        assert "error" in out
        assert "ghost_signal" in out["error"]

    def test_loads_run_artifacts_on_success(self, monkeypatch):
        monkeypatch.setattr(ba, "find_latest", lambda name: "/run/dir")
        monkeypatch.setattr(ba, "load_run", lambda run_dir: (
            {"summary": {"n_trades": 5}}, {"input_name": "tv_threshold"}, None))
        out = ba.load_signal("tv_threshold")
        assert out == {
            "run_dir": "/run/dir",
            "results": {"summary": {"n_trades": 5}},
            "meta": {"input_name": "tv_threshold"},
            "trades": None,
        }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_backtest_app.py::TestLoadSignal -v`
Expected: FAIL with `AttributeError: module 'backtest_app' has no attribute 'load_signal'`

- [ ] **Step 3: Write minimal implementation**

Add to `backtest_app.py`, after `list_evaluated_signals`:

```python
def load_signal(name: str) -> dict:
    """Latest run's artifacts for one signal, or an {"error": ...} dict if
    the registry knows this name but no local run directory exists (e.g. a
    registry synced from another machine without its gitignored artifacts)."""
    run_dir = find_latest(name)
    if run_dir is None:
        return {"error": f"no local artifacts for {name!r} -- run "
                         f"evaluate.py --adapter ... first"}
    results, meta, trades = load_run(run_dir)
    return {"run_dir": run_dir, "results": results, "meta": meta,
            "trades": trades}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_backtest_app.py::TestLoadSignal -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backtest_app.py tests/test_backtest_app.py
git commit -m "Add load_signal() to backtest_app.py"
```

---

## Task 5: build_tv_threshold_rule()

Parameterized version of `evaluation.adapters.tv_threshold_rule()` -- same
crossed-up/crossed-down semantics, but thresholds come from slider values
instead of `tv_rating_eval`'s hardcoded module constants.

**Files:**
- Modify: `backtest_app.py`
- Test: `tests/test_backtest_app.py`

**Interfaces:**
- Consumes: `evaluation.contracts.TradeRule`.
- Produces: `build_tv_threshold_rule(bull_min, exit_long_max, bear_max, exit_short_min, notional=10_000.0) -> TradeRule`, `side="both"`, `name="tv_threshold_live"`.

- [ ] **Step 1: Write the failing test**

```python
import numpy as np


class TestBuildTvThresholdRule:
    def _df(self):
        # rating_all path: 0.0 -> 0.6 (crosses bull 0.5) -> 0.05 (exits long,
        # < 0.1) -> -0.6 (crosses bear -0.5) -> -0.05 (exits short, > -0.1)
        return pd.DataFrame({
            "close": [10.0, 11.0, 12.0, 13.0, 14.0],
            "rating_all": [0.0, 0.6, 0.05, -0.6, -0.05],
        }, index=pd.bdate_range("2024-01-01", periods=5))

    def test_matches_adapters_tv_threshold_rule_at_default_thresholds(self):
        import tv_rating_eval as tve
        from evaluation.adapters import tv_threshold_rule

        df = self._df()
        live_rule = ba.build_tv_threshold_rule(
            bull_min=tve.BULL_MIN, exit_long_max=tve.EXIT_LONG_MAX,
            bear_max=tve.BEAR_MAX, exit_short_min=tve.EXIT_SHORT_MIN,
            notional=tve.NOTIONAL)
        fixed_rule = tv_threshold_rule()

        le1, lx1, se1, sx1 = ev_trades.rule_flags(live_rule, df)
        le2, lx2, se2, sx2 = ev_trades.rule_flags(fixed_rule, df)
        assert np.array_equal(le1, le2)
        assert np.array_equal(lx1, lx2)
        assert np.array_equal(se1, se2)
        assert np.array_equal(sx1, sx2)

    def test_tighter_bull_threshold_enters_earlier(self):
        df = self._df()
        loose = ba.build_tv_threshold_rule(0.5, 0.1, -0.5, -0.1)
        tight = ba.build_tv_threshold_rule(0.0, 0.1, -0.5, -0.1)
        le_loose, _, _, _ = ev_trades.rule_flags(loose, df)
        le_tight, _, _, _ = ev_trades.rule_flags(tight, df)
        # tight (0.0) fires on the very first crossing above 0.0, loose only
        # once rating_all reaches 0.5 in the same step -- both fire on row 1
        # here, so assert the tight rule fires at least as early overall.
        assert np.flatnonzero(le_tight)[0] <= np.flatnonzero(le_loose)[0]

    def test_side_is_both(self):
        rule = ba.build_tv_threshold_rule(0.5, 0.1, -0.5, -0.1)
        assert rule.side == "both"
        assert rule.name == "tv_threshold_live"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_backtest_app.py::TestBuildTvThresholdRule -v`
Expected: FAIL with `AttributeError: module 'backtest_app' has no attribute 'build_tv_threshold_rule'`

- [ ] **Step 3: Write minimal implementation**

Add to `backtest_app.py`:

```python
DEFAULT_NOTIONAL = 10_000.0


def _crossed_up(series, level):
    return (series >= level) & (series.shift(1) < level)


def _crossed_down(series, level):
    return (series <= level) & (series.shift(1) > level)


def build_tv_threshold_rule(bull_min: float, exit_long_max: float,
                            bear_max: float, exit_short_min: float,
                            notional: float = DEFAULT_NOTIONAL) -> TradeRule:
    """Same crossed-up/crossed-down shape as evaluation.adapters.
    tv_threshold_rule(), with slider-driven thresholds instead of
    tv_rating_eval's fixed module constants."""
    return TradeRule(
        name="tv_threshold_live",
        entries=lambda d: _crossed_up(d["rating_all"], bull_min),
        exits=lambda d: d["rating_all"] < exit_long_max,
        side="both",
        short_entries=lambda d: _crossed_down(d["rating_all"], bear_max),
        short_exits=lambda d: d["rating_all"] > exit_short_min,
        notional=notional)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_backtest_app.py::TestBuildTvThresholdRule -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backtest_app.py tests/test_backtest_app.py
git commit -m "Add build_tv_threshold_rule() for live slider-driven trade rules"
```

---

## Task 6: get_cache() and simulate_live()

**Files:**
- Modify: `backtest_app.py`
- Test: `tests/test_backtest_app.py`

**Interfaces:**
- Consumes: `KNOWN_TRADE_RULE_SIGNALS` (Task 3), `build_tv_threshold_rule` (Task 5), `evaluation.trades.simulate(rule, cache) -> pd.DataFrame`, `evaluation.trades.trade_summary(trades) -> dict`.
- Produces: `has_trade_rule(name: str) -> bool`; `get_cache(name: str) -> dict` (raises `KeyError` if `name` isn't in `KNOWN_TRADE_RULE_SIGNALS` -- callers must check `has_trade_rule` first); `simulate_live(name, bull_min, exit_long_max, bear_max, exit_short_min, notional=None) -> (trades: pd.DataFrame, summary: dict)`.

- [ ] **Step 1: Write the failing test**

```python
class TestCacheAndSimulateLive:
    def test_has_trade_rule_true_for_known_signal(self):
        assert ba.has_trade_rule("tv_threshold") is True

    def test_has_trade_rule_false_for_unknown_signal(self):
        assert ba.has_trade_rule("factor_value") is False

    def test_get_cache_builds_once_and_reuses(self, monkeypatch):
        calls = []

        def fake_rating_cache():
            calls.append(1)
            return {"AAPL": pd.DataFrame({"close": [1.0], "rating_all": [0.0]})}

        monkeypatch.setitem(ba.KNOWN_TRADE_RULE_SIGNALS, "tv_threshold",
                            fake_rating_cache)
        ba._CACHE.clear()
        first = ba.get_cache("tv_threshold")
        second = ba.get_cache("tv_threshold")
        assert first is second
        assert len(calls) == 1

    def test_get_cache_raises_for_unknown_signal(self):
        with pytest.raises(KeyError):
            ba.get_cache("factor_value")

    def test_simulate_live_zero_trades_at_extreme_threshold(self, monkeypatch):
        cache = {"AAPL": pd.DataFrame(
            {"close": [10.0, 11.0, 12.0], "rating_all": [0.0, 0.1, 0.2]},
            index=pd.bdate_range("2024-01-01", periods=3))}
        monkeypatch.setattr(ba, "get_cache", lambda name: cache)
        trades, summary = ba.simulate_live("tv_threshold", bull_min=0.99,
                                           exit_long_max=0.1, bear_max=-0.99,
                                           exit_short_min=-0.1)
        assert trades.empty
        assert summary == {"n_trades": 0, "summary_reason": "no realized trades"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_backtest_app.py::TestCacheAndSimulateLive -v`
Expected: FAIL with `AttributeError: module 'backtest_app' has no attribute 'has_trade_rule'`

- [ ] **Step 3: Write minimal implementation**

Add to `backtest_app.py`:

```python
def has_trade_rule(name: str) -> bool:
    return name in KNOWN_TRADE_RULE_SIGNALS


def get_cache(name: str) -> dict:
    """Per-symbol price+rating cache for one signal, built once and reused
    (module-level, server-side -- NOT round-tripped through dcc.Store,
    which would serialize the full multi-decade panel to browser JSON on
    every slider tick)."""
    if name not in _CACHE:
        builder = KNOWN_TRADE_RULE_SIGNALS[name]     # raises KeyError if unknown
        _CACHE[name] = builder()
    return _CACHE[name]


def simulate_live(name: str, bull_min: float, exit_long_max: float,
                  bear_max: float, exit_short_min: float,
                  notional: "float | None" = None):
    """Re-run the trade simulation in-process against the cached panel --
    no disk I/O, cost bounded by in-memory panel size."""
    cache = get_cache(name)
    rule = build_tv_threshold_rule(bull_min, exit_long_max, bear_max,
                                   exit_short_min,
                                   notional or DEFAULT_NOTIONAL)
    trades = ev_trades.simulate(rule, cache)
    summary = ev_trades.trade_summary(trades)
    return trades, summary
```

- [ ] **Step 4: Run test to verify it passes**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_backtest_app.py::TestCacheAndSimulateLive -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backtest_app.py tests/test_backtest_app.py
git commit -m "Add get_cache()/simulate_live() for live trade-rule re-simulation"
```

---

## Task 7: baseline_vs_live()

Compares the **recorded** trade summary already saved in the loaded run's
`results.json` (`results["summary"]`, from `load_signal`) against the
**live** slider-driven summary from `simulate_live`. Using the artifact's own
recorded summary (rather than re-querying the registry) is simpler and
includes `n_trades`, which the registry itself excludes as a count/metadata
key (see `evaluation/runner.py`'s `_METADATA_KEYS`).

**Files:**
- Modify: `backtest_app.py`
- Test: `tests/test_backtest_app.py`

**Interfaces:**
- Produces: `baseline_vs_live(baseline_summary: dict, live_summary: dict) -> dict`, keys `"n_trades"`, `"win_rate_pct"`, `"total_pnl_dollars"`, each mapping to `{"baseline": value_or_None, "live": value_or_None}`.

- [ ] **Step 1: Write the failing test**

```python
class TestBaselineVsLive:
    def test_diffs_the_three_headline_stats(self):
        baseline = {"n_trades": 21938, "win_rate_pct": 36.6,
                   "total_pnl_dollars": 378073.0}
        live = {"n_trades": 1847, "win_rate_pct": 41.2,
               "total_pnl_dollars": 612340.0}
        out = ba.baseline_vs_live(baseline, live)
        assert out == {
            "n_trades": {"baseline": 21938, "live": 1847},
            "win_rate_pct": {"baseline": 36.6, "live": 41.2},
            "total_pnl_dollars": {"baseline": 378073.0, "live": 612340.0},
        }

    def test_missing_baseline_keys_are_none(self):
        out = ba.baseline_vs_live({}, {"n_trades": 0,
                                       "summary_reason": "no realized trades"})
        assert out["n_trades"] == {"baseline": None, "live": 0}
        assert out["win_rate_pct"] == {"baseline": None, "live": None}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_backtest_app.py::TestBaselineVsLive -v`
Expected: FAIL with `AttributeError: module 'backtest_app' has no attribute 'baseline_vs_live'`

- [ ] **Step 3: Write minimal implementation**

Add to `backtest_app.py`:

```python
BASELINE_DIFF_KEYS = ("n_trades", "win_rate_pct", "total_pnl_dollars")


def baseline_vs_live(baseline_summary: dict, live_summary: dict) -> dict:
    return {k: {"baseline": baseline_summary.get(k), "live": live_summary.get(k)}
            for k in BASELINE_DIFF_KEYS}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_backtest_app.py::TestBaselineVsLive -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backtest_app.py tests/test_backtest_app.py
git commit -m "Add baseline_vs_live() for at-a-glance tuning feedback"
```

---

## Task 8: symbol_price_fig() and cumulative_pnl_fig()

**Files:**
- Modify: `backtest_app.py`
- Test: `tests/test_backtest_app.py`

**Interfaces:**
- Consumes: a `cache[symbol]` DataFrame (`close` column, DatetimeIndex) and the live `trades` DataFrame (`TRADE_COLS` shape from `evaluation.trades`).
- Produces: `symbol_price_fig(symbol: str, price_df: pd.DataFrame, trades_df: pd.DataFrame) -> go.Figure` (always renders the price line, even with zero trades for that symbol); `cumulative_pnl_fig(trades_df: pd.DataFrame) -> "go.Figure | None"` (`None` on empty trades, so the layout can show an empty-state message instead of a broken chart).

- [ ] **Step 1: Write the failing test**

```python
class TestCharts:
    def _price_df(self):
        return pd.DataFrame({"close": [10.0, 11.0, 12.0]},
                            index=pd.bdate_range("2024-01-01", periods=3))

    def _trades_df(self):
        return pd.DataFrame({
            "symbol": ["AAPL", "MSFT"], "side": ["long", "long"],
            "entry_signal_date": pd.bdate_range("2024-01-01", periods=2),
            "entry_date": pd.bdate_range("2024-01-02", periods=2),
            "entry_price": [10.0, 20.0],
            "exit_signal_date": pd.bdate_range("2024-01-03", periods=2),
            "exit_date": pd.bdate_range("2024-01-04", periods=2),
            "exit_price": [12.0, 19.0], "days_held": [2, 2],
            "pnl_dollars": [200.0, -50.0], "pnl_pct": [2.0, -0.5],
        })

    def test_symbol_price_fig_renders_with_zero_trades_for_symbol(self):
        fig = ba.symbol_price_fig("AAPL", self._price_df(), pd.DataFrame(
            columns=ba.ev_trades.TRADE_COLS))
        assert isinstance(fig, go.Figure)
        assert len(fig.data) == 1     # price line only, no marker traces

    def test_symbol_price_fig_adds_entry_exit_markers(self):
        fig = ba.symbol_price_fig("AAPL", self._price_df(), self._trades_df())
        assert len(fig.data) > 1

    def test_cumulative_pnl_fig_none_on_empty_trades(self):
        assert ba.cumulative_pnl_fig(pd.DataFrame(
            columns=ba.ev_trades.TRADE_COLS)) is None

    def test_cumulative_pnl_fig_builds_running_sum(self):
        fig = ba.cumulative_pnl_fig(self._trades_df())
        assert isinstance(fig, go.Figure)
        assert list(fig.data[0].y) == [200.0, 150.0]
```

Add `import plotly.graph_objects as go` to the test file's imports.

- [ ] **Step 2: Run test to verify it fails**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_backtest_app.py::TestCharts -v`
Expected: FAIL with `AttributeError: module 'backtest_app' has no attribute 'symbol_price_fig'`

- [ ] **Step 3: Write minimal implementation**

Add to `backtest_app.py` (reusing the same palette as `generate_eval_report.py` for visual consistency):

```python
SLOT0, COLOR_GOOD, COLOR_CRITICAL = "#2a78d6", "#0ca30c", "#d03b3b"


def symbol_price_fig(symbol: str, price_df: "pd.DataFrame", trades_df: "pd.DataFrame") -> go.Figure:
    """Price line always renders; entry/exit markers only if this symbol has
    trades at the current threshold settings."""
    fig = go.Figure()
    fig.add_scatter(x=price_df.index, y=price_df["close"], mode="lines",
                    name=symbol, line=dict(color=SLOT0))
    sub = trades_df[trades_df["symbol"] == symbol] if not trades_df.empty else trades_df
    if not sub.empty:
        wins, losses = sub[sub["pnl_dollars"] > 0], sub[sub["pnl_dollars"] <= 0]
        for grp, color, label in ((wins, COLOR_GOOD, "win"),
                                  (losses, COLOR_CRITICAL, "loss")):
            if grp.empty:
                continue
            fig.add_scatter(x=grp["entry_date"], y=grp["entry_price"],
                            mode="markers", name=f"entry ({label})",
                            marker=dict(symbol="triangle-up", color=color, size=10),
                            hovertemplate="entry %{x}<extra></extra>")
            fig.add_scatter(x=grp["exit_date"], y=grp["exit_price"],
                            mode="markers", name=f"exit ({label})",
                            marker=dict(symbol="x", color=color, size=9),
                            hovertemplate="exit %{x}<extra></extra>")
    fig.update_layout(title=f"{symbol} price + trades", height=360)
    return fig


def cumulative_pnl_fig(trades_df: "pd.DataFrame") -> "go.Figure | None":
    if trades_df.empty:
        return None
    ordered = trades_df.sort_values("exit_date")
    cum = ordered["pnl_dollars"].cumsum()
    fig = go.Figure()
    fig.add_scatter(x=ordered["exit_date"], y=cum, mode="lines",
                    line=dict(color=SLOT0),
                    customdata=ordered[["symbol", "pnl_dollars", "pnl_pct"]].to_numpy(),
                    hovertemplate="%{customdata[0]}: $%{customdata[1]:.2f} "
                                 "(%{customdata[2]:.2f}%%)<extra></extra>")
    fig.update_layout(title="Cumulative P&L (current thresholds)", height=320)
    return fig
```

- [ ] **Step 4: Run test to verify it passes**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_backtest_app.py::TestCharts -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backtest_app.py tests/test_backtest_app.py
git commit -m "Add symbol_price_fig()/cumulative_pnl_fig() chart builders"
```

---

## Task 9: Dash layout and callbacks

Wires every pure function from Tasks 3-8 into the actual live app: a signal
dropdown, four sliders, a symbol dropdown, and the four view panels from the
spec's mockup. This task is smoke-tested (layout builds without exceptions
for both an empty and a populated registry) rather than fully unit-tested,
since exercising real Dash callbacks needs a running server/browser -- that
happens in Task 10's manual pass.

**Files:**
- Modify: `backtest_app.py`
- Test: `tests/test_backtest_app.py`

**Interfaces:**
- Consumes: `list_evaluated_signals()`, `load_signal()`, `has_trade_rule()`, `simulate_live()`, `baseline_vs_live()`, `symbol_price_fig()`, `cumulative_pnl_fig()`, and `generate_eval_report._ic_by_horizon` / `_spread_with_ci` / `_regimes` (reused, not duplicated, per spec).
- Produces: `build_layout(signals: "list[dict]") -> dash.html.Div`; `register_callbacks(app: dash.Dash) -> None`; module-level `app = dash.Dash(__name__)` with `app.layout` and callbacks already wired, ready for `app.run()`.

- [ ] **Step 1: Write the failing test**

```python
class TestLayout:
    def test_builds_with_empty_registry(self):
        div = ba.build_layout([])
        assert isinstance(div, ba.html.Div)

    def test_builds_with_signals(self):
        div = ba.build_layout([{"name": "tv_threshold", "has_local_artifacts": True},
                               {"name": "factor_value", "has_local_artifacts": False}])
        assert isinstance(div, ba.html.Div)

    def test_register_callbacks_does_not_raise(self):
        app = dash.Dash(__name__)
        app.layout = ba.build_layout(ba.list_evaluated_signals())
        ba.register_callbacks(app)     # just verifies callback registration succeeds
```

Add `import dash` to the test file's imports.

- [ ] **Step 2: Run test to verify it fails**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_backtest_app.py::TestLayout -v`
Expected: FAIL with `AttributeError: module 'backtest_app' has no attribute 'build_layout'`

- [ ] **Step 3: Write minimal implementation**

Add to `backtest_app.py` (this is the last piece -- append it after Task 8's chart builders, then create the module-level `app`):

```python
from generate_eval_report import _ic_by_horizon, _spread_with_ci, _regimes

SLIDER_MIN, SLIDER_MAX, SLIDER_STEP = -1.0, 1.0, 0.05


def _slider(id_, value):
    return dcc.Slider(id=id_, min=SLIDER_MIN, max=SLIDER_MAX, step=SLIDER_STEP,
                      value=value, updatemode="mouseup",
                      marks={-1: "-1", 0: "0", 1: "1"})


def build_layout(signals: "list[dict]") -> "html.Div":
    options = [{"label": s["name"] + ("" if s["has_local_artifacts"]
                                      else "  [no local artifacts]"),
               "value": s["name"]} for s in signals]
    return html.Div([
        html.Div([
            dcc.Dropdown(id="signal-dropdown", options=options,
                        value=options[0]["value"] if options else None,
                        placeholder="no evaluated signals yet"),
            html.Button("Refresh", id="refresh-button", n_clicks=0),
            html.Div(id="run-banner"),
        ]),
        html.Div([
            html.Div(id="ic-panel"),
            html.Div([
                html.Div("Bull entry"), _slider("bull-min", 0.5),
                html.Div("Exit long"), _slider("exit-long-max", 0.1),
                html.Div("Bear entry"), _slider("bear-max", -0.5),
                html.Div("Exit short"), _slider("exit-short-min", -0.1),
                html.Div(id="trade-summary"),
            ]),
        ]),
        html.Div([
            dcc.Dropdown(id="symbol-dropdown", placeholder="select a symbol"),
            dcc.Graph(id="symbol-fig"),
        ]),
        dcc.Graph(id="pnl-fig"),
        dcc.Store(id="signal-store"),
    ])


def _render_ic_panel(results: dict) -> "list":
    ic = results.get("ic", {})
    figs = [_ic_by_horizon(ic), _spread_with_ci(ic, results.get("tier2", {})),
           _regimes(results.get("tier3", {}))]
    return [dcc.Graph(figure=f) for f in figs if f is not None]


def register_callbacks(app: "dash.Dash") -> None:
    @app.callback(
        Output("signal-store", "data"), Output("run-banner", "children"),
        Output("ic-panel", "children"), Output("symbol-dropdown", "options"),
        Input("signal-dropdown", "value"), Input("refresh-button", "n_clicks"))
    def _on_signal_change(name, _n_clicks):
        if not name:
            return None, "no evaluated signals yet", [], []
        loaded = load_signal(name)
        if "error" in loaded:
            return None, loaded["error"], [], []
        meta = loaded["meta"]
        banner = (f'{meta.get("run_id")} - {meta.get("date_range")} - '
                 f'loaded {pd.Timestamp.now():%H:%M:%S}')
        symbol_options = []
        if has_trade_rule(name):
            symbol_options = [{"label": s, "value": s}
                             for s in sorted(get_cache(name).keys())]
        return ({"name": name, "results": loaded["results"]}, banner,
               _render_ic_panel(loaded["results"]), symbol_options)

    @app.callback(
        Output("trade-summary", "children"), Output("symbol-fig", "figure"),
        Output("pnl-fig", "figure"),
        Input("signal-store", "data"), Input("bull-min", "value"),
        Input("exit-long-max", "value"), Input("bear-max", "value"),
        Input("exit-short-min", "value"), Input("symbol-dropdown", "value"))
    def _on_sliders_change(store, bull_min, exit_long_max, bear_max,
                          exit_short_min, symbol):
        empty_fig = go.Figure()
        if not store or not has_trade_rule(store["name"]):
            return "no trade rule defined for this signal", empty_fig, empty_fig
        trades, summary = simulate_live(store["name"], bull_min, exit_long_max,
                                        bear_max, exit_short_min)
        baseline = store["results"].get("summary", {})
        diff = baseline_vs_live(baseline, summary)
        if summary.get("n_trades", 0) == 0:
            text = "0 realized trades at this threshold"
        else:
            text = (f'n={diff["n_trades"]["live"]} trades | '
                   f'win {diff["win_rate_pct"]["live"]}% | '
                   f'${diff["total_pnl_dollars"]["live"]:,.0f} net '
                   f'(baseline: {diff["n_trades"]["baseline"]} / '
                   f'{diff["win_rate_pct"]["baseline"]}% / '
                   f'${diff["total_pnl_dollars"]["baseline"]:,.0f})')
        cache = get_cache(store["name"])
        sym_fig = (symbol_price_fig(symbol, cache[symbol], trades)
                  if symbol and symbol in cache else empty_fig)
        pnl_fig = cumulative_pnl_fig(trades) or empty_fig
        return text, sym_fig, pnl_fig


app = dash.Dash(__name__)
app.layout = build_layout(list_evaluated_signals())
register_callbacks(app)


if __name__ == "__main__":
    app.run(debug=True)
```

Add `import pandas as pd` to `backtest_app.py`'s top-of-file imports (needed for `pd.Timestamp.now()` in the banner).

- [ ] **Step 4: Run test to verify it passes**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_backtest_app.py::TestLayout -v`
Expected: PASS

- [ ] **Step 5: Run the full test file**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_backtest_app.py -v`
Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add backtest_app.py tests/test_backtest_app.py
git commit -m "Wire Dash layout and callbacks for the interactive backtest explorer"
```

---

## Task 10: Manual browser verification

Dash callbacks aren't exercised by the pytest suite (no Selenium harness, per
spec). Run the app for real and walk the golden path plus the spec's edge
cases before calling this done.

**Files:** none (verification only).

- [ ] **Step 1: Run the full automated test suite once more**

Run: `"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_backtest_app.py tests/test_generate_eval_report.py -v`
Expected: all PASS.

- [ ] **Step 2: Start the app**

Run: `"C:\ProgramData\anaconda3\python.exe" backtest_app.py`
Expected: prints the local URL (default `http://127.0.0.1:8050`) with no traceback.

- [ ] **Step 3: Exercise the golden path in a browser**

Open the URL. Select `tv_threshold` in the signal dropdown (if it has local
artifacts -- if not, first run
`"C:\ProgramData\anaconda3\python.exe" evaluate.py --adapter tv-rule` to
produce a run directory). Confirm: IC panel renders, sliders show the
`tv_rating_eval` defaults (0.5 / 0.1 / -0.5 / -0.1), trade summary shows a
live count with a baseline comparison, symbol dropdown populates, selecting a
symbol renders its price chart, and the P&L chart renders.

- [ ] **Step 4: Exercise edge cases**

- Drag the bull-entry slider to `1.0` (near-impossible threshold) -- confirm
  "0 realized trades at this threshold" appears instead of a broken chart.
- Select a signal with no `TradeRule` (e.g. `factor_value`) -- confirm the
  Live Trade Rule / Symbol Explorer / P&L panels show the "no trade rule
  defined for this signal" message and IC & Significance still renders.
- Click **Refresh** -- confirm the banner's `loaded HH:MM:SS` timestamp
  updates.

- [ ] **Step 5: Report results**

No commit for this task -- it's verification only. If any edge case breaks,
fix it in the relevant earlier task's code and re-run that task's tests
before re-verifying here.

---

## Self-Review Notes

- **Spec coverage:** Architecture/data flow -> Tasks 3-6; Views & interactions -> Task 9; error handling (no-TradeRule, zero-trades, missing-artifacts, empty-registry) -> Tasks 3-4, 6, 9 (each has an explicit test); staleness/Refresh -> Task 9; performance (server-side cache, `mouseup`) -> Task 6, Task 9's `_slider`; testing gap on `generate_eval_report.py` -> Task 2; Altair, retiring the static report, multi-signal comparison, `run_all.py` wiring, live trading -> explicitly out of scope, no task touches them.
- **Type consistency:** `TRADE_COLS`-shaped DataFrames flow unchanged from `evaluation.trades.simulate()` through `simulate_live` into both chart builders; `load_signal`'s `{"results", "meta", "trades", "run_dir"}` / `{"error"}` shape is the only contract every downstream caller (Task 9's callbacks) checks via `"error" in loaded`.
- **No placeholders:** every step above has real code, real assertions, and a real command to run.
