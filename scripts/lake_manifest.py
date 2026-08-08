"""
Export a single JSON manifest describing the data lake: every CATALOG table's
category, row count, schema, size, date range, freshness, and source pipeline.

The local checkout's storage/ is code-only (empty .gitkeep placeholders — the
actual populated Parquet snapshot lives on Hugging Face, pushed by
upload_huggingface.py from wherever the pipelines actually ran). So:
  - category / pipeline-source / lineage metadata comes from THIS repo's code
    (query.py CATALOG comments, docs/PIPELINE_CATALOG.md, ANALYTICS_VIEWS SQL)
  - row counts / schema / size / date range / freshness come from live remote
    queries against the HF-hosted parquet files (DuckDB httpfs, footer-only —
    no full download).

Usage:
    python scripts/lake_manifest.py [output_path]

Re-run any time the HF dataset or this repo's CATALOG/docs change.
"""
import json
import re
import sys
import time
from pathlib import Path

import duckdb
import pandas as pd
import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import query as q  # noqa: E402

QUERY_PY = REPO / "query.py"
PIPELINE_CATALOG_MD = REPO / "docs" / "PIPELINE_CATALOG.md"

HF_DATASET = "ZanderL1337/financial-data-pipeline"
HF_TREE_API = f"https://huggingface.co/api/datasets/{HF_DATASET}/tree/main"
HF_RESOLVE = f"https://huggingface.co/datasets/{HF_DATASET}/resolve/main"

DATE_COL_PRIORITY = ["date", "snapshot_date", "report_date", "observation_date", "period"]
FRESHNESS_COL = "fetched_at"


# ---------------------------------------------------------------------------
# Local repo parsing: category / pipeline source / lineage (code, not data —
# still valid even though local storage/ has no actual files).
# ---------------------------------------------------------------------------

# query.py has no category comment above ~93 of its 233 CATALOG entries (everything
# from `signal_health` onward through `cfpb_complaints` was appended without headers).
# A naive "nearest preceding comment" parse dumps all of them under whatever header
# happened to precede that block ("Signal health monitor..."). Fix up with a
# longest-prefix-match against known source families so the dashboard's category
# breakdown isn't 40% wrong.
_UNHEADERED_BUCKET = "Signal health monitor (maintained backtest performance tracking)"
_PREFIX_FALLBACK = [
    ("fred_macro_", "FRED macro indicators"),
    ("fred_rates_gdp_", "FRED rates & GDP"),
    ("alpha_vantage_", "Alpha Vantage fundamentals"),
    ("coingecko_", "CoinGecko (extended)"),
    ("sec_edgar_", "SEC EDGAR filings & fundamentals"),
    ("bls_", "BLS labor market"),
    ("eia_", "EIA energy data"),
    ("finnhub_", "Finnhub fundamentals + market data"),
    ("tiingo_", "Tiingo prices + news"),
    ("treasury_", "US Treasury fiscal data"),
    ("cfpb_", "CFPB consumer finance complaints"),
]


def _prefix_category(name: str) -> str | None:
    for prefix, label in sorted(_PREFIX_FALLBACK, key=lambda p: -len(p[0])):
        if name.startswith(prefix):
            return label
    return None


def parse_categories() -> dict[str, str]:
    """Map each CATALOG table name to the nearest preceding '# -- Category --' comment,
    with a name-prefix fallback for the block of entries the source never labeled."""
    text = QUERY_PY.read_text(encoding="utf-8")
    start = text.index("CATALOG: dict[str, str] = {")
    end = text.index("\n}\n", start)
    block = text[start:end].splitlines()

    header_re = re.compile(r"#\s*[─┄┈┌─-]{2,}\s*(.+?)\s*[─┄┈-]{2,}")
    key_re = re.compile(r'^\s*"([a-zA-Z0-9_]+)":')

    current = "Uncategorized"
    out: dict[str, str] = {}
    for line in block:
        m = header_re.search(line)
        if m:
            current = m.group(1).strip()
            continue
        m = key_re.match(line)
        if m:
            name = m.group(1)
            if current == _UNHEADERED_BUCKET and name != "signal_health":
                out[name] = _prefix_category(name) or f"{_UNHEADERED_BUCKET} (unlabeled in source)"
            else:
                out[name] = current
    return out


