# Strike Introduction Study — Increment 1: Data Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect a lossless daily history (2 years, ~100 names) of which option contracts were listed, from Massive's `as_of` contracts endpoint, as the input to the strike-introduction study.

**Architecture:** A gate probe first proves Massive's `as_of` history includes since-expired contracts. Then one pipeline (`massive_option_listings_pipeline.py`) walks each symbol day by day, diffs each day's standard-contract list against the previous day, and writes only changes plus a per-day summary, checkpointing per symbol so a ~3-week detached backfill can resume. Increment 2 (analysis + report) gets its own plan after real data exists.

**Tech Stack:** Python 3.11 (`C:\ProgramData\anaconda3\python.exe`), requests, pandas/pyarrow, DuckDB query layer (`query.py`), pytest.

**Spec:** `docs/superpowers/specs/2026-10-01-strike-intro-study-design.md`

## Gate result (Task 1, 2026-10-01): PASS (A)

`as_of` alone returns every contract live on that date, including ones that have since
expired; `expired=true` (with `expiration_date.gte=as_of`) adds nothing.
`EXPIRED_QUERIES = (None,)`, `BASE_URL = https://api.massive.com`.

| AAPL as_of | default | expired=true | expirations | first..last expiry |
|---|---|---|---|---|
| 2025-04-09 | 2,456 | 0 | 20 | 2025-04-11..2027-12-17 |
| 2026-04-04 | 3,080 | 0 | 25 | 2026-04-06..2028-12-15 |
| 2026-10-01 | 3,538 | (not run) | 25 | 2026-10-02..2029-01-19 |

Today's 3,538 equals Schwab's full-chain AAPL count measured the same day.

## Global Constraints

- Python: always `C:\ProgramData\anaconda3\python.exe`; run from repo root.
- Key: `MASSIVE_API_KEY` in `.env`; never print or log its value (it travels as the `apiKey` query param — strip it from any logged URL).
- Free tier: 5 requests/minute, 2 years of history. Pace requests at 12.5 s.
- Standard contracts only: `shares_per_contract == 100`, contract root == underlying symbol, no `additional_underlyings`.
- Never name a column `year` or `month` (Hive partition shadowing). ASCII-only console output.
- Write via `storage_utils.write_partitioned()`; every row carries `fetched_at` (naive UTC ISO).
- Long jobs launch detached (`Start-Process` + `python -u` + log file), never as a child of the agent shell.

## Review Focus

1. **A day returns zero contracts** (API hiccup) while the previous day had thousands → must be recorded as `status='missing'`, not as every contract removed then re-added the next day. Test in Task 3.
2. **Pagination `next_url`** comes back without the API key → every page after the first must still be authenticated. Test in Task 4.
3. **Crash mid-chunk, then resume** → the output must equal an uninterrupted run (no skipped or doubled days). Test in Task 5.
4. **Adjusted/odd roots** (`O:FDX1...`, class shares) → excluded, and never counted as "new strikes". Test in Task 3.
5. **A contract that expires** → shows as `removed` on the first trading day after expiration, never as a phantom listing. Test in Task 3.

---

### Task 1: Step 0 gate — prove `as_of` history is usable

**Prerequisite (Zander):** sign up at massive.com (free "Options Basic"), add `MASSIVE_API_KEY=<key>` to `.env`.

**Files:**
- Create: `scripts/massive_gate_probe.py`

**Interfaces:**
- Produces: a recorded decision for `EXPIRED_QUERIES` in Task 4 — `(None,)` if `as_of` alone returns contracts live on that date, or `("false", "true")` if since-expired contracts only appear with `expired=true`. Also confirms `BASE_URL` (`https://api.massive.com`, fallback `https://api.polygon.io`).

- [ ] **Step 1: Write the probe**

```python
"""Step 0 gate for the strike-introduction study (spec 2026-10-01).

Answers, for AAPL: does `as_of` history include contracts that have since
expired? Prints counts only -- never the API key.
"""
import datetime as dt
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()
KEY = os.environ.get("MASSIVE_API_KEY")
if not KEY:
    sys.exit("MASSIVE_API_KEY missing from .env")

PATH = "/v3/reference/options/contracts"


def count(base, as_of, expired=None):
    params = {"underlying_ticker": "AAPL", "as_of": as_of, "limit": 1000, "apiKey": KEY}
    if expired is not None:
        params["expired"] = expired
    n, live, url, exps = 0, 0, base + PATH, set()
    while url:
        r = requests.get(url, params=params, timeout=30)
        if r.status_code != 200:
            return f"HTTP {r.status_code}: {r.text[:120]}"
        body = r.json()
        for c in body.get("results", []):
            n += 1
            exps.add(c["expiration_date"])
            live += c["expiration_date"] >= as_of
        url, params = body.get("next_url"), {"apiKey": KEY}
    return f"contracts={n} live_on_date={live} expirations={len(exps)} " \
           f"first_exp={min(exps) if exps else None}"


for base in ("https://api.massive.com", "https://api.polygon.io"):
    print("BASE", base)
    for days_back in (0, 180, 540):
        d = (dt.date.today() - dt.timedelta(days=days_back)).isoformat()
        print(f"  as_of={d} default      :", count(base, d))
        print(f"  as_of={d} expired=true :", count(base, d, "true"))
    break  # remove only if the first base fails
```

