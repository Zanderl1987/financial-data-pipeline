"""
SEC EDGAR Pipeline -- company filings metadata, XBRL fundamentals, and
full-text search (EFTS) for DOW 30 companies.

No API key required. Only a User-Agent header is mandatory.
Rate limit: 10 requests/second.

CLI:
  python sec_edgar_pipeline.py             # incremental (recent filings)
  python sec_edgar_pipeline.py --backfill  # full available history

Output:
  storage/raw/sec_edgar/submissions/
  storage/raw/sec_edgar/xbrl_fundamentals/
  storage/raw/sec_edgar/efts_search/
"""

import argparse
import datetime
import os
import time

import pandas as pd
import requests
from storage_utils import write_partitioned

SEC_BASE = "https://data.sec.gov"
EFTS_BASE = "https://efts.sec.gov/LATEST"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

BASE_DIR = os.path.join("storage", "raw", "sec_edgar")

HEADERS = {
    "User-Agent": "FinancialDataPipeline research@financial-data-pipeline.com"
}

REQUEST_INTERVAL = 0.12
MAX_RETRIES = 3
BACKOFF_SECONDS = 10

# Key XBRL concepts to extract per company
XBRL_CONCEPTS = [
    ("us-gaap", "Revenues"),
    ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
    ("us-gaap", "CostOfGoodsAndServicesSold"),
    ("us-gaap", "GrossProfit"),
    ("us-gaap", "OperatingIncomeLoss"),
    ("us-gaap", "NetIncomeLoss"),
    ("us-gaap", "EarningsPerShareBasic"),
    ("us-gaap", "EarningsPerShareDiluted"),
    ("us-gaap", "Assets"),
    ("us-gaap", "Liabilities"),
    ("us-gaap", "StockholdersEquity"),
    ("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
    ("us-gaap", "LongTermDebt"),
    ("us-gaap", "CommonStockSharesOutstanding"),
    ("us-gaap", "OperatingCashFlow"),
    ("us-gaap", "PaymentsForCapitalExpenditures"),
    ("us-gaap", "Dividends"),
    ("dei", "EntityName"),
    ("dei", "EntityCommonStockSharesOutstanding"),
]

# EFTS search terms for risk/event monitoring
EFTS_SEARCH_QUERIES = [
    '"material weakness"',
    '"going concern"',
    '"impairment"',
    '"restatement"',
    '"cybersecurity"',
    '"artificial intelligence"',
]


def get_dji_symbols():
    """Scrape DOW 30 tickers from Wikipedia."""
    try:
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average",
            storage_options={"User-Agent": "Mozilla/5.0"},
        )
        for df in tables:
            col = next(
                (c for c in df.columns
                 if str(c).strip().lower() in ("symbol", "ticker")),
                None,
            )
            if col is not None and 25 <= len(df) <= 35:
                symbols = (
                    df[col].astype(str).str.strip().str.upper()
                    .str.replace(r"\s+.*$", "", regex=True)
                    .tolist()
                )
                print(f"Scraped {len(symbols)} DJI symbols from Wikipedia.")
                return symbols
        raise ValueError("no components table found")
    except Exception as e:
        print(f"Wikipedia scrape failed ({e}). Using fallback.")
        return [
            "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX",
            "DIS", "DOW", "GS", "HD", "HON", "IBM", "INTC", "JNJ", "JPM",
            "KO", "MCD", "MMM", "MRK", "MSFT", "NKE", "NVDA", "PG", "SHW",
            "TRV", "UNH", "VZ", "WBA", "WMT", "XOM",
        ]