def parse_pipeline_catalog() -> dict[str, list[dict]]:
    """Parse docs/PIPELINE_CATALOG.md markdown tables into table_name -> [{pipeline, fetches, key, section}]."""
    text = PIPELINE_CATALOG_MD.read_text(encoding="utf-8")
    section = "Uncategorized"
    out: dict[str, list[dict]] = {}
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        if not line.startswith("|") or line.startswith("|---") or "Pipeline" in line and "Tables" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        pipeline_cell, tables_cell, fetches_cell, key_cell = cells[0], cells[1], cells[2], cells[3]
        pipelines = re.findall(r"`([^`]+\.py)`", pipeline_cell)
        table_names = re.findall(r"`([a-zA-Z0-9_]+)`", tables_cell)
        if not pipelines:
            continue
        for t in table_names:
            out.setdefault(t, []).append({
                "pipeline": " / ".join(pipelines),
                "fetches": fetches_cell,
                "key": key_cell,
                "section": section,
            })
    return out


def parse_analytics_view_edges() -> dict[str, dict]:
    """Regex the ANALYTICS_VIEWS SQL blocks for FROM/JOIN table + ON column pairs."""
    text = QUERY_PY.read_text(encoding="utf-8")
    start = text.index("ANALYTICS_VIEWS: dict[str, str] = {")
    block = text[start:]
    end = block.index("\n}\n")
    block = block[:end]

    view_re = re.compile(r'"(\w+)":\s*"""(.*?)"""', re.DOTALL)
    edges: dict[str, dict] = {}
    for name, sql in view_re.findall(block):
        tables = {}
        for m in re.finditer(r"FROM\s+(\w+)\s+(\w+)", sql):
            tables[m.group(2)] = m.group(1)
        for m in re.finditer(r"JOIN\s+(\w+)\s+(\w+)", sql):
            tables[m.group(2)] = m.group(1)
        joins = []
        for m in re.finditer(r"ON\s+([\w.]+)\s*=\s*([\w.]+)", sql):
            left_alias, left_col = m.group(1).split(".")
            right_alias, right_col = m.group(2).split(".")
            joins.append({
                "left_table": tables.get(left_alias, left_alias),
                "left_col": left_col,
                "right_table": tables.get(right_alias, right_alias),
                "right_col": right_col,
            })
        edges[name] = {"base_tables": sorted(set(tables.values())), "joins": joins}
    return edges


# ---------------------------------------------------------------------------
# Live remote population stats from the HF-hosted parquet files.
# ---------------------------------------------------------------------------

def fetch_hf_files() -> dict[str, list[dict]]:
    """table_name -> [{path, size}] for every parquet file in the HF dataset."""
    r = requests.get(HF_TREE_API, params={"recursive": "true"}, timeout=30)
    r.raise_for_status()
    by_table: dict[str, list[dict]] = {}
    for entry in r.json():
        if entry["type"] != "file" or not entry["path"].endswith(".parquet"):
            continue
        table = entry["path"].split("/")[0]
        by_table.setdefault(table, []).append({"path": entry["path"], "size": entry["size"]})
    return by_table


def inspect_hf_table(con: duckdb.DuckDBPyConnection, files: list[dict]) -> dict:
    urls = [f"{HF_RESOLVE}/{f['path']}" for f in files]
    url_list = "[" + ",".join(f"'{u}'" for u in urls) + "]"
    size_bytes = sum(f["size"] for f in files)

    columns: list[dict] = []
    try:
        schema_df = con.execute(f"DESCRIBE SELECT * FROM read_parquet({url_list})").fetchdf()
        columns = [{"name": r.column_name, "type": r.column_type} for r in schema_df.itertuples()]
    except Exception as e:
        return {"size_bytes": size_bytes, "columns": [], "rows": 0, "min_date": None,
                "max_date": None, "last_fetched": None, "error": str(e)}

    # Only VARCHAR/DATE/TIMESTAMP columns hold sortable ISO-ish date strings. A BIGINT
    # column named e.g. `datetime` (Unix epoch, as in finnhub_news) matches the "date"
    # substring but its raw integer value sorts nonsensically against real date strings.
    textual_types = {c["name"] for c in columns if c["type"].upper().startswith(("VARCHAR", "DATE", "TIMESTAMP"))}
    col_names = {c["name"] for c in columns}
    date_col = next((c for c in DATE_COL_PRIORITY if c in col_names and c in textual_types), None)
    if date_col is None:
        # Source-specific date column names (fiscalDateEnding, faildate, asofdate, ...) —
        # take the first plausible one; skip point-in-time record fields (ATH/ATL) and
        # the hive partition columns, which aren't observation dates.
        deny = {"month", "year", "ath_date", "atl_date"}
        date_col = next(
            (c["name"] for c in columns
             if "date" in c["name"].lower() and c["name"] not in deny and c["name"] in textual_types),
            None,
        )

    rows = 0
    min_date = max_date = last_fetched = None
    try:
        meta = con.execute(
            f"SELECT path_in_schema, row_group_id, row_group_num_rows, stats_min_value, stats_max_value "
            f"FROM parquet_metadata({url_list})"
        ).fetchdf()
        first_col = columns[0]["name"]
        rows = int(meta[meta["path_in_schema"] == first_col]["row_group_num_rows"].sum())
        if date_col:
            dc = meta[meta["path_in_schema"] == date_col]
            if len(dc):
                min_date = dc["stats_min_value"].min()
                max_date = dc["stats_max_value"].max()
        fc = meta[meta["path_in_schema"] == FRESHNESS_COL]
        if len(fc):
            last_fetched = fc["stats_max_value"].max()
    except Exception:
        pass

    def _clean(v):
        # pandas .min()/.max() on an all-null stats column returns float NaN, which
        # Python's json module serializes as a bare `NaN` token — invalid JSON, breaks
        # JS JSON.parse. Normalize to None.
        if v is None:
            return None
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            pass
        return v

    return {
        "size_bytes": size_bytes, "columns": columns, "rows": rows,
        "min_date": _clean(min_date), "max_date": _clean(max_date), "last_fetched": _clean(last_fetched),
    }


