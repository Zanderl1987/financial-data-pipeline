#!/usr/bin/env python3
"""
ETF Holdings Pipeline (SecuritiesDB, keyless).

Downloads ETF holdings with per-holding quant scores (Piotroski F-Score,
Altman Z-Score, market cap, sector) from SecuritiesDB's free REST API
(https://securitiesdb.com). No API key required.

API returns the top-100 holdings by weight per ETF (no pagination), plus
sector breakdown and fund AUM. Holdings are refreshed daily from SEC N-PORT
filings; quant scores recomputed after each holdings update.

Outputs:
  storage/raw/etf_holdings/year=YYYY/month=MM/etf_holdings_{mode}_{date}.parquet
  CATALOG table: etf_holdings
  Columns: snapshot_date, fund_ticker, fund_name, holding_ticker,
           holding_name, weight_pct, sector, market_cap, piotroski_f,
           altman_z, source, fetched_at

CLI:
  python etf_holdings_pipeline.py                  # all 119 funds in DEFAULT_UNIVERSE
  python etf_holdings_pipeline.py --tickers VTI SPY  # subset
"""

import argparse
import datetime
import json
import os
import time

import pandas as pd
import requests

from storage_utils import write_partitioned

BASE_DIR = "storage/raw/etf_holdings"
BASE_URL = "https://securitiesdb.com/api/v1/etfs/{ticker}/holdings"
SOURCE = "securitiesdb"
SNAPSHOT_DATE = datetime.date.today().isoformat()

