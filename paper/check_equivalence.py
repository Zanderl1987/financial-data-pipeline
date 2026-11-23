"""Equivalence check: paper.sim (slots=None) vs the campaign engine.

Run from the main clone with PYTHONPATH = this worktree (see task notes):
    python -u -m paper.check_equivalence
Trade identity/timing only is compared (symbol, side, entry_date, exit_date).
"""
from __future__ import annotations

import random
import sys

import numpy as np
import pandas as pd

import paper
from paper.sim import simulate_account
from paper.strategies import STRATEGIES

END = "2026-09-11"
NOTIONAL = 10_000.0


def _classify(row, bad_syms, joey_sig, joey_slug, joey_px=None):
    """Return class label for one mismatch row."""
    sym = row["symbol"]
    if sym in bad_syms:
        return "documented_difference"
    if row["slug"] == joey_slug and sym in joey_sig:
        sig = joey_sig[sym]
        dates = sig.index[sig.to_numpy(bool)]
        pairs = ((row["c_sig"], row["c_exit_sig"]), (row["s_sig"], row["s_exit_sig"]))
        for a, b in pairs:
            if pd.notna(a) and pd.notna(b) and ((dates > a) & (dates < b)).any():
                return "joey_expected"
        # a signal ON the exit signal bar also re-anchors the ffilled ATR used
        # by that very bar's exit test (atr_arr[j]); same mechanism, shown apart
        for a, b in pairs:
            if pd.notna(a) and pd.notna(b) and ((dates > a) & (dates == b)).any():
                return "joey_expected_exitbar"
    if row["slug"] == joey_slug and sym in joey_sig and joey_px is not None:
        # The port's replay opens walk positions on EVERY raw signal, but the
        # campaign only floor-gates entries afterwards. A sub-floor signal opens
        # a walk position anchored at a different entry price; its exit flag is
        # then used for the next real trade. Shown apart from the ATR kind.
        sig = joey_sig[sym]
        dates = sig.index[sig.to_numpy(bool)]
        for a in (row["c_sig"], row["s_sig"]):
            if pd.notna(a):
                prev = dates[dates < a]
                if len(prev) and joey_px[sym].loc[prev[-1]] < 5.0:
                    return "joey_floor_replay"
    return "unexpected"


def _mark_cascades(mm):
    """Relabel 'unexpected' joey rows that start inside (or whose signal falls on/before the exit fill of) a
    position of an expected-kind mismatch on the same symbol: once an exit moves,
    the engine and the sim re-enter on different signals. Chains extend."""
    until = {}
    order = mm.sort_values(["symbol", "entry_date"]).index
    for i in order:
        r = mm.loc[i]
        sym = r["symbol"]
        end_ = max([x for x in (r["c_exit"], r["s_exit"], r["entry_date"])
                    if pd.notna(x)])
        sig = r["c_sig"] if pd.notna(r["c_sig"]) else r["s_sig"]
        if r["cls"] == "unexpected" and sym in until and sig <= until[sym]:
            mm.loc[i, "cls"] = "joey_cascade"
        if mm.loc[i, "cls"] in ("joey_expected", "joey_expected_exitbar", "joey_floor_replay", "joey_cascade"):
            until[sym] = max(until.get(sym, end_), end_)


