"""
Progress math for the Massive option-listings backfill (strike-intro study).

Pure readers over what massive_option_listings_pipeline.py leaves on disk --
per-symbol checkpoints and the summary/changes parquet chunks -- so the live
dashboard (scripts/massive_backfill_dashboard.py) never talks to the API or
touches the running job.
"""

import glob
import json
import os

import pandas as pd


def _root_dirs(out_root: str):
    raw = os.path.join(out_root, "raw", "massive")
    return (os.path.join(raw, "option_listing_changes"),
            os.path.join(raw, "option_chain_summary"),
            os.path.join(out_root, "state", "massive_listings"))


def load_progress(out_root: str, symbols: list[str], days: list[str]) -> pd.DataFrame:
    """One row per symbol: checkpoint date, days done/total, percent, status."""
    _, _, state_dir = _root_dirs(out_root)
    rows = []
    for sym in symbols:
        meta = os.path.join(state_dir, f"{sym}.json")
        last = None
        if os.path.exists(meta):
            try:
                with open(meta, encoding="utf-8") as f:
                    last = json.load(f)["last_date"]
            except (OSError, ValueError, KeyError):
                last = None          # caught mid-write; next refresh reads it
        done = sum(d <= last for d in days) if last else 0
        status = "queued" if done == 0 else ("done" if done >= len(days) else "in progress")
        rows.append({"symbol": sym, "last_date": last, "days_done": done,
                     "days_total": len(days), "pct": round(100.0 * done / len(days), 1),
                     "status": status})
    return pd.DataFrame(rows)


def estimate_eta(progress: pd.DataFrame, started: pd.Timestamp, now: pd.Timestamp) -> dict:
    """Remaining trading-days of work and hours left at the observed rate."""
    done = int(progress["days_done"].sum())
    remaining = int(progress["days_total"].sum()) - done
    hours = (now - started).total_seconds() / 3600
    if done == 0 or hours <= 0:
        return {"remaining_days": remaining, "hours_left": None, "days_per_hour": None}
    rate = done / hours
    return {"remaining_days": remaining, "hours_left": round(remaining / rate, 1),
            "days_per_hour": round(rate, 2)}


def _read_files(files: list[str]) -> pd.DataFrame:
    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(f))
        except Exception:  # noqa: BLE001 -- a file the job is still writing
            continue
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _read_all(directory: str) -> pd.DataFrame:
    return _read_files(glob.glob(os.path.join(directory, "**", "*.parquet"), recursive=True))


def load_window(out_root: str) -> dict | None:
    """The job's fixed run window and launch time, from its run manifest."""
    path = os.path.join(_root_dirs(out_root)[2], "run.json")
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return None
    return {"start": m["start"], "end": m["end"],
            "launched_at": pd.Timestamp(m["launched_at"])}


def load_summaries(out_root: str) -> pd.DataFrame:
    return _read_all(_root_dirs(out_root)[1])


def load_changes(out_root: str, symbol: str) -> pd.DataFrame:
    changes_dir = _root_dirs(out_root)[0]
    return _read_files(glob.glob(
        os.path.join(changes_dir, "**", f"option_listing_changes_{symbol}_*.parquet"),
        recursive=True))
