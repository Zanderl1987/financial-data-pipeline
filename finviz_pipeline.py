#!/usr/bin/env python3
"""
Finviz Pipeline — scrapes 15 datasets from finviz.com (no API key required).

Datasets collected
------------------
  finviz_movers        — Items 1-7: gainers, losers, unusual_volume, new_highs,
                         new_lows, most_volatile, overbought, oversold
                         (signal column distinguishes them; identical schema)
  finviz_screener      — Item 8:  S&P 500 overview (price, change, vol, mkt cap, P/E)
  finviz_financials    — Item 9:  S&P 500 financials (ROA, ROE, margins, debt ratios)
  finviz_insider       — Item 10: Insider buy/sell/option-exercise transactions
  finviz_sector_perf   — Item 11: Sector performance across multiple timeframes
  finviz_industry_perf — Item 12: Industry performance across multiple timeframes
  finviz_country_perf  — Item 13: Country performance across multiple timeframes
  finviz_group_valuation — Items 14-15: P/E, P/S, P/B, dividend, analyst rec
                           by sector and industry (group_type column distinguishes)

CLI
---
  python finviz_pipeline.py                             # all datasets
  python finviz_pipeline.py --only movers               # just movers
  python finviz_pipeline.py --only screener,financials
  python finviz_pipeline.py --only insider
  python finviz_pipeline.py --only groups
  python finviz_pipeline.py --max-screener-results 250
  python finviz_pipeline.py --insider-pages 5

Outputs
-------
  storage/raw/finviz/movers/           → CATALOG: finviz_movers
  storage/raw/finviz/screener/         → CATALOG: finviz_screener
  storage/raw/finviz/financials/       → CATALOG: finviz_financials
  storage/raw/finviz/insider/          → CATALOG: finviz_insider
  storage/raw/finviz/groups/sector/    → CATALOG: finviz_sector_perf
  storage/raw/finviz/groups/industry/  → CATALOG: finviz_industry_perf
  storage/raw/finviz/groups/country/   → CATALOG: finviz_country_perf
  storage/raw/finviz/groups/valuation/ → CATALOG: finviz_group_valuation

Note: Finviz provides 15-minute delayed quotes on the free tier.
"""

import argparse
import datetime
import io
import os
import re
import time

import pandas as pd
import requests

from storage_utils import write_partitioned

BASE_URL = "https://finviz.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://finviz.com/",
}
REQUEST_DELAY = 1.5   # seconds between requests
MAX_RETRIES   = 3
PAGE_SIZE     = 20    # Finviz default rows per screener page


# ── Storage directories ──────────────────────────────────────────────────────

DIR_MOVERS     = os.path.join("storage", "raw", "finviz", "movers")
DIR_SCREENER   = os.path.join("storage", "raw", "finviz", "screener")
DIR_FINANCIALS = os.path.join("storage", "raw", "finviz", "financials")
DIR_INSIDER    = os.path.join("storage", "raw", "finviz", "insider")
DIR_SECTOR     = os.path.join("storage", "raw", "finviz", "groups", "sector")
DIR_INDUSTRY   = os.path.join("storage", "raw", "finviz", "groups", "industry")
DIR_COUNTRY    = os.path.join("storage", "raw", "finviz", "groups", "country")
DIR_VALUATION  = os.path.join("storage", "raw", "finviz", "groups", "valuation")


# Screener signal codes → canonical names stored in the `signal` column
MOVER_SIGNALS: dict[str, str] = {
    "ta_topgainers":    "gainers",
    "ta_toplosers":     "losers",
    "ta_unusualvolume": "unusual_volume",
    "ta_newhigh":       "new_highs",
    "ta_newlow":        "new_lows",
    "ta_mostvolatile":  "most_volatile",
    "ta_overbought":    "overbought",
    "ta_oversold":      "oversold",
}


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _get(url: str, params: dict | None = None) -> requests.Response | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=20)
            if resp.status_code == 200:
                return resp
            print(f"    HTTP {resp.status_code} (attempt {attempt}/{MAX_RETRIES}): {url}")
        except Exception as exc:
            print(f"    Request error (attempt {attempt}/{MAX_RETRIES}): {exc}")
        time.sleep(REQUEST_DELAY * attempt)
    return None


# ── Column normalisation ─────────────────────────────────────────────────────

_SKIP_COERCE = frozenset({
    "ticker", "company", "sector", "industry", "country", "signal",
    "owner", "relationship", "transaction", "name", "group_type",
    "sec_form_4", "date", "fetched_at",
})


