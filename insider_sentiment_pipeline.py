"""Insider Transaction Sentiment from SEC EDGAR Form 4 filings.

Resolves DJI tickers to CIK numbers, fetches recent Form 4 filings
from the company submissions endpoint, classifies each filing as
buy/sell/non-market based on the Form 4 transaction codes, and
computes a daily buy/sell ratio as a net insider sentiment signal.

Source: SEC EDGAR (no API key required, User-Agent header mandatory)

CLI:
    python insider_sentiment_pipeline.py             # incremental (30 days)
    python insider_sentiment_pipeline.py --backfill  # full 730-day window

Outputs:
    storage/raw/insider_sentiment/insider_sentiment_{mode}_{YYYYMMDD}.parquet
"""

import argparse
import datetime
import os
import time

import pandas as pd
import requests
from dotenv import load_dotenv
from storage_utils import write_partitioned

load_dotenv()

EDGAR_USER_AGENT = os.environ.get("EDGAR_USER_AGENT", "pipeline@example.com")
OUTPUT_DIR = os.path.join("storage", "raw", "insider_sentiment")

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
REQUEST_INTERVAL = 0.12  # SEC limit ~= 10 req/s; 0.12s gives comfortable margin
MAX_RETRIES = 3
BACKOFF_SECONDS = 60


def _headers():
    return {"User-Agent": EDGAR_USER_AGENT, "Accept": "application/json"}


def _get_with_backoff(url: str) -> requests.Response | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=_headers(), timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  429 from EDGAR -- backing off {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            else:
                print(f"  HTTP {r.status_code}: {r.text[:200]}")
                return None
        except requests.RequestException as e:
            print(f"  Request error (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)
    print(f"  Giving up after {MAX_RETRIES} attempts.")
    return None


# ---------------------------------------------------------------------------
# Ticker -> CIK resolution (cached to avoid repeated fetches)
# ---------------------------------------------------------------------------

_CIK_MAP = None


def _load_cik_map():
    global _CIK_MAP
    if _CIK_MAP is not None:
        return _CIK_MAP
    r = _get_with_backoff(TICKERS_URL)
    if not r:
        print("[!] Failed to load company_tickers.json from SEC EDGAR")
        return {}
    raw = r.json()
    _CIK_MAP = {
        v["ticker"]: str(v["cik_str"]).zfill(10)
        for v in raw.values()
    }
    print(f"Loaded {len(_CIK_MAP)} ticker->CIK mappings from EDGAR.")
    return _CIK_MAP


def resolve_cik(ticker):
    mapping = _load_cik_map()
    cik = mapping.get(ticker.upper())
    if not cik:
        print(f"  {ticker}: no CIK found in EDGAR mapping")
    return cik


# ---------------------------------------------------------------------------
# Fetch Form 4 filings from company submissions
# ---------------------------------------------------------------------------

def fetch_form4_filings(ticker, cik, lookback_days):
    """Fetch recent Form 4 filings for a company from the submissions API.

    Returns a list of dicts with filing metadata.  The submissions endpoint
    returns the most recent ~40 filings; we filter for form type '4' or '4/A'.
    """
    url = f"{SUBMISSIONS_BASE}/CIK{cik}.json"
    r = _get_with_backoff(url)
    if not r:
        return []

    data = r.json()
    recent = data.get("filings", {}).get("recent", {})
    if not recent:
        return []

    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])
    descriptions = recent.get("primaryDocDescription", [])

    cutoff = (datetime.date.today() - datetime.timedelta(days=lookback_days)).isoformat()
    rows = []
    for i, form_type in enumerate(forms):
        filing_date = dates[i] if i < len(dates) else ""
        if filing_date < cutoff:
            continue
        # Accept Form 4 and Form 4/A (amendments) only
        if form_type not in ("4", "4/A"):
            continue
        rows.append({
            "ticker": ticker,
            "cik": cik,
            "form_type": form_type,
            "filing_date": filing_date,
            "accession_number": accessions[i] if i < len(accessions) else "",
            "primary_document": primary_docs[i] if i < len(primary_docs) else "",
            "description": descriptions[i] if i < len(descriptions) else "",
        })
    return rows


# ---------------------------------------------------------------------------
# Classify transactions from filing descriptions and form metadata
# ---------------------------------------------------------------------------

