"""
US Treasury Fiscal Data Pipeline -- national debt, average interest rates,
Treasury auctions, exchange rates, interest expense, savings bonds,
Monthly Treasury Statement, and Daily Treasury Statement.

Uses the Treasury Fiscal Data API (completely free, no auth required).

API base: https://api.fiscaldata.treasury.gov/services/api/fiscal_service/

CLI:
  python treasury_fiscal_pipeline.py             # incremental (last 3 years)
  python treasury_fiscal_pipeline.py --backfill  # full history (1993+)

Output:
  storage/raw/treasury/debt_to_penny/
  storage/raw/treasury/avg_interest_rates/
  storage/raw/treasury/interest_expense/
  storage/raw/treasury/auctions_detail/
  (named auctions_detail, not auctions -- storage/raw/treasury/auctions/ is
  already used by the existing production treasury_pipeline.py's
  "record-setting auctions" table; this one is full individual-auction
  records from v1/accounting/od/auctions_query, a different dataset)
  storage/raw/treasury/exchange_rates/
  storage/raw/treasury/savings_bonds/
  storage/raw/treasury/mts_receipts_outlays/
  storage/raw/treasury/mts_outlays_by_agency/
  storage/raw/treasury/dts_operating_cash/
  storage/raw/treasury/mts_budget_comparison/
"""

import argparse
import datetime
import os
import time

import pandas as pd
import requests
from storage_utils import write_partitioned

FISCAL_DATA_BASE = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"

BASE_DIR = os.path.join("storage", "raw", "treasury")

REQUEST_INTERVAL = 0.25
MAX_RETRIES = 3
BACKOFF_SECONDS = 30


def get_with_backoff(url, params=None):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=60)
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
            print(f"  Error: {e}, retrying in {wait}s")
            time.sleep(wait)
    return None


def fetch_all_pages(endpoint, params=None, page_size=10000, sort=None, date_filter=None, max_pages=None):
    """Paginate through a Fiscal Data API endpoint, collecting all records."""
    if params is None:
        params = {}
    params["page[size]"] = page_size
    if sort:
        params["sort"] = sort
    if date_filter:
        params["filter"] = date_filter

    all_data = []
    page_num = 1
    while True:
        params["page[number]"] = page_num
        data = get_with_backoff(f"{FISCAL_DATA_BASE}/{endpoint}", params=params)
        if not data or "data" not in data:
            break
        records = data["data"]
        if not records:
            break
        all_data.extend(records)
        meta = data.get("meta", {}).get("pagination", data.get("meta", {}))
        total_pages = meta.get("total_pages", meta.get("total-pages", 1))
        print(f"    Page {page_num}/{total_pages} ({len(records)} records)")
        if page_num >= total_pages:
            break
        if max_pages and page_num >= max_pages:
            break
        page_num += 1
        time.sleep(REQUEST_INTERVAL)
    return all_data


def years_ago(n):
    """Return a date string for n years ago."""
    return (datetime.date.today() - datetime.timedelta(days=365 * n)).isoformat()