- [ ] **Step 2: Run it**

Run: `C:\ProgramData\anaconda3\python.exe -u scripts/massive_gate_probe.py`
Expected: three `as_of` dates, each with a contract count. Requests are slow (5/min); the probe takes ~10 min.

- [ ] **Step 3: Decide**

- PASS (A): for `as_of` 540 days back, the default query returns thousands of contracts with `live_on_date` ≈ `contracts` and `first_exp` near that date → `EXPIRED_QUERIES = (None,)`.
- PASS (B): default returns ~0 for 540 days back but `expired=true` returns thousands live on that date → `EXPIRED_QUERIES = ("false", "true")`.
- FAIL: neither returns contracts that were live 540 days ago → STOP the plan; report to Zander (paid source needed).
- Sanity: today's count should be within ~5% of AAPL's contract count in the first full `schwab_options` snapshot (2026-10-02): `q.sql("SELECT COUNT(*) FROM schwab_options WHERE symbol='AAPL' AND snapshot_date='2026-10-02'")`.

- [ ] **Step 4: Record and commit**

Write the outcome (A/B/FAIL, the printed counts, the base URL) at the top of this plan under a `## Gate result` heading.

```bash
git add scripts/massive_gate_probe.py docs/superpowers/plans/2026-10-01-strike-intro-data.md
git commit -m "Strike-intro study: Massive as_of gate probe and result"
```

---

### Task 2: Freeze the study universe

**Files:**
- Create: `scripts/build_strike_intro_universe.py`
- Create: `experiments/strike_intro_universe.csv`

**Interfaces:**
- Produces: `experiments/strike_intro_universe.csv` with columns `rank,symbol,total_option_volume` (100 rows). Task 5 reads its `symbol` column.

- [ ] **Step 1: Write the script**

```python
"""Freeze the ~100-name universe for the strike-introduction study.

S&P 500 single stocks (IVV holdings -- no ETFs) ranked by total option volume
across schwab_options snapshots 2026-08-02..2026-09-30. Selection on recent
activity is a survivorship tilt; the writeup must say so.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import query as q
from analytics.portfolio import top_holdings

N = 100
# Class shares lose their dot in IVV holdings (BRKB, BFB) and won't match
# option roots, so they can't be measured cleanly.
EXCLUDE = {"BRKB", "BFB"}

stocks = set(top_holdings("IVV", n=600)["holding_ticker"].dropna()) - EXCLUDE
vol = q.sql("""
    SELECT symbol, SUM(volume) AS total_option_volume
    FROM schwab_options
    WHERE snapshot_date BETWEEN '2026-08-02' AND '2026-09-30'
    GROUP BY symbol
""")
vol = vol[vol["symbol"].isin(stocks)].sort_values("total_option_volume", ascending=False)
out = vol.head(N).reset_index(drop=True)
out.insert(0, "rank", out.index + 1)
out.to_csv("experiments/strike_intro_universe.csv", index=False)
print(f"{len(out)} symbols; top 5: {', '.join(out['symbol'].head(5))}")
```

- [ ] **Step 2: Run and inspect**

Run: `C:\ProgramData\anaconda3\python.exe scripts/build_strike_intro_universe.py`
Expected: `100 symbols; top 5: ...` with familiar heavy-options names (e.g. NVDA, TSLA, AAPL). No SPY/QQQ/IWM.

- [ ] **Step 3: Commit**

```bash
git add scripts/build_strike_intro_universe.py experiments/strike_intro_universe.csv
git commit -m "Strike-intro study: freeze 100-name universe by option volume"
```

---

### Task 3: Pure functions — standard-contract filter, daily diff, summary, replay

**Files:**
- Create: `massive_option_listings_pipeline.py` (pure functions only in this task)
- Test: `tests/test_massive_option_listings.py`

**Interfaces:**
- Produces:
  - `standard_frame(results: list[dict], symbol: str) -> pd.DataFrame` with columns `contract_ticker, strike, expiration_date, put_call, shares_per_contract`, filtered to standard contracts live on the query date is NOT done here (see `live_on`).
  - `live_on(df: pd.DataFrame, as_of: str) -> pd.DataFrame` — rows with `expiration_date >= as_of`.
  - `diff_contracts(prev: pd.DataFrame | None, curr: pd.DataFrame, symbol: str, date: str) -> pd.DataFrame` with columns `symbol, date, contract_ticker, strike, expiration_date, put_call, shares_per_contract, change` (`initial` when `prev is None`, else `added`/`removed`).
  - `summarize(curr: pd.DataFrame, symbol: str, date: str, status: str) -> dict` with keys `symbol, date, status, n_contracts, n_expirations, min_strike, max_strike`.
  - `replay_live_set(changes: pd.DataFrame, date: str) -> set[str]` — contract tickers live on `date` reconstructed from changes up to and including `date`.
  - `CHANGE_COLS: list[str]` (the diff column order above).

- [ ] **Step 1: Write the failing tests**