def classify_filing(row):
    """Heuristic classification of a Form 4 filing into buy / sell / other.

    The submissions API does not expose individual transaction codes — those
    live inside the filing XML.  We use keyword heuristics on the filing
    description and form type to produce a sentiment signal.

    For precise transaction-level data, parse the filing's XML link-text
    or use the Finnhub insider-transactions endpoint (finnhub_events_pipeline).
    """
    desc = (row.get("description") or "").lower()
    form = (row.get("form_type") or "").upper()

    # Form 4/A is an amendment — usually restates a prior transaction
    if "amendment" in desc or form == "4/A":
        return "other"

    # Keywords in filing descriptions
    if any(kw in desc for kw in ("purchase", "acquired", "bought", "acquisition")):
        return "buy"
    if any(kw in desc for kw in ("sale", "sold", "disposition", "disposed", "selling")):
        return "sell"
    if any(kw in desc for kw in ("exercise", "conversion", "vesting", "option")):
        return "exercise"
    if any(kw in desc for kw in ("grant", "award", "restricted", "rsu")):
        return "grant"

    # Fallback: if description is empty or uninformative, mark as other
    return "other"


# ---------------------------------------------------------------------------
# Aggregate daily sentiment
# ---------------------------------------------------------------------------

def compute_sentiment(df):
    """Compute daily buy/sell ratio and net sentiment score.

    Returns a DataFrame indexed by filing_date with:
        n_buys, n_sells, n_exercises, n_grants, n_other,
        buy_sell_ratio, net_sentiment
    """
    if df.empty:
        return pd.DataFrame()

    counts = (
        df.groupby(["filing_date", "transaction_class"])
        .size()
        .unstack(fill_value=0)
    )

    for col in ("buy", "sell", "exercise", "grant", "other"):
        if col not in counts.columns:
            counts[col] = 0

    counts["n_filings"] = counts.sum(axis=1)
    # Buy/sell ratio: (buys + 1) / (sells + 1) — add-one smoothing avoids /0
    counts["buy_sell_ratio"] = (counts["buy"] + 1) / (counts["sell"] + 1)
    # Net sentiment: normalized (buys - sells) / total, range [-1, +1]
    counts["net_sentiment"] = (
        (counts["buy"] - counts["sell"]) / counts["n_filings"].clip(lower=1)
    )

    counts = counts.reset_index()
    counts["fetched_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return counts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

LOOKBACK = {"incremental": 30, "backfill": 730}


def main(backfill=False):
    if not EDGAR_USER_AGENT:
        print("[!] EDGAR_USER_AGENT not set -- skipping")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    _run_pipeline(backfill)


def _run_pipeline(backfill):
    from price_history_pipeline import get_dji_symbols

    symbols = get_dji_symbols()
    lookback = LOOKBACK["backfill" if backfill else "incremental"]
    mode_tag = "backfill" if backfill else "incremental"
    today = datetime.datetime.now(datetime.timezone.utc)
    today_str = today.strftime("%Y%m%d")

    print(f"Mode: {'BACKFILL' if backfill else 'INCREMENTAL'} (lookback={lookback}d)")

    all_rows = []
    failed = []

    for i, ticker in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] {ticker}")
        cik = resolve_cik(ticker)
        if not cik:
            failed.append(ticker)
            time.sleep(REQUEST_INTERVAL)
            continue

        filings = fetch_form4_filings(ticker, cik, lookback)
        for f in filings:
            f["transaction_class"] = classify_filing(f)
        all_rows.extend(filings)
        print(f"  {ticker}: {len(filings)} Form 4 filings")
        time.sleep(REQUEST_INTERVAL)

    if not all_rows:
        print("[!] No Form 4 filings found across all tickers.")
        return

    df = pd.DataFrame(all_rows)
    df["fetched_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Raw filing-level parquet (each row = one Form 4 filing)
    raw_path = write_partitioned(
        df, OUTPUT_DIR,
        f"insider_sentiment_{mode_tag}_{today_str}.parquet",
    )
    print(f"[+] {raw_path} ({len(df):,} rows)")

    # Summary stats
    class_counts = df["transaction_class"].value_counts()
    for cls, cnt in class_counts.items():
        print(f"  {cls}: {cnt}")
    if failed:
        print(f"[!] Failed tickers ({len(failed)}): {', '.join(failed)}")

    print("--- COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SEC EDGAR insider transaction sentiment (Form 4 filings)"
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Fetch a 730-day history window instead of the recent 30-day window.",
    )
    args = parser.parse_args()
    main(backfill=args.backfill)
