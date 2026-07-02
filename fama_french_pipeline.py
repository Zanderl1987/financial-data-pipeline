#!/usr/bin/env python3
"""
Fama-French Factor Pipeline.

Downloads factor return data from Ken French's Data Library (Dartmouth):
  - 5-factor model: Mkt-RF, SMB, HML, RMW, CMA, RF (daily + monthly)
  - Momentum factor: UMD (daily + monthly)
  - 48 Industry portfolios: equal- and value-weighted returns (monthly)

No API key required. Files are ZIP archives containing space-delimited CSVs.

CLI:
  python fama_french_pipeline.py             # incremental (last 5 years)
  python fama_french_pipeline.py --backfill  # full history (1926+)

Outputs:
  storage/raw/fama_french/factors/year=YYYY/month=MM/ff_factors_{mode}_{date}.parquet
  storage/raw/fama_french/industry/year=YYYY/month=MM/ff_industry_{mode}_{date}.parquet
"""

import argparse
import datetime
import io
import zipfile

import pandas as pd
import requests
from storage_utils import write_partitioned

BASE_DIR   = "storage/raw/fama_french"
BASE_URL   = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp"

FACTOR_ZIPS = [
    ("F-F_Research_Data_5_Factors_2x3_CSV.zip",  "5factor"),
    ("F-F_Momentum_Factor_CSV.zip",               "momentum"),
]

INDUSTRY_ZIP = "48_Industry_Portfolios_CSV.zip"


def _download_zip(filename: str) -> bytes:
    url = f"{BASE_URL}/{filename}"
    print(f"  GET {url}")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content


def _read_csv_from_zip(content: bytes) -> dict[str, str]:
    """Return {csv_filename: text} for every .csv inside the zip."""
    result = {}
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for name in zf.namelist():
            if name.lower().endswith(".csv"):
                result[name] = zf.read(name).decode("latin-1")
    return result


def _parse_factor_csv(text: str, label: str) -> pd.DataFrame:
    """
    Parse a French factor CSV that may contain multiple frequency sections
    (Annual, Monthly, Daily) separated by blank lines / non-numeric headers.

    Returns long-format DataFrame with columns:
      date, frequency, factor, value
    """
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = []  # (frequency_label, data_lines)
    current_freq = "monthly"
    current_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if "annual" in lower and not any(c.isdigit() for c in stripped[:6]):
            if current_lines:
                sections.append((current_freq, current_lines))
            current_freq = "annual"
            current_lines = []
        elif "daily" in lower and not any(c.isdigit() for c in stripped[:6]):
            if current_lines:
                sections.append((current_freq, current_lines))
            current_freq = "daily"
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        sections.append((current_freq, current_lines))

    frames = []
    for freq, data_lines in sections:
        buf = "\n".join(data_lines)
        try:
            df = pd.read_csv(io.StringIO(buf), skipinitialspace=True)
        except Exception:
            continue

        df.columns = [c.strip() for c in df.columns]
        # First column is the date key
        date_col = df.columns[0]
        df = df.rename(columns={date_col: "period_key"})
        df["period_key"] = df["period_key"].astype(str).str.strip()

        # Drop non-data rows (copyright lines, etc.)
        df = df[df["period_key"].str.match(r"^\d{4,8}$", na=False)].copy()
        if df.empty:
            continue

        # Parse date from period_key
        if freq == "daily":
            df["date"] = pd.to_datetime(df["period_key"], format="%Y%m%d", errors="coerce")
        elif freq == "annual":
            df["date"] = pd.to_datetime(df["period_key"] + "0101", format="%Y%m%d", errors="coerce")
        else:
            df["date"] = pd.to_datetime(df["period_key"] + "01", format="%Y%m%d", errors="coerce")

        df = df.dropna(subset=["date"])
        df["frequency"] = freq
        df["source"] = label

        # Convert factor columns to numeric (they're in percent; keep as-is)
        value_cols = [c for c in df.columns if c not in ("period_key", "date", "frequency", "source")]
        for col in value_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # Melt to long format
        id_vars = ["date", "frequency", "source"]
        melted = df[id_vars + value_cols].melt(
            id_vars=id_vars, var_name="factor", value_name="value"
        )
        melted = melted.dropna(subset=["value"])
        frames.append(melted)

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _parse_industry_csv(text: str) -> pd.DataFrame:
    """
    Parse the 48 Industry Portfolios CSV.
    Contains multiple sections: Average Value Weighted Returns (monthly/annual/daily),
    Average Equal Weighted Returns, etc.
    Returns long-format: date, frequency, weighting, industry, return_pct
    """
    lines = text.splitlines()
    sections: list[tuple[str, str, list[str]]] = []
    current_freq = "monthly"
    current_weight = "value_weighted"
    current_is_returns = False
    current_lines: list[str] = []
    header_cols: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()

        # Section titles (e.g. "Average Value Weighted Returns -- Monthly")
        # are prose with few/no commas. The column-header row itself
        # (",Agric,Food,Soda,...") also has no digits in the first 6 chars,
        # so it must be excluded here via a comma-count check, or it gets
        # misdetected as a section title and header_cols never gets set.
        is_section_header = (
            not any(c.isdigit() for c in stripped[:6])
            and len(stripped) > 10
            and stripped.count(",") < 3
        )

        if is_section_header:
            if current_lines and header_cols and current_is_returns:
                sections.append((current_freq, current_weight, header_cols, current_lines))
            current_lines = []
            header_cols = []
            current_is_returns = "returns" in lower
            if "annual" in lower:
                current_freq = "annual"
            elif "daily" in lower:
                current_freq = "daily"
            else:
                current_freq = "monthly"
            current_weight = "equal_weighted" if "equal" in lower else "value_weighted"
        elif not header_cols and not stripped[0].isdigit():
            header_cols = [c.strip() for c in line.split(",") if c.strip()]
        else:
            current_lines.append(line)

    if current_lines and header_cols and current_is_returns:
        sections.append((current_freq, current_weight, header_cols, current_lines))

    frames = []
    for freq, weight, cols, data_lines in sections:
        buf = "\n".join(data_lines)
        try:
            df = pd.read_csv(io.StringIO(buf), header=None, skipinitialspace=True)
        except Exception:
            continue
        if df.shape[1] < 2:
            continue
        # Trim columns to match header
        if len(cols) == df.shape[1] - 1:
            cols = ["period_key"] + cols
        elif len(cols) != df.shape[1]:
            cols = (["period_key"] + [f"ind_{i}" for i in range(df.shape[1] - 1)])
        df.columns = cols[:df.shape[1]]

        df["period_key"] = df["period_key"].astype(str).str.strip()
        df = df[df["period_key"].str.match(r"^\d{4,8}$", na=False)].copy()
        if df.empty:
            continue

        if freq == "daily":
            df["date"] = pd.to_datetime(df["period_key"], format="%Y%m%d", errors="coerce")
        elif freq == "annual":
            df["date"] = pd.to_datetime(df["period_key"] + "0101", format="%Y%m%d", errors="coerce")
        else:
            df["date"] = pd.to_datetime(df["period_key"] + "01", format="%Y%m%d", errors="coerce")

        df = df.dropna(subset=["date"])
        industry_cols = [c for c in df.columns if c not in ("period_key", "date")]
        for col in industry_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        melted = df[["date"] + industry_cols].melt(
            id_vars=["date"], var_name="industry", value_name="return_pct"
        )
        melted["frequency"] = freq
        melted["weighting"] = weight
        melted = melted.dropna(subset=["return_pct"])
        frames.append(melted)

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _filter_years(df: pd.DataFrame, cutoff_year: int | None) -> pd.DataFrame:
    if cutoff_year is None or df.empty:
        return df
    return df[df["date"].dt.year >= cutoff_year].copy()


