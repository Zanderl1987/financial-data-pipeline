import requests
import pandas as pd
import datetime
import time
import os
import json
import glob
import zipfile
import tempfile
import argparse
import pyarrow.parquet as pq
from dotenv import load_dotenv
from storage_utils import write_partitioned

load_dotenv()

USER_AGENT = os.environ.get("EDGAR_USER_AGENT", "FinancialDataPipeline zander.s.luke@gmail.com")
HF_TOKEN = os.environ.get("HF_TOKEN")
HF_DATASET_REPO = os.environ.get("HF_DATASET_REPO", "")

EDGAR_BASE = "https://data.sec.gov"
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_ZIP_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"

OUTPUT_DIR          = os.path.join("storage", "raw", "fundamentals")
ANNUAL_DIR          = os.path.join(OUTPUT_DIR, "annual")
QUARTERLY_DIR       = os.path.join(OUTPUT_DIR, "quarterly")
CIK_CACHE_PATH      = os.path.join(OUTPUT_DIR, "cik_map.json")

# Safe target: 8 req/sec (hard limit is 10 req/sec; violations trigger ~10 min IP block)
REQUEST_INTERVAL = 0.125
MAX_RETRIES = 3
BACKOFF_SECONDS = 30

# Concepts to extract. Candidate XBRL names are tried in order —
# companies use different names for the same metric.
CONCEPTS = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        # Banks: net interest income + non-interest income rolled up into one line
        "RevenuesNetOfInterestExpense",
        # Fallback for banks that only file interest income separately
        "InterestAndDividendIncomeOperating",
    ],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "eps_diluted":         ["EarningsPerShareDiluted"],
    "eps_basic":           ["EarningsPerShareBasic"],
    "gross_profit":        ["GrossProfit"],
    "operating_income": [
        "OperatingIncomeLoss",
        # Banks don't file OperatingIncomeLoss; pre-tax income is the closest proxy
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ],
    "total_assets":        ["Assets"],
    "total_liabilities":   ["Liabilities"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "shares_outstanding":  ["CommonStockSharesOutstanding", "CommonStockSharesIssued"],
}

# IFRS (ifrs-full) candidates for foreign private issuers / Canadian banks that
# file under IFRS instead of US GAAP. Some tags (ProfitLoss, GrossProfit, Assets,
# Liabilities) are shared by both taxonomies; the rest are IFRS-specific. EPS has
# NO ifrs-full tag, so IFRS filers get 8 of the 10 metrics (all except EPS).
# Verified live 2026-08-05 against TSM / RY (334/298 ifrs-full tags each).
IFRS_CONCEPTS = {
    "revenue": [
        "Revenue",
        "RevenueFromContractsWithCustomers",
    ],
    "net_income": ["ProfitLoss"],
    "eps_diluted": [],
    "eps_basic":   [],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["ProfitLossFromOperatingActivities"],
    "total_assets":      ["Assets"],
    "total_liabilities": ["Liabilities"],
    "operating_cash_flow": ["CashFlowsFromUsedInOperatingActivities"],
    "shares_outstanding":  ["NumberOfSharesIssuedAndFullyPaid"],
}

# Taxonomies to read from companyfacts (in priority order per company).
TAXONOMIES = ["us-gaap", "ifrs-full"]

# Filing forms whose tagged facts are real periodic financial data. Forms not in
# these sets are dropped (e.g. S-1/S-1/A carry no facts at all). The annual
# bucket holds full-year statements; the quarterly bucket holds interim
# statements. 6-K (foreign interim reports) and 8-K (usually a tagged copy of an
# interim press release) land in the quarterly bucket; 8-K facts are mostly
# one-offs and are kept for completeness rather than value.
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F"}
QUARTERLY_FORMS = {"10-Q", "10-Q/A", "6-K", "8-K", "8-K/A"}

HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def get_with_backoff(url, stream=False, timeout=30):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, stream=stream, timeout=timeout)
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
# CIK mapping + DJI symbol list (used in DJI mode)
# ---------------------------------------------------------------------------

# When multiple tickers share a CIK, the auto-picker may land on a
# non-primary class (e.g. GOOGN instead of GOOGL). Override here.
CANONICAL_TICKER_OVERRIDES = {
    "0001652044": "GOOGL",  # Alphabet: Class A (voting) over GOOG/GOOGM/GOOGN
}


def _ticker_rank(ticker: str) -> tuple:
    """Lower rank = more preferred when multiple tickers share a CIK."""
    has_hyphen = "-" in ticker
    # Warrants (W), rights (R), units (U), non-voting class suffixes (M, N)
    has_class_suffix = ticker[-1] in ("W", "R", "U", "M", "N") and len(ticker) > 2
    return (has_hyphen, has_class_suffix, len(ticker))


