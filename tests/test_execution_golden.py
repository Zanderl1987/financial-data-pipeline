"""
test_execution_golden.py -- golden masters pinning execution behavior across the
W1 Step A refactor (see docs/superpowers/specs/2026-08-16-execution-engine-
unification-design.md).

Step A moves cost arithmetic out of backtest.py and event_backtest.py into
evaluation/execution.py with ZERO behavior change. These snapshots are the proof.
They were generated from the pre-refactor code and must reproduce EXACTLY --
`==`, not `approx`. A failure here means the refactor changed results, which is
the one thing Step A is not allowed to do.

Coverage is chosen to hit every branch of the cost math specifically:
  - a zero-cost baseline
  - commission + spread
  - borrow fee (the only path exercising short exposure)
  - slippage_model="sqrt_impact", which means DIFFERENT things in the two engines
    (backtest.py: turnover**0.5 * coeff; event_backtest.py: flat 10 bps) and so
    must be pinned separately in each
  - risk controls (vol target / max weight / drawdown stop) as cheap insurance

Regenerate deliberately with UPDATE_EXECUTION_GOLDEN=1 -- never to make a red
test green.
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

import backtest as bt                      # noqa: E402
import event_backtest as eb                # noqa: E402

GOLDEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "golden", "execution_step_a.json")
UPDATE = os.environ.get("UPDATE_EXECUTION_GOLDEN") == "1"


# --------------------------------------------------------------- synthetic data

def _returns_matrix(n_days: int = 250, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=n_days)
    syms = ["A", "B", "C", "D", "E", "F", "G", "H"]
    return pd.DataFrame(rng.normal(0.0004, 0.015, (n_days, len(syms))),
                        index=dates, columns=syms)


def _signal_long(R: pd.DataFrame, seed: int = 11) -> pd.DataFrame:
    """A signal uncorrelated with returns -- we are pinning arithmetic, not edge."""
    rng = np.random.default_rng(seed)
    scores = pd.DataFrame(rng.normal(0, 1, R.shape), index=R.index, columns=R.columns)
    return (scores.reset_index().melt(id_vars="index", var_name="symbol",
                                      value_name="composite")
            .rename(columns={"index": "date"}).dropna())


def _closes(n: int = 300, seed: int = 3) -> pd.Series:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=n)
    px = 100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.014, n)))
    return pd.Series(px, index=dates)


# --------------------------------------------------------------- serialization

def _clean(v):
    """JSON-safe, precision-preserving. NaN/inf become tagged strings so a NaN
    never silently compares unequal to itself and hides a real change."""
    if isinstance(v, float):
        if math.isnan(v):
            return "__nan__"
        if math.isinf(v):
            return "__inf__" if v > 0 else "__-inf__"
        return v
    if isinstance(v, (np.floating,)):
        return _clean(float(v))
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    return v


def _series_fingerprint(s: pd.Series) -> dict:
    return {"n": int(len(s)), "values": [_clean(float(x)) for x in s.to_numpy()]}


# --------------------------------------------------------------- case builders

BACKTEST_CASES = {
    "baseline": {},
    "commission_spread": {"cost_bps": 10.0, "spread_bps": 20.0},
    "borrow_fee": {"cost_bps": 10.0, "borrow_fee_bps": 50.0, "long_short": True},
    "sqrt_impact_default_coeff": {"cost_bps": 5.0, "slippage_model": "sqrt_impact"},
    "sqrt_impact_explicit_coeff": {"cost_bps": 5.0, "slippage_model": "sqrt_impact",
                                   "adv_impact_coeff": 0.25},
    "risk_controls": {"cost_bps": 10.0, "vol_target": 0.10, "max_weight": 0.30,
                      "max_drawdown_stop": 0.15},
}

SCENARIO_CASES = {
    "baseline": {},
    "commission_spread": {"cost_bps": 10.0, "spread_bps": 20.0},
    "flat_impact": {"cost_bps": 10.0, "spread_bps": 20.0,
                    "slippage_model": "sqrt_impact"},
    "vol_stop": {"cost_bps": 10.0, "atr_stop_mult": 2.0},
    "stop_and_target": {"cost_bps": 10.0, "stop_loss_pct": 3.0,
                        "take_profit_pct": 5.0},
}


def _run_backtest_case(monkeypatch, kwargs: dict) -> dict:
    R = _returns_matrix()
    sig = _signal_long(R)
    monkeypatch.setattr(bt, "_pick_price_table", lambda *a, **k: "synthetic")
    monkeypatch.setattr(bt, "_returns_matrix", lambda *a, **k: R)
    params = {"quantiles": 4, "rebalance": "M", "long_short": True}
    params.update(kwargs)
    res = bt.backtest(sig, **params)
    return {"returns": _series_fingerprint(res.returns),
            "metrics": _clean(res.metrics)}


def _run_scenario_case(monkeypatch, kwargs: dict) -> dict:
    px = _closes()
    px.name = "TEST"
    bench = _closes(seed=4)
    bench.name = "SPY"
    series = {"TEST": px, "SPY": bench}
    monkeypatch.setattr(eb, "load_close",
                        lambda symbol, **k: series.get(symbol, pd.Series(dtype=float)).copy())
    monkeypatch.setattr(eb, "load_close_matrix",
                        lambda symbols, **k: pd.DataFrame(
                            {s: series[s] for s in dict.fromkeys(symbols) if s in series}))
    ev = pd.DataFrame({"date": px.index[[40, 80, 120, 160, 200, 240]]})
    params = {"symbols": "TEST", "holding_days": 21, "entry_lag": 1}
    params.update(kwargs)
    sc = eb.scenario(ev, **params)
    return {"trades": _clean(sc.trades.to_dict(orient="records")),
            "metrics": _clean(sc.metrics)}


def _build_all(monkeypatch) -> dict:
    return {
        "backtest": {name: _run_backtest_case(monkeypatch, kw)
                     for name, kw in BACKTEST_CASES.items()},
        "scenario": {name: _run_scenario_case(monkeypatch, kw)
                     for name, kw in SCENARIO_CASES.items()},
    }


# --------------------------------------------------------------- the tests

def _load_golden() -> dict:
    with open(GOLDEN_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def test_golden_file_exists():
    """Guards against the snapshot being deleted to make a failure go away."""
    assert os.path.isfile(GOLDEN_PATH), (
        f"missing {GOLDEN_PATH} -- regenerate with UPDATE_EXECUTION_GOLDEN=1 only "
        "if you intend to re-baseline")


@pytest.mark.parametrize("case", sorted(BACKTEST_CASES))
def test_backtest_matches_golden(monkeypatch, case):
    if UPDATE:
        pytest.skip("regenerating golden file")
    expected = _load_golden()["backtest"][case]
    actual = json.loads(json.dumps(_run_backtest_case(monkeypatch, BACKTEST_CASES[case])))
    assert actual == expected, f"backtest case '{case}' changed"


@pytest.mark.parametrize("case", sorted(SCENARIO_CASES))
def test_scenario_matches_golden(monkeypatch, case):
    if UPDATE:
        pytest.skip("regenerating golden file")
    expected = _load_golden()["scenario"][case]
    actual = json.loads(json.dumps(_run_scenario_case(monkeypatch, SCENARIO_CASES[case])))
    assert actual == expected, f"scenario case '{case}' changed"


def test_cost_cases_actually_differ_from_baseline():
    """A golden master that pins identical numbers everywhere proves nothing.
    This asserts the cost parameters genuinely move results, so the snapshots
    above have real discriminating power."""
    g = _load_golden()
    base = g["backtest"]["baseline"]["returns"]["values"]
    for case in ("commission_spread", "borrow_fee", "sqrt_impact_default_coeff"):
        assert g["backtest"][case]["returns"]["values"] != base, \
            f"backtest '{case}' is identical to baseline -- cost path not exercised"
    s_base = [t["return_pct"] for t in g["scenario"]["baseline"]["trades"]]
    for case in ("commission_spread", "flat_impact"):
        got = [t["return_pct"] for t in g["scenario"][case]["trades"]]
        assert got != s_base, \
            f"scenario '{case}' is identical to baseline -- cost path not exercised"


def test_sqrt_impact_differs_between_engines():
    """Pins the finding that slippage_model='sqrt_impact' is NOT one model:
    backtest.py applies turnover**0.5 * coeff, event_backtest.py adds a flat
    10 bps. If a future refactor unifies them, this test fails and forces the
    change to be a deliberate, documented decision rather than a silent one."""
    g = _load_golden()
    bt_plain = g["backtest"]["sqrt_impact_default_coeff"]["returns"]["values"]
    bt_coeff = g["backtest"]["sqrt_impact_explicit_coeff"]["returns"]["values"]
    assert bt_plain != bt_coeff, \
        "backtest sqrt_impact ignored adv_impact_coeff -- it is coefficient-scaled"

    sc_cost = [t["return_pct"] for t in g["scenario"]["commission_spread"]["trades"]]
    sc_flat = [t["return_pct"] for t in g["scenario"]["flat_impact"]["trades"]]
    deltas = {round(c - f, 6) for c, f in zip(sc_cost, sc_flat)}
    assert deltas == {0.2}, (
        f"scenario sqrt_impact should be a flat 10bps round trip (0.2 pct points), "
        f"got deltas {deltas}")


def test_regenerate(monkeypatch):
    """Only does anything under UPDATE_EXECUTION_GOLDEN=1."""
    if not UPDATE:
        pytest.skip("set UPDATE_EXECUTION_GOLDEN=1 to regenerate")
    os.makedirs(os.path.dirname(GOLDEN_PATH), exist_ok=True)
    with open(GOLDEN_PATH, "w", encoding="utf-8") as f:
        json.dump(_build_all(monkeypatch), f, indent=1, sort_keys=True)
