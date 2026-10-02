"""End-to-end on synthetic data: a planted continuation effect is found; leakage probe holds."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strike_intro import run


def _world(effect: float, seed=0, n_sym=12, n_days=420, onset=4):
    """Prices with jumps; strikes added above/below after some jumps; HIT events drift by `effect`."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-10-07", periods=n_days)
    spy = 100 * np.cumprod(1 + rng.normal(0, 0.006, n_days))
    close = {"SPY": spy}
    ch, summ = [], []
    for s in range(n_sym):
        sym = f"S{s}"
        r = rng.normal(0, 0.012, n_days)
        hit_days = set()
        for e in rng.choice(np.arange(80, n_days - 140), 12, replace=False):
            d = rng.choice([-1, 1])
            r[e] += d * 0.08
            if rng.random() < 0.5:
                hit_days.add((e, d))
                r[e + onset:e + onset + 21] += d * effect / 21   # drift from e+onset
        close[sym] = 50 * np.cumprod(1 + r)
        dates = [d.strftime("%Y-%m-%d") for d in idx]
        for k in range(100, 300, 10):
            ch.append((sym, dates[0], f"{sym}c{k}", float(k), "2027-01-15", "initial"))
        for e, d in hit_days:
            # strikes keep extending the range over time (e grows with time), so
            # every planted listing is a genuine new extreme
            strike = 1000.0 + e if d > 0 else 90.0 - e / 10
            ch.append((sym, dates[e + 1], f"{sym}x{e}", strike, "2027-01-15", "added"))
        summ += [{"symbol": sym, "date": dd, "status": "ok", "replaced_frac": 0.0} for dd in dates]
    changes = pd.DataFrame(ch, columns=["symbol", "date", "contract_ticker", "strike",
                                        "expiration_date", "change"])
    return changes, pd.DataFrame(summ), pd.DataFrame(close, index=idx)


def test_planted_effect_is_found_in_the_primary_test(tmp_path):
    changes, summary, close = _world(effect=0.06)
    res = run.run_study(changes, summary, close, pd.DataFrame(columns=["symbol", "date"]),
                        str(tmp_path), n_perm=1000, n_boot=300)
    prim = res["tests"][res["tests"]["primary"]].iloc[0]
    assert prim["diff"] > 0.02 and prim["p_perm"] < 0.01
    assert os.path.exists(tmp_path / "events.parquet")


def test_no_effect_world_is_not_significant(tmp_path):
    changes, summary, close = _world(effect=0.0, seed=5)
    res = run.run_study(changes, summary, close, pd.DataFrame(columns=["symbol", "date"]),
                        str(tmp_path), n_perm=1000, n_boot=300)
    prim = res["tests"][res["tests"]["primary"]].iloc[0]
    assert prim["p_perm"] > 0.01


def test_complete_symbols_only_counts_finished_checkpoints(tmp_path):
    import json
    d = tmp_path / "state" / "massive_listings"
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"start": "2024-10-06", "end": "2026-10-01",
                                            "launched_at": "2026-10-01T22:38:07"}), encoding="utf-8")
    (d / "A.json").write_text(json.dumps({"last_date": "2026-10-01"}), encoding="utf-8")
    (d / "B.json").write_text(json.dumps({"last_date": "2025-03-01"}), encoding="utf-8")
    assert run.complete_symbols(str(tmp_path)) == ["A"]


def test_leakage_probe_earlier_entry_looks_better():
    # Spec correctness check: if the information arrives right after the move,
    # an (illegal) earlier entry must capture more of it than the e+3 entry.
    # If it didn't, the timing machinery would not be doing what we think.
    from strike_intro import events as ev, measures
    changes, summary, close = _world(effect=0.06, onset=1)
    intros = measures.daily_intros(changes, summary)

    def hit_minus_miss(offset):
        e = ev.attach_intros(ev.find_events(close, "day"), intros, close.index,
                             window=(1, 2), entry_offset=offset)
        e = ev.forward_returns(e, close, [21]).dropna(subset=["ret_21"])
        return e.loc[e["hit"], "ret_21"].mean() - e.loc[~e["hit"], "ret_21"].mean()
    assert hit_minus_miss(0) > hit_minus_miss(3) + 0.01


def test_fewer_than_min_symbols_is_a_clear_error(tmp_path):
    import pytest
    changes, summary, close = _world(effect=0.0, n_sym=1, n_days=300)
    with pytest.raises(ValueError, match="at least"):
        run.run_study(changes, summary, close, pd.DataFrame(columns=["symbol", "date"]),
                      str(tmp_path), n_perm=100, n_boot=50)


def test_intros_are_built_one_symbol_at_a_time():
    # Review #4: never load every symbol's listing changes at once.
    calls = []
    changes, summary, _ = _world(effect=0.0, n_sym=3, n_days=300)

    def fetch(sym):
        calls.append(sym)
        return changes[changes["symbol"] == sym], summary[summary["symbol"] == sym]
    out = run.intros_by_symbol(["S0", "S1", "S2"], fetch)
    assert calls == ["S0", "S1", "S2"] and set(out["symbol"]) == {"S0", "S1", "S2"}


def test_study_reports_robustness_rows_and_meta(tmp_path):
    changes, summary, close = _world(effect=0.06)
    res = run.run_study(changes, summary, close, pd.DataFrame(columns=["symbol", "date"]),
                        str(tmp_path), n_perm=200, n_boot=100, universe_size=100)
    ids = set(res["tests"]["test_id"])
    assert "nonoverlap:day|ret_21|all" in ids
    assert "day|ar_21|all" in ids
    assert "day|excess_tercile|ret_21" in ids and "day|excess_tercile|absret_21" in ids
    assert {"sigma60", "headroom"} <= set(res["regression"]["term"])
    assert res["meta"]["universe"] == 100 and res["meta"]["complete"] == 12
    assert "earnings_coverage" in res["meta"] and "trading" in res
