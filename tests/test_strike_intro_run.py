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
