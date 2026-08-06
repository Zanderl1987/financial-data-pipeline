"""
build_fundamentals_dataset.py — assemble the public HuggingFace
financial-fundamentals dataset snapshot from the curated fundamentals tables.

Implements the Option-D design (approved 2026-08-05): one coherent revision per
run containing 6 files —

  facts.parquet                       long atomic fact table (all periods)
  companies.parquet                   company master (CIK -> symbol, taxonomies)
  filings.parquet                     filing master (accession x fiscal period)
  financials_annual_latest.parquet    wide, pivoted, latest-filing-wins
  financials_quarterly_latest.parquet wide, pivoted, latest-filing-wins
  metrics.parquet                     static reference (concept mappings)
  README.md                           generated dataset description

All files are pushed to Hugging Face in a SINGLE atomic commit so every revision
of the dataset is internally consistent ("Living Databases" rule 1). Schema
changes are additive-only (rule 2): new columns are nullable additions, never
renames or drops of existing columns.

Reads from the curated snapshots (`storage/curated/fundamentals_{annual,
quarterly}/`) built by curated.py — run the pipeline + curated.py first.

Usage:
  C:\\ProgramData\\anaconda3\\python.exe build_fundamentals_dataset.py \
      [--snapshot-dir DIR] [--repo owner/financial-fundamentals] [--no-push]
"""

import argparse
import datetime
import json
import os
import sys

import pandas as pd
from dotenv import load_dotenv

from fundamentals_pipeline import CONCEPTS, IFRS_CONCEPTS

load_dotenv()

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
CURATED_DIR = os.path.join(REPO_ROOT, "storage", "curated")
DEFAULT_SNAPSHOT_DIR = os.path.join(REPO_ROOT, "storage", "hf_fundamentals_snapshot")

# Canonical fact columns, in order. Older curated snapshots (pre-2026-08-05)
# lack the new columns; they are added as all-null.
FACT_COLUMNS = [
    "period", "symbol", "cik", "entity_name", "metric", "concept", "unit",
    "value", "period_end", "start_date", "duration_days", "fiscal_year",
    "fiscal_period", "form", "filed", "frame", "accession_number",
    "taxonomy", "fetched_at",
]

METRIC_ORDER = list(CONCEPTS.keys())

METRIC_DESCRIPTIONS = {
    "revenue":            "Total revenue / gross sales for the period",
    "net_income":         "Net income (loss) attributable to the company",
    "eps_diluted":        "Diluted earnings per share",
    "eps_basic":          "Basic earnings per share",
    "gross_profit":       "Gross profit = revenue - cost of goods sold",
    "operating_income":   "Operating income (loss); pre-tax income for banks",
    "total_assets":       "Total assets at period end",
    "total_liabilities":  "Total liabilities at period end",
    "operating_cash_flow": "Net cash provided by (used in) operating activities",
    "shares_outstanding": "Common shares outstanding at period end",
}

FILES = [
    "facts.parquet",
    "companies.parquet",
    "filings.parquet",
    "financials_annual_latest.parquet",
    "financials_quarterly_latest.parquet",
    "metrics.parquet",
    "snapshot.json",
    "README.md",
]


