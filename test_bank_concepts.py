"""Test bank-specific XBRL concepts for JPM and GS."""
import sys
sys.path.insert(0, ".")
from fundamentals_pipeline import fetch_company_facts, extract_company, load_cik_map

cik_map = load_cik_map()

for sym, cik in [("JPM", cik_map.get("JPM")), ("GS", cik_map.get("GS"))]:
    print(f"\n{'='*50}")
    print(f"{sym} (CIK {cik})")
    data = fetch_company_facts(cik)
    if not data:
        print("  ERROR: no data returned")
        continue

    annual_rows, quarterly_rows = extract_company(data, symbol=sym)
    import pandas as pd
    annual = pd.DataFrame(annual_rows)
    if annual.empty:
        print("  No annual rows extracted")
        continue

    for metric in ["revenue", "net_income", "operating_income"]:
        sub = annual[annual["metric"] == metric]

        if sub.empty:
            print(f"  {metric}: MISSING")
        else:
            latest = sub.sort_values("fiscal_year", ascending=False).head(3)
            vals = latest[["fiscal_year", "value", "concept"]].to_string(index=False)
            print(f"  {metric}: {len(sub)} rows, latest:\n    {vals}")
