"""
Labor-market analytics: Indeed Hiring Lab job-postings index trends.

Requires the indeed_job_postings_{national,sector,state} tables populated by
indeed_hiringlab_pipeline.py.
"""

import os
import sys
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import query as q


def hiring_trend(
    sector: "str | None" = None,
    state: "str | None" = None,
    variable: str = "total postings",
) -> pd.DataFrame:
    """
    Latest job-postings index level plus week-over-week and month-over-month
    % change, at national, sector, or state granularity.

    Parameters
    ----------
    sector   : sector display name (e.g. "Accounting") -- uses indeed_job_postings_sector
    state    : two-letter state code (e.g. "ca") -- uses indeed_job_postings_state
               (takes priority over sector if both are given)
    variable : "total postings" or "new postings" (national/sector tables only)

    Returns a one-row DataFrame:
        label | date | index_level | wow_pct_change | mom_pct_change
    """
    if state:
        df = q.load("indeed_job_postings_state")
        if not df.empty:
            df = df[df["state"] == state.lower()]
        value_col = "indeed_job_postings_index"
        label = f"state:{state}"
    elif sector:
        df = q.load("indeed_job_postings_sector")
        if not df.empty:
            df = df[(df["sector"] == sector) & (df["variable"] == variable)]
        value_col = "indeed_job_postings_index"
        label = f"sector:{sector}"
    else:
        df = q.load("indeed_job_postings_national")
        if not df.empty:
            df = df[df["variable"] == variable]
        value_col = "indeed_job_postings_index_sa"
        label = "national"

    if df.empty:
        return df

    df = df.sort_values("date").reset_index(drop=True)
    series = df.set_index("date")[value_col]
    latest_date = df["date"].iloc[-1]
    latest_value = df[value_col].iloc[-1]

    def _pct_change_over(days):
        cutoff = latest_date - pd.Timedelta(days=days)
        prior = series[series.index <= cutoff]
        if prior.empty:
            return None
        return round((latest_value / prior.iloc[-1] - 1) * 100, 2)

    return pd.DataFrame([{
        "label":           label,
        "date":            latest_date,
        "index_level":     latest_value,
        "wow_pct_change":  _pct_change_over(7),
        "mom_pct_change":  _pct_change_over(30),
    }])
