# Screening driver for a batch of slug lists (resumable via outfile).
# 50-symbol dev cache, n_perm=20, workers=0, no registry write.
# Usage: python screen_batch.py <listfile> <outjson>
import json
import os
import sys
import time
import multiprocessing as mp
import query as q
from evaluation.universe import clean_symbols
from strategies.stage3 import build_dev_cache, dev_holdout_symbols, run_strategy


def main():
    listfile, outjson = sys.argv[1], sys.argv[2]
    slugs = [s.strip() for s in open(listfile) if s.strip()]

    done = {}
    if os.path.isfile(outjson):
        done = json.load(open(outjson))
    todo = [s for s in slugs if s not in done]
    if todo:
        symbols = q.symbols("yfinance_universe_prices")
        clean = clean_symbols(symbols, price_table="yfinance_universe_prices")
        dev, _ = dev_holdout_symbols(clean)
        cache = build_dev_cache(dev[:50])
        print(f"cache symbols: {len(cache)}  todo: {len(todo)}/{len(slugs)}", flush=True)

        for i, slug in enumerate(todo):
            t0 = time.time()
            print(f"--- [{len(done) + i + 1}/{len(slugs)}] {slug} ---", flush=True)
            try:
                row = run_strategy(slug, {}, "", cache, n_perm=20, seed=0, workers=0)
                done[slug] = {
                    "n_trades": row["n_trades"],
                    "pnl_p": row["pnl_p"],
                    "pnl_p_5bps": row["pnl_p_5bps"],
                    "pnl_p_20bps": row["pnl_p_20bps"],
                    "profit_factor": row["profit_factor"],
                    "win_rate": row["win_rate"],
                    "total_pnl_net": row["total_pnl_net"],
                    "cost_fragile": row["cost_fragile"],
                    "mechanism_family": row["mechanism_family"],
                }
                print(f"done {time.time() - t0:.0f}s: n={row['n_trades']} "
                      f"pnl_p={row['pnl_p']} pnl={row['total_pnl_net']:.0f} "
                      f"pf={row['profit_factor']}", flush=True)
            except Exception as e:
                done[slug] = {"error": f"{type(e).__name__}: {e}"}
                print(f"ERROR {time.time() - t0:.0f}s: {e}", flush=True)
            with open(outjson, "w", encoding="utf-8") as fh:
                json.dump(done, fh, indent=1)

    print(f"\n=== BATCH SCREEN ({outjson}) ===")
    for s in slugs:
        r = done.get(s, {})
        if "error" in r:
            print(f"{s:50s} ERROR {r['error']}")
        elif r:
            print(f"{s:50s} n={r['n_trades']:7d} pnl_p={r['pnl_p']} "
                  f"pnl={r['total_pnl_net']:12.0f} pf={r['profit_factor']:.3f}")


if __name__ == "__main__":
    mp.freeze_support()
    main()