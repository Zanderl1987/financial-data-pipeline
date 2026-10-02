"""
Live view of a strike-introduction study run: stage progress, the leave-one-
symbol-out model's coefficients as each fold finishes, and the permutation
null filling in. Reads storage/reports/strike_intro/<run_id>/progress.json.

Run:
  C:\\ProgramData\\anaconda3\\python.exe -m streamlit run scripts/strike_intro_study_dashboard.py --server.port 8502 --server.headless true
"""
import glob
import os
import sys

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)
from strike_intro.progress import read_progress  # noqa: E402

RUNS = os.environ.get("STRIKE_INTRO_RUNS", os.path.join("storage", "reports", "strike_intro"))
BLUE, ORANGE = "#2a78d6", "#eb6834"

st.set_page_config(page_title="Strike-intro study run", layout="wide")
st.title("Strike-introduction study - live run")


@st.fragment(run_every="2s")
def live():
    runs = sorted(glob.glob(os.path.join(RUNS, "*")))
    if not runs:
        st.info("No study run yet. Start one: python -m strike_intro.run")
        return
    run = st.selectbox("Run", runs[::-1], format_func=os.path.basename, key="run")
    p = read_progress(run)
    st.caption(f"Updated {p['updated_at']}")
    for name, s in p["stages"].items():
        frac = s["done"] / s["total"] if s["total"] else 0
        st.markdown(f"**{name}** - {s['done']}/{s['total']} {s['note']}")
        st.progress(min(1.0, frac))

    coefs = pd.DataFrame(p["data"].get("loso_coefs", []))
    if not coefs.empty:
        fig = go.Figure()
        for c in [c for c in coefs.columns if c != "symbol"]:
            fig.add_trace(go.Box(y=coefs[c], name=c, marker_color=BLUE, boxpoints="all",
                                 customdata=coefs["symbol"],
                                 hovertemplate="%{customdata}: %{y:.3f}<extra>" + c + "</extra>"))
        fig.update_layout(title=f"Excess model coefficients across {len(coefs)} folds "
                                f"(stable = mechanics are consistent across stocks)",
                          height=380, showlegend=False, margin=dict(l=10, r=10, t=50, b=10))
        st.plotly_chart(fig, use_container_width=True)

    null = p["data"].get("perm_null")
    if null and null.get("sample"):
        fig = go.Figure(go.Histogram(x=[v * 100 for v in null["sample"]], nbinsx=60,
                                     marker_color=BLUE, name="shuffled-label differences"))
        fig.add_vline(x=null["observed"] * 100, line_color=ORANGE, line_width=3,
                      annotation_text="observed")
        fig.update_layout(title=f"Permutation null - {null['label']} (% points)", height=340,
                          margin=dict(l=10, r=10, t=50, b=10))
        st.plotly_chart(fig, use_container_width=True)


live()
