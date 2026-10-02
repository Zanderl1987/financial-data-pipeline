# Strike Introduction Study — Increment 2: Analysis + Interactive Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Answer the spec's two questions — does a post-move listing of new strikes on the move's side predict continuation, and is it more than mechanical/volatility listing — with a pre-registered event study, a full significance battery, a live view of the study run, and an interactive report.

**Architecture:** A `strike_intro/` package of small units: `measures.py` turns the Massive listing changes into a per-symbol-day panel of per-expiration range extensions; `events.py` finds move events and attaches HIT/MISS, entry and signed forward returns; `excess_model.py` fits a leave-one-symbol-out Poisson model of mechanical listing; `battery.py` runs the statistics; `progress.py` publishes run progress that a Streamlit page renders live; `report.py` builds the interactive Plotly report; `run.py` orchestrates and writes results, registry rows and the writeup. Every unit is tested on synthetic data with a known answer before touching real data.

**Tech Stack:** Python 3.11 (`C:\ProgramData\anaconda3\python.exe`), pandas, numpy, scipy 1.10, statsmodels 0.14, plotly 6.9, streamlit 1.60, DuckDB query layer.

**Spec:** `docs/superpowers/specs/2026-10-01-strike-intro-study-design.md` (amended 2026-10-02: per-expiration range, data-quality exclusions)

## Global Constraints

- Range is **per expiration**; contracts in a brand-new expiration never count; whole-chain extension is reported as a secondary measure only.
- Symbol-days with `status` in {`missing`, `suspect`} or `replaced_frac > 0.8` contribute no introductions; an event whose e+1..e+2 window touches one is dropped.
- Day trigger: |excess return| > 2.5 × trailing 60-day σ (σ excludes the event day). Run trigger: |5-day cumulative excess| > 2 × σ × √5, first crossing, 5-day cooldown.
- `DIR_INTRO` = introductions on the move's side over e+1..e+2; HIT = `DIR_INTRO > 0`. **Entry = close of e+3 for every event.** Sensitivity (labelled): window e+1, entry e+2.
- Returns: excess vs SPY, signed by direction; horizons 1, 3, 5, 10, 21, 63, 126 trading days from entry.
- **Primary test (pre-registered):** day trigger, HIT − MISS, 21-day signed excess return. All other tests are secondary and BH-adjusted together (`evaluation.stats.bh_fdr`).
- Prices via `event_backtest.load_close_matrix` (curated, longest-series rule). Earnings via `event_backtest.earnings_events()`.
- Excess-model covariates use only information known at the close of t−1.
- Never name a column `year`/`month`. ASCII-only console output. Never `os.kill(pid, 0)` on Windows.

## Review Focus

1. **A symbol whose backfill is incomplete** (the study can run while the backfill is still going) → only symbols whose checkpoint reached the manifest end are used, and the report says how many. Test in Task 7.
2. **Event near the end of the price history** (e+3 or e+3+h beyond the last close) → the event is kept for horizons that exist and is NaN for the rest, never filled with zeros. Test in Task 2.
3. **A stock split inside the window** (NFLX, NOW) → the split day contributes no introductions and does not move the measured range off course. Test in Task 1 (`test_missing_suspect_and_split_days_...`). Price events rely on the split-adjusted price store; Task 9 step 3 eyeballs NFLX/NOW for a spurious split-day event.
4. **Permutation and bootstrap with a tiny group** (e.g. 3 HITs in a subgroup) → returns NaN p-values with n reported, not a crash or a fake p=0. Test in Task 5.
5. **Two-way clustered regression with a constant regressor** (e.g. earnings flag all zero in a subgroup) → the column is dropped, not a singular-matrix crash. Test in Task 5.

---

### Task 1: Package scaffold + per-expiration introduction panel

**Files:**
- Create: `strike_intro/__init__.py` (empty docstring module)
- Create: `strike_intro/measures.py`
- Test: `tests/test_strike_intro_measures.py`