# 119 ETF tickers -> full fund name. This universe is a snapshot of what was
# originally published on HuggingFace as `etf_holdings`; extend freely.
ETF_NAME_MAP = {
    "ACWI": "iShares MSCI ACWI ETF",
    "AGG": "iShares Core US Aggregate Bond ETF",
    "ARKF": "ARK Fintech Innovation ETF",
    "ARKG": "ARK Genomic Revolution ETF",
    "ARKK": "ARK Innovation ETF",
    "ARKQ": "ARK Autonomous Technology & Robotics ETF",
    "ARKW": "ARK Next Generation Internet ETF",
    "ARKX": "ARK Space Exploration & Innovation ETF",
    "BIL": "SPDR Bloomberg 1-3 Month T-Bill ETF",
    "BOTZ": "Global X Robotics & AI ETF",
    "BWX": "SPDR Bloomberg International Treasury Bond ETF",
    "DGRO": "iShares Core Dividend Growth ETF",
    "DGRW": "WisdomTree US Dividend Growth ETF",
    "DIA": "SPDR Dow Jones Industrial Average ETF",
    "DVY": "iShares Select Dividend ETF",
    "EEM": "iShares MSCI Emerging Markets ETF",
    "EFA": "iShares MSCI EAFE ETF",
    "EMB": "iShares JP Morgan USD Emerging Markets Bond ETF",
    "HDV": "iShares Core High Dividend ETF",
    "HYG": "iShares iBoxx High Yield Corporate Bond ETF",
    "IBB": "iShares Biotechnology ETF",
    "IEF": "iShares 7-10 Year Treasury Bond ETF",
    "IEFA": "iShares Core MSCI EAFE ETF",
    "IEMG": "iShares Core MSCI Emerging Markets ETF",
    "IGV": "iShares Expanded Tech-Software Sector ETF",
    "IHI": "iShares US Medical Devices ETF",
    "IJH": "iShares Core S&P Mid-Cap ETF",
    "IJR": "iShares Core S&P Small-Cap ETF",
    "ITA": "iShares US Aerospace & Defense ETF",
    "ITOT": "iShares Core S&P Total US Stock Market ETF",
    "IVV": "iShares Core S&P 500 ETF",
    "IWB": "iShares Russell 1000 ETF",
    "IWD": "iShares Russell 1000 Value ETF",
    "IWF": "iShares Russell 1000 Growth ETF",
    "IWM": "iShares Russell 2000 ETF",
    "IWN": "iShares Russell 2000 Value ETF",
    "IWO": "iShares Russell 2000 Growth ETF",
    "IWR": "iShares Russell Mid-Cap ETF",
    "IXUS": "iShares Core MSCI Total Intl Stock ETF",
    "IYE": "iShares US Energy ETF",
    "IYF": "iShares US Financials ETF",
    "IYJ": "iShares US Industrials ETF",
    "IYK": "iShares US Consumer Staples ETF",
    "IYR": "iShares US Real Estate ETF",
    "JEPI": "JPMorgan Equity Premium Income ETF",
    "JEPQ": "JPMorgan Nasdaq Equity Premium Income ETF",
    "JNK": "SPDR Bloomberg High Yield Bond ETF",
    "JPST": "JPMorgan Ultra-Short Income ETF",
    "KBE": "SPDR S&P Bank ETF",
    "KRE": "SPDR S&P Regional Banking ETF",
    "LIT": "Global X Lithium & Battery Tech ETF",
    "LQD": "iShares iBoxx Investment Grade Corporate Bond ETF",
    "MTUM": "iShares MSCI USA Momentum Factor ETF",
    "MUB": "iShares National Muni Bond ETF",
    "QQQ": "Invesco QQQ Trust",
    "QUAL": "iShares MSCI USA Quality Factor ETF",
    "SCHA": "Schwab US Small-Cap ETF",
    "SCHB": "Schwab US Broad Market ETF",
    "SCHD": "Schwab US Dividend Equity ETF",
    "SCHE": "Schwab Emerging Markets Equity ETF",
    "SCHF": "Schwab International Equity ETF",
    "SCHG": "Schwab US Large-Cap Growth ETF",
    "SCHH": "Schwab US REIT ETF",
    "SCHV": "Schwab US Large-Cap Value ETF",
    "SCHX": "Schwab US Large-Cap ETF",
    "SDY": "SPDR S&P Dividend ETF",
    "SHY": "iShares 1-3 Year Treasury Bond ETF",
    "SIZE": "iShares MSCI USA Size Factor ETF",
    "SMH": "VanEck Semiconductor ETF",
    "SOXX": "iShares PHLX Semiconductor Sector ETF",
    "SPDW": "SPDR Portfolio Developed World ex-US ETF",
    "SPEM": "SPDR Portfolio Emerging Markets ETF",
    "SPLG": "SPDR Portfolio S&P 500 ETF",
    "SPTM": "SPDR Total Stock Market ETF",
    "SPY": "SPDR S&P 500 ETF Trust",
    "SPYD": "SPDR Portfolio S&P 500 High Dividend ETF",
    "SPYG": "SPDR Portfolio S&P 500 Growth ETF",
    "SPYV": "SPDR Portfolio S&P 500 Value ETF",
    "TIP": "iShares TIPS Bond ETF",
    "TLT": "iShares 20+ Year Treasury Bond ETF",
    "USMV": "iShares MSCI USA Min Vol Factor ETF",
    "VAW": "Vanguard Materials ETF",
    "VBK": "Vanguard Small-Cap Growth ETF",
    "VBR": "Vanguard Small-Cap Value ETF",
    "VCR": "Vanguard Consumer Discretionary ETF",
    "VDE": "Vanguard Energy ETF",
    "VEA": "Vanguard FTSE Developed Markets ETF",
    "VEU": "Vanguard FTSE All-World ex-US ETF",
    "VFH": "Vanguard Financials ETF",
    "VGT": "Vanguard Information Technology ETF",
    "VHT": "Vanguard Health Care ETF",
    "VIG": "Vanguard Dividend Appreciation ETF",
    "VIS": "Vanguard Industrials ETF",
    "VLUE": "iShares MSCI USA Value Factor ETF",
    "VNQ": "Vanguard Real Estate ETF",
    "VOO": "Vanguard S&P 500 ETF",
    "VOX": "Vanguard Communication Services ETF",
    "VPU": "Vanguard Utilities ETF",
    "VTI": "Vanguard Total Stock Market ETF",
    "VTV": "Vanguard Value ETF",
    "VUG": "Vanguard Growth ETF",
    "VWO": "Vanguard FTSE Emerging Markets ETF",
    "VXUS": "Vanguard Total International Stock ETF",
    "VYM": "Vanguard High Dividend Yield ETF",
    "VYMI": "Vanguard International High Dividend Yield ETF",
    "XAR": "SPDR S&P Aerospace & Defense ETF",
    "XBI": "SPDR S&P Biotech ETF",
    "XLB": "Materials Select Sector SPDR ETF",
    "XLC": "Communication Services Select Sector SPDR ETF",
    "XLE": "Energy Select Sector SPDR ETF",
    "XLF": "Financial Select Sector SPDR ETF",
    "XLI": "Industrial Select Sector SPDR ETF",
    "XLK": "Technology Select Sector SPDR ETF",
    "XLP": "Consumer Staples Select Sector SPDR ETF",
    "XLRE": "Real Estate Select Sector SPDR ETF",
    "XLU": "Utilities Select Sector SPDR ETF",
    "XLV": "Health Care Select Sector SPDR ETF",
    "XLY": "Consumer Discretionary Select Sector SPDR ETF",
    "XOP": "SPDR S&P Oil & Gas Exploration & Production ETF",
}

