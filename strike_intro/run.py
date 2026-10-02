"""
Run the strike-introduction study end to end.

  python -m strike_intro.run [--n-perm 10000] [--register]

Only symbols whose Massive checkpoint reached the run manifest's end date are
used, so the study can run (on fewer names) while the backfill continues.
"""
import argparse
import datetime
import glob
import json
import os

import numpy as np
import pandas as pd

from strike_intro import battery, events as ev, excess_model as xm, measures, report
from strike_intro.progress import RunProgress

HORIZONS = ev.HORIZONS
OUT_ROOT = "storage"


def complete_symbols(out_root: str) -> list[str]:
    state = os.path.join(out_root, "state", "massive_listings")
    with open(os.path.join(state, "run.json"), encoding="utf-8") as f:
        end = json.load(f)["end"]
    done = []
    for p in sorted(glob.glob(os.path.join(state, "*.json"))):
        name = os.path.basename(p)[:-5]
        if name == "run":
            continue
        try:
            with open(p, encoding="utf-8") as f:
                last = json.load(f)["last_date"]
        except (OSError, ValueError, KeyError):
            continue
        if last >= end or last >= _last_trading_on_or_before(end):
            done.append(name)
    return done


def _last_trading_on_or_before(d: str) -> str:
    t = pd.Timestamp(d)
    while t.weekday() >= 5:
        t -= pd.Timedelta(days=1)
    return t.strftime("%Y-%m-%d")


def _events_for(trigger, close, intros, earnings, window, offset):
    e = ev.find_events(close, trigger)
    e = ev.attach_intros(e, intros, close.index, window=window, entry_offset=offset)
    if e.empty:
        return e
    e = ev.forward_returns(e, close, HORIZONS)
    e["trigger"] = trigger
    e["earnings"] = (ev.earnings_flag(e, earnings, close.index).values
                     if len(earnings) else False)
    return e


def _vol_outcomes(e, close, bench="SPY"):
    ex = ev.excess_returns(close, bench)
    rv = []
    for _, r in e.iterrows():
        i = close.index.get_loc(r["entry_date"])
        post = ex[r["symbol"]].iloc[i + 1:i + 22]
        pre = ex[r["symbol"]].iloc[max(0, i - 63):i - 3]
        rv.append(post.std() / pre.std() if len(post) == 21 and pre.std() > 0 else np.nan)
    e["absret_21"] = e["ret_21"].abs()
    e["rv_ratio"] = rv
    return e


def _test_rows(e, trig, progress, n_perm, n_boot):
    rows = []
    # Permutation strata = calendar month of entry (controls market-wide timing).
    # Symbol-month (the original spec) left most strata with one event, so the
    # test could barely shuffle: on a synthetic +7pt effect it gave p=0.10 while
    # Welch/MW/bootstrap all gave p<0.001. Month strata: p=0.0005.
    strata = pd.to_datetime(e["entry_date"]).dt.strftime("%Y-%m")
    weeks = pd.to_datetime(e["entry_date"]).dt.strftime("%G-%V")
    subgroups = {"all": np.ones(len(e), bool), "up": (e["direction"] > 0).values,
                 "down": (e["direction"] < 0).values,
                 "earnings": e["earnings"].astype(bool).values,
                 "non-earnings": ~e["earnings"].astype(bool).values}
    measures_ = [("ret", h, f"ret_{h}") for h in HORIZONS] + \
                [("absret", 21, "absret_21"), ("rv_ratio", 21, "rv_ratio")]
    for sg, m in subgroups.items():
        for meas, h, col in measures_:
            if sg != "all" and meas != "ret":
                continue
            hit, miss = m & e["hit"].values, m & ~e["hit"].values
            label = f"{trig}|{col}|{sg}"
            r = battery.compare_groups(e.loc[hit, col], e.loc[miss, col],
                                       strata[hit], strata[miss], weeks[hit], weeks[miss],
                                       n_perm=n_perm, n_boot=n_boot, progress=progress,
                                       label=label)
            rows.append({"test_id": label, "trigger": trig, "measure": meas, "horizon": h,
                         "subgroup": sg, "primary": label == "day|ret_21|all", **r})
    # EXCESS > 0 vs <= 0 (mechanics vs demand)
    if "excess" in e:
        for h in (21, 63):
            col = f"ret_{h}"
            pos, neg = (e["excess"] > 0).values, (e["excess"] <= 0).values
            r = battery.compare_groups(e.loc[pos, col], e.loc[neg, col], strata[pos], strata[neg],
                                       weeks[pos], weeks[neg], n_perm=n_perm, n_boot=n_boot,
                                       progress=progress, label=f"{trig}|excess|{col}")
            rows.append({"test_id": f"{trig}|excess>0|{col}", "trigger": trig,
                         "measure": "excess_split", "horizon": h, "subgroup": "all",
                         "primary": False, **r})
    return rows


