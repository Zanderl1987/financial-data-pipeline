"""Interactive HTML report for the strike-introduction study (Plotly)."""
import html as _html

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio

HIT, MISS, THIRD = "#2a78d6", "#eb6834", "#1baf7a"
HORIZONS = [1, 3, 5, 10, 21, 63, 126]


def _rgba(hex_color: str, alpha: float) -> str:
    """Plotly's validator rejects 8-digit hex; translucent fills need rgba()."""
    h = hex_color.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{alpha})"
_CSS = """
:root{--bg:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--rule:#e4e3df}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--rule:#33332f}}
:root[data-theme="dark"]{--bg:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--rule:#33332f}
body{background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif;margin:0 auto;max-width:1100px;padding:16px}
h1{font-size:24px}h2{font-size:18px;margin-top:32px}p.note{color:var(--ink2)}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid var(--rule);padding:4px 6px;text-align:right}
th{cursor:pointer}td:first-child,th:first-child{text-align:left}.card{border:1px solid var(--rule);border-radius:8px;padding:12px 16px}
"""
_SORT_JS = """
document.querySelectorAll('table.sortable th').forEach((th,i)=>th.onclick=()=>{
 const tb=th.closest('table').tBodies[0],rows=[...tb.rows],asc=th.dataset.asc!=='1';
 rows.sort((a,b)=>{const x=a.cells[i].innerText,y=b.cells[i].innerText,nx=parseFloat(x),ny=parseFloat(y);
  return (isNaN(nx)||isNaN(ny)?x.localeCompare(y):nx-ny)*(asc?1:-1)});
 th.dataset.asc=asc?'1':'0';rows.forEach(r=>tb.appendChild(r));});
"""