**Interfaces:**
- Produces: `daily_intros(changes: pd.DataFrame, summary: pd.DataFrame) -> pd.DataFrame` with columns
  `symbol, date, valid, above, below, above_all, below_all, near_max, near_min` (one row per symbol-day in `summary`; `above/below` = per-expiration extensions into existing expirations; `*_all` = whole-chain; `near_max/near_min` = strike extremes of the nearest expiration strictly after `date`, AFTER applying that day's changes, used as next-day headroom inputs; `valid=False` → counts NaN).
- Consumes: Massive tables' columns (`symbol, date, contract_ticker, strike, expiration_date, change`; summary `symbol, date, status, replaced_frac`).

- [ ] **Step 1: Write the failing tests**

```python
"""Per-expiration introduction panel (spec amendment 2026-10-02)."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strike_intro import measures


def _ch(rows):
    return pd.DataFrame(rows, columns=["symbol", "date", "contract_ticker", "strike",
                                       "expiration_date", "change"])


def _summ(dates, status=None, replaced=None):
    n = len(dates)
    return pd.DataFrame({"symbol": ["A"] * n, "date": dates,
                         "status": status or ["ok"] * n,
                         "replaced_frac": replaced or [0.0] * n})


BASE = [
    ("A", "2026-09-01", "c100", 100.0, "2026-10-16", "initial"),
    ("A", "2026-09-01", "c110", 110.0, "2026-10-16", "initial"),
    ("A", "2026-09-01", "l050", 50.0, "2027-01-15", "initial"),
    ("A", "2026-09-01", "l300", 300.0, "2027-01-15", "initial"),
]


def test_extension_of_an_existing_expiration_counts_per_expiration_not_whole_chain():
    ch = _ch(BASE + [("A", "2026-09-02", "c120", 120.0, "2026-10-16", "added")])
    out = measures.daily_intros(ch, _summ(["2026-09-01", "2026-09-02"])).set_index("date")
    assert out.loc["2026-09-01", "valid"] == False          # no prior day
    assert out.loc["2026-09-02", "above"] == 1 and out.loc["2026-09-02", "below"] == 0
    assert out.loc["2026-09-02", "above_all"] == 0          # LEAPS pin the chain max (300)


def test_brand_new_expiration_never_counts():
    ch = _ch(BASE + [("A", "2026-09-02", "n500", 500.0, "2026-11-20", "added"),
                     ("A", "2026-09-02", "n010", 10.0, "2026-11-20", "added")])
    out = measures.daily_intros(ch, _summ(["2026-09-01", "2026-09-02"])).set_index("date")
    assert (out.loc["2026-09-02", "above"], out.loc["2026-09-02", "below"]) == (0, 0)
    assert (out.loc["2026-09-02", "above_all"], out.loc["2026-09-02", "below_all"]) == (1, 1)


def test_below_extension_and_inside_strikes():
    ch = _ch(BASE + [("A", "2026-09-02", "c090", 90.0, "2026-10-16", "added"),
                     ("A", "2026-09-02", "c105", 105.0, "2026-10-16", "added")])
    out = measures.daily_intros(ch, _summ(["2026-09-01", "2026-09-02"])).set_index("date")
    assert (out.loc["2026-09-02", "above"], out.loc["2026-09-02", "below"]) == (0, 1)


def test_missing_suspect_and_split_days_are_invalid_and_do_not_move_the_range():
    ch = _ch(BASE + [("A", "2026-09-04", "c120", 120.0, "2026-10-16", "added"),
                     ("A", "2026-09-08", "c130", 130.0, "2026-10-16", "added")])
    s = _summ(["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-08"],
              status=["ok", "missing", "suspect", "ok", "ok"],
              replaced=[0.0, 0.0, 0.0, 0.95, 0.0])
    out = measures.daily_intros(ch, s).set_index("date")
    assert list(out["valid"]) == [False, False, False, False, True]
    assert np.isnan(out.loc["2026-09-02", "above"])
    # the split day's listing still updates the range, so 09-08 is measured from 120
    assert out.loc["2026-09-08", "above"] == 1


def test_near_expiration_extremes_after_the_day():
    ch = _ch(BASE + [("A", "2026-09-02", "c120", 120.0, "2026-10-16", "added"),
                     ("A", "2026-09-02", "c100", 100.0, "2026-10-16", "removed")])
    out = measures.daily_intros(ch, _summ(["2026-09-01", "2026-09-02"])).set_index("date")
    assert (out.loc["2026-09-02", "near_min"], out.loc["2026-09-02", "near_max"]) == (110.0, 120.0)
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_strike_intro_measures.py -q -p no:cacheprovider --no-cov`
Expected: FAIL — `ModuleNotFoundError: No module named 'strike_intro'`.

- [ ] **Step 3: Implement**

`strike_intro/__init__.py`:
```python
"""Option strike-introduction study (spec docs/superpowers/specs/2026-10-01-strike-intro-study-design.md)."""
```

`strike_intro/measures.py`:
```python
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
    for _, s in summ.sort_values("date").iterrows():
        d = s["date"]
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
            if not split:
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
            "near_max", "near_min"]
    return pd.DataFrame(rows, columns=cols)
```

- [ ] **Step 4: Run tests**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_strike_intro_measures.py -q -p no:cacheprovider --no-cov`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add strike_intro/__init__.py strike_intro/measures.py tests/test_strike_intro_measures.py
git commit -m "strike_intro: per-expiration introduction panel"
```

---

### Task 2: Move events, HIT/MISS, entry, signed forward returns, CAR paths

**Files:**
- Create: `strike_intro/events.py`
- Test: `tests/test_strike_intro_events.py`

**Interfaces:**
- Consumes: `daily_intros` output (Task 1).
- Produces:
  - `excess_returns(close: pd.DataFrame, bench: str = "SPY") -> pd.DataFrame` (date × symbol daily excess return; bench column dropped).
  - `find_events(close, trigger: str, bench="SPY", k_day=2.5, k_run=2.0, vol_window=60, run_len=5) -> pd.DataFrame[symbol, event_date, direction, move_sigma]`.
  - `attach_intros(events, intros, close_index: pd.DatetimeIndex, window=(1, 2), entry_offset=3) -> pd.DataFrame` adding `dir_intro, opp_intro, dir_intro_all, hit, entry_date` and dropping events with an invalid/missing window day or no entry date.
  - `forward_returns(events, close, horizons, bench="SPY") -> pd.DataFrame` adding `ret_<h>` (signed excess, NaN past history end).
  - `car_paths(events, close, pre=5, post=126, bench="SPY") -> pd.DataFrame` (rows = events, columns = offsets −pre..post, signed cumulative excess vs the entry close).
  - `earnings_flag(events, earnings_dates: pd.DataFrame, close_index) -> pd.Series[bool]` (event within ±1 trading day of a report).
  - `HORIZONS = [1, 3, 5, 10, 21, 63, 126]`.

- [ ] **Step 1: Write the failing tests**

```python
"""Event construction for the strike-introduction study."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strike_intro import events as ev

IDX = pd.bdate_range("2026-01-01", periods=120)


def _close(jump_at=80, jump=0.12, n=120, seed=0):
    rng = np.random.default_rng(seed)
    spy = 100 * np.cumprod(1 + rng.normal(0, 0.005, n))
    a_ret = rng.normal(0, 0.01, n)
    a_ret[jump_at] += jump
    a = 50 * np.cumprod(1 + a_ret)
    return pd.DataFrame({"SPY": spy, "A": a}, index=IDX[:n])


def test_day_trigger_finds_the_jump_with_direction_and_excludes_event_day_from_sigma():
    e = ev.find_events(_close(), "day").set_index("event_date")
    assert IDX[80] in e.index
    assert e.loc[IDX[80], "direction"] == 1 and e.loc[IDX[80], "move_sigma"] > 2.5
    # sigma comes from the 60 days BEFORE the event: a jump of 12 sd-units of 1% noise
    assert e.loc[IDX[80], "move_sigma"] > 8


def test_run_trigger_cooldown_gives_one_event_for_one_run():
    c = _close(jump=0.0)
    r = c["A"].pct_change().fillna(0).to_numpy().copy()
    r[80:85] += 0.03                                  # five +3% excess days
    c["A"] = 50 * np.cumprod(1 + r)
    e = ev.find_events(c, "run")
    assert len(e[(e["event_date"] >= IDX[80]) & (e["event_date"] <= IDX[90])]) == 1


def test_hit_uses_move_side_over_e1_e2_and_entry_is_e3():
    events = pd.DataFrame({"symbol": ["A", "A"], "event_date": [IDX[80], IDX[100]],
                           "direction": [1, -1], "move_sigma": [3.0, -3.0]})
    dates = [d.strftime("%Y-%m-%d") for d in IDX]
    intros = pd.DataFrame({"symbol": "A", "date": dates, "valid": True, "above": 0,
                           "below": 0, "above_all": 0, "below_all": 0})
    intros.loc[82, "above"] = 2                      # e+2 of the up event
    intros.loc[101, "above"] = 5                     # opposite side for the down event
    out = ev.attach_intros(events, intros, IDX).set_index("event_date")
    assert out.loc[IDX[80], "hit"] and out.loc[IDX[80], "dir_intro"] == 2
    assert not out.loc[IDX[100], "hit"] and out.loc[IDX[100], "opp_intro"] == 5
    assert out.loc[IDX[80], "entry_date"] == IDX[83]


def test_event_with_invalid_window_day_is_dropped():
    events = pd.DataFrame({"symbol": ["A"], "event_date": [IDX[80]], "direction": [1],
                           "move_sigma": [3.0]})
    dates = [d.strftime("%Y-%m-%d") for d in IDX]
    intros = pd.DataFrame({"symbol": "A", "date": dates, "valid": True, "above": 0,
                           "below": 0, "above_all": 0, "below_all": 0})
    intros.loc[81, "valid"] = False
    assert ev.attach_intros(events, intros, IDX).empty


def test_forward_returns_are_signed_excess_and_nan_past_history():
    c = pd.DataFrame({"SPY": np.linspace(100, 110, 120), "A": np.linspace(50, 60, 120)},
                     index=IDX)
    events = pd.DataFrame({"symbol": ["A"], "event_date": [IDX[100]], "direction": [-1],
                           "entry_date": [IDX[103]]})
    out = ev.forward_returns(events, c, [1, 21])
    a = c["A"].iloc[104] / c["A"].iloc[103] - 1
    s = c["SPY"].iloc[104] / c["SPY"].iloc[103] - 1
    assert np.isclose(out.iloc[0]["ret_1"], -(a - s))
    assert np.isnan(out.iloc[0]["ret_21"])            # 103 + 21 > last index


def test_earnings_flag_within_one_trading_day():
    events = pd.DataFrame({"symbol": ["A", "A"], "event_date": [IDX[10], IDX[50]]})
    earn = pd.DataFrame({"symbol": ["A"], "date": [IDX[11]]})
    assert list(ev.earnings_flag(events, earn, IDX)) == [True, False]
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_strike_intro_events.py -q -p no:cacheprovider --no-cov`
Expected: FAIL — `ImportError: cannot import name 'events'`.

- [ ] **Step 3: Implement**

`strike_intro/events.py`:
```python
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
```

- [ ] **Step 4: Run tests**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_strike_intro_events.py -q -p no:cacheprovider --no-cov`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add strike_intro/events.py tests/test_strike_intro_events.py
git commit -m "strike_intro: move events, HIT/MISS, entry e+3, signed excess returns"
```

---

### Task 3: Run-progress channel (what the live study page reads)

**Files:**
- Create: `strike_intro/progress.py`
- Test: `tests/test_strike_intro_progress.py`

**Interfaces:**
- Produces: `class RunProgress(run_dir: str)` with `.stage(name: str, done: int, total: int, note: str = "")`, `.put(key: str, value)` (JSON-serialisable payload, e.g. per-fold coefficients or a permutation null sample), and `read_progress(run_dir) -> dict` (`{"stages": {...}, "data": {...}, "updated_at": str}`); writes `progress.json` atomically (tmp + `os.replace`). Tasks 4–6 call it; Task 8's page reads it.

- [ ] **Step 1: Write the failing tests**

```python
"""Run-progress channel for the live study page."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strike_intro import progress


def test_stages_and_payloads_round_trip(tmp_path):
    p = progress.RunProgress(str(tmp_path))
    p.stage("excess_model", 3, 100, "fitting AAPL")
    p.put("loso_coefs", [{"symbol": "AAPL", "headroom": -2.1}])
    got = progress.read_progress(str(tmp_path))
    assert got["stages"]["excess_model"] == {"done": 3, "total": 100, "note": "fitting AAPL"}
    assert got["data"]["loso_coefs"][0]["symbol"] == "AAPL"


def test_read_before_any_write_is_empty(tmp_path):
    assert progress.read_progress(str(tmp_path)) == {"stages": {}, "data": {}, "updated_at": None}
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_strike_intro_progress.py -q -p no:cacheprovider --no-cov`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement**

`strike_intro/progress.py`:
```python
"""Atomic progress.json for a study run; the live Streamlit page polls it."""
import datetime
import json
import os


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


class RunProgress:
    def __init__(self, run_dir: str):
        self.path = os.path.join(run_dir, "progress.json")
        os.makedirs(run_dir, exist_ok=True)
        self.state = {"stages": {}, "data": {}, "updated_at": None}

    def _flush(self):
        self.state["updated_at"] = _now()
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, default=float)
        os.replace(tmp, self.path)

    def stage(self, name: str, done: int, total: int, note: str = ""):
        self.state["stages"][name] = {"done": done, "total": total, "note": note}
        self._flush()

    def put(self, key: str, value):
        self.state["data"][key] = value
        self._flush()


def read_progress(run_dir: str) -> dict:
    try:
        with open(os.path.join(run_dir, "progress.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"stages": {}, "data": {}, "updated_at": None}
```

- [ ] **Step 4: Run tests**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_strike_intro_progress.py -q -p no:cacheprovider --no-cov`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add strike_intro/progress.py tests/test_strike_intro_progress.py
git commit -m "strike_intro: atomic run-progress channel"
```

---

### Task 4: Excess model — leave-one-symbol-out Poisson of mechanical listing

**Files:**
- Create: `strike_intro/excess_model.py`
- Test: `tests/test_strike_intro_excess.py`

**Interfaces:**
- Consumes: `daily_intros` (Task 1), `excess_returns` (Task 2), `RunProgress` (Task 3).
- Produces:
  - `build_model_panel(intros, close, earnings_near: set[tuple[str, pd.Timestamp]], bench="SPY", vol_window=60) -> pd.DataFrame` with one row per (symbol, date, side ∈ {up, down}) on valid days: `y`, `headroom`, `move_toward`, `sigma60`, `earnings`, `days_since_monthly`, `log_price` — all covariates from t−1.
  - `fit_loso(panel, progress=None) -> pd.DataFrame` = panel plus `expected` (each symbol predicted by a model fit without it) — reports each fold's coefficients via `progress.put("loso_coefs", [...])` and `progress.stage("excess_model", i, n, sym)`.
  - `event_excess(events, panel_with_expected, close_index, window=(1, 2)) -> pd.Series` = `dir_intro − Σ expected over the window on the move's side`.
  - `FEATURES = ["headroom", "move_toward", "sigma60", "earnings", "days_since_monthly", "log_price"]`.

- [ ] **Step 1: Write the failing tests**

```python
"""LOSO Poisson excess model: recovers a planted mechanical rule, no self-prediction."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strike_intro import excess_model as xm


def _synthetic_panel(n_sym=6, n=300, seed=1):
    rng = np.random.default_rng(seed)
    rows = []
    for s in range(n_sym):
        for t in range(n):
            for side in ("up", "down"):
                head = rng.uniform(0.0, 0.5)
                lam = np.exp(0.5 - 6.0 * head)          # mechanics: close to the edge -> listings
                rows.append({"symbol": f"S{s}", "date": t, "side": side, "headroom": head,
                             "move_toward": rng.normal(), "sigma60": rng.uniform(0.01, 0.03),
                             "earnings": 0, "days_since_monthly": rng.integers(0, 20),
                             "log_price": np.log(100), "y": rng.poisson(lam)})
    return pd.DataFrame(rows)


def test_loso_recovers_headroom_effect_and_every_row_is_out_of_fold():
    panel = _synthetic_panel()
    out = xm.fit_loso(panel)
    assert out["expected"].notna().all()
    near = out[out["headroom"] < 0.05]["expected"].mean()
    far = out[out["headroom"] > 0.45]["expected"].mean()
    assert near > 5 * far


def test_constant_feature_is_dropped_not_singular():
    panel = _synthetic_panel(n_sym=3, n=100)          # earnings and log_price are constant
    out = xm.fit_loso(panel)
    assert out["expected"].notna().all()


def test_event_excess_subtracts_expected_on_the_move_side():
    idx = pd.bdate_range("2026-01-01", periods=10)
    panel = pd.DataFrame({"symbol": "A", "date": [d.strftime("%Y-%m-%d") for d in idx[1:3]] * 2,
                          "side": ["up", "up", "down", "down"], "expected": [0.4, 0.6, 2.0, 2.0]})
    events = pd.DataFrame({"symbol": ["A"], "event_date": [idx[0]], "direction": [1],
                           "dir_intro": [3]})
    assert np.isclose(xm.event_excess(events, panel, idx).iloc[0], 3 - 1.0)
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_strike_intro_excess.py -q -p no:cacheprovider --no-cov`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement**

`strike_intro/excess_model.py`:
```python
"""
How many strike extensions would mechanics alone predict? Poisson GLM on
covariates known at the close of t-1, fit leave-one-symbol-out so no event's
own symbol informs its expectation (spec: Excess measure).
"""
import numpy as np
import pandas as pd
import statsmodels.api as sm

from strike_intro.events import excess_returns

FEATURES = ["headroom", "move_toward", "sigma60", "earnings", "days_since_monthly", "log_price"]


def _days_since_monthly(index: pd.DatetimeIndex) -> pd.Series:
    third_fri = {(d.year, d.month): d for d in
                 pd.date_range(index.min() - pd.Timedelta(days=40), index.max(),
                               freq="WOM-3FRI")}
    out = []
    for d in index:
        f = third_fri.get((d.year, d.month))
        if f is None or d <= f:
            prev = (d.replace(day=1) - pd.Timedelta(days=1))
            f = third_fri.get((prev.year, prev.month), f)
        out.append(int(np.busday_count(f.date(), d.date())) if f is not None else 0)
    return pd.Series(out, index=index)


def build_model_panel(intros, close, earnings_near, bench="SPY", vol_window=60) -> pd.DataFrame:
    ex = excess_returns(close, bench)
    sigma = ex.rolling(vol_window, min_periods=vol_window).std().shift(1)
    dsm = _days_since_monthly(close.index)
    pos = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(close.index)}
    rows = []
    for sym, g in intros.groupby("symbol"):
        if sym not in close.columns:
            continue
        g = g.sort_values("date").reset_index(drop=True)
        prev = g.shift(1)
        for i, r in g.iterrows():
            t = pos.get(str(r["date"])[:10])
            if not r["valid"] or t is None or t == 0 or i == 0:
                continue
            c_prev = close[sym].iloc[t - 1]
            sd = sigma[sym].iloc[t - 1]
            mv = ex[sym].iloc[t - 1]
            if not np.isfinite(c_prev) or not np.isfinite(sd) or sd == 0:
                continue
            base = {"symbol": sym, "date": str(r["date"])[:10], "sigma60": sd,
                    "earnings": int((sym, close.index[t]) in earnings_near),
                    "days_since_monthly": int(dsm.iloc[t]), "log_price": float(np.log(c_prev))}
            hi, lo = prev.loc[i, "near_max"], prev.loc[i, "near_min"]
            if np.isfinite(hi):
                rows.append({**base, "side": "up", "y": r["above"],
                             "headroom": (hi - c_prev) / c_prev, "move_toward": mv / sd})
            if np.isfinite(lo):
                rows.append({**base, "side": "down", "y": r["below"],
                             "headroom": (c_prev - lo) / c_prev, "move_toward": -mv / sd})
    return pd.DataFrame(rows)


def _design(df, cols):
    return sm.add_constant(df[cols].astype(float), has_constant="add")


def fit_loso(panel, progress=None) -> pd.DataFrame:
    out = panel.copy()
    out["expected"] = np.nan
    syms = sorted(out["symbol"].unique())
    coefs = []
    for i, sym in enumerate(syms, 1):
        train = out[out["symbol"] != sym]
        cols = [c for c in FEATURES if train[c].nunique() > 1]
        model = sm.GLM(train["y"].astype(float), _design(train, cols),
                       family=sm.families.Poisson()).fit()
        test = out["symbol"] == sym
        out.loc[test, "expected"] = model.predict(_design(out[test], cols))
        coefs.append({"symbol": sym, **{k: float(v) for k, v in model.params.items()}})
        if progress is not None:
            progress.stage("excess_model", i, len(syms), sym)
            progress.put("loso_coefs", coefs)
    return out


def event_excess(events, panel, close_index, window=(1, 2)) -> pd.Series:
    exp = panel.set_index(["symbol", "date", "side"])["expected"]
    vals = []
    for _, e in events.iterrows():
        loc = close_index.get_loc(e["event_date"])
        side = "up" if e["direction"] > 0 else "down"
        total = 0.0
        for k in range(window[0], window[1] + 1):
            if loc + k >= len(close_index):
                total = np.nan
                break
            total += exp.get((e["symbol"], close_index[loc + k].strftime("%Y-%m-%d"), side), np.nan)
        vals.append(e["dir_intro"] - total)
    return pd.Series(vals, index=events.index)
```

- [ ] **Step 4: Run tests**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_strike_intro_excess.py -q -p no:cacheprovider --no-cov`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add strike_intro/excess_model.py tests/test_strike_intro_excess.py
git commit -m "strike_intro: leave-one-symbol-out Poisson excess model"
```

---

### Task 5: Significance battery

**Files:**
- Create: `strike_intro/battery.py`
- Test: `tests/test_strike_intro_battery.py`

**Interfaces:**
- Consumes: `RunProgress` (Task 3).
- Produces:
  - `compare_groups(a: np.ndarray, b: np.ndarray, strata_a=None, strata_b=None, weeks_a=None, weeks_b=None, n_perm=10000, n_boot=2000, seed=0, progress=None, label="") -> dict` with keys `n_a, n_b, mean_a, mean_b, median_a, median_b, cont_a, cont_b, diff, ci_lo, ci_hi, p_welch, p_perm, p_boot, p_mw, cohen_d` (p-values NaN when either group has < `MIN_N` = 10).
  - `mde(n_a, n_b, sd, alpha=0.05, power=0.8) -> float`.
  - `clustered_ols(df, y: str, xcols: list[str], cluster_cols=("entry_date", "symbol")) -> pd.DataFrame[term, coef, se, p]` (drops constant columns; two-way clustered SEs).
  - `MIN_N = 10`.

- [ ] **Step 1: Write the failing tests**

```python
"""Battery calibration: detects a planted effect, stays quiet on noise, survives tiny groups."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strike_intro import battery as bt


def test_planted_effect_is_detected_by_every_test():
    rng = np.random.default_rng(0)
    a, b = rng.normal(0.02, 0.05, 400), rng.normal(0.0, 0.05, 400)
    r = bt.compare_groups(a, b, n_perm=2000, n_boot=500)
    assert r["diff"] > 0.01 and r["ci_lo"] > 0
    for k in ("p_welch", "p_perm", "p_boot", "p_mw"):
        assert r[k] < 0.01, k


def test_null_p_values_are_roughly_uniform():
    rng = np.random.default_rng(1)
    ps = [bt.compare_groups(rng.normal(0, 1, 60), rng.normal(0, 1, 60),
                            n_perm=300, n_boot=100, seed=i)["p_perm"] for i in range(200)]
    ps = np.array(ps)
    assert 0.02 < (ps < 0.05).mean() < 0.10
    assert 0.40 < np.median(ps) < 0.60


def test_tiny_group_gives_nan_p_values_not_zero():
    r = bt.compare_groups(np.array([0.1, 0.2, 0.3]), np.random.default_rng(2).normal(0, 1, 50))
    assert r["n_a"] == 3 and np.isnan(r["p_perm"]) and np.isnan(r["p_welch"])


def test_permutation_respects_strata():
    # Group a is all in stratum 0, which has a high mean for both groups: shuffling
    # within strata must not call that a group effect.
    rng = np.random.default_rng(3)
    a = rng.normal(1.0, 0.1, 100)
    b = np.r_[rng.normal(1.0, 0.1, 100), rng.normal(0.0, 0.1, 100)]
    r = bt.compare_groups(a, b, strata_a=np.zeros(100), strata_b=np.r_[np.zeros(100), np.ones(100)],
                          n_perm=2000, n_boot=200)
    assert r["p_perm"] > 0.05


def test_mde_shrinks_with_n():
    assert bt.mde(1000, 1000, 0.05) < bt.mde(100, 100, 0.05)


def test_clustered_ols_drops_constant_column():
    rng = np.random.default_rng(4)
    df = pd.DataFrame({"y": rng.normal(size=200), "hit": rng.integers(0, 2, 200),
                       "earn": 0, "entry_date": np.repeat(np.arange(40), 5),
                       "symbol": np.tile(list("ABCDE"), 40)})
    out = bt.clustered_ols(df, "y", ["hit", "earn"])
    assert set(out["term"]) == {"const", "hit"}
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_strike_intro_battery.py -q -p no:cacheprovider --no-cov`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement**

`strike_intro/battery.py`:
```python
"""HIT-vs-MISS significance battery (spec: Evaluation)."""
import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy import stats

MIN_N = 10


def _nan_result(a, b):
    return {"n_a": len(a), "n_b": len(b), "mean_a": np.nanmean(a) if len(a) else np.nan,
            "mean_b": np.nanmean(b) if len(b) else np.nan, "median_a": np.nan,
            "median_b": np.nan, "cont_a": np.nan, "cont_b": np.nan, "diff": np.nan,
            "ci_lo": np.nan, "ci_hi": np.nan, "p_welch": np.nan, "p_perm": np.nan,
            "p_boot": np.nan, "p_mw": np.nan, "cohen_d": np.nan}


def compare_groups(a, b, strata_a=None, strata_b=None, weeks_a=None, weeks_b=None,
                   n_perm=10000, n_boot=2000, seed=0, progress=None, label="") -> dict:
    a, b = np.asarray(a, float), np.asarray(b, float)
    ka, kb = np.isfinite(a), np.isfinite(b)
    a, b = a[ka], b[kb]
    strata_a = None if strata_a is None else np.asarray(strata_a)[ka]
    strata_b = None if strata_b is None else np.asarray(strata_b)[kb]
    weeks_a = None if weeks_a is None else np.asarray(weeks_a)[ka]
    weeks_b = None if weeks_b is None else np.asarray(weeks_b)[kb]
    if len(a) < MIN_N or len(b) < MIN_N:
        return _nan_result(a, b)
    rng = np.random.default_rng(seed)
    diff = a.mean() - b.mean()
    pooled = np.sqrt(((len(a) - 1) * a.var(ddof=1) + (len(b) - 1) * b.var(ddof=1))
                     / (len(a) + len(b) - 2))

    # permutation: shuffle labels within strata, vectorised (lexsort by
    # (random key, stratum) = an independent random order inside every stratum)
    vals = np.r_[a, b]
    lab = np.r_[np.ones(len(a), bool), np.zeros(len(b), bool)]
    strata = (np.r_[strata_a, strata_b] if strata_a is not None and strata_b is not None
              else np.zeros(len(vals)))
    codes = pd.factorize(strata)[0]
    base = np.lexsort((np.arange(len(vals)), codes))
    null = np.empty(n_perm)
    for i in range(n_perm):
        order = np.lexsort((rng.random(len(vals)), codes))
        perm = np.empty_like(lab)
        perm[base] = lab[order]
        null[i] = vals[perm].mean() - vals[~perm].mean()
        if progress is not None and (i + 1) % 1000 == 0:
            progress.stage("permutation", i + 1, n_perm, label)
            progress.put("perm_null", {"label": label, "observed": float(diff),
                                       "sample": null[: i + 1][-2000:].tolist()})
    p_perm = (1 + np.sum(np.abs(null) >= abs(diff))) / (n_perm + 1)

    # week-block bootstrap, vectorised: resample weeks via a multinomial count
    # matrix and recombine per-week sums
    wa = weeks_a if weeks_a is not None else np.arange(len(a))
    wb = weeks_b if weeks_b is not None else np.arange(len(b)) + 10**9
    codes, uniq = pd.factorize(np.r_[wa, wb])
    ca, cb = codes[:len(a)], codes[len(a):]
    W = len(uniq)
    sa, na = np.bincount(ca, a, W), np.bincount(ca, minlength=W)
    sb, nb = np.bincount(cb, b, W), np.bincount(cb, minlength=W)
    K = rng.multinomial(W, np.full(W, 1.0 / W), size=n_boot)
    with np.errstate(invalid="ignore", divide="ignore"):
        boots = (K @ sa) / (K @ na) - (K @ sb) / (K @ nb)
    boots = boots[np.isfinite(boots)]
    p_boot = float(min(1.0, 2 * min((boots <= 0).mean(), (boots >= 0).mean())))

    return {
        "n_a": len(a), "n_b": len(b), "mean_a": a.mean(), "mean_b": b.mean(),
        "median_a": np.median(a), "median_b": np.median(b),
        "cont_a": (a > 0).mean(), "cont_b": (b > 0).mean(), "diff": diff,
        "ci_lo": float(np.percentile(boots, 2.5)), "ci_hi": float(np.percentile(boots, 97.5)),
        "p_welch": float(stats.ttest_ind(a, b, equal_var=False).pvalue),
        "p_perm": float(p_perm), "p_boot": p_boot,
        "p_mw": float(stats.mannwhitneyu(a, b, alternative="two-sided").pvalue),
        "cohen_d": float(diff / pooled) if pooled > 0 else np.nan,
    }


def mde(n_a, n_b, sd, alpha=0.05, power=0.8) -> float:
    z = stats.norm.ppf(1 - alpha / 2) + stats.norm.ppf(power)
    return float(z * sd * np.sqrt(1 / n_a + 1 / n_b))


def clustered_ols(df, y, xcols, cluster_cols=("entry_date", "symbol")) -> pd.DataFrame:
    d = df.dropna(subset=[y] + list(xcols))
    cols = [c for c in xcols if d[c].nunique() > 1]
    X = sm.add_constant(d[cols].astype(float), has_constant="add")
    groups = np.column_stack([pd.factorize(d[c])[0] for c in cluster_cols])
    res = sm.OLS(d[y].astype(float), X).fit(cov_type="cluster", cov_kwds={"groups": groups})
    return pd.DataFrame({"term": res.params.index, "coef": res.params.values,
                         "se": res.bse.values, "p": res.pvalues.values})
```

- [ ] **Step 4: Run tests**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_strike_intro_battery.py -q -p no:cacheprovider --no-cov`
Expected: 6 passed (the uniformity test takes ~1 min).

- [ ] **Step 5: Commit**

```bash
git add strike_intro/battery.py tests/test_strike_intro_battery.py
git commit -m "strike_intro: significance battery (Welch/permutation/block bootstrap/MW/clustered OLS/MDE)"
```

---

### Task 6: Interactive report

**Files:**
- Create: `strike_intro/report.py`
- Test: `tests/test_strike_intro_report.py`

**Interfaces:**
- Consumes: results produced by Task 7 — `events` (with `trigger, symbol, event_date, direction, hit, excess, earnings, ret_<h>, absret_21, rv_ratio`), `car` (event-indexed CAR matrix), `tests` (one row per test with `test_id, trigger, measure, horizon, subgroup, n_a, n_b, diff, ci_lo, ci_hi, p_welch, p_perm, p_boot, p_mw, p_adj, cohen_d, primary`), `regression` table, `equity` (date-indexed cumulative return Series), `ranges` (`symbol, date, close, near_min, near_max, above, below`), and `meta` dict (universe size, symbols complete, date range, MDE).
- Produces: `build_report(results: dict, path: str) -> str` writing one self-contained HTML file (Plotly via cdn.jsdelivr.net) and returning the path.

Charts follow the dataviz skill: reference palette slots 1–3 (`#2a78d6` HIT, `#eb6834` MISS, `#1baf7a` third), one axis per chart, legends + hover on every chart, a data table, light/dark tokens.

- [ ] **Step 1: Write the failing test**

```python
"""Report renders every section from a synthetic results bundle."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strike_intro import report


def _bundle():
    rng = np.random.default_rng(0)
    n = 80
    ev = pd.DataFrame({"trigger": ["day"] * n, "symbol": "A",
                       "event_date": pd.bdate_range("2025-01-01", periods=n),
                       "direction": rng.choice([-1, 1], n), "hit": rng.random(n) > 0.5,
                       "excess": rng.normal(size=n), "earnings": rng.random(n) > 0.8,
                       **{f"ret_{h}": rng.normal(0, 0.02, n) for h in [1, 3, 5, 10, 21, 63, 126]},
                       "absret_21": rng.uniform(0, 0.1, n), "rv_ratio": rng.uniform(0.5, 2, n)})
    car = pd.DataFrame(rng.normal(0, 0.01, (n, 132)).cumsum(axis=1), columns=range(-5, 127))
    tests = pd.DataFrame([{"test_id": "day|ret_21|all", "trigger": "day", "measure": "ret",
                           "horizon": 21, "subgroup": "all", "n_a": 40, "n_b": 40, "diff": 0.01,
                           "ci_lo": -0.01, "ci_hi": 0.03, "p_welch": 0.2, "p_perm": 0.21,
                           "p_boot": 0.25, "p_mw": 0.3, "p_adj": np.nan, "cohen_d": 0.2,
                           "primary": True}])
    reg = pd.DataFrame({"term": ["const", "hit"], "coef": [0.0, 0.01], "se": [0.01, 0.01],
                        "p": [0.9, 0.3]})
    eq = pd.Series(np.cumsum(rng.normal(0, 0.01, 50)), index=pd.bdate_range("2025-02-01", periods=50))
    ranges = pd.DataFrame({"symbol": "A", "date": pd.bdate_range("2025-01-01", periods=30),
                           "close": 100.0, "near_min": 80.0, "near_max": 120.0,
                           "above": 0, "below": 0})
    meta = {"universe": 100, "complete": 1, "date_range": "2025-01..2025-06", "mde_21": 0.012}
    return {"events": ev, "car": car, "tests": tests, "regression": reg, "equity": eq,
            "ranges": ranges, "meta": meta}


def test_report_contains_every_section(tmp_path):
    path = report.build_report(_bundle(), str(tmp_path / "r.html"))
    html = open(path, encoding="utf-8").read()
    for title in ["Return paths after the event", "HIT minus MISS by horizon",
                  "Forward return by EXCESS tercile", "Volatility check",
                  "Event scatter", "Strike-range explorer", "All tests",
                  "Trading view", "Primary test"]:
        assert title in html, title
    assert "cdn.jsdelivr.net" in html
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_strike_intro_report.py -q -p no:cacheprovider --no-cov`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement**

`strike_intro/report.py`:
```python
"""Interactive HTML report for the strike-introduction study (Plotly)."""
import html as _html

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio

HIT, MISS, THIRD = "#2a78d6", "#eb6834", "#1baf7a"
HORIZONS = [1, 3, 5, 10, 21, 63, 126]
_CSS = """
:root{--bg:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--rule:#e4e3df}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--rule:#33332f}}
:root[data-theme="dark"]{--bg:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--rule:#33332f}
body{background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif;margin:0 auto;max-width:1100px;padding:16px}
h1{font-size:24px}h2{font-size:18px;margin-top:32px}p.note{color:var(--ink2)}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid var(--rule);padding:4px 6px;text-align:right}
th{cursor:pointer}td:first-child,th:first-child{text-align:left}.card{border:1px solid var(--rule);border-radius:8px;padding:12px 16px}
"""
_SORT_JS = """
document.querySelectorAll('table.sortable th').forEach((th,i)=>th.onclick=()=>{
 const tb=th.closest('table').tBodies[0],rows=[...tb.rows],asc=th.dataset.asc!=='1';
 rows.sort((a,b)=>{const x=a.cells[i].innerText,y=b.cells[i].innerText,nx=parseFloat(x),ny=parseFloat(y);
  return (isNaN(nx)||isNaN(ny)?x.localeCompare(y):nx-ny)*(asc?1:-1)});
 th.dataset.asc=asc?'1':'0';rows.forEach(r=>tb.appendChild(r));});
"""


def _layout(fig, title, ytitle, height=380):
    fig.update_layout(title=title, height=height, template="plotly_white",
                      margin=dict(l=10, r=10, t=50, b=10), hovermode="x unified",
                      legend=dict(orientation="h", y=1.1, x=0), yaxis_title=ytitle,
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    return fig


def _mean_band(m: pd.DataFrame):
    mu = m.mean()
    se = m.std() / np.sqrt(m.notna().sum())
    return mu, mu - 1.96 * se, mu + 1.96 * se


def _car_figure(ev, car):
    fig = go.Figure()
    combos = [(t, d, e) for t in sorted(ev["trigger"].unique())
              for d in ("all", "up", "down") for e in ("all", "earnings", "non-earnings")]
    buttons = []
    per = 6
    for ci, (t, d, e) in enumerate(combos):
        m = ev["trigger"] == t
        if d != "all":
            m &= ev["direction"] == (1 if d == "up" else -1)
        if e != "all":
            m &= ev["earnings"] == (e == "earnings")
        for name, col, sel in (("HIT", HIT, ev["hit"]), ("MISS", MISS, ~ev["hit"])):
            rows = car.loc[ev.index[m & sel]]
            mu, lo, hi = _mean_band(rows) if len(rows) else (pd.Series(dtype=float),) * 3
            x = list(car.columns)
            fig.add_trace(go.Scatter(x=x, y=hi * 100, line=dict(width=0), showlegend=False,
                                     hoverinfo="skip", visible=ci == 0))
            fig.add_trace(go.Scatter(x=x, y=lo * 100, fill="tonexty", line=dict(width=0),
                                     fillcolor=col + "22", showlegend=False, hoverinfo="skip",
                                     visible=ci == 0))
            fig.add_trace(go.Scatter(x=x, y=mu * 100, name=f"{name} (n={len(rows)})",
                                     line=dict(color=col, width=2), visible=ci == 0))
        vis = [False] * (len(combos) * per)
        vis[ci * per:(ci + 1) * per] = [True] * per
        buttons.append(dict(label=f"{t} | {d} | {e}", method="update", args=[{"visible": vis}]))
    fig.update_layout(updatemenus=[dict(buttons=buttons, x=0, y=1.25, xanchor="left")])
    fig.add_vline(x=0, line_dash="dot", line_color="#888")
    return _layout(fig, "Return paths after the event (signed excess vs SPY, entry = day 0)",
                   "% (positive = continuation)", 460)


def _diff_figure(tests):
    t = tests[(tests["measure"] == "ret") & (tests["subgroup"] == "all")]
    fig = go.Figure()
    for trig, col in zip(sorted(t["trigger"].unique()), (HIT, THIRD)):
        g = t[t["trigger"] == trig].sort_values("horizon")
        fig.add_trace(go.Bar(
            x=[str(h) for h in g["horizon"]], y=g["diff"] * 100, name=f"{trig} trigger",
            marker_color=col,
            error_y=dict(type="data", symmetric=False, array=(g["ci_hi"] - g["diff"]) * 100,
                         arrayminus=(g["diff"] - g["ci_lo"]) * 100),
            customdata=np.c_[g["p_perm"], g["p_adj"], g["n_a"], g["n_b"]],
            hovertemplate="h=%{x}d diff=%{y:.2f}%<br>p_perm=%{customdata[0]:.3f} "
                          "p_adj=%{customdata[1]:.3f}<br>n HIT=%{customdata[2]} "
                          "MISS=%{customdata[3]}<extra></extra>"))
    fig.update_layout(barmode="group")
    return _layout(fig, "HIT minus MISS by horizon (95% week-block bootstrap CI)",
                   "difference, % points")


def _tercile_figure(ev):
    d = ev[ev["trigger"] == "day"].dropna(subset=["excess", "ret_21"])
    fig = go.Figure()
    if len(d) >= 30:
        d = d.assign(t=pd.qcut(d["excess"].rank(method="first"), 3, labels=["low", "mid", "high"]))
        g = d.groupby("t", observed=True)["ret_21"].agg(["mean", "count", "std"])
        fig.add_trace(go.Bar(x=g.index.astype(str), y=g["mean"] * 100, marker_color=HIT,
                             name="mean 21d return",
                             error_y=dict(type="data", array=1.96 * g["std"] / np.sqrt(g["count"]) * 100),
                             customdata=g["count"], hovertemplate="%{x}: %{y:.2f}% (n=%{customdata})<extra></extra>"))
    return _layout(fig, "Forward return by EXCESS tercile (day trigger, 21 days)",
                   "% signed excess")


def _vol_figure(ev):
    fig = go.Figure()
    for name, col, sel in (("HIT", HIT, ev["hit"]), ("MISS", MISS, ~ev["hit"])):
        fig.add_trace(go.Box(y=ev.loc[sel, "absret_21"] * 100, name=f"{name} |21d move|",
                             marker_color=col, boxmean=True))
        fig.add_trace(go.Box(y=ev.loc[sel, "rv_ratio"], name=f"{name} vol ratio",
                             marker_color=col, boxmean=True, visible="legendonly"))
    return _layout(fig, "Volatility check: size of the next 21 days' move (direction ignored)",
                   "% / ratio")


def _scatter_figure(ev):
    d = ev[ev["trigger"] == "day"]
    fig = go.Figure(go.Scatter(
        x=d["excess"], y=d["ret_21"] * 100, mode="markers",
        marker=dict(size=8, color=np.where(d["hit"], HIT, MISS), opacity=0.7,
                    line=dict(width=1, color="white")),
        customdata=np.c_[d["symbol"], d["event_date"].astype(str).str[:10], d["earnings"]],
        hovertemplate="%{customdata[0]} %{customdata[1]}<br>EXCESS=%{x:.2f}<br>"
                      "21d=%{y:.2f}%<br>earnings=%{customdata[2]}<extra></extra>",
        name="events (blue = HIT, orange = MISS)"))
    fig.update_layout(hovermode="closest")
    return _layout(fig, "Event scatter: EXCESS vs 21-day signed return (day trigger)", "%")


def _ranges_figure(ranges):
    fig = go.Figure()
    syms = sorted(ranges["symbol"].unique())
    buttons = []
    for i, s in enumerate(syms):
        g = ranges[ranges["symbol"] == s]
        vis = i == 0
        fig.add_trace(go.Scatter(x=g["date"], y=g["near_max"], name="near-expiry max strike",
                                 line=dict(color=HIT, width=1), visible=vis))
        fig.add_trace(go.Scatter(x=g["date"], y=g["near_min"], name="near-expiry min strike",
                                 line=dict(color=HIT, width=1), fill="tonexty",
                                 fillcolor=HIT + "1a", visible=vis))
        fig.add_trace(go.Scatter(x=g["date"], y=g["close"], name="close",
                                 line=dict(color=MISS, width=2), visible=vis))
        up = g[g["above"] > 0]
        dn = g[g["below"] > 0]
        fig.add_trace(go.Scatter(x=up["date"], y=up["near_max"], mode="markers",
                                 name="strikes added above", marker=dict(color=THIRD, size=8,
                                 symbol="triangle-up"), visible=vis))
        fig.add_trace(go.Scatter(x=dn["date"], y=dn["near_min"], mode="markers",
                                 name="strikes added below", marker=dict(color=THIRD, size=8,
                                 symbol="triangle-down"), visible=vis))
        v = [False] * (5 * len(syms))
        v[i * 5:(i + 1) * 5] = [True] * 5
        buttons.append(dict(label=s, method="update", args=[{"visible": v}]))
    fig.update_layout(updatemenus=[dict(buttons=buttons, x=0, y=1.2, xanchor="left")])
    return _layout(fig, "Strike-range explorer: price inside its nearest-expiry strike range",
                   "USD", 440)


def _equity_figure(eq):
    fig = go.Figure(go.Scatter(x=eq.index, y=eq * 100, line=dict(color=HIT, width=2),
                               name="cumulative return"))
    return _layout(fig, "Trading view (illustrative): long up-move HITs / short down-move HITs, "
                        "21-day hold, 10 bp per side", "% cumulative")


def _table(df, cls="sortable"):
    head = "".join(f"<th>{_html.escape(str(c))}</th>" for c in df.columns)
    body = "".join("<tr>" + "".join(
        f"<td>{v:.4g}</td>" if isinstance(v, (float, np.floating)) else f"<td>{_html.escape(str(v))}</td>"
        for v in r) + "</tr>" for r in df.itertuples(index=False))
    return f'<table class="{cls}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def build_report(results: dict, path: str) -> str:
    ev, tests, meta = results["events"], results["tests"], results["meta"]
    prim = tests[tests["primary"]].iloc[0] if tests["primary"].any() else None
    figs = [_car_figure(ev, results["car"]), _diff_figure(tests), _tercile_figure(ev),
            _vol_figure(ev), _scatter_figure(ev), _ranges_figure(results["ranges"]),
            _equity_figure(results["equity"])]
    parts = [pio.to_html(f, full_html=False, include_plotlyjs=False) for f in figs]
    primary = ("<p>No primary result.</p>" if prim is None else
               f"<p><b>HIT − MISS, day trigger, 21 days:</b> {prim['diff']*100:.2f} pts "
               f"(95% CI {prim['ci_lo']*100:.2f} to {prim['ci_hi']*100:.2f}); "
               f"n = {int(prim['n_a'])} HIT / {int(prim['n_b'])} MISS; "
               f"permutation p = {prim['p_perm']:.3f}, Welch p = {prim['p_welch']:.3f}, "
               f"bootstrap p = {prim['p_boot']:.3f}, Mann-Whitney p = {prim['p_mw']:.3f}; "
               f"Cohen's d = {prim['cohen_d']:.2f}. Minimum detectable effect at 80% power: "
               f"{meta.get('mde_21', float('nan'))*100:.2f} pts.</p>")
    body = f"""
<h1>Option strike introductions after a move</h1>
<p class="note">Universe {meta['universe']} names ({meta['complete']} with complete listing
history), {_html.escape(str(meta['date_range']))}. Returns are excess vs SPY, signed so that
positive = the move continued. Entry is the close 3 trading days after the move.</p>
<div class="card"><h2>Primary test (pre-registered)</h2>{primary}</div>
{parts[0]}{parts[1]}{parts[2]}{parts[3]}{parts[4]}{parts[5]}
<h2>All tests</h2><p class="note">Click a column to sort. p_adj = Benjamini-Hochberg across
all secondary tests.</p>{_table(tests)}
<h2>Regression (two-way clustered by entry date and symbol)</h2>{_table(results['regression'], 'plain')}
{parts[6]}"""
    doc = (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' "
           f"content='width=device-width,initial-scale=1'><title>Strike Introduction Study</title>"
           f"<script src='https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js'></script>"
           f"<style>{_CSS}</style></head><body>{body}<script>{_SORT_JS}</script></body></html>")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path
```

- [ ] **Step 4: Run test**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_strike_intro_report.py -q -p no:cacheprovider --no-cov`
Expected: 1 passed. Then open the HTML (`Start-Process <path>`) and eyeball it for label collisions/overflow (dataviz step 7).

- [ ] **Step 5: Commit**

```bash
git add strike_intro/report.py tests/test_strike_intro_report.py
git commit -m "strike_intro: interactive Plotly report"
```

---

### Task 7: Study runner — real data in, results/registry/report out

**Files:**
- Create: `strike_intro/run.py`
- Test: `tests/test_strike_intro_run.py`

**Interfaces:**
- Consumes: everything above; `massive_backfill_progress.load_progress`/`load_window`, `event_backtest.load_close_matrix`, `event_backtest.earnings_events`, `evaluation.stats.bh_fdr`, `evaluation.registry`.
- Produces:
  - `complete_symbols(out_root: str) -> list[str]` — symbols whose checkpoint reached the manifest end.
  - `run_study(changes, summary, close, earnings, run_dir, progress=None, n_perm=10000, n_boot=2000) -> dict` (the results bundle consumed by `report.build_report`), also writing `events.parquet`, `tests.parquet`, `regression.parquet` into `run_dir`.
  - CLI `python -m strike_intro.run [--n-perm N] [--register]` → `storage/reports/strike_intro/<run_id>/` with `report.html`, parquet outputs and `progress.json`.

- [ ] **Step 1: Write the failing tests**

```python
"""End-to-end on synthetic data: a planted continuation effect is found; leakage probe holds."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strike_intro import run


def _world(effect: float, seed=0, n_sym=12, n_days=420):
    """Prices with jumps; strikes added above/below after some jumps; HIT events drift by `effect`."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-10-07", periods=n_days)
    spy = 100 * np.cumprod(1 + rng.normal(0, 0.006, n_days))
    close = {"SPY": spy}
    ch, summ = [], []
    for s in range(n_sym):
        sym = f"S{s}"
        r = rng.normal(0, 0.012, n_days)
        hit_days = set()
        for e in rng.choice(np.arange(80, n_days - 140), 12, replace=False):
            d = rng.choice([-1, 1])
            r[e] += d * 0.08
            if rng.random() < 0.5:
                hit_days.add((e, d))
                r[e + 4:e + 25] += d * effect / 21          # drift after entry (e+3)
        close[sym] = 50 * np.cumprod(1 + r)
        dates = [d.strftime("%Y-%m-%d") for d in idx]
        for k in range(0, 200, 10):
            ch.append((sym, dates[0], f"{sym}c{k}", float(k), "2027-01-15", "initial"))
        for e, d in hit_days:
            ch.append((sym, dates[e + 1], f"{sym}x{e}", 500.0 if d > 0 else -1.0, "2027-01-15", "added"))
        summ += [{"symbol": sym, "date": dd, "status": "ok", "replaced_frac": 0.0} for dd in dates]
    changes = pd.DataFrame(ch, columns=["symbol", "date", "contract_ticker", "strike",
                                        "expiration_date", "change"])
    return changes, pd.DataFrame(summ), pd.DataFrame(close, index=idx)