def build_cik_to_ticker(cik_map: dict) -> dict:
    """Reverse ticker->CIK map, picking the most canonical ticker per CIK."""
    from collections import defaultdict
    cik_to_tickers: dict[str, list] = defaultdict(list)
    for ticker, cik in cik_map.items():
        cik_to_tickers[cik].append(ticker)
    result = {
        cik: min(tickers, key=_ticker_rank)
        for cik, tickers in cik_to_tickers.items()
    }
    result.update(CANONICAL_TICKER_OVERRIDES)
    return result


def load_cik_map(force_refresh=False):
    if not force_refresh and os.path.exists(CIK_CACHE_PATH):
        with open(CIK_CACHE_PATH) as f:
            cached = json.load(f)
        print(f"Loaded CIK map from cache ({len(cached)} tickers).")
        return cached

    print("Fetching ticker->CIK map from EDGAR...")
    r = get_with_backoff(COMPANY_TICKERS_URL)
    if not r:
        raise RuntimeError("Failed to fetch company_tickers.json from EDGAR.")

    raw = r.json()
    cik_map = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in raw.values()}
    os.makedirs(os.path.dirname(CIK_CACHE_PATH), exist_ok=True)
    with open(CIK_CACHE_PATH, "w") as f:
        json.dump(cik_map, f)
    print(f"Fetched and cached {len(cik_map)} tickers.")
    return cik_map


def get_dji_symbols():
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
                print(f"Fetched {len(symbols)} DJI symbols from Wikipedia.")
                return symbols
        raise ValueError("no components table with a Symbol/Ticker column found")
    except Exception as e:
        print(f"Wikipedia fetch failed ({e}). Using fallback list.")
        return [
            "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS",
            "GS", "HD", "HON", "IBM", "JNJ", "JPM", "KO", "MCD", "MMM", "MRK",
            "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH", "V", "VZ", "WMT",
        ]


# ---------------------------------------------------------------------------
# XBRL extraction — shared between DJI and full-market modes
# ---------------------------------------------------------------------------

# Income-statement and cash-flow facts are filed BOTH as the discrete period and
# as year-to-date under the SAME period_end (e.g. a Q3 10-Q carries both the
# 3-month Jul-Sep value and the 9-month Jan-Sep value, each ending Sep 30). The
# companyfacts feed does not flag which is which, so we select by duration:
#   10-Q -> keep the ~3-month discrete quarter (drop 6-/9-month YTD)
#   10-K -> keep the ~12-month fiscal year     (drop any stray quarterly period)
# Instant facts (balance sheet: assets, liabilities, shares) have no start date
# and are always kept. Other forms (20-F, 6-K, 8-K, amendments) are left as-is.
_QUARTER_DAYS = (80, 100)     # ~3 months, tolerant of 13-/14-week fiscal quarters
_ANNUAL_DAYS  = (340, 380)    # ~12 months, tolerant of 52-/53-week fiscal years


def _period_days(start, end):
    """Length in days (end - start) of a fact's period, or None for instant facts."""
    if not start or not end:
        return None
    try:
        return (datetime.date.fromisoformat(end) - datetime.date.fromisoformat(start)).days
    except (ValueError, TypeError):
        return None


def _duration_matches_form(form, days):
    """Keep instant facts always; keep duration facts only when their length matches
    the period implied by the form (a quarter for 10-Q, a fiscal year for 10-K)."""
    if days is None:
        return True
    if form == "10-Q":
        return _QUARTER_DAYS[0] <= days <= _QUARTER_DAYS[1]
    if form == "10-K":
        return _ANNUAL_DAYS[0] <= days <= _ANNUAL_DAYS[1]
    return True


