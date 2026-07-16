"""
EIA Hourly Grid Pipeline -- demand, demand forecast, net generation, and
interchange from the EIA-930 Hourly Electric Grid Monitor.

Single table with all 4 metric types, 65+ balancing authorities, UTC and local time.

Supplements eia_pipeline.py, eia_expansion_pipeline.py, and eia_petng_prices_pipeline.py.
Uses the same EIA Open Data API v2.

CLI:
  python eia_hourly_grid_pipeline.py              # incremental (last 7 days)
  python eia_hourly_grid_pipeline.py --backfill   # full history from 2019
  python eia_hourly_grid_pipeline.py --backfill --start-year 2022  # partial backfill

Outputs:
  storage/raw/eia/hourly_grid/**/*.parquet
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

EIA_API_KEY = os.environ["EIA_API_KEY"]
EIA_BASE = "https://api.eia.gov/v2"

EIA_DIR = os.path.join("storage", "raw", "eia")
GRID_DIR = os.path.join(EIA_DIR, "hourly_grid")

REQUEST_INTERVAL = 0.25
MAX_RETRIES = 3
BACKOFF_SECONDS = 60
PAGE_SIZE = 5000

INCREMENTAL_DAYS = 7
BACKFILL_START_YEAR = 2019

# ---------------------------------------------------------------------------
# Metric types
# ---------------------------------------------------------------------------
METRIC_TYPES: dict[str, str] = {
    "D":  "Demand",
    "DF": "Day-ahead demand forecast",
    "NG": "Net generation",
    "TI": "Total interchange",
}

# ---------------------------------------------------------------------------
# All 65 balancing authorities (from EIA facet)
# ---------------------------------------------------------------------------
ALL_RESPONDENTS: dict[str, str] = {
    "AEC":  "PowerSouth Energy Cooperative",
    "AECI": "Associated Electric Cooperative, Inc.",
    "AVA":  "Avista Corporation",
    "AVRN": "Avangrid Renewables, LLC",
    "AZPS": "Arizona Public Service Company",
    "BANC": "Balancing Authority of Northern California",
    "BPAT": "Bonneville Power Administration",
    "CAL":  "California",
    "CAR":  "Carolinas",
    "CENT": "Central",
    "CHPD": "Public Utility District No. 1 of Chelan County",
    "CISO": "California Independent System Operator",
    "CPLE": "Duke Energy Progress East",
    "CPLW": "Duke Energy Progress West",
    "DEAA": "Arlington Valley, LLC",
    "DOPD": "PUD No. 1 of Douglas County",
    "DUK":  "Duke Energy Carolinas",
    "EEI":  "Electric Energy, Inc.",
    "EPE":  "El Paso Electric Company",
    "ERCO": "Electric Reliability Council of Texas, Inc.",
    "FLA":  "Florida",
    "FMPP": "Florida Municipal Power Pool",
    "FPC":  "Duke Energy Florida, Inc.",
    "FPL":  "Florida Power & Light Co.",
    "GCPD": "Public Utility District No. 2 of Grant County, Washington",
    "GVL":  "Gainesville Regional Utilities",
    "GWA":  "NaturEner Power Watch, LLC",
    "HGMA": "New Harquahala Generating Company, LLC",
    "HST":  "City of Homestead",
    "IID":  "Imperial Irrigation District",
    "IPCO": "Idaho Power Company",
    "ISNE": "ISO New England",
    "JEA":  "JEA",
    "LDWP": "Los Angeles Department of Water and Power",
    "LGEE": "LG&E and KU Services Company",
    "MIDA": "Mid-Atlantic",
    "MIDW": "Midwest",
    "MISO": "Midcontinent Independent System Operator, Inc.",
    "NE":   "New England",
    "NEVP": "Nevada Power Company",
    "NSB":  "Utilities Commission of New Smyrna Beach",
    "NW":   "Northwest",
    "NWMT": "NorthWestern Corporation",
    "NY":   "New York",
    "NYIS": "New York Independent System Operator",
    "PACE": "PacifiCorp East",
    "PACW": "PacifiCorp West",
    "PGE":  "Portland General Electric Company",
    "PJMI": "PJM Interconnection, LLC",
    "PJM":  "PJM Interconnection, LLC",
    "PNM":  "Public Service Company of New Mexico",
    "PSCO": "Public Service Company of Colorado",
    "PSEI": "Puget Sound Energy, Inc.",
    "SCEG": "Dominion Energy South Carolina, Inc.",
    "SCL":  "Seattle City Light",
    "SC":   "South Carolina Public Service Authority",
    "SE":   "Southeast",
    "SEC":  "Seminole Electric Cooperative",
    "SEPA": "Southeastern Power Administration",
    "SIKE": "Sikeston Board of Municipal Utilities",
    "SOCO": "Southern Company Services, Inc. - Trans",
    "SPA":  "Southwestern Power Administration",
    "SRP":  "Salt River Project Agricultural Improvement and Power District",
    "SW":   "Southwest",
    "SWPP": "Southwest Power Pool",
    "TAL":  "City of Tallahassee",
    "TEPC": "Tucson Electric Power",
    "TEC":  "Tampa Electric Company",
    "TEN":  "Tennessee",
    "TIDC": "Turlock Irrigation District",
    "TPWR": "City of Tacoma, Department of Public Utilities",
    "TVA":  "Tennessee Valley Authority",
    "US48": "United States Lower 48",
    "WACM": "Western Area Power Administration - Rocky Mountain Region",
    "WAUW": "Western Area Power Administration - Upper Great Plains West",
    "WALC": "Western Area Power Administration - Desert Southwest Region",
    "WWA":  "NaturEner Wind Watch, LLC",
    "YAD":  "Alcoa Power Generating, Inc. - Yadkin Division",
}

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_with_backoff(url: str, params: dict) -> requests.Response | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=60)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  429 from EIA -- backing off {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            else:
                print(f"  HTTP {r.status_code}: {r.text[:200]}")
                return None
        except requests.RequestException as e:
            print(f"  Request error (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)
    print(f"  Giving up after {MAX_RETRIES} attempts.")
    return None


# ---------------------------------------------------------------------------
# Fetch hourly grid data (single request, paginated)
# ---------------------------------------------------------------------------

def _fetch_hourly_grid(
    frequency: str,
    start_date: str | None = None,
    end_date: str | None = None,
    respondents: list[str] | None = None,
    types: list[str] | None = None,
    label: str = "",
) -> list[dict]:
    url = f"{EIA_BASE}/electricity/rto/region-data/data/"
    all_rows: list[dict] = []
    offset = 0

    while True:
        params: list[tuple[str, str]] = [
            ("api_key", EIA_API_KEY),
            ("data[]", "value"),
            ("frequency", frequency),
            ("sort[0][column]", "period"),
            ("sort[0][direction]", "asc"),
            ("offset", str(offset)),
            ("length", str(PAGE_SIZE)),
        ]

        if start_date:
            params.append(("start", start_date))
        if end_date:
            params.append(("end", end_date))

        if respondents:
            for r in respondents:
                params.append(("facets[respondent][]", r))
        if types:
            for t in types:
                params.append(("facets[type][]", t))

        r = _get_with_backoff(url, dict(params))
        if not r:
            break

        resp = r.json().get("response", {})
        data = resp.get("data", [])
        total = int(resp.get("total", 0))
        all_rows.extend(data)
        fetched = offset + len(data)

        if label:
            print(f"  {label}: {fetched}/{total} rows...", end="\r")

        if len(data) < PAGE_SIZE or fetched >= total:
            break
        offset += PAGE_SIZE
        time.sleep(REQUEST_INTERVAL)

    if label:
        print(f"  {label}: {len(all_rows)} rows.   ")
    return all_rows


# ---------------------------------------------------------------------------
# Build DataFrame
# ---------------------------------------------------------------------------

def _build_grid_df(rows: list[dict], frequency: str) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # Parse period based on frequency
    if frequency == "hourly":
        df["period"] = df["period"].str.replace("T", " ", regex=False)
        df["timestamp_utc"] = pd.to_datetime(df["period"], format="%Y-%m-%d %H", errors="coerce")
    else:
        df["timestamp_utc"] = pd.to_datetime(df["period"], errors="coerce")

    df["value"] = pd.to_numeric(df["value"], errors="coerce")

    # Rename columns
    df = df.rename(columns={
        "respondent": "region_code",
        "respondent-name": "region_name",
        "type": "metric_type",
        "type-name": "metric_name",
        "value-units": "units",
    })

    df["source"] = "EIA"
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()

    keep = [
        "timestamp_utc", "region_code", "region_name",
        "metric_type", "metric_name", "value", "units",
        "source", "fetched_at",
    ]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].dropna(subset=["timestamp_utc", "value"])

    return df.sort_values(["region_code", "metric_type", "timestamp_utc"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(backfill: bool = False, start_year: int = BACKFILL_START_YEAR) -> None:
    os.makedirs(GRID_DIR, exist_ok=True)

    now = datetime.datetime.utcnow()
    today = now.strftime("%Y%m%d")

    if backfill:
        start_date = f"{start_year}-01-01"
        mode_tag = "backfill"
        print(f"Mode: BACKFILL (from {start_date})")
    else:
        cutoff = now - datetime.timedelta(days=INCREMENTAL_DAYS)
        start_date = cutoff.strftime("%Y-%m-%d")
        mode_tag = "incremental"
        print(f"Mode: INCREMENTAL (from {start_date})")

    end_date = now.strftime("%Y-%m-%d")

    # Fetch UTC data (all regions, all types)
    print("\n--- Fetching UTC hourly grid data ---")
    rows_utc = _fetch_hourly_grid(
        frequency="hourly",
        start_date=start_date,
        end_date=end_date,
        respondents=list(ALL_RESPONDENTS.keys()),
        types=list(METRIC_TYPES.keys()),
        label="grid_utc",
    )

    df_utc = _build_grid_df(rows_utc, "hourly")
    if not df_utc.empty:
        path = write_partitioned(
            df_utc, GRID_DIR,
            f"eia_hourly_grid_utc_{mode_tag}_{today}.parquet",
        )
        n_regions = df_utc["region_code"].nunique()
        n_types = df_utc["metric_type"].nunique()
        print(f"[+] {path} | {len(df_utc):,} rows | {n_regions} regions | {n_types} types")
    else:
        print("[!] No UTC grid data returned.")

    time.sleep(REQUEST_INTERVAL)

    # Fetch local time data
    print("\n--- Fetching local-time hourly grid data ---")
    rows_local = _fetch_hourly_grid(
        frequency="local-hourly",
        start_date=start_date,
        end_date=end_date,
        respondents=list(ALL_RESPONDENTS.keys()),
        types=list(METRIC_TYPES.keys()),
        label="grid_local",
    )

    df_local = _build_grid_df(rows_local, "local-hourly")
    if not df_local.empty:
        df_local = df_local.rename(columns={"timestamp_utc": "timestamp_local"})
        path = write_partitioned(
            df_local, GRID_DIR,
            f"eia_hourly_grid_local_{mode_tag}_{today}.parquet",
        )
        n_regions = df_local["region_code"].nunique()
        n_types = df_local["metric_type"].nunique()
        print(f"[+] {path} | {len(df_local):,} rows | {n_regions} regions | {n_types} types")
    else:
        print("[!] No local-time grid data returned.")

    print("\n--- COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="EIA Hourly Grid Pipeline -- demand, forecast, generation, interchange"
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Fetch full history from EIA (default: last 7 days).",
    )
    parser.add_argument(
        "--start-year",
        type=int,
        default=BACKFILL_START_YEAR,
        help=f"Start year for backfill (default: {BACKFILL_START_YEAR}).",
    )
    args = parser.parse_args()
    main(backfill=args.backfill, start_year=args.start_year)