```python
"""Pure-function tests for massive_option_listings_pipeline (no network)."""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import massive_option_listings_pipeline as mol


def _c(ticker, strike, exp, typ="call", shares=100, extra=None):
    d = {"ticker": ticker, "strike_price": strike, "expiration_date": exp,
         "contract_type": typ, "shares_per_contract": shares}
    if extra:
        d["additional_underlyings"] = extra
    return d


def test_standard_frame_drops_adjusted_and_odd_size_contracts():
    rows = [
        _c("O:FDX261016C00195000", 195.0, "2026-10-16"),
        _c("O:FDX1261016C00195000", 195.0, "2026-10-16",
           extra=[{"type": "equity", "underlying": "FDXF", "amount": 50}]),
        _c("O:FDX261016P00195000", 195.0, "2026-10-16", typ="put", shares=150),
    ]
    df = mol.standard_frame(rows, "FDX")
    assert df["contract_ticker"].tolist() == ["O:FDX261016C00195000"]
    assert df["put_call"].tolist() == ["CALL"]


def test_live_on_excludes_expired():
    df = mol.standard_frame([_c("O:A260918C00100000", 100.0, "2026-09-18"),
                             _c("O:A261016C00100000", 100.0, "2026-10-16")], "A")
    assert mol.live_on(df, "2026-10-01")["expiration_date"].tolist() == ["2026-10-16"]


def test_first_day_is_initial_then_added_and_removed():
    d1 = mol.standard_frame([_c("O:A261016C00100000", 100.0, "2026-10-16")], "A")
    d2 = mol.standard_frame([_c("O:A261016C00110000", 110.0, "2026-10-16")], "A")
    first = mol.diff_contracts(None, d1, "A", "2026-10-01")
    assert first["change"].tolist() == ["initial"]
    nxt = mol.diff_contracts(d1, d2, "A", "2026-10-02").sort_values("change")
    assert nxt[["contract_ticker", "change"]].values.tolist() == [
        ["O:A261016C00110000", "added"], ["O:A261016C00100000", "removed"]]
    assert list(nxt.columns) == mol.CHANGE_COLS


def test_expiry_shows_as_removed_not_as_a_listing():
    d1 = mol.standard_frame([_c("O:A261002C00100000", 100.0, "2026-10-02"),
                             _c("O:A261016C00100000", 100.0, "2026-10-16")], "A")
    d2 = mol.live_on(d1, "2026-10-05")  # next trading day after the 10-02 expiry
    out = mol.diff_contracts(d1, d2, "A", "2026-10-05")
    assert out[["contract_ticker", "change"]].values.tolist() == [
        ["O:A261002C00100000", "removed"]]


def test_summary_and_missing_status():
    d = mol.standard_frame([_c("O:A261016C00100000", 100.0, "2026-10-16"),
                            _c("O:A261120P00080000", 80.0, "2026-11-20", typ="put")], "A")
    s = mol.summarize(d, "A", "2026-10-01", "ok")
    assert (s["n_contracts"], s["n_expirations"], s["min_strike"], s["max_strike"]) == (2, 2, 80.0, 100.0)
    empty = mol.summarize(d.iloc[0:0], "A", "2026-10-02", "missing")
    assert empty["status"] == "missing" and empty["n_contracts"] == 0
    assert empty["min_strike"] is None


def test_replay_reconstructs_live_set():
    d1 = mol.standard_frame([_c("O:A261016C00100000", 100.0, "2026-10-16")], "A")
    d2 = mol.standard_frame([_c("O:A261016C00100000", 100.0, "2026-10-16"),
                             _c("O:A261016C00110000", 110.0, "2026-10-16")], "A")
    d3 = mol.standard_frame([_c("O:A261016C00110000", 110.0, "2026-10-16")], "A")
    ch = pd.concat([mol.diff_contracts(None, d1, "A", "2026-10-01"),
                    mol.diff_contracts(d1, d2, "A", "2026-10-02"),
                    mol.diff_contracts(d2, d3, "A", "2026-10-05")])
    assert mol.replay_live_set(ch, "2026-10-02") == {"O:A261016C00100000", "O:A261016C00110000"}
    assert mol.replay_live_set(ch, "2026-10-05") == {"O:A261016C00110000"}
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_massive_option_listings.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'massive_option_listings_pipeline'`.

- [ ] **Step 3: Implement**

