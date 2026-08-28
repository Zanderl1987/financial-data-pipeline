"""
Alpha Vantage Fundamentals Pipeline -- company overview, financial statements,
and earnings history for DOW 30 companies.

Free tier: 25 requests/day. A default (non-backfill) run is capped at
--max-requests (20 by default) total Alpha Vantage requests, split across
overview, earnings, earnings calendar, dividends, insider transactions,
news sentiment, and top gainers/losers. Overview/earnings/dividends/insider
each pull a small rotating subset of symbols per day (based on day-of-year)
so the full DOW 30 universe is covered over successive daily runs instead of
blowing the daily quota in one run.

--backfill fetches full financial statements (income statement, balance
sheet, cash flow) for ALL symbols and is NOT capped by --max-requests --
expect it to vastly exceed the 25 req/day free-tier quota and require
pacing across multiple days (or a paid tier) to complete.

Requires ALPHA_VANTAGE_API_KEY in .env (free registration at alphavantage.co).

CLI:
  python alpha_vantage_fundamentals_pipeline.py                    # incremental (latest data, budget-capped)
  python alpha_vantage_fundamentals_pipeline.py --max-requests 15   # tighter budget
  python alpha_vantage_fundamentals_pipeline.py --backfill          # full available history (uncapped, slow)

Output:
  storage/raw/alpha_vantage/overview/
  storage/raw/alpha_vantage/income_statement/
  storage/raw/alpha_vantage/balance_sheet/
  storage/raw/alpha_vantage/cash_flow/
  storage/raw/alpha_vantage/earnings/
  storage/raw/alpha_vantage/earnings_calendar/
  storage/raw/alpha_vantage/dividends/
  storage/raw/alpha_vantage/insider_transactions/
  storage/raw/alpha_vantage/news_sentiment/
  storage/raw/alpha_vantage/top_gainers_losers/
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

AV_API_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY")
AV_BASE = "https://www.alphavantage.co/query"

BASE_DIR = os.path.join("storage", "raw", "alpha_vantage")

# Free tier: 5 requests/minute, 25 requests/day
REQUEST_INTERVAL = 12.0
MAX_RETRIES = 3
BACKOFF_SECONDS = 60

# Default request budget for a non-backfill run. Kept comfortably under the
# 25 requests/day free-tier ceiling so one run never exhausts the day's quota.
DEFAULT_MAX_REQUESTS = 20

# Per-section symbol counts for incremental (non-backfill) runs. These are
# soft targets -- the RequestBudget below is the hard backstop that actually
# guarantees the total never exceeds --max-requests.
INCREMENTAL_OVERVIEW_N = 7
INCREMENTAL_EARNINGS_N = 7
INCREMENTAL_DIVIDENDS_N = 2
INCREMENTAL_INSIDER_N = 2


class RequestBudget:
    """Hard cap on the number of Alpha Vantage requests issued in a run.

    limit=None means unlimited (used for --backfill, which intentionally
    exceeds the free-tier daily quota and is expected to be paced by the
    caller across multiple days).
    """

    def __init__(self, limit):
        self.limit = limit
        self.used = 0
        self.skipped = []

    def allow(self, label):
        if self.limit is not None and self.used >= self.limit:
            self.skipped.append(label)
            return False
        self.used += 1
        return True

    def report_skipped(self):
        if self.skipped:
            preview = ", ".join(self.skipped[:10])
            more = " ..." if len(self.skipped) > 10 else ""
            print(f"  Budget exhausted -- skipped {len(self.skipped)} request(s): {preview}{more}")


def get_rotating_subset(symbols, n, offset=0):
    """Return n symbols starting from a day-of-year-based rotating window.

    Lets a small per-day quota still cover the full symbol universe over
    successive daily runs instead of always hitting the same first N symbols.
    """
    if not symbols or n <= 0:
        return []
    n = min(n, len(symbols))
    start = (datetime.date.today().toordinal() + offset) % len(symbols)
    return [symbols[(start + i) % len(symbols)] for i in range(n)]


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
            "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS",
            "GS", "HD", "HON", "IBM", "JNJ", "JPM", "KO", "MCD", "MMM", "MRK",
            "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH", "V", "VZ", "WMT",
        ]


def get_with_backoff(params):
    """Make API request with retry logic."""
    if not AV_API_KEY:
        print("  ERROR: ALPHA_VANTAGE_API_KEY not set in .env")
        return None

    params["apikey"] = AV_API_KEY

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(AV_BASE, params=params, timeout=30)
            data = resp.json()

            if "Error Message" in data:
                print(f"  API Error: {data['Error Message']}")
                return None
            if "Note" in data:
                print(f"  Rate limit hit: {data['Note']}")
                wait = BACKOFF_SECONDS * attempt
                print(f"  Waiting {wait}s...")
                time.sleep(wait)
                continue
            if "Information" in data:
                print(f"  Info: {data['Information']}")
                return None

            return data
        except requests.exceptions.RequestException as e:
            if attempt == MAX_RETRIES:
                print(f"  Failed after {MAX_RETRIES} attempts: {e}")
                return None
            wait = BACKOFF_SECONDS * attempt
            print(f"  Error: {e}, retrying in {wait}s (attempt {attempt}/{MAX_RETRIES})")
            time.sleep(wait)
    return None


def fetch_overview(symbol):
    """Fetch company overview/summary."""
    params = {"function": "OVERVIEW", "symbol": symbol}
    data = get_with_backoff(params)
    time.sleep(REQUEST_INTERVAL)
    if not data or not data.get("Symbol"):
        return pd.DataFrame()
    df = pd.DataFrame([data])
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return df


def fetch_financial_statement(function_name, symbol):
    """Fetch income statement, balance sheet, or cash flow."""
    params = {"function": function_name, "symbol": symbol}
    data = get_with_backoff(params)
    time.sleep(REQUEST_INTERVAL)
    if not data:
        return pd.DataFrame()

    rows = []
    for report_type in ["annualReports", "quarterlyReports"]:
        for report in data.get(report_type, []):
            report["report_type"] = report_type.replace("Reports", "")
            report["ticker"] = symbol
            rows.append(report)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return df


def fetch_earnings(symbol):
    """Fetch earnings history with estimates."""
    params = {"function": "EARNINGS", "symbol": symbol}
    data = get_with_backoff(params)
    time.sleep(REQUEST_INTERVAL)
    if not data:
        return pd.DataFrame()

    rows = []
    for report_type in ["annualEarnings", "quarterlyEarnings"]:
        for report in data.get(report_type, []):
            report["report_type"] = report_type.replace("Earnings", "")
            report["ticker"] = symbol
            rows.append(report)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return df


def fetch_earnings_calendar(horizon="3month"):
    """Fetch upcoming earnings calendar (CSV format)."""
    params = {"function": "EARNINGS_CALENDAR", "horizon": horizon}
    params["apikey"] = AV_API_KEY

    try:
        resp = requests.get(AV_BASE, params=params, timeout=30)
        time.sleep(REQUEST_INTERVAL)
        if resp.status_code != 200:
            print(f"  Earnings calendar request failed: {resp.status_code}")
            return pd.DataFrame()
        from io import StringIO
        df = pd.read_csv(StringIO(resp.text))
        df["fetched_at"] = datetime.datetime.utcnow().isoformat()
        return df
    except Exception as e:
        print(f"  Earnings calendar error: {e}")
        return pd.DataFrame()


def fetch_dividends(symbol):
    """Fetch dividend history."""
    params = {"function": "DIVIDENDS", "symbol": symbol}
    data = get_with_backoff(params)
    time.sleep(REQUEST_INTERVAL)
    if not data:
        return pd.DataFrame()

    rows = data.get("data", [])
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["ticker"] = symbol
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return df


def fetch_insider_transactions(symbol):
    """Fetch insider transactions."""
    params = {"function": "INSIDER_TRANSACTIONS", "symbol": symbol}
    data = get_with_backoff(params)
    time.sleep(REQUEST_INTERVAL)
    if not data:
        return pd.DataFrame()

    rows = data.get("data", [])
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["ticker"] = symbol
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return df


def fetch_news_sentiment(tickers=None, topics=None):
    """Fetch market news and sentiment."""
    params = {"function": "NEWS_SENTIMENT"}
    if tickers:
        params["tickers"] = ",".join(tickers)
    if topics:
        params["topics"] = ",".join(topics)

    data = get_with_backoff(params)
    time.sleep(REQUEST_INTERVAL)
    if not data:
        return pd.DataFrame()

    rows = data.get("feed", [])
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return df


def fetch_top_gainers_losers():
    """Fetch top gainers and losers."""
    params = {"function": "TOP_GAINERS_LOSERS"}
    data = get_with_backoff(params)
    time.sleep(REQUEST_INTERVAL)
    if not data:
        return pd.DataFrame()

    rows = []
    for category in ["top_gainers", "top_losers", "most_actively_traded"]:
        for item in data.get(category, []):
            item["category"] = category
            rows.append(item)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return df


def run_pipeline(backfill=False, max_requests=DEFAULT_MAX_REQUESTS):
    """Run the Alpha Vantage fundamentals pipeline."""
    print("=" * 60)
    print("Alpha Vantage Fundamentals Pipeline")
    print("=" * 60)

    if not AV_API_KEY:
        print("ERROR: ALPHA_VANTAGE_API_KEY not set in .env")
        print("Register at https://www.alphavantage.co/support/#api-key")
        return

    symbols = get_dji_symbols()
    print(f"Processing {len(symbols)} DOW companies")

    budget = RequestBudget(None if backfill else max_requests)

    if backfill:
        print("WARNING: --backfill fetches full financial statements for ALL symbols "
              "in addition to overview/earnings/etc. This is NOT budget-capped and "
              "vastly exceeds the 25 requests/day free-tier quota -- expect a "
              "multi-day runtime (or pace it manually across days) unless you have "
              "a paid Alpha Vantage tier.")
    else:
        print(f"Free tier: 25 requests/day. This run is capped at {max_requests} "
              f"total requests (use --max-requests to change, --backfill for full "
              f"history). Overview/earnings/dividends/insider use a rotating daily "
              f"subset of symbols so full coverage accumulates over multiple days.")

    today = datetime.date.today().isoformat()
    today_compact = datetime.date.today().strftime("%Y%m%d")

    # Symbol subsets: full universe for --backfill, small rotating subsets otherwise.
    if backfill:
        overview_symbols = symbols
        earnings_symbols = symbols
        dividend_symbols = symbols
        insider_symbols = symbols
    else:
        overview_symbols = get_rotating_subset(symbols, INCREMENTAL_OVERVIEW_N, offset=0)
        earnings_symbols = get_rotating_subset(symbols, INCREMENTAL_EARNINGS_N, offset=11)
        dividend_symbols = get_rotating_subset(symbols, INCREMENTAL_DIVIDENDS_N, offset=22)
        insider_symbols = get_rotating_subset(symbols, INCREMENTAL_INSIDER_N, offset=27)

    # 1. Company Overview
    print("\n--- Company Overview ---")
    overview_dir = os.path.join(BASE_DIR, "overview")
    os.makedirs(overview_dir, exist_ok=True)

    all_overview = []
    for symbol in overview_symbols:
        if not budget.allow(f"overview:{symbol}"):
            break
        print(f"  Fetching overview for {symbol}...")
        df = fetch_overview(symbol)
        if not df.empty:
            all_overview.append(df)
            print(f"    OK")

    if all_overview:
        overview_df = pd.concat(all_overview, ignore_index=True)
        filename = f"alpha_vantage_overview_{today_compact}.parquet"
        write_partitioned(overview_df, overview_dir, filename)
        print(f"  {len(overview_df)} company overviews for {today}")

    # 2. Earnings History
    print("\n--- Earnings History ---")
    earnings_dir = os.path.join(BASE_DIR, "earnings")
    os.makedirs(earnings_dir, exist_ok=True)

    all_earnings = []
    for symbol in earnings_symbols:
        if not budget.allow(f"earnings:{symbol}"):
            break
        print(f"  Fetching earnings for {symbol}...")
        df = fetch_earnings(symbol)
        if not df.empty:
            all_earnings.append(df)
            print(f"    {len(df)} earnings records")

    if all_earnings:
        earnings_df = pd.concat(all_earnings, ignore_index=True)
        filename = f"alpha_vantage_earnings_{today_compact}.parquet"
        write_partitioned(earnings_df, earnings_dir, filename)
        print(f"  {len(earnings_df)} earnings records for {today}")

    # 3. Earnings Calendar
    print("\n--- Earnings Calendar ---")
    calendar_dir = os.path.join(BASE_DIR, "earnings_calendar")
    os.makedirs(calendar_dir, exist_ok=True)

    if budget.allow("earnings_calendar"):
        print("  Fetching earnings calendar (3 month horizon)...")
        cal_df = fetch_earnings_calendar("3month")
        if not cal_df.empty:
            filename = f"alpha_vantage_earnings_calendar_{today_compact}.parquet"
            write_partitioned(cal_df, calendar_dir, filename)
            print(f"  {len(cal_df)} upcoming earnings dates")

    # 4. Dividends
    print("\n--- Dividends ---")
    div_dir = os.path.join(BASE_DIR, "dividends")
    os.makedirs(div_dir, exist_ok=True)

    all_divs = []
    for symbol in dividend_symbols:
        if not budget.allow(f"dividends:{symbol}"):
            break
        print(f"  Fetching dividends for {symbol}...")
        df = fetch_dividends(symbol)
        if not df.empty:
            all_divs.append(df)
            print(f"    {len(df)} dividend records")

    if all_divs:
        div_df = pd.concat(all_divs, ignore_index=True)
        filename = f"alpha_vantage_dividends_{today_compact}.parquet"
        write_partitioned(div_df, div_dir, filename)
        print(f"  {len(div_df)} dividend records for {today}")

    # 5. Insider Transactions
    print("\n--- Insider Transactions ---")
    insider_dir = os.path.join(BASE_DIR, "insider_transactions")
    os.makedirs(insider_dir, exist_ok=True)

    all_insider = []
    for symbol in insider_symbols:
        if not budget.allow(f"insider:{symbol}"):
            break
        print(f"  Fetching insider transactions for {symbol}...")
        df = fetch_insider_transactions(symbol)
        if not df.empty:
            all_insider.append(df)
            print(f"    {len(df)} insider records")

    if all_insider:
        insider_df = pd.concat(all_insider, ignore_index=True)
        filename = f"alpha_vantage_insider_transactions_{today_compact}.parquet"
        write_partitioned(insider_df, insider_dir, filename)
        print(f"  {len(insider_df)} insider records for {today}")

    # 6. News & Sentiment
    print("\n--- News & Sentiment ---")
    news_dir = os.path.join(BASE_DIR, "news_sentiment")
    os.makedirs(news_dir, exist_ok=True)

    if budget.allow("news_sentiment"):
        print("  Fetching market news & sentiment...")
        news_df = fetch_news_sentiment(
            tickers=symbols[:5],
            topics="earnings,financial_markets,economy_macro",
        )
        if not news_df.empty:
            filename = f"alpha_vantage_news_sentiment_{today_compact}.parquet"
            write_partitioned(news_df, news_dir, filename)
            print(f"  {len(news_df)} news articles for {today}")

    # 7. Top Gainers/Losers
    print("\n--- Top Gainers/Losers ---")
    gainers_dir = os.path.join(BASE_DIR, "top_gainers_losers")
    os.makedirs(gainers_dir, exist_ok=True)

    if budget.allow("top_gainers_losers"):
        print("  Fetching top gainers/losers...")
        gainers_df = fetch_top_gainers_losers()
        if not gainers_df.empty:
            filename = f"alpha_vantage_top_gainers_losers_{today_compact}.parquet"
            write_partitioned(gainers_df, gainers_dir, filename)
            print(f"  {len(gainers_df)} gainers/losers records for {today}")

    budget.report_skipped()

    # Backfill: fetch financial statements (uses many requests, not budget-capped)
    if backfill:
        print("\n--- Backfill: Financial Statements ---")

        print("  Income Statement...")
        income_dir = os.path.join(BASE_DIR, "income_statement")
        os.makedirs(income_dir, exist_ok=True)
        all_income = []
        for symbol in symbols:
            print(f"    Fetching income statement for {symbol}...")
            df = fetch_financial_statement("INCOME_STATEMENT", symbol)
            if not df.empty:
                all_income.append(df)
                print(f"      {len(df)} records")
        if all_income:
            income_df = pd.concat(all_income, ignore_index=True)
            filename = f"alpha_vantage_income_statement_{today_compact}.parquet"
            write_partitioned(income_df, income_dir, filename)
            print(f"  {len(income_df)} income statement records")

        print("  Balance Sheet...")
        bs_dir = os.path.join(BASE_DIR, "balance_sheet")
        os.makedirs(bs_dir, exist_ok=True)
        all_bs = []
        for symbol in symbols:
            print(f"    Fetching balance sheet for {symbol}...")
            df = fetch_financial_statement("BALANCE_SHEET", symbol)
            if not df.empty:
                all_bs.append(df)
                print(f"      {len(df)} records")
        if all_bs:
            bs_df = pd.concat(all_bs, ignore_index=True)
            filename = f"alpha_vantage_balance_sheet_{today_compact}.parquet"
            write_partitioned(bs_df, bs_dir, filename)
            print(f"  {len(bs_df)} balance sheet records")

        print("  Cash Flow...")
        cf_dir = os.path.join(BASE_DIR, "cash_flow")
        os.makedirs(cf_dir, exist_ok=True)
        all_cf = []
        for symbol in symbols:
            print(f"    Fetching cash flow for {symbol}...")
            df = fetch_financial_statement("CASH_FLOW", symbol)
            if not df.empty:
                all_cf.append(df)
                print(f"      {len(df)} records")
        if all_cf:
            cf_df = pd.concat(all_cf, ignore_index=True)
            filename = f"alpha_vantage_cash_flow_{today_compact}.parquet"
            write_partitioned(cf_df, cf_dir, filename)
            print(f"  {len(cf_df)} cash flow records")

    print("\n" + "=" * 60)
    print(f"Alpha Vantage Fundamentals Pipeline complete. "
          f"({budget.used} request(s) used"
          f"{'' if backfill else f'/{max_requests} budgeted'})")
    print("NOTE: Free tier is 25 requests/day. Run --backfill separately, paced across days.")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Alpha Vantage Fundamentals Pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch full financial statements for all symbols "
                             "(uncapped -- vastly exceeds daily quota, expect multi-day runtime)")
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS,
                        help=f"Max Alpha Vantage requests for a non-backfill run "
                             f"(default {DEFAULT_MAX_REQUESTS}; free tier is 25/day)")
    args = parser.parse_args()
    run_pipeline(backfill=args.backfill, max_requests=args.max_requests)


if __name__ == "__main__":
    main()
