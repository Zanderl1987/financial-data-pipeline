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


def population(statistic: str, path: str = REG_PATH,
               exclude_input_name=None) -> list:
    """Latest value per input_name for one statistic (deflated-Sharpe trials).

    exclude_input_name, if given, drops that input_name's own rows before
    taking the latest-per-name population. Callers who are about to append
    their own just-computed value to this population (e.g. DSR trials) pass
    their own name here -- otherwise a re-run of an already-registered
    signal double-counts its own prior entry (stale value + current value).
    """
    reg = load(path)
    if reg.empty:
        return []
    sub = reg[(reg["statistic"] == statistic) & reg["value"].notna()]
    if exclude_input_name is not None:
        sub = sub[sub["input_name"] != exclude_input_name]
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