```python
#!/usr/bin/env python3
"""
Massive Option Listings Pipeline:
  Daily history of which option contracts were LISTED for each symbol, from
  Massive's (formerly Polygon) contracts endpoint with `as_of`. Input to the
  strike-introduction study (docs/superpowers/specs/2026-10-01-strike-intro-study-design.md).

  Stores only changes: each symbol's first day is the full list
  (change='initial'), then contracts added/removed vs the previous trading
  day. Standard contracts only (100 shares, root == symbol, no adjusted
  deliverables). A day that returns nothing while the previous day had
  contracts is recorded as status='missing' and skipped, not diffed.

Outputs:
  storage/raw/massive/option_listing_changes/year=YYYY/month=MM/option_listing_changes_<SYM>_<first>_<last>.parquet
  storage/raw/massive/option_chain_summary/year=YYYY/month=MM/option_chain_summary_<SYM>_<first>_<last>.parquet

Usage:
  python massive_option_listings_pipeline.py                      # advance universe to latest day
  python massive_option_listings_pipeline.py --symbols AAPL --start 2026-08-01 --end 2026-08-31 --out-root C:/tmp/trial
"""

import re

import pandas as pd

CHANGE_COLS = ["symbol", "date", "contract_ticker", "strike", "expiration_date",
               "put_call", "shares_per_contract", "change"]
_FRAME_COLS = ["contract_ticker", "strike", "expiration_date", "put_call",
               "shares_per_contract"]
_ROOT = re.compile(r"^O:([A-Z.]+?)\d{6}[CP]\d{8}$")


def standard_frame(results: list[dict], symbol: str) -> pd.DataFrame:
    """Contracts from one or more result pages, standard contracts only."""
    root = symbol.replace(".", "")
    rows = []
    for c in results:
        m = _ROOT.match(c.get("ticker", ""))
        if not m or m.group(1) != root:
            continue                      # adjusted root (FDX1) or malformed
        if c.get("shares_per_contract") != 100 or c.get("additional_underlyings"):
            continue
        rows.append({
            "contract_ticker":     c["ticker"],
            "strike":              float(c["strike_price"]),
            "expiration_date":     c["expiration_date"],
            "put_call":            c["contract_type"].upper(),
            "shares_per_contract": int(c["shares_per_contract"]),
        })
    df = pd.DataFrame(rows, columns=_FRAME_COLS)
    return df.drop_duplicates("contract_ticker").reset_index(drop=True)


def live_on(df: pd.DataFrame, as_of: str) -> pd.DataFrame:
    return df[df["expiration_date"] >= as_of].reset_index(drop=True)


def diff_contracts(prev, curr: pd.DataFrame, symbol: str, date: str) -> pd.DataFrame:
    if prev is None:
        out = curr.assign(change="initial")
    else:
        added = curr[~curr["contract_ticker"].isin(prev["contract_ticker"])]
        removed = prev[~prev["contract_ticker"].isin(curr["contract_ticker"])]
        out = pd.concat([added.assign(change="added"),
                         removed.assign(change="removed")], ignore_index=True)
    out = out.assign(symbol=symbol, date=date)
    return out[CHANGE_COLS].reset_index(drop=True)


def summarize(curr: pd.DataFrame, symbol: str, date: str, status: str) -> dict:
    empty = curr.empty
    return {
        "symbol": symbol, "date": date, "status": status,
        "n_contracts":   int(len(curr)),
        "n_expirations": int(curr["expiration_date"].nunique()),
        "min_strike":    None if empty else float(curr["strike"].min()),
        "max_strike":    None if empty else float(curr["strike"].max()),
    }


def replay_live_set(changes: pd.DataFrame, date: str) -> set[str]:
    live: set[str] = set()
    upto = changes[changes["date"] <= date].sort_values("date", kind="stable")
    for d, day in upto.groupby("date", sort=True):
        if (day["change"] == "initial").any():
            live = set()
        live |= set(day.loc[day["change"].isin(["initial", "added"]), "contract_ticker"])
        live -= set(day.loc[day["change"] == "removed", "contract_ticker"])
    return live
```

- [ ] **Step 4: Run tests**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_massive_option_listings.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add massive_option_listings_pipeline.py tests/test_massive_option_listings.py
git commit -m "Massive listings pipeline: standard-contract filter, daily diff, replay"
```

---

### Task 4: HTTP client — pacing, pagination, retries

**Files:**
- Modify: `massive_option_listings_pipeline.py` (add client + fetch)
- Test: `tests/test_massive_option_listings.py` (append)

**Interfaces:**
- Consumes: `standard_frame`, `live_on` (Task 3); `EXPIRED_QUERIES` value from Task 1's gate result.
- Produces:
  - `class MassiveClient(api_key: str, session=None, interval: float = REQUEST_INTERVAL, sleep=time.sleep)` with `.get(url: str, params: dict) -> dict`; raises `RuntimeError` after `MAX_RETRIES` failures.
  - `fetch_contracts(client: MassiveClient, symbol: str, as_of: str) -> pd.DataFrame` — standard contracts live on `as_of` (all pages, all `EXPIRED_QUERIES`).
  - Constants `BASE_URL`, `CONTRACTS_PATH`, `REQUEST_INTERVAL = 12.5`, `PAGE_LIMIT = 1000`, `MAX_RETRIES = 5`, `EXPIRED_QUERIES`.

- [ ] **Step 1: Write the failing tests (append)**

```python
class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.text = status, body, str(body)[:100]

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return self.responses.pop(0)


def _client(responses):
    s = FakeSession(responses)
    return mol.MassiveClient("KEY", session=s, interval=0, sleep=lambda x: None), s


def test_every_page_is_authenticated_and_all_pages_are_read(monkeypatch):
    monkeypatch.setattr(mol, "EXPIRED_QUERIES", (None,))
    p1 = {"results": [_c("O:A261016C00100000", 100.0, "2026-10-16")],
          "next_url": "https://api.massive.com/v3/reference/options/contracts?cursor=abc"}
    p2 = {"results": [_c("O:A261016C00110000", 110.0, "2026-10-16")]}
    client, s = _client([_Resp(200, p1), _Resp(200, p2)])
    df = mol.fetch_contracts(client, "A", "2026-10-01")
    assert len(df) == 2
    assert all(params.get("apiKey") == "KEY" for _, params in s.calls)
    assert s.calls[1][0].endswith("cursor=abc")


