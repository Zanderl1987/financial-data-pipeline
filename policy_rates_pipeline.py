#!/usr/bin/env python3
"""
Central bank policy rates pipeline (BIS).

Downloads the BIS "Central bank policy rates" dataset (WS_CBPOL), which covers
38 jurisdictions plus the euro area, daily where the central bank publishes a
daily rate and monthly otherwise, with history back to the 1940s for some
countries.

Why this exists
---------------
Carry strategies are defined by the interest rate differential between
currencies. The store previously held US rates only, so any cross-currency
carry signal was unimplementable -- the strategy catalog's `fx-carry-trade`
adapter refuses to run for exactly this reason rather than substituting spot
drift, which would test momentum under the name of carry.

BIS publishes this free, with no key, as a single bulk CSV. It is the standard
academic source for policy rates precisely because it reconciles the definition
changes each central bank has made over time (the COMPILATION column carries
that history, and it is preserved here).

Shape
-----
The source CSV is wide: one row per (frequency, country), ~30,800 date columns
spanning both monthly ('1945-01') and daily ('1945-01-01') labels. This melts
it to long form and keeps only populated observations.

No API key required.

CLI:
  python policy_rates_pipeline.py              # daily series only (default)
  python policy_rates_pipeline.py --frequency M
  python policy_rates_pipeline.py --frequency both

Output:
  storage/raw/policy_rates/year=YYYY/month=MM/policy_rates_{freq}_{date}.parquet
"""

import argparse
import datetime
import io
import zipfile

import pandas as pd
import requests

from storage_utils import write_partitioned

BASE_DIR = "storage/raw/policy_rates"
BULK_URL = "https://data.bis.org/static/bulk/WS_CBPOL_csv_col.zip"
REQUEST_TIMEOUT = 300

# BIS uses ISO-3166 reference areas; downstream FX work keys on currency. XM is
# the euro area, which is the correct reference for EUR -- the individual legacy
# members (DE, FR, IT...) stop being meaningful policy rates after 1999 and are
# deliberately not mapped to EUR.
AREA_TO_CURRENCY = {
    "AR": "ARS", "AU": "AUD", "BR": "BRL", "CA": "CAD", "CH": "CHF",
    "CL": "CLP", "CN": "CNY", "CO": "COP", "CZ": "CZK", "DK": "DKK",
    "GB": "GBP", "HK": "HKD", "HR": "HRK", "HU": "HUF", "ID": "IDR",
    "IL": "ILS", "IN": "INR", "IS": "ISK", "JP": "JPY", "KR": "KRW",
    "KW": "KWD", "MA": "MAD", "MK": "MKD", "MX": "MXN", "MY": "MYR",
    "NO": "NOK", "NZ": "NZD", "PE": "PEN", "PH": "PHP", "PL": "PLN",
    "RO": "RON", "RS": "RSD", "RU": "RUB", "SA": "SAR", "SE": "SEK",
    "TH": "THB", "TR": "TRY", "US": "USD", "XM": "EUR", "ZA": "ZAR",
}

# Columns that describe the series rather than carrying an observation.
META_COLUMNS = [
    "FREQ", "Frequency", "REF_AREA", "Reference area", "TIME_FORMAT",
    "Time Format", "COMPILATION", "DECIMALS", "Decimals", "SOURCE_REF",
    "SUPP_INFO_BREAKS", "TITLE", "Series",
]


def fetch_bulk() -> pd.DataFrame:
    resp = requests.get(BULK_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    archive = zipfile.ZipFile(io.BytesIO(resp.content))
    name = archive.namelist()[0]
    with archive.open(name) as handle:
        return pd.read_csv(handle, low_memory=False, encoding="utf-8")


def _is_daily_label(label: str) -> bool:
    """BIS mixes 'YYYY-MM' and 'YYYY-MM-DD' columns in the same file."""
    return len(label) == 10 and label[4] == "-" and label[7] == "-"


def reshape(wide: pd.DataFrame, frequency: str) -> pd.DataFrame:
    """Melt the wide BIS table to one row per (currency, date)."""
    if frequency in ("D", "M"):
        wide = wide[wide["FREQ"] == frequency]
    if wide.empty:
        return pd.DataFrame()

    meta = [c for c in META_COLUMNS if c in wide.columns]
    date_columns = [c for c in wide.columns if c not in meta]

    # A daily series must not absorb the monthly-labelled columns, and vice
    # versa -- they are separate observations of the same rate at different
    # granularity, and melting both would duplicate every month.
    if frequency == "D":
        date_columns = [c for c in date_columns if _is_daily_label(c)]
    elif frequency == "M":
        date_columns = [c for c in date_columns if not _is_daily_label(c)]

    long = wide.melt(
        id_vars=meta, value_vars=date_columns, var_name="date", value_name="rate"
    )
    long["rate"] = pd.to_numeric(long["rate"], errors="coerce")
    long = long.dropna(subset=["rate"])
    if long.empty:
        return pd.DataFrame()

    long = long.rename(
        columns={
            "FREQ": "frequency",
            "REF_AREA": "ref_area",
            "Reference area": "country",
            "COMPILATION": "definition",
            "SOURCE_REF": "source",
        }
    )
    long["currency"] = long["ref_area"].map(AREA_TO_CURRENCY)

    # Monthly labels are period starts; normalize so every row is a real date.
    monthly = ~long["date"].str.len().eq(10)
    long.loc[monthly, "date"] = long.loc[monthly, "date"] + "-01"
    long["date"] = pd.to_datetime(long["date"], errors="coerce")
    long = long.dropna(subset=["date"])
    long["date"] = long["date"].dt.strftime("%Y-%m-%d")

    columns = ["date", "ref_area", "currency", "country", "frequency", "rate",
               "definition", "source"]
    for column in columns:
        if column not in long.columns:
            long[column] = pd.NA
    return long[columns].sort_values(["ref_area", "frequency", "date"])


def main() -> None:
    parser = argparse.ArgumentParser(description="BIS central bank policy rates")
    parser.add_argument(
        "--frequency",
        choices=["D", "M", "both"],
        default="D",
        help="daily (default), monthly, or both",
    )
    args = parser.parse_args()

    now = datetime.datetime.utcnow()
    fetched_at = now.isoformat()
    today_str = now.strftime("%Y%m%d")

    print(f"Policy Rates Pipeline (BIS WS_CBPOL)  frequency={args.frequency}\n")
    print("[policy_rates]")

    wide = fetch_bulk()
    print(f"  source: {wide.shape[0]} series x {wide.shape[1]:,} columns")

    frames = []
    for frequency in (["D", "M"] if args.frequency == "both" else [args.frequency]):
        frame = reshape(wide, frequency)
        if frame.empty:
            print(f"  {frequency}: no observations")
            continue
        countries = frame["ref_area"].nunique()
        unmapped = sorted(set(frame.loc[frame["currency"].isna(), "ref_area"]))
        print(
            f"  {frequency}: {len(frame):,} rows, {countries} reference areas, "
            f"{frame['date'].min()}..{frame['date'].max()}"
        )
        if unmapped:
            print(f"     unmapped reference areas (no currency assigned): {unmapped}")
        frames.append(frame)

    if not frames:
        print("  No data retrieved")
        return

    combined = pd.concat(frames, ignore_index=True)
    combined["fetched_at"] = fetched_at
    suffix = args.frequency.lower()
    path = write_partitioned(
        combined, BASE_DIR, f"policy_rates_{suffix}_{today_str}.parquet"
    )
    print(f"  -> {path}  ({len(combined):,} rows)")

    print("\n--- POLICY RATES PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
