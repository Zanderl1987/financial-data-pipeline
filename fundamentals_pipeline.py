import requests
import pandas as pd
import datetime
import time
import os
import json
import argparse
from dotenv import load_dotenv

load_dotenv()

# SEC requires a User-Agent header identifying your app and contact email.
# Set EDGAR_USER_AGENT in your .env, e.g. "MyApp you@example.com"
USER_AGENT = os.environ.get("EDGAR_USER_AGENT", "FinancialDataPipeline zander.s.luke@gmail.com")

EDGAR_BASE = "https://data.sec.gov"
COMPANY_TICKERS_URL = f"{EDGAR_BASE}/files/company_tickers.json"

OUTPUT_DIR = os.path.join("storage", "raw", "fundamentals")
CIK_CACHE_PATH = os.path.join("storage", "raw", "fundamentals", "cik_map.json")

# Safe target: 8 req/sec (hard limit is 10 req/sec; violations trigger ~10min IP block)
REQUEST_INTERVAL = 0.125

MAX_RETRIES = 3
BACKOFF_SECONDS = 30

# Concepts to extract. Each entry is a list of candidate XBRL names tried in order —
# companies use different concept names for the same metric.
CONCEPTS = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "eps_basic": ["EarningsPerShareBasic"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "total_assets": ["Assets"],
    "total_liabilities": ["Liabilities"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "shares_outstanding": ["CommonStockSharesOutstanding", "CommonStockSharesIssued"],
}

HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def get_with_backoff(url):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  429 from EDGAR. Backing off {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            else:
                print(f"  HTTP {r.status_code} for {url}")
                return None
        except requests.RequestException as e:
            print(f"  Request error (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)
    print(f"  Giving up after {MAX_RETRIES} attempts: {url}")
    return None


# ---------------------------------------------------------------------------
# CIK mapping
# ---------------------------------------------------------------------------

def load_cik_map(force_refresh=False):
    """Returns {ticker: cik_padded_10digits}. Caches to disk for reuse."""
    if not force_refresh and os.path.exists(CIK_CACHE_PATH):
        with open(CIK_CACHE_PATH) as f:
            cached = json.load(f)
        print(f"Loaded CIK map from cache ({len(cached)} tickers).")
        return cached

    print("Fetching ticker→CIK map from EDGAR...")
    r = get_with_backoff(COMPANY_TICKERS_URL)
    if not r:
        raise RuntimeError("Failed to fetch company_tickers.json from EDGAR.")

    raw = r.json()
    cik_map = {
        v["ticker"].upper(): str(v["cik_str"]).zfill(10)
        for v in raw.values()
    }
    os.makedirs(os.path.dirname(CIK_CACHE_PATH), exist_ok=True)
    with open(CIK_CACHE_PATH, "w") as f:
        json.dump(cik_map, f)
    print(f"Fetched and cached {len(cik_map)} tickers.")
    return cik_map


def get_dji_symbols():
    try:
        df = pd.read_html(
            "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average",
            storage_options={"User-Agent": "Mozilla/5.0"},
        )[2]
        symbols = df["Symbol"].tolist()
        print(f"Fetched {len(symbols)} DJI symbols from Wikipedia.")
        return symbols
    except Exception as e:
        print(f"Wikipedia fetch failed ({e}). Using fallback list.")
        return [
            "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS",
            "GS", "HD", "HON", "IBM", "JNJ", "JPM", "KO", "MCD", "MMM", "MRK",
            "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH", "V", "VZ", "WMT",
        ]


# ---------------------------------------------------------------------------
# XBRL extraction
# ---------------------------------------------------------------------------

def extract_concept(facts_us_gaap, metric_name, candidate_concepts):
    """
    Try each candidate concept name in order; return a list of fact dicts
    for the first one that has data. Returns [] if none found.
    """
    for concept in candidate_concepts:
        node = facts_us_gaap.get(concept)
        if not node:
            continue
        # Units can be USD, shares, USD/shares, etc. — take the first unit bucket.
        units = node.get("units", {})
        for unit_key, entries in units.items():
            if not entries:
                continue
            rows = []
            for e in entries:
                rows.append({
                    "metric": metric_name,
                    "concept": concept,
                    "unit": unit_key,
                    "value": e.get("val"),
                    "period_end": e.get("end"),
                    "fiscal_year": e.get("fy"),
                    "fiscal_period": e.get("fp"),
                    "form": e.get("form"),
                    "filed": e.get("filed"),
                    "frame": e.get("frame"),
                })
            return rows  # Return first unit bucket with data
    return []


def fetch_company_facts(cik_padded):
    url = f"{EDGAR_BASE}/api/xbrl/companyfacts/CIK{cik_padded}.json"
    r = get_with_backoff(url)
    if not r:
        return None
    return r.json()


def process_company(symbol, cik_padded, n_quarters=8):
    """
    Fetch all XBRL facts for a company and extract the configured metrics.
    Returns (annual_rows, quarterly_rows).
    """
    data = fetch_company_facts(cik_padded)
    if not data:
        return None, None

    entity_name = data.get("entityName", symbol)
    facts_us_gaap = data.get("facts", {}).get("us-gaap", {})

    all_rows = []
    for metric_name, candidates in CONCEPTS.items():
        rows = extract_concept(facts_us_gaap, metric_name, candidates)
        all_rows.extend(rows)

    if not all_rows:
        print(f"  No XBRL data found for {symbol}.")
        return None, None

    df = pd.DataFrame(all_rows)
    df["symbol"] = symbol
    df["entity_name"] = entity_name
    df["cik"] = cik_padded
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    df["period_end"] = pd.to_datetime(df["period_end"], errors="coerce")

    # Annual: latest value per metric from 10-K filings
    annual = (
        df[df["form"] == "10-K"]
        .sort_values("period_end", ascending=False)
        .drop_duplicates(subset=["metric"])
        .copy()
    )

    # Quarterly: last n_quarters per metric from 10-Q filings
    quarterly = (
        df[df["form"] == "10-Q"]
        .sort_values("period_end", ascending=False)
        .groupby("metric")
        .head(n_quarters)
        .copy()
    )

    return annual, quarterly


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(n_quarters=8, refresh_cik=False):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    cik_map = load_cik_map(force_refresh=refresh_cik)
    symbols = get_dji_symbols()

    annual_frames = []
    quarterly_frames = []
    failed = []

    for i, symbol in enumerate(symbols, 1):
        cik = cik_map.get(symbol.upper())
        if not cik:
            print(f"[{i}/{len(symbols)}] {symbol}: no CIK found, skipping.")
            failed.append(symbol)
            continue

        print(f"[{i}/{len(symbols)}] {symbol} (CIK {cik})...")
        annual, quarterly = process_company(symbol, cik, n_quarters=n_quarters)
        if annual is not None:
            annual_frames.append(annual)
        if quarterly is not None:
            quarterly_frames.append(quarterly)
        if annual is None and quarterly is None:
            failed.append(symbol)

        time.sleep(REQUEST_INTERVAL)

    today = datetime.datetime.utcnow().strftime("%Y%m%d")

    if annual_frames:
        annual_df = pd.concat(annual_frames, ignore_index=True)
        path = os.path.join(OUTPUT_DIR, f"fundamentals_annual_{today}.parquet")
        annual_df.to_parquet(path, index=False)
        print(f"\nAnnual   → {path} ({len(annual_df)} rows, {annual_df['symbol'].nunique()} companies)")

    if quarterly_frames:
        quarterly_df = pd.concat(quarterly_frames, ignore_index=True)
        path = os.path.join(OUTPUT_DIR, f"fundamentals_quarterly_{today}.parquet")
        quarterly_df.to_parquet(path, index=False)
        print(f"Quarterly → {path} ({len(quarterly_df)} rows, {quarterly_df['symbol'].nunique()} companies)")

    if failed:
        print(f"\nFailed/skipped ({len(failed)}): {', '.join(failed)}")

    print("\n--- COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SEC EDGAR fundamentals pipeline")
    parser.add_argument(
        "--quarters", type=int, default=8,
        help="Number of recent quarters to retain per metric (default: 8 = 2 years).",
    )
    parser.add_argument(
        "--refresh-cik", action="store_true",
        help="Force re-download of the ticker→CIK map even if cached.",
    )
    args = parser.parse_args()
    main(n_quarters=args.quarters, refresh_cik=args.refresh_cik)