def test_429_backs_off_then_succeeds(monkeypatch):
    monkeypatch.setattr(mol, "EXPIRED_QUERIES", (None,))
    client, s = _client([_Resp(429, {}), _Resp(200, {"results": []})])
    assert mol.fetch_contracts(client, "A", "2026-10-01").empty
    assert len(s.calls) == 2


def test_persistent_failure_raises(monkeypatch):
    monkeypatch.setattr(mol, "EXPIRED_QUERIES", (None,))
    client, _ = _client([_Resp(500, {})] * mol.MAX_RETRIES)
    import pytest
    with pytest.raises(RuntimeError):
        mol.fetch_contracts(client, "A", "2026-10-01")


def test_both_expired_queries_are_unioned_and_filtered_to_live(monkeypatch):
    monkeypatch.setattr(mol, "EXPIRED_QUERIES", ("false", "true"))
    live = {"results": [_c("O:A261016C00100000", 100.0, "2026-10-16")]}
    gone = {"results": [_c("O:A261002C00100000", 100.0, "2026-10-02"),
                        _c("O:A260918C00100000", 100.0, "2026-09-18")]}
    client, s = _client([_Resp(200, live), _Resp(200, gone)])
    df = mol.fetch_contracts(client, "A", "2026-10-01")
    assert sorted(df["expiration_date"]) == ["2026-10-02", "2026-10-16"]
    assert [p.get("expired") for _, p in s.calls] == ["false", "true"]
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_massive_option_listings.py -q`
Expected: 4 new FAIL — `AttributeError: module ... has no attribute 'MassiveClient'`.

- [ ] **Step 3: Implement (append to the module; add `import time` and `import requests` at top)**

```python
BASE_URL = "https://api.massive.com"          # confirmed by Task 1 gate
CONTRACTS_PATH = "/v3/reference/options/contracts"
REQUEST_INTERVAL = 12.5                        # free tier: 5 requests/minute
PAGE_LIMIT = 1000
MAX_RETRIES = 5
BACKOFF_SECONDS = 60
# Set from the Task 1 gate result: (None,) if as_of alone returns contracts
# live on that date; ("false", "true") if since-expired ones need expired=true.
EXPIRED_QUERIES = (None,)


class MassiveClient:
    def __init__(self, api_key, session=None, interval=REQUEST_INTERVAL, sleep=time.sleep):
        self.api_key, self.interval, self.sleep = api_key, interval, sleep
        self.session = session or requests.Session()
        self._last = 0.0

    def get(self, url: str, params: dict) -> dict:
        params = {**params, "apiKey": self.api_key}   # next_url omits the key
        for attempt in range(1, MAX_RETRIES + 1):
            wait = self.interval - (time.monotonic() - self._last)
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.session.get(url, params=params, timeout=60)
            except requests.RequestException as e:
                print(f"  request error (attempt {attempt}): {type(e).__name__}")
                self.sleep(BACKOFF_SECONDS)
                continue
            if r.status_code == 200:
                return r.json()
            print(f"  HTTP {r.status_code} (attempt {attempt})")
            self.sleep(BACKOFF_SECONDS * attempt if r.status_code == 429 else BACKOFF_SECONDS)
        raise RuntimeError(f"Massive request failed {MAX_RETRIES}x: {url.split('?')[0]}")


def fetch_contracts(client: MassiveClient, symbol: str, as_of: str) -> pd.DataFrame:
    results = []
    for expired in EXPIRED_QUERIES:
        params = {"underlying_ticker": symbol, "as_of": as_of, "limit": PAGE_LIMIT}
        if expired is not None:
            params["expired"] = expired
        url = BASE_URL + CONTRACTS_PATH
        while url:
            body = client.get(url, params)
            results += body.get("results", [])
            url, params = body.get("next_url"), {}
    return live_on(standard_frame(results, symbol), as_of)
```

- [ ] **Step 4: Run tests**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_massive_option_listings.py -q`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add massive_option_listings_pipeline.py tests/test_massive_option_listings.py
git commit -m "Massive listings pipeline: paced client with pagination and retries"
```

---

### Task 5: Per-symbol backfill loop with checkpoint/resume, and CLI

**Files:**
- Modify: `massive_option_listings_pipeline.py` (add loop + `main`)
- Test: `tests/test_massive_option_listings.py` (append)

**Interfaces:**
- Consumes: `fetch_contracts`, `diff_contracts`, `summarize`, `MassiveClient` (Tasks 3–4); `experiments/strike_intro_universe.csv` (Task 2).
- Produces:
  - `advance_symbol(client, symbol: str, days: list[str], out_root: str, flush_every: int = FLUSH_EVERY, fetch=fetch_contracts) -> int` — processes the days after the symbol's checkpoint, returns days processed.
  - `trading_days(start: str, end: str) -> list[str]` — SPY trading dates from `prices`.
  - Output dirs `<out_root>/raw/massive/option_listing_changes`, `<out_root>/raw/massive/option_chain_summary`; state `<out_root>/state/massive_listings/<SYM>.parquet` + `<SYM>.json`.
  - CLI: `--symbols`, `--universe-file`, `--start`, `--end`, `--out-root` (default `storage`).

- [ ] **Step 1: Write the failing tests (append)**

```python
def _fake_fetch(lists):
    """lists: {date: [contract tickers]} -> fetch(client, symbol, as_of)."""
    def fetch(client, symbol, as_of):
        rows = [_c(t, float(t[-8:]) / 1000, "2027-01-15") for t in lists[as_of]]
        return mol.standard_frame(rows, symbol)
    return fetch


