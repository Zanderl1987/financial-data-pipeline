"""Equivalence check: paper.sim (slots=None) vs the campaign engine.

Run from the main clone with PYTHONPATH = this worktree:
    python -u -m paper.check_equivalence

GATE (mechanism control): the sim is run with a "port-exit" spec -- entries as
in paper.strategies, but held_exit = the port's own exit flag at bar j (the
campaign's exit rule). That run must match the campaign 100% on
(symbol, side, entry_date, exit_date). This proves the sim's entry / timing /
re-entry machinery. The remaining joey difference in the real spec is then the
intended held_exit design (ATR locked at the position's own signal bar).
P&L is compared separately.
"""
from __future__ import annotations

import random
import sys

import numpy as np
import pandas as pd

import paper
from paper.sim import simulate_account
from paper.strategies import STRATEGIES, StrategySpec

END = "2026-09-11"
NOTIONAL = 10_000.0
HUGE = 1e12          # slot_usd that disables the whole-share skip (parity runs)
KEY = ["symbol", "side", "entry_date"]
OUT_DIR = ("C:/Users/zande/PycharmProjects/fdp-paper/.superpowers/sdd/"
           "2026-11-23-paper-trading/")


def _port_exit_spec(spec: StrategySpec, rule) -> StrategySpec:
    """Same entries; held_exit = the campaign rule's own exit flag at bar j."""
    from evaluation.trades import rule_flags

    def held_exit(df):
        _, lx, _, sx = rule_flags(rule, df)

        def fn(j, sig_i, entry_price, side):
            return bool(lx[j] if side == "long" else sx[j])
        return fn
    return StrategySpec(spec.slug, spec.entries, held_exit)


def _merge(c, s):
    m = c.merge(s, on=KEY, how="outer", indicator=True)
    ok = (m["_merge"] == "both") & (m["c_exit"] == m["s_exit"])
    mm = m[~ok].copy()
    mm["kind"] = np.where(mm["_merge"] == "left_only", "campaign_only",
                 np.where(mm["_merge"] == "right_only", "sim_only", "exit_differs"))
    return m, ok, mm.drop(columns="_merge")


def _cs(camp, sim):
    c = camp.rename(columns={"entry_signal_date": "c_sig",
                             "exit_signal_date": "c_exit_sig",
                             "exit_date": "c_exit"})[
        KEY + ["c_sig", "c_exit_sig", "c_exit"]]
    s = sim.rename(columns={"sig_date": "s_sig", "exit_sig_date": "s_exit_sig",
                            "exit_date": "s_exit"})[
        KEY + ["s_sig", "s_exit_sig", "s_exit"]]
    return c, s