def extract_concept(facts, metric_name, candidate_concepts, taxonomy):
    """
    Collect fact rows across all candidate XBRL concepts, deduplicating by
    (taxonomy, start, end, form, accession) so companies that switched concepts
    mid-history (e.g. NVDA moving from RevenueFromContractWithCustomer to
    Revenues) return a complete time series rather than only the first concept's
    data, while restatements filed under a new accession number are preserved.

    Flow metrics are filed as both the discrete period and a year-to-date
    cumulative under the same period_end; only the fact whose duration matches
    the form is kept, so quarterly values are true discrete quarters (~3 months)
    rather than YTD cumulatives. See _duration_matches_form.
    """
    rows = []
    seen: set[tuple] = set()

    for concept in candidate_concepts:
        node = facts.get(concept)
        if not node:
            continue
        for unit_key, entries in node.get("units", {}).items():
            for e in entries:
                form = e.get("form")
                days = _period_days(e.get("start"), e.get("end"))
                if not _duration_matches_form(form, days):
                    continue
                key = (taxonomy, e.get("start"), e.get("end"), form, e.get("accn"))
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "metric":        metric_name,
                    "concept":       concept,
                    "unit":          unit_key,
                    "value":         e.get("val"),
                    "period_end":    e.get("end"),
                    "start_date":    e.get("start"),
                    "accession_number": e.get("accn"),
                    "fiscal_year":   e.get("fy"),
                    "fiscal_period": e.get("fp"),
                    "form":          form,
                    "filed":         e.get("filed"),
                    "frame":         e.get("frame"),
                    "taxonomy":      taxonomy,
                })

    return rows


def extract_company(data, symbol=""):
    """
    Extract all configured metrics from a companyfacts JSON dict.
    Returns (annual_rows, quarterly_rows) as lists of dicts.
    Works for both HTTP-fetched and ZIP-sourced data.

    Reads every taxonomy present (us-gaap for US domestic filers, ifrs-full for
    foreign private issuers), tags each row with its taxonomy, and keeps only
    the periodic forms in ANNUAL_FORMS / QUARTERLY_FORMS.
    """
    entity_name = data.get("entityName", symbol)
    cik = str(data.get("cik", "")).zfill(10)
    facts = data.get("facts", {})
    if not facts:
        return [], []

    fetch_ts = datetime.datetime.utcnow().isoformat()
    annual, quarterly = [], []

    for taxonomy in TAXONOMIES:
        taxonomy_facts = facts.get(taxonomy, {})
        if not taxonomy_facts:
            continue
        concept_map = IFRS_CONCEPTS if taxonomy == "ifrs-full" else CONCEPTS
        for metric_name, candidates in concept_map.items():
            if not candidates:
                continue
            for row in extract_concept(taxonomy_facts, metric_name, candidates, taxonomy):
                enriched = {
                    **row,
                    "symbol":      symbol,
                    "entity_name": entity_name,
                    "cik":         cik,
                    "fetched_at":  fetch_ts,
                }
                form = enriched.get("form", "")
                if form in ANNUAL_FORMS:
                    annual.append(enriched)
                elif form in QUARTERLY_FORMS:
                    quarterly.append(enriched)

    return annual, quarterly


# ---------------------------------------------------------------------------
# DJI mode — per-company HTTP requests
# ---------------------------------------------------------------------------

def fetch_company_facts(cik_padded):
    url = f"{EDGAR_BASE}/api/xbrl/companyfacts/CIK{cik_padded}.json"
    r = get_with_backoff(url)
    return r.json() if r else None


def process_company_dji(symbol, cik_padded, n_quarters=8):
    data = fetch_company_facts(cik_padded)
    if not data:
        return None, None

    annual_rows, quarterly_rows = extract_company(data, symbol=symbol)
    if not annual_rows and not quarterly_rows:
        print(f"  No XBRL data for {symbol}.")
        return None, None

    def to_df(rows):
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df["period_end"] = pd.to_datetime(df["period_end"], errors="coerce")
        df["start_date"] = pd.to_datetime(df["start_date"], errors="coerce")
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df["duration_days"] = (df["period_end"] - df["start_date"]).dt.days
        return df

    annual_df = to_df(annual_rows)
    quarterly_df = to_df(quarterly_rows)

    if quarterly_df is not None and n_quarters:
        quarterly_df = (
            quarterly_df.sort_values("period_end", ascending=False)
            .groupby("metric")
            .head(n_quarters)
        )

    return annual_df, quarterly_df


# ---------------------------------------------------------------------------
# Full-market mode — stream companyfacts.zip
# ---------------------------------------------------------------------------

def download_companyfacts_zip():
    """Stream companyfacts.zip to a temp file. Reuses existing temp if already downloaded."""
    tmp_path = os.path.join(tempfile.gettempdir(), "companyfacts_edgar.zip")

    if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 100 * 1024 * 1024:
        print(f"Reusing existing temp file ({os.path.getsize(tmp_path) / 1024 / 1024:,.0f} MB): {tmp_path}")
        return tmp_path

    print("Downloading companyfacts.zip from EDGAR (~1 GB, ~15,000 companies)...")
    print("  This may take 1-5 minutes depending on your connection speed.")

    r = get_with_backoff(COMPANYFACTS_ZIP_URL, stream=True, timeout=600)
    if not r:
        raise RuntimeError("Failed to initiate download of companyfacts.zip.")

    total_bytes = int(r.headers.get("content-length", 0))
    downloaded = 0
    chunk_size = 4 * 1024 * 1024  # 4 MB chunks

    with open(tmp_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=chunk_size):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total_bytes:
                    pct = downloaded / total_bytes * 100
                    mb = downloaded / 1024 / 1024
                    total_mb = total_bytes / 1024 / 1024
                    print(f"  {mb:,.0f} / {total_mb:,.0f} MB  ({pct:.1f}%)  ", end="\r")

    print(f"\n  Download complete: {downloaded / 1024 / 1024:,.0f} MB -> {tmp_path}")
    return tmp_path