DAYS = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-08"]
LISTS = {
    "2026-09-01": ["O:A270115C00100000"],
    "2026-09-02": ["O:A270115C00100000", "O:A270115C00110000"],
    "2026-09-03": [],                                   # API hiccup
    "2026-09-04": ["O:A270115C00110000"],
    "2026-09-08": ["O:A270115C00110000", "O:A270115C00120000"],
}


def _read(out_root, kind):
    import glob
    files = glob.glob(f"{out_root}/raw/massive/{kind}/**/*.parquet", recursive=True)
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def test_missing_day_is_skipped_not_diffed(tmp_path):
    mol.advance_symbol(None, "A", DAYS, str(tmp_path), flush_every=2, fetch=_fake_fetch(LISTS))
    ch = _read(tmp_path, "option_listing_changes")
    assert "2026-09-03" not in set(ch["date"])
    summ = _read(tmp_path, "option_chain_summary").set_index("date")
    assert summ.loc["2026-09-03", "status"] == "missing"
    removed_0904 = ch[(ch["date"] == "2026-09-04") & (ch["change"] == "removed")]
    assert removed_0904["contract_ticker"].tolist() == ["O:A270115C00100000"]


def test_resume_after_crash_matches_uninterrupted_run(tmp_path):
    full, crash = tmp_path / "full", tmp_path / "crash"
    mol.advance_symbol(None, "A", DAYS, str(full), flush_every=2, fetch=_fake_fetch(LISTS))

    calls = {"n": 0}
    def dying(client, symbol, as_of):
        calls["n"] += 1
        if calls["n"] == 4:
            raise RuntimeError("boom")
        return _fake_fetch(LISTS)(client, symbol, as_of)
    import pytest
    with pytest.raises(RuntimeError):
        mol.advance_symbol(None, "A", DAYS, str(crash), flush_every=2, fetch=dying)
    mol.advance_symbol(None, "A", DAYS, str(crash), flush_every=2, fetch=_fake_fetch(LISTS))

    key = ["date", "contract_ticker", "change"]
    a = _read(full, "option_listing_changes").drop_duplicates(key).sort_values(key)[key]
    b = _read(crash, "option_listing_changes").drop_duplicates(key).sort_values(key)[key]
    assert a.reset_index(drop=True).equals(b.reset_index(drop=True))
    assert mol.advance_symbol(None, "A", DAYS, str(crash), flush_every=2,
                              fetch=_fake_fetch(LISTS)) == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_massive_option_listings.py -q`
Expected: 2 new FAIL — no attribute `advance_symbol`.

- [ ] **Step 3: Implement (append; add `import argparse, datetime, json, os, sys` and `from dotenv import load_dotenv`, `from storage_utils import write_partitioned` at top)**

```python
FLUSH_EVERY = 20                 # trading days per output chunk / checkpoint
HISTORY_DAYS = 725               # free tier keeps 2 years; stay inside it
UNIVERSE_FILE = os.path.join("experiments", "strike_intro_universe.csv")


def _paths(out_root: str):
    raw = os.path.join(out_root, "raw", "massive")
    return (os.path.join(raw, "option_listing_changes"),
            os.path.join(raw, "option_chain_summary"),
            os.path.join(out_root, "state", "massive_listings"))


def _load_state(state_dir: str, symbol: str):
    meta = os.path.join(state_dir, f"{symbol}.json")
    if not os.path.exists(meta):
        return None, None
    with open(meta, encoding="utf-8") as f:
        last = json.load(f)["last_date"]
    live = pd.read_parquet(os.path.join(state_dir, f"{symbol}.parquet"))
    return last, live


def _save_state(state_dir: str, symbol: str, last: str, live: pd.DataFrame):
    os.makedirs(state_dir, exist_ok=True)
    live.to_parquet(os.path.join(state_dir, f"{symbol}.parquet"), index=False)
    with open(os.path.join(state_dir, f"{symbol}.json"), "w", encoding="utf-8") as f:
        json.dump({"last_date": last}, f)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat()


def advance_symbol(client, symbol, days, out_root, flush_every=FLUSH_EVERY,
                   fetch=fetch_contracts) -> int:
    changes_dir, summary_dir, state_dir = _paths(out_root)
    last, live = _load_state(state_dir, symbol)
    todo = [d for d in days if last is None or d > last]
    changes, summaries, done = [], [], 0

    def flush(upto):
        if summaries:
            tag = f"{symbol}_{summaries[0]['date']}_{upto}"
            now = _now()
            if changes:
                write_partitioned(pd.concat(changes, ignore_index=True).assign(fetched_at=now),
                                  changes_dir, f"option_listing_changes_{tag}.parquet")
            write_partitioned(pd.DataFrame(summaries).assign(fetched_at=now),
                              summary_dir, f"option_chain_summary_{tag}.parquet")
        # state AFTER outputs: a crash in between re-processes the chunk,
        # and curated's key absorbs the duplicate rows
        _save_state(state_dir, symbol, upto, live)
        changes.clear(); summaries.clear()

    for d in todo:
        curr = fetch(client, symbol, d)
        if curr.empty and live is not None and not live.empty:
            summaries.append(summarize(curr, symbol, d, "missing"))
        else:
            ch = diff_contracts(live, curr, symbol, d)
            if not ch.empty:
                changes.append(ch)
            summaries.append(summarize(curr, symbol, d, "ok"))
            live = curr
        done += 1
        if done % flush_every == 0:
            flush(d)
    if todo and (summaries or changes):
        flush(todo[-1])
    return done


