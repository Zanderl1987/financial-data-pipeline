"""
Shared "broad universe" symbol list for pipelines against sources with no
per-symbol daily quota (currently: Schwab, 120 req/min hard cap only).

Sourced from IVV's S&P 500 holdings (already fetched by
fund_holdings_pipeline.py) rather than a hardcoded or re-scraped list, so it
tracks real index membership and stays in sync with data already on disk.
"""

from analytics.portfolio import top_holdings

# Safety net if fund_holdings/etf_holdings has no IVV snapshot yet (fresh
# clone, or the pipeline hasn't run) -- same DJI-30 fallback other pipelines
# use for get_dji_symbols().
FALLBACK_SYMBOLS = [
    "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS",
    "GS", "HD", "HON", "IBM", "JNJ", "JPM", "KO", "MCD", "MMM", "MRK",
    "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH", "V", "VZ", "WMT",
]


def get_broad_universe(extra: list[str] | None = None) -> list[str]:
    """S&P 500 constituents (via IVV holdings) plus any extra symbols, deduped and sorted."""
    df = top_holdings("IVV", n=600)
    symbols = sorted(df["holding_ticker"].dropna().unique().tolist()) if not df.empty else list(FALLBACK_SYMBOLS)
    if extra:
        symbols = sorted(set(symbols) | set(extra))
    return symbols
