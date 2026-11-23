"""Streamlit dashboard (layout only; logic in dashboard_data). Paper accounts only."""
from __future__ import annotations

import base64
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

from paper import dashboard_data as dd
from paper.accounts import ACCOUNTS, LOSS_LIMIT_USD
from paper.broker import NY, Broker
from paper.decide import parse_client_order_id

st.set_page_config(page_title="Paper trading", layout="centered")
RANGES = dd.load_ranges()["accounts"]


def _secret(name):
    try:
        return st.secrets[name]
    except Exception:
        return None


@st.cache_data(ttl=60)
def runlog_rows(repo: str, token: str) -> list[dict]:
    r = requests.get(f"https://api.github.com/repos/{repo}/contents/runs.jsonl",
                     headers={"Authorization": f"Bearer {token}",
                              "Accept": "application/vnd.github+json"}, timeout=30)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return dd.parse_runlog(base64.b64decode(r.json()["content"]).decode("utf-8"))


@st.cache_data(ttl=60)
def trading_days(key: str, secret: str, start: date, end: date) -> list[date]:
    return [c.date for c in Broker(key, secret)._calendar(start, end)]


@st.cache_data(ttl=3600)
def closes_for(symbols: tuple, start: date, end: date) -> pd.DataFrame:
    import yfinance as yf
    if not symbols:
        return pd.DataFrame()
    df = yf.download(list(symbols), start=start, end=end + timedelta(days=1),
                     auto_adjust=False, progress=False)["Close"]
    if isinstance(df, pd.Series):
        df = df.to_frame(symbols[0])
    df.index = [x.date() for x in df.index]
    return df


@st.cache_data(ttl=60)
def account_data(key: str, secret: str, acct_key: str) -> dict:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestTradeRequest
    from alpaca.trading.requests import GetPortfolioHistoryRequest
    b = Broker(key, secret)
    equity, lots = b.fee_inputs()
    hist = b._c.get_portfolio_history(
        GetPortfolioHistoryRequest(period="3M", timeframe="1D"))
    pos = b._c.get_all_positions()
    syms = [str(p.symbol) for p in pos]
    last = {}
    if syms:
        dc = StockHistoricalDataClient(key, secret)
        t = dc.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=syms, feed="iex"))
        last = {s: float(v.price) for s, v in t.items()}
    orders = []
    for o in b._all_orders(days=60):
        p = parse_client_order_id(str(o.client_order_id))
        if p is None or o.filled_at is None:
            continue
        orders.append({"symbol": str(o.symbol),
                       "side": str(getattr(o.side, "value", o.side)).lower(),
                       "date": o.filled_at.astimezone(NY).date(),
                       "price": float(o.filled_avg_price), "qty": float(o.filled_qty),
                       "action": p["action"]})
    positions = []
    for p in pos:
        s = str(p.symbol)
        positions.append({"symbol": s, "side": str(getattr(p.side, "value", p.side)),
                          "qty": float(p.qty), "entry": float(p.avg_entry_price),
                          "last": last.get(s), "pnl": float(p.unrealized_pl)})
    return {"equity": equity, "lots": lots, "orders": orders, "positions": positions,
            "hist_t": [datetime.fromtimestamp(t, NY).date() for t in hist.timestamp],
            "hist_e": [float(e) for e in hist.equity]}


st.title("Paper trading")
launch = dd.parse_launch(_secret("PAPER_LAUNCH_DATE"))
repo, token = _secret("PAPER_LOG_REPO"), _secret("PAPER_LOG_TOKEN")
log_rows = []
if repo and token:
    try:
        log_rows = runlog_rows(repo, token)
    except Exception as e:
        st.warning(f"Run log unavailable ({type(e).__name__}).")
else:
    st.warning("Run log secrets missing: missed days unavailable.")
if launch is None:
    st.info("Not launched: PAPER_LAUNCH_DATE secret is not set. "
            "No verdicts or missed days yet.")

today = date.today()
summary = []
for a in ACCOUNTS:
    st.subheader(f"{a.key}  ({a.slug})")
    k, s = _secret(a.env_key), _secret(a.env_secret)
    if not k or not s:
        st.error(f"Missing secrets {a.env_key} / {a.env_secret}.")
        continue
    try:
        d = account_data(k, s, a.key)
        days = trading_days(k, s, launch, today) if launch else []
    except Exception as e:
        st.error(f"Could not load account ({type(e).__name__}).")
        continue
    band = RANGES[a.key]["band"]
    adj = dd.fee_adjusted(d["equity"], d["lots"], today)
    ret = (adj - a.start_equity) / a.start_equity
    dist = dd.limit_distance(a.start_equity, adj)
    idx = dd.day_index(days, launch, today)
    stopped = dist <= 0 or any(
        r.get("account") == a.key and r.get("status") == "stopped" for r in log_rows)
    c1, c2 = st.columns(2)
    c1.metric("Equity (net of borrow fee)", f"${adj:,.2f}", f"{ret:+.2%}")
    c2.metric(f"Room before ${LOSS_LIMIT_USD:,.0f} limit", f"${dist:,.2f}")

    fig = go.Figure()
    if launch and days:
        xs = days[:63]
        for ky in ("p5", "p50", "p95"):
            ys = [a.start_equity * (1 + v) for v in band[ky][:len(xs)]]
            fig.add_scatter(x=xs, y=ys, name=ky, line=dict(dash="dot", width=1))
    fig.add_scatter(x=d["hist_t"], y=d["hist_e"], name="equity (raw)")
    fig.update_layout(height=280, margin=dict(l=0, r=0, t=10, b=0),
                      legend=dict(orientation="h"))
    st.plotly_chart(fig, use_container_width=True)

    if d["positions"]:
        st.caption("Open positions (shorts: borrow fee is in the equity above)")
        st.dataframe(pd.DataFrame(d["positions"]), use_container_width=True,
                     hide_index=True)
    else:
        st.caption("No open positions.")

    fills = pd.DataFrame(d["orders"])
    if not fills.empty:
        todays = fills[fills["date"] == today]
        st.caption("Fills today")
        st.dataframe(todays, use_container_width=True, hide_index=True)
        try:
            cl = closes_for(tuple(sorted(set(fills["symbol"]))),
                            fills["date"].min(), today)
            costs = dd.trade_costs(fills, cl).dropna(subset=["cost_bps"])
            if not costs.empty:
                st.caption(f"Cost vs close: mean {costs['cost_bps'].mean():.1f} bps "
                           f"(expected {RANGES[a.key]['cost_bps_mean']:.1f} bps; "
                           "shown, not judged)")
        except Exception as e:
            st.caption(f"Cost calc unavailable ({type(e).__name__}).")

    row = {"account": a.key, "return": f"{ret:+.2%}"}
    if launch:
        row["status"] = dd.account_status(idx, ret, band, stopped)
        row["missed days"] = len(dd.missed_days(days, log_rows, a.key))
        if idx >= dd.HORIZON:
            row["verdict"] = dd.verdict(ret, (band["p5"][62], band["p95"][62]), stopped)
    else:
        row["status"] = "not launched"
    summary.append(row)

st.header("Summary")
if summary:
    st.dataframe(pd.DataFrame(summary), use_container_width=True, hide_index=True)