def trading_days(start: str, end: str) -> list[str]:
    import query as q
    df = q.sql(f"""SELECT DISTINCT CAST(date AS VARCHAR) AS d FROM prices
                   WHERE symbol = 'SPY' AND date BETWEEN '{start}' AND '{end}'
                   ORDER BY d""")
    return [d[:10] for d in df["d"]]


def main():
    load_dotenv()
    p = argparse.ArgumentParser(description="Massive option listings (as_of) history")
    p.add_argument("--symbols", nargs="+")
    p.add_argument("--universe-file", default=UNIVERSE_FILE)
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--out-root", default="storage")
    args = p.parse_args()

    key = os.environ.get("MASSIVE_API_KEY")
    if not key:
        sys.exit("MASSIVE_API_KEY not set in .env")
    today = datetime.date.today()
    start = args.start or (today - datetime.timedelta(days=HISTORY_DAYS)).isoformat()
    end = args.end or today.isoformat()
    symbols = args.symbols or pd.read_csv(args.universe_file)["symbol"].tolist()
    days = trading_days(start, end)
    client = MassiveClient(key)
    print(f"{len(symbols)} symbols x {len(days)} trading days ({start}..{end})")
    for i, sym in enumerate(symbols, 1):
        n = advance_symbol(client, sym, days, args.out_root)
        print(f"[{i}/{len(symbols)}] {sym}: {n} new days", flush=True)


if __name__ == "__main__":
    main()
```

Note: `trading_days` reads `prices`; if SPY's latest curated date lags `end`, the last few days are simply not processed yet — the next run picks them up.

- [ ] **Step 4: Run tests**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_massive_option_listings.py -q`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add massive_option_listings_pipeline.py tests/test_massive_option_listings.py
git commit -m "Massive listings pipeline: resumable per-symbol backfill and CLI"
```

---

### Task 6: Repo wiring

**Files:**
- Modify: `query.py` (CATALOG), `validate.py` (SCHEMAS), `curated.py` (KEYS), `run_all.py` (PipelineSpec), `scripts/daily_pipelines.ps1` (skip list), `tests/test_catalog.py` (EXPECTED_TABLES), `tests/test_pipelines.py` (PIPELINE_MODULES)
- Create dirs: `storage/raw/massive/option_listing_changes`, `storage/raw/massive/option_chain_summary`

**Interfaces:**
- Produces: tables `option_listing_changes`, `option_chain_summary` readable via `q.load()` / `q.sql()` (curated preferred) — Increment 2 consumes these.

- [ ] **Step 1: Add entries**

`query.py` CATALOG (next to the schwab entries):
```python
    "option_listing_changes":  _glob("massive/option_listing_changes/**/option_listing_changes_*.parquet"),
    "option_chain_summary":    _glob("massive/option_chain_summary/**/option_chain_summary_*.parquet"),
```

`validate.py` SCHEMAS:
```python
    "option_listing_changes": {
        "required":    ["symbol", "date", "contract_ticker", "strike", "expiration_date",
                        "put_call", "change"],
        "critical_nn": ["symbol", "date", "contract_ticker", "change"],
        "date_col":    "date",
    },
    "option_chain_summary": {
        "required":    ["symbol", "date", "status", "n_contracts"],
        "critical_nn": ["symbol", "date", "status"],
        "date_col":    "date",
    },
```

`curated.py` KEYS:
```python
    # Strike-introduction study (spec 2026-10-01): lossless daily listing diffs
    # from Massive as_of. A chunk re-processed after a crash rewrites the same
    # rows; the key collapses them.
    "option_listing_changes":          ['symbol', 'date', 'contract_ticker', 'change'],
    "option_chain_summary":            ['symbol', 'date'],
```

`run_all.py` PIPELINES (stage 1):
```python
    PipelineSpec(
        name="massive_option_listings",
        file="massive_option_listings_pipeline.py",
        desc="Massive as_of option listing history (strike-introduction study backfill)",
        stage=1,
        tables=["option_listing_changes", "option_chain_summary"],
        requires_env=["MASSIVE_API_KEY"],
        timeout=600,   # one-time ~3-week backfill runs detached; checkpointed, so a
                       # timeout here only pauses it
    ),
```

`scripts/daily_pipelines.ps1` `$skip` — add under "metered keys":
```powershell
    # one-time backfill at 5 req/min, run detached (strike-intro study)
    "massive_option_listings",
