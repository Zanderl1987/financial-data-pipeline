"""
Macro analytics: yield curve shape, 2s10s inversion, credit spreads,
commodity correlations.

Requires the macro table (FRED) to have data; commodity_vs_symbol also
needs the prices table populated.
"""

import os
import sys
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import query as q

# FRED series IDs mapped to human-readable maturity labels
YIELD_SERIES: dict[str, str] = {
    "3m":  "DGS3MO",
    "2y":  "DGS2",
    "5y":  "DGS5",
    "10y": "DGS10",
    "30y": "DGS30",
}


def rate_environment(start: "str | None" = None) -> pd.DataFrame:
    """
    Treasury yields for key maturities over time (wide format).

    Returns:
        date | 3m | 2y | 5y | 10y | 30y
    Columns absent from the data are omitted.
    """
    series_ids = list(YIELD_SERIES.values())
    df = q.load("macro", series_id=series_ids, start=start)
    if df.empty:
        return df

    reverse_map = {v: k for k, v in YIELD_SERIES.items()}
    df = df.copy()
    df["maturity"] = df["series_id"].map(reverse_map)

    ordered = [m for m in ["3m", "2y", "5y", "10y", "30y"] if m in df["maturity"].values]
    return (
        df.pivot_table(index="date", columns="maturity", values="value")[ordered]
          .reset_index()
          .sort_values("date")
    )


def inversion(start: "str | None" = None) -> pd.DataFrame:
    """
    2s10s yield spread (10y minus 2y) — the canonical recession signal.

    Negative spread = inverted curve.

    Returns:
        date | 2y | 10y | spread_2s10s | inverted
    """
    rates = rate_environment(start=start)
    if rates.empty or "2y" not in rates.columns or "10y" not in rates.columns:
        return pd.DataFrame(columns=["date", "2y", "10y", "spread_2s10s", "inverted"])

    out = rates[["date", "2y", "10y"]].dropna().copy()
    out["spread_2s10s"] = (out["10y"] - out["2y"]).round(3)
    out["inverted"] = out["spread_2s10s"] < 0
    return out.reset_index(drop=True)


CREDIT_SERIES: dict[str, str] = {
    "hy_spread":  "BAMLH0A0HYM2",
    "ig_spread":  "BAMLC0A0CM",
    "hy_yield":   "BAMLH0A0HYM2EY",
    "em_spread":  "BAMLEMCBPIOAS",
}


def credit_spreads(start: "str | None" = None) -> pd.DataFrame:
    """
    Investment-grade and high-yield credit spreads (ICE BofA OAS) over time.

    Wide-format output:
        date | hy_spread | ig_spread | hy_yield | em_spread
    Absent series (not yet fetched) are omitted as columns.

    Interpretation:
        hy_spread > 500 bps  → stress / risk-off
        ig_spread > 200 bps  → elevated corporate risk
        widening spread      → market pricing in higher default probability
    """
    series_ids = list(CREDIT_SERIES.values())
    df = q.load("macro", series_id=series_ids, start=start)
    if df.empty:
        return df

    reverse_map = {v: k for k, v in CREDIT_SERIES.items()}
    df = df.copy()
    df["label"] = df["series_id"].map(reverse_map)

    ordered = [k for k in CREDIT_SERIES if k in df["label"].values]
    return (
        df.pivot_table(index="date", columns="label", values="value")[ordered]
          .reset_index()
          .sort_values("date")
    )


HOUSING_SERIES: dict[str, str] = {
    "housing_starts":   "HOUST",
    "building_permits": "PERMIT",
}

LUMBER_STEEL_SERIES: dict[str, str] = {
    "lumber_ppi": "WPU081",
    "steel_ppi":  "WPU101",
}