def test_planted_effect_is_found_in_the_primary_test(tmp_path):
    changes, summary, close = _world(effect=0.06)
    res = run.run_study(changes, summary, close, pd.DataFrame(columns=["symbol", "date"]),
                        str(tmp_path), n_perm=1000, n_boot=300)
    prim = res["tests"][res["tests"]["primary"]].iloc[0]
    assert prim["diff"] > 0.02 and prim["p_perm"] < 0.01
    assert os.path.exists(tmp_path / "events.parquet")


def test_no_effect_world_is_not_significant(tmp_path):
    changes, summary, close = _world(effect=0.0, seed=5)
    res = run.run_study(changes, summary, close, pd.DataFrame(columns=["symbol", "date"]),
                        str(tmp_path), n_perm=1000, n_boot=300)
    prim = res["tests"][res["tests"]["primary"]].iloc[0]
    assert prim["p_perm"] > 0.01


def test_complete_symbols_only_counts_finished_checkpoints(tmp_path):
    import json
    d = tmp_path / "state" / "massive_listings"
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"start": "2024-10-06", "end": "2026-10-01",
                                            "launched_at": "2026-10-01T22:38:07"}), encoding="utf-8")
    (d / "A.json").write_text(json.dumps({"last_date": "2026-10-01"}), encoding="utf-8")
    (d / "B.json").write_text(json.dumps({"last_date": "2025-03-01"}), encoding="utf-8")
    assert run.complete_symbols(str(tmp_path)) == ["A"]
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_strike_intro_run.py -q -p no:cacheprovider --no-cov`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement**

`strike_intro/run.py`:
```python
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
    strata = e["symbol"] + "|" + pd.to_datetime(e["entry_date"]).dt.strftime("%Y-%m")
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
```

- [ ] **Step 4: Run tests**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_strike_intro_run.py -q -p no:cacheprovider --no-cov`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add strike_intro/run.py tests/test_strike_intro_run.py
git commit -m "strike_intro: study runner, results, registry rows"
```

---

### Task 8: Live study page (watch the model fit and the tests in real time)

**Files:**
- Create: `scripts/strike_intro_study_dashboard.py`
- Test: `tests/test_strike_intro_study_dashboard.py`

**Interfaces:**
- Consumes: `read_progress` (Task 3); run dirs under `storage/reports/strike_intro/`.
- Produces: Streamlit page (2 s auto-refresh) showing the latest run's stage progress bars, the LOSO coefficient stability (one box per coefficient across folds, updating as folds finish), and the permutation null distribution filling in with the observed difference marked.

- [ ] **Step 1: Write the failing test**

```python
"""The live study page renders from a progress.json mid-run."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_page_renders_stages_coefficients_and_null(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    from strike_intro.progress import RunProgress
    run = tmp_path / "20261002_120000"
    p = RunProgress(str(run))
    p.stage("excess_model", 2, 10, "MSFT")
    p.put("loso_coefs", [{"symbol": "AAPL", "const": 0.1, "headroom": -5.0},
                         {"symbol": "MSFT", "const": 0.2, "headroom": -4.5}])
    p.put("perm_null", {"label": "day|ret_21|all", "observed": 0.01, "sample": [0.0, 0.001, -0.002]})
    monkeypatch.setenv("STRIKE_INTRO_RUNS", str(tmp_path))
    app = AppTest.from_file(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "scripts", "strike_intro_study_dashboard.py"), default_timeout=60).run()
    assert not app.exception, app.exception
    assert any("excess_model" in str(t.value) for t in app.markdown + app.caption)
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_strike_intro_study_dashboard.py -q -p no:cacheprovider --no-cov`
Expected: FAIL — file not found.

- [ ] **Step 3: Implement**

`scripts/strike_intro_study_dashboard.py`:
```python
"""
Live view of a strike-introduction study run: stage progress, the leave-one-
symbol-out model's coefficients as each fold finishes, and the permutation
null filling in. Reads storage/reports/strike_intro/<run_id>/progress.json.