def stream_zip_to_parquet(zip_path, annual_out, quarterly_out, batch_size=1000, cik_to_ticker=None):
    """
    Stream all company JSON files from the ZIP, write in batches to keep
    memory bounded (~batch_size companies in RAM at a time), then merge
    batch parquets with pyarrow (one batch at a time — no full-load concat).
    cik_to_ticker: optional dict {zero-padded-cik: ticker} for symbol resolution.
    """
    cik_to_ticker = cik_to_ticker or {}

    with tempfile.TemporaryDirectory() as tmpdir:
        batch_annual, batch_quarterly = [], []
        batch_num = 0
        failed = 0

        with zipfile.ZipFile(zip_path, "r") as zf:
            entries = sorted(n for n in zf.namelist() if n.endswith(".json"))
            total = len(entries)
            print(f"  {total:,} company files in ZIP")

            for i, name in enumerate(entries, 1):
                try:
                    with zf.open(name) as f:
                        data = json.load(f)
                    cik_padded = str(data.get("cik", "")).zfill(10)
                    ticker = cik_to_ticker.get(cik_padded, "")
                    a_rows, q_rows = extract_company(data, symbol=ticker)
                    batch_annual.extend(a_rows)
                    batch_quarterly.extend(q_rows)
                except Exception:
                    failed += 1

                if i % batch_size == 0 or i == total:
                    batch_num += 1
                    _flush_batch(batch_annual, tmpdir, f"annual_{batch_num:05d}.parquet")
                    _flush_batch(batch_quarterly, tmpdir, f"quarterly_{batch_num:05d}.parquet")
                    batch_annual.clear()
                    batch_quarterly.clear()
                    print(f"  [{i:,}/{total:,}] {batch_num} batches written    ", end="\r")

        print(f"\n  Processed {total - failed:,}/{total:,} companies. Failed: {failed:,}")
        print("  Merging batch files...")

        annual_parts = sorted(glob.glob(os.path.join(tmpdir, "annual_*.parquet")))
        quarterly_parts = sorted(glob.glob(os.path.join(tmpdir, "quarterly_*.parquet")))

        row_counts = {}
        row_counts["annual"] = _merge_parquets(annual_parts, annual_out)
        row_counts["quarterly"] = _merge_parquets(quarterly_parts, quarterly_out)

    return row_counts


