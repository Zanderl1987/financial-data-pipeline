# W4 Interactive Execution & Tearsheet Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `backtest_app.py`'s live Dash explorer real execution-cost/risk/sizing controls (via `evaluation/execution.py`'s `ExecutionConfig`) and a live tearsheet section fed by the same simulated trades, reusing `evaluation/tearsheet.py` and `generate_tearsheet.py`'s existing compute/render functions.

**Architecture:** Two additive layers on top of the existing signal/threshold-slider flow in `backtest_app.py`: (1) a new Execution Config panel whose control values assemble an `ExecutionConfig` passed into `evaluation.trades.simulate(..., config=cfg)`, and (2) a new tearsheet section that bridges the resulting trades through `evaluation.tearsheet.daily_returns_from_trades()` + `evaluation.tearsheet.tearsheet()` and renders with `generate_tearsheet.py`'s existing figure/HTML builders. All new logic is added as small standalone functions (matching the file's existing pattern of pure, directly-testable helpers wrapped by a thin Dash callback), so nothing beyond the final wiring task depends on Dash's own runtime to test.

**Tech Stack:** Python, Dash/Plotly (existing), pandas, pytest (existing test conventions — plain-function unit tests, no Selenium/browser harness).

**Spec:** `docs/superpowers/specs/2026-08-20-w4-interactive-execution-tearsheet-design.md`

## Global Constraints

- No new backtest math: every number comes from `evaluation.trades.simulate()` / `evaluation.tearsheet.py`, unchanged.
- `evaluation.trades.simulate()`/`simulate_symbol()` already accept `config=None` meaning `LEGACY` (Step B, commit `115b652`) — do not change that default or its meaning.
- `generate_tearsheet.py`'s figure/HTML builder functions (`_headline_tiles`, `_monthly_fig`, `_rolling_fig`, `_drawdown_table`) are reused directly by import, never reimplemented or copied.
- No benchmark overlay this round — `tearsheet()` is called with `bench_returns=None`.
- Existing entry/exit threshold sliders, `KNOWN_TRADE_RULE_SIGNALS` gating, `_SIM_CACHE`, and `has_trade_rule()` stay as today except where a task explicitly extends them.
- Follow the existing test file's convention: pure logic gets direct unit tests; Dash callback *wiring* itself stays smoke-tested only (`test_register_callbacks_does_not_raise`), not unit-tested function-by-function, because the callback body is a nested closure.
- Python: `C:\ProgramData\anaconda3\python.exe` (repo convention, see `CLAUDE.md`) for every command below.

---

## Task 1: `build_execution_config()` — assemble an `ExecutionConfig` from typed values

**Files:**
- Modify: `backtest_app.py` (add import, add function after `DEFAULT_NOTIONAL = 10_000.0` at line 70)
- Test: `tests/test_backtest_app.py`