def _normalize_cols(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = (
        df.columns.astype(str)
                  .str.strip()
                  .str.lower()
                  .str.replace(r"[^\w]+", "_", regex=True)
                  .str.strip("_")
    )
    return df


def _coerce_numerics(df: pd.DataFrame) -> pd.DataFrame:
    for col in df.columns:
        if col in _SKIP_COERCE:
            continue
        df[col] = pd.to_numeric(
            df[col].astype(str)
                   .str.replace(r"[%,$]", "", regex=True)
                   .str.replace(",", "", regex=False),
            errors="coerce",
        )
    return df


# ── HTML table parsers ───────────────────────────────────────────────────────

def _find_table_with_col(html: str, col_name: str) -> pd.DataFrame:
    """
    Parse all HTML tables and return the LARGEST one that contains col_name
    as a column header.  The page now includes several small widget tables
    (e.g. a "recent tickers" strip) that also carry a Ticker column ahead of
    the real results grid in document order, so picking the first match is
    no longer safe -- the real data table is reliably the one with the most
    rows.  Uses pd.read_html so no extra deps are needed.
    """
    try:
        tables = pd.read_html(io.StringIO(html), match=col_name)
    except Exception:
        return pd.DataFrame()

    best = pd.DataFrame()
    for df in tables:
        # read_html may use MultiIndex columns; flatten to strings
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [" ".join(str(c) for c in col).strip() for col in df.columns]
        df.columns = df.columns.astype(str)
        if col_name not in df.columns:
            continue
        # Drop the row-number column Finviz includes
        df = df.drop(columns=[c for c in df.columns if c in ("No.", "")], errors="ignore")
        # Drop fully-NaN rows (Finviz often adds spacer rows)
        df = df.dropna(how="all").reset_index(drop=True)
        if len(df) > len(best):
            best = df

    return best


def _get_screener_total(html: str) -> int:
    """Extract the total result count shown on a Finviz screener page."""
    m = re.search(r"([\d,]+)\s*Total", html)
    if m:
        return int(m.group(1).replace(",", ""))
    m = re.search(r"\d+\s*/\s*([\d,]+)", html)
    if m:
        return int(m.group(1).replace(",", ""))
    return 0


# ── Screener paginator ───────────────────────────────────────────────────────

def _scrape_screener(
    url: str,
    extra_params: dict | None = None,
    max_results: int = 500,
) -> pd.DataFrame:
    """
    Paginate through a Finviz screener URL and return all rows as a DataFrame.

    Finviz paginates with the `r` query param (1-based row offset).
    First request omits `r`; subsequent requests use r=21, r=41, etc.
    """
    frames: list[pd.DataFrame] = []
    offset = 1
    total: int | None = None

    while True:
        params = dict(extra_params or {})
        if offset > 1:
            params["r"] = offset

        resp = _get(url, params=params or None)
        if resp is None:
            break

        df = _find_table_with_col(resp.text, "Ticker")
        if df.empty:
            break

        frames.append(df)

        if total is None:
            total = _get_screener_total(resp.text)
            if total == 0:
                total = len(df)  # single-page result with no counter

        fetched = sum(len(f) for f in frames)
        if fetched >= total or fetched >= max_results:
            break

        offset += PAGE_SIZE
        if offset > total:
            break
        time.sleep(REQUEST_DELAY)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    if "Ticker" in combined.columns:
        combined = combined.drop_duplicates(subset=["Ticker"])
    return combined


# ── Runner: Items 1-7 — Market Movers ────────────────────────────────────────

def run_movers() -> None:
    """Fetch all 8 screener signals and write to a single partitioned parquet."""
    os.makedirs(DIR_MOVERS, exist_ok=True)
    today_str  = datetime.datetime.utcnow().strftime("%Y%m%d")
    fetched_at = datetime.datetime.utcnow().isoformat()
    url        = f"{BASE_URL}/screener.ashx"

    frames: list[pd.DataFrame] = []
    for signal_code, signal_name in MOVER_SIGNALS.items():
        print(f"  [{signal_name}]", end=" ", flush=True)
        df = _scrape_screener(url, extra_params={"v": "111", "s": signal_code}, max_results=200)
        if df.empty:
            print("no data")
            continue
        df["signal"]     = signal_name
        df["fetched_at"] = fetched_at
        frames.append(df)
        print(f"{len(df)} rows")
        time.sleep(REQUEST_DELAY)

    if not frames:
        print("  No mover data collected.")
        return

    combined = pd.concat(frames, ignore_index=True)
    combined = _normalize_cols(combined)
    combined = _coerce_numerics(combined)

    path = write_partitioned(combined, DIR_MOVERS, f"finviz_movers_{today_str}.parquet")
    print(f"\n  Saved {len(combined)} total rows -> {path}")

    for sig in combined["signal"].unique():
        sub  = combined[combined["signal"] == sig]
        cols = [c for c in ["ticker", "company", "price", "change", "volume"] if c in sub.columns]
        print(f"\n  {sig.upper()} — top 5:")
        print(sub[cols].head(5).to_string(index=False))


# ── Runner: Item 8 — S&P 500 Overview Screener ───────────────────────────────

def run_screener(max_results: int = 500) -> None:
    """S&P 500 overview: ticker, company, sector, market cap, P/E, price, change, volume."""
    os.makedirs(DIR_SCREENER, exist_ok=True)
    today_str  = datetime.datetime.utcnow().strftime("%Y%m%d")
    fetched_at = datetime.datetime.utcnow().isoformat()

    print(f"  Fetching S&P 500 overview (up to {max_results} rows)...")
    df = _scrape_screener(
        f"{BASE_URL}/screener.ashx",
        extra_params={"v": "111", "f": "idx_sp500"},
        max_results=max_results,
    )
    if df.empty:
        print("  No screener data.")
        return

    df["fetched_at"] = fetched_at
    df = _normalize_cols(df)
    df = _coerce_numerics(df)

    path = write_partitioned(df, DIR_SCREENER, f"finviz_screener_{today_str}.parquet")
    print(f"  Saved {len(df)} rows -> {path}")
    cols = [c for c in ["ticker", "company", "market_cap", "p_e", "price", "change", "volume"] if c in df.columns]
    print(df[cols].head(10).to_string(index=False))


# ── Runner: Item 9 — S&P 500 Financial Metrics ───────────────────────────────

def run_financials(max_results: int = 500) -> None:
    """S&P 500 financial metrics: ROA, ROE, ROIC, margins, current/quick ratios, debt/equity."""
    os.makedirs(DIR_FINANCIALS, exist_ok=True)
    today_str  = datetime.datetime.utcnow().strftime("%Y%m%d")
    fetched_at = datetime.datetime.utcnow().isoformat()

    print(f"  Fetching S&P 500 financial metrics (up to {max_results} rows)...")
    df = _scrape_screener(
        f"{BASE_URL}/screener.ashx",
        extra_params={"v": "161", "f": "idx_sp500"},
        max_results=max_results,
    )
    if df.empty:
        print("  No financials data.")
        return

    df["fetched_at"] = fetched_at
    df = _normalize_cols(df)
    df = _coerce_numerics(df)

    path = write_partitioned(df, DIR_FINANCIALS, f"finviz_financials_{today_str}.parquet")
    print(f"  Saved {len(df)} rows -> {path}")
    cols = [c for c in ["ticker", "market_cap", "roa", "roe", "roic", "gross_m", "oper_m", "profit_m"] if c in df.columns]
    print(df[cols].head(10).to_string(index=False))


# ── Runner: Item 10 — Insider Trading ────────────────────────────────────────

def run_insider(pages: int = 10) -> None:
    """Insider buy/sell/option-exercise transactions with $ values from SEC Form 4."""
    os.makedirs(DIR_INSIDER, exist_ok=True)
    today_str  = datetime.datetime.utcnow().strftime("%Y%m%d")
    fetched_at = datetime.datetime.utcnow().isoformat()

    print(f"  Fetching insider transactions ({pages} pages × {PAGE_SIZE} rows)...")
    url    = f"{BASE_URL}/insidertrading.ashx"
    frames: list[pd.DataFrame] = []

    for page in range(1, pages + 1):
        params = {"tc": page} if page > 1 else None
        resp   = _get(url, params=params)
        if resp is None:
            break

        df = _find_table_with_col(resp.text, "Owner")
        if df.empty:
            break

        # Finviz insider table has "Ticker" and "Owner" in the same table
        if "Ticker" not in df.columns:
            break

        frames.append(df)
        print(f"    Page {page}: {len(df)} rows")
        time.sleep(REQUEST_DELAY)

    if not frames:
        print("  No insider data.")
        return

    combined = pd.concat(frames, ignore_index=True)
    combined["fetched_at"] = fetched_at
    combined = _normalize_cols(combined)

    # Normalise Finviz's punctuation-heavy column names after lowercasing
    rename_map = {
        "_shares":       "shares",
        "value_":        "value_usd",
        "_shares_total": "shares_total",
        "shares_1":      "shares",
        "value_1":       "value_usd",
        "_shares_1":     "shares_total",
    }
    combined = combined.rename(columns={k: v for k, v in rename_map.items() if k in combined.columns})

    for col in ("cost", "shares", "value_usd", "shares_total"):
        if col in combined.columns:
            combined[col] = pd.to_numeric(
                combined[col].astype(str).str.replace(r"[,$]", "", regex=True),
                errors="coerce",
            )

    path = write_partitioned(combined, DIR_INSIDER, f"finviz_insider_{today_str}.parquet")
    print(f"  Saved {len(combined)} rows -> {path}")
    cols = [c for c in ["ticker", "owner", "relationship", "date", "transaction", "value_usd"] if c in combined.columns]
    print(combined[cols].head(10).to_string(index=False))


# ── Runner: Items 11-15 — Groups (sector/industry/country/valuation) ─────────

def run_groups() -> None:
    """
    Sector, industry, country performance + valuation by group.

    View codes:
      v=130 — Performance (week/month/quarter/half/year/YTD returns)
      v=140 — Valuation   (P/E, Fwd P/E, PEG, P/S, P/B, P/FCF, div yield, analyst rec)
    """
    today_str  = datetime.datetime.utcnow().strftime("%Y%m%d")
    fetched_at = datetime.datetime.utcnow().isoformat()

    # (g_param, v_param, out_dir, group_type, filename_prefix or None=valuation)
    configs = [
        ("sector",   "130", DIR_SECTOR,    "sector",   "finviz_sector_perf"),
        ("industry", "130", DIR_INDUSTRY,  "industry", "finviz_industry_perf"),
        ("country",  "130", DIR_COUNTRY,   "country",  "finviz_country_perf"),
        ("sector",   "140", DIR_VALUATION, "sector",   None),
        ("industry", "140", DIR_VALUATION, "industry", None),
    ]

    valuation_frames: list[pd.DataFrame] = []

    for g_param, v_param, out_dir, group_type, fname_prefix in configs:
        os.makedirs(out_dir, exist_ok=True)
        label = f"{group_type} (v={v_param})"
        print(f"  [{label}]", end=" ", flush=True)

        resp = _get(f"{BASE_URL}/groups.ashx", params={"g": g_param, "v": v_param})
        if resp is None:
            print("FAILED")
            continue

        df = _find_table_with_col(resp.text, "Name")
        if df.empty:
            print("no data")
            continue

        df["group_type"] = group_type
        df["fetched_at"] = fetched_at
        df = _normalize_cols(df)
        df = _coerce_numerics(df)
        print(f"{len(df)} rows")

        if fname_prefix is None:
            valuation_frames.append(df)
        else:
            path = write_partitioned(df, out_dir, f"{fname_prefix}_{today_str}.parquet")
            print(f"    -> {path}")
            perf_cols = [c for c in ["name", "change", "week", "month", "ytd"] if c in df.columns]
            if perf_cols:
                print(df[perf_cols].head(5).to_string(index=False))

        time.sleep(REQUEST_DELAY)

    if valuation_frames:
        combined = pd.concat(valuation_frames, ignore_index=True)
        path = write_partitioned(
            combined, DIR_VALUATION, f"finviz_group_valuation_{today_str}.parquet"
        )
        print(f"\n  Saved group valuation ({len(combined)} rows, sector + industry) -> {path}")
        val_cols = [c for c in ["name", "group_type", "p_e", "forward_p_e", "p_s", "dividend"] if c in combined.columns]
        if val_cols:
            print(combined[val_cols].head(10).to_string(index=False))


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Finviz scraper pipeline (no API key required)")
    parser.add_argument(
        "--only",
        default="all",
        help=(
            "Comma-separated subset to run: movers,screener,financials,insider,groups "
            "(default: all)"
        ),
    )
    parser.add_argument(
        "--max-screener-results",
        type=int,
        default=500,
        help="Max rows for screener and financials (default: 500)",
    )
    parser.add_argument(
        "--insider-pages",
        type=int,
        default=10,
        help="Pages of insider transactions to fetch, 20 rows/page (default: 10)",
    )
    args    = parser.parse_args()
    run_set = {s.strip().lower() for s in args.only.split(",")}
    run_all = "all" in run_set

    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*60}\n  Finviz Pipeline — {ts}\n{'='*60}")

    if run_all or "movers" in run_set:
        print("\n[Items 1-7] Market Movers")
        run_movers()

    if run_all or "screener" in run_set:
        print("\n[Item 8] S&P 500 Overview Screener")
        run_screener(max_results=args.max_screener_results)

    if run_all or "financials" in run_set:
        print("\n[Item 9] S&P 500 Financial Metrics")
        run_financials(max_results=args.max_screener_results)

    if run_all or "insider" in run_set:
        print("\n[Item 10] Insider Trading")
        run_insider(pages=args.insider_pages)

    if run_all or "groups" in run_set:
        print("\n[Items 11-15] Sector / Industry / Country / Valuation Groups")
        run_groups()

    print("\n--- FINVIZ PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
