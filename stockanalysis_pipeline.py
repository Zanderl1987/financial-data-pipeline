#!/usr/bin/env python3
"""
Stock Analysis Pipeline — scrapes stockanalysis.com (no API key required).

Datasets
--------
  sa_movers          Items 1-3:  daily/weekly/monthly/YTD/1Y/3Y/5Y gainers+losers
                                 and premarket gainers+losers (signal column)
  sa_ipos            Item 4:     IPO history — date, symbol, company, IPO price,
                                 current price, return
  sa_ipo_calendar    Item 5:     upcoming IPOs — exchange, price range, shares
                                 offered, deal size, market cap, revenue
  sa_ipo_stats       Item 6:     annual + monthly IPO counts back to 2000
  sa_corporate_actions Items 7-13: splits, acquisitions, spinoffs, bankruptcies,
                                   symbol changes, new listings (action_type column)
  sa_stock_list      Item 18:    ~500 US stocks reference (symbol, company,
                                  industry, market cap)
  sa_etf_list        Item 19:    ETF reference (symbol, fund name, asset class, AUM)
  sa_income          Item 14:    income statements — wide format, annual + quarterly
  sa_balance         Item 15:    balance sheets — wide format, annual + quarterly
  sa_cashflow        Item 16:    cash flow statements — wide format, annual + quarterly
  sa_ratios          Item 17:    financial ratios + KPIs — wide format

CLI
---
  python stockanalysis_pipeline.py                      # all datasets
  python stockanalysis_pipeline.py --only movers
  python stockanalysis_pipeline.py --only ipos
  python stockanalysis_pipeline.py --only actions
  python stockanalysis_pipeline.py --only actions --backfill   # all years to 1998
  python stockanalysis_pipeline.py --only reference
  python stockanalysis_pipeline.py --only financials
  python stockanalysis_pipeline.py --only financials --symbols AAPL,MSFT,NVDA

Outputs
-------
  storage/raw/stockanalysis/movers/              → CATALOG: sa_movers
  storage/raw/stockanalysis/ipos/history/        → CATALOG: sa_ipos
  storage/raw/stockanalysis/ipos/calendar/       → CATALOG: sa_ipo_calendar
  storage/raw/stockanalysis/ipos/stats/          → CATALOG: sa_ipo_stats
  storage/raw/stockanalysis/corporate_actions/   → CATALOG: sa_corporate_actions
  storage/raw/stockanalysis/stocks/              → CATALOG: sa_stock_list
  storage/raw/stockanalysis/etfs/                → CATALOG: sa_etf_list
  storage/raw/stockanalysis/financials/income/   → CATALOG: sa_income
  storage/raw/stockanalysis/financials/balance/  → CATALOG: sa_balance
  storage/raw/stockanalysis/financials/cashflow/ → CATALOG: sa_cashflow
  storage/raw/stockanalysis/financials/ratios/   → CATALOG: sa_ratios
"""

import argparse
import datetime
import os
import time

import pandas as pd
import requests

from finnhub_pipeline import get_dji_symbols
from storage_utils import write_partitioned

BASE_URL        = "https://stockanalysis.com"
HEADERS         = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer":         "https://stockanalysis.com/",
}
REQUEST_DELAY   = 2.0   # seconds between page requests
FINANCIAL_DELAY = 2.5   # per-symbol financial pages
MAX_RETRIES     = 3
MOVERS_PAGES    = 5     # pages per signal (5 × 20 = top 100)

SECTOR_ETFS = [
    "XLK", "XLF", "XLE", "XLV", "XLY",
    "XLI", "XLC", "XLRE", "XLP", "XLU", "XLB",
    "SPY", "QQQ", "IWM", "DIA",
]


# ── Storage directories ──────────────────────────────────────────────────────

DIR_MOVERS    = os.path.join("storage", "raw", "stockanalysis", "movers")
DIR_IPO_HIST  = os.path.join("storage", "raw", "stockanalysis", "ipos", "history")
DIR_IPO_CAL   = os.path.join("storage", "raw", "stockanalysis", "ipos", "calendar")
DIR_IPO_STATS = os.path.join("storage", "raw", "stockanalysis", "ipos", "stats")
DIR_ACTIONS   = os.path.join("storage", "raw", "stockanalysis", "corporate_actions")
DIR_STOCKS    = os.path.join("storage", "raw", "stockanalysis", "stocks")
DIR_ETFS      = os.path.join("storage", "raw", "stockanalysis", "etfs")
DIR_INCOME    = os.path.join("storage", "raw", "stockanalysis", "financials", "income")
DIR_BALANCE   = os.path.join("storage", "raw", "stockanalysis", "financials", "balance")
DIR_CASHFLOW  = os.path.join("storage", "raw", "stockanalysis", "financials", "cashflow")
DIR_RATIOS    = os.path.join("storage", "raw", "stockanalysis", "financials", "ratios")