def main():
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "scripts" / "output" / "lake_manifest.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print("Parsing local repo (categories, pipeline sources, view edges)...", file=sys.stderr)
    categories = parse_categories()
    pipeline_map = parse_pipeline_catalog()
    view_edges = parse_analytics_view_edges()

    print("Fetching HF dataset file tree...", file=sys.stderr)
    hf_files = fetch_hf_files()
    print(f"  {len(hf_files)} tables populated on HF", file=sys.stderr)

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")

    catalog_names = set(q.CATALOG.keys())
    all_names = sorted(catalog_names | set(hf_files.keys()))

    tables = []
    t0 = time.time()
    for i, name in enumerate(all_names):
        in_catalog = name in catalog_names
        files = hf_files.get(name)

        if files:
            stats = inspect_hf_table(con, files)
            populated = True
        else:
            stats = {"size_bytes": 0, "columns": [], "rows": 0, "min_date": None,
                      "max_date": None, "last_fetched": None}
            populated = False

        tables.append({
            "table": name,
            "category": categories.get(name, "Not in current CATALOG (HF-only)"),
            "in_catalog": in_catalog,
            "populated_on_hf": populated,
            "rows": stats["rows"],
            "columns": stats["columns"],
            "size_bytes": stats["size_bytes"],
            "min_date": stats["min_date"],
            "max_date": stats["max_date"],
            "last_fetched": stats["last_fetched"],
            "sources": pipeline_map.get(name, []),
            "kind": "base",
        })
        if (i + 1) % 25 == 0:
            print(f"  {i + 1}/{len(all_names)} tables ({time.time() - t0:.0f}s)", file=sys.stderr)

    for name, sql_info in view_edges.items():
        tables.append({
            "table": name,
            "category": "Analytics view",
            "in_catalog": False,
            "populated_on_hf": False,
            "rows": None,
            "columns": [],
            "size_bytes": 0,
            "min_date": None,
            "max_date": None,
            "last_fetched": None,
            "sources": [],
            "kind": "analytics_view",
            "view_definition": sql_info,
        })

    populated_tables = [t for t in tables if t["populated_on_hf"]]
    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo": str(REPO),
        "hf_dataset": HF_DATASET,
        "table_count": len(tables),
        "populated_count": len(populated_tables),
        "defined_only_count": len([t for t in tables if t["in_catalog"] and not t["populated_on_hf"]]),
        "hf_only_count": len([t for t in tables if t["populated_on_hf"] and not t["in_catalog"]]),
        "total_rows": sum(t["rows"] or 0 for t in populated_tables),
        "total_size_bytes": sum(t["size_bytes"] for t in populated_tables),
        "tables": tables,
        "analytics_view_edges": view_edges,
    }

    out_path.write_text(json.dumps(manifest, indent=2, default=str, allow_nan=False), encoding="utf-8")
    print(f"Wrote {out_path} ({out_path.stat().st_size / 1024:.0f} KB) — "
          f"{manifest['table_count']} total ({manifest['populated_count']} populated on HF, "
          f"{manifest['defined_only_count']} defined-only, {manifest['hf_only_count']} HF-only), "
          f"{manifest['total_rows']:,} rows, {manifest['total_size_bytes'] / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
