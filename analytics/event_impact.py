"""
Event impact: how do driver-exposed stocks actually react to driver shocks?

Generalizes event_backtest.py's oil->airlines example into a repeatable
research tool: pick a driver (oil, gold, t10y, ...), classify symbols as
positively/negatively exposed to it using ONLY a trailing lookback window
known as of each event date (point-in-time — no full-history look-ahead),
then run separate event studies for the two exposure groups on the
driver's own shock dates.

This is a RESEARCH module, not (yet) a live factor. Per the 2026-07-07
sentiment null result (see
experiments/2026-07-07_news-sentiment-null-result.md) a factor should
show a real, point-in-time-honest, economically-signed effect here before
it gets wired into analytics/signals.py's signal_panel().

Usage
-----
  python -m analytics.event_impact --driver oil --pct 15 --days 10
  python -m analytics.event_impact --driver oil --pct -15 --days 10 --min-t 3

Library:
  from analytics import event_impact as ei
  pos_res, neg_res, grouped = ei.driver_event_study("oil", pct=15, days=10)
  print(pos_res.summary())
"""

import argparse
import math
import os
import sys

import pandas as pd
from scipy import stats

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analytics.exposure import DRIVERS, MARKET_DRIVER, MIN_OBS, compute_exposure, load_driver_returns
from event_backtest import event_study, load_close_matrix, price_move_events
from tiingo_pipeline import DEFAULT_SYMBOLS

T_SIGNIFICANT = 3.0     # matches analytics/relevance.py's T_SIGNIFICANT convention
LOOKBACK_YEARS = 3      # trailing window used to classify exposure as of each event date
REACTION_DAYS = 3       # only the positive-exposure leg's 1-3 day co-movement validated so far


def _rolling_grouping(driver: str,
                      event_dates,
                      universe=None,
                      min_t: float = T_SIGNIFICANT,
                      lookback_years: int = LOOKBACK_YEARS,
                      min_obs: int = MIN_OBS,
                      end: "str | None" = None) -> pd.DataFrame:
    """
    Point-in-time exposure classification: for each event date, regress
    each candidate symbol's daily returns on `driver` (market-controlled)
    using ONLY the trailing `lookback_years` of data ending at that date —
    never data from after the event. A symbol is only included for an
    event if that trailing-window regression itself clears |t_ex_mkt| >
    min_t; membership can therefore differ from one event date to the next.

    Returns tidy df: date, symbol, sign (+1/-1), beta_ex_mkt, t_ex_mkt, n.
    """
    trigger_symbol = DRIVERS[driver][1]
    syms = [s for s in (universe or DEFAULT_SYMBOLS) if s != trigger_symbol]

    drv = load_driver_returns([driver, MARKET_DRIVER], end=end)
    if driver not in drv.columns or MARKET_DRIVER not in drv.columns:
        return pd.DataFrame(columns=["date", "symbol", "sign", "beta_ex_mkt", "t_ex_mkt", "n"])
    driver_ret, mkt_ret = drv[driver], drv[MARKET_DRIVER]

    closes = load_close_matrix(syms, end=end)
    stock_ret = closes.pct_change()

    lookback = pd.DateOffset(years=lookback_years)
    rows = []
    for d in sorted(pd.DatetimeIndex(pd.to_datetime(event_dates)).unique()):
        window_start = d - lookback
        dr = driver_ret[(driver_ret.index > window_start) & (driver_ret.index <= d)]
        mk = mkt_ret[(mkt_ret.index > window_start) & (mkt_ret.index <= d)]
        for sym in syms:
            if sym not in stock_ret.columns:
                continue
            sr = stock_ret[sym]
            sr = sr[(sr.index > window_start) & (sr.index <= d)]
            res = compute_exposure(sr, dr, market_ret=mk, min_obs=min_obs)
            if res is None or "t_ex_mkt" not in res:
                continue
            if abs(res["t_ex_mkt"]) > min_t:
                rows.append({"date": d, "symbol": sym,
                            "sign": 1 if res["beta_ex_mkt"] > 0 else -1,
                            "beta_ex_mkt": res["beta_ex_mkt"],
                            "t_ex_mkt": res["t_ex_mkt"], "n": res["n"]})
    if not rows:
        return pd.DataFrame(columns=["date", "symbol", "sign", "beta_ex_mkt", "t_ex_mkt", "n"])
    return pd.DataFrame(rows)


