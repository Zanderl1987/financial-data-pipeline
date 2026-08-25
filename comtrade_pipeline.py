#!/usr/bin/env python3
"""
UN Comtrade Trade Flow Pipeline — US imports/exports of battery materials and
advanced manufacturing components.

Uses the UN Comtrade API to fetch annual trade statistics by HS commodity code.
Two access modes:
  - No key:   public preview endpoint (recent 2-3 years, rate-limited ~60/hr)
  - With key: authenticated endpoint (full history to 1988, 500 req/day free tier)
  Register free at https://comtradeapi.un.org/ for an Ocp-Apim-Subscription-Key.
  Add COMTRADE_API_KEY to your .env file.

HS codes tracked:
  283691 — Lithium carbonates         (battery-grade cathode material)
  282520 — Lithium oxide/hydroxide    (battery material)
  810520 — Cobalt, unwrought          (NMC/NCA cathode precursor)
  260200 — Manganese ores             (LFP/NMC batteries, steel alloys)
  250410 — Natural graphite           (battery anode material)
  284690 — Rare earth compounds       (EV motor magnets — Nd, Pr, Dy)
  850760 — Lithium-ion batteries      (EV and grid storage)
  854231 — Processor/controller ICs   (semiconductors)
  854232 — Memory ICs                 (semiconductors)
   720829 — Steel flat-rolled HRC      (automotive body/frame)
   760110 — Aluminum unwrought         (EV body, battery enclosures)
   280429 — Helium                     (MRI, semiconductors, lifting/aerospace)

CLI:
  python comtrade_pipeline.py             # last 3 years, annual
  python comtrade_pipeline.py --backfill  # 2012 to present

Output:
  storage/raw/comtrade/comtrade_{mode}_{YYYYMMDD}.parquet
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

COMTRADE_KEY   = os.getenv("COMTRADE_API_KEY", "")
REPORTER_USA      = "842"   # M49 country code for the United States
PARTNER_WORLD     = "0"    # World total (sum of all partners)
BACKFILL_START    = 2012
INCREMENTAL_YEARS = 3

BASE_DIR = os.path.join("storage", "raw", "comtrade")

AUTH_URL        = "https://comtradeapi.un.org/data/v1/get/C/A/HS"
AUTH_INTERVAL   = 0.3
MAX_RETRIES     = 3
BACKOFF_SECONDS = 60

HS_CODES: dict[str, tuple[str, str]] = {
    "283691": ("Lithium Carbonates",            "battery_materials"),
    "282520": ("Lithium Oxide & Hydroxide",     "battery_materials"),
    "810520": ("Cobalt Unwrought",              "battery_materials"),
    "260200": ("Manganese Ores",                "ores"),
    "250410": ("Natural Graphite",              "battery_materials"),
    "284690": ("Rare Earth Compounds",          "battery_materials"),
    "850760": ("Lithium-Ion Batteries",         "batteries"),
    "854231": ("Processor & Controller ICs",    "semiconductors"),
    "854232": ("Memory ICs",                    "semiconductors"),
    "720829": ("Steel Flat-Rolled HRC",         "metals"),
    "760110": ("Aluminum Unwrought Non-Alloy",  "metals"),
    "280429": ("Helium",                        "industrial_gases"),
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(url: str, params: dict, headers: dict | None = None) -> dict | None:
    interval = AUTH_INTERVAL if COMTRADE_KEY else REQUEST_INTERVAL
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, headers=hdrs, timeout=60)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"    429 rate limit — backing off {wait}s")
                time.sleep(wait)
                continue
            if r.status_code in (401, 403):
                print(f"    HTTP {r.status_code}: check COMTRADE_API_KEY")
                return None
            print(f"    HTTP {r.status_code}: {r.text[:200]}")
            return None
        except requests.RequestException as e:
            print(f"    Request error (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)
    return None


# ---------------------------------------------------------------------------
# Data fetch — authenticated (new API)
# ---------------------------------------------------------------------------

def _fetch_auth(hs_code: str, year: int) -> list[dict]:
    """Fetch via authenticated Comtrade API — full data, all partners aggregated."""
    params = {
        "reporterCode": REPORTER_USA,
        "period":       str(year),
        "cmdCode":      hs_code,
        "flowCode":     "M,X",       # imports and exports
        "partnerCode":  PARTNER_WORLD,
        "partner2Code": "0",
        "customsCode":  "C00",
        "motCode":      "0",
        "breakdownMode": "plus",
        "includeDesc":  "true",
    }
    hdrs = {"Ocp-Apim-Subscription-Key": COMTRADE_KEY}
    data = _get(AUTH_URL, params, hdrs)
    if data is None:
        return []
    return data.get("data", []) or []




# ---------------------------------------------------------------------------
# Row normalisation
# ---------------------------------------------------------------------------

_RENAME = {
    "refPeriodId":    "period",
    "reporterCode":   "reporter_code",
    "reporterISO":    "reporter_iso",
    "reporterDesc":   "reporter",
    "flowCode":       "flow",
    "flowDesc":       "flow_desc",
    "partnerCode":    "partner_code",
    "partnerISO":     "partner_iso",
    "partnerDesc":    "partner",
    "cmdCode":        "hs_code",
    "cmdDesc":        "commodity_desc",
    "primaryValue":   "trade_value_usd",
    "netWgt":         "net_weight_kg",
    "qty":            "quantity",
    "qtyUnitCode":    "qty_unit_code",
    "qtyUnitAbbr":    "qty_unit",
    "isLeaf":         "is_leaf",
    "classificationCode": "classification",
}

def _normalise(rows: list[dict], hs_code: str, hs_name: str, category: str) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).rename(columns=_RENAME)
    df["hs_code"]   = hs_code
    df["hs_name"]   = hs_name
    df["category"]  = category
    # Keep a clean numeric year column. Named "obs_year" (not "year"): DuckDB's
    # hive_partitioning=True treats "year" as a reserved virtual column (from
    # storage/raw/.../year=YYYY/) and silently overwrites a same-named
    # physical column with the fetch year instead of the real trade year.
    if "period" in df.columns:
        df["obs_year"] = pd.to_numeric(df["period"], errors="coerce").astype("Int64")
    # Numeric values
    for col in ("trade_value_usd", "net_weight_kg", "quantity"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    keep = [c for c in [
        "hs_code", "hs_name", "category", "obs_year", "period",
        "reporter", "reporter_iso",
        "partner", "partner_iso",
        "flow", "flow_desc",
        "trade_value_usd", "net_weight_kg", "quantity", "qty_unit",
        "commodity_desc",
    ] if c in df.columns]
    return df[keep]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(backfill: bool = False) -> None:
    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    mode = "backfill" if backfill else "incremental"

    if not COMTRADE_KEY:
        print("ERROR: COMTRADE_API_KEY not set.")
        print("  Register free (B1 tier) at https://comtradeapi.un.org/")
        print("  Then add COMTRADE_API_KEY=<your-key> to .env")
        return

    if backfill:
        years = list(range(BACKFILL_START, now.year + 1))
    else:
        years = list(range(now.year - INCREMENTAL_YEARS, now.year + 1))

    print(f"UN Comtrade Pipeline  mode={mode}  years={years[0]}–{years[-1]}\n")
    os.makedirs(BASE_DIR, exist_ok=True)

    frames: list[pd.DataFrame] = []
    total = len(HS_CODES)
    interval = AUTH_INTERVAL

    for i, (hs_code, (hs_name, category)) in enumerate(HS_CODES.items(), 1):
        print(f"[{i}/{total}] HS {hs_code} — {hs_name}")
        commodity_frames: list[pd.DataFrame] = []

        for year in years:
            rows = _fetch_auth(hs_code, year)

            if rows:
                df_yr = _normalise(rows, hs_code, hs_name, category)
                if not df_yr.empty:
                    commodity_frames.append(df_yr)
                    print(f"    {year}: {len(df_yr)} rows", end="")
                    if "trade_value_usd" in df_yr.columns:
                        val = df_yr["trade_value_usd"].sum()
                        if pd.notna(val) and val > 0:
                            print(f"  (${val/1e6:,.1f}M total)", end="")
                    print()
            else:
                print(f"    {year}: no data")

            time.sleep(interval)

        if commodity_frames:
            frames.append(pd.concat(commodity_frames, ignore_index=True))
        else:
            print(f"  [!] No data for {hs_name}")

    if not frames:
        print("\nNo trade data fetched.")
        if not COMTRADE_KEY:
            print("  Public preview may not cover the requested year range.")
            print("  Set COMTRADE_API_KEY in .env for full access.")
        return

    combined = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates()
        .sort_values(["category", "hs_code", "obs_year", "flow"])
        .reset_index(drop=True)
    )
    combined["source"]     = "UN Comtrade"
    combined["fetched_at"] = now.isoformat()
    access_mode = "authenticated" if COMTRADE_KEY else "public_preview"
    combined["access_mode"] = access_mode

    path = write_partitioned(
        combined, BASE_DIR,
        f"comtrade_{mode}_{today_str}.parquet",
    )
    print(f"\n-> {path}")
    print(f"   {len(combined):,} rows | {combined['hs_code'].nunique()} HS codes "
          f"| {combined['obs_year'].nunique()} years")

    if "trade_value_usd" in combined.columns and not backfill:
        latest_year = combined["obs_year"].max()
        latest = combined[combined["obs_year"] == latest_year]
        total_val = latest["trade_value_usd"].sum()
        print(f"   Latest year ({latest_year}) total trade value: ${total_val/1e9:,.2f}B")

    print("\n--- COMTRADE PIPELINE COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="UN Comtrade — US trade flows for battery materials and components"
    )
    parser.add_argument("--backfill", action="store_true",
                        help=f"Fetch from {BACKFILL_START} to present (default: last {INCREMENTAL_YEARS} years)")
    args = parser.parse_args()
    main(backfill=args.backfill)
