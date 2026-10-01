"""
Live progress dashboard for the Massive option-listings backfill
(strike-introduction study). Read-only: it reads the checkpoints, summary and
change files the detached job writes, and refreshes every 30 seconds.

Run:
  C:\\ProgramData\\anaconda3\\python.exe -m streamlit run scripts/massive_backfill_dashboard.py
"""
import os
import sys

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

import massive_backfill_progress as mbp  # noqa: E402

# Env overrides exist for the render test (synthetic data in a temp dir).
OUT_ROOT = os.environ.get("MASSIVE_PROGRESS_ROOT", "storage")
UNIVERSE = os.environ.get("MASSIVE_PROGRESS_UNIVERSE",
                          os.path.join("experiments", "strike_intro_universe.csv"))
LOG = os.path.join("storage", "logs", "massive_listings_backfill.log")
HISTORY_DAYS = 725

# Reference palette (dataviz skill): categorical slots 1-3 validate all-pairs.
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
GRID = "rgba(128,128,128,0.18)"

st.set_page_config(page_title="Strike-listing backfill", layout="wide")
st.title("Strike-listing backfill - live progress")
st.caption("Massive as_of contract history, 2 years x 100 names. Refreshes every 30 s; "
           "reads files only, never the API.")


@st.cache_data(ttl=3600)
def _days() -> list[str]:
    import query as q
    start = (pd.Timestamp.today() - pd.Timedelta(days=HISTORY_DAYS)).date().isoformat()
    df = q.sql(f"""SELECT DISTINCT CAST(date AS VARCHAR) AS d FROM prices
                   WHERE symbol = 'SPY' AND date >= '{start}' ORDER BY d""")
    return [d[:10] for d in df["d"]]


@st.cache_data(ttl=600)
def _closes(symbol: str, start: str) -> pd.DataFrame:
    import query as q
    df = q.sql(f"""SELECT CAST(date AS VARCHAR) AS date, close FROM prices
                   WHERE symbol = '{symbol}' AND date >= '{start}' ORDER BY date""")
    df["date"] = df["date"].str[:10]
    return df.drop_duplicates("date")


def _layout(fig, title, ytitle, height=320):
    fig.update_layout(title=title, height=height, margin=dict(l=10, r=10, t=40, b=10),
                      hovermode="x unified", legend=dict(orientation="h", y=1.12, x=0),
                      yaxis_title=ytitle)
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(gridcolor=GRID, zeroline=False)
    return fig


@st.fragment(run_every="30s")
def live():
    symbols = pd.read_csv(UNIVERSE)["symbol"].tolist()
    days = _days()
    prog = mbp.load_progress(OUT_ROOT, symbols, days)
    summ = mbp.load_summaries(OUT_ROOT)

    started = pd.to_datetime(summ["fetched_at"]).min() if not summ.empty else None
    eta = mbp.estimate_eta(prog, started, pd.Timestamp.now("UTC").tz_localize(None)) \
        if started is not None else {"hours_left": None, "remaining_days": None}
    done_days, total_days = int(prog["days_done"].sum()), int(prog["days_total"].sum())
    missing = int((summ["status"] == "missing").sum()) if not summ.empty else 0

    c = st.columns(5)
    c[0].metric("Symbols complete", f"{(prog['status'] == 'done').sum()} / {len(prog)}")
    c[1].metric("Symbol-days processed", f"{done_days:,} / {total_days:,}",
                f"{100 * done_days / max(total_days, 1):.1f}%", delta_color="off")
    c[2].metric("Estimated time left",
                "-" if eta["hours_left"] is None else
                f"{eta['hours_left'] / 24:.1f} days" if eta["hours_left"] > 48 else
                f"{eta['hours_left']:.1f} h")
    c[3].metric("Missing days (API empty)", missing)
    running = prog[prog["status"] == "in progress"]["symbol"].tolist()
    c[4].metric("Working on", running[0] if running else "-")
    st.caption(f"Last refresh: {pd.Timestamp.now():%Y-%m-%d %H:%M:%S}")

    left, right = st.columns([1, 2])
    with left:
        st.subheader("Per-symbol progress")
        st.dataframe(
            prog[["symbol", "pct", "days_done", "last_date", "status"]],
            hide_index=True, height=560,
            column_config={"pct": st.column_config.ProgressColumn(
                "Progress", min_value=0, max_value=100, format="%.0f%%")})

    with right:
        st.subheader("Symbol explorer")
        have = prog[prog["days_done"] > 0]["symbol"].tolist()
        if not have:
            st.info("No symbol has a checkpoint yet - the first one lands after "
                    "20 trading days of that symbol are fetched (~1 hour).")
            return
        sym = st.selectbox("Symbol", have, index=0, key="sym")
        s = summ[(summ["symbol"] == sym) & (summ["status"] == "ok")].sort_values("date")
        px = _closes(sym, s["date"].min())
        px = px[px["date"] <= s["date"].max()]

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=s["date"], y=s["max_strike"], name="Highest strike",
                                 line=dict(color=BLUE, width=1), opacity=0.6))
        fig.add_trace(go.Scatter(x=s["date"], y=s["min_strike"], name="Lowest strike",
                                 line=dict(color=BLUE, width=1), opacity=0.6,
                                 fill="tonexty", fillcolor="rgba(42,120,214,0.10)"))
        fig.add_trace(go.Scatter(x=px["date"], y=px["close"], name="Stock close",
                                 line=dict(color=ORANGE, width=2)))
        st.plotly_chart(_layout(fig, f"{sym}: price inside its listed strike range",
                                "USD"), use_container_width=True)

        ch = mbp.load_changes(OUT_ROOT, sym)
        if not ch.empty:
            ch = ch[ch["change"].isin(["added", "removed"])]
            daily = ch.groupby(["date", "change"]).size().unstack(fill_value=0)
            daily = daily.reindex(columns=["added", "removed"], fill_value=0)
            fig2 = go.Figure()
            fig2.add_trace(go.Bar(x=daily.index, y=daily["added"], name="Contracts added",
                                  marker_color=AQUA))
            fig2.add_trace(go.Bar(x=daily.index, y=-daily["removed"],
                                  name="Contracts removed (expired/delisted)",
                                  marker_color=ORANGE,
                                  customdata=daily["removed"],
                                  hovertemplate="%{customdata}"))
            fig2.update_layout(barmode="relative", bargap=0.15)
            st.plotly_chart(_layout(fig2, f"{sym}: contracts added / removed per day",
                                    "contracts"), use_container_width=True)

        fig3 = go.Figure(go.Scatter(x=s["date"], y=s["n_contracts"], name="Listed contracts",
                                    line=dict(color=BLUE, width=2)))
        st.plotly_chart(_layout(fig3, f"{sym}: total listed contracts", "contracts",
                                height=260), use_container_width=True)

    with st.expander("Backfill log (last 40 lines)"):
        if os.path.exists(LOG):
            with open(LOG, encoding="utf-8", errors="replace") as f:
                st.code("".join(f.readlines()[-40:]) or "(empty)")
        else:
            st.write("Log not created yet - the backfill has not been launched.")


live()
