"""
Per-symbol-day introduction panel from the Massive listing changes.

Range is per expiration (spec amendment 2026-10-02): a contract added on day
t counts as ABOVE/BELOW only if it extends the strike range of an expiration
that already existed on t-1. Brand-new expirations are calendar listings and
never count. Whole-chain extension is kept as a secondary measure.
"""
import numpy as np
import pandas as pd

INVALID_STATUS = {"missing", "suspect"}
SPLIT_REPLACED_FRAC = 0.8
# Strikes are rescaled into post-split units (dividing by every LATER split's
# ratio), the same convention as the split-adjusted closes they are compared
# with. The ratio comes from the chain itself -- on a split day every contract
# is re-tickered with strikes scaled by it -- so no splits table is needed.


def _near_extremes(live: pd.DataFrame, date: str):
    later = live[live["expiration_date"] > date]
    if later.empty:
        return np.nan, np.nan
    near = later[later["expiration_date"] == later["expiration_date"].min()]["strike"]
    return float(near.min()), float(near.max())


def _one_symbol(sym: str, ch: pd.DataFrame, summ: pd.DataFrame) -> list[dict]:
    by_date = {d: g for d, g in ch.groupby("date")}
    live = None
    rows = []
    prev_invalid = False
    for _, s in summ.sort_values("date").iterrows():
        d = s["date"]
        # The pipeline diffs against the last GOOD snapshot, so the day after a
        # missing/suspect day carries two days of listings: not attributable.
        after_gap, prev_invalid = prev_invalid, s["status"] in INVALID_STATUS
        day = by_date.get(d)
        row = {"symbol": sym, "date": d, "valid": False, "above": np.nan, "below": np.nan,
               "above_all": np.nan, "below_all": np.nan}
        if s["status"] in INVALID_STATUS:
            row["near_min"], row["near_max"] = (_near_extremes(live, d) if live is not None
                                                else (np.nan, np.nan))
            rows.append(row)
            continue
        if day is not None and (day["change"] == "initial").any():
            live = day[day["change"] == "initial"][["contract_ticker", "strike", "expiration_date"]]
        elif live is not None:
            added = (day[day["change"] == "added"] if day is not None
                     else ch.iloc[0:0])
            removed = (set(day.loc[day["change"] == "removed", "contract_ticker"])
                       if day is not None else set())
            rng = live.groupby("expiration_date")["strike"].agg(["min", "max"])
            ext = added.join(rng, on="expiration_date", how="inner")
            split = (s.get("replaced_frac") or 0) > SPLIT_REPLACED_FRAC
            prev_median = float(live["strike"].median())
            if not (split or after_gap):
                row.update({
                    "valid": True,
                    "above": int((ext["strike"] > ext["max"]).sum()),
                    "below": int((ext["strike"] < ext["min"]).sum()),
                    "above_all": int((added["strike"] > live["strike"].max()).sum()),
                    "below_all": int((added["strike"] < live["strike"].min()).sum()),
                })
            live = pd.concat([live[~live["contract_ticker"].isin(removed)],
                              added[["contract_ticker", "strike", "expiration_date"]]],
                             ignore_index=True)
            if split and len(live):
                ratio = prev_median / float(live["strike"].median())
                row["split_ratio"] = round(ratio * 2) / 2 if ratio > 1.25 else round(ratio, 3)
        row["near_min"], row["near_max"] = (_near_extremes(live, d) if live is not None
                                            else (np.nan, np.nan))
        rows.append(row)
    return rows


def daily_intros(changes: pd.DataFrame, summary: pd.DataFrame) -> pd.DataFrame:
    changes = changes.drop_duplicates(["symbol", "date", "contract_ticker", "change"])
    summary = summary.drop_duplicates(["symbol", "date"], keep="last")
    rows = []
    for sym, summ in summary.groupby("symbol"):
        rows += _one_symbol(sym, changes[changes["symbol"] == sym], summ)
    cols = ["symbol", "date", "valid", "above", "below", "above_all", "below_all",
            "near_max", "near_min", "split_ratio"]
    out = pd.DataFrame(rows, columns=cols)
    out["split_ratio"] = out["split_ratio"].fillna(1.0)
    for sym, g in out.groupby("symbol"):
        r = g.sort_values("date")["split_ratio"]
        later = r[::-1].cumprod()[::-1] / r          # product of splits strictly after
        out.loc[later.index, "near_max"] = out.loc[later.index, "near_max"] / later
        out.loc[later.index, "near_min"] = out.loc[later.index, "near_min"] / later
    return out
