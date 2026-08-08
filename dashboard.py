#!/usr/bin/env python3
"""
Earnings Sentiment Dashboard -- interactive Streamlit app for visualizing
earnings sentiment predictions vs stock price movement.

Usage:
  streamlit run dashboard.py
  streamlit run dashboard.py -- --sentiment-window 10 --car-window 10

Requires: streamlit, plotly, pandas, numpy
"""

import os
import sys
import argparse

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from earnings_sentiment_eval import (
    build_panel, print_stats,
    CAR_HORIZONS, FWD_HORIZONS,
    BULLISH_THRESHOLD, BEARISH_THRESHOLD,
)


st.set_page_config(
    page_title="Earnings Sentiment Dashboard",
    page_icon=":chart_with_upwards_trend:",
    layout="wide",
)

st.title("Earnings Sentiment vs Price Movement")
st.caption("Pre-earnings news sentiment compared to post-earnings stock reaction")


@st.cache_data(ttl=300)
def load_data(sentiment_window, car_window, start, end):
    panel = build_panel(
        sentiment_window=sentiment_window,
        car_window=car_window,
        start=start if start else None,
        end=end if end else None,
    )
    return panel


with st.sidebar:
    st.header("Parameters")
    sentiment_window = st.slider("Sentiment lookback (trading days)", 1, 15, 5)
    car_window = st.slider("CAR window (trading days)", 1, 20, 5)
    start_date = st.date_input("Start date", value=None)
    end_date = st.date_input("End date", value=None)
    car_horizon = st.selectbox("CAR horizon for charts", CAR_HORIZONS, index=2)
    min_surprise = st.slider("Min |EPS surprise| %", 0, 50, 0)

    build_btn = st.button("Build / Refresh Data", type="primary")

if build_btn or "panel" not in st.session_state:
    start_str = start_date.isoformat() if start_date else None
    end_str = end_date.isoformat() if end_date else None
    with st.spinner("Loading data and building crosswalk panel..."):
        panel = load_data(sentiment_window, car_window, start_str, end_str)
        st.session_state["panel"] = panel

panel = st.session_state.get("panel", pd.DataFrame())

if panel.empty:
    st.warning("No data available. Make sure earnings_calendar and news_sentiment pipelines have been run.")
    st.stop()

if min_surprise > 0:
    panel = panel[panel["surprise_pct"].abs() >= min_surprise].copy()

car_col = f"car_{car_horizon}d"
fwd_col = f"fwd_{car_horizon}d"
has_car = car_col in panel.columns and panel[car_col].notna().sum() > 5
has_fwd = fwd_col in panel.columns and panel[fwd_col].notna().sum() > 5


st.header("Overview")
col1, col2, col3, col4 = st.columns(4)
col1.metric("Total Events", f"{len(panel):,}")
col2.metric("Unique Symbols", f"{panel['symbol'].nunique()}")
col3.metric("With Sentiment", f"{panel['sent_score'].notna().sum():,}")
col4.metric("Date Range", f"{panel['date'].min().date()} to {panel['date'].max().date()}")

st.divider()


st.header("Sentiment vs Price Movement")


tab1, tab2, tab3, tab4 = st.tabs([
    "Scatter: Sentiment vs CAR",
    "CAR by Sentiment Bucket",
    "Surprise vs CAR",
    "Per-Symbol Breakdown",
])


