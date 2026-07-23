#!/usr/bin/env python3
"""Constituents Iceberg table visualizations."""

import duckdb
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from pathlib import Path

OUT_DIR = Path(__file__).parent / "storage" / "iceberg" / "viz"
OUT_DIR.mkdir(parents=True, exist_ok=True)

ICEBERG_PATH = "C:/Users/zande/PycharmProjects/financial-data-pipeline/storage/iceberg/constituents/index_members/**/*.parquet"

con = duckdb.connect()
con.execute("INSTALL iceberg; LOAD iceberg;")

# --- 1. S&P 500 Sector Pie Chart ---
print("Generating S&P 500 sector pie chart...")
sectors = con.execute(
    "SELECT gics_sector, COUNT(*) as count "
    f"FROM read_parquet('{ICEBERG_PATH}', hive_partitioning=true) "
    "WHERE index_code = 'SPX' AND snapshot_date = '2026-07-18' AND gics_sector IS NOT NULL "
    "GROUP BY gics_sector ORDER BY count DESC"
).fetchdf()

colors = [
    "#2563eb", "#dc2626", "#16a34a", "#f59e0b", "#8b5cf6",
    "#ec4899", "#06b6d4", "#f97316", "#6366f1", "#14b8a6", "#64748b"
]

fig, ax = plt.subplots(figsize=(10, 8))
wedges, texts, autotexts = ax.pie(
    sectors["count"], labels=sectors["gics_sector"], autopct="%1.0f%%",
    colors=colors[:len(sectors)], startangle=140, pctdistance=0.8,
    textprops={"fontsize": 9}
)
for t in autotexts:
    t.set_fontsize(8)
    t.set_color("white")
    t.set_fontweight("bold")
ax.set_title("S&P 500 Constituents by GICS Sector\n(2026-07-18 snapshot)", fontsize=13, fontweight="bold")
plt.tight_layout()
fig.savefig(OUT_DIR / "sp500_sectors.png", dpi=150, bbox_inches="tight")
plt.close()
print(f"  Saved: {OUT_DIR / 'sp500_sectors.png'}")

# --- 2. Index Size Comparison Bar Chart ---
print("Generating index size bar chart...")
sizes = con.execute(
    "SELECT index_code, index_name, COUNT(*) as count "
    f"FROM read_parquet('{ICEBERG_PATH}', hive_partitioning=true) "
    "WHERE snapshot_date = '2026-07-18' "
    "GROUP BY index_code, index_name ORDER BY count DESC"
).fetchdf()

fig, ax = plt.subplots(figsize=(10, 5))
bar_colors = ["#1e40af", "#2563eb", "#60a5fa", "#93c5fd", "#bfdbfe"]
bars = ax.barh(sizes["index_code"][::-1], sizes["count"][::-1], color=bar_colors[::-1], edgecolor="white", height=0.6)
for bar, count in zip(bars, sizes["count"][::-1]):
    ax.text(bar.get_width() + 50, bar.get_y() + bar.get_height()/2, f"{count:,}",
            va="center", fontsize=10, fontweight="bold")
ax.set_xlabel("Number of Constituents", fontsize=11)
ax.set_title("Index Constituent Counts\n(2026-07-18 snapshot)", fontsize=13, fontweight="bold")
ax.set_xlim(0, max(sizes["count"]) * 1.15)
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)
plt.tight_layout()
fig.savefig(OUT_DIR / "index_sizes.png", dpi=150, bbox_inches="tight")
plt.close()
print(f"  Saved: {OUT_DIR / 'index_sizes.png'}")

# --- 3. Cross-Index Overlap Matrix ---
print("Generating cross-index overlap matrix...")
index_codes = ["SPX", "NDX", "RUT2000", "RUT3000", "W5000"]
overlap_data = con.execute(
    "SELECT ticker, STRING_AGG(DISTINCT index_code, ',') as indices "
    f"FROM read_parquet('{ICEBERG_PATH}', hive_partitioning=true) "
    "WHERE snapshot_date = '2026-07-18' "
    "GROUP BY ticker"
).fetchdf()