# Mover page timeframe params (None = default "today")
MOVER_TIMEFRAMES: dict[str, str | None] = {
    "1D":  None,
    "1W":  "w",
    "1M":  "m",
    "YTD": "ytd",
    "1Y":  "1y",
    "3Y":  "3y",
    "5Y":  "5y",
}

# Corporate action sub-paths and unique column heuristics
CORPORATE_ACTIONS: list[tuple[str, str, str]] = [
    # (action_type,   url_path,                    identifying_col)
    ("split",         "/actions/splits/",          "Symbol"),
    ("acquisition",   "/actions/acquisitions/",    "Symbol"),
    ("spinoff",       "/actions/spinoffs/",         "Parent"),
    ("bankruptcy",    "/actions/bankruptcies/",     "Symbol"),
    ("symbol_change", "/actions/symbol-changes/",  "Symbol"),
    ("listing",       "/actions/listings/",         "Symbol"),
    ("delisting",     "/actions/delistings/",       "Symbol"),
]

# Financial statement sub-paths
FINANCIAL_STMTS: list[tuple[str, str, str]] = [
    # (name,       url_suffix,              out_dir)
    ("income",   "",                        DIR_INCOME),
    ("balance",  "/balance-sheet",          DIR_BALANCE),
    ("cashflow", "/cash-flow-statement",    DIR_CASHFLOW),
    ("ratios",   "/ratios",                 DIR_RATIOS),
]


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _get(url: str, params: dict | None = None) -> requests.Response | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=25)
            if resp.status_code == 200:
                return resp
            if resp.status_code == 404:
                return None
            print(f"    HTTP {resp.status_code} (attempt {attempt}/{MAX_RETRIES})")
        except Exception as exc:
            print(f"    Request error (attempt {attempt}/{MAX_RETRIES}): {exc}")
        time.sleep(REQUEST_DELAY * attempt)
    return None