def main(n_symbols: int = 100, seed: int = 0, out_prefix: str = OUT_DIR + "task-3-fix1-"):
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
    joey_sig = {s: joey.compute(df)["entries"].fillna(False).astype(bool)
                for s, df in frames.items()}

    gate_ok = True
    all_rows = []
    for slug, spec in STRATEGIES.items():
        rule, _ = stage3.load_rule_for(slug)
        rule = stage3.with_price_floor(rule)
        camp = simulate(rule, cache, notional=NOTIONAL,
                        config=stage3.cost_config(10.0))
        n_all = len(camp)
        camp = camp[camp["exit_date"] <= end].copy()
        cspec = _port_exit_spec(spec, rule)
        print(f"\n=== {slug} ===")
        print(f"campaign trades (all / exit<=end): {n_all}/{len(camp)}")

        # ---- GATE: control run (port exits), whole-share skip disabled ----
        ctl = simulate_account(frames, cspec, start_equity=1e6, slots=None,
                               slot_usd=HUGE, start=start, end=END).trades
        c, s = _cs(camp, ctl)
        _, ok, mm = _merge(c, s)
        print(f"CONTROL (port exits) sim trades {len(ctl)}; matched on "
              f"(symbol, side, entry_date, exit_date): {int(ok.sum())}/{len(c)}; "
              f"mismatches {len(mm)}")
        if len(mm):
            gate_ok = False
            print(mm.groupby("kind").size().to_string())
            print(mm.head(10)[KEY + ["kind", "c_exit", "s_exit"]].to_string(index=False))
            mm.assign(slug=slug).to_csv(f"{out_prefix}control-{slug}.csv", index=False)

        # ---- whole-share skip, counted from sim runs (control spec) ----
        ctl10 = simulate_account(frames, cspec, start_equity=1e6, slots=None,
                                 slot_usd=NOTIONAL, start=start, end=END).trades
        ctl_k = ctl.set_index(KEY)
        gone = ctl_k.index.difference(ctl10.set_index(KEY).index)
        direct = 0
        for sym, side, ed in gone:
            r = ctl_k.loc[(sym, side, ed)]
            if frames[sym]["close"].at[r["sig_date"]] > NOTIONAL:
                direct += 1
        print(f"whole-share skip at ${NOTIONAL:,.0f}: control trades lost "
              f"{len(gone)}; of which signal-day close > ${NOTIONAL:,.0f} "
              f"(shares=0) {direct}; rest are knock-on re-entries")

        # ---- real spec vs campaign (descriptive, not a gate) ----
        real = simulate_account(frames, spec, start_equity=1e6, slots=None,
                                slot_usd=HUGE, start=start, end=END).trades
        c, s = _cs(camp, real)
        m, ok, mm = _merge(c, s)
        print(f"REAL spec (held_exit design) vs campaign, descriptive, not a gate: "
              f"sim trades {len(real)}, matched {int(ok.sum())}/{len(c)}, "
              f"mismatches {len(mm)}")
        if len(mm):
            if slug == joey.SLUG:
                def has_sig(r):
                    sg = joey_sig[r["symbol"]]
                    d = sg.index[sg.to_numpy(bool)]
                    pairs = ((r["c_sig"], r["c_exit_sig"]), (r["s_sig"], r["s_exit_sig"]))
                    return any(pd.notna(a) and pd.notna(b) and
                               ((d > a) & (d <= b)).any() for a, b in pairs)
                mm["raw_signal_in_hold"] = [has_sig(r) for r in mm.to_dict("records")]
                print(mm.groupby(["kind", "raw_signal_in_hold"]).size().to_string())
            else:
                print(mm.groupby("kind").size().to_string())
            all_rows.append(mm.assign(slug=slug))

        # ---- P&L at $10k/trade ----
        r_off = simulate_account(frames, spec, start_equity=1e6, slots=None,
                                 slot_usd=NOTIONAL, start=start, end=END,
                                 borrow_bps_yr=0.0).trades
        r_on = simulate_account(frames, spec, start_equity=1e6, slots=None,
                                slot_usd=NOTIONAL, start=start, end=END).trades

        def ret(t):
            return t["net_pnl"] / (t["shares"] * t["entry_price"]) * 100.0
        mk = camp.set_index(KEY).join(
            r_off.set_index(KEY)[["net_pnl", "shares", "entry_price"]]
            .rename(columns={"net_pnl": "s_off", "entry_price": "s_px"}), how="inner")
        mk = mk.join(r_on.set_index(KEY)[["net_pnl"]].rename(columns={"net_pnl": "s_on"}))
        mk["s_off_ret"] = mk["s_off"] / (mk["shares"] * mk["s_px"]) * 100.0
        mk["s_on_ret"] = mk["s_on"] / (mk["shares"] * mk["s_px"]) * 100.0
        print(f"P&L at ${NOTIONAL:,.0f}/trade, net of costs")
        print(f"  (a) matched keys only, n={len(mk)} (same symbol/side/entry_date):")
        print(f"      campaign        ${mk['pnl_dollars'].sum():>10,.0f}  avg {mk['pnl_pct'].mean():.3f}%")
        print(f"      sim borrow OFF  ${mk['s_off'].sum():>10,.0f}  avg {mk['s_off_ret'].mean():.3f}%")
        print(f"      sim borrow ON   ${mk['s_on'].sum():>10,.0f}  avg {mk['s_on_ret'].mean():.3f}%")
        print("  (b) totals, all trades each engine produces:")
        print(f"      campaign        n={len(camp):>6}  ${camp['pnl_dollars'].sum():>10,.0f}  avg {camp['pnl_pct'].mean():.3f}%")
        print(f"      sim borrow OFF  n={len(r_off):>6}  ${r_off['net_pnl'].sum():>10,.0f}  avg {ret(r_off).mean():.3f}%")
        print(f"      sim borrow ON   n={len(r_on):>6}  ${r_on['net_pnl'].sum():>10,.0f}  avg {ret(r_on).mean():.3f}%"
              f"  (borrow fees ${r_on['borrow_usd'].sum():,.0f})")

    if all_rows:
        pd.concat(all_rows, ignore_index=True).to_csv(
            out_prefix + "real-spec-mismatches.csv", index=False)
    print("\nGATE (control run 100% match, all strategies):",
          "PASS" if gate_ok else "FAIL")
    return gate_ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