def main(n_symbols: int = 100, seed: int = 0, out_csv: str | None = None):
    import query
    from evaluation.universe import clean_symbols
    from evaluation.trades import simulate
    from strategies import stage3
    from strategies.ports import joey_stochrsi_atr as joey

    print("paper package:", paper.__file__)
    syms = query.symbols("yfinance_universe_prices")
    syms = clean_symbols(syms, price_table="yfinance_universe_prices")
    dev = sorted(s for s in syms if not stage3._is_holdout_symbol(s))
    sample = sorted(random.Random(seed).sample(dev, min(n_symbols, len(dev))))
    print(f"dev symbols {len(dev)}, sampled {len(sample)}")
    cache = stage3.build_dev_cache(sample)
    frames = {s: df.sort_index() for s, df in cache.items()}
    start = str(min(df.index.min() for df in frames.values()).date())
    end = pd.Timestamp(END)
    print(f"frames {len(frames)}, window {start}..{END}")

    bad_syms = {s for s, df in frames.items()
                if not np.isfinite(df["close"].to_numpy(float)).all()
                or (df["close"].to_numpy(float) <= 0).any()}
    print(f"symbols with bad closes: {len(bad_syms)}")
    joey_sig = {s: joey.compute(df)["entries"].fillna(False).astype(bool)
                for s, df in frames.items()}

    joey_px = {s: df["close"] for s, df in frames.items()}
    all_rows = []
    for slug, spec in STRATEGIES.items():
        rule, _ = stage3.load_rule_for(slug)
        rule = stage3.with_price_floor(rule)
        camp = simulate(rule, cache, notional=NOTIONAL, config=stage3.cost_config(10.0))
        n_camp_all = len(camp)
        camp = camp[camp["exit_date"] <= end].copy()
        # Parity run: slot_usd huge so the whole-share skip (close > $1000, a
        # production rule the engine lacks) cannot hide timing differences.
        sim = simulate_account(frames, spec, start_equity=1e6, slots=None,
                               slot_usd=1e12, start=start, end=END).trades
        sim_usd = simulate_account(frames, spec, start_equity=1e6, slots=None,
                                   slot_usd=NOTIONAL, start=start, end=END).trades
        sim_ret = (sim_usd["net_pnl"] / (sim_usd["shares"] * sim_usd["entry_price"])
                   * 100.0)
        sim_prod = simulate_account(frames, spec, start_equity=1e6, slots=None,
                                    start=start, end=END).trades
        c = camp.rename(columns={"entry_signal_date": "c_sig",
                                 "exit_signal_date": "c_exit_sig",
                                 "exit_date": "c_exit"})[
            ["symbol", "side", "entry_date", "c_sig", "c_exit_sig", "c_exit"]]
        s = sim.rename(columns={"sig_date": "s_sig", "exit_sig_date": "s_exit_sig",
                                "exit_date": "s_exit"})[
            ["symbol", "side", "entry_date", "s_sig", "s_exit_sig", "s_exit"]]
        m = c.merge(s, on=["symbol", "side", "entry_date"], how="outer", indicator=True)
        both = m["_merge"] == "both"
        ok = both & (m["c_exit"] == m["s_exit"])
        mm = m[~ok].copy()
        mm["kind"] = np.where(mm["_merge"] == "left_only", "campaign_only",
                     np.where(mm["_merge"] == "right_only", "sim_only", "exit_differs"))
        mm["slug"] = slug
        mm["cls"] = [_classify(r, bad_syms, joey_sig, joey.SLUG, joey_px)
                     for r in mm.to_dict("records")]
        if slug == joey.SLUG:
            _mark_cascades(mm)
        all_rows.append(mm.drop(columns="_merge"))
        print(f"\n=== {slug} ===")
        print(f"campaign trades (all/in-window): {n_camp_all}/{len(c)}")
        print(f"sim trades: {len(s)}  (production slot_usd=1000: {len(sim_prod)})")
        print(f"P&L at ${NOTIONAL:,.0f}/trade, net of costs (campaign cost_config(10.0) "
              f"vs sim 1bp + sqrt-law, whole shares):")
        print(f"  campaign: n={len(camp)}  total net P&L ${camp['pnl_dollars'].sum():,.0f}"
              f"  avg return/trade {camp['pnl_pct'].mean():.3f}%")
        print(f"  sim     : n={len(sim_usd)}  total net P&L ${sim_usd['net_pnl'].sum():,.0f}"
              f"  avg return/trade {sim_ret.mean():.3f}%")
        print(f"  sim borrow fees included above: ${sim_usd['borrow_usd'].sum():,.0f}"
              f" (campaign config charges none unless set); sim trades with close"
              f" > ${NOTIONAL:,.0f} skipped: {len(camp) - len(sim_usd)}")
        key = ["symbol", "side", "entry_date"]
        c_keys = set(map(tuple, c[key].to_numpy()))
        p_keys = set(map(tuple, sim_prod[key].to_numpy()))
        print(f"production-slot run: campaign trades absent from sim: "
              f"{len(c_keys - p_keys)}, sim trades absent from campaign: "
              f"{len(p_keys - c_keys)} (whole-share skip for close > $1000)")
        print(f"matched (symbol, side, entry_date, exit_date): {int(ok.sum())}")
        print(f"mismatches: {len(mm)}")
        if len(mm):
            print(mm.groupby(["cls", "kind"]).size().to_string())
            un = mm[mm["cls"] == "unexpected"].head(10)
            if len(un):
                print("first unexpected:")
                print(un[["symbol", "side", "entry_date", "kind", "c_exit", "s_exit"]]
                      .to_string(index=False))
    res = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
    if out_csv:
        res.to_csv(out_csv, index=False)
        print("\nmismatch table:", out_csv)
    return res


if __name__ == "__main__":
    out = ("C:/Users/zande/PycharmProjects/fdp-paper/.superpowers/sdd/"
           "2026-11-23-paper-trading/task-3-mismatches.csv")
    main(out_csv=out)