def _normalize_cols(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = (
        df.columns.astype(str)
                  .str.strip()
                  .str.lower()
                  .str.replace(r"[^\w]+", "_", regex=True)
                  .str.strip("_")
    )
    return df


_SKIP_COERCE = frozenset({
    "symbol", "company", "company_name", "fund_name", "name", "parent",
    "new_stock", "acquirer", "acquirer_name", "new_company", "parent_company",
    "industry", "sector", "asset_class", "exchange", "action_type",
    "type", "action", "date", "ipo_date", "signal", "period_type",
    "fetched_at", "metric",
})


def _coerce_numerics(df: pd.DataFrame) -> pd.DataFrame:
    for col in df.columns:
        if col in _SKIP_COERCE:
            continue
        df[col] = pd.to_numeric(
            df[col].astype(str)
                   .str.replace(r"[%,$BMKTbmkt]", "", regex=True)
                   .str.replace(",", "", regex=False),
            errors="coerce",
        )
    return df


def _parse_tables(html: str) -> list[pd.DataFrame]:
    """Return all non-empty HTML tables with flattened string column names."""
    try:
        raw = pd.read_html(html)
    except Exception:
        return []
    result = []
    for df in raw:
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [" ".join(str(c) for c in tup).strip() for tup in df.columns]
        df.columns = df.columns.astype(str)
        df = df.dropna(how="all").reset_index(drop=True)
        if not df.empty:
            result.append(df)
    return result


def _find_table(html: str, col: str) -> pd.DataFrame:
    """Return the first table that contains col as a column header."""
    for df in _parse_tables(html):
        if col in df.columns:
            return df
    return pd.DataFrame()


# ── Runner: Items 1-3 — Market Movers + Premarket ────────────────────────────

def run_movers() -> None:
    """
    Gainers + losers across 7 timeframes (top 100 each) and premarket movers.
    All rows land in a single parquet with a 'signal' column.
    """
    os.makedirs(DIR_MOVERS, exist_ok=True)
    today_str  = datetime.datetime.utcnow().strftime("%Y%m%d")
    fetched_at = datetime.datetime.utcnow().isoformat()
    frames: list[pd.DataFrame] = []

    for direction in ("gainers", "losers"):
        for tf_label, t_param in MOVER_TIMEFRAMES.items():
            signal = f"{direction}_{tf_label}"
            url    = f"{BASE_URL}/markets/{direction}/"
            tf_frames: list[pd.DataFrame] = []

            for page in range(1, MOVERS_PAGES + 1):
                params: dict = {}
                if t_param:
                    params["t"] = t_param
                if page > 1:
                    params["p"] = page
                resp = _get(url, params=params or None)
                if resp is None:
                    break
                df = _find_table(resp.text, "Symbol")
                if df.empty:
                    break
                tf_frames.append(df)
                time.sleep(REQUEST_DELAY)

            if tf_frames:
                combined = pd.concat(tf_frames, ignore_index=True)
                if "Symbol" in combined.columns:
                    combined = combined.drop_duplicates(subset=["Symbol"])
                combined["signal"]     = signal
                combined["fetched_at"] = fetched_at
                frames.append(combined)
                print(f"  [{signal}] {len(combined)} rows")

    # Premarket (both gainers + losers on one page)
    print(f"  [premarket]", end=" ", flush=True)
    resp = _get(f"{BASE_URL}/markets/premarket/")
    if resp is not None:
        pm_count = 0
        idx      = 0
        for df in _parse_tables(resp.text):
            if "Symbol" in df.columns and len(df) >= 5:
                label = "premarket_gainers" if idx == 0 else "premarket_losers"
                df["signal"]     = label
                df["fetched_at"] = fetched_at
                frames.append(df)
                pm_count += len(df)
                idx += 1
                if idx >= 2:
                    break
        print(f"{pm_count} rows")
    else:
        print("no data")

    if not frames:
        print("  No mover data collected.")
        return

    combined = pd.concat(frames, ignore_index=True)
    combined = _normalize_cols(combined)
    combined = _coerce_numerics(combined)

    path = write_partitioned(combined, DIR_MOVERS, f"sa_movers_{today_str}.parquet")
    print(f"\n  Saved {len(combined)} total rows -> {path}")

    sub = combined[combined["signal"] == "gainers_1D"]
    if not sub.empty:
        cols = [c for c in ["symbol", "company_name", "_change", "price", "volume", "market_cap"] if c in sub.columns]
        print("\n  TODAY'S GAINERS — top 5:")
        print(sub[cols].head(5).to_string(index=False))


# ── Runner: Items 4-6 — IPO Data ─────────────────────────────────────────────

def run_ipos() -> None:
    """IPO history (~200 recent), upcoming calendar, annual/monthly statistics."""
    today_str  = datetime.datetime.utcnow().strftime("%Y%m%d")
    fetched_at = datetime.datetime.utcnow().isoformat()

    # History
    os.makedirs(DIR_IPO_HIST, exist_ok=True)
    print(f"  [ipo_history]", end=" ", flush=True)
    resp = _get(f"{BASE_URL}/ipos/")
    if resp is not None:
        df = _find_table(resp.text, "Symbol")
        if not df.empty:
            df["fetched_at"] = fetched_at
            df = _normalize_cols(df)
            df = _coerce_numerics(df)
            path = write_partitioned(df, DIR_IPO_HIST, f"sa_ipos_{today_str}.parquet")
            print(f"{len(df)} rows -> {path}")
        else:
            print("no table")
    else:
        print("no data")
    time.sleep(REQUEST_DELAY)

    # Calendar (upcoming)
    os.makedirs(DIR_IPO_CAL, exist_ok=True)
    print(f"  [ipo_calendar]", end=" ", flush=True)
    resp = _get(f"{BASE_URL}/ipos/calendar/")
    if resp is not None:
        df = _find_table(resp.text, "Symbol")
        if not df.empty:
            df["fetched_at"] = fetched_at
            df = _normalize_cols(df)
            df = _coerce_numerics(df)
            path = write_partitioned(df, DIR_IPO_CAL, f"sa_ipo_calendar_{today_str}.parquet")
            print(f"{len(df)} rows -> {path}")
        else:
            print("no table")
    else:
        print("no data")
    time.sleep(REQUEST_DELAY)

    # Statistics (annual + monthly counts)
    os.makedirs(DIR_IPO_STATS, exist_ok=True)
    print(f"  [ipo_stats]", end=" ", flush=True)
    resp = _get(f"{BASE_URL}/ipos/statistics/")
    if resp is not None:
        stat_frames: list[pd.DataFrame] = []
        for df in _parse_tables(resp.text):
            if len(df.columns) >= 2 and len(df) >= 3:
                stat_frames.append(df)
        if stat_frames:
            combined = pd.concat(stat_frames, ignore_index=True)
            combined["fetched_at"] = fetched_at
            combined = _normalize_cols(combined)
            path = write_partitioned(combined, DIR_IPO_STATS, f"sa_ipo_stats_{today_str}.parquet")
            print(f"{len(combined)} rows -> {path}")
        else:
            print("no table")
    else:
        print("no data")


# ── Runner: Items 7-13 — Corporate Actions ───────────────────────────────────

def run_corporate_actions(backfill: bool = False) -> None:
    """
    Splits, acquisitions, spinoffs, bankruptcies, symbol changes, listings, delistings.

    Default (incremental): fetch the current page for each action type.
    --backfill: loop through every year from 1998 to present for historical depth.
    """
    os.makedirs(DIR_ACTIONS, exist_ok=True)
    today_str  = datetime.datetime.utcnow().strftime("%Y%m%d")
    fetched_at = datetime.datetime.utcnow().isoformat()
    all_frames: list[pd.DataFrame] = []

    years = list(range(1998, datetime.date.today().year + 1)) if backfill else [None]

    for action_type, path_suffix, id_col in CORPORATE_ACTIONS:
        url          = f"{BASE_URL}{path_suffix}"
        type_frames: list[pd.DataFrame] = []

        for year in years:
            params = {"year": year} if year else None
            label  = f"{action_type}" + (f" {year}" if year else "")
            print(f"  [{label}]", end=" ", flush=True)

            resp = _get(url, params=params)
            if resp is None:
                print("no data")
                continue

            df = _find_table(resp.text, id_col)
            if df.empty:
                df = _find_table(resp.text, "Date")
            if df.empty:
                print("no table")
                continue

            df["action_type"] = action_type
            df["fetched_at"]  = fetched_at
            type_frames.append(df)
            print(f"{len(df)} rows")
            time.sleep(REQUEST_DELAY)

        all_frames.extend(type_frames)

    if not all_frames:
        print("  No corporate action data collected.")
        return

    combined = pd.concat(all_frames, ignore_index=True)
    combined  = _normalize_cols(combined)
    mode      = "backfill" if backfill else "incremental"
    path      = write_partitioned(
        combined, DIR_ACTIONS, f"sa_corporate_actions_{mode}_{today_str}.parquet"
    )
    print(f"\n  Saved {len(combined)} corporate action rows -> {path}")
    print(combined["action_type"].value_counts().to_string())


# ── Runner: Items 18-19 — Reference Lists ────────────────────────────────────

def run_reference() -> None:
    """
    Stock and ETF reference lists.

    Note: stockanalysis.com renders these tables with JavaScript pagination,
    so only the first server-rendered page (~500 rows) is reliably available
    without a headless browser.
    """
    today_str  = datetime.datetime.utcnow().strftime("%Y%m%d")
    fetched_at = datetime.datetime.utcnow().isoformat()

    # Stock list
    os.makedirs(DIR_STOCKS, exist_ok=True)
    print(f"  [stock_list]", end=" ", flush=True)
    resp = _get(f"{BASE_URL}/stocks/")
    if resp is not None:
        df = _find_table(resp.text, "Symbol")
        if not df.empty:
            df["fetched_at"] = fetched_at
            df = _normalize_cols(df)
            df = _coerce_numerics(df)
            path = write_partitioned(df, DIR_STOCKS, f"sa_stock_list_{today_str}.parquet")
            print(f"{len(df)} rows -> {path}")
        else:
            print("no table")
    else:
        print("no data")
    time.sleep(REQUEST_DELAY)

    # ETF list
    os.makedirs(DIR_ETFS, exist_ok=True)
    print(f"  [etf_list]", end=" ", flush=True)
    resp = _get(f"{BASE_URL}/etf/")
    if resp is not None:
        df = _find_table(resp.text, "Symbol")
        if not df.empty:
            df["fetched_at"] = fetched_at
            df = _normalize_cols(df)
            df = _coerce_numerics(df)
            path = write_partitioned(df, DIR_ETFS, f"sa_etf_list_{today_str}.parquet")
            print(f"{len(df)} rows -> {path}")
        else:
            print("no table")
    else:
        print("no data")


# ── Runner: Items 14-17 — Per-Symbol Financials ──────────────────────────────

def _fetch_one_financial(symbol: str, url_suffix: str) -> pd.DataFrame:
    """
    Fetch annual + quarterly data for one symbol + statement type.

    stockanalysis.com lays out financials as a wide table:
      Row 0 header: years/quarters as column names
      Rows 1+: metric name in col[0], values in subsequent columns

    Returns a DataFrame with columns:
      metric | <year1> | <year2> | ... | symbol | period_type | fetched_at
    """
    base   = f"{BASE_URL}/stocks/{symbol.lower()}/financials{url_suffix}/"
    frames: list[pd.DataFrame] = []

    for period, p_param in (("annual", None), ("quarterly", "quarterly")):
        resp = _get(base, params={"p": p_param} if p_param else None)
        if resp is None:
            time.sleep(FINANCIAL_DELAY)
            continue

        for df in _parse_tables(resp.text):
            if len(df) < 5 or len(df.columns) < 3:
                continue
            # Financial tables: first column is metric names (strings), rest are numbers
            first_col_text_ratio = (
                df.iloc[:, 0].astype(str)
                              .str.contains(r"[A-Za-z]", regex=True)
                              .mean()
            )
            if first_col_text_ratio < 0.6:
                continue
            df.columns = ["metric"] + list(df.columns[1:])
            df["symbol"]      = symbol.upper()
            df["period_type"] = period
            frames.append(df)
            break

        time.sleep(FINANCIAL_DELAY)

    if not frames:
        return pd.DataFrame()

    combined               = pd.concat(frames, ignore_index=True)
    combined["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return combined


def run_financials(symbols: list[str]) -> None:
    """
    Fetch income, balance sheet, cash flow, and ratios for each symbol.
    Annual + quarterly periods. One parquet per statement type.
    """
    today_str = datetime.datetime.utcnow().strftime("%Y%m%d")
    total     = len(symbols)
    print(f"  {total} symbols × 4 statements × 2 periods = up to {total * 8} requests")

    for stmt_name, url_suffix, out_dir in FINANCIAL_STMTS:
        os.makedirs(out_dir, exist_ok=True)
        stmt_frames: list[pd.DataFrame] = []
        print(f"\n  [{stmt_name}]")

        for i, symbol in enumerate(symbols, 1):
            print(f"    [{i}/{total}] {symbol}...", end=" ", flush=True)
            df = _fetch_one_financial(symbol, url_suffix)
            if not df.empty:
                stmt_frames.append(df)
                print(f"{len(df)} rows")
            else:
                print("no data")

        if not stmt_frames:
            print(f"  No data collected for {stmt_name}.")
            continue

        combined = pd.concat(stmt_frames, ignore_index=True)
        combined = _normalize_cols(combined)
        path     = write_partitioned(
            combined, out_dir, f"sa_{stmt_name}_{today_str}.parquet"
        )
        print(f"  Saved {len(combined)} rows ({total} symbols) -> {path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stock Analysis scraper pipeline (stockanalysis.com, no API key)"
    )
    parser.add_argument(
        "--only",
        default="all",
        help=(
            "Comma-separated subset: movers,ipos,actions,reference,financials "
            "(default: all)"
        ),
    )
    parser.add_argument(
        "--symbols",
        default=None,
        help="Symbols for financials mode (default: DJI components + sector ETFs)",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="For corporate actions: fetch all years back to 1998",
    )
    args    = parser.parse_args()
    run_set = {s.strip().lower() for s in args.only.split(",")}
    run_all = "all" in run_set

    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*60}\n  Stock Analysis Pipeline — {ts}\n{'='*60}")

    if run_all or "movers" in run_set:
        print("\n[Items 1-3] Market Movers (7 timeframes + premarket)")
        run_movers()

    if run_all or "ipos" in run_set:
        print("\n[Items 4-6] IPO History + Calendar + Statistics")
        run_ipos()

    if run_all or "actions" in run_set:
        print("\n[Items 7-13] Corporate Actions (splits/acquisitions/spinoffs/etc.)")
        run_corporate_actions(backfill=args.backfill)

    if run_all or "reference" in run_set:
        print("\n[Items 18-19] Stock + ETF Reference Lists")
        run_reference()

    if run_all or "financials" in run_set:
        print("\n[Items 14-17] Per-Symbol Financials (income/balance/cashflow/ratios)")
        if args.symbols:
            syms = [s.strip().upper() for s in args.symbols.split(",")]
        else:
            dji  = get_dji_symbols()
            syms = sorted(set(dji + SECTOR_ETFS))
        run_financials(syms)

    print("\n--- STOCK ANALYSIS PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