with tab1:
    if has_car:
        scatter_df = panel.dropna(subset=["sent_score", car_col]).copy()
        if not scatter_df.empty:
            fig = px.scatter(
                scatter_df,
                x="sent_score",
                y=car_col,
                color="surprise_dir",
                hover_data=["symbol", "date", "surprise_pct", "sent_n_articles"],
                title=f"Pre-Earnings Sentiment vs CAR ({car_horizon}d)",
                labels={
                    "sent_score": "Sentiment Score",
                    car_col: f"CAR {car_horizon}d (%)",
                    "surprise_dir": "EPS Result",
                },
                opacity=0.6,
                color_discrete_map={"beat": "#2ecc71", "miss": "#e74c3c", "inline": "#95a5a6"},
            )
            fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)
            fig.add_vline(x=0, line_dash="dash", line_color="gray", opacity=0.5)

            if len(scatter_df) > 20:
                z = np.polyfit(scatter_df["sent_score"], scatter_df[car_col], 1)
                p = np.poly1d(z)
                x_range = np.linspace(scatter_df["sent_score"].min(),
                                      scatter_df["sent_score"].max(), 100)
                fig.add_trace(go.Scatter(
                    x=x_range, y=p(x_range),
                    mode="lines", name="Trend",
                    line=dict(color="orange", dash="dash", width=2),
                ))

            fig.update_layout(height=500)
            st.plotly_chart(fig, use_container_width=True)

            from scipy import stats
            rho, p_val = stats.spearmanr(scatter_df["sent_score"], scatter_df[car_col])
            st.info(f"Spearman rho: **{rho:.4f}** (p={p_val:.4f}) -- "
                     f"{'Significant' if p_val < 0.05 else 'Not significant'} at 5% level")
        else:
            st.info("No data with both sentiment and CAR.")
    else:
        st.info(f"Not enough data for CAR {car_horizon}d.")


with tab2:
    if has_car:
        bull = panel[panel["sent_score"] >= BULLISH_THRESHOLD]
        bear = panel[panel["sent_score"] <= BEARISH_THRESHOLD]
        neutral = panel[(panel["sent_score"] > BEARISH_THRESHOLD) &
                        (panel["sent_score"] < BULLISH_THRESHOLD) &
                        panel["sent_score"].notna()]

        buckets = []
        for name, df in [("Bullish", bull), ("Neutral", neutral), ("Bearish", bear)]:
            if df.empty:
                continue
            for h in CAR_HORIZONS:
                col = f"car_{h}d"
                if col in df.columns:
                    valid = df[col].dropna()
                    if len(valid) > 0:
                        buckets.append({
                            "Sentiment": name,
                            "Horizon (d)": h,
                            "Mean CAR (%)": valid.mean(),
                            "Median CAR (%)": valid.median(),
                            "Std (%)": valid.std(),
                            "N": len(valid),
                        })

        if buckets:
            bucket_df = pd.DataFrame(buckets)
            fig = px.bar(
                bucket_df[bucket_df["Horizon (d)"] == car_horizon],
                x="Sentiment",
                y="Mean CAR (%)",
                color="Sentiment",
                error_y="Std (%)",
                title=f"Mean CAR ({car_horizon}d) by Sentiment Bucket",
                color_discrete_map={"Bullish": "#2ecc71", "Neutral": "#3498db", "Bearish": "#e74c3c"},
            )
            fig.update_layout(height=400, showlegend=False)
            st.plotly_chart(fig, use_container_width=True)

            fig2 = px.line(
                bucket_df,
                x="Horizon (d)",
                y="Mean CAR (%)",
                color="Sentiment",
                markers=True,
                title="Mean CAR Across Horizons by Sentiment",
                color_discrete_map={"Bullish": "#2ecc71", "Neutral": "#3498db", "Bearish": "#e74c3c"},
            )
            fig2.update_layout(height=400)
            st.plotly_chart(fig2, use_container_width=True)

            st.dataframe(bucket_df.pivot_table(
                index="Sentiment", columns="Horizon (d)",
                values=["Mean CAR (%)", "N"], aggfunc="first",
            ).round(2), use_container_width=True)
        else:
            st.info("Not enough sentiment buckets to compare.")
    else:
        st.info("Not enough CAR data.")


