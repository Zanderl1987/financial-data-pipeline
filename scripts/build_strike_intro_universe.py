"""Freeze the ~100-name universe for the strike-introduction study.

S&P 500 single stocks (IVV holdings -- no ETFs) ranked by total option volume
across schwab_options snapshots 2026-08-02..2026-09-30. Selection on recent
activity is a survivorship tilt; the writeup must say so.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import query as q
from analytics.portfolio import top_holdings

N = 100
# Class shares lose their dot in IVV holdings (BRKB, BFB) and won't match
# option roots, so they can't be measured cleanly.
EXCLUDE = {"BRKB", "BFB"}
# GOOG duplicates GOOGL (same company -> every move counted twice). The rest
# changed ticker or listed inside the 2-year window, which breaks the day-by-
# day contract history at that date: XYZ (was SQ until 2025-01), PSKY (was
# PARA until 2025-08), SNDK (listed 2025-02).
EXCLUDE |= {"GOOG", "XYZ", "PSKY", "SNDK"}

stocks = set(top_holdings("IVV", n=600)["holding_ticker"].dropna()) - EXCLUDE
vol = q.sql("""
    SELECT symbol, SUM(volume) AS total_option_volume
    FROM schwab_options
    WHERE snapshot_date BETWEEN '2026-08-02' AND '2026-09-30'
    GROUP BY symbol
""")
vol = vol[vol["symbol"].isin(stocks)].sort_values("total_option_volume", ascending=False)
out = vol.head(N).reset_index(drop=True)
out.insert(0, "rank", out.index + 1)
out.to_csv("experiments/strike_intro_universe.csv", index=False)
print(f"{len(out)} symbols; top 5: {', '.join(out['symbol'].head(5))}")
