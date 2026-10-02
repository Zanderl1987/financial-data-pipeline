"""Move events, HIT/MISS, entry timing and signed excess returns (pre-registered)."""
import numpy as np
import pandas as pd

HORIZONS = [1, 3, 5, 10, 21, 63, 126]


def excess_returns(close: pd.DataFrame, bench: str = "SPY") -> pd.DataFrame:
    ret = close.pct_change()
    return ret.drop(columns=[bench]).sub(ret[bench], axis=0)


def find_events(close, trigger, bench="SPY", k_day=2.5, k_run=2.0, vol_window=60,
                run_len=5) -> pd.DataFrame:
    ex = excess_returns(close, bench)
    sigma = ex.rolling(vol_window, min_periods=vol_window).std().shift(1)
    rows = []
    for sym in ex.columns:
        x, sd = ex[sym], sigma[sym]
        if trigger == "day":
            z = x / sd
            hits = z[z.abs() > k_day].dropna()
            rows += [{"symbol": sym, "event_date": d, "direction": int(np.sign(v)),
                      "move_sigma": float(v)} for d, v in hits.items()]
        elif trigger == "run":
            # "first crossing": fire when |z| crosses k_run, then re-arm only after
            # |z| falls back below it AND run_len days have passed.
            cum = x.rolling(run_len).sum()
            z = cum / (sd * np.sqrt(run_len))
            last, armed = None, True
            for d, v in z.dropna().items():
                i = ex.index.get_loc(d)
                if abs(v) <= k_run:
                    armed = True
                    continue
                if armed and (last is None or i - last > run_len):
                    rows.append({"symbol": sym, "event_date": d, "direction": int(np.sign(v)),
                                 "move_sigma": float(v)})
                    last, armed = i, False
        else:
            raise ValueError(f"unknown trigger {trigger!r}")
    return pd.DataFrame(rows, columns=["symbol", "event_date", "direction", "move_sigma"])


def attach_intros(events, intros, close_index, window=(1, 2), entry_offset=3) -> pd.DataFrame:
    key = intros.assign(date=intros["date"].astype(str).str[:10]).set_index(["symbol", "date"])
    out = []
    for _, e in events.iterrows():
        loc = close_index.get_loc(e["event_date"])
        if loc + entry_offset >= len(close_index):
            continue
        wdays = [close_index[loc + k].strftime("%Y-%m-%d") for k in range(window[0], window[1] + 1)]
        try:
            w = key.loc[[(e["symbol"], d) for d in wdays]]
        except KeyError:
            continue
        if not w["valid"].all():
            continue
        up = e["direction"] > 0
        rec = e.to_dict()
        rec["dir_intro"] = int(w["above" if up else "below"].sum())
        rec["opp_intro"] = int(w["below" if up else "above"].sum())
        rec["dir_intro_all"] = int(w["above_all" if up else "below_all"].sum())
        rec["hit"] = rec["dir_intro"] > 0
        rec["entry_date"] = close_index[loc + entry_offset]
        out.append(rec)
    return pd.DataFrame(out)


def forward_returns(events, close, horizons, bench="SPY") -> pd.DataFrame:
    out = events.copy()
    idx = close.index
    for h in horizons:
        vals = []
        for _, e in events.iterrows():
            i = idx.get_loc(e["entry_date"])
            if i + h >= len(idx):
                vals.append(np.nan)
                continue
            a = close[e["symbol"]].iloc[i + h] / close[e["symbol"]].iloc[i] - 1
            b = close[bench].iloc[i + h] / close[bench].iloc[i] - 1
            vals.append(e["direction"] * (a - b))
        out[f"ret_{h}"] = vals
    return out


def car_paths(events, close, pre=5, post=126, bench="SPY") -> pd.DataFrame:
    idx = close.index
    offsets = list(range(-pre, post + 1))
    rows = []
    for _, e in events.iterrows():
        i = idx.get_loc(e["entry_date"])
        row = []
        for k in offsets:
            j = i + k
            if j < 0 or j >= len(idx):
                row.append(np.nan)
                continue
            a = close[e["symbol"]].iloc[j] / close[e["symbol"]].iloc[i] - 1
            b = close[bench].iloc[j] / close[bench].iloc[i] - 1
            row.append(e["direction"] * (a - b))
        rows.append(row)
    return pd.DataFrame(rows, columns=offsets, index=events.index)


def earnings_flag(events, earnings_dates, close_index) -> pd.Series:
    pos = {d: i for i, d in enumerate(close_index)}
    near = {}
    for sym, g in earnings_dates.groupby("symbol"):
        locs = set()
        for d in pd.to_datetime(g["date"]):
            j = close_index.searchsorted(d)
            if j < len(close_index):
                locs |= {j - 1, j, j + 1}
        near[sym] = locs
    return pd.Series([pos.get(e["event_date"], -9) in near.get(e["symbol"], set())
                      for _, e in events.iterrows()], index=events.index)