def _equity(e, close, cost_bp=10):
    d = e[(e["trigger"] == "day") & e["hit"]].dropna(subset=["ret_21"]).sort_values("entry_date")
    if d.empty:
        return pd.Series(dtype=float)
    pnl = d.groupby("entry_date")["ret_21"].mean() - 2 * cost_bp / 1e4
    return pnl.cumsum()


def run_study(changes, summary, close, earnings, run_dir, progress=None,
              n_perm=10000, n_boot=2000) -> dict:
    os.makedirs(run_dir, exist_ok=True)
    progress = progress or RunProgress(run_dir)
    progress.stage("measures", 0, 1, "per-expiration introductions")
    intros = measures.daily_intros(changes, summary)
    progress.stage("measures", 1, 1, f"{intros['symbol'].nunique()} symbols")
    syms = [s for s in intros["symbol"].unique() if s in close.columns]
    close = close[["SPY"] + syms]

    earn_near = set()
    if len(earnings):
        for _, r in earnings.iterrows():
            j = close.index.searchsorted(pd.Timestamp(r["date"]))
            for k in (j - 1, j, j + 1):
                if 0 <= k < len(close.index):
                    earn_near.add((r["symbol"], close.index[k]))
    progress.stage("excess_model", 0, len(syms), "building panel")
    panel = xm.fit_loso(xm.build_model_panel(intros, close, earn_near), progress)

    all_events = []
    for trig in ("day", "run"):
        progress.stage("events", 0, 1, trig)
        e = _events_for(trig, close, intros, earnings, (1, 2), 3)
        if e.empty:
            continue
        e["excess"] = xm.event_excess(e, panel, close.index).values
        all_events.append(_vol_outcomes(e, close))
    events = pd.concat(all_events, ignore_index=True) if all_events else pd.DataFrame()
    sens = _events_for("day", close, intros, earnings, (1, 1), 2)
    if not sens.empty:
        sens = _vol_outcomes(sens, close)

    rows = []
    for trig in sorted(events["trigger"].unique()):
        rows += _test_rows(events[events["trigger"] == trig], trig, progress, n_perm, n_boot)
    if not sens.empty:
        sens["excess"] = np.nan
        rows += [dict(r, test_id="sens:" + r["test_id"], primary=False) for r in
                 _test_rows(sens.drop(columns=["excess"]), "day", None, n_perm, n_boot)
                 if r["subgroup"] == "all" and r["measure"] == "ret"]
    from evaluation.stats import bh_fdr
    tests = pd.DataFrame(rows)
    sec = bh_fdr(tests[~tests["primary"]].to_dict("records"), alpha=0.05, p_key="p_perm")
    tests["p_adj"] = np.nan
    tests.loc[~tests["primary"], "p_adj"] = sec["p_adj"].values

    d = events[events["trigger"] == "day"].copy()
    d["hit_i"] = d["hit"].astype(int)
    d["earn_i"] = d["earnings"].astype(int)
    prior = []
    for _, r in d.iterrows():
        i = close.index.get_loc(r["event_date"])
        p0 = close[r["symbol"]].iloc[max(0, i - 21)]
        prior.append(r["direction"] * (close[r["symbol"]].iloc[i - 1] / p0 - 1))
    d["prior21"] = prior
    d["abs_sigma"] = d["move_sigma"].abs()
    regression = battery.clustered_ols(d, "ret_21", ["hit_i", "excess", "abs_sigma", "earn_i",
                                                     "prior21"])
    prim = tests[tests["primary"]]
    sd21 = d["ret_21"].std()
    mde21 = (battery.mde(int(prim.iloc[0]["n_a"]), int(prim.iloc[0]["n_b"]), sd21)
             if len(prim) and prim.iloc[0]["n_a"] > 0 and prim.iloc[0]["n_b"] > 0 else np.nan)

    car = ev.car_paths(events, close)
    ranges = intros.merge(
        close.stack().rename("close").reset_index().rename(columns={"level_0": "date",
                                                                     "level_1": "symbol"})
        .assign(date=lambda x: x["date"].dt.strftime("%Y-%m-%d")),
        on=["symbol", "date"], how="left")
    meta = {"universe": len(syms), "complete": len(syms),
            "date_range": f"{close.index.min():%Y-%m-%d}..{close.index.max():%Y-%m-%d}",
            "mde_21": mde21, "n_events": int(len(events))}
    events.to_parquet(os.path.join(run_dir, "events.parquet"), index=False)
    tests.to_parquet(os.path.join(run_dir, "tests.parquet"), index=False)
    regression.to_parquet(os.path.join(run_dir, "regression.parquet"), index=False)
    progress.stage("done", 1, 1, "")
    return {"events": events, "car": car, "tests": tests, "regression": regression,
            "equity": _equity(events, close), "ranges": ranges, "meta": meta}