def driver_event_study(driver: str,
                       pct: float,
                       days: int = 10,
                       window: "tuple[int, int]" = (-10, 40),
                       min_t: float = T_SIGNIFICANT,
                       benchmark: "str | None" = "SPY",
                       universe=None,
                       start: "str | None" = None,
                       min_gap_days: int = 10,
                       lookback_years: int = LOOKBACK_YEARS):
    """
    Point-in-time event study of `driver`'s own X%-over-N-days shocks:
    for each shock episode, only symbols whose TRAILING `lookback_years`
    exposure (known as of that date) clears |t_ex_mkt| > min_t are
    included, split into positive- and negative-exposure groups and run
    as separate event studies (pooling opposite-signed reactions would
    average away the effect being tested).

    Returns (pos_result, neg_result, grouped) — grouped is the tidy
    per-event-date classification table from _rolling_grouping(); either
    result is None if its group has no qualifying (date, symbol) rows.
    """
    trigger_symbol = DRIVERS[driver][1]
    events = price_move_events(trigger_symbol, pct=pct, days=days,
                               start=start, min_gap_days=min_gap_days)
    if events.empty:
        raise RuntimeError(
            f"No '{driver}' ({trigger_symbol}) moves of {pct}% over {days}d found.")

    grouped = _rolling_grouping(driver, events["date"], universe=universe,
                                min_t=min_t, lookback_years=lookback_years)
    if grouped.empty:
        res = event_study(events, symbols=[trigger_symbol], window=window,
                          benchmark=benchmark, min_gap_days=0)
        return res, None, grouped

    pos_events = grouped.loc[grouped["sign"] == 1, ["date", "symbol"]]
    neg_events = grouped.loc[grouped["sign"] == -1, ["date", "symbol"]]
    pos_res = (event_study(pos_events, window=window, benchmark=benchmark, min_gap_days=0)
               if not pos_events.empty else None)
    neg_res = (event_study(neg_events, window=window, benchmark=benchmark, min_gap_days=0)
               if not neg_events.empty else None)
    return pos_res, neg_res, grouped


def oil_shock_signal(symbols=None,
                     start: "str | None" = None,
                     end: "str | None" = None,
                     pct: float = 15,
                     days: int = 10,
                     min_t: float = T_SIGNIFICANT,
                     lookback_years: int = LOOKBACK_YEARS,
                     reaction_days: int = REACTION_DAYS,
                     min_gap_days: int = 10) -> pd.DataFrame:
    """
    Sparse (symbol, date) factor panel for analytics/signals.py: a directional
    tilt, scaled by each symbol's measured exposure strength (beta_ex_mkt),
    for POSITIVELY oil-exposed names, active only on the `reaction_days`
    trading days after a qualifying oil shock (surge or drop). NaN/absent
    everywhere else. Scaling by exposure strength (rather than a flat +-1
    for every qualifying symbol) matters here specifically: every symbol
    tagged on the same event date would otherwise carry the IDENTICAL raw
    value, which cross-sectional z-scoring treats as a zero-spread (fully
    degenerate) group and reduces to 0 for everyone — present in the
    panel but contributing nothing to the ranking.

    Scoped deliberately narrow: only the positive-exposure leg's short
    (1-3 day) co-movement held up under a point-in-time, date-clustering-
    honest re-test (see analytics/event_impact.py module docstring and
    experiments/2026-07-07_news-sentiment-null-result.md for the
    methodology this follows). The negative-exposure leg's significant
    horizon moved between the surge test (21d) and the drop test (1d/5d) —
    inconsistent enough not to trust yet, so it is NOT included here.

    Returns columns: symbol, date, oil_shock_raw (float, signed by shock
    direction and scaled by the symbol's measured beta_ex_mkt — magnitude
    is not bounded to +-1, only the sign is driven by shock direction).
    """
    trigger_symbol = DRIVERS["oil"][1]
    syms = list(symbols) if symbols is not None else list(DEFAULT_SYMBOLS)
    start = pd.Timestamp(start).strftime("%Y-%m-%d") if start is not None else None
    end = pd.Timestamp(end).strftime("%Y-%m-%d") if end is not None else None

    up = price_move_events(trigger_symbol, pct=abs(pct), days=days,
                           start=start, min_gap_days=min_gap_days)
    down = price_move_events(trigger_symbol, pct=-abs(pct), days=days,
                             start=start, min_gap_days=min_gap_days)
    up = up.assign(direction=1.0)
    down = down.assign(direction=-1.0)
    events = pd.concat([up, down], ignore_index=True).sort_values("date")
    if events.empty:
        return pd.DataFrame(columns=["symbol", "date", "oil_shock_raw"])

    grouped = _rolling_grouping("oil", events["date"], universe=syms, min_t=min_t,
                                lookback_years=lookback_years, end=end)
    pos = grouped.loc[grouped["sign"] == 1, ["date", "symbol", "beta_ex_mkt"]]
    if pos.empty:
        return pd.DataFrame(columns=["symbol", "date", "oil_shock_raw"])
    pos = pos.merge(events[["date", "direction"]], on="date", how="left")

    closes = load_close_matrix(syms, start=start, end=end)
    if closes.empty:
        return pd.DataFrame(columns=["symbol", "date", "oil_shock_raw"])
    idx = closes.index

    rows = []
    for r in pos.itertuples(index=False):
        loc = idx.searchsorted(r.date, side="left")
        score = float(r.direction) * float(r.beta_ex_mkt)
        for k in range(1, reaction_days + 1):
            if loc + k < len(idx):
                rows.append({"symbol": r.symbol, "date": idx[loc + k],
                            "oil_shock_raw": score})
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    # overlapping reaction windows (rare, back-to-back shocks): keep the
    # most recently triggered shock's direction for that (symbol, date)
    return (out.sort_values("date")
               .drop_duplicates(["symbol", "date"], keep="last")
               .reset_index(drop=True))