import numpy as np
matrix = np.zeros((len(index_codes), len(index_codes)), dtype=int)
for _, row in overlap_data.iterrows():
    idxs = set(row["indices"].split(","))
    for i, a in enumerate(index_codes):
        for j, b in enumerate(index_codes):
            if a in idxs and b in idxs:
                matrix[i][j] += 1

fig, ax = plt.subplots(figsize=(8, 7))
im = ax.imshow(matrix, cmap="Blues", aspect="auto")
ax.set_xticks(range(len(index_codes)))
ax.set_yticks(range(len(index_codes)))
ax.set_xticklabels(index_codes, fontsize=10, rotation=45, ha="right")
ax.set_yticklabels(index_codes, fontsize=10)
for i in range(len(index_codes)):
    for j in range(len(index_codes)):
        val = matrix[i][j]
        color = "white" if val > matrix.max() * 0.5 else "black"
        ax.text(j, i, f"{val:,}", ha="center", va="center", fontsize=10, color=color, fontweight="bold")
fig.colorbar(im, ax=ax, shrink=0.8, label="Shared Tickers")
ax.set_title("Cross-Index Constituent Overlap\n(2026-07-18 snapshot)", fontsize=13, fontweight="bold")
plt.tight_layout()
fig.savefig(OUT_DIR / "index_overlap.png", dpi=150, bbox_inches="tight")
plt.close()
print(f"  Saved: {OUT_DIR / 'index_overlap.png'}")

# --- 4. S&P 500 Sector Horizontal Bar ---
print("Generating S&P 500 sector bar chart...")
fig, ax = plt.subplots(figsize=(10, 6))
sector_colors = {s: c for s, c in zip(sectors["gics_sector"], colors)}
barh = ax.barh(
    sectors["gics_sector"][::-1], sectors["count"][::-1],
    color=[sector_colors[s] for s in sectors["gics_sector"][::-1]],
    edgecolor="white", height=0.6
)
for bar, count in zip(barh, sectors["count"][::-1]):
    ax.text(bar.get_width() + 1, bar.get_y() + bar.get_height()/2, str(count),
            va="center", fontsize=10, fontweight="bold")
ax.set_xlabel("Number of Companies", fontsize=11)
ax.set_title("S&P 500 Companies by GICS Sector\n(2026-07-18 snapshot)", fontsize=13, fontweight="bold")
ax.set_xlim(0, max(sectors["count"]) * 1.15)
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)
plt.tight_layout()
fig.savefig(OUT_DIR / "sp500_sector_bar.png", dpi=150, bbox_inches="tight")
plt.close()
print(f"  Saved: {OUT_DIR / 'sp500_sector_bar.png'}")

# --- 5. Top Russell 2000 Holdings by Weight ---
print("Generating Russell 2000 top holdings...")
r2k = con.execute(
    "SELECT ticker, company_name, weight_pct "
    f"FROM read_parquet('{ICEBERG_PATH}', hive_partitioning=true) "
    "WHERE index_code = 'RUT2000' AND snapshot_date = '2026-07-18' AND weight_pct IS NOT NULL "
    "ORDER BY weight_pct DESC LIMIT 20"
).fetchdf()

fig, ax = plt.subplots(figsize=(10, 7))
y_labels = [f"{row['ticker']}  {str(row['company_name'])[:25]}" for _, row in r2k.iterrows()]
ax.barh(y_labels[::-1], r2k["weight_pct"][::-1], color="#2563eb", edgecolor="white", height=0.6)
ax.set_xlabel("Weight (%)", fontsize=11)
ax.set_title("Russell 2000 — Top 20 Holdings by Weight\n(2026-07-18 snapshot)", fontsize=13, fontweight="bold")
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)
plt.tight_layout()
fig.savefig(OUT_DIR / "rut2000_top_holdings.png", dpi=150, bbox_inches="tight")
plt.close()
print(f"  Saved: {OUT_DIR / 'rut2000_top_holdings.png'}")

con.close()
print(f"\nAll charts saved to: {OUT_DIR}")