# ── Table 1: Debt to the Penny ────────────────────────────────────────────
def fetch_debt_to_penny(backfill=False):
    print("Fetching Debt to the Penny...")
    endpoint = "v2/accounting/od/debt_to_penny"
    start = "1993-04-01" if backfill else years_ago(3)
    date_filter = f"record_date:gte:{start}"
    records = fetch_all_pages(
        endpoint,
        sort="-record_date",
        date_filter=date_filter,
        page_size=10000,
    )
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    numeric_cols = [
        "debt_held_public_amt", "intragov_hold_amt", "record_date",
        "record_fiscal_year", "record_fiscal_quarter", "record_calendar_year",
        "record_calendar_month", "record_calendar_day",
    ]
    for col in ["debt_held_public_amt", "intragov_hold_amt", "tot_pub_debt_out_amt",
                 "src_line_nbr", "record_fiscal_year", "record_fiscal_quarter",
                 "record_calendar_year", "record_calendar_month", "record_calendar_day"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["record_date"] = pd.to_datetime(df["record_date"], errors="coerce")
    df["fetched_at"] = pd.Timestamp.utcnow().isoformat()
    print(f"  {len(df)} records (since {start})")
    return df


# ── Table 2: Average Interest Rates ───────────────────────────────────────
def fetch_avg_interest_rates(backfill=False):
    print("Fetching Average Interest Rates on Treasury Securities...")
    endpoint = "v2/accounting/od/avg_interest_rates"
    start = "1990-01-01" if backfill else years_ago(5)
    date_filter = f"record_date:gte:{start}"
    records = fetch_all_pages(
        endpoint,
        sort="-record_date",
        date_filter=date_filter,
        page_size=10000,
    )
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    for col in ["avg_interest_rate_amt", "record_fiscal_year", "record_fiscal_quarter",
                 "record_calendar_year", "record_calendar_month"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["record_date"] = pd.to_datetime(df["record_date"], errors="coerce")
    df["fetched_at"] = pd.Timestamp.utcnow().isoformat()
    print(f"  {len(df)} records (since {start})")
    return df


# ── Table 3: Interest Expense on the Debt ─────────────────────────────────
def fetch_interest_expense(backfill=False):
    print("Fetching Interest Expense on the Debt...")
    endpoint = "v2/accounting/od/interest_expense"
    start = "1990-01-01" if backfill else years_ago(5)
    date_filter = f"record_date:gte:{start}"
    records = fetch_all_pages(
        endpoint,
        sort="-record_date",
        date_filter=date_filter,
        page_size=10000,
    )
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    for col in ["month_expense_amt", "fytd_expense_amt", "record_fiscal_year", "record_fiscal_quarter",
                 "record_calendar_year", "record_calendar_month"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["record_date"] = pd.to_datetime(df["record_date"], errors="coerce")
    df["fetched_at"] = pd.Timestamp.utcnow().isoformat()
    print(f"  {len(df)} records (since {start})")
    return df


# ── Table 4: Treasury Auctions ────────────────────────────────────────────
def fetch_auctions(backfill=False):
    print("Fetching Treasury Security Auctions...")
    endpoint = "v1/accounting/od/auctions_query"
    start = "2000-01-01" if backfill else years_ago(5)
    date_filter = f"auction_date:gte:{start}"
    records = fetch_all_pages(
        endpoint,
        sort="-auction_date",
        date_filter=date_filter,
        page_size=10000,
    )
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    for col in ["high_yield", "high_investment_rate", "bid_to_cover_ratio",
                 "total_accepted", "total_tendered", "offering_amt"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ["auction_date", "issue_date", "maturity_date", "close_date",
                 "announcement_date", "settlement_date"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    df["fetched_at"] = pd.Timestamp.utcnow().isoformat()
    print(f"  {len(df)} records (since {start})")
    return df


# ── Table 5: Treasury Reporting Rates of Exchange ─────────────────────────
def fetch_exchange_rates(backfill=False):
    print("Fetching Treasury Reporting Rates of Exchange...")
    endpoint = "v1/accounting/od/rates_of_exchange"
    start = "2000-01-01" if backfill else years_ago(5)
    date_filter = f"record_date:gte:{start}"
    records = fetch_all_pages(
        endpoint,
        sort="-record_date",
        date_filter=date_filter,
        page_size=10000,
    )
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    for col in ["exchange_rate", "record_fiscal_year"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["record_date"] = pd.to_datetime(df["record_date"], errors="coerce")
    if "effective_date" in df.columns:
        df["effective_date"] = pd.to_datetime(df["effective_date"], errors="coerce")
    df["fetched_at"] = pd.Timestamp.utcnow().isoformat()
    print(f"  {len(df)} records (since {start})")
    return df


# ── Table 6: Savings Bonds ────────────────────────────────────────────────
def fetch_savings_bonds(backfill=False):
    print("Fetching Savings Bonds Issuances & Redemptions...")
    endpoint = "v1/accounting/od/savings_bonds_report"
    start = "2000-01-01" if backfill else years_ago(5)
    date_filter = f"record_date:gte:{start}"
    records = fetch_all_pages(
        endpoint,
        sort="-record_date",
        date_filter=date_filter,
        page_size=10000,
    )
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    for col in ["bonds_issued_cnt", "bonds_redeemed_cnt", "bonds_out_cnt", "bonds_matured_cnt",
                 "bonds_unmatured_cnt", "matured_redeemed_cnt", "matured_unredeemed_cnt",
                 "unmatured_redeemed_cnt", "unmatured_unredeemed_cnt", "record_fiscal_year",
                 "record_fiscal_quarter", "record_calendar_year", "record_calendar_month"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["record_date"] = pd.to_datetime(df["record_date"], errors="coerce")
    df["fetched_at"] = pd.Timestamp.utcnow().isoformat()
    print(f"  {len(df)} records (since {start})")
    return df


# ── Table 7: MTS Receipts & Outlays (Table 9) ────────────────────────────
def fetch_mts_receipts_outlays(backfill=False):
    print("Fetching Monthly Treasury Statement: Receipts & Outlays (Table 9)...")
    endpoint = "v1/accounting/mts/mts_table_9"
    start = "2000-01-01" if backfill else years_ago(5)
    date_filter = f"record_date:gte:{start}"
    records = fetch_all_pages(
        endpoint,
        sort="-record_date",
        date_filter=date_filter,
        page_size=10000,
    )
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    for col in df.columns:
        if col not in ["record_date", "record_fiscal_year", "record_fiscal_quarter",
                        "record_calendar_year", "record_calendar_month", "record_calendar_day",
                        "src_line_nbr", "classification_desc"]:
            try:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            except Exception:
                pass
    for col in ["record_fiscal_year", "record_fiscal_quarter", "record_calendar_year",
                 "record_calendar_month", "record_calendar_day"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["record_date"] = pd.to_datetime(df["record_date"], errors="coerce")
    df["fetched_at"] = pd.Timestamp.utcnow().isoformat()
    print(f"  {len(df)} records (since {start})")
    return df


# ── Table 8: MTS Outlays by Agency (Table 5) ─────────────────────────────
def fetch_mts_outlays_by_agency(backfill=False):
    print("Fetching Monthly Treasury Statement: Outlays by Agency (Table 5)...")
    endpoint = "v1/accounting/mts/mts_table_5"
    start = "2000-01-01" if backfill else years_ago(5)
    date_filter = f"record_date:gte:{start}"
    records = fetch_all_pages(
        endpoint,
        sort="-record_date",
        date_filter=date_filter,
        page_size=10000,
    )
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    for col in df.columns:
        if col not in ["record_date", "record_fiscal_year", "record_fiscal_quarter",
                        "record_calendar_year", "record_calendar_month", "record_calendar_day",
                        "src_line_nbr", "classification_desc"]:
            try:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            except Exception:
                pass
    for col in ["record_fiscal_year", "record_fiscal_quarter", "record_calendar_year",
                 "record_calendar_month", "record_calendar_day"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["record_date"] = pd.to_datetime(df["record_date"], errors="coerce")
    df["fetched_at"] = pd.Timestamp.utcnow().isoformat()
    print(f"  {len(df)} records (since {start})")
    return df


# ── Table 9: Daily Treasury Statement Operating Cash ──────────────────────
def fetch_dts_operating_cash(backfill=False):
    print("Fetching Daily Treasury Statement: Operating Cash...")
    endpoint = "v1/accounting/dts/deposits_withdrawals_operating_cash"
    start = "2010-01-01" if backfill else years_ago(3)
    date_filter = f"record_date:gte:{start}"
    records = fetch_all_pages(
        endpoint,
        sort="-record_date",
        date_filter=date_filter,
        page_size=10000,
    )
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    for col in df.columns:
        if col not in ["record_date", "record_fiscal_year", "record_fiscal_quarter",
                        "record_calendar_year", "record_calendar_month", "record_calendar_day",
                        "account_type", "transaction_type", "transaction_catg",
                        "transaction_catg_desc", "table_nm"]:
            try:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            except Exception:
                pass
    for col in ["record_fiscal_year", "record_fiscal_quarter", "record_calendar_year",
                 "record_calendar_month", "record_calendar_day"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["record_date"] = pd.to_datetime(df["record_date"], errors="coerce")
    df["fetched_at"] = pd.Timestamp.utcnow().isoformat()
    print(f"  {len(df)} records (since {start})")
    return df


# ── Table 10: MTS Budget Comparison ───────────────────────────────────────
def fetch_mts_budget_comparison(backfill=False):
    print("Fetching Monthly Treasury Statement: Budget Comparison (Table 1)...")
    endpoint = "v1/accounting/mts/mts_table_1"
    start = "2000-01-01" if backfill else years_ago(5)
    date_filter = f"record_date:gte:{start}"
    records = fetch_all_pages(
        endpoint,
        sort="-record_date",
        date_filter=date_filter,
        page_size=10000,
    )
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    for col in df.columns:
        if col not in ["record_date", "record_fiscal_year", "record_fiscal_quarter",
                        "record_calendar_year", "record_calendar_month", "record_calendar_day",
                        "src_line_nbr", "classification_desc"]:
            try:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            except Exception:
                pass
    for col in ["record_fiscal_year", "record_fiscal_quarter", "record_calendar_year",
                 "record_calendar_month", "record_calendar_day"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["record_date"] = pd.to_datetime(df["record_date"], errors="coerce")
    df["fetched_at"] = pd.Timestamp.utcnow().isoformat()
    print(f"  {len(df)} records (since {start})")
    return df


def run_pipeline(backfill=False):
    today = datetime.date.today()
    run_ts = today.isoformat()

    print("=" * 60)
    print("US Treasury Fiscal Data Pipeline")
    print(f"  Mode: {'backfill' if backfill else 'incremental'}")
    print(f"  Date: {run_ts}")
    print("=" * 60)

    tables = {
        "debt_to_penny":           fetch_debt_to_penny(backfill),
        "avg_interest_rates":      fetch_avg_interest_rates(backfill),
        "interest_expense":        fetch_interest_expense(backfill),
        "auctions_detail":         fetch_auctions(backfill),
        "exchange_rates":          fetch_exchange_rates(backfill),
        "savings_bonds":           fetch_savings_bonds(backfill),
        "mts_receipts_outlays":    fetch_mts_receipts_outlays(backfill),
        "mts_outlays_by_agency":   fetch_mts_outlays_by_agency(backfill),
        "dts_operating_cash":      fetch_dts_operating_cash(backfill),
        "mts_budget_comparison":   fetch_mts_budget_comparison(backfill),
    }

    mode = "backfill" if backfill else "incremental"
    for table_name, df in tables.items():
        if df.empty:
            print(f"  Skipping {table_name} (no data)")
            continue
        output_dir = os.path.join(BASE_DIR, table_name)
        filename = f"treasury_{table_name}_{mode}_{today:%Y%m%d}.parquet"
        out = write_partitioned(
            df,
            output_dir=output_dir,
            filename=filename,
        )
        print(f"  Wrote {table_name}: {out}")

    print("=" * 60)
    print("Treasury Fiscal Data pipeline complete.")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="US Treasury Fiscal Data Pipeline")
    parser.add_argument("--backfill", action="store_true", help="Full history vs incremental")
    args = parser.parse_args()
    run_pipeline(backfill=args.backfill)


if __name__ == "__main__":
    main()
