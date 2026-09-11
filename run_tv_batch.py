#!/usr/bin/env python3
"""
Batch runner for TV Strategy Catalog Stage 3.
Runs strategies sequentially with caching, writes to registry.
Supports resume via progress file.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategies.stage3 import (
    dev_cache, load_rule_for, with_price_floor, cost_config,
    run_strategy, registry_rows_for, admitted_slugs
)
from evaluation import registry as ev_registry


PROGRESS_FILE = "tv_batch_progress.json"


def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r") as f:
            return json.load(f)
    return {"completed": [], "failed": [], "run_id": None}


def save_progress(progress):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


def run_batch(slugs: list[str], n_perm: int = 200, workers: int = 4, write_registry: bool = True):
    """Run a batch of strategies with progress tracking."""
    progress = load_progress()
    completed = set(progress["completed"])
    failed = set(progress["failed"])
    
    # Filter to only run uncompleted
    to_run = [s for s in slugs if s not in completed and s not in failed]
    
    if not to_run:
        print("All strategies already completed or failed.")
        return
    
    print(f"Building dev cache...")
    t0 = time.time()
    cache = dev_cache()
    print(f"Cache built: {len(cache)} symbols in {time.time()-t0:.0f}s")

    uhash = ev_registry.universe_hash(cache.keys())
    date_range = f"dev_split_thru_2017-12-31 ({len(cache)} symbols)"
    created_at = datetime.now(timezone.utc).isoformat()
    run_id = progress["run_id"] or ev_registry.new_run_id()
    progress["run_id"] = run_id

    print(f"Run ID: {run_id}")
    print(f"Universe hash: {uhash}")
    print(f"Strategies to run: {len(to_run)}/{len(slugs)}, n_perm={n_perm}, workers={workers}")
    print("=" * 60)

    for i, slug in enumerate(to_run):
        idx = slugs.index(slug) + 1
        print(f"\n[{idx}/{len(slugs)}] {slug}")
        
        meta_path = f"storage/tv_scripts/{slug}.meta.json"
        meta = {}
        if os.path.isfile(meta_path):
            with open(meta_path, "r") as f:
                meta = json.load(f)

        try:
            t0 = time.time()
            row = run_strategy(slug, meta, f"batch_{run_id[:8]}", cache,
                               n_perm=n_perm, seed=0, workers=workers)
            elapsed = time.time() - t0
            
            pnl_p = row.get('pnl_p')
            n_trades = row.get('n_trades')
            total_pnl = row.get('total_pnl_net')
            print(f"  Done in {elapsed:.0f}s: n_trades={n_trades}, pnl_p={pnl_p}, total_pnl={total_pnl}")

            if write_registry and pnl_p is not None:
                reg_rows = registry_rows_for(row, run_id, uhash, date_range, created_at)
                if not reg_rows.empty:
                    ev_registry.append(reg_rows)
                    print(f"  Written to registry")

            progress["completed"].append(slug)
            save_progress(progress)

        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            progress["failed"].append({"slug": slug, "error": str(e)})
            save_progress(progress)

    print("\n" + "=" * 60)
    print(f"Batch complete. Completed: {len(progress['completed'])}, Failed: {len(progress['failed'])}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="TV Strategy Catalog Stage 3 batch runner")
    ap.add_argument("--slugs-file", required=True, help="File with one slug per line")
    ap.add_argument("--n-perm", type=int, default=200)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--no-registry", action="store_true")
    args = ap.parse_args()

    with open(args.slugs_file, "r") as f:
        slugs = [ln.strip() for ln in f if ln.strip()]

    run_batch(slugs, n_perm=args.n_perm, workers=args.workers, write_registry=not args.no_registry)