def _bh_adjust(pvals: pd.Series) -> pd.Series:
    """
    Benjamini-Hochberg FDR-adjusted p-values across the horizons tested
    together in one _date_level_stats() call. Scanning multiple reaction
    horizons for the same event set inflates the chance of a spuriously
    significant t-stat (see signal-eval skill: "5 horizons x several
    stats — one marginal t=2 among 15 numbers is expected by chance").
    NaN inputs (too few independent dates to test) stay NaN and are
    excluded from the ranking.
    """
    valid = pvals.dropna().sort_values()
    m = len(valid)
    result = pd.Series(float("nan"), index=pvals.index)
    if m == 0:
        return result
    ranks = pd.Series(range(1, m + 1), index=valid.index)
    adj = (valid * m / ranks).clip(upper=1.0)
    adj = adj.iloc[::-1].cummin().iloc[::-1]  # standard BH step-up monotonicity
    result.loc[adj.index] = adj
    return result


def _date_level_stats(res) -> pd.DataFrame:
    """
    Symbols on the same event date share the same driver shock, so they are
    NOT independent draws — pooling (symbol, event) rows the way
    event_study()'s own horizons table does can overstate significance
    (same failure mode as pooled-vs-daily IC in signal_eval.py). This
    re-aggregates to one mean CAR per event DATE first, then t-tests across
    dates — the honest, conservative version of the same stat. Restricted
    to the same horizons already reported in res.horizons.

    Adds a two-tailed p-value per horizon (t-distribution, df = n_dates-1)
    plus a Benjamini-Hochberg-adjusted p-value across the horizons tested
    here, since checking several horizons at once is itself a multiple-
    comparisons problem.
    """
    events = res.events[["date"]].reset_index(drop=True)
    car = res.car.reset_index(drop=True)
    df = pd.concat([events, car], axis=1)
    rows = {}
    for h in res.horizons.index:
        if h not in car.columns:
            continue
        per_date = df.groupby("date")[h].mean().dropna()
        n = len(per_date)
        mean, sd = float(per_date.mean()), float(per_date.std(ddof=1))
        t = mean / (sd / math.sqrt(n)) if n > 1 and sd > 0 else float("nan")
        p = 2 * stats.t.sf(abs(t), df=n - 1) if n > 1 and sd > 0 else float("nan")
        rows[h] = {"n_dates": n, "mean_pct": round(100 * mean, 2), "t_stat": round(t, 2),
                  "p_value": p}
    out = pd.DataFrame.from_dict(rows, orient="index")
    if not out.empty:
        out["p_adj"] = _bh_adjust(out["p_value"])
    return out