```

`tests/test_catalog.py` EXPECTED_TABLES: add `"option_listing_changes", "option_chain_summary",`.
`tests/test_pipelines.py` PIPELINE_MODULES: add `"massive_option_listings_pipeline",`.

Create the two storage dirs (`New-Item -ItemType Directory -Force`).

- [ ] **Step 2: Run the full suite**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/ -q -p no:cacheprovider`
Expected: all pass (baseline 3,374 passed + 12 new). If a catalog/wiring guard test fails, it names the missing site — fix that site.

- [ ] **Step 3: Commit**

```bash
git add query.py validate.py curated.py run_all.py scripts/daily_pipelines.ps1 tests/test_catalog.py tests/test_pipelines.py
git commit -m "Wire option_listing_changes / option_chain_summary tables"
```

---

### Task 7: Live trial and lossless check

**Files:**
- Create: `scripts/massive_lossless_check.py`

**Interfaces:**
- Consumes: `fetch_contracts`, `replay_live_set`, `MassiveClient` (Tasks 3–5).

- [ ] **Step 1: Run a live trial into a scratch root (never the real store)**

Run (detached not needed, ~3 names x ~21 days x ~3 pages x 12.5s ≈ 40 min; use `run_in_background` with a log):
`C:\ProgramData\anaconda3\python.exe -u massive_option_listings_pipeline.py --symbols AAPL KO NVDA --start 2026-08-03 --end 2026-08-31 --out-root %TEMP%/massive_trial > %TEMP%/massive_trial.log 2>&1`
Expected log: `[1/3] AAPL: 21 new days` etc.; no tracebacks.

- [ ] **Step 2: Write the lossless check**

```python
"""Lossless check: replaying stored changes must reproduce a direct as_of list."""
import glob
import os
import random
import sys

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import massive_option_listings_pipeline as mol

root = sys.argv[1]
load_dotenv()
files = glob.glob(f"{root}/raw/massive/option_listing_changes/**/*.parquet", recursive=True)
ch = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
summ = pd.concat([pd.read_parquet(f) for f in glob.glob(
    f"{root}/raw/massive/option_chain_summary/**/*.parquet", recursive=True)])
ok_days = summ[summ["status"] == "ok"]
client = mol.MassiveClient(os.environ["MASSIVE_API_KEY"])
random.seed(7)
bad = 0
for _, row in ok_days.sample(3, random_state=7).iterrows():
    sym, d = row["symbol"], row["date"]
    replayed = mol.replay_live_set(ch[ch["symbol"] == sym], d)
    direct = set(mol.fetch_contracts(client, sym, d)["contract_ticker"])
    diff = len(replayed ^ direct)
    bad += diff > 0
    print(f"{sym} {d}: replayed={len(replayed)} direct={len(direct)} mismatches={diff}")
print("LOSSLESS OK" if bad == 0 else f"LOSSLESS FAIL ({bad}/3)")
```

- [ ] **Step 3: Run it and inspect the trial data**

Run: `C:\ProgramData\anaconda3\python.exe -u scripts/massive_lossless_check.py %TEMP%/massive_trial`
Expected: `LOSSLESS OK`.
Also eyeball: AAPL summary `n_contracts` in the low thousands and stable day to day; `added` rows cluster on Thursdays/Fridays (new weeklies) and the Monday after monthly expiration; `removed` rows on the day after each expiration. If daily `added` counts are in the thousands every day, the `as_of` semantics differ from the gate result — stop and re-check Task 1.

- [ ] **Step 4: Commit**

```bash
git add scripts/massive_lossless_check.py
git commit -m "Massive listings: lossless replay check"
```

---

### Task 8: Launch the detached backfill

**Files:**
- Create: `scripts/massive_listings_backfill.ps1`

- [ ] **Step 1: Write the wrapper**

```powershell
# Detached ~3-week backfill for the strike-introduction study.
# Checkpointed per symbol: re-running this resumes where it stopped.
$repo = "C:\Users\zande\PycharmProjects\financial-data-pipeline"
$py   = "C:\ProgramData\anaconda3\python.exe"
$log  = Join-Path $repo "storage\logs\massive_listings_backfill.log"
Set-Location $repo
cmd /c "`"$py`" -u massive_option_listings_pipeline.py >> `"$log`" 2>&1"
```

- [ ] **Step 2: Launch detached**

Run (PowerShell):
`Start-Process powershell.exe -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','C:\Users\zande\PycharmProjects\financial-data-pipeline\scripts\massive_listings_backfill.ps1' -WindowStyle Hidden`

- [ ] **Step 3: Verify it is alive and progressing (after ~15 min)**

Run: `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object CommandLine -like '*massive_option_listings*' | Select-Object ProcessId, CreationDate`
and read the tail of `storage\logs\massive_listings_backfill.log`.
Expected: one process; log shows `100 symbols x ~500 trading days`, and the first `[1/100] ...` line appears after ~5 hours (one symbol ≈ 500 days × ~3 pages × 12.5 s).

- [ ] **Step 4: Commit and record**

```bash
git add scripts/massive_listings_backfill.ps1
git commit -m "Massive listings: detached backfill wrapper"
```
Note the launch date and the resume command in `work-notes/financial-data-pipeline/` session notes. After completion: run `curated.py`, then `validate.py`, then write the Increment 2 (analysis + report) plan.