with tab3:
    if has_car:
        sent_valid = panel.dropna(subset=["sent_score", car_col])
        if len(sent_valid) > 10:
            sent_valid = sent_valid.copy()
            sent_valid["sent_bucket"] = pd.cut(
                sent_valid["sent_score"],
                bins=[-2, BEARISH_THRESHOLD, BULLISH_THRESHOLD, 2],
                labels=["Bearish", "Neutral", "Bullish"],
            )

            fig = px.box(
                sent_valid,
                x="surprise_dir",
                y=car_col,
                color="surprise_dir",
                hover_data=["symbol", "date", "sent_score"],
                title=f"CAR ({car_horizon}d) by EPS Surprise Direction",
                color_discrete_map={"beat": "#2ecc71", "miss": "#e74c3c", "inline": "#95a5a6"},
            )
            fig.update_layout(height=450, showlegend=False)
            st.plotly_chart(fig, use_container_width=True)

            fig2 = px.scatter(
                sent_valid,
                x="surprise_pct",
                y=car_col,
                color="sent_bucket",
                hover_data=["symbol", "date"],
                title="EPS Surprise % vs CAR (colored by Sentiment)",
                labels={"surprise_pct": "EPS Surprise (%)", car_col: f"CAR {car_horizon}d (%)"},
                opacity=0.5,
            )
            fig2.update_layout(height=450)
            st.plotly_chart(fig2, use_container_width=True)
        else:
            st.info("Not enough data with sentiment and CAR.")
    else:
        st.info("Not enough CAR data.")


with tab4:
    sym_stats = []
    for sym, grp in panel.groupby("symbol"):
        has_s = grp["sent_score"].notna().sum()
        has_c = grp[car_col].notna().sum() if car_col in grp.columns else 0
        if has_c < 2:
            continue
        valid = grp.dropna(subset=["sent_score", car_col]) if car_col in grp.columns else pd.DataFrame()
        if len(valid) < 2:
            continue
        from scipy import stats as sp_stats
        rho, p = sp_stats.spearmanr(valid["sent_score"], valid[car_col])
        sym_stats.append({
            "Symbol": sym,
            "Events": len(grp),
            "With Sentiment": has_s,
            "Mean Surprise %": grp["surprise_pct"].mean(),
            "Mean CAR": grp[car_col].mean() if car_col in grp.columns else np.nan,
            "Sent-CAR rho": round(rho, 3) if np.isfinite(rho) else np.nan,
            "p-value": round(p, 4) if np.isfinite(p) else np.nan,
        })

    if sym_stats:
        sym_df = pd.DataFrame(sym_stats).sort_values("Sent-CAR rho", ascending=False)
        st.dataframe(sym_df, use_container_width=True)

        top_n = min(20, len(sym_df))
        fig = px.bar(
            sym_df.head(top_n),
            x="Symbol",
            y="Sent-CAR rho",
            color="Events",
            title=f"Sentiment-CAR Correlation by Symbol (top {top_n})",
            color_continuous_scale="Blues",
        )
        fig.update_layout(height=400)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Not enough per-symbol data for breakdown.")


st.divider()
st.header("Distribution Plots")

col_a, col_b = st.columns(2)

with col_a:
    sent_valid = panel["sent_score"].dropna()
    if len(sent_valid) > 5:
        fig = px.histogram(
            x=sent_valid, nbins=30,
            title="Earnings Sentiment Score Distribution",
            labels={"x": "Sentiment Score"},
        )
        fig.add_vline(x=BULLISH_THRESHOLD, line_dash="dash", line_color="green", opacity=0.7)
        fig.add_vline(x=BEARISH_THRESHOLD, line_dash="dash", line_color="red", opacity=0.7)
        fig.update_layout(height=350)
        st.plotly_chart(fig, use_container_width=True)

with col_b:
    if "surprise_pct" in panel.columns:
        fig = px.histogram(
            x=panel["surprise_pct"].dropna(), nbins=40,
            title="EPS Surprise % Distribution",
            labels={"x": "EPS Surprise (%)"},
        )
        fig.add_vline(x=0, line_dash="dash", line_color="gray", opacity=0.7)
        fig.update_layout(height=350)
        st.plotly_chart(fig, use_container_width=True)


st.divider()
st.header("Raw Data")
st.dataframe(
    panel.sort_values("date", ascending=False).head(200),
    use_container_width=True,
    height=400,
)
