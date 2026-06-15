import requests
import pandas as pd
import datetime
import time
import os
import argparse
from dotenv import load_dotenv

load_dotenv()

FRED_API_KEY = os.environ["FRED_API_KEY"]
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

COMMODITIES_DIR = os.path.join("storage", "raw", "commodities")
MACRO_DIR = os.path.join("storage", "raw", "macro")

# 120 req/min hard limit — 0.5s keeps us safely under
REQUEST_INTERVAL = 0.5
MAX_RETRIES = 3
BACKOFF_SECONDS = 60

# ---------------------------------------------------------------------------
# Series catalog
# Each entry: series_id, human name, frequency, unit, category
# ---------------------------------------------------------------------------

SERIES = {
    # --- Energy ---
    "DCOILWTICO":     ("WTI Crude Oil",               "daily",    "USD/barrel",   "energy"),
    "DCOILBRENTEU":   ("Brent Crude Oil",              "daily",    "USD/barrel",   "energy"),
    "DHHNGSP":        ("Henry Hub Natural Gas",        "daily",    "USD/MMBtu",    "energy"),
    "GASREGCOVW":     ("US Regular Gasoline (conv.)",  "weekly",   "USD/gallon",   "energy"),
    "GASDESW":        ("US On-Highway Diesel",         "weekly",   "USD/gallon",   "energy"),
    "DHOILNYH":       ("Heating Oil (New York)",       "daily",    "USD/gallon",   "energy"),

    # --- Agriculture (IMF Global Prices via FRED) ---
    "PMAIZMTUSDM":    ("Corn",                         "monthly",  "USD/MT",       "agriculture"),
    "PWHEAMTUSDM":    ("Wheat",                        "monthly",  "USD/MT",       "agriculture"),
    "PSOYBUSDM":      ("Soybeans",                     "monthly",  "USD/MT",       "agriculture"),
    "PCOTTINDUSDM":   ("Cotton",                       "monthly",  "USD/kg",       "agriculture"),
    "PSUGAISAUSDM":   ("Sugar",                        "monthly",  "USD/kg",       "agriculture"),
    "PCOFFOTMUSDM":   ("Coffee (Arabica)",             "monthly",  "USD/kg",       "agriculture"),

    # --- Metals ---
    "GOLDPMGBD228NLBM": ("Gold (London PM Fix)",       "daily",    "USD/troy oz",  "metals"),
    "PCOPPUSDM":      ("Copper",                       "monthly",  "USD/MT",       "metals"),
    "PPALAUSDM":      ("Palladium",                    "monthly",  "USD/troy oz",  "metals"),
    "PPLATINUMUSDM":  ("Platinum",                     "monthly",  "USD/troy oz",  "metals"),

    # --- Macro ---
    "CPIAUCSL":       ("CPI All Urban Consumers",      "monthly",  "Index",        "macro"),
    "PPIACO":         ("PPI All Commodities",          "monthly",  "Index",        "macro"),
    "GDP":            ("Gross Domestic Product",       "quarterly","Bil. USD",     "macro"),
    "GDPC1":          ("Real GDP",                     "quarterly","Bil. Chained", "macro"),
    "FEDFUNDS":       ("Federal Funds Rate",           "monthly",  "Percent",      "macro"),
    "DGS10":          ("10-Year Treasury Rate",        "daily",    "Percent",      "macro"),
    "DGS2":           ("2-Year Treasury Rate",         "daily",    "Percent",      "macro"),
    "T10YIE":         ("10-Year Breakeven Inflation",  "daily",    "Percent",      "macro"),
    "UNRATE":         ("Unemployment Rate",            "monthly",  "Percent",      "macro"),
    "M2SL":           ("M2 Money Supply",              "monthly",  "Bil. USD",     "macro"),
    "VIXCLS":         ("CBOE VIX",                     "daily",    "Index",        "macro"),
    "DTWEXBGS":       ("USD Trade-Weighted Index",     "daily",    "Index",        "macro"),
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def get_with_backoff(url, params):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  429 from FRED. Backing off {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            else:
                print(f"  HTTP {r.status_code} for {params.get('series_id')}: {r.text[:120]}")
                return None
        except requests.RequestException as e:
            print(f"  Request error (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)
    print(f"  Giving up after {MAX_RETRIES} attempts: {params.get('series_id')}")
    return None


# ---------------------------------------------------------------------------
# Fetch a single FRED series
# ---------------------------------------------------------------------------

def fetch_series(series_id, observation_start=None):
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "asc",
        "limit": 100000,
    }
    if observation_start:
        params["observation_start"] = observation_start

    r = get_with_backoff(FRED_BASE, params)
    if not r:
        return None

    observations = r.json().get("observations", [])
    if not observations:
        return None

    df = pd.DataFrame(observations)[["date", "value"]]
    # FRED encodes missing values as "."
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"])
    df["date"] = pd.to_datetime(df["date"])
    return df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(backfill=False):
    os.makedirs(COMMODITIES_DIR, exist_ok=True)
    os.makedirs(MACRO_DIR, exist_ok=True)

    if backfill:
        observation_start = None  # Fetch full history
        print("Mode: BACKFILL (full history)")
    else:
        # Incremental: last 90 days — covers monthly series + daily series updates
        observation_start = (datetime.datetime.utcnow() - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
        print(f"Mode: INCREMENTAL (from {observation_start})")

    commodity_frames = []
    macro_frames = []
    failed = []

    total = len(SERIES)
    for i, (series_id, (name, frequency, unit, category)) in enumerate(SERIES.items(), 1):
        print(f"[{i}/{total}] {series_id} — {name}...")
        df = fetch_series(series_id, observation_start)

        if df is None or df.empty:
            print(f"  No data returned.")
            failed.append(series_id)
            time.sleep(REQUEST_INTERVAL)
            continue

        df["series_id"] = series_id
        df["name"] = name
        df["frequency"] = frequency
        df["unit"] = unit
        df["category"] = category
        df["fetched_at"] = datetime.datetime.utcnow().isoformat()
        df = df.rename(columns={"value": "value"})

        if category == "macro":
            macro_frames.append(df)
        else:
            commodity_frames.append(df)

        time.sleep(REQUEST_INTERVAL)

    today = datetime.datetime.utcnow().strftime("%Y%m%d")
    mode_tag = "backfill" if backfill else "incremental"

    if commodity_frames:
        commodities_df = pd.concat(commodity_frames, ignore_index=True)
        path = os.path.join(COMMODITIES_DIR, f"commodities_{mode_tag}_{today}.parquet")
        commodities_df.to_parquet(path, index=False)
        print(f"\nCommodities → {path} ({len(commodities_df)} rows, {commodities_df['series_id'].nunique()} series)")

    if macro_frames:
        macro_df = pd.concat(macro_frames, ignore_index=True)
        path = os.path.join(MACRO_DIR, f"macro_{mode_tag}_{today}.parquet")
        macro_df.to_parquet(path, index=False)
        print(f"Macro       → {path} ({len(macro_df)} rows, {macro_df['series_id'].nunique()} series)")

    if failed:
        print(f"\nFailed/empty ({len(failed)}): {', '.join(failed)}")

    print("\n--- COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FRED commodity & macro pipeline")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Fetch full available history for all series (use on first run).",
    )
    args = parser.parse_args()
    main(backfill=args.backfill)