def _print_horizons(label: str, res):
    print(f"\n[{label}] {res.params['n_events_used']}/{res.params['n_events_in']} "
          f"(symbol,event) rows | window {res.params['window']} | benchmark {res.params['benchmark']}")
    print("Horizon  n   mean%   median%  hit%   t     baseline%  edge%")
    for h, r in res.horizons.iterrows():
        print(f"{h:>6}  {r['n']:>4}  {r['mean_pct']:>6}  {r['median_pct']:>7}  "
              f"{r['hit_rate_pct']:>5}  {r['t_stat']:>5}  {r['baseline_pct']:>9}  "
              f"{r['edge_pct']:>6}")
    dl = _date_level_stats(res)
    print(f"  date-level (honest, {dl['n_dates'].iloc[0] if not dl.empty else 0} "
          f"independent dates, BH-adjusted across {len(dl)} horizons): " +
          " | ".join(f"h{h}: mean={r['mean_pct']}% t={r['t_stat']} "
                    f"p={r['p_value']:.4f} p_adj={r['p_adj']:.4f}"
                    for h, r in dl.iterrows()))


def print_report(driver: str, pct: float, days: int, pos_res, neg_res, grouped: pd.DataFrame):
    direction = "surge" if pct > 0 else "drop"
    print(f"\n=== EVENT IMPACT: {driver} {direction} ({pct}% over {days}d, "
          f"point-in-time exposure, {LOOKBACK_YEARS}y trailing window) ===")

    if grouped.empty:
        print(f"\nNo (date, symbol) pair ever cleared |t_ex_mkt| > {T_SIGNIFICANT} "
              f"for '{driver}' on a trailing window — falling back to the driver's "
              f"own proxy symbol as a baseline.")
        _print_horizons(DRIVERS[driver][1], pos_res)
    else:
        n_pos_sym = grouped.loc[grouped["sign"] == 1, "symbol"].nunique()
        n_neg_sym = grouped.loc[grouped["sign"] == -1, "symbol"].nunique()
        print(f"\n{grouped['date'].nunique()} event dates classified | "
              f"{n_pos_sym} distinct positively-exposed symbols ever appeared, "
              f"{n_neg_sym} negatively-exposed (membership varies by date — "
              f"point-in-time, not a fixed list).")
        print("\nPositively- and negatively-exposed names are scored SEPARATELY: a "
              "real driver effect should show positive-exposure names moving WITH "
              f"the {direction} and negative-exposure names moving AGAINST it.")
        if pos_res is not None:
            _print_horizons("positive exposure", pos_res)
        else:
            print("\n[positive exposure] no symbols in this group.")
        if neg_res is not None:
            _print_horizons("negative exposure", neg_res)
        else:
            print("\n[negative exposure] no symbols in this group.")

    print("\nGuide: |t| > 2 over enough events = a real event-conditional effect;")
    print("edge% is mean CAR minus the unconditional base rate for the same symbols.")
    print("Trust p_adj (BH-adjusted across horizons), not the raw p, before calling")
    print("anything significant — the raw p is inflated by scanning several horizons.")
    print("A real effect should show OPPOSITE-signed CAR between the two groups —")
    print("same-signed or only-one-group significance is more likely confounding.")
    print("This is a RESEARCH result only — not wired into signal_panel() yet.")