def _layout(fig, title, ytitle, height=380):
    fig.update_layout(title=title, height=height, template="plotly_white",
                      margin=dict(l=10, r=10, t=50, b=10), hovermode="x unified",
                      legend=dict(orientation="h", y=1.1, x=0), yaxis_title=ytitle,
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    return fig


def _mean_band(m: pd.DataFrame):
    mu = m.mean()
    se = m.std() / np.sqrt(m.notna().sum())
    return mu, mu - 1.96 * se, mu + 1.96 * se


def _car_figure(ev, car):
    fig = go.Figure()
    combos = [(t, d, e) for t in sorted(ev["trigger"].unique())
              for d in ("all", "up", "down") for e in ("all", "earnings", "non-earnings")]
    buttons = []
    per = 6
    for ci, (t, d, e) in enumerate(combos):
        m = ev["trigger"] == t
        if d != "all":
            m &= ev["direction"] == (1 if d == "up" else -1)
        if e != "all":
            m &= ev["earnings"] == (e == "earnings")
        for name, col, sel in (("HIT", HIT, ev["hit"]), ("MISS", MISS, ~ev["hit"])):
            rows = car.loc[ev.index[m & sel]]
            mu, lo, hi = _mean_band(rows) if len(rows) else (pd.Series(dtype=float),) * 3
            x = list(car.columns)
            fig.add_trace(go.Scatter(x=x, y=hi * 100, line=dict(width=0), showlegend=False,
                                     hoverinfo="skip", visible=ci == 0))
            fig.add_trace(go.Scatter(x=x, y=lo * 100, fill="tonexty", line=dict(width=0),
                                     fillcolor=_rgba(col, 0.13), showlegend=False, hoverinfo="skip",
                                     visible=ci == 0))
            fig.add_trace(go.Scatter(x=x, y=mu * 100, name=f"{name} (n={len(rows)})",
                                     line=dict(color=col, width=2), visible=ci == 0))
        vis = [False] * (len(combos) * per)
        vis[ci * per:(ci + 1) * per] = [True] * per
        buttons.append(dict(label=f"{t} | {d} | {e}", method="update", args=[{"visible": vis}]))
    fig.update_layout(updatemenus=[dict(buttons=buttons, x=0, y=1.25, xanchor="left")])
    fig.add_vline(x=0, line_dash="dot", line_color="#888")
    return _layout(fig, "Return paths after the event (signed excess vs SPY, entry = day 0)",
                   "% (positive = continuation)", 460)


def _diff_figure(tests):
    t = tests[(tests["measure"] == "ret") & (tests["subgroup"] == "all")]
    fig = go.Figure()
    for trig, col in zip(sorted(t["trigger"].unique()), (HIT, THIRD)):
        g = t[t["trigger"] == trig].sort_values("horizon")
        fig.add_trace(go.Bar(
            x=[str(h) for h in g["horizon"]], y=g["diff"] * 100, name=f"{trig} trigger",
            marker_color=col,
            error_y=dict(type="data", symmetric=False, array=(g["ci_hi"] - g["diff"]) * 100,
                         arrayminus=(g["diff"] - g["ci_lo"]) * 100),
            customdata=np.c_[g["p_perm"], g["p_adj"], g["n_a"], g["n_b"]],
            hovertemplate="h=%{x}d diff=%{y:.2f}%<br>p_perm=%{customdata[0]:.3f} "
                          "p_adj=%{customdata[1]:.3f}<br>n HIT=%{customdata[2]} "
                          "MISS=%{customdata[3]}<extra></extra>"))
    fig.update_layout(barmode="group")
    return _layout(fig, "HIT minus MISS by horizon (95% week-block bootstrap CI)",
                   "difference, % points")


def _tercile_figure(ev):
    d = ev[ev["trigger"] == "day"].dropna(subset=["excess", "ret_21"])
    fig = go.Figure()
    if len(d) >= 30:
        d = d.assign(t=pd.qcut(d["excess"].rank(method="first"), 3, labels=["low", "mid", "high"]))
        g = d.groupby("t", observed=True)["ret_21"].agg(["mean", "count", "std"])
        fig.add_trace(go.Bar(x=g.index.astype(str), y=g["mean"] * 100, marker_color=HIT,
                             name="mean 21d return",
                             error_y=dict(type="data", array=1.96 * g["std"] / np.sqrt(g["count"]) * 100),
                             customdata=g["count"], hovertemplate="%{x}: %{y:.2f}% (n=%{customdata})<extra></extra>"))
    return _layout(fig, "Forward return by EXCESS tercile (day trigger, 21 days)",
                   "% signed excess")


def _vol_figure(ev):
    fig = go.Figure()
    for name, col, sel in (("HIT", HIT, ev["hit"]), ("MISS", MISS, ~ev["hit"])):
        fig.add_trace(go.Box(y=ev.loc[sel, "absret_21"] * 100, name=f"{name} |21d move|",
                             marker_color=col, boxmean=True))
        fig.add_trace(go.Box(y=ev.loc[sel, "rv_ratio"], name=f"{name} vol ratio",
                             marker_color=col, boxmean=True, visible="legendonly"))
    return _layout(fig, "Volatility check: size of the next 21 days' move (direction ignored)",
                   "% / ratio")


def _scatter_figure(ev):
    d = ev[ev["trigger"] == "day"]
    fig = go.Figure(go.Scatter(
        x=d["excess"], y=d["ret_21"] * 100, mode="markers",
        marker=dict(size=8, color=np.where(d["hit"], HIT, MISS), opacity=0.7,
                    line=dict(width=1, color="white")),
        customdata=np.c_[d["symbol"], d["event_date"].astype(str).str[:10], d["earnings"]],
        hovertemplate="%{customdata[0]} %{customdata[1]}<br>EXCESS=%{x:.2f}<br>"
                      "21d=%{y:.2f}%<br>earnings=%{customdata[2]}<extra></extra>",
        name="events (blue = HIT, orange = MISS)"))
    fig.update_layout(hovermode="closest")
    return _layout(fig, "Event scatter: EXCESS vs 21-day signed return (day trigger)", "%")


def _ranges_figure(ranges):
    fig = go.Figure()
    syms = sorted(ranges["symbol"].unique())
    buttons = []
    for i, s in enumerate(syms):
        g = ranges[ranges["symbol"] == s]
        vis = i == 0
        fig.add_trace(go.Scatter(x=g["date"], y=g["near_max"], name="near-expiry max strike",
                                 line=dict(color=HIT, width=1), visible=vis))
        fig.add_trace(go.Scatter(x=g["date"], y=g["near_min"], name="near-expiry min strike",
                                 line=dict(color=HIT, width=1), fill="tonexty",
                                 fillcolor=_rgba(HIT, 0.10), visible=vis))
        fig.add_trace(go.Scatter(x=g["date"], y=g["close"], name="close",
                                 line=dict(color=MISS, width=2), visible=vis))
        up = g[g["above"] > 0]
        dn = g[g["below"] > 0]
        fig.add_trace(go.Scatter(x=up["date"], y=up["near_max"], mode="markers",
                                 name="strikes added above", marker=dict(color=THIRD, size=8,
                                 symbol="triangle-up"), visible=vis))
        fig.add_trace(go.Scatter(x=dn["date"], y=dn["near_min"], mode="markers",
                                 name="strikes added below", marker=dict(color=THIRD, size=8,
                                 symbol="triangle-down"), visible=vis))
        v = [False] * (5 * len(syms))
        v[i * 5:(i + 1) * 5] = [True] * 5
        buttons.append(dict(label=s, method="update", args=[{"visible": v}]))
    fig.update_layout(updatemenus=[dict(buttons=buttons, x=0, y=1.2, xanchor="left")])
    return _layout(fig, "Strike-range explorer: price inside its nearest-expiry strike range",
                   "USD", 440)


def _equity_figure(eq):
    fig = go.Figure(go.Scatter(x=eq.index, y=eq * 100, line=dict(color=HIT, width=2),
                               name="cumulative return"))
    return _layout(fig, "Trading view (illustrative): long up-move HITs / short down-move HITs, "
                        "21-day hold, 10 bp per side", "% cumulative")


def _table(df, cls="sortable"):
    head = "".join(f"<th>{_html.escape(str(c))}</th>" for c in df.columns)
    body = "".join("<tr>" + "".join(
        f"<td>{v:.4g}</td>" if isinstance(v, (float, np.floating)) else f"<td>{_html.escape(str(v))}</td>"
        for v in r) + "</tr>" for r in df.itertuples(index=False))
    return f'<table class="{cls}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def build_report(results: dict, path: str) -> str:
    ev, tests, meta = results["events"], results["tests"], results["meta"]
    prim = tests[tests["primary"]].iloc[0] if tests["primary"].any() else None
    figs = [_car_figure(ev, results["car"]), _diff_figure(tests), _tercile_figure(ev),
            _vol_figure(ev), _scatter_figure(ev), _ranges_figure(results["ranges"]),
            _equity_figure(results["equity"])]
    parts = [pio.to_html(f, full_html=False, include_plotlyjs=False) for f in figs]
    primary = ("<p>No primary result.</p>" if prim is None else
               f"<p><b>HIT − MISS, day trigger, 21 days:</b> {prim['diff']*100:.2f} pts "
               f"(95% CI {prim['ci_lo']*100:.2f} to {prim['ci_hi']*100:.2f}); "
               f"n = {int(prim['n_a'])} HIT / {int(prim['n_b'])} MISS; "
               f"permutation p = {prim['p_perm']:.3f}, Welch p = {prim['p_welch']:.3f}, "
               f"bootstrap p = {prim['p_boot']:.3f}, Mann-Whitney p = {prim['p_mw']:.3f}; "
               f"Cohen's d = {prim['cohen_d']:.2f}. Minimum detectable effect at 80% power: "
               f"{meta.get('mde_21', float('nan'))*100:.2f} pts.</p>")
    body = f"""
<h1>Option strike introductions after a move</h1>
<p class="note">Universe {meta['universe']} names ({meta['complete']} with complete listing
history), {_html.escape(str(meta['date_range']))}. Returns are excess vs SPY, signed so that
positive = the move continued. Entry is the close 3 trading days after the move.</p>
<div class="card"><h2>Primary test (pre-registered)</h2>{primary}</div>
{parts[0]}{parts[1]}{parts[2]}{parts[3]}{parts[4]}{parts[5]}
<h2>All tests</h2><p class="note">Click a column to sort. p_adj = Benjamini-Hochberg across
all secondary tests.</p>{_table(tests)}
<h2>Regression (two-way clustered by entry date and symbol)</h2>{_table(results['regression'], 'plain')}
{parts[6]}"""
    doc = (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' "
           f"content='width=device-width,initial-scale=1'><title>Strike Introduction Study</title>"
           f"<script src='https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js'></script>"
           f"<style>{_CSS}</style></head><body>{body}<script>{_SORT_JS}</script></body></html>")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path