def _register(tests, run_id, symbols, date_range):
    from evaluation import registry
    now = datetime.datetime.now().isoformat(timespec="seconds")
    rows = []
    for _, t in tests.iterrows():
        for stat in ("diff", "p_perm", "p_adj"):
            rows.append({"run_id": run_id, "input_name": "strike_intro_" + t["test_id"],
                         "input_type": "event_set", "evaluation": "hit_minus_miss",
                         "horizon": int(t["horizon"]), "statistic": stat, "value": t[stat],
                         "n": int(t["n_a"] + t["n_b"]), "universe_hash": registry.universe_hash(symbols),
                         "date_range": date_range, "created_at": now})
    registry.append(pd.DataFrame(rows))


def main():
    import query as q
    from event_backtest import earnings_events, load_close_matrix
    import massive_backfill_progress as mbp

    p = argparse.ArgumentParser()
    p.add_argument("--n-perm", type=int, default=10000)
    p.add_argument("--n-boot", type=int, default=2000)
    p.add_argument("--register", action="store_true")
    args = p.parse_args()

    syms = complete_symbols(OUT_ROOT)
    if not syms:
        raise SystemExit("no symbol has a complete listing history yet")
    window = mbp.load_window(OUT_ROOT)
    run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(OUT_ROOT, "reports", "strike_intro", run_id)
    prog = RunProgress(run_dir)
    prog.put("symbols", syms)
    lst = ",".join(f"'{s}'" for s in syms)
    changes = q.sql(f"SELECT symbol, date, contract_ticker, strike, expiration_date, change "
                    f"FROM option_listing_changes WHERE symbol IN ({lst})")
    summary = q.sql(f"SELECT symbol, date, status, replaced_frac FROM option_chain_summary "
                    f"WHERE symbol IN ({lst})")
    start = (pd.Timestamp(window["start"]) - pd.Timedelta(days=120)).strftime("%Y-%m-%d")
    close = load_close_matrix(syms + ["SPY"], start=start).sort_index()
    earnings = earnings_events(syms)[["symbol", "date"]] if syms else pd.DataFrame()
    res = run_study(changes, summary, close, earnings, run_dir, prog, args.n_perm, args.n_boot)
    res["meta"]["universe"] = 100
    path = report.build_report(res, os.path.join(run_dir, "report.html"))
    if args.register:
        _register(res["tests"], run_id, syms, res["meta"]["date_range"])
    print(f"report: {path}")


if __name__ == "__main__":
    main()
