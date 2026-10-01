"""Step 0 gate for the strike-introduction study (spec 2026-10-01).

Answers, for AAPL: does `as_of` history include contracts that have since
expired? Prints counts only -- never the API key. Paced for the free tier
(5 requests/minute).
"""
import datetime as dt
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()
KEY = os.environ.get("MASSIVE_API_KEY")
if not KEY:
    sys.exit("MASSIVE_API_KEY missing from .env")

PATH = "/v3/reference/options/contracts"
INTERVAL = 12.5
_last = [0.0]


def _get(url, params):
    for attempt in range(1, 6):
        wait = INTERVAL - (time.monotonic() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()
        r = requests.get(url, params=params, timeout=60)
        if r.status_code != 429:
            return r
        print(f"    429, backing off (attempt {attempt})", flush=True)
        time.sleep(60 * attempt)
    return r


def count(base, as_of, expired=None):
    params = {"underlying_ticker": "AAPL", "as_of": as_of, "limit": 1000, "apiKey": KEY}
    if expired is not None:
        params["expired"] = expired
        # only contracts still live on as_of -- without this, expired=true walks
        # every AAPL contract ever listed (hundreds of pages at 5 req/min)
        params["expiration_date.gte"] = as_of
    n, live, url, exps, pages = 0, 0, base + PATH, set(), 0
    while url:
        r = _get(url, params)
        if r.status_code != 200:
            return f"HTTP {r.status_code}: {r.text[:160]}"
        body = r.json()
        pages += 1
        for c in body.get("results", []):
            n += 1
            exps.add(c["expiration_date"])
            live += c["expiration_date"] >= as_of
        url, params = body.get("next_url"), {"apiKey": KEY}
    return (f"contracts={n} live_on_date={live} expirations={len(exps)} pages={pages} "
            f"first_exp={min(exps) if exps else None} last_exp={max(exps) if exps else None}")


base = sys.argv[1] if len(sys.argv) > 1 else "https://api.massive.com"
print("BASE", base, flush=True)
for days_back in (540, 180):
    d = (dt.date.today() - dt.timedelta(days=days_back)).isoformat()
    print(f"  as_of={d} default      :", count(base, d), flush=True)
    print(f"  as_of={d} expired=true :", count(base, d, "true"), flush=True)