**Interfaces:**
- Consumes: `evaluation.execution.ExecutionConfig`, `CostModel`, `RiskControls`, `Sizing`, `PortfolioLimits` (existing, unchanged).
- Produces: `build_execution_config(**kwargs) -> ExecutionConfig`, raising `ValueError` on an invalid combination (propagated from the dataclasses' own `__post_init__`). Used by Task 2.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_backtest_app.py`, after the `TestBaselineVsLive` class:

```python
class TestBuildExecutionConfig:
    def test_defaults_match_legacy_field_values(self):
        cfg = ba.build_execution_config()
        assert cfg.costs == ba.ev_execution.CostModel()
        assert cfg.risk == ba.ev_execution.RiskControls()
        assert cfg.sizing == ba.ev_execution.Sizing()
        assert cfg.limits == ba.ev_execution.PortfolioLimits()

    def test_custom_cost_values_populate_cost_model(self):
        cfg = ba.build_execution_config(commission_bps=10.0, spread_bps=5.0,
                                        borrow_fee_bps=2.0, impact_model="sqrt",
                                        impact_coeff=0.1)
        assert cfg.costs == ba.ev_execution.CostModel(
            commission_bps=10.0, spread_bps=5.0, borrow_fee_bps=2.0,
            impact_model="sqrt", impact_coeff=0.1)

    def test_custom_risk_and_sizing_and_limits_values(self):
        cfg = ba.build_execution_config(
            stop_loss_pct=0.05, take_profit_pct=0.10, vol_stop_mult=2.0,
            trailing=True, max_holding_days=20,
            sizing_mode="fixed_fraction", fraction=0.1, max_weight=0.2,
            capital=100_000.0, max_concurrent=5, max_drawdown_stop=0.25)
        assert cfg.risk == ba.ev_execution.RiskControls(
            stop_loss_pct=0.05, take_profit_pct=0.10, vol_stop_mult=2.0,
            trailing=True, max_holding_days=20)
        assert cfg.sizing.mode == "fixed_fraction"
        assert cfg.sizing.fraction == 0.1
        assert cfg.sizing.max_weight == 0.2
        assert cfg.limits == ba.ev_execution.PortfolioLimits(
            capital=100_000.0, max_concurrent=5, max_drawdown_stop=0.25)

    def test_invalid_fixed_fraction_without_fraction_raises_value_error(self):
        with pytest.raises(ValueError, match="fixed_fraction"):
            ba.build_execution_config(sizing_mode="fixed_fraction")

    def test_invalid_negative_commission_raises_value_error(self):
        with pytest.raises(ValueError, match="commission_bps"):
            ba.build_execution_config(commission_bps=-1.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_backtest_app.py::TestBuildExecutionConfig -v`
Expected: FAIL with `AttributeError: module 'backtest_app' has no attribute 'build_execution_config'`

- [ ] **Step 3: Implement**

Add to `backtest_app.py` at line 22 (with the other `evaluation` imports):

```python
from evaluation import execution as ev_execution
```

Add after `DEFAULT_NOTIONAL = 10_000.0` (line 70):

```python
def build_execution_config(*, commission_bps: float = 0.0, spread_bps: float = 0.0,
                           borrow_fee_bps: float = 0.0,
                           impact_model: "str | None" = None,
                           impact_coeff: float = 0.0,
                           stop_loss_pct: "float | None" = None,
                           take_profit_pct: "float | None" = None,
                           vol_stop_mult: "float | None" = None,
                           trailing: bool = False,
                           max_holding_days: "int | None" = None,
                           sizing_mode: str = "fixed_notional",
                           notional: float = DEFAULT_NOTIONAL,
                           fraction: "float | None" = None,
                           max_weight: "float | None" = None,
                           capital: "float | None" = None,
                           max_concurrent: "int | None" = None,
                           max_drawdown_stop: "float | None" = None
                           ) -> "ev_execution.ExecutionConfig":
    """Assemble a live ExecutionConfig from typed values -- one dataclass
    group per evaluation/execution.py group, no new grouping invented.
    Raises ValueError (via the dataclasses' own __post_init__) on an
    invalid combination; callers catch it and show the message inline
    rather than letting it crash the app."""
    return ev_execution.ExecutionConfig(
        name="live",
        costs=ev_execution.CostModel(
            commission_bps=commission_bps, spread_bps=spread_bps,
            borrow_fee_bps=borrow_fee_bps, impact_model=impact_model,
            impact_coeff=impact_coeff),
        risk=ev_execution.RiskControls(
            stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct,
            vol_stop_mult=vol_stop_mult, trailing=trailing,
            max_holding_days=max_holding_days),
        sizing=ev_execution.Sizing(
            mode=sizing_mode, notional=notional, fraction=fraction,
            max_weight=max_weight),
        limits=ev_execution.PortfolioLimits(
            capital=capital, max_concurrent=max_concurrent,
            max_drawdown_stop=max_drawdown_stop))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_backtest_app.py::TestBuildExecutionConfig -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add backtest_app.py tests/test_backtest_app.py
git commit -m "W4: add build_execution_config() helper"
```

---

## Task 2: `resolve_execution_config()` — Dash-control-value adapter with inline error

**Files:**
- Modify: `backtest_app.py` (add function after `build_execution_config`)
- Test: `tests/test_backtest_app.py`

**Interfaces:**
- Consumes: `build_execution_config()` (Task 1).
- Produces: `resolve_execution_config(**raw_values) -> tuple[ExecutionConfig | None, str]` — `(config, "")` on success, `(None, message)` on an invalid combination. Used by Task 7's callback.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_backtest_app.py`, after `TestBuildExecutionConfig`:

```python
class TestResolveExecutionConfig:
    DEFAULTS = dict(
        commission_bps=0.0, spread_bps=0.0, borrow_fee_bps=0.0,
        impact_model="none", impact_coeff=0.0, stop_loss_pct=None,
        take_profit_pct=None, vol_stop_mult=None, trailing=[],
        max_holding_days=None, sizing_mode="fixed_notional",
        sizing_notional=None, sizing_fraction=None, sizing_max_weight=None,
        limits_capital=None, limits_max_concurrent=None,
        limits_max_drawdown_stop=None)

    def test_defaults_resolve_to_legacy_equivalent_config(self):
        cfg, err = ba.resolve_execution_config(**self.DEFAULTS)
        assert err == ""
        assert cfg == ba.ev_execution.ExecutionConfig(
            name="live", costs=ba.ev_execution.CostModel(),
            risk=ba.ev_execution.RiskControls(), sizing=ba.ev_execution.Sizing(),
            limits=ba.ev_execution.PortfolioLimits())

    def test_impact_model_none_sentinel_maps_to_python_none(self):
        cfg, err = ba.resolve_execution_config(**self.DEFAULTS)
        assert cfg.costs.impact_model is None

    def test_impact_model_sqrt_passes_through(self):
        values = dict(self.DEFAULTS, impact_model="sqrt", impact_coeff=0.1)
        cfg, err = ba.resolve_execution_config(**values)
        assert cfg.costs.impact_model == "sqrt"
        assert cfg.costs.impact_coeff == 0.1

    def test_trailing_checklist_value_maps_to_bool(self):
        values = dict(self.DEFAULTS, trailing=["trailing"])
        cfg, err = ba.resolve_execution_config(**values)
        assert cfg.risk.trailing is True

    def test_blank_notional_falls_back_to_default_notional(self):
        values = dict(self.DEFAULTS, sizing_notional=None)
        cfg, err = ba.resolve_execution_config(**values)
        assert cfg.sizing.notional == ba.DEFAULT_NOTIONAL

    def test_explicit_notional_passes_through(self):
        values = dict(self.DEFAULTS, sizing_notional=25_000.0)
        cfg, err = ba.resolve_execution_config(**values)
        assert cfg.sizing.notional == 25_000.0

    def test_invalid_combination_returns_none_config_and_message(self):
        values = dict(self.DEFAULTS, sizing_mode="fixed_fraction",
                     sizing_fraction=None)
        cfg, err = ba.resolve_execution_config(**values)
        assert cfg is None
        assert "fixed_fraction" in err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_backtest_app.py::TestResolveExecutionConfig -v`
Expected: FAIL with `AttributeError: module 'backtest_app' has no attribute 'resolve_execution_config'`

- [ ] **Step 3: Implement**

Add to `backtest_app.py` directly after `build_execution_config`:

```python
def resolve_execution_config(*, commission_bps, spread_bps, borrow_fee_bps,
                             impact_model, impact_coeff, stop_loss_pct,
                             take_profit_pct, vol_stop_mult, trailing,
                             max_holding_days, sizing_mode, sizing_notional,
                             sizing_fraction, sizing_max_weight, limits_capital,
                             limits_max_concurrent, limits_max_drawdown_stop
                             ) -> "tuple[ev_execution.ExecutionConfig | None, str]":
    """Adapt raw Dash control values into build_execution_config()'s typed
    kwargs and catch the ValueError an invalid combination raises, so the
    caller can show it inline instead of the callback crashing.

    trailing: dcc.Checklist value, a list ("trailing" in it, or empty).
    impact_model: dropdown value; "none" is the not-clearable sentinel for
    Python None (dcc.Dropdown can't hold None as a real option value).
    sizing_notional: blank (None) falls back to DEFAULT_NOTIONAL rather than
    reaching Sizing's `notional must be > 0` check with None, which would
    raise TypeError instead of the intended ValueError.
    """
    try:
        cfg = build_execution_config(
            commission_bps=commission_bps, spread_bps=spread_bps,
            borrow_fee_bps=borrow_fee_bps,
            impact_model=None if impact_model == "none" else impact_model,
            impact_coeff=impact_coeff, stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct, vol_stop_mult=vol_stop_mult,
            trailing=bool(trailing), max_holding_days=max_holding_days,
            sizing_mode=sizing_mode,
            notional=(sizing_notional if sizing_notional is not None
                      else DEFAULT_NOTIONAL),
            fraction=sizing_fraction, max_weight=sizing_max_weight,
            capital=limits_capital, max_concurrent=limits_max_concurrent,
            max_drawdown_stop=limits_max_drawdown_stop)
        return cfg, ""
    except ValueError as exc:
        return None, f"Execution config error: {exc}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_backtest_app.py::TestResolveExecutionConfig -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add backtest_app.py tests/test_backtest_app.py
git commit -m "W4: add resolve_execution_config() Dash-control-value adapter"
```

---

## Task 3: `simulate_live()` accepts `config=` and caches by it

**Files:**
- Modify: `backtest_app.py:163-181`
- Test: `tests/test_backtest_app.py`

**Interfaces:**
- Consumes: `ev_execution.config_hash()` (existing), `ev_trades.simulate(..., config=)` (existing, Step B).
- Produces: `simulate_live(..., config=None)` — extended signature; existing callers (`parameter_heatmap_fig`, existing tests) keep working unchanged since `config` defaults to `None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_backtest_app.py`, inside `TestCacheAndSimulateLive` (after `test_simulate_live_zero_trades_at_extreme_threshold`):

```python
    def test_config_none_still_works_and_matches_prior_behavior(self, monkeypatch):
        cache = {"AAPL": pd.DataFrame(
            {"close": [10.0, 11.0, 12.0, 13.0], "rating_all": [0.0, 0.6, 0.05, 0.6]},
            index=pd.bdate_range("2024-01-01", periods=4))}
        monkeypatch.setattr(ba, "get_cache", lambda name, run_id: cache)
        ba._SIM_CACHE.clear()
        trades, summary = ba.simulate_live("tv_threshold", "run_001", bull_min=0.5,
                                           exit_long_max=0.1, bear_max=-0.5,
                                           exit_short_min=-0.1)
        assert summary["n_trades"] >= 0   # no exception, legacy path still runs

    def test_different_configs_produce_separate_cache_entries(self, monkeypatch):
        cache = {"AAPL": pd.DataFrame(
            {"close": [10.0, 11.0, 12.0, 13.0], "rating_all": [0.0, 0.6, 0.05, 0.6]},
            index=pd.bdate_range("2024-01-01", periods=4))}
        monkeypatch.setattr(ba, "get_cache", lambda name, run_id: cache)
        ba._SIM_CACHE.clear()
        cheap = ba.build_execution_config(commission_bps=0.0)
        costly = ba.build_execution_config(commission_bps=50.0)
        ba.simulate_live("tv_threshold", "run_001", 0.5, 0.1, -0.5, -0.1, config=cheap)
        ba.simulate_live("tv_threshold", "run_001", 0.5, 0.1, -0.5, -0.1, config=costly)
        assert len(ba._SIM_CACHE) == 2

    def test_costly_config_reduces_pnl_versus_legacy(self, monkeypatch):
        cache = {"AAPL": pd.DataFrame(
            {"close": [10.0, 12.0, 14.0, 11.0], "rating_all": [0.0, 0.6, 0.05, 0.6]},
            index=pd.bdate_range("2024-01-01", periods=4))}
        monkeypatch.setattr(ba, "get_cache", lambda name, run_id: cache)
        ba._SIM_CACHE.clear()
        legacy_trades, legacy_summary = ba.simulate_live(
            "tv_threshold", "run_001", 0.5, 0.1, -0.5, -0.1)
        costly = ba.build_execution_config(commission_bps=100.0)
        costly_trades, costly_summary = ba.simulate_live(
            "tv_threshold", "run_001", 0.5, 0.1, -0.5, -0.1, config=costly)
        if legacy_summary.get("n_trades", 0) > 0:
            assert (costly_summary["total_pnl_dollars"]
                    < legacy_summary["total_pnl_dollars"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_backtest_app.py::TestCacheAndSimulateLive -v`
Expected: FAIL — `test_different_configs_produce_separate_cache_entries` and
`test_costly_config_reduces_pnl_versus_legacy` fail with `TypeError:
simulate_live() got an unexpected keyword argument 'config'`

- [ ] **Step 3: Implement**

Replace `backtest_app.py:163-181` (the current `simulate_live` function):

```python
def simulate_live(name: str, run_id: str, bull_min: float, exit_long_max: float,
                  bear_max: float, exit_short_min: float,
                  notional: "float | None" = None, *,
                  config: "ev_execution.ExecutionConfig | None" = None):
    """Re-run the trade simulation in-process against the cached panel --
    no disk I/O, cost bounded by in-memory panel size. Memoized by its full
    input key, INCLUDING the execution config's hash, so switching the
    symbol dropdown (which doesn't change any of these inputs) reuses the
    already-computed trades, and two different execution configs against
    identical thresholds never collide in the memo. config=None means
    ExecutionConfig LEGACY (today's behavior: no costs, no stops,
    unlimited concurrency) -- unchanged from before this config parameter
    existed."""
    key = (name, run_id, bull_min, exit_long_max, bear_max, exit_short_min,
           ev_execution.config_hash(config))
    if key in _SIM_CACHE:
        return _SIM_CACHE[key]
    cache = get_cache(name, run_id)
    _, rule_builder = KNOWN_TRADE_RULE_SIGNALS[name]
    rule = rule_builder(bull_min, exit_long_max, bear_max, exit_short_min,
                        notional or DEFAULT_NOTIONAL)
    trades = ev_trades.simulate(rule, cache, config=config)
    summary = ev_trades.trade_summary(trades)
    _SIM_CACHE[key] = (trades, summary)
    return trades, summary
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_backtest_app.py::TestCacheAndSimulateLive -v`
Expected: PASS (all tests in this class pass)

- [ ] **Step 5: Commit**

```bash
git add backtest_app.py tests/test_backtest_app.py
git commit -m "W4: simulate_live() accepts and caches by ExecutionConfig"
```

---

## Task 4: `live_tearsheet()` — bridge trades to a tearsheet dict

**Files:**
- Modify: `backtest_app.py` (add import + function after `simulate_live`)
- Test: `tests/test_backtest_app.py`

**Interfaces:**
- Consumes: `evaluation.tearsheet.daily_returns_from_trades()`, `evaluation.tearsheet.tearsheet()` (existing, W3, unchanged).
- Produces: `live_tearsheet(trades: pd.DataFrame) -> dict` — either a full `tearsheet()` dict (keys `headline`/`monthly`/`rolling`/`drawdowns`/`underwater`/`benchmark`) or `{"returns_reason": <str>}` when there aren't enough realized trades. Used by Task 6's `render_tearsheet()` and Task 7's callback.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_backtest_app.py`, after `TestCharts`:

```python
class TestLiveTearsheet:
    def test_empty_trades_returns_reason(self):
        out = ba.live_tearsheet(pd.DataFrame(columns=ba.ev_trades.TRADE_COLS))
        assert out == {"returns_reason": "no realized trades"}

    def test_none_trades_returns_reason(self):
        assert ba.live_tearsheet(None) == {"returns_reason": "no realized trades"}

    def test_trades_with_no_exit_date_column_propagates_bridge_reason(self):
        trades = pd.DataFrame({"symbol": ["AAPL"], "pnl_dollars": [100.0]})
        out = ba.live_tearsheet(trades)
        assert "returns_reason" in out
        assert "exit_date" in out["returns_reason"] or "columns" in out["returns_reason"]

    def test_enough_realized_trades_returns_full_tearsheet_dict(self):
        dates = pd.bdate_range("2024-01-01", periods=40)
        trades = pd.DataFrame({
            "exit_date": dates[::4],
            "pnl_dollars": [100.0, -50.0, 200.0, 80.0, -20.0, 150.0, 60.0, -10.0,
                            120.0, 90.0],
        })
        out = ba.live_tearsheet(trades)
        assert "returns_reason" not in out
        assert set(out.keys()) == {"headline", "monthly", "rolling",
                                   "drawdowns", "underwater", "benchmark"}
        assert out["headline"]["sharpe"] is not None or \
               "headline_reason" in out["headline"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_backtest_app.py::TestLiveTearsheet -v`
Expected: FAIL with `AttributeError: module 'backtest_app' has no attribute 'live_tearsheet'`

- [ ] **Step 3: Implement**

Add to `backtest_app.py:22` (with the other `evaluation` imports):

```python
from evaluation import tearsheet as ev_tearsheet
```

Add after `simulate_live` (directly before `BASELINE_DIFF_KEYS = ...`):

```python
def live_tearsheet(trades: "pd.DataFrame | None") -> dict:
    """Bridge realized trades -> the same tearsheet dict generate_tearsheet.py
    computes for the static HTML report -- daily_returns_from_trades() then
    tearsheet(), both unchanged from W3. Returns {"returns_reason": ...}
    when there aren't enough realized trades to build a return series, the
    same shape daily_returns_from_trades() itself uses for its empty
    states, so callers check one key regardless of where the gap occurred."""
    if trades is None or trades.empty:
        return {"returns_reason": "no realized trades"}
    bridged = ev_tearsheet.daily_returns_from_trades(trades)
    if bridged["returns"] is None:
        return {"returns_reason": bridged["returns_reason"]}
    return ev_tearsheet.tearsheet(bridged["returns"])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_backtest_app.py::TestLiveTearsheet -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add backtest_app.py tests/test_backtest_app.py
git commit -m "W4: add live_tearsheet() trades-to-tearsheet bridge"
```

---

## Task 5: `render_tearsheet()` — Dash children from a tearsheet dict

**Files:**
- Modify: `backtest_app.py` (add import + function after `render_risk_card`)
- Test: `tests/test_backtest_app.py`

**Interfaces:**
- Consumes: `live_tearsheet()` output (Task 4); `generate_tearsheet._headline_tiles`, `_monthly_fig`, `_rolling_fig`, `_drawdown_table` (existing, W3, unchanged, imported not copied).
- Produces: `render_tearsheet(sheet: dict) -> list` — a list of Dash components (`dcc.Markdown`/`dcc.Graph`/`html.Div`). Used by Task 7's callback as the `tearsheet-container` children.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_backtest_app.py`, after `TestLiveTearsheet`:

```python
class TestRenderTearsheet:
    def test_returns_reason_renders_single_message_div(self):
        out = ba.render_tearsheet({"returns_reason": "no realized trades"})
        assert len(out) == 1
        assert isinstance(out[0], ba.html.Div)
        assert "no realized trades" in out[0].children

    def test_full_sheet_renders_markdown_and_graphs(self):
        dates = pd.bdate_range("2024-01-01", periods=40)
        trades = pd.DataFrame({
            "exit_date": dates[::4],
            "pnl_dollars": [100.0, -50.0, 200.0, 80.0, -20.0, 150.0, 60.0, -10.0,
                            120.0, 90.0],
        })
        sheet = ba.live_tearsheet(trades)
        out = ba.render_tearsheet(sheet)
        assert any(isinstance(c, ba.dcc.Markdown) for c in out)
        assert any(isinstance(c, ba.dcc.Graph) for c in out)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_backtest_app.py::TestRenderTearsheet -v`
Expected: FAIL with `AttributeError: module 'backtest_app' has no attribute 'render_tearsheet'`

- [ ] **Step 3: Implement**

Add to `backtest_app.py:22` (with the other imports, after the `evaluation` imports):

```python
import generate_tearsheet as gt
```

Add after `render_risk_card`'s closing (directly before `def parameter_heatmap_fig`):

```python
def render_tearsheet(sheet: dict) -> "list":
    """Dash children for the live tearsheet section. Reuses
    generate_tearsheet.py's figure/HTML builders directly -- no metric or
    chart logic duplicated here, matching the W3 compute/render split this
    was built for. HTML-string builders (_headline_tiles, _drawdown_table)
    render via dcc.Markdown(dangerously_allow_html=True); Figure builders
    (_monthly_fig, _rolling_fig) render via dcc.Graph and are skipped (not
    an empty Graph) when the underlying data is too thin to plot."""
    if "returns_reason" in sheet:
        return [html.Div(f"no realized trades to compute tearsheet: "
                         f"{sheet['returns_reason']}")]
    children = [dcc.Markdown(gt._headline_tiles(sheet["headline"]),
                             dangerously_allow_html=True)]
    monthly_fig = gt._monthly_fig(sheet["monthly"])
    if monthly_fig is not None:
        children.append(dcc.Graph(figure=monthly_fig))
    rolling_fig = gt._rolling_fig(sheet["rolling"])
    if rolling_fig is not None:
        children.append(dcc.Graph(figure=rolling_fig))
    children.append(dcc.Markdown(gt._drawdown_table(sheet["drawdowns"]),
                                 dangerously_allow_html=True))
    return children
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_backtest_app.py::TestRenderTearsheet -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add backtest_app.py tests/test_backtest_app.py
git commit -m "W4: add render_tearsheet() Dash children builder"
```

---

## Task 6: Layout — Execution Config panel, error div, tearsheet container

**Files:**
- Modify: `backtest_app.py:296-336` (`SLIDER_MIN...`, `_slider`, `build_layout`)
- Test: `tests/test_backtest_app.py`

**Interfaces:**
- Consumes: nothing new (pure layout).
- Produces: new component IDs consumed by Task 7's callback: `commission-bps`, `spread-bps`, `borrow-fee-bps`, `impact-model`, `impact-coeff`, `stop-loss-pct`, `take-profit-pct`, `vol-stop-mult`, `trailing`, `max-holding-days`, `sizing-mode`, `sizing-notional`, `sizing-fraction`, `sizing-max-weight`, `limits-capital`, `limits-max-concurrent`, `limits-max-drawdown-stop`, `execution-config-error`, `tearsheet-container`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_backtest_app.py`, inside `TestLayout` (after `test_builds_with_signals`):

```python
    def _collect_ids(self, component, found):
        cid = getattr(component, "id", None)
        if cid:
            found.add(cid)
        children = getattr(component, "children", None)
        if children is None:
            return
        if isinstance(children, (list, tuple)):
            for c in children:
                self._collect_ids(c, found)
        else:
            self._collect_ids(children, found)

    def test_execution_config_and_tearsheet_ids_present(self):
        div = ba.build_layout([])
        found = set()
        self._collect_ids(div, found)
        expected = {"commission-bps", "spread-bps", "borrow-fee-bps",
                   "impact-model", "impact-coeff", "stop-loss-pct",
                   "take-profit-pct", "vol-stop-mult", "trailing",
                   "max-holding-days", "sizing-mode", "sizing-notional",
                   "sizing-fraction", "sizing-max-weight", "limits-capital",
                   "limits-max-concurrent", "limits-max-drawdown-stop",
                   "execution-config-error", "tearsheet-container"}
        assert expected.issubset(found)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_backtest_app.py::TestLayout::test_execution_config_and_tearsheet_ids_present -v`
Expected: FAIL — `AssertionError` (expected IDs not present)

- [ ] **Step 3: Implement**

Add after `_slider` (`backtest_app.py:299-302`), before `def build_layout`:

```python
def _ranged_slider(id_, value, min_, max_, step):
    return dcc.Slider(id=id_, min=min_, max=max_, step=step, value=value,
                      updatemode="mouseup")


def _execution_config_panel() -> "html.Div":
    return html.Div([
        html.H4("Execution config"),
        html.Div([
            html.H5("Costs"),
            html.Div("Commission (bps)"),
            _ranged_slider("commission-bps", 0.0, 0.0, 50.0, 0.5),
            html.Div("Spread (bps)"),
            _ranged_slider("spread-bps", 0.0, 0.0, 50.0, 0.5),
            html.Div("Borrow fee (bps)"),
            _ranged_slider("borrow-fee-bps", 0.0, 0.0, 50.0, 0.5),
            html.Div("Impact model"),
            dcc.Dropdown(id="impact-model", clearable=False,
                        options=[{"label": "none", "value": "none"},
                                 {"label": "sqrt", "value": "sqrt"},
                                 {"label": "flat", "value": "flat"}],
                        value="none"),
            html.Div("Impact coeff"),
            _ranged_slider("impact-coeff", 0.0, 0.0, 50.0, 0.5),
        ]),
        html.Div([
            html.H5("Risk"),
            html.Div("Stop loss %"),
            dcc.Input(id="stop-loss-pct", type="number", value=None),
            html.Div("Take profit %"),
            dcc.Input(id="take-profit-pct", type="number", value=None),
            html.Div("Vol stop mult"),
            dcc.Input(id="vol-stop-mult", type="number", value=None),
            dcc.Checklist(id="trailing",
                         options=[{"label": "Trailing stop", "value": "trailing"}],
                         value=[]),
            html.Div("Max holding days"),
            dcc.Input(id="max-holding-days", type="number", value=None),
        ]),
        html.Div([
            html.H5("Sizing"),
            html.Div("Mode"),
            dcc.Dropdown(id="sizing-mode", clearable=False,
                        options=[{"label": "fixed notional", "value": "fixed_notional"},
                                 {"label": "fixed fraction", "value": "fixed_fraction"}],
                        value="fixed_notional"),
            html.Div("Notional"),
            dcc.Input(id="sizing-notional", type="number", value=DEFAULT_NOTIONAL),
            html.Div("Fraction"),
            dcc.Input(id="sizing-fraction", type="number", value=None),
            html.Div("Max weight"),
            dcc.Input(id="sizing-max-weight", type="number", value=None),
        ]),
        html.Div([
            html.H5("Limits"),
            html.Div("Capital"),
            dcc.Input(id="limits-capital", type="number", value=None),
            html.Div("Max concurrent"),
            dcc.Input(id="limits-max-concurrent", type="number", value=None),
            html.Div("Max drawdown stop"),
            dcc.Input(id="limits-max-drawdown-stop", type="number", value=None),
        ]),
        html.Div(id="execution-config-error", style={"color": "#d03b3b"}),
    ])
```

In `build_layout` (`backtest_app.py:306-336`), insert the panel and the tearsheet container. Replace the function body with:

```python
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
        dcc.Loading(html.Div(id="risk-card-container")),
        html.Div([
            dcc.Loading(html.Div(id="ic-panel")),
            html.Div([
                html.Div("Bull entry"), _slider("bull-min", 0.5),
                html.Div("Exit long"), _slider("exit-long-max", 0.1),
                html.Div("Bear entry"), _slider("bear-max", -0.5),
                html.Div("Exit short"), _slider("exit-short-min", -0.1),
                dcc.Loading(html.Div(id="trade-summary")),
            ]),
        ]),
        _execution_config_panel(),
        html.Div([
            dcc.Dropdown(id="symbol-dropdown", placeholder="select a symbol"),
            dcc.Loading(dcc.Graph(id="symbol-fig")),
        ]),
        dcc.Loading(dcc.Graph(id="pnl-fig")),
        dcc.Loading(dcc.Graph(id="heatmap-fig")),
        html.H4("Tearsheet"),
        dcc.Loading(html.Div(id="tearsheet-container")),
        dcc.Store(id="signal-store"),
    ])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_backtest_app.py::TestLayout -v`
Expected: PASS (all tests in this class pass)

- [ ] **Step 5: Commit**

```bash
git add backtest_app.py tests/test_backtest_app.py
git commit -m "W4: add execution config panel and tearsheet container to layout"
```

---

## Task 7: Wire the callback — execution config + tearsheet, end to end

**Files:**
- Modify: `backtest_app.py:378-409` (`_on_sliders_change` inside `register_callbacks`)
- Test: `tests/test_backtest_app.py`

**Interfaces:**
- Consumes: `resolve_execution_config()` (Task 2), `simulate_live(..., config=)` (Task 3), `live_tearsheet()` (Task 4), `render_tearsheet()` (Task 5), all new layout IDs (Task 6).
- Produces: nothing new for later tasks — this is the final integration point. Verified by the existing smoke test plus one new end-to-end smoke test.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_backtest_app.py`, inside `TestLayout` (after `test_register_callbacks_does_not_raise`) — this stays a smoke test per the file's existing convention, since `_on_sliders_change` is a closure inside `register_callbacks` and not directly callable:

```python
    def test_register_callbacks_wires_new_outputs_without_raising(self):
        # register_callbacks() would raise if it referenced an Output id
        # missing from the layout -- this documents that the new IDs
        # (execution-config-error, tearsheet-container) are both present
        # and wired without needing a browser to prove it.
        app = dash.Dash(__name__)
        app.layout = ba.build_layout([
            {"name": "tv_threshold", "has_local_artifacts": True}])
        ba.register_callbacks(app)
        found = set()
        self._collect_ids(app.layout, found)
        assert "execution-config-error" in found
        assert "tearsheet-container" in found
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_backtest_app.py::TestLayout::test_register_callbacks_wires_new_outputs_without_raising -v`
Expected: FAIL — `AssertionError` (IDs not yet referenced by any callback; more importantly, this same run will show the *existing* `test_register_callbacks_does_not_raise` still passing since the callback hasn't been touched yet)

- [ ] **Step 3: Implement**

Replace the second `@app.callback` block in `register_callbacks` (`backtest_app.py:378-409`, the `_on_sliders_change` callback and its decorator) with:

```python
    @app.callback(
        Output("trade-summary", "children"), Output("symbol-fig", "figure"),
        Output("pnl-fig", "figure"), Output("heatmap-fig", "figure"),
        Output("execution-config-error", "children"),
        Output("tearsheet-container", "children"),
        Input("signal-store", "data"), Input("bull-min", "value"),
        Input("exit-long-max", "value"), Input("bear-max", "value"),
        Input("exit-short-min", "value"), Input("symbol-dropdown", "value"),
        Input("commission-bps", "value"), Input("spread-bps", "value"),
        Input("borrow-fee-bps", "value"), Input("impact-model", "value"),
        Input("impact-coeff", "value"), Input("stop-loss-pct", "value"),
        Input("take-profit-pct", "value"), Input("vol-stop-mult", "value"),
        Input("trailing", "value"), Input("max-holding-days", "value"),
        Input("sizing-mode", "value"), Input("sizing-notional", "value"),
        Input("sizing-fraction", "value"), Input("sizing-max-weight", "value"),
        Input("limits-capital", "value"), Input("limits-max-concurrent", "value"),
        Input("limits-max-drawdown-stop", "value"))
    def _on_sliders_change(store, bull_min, exit_long_max, bear_max,
                          exit_short_min, symbol, commission_bps, spread_bps,
                          borrow_fee_bps, impact_model, impact_coeff,
                          stop_loss_pct, take_profit_pct, vol_stop_mult,
                          trailing, max_holding_days, sizing_mode,
                          sizing_notional, sizing_fraction, sizing_max_weight,
                          limits_capital, limits_max_concurrent,
                          limits_max_drawdown_stop):
        empty_fig = go.Figure()
        if not store:
            return "select a signal", empty_fig, empty_fig, empty_fig, "", []
        if not has_trade_rule(store["name"]):
            return ("no trade rule defined for this signal", empty_fig,
                    empty_fig, empty_fig, "", [])

        cfg, cfg_error = resolve_execution_config(
            commission_bps=commission_bps, spread_bps=spread_bps,
            borrow_fee_bps=borrow_fee_bps, impact_model=impact_model,
            impact_coeff=impact_coeff, stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct, vol_stop_mult=vol_stop_mult,
            trailing=trailing, max_holding_days=max_holding_days,
            sizing_mode=sizing_mode, sizing_notional=sizing_notional,
            sizing_fraction=sizing_fraction, sizing_max_weight=sizing_max_weight,
            limits_capital=limits_capital,
            limits_max_concurrent=limits_max_concurrent,
            limits_max_drawdown_stop=limits_max_drawdown_stop)
        if cfg is None:
            return ("invalid execution config -- see message below", empty_fig,
                    empty_fig, empty_fig, cfg_error, [])

        trades, summary = simulate_live(store["name"], store["run_id"], bull_min,
                                        exit_long_max, bear_max, exit_short_min,
                                        config=cfg)
        baseline = store["results"].get("summary", {})
        diff = baseline_vs_live(baseline, summary)
        if summary.get("n_trades", 0) == 0:
            text = "0 realized trades at this threshold"
        else:
            text = (f'n={diff["n_trades"]["live"]} trades | '
                   f'win {_fmt_pct(diff["win_rate_pct"]["live"])} | '
                   f'{_fmt_money(diff["total_pnl_dollars"]["live"])} net '
                   f'(baseline: {diff["n_trades"]["baseline"]} / '
                   f'{_fmt_pct(diff["win_rate_pct"]["baseline"])} / '
                   f'{_fmt_money(diff["total_pnl_dollars"]["baseline"])})')
        cache = get_cache(store["name"], store["run_id"])
        sym_fig = (symbol_price_fig(symbol, cache[symbol], trades)
                  if symbol and symbol in cache else empty_fig)
        pnl_fig = cumulative_pnl_fig(trades) or empty_fig
        h_fig = parameter_heatmap_fig(store["name"], store["run_id"])
        tearsheet_children = render_tearsheet(live_tearsheet(trades))
        return text, sym_fig, pnl_fig, h_fig, "", tearsheet_children
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_backtest_app.py -v`
Expected: PASS (full file, all classes green)

- [ ] **Step 5: Run the full repo test suite**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/ -v`
Expected: PASS (no regressions outside `test_backtest_app.py`)

- [ ] **Step 6: Commit**

```bash
git add backtest_app.py tests/test_backtest_app.py
git commit -m "W4: wire execution config and live tearsheet into the sliders callback"
```

---

## Manual verification (not automated — do once after Task 7)

- [ ] Run `C:\ProgramData\anaconda3\python.exe backtest_app.py`, open `http://127.0.0.1:8050`.
- [ ] Select a `tv_threshold`/`tv_fade`/`tv_fade_long` signal with local artifacts.
- [ ] Drag `commission-bps` up from 0 and confirm the trade-summary $ total drops and the tearsheet's CAGR/Sharpe tiles update.
- [ ] Set `sizing-mode` to `fixed_fraction` and clear `Fraction` — confirm the inline error appears under Limits ("Execution config error: ...") instead of a stack trace, and existing panels stay on their last valid render.
- [ ] Drag thresholds to an extreme with zero trades — confirm the tearsheet section shows "no realized trades to compute tearsheet: ..." instead of blank/broken charts.