Run:
  C:\\ProgramData\\anaconda3\\python.exe -m streamlit run scripts/strike_intro_study_dashboard.py --server.port 8502 --server.headless true
"""
import glob
import os
import sys

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)
from strike_intro.progress import read_progress  # noqa: E402

RUNS = os.environ.get("STRIKE_INTRO_RUNS", os.path.join("storage", "reports", "strike_intro"))
BLUE, ORANGE = "#2a78d6", "#eb6834"

st.set_page_config(page_title="Strike-intro study run", layout="wide")
st.title("Strike-introduction study - live run")


@st.fragment(run_every="2s")
def live():
    runs = sorted(glob.glob(os.path.join(RUNS, "*")))
    if not runs:
        st.info("No study run yet. Start one: python -m strike_intro.run")
        return
    run = st.selectbox("Run", runs[::-1], format_func=os.path.basename, key="run")
    p = read_progress(run)
    st.caption(f"Updated {p['updated_at']}")
    for name, s in p["stages"].items():
        frac = s["done"] / s["total"] if s["total"] else 0
        st.markdown(f"**{name}** - {s['done']}/{s['total']} {s['note']}")
        st.progress(min(1.0, frac))

    coefs = pd.DataFrame(p["data"].get("loso_coefs", []))
    if not coefs.empty:
        fig = go.Figure()
        for c in [c for c in coefs.columns if c != "symbol"]:
            fig.add_trace(go.Box(y=coefs[c], name=c, marker_color=BLUE, boxpoints="all",
                                 customdata=coefs["symbol"],
                                 hovertemplate="%{customdata}: %{y:.3f}<extra>" + c + "</extra>"))
        fig.update_layout(title=f"Excess model coefficients across {len(coefs)} folds "
                                f"(stable = mechanics are consistent across stocks)",
                          height=380, showlegend=False, margin=dict(l=10, r=10, t=50, b=10))
        st.plotly_chart(fig, use_container_width=True)

    null = p["data"].get("perm_null")
    if null and null.get("sample"):
        fig = go.Figure(go.Histogram(x=[v * 100 for v in null["sample"]], nbinsx=60,
                                     marker_color=BLUE, name="shuffled-label differences"))
        fig.add_vline(x=null["observed"] * 100, line_color=ORANGE, line_width=3,
                      annotation_text="observed")
        fig.update_layout(title=f"Permutation null - {null['label']} (% points)", height=340,
                          margin=dict(l=10, r=10, t=50, b=10))
        st.plotly_chart(fig, use_container_width=True)


live()
```

- [ ] **Step 4: Run test**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_strike_intro_study_dashboard.py -q -p no:cacheprovider --no-cov`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/strike_intro_study_dashboard.py tests/test_strike_intro_study_dashboard.py
git commit -m "strike_intro: live study-run page"
```

---

### Task 9: First real run on completed symbols, publish, write up

**Files:**
- Create: `experiments/2026-10-xx_strike-intro.md` (date = run date)

- [ ] **Step 1:** Rebuild curated for the two Massive tables:
  `C:\ProgramData\anaconda3\python.exe curated.py --table option_listing_changes` and `--table option_chain_summary`.
- [ ] **Step 2:** Start the live page on port 8502 detached (headless), then run
  `C:\ProgramData\anaconda3\python.exe -m strike_intro.run` (no `--register` while the backfill is incomplete — registry rows are for the final run).
  Expected: `report: storage/reports/strike_intro/<run_id>/report.html`.
- [ ] **Step 3:** Open the report, eyeball every chart (label collisions, empty panels). If fewer than ~10 symbols are complete, label the run "interim" in the report title and writeup.
- [ ] **Step 4:** Publish the report as a private artifact (Artifact tool, `icon: chart`).
- [ ] **Step 5:** Write `experiments/2026-10-xx_strike-intro.md` (experiment-writeup skill): hypothesis, data, method, primary result with CI and MDE, secondary results, limitations (survivorship tilt, 2 years, one regime, interim universe), decision. Commit it.
