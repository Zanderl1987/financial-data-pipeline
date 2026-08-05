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
# XBRL extraction — shared between DJI and full-market modes
# ---------------------------------------------------------------------------

def extract_concept(facts_us_gaap, metric_name, candidate_concepts):
    """
    Collect fact rows across all candidate XBRL concepts, deduplicating by
    (period_end, form) so companies that switched concepts mid-history (e.g.
    NVDA moving from RevenueFromContractWithCustomer to Revenues) return a
    complete time series rather than only the first concept's data.
    """
    rows = []
    seen: set[tuple] = set()

    for concept in candidate_concepts:
        node = facts_us_gaap.get(concept)
        if not node:
            continue
        for unit_key, entries in node.get("units", {}).items():
            for e in entries:
                key = (e.get("end"), e.get("form"))
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "metric":        metric_name,
                    "concept":       concept,
                    "unit":          unit_key,
                    "value":         e.get("val"),
                    "period_end":    e.get("end"),
                    "fiscal_year":   e.get("fy"),
                    "fiscal_period": e.get("fp"),
                    "form":          e.get("form"),
                    "filed":         e.get("filed"),
                    "frame":         e.get("frame"),
                })

    return rows


def extract_company(data, symbol=""):
    """
    Extract all configured metrics from a companyfacts JSON dict.
    Returns (annual_rows, quarterly_rows) as lists of dicts.
    Works for both HTTP-fetched and ZIP-sourced data.
    """
    entity_name = data.get("entityName", symbol)
    cik = str(data.get("cik", "")).zfill(10)
    facts_us_gaap = data.get("facts", {}).get("us-gaap", {})
    if not facts_us_gaap:
        return [], []

    fetch_ts = datetime.datetime.utcnow().isoformat()
    annual, quarterly = [], []

    for metric_name, candidates in CONCEPTS.items():
        for row in extract_concept(facts_us_gaap, metric_name, candidates):
            enriched = {
                **row,
                "symbol":      symbol,
                "entity_name": entity_name,
                "cik":         cik,
                "fetched_at":  fetch_ts,
            }
            form = enriched.get("form", "")
            if form == "10-K":
                annual.append(enriched)
            elif form == "10-Q":
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
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
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
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
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
# Hugging Face Hub helpers
# ---------------------------------------------------------------------------

def hf_push(local_path, repo_id, filename_in_repo):
    """Upload a parquet file to a Hugging Face dataset repo (creates repo if needed)."""
    if not HF_TOKEN:
        print("  HF_TOKEN not set — skipping upload. Add it to .env to enable.")
        return
    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("  huggingface_hub not installed. Run: pip install huggingface_hub")
        return

    api = HfApi()
    try:
        api.create_repo(repo_id=repo_id, repo_type="dataset", exist_ok=True, token=HF_TOKEN)
        print(f"  Uploading {os.path.basename(local_path)} -> {repo_id}/{filename_in_repo} ...")
        api.upload_file(
            path_or_fileobj=local_path,
            path_in_repo=filename_in_repo,
            repo_id=repo_id,
            repo_type="dataset",
            token=HF_TOKEN,
        )
        print(f"  -> https://huggingface.co/datasets/{repo_id}")
    except Exception as e:
        print(f"  HF upload failed: {e}")


def hf_pull(repo_id, filename_in_repo, dest_dir):
    """
    Download a file from HF Hub into dest_dir. Returns local path or None.
    Uses HF's built-in cache — won't re-download if the file hasn't changed.
    """
    if not HF_TOKEN or not repo_id:
        return None
    try:
        from huggingface_hub import hf_hub_download
        path = hf_hub_download(
            repo_id=repo_id,
            filename=filename_in_repo,
            repo_type="dataset",
            token=HF_TOKEN,
            local_dir=dest_dir,
            local_dir_use_symlinks=False,
        )
        return path
    except Exception:
        return None