def housing_leading_indicators(start: "str | None" = None) -> pd.DataFrame:
    """
    Housing starts, building permits, and lumber/steel PPI in one wide monthly
    frame — the base table for lead-lag tests of what predicts new home
    construction (see lead_lag_correlation()).

    Returns:
        date | housing_starts | building_permits | lumber_ppi | steel_ppi
    Columns absent from the data are omitted.
    """
    series_map = {**HOUSING_SERIES, **LUMBER_STEEL_SERIES}
    housing = q.load("fred_macro_housing", series_id=list(HOUSING_SERIES.values()), start=start)
    commod = q.load("commodities", series_id=list(LUMBER_STEEL_SERIES.values()), start=start)

    frames = [df for df in (housing, commod) if not df.empty]
    if not frames:
        return pd.DataFrame(columns=["date"] + list(series_map))

    reverse_map = {v: k for k, v in series_map.items()}
    combined = pd.concat(frames, ignore_index=True)
    combined["date"] = pd.to_datetime(combined["date"]).dt.tz_localize(None)
    combined["label"] = combined["series_id"].map(reverse_map)

    ordered = [k for k in series_map if k in combined["label"].values]
    return (
        combined.pivot_table(index="date", columns="label", values="value")[ordered]
                .reset_index()
                .sort_values("date")
    )


def lead_lag_correlation(
    df: pd.DataFrame,
    leading: str,
    target: str,
    max_lag: int = 12,
    transform: str = "yoy",
) -> pd.DataFrame:
    """
    Cross-correlate two columns of a wide monthly DataFrame at lags
    -max_lag..+max_lag to test whether `leading` predicts `target` ahead of time.

    Parameters
    ----------
    df       : wide DataFrame with a `date` column plus `leading`/`target`
               columns (e.g. from housing_leading_indicators())
    leading  : candidate leading-indicator column name
    target   : column being predicted (e.g. 'housing_starts')
    max_lag  : months to test in each direction
    transform: 'yoy' (year-over-year %, detrends level series) or 'level'

    Returns:
        lag | corr
    Negative lag means `leading` leads `target` by that many months; positive
    lag means `leading` actually follows `target` (a lagging indicator). The
    row with the largest |corr| is the best-fit lag — a genuinely predictive
    relationship needs that peak at lag < 0, not at 0 or positive.
    """
    if df.empty or leading not in df.columns or target not in df.columns:
        return pd.DataFrame(columns=["lag", "corr"])

    work = df[["date", leading, target]].copy()
    work["date"] = pd.to_datetime(work["date"])
    work = work.set_index("date").sort_index()

    if transform == "yoy":
        a = work[leading].pct_change(12) * 100
        b = work[target].pct_change(12) * 100
    elif transform == "level":
        a = work[leading]
        b = work[target]
    else:
        raise ValueError("transform must be 'yoy' or 'level'")

    rows = []
    for lag in range(-max_lag, max_lag + 1):
        shifted = b.shift(-lag)
        both = pd.concat([a, shifted], axis=1).dropna()
        r = both.iloc[:, 0].corr(both.iloc[:, 1]) if len(both) >= 24 else None
        rows.append({"lag": lag, "corr": None if r is None else round(float(r), 4)})

    return pd.DataFrame(rows)


def commodity_vs_symbol(
    commodity_series_id: str,
    symbol: str,
    start: "str | None" = None,
    end: "str | None" = None,
) -> pd.DataFrame:
    """
    Align a FRED/EIA commodity series with an equity's close price.

    Useful for correlation analysis (e.g. WTI crude vs XOM).

    Parameters
    ----------
    commodity_series_id : FRED series ID (e.g. 'DCOILWTICO' for WTI crude)
    symbol              : equity ticker (e.g. 'XOM')
    start / end         : 'YYYY-MM-DD' date bounds

    Returns wide DataFrame (inner join on date):
        date | close | commodity_value
    """
    prices = q.load("prices", symbol=symbol, start=start, end=end,
                    columns=["date", "close"])
    comm = q.load("macro", series_id=commodity_series_id, start=start, end=end,
                  columns=["date", "value"])

    if prices.empty or comm.empty:
        return pd.DataFrame(columns=["date", "close", "commodity_value"])

    return (
        prices.merge(comm.rename(columns={"value": "commodity_value"}), on="date", how="inner")
              .sort_values("date")
              .reset_index(drop=True)
    )