DEFAULT_UNIVERSE = sorted(ETF_NAME_MAP)


def _fetch_holdings(ticker: str) -> list[dict]:
    """Fetch holdings for one ETF with retry/backoff on transient errors."""
    url = BASE_URL.format(ticker=ticker)
    for attempt in range(4):
        try:
            resp = requests.get(url, timeout=60, headers={"User-Agent": "financial-data-pipeline"})
            if resp.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            resp.raise_for_status()
            payload = resp.json().get("data", {})
            return payload.get("holdings", [])
        except (requests.RequestException, ValueError) as exc:
            if attempt == 3:
                print(f"  WARN {ticker}: {exc}")
                return []
            time.sleep(2 * (attempt + 1))
    return []


def _one_etf(ticker: str, fetched_at: str) -> pd.DataFrame:
    rows = _fetch_holdings(ticker)
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    out.rename(columns={"ticker": "holding_ticker", "name": "holding_name"}, inplace=True)
    out["fund_ticker"] = ticker
    out["fund_name"] = ETF_NAME_MAP.get(ticker, ticker)
    out["snapshot_date"] = pd.Timestamp(SNAPSHOT_DATE)
    out["source"] = SOURCE
    out["fetched_at"] = fetched_at
    cols = [
        "snapshot_date", "fund_ticker", "fund_name",
        "holding_ticker", "holding_name",
        "weight_pct", "sector", "market_cap", "piotroski_f", "altman_z",
        "source", "fetched_at",
    ]
    return out[cols]


def main() -> None:
    parser = argparse.ArgumentParser(description="ETF holdings pipeline (SecuritiesDB, keyless)")
    parser.add_argument("--tickers", nargs="+", default=None,
                        help="ETF tickers to fetch (default: all in DEFAULT_UNIVERSE)")
    parser.add_argument("--backfill", action="store_true",
                        help="Accepted for run_all parity; no-op (API is snapshot-only)")
    args = parser.parse_args()

    tickers = sorted(set(args.tickers or DEFAULT_UNIVERSE))
    unknown = [t for t in tickers if t not in ETF_NAME_MAP]
    for t in unknown:
        ETF_NAME_MAP.setdefault(t, t)

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode = "backfill" if args.backfill else "incremental"

    print(f"ETF Holdings Pipeline (SecuritiesDB)  mode={mode}  funds={len(tickers)}\n")

    frames = []
    for i, ticker in enumerate(tickers, start=1):
        df = _one_etf(ticker, fetched_at)
        if not df.empty:
            frames.append(df)
            print(f"  [{i}/{len(tickers)}] {ticker}: {len(df)} holdings")
        time.sleep(0.4)

    if not frames:
        print("  No holdings retrieved")
        return

    combined = pd.concat(frames, ignore_index=True)
    os.makedirs(BASE_DIR, exist_ok=True)
    path = write_partitioned(combined, BASE_DIR, f"etf_holdings_{mode}_{today_str}.parquet")
    print(f"  -> {path}  ({len(combined):,} rows, {combined['fund_ticker'].nunique()} funds)")

    print("\n--- ETF HOLDINGS PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