def hf_append(fresh_path, repo_id, filename_in_repo):
    """
    Merge a fresh parquet snapshot into the existing file on HF and push the union.

    Preserves the full accumulated history on HF (including restatement/filed
    versions); collapses rows that are identical on every column EXCEPT
    fetched_at, keeping the newest fetch. This is the default HF push path for
    both full-market and DJI modes.
    """
    if not HF_TOKEN:
        print("  HF_TOKEN not set — skipping upload. Add it to .env to enable.")
        return
    try:
        from huggingface_hub import HfApi, hf_hub_download
    except ImportError:
        print("  huggingface_hub not installed. Run: pip install huggingface_hub")
        return

    api = HfApi()
    try:
        api.create_repo(repo_id=repo_id, repo_type="dataset", exist_ok=True, token=HF_TOKEN)

        # Pull the current file from HF (force, so we merge with the live state).
        existing_path = None
        try:
            existing_path = hf_hub_download(
                repo_id=repo_id,
                filename=filename_in_repo,
                repo_type="dataset",
                token=HF_TOKEN,
                force_download=True,
            )
        except Exception:
            existing_path = None

        fresh = pd.read_parquet(fresh_path)
        for col in ("period_end", "fetched_at"):
            if col in fresh.columns:
                fresh[col] = pd.to_datetime(fresh[col], errors="coerce")

        if existing_path and os.path.exists(existing_path):
            old = pd.read_parquet(existing_path)
            for col in ("period_end", "fetched_at"):
                if col in old.columns:
                    old[col] = pd.to_datetime(old[col], errors="coerce")
            merged = pd.concat([old, fresh], ignore_index=True)
            dupes = len(merged)
            if "fetched_at" in merged.columns:
                dedup_cols = [c for c in merged.columns if c != "fetched_at"]
                merged = (
                    merged.sort_values("fetched_at", na_position="first")
                          .drop_duplicates(subset=dedup_cols, keep="last")
                )
            else:
                merged = merged.drop_duplicates(keep="last")
            print(f"  Merged {os.path.basename(fresh_path)} with existing {filename_in_repo}: "
                  f"{len(old):,} + {len(fresh):,} -> {len(merged):,} rows "
                  f"({dupes - len(merged):,} duplicates collapsed to newest fetch)")
        else:
            merged = fresh
            print(f"  No existing {filename_in_repo} on HF — pushing fresh snapshot "
                  f"({len(merged):,} rows)")

        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".parquet")
        os.close(tmp_fd)
        merged.to_parquet(tmp_path, index=False)
        print(f"  Uploading merged {filename_in_repo} -> {repo_id} ...")
        api.upload_file(
            path_or_fileobj=tmp_path,
            path_in_repo=filename_in_repo,
            repo_id=repo_id,
            repo_type="dataset",
            token=HF_TOKEN,
        )
        print(f"  -> https://huggingface.co/datasets/{repo_id}")
        os.unlink(tmp_path)
    except Exception as e:
        print(f"  HF append upload failed: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(n_quarters=8, refresh_cik=False, full_market=False, hf_repo=None, use_hf_cache=True):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    today = datetime.datetime.utcnow().strftime("%Y%m%d")
    repo_id = hf_repo or HF_DATASET_REPO

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

        # Check HF Hub first — skip the 7 GB download if fresh data is already there
        if use_hf_cache and repo_id:
            print(f"Checking HF Hub for cached data ({repo_id})...")
            a_path = hf_pull(repo_id, "fundamentals_annual_latest.parquet", OUTPUT_DIR)
            q_path = hf_pull(repo_id, "fundamentals_quarterly_latest.parquet", OUTPUT_DIR)
            if a_path and q_path:
                a_rows = pq.read_metadata(a_path).num_rows
                q_rows = pq.read_metadata(q_path).num_rows
                print(f"  Cache hit — using HF Hub data.")
                print(f"  Annual:    {a_path} ({a_rows:,} rows)")
                print(f"  Quarterly: {q_path} ({q_rows:,} rows)")
                print("  Run with --no-cache to force a fresh download and reprocess.")
                return

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

        # Push to Hugging Face Hub — append (merge + dedup) is always the behavior,
        # so the full accumulated history on HF survives every refresh.
        if repo_id:
            print("\nUploading to Hugging Face Hub (append mode)...")
            if os.path.exists(annual_out):
                hf_append(annual_out, repo_id, "fundamentals_annual_latest.parquet")
            if os.path.exists(quarterly_out):
                hf_append(quarterly_out, repo_id, "fundamentals_quarterly_latest.parquet")

        print("\n--- COMPLETE ---")
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
        if repo_id:
            hf_append(path, repo_id, "fundamentals_annual_latest.parquet")

    if quarterly_frames:
        quarterly_df = pd.concat(quarterly_frames, ignore_index=True)
        path = write_partitioned(quarterly_df, QUARTERLY_DIR, f"fundamentals_quarterly_{today}.parquet")
        print(f"Quarterly -> {path} ({len(quarterly_df)} rows, {quarterly_df['symbol'].nunique()} companies)")
        if repo_id:
            hf_append(path, repo_id, "fundamentals_quarterly_latest.parquet")

    if failed:
        print(f"\nFailed/skipped ({len(failed)}): {', '.join(failed)}")

    print("\n--- COMPLETE ---")


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
            "Checks HF Hub cache first. Requires HF_TOKEN + HF_DATASET_REPO in .env."
        ),
    )
    parser.add_argument(
        "--hf-repo", type=str, default=None,
        help="Hugging Face dataset repo ID (e.g. username/financial-fundamentals). "
             "Falls back to HF_DATASET_REPO env var.",
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Force re-download from EDGAR even if HF Hub cache exists.",
    )
    args = parser.parse_args()
    main(
        n_quarters=args.quarters,
        refresh_cik=args.refresh_cik,
        full_market=args.full_market,
        hf_repo=args.hf_repo,
        use_hf_cache=not args.no_cache,
    )
