# Task 5: Interactive Backtest Explorer - Fix Report

## Fixes Applied

### 1. Operator Deviation in `_crossed_up` and `_crossed_down` (CRITICAL)

**Issue**: The crossing detection functions used non-strict inequalities on the previous day's value, deviating from the original specification in `evaluation/adapters.py`.

**Original (incorrect) code**:
```python
def _crossed_up(series, level):
    return (series >= level) & (series.shift(1) <= level)

def _crossed_down(series, level):
    return (series <= level) & (series.shift(1) >= level)
```

**Fixed code**:
```python
def _crossed_up(series, level):
    return (series >= level) & (series.shift(1) < level)

def _crossed_down(series, level):
    return (series <= level) & (series.shift(1) > level)
```

**Impact**: The strict inequalities (`<` and `>` instead of `<=` and `>=`) ensure that a price level must have been strictly beyond the threshold in the previous period to be considered a "crossing" today. This prevents false signals when price merely touches the threshold boundary without truly crossing from the other side.

**Files modified**:
- `backtest_app.py` (lines 64-69)

### 2. Insufficient Boundary Test Coverage (IMPORTANT)

**Issue**: The existing test suite did not exercise the critical boundary case where a price equals a threshold exactly. The test data `[0.0, 0.6, 0.05, -0.6, -0.05]` against thresholds `(0.5, -0.5, 0.1, -0.1)` has no values landing on thresholds, so a strict-vs-non-strict operator bug would pass silently.

**Solution**: 
1. Added comprehensive boundary test `test_boundary_values_do_not_cross_on_equal_previous()` in `TestBuildTvThresholdRule`
2. Test uses data `[0.4, 0.5, 0.6, 0.1, -0.5, -0.4]` where thresholds are exactly 0.5 and -0.5
3. Validates that when previous day's value equals (but is not strictly beyond) a threshold, no cross is detected
4. Fixed existing `test_tighter_bull_threshold_enters_earlier()` to use data starting below the threshold

**Files modified**:
- `tests/test_backtest_app.py` (added test method + fixed existing test)

## Test Results

Command:
```
"C:\ProgramData\anaconda3\python.exe" -m pytest tests/test_backtest_app.py -v
```

Output:
```
============================= test session starts =============================
platform win32 -- Python 3.11.5, pytest-9.1.1, pluggy-1.6.0
collected 8 items

tests/test_backtest_app.py::TestListEvaluatedSignals::test_empty_registry_returns_empty_list PASSED [ 12%]
tests/test_backtest_app.py::TestListEvaluatedSignals::test_lists_unique_sorted_names_with_artifact_flag PASSED [ 25%]
tests/test_backtest_app.py::TestLoadSignal::test_missing_artifacts_returns_error_dict PASSED [ 37%]
tests/test_backtest_app.py::TestLoadSignal::test_loads_run_artifacts_on_success PASSED [ 50%]
tests/test_backtest_app.py::TestBuildTvThresholdRule::test_matches_adapters_tv_threshold_rule_at_default_thresholds PASSED [ 62%]
tests/test_backtest_app.py::TestBuildTvThresholdRule::test_tighter_bull_threshold_enters_earlier PASSED [ 75%]
tests/test_backtest_app.py::TestBuildTvThresholdRule::test_side_is_both PASSED [ 87%]
tests/test_backtest_app.py::TestBuildTvThresholdRule::test_boundary_values_do_not_cross_on_equal_previous PASSED [100%]

============================== 8 passed, 1 warning in 2.29s =========================
```

**Result**: All 8 tests passing, including the new boundary coverage test.