def _flush_batch(rows, tmpdir, filename):
    if not rows:
        return
    df = pd.DataFrame(rows)
    df["period_end"] = pd.to_datetime(df["period_end"], errors="coerce")
    df["start_date"] = pd.to_datetime(df["start_date"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["duration_days"] = (df["period_end"] - df["start_date"]).dt.days
    df.to_parquet(os.path.join(tmpdir, filename), index=False, compression="snappy")


def _merge_parquets(parts, output_path):
    """Merge sorted parquet part files into one file via pyarrow (streaming, not in-memory concat)."""
    if not parts:
        return 0

    first = pq.read_table(parts[0])
    schema = first.schema
    total_rows = 0

    with pq.ParquetWriter(output_path, schema, compression="snappy") as writer:
        writer.write_table(first)
        total_rows += len(first)
        del first

        for path in parts[1:]:
            table = pq.read_table(path)
            writer.write_table(table.cast(schema))
            total_rows += len(table)
            del table

    return total_rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(n_quarters=8, refresh_cik=False, full_market=False):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    today = datetime.datetime.utcnow().strftime("%Y%m%d")

    # ------------------------------------------------------------------ #
    # FULL-MARKET MODE                                                     #
    # ------------------------------------------------------------------ #
    if full_market:
        print("=== FULL MARKET MODE (EDGAR companyfacts.zip — ~15,000 companies) ===")

        year_tag  = datetime.datetime.utcnow().year
        month_tag = f"{datetime.datetime.utcnow().month:02d}"
        annual_partition   = os.path.join(ANNUAL_DIR,    f"year={year_tag}", f"month={month_tag}")
        quarterly_partition = os.path.join(QUARTERLY_DIR, f"year={year_tag}", f"month={month_tag}")
        os.makedirs(annual_partition,   exist_ok=True)
        os.makedirs(quarterly_partition, exist_ok=True)

        annual_out    = os.path.join(annual_partition,    f"fundamentals_full_annual_{today}.parquet")
        quarterly_out = os.path.join(quarterly_partition, f"fundamentals_full_quarterly_{today}.parquet")

        # (No HF-cache short-circuit here: the pipeline owns extraction only. The HF
        # dataset is assembled from curated by build_fundamentals_dataset.py, which
        # pushes the whole snapshot as one coherent revision. A fresh full-market
        # extract always runs.)

        # Build reverse CIK map so full-market rows get ticker symbols populated
        cik_map = load_cik_map(force_refresh=refresh_cik)
        cik_to_ticker = build_cik_to_ticker(cik_map)

        # Download and stream-process the ZIP
        zip_path = None
        try:
            zip_path = download_companyfacts_zip()
            print("\nStreaming ZIP -> parquet (batched)...")
            counts = stream_zip_to_parquet(zip_path, annual_out, quarterly_out, cik_to_ticker=cik_to_ticker)
        finally:
            if zip_path and os.path.exists(zip_path):
                os.unlink(zip_path)
                print("Cleaned up temp ZIP.")

        if os.path.exists(annual_out):
            print(f"\nAnnual    -> {annual_out} ({counts['annual']:,} rows)")
        if os.path.exists(quarterly_out):
            print(f"Quarterly -> {quarterly_out} ({counts['quarterly']:,} rows)")

        print("\n--- COMPLETE (run curated.py then build_fundamentals_dataset.py to push to HF) ---")
        return

    # ------------------------------------------------------------------ #
    # DJI MODE — 30 components via per-company EDGAR API                  #
    # ------------------------------------------------------------------ #
    print("=== DJI MODE (30 components via EDGAR API) ===")
    cik_map = load_cik_map(force_refresh=refresh_cik)
    symbols = get_dji_symbols()

    annual_frames, quarterly_frames, failed = [], [], []

    for i, symbol in enumerate(symbols, 1):
        cik = cik_map.get(symbol.upper())
        if not cik:
            print(f"[{i}/{len(symbols)}] {symbol}: no CIK found, skipping.")
            failed.append(symbol)
            continue

        print(f"[{i}/{len(symbols)}] {symbol} (CIK {cik})...")
        annual, quarterly = process_company_dji(symbol, cik, n_quarters=n_quarters)
        if annual is not None:
            annual_frames.append(annual)
        if quarterly is not None:
            quarterly_frames.append(quarterly)
        if annual is None and quarterly is None:
            failed.append(symbol)

        time.sleep(REQUEST_INTERVAL)

    if annual_frames:
        annual_df = pd.concat(annual_frames, ignore_index=True)
        path = write_partitioned(annual_df, ANNUAL_DIR, f"fundamentals_annual_{today}.parquet")
        print(f"\nAnnual   -> {path} ({len(annual_df)} rows, {annual_df['symbol'].nunique()} companies)")

    if quarterly_frames:
        quarterly_df = pd.concat(quarterly_frames, ignore_index=True)
        path = write_partitioned(quarterly_df, QUARTERLY_DIR, f"fundamentals_quarterly_{today}.parquet")
        print(f"Quarterly -> {path} ({len(quarterly_df)} rows, {quarterly_df['symbol'].nunique()} companies)")

    if failed:
        print(f"\nFailed/skipped ({len(failed)}): {', '.join(failed)}")

    print("\n--- COMPLETE (run curated.py then build_fundamentals_dataset.py to push to HF) ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SEC EDGAR fundamentals pipeline")
    parser.add_argument(
        "--quarters", type=int, default=8,
        help="Recent quarters to retain per metric in DJI mode (default: 8 = 2 years).",
    )
    parser.add_argument(
        "--refresh-cik", action="store_true",
        help="Force re-download of the ticker->CIK map even if cached.",
    )
    parser.add_argument(
        "--full-market", action="store_true",
        help=(
            "Download all ~15,000 public companies via companyfacts.zip (~1 GB download). "
            "The pipeline extracts raw data only; run curated.py then "
            "build_fundamentals_dataset.py to assemble + push the HF dataset."
        ),
    )
    args = parser.parse_args()
    main(
        n_quarters=args.quarters,
        refresh_cik=args.refresh_cik,
        full_market=args.full_market,
    )
