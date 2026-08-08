#!/usr/bin/env python3
"""
AQR Factor Library Pipeline.

Downloads factor return series from AQR Capital Management's public
Data Library (https://www.aqr.com/Insights/Datasets). No API key.

Each data set is an Excel workbook whose first sheet contains preamble text
rows followed by a wide table: first column = DATE, remaining columns =
factor series. Returns are decimal excess returns (e.g. 0.01 = 1%).

Data sets pulled:
  - Value and Momentum Everywhere (VME): factor returns for 8 asset classes
    (stocks US/UK/Europe/Japan, equity index futures, bonds, FX, commodities)
    plus aggregate stock-selection / asset-allocation / all-assets factors.
  - Quality Minus Junk (QMJ): long/short quality factor for US + 23 markets.
  - Time Series Momentum (TSMOM): tsmom factors for all assets and by
    asset class (equities, FX, fixed income, commodities).

CLI:
  python aqr_factors_pipeline.py             # incremental (last 5 years)
  python aqr_factors_pipeline.py --backfill  # full history (1957+)

Outputs:
  storage/raw/aqr/factors/year=YYYY/month=MM/aqr_factors_{mode}_{date}.parquet
"""

import argparse
import datetime
import io
import os

import openpyxl
import pandas as pd
import requests

from storage_utils import write_partitioned

BASE_DIR = "storage/raw/aqr/factors"
BASE_URL = "https://www.aqr.com/-/media/AQR/Documents/Insights/Data-Sets"

DATASETS = [
    ("VME",   "Value-and-Momentum-Everywhere-Factors-Monthly.xlsx",  "VME Factors"),
    ("QMJ",   "Quality-Minus-Junk-Factors-Monthly.xlsx",              "QMJ Factors"),
    ("TSMOM", "Time-Series-Momentum-Factors-Monthly.xlsx",            "TSMOM Factors"),
]


def _download(name: str) -> bytes:
    url = f"{BASE_URL}/{name}"
    print(f"  GET {url}")
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    return resp.content


def _read_sheet_rows(content: bytes, sheet: str) -> list[list]:
    """Return all rows of the given sheet as a list of lists (values only)."""
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        ws = wb[sheet]
        rows = [list(row) for row in ws.iter_rows(values_only=True)]
    finally:
        wb.close()
    return rows


def _find_header_row(rows: list[list]) -> tuple[int, list[str]]:
    """
    Find the header row: the first row whose first non-empty cell is a
    non-numeric label ('DATE' or a factor name) and whose following row's
    first non-empty cell parses as a date. Some workbooks (TSMOM) leave the
    date column unlabeled, so the header's first cell is None.
    Returns (header_row_index, header_cols) where header_cols may contain
    None entries (e.g. an unlabeled date column or a trailing pad cell).
    """
    for i in range(len(rows) - 1):
        nonempty = [c for c in rows[i] if c is not None]
        if not nonempty:
            continue
        first = str(nonempty[0]).strip()
        if not first or first[0].isdigit():
            continue
        next_nonempty = [c for c in rows[i + 1] if c is not None]
        if not next_nonempty:
            continue
        try:
            pd.to_datetime(next_nonempty[0])
        except Exception:
            continue
        cols = [str(c).strip() if c is not None else None for c in rows[i]]
        return i, cols
    raise ValueError("could not locate AQR data table header row")


def _parse_workbook(content: bytes, sheet: str, label: str) -> pd.DataFrame:
    rows = _read_sheet_rows(content, sheet)
    header_idx, header_cols = _find_header_row(rows)
    width = len(header_cols)

    # Pad/truncate every data row to the header width.
    data = [r[:width] + [None] * (width - len(r)) for r in rows[header_idx + 1:]]

    # Column 0 is the date. It may be labeled 'DATE' (VME/QMJ) or unlabeled
    # (TSMOM, where header_cols[0] is None) — normalize both to "date".
    # Other blank header cells (trailing pads) get unique names and are dropped.
    cols = list(header_cols)
    cols[0] = "date"
    for i in range(1, len(cols)):
        if cols[i] is None:
            cols[i] = f"__pad{i}__"
    df = pd.DataFrame(data, columns=cols)

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).copy()

    factor_cols = [c for c in cols[1:] if not c.startswith("__pad")]
    for col in factor_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    melted = df[["date"] + factor_cols].melt(
        id_vars=["date"], var_name="factor", value_name="value"
    )
    melted = melted.dropna(subset=["value"])
    melted["source"] = label
    return melted[["date", "source", "factor", "value"]]


def _filter_years(df: pd.DataFrame, cutoff_year: int | None) -> pd.DataFrame:
    if cutoff_year is None or df.empty:
        return df
    return df[df["date"].dt.year >= cutoff_year].copy()


def main() -> None:
    parser = argparse.ArgumentParser(description="AQR factor library pipeline")
    parser.add_argument("--backfill", action="store_true", help="Full history (1957+)")
    args = parser.parse_args()

    now        = datetime.datetime.utcnow()
    today_str  = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode       = "backfill" if args.backfill else "incremental"
    cutoff     = None if args.backfill else (now.year - 5)

    print(f"AQR Factors Pipeline  mode={mode}\n")

    frames = []
    for label, filename, sheet in DATASETS:
        try:
            content = _download(filename)
            df = _parse_workbook(content, sheet, label)
            if not df.empty:
                frames.append(df)
                print(f"  {label}: {len(df):,} rows")
        except Exception as exc:
            print(f"  ERROR {filename}: {exc}")

    if not frames:
        print("  No AQR data retrieved")
        return

    combined = pd.concat(frames, ignore_index=True)
    combined = _filter_years(combined, cutoff)
    combined["fetched_at"] = fetched_at

    os.makedirs(BASE_DIR, exist_ok=True)
    path = write_partitioned(combined, BASE_DIR, f"aqr_factors_{mode}_{today_str}.parquet")
    print(f"  -> {path}  ({len(combined):,} rows)")

    print("\n--- AQR FACTORS PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