def _read_curated(bucket: str) -> pd.DataFrame:
    path = os.path.join(CURATED_DIR, f"fundamentals_{bucket}", f"fundamentals_{bucket}.parquet")
    if not os.path.exists(path):
        print(f"  ! Missing curated file: {path}")
        return pd.DataFrame()
    df = pd.read_parquet(path)
    for col in FACT_COLUMNS:
        if col not in df.columns:
            df[col] = None
    return df


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Canonicalize dates to date-only strings and value to numeric."""
    for col in ("period_end", "start_date", "filed", "fetched_at"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df


def build_facts() -> pd.DataFrame:
    annual = _read_curated("annual")
    quarterly = _read_curated("quarterly")
    annual["period"] = "annual"
    quarterly["period"] = "quarterly"
    df = pd.concat([annual, quarterly], ignore_index=True)
    df = _normalize(df)
    return df[FACT_COLUMNS]


def build_companies(facts: pd.DataFrame) -> pd.DataFrame:
    def _first_nonempty(s):
        return next((v for v in s.dropna().astype(str) if v != ""), None)

    g = facts.groupby("cik", dropna=False)
    out = g.agg(
        symbol=("symbol", lambda s: _first_nonempty(s) or ""),
        entity_name=("entity_name", lambda s: _first_nonempty(s) or ""),
        taxonomy=("taxonomy", lambda s: ",".join(sorted({v for v in s.dropna()}))),
        forms=("form", lambda s: ",".join(sorted({v for v in s.dropna()}))),
        periods=("period", lambda s: ",".join(sorted({v for v in s.dropna()}))),
        n_facts=("metric", "size"),
        n_metrics=("metric", lambda s: s.nunique()),
        first_filed=("filed", lambda s: s.dropna().min() or None),
        last_filed=("filed", lambda s: s.dropna().max() or None),
        first_period_end=("period_end", lambda s: s.dropna().min() or None),
        last_period_end=("period_end", lambda s: s.dropna().max() or None),
    ).reset_index()
    return out


def build_filings(facts: pd.DataFrame) -> pd.DataFrame:
    key = ["accession_number", "period", "fiscal_year", "fiscal_period"]
    out = (
        facts.groupby(key, dropna=False)
        .agg(
            cik=("cik", "first"),
            symbol=("symbol", lambda s: next((v for v in s.dropna() if v != ""), "")),
            form=("form", lambda s: next((v for v in s.dropna() if v != ""), "")),
            filed=("filed", lambda s: s.dropna().max() or None),
            taxonomy=("taxonomy", lambda s: next((v for v in s.dropna()), "")),
            n_facts=("metric", "size"),
            metrics=("metric", lambda s: ",".join(sorted(set(s.dropna())))),
        )
        .reset_index()
    )
    return out


def _unit_rank(u) -> int:
    return {"USD": 0, "USD/shares": 1, "shares": 2}.get(u, 3)


def _wide_value_cols() -> list:
    return list(METRIC_ORDER)


def _wide_unit_cols() -> list:
    return [f"{m}_unit" for m in METRIC_ORDER]


def _wide_col_order() -> list:
    cols = ["symbol", "cik", "period_end", "fiscal_year", "fiscal_period",
            "form", "filed", "accession_number", "taxonomy"]
    for m in METRIC_ORDER:
        cols += [m, f"{m}_unit"]
    return cols


def build_wide(facts: pd.DataFrame, bucket: str) -> pd.DataFrame:
    """Wide latest-filing-wins pivot for one period bucket.

    Key = (symbol, period_end). For each key, the facts of the most recently
    FILED accession win; within that filing one value per metric survives
    (preferring USD / USD-per-share / shares units).
    """
    d = facts[facts["period"] == bucket].copy()
    if d.empty:
        return pd.DataFrame(columns=["symbol", "cik", "period_end", "fiscal_year",
                                     "fiscal_period", "form", "filed",
                                     "accession_number", "taxonomy"] + _wide_value_cols() + _wide_unit_cols())

    d["_unit_rank"] = d["unit"].map(_unit_rank)
    d["_filed"] = pd.to_datetime(d["filed"], errors="coerce")
    d["accession_number"] = d["accession_number"].fillna("").astype(str)
    d["_accn"] = d["accession_number"]

    # rank accessions per (symbol, period_end) by filing recency; keep the latest
    accn = (
        d.groupby(["symbol", "period_end", "accession_number"], dropna=False)
        .agg(_max_filed=("_filed", "max"))
        .reset_index()
    )
    accn = accn.sort_values(["symbol", "period_end", "_max_filed", "accession_number"],
                            na_position="first")
    accn["_n"] = accn.groupby(["symbol", "period_end"])["_max_filed"].transform("count")
    accn["_rank"] = accn.groupby(["symbol", "period_end"]).cumcount()
    keep = accn[accn["_rank"] == accn["_n"] - 1][["symbol", "period_end", "accession_number"]]

    chosen = d.merge(keep, on=["symbol", "period_end", "accession_number"], how="inner")
    chosen = chosen.sort_values(["symbol", "period_end", "metric", "_unit_rank"])
    chosen = chosen.drop_duplicates(["symbol", "period_end", "metric"], keep="first")

    meta = (
        chosen.sort_values(["symbol", "period_end", "_filed", "_accn"], na_position="first")
        .groupby(["symbol", "period_end"], dropna=False)
        .agg(
            cik=("cik", "first"),
            fiscal_year=("fiscal_year", "first"),
            fiscal_period=("fiscal_period", "first"),
            form=("form", "first"),
            filed=("filed", "first"),
            accession_number=("accession_number", "first"),
            taxonomy=("taxonomy", "first"),
        )
        .reset_index()
    )

    piv = chosen.pivot_table(
        index=["symbol", "period_end"],
        columns="metric",
        values="value",
        aggfunc="first",
    ).reset_index()
    wide = meta.merge(piv, on=["symbol", "period_end"], how="left")

    piv_unit = chosen.pivot_table(
        index=["symbol", "period_end"],
        columns="metric",
        values="unit",
        aggfunc="first",
    ).reset_index()
    wide = wide.merge(piv_unit, on=["symbol", "period_end"], how="left", suffixes=("", "_u"))
    for m in METRIC_ORDER:
        unit_col = f"{m}_unit"
        if f"{m}_u" in wide.columns:
            wide[unit_col] = wide[f"{m}_u"]
            wide = wide.drop(columns=[f"{m}_u"])

    for m in METRIC_ORDER:
        if m not in wide.columns:
            wide[m] = None
        if f"{m}_unit" not in wide.columns:
            wide[f"{m}_unit"] = ""
    return wide[_wide_col_order()]


def build_metrics() -> pd.DataFrame:
    rows = []
    for metric in METRIC_ORDER:
        rows.append({
            "metric": metric,
            "us_gaap_tags": ";".join(CONCEPTS[metric]),
            "ifrs_tags": ";".join(IFRS_CONCEPTS.get(metric, [])),
            "ifrs_supported": bool(IFRS_CONCEPTS.get(metric, [])),
            "description": METRIC_DESCRIPTIONS.get(metric, ""),
            "notes": (
                "No ifrs-full tag exists for EPS; IFRS filers report 8/10 metrics."
                if metric in ("eps_diluted", "eps_basic") else ""
            ),
        })
    return pd.DataFrame(rows)


def _write(df: pd.DataFrame, snapshot_dir: str, name: str):
    path = os.path.join(snapshot_dir, name)
    df.to_parquet(path, index=False, compression="snappy")
    print(f"  wrote {name}: {len(df):,} rows ({os.path.getsize(path) / 1024 / 1024:,.1f} MB)")
    return len(df)


def build_snapshot(snapshot_dir: str) -> dict:
    print("Building fundamentals dataset snapshot...")
    os.makedirs(snapshot_dir, exist_ok=True)

    facts = build_facts()
    n_facts = _write(facts, snapshot_dir, "facts.parquet")

    companies = build_companies(facts)
    n_companies = _write(companies, snapshot_dir, "companies.parquet")

    filings = build_filings(facts)
    n_filings = _write(filings, snapshot_dir, "filings.parquet")

    wide_annual = build_wide(facts, "annual")
    n_annual = _write(wide_annual, snapshot_dir, "financials_annual_latest.parquet")

    wide_quarterly = build_wide(facts, "quarterly")
    n_quarterly = _write(wide_quarterly, snapshot_dir, "financials_quarterly_latest.parquet")

    metrics = build_metrics()
    n_metrics = _write(metrics, snapshot_dir, "metrics.parquet")

    counts = {
        "built_at_utc": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "facts_rows": n_facts,
        "companies_rows": n_companies,
        "filings_rows": n_filings,
        "annual_latest_rows": n_annual,
        "quarterly_latest_rows": n_quarterly,
        "metrics_rows": n_metrics,
        "facts_symbols": int(facts["symbol"].nunique()) if n_facts else 0,
        "facts_taxonomies": ",".join(sorted(facts["taxonomy"].dropna().unique())),
        "facts_forms": ",".join(sorted(facts["form"].dropna().unique())),
    }
    with open(os.path.join(snapshot_dir, "snapshot.json"), "w") as f:
        json.dump(counts, f, indent=2)
    print(f"  wrote snapshot.json: {json.dumps(counts, indent=2)}")

    _write_readme(snapshot_dir, counts)

    # verify all rows survived the round trip before we push
    _check(facts, "facts")
    _check(wide_annual, "financials_annual_latest")
    _check(wide_quarterly, "financials_quarterly_latest")
    return counts


def _check(df: pd.DataFrame, name: str):
    if df.empty:
        print(f"  ! WARNING: {name} is empty")
        return
    if "symbol" in df.columns and df["symbol"].isna().any():
        print(f"  ! WARNING: {name} has null symbols")


def _write_readme(snapshot_dir: str, counts: dict):
    lines = [
        "# financial-fundamentals",
        "",
        "SEC EDGAR XBRL fundamentals for US-listed and foreign issuers, refreshed weekly",
        "from the financial-data-pipeline (`fundamentals_pipeline.py` + `curated.py` +",
        "`build_fundamentals_dataset.py`).",
        "",
        "Built: " + counts["built_at_utc"] + " UTC",
        "",
        "## Files",
        "",
        "| file | rows | grain |",
        "|------|------|-------|",
        f"| `facts.parquet` | {counts['facts_rows']:,} | one row per fact (cik, metric, period, unit) |",
        f"| `companies.parquet` | {counts['companies_rows']:,} | one row per CIK |",
        f"| `filings.parquet` | {counts['filings_rows']:,} | one row per accession x fiscal period |",
        f"| `financials_annual_latest.parquet` | {counts['annual_latest_rows']:,} | wide, latest filing per (symbol, fiscal year-end) |",
        f"| `financials_quarterly_latest.parquet` | {counts['quarterly_latest_rows']:,} | wide, latest filing per (symbol, quarter-end) |",
        f"| `metrics.parquet` | {counts['metrics_rows']:,} | reference: concept tag mappings |",
        "",
        "## Facts columns",
        "",
        "`period`, `symbol`, `cik`, `entity_name`, `metric`, `concept`, `unit`, `value`,",
        "`period_end`, `start_date`, `duration_days`, `fiscal_year`, `fiscal_period`,",
        "`form`, `filed`, `frame`, `accession_number`, `taxonomy`, `fetched_at`",
        "",
        "`taxonomy` is `us-gaap` (US domestic filers) or `ifrs-full` (foreign issuers).",
        "IFRS filers report 8/10 metrics — EPS has no ifrs-full tag. Wide tables are",
        "latest-filing-wins: the most recently filed accession per (symbol, period_end)",
        "restatement versions remain in `facts.parquet`.",
        "",
    ]
    with open(os.path.join(snapshot_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("  wrote README.md")


def hf_push_revision(snapshot_dir: str, repo_id: str, commit_message: str | None = None):
    """Push every snapshot file to HF in ONE atomic commit (one coherent revision)."""
    token = os.environ.get("HF_TOKEN")
    if not token:
        print("  HF_TOKEN not set — skipping upload. Add it to .env to enable.")
        return False
    try:
        from huggingface_hub import CommitOperationAdd, HfApi
    except ImportError:
        print("  huggingface_hub not installed. Run: pip install huggingface_hub")
        return False

    api = HfApi()
    api.create_repo(repo_id=repo_id, repo_type="dataset", exist_ok=True, token=token)

    operations = [
        CommitOperationAdd(path_in_repo=name, path_or_fileobj=os.path.join(snapshot_dir, name))
        for name in FILES
        if os.path.exists(os.path.join(snapshot_dir, name))
    ]
    message = commit_message or (
        "weekly fundamentals snapshot (us-gaap + ifrs-full, forms 10-K/10-Q/"
        "20-F/40-F/6-K/8-K, accession-tracked) — " + datetime.datetime.utcnow().strftime("%Y-%m-%d")
    )
    print(f"  Pushing {len(operations)} files -> {repo_id} in one commit...")
    api.create_commit(
        repo_id=repo_id,
        repo_type="dataset",
        operations=operations,
        commit_message=message,
        token=token,
    )
    print(f"  -> https://huggingface.co/datasets/{repo_id}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Build the HF financial-fundamentals dataset snapshot.")
    parser.add_argument("--snapshot-dir", default=DEFAULT_SNAPSHOT_DIR,
                        help="Directory to write the snapshot files into.")
    parser.add_argument("--repo", default=os.environ.get("HF_DATASET_REPO", ""),
                        help="HF dataset repo id (defaults to HF_DATASET_REPO env var).")
    parser.add_argument("--no-push", action="store_true",
                        help="Build the snapshot locally without pushing to Hugging Face.")
    args = parser.parse_args()

    counts = build_snapshot(args.snapshot_dir)

    if args.no_push:
        print("Skipping HF push (--no-push). Snapshot ready at:")
        print("  " + args.snapshot_dir)
        return 0
    if not args.repo:
        print("No --repo and no HF_DATASET_REPO in .env. Snapshot built, not pushed.")
        return 0
    ok = hf_push_revision(args.snapshot_dir, args.repo)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