def run_factors(mode: str, cutoff_year: int | None, today_str: str, fetched_at: str) -> None:
    import os
    os.makedirs(f"{BASE_DIR}/factors", exist_ok=True)
    frames = []
    for zip_name, label in FACTOR_ZIPS:
        try:
            content = _download_zip(zip_name)
            csvs = _read_csv_from_zip(content)
            for csv_name, text in csvs.items():
                print(f"    parsing {csv_name} ({label})")
                df = _parse_factor_csv(text, label)
                if not df.empty:
                    frames.append(df)
        except Exception as exc:
            print(f"  ERROR {zip_name}: {exc}")

    if not frames:
        print("  No factor data retrieved")
        return

    combined = pd.concat(frames, ignore_index=True)
    combined = _filter_years(combined, cutoff_year)
    combined["fetched_at"] = fetched_at
    path = write_partitioned(combined, f"{BASE_DIR}/factors",
                             f"ff_factors_{mode}_{today_str}.parquet")
    print(f"  -> {path}  ({len(combined):,} rows)\n")


def run_industry(mode: str, cutoff_year: int | None, today_str: str, fetched_at: str) -> None:
    import os
    os.makedirs(f"{BASE_DIR}/industry", exist_ok=True)
    try:
        content = _download_zip(INDUSTRY_ZIP)
        csvs = _read_csv_from_zip(content)
        frames = []
        for csv_name, text in csvs.items():
            print(f"    parsing {csv_name}")
            df = _parse_industry_csv(text)
            if not df.empty:
                frames.append(df)
    except Exception as exc:
        print(f"  ERROR {INDUSTRY_ZIP}: {exc}")
        return

    if not frames:
        print("  No industry data retrieved")
        return

    combined = pd.concat(frames, ignore_index=True)
    combined = _filter_years(combined, cutoff_year)
    combined["fetched_at"] = fetched_at
    path = write_partitioned(combined, f"{BASE_DIR}/industry",
                             f"ff_industry_{mode}_{today_str}.parquet")
    print(f"  -> {path}  ({len(combined):,} rows)\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fama-French factor + industry portfolio returns")
    parser.add_argument("--backfill", action="store_true", help="Full history (1926+)")
    args = parser.parse_args()

    now        = datetime.datetime.utcnow()
    today_str  = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode       = "backfill" if args.backfill else "incremental"
    cutoff     = None if args.backfill else (now.year - 5)

    print(f"Fama-French Pipeline  mode={mode}\n")

    print("[ff_factors]")
    run_factors(mode, cutoff, today_str, fetched_at)

    print("[ff_industry]")
    run_industry(mode, cutoff, today_str, fetched_at)

    print("--- FAMA-FRENCH PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