def sensitivity_check(driver: str,
                      pct: float,
                      days: int = 10,
                      window: "tuple[int, int]" = (-10, 40),
                      benchmark: "str | None" = "SPY",
                      universe=None,
                      start: "str | None" = None,
                      min_gap_days: int = 10,
                      min_t_grid=(2.5, 3.0, 3.5),
                      lookback_years_grid=(2, 3, 5),
                      horizon: int = REACTION_DAYS) -> pd.DataFrame:
    """
    Reruns driver_event_study()'s POSITIVE-exposure leg across a grid of
    (min_t, lookback_years) classification-threshold choices and reports
    the date-level stat at `horizon` for each combination. An effect that
    is only significant for one specific threshold choice was likely
    tuned to this result by the choice of threshold, not validated by it
    — the classification parameters should be picked before looking at
    the outcome, and a robust effect should survive nearby choices too.

    Returns tidy df: min_t, lookback_years, n_pos_symbols, n_dates,
    mean_pct, t_stat, p_value, p_adj, sign_stable (True if this row's
    mean CAR sign matches the majority sign across the whole grid — a
    row can be individually "significant" and still flagged unstable if
    it's the only one with that sign).
    """
    rows = []
    for min_t in min_t_grid:
        for lookback_years in lookback_years_grid:
            try:
                pos_res, _, grouped = driver_event_study(
                    driver, pct=pct, days=days, window=window, min_t=min_t,
                    benchmark=benchmark, universe=universe, start=start,
                    min_gap_days=min_gap_days, lookback_years=lookback_years)
            except RuntimeError:
                continue
            n_pos_sym = (grouped.loc[grouped["sign"] == 1, "symbol"].nunique()
                        if not grouped.empty else 0)
            row = {"min_t": min_t, "lookback_years": lookback_years,
                  "n_pos_symbols": n_pos_sym, "n_dates": 0,
                  "mean_pct": float("nan"), "t_stat": float("nan"),
                  "p_value": float("nan"), "p_adj": float("nan")}
            if pos_res is not None:
                dl = _date_level_stats(pos_res)
                if horizon in dl.index:
                    r = dl.loc[horizon]
                    row.update({"n_dates": int(r["n_dates"]), "mean_pct": r["mean_pct"],
                               "t_stat": r["t_stat"], "p_value": r["p_value"],
                               "p_adj": r["p_adj"]})
            rows.append(row)

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    present = out["mean_pct"].dropna()
    if present.empty:
        out["sign_stable"] = False
        return out
    signed = present.apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    majority_sign = signed.value_counts().idxmax()
    out["sign_stable"] = out["mean_pct"].apply(
        lambda x: not pd.isna(x) and (1 if x > 0 else (-1 if x < 0 else 0)) == majority_sign)
    return out


def print_sensitivity_report(driver: str, pct: float, horizon: int, df: pd.DataFrame):
    direction = "surge" if pct > 0 else "drop"
    print(f"\n=== SENSITIVITY: {driver} {direction} positive-exposure leg, "
          f"h{horizon}, across {len(df)} (min_t, lookback_years) combinations ===")
    if df.empty:
        print("No combination produced a positive-exposure result.")
        return
    print("min_t  lookback  n_sym  n_dates   mean%     t     p_adj  sign_stable")
    for _, r in df.iterrows():
        print(f"{r['min_t']:>5}  {r['lookback_years']:>8}  {r['n_pos_symbols']:>5}  "
              f"{r['n_dates']:>7}  {r['mean_pct']:>7}  {r['t_stat']:>5}  "
              f"{r['p_adj']:>7.4f}  {str(bool(r['sign_stable'])):>5}")
    n_stable = int(df["sign_stable"].sum())
    n_sig = int((df["p_adj"] < 0.05).sum())
    print(f"\n{n_stable}/{len(df)} combinations agree on the majority sign; "
          f"{n_sig}/{len(df)} still clear p_adj < 0.05.")
    print("A robust effect should show sign_stable across nearly all combinations —")
    print("if significance only survives at one specific threshold choice, that")
    print("choice was likely tuned to this result rather than validated by it.")


def main():
    parser = argparse.ArgumentParser(
        description="Point-in-time event-conditional reaction of driver-exposed symbols")
    parser.add_argument("--driver", required=True, choices=sorted(DRIVERS))
    parser.add_argument("--pct", type=float, required=True,
                        help="Move threshold, e.g. 15 (surge) or -15 (drop)")
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--window", type=int, nargs=2, default=[-10, 40])
    parser.add_argument("--min-t", type=float, default=T_SIGNIFICANT)
    parser.add_argument("--benchmark", default="SPY")
    parser.add_argument("--start", default=None)
    parser.add_argument("--lookback-years", type=int, default=LOOKBACK_YEARS)
    parser.add_argument("--sensitivity", action="store_true",
                        help="Also rerun the positive-exposure leg across a "
                             "(min_t, lookback_years) grid to check robustness")
    args = parser.parse_args()

    pos_res, neg_res, grouped = driver_event_study(
        args.driver, pct=args.pct, days=args.days,
        window=tuple(args.window), min_t=args.min_t,
        benchmark=args.benchmark, start=args.start,
        lookback_years=args.lookback_years)
    print_report(args.driver, args.pct, args.days, pos_res, neg_res, grouped)

    if args.sensitivity:
        sens = sensitivity_check(args.driver, pct=args.pct, days=args.days,
                                 window=tuple(args.window), benchmark=args.benchmark,
                                 start=args.start)
        print_sensitivity_report(args.driver, args.pct, REACTION_DAYS, sens)


if __name__ == "__main__":
    main()