def get_cik_lookup():
    """Download ticker-to-CIK mapping from SEC."""
    print("Downloading ticker-to-CIK mapping...")
    try:
        resp = requests.get(TICKERS_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        mapping = {}
        for entry in data.values():
            ticker = entry.get("ticker", "").upper()
            cik = str(entry.get("cik_str", "")).zfill(10)
            mapping[ticker] = cik
        print(f"  Loaded {len(mapping)} ticker-CIK mappings")
        return mapping
    except Exception as e:
        print(f"  Failed to load CIK mapping: {e}")
        return {}


def get_with_backoff(url, params=None):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=30)
            if resp.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  Rate limited, waiting {wait}s (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            if attempt == MAX_RETRIES:
                print(f"  Failed after {MAX_RETRIES} attempts: {e}")
                return None
            wait = BACKOFF_SECONDS * attempt
            print(f"  Error: {e}, retrying in {wait}s (attempt {attempt}/{MAX_RETRIES})")
            time.sleep(wait)
    return None


def fetch_submissions(cik, backfill=False):
    """Fetch filing metadata for a company.

    filings.recent only holds the most recent filings that fit inline in the
    submissions JSON. Companies with long filing histories (e.g. DOW 30
    incumbents) overflow into filings.files, a list of additional per-CIK
    JSON files (named like CIK{cik}-submissions-001.json) that must be
    fetched separately from https://data.sec.gov/submissions/{name}. When
    backfill=True we paginate through all of those files and concatenate
    their records with filings.recent to return the full available history;
    otherwise we keep incremental mode cheap and only return filings.recent.
    """
    url = f"{SEC_BASE}/submissions/CIK{cik}.json"
    data = get_with_backoff(url)
    time.sleep(REQUEST_INTERVAL)
    if not data:
        return pd.DataFrame()

    entity_name = data.get("name", "")
    sic = data.get("sic", "")
    state = data.get("stateOfIncorporation", "")

    filings = data.get("filings", {})
    recent = filings.get("recent", {})

    filing_blocks = []
    if recent:
        filing_blocks.append(recent)

    if backfill:
        for file_ref in filings.get("files", []):
            file_name = file_ref.get("name")
            if not file_name:
                continue
            file_url = f"{SEC_BASE}/submissions/{file_name}"
            file_data = get_with_backoff(file_url)
            time.sleep(REQUEST_INTERVAL)
            if file_data:
                filing_blocks.append(file_data)

    if not filing_blocks:
        return pd.DataFrame()

    rows = []
    for block in filing_blocks:
        n_filings = len(block.get("form", []))
        for i in range(n_filings):
            form_type = block.get("form", [None])[i]
            filing_date = block.get("filingDate", [None])[i]
            accession = block.get("accessionNumber", [None])[i]
            primary_doc = block.get("primaryDocument", [None])[i]
            primary_type = block.get("primaryDocDescription", [None])[i]

            rows.append({
                "entity_name": entity_name,
                "cik": cik,
                "sic": sic,
                "state_of_incorporation": state,
                "form_type": form_type,
                "filing_date": filing_date,
                "accession_number": accession,
                "primary_document": primary_doc,
                "primary_doc_description": primary_type,
            })

    df = pd.DataFrame(rows)
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return df


def fetch_xbrl_fundamentals(cik, ticker):
    """Fetch key XBRL financial facts for a company."""
    url = f"{SEC_BASE}/api/xbrl/companyfacts/CIK{cik}.json"
    data = get_with_backoff(url)
    time.sleep(REQUEST_INTERVAL)
    if not data:
        return pd.DataFrame()

    entity_name = data.get("entityName", "")
    facts = data.get("facts", {})

    rows = []
    for taxonomy, concept in XBRL_CONCEPTS:
        concept_data = facts.get(taxonomy, {}).get(concept, {})
        if not concept_data:
            continue

        units = concept_data.get("units", {})
        # Priority: USD (most $ concepts) -> USD/shares (EPS concepts, which
        # SEC XBRL reports under this compound unit) -> shares (share counts)
        # -> pure (ratios). Record whichever key was actually used so the
        # "unit" field matches the values pulled, not just the first raw key.
        if "USD" in units:
            unit_key = "USD"
        elif "USD/shares" in units:
            unit_key = "USD/shares"
        elif "shares" in units:
            unit_key = "shares"
        elif "pure" in units:
            unit_key = "pure"
        else:
            unit_key = ""
        values = units.get(unit_key, [])

        for v in values:
            if v.get("form") not in ("10-K", "10-Q"):
                continue
            rows.append({
                "ticker": ticker,
                "entity_name": entity_name,
                "cik": cik,
                "taxonomy": taxonomy,
                "concept": concept,
                "value": v.get("val"),
                "unit": unit_key,
                "start_date": v.get("start", ""),
                "end_date": v.get("end", ""),
                "fiscal_year": v.get("fy"),
                "fiscal_period": v.get("fp"),
                "form_type": v.get("form"),
                "filed_date": v.get("filed"),
                "accession_number": v.get("accn"),
            })

    df = pd.DataFrame(rows)
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return df


def fetch_efts_search(query, forms="10-K,10-Q,8-K", startdt="2020-01-01"):
    """Full-text search across EDGAR filings."""
    params = {
        "q": query,
        "forms": forms,
        "startdt": startdt,
        "enddt": datetime.date.today().isoformat(),
        "from": 0,
    }
    data = get_with_backoff(f"{EFTS_BASE}/search-index", params=params)
    time.sleep(REQUEST_INTERVAL)
    if not data:
        return pd.DataFrame()

    hits = data.get("hits", {}).get("hits", [])
    total = data.get("hits", {}).get("total", {}).get("value", 0)

    rows = []
    for hit in hits:
        source = hit.get("_source", {})
        rows.append({
            "search_query": query,
            "total_results": total,
            "ciks": ",".join(source.get("ciks", [])),
            "display_names": ",".join(source.get("display_names", [])),
            "form_type": source.get("form"),
            "filing_date": source.get("file_date"),
            "accession_number": source.get("adsh"),
            "document_type": source.get("file_type"),
            "sic_codes": ",".join(source.get("sics", [])),
            "business_states": ",".join(source.get("biz_states", [])),
        })

    df = pd.DataFrame(rows)
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return df


def run_pipeline(backfill=False):
    """Run the SEC EDGAR pipeline."""
    print("=" * 60)
    print("SEC EDGAR Pipeline")
    print("=" * 60)

    cik_lookup = get_cik_lookup()
    symbols = get_dji_symbols()
    ciks = [(s, cik_lookup[s]) for s in symbols if s in cik_lookup]
    print(f"Processing {len(ciks)} DOW companies with CIK mappings")

    # 1. Company Submissions
    print("\n--- Company Filings Metadata ---")
    submissions_dir = os.path.join(BASE_DIR, "submissions")
    os.makedirs(submissions_dir, exist_ok=True)

    all_subs = []
    for ticker, cik in ciks:
        print(f"  Fetching submissions for {ticker} (CIK {cik})...")
        df = fetch_submissions(cik, backfill=backfill)
        if not df.empty:
            all_subs.append(df)
            print(f"    {len(df)} filings")
        time.sleep(REQUEST_INTERVAL)

    if all_subs:
        subs_df = pd.concat(all_subs, ignore_index=True)
        if backfill:
            today = datetime.date.today().isoformat()
            write_partitioned(subs_df, submissions_dir,
                               filename=f"sec_edgar_submissions_backfill_{today}.parquet")
            print(f"  Backfill: {len(subs_df)} total filings saved")
        else:
            today = datetime.date.today().isoformat()
            write_partitioned(subs_df, submissions_dir,
                               filename=f"sec_edgar_submissions_{today}.parquet")
            print(f"  Incremental: {len(subs_df)} filings for {today}")

    # 2. XBRL Fundamentals
    print("\n--- XBRL Fundamentals ---")
    xbrl_dir = os.path.join(BASE_DIR, "xbrl_fundamentals")
    os.makedirs(xbrl_dir, exist_ok=True)

    all_xbrl = []
    for ticker, cik in ciks:
        print(f"  Fetching XBRL data for {ticker}...")
        df = fetch_xbrl_fundamentals(cik, ticker)
        if not df.empty:
            all_xbrl.append(df)
            print(f"    {len(df)} XBRL facts")
        time.sleep(REQUEST_INTERVAL)

    if all_xbrl:
        xbrl_df = pd.concat(all_xbrl, ignore_index=True)
        if backfill:
            today = datetime.date.today().isoformat()
            write_partitioned(xbrl_df, xbrl_dir,
                               filename=f"sec_edgar_xbrl_fundamentals_backfill_{today}.parquet")
            print(f"  Backfill: {len(xbrl_df)} total XBRL facts saved")
        else:
            today = datetime.date.today().isoformat()
            write_partitioned(xbrl_df, xbrl_dir,
                               filename=f"sec_edgar_xbrl_fundamentals_{today}.parquet")
            print(f"  Incremental: {len(xbrl_df)} XBRL facts for {today}")

    # 3. EFTS Full-Text Search
    print("\n--- EFTS Full-Text Search ---")
    efts_dir = os.path.join(BASE_DIR, "efts_search")
    os.makedirs(efts_dir, exist_ok=True)

    all_search = []
    for query in EFTS_SEARCH_QUERIES:
        print(f"  Searching: {query}...")
        df = fetch_efts_search(query)
        if not df.empty:
            all_search.append(df)
            print(f"    {len(df)} results")
        time.sleep(REQUEST_INTERVAL)

    if all_search:
        search_df = pd.concat(all_search, ignore_index=True)
        today = datetime.date.today().isoformat()
        write_partitioned(search_df, efts_dir,
                           filename=f"sec_edgar_efts_search_{today}.parquet")
        print(f"  {len(search_df)} search results for {today}")

    print("\n" + "=" * 60)
    print("SEC EDGAR Pipeline complete.")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="SEC EDGAR Pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch full available history")
    args = parser.parse_args()
    run_pipeline(backfill=args.backfill)


if __name__ == "__main__":
    main()
