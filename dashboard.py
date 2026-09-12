"""
dashboard.py — Public evaluation & data health dashboard (Streamlit).

Run: streamlit run dashboard.py
"""

import os
import sys
from datetime import datetime, timezone, timedelta
import json

import pandas as pd
import streamlit as st

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)

import query as q
from evaluation import registry as ev_registry
from freshness_dashboard import get_freshness_report, generate_html

st.set_page_config(
    page_title="Financial Data Pipeline — Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Helpers ────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def load_freshness():
    return get_freshness_report()


@st.cache_data(ttl=300)
def load_signal_health():
    try:
        df = q.load("signal_health")
        if df.empty:
            return pd.DataFrame()
        df["run_date"] = pd.to_datetime(df["run_date"])
        df["fetched_at"] = pd.to_datetime(df["fetched_at"])
        return df
    except Exception as e:
        st.error(f"Failed to load signal_health: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=300)
def load_eval_registry():
    try:
        return ev_registry.summary_df()
    except Exception as e:
        st.error(f"Failed to load eval registry: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=60)
def load_trades_daily():
    """Load survivor portfolio daily P&L from eval registry."""
    try:
        df = ev_registry.baselines(
            "bollinger_bands_simple+optimized_doji_breakout_short+rsi_bb_inside_strategy"
        )
        if df.empty:
            return pd.DataFrame()
        df["created_at"] = pd.to_datetime(df["created_at"])
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300)
def load_daily_artifacts():
    """Load daily paper trade artifacts from storage."""
    artifact_dir = os.path.join(REPO_ROOT, "storage", "eval_artifacts", "tv_survivor_portfolio_daily")
    if not os.path.exists(artifact_dir):
        return pd.DataFrame()

    rows = []
    for fname in sorted(os.listdir(artifact_dir)):
        if fname.endswith(".json") and fname.startswith("summary_"):
            try:
                with open(os.path.join(artifact_dir, fname), "r") as f:
                    meta = json.load(f)
                meta["_file"] = fname
                rows.append(meta)
            except Exception:
                pass
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["run_ts"] = pd.to_datetime(df["run_ts"], format="%Y%m%dT%H%M%SZ")
    return df.sort_values("run_ts")


def status_badge(status: str) -> str:
    colors = {
        "FRESH": "#22c55e",
        "STALE": "#f59e0b",
        "STALE_CRITICAL": "#ef4444",
        "NO DATA": "#6b7280",
        "NO DATE COL": "#8b5cf6",
        "DEGRADED": "#ef4444",
        "HEALTHY": "#22c55e",
    }
    color = colors.get(status, "#6b7280")
    return f'<span style="background:{color};color:white;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:600">{status}</span>'


# ── Sidebar ────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("📊 Pipeline Dashboard")
    st.caption(f"Repo: `{os.path.basename(REPO_ROOT)}`")
    st.caption(f"Updated: {datetime.now().strftime('%H:%M:%S')}")

    page = st.radio(
        "Navigate",
        ["Overview", "Data Freshness", "Signal Health", "Eval Registry", "Survivor Portfolio"],
        index=0,
    )

    st.divider()
    if st.button("🔄 Clear Cache & Refresh"):
        st.cache_data.clear()
        st.rerun()


# ── Pages ──────────────────────────────────────────────────────────────────────

if page == "Overview":
    st.header("Pipeline Overview")

    freshness = load_freshness()
    fresh = sum(1 for r in freshness if r.status == "FRESH")
    stale = sum(1 for r in freshness if r.status in ("STALE", "STALE_CRITICAL"))
    no_data = sum(1 for r in freshness if r.status == "NO DATA")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Tables", len(freshness))
    col2.metric("✅ Fresh", fresh, delta_color="normal")
    col3.metric("⚠️ Stale", stale, delta_color="inverse")
    col4.metric("❌ No Data", no_data)

    st.divider()

    # Signal health summary
    sig = load_signal_health()
    if not sig.empty:
        latest_run = sig["run_date"].max()
        st.subheader("Signal Monitor (Latest Run)")
        st.caption(f"Latest run: {latest_run.strftime('%Y-%m-%d')}")

        latest_sig = sig[sig["run_date"] == latest_run]
        degraded = latest_sig[latest_sig["car21_tstat"] < 2.0] if "car21_tstat" in latest_sig.columns else pd.DataFrame()

        c1, c2, c3 = st.columns(3)
        c1.metric("Signals Tracked", latest_sig["signal"].nunique())
        c2.metric("Total Trades (full window)", int(latest_sig["n_trades"].sum()))
        c3.metric("Avg Win Rate", f"{latest_sig['win_rate_pct'].mean():.1f}%")

        if not degraded.empty:
            st.warning(f"⚠️ {len(degraded)} signal(s) with t-stat < 2.0 in latest run")

    # Survivor portfolio
    daily = load_daily_artifacts()
    if not daily.empty:
        latest = daily.iloc[-1]
        st.subheader("Survivor Portfolio (Daily)")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Latest Run", latest["run_ts"].strftime("%Y-%m-%d %H:%M"))
        c2.metric("Trades", int(latest.get("n_trades", 0)))
        c3.metric("Win Rate", f"{latest.get('win_rate_pct', 0):.1f}%")
        c4.metric("P&L", f"${latest.get('total_pnl_dollars', 0):,.0f}")

    # Eval registry
    reg = load_eval_registry()
    if not reg.empty:
        st.subheader("Evaluation Registry")
        c1, c2 = st.columns(2)
        c1.metric("Total Runs", reg["run_id"].nunique())
        c2.metric("Unique Inputs", reg["input_name"].nunique())


elif page == "Data Freshness":
    st.header("Data Freshness Report")

    freshness = load_freshness()

    # Summary cards
    fresh = sum(1 for r in freshness if r.status == "FRESH")
    stale = sum(1 for r in freshness if r.status in ("STALE", "STALE_CRITICAL"))
    no_data = sum(1 for r in freshness if r.status == "NO DATA")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total", len(freshness))
    c2.metric("✅ Fresh", fresh)
    c3.metric("⚠️ Stale", stale)
    c4.metric("❌ No Data", no_data)

    st.divider()

    # Filter
    status_filter = st.multiselect(
        "Filter by status",
        options=["FRESH", "STALE", "STALE_CRITICAL", "NO DATA", "NO DATE COL"],
        default=["STALE_CRITICAL", "STALE", "NO DATA"],
    )

    filtered = [r for r in freshness if r.status in status_filter]

    # Table
    rows = []
    for item in filtered:
        rows.append({
            "Status": status_badge(item.status),
            "Table": item.table,
            "Latest Date": item.latest_date or "n/a",
            "Age (hrs)": f"{item.staleness_hours:.1f}" if item.staleness_hours is not None else "n/a",
            "Files": item.file_count,
            "Rows (latest)": f"{item.total_rows:,}",
        })

    if rows:
        df_disp = pd.DataFrame(rows)
        st.write(df_disp.to_html(escape=False, index=False), unsafe_allow_html=True)
    else:
        st.info("No tables match the selected filters.")

    # Download
    if st.button("Download Full Report (JSON)"):
        import json
        from dataclasses import asdict
        json_str = json.dumps([r.__dict__ for r in freshness], indent=2, default=str)
        st.download_button("Download JSON", json_str, "freshness_report.json", "application/json")


elif page == "Signal Health":
    st.header("Signal Health Monitor")

    sig = load_signal_health()
    if sig.empty:
        st.warning("No signal_health data found.")
        st.stop()

    # Latest run selector
    runs = sorted(sig["run_date"].unique(), reverse=True)
    selected_run = st.selectbox("Select run date", runs, format_func=lambda x: x.strftime("%Y-%m-%d"))

    run_data = sig[sig["run_date"] == selected_run]

    # Summary
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Signals", run_data["signal"].nunique())
    c2.metric("Total Trades", int(run_data["n_trades"].sum()))
    c3.metric("Avg Win Rate", f"{run_data['win_rate_pct'].mean():.1f}%")
    c4.metric("Avg CAR21 t-stat", f"{run_data['car21_tstat'].mean():.2f}")

    st.divider()

    # Window selector
    windows = sorted(run_data["window"].unique())
    selected_window = st.selectbox("Window", windows, index=0)
    win_data = run_data[run_data["window"] == selected_window]

    # Table
    display_cols = ["signal", "symbols_key", "n_trades", "win_rate_pct", "avg_return_pct",
                    "profit_factor", "car21_mean_pct", "car21_tstat", "holding_days"]
    avail = [c for c in display_cols if c in win_data.columns]

    df_disp = win_data[avail].copy()
    df_disp["car21_tstat"] = df_disp["car21_tstat"].round(2)
    df_disp["win_rate_pct"] = df_disp["win_rate_pct"].round(1)
    df_disp["avg_return_pct"] = df_disp["avg_return_pct"].round(2)
    df_disp["profit_factor"] = df_disp["profit_factor"].round(2)
    df_disp["car21_mean_pct"] = df_disp["car21_mean_pct"].round(2)

    # Color code t-stat
    def highlight_tstat(val):
        if val >= 2.0:
            return "background-color: #166534; color: white"
        elif val >= 1.0:
            return "background-color: #854d0e; color: white"
        else:
            return "background-color: #7f1d1d; color: white"

    styled = df_disp.style.applymap(highlight_tstat, subset=["car21_tstat"])
    st.dataframe(styled, use_container_width=True, height=500)

    # Trend chart
    st.subheader("Signal Trend (CAR21 t-stat over time)")
    signal_names = sorted(sig["signal"].unique())
    selected_signals = st.multiselect("Select signals to plot", signal_names,
                                       default=signal_names[:3] if len(signal_names) >= 3 else signal_names)

    if selected_signals:
        trend_data = sig[sig["signal"].isin(selected_signals) & (sig["window"] == "full")]
        if not trend_data.empty:
            chart_df = trend_data.pivot_table(
                index="run_date", columns="signal", values="car21_tstat"
            )
            st.line_chart(chart_df, height=300)


elif page == "Eval Registry":
    st.header("Evaluation Registry")

    reg = load_eval_registry()
    if reg.empty:
        st.warning("No evaluation registry data found.")
        st.stop()

    # Summary
    c1, c2, c3 = st.columns(3)
    c1.metric("Total Rows", len(reg))
    c2.metric("Unique Runs", reg["run_id"].nunique())
    c3.metric("Unique Inputs", reg["input_name"].nunique())

    st.divider()

    # Filter by input
    inputs = sorted(reg["input_name"].unique())
    selected_input = st.selectbox("Filter by input", ["All"] + inputs)

    if selected_input != "All":
        reg = reg[reg["input_name"] == selected_input]

    # Filter by evaluation type
    eval_types = sorted(reg["evaluation"].unique())
    selected_eval = st.selectbox("Filter by evaluation", ["All"] + eval_types)
    if selected_eval != "All":
        reg = reg[reg["evaluation"] == selected_eval]

    # Latest run per input
    st.subheader("Latest Run per Input")
    latest_per_input = reg.sort_values("created_at").groupby("input_name").tail(1)
    st.dataframe(
        latest_per_input[["input_name", "input_type", "evaluation", "run_id", "created_at", "universe_hash", "date_range"]],
        use_container_width=True,
    )

    st.divider()

    # Full registry table
    st.subheader("Full Registry")
    st.dataframe(
        reg.sort_values("created_at", ascending=False),
        use_container_width=True,
        height=400,
    )

    # Download
    if st.button("Download Registry (Parquet)"):
        import io
        buf = io.BytesIO()
        reg.to_parquet(buf, index=False)
        st.download_button("Download", buf.getvalue(), "eval_registry.parquet", "application/octet-stream")


elif page == "Survivor Portfolio":
    st.header("Survivor Portfolio — Daily Paper Trade")

    # Load artifacts
    daily = load_daily_artifacts()
    reg_daily = load_trades_daily()

    if daily.empty and reg_daily.empty:
        st.warning("No daily paper trade data yet. Run `python -m strategies.portfolio --daily --confirm-run --write` to generate.")
        st.stop()

    # Tabs
    tab1, tab2 = st.tabs(["📈 Daily Runs (Artifacts)", "📋 Eval Registry (trades_daily)"])

    with tab1:
        if not daily.empty:
            # Summary metrics over time
            st.subheader("Daily P&L Tracking")

            col1, col2, col3, col4 = st.columns(4)
            latest = daily.iloc[-1]
            col1.metric("Latest Run", latest["run_ts"].strftime("%Y-%m-%d %H:%M"))
            col2.metric("Trades", int(latest.get("n_trades", 0)))
            col3.metric("Win Rate", f"{latest.get('win_rate_pct', 0):.1f}%")
            col4.metric("Total P&L", f"${latest.get('total_pnl_dollars', 0):,.0f}")

            st.divider()

            # Time series charts
            chart_data = daily.set_index("run_ts")
            if "total_pnl_dollars" in chart_data.columns:
                st.line_chart(chart_data["total_pnl_dollars"], height=250, use_container_width=True)
                st.caption("Daily Total P&L ($)")

            c1, c2 = st.columns(2)
            if "win_rate_pct" in chart_data.columns:
                c1.line_chart(chart_data["win_rate_pct"], height=200)
                c1.caption("Win Rate (%)")
            if "avg_pnl_pct" in chart_data.columns:
                c2.line_chart(chart_data["avg_pnl_pct"], height=200)
                c2.caption("Avg P&L per Trade (%)")

            st.divider()

            # Full table
            st.subheader("All Daily Runs")
            display_daily = daily.copy()
            display_daily["run_ts"] = display_daily["run_ts"].dt.strftime("%Y-%m-%d %H:%M:%S")
            st.dataframe(display_daily, use_container_width=True)

        else:
            st.info("No daily artifacts found. Run with `--write` to persist.")

    with tab2:
        if not reg_daily.empty:
            st.subheader("Registry Entries (evaluation=trades_daily)")
            st.dataframe(reg_daily, use_container_width=True)

            # Latest stats
            latest_run = reg_daily["run_id"].iloc[0]
            latest_rows = reg_daily[reg_daily["run_id"] == latest_run]
            st.caption(f"Latest run: {latest_run} — {len(latest_rows)} metrics registered")
        else:
            st.info("No registry entries for trades_daily yet.")


# ── Footer ────────────────────────────────────────────────────────────────────

st.divider()
st.caption(
    "Financial Data Pipeline Dashboard | "
    f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC | "
    "[GitHub](https://github.com/Zanderl1987/financial-data-pipeline)"
)