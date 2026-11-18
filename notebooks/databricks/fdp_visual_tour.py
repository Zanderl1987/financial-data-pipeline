# Databricks notebook source
# MAGIC %md
# MAGIC # Financial Data Pipeline: a visual tour
# MAGIC
# MAGIC This notebook reads the pipeline's public output straight from Hugging Face and charts it.
# MAGIC Nothing is copied by hand: DuckDB queries the Parquet files over HTTP (`hf://`), reading only
# MAGIC the columns and row groups each chart needs, so even the 2 GB `prices` table answers in seconds.
# MAGIC
# MAGIC | Dataset | What it holds |
# MAGIC |---|---|
# MAGIC | [`ZanderL1337/financial-data-pipeline`](https://huggingface.co/datasets/ZanderL1337/financial-data-pipeline) | ~240 curated tables from ~100 free/public sources: prices, options, rates, macro, fund holdings, energy, filings, sentiment |
# MAGIC | [`ZanderL1337/financial-fundamentals`](https://huggingface.co/datasets/ZanderL1337/financial-fundamentals) | SEC XBRL company facts normalized to 10 core metrics for ~17,000 filers |
# MAGIC
# MAGIC **How the data gets there.** Each source has its own `*_pipeline.py` that writes partitioned
# MAGIC Parquet. `curated.py` deduplicates the raw store on each table's natural key, and
# MAGIC `upload_huggingface.py` publishes the curated snapshot nightly, with a guard that refuses to
# MAGIC publish a table that lost rows.
# MAGIC
# MAGIC ```
# MAGIC  ~100 public APIs ──> *_pipeline.py ──> raw Parquet (partitioned) ──> curated.py (dedup) ──> Hugging Face
# MAGIC                                                                                                │
# MAGIC                         this notebook:  DuckDB hf:// ──> pandas / Spark Delta ──> Plotly  <────┘
# MAGIC ```
# MAGIC
# MAGIC **Sections**
# MAGIC 1. The dataset as a whole: size, shape, history depth, freshness, publishing cadence
# MAGIC 2. Lakehouse: land a slice in Unity Catalog Delta tables and analyze it with Spark SQL
# MAGIC 3. Markets: cross-asset growth, drawdowns, sector correlation, volatility term structure
# MAGIC 4. Rates and macro: the yield curve in 3D, inversions, the macro dashboard, global policy rates
# MAGIC 5. Options: volatility smile and surface, open interest, put/call history (pick the symbol above)
# MAGIC 6. Holdings and ownership: fund composition, concentration, congressional trading
# MAGIC 7. Fundamentals: SEC filing coverage, margins by sector
# MAGIC 8. Sentiment and alternative data: news, ratings, complaints, housing, supply chains
# MAGIC 9. Pipeline health: signal monitor and column completeness
# MAGIC 10. Energy and commodities: futures scoreboard, natural gas storage, speculator positioning
# MAGIC 11. Government, banks and credit: federal debt, Treasury auction demand, bank failures since 1934
# MAGIC 12. Insiders and settlement fails: Form 4 buying and selling, SEC fails-to-deliver
# MAGIC 13. Strategy research: 35 TradingView strategies tested against a random-timing null
# MAGIC 14. Lakehouse engineering: bronze/silver/gold layers, an idempotent MERGE, constraints, time travel
# MAGIC 15. Machine learning with MLflow: forecasting SPY volatility from the gold table, walk-forward
# MAGIC
# MAGIC Use the widgets at the top to change the options symbol, the fund, the lookback window, and
# MAGIC whether sections 2 and 14 write Delta tables.

# COMMAND ----------

# MAGIC %pip install -q "duckdb>=1.4" "plotly>=5.22" "huggingface_hub>=0.24" "scikit-learn>=1.3" "mlflow-skinny[databricks]>=2.16"

# COMMAND ----------

try:
    dbutils.library.restartPython()  # noqa: F821  (Databricks only)
except NameError:
    pass

# COMMAND ----------

# MAGIC %md
# MAGIC ## Setup
# MAGIC Connection, widgets, a chart theme, and three helpers: `T()` names a table on Hugging Face,
# MAGIC `q()` runs DuckDB SQL into pandas, and `show()` renders a figure.

# COMMAND ----------

import io
import json
import math
import os
import time
import urllib.request
from datetime import date, datetime, timedelta

import duckdb
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio
from huggingface_hub import HfApi
from plotly.subplots import make_subplots

ON_DATABRICKS = "dbutils" in globals()

OPTION_SYMBOLS = ["SPY", "QQQ", "NVDA", "TSLA", "AAPL", "MSFT", "META", "AMD", "GOOGL", "MU"]
FUNDS = ["SPY", "IVV", "DIA", "ITOT", "IJH", "IJR", "DGRO", "HDV", "ARKK", "EFA"]

if ON_DATABRICKS:
    dbutils.widgets.dropdown("options_symbol", "SPY", OPTION_SYMBOLS, "Options symbol")  # noqa: F821
    dbutils.widgets.dropdown("fund", "SPY", FUNDS, "Fund")  # noqa: F821
    dbutils.widgets.dropdown("lookback_years", "10", ["1", "3", "5", "10", "20"], "Lookback (years)")  # noqa: F821
    dbutils.widgets.dropdown("write_delta", "yes", ["yes", "no"], "Write Delta tables")  # noqa: F821
    W = {k: dbutils.widgets.get(k) for k in ["options_symbol", "fund", "lookback_years", "write_delta"]}  # noqa: F821
else:
    W = {"options_symbol": "SPY", "fund": "SPY", "lookback_years": "10", "write_delta": "no"}

OPT_SYM = W["options_symbol"]
FUND = W["fund"]
LOOKBACK_YEARS = int(W["lookback_years"])
WRITE_DELTA = W["write_delta"] == "yes" and "spark" in globals()
TODAY = pd.Timestamp(date.today())
START = (TODAY - pd.DateOffset(years=LOOKBACK_YEARS)).date().isoformat()

REPO = "ZanderL1337/financial-data-pipeline"
FUND_REPO = "ZanderL1337/financial-fundamentals"
HF = f"hf://datasets/{REPO}"


def T(table: str) -> str:
    """SQL literal for a curated table on Hugging Face."""
    return f"'{HF}/{table}/{table}.parquet'"


def F(name: str) -> str:
    """SQL literal for a file in the fundamentals dataset."""
    return f"'hf://datasets/{FUND_REPO}/{name}.parquet'"


con = duckdb.connect()
con.execute("INSTALL httpfs; LOAD httpfs; SET threads = 8;")


def q(sql: str) -> pd.DataFrame:
    return con.execute(sql).df()


# ---- chart theme: one validated categorical palette, sequential blue, blue<->red diverging ----
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
SEQ = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
SEQ_SCALE = [[i / (len(SEQ) - 1), c] for i, c in enumerate(SEQ)]
ORDINAL = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281", "#0d366b"]  # starts at step 250
DIV_SCALE = [[0.0, "#a8292a"], [0.17, "#e34948"], [0.33, "#f3a9a8"], [0.5, "#f0efec"],
             [0.67, "#9ec5f4"], [0.83, "#3987e5"], [1.0, "#184f95"]]
NEG, NEUTRAL, POS = "#e34948", "#a8a69e", "#2a78d6"
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}
INK, INK2, MUTED, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"

_axis = dict(gridcolor=GRID, linecolor=AXIS, zerolinecolor=AXIS, tickfont=dict(color=MUTED),
             title=dict(font=dict(color=INK2)))
pio.templates["fdp"] = go.layout.Template(
    layout=dict(
        font=dict(family="system-ui, -apple-system, Segoe UI, sans-serif", color=INK, size=13),
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE, colorway=PALETTE,
        xaxis=_axis, yaxis=_axis, hovermode="closest",
        title=dict(font=dict(size=17), x=0, xanchor="left"),
        legend=dict(orientation="h", y=1.0, yanchor="bottom", x=0, title=None),
        margin=dict(l=60, r=30, t=80, b=50),
        hoverlabel=dict(bgcolor="white", font=dict(color=INK)),
    ),
    data=dict(scatter=[go.Scatter(line=dict(width=2), marker=dict(size=8))]),
)
pio.templates.default = "fdp"


def show(fig, height=480, subtitle=None):
    if subtitle:
        t = fig.layout.title.text or ""
        fig.update_layout(title=dict(text=f"{t}<br><span style='font-size:12px;color:{INK2}'>{subtitle}</span>"))
    fig.update_layout(height=height)
    fig.show()


def html(s: str):
    if ON_DATABRICKS:
        displayHTML(s)  # noqa: F821
    else:
        from IPython.display import HTML, display as _d
        _d(HTML(s))


def table(df: pd.DataFrame, n=50):
    if ON_DATABRICKS:
        display(df.head(n))  # noqa: F821
    else:
        print(df.head(n).to_string())


def tiles(items):
    """Row of stat tiles: [(label, value, note), ...]."""
    cells = "".join(
        f"<div style='flex:1;min-width:150px;padding:14px 16px;border:1px solid rgba(11,11,11,.10);"
        f"border-radius:10px;background:{SURFACE}'>"
        f"<div style='font-size:12px;color:{INK2}'>{label}</div>"
        f"<div style='font-size:28px;font-weight:600;color:{INK};margin-top:2px'>{value}</div>"
        f"<div style='font-size:12px;color:{MUTED};margin-top:2px'>{note}</div></div>"
        for label, value, note in items
    )
    html(f"<div style='display:flex;flex-wrap:wrap;gap:12px;font-family:system-ui,sans-serif'>{cells}</div>")


print(f"widgets: {W}  |  Spark Delta writes: {WRITE_DELTA}  |  lookback start: {START}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. The dataset as a whole
# MAGIC
# MAGIC Every table is one Parquet file. Instead of downloading 6 GB, the inventory below reads only
# MAGIC each file's footer: row counts, column names, and the min/max statistics Parquet keeps per
# MAGIC row group. Those statistics give each table's history span and freshness without scanning
# MAGIC a single data page.

# COMMAND ----------

api = HfApi()
tree = [f for f in api.list_repo_tree(REPO, repo_type="dataset", recursive=True)
        if getattr(f, "size", None) is not None and f.path.endswith(".parquet")]
files = pd.DataFrame({"path": [f.path for f in tree], "bytes": [f.size for f in tree]})
files["table"] = files["path"].str.split("/").str[0]
files["file_name"] = HF + "/" + files["path"]

path_list = "[" + ",".join(f"'{p}'" for p in files["file_name"]) + "]"
t0 = time.time()
meta = q(f"""
    SELECT file_name, row_group_id, row_group_num_rows, path_in_schema,
           stats_min_value, stats_max_value, stats_null_count
    FROM parquet_metadata({path_list})
""")
print(f"read {len(files)} footers ({len(meta):,} row-group/column entries) in {time.time() - t0:.1f}s")

# COMMAND ----------

# Which column carries each table's timeline: the first match in this list, else fetched_at.
DATE_COLS = ["date", "snapshot_date", "record_date", "run_date", "period_end", "date_received",
             "transaction_date", "faildate", "filing_date", "reporting_period_end", "obs_date",
             "report_date", "week", "timestamp", "datetime", "fetched_at"]

# Domain of each table, first matching prefix wins (8 domains = the 8 palette slots, in order).
DOMAINS = ["Markets & prices", "Options & volatility", "Macro & rates", "Government & fiscal",
           "Energy & commodities", "Holdings & ownership", "Fundamentals & filings", "Sentiment & alt data"]
RULES = [
    ("Macro & rates", ["treasury_yield_curve", "treasury_avg_interest", "fred_", "macro", "bls_", "bea_", "ecb_",
                       "policy_rates", "oecd", "world_bank", "ff_", "shiller", "fhfa", "zillow", "indeed",
                       "tsa_", "treasury_tic", "treasury_exchange", "fed_soma"]),
    ("Options & volatility", ["options", "option_", "schwab_options", "synthetic_options", "cboe_"]),
    ("Holdings & ownership", ["fund_holdings", "etf_holdings", "institutional", "insider", "alpha_vantage_insider",
                              "finviz_insider", "congressional", "california_disclosures", "finnhub_lobbying",
                              "lda_lobbying"]),
    ("Fundamentals & filings", ["fundamentals_", "simfin_", "sa_balance", "sa_cashflow", "sa_income", "sa_ratios",
                                "tiingo_fundamentals", "sec_", "finnhub_metrics", "finnhub_earnings",
                                "alpha_vantage_earnings", "alpha_vantage_overview", "alpha_vantage_dividends",
                                "earnings_calendar", "company_profile", "finnhub_profile", "finnhub_peers",
                                "finnhub_sec", "sa_corporate", "tiingo_corporate", "finviz_financials", "ipo_",
                                "sa_ipo", "delisting", "securities", "identifier_map", "sa_stock_list", "sa_etf_list",
                                "sp500_membership", "index_members"]),
    ("Government & fiscal", ["treasury_", "fdic_", "cfpb", "usaspending", "finnhub_usa_spending", "openfda",
                             "finnhub_uspto", "finnhub_visa", "epa_"]),
    ("Energy & commodities", ["eia_", "gas_", "commodities", "agriculture", "imf_", "wb_", "fao_", "usda_", "usgs_",
                              "gem_", "metals", "plastics", "cot", "shipping_", "jmic", "piracy", "somali", "noaa",
                              "open_meteo", "futures"]),
    ("Sentiment & alt data", ["news_sentiment", "retail_sentiment", "fear_greed", "finnhub_news",
                              "finnhub_recommendations", "tv_ratings", "google_trends", "wikipedia", "signal_health",
                              "finviz_screener"]),
]


def domain_of(t: str) -> str:
    for dom, prefixes in RULES:
        if any(t.startswith(p) for p in prefixes):
            return dom
    return "Markets & prices"


rg = meta.drop_duplicates(["file_name", "row_group_id"])
inv = files.merge(rg.groupby("file_name", as_index=False)["row_group_num_rows"].sum()
                  .rename(columns={"row_group_num_rows": "rows"}), on="file_name", how="left")
inv = inv.merge(meta.groupby("file_name", as_index=False)["path_in_schema"].nunique()
                .rename(columns={"path_in_schema": "columns"}), on="file_name", how="left")

cols_by_file = meta.groupby("file_name")["path_in_schema"].apply(set)
inv["date_col"] = inv["file_name"].map(lambda f: next((c for c in DATE_COLS if c in cols_by_file.get(f, set())), None))
dm = meta.merge(inv[["file_name", "date_col"]], left_on=["file_name", "path_in_schema"],
                right_on=["file_name", "date_col"])
dm["lo"] = pd.to_datetime(dm["stats_min_value"].astype(str).str[:10], errors="coerce")
dm["hi"] = pd.to_datetime(dm["stats_max_value"].astype(str).str[:10], errors="coerce")
span = dm.groupby("file_name").agg(first_obs=("lo", "min"), last_obs=("hi", "max")).reset_index()
inv = inv.merge(span, on="file_name", how="left")

inv["domain"] = pd.Categorical(inv["table"].map(domain_of), categories=DOMAINS, ordered=True)
inv["mb"] = inv["bytes"] / 1e6
inv["future_dated"] = inv["last_obs"] > TODAY + pd.Timedelta(days=31)
inv["last_obs_capped"] = inv["last_obs"].where(~inv["future_dated"], TODAY)
inv["lag_days"] = (TODAY - inv["last_obs_capped"]).dt.days.clip(lower=0)
inv["history_years"] = (inv["last_obs_capped"] - inv["first_obs"]).dt.days / 365.25
inv = inv.sort_values(["domain", "table"]).reset_index(drop=True)

tiles([
    ("Tables", f"{len(inv):,}", f"in {inv['domain'].nunique()} domains"),
    ("Rows", f"{inv['rows'].sum() / 1e6:,.1f}M", "after deduplication"),
    ("Size on Hugging Face", f"{inv['bytes'].sum() / 1e9:,.2f} GB", "Parquet, compressed"),
    ("Columns", f"{inv['columns'].sum():,}", f"median {int(inv['columns'].median())} per table"),
    ("Deepest history", f"{inv['first_obs'].min():%Y}", inv.loc[inv['first_obs'].idxmin(), 'table']),
    ("Fresh within 3 days", f"{(inv['lag_days'] <= 3).mean():.0%}", "of tables, by last observation"),
])

# COMMAND ----------

fig = px.treemap(
    inv.assign(log_rows=np.log10(inv["rows"].clip(lower=1)), domain=inv["domain"].astype(str)),
    path=[px.Constant("financial-data-pipeline"), "domain", "table"], values="mb",
    color="log_rows", color_continuous_scale=SEQ_SCALE,
    hover_data={"rows": ":,", "columns": True, "mb": ":.2f", "log_rows": False},
)
fig.update_traces(marker=dict(line=dict(color=SURFACE, width=2)), root_color=SURFACE,
                  hovertemplate="<b>%{label}</b><br>%{value:.2f} MB<br>rows: %{customdata[0]:,}<extra></extra>")
fig.update_layout(title="Where the bytes are: every table, sized by storage, shaded by row count",
                  coloraxis_colorbar=dict(title="rows", tickvals=[1, 3, 5, 7], ticktext=["10", "1K", "100K", "10M"]))
show(fig, height=650, subtitle="Click a domain to zoom in. Two tables (prices, CFPB complaints) are 60% of the bytes.")

# COMMAND ----------

by_dom = (inv.groupby("domain", observed=True)
          .agg(tables=("table", "count"), rows=("rows", "sum"), mb=("mb", "sum")).reset_index())
fig = make_subplots(rows=1, cols=3, shared_yaxes=True, horizontal_spacing=0.04,
                    subplot_titles=("Tables", "Rows (millions)", "Storage (MB)"))
for i, (col, scale) in enumerate([("tables", 1), ("rows", 1e6), ("mb", 1)], start=1):
    fig.add_bar(y=by_dom["domain"].astype(str), x=by_dom[col] / scale, orientation="h",
                marker_color=PALETTE[0], showlegend=False, row=1, col=i,
                hovertemplate="%{y}: %{x:,.1f}<extra></extra>")
fig.update_yaxes(autorange="reversed", automargin=True)
fig.update_layout(title="The eight domains, three ways of measuring them", bargap=0.35, margin=dict(l=170))
show(fig, height=420)

# COMMAND ----------

deep = inv.dropna(subset=["first_obs"]).nsmallest(45, "first_obs").sort_values("first_obs", ascending=False)
fig = px.timeline(deep.assign(domain=deep["domain"].astype(str)), x_start="first_obs", x_end="last_obs_capped",
                  y="table", color="domain", category_orders={"domain": DOMAINS},
                  color_discrete_sequence=PALETTE,
                  hover_data={"rows": ":,", "date_col": True, "history_years": ":.1f"})
fig.update_traces(marker_line_color=SURFACE, marker_line_width=1)
fig.update_layout(title="History depth: the 45 tables reaching furthest back",
                  xaxis_title=None, yaxis_title=None)
show(fig, height=900, subtitle="Bar = first to last observation, read from Parquet statistics. Color = domain.")

# COMMAND ----------

bins = [-1, 3, 35, 120, 1e9]
labels = ["Fresh (3 days or less)", "Monthly cadence (4-35 days)", "Lagging (36-120 days)", "Stale (over 120 days)"]
inv["freshness"] = pd.cut(inv["lag_days"], bins=bins, labels=labels)
fcolors = dict(zip(labels, [STATUS["good"], STATUS["warning"], STATUS["serious"], STATUS["critical"]]))
fig = px.strip(inv.dropna(subset=["lag_days"]).assign(domain=inv["domain"].astype(str), lag1=inv["lag_days"] + 1),
               x="lag1", y="domain", color="freshness", color_discrete_map=fcolors,
               category_orders={"freshness": labels, "domain": DOMAINS},
               hover_data={"table": True, "lag_days": True, "last_obs": True, "lag1": False})
fig.update_traces(marker=dict(size=9, line=dict(color=SURFACE, width=1)), jitter=0.6)
fig.update_xaxes(type="log", title="days since last observation (+1, log scale)")
fig.update_layout(title="Freshness: how old is each table's newest row?", yaxis_title=None)
show(fig, height=520, subtitle="Monthly and quarterly sources (BLS, BEA, Zillow) sit in the middle band by design. "
                               "Hover a dot for the table.")

stale = inv[inv["lag_days"] > 120].sort_values("lag_days", ascending=False)[
    ["table", "domain", "date_col", "last_obs", "lag_days", "rows"]]
print(f"{len(stale)} tables have had no new observation in 120+ days (frozen history or a dead source):")
table(stale, 40)
if inv["future_dated"].any():
    print("Tables whose max date is in the future (a data-quality flag worth a look):")
    table(inv.loc[inv["future_dated"], ["table", "date_col", "last_obs"]])

# COMMAND ----------

commits = pd.DataFrame([{"ts": c.created_at, "title": c.title}
                        for c in api.list_repo_commits(REPO, repo_type="dataset")])
commits["day"] = pd.to_datetime(commits["ts"]).dt.tz_convert(None).dt.normalize()
daily = commits.groupby("day").size().rename("commits").reset_index()
recent = daily[daily["day"] >= TODAY - pd.Timedelta(days=180)]
fig = px.bar(recent, x="day", y="commits", color_discrete_sequence=[PALETTE[0]])
fig.update_layout(title="Publishing cadence: commits to the Hugging Face dataset per day (last 180 days)",
                  xaxis_title=None, yaxis_title="commits", bargap=0.15)
show(fig, height=380, subtitle=f"{len(commits):,} commits since {commits['day'].min():%Y-%m-%d}. "
                               "Gaps are nights the publish was skipped or blocked by the row-count guard.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Lakehouse: land a slice in Delta, analyze it with Spark SQL
# MAGIC
# MAGIC Three tables are landed in Unity Catalog under `workspace.fdp_showcase`: daily closes for
# MAGIC seven ETFs, the Treasury yield curve, and the FRED macro panel. The analysis SQL below is
# MAGIC written once and runs on Spark against the Delta tables or, with the `write_delta`
# MAGIC widget set to `no` (or outside Databricks), on DuckDB against the same data in memory.
# MAGIC
# MAGIC * **Growth, drawdown, rolling volatility** use window functions over each symbol's history.
# MAGIC   Daily log returns larger than 35% are treated as share splits and zeroed, because the
# MAGIC   `prices` table stores unadjusted closes.
# MAGIC * **Yield-curve inversion episodes** use a gaps-and-islands query: consecutive inverted days
# MAGIC   are grouped into runs, and runs shorter than 20 trading days are dropped as noise.

# COMMAND ----------

ETFS = ["SPY", "QQQ", "IWM", "EFA", "TLT", "HYG", "GLD"]
etf_daily = q(f"""
    SELECT symbol, CAST(date AS DATE) AS date, close, volume
    FROM {T('prices')}
    WHERE symbol IN ({",".join(f"'{s}'" for s in ETFS)}) AND close > 0
    ORDER BY symbol, date
""")
yc_cols = {"1mo": "y_1mo", "2mo": "y_2mo", "3mo": "y_3mo", "4mo": "y_4mo", "6mo": "y_6mo", "1yr": "y_1yr",
           "2yr": "y_2yr", "3yr": "y_3yr", "5yr": "y_5yr", "7yr": "y_7yr", "10yr": "y_10yr", "20yr": "y_20yr",
           "30yr": "y_30yr"}
yield_curve = q(f"""SELECT CAST(date AS DATE) AS date, {", ".join(f'"{k}" AS {v}' for k, v in yc_cols.items())}
                    FROM {T('treasury_yield_curve')} ORDER BY date""")
macro_panel = q(f"""SELECT CAST(date AS DATE) AS date, series_id, name, category, value
                    FROM {T('macro')} WHERE value IS NOT NULL""")
LAKE = {"etf_daily": etf_daily, "yield_curve": yield_curve, "macro_panel": macro_panel}
print({k: f"{len(v):,} rows" for k, v in LAKE.items()})

CATALOG, SCHEMA = "workspace", "fdp_showcase"
FQ = f"{CATALOG}.{SCHEMA}"
if WRITE_DELTA:
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {FQ}")  # noqa: F821
    for name, pdf in LAKE.items():
        (spark.createDataFrame(pdf)  # noqa: F821
         .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{FQ}.{name}"))
        print(f"wrote {FQ}.{name}")
else:
    for name, pdf in LAKE.items():
        con.register(name, pdf)


def lake_sql(sql: str) -> pd.DataFrame:
    """Run the same SQL on Spark (Delta tables) or DuckDB (in-memory frames)."""
    names = {k: (f"{FQ}.{k}" if WRITE_DELTA else k) for k in LAKE}
    sql = sql.format(**names)
    return spark.sql(sql).toPandas() if WRITE_DELTA else con.execute(sql).df()  # noqa: F821

# COMMAND ----------

PERF_SQL = """
WITH r AS (
  SELECT symbol, date, close,
         LN(close / LAG(close) OVER (PARTITION BY symbol ORDER BY date)) AS lr
  FROM {etf_daily}
), clean AS (
  SELECT symbol, date, CASE WHEN ABS(lr) > 0.35 THEN 0 ELSE COALESCE(lr, 0) END AS lr
  FROM r
), idx AS (
  SELECT symbol, date, lr,
         EXP(SUM(lr) OVER (PARTITION BY symbol ORDER BY date
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)) AS growth,
         STDDEV_SAMP(lr) OVER (PARTITION BY symbol ORDER BY date
                               ROWS BETWEEN 62 PRECEDING AND CURRENT ROW) * SQRT(252) AS vol_63d
  FROM clean
)
SELECT symbol, date, growth, vol_63d,
       growth / MAX(growth) OVER (PARTITION BY symbol ORDER BY date
                                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) - 1 AS drawdown
FROM idx
ORDER BY symbol, date
"""
perf = lake_sql(PERF_SQL)
perf["date"] = pd.to_datetime(perf["date"])

INVERSION_SQL = """
WITH s AS (
  SELECT date, y_10yr - y_3mo AS spread
  FROM {yield_curve} WHERE y_10yr IS NOT NULL AND y_3mo IS NOT NULL
), f AS (
  SELECT date, spread, CASE WHEN spread < 0 THEN 1 ELSE 0 END AS inv,
         ROW_NUMBER() OVER (ORDER BY date)
           - ROW_NUMBER() OVER (PARTITION BY CASE WHEN spread < 0 THEN 1 ELSE 0 END ORDER BY date) AS grp
  FROM s
)
SELECT MIN(date) AS start_date, MAX(date) AS end_date, COUNT(*) AS trading_days,
       ROUND(MIN(spread), 2) AS deepest_spread_pct
FROM f WHERE inv = 1
GROUP BY grp HAVING COUNT(*) >= 20
ORDER BY start_date
"""
inversions = lake_sql(INVERSION_SQL)
print("10-year minus 3-month inversion episodes (20+ trading days):")
table(inversions)

if WRITE_DELTA:
    hist = spark.sql(f"DESCRIBE HISTORY {FQ}.etf_daily").select("version", "timestamp", "operation").toPandas()  # noqa: F821
    print(f"Delta history of {FQ}.etf_daily (each notebook run adds a version):")
    table(hist, 10)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Markets

# COMMAND ----------

win = perf[perf["date"] >= START].copy()
win["growth_rebased"] = win["growth"] / win.groupby("symbol")["growth"].transform("first")
order = win.groupby("symbol")["growth_rebased"].last().sort_values(ascending=False).index.tolist()
fig = go.Figure()
for i, s in enumerate(ETFS):
    d = win[win["symbol"] == s]
    fig.add_scatter(x=d["date"], y=d["growth_rebased"], name=s, line=dict(color=PALETTE[i]),
                    hovertemplate=f"{s}: %{{y:.2f}}x<extra></extra>")
    fig.add_annotation(x=d["date"].iloc[-1], y=np.log10(d["growth_rebased"].iloc[-1]), text=s, showarrow=False,
                       xanchor="left", xshift=6, font=dict(color=INK2, size=11))
fig.update_yaxes(type="log", title="growth of $1 (log scale)")
fig.update_layout(title=f"Cross-asset growth of $1 over {LOOKBACK_YEARS} years (price return)",
                  hovermode="x unified", legend_traceorder="normal", margin=dict(r=60))
show(fig, height=520, subtitle=f"Ranked at the end: {' > '.join(order)}. Dividends are not included.")

# COMMAND ----------

fig = go.Figure()
for i, s in enumerate(["SPY", "TLT", "GLD"]):
    d = win[win["symbol"] == s]
    fig.add_scatter(x=d["date"], y=d["drawdown"] * 100, name=s, line=dict(color=PALETTE[i]),
                    hovertemplate=f"{s}: %{{y:.1f}}%<extra></extra>")
fig.update_yaxes(title="% below previous peak", ticksuffix="%")
fig.update_layout(title="Drawdowns: stocks, long Treasuries, and gold rarely fall together",
                  hovermode="x unified")
show(fig, height=440, subtitle="Peak-to-trough loss, computed with a running MAX window in the SQL above.")

# COMMAND ----------

vol_m = (perf.dropna(subset=["vol_63d"]).assign(month=lambda d: d["date"].dt.to_period("M").dt.to_timestamp())
         .query("month >= @START").groupby(["symbol", "month"])["vol_63d"].mean().unstack("month"))
vol_m = vol_m.reindex(ETFS)
fig = go.Figure(go.Heatmap(z=vol_m.values * 100, x=vol_m.columns, y=vol_m.index, colorscale=SEQ_SCALE,
                           xgap=1, ygap=2, colorbar=dict(title="ann. vol %"),
                           hovertemplate="%{y} %{x|%b %Y}: %{z:.0f}%<extra></extra>"))
fig.update_layout(title="Volatility regimes: 63-day annualized volatility by month")
show(fig, height=360)

# COMMAND ----------

SECTORS = {"XLB": "Materials", "XLC": "Communication", "XLE": "Energy", "XLF": "Financials", "XLI": "Industrials",
           "XLK": "Technology", "XLP": "Staples", "XLRE": "Real Estate", "XLU": "Utilities", "XLV": "Health Care",
           "XLY": "Discretionary"}
sec = q(f"""SELECT symbol, CAST(date AS DATE) AS date, close FROM {T('prices')}
            WHERE symbol IN ({",".join(f"'{s}'" for s in SECTORS)}) AND close > 0
              AND CAST(date AS DATE) >= DATE '{(TODAY - pd.DateOffset(years=3)).date()}'""")
wide = sec.pivot_table(index="date", columns="symbol", values="close").sort_index()
rets = np.log(wide).diff()
rets = rets.mask(rets.abs() > 0.35)
corr = rets.tail(252).corr().rename(index=SECTORS, columns=SECTORS)
ordr = corr.mean().sort_values().index
corr = corr.loc[ordr, ordr]
fig = go.Figure(go.Heatmap(z=corr.values, x=corr.columns, y=corr.index, zmin=-1, zmax=1, colorscale=DIV_SCALE,
                           xgap=2, ygap=2, text=np.round(corr.values, 2), texttemplate="%{text}",
                           textfont=dict(size=10), colorbar=dict(title="corr"),
                           hovertemplate="%{y} vs %{x}: %{z:.2f}<extra></extra>"))
fig.update_layout(title="Sector correlation, last 252 trading days (daily log returns)")
show(fig, height=560, subtitle="Sorted from least to most correlated with the rest. Defensive sectors sit at the top-left.")

# COMMAND ----------

monthly = wide.resample("ME").last().pct_change().iloc[-24:].rename(columns=SECTORS) * 100
lim = float(np.nanpercentile(np.abs(monthly.values), 98))
fig = go.Figure(go.Heatmap(z=monthly.T.values, x=monthly.index, y=monthly.columns, zmin=-lim, zmax=lim,
                           colorscale=DIV_SCALE, xgap=2, ygap=2, colorbar=dict(title="% m/m"),
                           hovertemplate="%{y} %{x|%b %Y}: %{z:+.1f}%<extra></extra>"))
fig.update_layout(title="Sector rotation: monthly price return, last 24 months")
show(fig, height=480)

# COMMAND ----------

vix = q(f"""SELECT CAST(date AS DATE) AS date, index_name, close FROM {T('cboe_volatility')}
            WHERE index_name IN ('VIX9D','VIX','VIX3M','VIX6M','SKEW','VVIX')""")
vw = vix.pivot_table(index="date", columns="index_name", values="close").sort_index()
vw.index = pd.to_datetime(vw.index)
tenor = {"VIX9D": 9, "VIX": 30, "VIX3M": 93, "VIX6M": 182}
last = vw[list(tenor)].dropna().index.max()
snaps = [("latest", last), ("1 month ago", last - pd.DateOffset(months=1)), ("1 year ago", last - pd.DateOffset(years=1))]
fig = make_subplots(rows=1, cols=2, column_widths=[0.35, 0.65], horizontal_spacing=0.08,
                    subplot_titles=("Term structure (days to expiry)", "VIX / VIX3M: above 1 = stress (backwardation)"))
for i, (lab, d) in enumerate(snaps):
    row = vw[list(tenor)].loc[:d].dropna().iloc[-1]
    fig.add_scatter(x=list(tenor.values()), y=row.values, name=f"{lab} ({row.name:%Y-%m-%d})", mode="lines+markers",
                    line=dict(color=[PALETTE[0], PALETTE[1], PALETTE[2]][i]), row=1, col=1)
ratio = (vw["VIX"] / vw["VIX3M"]).dropna()
ratio = ratio[ratio.index >= START]
fig.add_scatter(x=ratio.index, y=ratio.values, name="VIX / VIX3M", line=dict(color=INK2, width=1.5),
                showlegend=False, row=1, col=2)
fig.add_hline(y=1, line=dict(color=STATUS["critical"], dash="dot", width=1), row=1, col=2)
fig.update_layout(title="Volatility term structure from CBOE indices")
show(fig, height=440, subtitle=f"{(ratio > 1).mean():.0%} of days in the window were in backwardation.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Rates and macro

# COMMAND ----------

mat_years = {"y_1mo": 1 / 12, "y_3mo": 0.25, "y_6mo": 0.5, "y_1yr": 1, "y_2yr": 2, "y_3yr": 3, "y_5yr": 5,
             "y_7yr": 7, "y_10yr": 10, "y_20yr": 20, "y_30yr": 30}
ycw = yield_curve.assign(date=pd.to_datetime(yield_curve["date"])).set_index("date")[list(mat_years)]
weekly = ycw.resample("W-FRI").last().dropna(how="all")
fig = go.Figure(go.Surface(z=weekly.values, x=list(mat_years.values()), y=weekly.index, colorscale=SEQ_SCALE,
                           colorbar=dict(title="yield %"), showscale=True,
                           hovertemplate="%{y|%Y-%m-%d}<br>%{x} yr: %{z:.2f}%<extra></extra>"))
fig.update_layout(title="The US Treasury yield curve since 1990, weekly",
                  scene=dict(xaxis_title="maturity (years)", yaxis_title="", zaxis_title="yield %",
                             camera=dict(eye=dict(x=1.8, y=-1.3, z=0.8)), aspectratio=dict(x=1, y=2.2, z=0.7)))
show(fig, height=700, subtitle="Drag to rotate. The 2022-23 hiking cycle shows as the short end climbing above the long end.")

# COMMAND ----------

ld = ycw.dropna(subset=["y_10yr"]).index.max()
picks = [("2000-06-30", "Jun 2000"), ("2007-06-29", "Jun 2007"), ("2019-12-31", "Dec 2019"),
         (str((ld - pd.DateOffset(years=2)).date()), "2 years ago"), (str((ld - pd.DateOffset(years=1)).date()), "1 year ago"),
         (str(ld.date()), "latest")]
fig = go.Figure()
for i, (d, lab) in enumerate(picks):
    row = ycw.loc[:d].dropna(subset=["y_10yr"]).iloc[-1].dropna()
    fig.add_scatter(x=[mat_years[c] for c in row.index], y=row.values, mode="lines+markers",
                    name=f"{lab} ({row.name:%Y-%m-%d})", line=dict(color=ORDINAL[i]))
fig.update_xaxes(type="log", title="maturity (years, log scale)", tickvals=[0.083, 0.25, 0.5, 1, 2, 5, 10, 30],
                 ticktext=["1m", "3m", "6m", "1y", "2y", "5y", "10y", "30y"])
fig.update_yaxes(title="yield %")
fig.update_layout(title="Yield-curve snapshots: older curves lighter, latest darkest")
show(fig, height=480)

# COMMAND ----------

spr = ycw.assign(s10_2=ycw["y_10yr"] - ycw["y_2yr"], s10_3m=ycw["y_10yr"] - ycw["y_3mo"])[["s10_2", "s10_3m"]].dropna()
fig = go.Figure()
for _, ep in inversions.iterrows():
    fig.add_vrect(x0=ep["start_date"], x1=ep["end_date"], fillcolor=NEG, opacity=0.10, line_width=0)
fig.add_scatter(x=spr.index, y=spr["s10_2"], name="10y minus 2y", line=dict(color=PALETTE[0]))
fig.add_scatter(x=spr.index, y=spr["s10_3m"], name="10y minus 3m", line=dict(color=PALETTE[1]))
fig.add_hline(y=0, line=dict(color=AXIS, width=1))
fig.update_yaxes(title="percentage points")
fig.update_layout(title="Term spreads, with inversion episodes from the gaps-and-islands query shaded",
                  hovermode="x unified")
show(fig, height=440, subtitle=f"{len(inversions)} episodes of 20+ trading days with the 10y below the 3m.")

# COMMAND ----------

mp = macro_panel.assign(date=pd.to_datetime(macro_panel["date"]))
ser = lambda sid: mp[mp["series_id"] == sid].set_index("date")["value"].sort_index()  # noqa: E731
panels = [
    ("CPI inflation, % y/y", ser("CPIAUCSL").pct_change(12) * 100),
    ("Unemployment rate, %", ser("UNRATE")),
    ("Fed funds rate, %", ser("FEDFUNDS")),
    ("M2 money supply, % y/y", ser("M2SL").pct_change(12) * 100),
    ("10y breakeven inflation, %", ser("T10YIE")),
    ("High-yield credit spread, %", ser("BAMLH0A0HYM2")),
]
fig = make_subplots(rows=2, cols=3, subplot_titles=[p[0] for p in panels], vertical_spacing=0.14,
                    horizontal_spacing=0.06)
cut = TODAY - pd.DateOffset(years=max(LOOKBACK_YEARS, 10))
for k, (name, s) in enumerate(panels):
    s = s.dropna()
    s = s[s.index >= cut]
    r, c = divmod(k, 3)
    fig.add_scatter(x=s.index, y=s.values, line=dict(color=PALETTE[0], width=1.8), showlegend=False,
                    hovertemplate=f"{name}<br>%{{x|%b %Y}}: %{{y:.2f}}<extra></extra>", row=r + 1, col=c + 1)
    if len(s):
        fig.add_annotation(x=s.index[-1], y=s.iloc[-1], text=f"{s.iloc[-1]:.1f}", showarrow=False, xanchor="left",
                           xshift=4, font=dict(size=11, color=INK2), row=r + 1, col=c + 1)
fig.update_layout(title="Macro dashboard (FRED series, one scale per panel)")
show(fig, height=620, subtitle="Credit spreads in this dataset start in 2023.")

# COMMAND ----------

pol = q(f"""
    WITH m AS (
      SELECT country, date_trunc('month', CAST(date AS DATE)) AS month, AVG(rate) AS rate
      FROM {T('policy_rates')} WHERE rate IS NOT NULL GROUP BY 1, 2
    )
    SELECT * FROM m WHERE month >= DATE '2000-01-01'
""")
latest_pol = pol.sort_values("month").groupby("country").tail(1)
latest_pol = latest_pol[latest_pol["month"] >= TODAY - pd.DateOffset(months=3)].sort_values("rate")
fig = make_subplots(rows=1, cols=2, column_widths=[0.38, 0.62], horizontal_spacing=0.12,
                    subplot_titles=("Latest policy rate by central bank", "Selected central banks since 2000"))
fig.add_scatter(x=latest_pol["rate"], y=latest_pol["country"], mode="markers", marker=dict(color=PALETTE[0], size=9),
                showlegend=False, hovertemplate="%{y}: %{x:.2f}%<extra></extra>", row=1, col=1)
for i, ctry in enumerate(["United States", "United Kingdom", "Japan"]):
    d = pol[pol["country"] == ctry].sort_values("month")
    fig.add_scatter(x=d["month"], y=d["rate"], name=ctry, line=dict(color=PALETTE[i]), row=1, col=2)
fig.update_xaxes(title="%", row=1, col=1)
fig.update_layout(title="Global policy rates (BIS central bank series)", hovermode="closest")
show(fig, height=max(420, 18 * len(latest_pol) + 140))

# COMMAND ----------

cape = q(f"SELECT CAST(date AS DATE) AS date, cape FROM {T('shiller_cape')} WHERE cape IS NOT NULL ORDER BY date")
cape["date"] = pd.to_datetime(cape["date"])
pct = (cape["cape"] < cape["cape"].iloc[-1]).mean()
fig = go.Figure()
fig.add_hrect(y0=cape["cape"].quantile(0.25), y1=cape["cape"].quantile(0.75), fillcolor=PALETTE[0], opacity=0.08,
              line_width=0, annotation_text="middle 50% of history", annotation_position="top left",
              annotation_font=dict(color=INK2, size=11))
fig.add_scatter(x=cape["date"], y=cape["cape"], name="CAPE", line=dict(color=PALETTE[0]), showlegend=False)
fig.add_hline(y=cape["cape"].median(), line=dict(color=MUTED, dash="dot", width=1))
fig.update_yaxes(title="cyclically adjusted P/E")
fig.update_layout(title="Shiller CAPE since 1881")
show(fig, height=420, subtitle=f"Latest {cape['cape'].iloc[-1]:.1f} ({cape['date'].iloc[-1]:%b %Y}) is above "
                               f"{pct:.0%} of all months on record.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Options
# MAGIC Schwab option-chain snapshots, one per trading day. Change the **Options symbol** widget to
# MAGIC redraw this section. Implied volatility is stored in percent.

# COMMAND ----------

chain = q(f"""
    WITH s AS (SELECT max(snapshot_date) AS d FROM {T('schwab_options')} WHERE symbol = '{OPT_SYM}')
    SELECT put_call, CAST(expiration_date AS DATE) AS expiry, days_to_expiration AS dte, strike,
           implied_volatility AS iv, open_interest AS oi, volume, delta, underlying_price AS spot, mark
    FROM {T('schwab_options')}, s
    WHERE symbol = '{OPT_SYM}' AND snapshot_date = s.d AND NOT coalesce(non_standard, false)
""")
spot = float(chain["spot"].median())
chain["moneyness"] = chain["strike"] / spot
otm = chain[((chain["put_call"] == "PUT") & (chain["moneyness"] <= 1)) |
            ((chain["put_call"] == "CALL") & (chain["moneyness"] > 1))]
otm = otm[(otm["iv"] > 1) & (otm["iv"] < 300) & otm["moneyness"].between(0.7, 1.3)]
print(f"{OPT_SYM}: {len(chain):,} contracts in the latest snapshot, spot {spot:,.2f}, "
      f"{chain['expiry'].nunique()} expiries")

targets = [7, 30, 60, 120, 240, 400]
exps = sorted(otm["dte"].unique())
chosen = sorted({min(exps, key=lambda e: abs(e - t)) for t in targets if exps})
fig = go.Figure()
for i, e in enumerate(chosen):
    d = otm[otm["dte"] == e].sort_values("moneyness")
    fig.add_scatter(x=d["moneyness"], y=d["iv"], name=f"{e} days", mode="lines",
                    line=dict(color=ORDINAL[min(i, len(ORDINAL) - 1)]),
                    hovertemplate=f"{e}d, K/S %{{x:.2f}}: %{{y:.1f}}%<extra></extra>")
fig.add_vline(x=1, line=dict(color=AXIS, dash="dot", width=1))
fig.update_xaxes(title="strike / spot", tickformat=".2f")
fig.update_yaxes(title="implied volatility %")
fig.update_layout(title=f"{OPT_SYM} volatility smile by expiry (out-of-the-money options)")
show(fig, height=460, subtitle="Short expiries darken toward long ones; the left-side skew is the price of crash protection.")

# COMMAND ----------

surf = otm[otm["dte"].between(5, 400)].copy()
surf["mbin"] = (surf["moneyness"] / 0.025).round() * 0.025
grid = surf.pivot_table(index="dte", columns="mbin", values="iv", aggfunc="median").sort_index()
grid = grid.loc[:, (grid.columns >= 0.8) & (grid.columns <= 1.2)].interpolate(axis=1, limit=2)
fig = go.Figure(go.Surface(z=grid.values, x=grid.columns, y=grid.index, colorscale=SEQ_SCALE,
                           colorbar=dict(title="IV %"),
                           hovertemplate="K/S %{x:.3f}<br>%{y} days<br>IV %{z:.1f}%<extra></extra>"))
fig.update_layout(title=f"{OPT_SYM} implied-volatility surface",
                  scene=dict(xaxis_title="strike / spot", yaxis_title="days to expiry", zaxis_title="IV %",
                             camera=dict(eye=dict(x=-1.6, y=-1.6, z=0.9))))
show(fig, height=640)

# COMMAND ----------

near = chain[chain["moneyness"].between(0.85, 1.15) & (chain["dte"] <= 60)]
oi = near.groupby(["strike", "put_call"])["oi"].sum().unstack(fill_value=0)
fig = go.Figure()
fig.add_bar(x=oi.index, y=oi.get("CALL", 0), name="calls", marker_color=PALETTE[0])
fig.add_bar(x=oi.index, y=-oi.get("PUT", 0), name="puts (shown below zero)", marker_color=PALETTE[1])
fig.add_vline(x=spot, line=dict(color=INK2, dash="dot", width=1), annotation_text=f"spot {spot:,.0f}",
              annotation_font=dict(color=INK2, size=11))
fig.update_layout(title=f"{OPT_SYM} open interest by strike, expiries within 60 days", barmode="relative",
                  bargap=0.1, yaxis_title="contracts", xaxis_title="strike")
show(fig, height=440)

# COMMAND ----------

pcr = q(f"""
    SELECT CAST(snapshot_date AS DATE) AS day,
           SUM(CASE WHEN put_call = 'PUT'  THEN open_interest END) * 1.0
             / NULLIF(SUM(CASE WHEN put_call = 'CALL' THEN open_interest END), 0) AS put_call_oi,
           MEDIAN(CASE WHEN days_to_expiration BETWEEN 20 AND 45 AND abs(delta) BETWEEN 0.4 AND 0.6
                       AND implied_volatility BETWEEN 1 AND 300 THEN implied_volatility END) AS atm_iv_30d
    FROM {T('schwab_options')}
    WHERE symbol = '{OPT_SYM}'
    GROUP BY 1 ORDER BY 1
""")
fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08,
                    subplot_titles=("Put/call open-interest ratio", "At-the-money implied volatility, ~30 days (%)"))
fig.add_scatter(x=pcr["day"], y=pcr["put_call_oi"], line=dict(color=PALETTE[1]), showlegend=False, row=1, col=1)
fig.add_scatter(x=pcr["day"], y=pcr["atm_iv_30d"], line=dict(color=PALETTE[0]), showlegend=False, row=2, col=1)
fig.update_layout(title=f"{OPT_SYM} positioning and implied volatility across {len(pcr)} daily snapshots",
                  hovermode="x unified")
show(fig, height=520)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Holdings and ownership
# MAGIC Fund holdings come from issuer files (SSGA, BlackRock, Invesco, Global X, ...) and SEC N-PORT
# MAGIC filings. Sectors come from the `securities` reference table, which classifies S&P 500 names;
# MAGIC other holdings show as Unclassified. Change the **Fund** widget to redraw the treemap.

# COMMAND ----------

hold = q(f"""
    WITH f AS (
      SELECT * FROM {T('fund_holdings')}
      WHERE fund_ticker IN ('{FUND}', 'SPY', 'IJR', 'ARKK', 'DIA')
    ), m AS (SELECT fund_ticker, max(snapshot_date) AS d FROM f GROUP BY 1)
    SELECT f.fund_ticker, f.snapshot_date, f.holding_ticker, f.holding_name, f.weight_pct,
           f.market_value_usd, nullif(f.sector, '-') AS fund_sector, f.source
    FROM f JOIN m ON f.fund_ticker = m.fund_ticker AND f.snapshot_date = m.d
    WHERE f.weight_pct > 0
""")
secs = q(f"SELECT symbol, sector FROM {T('securities')} WHERE sector IS NOT NULL")
hold = hold.merge(secs, left_on="holding_ticker", right_on="symbol", how="left")
hold["sector"] = hold["sector"].fillna(hold["fund_sector"]).fillna("Unclassified")

h = hold[hold["fund_ticker"] == FUND].copy()
h["label"] = h["holding_ticker"].fillna(h["holding_name"].str[:18])
fig = px.treemap(h, path=[px.Constant(FUND), "sector", "label"], values="weight_pct", color="weight_pct",
                 color_continuous_scale=SEQ_SCALE, hover_data={"holding_name": True, "weight_pct": ":.2f"})
fig.update_traces(marker=dict(line=dict(color=SURFACE, width=2)), root_color=SURFACE,
                  hovertemplate="<b>%{label}</b><br>%{value:.2f}% of fund<extra></extra>")
fig.update_layout(title=f"{FUND} holdings by sector, sized by portfolio weight", coloraxis_showscale=False)
src = h["source"].iloc[0] if len(h) else "n/a"
show(fig, height=620, subtitle=f"{len(h):,} holdings as of {h['snapshot_date'].max():%Y-%m-%d} (source: {src}).")

# COMMAND ----------

fig = go.Figure()
for i, fund in enumerate(["DIA", "ARKK", "SPY", "IJR"]):
    w = hold.loc[hold["fund_ticker"] == fund, "weight_pct"].sort_values(ascending=False).reset_index(drop=True)
    if w.empty:
        continue
    cum = w.cumsum() / w.sum() * 100
    fig.add_scatter(x=np.arange(1, len(cum) + 1), y=cum, name=f"{fund} ({len(w)} holdings)",
                    line=dict(color=PALETTE[i]), hovertemplate=f"{fund}: top %{{x}} = %{{y:.1f}}%<extra></extra>")
fig.add_vline(x=10, line=dict(color=AXIS, dash="dot", width=1), annotation_text="top 10",
              annotation_font=dict(color=INK2, size=11))
fig.update_xaxes(type="log", title="number of largest holdings (log scale)")
fig.update_yaxes(title="cumulative weight %", range=[0, 101])
fig.update_layout(title="Concentration: how much of each fund sits in its largest holdings")
show(fig, height=460)

# COMMAND ----------

cong = q(f"""
    SELECT chamber,
           CASE WHEN lower(transaction_type) LIKE 'p%' THEN 'Purchase'
                WHEN lower(transaction_type) LIKE 's%' THEN 'Sale'
                ELSE 'Other' END AS side,
           date_trunc('month', try_cast(transaction_date AS DATE)) AS month,
           ticker, amount_min, amount_max
    FROM {T('congressional_trades')}
""").dropna(subset=["month"])
cong = cong[cong["month"] >= "2014-01-01"]
m = cong[cong["side"] != "Other"].groupby(["month", "side"]).size().unstack(fill_value=0)
top = (cong[cong["ticker"].notna() & (cong["ticker"].str.len() <= 5)]
       .groupby("ticker").size().nlargest(15).sort_values())
fig = make_subplots(rows=1, cols=2, column_widths=[0.65, 0.35], horizontal_spacing=0.1,
                    subplot_titles=("Disclosed trades per month (House + Senate)", "Most-traded tickers"))
fig.add_scatter(x=m.index, y=m.get("Purchase"), name="purchases", line=dict(color=PALETTE[0]), row=1, col=1)
fig.add_scatter(x=m.index, y=m.get("Sale"), name="sales", line=dict(color=PALETTE[1]), row=1, col=1)
fig.add_bar(x=top.values, y=top.index, orientation="h", marker_color=PALETTE[0], showlegend=False,
            hovertemplate="%{y}: %{x} trades<extra></extra>", row=1, col=2)
fig.update_layout(title="Congressional stock trading, from House Clerk and Senate eFD disclosures")
show(fig, height=460, subtitle=f"{len(cong):,} transactions; latest transaction date "
                               f"{cong['month'].max():%b %Y}. Disclosure lags the trade by up to 45 days.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Fundamentals (the `financial-fundamentals` dataset)
# MAGIC SEC XBRL company facts, mapped to 10 core metrics, keeping each period's latest restatement.

# COMMAND ----------

comp = q(f"SELECT taxonomy, count(*) AS n FROM {F('companies')} GROUP BY 1 ORDER BY 2 DESC")
filings = q(f"""
    SELECT try_cast(substr(filed, 1, 4) AS INT) AS year,
           CASE WHEN form IN ('10-K', '10-Q', '20-F') THEN form
                WHEN form LIKE '%/A' THEN 'amendments'
                ELSE 'other (6-K, 40-F, ...)' END AS form_group,
           count(*) AS n
    FROM {F('filings')} GROUP BY 1, 2
""").dropna()
bad_fy = q(f"""SELECT count(*) AS n FROM {F('fundamentals_annual_latest')}
               WHERE fiscal_year NOT BETWEEN 1990 AND 2030""")["n"].iloc[0]
tiles([
    ("Companies", f"{comp['n'].sum():,}", ", ".join(f"{r.taxonomy}: {r.n:,}" for r in comp.itertuples())),
    ("Filings", f"{filings['n'].sum():,}", f"{int(filings['year'].min())}-{int(filings['year'].max())}"),
    ("Out-of-range fiscal_year", f"{bad_fy:,}", "rows in the annual file; period_end is used instead"),
])
groups = ["10-Q", "10-K", "20-F", "amendments", "other (6-K, 40-F, ...)"]
fig = go.Figure()
for i, g in enumerate(groups):
    d = filings[filings["form_group"] == g].sort_values("year")
    fig.add_bar(x=d["year"], y=d["n"], name=g, marker_color=PALETTE[i], marker_line=dict(color=SURFACE, width=1))
fig.update_layout(title="XBRL filings by year filed", barmode="stack", bargap=0.2, yaxis_title="filings",
                  hovermode="x unified")
show(fig, height=420, subtitle="The current year is partial.")

# COMMAND ----------

fa = q(f"""
    WITH a AS (
      SELECT symbol, metric, value, period_end,
             row_number() OVER (PARTITION BY symbol, metric ORDER BY period_end DESC, filed DESC) AS rn
      FROM {F('fundamentals_annual_latest')}
      WHERE unit = 'USD' AND fiscal_period = 'FY' AND symbol IS NOT NULL
        AND metric IN ('revenue', 'net_income', 'operating_income', 'total_assets', 'total_liabilities')
        AND period_end >= DATE '{(TODAY - pd.DateOffset(months=20)).date()}'
    )
    PIVOT (SELECT symbol, metric, value FROM a WHERE rn = 1) ON metric USING first(value)
""")
fa = fa.merge(secs, on="symbol", how="inner")
fa = fa[fa["revenue"] > 1e8].copy()
fa["net_margin"] = fa["net_income"] / fa["revenue"] * 100
fa["leverage"] = fa["total_liabilities"] / fa["total_assets"] * 100
sec_order = fa.groupby("sector")["net_margin"].median().sort_values().index.tolist()
fig = go.Figure()
fig.add_box(x=fa["net_margin"].clip(-50, 80), y=fa["sector"], orientation="h", marker_color=PALETTE[0],
            line=dict(width=1.5), boxpoints="outliers", hovertext=fa["symbol"], name="")
fig.update_yaxes(categoryorder="array", categoryarray=sec_order)
fig.update_xaxes(title="net margin, latest fiscal year (%; clipped to -50..80)", ticksuffix="%")
fig.update_layout(title="Net margin by sector, S&P 500 companies", showlegend=False)
show(fig, height=520, subtitle=f"{len(fa)} companies with revenue above $100M. Sorted by median margin.")

# COMMAND ----------

lab = fa.nlargest(10, "revenue")
fig = go.Figure()
fig.add_scatter(x=fa["revenue"] / 1e9, y=fa["net_margin"].clip(-50, 80), mode="markers",
                marker=dict(color=PALETTE[0], size=8, opacity=0.6, line=dict(color=SURFACE, width=1)),
                text=fa["symbol"], customdata=fa["sector"], showlegend=False,
                hovertemplate="<b>%{text}</b> (%{customdata})<br>revenue $%{x:,.1f}B<br>net margin %{y:.1f}%<extra></extra>")
for r in lab.itertuples():
    fig.add_annotation(x=np.log10(r.revenue / 1e9), y=min(max(r.net_margin, -50), 80), text=r.symbol,
                       showarrow=False, yshift=11, font=dict(size=10, color=INK2))
fig.update_xaxes(type="log", title="revenue, latest fiscal year ($B, log scale)")
fig.update_yaxes(title="net margin %", ticksuffix="%")
fig.update_layout(title="Scale versus profitability")
show(fig, height=500, subtitle="The ten largest by revenue are labeled.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Sentiment and alternative data

# COMMAND ----------

news = q(f"""SELECT date_trunc('week', try_cast(date AS DATE)) AS week, sentiment, count(*) AS n
             FROM {T('news_sentiment')} WHERE sentiment IN ('bullish','neutral','bearish') GROUP BY 1, 2""").dropna()
nw = news.pivot_table(index="week", columns="sentiment", values="n", fill_value=0)
nw = nw[nw.sum(axis=1) >= 50]
share = nw.div(nw.sum(axis=1), axis=0) * 100
fig = go.Figure()
for s, c in [("bearish", NEG), ("neutral", NEUTRAL), ("bullish", POS)]:
    fig.add_scatter(x=share.index, y=share[s], name=s, stackgroup="one", line=dict(width=0.5, color=c),
                    fillcolor=c, hovertemplate=f"{s}: %{{y:.0f}}%<extra></extra>")
fig.update_yaxes(title="share of headlines %", range=[0, 100], ticksuffix="%")
fig.update_layout(title="Company news tone, weekly (local VADER model with a finance lexicon)",
                  hovermode="x unified")
show(fig, height=420, subtitle=f"{int(nw.values.sum()):,} scored headlines.")

# COMMAND ----------

tv = q(f"""SELECT try_cast(date AS DATE) AS day, rating_label, count(*) AS n FROM {T('tv_ratings')}
           WHERE rating_label <> 'unknown' GROUP BY 1, 2""").dropna()
tvw = tv.pivot_table(index="day", columns="rating_label", values="n", fill_value=0)
tvs = tvw.div(tvw.sum(axis=1), axis=0) * 100
scale = [("strong_sell", "#a8292a"), ("sell", "#f3a9a8"), ("neutral", "#d9d7cf"), ("buy", "#9ec5f4"),
         ("strong_buy", "#184f95")]
fig = go.Figure()
for lab_, c in scale:
    if lab_ in tvs:
        fig.add_bar(x=tvs.index, y=tvs[lab_], name=lab_.replace("_", " "), marker_color=c,
                    hovertemplate=f"{lab_}: %{{y:.0f}}%<extra></extra>")
fig.update_layout(title="TradingView technical ratings across the watchlist, daily mix", barmode="stack",
                  bargap=0.05, yaxis=dict(title="share of symbols %", ticksuffix="%"), hovermode="x unified")
show(fig, height=420, subtitle="An ordinal scale: red is sell, blue is buy, gray is neutral.")

# COMMAND ----------

fg = q(f"SELECT CAST(date AS DATE) AS date, value FROM {T('fear_greed')} ORDER BY date")
fig = go.Figure()
for lo, hi, c, name in [(0, 25, "#a8292a", "extreme fear"), (25, 45, "#f3a9a8", "fear"), (55, 75, "#9ec5f4", "greed"),
                        (75, 100, "#184f95", "extreme greed")]:
    fig.add_hrect(y0=lo, y1=hi, fillcolor=c, opacity=0.12, line_width=0, annotation_text=name,
                  annotation_position="right", annotation_font=dict(size=10, color=INK2))
fig.add_scatter(x=fg["date"], y=fg["value"].rolling(7, min_periods=1).mean(), line=dict(color=INK2, width=1.5),
                showlegend=False, hovertemplate="%{x|%Y-%m-%d}: %{y:.0f}<extra></extra>")
fig.update_yaxes(range=[0, 100], title="index (7-day mean)")
fig.update_layout(title="Crypto Fear & Greed index since 2018", margin=dict(r=110))
show(fig, height=400)

# COMMAND ----------

cfpb = q(f"""SELECT date_trunc('month', try_cast(date_received AS DATE)) AS month, product, count(*) AS n
             FROM {T('cfpb_complaints')} GROUP BY 1, 2""").dropna()
top5 = cfpb.groupby("product")["n"].sum().nlargest(5).index
cfpb["product"] = np.where(cfpb["product"].isin(top5), cfpb["product"], "All other products")
cm = cfpb.groupby(["month", "product"])["n"].sum().unstack(fill_value=0).sort_index()
cm = cm[cm.index < TODAY.to_period("M").to_timestamp()]
fig = go.Figure()
for i, p in enumerate(list(top5) + ["All other products"]):
    fig.add_scatter(x=cm.index, y=cm[p], name=p[:48], line=dict(color=PALETTE[i], width=1.8))
fig.update_layout(title="CFPB consumer complaints per month, by product", hovermode="x unified",
                  yaxis_title="complaints", legend=dict(font=dict(size=11)))
show(fig, height=480, subtitle=f"{int(cm.values.sum()):,} complaints. Aggregated remotely: DuckDB read only "
                               "two columns of a 1.6 GB file. CFPB renamed several products over the years.")

# COMMAND ----------

STATES = {"Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
          "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA",
          "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
          "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA",
          "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
          "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
          "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
          "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
          "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
          "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY"}
zh = q(f"""SELECT region_name, CAST(date AS DATE) AS date, zhvi FROM {T('zillow_zhvi')}
           WHERE region_type = 'state'""")
zh["date"] = pd.to_datetime(zh["date"])
zl = zh["date"].max()
zz = zh[zh["date"].isin([zl, zl - pd.DateOffset(years=1)])].pivot_table(index="region_name", columns="date",
                                                                         values="zhvi")
zz = zz.dropna()
zz["yoy"] = (zz.iloc[:, 1] / zz.iloc[:, 0] - 1) * 100
zz["code"] = zz.index.map(STATES)
zz = zz.dropna(subset=["code"])
lim = float(zz["yoy"].abs().max())
fig = go.Figure(go.Choropleth(locations=zz["code"], z=zz["yoy"], locationmode="USA-states", colorscale=DIV_SCALE,
                              zmin=-lim, zmax=lim, marker_line_color=SURFACE, marker_line_width=1,
                              colorbar=dict(title="% y/y"), text=zz.index,
                              hovertemplate="%{text}: %{z:+.1f}%<extra></extra>"))
fig.update_layout(title=f"Home values, change over the year to {zl:%b %Y} (Zillow ZHVI)",
                  geo=dict(scope="usa", bgcolor=SURFACE, lakecolor=SURFACE))
show(fig, height=500, subtitle=f"Median state {zz['yoy'].median():+.1f}%. Blue is rising, red is falling.")

# COMMAND ----------

gs = q(f"SELECT CAST(date AS DATE) AS date, gscpi FROM {T('shipping_gscpi')} ORDER BY date")
fig = go.Figure()
fig.add_scatter(x=gs["date"], y=gs["gscpi"], line=dict(color=PALETTE[0]), showlegend=False,
                fill="tozeroy", fillcolor="rgba(42,120,214,0.10)",
                hovertemplate="%{x|%b %Y}: %{y:.2f} sd<extra></extra>")
fig.add_hline(y=0, line=dict(color=AXIS, width=1))
fig.update_yaxes(title="standard deviations from average")
fig.update_layout(title="New York Fed Global Supply Chain Pressure Index")
show(fig, height=380, subtitle="The 2021-22 spike is the pandemic shipping crunch.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 9. Pipeline health
# MAGIC `signal_monitor.py` re-scores a set of trading signals every night and publishes the result,
# MAGIC so signal decay shows up as data rather than as a surprise. Below that, a completeness check:
# MAGIC the share of missing values in every column of every table, read from Parquet null counts.

# COMMAND ----------

sh = q(f"""SELECT CAST(run_date AS DATE) AS run_date, signal, car21_tstat, win_rate_pct, n_trades
           FROM {T('signal_health')} WHERE "window" = '1y'""")
hm = sh.pivot_table(index="signal", columns="run_date", values="car21_tstat")
lim = float(np.nanmax(np.abs(hm.values))) if hm.size else 1
fig = go.Figure(go.Heatmap(z=hm.values, x=hm.columns, y=hm.index, zmin=-lim, zmax=lim, colorscale=DIV_SCALE,
                           xgap=1, ygap=2, colorbar=dict(title="t-stat"),
                           hovertemplate="%{y} on %{x|%Y-%m-%d}: t = %{z:.2f}<extra></extra>"))
fig.update_layout(title="Signal monitor: t-statistic of 21-day abnormal return after each signal (1-year window)")
show(fig, height=340, subtitle="Blue = the signal has been followed by gains, red = by losses. "
                               "Values near zero mean no detectable edge.")

# COMMAND ----------

# Null counts come from the same Parquet footers read in section 1: no data is scanned.
# A column with no statistics at all is an all-null column (Arrow's null type writes none).
mc = meta[~meta["path_in_schema"].isin(["month", "year"])].copy()
mc["nulls"] = mc["stats_null_count"].fillna(mc["row_group_num_rows"])
nulls = (mc.groupby(["file_name", "path_in_schema"], as_index=False)[["nulls", "row_group_num_rows"]].sum()
         .merge(inv[["file_name", "table", "domain"]], on="file_name"))
nulls["null_pct"] = nulls["nulls"] / nulls["row_group_num_rows"].clip(lower=1) * 100
nulls = nulls.rename(columns={"path_in_schema": "column"})
fig = px.strip(nulls.assign(domain=nulls["domain"].astype(str)), x="null_pct", y="domain",
               hover_data={"table": True, "column": True, "null_pct": ":.1f", "domain": False},
               category_orders={"domain": DOMAINS}, color_discrete_sequence=[PALETTE[0]])
fig.update_traces(marker=dict(size=6, opacity=0.45), jitter=0.8)
fig.update_xaxes(title="% of rows missing (one dot per column)", ticksuffix="%", range=[-3, 103])
fig.update_layout(title=f"Column completeness: all {len(nulls):,} columns in {nulls['table'].nunique()} tables",
                  yaxis_title=None)
empty = nulls[nulls["null_pct"] >= 99.99]
show(fig, height=520, subtitle=f"{len(empty)} columns are entirely empty: fields a source stopped filling or "
                               "never filled. Hover a dot for table and column.")
print("Entirely empty columns, by table:")
table(empty.groupby("table")["column"].apply(lambda s: ", ".join(sorted(s))).reset_index()
      .rename(columns={"column": "empty columns"}), 60)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 10. Energy and commodities
# MAGIC Futures closes from Yahoo Finance (`futures`), weekly natural gas storage from the EIA, and the
# MAGIC CFTC Commitments of Traders report (`cot`).

# COMMAND ----------

fut = q(f"""SELECT symbol, name, category, CAST(date AS DATE) AS date, close FROM {T('futures')}
            WHERE category IN ('energy', 'metals', 'agriculture', 'industrial') AND close > 0""")
fut["date"] = pd.to_datetime(fut["date"])
rows = []
for sym, d in fut.sort_values("date").groupby("symbol"):
    end = d.iloc[-1]
    base = d[d["date"] <= end["date"] - pd.DateOffset(years=1)]
    if base.empty or end["date"] < TODAY - pd.Timedelta(days=14):
        continue  # no year-ago price, or the contract stopped updating
    rows.append({"symbol": sym, "label": f"{end['name']} ({end['category']})",
                 "chg": (end["close"] / base["close"].iloc[-1] - 1) * 100, "asof": end["date"]})
board = pd.DataFrame(rows).sort_values("chg")
fig = go.Figure(go.Bar(x=board["chg"], y=board["label"], orientation="h",
                       marker_color=np.where(board["chg"] >= 0, POS, NEG), customdata=board["symbol"],
                       hovertemplate="%{y} [%{customdata}]: %{x:+.1f}%<extra></extra>"))
fig.update_xaxes(title="price change over 12 months (%)", ticksuffix="%")
fig.update_layout(title="Commodity futures scoreboard: 12-month price change", yaxis_title=None, margin=dict(l=230))
show(fig, height=max(420, 22 * len(board) + 140),
     subtitle=f"{len(board)} front-month contracts as of {board['asof'].max():%Y-%m-%d}. Continuous front-month "
              "prices include roll jumps, so this is a price change, not a futures return.")

# COMMAND ----------

ng = q(f"""SELECT CAST(date AS DATE) AS date, value FROM {T('eia_natgas_storage')}
           WHERE duoarea = 'R48' AND value IS NOT NULL ORDER BY date""")
ng["date"] = pd.to_datetime(ng["date"])
ng["yr"] = ng["date"].dt.year
ng["wk"] = ng["date"].dt.isocalendar().week.astype(int).clip(upper=52)
cur = int(ng["yr"].max())
band = ng[ng["yr"].between(cur - 5, cur - 1)].groupby("wk")["value"].agg(["min", "max", "mean"])
fig = go.Figure()
fig.add_scatter(x=band.index, y=band["max"], line=dict(width=0), showlegend=False, hoverinfo="skip")
fig.add_scatter(x=band.index, y=band["min"], fill="tonexty", fillcolor="rgba(42,120,214,0.12)", line=dict(width=0),
                name=f"{cur - 5}-{cur - 1} range", hoverinfo="skip")
fig.add_scatter(x=band.index, y=band["mean"], name="5-year average", line=dict(color=MUTED, dash="dot", width=1.5))
for yr, color, width in [(cur - 1, INK2, 1.5), (cur, PALETTE[0], 2.8)]:
    d = ng[ng["yr"] == yr]
    fig.add_scatter(x=d["wk"], y=d["value"], name=str(yr), line=dict(color=color, width=width),
                    hovertemplate=f"{yr} week %{{x}}: %{{y:,.0f}} Bcf<extra></extra>")
fig.update_xaxes(title="week of year")
fig.update_yaxes(title="working gas in storage (Bcf)")
fig.update_layout(title="US natural gas storage (Lower 48) against its 5-year range", hovermode="x unified")
last_ng = ng.iloc[-1]
vs_avg = (last_ng["value"] / band.loc[last_ng["wk"], "mean"] - 1) * 100
show(fig, height=460, subtitle=f"Week ending {last_ng['date']:%Y-%m-%d}: {last_ng['value']:,.0f} Bcf, "
                               f"{vs_avg:+.1f}% against the 5-year average for that week. Storage fills April-October "
                               "and draws down in winter.")

# COMMAND ----------

COT_MARKETS = {"GOLD - COMMODITY EXCHANGE INC.": "Gold (COMEX)",
               "S&P 500 Consolidated - CHICAGO MERCANTILE EXCHANGE": "S&P 500 (CME, consolidated)",
               "CRUDE OIL, LIGHT SWEET-WTI - ICE FUTURES EUROPE": "WTI crude (ICE Europe)",
               "BITCOIN - CHICAGO MERCANTILE EXCHANGE": "Bitcoin (CME)"}
mkt_list = ",".join("'" + m.replace("'", "''") + "'" for m in COT_MARKETS)
cot = q(f"""SELECT market, CAST(date AS DATE) AS date,
                   (noncomm_long - noncomm_short) * 100.0 / NULLIF(open_interest, 0) AS net_spec_pct
            FROM {T('cot')}
            WHERE market IN ({mkt_list}) AND CAST(date AS DATE) >= DATE '2015-01-01'
            ORDER BY date""")
fig = make_subplots(rows=2, cols=2, subplot_titles=list(COT_MARKETS.values()), vertical_spacing=0.14,
                    horizontal_spacing=0.07)
for k, (mkt, lab) in enumerate(COT_MARKETS.items()):
    d = cot[cot["market"] == mkt]
    r, c = divmod(k, 2)
    fig.add_scatter(x=d["date"], y=d["net_spec_pct"], line=dict(color=PALETTE[0], width=1.6), showlegend=False,
                    hovertemplate=f"{lab}<br>%{{x|%Y-%m-%d}}: %{{y:+.1f}}% of open interest<extra></extra>",
                    row=r + 1, col=c + 1)
    fig.add_hline(y=0, line=dict(color=AXIS, width=1), row=r + 1, col=c + 1)
fig.update_yaxes(ticksuffix="%")
fig.update_layout(title="Speculator positioning: non-commercial net long as a share of open interest")
show(fig, height=560, subtitle=f"CFTC legacy report, weekly. Latest report in the table: {cot['date'].max()}. "
                               "Above zero, large speculators are net long.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 11. Government, banks and credit
# MAGIC From the Treasury's Fiscal Data API (debt to the penny, auction results) and the FDIC's
# MAGIC failed-bank list.

# COMMAND ----------

debt = q(f"""SELECT CAST(record_date AS DATE) AS date, debt_held_public_amt / 1e12 AS public,
                    intragov_hold_amt / 1e12 AS intragov,
                    try_cast(tot_pub_debt_out_amt AS DOUBLE) / 1e12 AS total  -- stored as text on HF
             FROM {T('treasury_debt_to_penny')} WHERE try_cast(tot_pub_debt_out_amt AS DOUBLE) > 0
             ORDER BY date""")
debt["date"] = pd.to_datetime(debt["date"])
fig = go.Figure()
fig.add_scatter(x=debt["date"], y=debt["public"], name="held by the public", stackgroup="one",
                line=dict(width=0.5, color=PALETTE[0]), hovertemplate="public: $%{y:.2f}T<extra></extra>")
fig.add_scatter(x=debt["date"], y=debt["intragov"], name="intragovernmental (trust funds)", stackgroup="one",
                line=dict(width=0.5, color=PALETTE[1]), hovertemplate="intragov: $%{y:.2f}T<extra></extra>")
fig.update_yaxes(title="trillions of dollars", tickprefix="$", ticksuffix="T")
fig.update_layout(title="US federal debt outstanding, daily since 1993", hovermode="x unified")
lt = debt.iloc[-1]
yr_ago = debt[debt["date"] <= lt["date"] - pd.DateOffset(years=1)].iloc[-1]
show(fig, height=440, subtitle=f"${lt['total']:.2f}T on {lt['date']:%Y-%m-%d}, up ${lt['total'] - yr_ago['total']:.2f}T "
                               f"in a year. {lt['public'] / lt['total']:.0%} is held by the public.")

# COMMAND ----------

auc = q(f"""
    SELECT CAST(auction_date AS DATE) AS date, security_term AS term, bid_to_cover_ratio AS btc,
           try_cast(indirect_bidder_accepted AS DOUBLE) * 100
             / NULLIF(try_cast(comp_accepted AS DOUBLE), 0) AS indirect_pct
    FROM {T('treasury_auctions_detail')}
    WHERE security_type IN ('Note', 'Bond') AND security_term IN ('2-Year', '10-Year', '30-Year')
      AND inflation_index_security = 'No' AND floating_rate = 'No' AND bid_to_cover_ratio > 0
    ORDER BY date""")
auc["date"] = pd.to_datetime(auc["date"])
fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08,
                    subplot_titles=("Bid-to-cover ratio (dollars bid per dollar sold)",
                                    "Indirect bidders' share of competitive awards (%)"))
for i, term in enumerate(["2-Year", "10-Year", "30-Year"]):
    d = auc[auc["term"] == term]
    fig.add_scatter(x=d["date"], y=d["btc"].rolling(6, min_periods=3).mean(), name=f"{term} (6-auction mean)",
                    line=dict(color=PALETTE[i]), legendgroup=term, row=1, col=1)
    fig.add_scatter(x=d["date"], y=d["indirect_pct"].rolling(6, min_periods=3).mean(), name=term,
                    line=dict(color=PALETTE[i]), legendgroup=term, showlegend=False, row=2, col=1)
fig.update_layout(title="Demand at Treasury coupon auctions (new issues, excluding TIPS and FRNs)",
                  hovermode="x unified")
show(fig, height=560, subtitle="Indirect bidders bid through a dealer and include foreign central banks; "
                               "a falling share is read as weaker overseas demand.")

# COMMAND ----------

fdic = q(f"""SELECT try_cast(substr(CAST(faildate AS VARCHAR), 1, 4) AS INT) AS yr,
                    CASE WHEN savr IN ('FSLIC', 'RTC') THEN 'Savings and loans (FSLIC, RTC)'
                         ELSE 'Banks (FDIC insurance funds)' END AS fund,
                    count(*) AS n
             FROM {T('fdic_failures')} GROUP BY 1, 2""").dropna()
fw = fdic.pivot_table(index="yr", columns="fund", values="n", fill_value=0).sort_index()
fig = go.Figure()
for i, col in enumerate(["Banks (FDIC insurance funds)", "Savings and loans (FSLIC, RTC)"]):
    if col in fw:
        fig.add_bar(x=fw.index, y=fw[col], name=col, marker_color=PALETTE[i],
                    hovertemplate=f"%{{x}}: %{{y}} {col.split(' (')[0].lower()}<extra></extra>")
fig.update_layout(title="Bank failures and assisted rescues per year since 1934", barmode="stack", bargap=0.15,
                  yaxis_title="institutions", hovermode="x unified")
tot = fw.sum(axis=1)
show(fig, height=440, subtitle=f"{int(tot.sum()):,} institutions. Peak: {int(tot.max()):,} in {tot.idxmax()} (the savings and "
                               f"loan crisis). 2008-2012: {int(tot.loc[2008:2012].sum()):,}.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 12. Insiders and settlement fails
# MAGIC Open-market insider purchases and sales from Form 4 filings (scraped daily from Finviz's
# MAGIC rolling page, so history starts August 2026), and the SEC's fails-to-deliver files.

# COMMAND ----------

ins = q(f"""SELECT ticker, owner, relationship, date, "transaction" AS side, value FROM {T('finviz_insider')}
            WHERE "transaction" IN ('Buy', 'Sale') AND value > 0""")
ins["date"] = pd.to_datetime(ins["date"], format="%b %d '%y", errors="coerce")
ins = ins.dropna(subset=["date"])
ins = ins[ins["date"] >= "2026-07-27"]  # Finviz's page is a rolling window; collection began 2026-08-02
ins["week"] = ins["date"].dt.to_period("W").dt.start_time
wk = ins.groupby(["week", "side"])["value"].sum().unstack(fill_value=0) / 1e6
top_buys = ins[ins["side"] == "Buy"].groupby("ticker")["value"].sum().nlargest(12).sort_values() / 1e6
fig = make_subplots(rows=1, cols=2, column_widths=[0.62, 0.38], horizontal_spacing=0.1,
                    subplot_titles=("Weekly dollar value, sales shown below zero ($M)", "Largest insider buying ($M)"))
fig.add_bar(x=wk.index, y=wk.get("Buy", 0), name="purchases", marker_color=POS, row=1, col=1)
fig.add_bar(x=wk.index, y=-wk.get("Sale", 0), name="sales", marker_color=NEG, row=1, col=1)
fig.add_bar(x=top_buys.values, y=top_buys.index, orientation="h", marker_color=POS, showlegend=False,
            hovertemplate="%{y}: $%{x:,.1f}M<extra></extra>", row=1, col=2)
fig.update_layout(title="Insider trading from Form 4 filings", barmode="relative", bargap=0.15)
ratio = wk.get("Sale", pd.Series(0)).sum() / max(wk.get("Buy", pd.Series(0)).sum(), 1e-9)
show(fig, height=460, subtitle=f"{len(ins):,} transactions. Insiders sold ${ratio:,.1f} for every $1 they bought; "
                               "selling is routine (pay, diversification), so buying is the usual signal.")

# COMMAND ----------

ftd_daily = q(f"""
    SELECT strptime(CAST(settlement_date AS VARCHAR), '%Y%m%d')::DATE AS date,
           sum(shares_failed * price) / 1e9 AS usd_bn, count(DISTINCT symbol) AS symbols
    FROM {T('sec_ftd')} WHERE price > 0 AND shares_failed > 0 GROUP BY 1 ORDER BY 1""")
ftd_top = q(f"""
    SELECT symbol, any_value(description) AS description, sum(shares_failed * price) / 1e6 AS usd_m
    FROM {T('sec_ftd')} WHERE price > 0 AND shares_failed > 0 GROUP BY 1 ORDER BY 3 DESC LIMIT 15""")
ftd_top["avg_m"] = ftd_top["usd_m"] / len(ftd_daily)
ftd_top = ftd_top.sort_values("avg_m")
fig = make_subplots(rows=1, cols=2, column_widths=[0.6, 0.4], horizontal_spacing=0.12,
                    subplot_titles=("Value of shares failing to settle, per day ($B)",
                                    "Largest average daily fails ($M)"))
fig.add_bar(x=ftd_daily["date"], y=ftd_daily["usd_bn"], marker_color=PALETTE[0], showlegend=False,
            customdata=ftd_daily["symbols"],
            hovertemplate="%{x|%Y-%m-%d}: $%{y:.2f}B across %{customdata:,} securities<extra></extra>", row=1, col=1)
fig.add_bar(x=ftd_top["avg_m"], y=ftd_top["symbol"], orientation="h", marker_color=PALETTE[0], showlegend=False,
            customdata=ftd_top["description"], hovertemplate="%{y} (%{customdata}): $%{x:,.1f}M<extra></extra>",
            row=1, col=2)
fig.update_layout(title="SEC fails-to-deliver: trades that missed settlement", bargap=0.1)
show(fig, height=460, subtitle=f"{len(ftd_daily)} settlement days, {ftd_daily['date'].min()} to "
                               f"{ftd_daily['date'].max()}. Valued at the prior close in the SEC file; "
                               "ETFs dominate because creation and redemption settles in size.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 13. Strategy research: TradingView strategies against a random-timing null
# MAGIC 781 community strategies written in Pine Script passed an automated screen; 35 have been
# MAGIC translated to Python and scored so far, under a protocol written before any results existed.
# MAGIC
# MAGIC * **Data and split.** Split-adjusted daily prices, a $5 price floor, costs that grow with
# MAGIC   trade size. Development data is everything through 2017 plus 75% of symbols; the rest is a
# MAGIC   holdout, opened at most once per strategy.
# MAGIC * **The test.** Each strategy's real trades are compared with 200 copies whose positions are
# MAGIC   shifted to random points in time (a circular shift), which keeps time in the market and trade
# MAGIC   length the same. A low `pnl_p` means the timing itself earned money; it does not by itself
# MAGIC   mean the strategy made money, so a profit check runs alongside it.
# MAGIC * **Many tests at once.** Benjamini-Hochberg at q = 0.10 controls the share of false
# MAGIC   discoveries across every strategy tested.
# MAGIC
# MAGIC These results are **provisional**: the campaign is not closed, and BH at m = 35 is a preview
# MAGIC of the final correction. The price table holds only companies that survived to today, which
# MAGIC probably flatters strategies that buy after big falls. Two entries ("combined sha strategy" and
# MAGIC "smoothed heiken ashi strategy") are one mechanism published twice, so their rows match. The registry that holds these numbers
# MAGIC is local, so `export_strategy_results.py` writes the small CSV this section reads.

# COMMAND ----------

RAW_BASE = "https://raw.githubusercontent.com/Zanderl1987/financial-data-pipeline/master/notebooks/databricks/data/"


def repo_file(name: str) -> bytes:
    """A file from this notebook's data/ folder: the Git folder copy if present, else GitHub raw."""
    for p in (os.path.join("data", name), os.path.join("notebooks", "databricks", "data", name)):
        if os.path.exists(p):
            with open(p, "rb") as f:
                return f.read()
    with urllib.request.urlopen(RAW_BASE + name, timeout=30) as r:
        return r.read()


tv_meta = json.loads(repo_file("tv_catalog_meta.json"))
tvc = pd.read_csv(io.BytesIO(repo_file("tv_catalog_v2.csv"))).sort_values("pnl_p").reset_index(drop=True)
m_tests, q_fdr = len(tvc), tv_meta["bh_q"]
tvc["rank"] = np.arange(1, m_tests + 1)
tvc["bh_line"] = q_fdr * tvc["rank"] / m_tests
passing = tvc.index[tvc["pnl_p"] <= tvc["bh_line"]]
k_pass = int(passing.max()) + 1 if len(passing) else 0  # BH step-up: everything up to the largest passing rank
tvc["bh_pass"] = tvc["rank"] <= k_pass
VERDICTS = {"candidate": POS, "beat the null, lost money": PALETTE[1],
            "beat the null, exposure mismatch": PALETTE[3], "no timing edge": NEUTRAL}
tvc["verdict"] = np.select(
    [tvc["bh_pass"] & (tvc["exposure_mismatch"] > 0), tvc["bh_pass"] & (tvc["total_pnl_net"] <= 0), tvc["bh_pass"]],
    ["beat the null, exposure mismatch", "beat the null, lost money", "candidate"], default="no timing edge")
cands = tvc[tvc["verdict"] == "candidate"]
p_floor = 1 / (tv_meta["n_perm"] + 1)
tiles([
    ("Admitted by the screen", f"{tv_meta['admitted']:,}", "Pine strategies"),
    ("Scored", f"{m_tests}", f"{m_tests / tv_meta['admitted']:.0%} of admitted"),
    ("Pass BH at q = 0.10", f"{k_pass}", f"of {m_tests}, timing beats the null"),
    ("Candidates", f"{len(cands)}", "also profitable, exposure matched"),
    ("Holdouts still unopened", f"{int((~cands['holdout_spent']).sum())}", "among the candidates"),
])

# COMMAND ----------

fig = go.Figure()
fig.add_scatter(x=tvc["rank"], y=tvc["bh_line"], mode="lines", name=f"BH line: q x rank / {m_tests}",
                line=dict(color=INK2, dash="dash", width=1.5), hoverinfo="skip")
for v, color in VERDICTS.items():
    d = tvc[tvc["verdict"] == v]
    fig.add_scatter(x=d["rank"], y=d["pnl_p"], mode="markers", name=v,
                    marker=dict(color=color, size=10, line=dict(color=SURFACE, width=1)), text=d["strategy"],
                    hovertemplate="<b>%{text}</b><br>rank %{x}, p = %{y:.4f}<extra></extra>")
fig.add_hline(y=p_floor, line=dict(color=AXIS, dash="dot", width=1),
              annotation_text=f"smallest p that {tv_meta['n_perm']} shifts can give",
              annotation_position="bottom right", annotation_font=dict(color=INK2, size=11))
fig.update_yaxes(type="log", title="pnl_p (log scale)")
fig.update_xaxes(title="rank, smallest p first")
fig.update_layout(title="Benjamini-Hochberg: every p-value at or under the dashed line's last crossing passes")
show(fig, height=480, subtitle=f"{k_pass} pass. Ties at the floor mean more permutations could separate them further.")

# COMMAND ----------

fig = go.Figure()
for v, color in VERDICTS.items():
    d = tvc[tvc["verdict"] == v]
    fig.add_scatter(x=d["profit_factor"], y=d["pnl_p"], mode="markers", name=v,
                    marker=dict(color=color, size=np.clip(np.log10(d["n_trades"]) * 4, 6, 26),
                                line=dict(color=SURFACE, width=1)),
                    text=d["strategy"], customdata=np.c_[d["total_pnl_net"] / 1e6, d["n_trades"], d["sharpe"]],
                    hovertemplate="<b>%{text}</b><br>profit factor %{x:.2f}, p = %{y:.4f}<br>net P&L $%{customdata[0]:,.1f}M"
                                  " on %{customdata[1]:,.0f} trades, Sharpe %{customdata[2]:.3f}<extra></extra>")
for r in tvc[tvc["bh_pass"] | (tvc["profit_factor"] > 1.5)].itertuples():
    fig.add_annotation(x=np.log10(r.profit_factor), y=np.log10(r.pnl_p), text=r.strategy, showarrow=False,
                       yshift=12, font=dict(size=10, color=INK2))
fig.add_vline(x=1, line=dict(color=AXIS, width=1), annotation_text="break-even after costs",
              annotation_font=dict(color=INK2, size=11))
fig.update_xaxes(type="log", title="profit factor (gross wins / gross losses, log scale)")
fig.update_yaxes(type="log", autorange="reversed", title="pnl_p (log scale, stronger timing is higher)")
fig.update_layout(title="Making money and having an edge are different questions")
show(fig, height=560, subtitle="Top right: profitable with timing that beats random. Bottom right: profitable only because "
                               "the market rose while it held positions. Marker size grows with trade count.")

# COMMAND ----------

sens = tvc[tvc[["pnl_p", "pnl_p_5bps", "pnl_p_20bps"]].min(axis=1) < 0.5].sort_values("pnl_p", ascending=False)
fig = go.Figure()
for col, lab, color in [("pnl_p_5bps", "5 bps a side", PALETTE[2]), ("pnl_p", "primary (size-based costs)", INK),
                        ("pnl_p_20bps", "20 bps a side", PALETTE[1])]:
    fig.add_scatter(x=sens[col], y=sens["strategy"], mode="markers", name=lab,
                    marker=dict(color=color, size=11 if col == "pnl_p" else 9,
                                symbol="diamond" if col == "pnl_p" else "circle"),
                    hovertemplate=f"%{{y}}: p = %{{x:.4f}} at {lab}<extra></extra>")
fig.update_xaxes(type="log", title="pnl_p (log scale)")
fig.update_layout(title="Does the edge survive higher trading costs?", yaxis_title=None, margin=dict(l=240))
show(fig, height=max(360, 30 * len(sens) + 160),
     subtitle="Strategies with p under 0.5 at any cost level. A strategy whose p jumps at 20 bps "
              "trades too often for its edge to pay for itself.")
print(f"Exported {tv_meta['exported_at']} from results through {tv_meta['last_result_at'][:10]}; "
      f"development split {tv_meta['dev_split']}.")
table(tvc[["rank", "strategy", "verdict", "pnl_p", "profit_factor", "sharpe", "total_pnl_net", "n_trades",
           "median_hold", "null_exposure_ratio", "holdout_spent"]], 40)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 14. Lakehouse engineering: bronze, silver, gold
# MAGIC Section 2 overwrites three tables on every run. This section builds a small pipeline the way a
# MAGIC production one is built, using SPY closes and four CBOE volatility indexes:
# MAGIC
# MAGIC * **Bronze**: data exactly as delivered, append-only, tagged with a batch id and arrival time.
# MAGIC   The second delivery overlaps the first by 40 trading days, as this repo's own incremental
# MAGIC   pipelines do, so bronze holds duplicates on purpose.
# MAGIC * **Silver**: one row per series and date, checked and typed. New deliveries arrive by
# MAGIC   `MERGE`, which inserts new rows, updates rows whose value changed, and does nothing on a
# MAGIC   rerun (idempotent).
# MAGIC * **Gold**: one row per trading day of model-ready features plus the label section 15
# MAGIC   predicts: SPY's realized volatility over the next 21 trading days.
# MAGIC
# MAGIC On Databricks (with `write_delta` = yes) the layers are Delta tables in `workspace.fdp_showcase`
# MAGIC with liquid clustering, a CHECK constraint that rejects bad rows, and time travel to see exactly
# MAGIC what a MERGE changed. Elsewhere the same SQL runs on DuckDB and the Delta-only steps are skipped.

# COMMAND ----------

LAYERS = ["bronze_market", "silver_market", "gold_vol_features"]
VOL_SERIES = ["VIX", "VIX3M", "VVIX", "SKEW"]


def lname(t: str) -> str:
    return f"{FQ}.{t}" if WRITE_DELTA else t


def layer_sql(sql: str) -> pd.DataFrame:
    """Like lake_sql, but the layer tables are also available as {bronze_market} etc."""
    sql = sql.format(**{k: lname(k) for k in list(LAKE) + LAYERS})
    return spark.sql(sql).toPandas() if WRITE_DELTA else con.execute(sql).df()  # noqa: F821


def write_layer(name: str, pdf: pd.DataFrame, mode: str):
    if WRITE_DELTA:
        w = spark.createDataFrame(pdf).write.mode(mode)  # noqa: F821
        if mode == "overwrite":
            w = w.option("overwriteSchema", "true")
        w.saveAsTable(lname(name))
    else:
        con.register("_incoming", pdf)
        con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _incoming" if mode == "overwrite"
                    else f"INSERT INTO {name} SELECT * FROM _incoming")
        con.unregister("_incoming")


def count(t: str, where: str = "TRUE") -> int:
    return int(layer_sql(f"SELECT count(*) AS n FROM {{{t}}} WHERE {where}")["n"].iloc[0])


spy_src = etf_daily.loc[etf_daily["symbol"] == "SPY", ["date", "close"]].assign(series="SPY", source_table="prices")
vol_src = (vix[vix["index_name"].isin(VOL_SERIES)].rename(columns={"index_name": "series"})
           .assign(source_table="cboe_volatility"))
delivery = pd.concat([spy_src, vol_src])[["series", "date", "close", "source_table"]].rename(columns={"close": "value"})
delivery["date"] = pd.to_datetime(delivery["date"])
spy_days = np.sort(delivery.loc[delivery["series"] == "SPY", "date"].unique())
cutoff, overlap_start = spy_days[-21], spy_days[-60]


def batch(frame: pd.DataFrame, batch_id: int) -> pd.DataFrame:
    return frame.assign(batch_id=batch_id, _ingested_at=pd.Timestamp.now(tz="UTC").tz_localize(None))


batch1 = batch(delivery[delivery["date"] <= cutoff], 1)              # history up to three weeks ago
batch2 = batch(delivery[delivery["date"] >= overlap_start], 2)       # the latest 60 days, 40 of them again
write_layer("bronze_market", batch1, "overwrite")

DEDUP_SQL = """
  SELECT series, CAST(date AS DATE) AS date, CAST(value AS DOUBLE) AS value, source_table, batch_id
  FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY series, date ORDER BY _ingested_at DESC) AS rn
        FROM {{bronze_market}} WHERE {where}) b
  WHERE rn = 1
"""
cluster = "CLUSTER BY (series, date)" if WRITE_DELTA else ""
layer_sql(f"CREATE OR REPLACE TABLE {{silver_market}} {cluster} AS "
          + DEDUP_SQL.format(where="batch_id = 1 AND value > 0"))
if WRITE_DELTA:
    sil = lname("silver_market")
    spark.sql(f"ALTER TABLE {sil} DROP CONSTRAINT IF EXISTS value_positive")  # noqa: F821
    spark.sql(f"ALTER TABLE {sil} ADD CONSTRAINT value_positive CHECK (value > 0)")  # noqa: F821
    try:
        spark.sql(f"INSERT INTO {sil} VALUES ('SPY', DATE'1900-01-01', -1.0, 'bad_feed', 0)")  # noqa: F821
        print("WARNING: the CHECK constraint did not reject a negative value")
    except Exception as e:  # the expected outcome: Delta refuses the write
        print("CHECK constraint rejected a bad row as intended:", str(e).splitlines()[0][:160])
    v_before = int(spark.sql(f"DESCRIBE HISTORY {sil} LIMIT 1").collect()[0]["version"])  # noqa: F821

write_layer("bronze_market", batch2, "append")
print(f"bronze: {count('bronze_market'):,} rows in 2 deliveries, "
      f"{count('bronze_market') - len(delivery):,} of them duplicates by design")

# COMMAND ----------

MERGE_SQL = """
MERGE INTO {silver_market} AS t
USING (""" + DEDUP_SQL.format(where="batch_id = 2 AND value > 0") + """) AS s
ON t.series = s.series AND t.date = s.date
WHEN MATCHED AND t.value <> s.value THEN
  UPDATE SET value = s.value, source_table = s.source_table, batch_id = s.batch_id
WHEN NOT MATCHED THEN
  INSERT (series, date, value, source_table, batch_id)
  VALUES (s.series, s.date, s.value, s.source_table, s.batch_id)
"""

merge_log = []
for attempt in ("first MERGE of delivery 2", "same MERGE again (rerun)"):
    rows_before, b2_before = count("silver_market"), count("silver_market", "batch_id = 2")
    native = layer_sql(MERGE_SQL)
    inserted = count("silver_market") - rows_before
    updated = count("silver_market", "batch_id = 2") - b2_before - inserted
    merge_log.append({"step": attempt, "inserted": inserted, "updated": updated,
                      "silver rows after": count("silver_market")})
    if WRITE_DELTA:
        print(f"{attempt}: Delta reports", native.to_dict("records")[0])
table(pd.DataFrame(merge_log))
dups = layer_sql("SELECT count(*) AS n FROM (SELECT series, date FROM {silver_market} "
                 "GROUP BY series, date HAVING count(*) > 1) d")["n"].iloc[0]
print(f"silver duplicate (series, date) keys: {dups}")

if WRITE_DELTA:
    hist = spark.sql(f"DESCRIBE HISTORY {sil}").toPandas()  # noqa: F821
    hist = hist[hist["version"] >= v_before][["version", "timestamp", "operation", "operationMetrics"]]
    hist["inserted"] = hist["operationMetrics"].map(lambda m: (m or {}).get("numTargetRowsInserted"))
    hist["updated"] = hist["operationMetrics"].map(lambda m: (m or {}).get("numTargetRowsUpdated"))
    print("Delta history of the silver table since the MERGEs began:")
    table(hist.drop(columns="operationMetrics"))
    changed = spark.sql(f"""SELECT series, min(date) AS first_date, max(date) AS last_date, count(*) AS rows
                            FROM (SELECT * FROM {sil} EXCEPT SELECT * FROM {sil} VERSION AS OF {v_before})
                            GROUP BY series ORDER BY series""").toPandas()  # noqa: F821
    print(f"Time travel: rows in silver now that were not in version {v_before}, before the MERGEs:")
    table(changed)

# COMMAND ----------

GOLD_SQL = """
CREATE OR REPLACE TABLE {gold_vol_features} CLUSTERING AS
WITH px AS (
  SELECT date, value AS close FROM {silver_market} WHERE series = 'SPY'
), r AS (
  SELECT date, LN(close / LAG(close) OVER (ORDER BY date)) AS lr FROM px
), rc AS (
  SELECT date, CASE WHEN ABS(lr) > 0.35 THEN 0 ELSE lr END AS lr FROM r WHERE lr IS NOT NULL
), feat AS (
  SELECT date,
         STDDEV_SAMP(lr) OVER (ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) * SQRT(252) * 100 AS rv_5d,
         STDDEV_SAMP(lr) OVER (ORDER BY date ROWS BETWEEN 20 PRECEDING AND CURRENT ROW) * SQRT(252) * 100 AS rv_21d,
         STDDEV_SAMP(lr) OVER (ORDER BY date ROWS BETWEEN 62 PRECEDING AND CURRENT ROW) * SQRT(252) * 100 AS rv_63d,
         SUM(lr) OVER (ORDER BY date ROWS BETWEEN 20 PRECEDING AND CURRENT ROW) * 100 AS ret_21d,
         STDDEV_SAMP(lr) OVER (ORDER BY date ROWS BETWEEN 1 FOLLOWING AND 21 FOLLOWING) * SQRT(252) * 100 AS fwd,
         COUNT(lr) OVER (ORDER BY date ROWS BETWEEN 1 FOLLOWING AND 21 FOLLOWING) AS n_fwd
  FROM rc
), idx AS (
  SELECT date,
         MAX(CASE WHEN series = 'VIX' THEN value END) AS vix,
         MAX(CASE WHEN series = 'VIX3M' THEN value END) AS vix3m,
         MAX(CASE WHEN series = 'VVIX' THEN value END) AS vvix,
         MAX(CASE WHEN series = 'SKEW' THEN value END) AS skew_idx
  FROM {silver_market} WHERE series <> 'SPY' GROUP BY date
)
SELECT f.date, f.rv_5d, f.rv_21d, f.rv_63d, f.ret_21d,
       i.vix, i.vix3m, i.vvix, i.skew_idx, i.vix / i.vix3m AS vix_term_ratio,
       y.y_10yr - y.y_3mo AS term_spread,
       CASE WHEN f.n_fwd = 21 THEN f.fwd END AS label_rv_fwd_21d
FROM feat f
LEFT JOIN idx i ON f.date = i.date
LEFT JOIN {yield_curve} y ON f.date = CAST(y.date AS DATE)
"""
layer_sql(GOLD_SQL.replace("CLUSTERING", "CLUSTER BY (date)" if WRITE_DELTA else ""))
if WRITE_DELTA:
    spark.sql(f"OPTIMIZE {lname('gold_vol_features')}")  # noqa: F821
    gold_version = int(spark.sql(f"DESCRIBE HISTORY {lname('gold_vol_features')} LIMIT 1")  # noqa: F821
                       .collect()[0]["version"])
    detail = pd.concat([spark.sql(f"DESCRIBE DETAIL {lname(t)}").toPandas()  # noqa: F821
                        .assign(layer=t)[["layer", "numFiles", "sizeInBytes", "clusteringColumns"]] for t in LAYERS])
    table(detail)
else:
    gold_version = None
print({t: f"{count(t):,} rows" for t in LAYERS})
print("Every feature uses data up to and including its own date; only the label looks forward.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 15. Machine learning with MLflow: forecasting next month's SPY volatility
# MAGIC **Question:** how volatile will SPY be over the next 21 trading days? Volatility clusters, so it
# MAGIC is far more predictable than returns, and the options market prices its own forecast (the VIX).
# MAGIC
# MAGIC **Method.** Features and label come from the gold table. Five forecasters, fixed before any
# MAGIC test year was scored: two that need no fitting (last month's realized volatility, and the VIX
# MAGIC itself), the HAR regression on daily, weekly and monthly realized volatility that is the
# MAGIC standard benchmark, HAR plus the VIX, and gradient-boosted trees on all ten features.
# MAGIC Walk-forward: for each year from 2014, fit on every earlier day and predict that year. The last
# MAGIC 21 training rows before each test year are dropped, because their labels overlap the test year.
# MAGIC
# MAGIC On Databricks every run is logged to an MLflow experiment (parameters, per-year errors, the
# MAGIC gold table's Delta version), and the forecaster with the lowest walk-forward error is refit on
# MAGIC all data and registered in Unity Catalog as `workspace.fdp_showcase.spy_vol_forecaster` with
# MAGIC the alias `champion`. Picking the best of three on the same walk-forward makes its reported
# MAGIC error slightly optimistic.

# COMMAND ----------

from sklearn.compose import ColumnTransformer  # noqa: E402
from sklearn.ensemble import HistGradientBoostingRegressor  # noqa: E402
from sklearn.inspection import permutation_importance  # noqa: E402
from sklearn.linear_model import LinearRegression  # noqa: E402
from sklearn.pipeline import Pipeline  # noqa: E402

gold = layer_sql("SELECT * FROM {gold_vol_features} ORDER BY date")
gold["date"] = pd.to_datetime(gold["date"])
FEATURES = ["rv_5d", "rv_21d", "rv_63d", "ret_21d", "vix", "vix3m", "vvix", "skew_idx", "vix_term_ratio",
            "term_spread"]
LABEL, HORIZON, TRAIN_START, FIRST_TEST = "label_rv_fwd_21d", 21, "2010-01-01", 2014
data = gold[gold["date"] >= TRAIN_START].dropna(subset=FEATURES).reset_index(drop=True)
labeled = data.dropna(subset=[LABEL]).reset_index(drop=True)

vr = labeled[labeled["vix"] > labeled[LABEL]].shape[0] / len(labeled)
fig = go.Figure()
fig.add_scattergl(x=labeled["vix"], y=labeled[LABEL], mode="markers", showlegend=False,
                  marker=dict(color=PALETTE[0], size=5, opacity=0.35),
                  customdata=labeled["date"].dt.strftime("%Y-%m-%d"),
                  hovertemplate="%{customdata}: VIX %{x:.1f}, next 21 days realized %{y:.1f}%<extra></extra>")
mx = float(np.nanpercentile(labeled[["vix", LABEL]].values, 99.5))
fig.add_scatter(x=[5, mx], y=[5, mx], mode="lines", line=dict(color=INK2, dash="dash", width=1), name="forecast = outcome")
fig.update_xaxes(title="VIX at the close (market's 30-day implied volatility, %)", range=[5, mx])
fig.update_yaxes(title="SPY realized volatility over the next 21 trading days (%)", range=[0, mx])
fig.update_layout(title="The VIX against what actually happened next")
show(fig, height=520, subtitle=f"{len(labeled):,} days since {TRAIN_START[:4]}. On {vr:.0%} of them the VIX was above the "
                               "volatility that followed: option buyers pay a premium for protection.")

# COMMAND ----------


def pipe(cols, est):
    return Pipeline([("select", ColumnTransformer([("keep", "passthrough", cols)])), ("model", est)])


MODELS = {
    "HAR regression": lambda: pipe(["rv_5d", "rv_21d", "rv_63d"], LinearRegression()),
    "HAR + VIX": lambda: pipe(["rv_5d", "rv_21d", "rv_63d", "vix"], LinearRegression()),
    "gradient boosting": lambda: pipe(FEATURES, HistGradientBoostingRegressor(
        max_iter=300, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=40, l2_regularization=1.0,
        random_state=0)),
}
BASELINES = {"persistence (last 21 days)": "rv_21d", "VIX (implied)": "vix"}
NAMES = list(BASELINES) + list(MODELS)

test_years = [y for y in sorted(labeled["date"].dt.year.unique()) if y >= FIRST_TEST]
folds, last_fit = [], {}
for y in test_years:
    test = labeled[labeled["date"].dt.year == y]
    train = labeled[labeled["date"] < test["date"].min()].iloc[:-HORIZON]  # embargo
    out = test[["date", LABEL]].copy()
    for name, col in BASELINES.items():
        out[name] = test[col].to_numpy()
    for name, make in MODELS.items():
        est = make().fit(train[FEATURES], train[LABEL])
        out[name] = est.predict(test[FEATURES])
        last_fit[name] = est
    folds.append(out)
oos = pd.concat(folds, ignore_index=True)
err = {n: oos[n] - oos[LABEL] for n in NAMES}


def rmse(e):
    return float(np.sqrt(np.mean(e ** 2)))


yearly = pd.DataFrame([{"year": int(y), **{n: rmse(g[n] - g[LABEL]) for n in NAMES}}
                       for y, g in oos.groupby(oos["date"].dt.year)])
sse_persist = float((err[NAMES[0]] ** 2).sum())
pooled = pd.DataFrame({
    "model": NAMES,
    "rmse": [rmse(err[n]) for n in NAMES],
    "mae": [float(err[n].abs().mean()) for n in NAMES],
    "bias": [float(err[n].mean()) for n in NAMES],
    "r2_vs_persistence": [1 - float((err[n] ** 2).sum()) / sse_persist for n in NAMES],
}).sort_values("rmse").reset_index(drop=True)


def dm_test(e_a, e_b, lag=HORIZON):
    """Diebold-Mariano on squared errors with a Newey-West variance (labels overlap for 21 days).
    t < 0 means model A has the smaller errors."""
    d = np.asarray(e_a) ** 2 - np.asarray(e_b) ** 2
    n, dc = len(d), d - d.mean()
    gamma = [float(np.dot(dc[k:], dc[:n - k]) / n) for k in range(lag + 1)]
    var = gamma[0] + 2 * sum((1 - k / (lag + 1)) * gamma[k] for k in range(1, lag + 1))
    t = d.mean() / math.sqrt(var / n)
    return t, math.erfc(abs(t) / math.sqrt(2))


dm_rows = []
for a, b in [("HAR + VIX", "persistence (last 21 days)"), ("HAR + VIX", "HAR regression"),
             ("gradient boosting", "HAR + VIX")]:
    t, p = dm_test(err[a], err[b])
    dm_rows.append({"model A": a, "model B": b, "DM t (negative = A better)": round(t, 2), "p": round(p, 4)})
print(f"Walk-forward, out of sample: {len(oos):,} days in {test_years[0]}-{test_years[-1]}")
table(pooled.round(3))
table(pd.DataFrame(dm_rows))

# COMMAND ----------

fig = go.Figure()
for i, n in enumerate(NAMES):
    fig.add_scatter(x=yearly["year"], y=yearly[n], name=n, mode="lines+markers",
                    line=dict(color=PALETTE[i], width=2.4 if n in MODELS else 1.6,
                              dash=None if n in MODELS else "dot"),
                    hovertemplate=f"{n} %{{x}}: RMSE %{{y:.2f}} vol points<extra></extra>")
fig.update_xaxes(title="test year", dtick=1)
fig.update_yaxes(title="RMSE (annualized volatility points)")
fig.update_layout(title="Out-of-sample error by year (lower is better)", hovermode="x unified")
best = pooled.iloc[0]
show(fig, height=460, subtitle=f"Lowest pooled error: {best['model']} (RMSE {best['rmse']:.2f}, "
                               f"{best['r2_vs_persistence']:.0%} less squared error than persistence). "
                               "Dotted lines need no fitting.")

recent = oos[oos["date"] >= oos["date"].max() - pd.DateOffset(years=3)]
fig = go.Figure()
fig.add_scatter(x=recent["date"], y=recent[LABEL], name="actual (next 21 days)", line=dict(color=INK, width=2))
for i, n in enumerate(["VIX (implied)", "HAR + VIX", "gradient boosting"]):
    fig.add_scatter(x=recent["date"], y=recent[n], name=n, line=dict(color=PALETTE[NAMES.index(n)], width=1.4))
fig.update_yaxes(title="annualized volatility %")
fig.update_layout(title="Forecasts against outcomes, last three test years", hovermode="x unified")
show(fig, height=460, subtitle="Each point is the forecast made that day for the following 21 trading days.")

# COMMAND ----------

gb_last, last_test = last_fit["gradient boosting"], labeled[labeled["date"].dt.year == test_years[-1]]
pi = permutation_importance(gb_last, last_test[FEATURES], last_test[LABEL], n_repeats=10, random_state=0,
                            scoring="neg_root_mean_squared_error")
imp = pd.Series(pi.importances_mean, index=FEATURES).sort_values()
fig = go.Figure(go.Bar(x=imp.values, y=imp.index, orientation="h", marker_color=PALETTE[0],
                       error_x=dict(type="data", array=pd.Series(pi.importances_std, index=FEATURES)[imp.index].values,
                                    color=MUTED, thickness=1),
                       hovertemplate="%{y}: +%{x:.2f} RMSE when shuffled<extra></extra>"))
fig.update_xaxes(title="increase in RMSE when the feature is shuffled (vol points)")
fig.update_layout(title=f"What the gradient-boosted model relies on ({test_years[-1]} test year)")
show(fig, height=440, subtitle="Permutation importance on held-out data. Correlated features (the three realized "
                               "volatilities, VIX and VIX3M) share credit, so each one alone looks smaller.")

# COMMAND ----------

champion = pooled[pooled["model"].isin(list(MODELS))].iloc[0]["model"]
final_model = MODELS[champion]().fit(labeled[FEATURES], labeled[LABEL])
UC_MODEL = f"{CATALOG}.{SCHEMA}.spy_vol_forecaster"
try:
    import mlflow
    from mlflow.models import infer_signature
except ImportError:
    mlflow = None
registered_version = None

if mlflow is not None and ON_DATABRICKS:
    mlflow.autolog(disable=True)  # one run per fold would bury the experiment
    user = spark.sql("SELECT current_user()").first()[0]  # noqa: F821
    mlflow.set_experiment(f"/Users/{user}/fdp_spy_vol_forecast")
    mlflow.set_registry_uri("databricks-uc")
    with mlflow.start_run(run_name=f"walk-forward {test_years[0]}-{test_years[-1]}") as parent:
        mlflow.log_params({"label": LABEL, "horizon_days": HORIZON, "embargo_rows": HORIZON,
                           "train_start": TRAIN_START, "test_years": f"{test_years[0]}-{test_years[-1]}",
                           "features": ",".join(FEATURES), "source_table": lname("gold_vol_features"),
                           "source_table_delta_version": gold_version, "champion": champion})
        mlflow.log_text(pooled.to_csv(index=False), "oos_pooled.csv")
        mlflow.log_text(yearly.to_csv(index=False), "oos_by_year.csv")
        mlflow.log_text(pd.DataFrame(dm_rows).to_csv(index=False), "diebold_mariano.csv")
        for r in pooled.itertuples():
            with mlflow.start_run(run_name=r.model, nested=True):
                mlflow.log_param("model", r.model)
                mlflow.log_metrics({"oos_rmse": r.rmse, "oos_mae": r.mae, "oos_bias": r.bias,
                                    "oos_r2_vs_persistence": r.r2_vs_persistence})
                for yr_, v in zip(yearly["year"], yearly[r.model]):
                    mlflow.log_metric("oos_rmse_by_year", float(v), step=int(yr_))
        X_all = labeled[FEATURES]
        try:
            info = mlflow.sklearn.log_model(final_model, "model", input_example=X_all.head(5),
                                            signature=infer_signature(X_all, final_model.predict(X_all)),
                                            registered_model_name=UC_MODEL)
            registered_version = getattr(info, "registered_model_version", None) or max(
                int(v.version) for v in mlflow.MlflowClient().search_model_versions(f"name='{UC_MODEL}'"))
            mlflow.MlflowClient().set_registered_model_alias(UC_MODEL, "champion", registered_version)
            print(f"registered {UC_MODEL} version {registered_version} as @champion ({champion})")
        except Exception as e:  # registration depends on workspace permissions; the runs above are kept
            print(f"model logged to run {parent.info.run_id} but not registered: {type(e).__name__}: {e}")
    print(f"MLflow experiment: /Users/{user}/fdp_spy_vol_forecast, parent run {parent.info.run_id}")
else:
    print("MLflow tracking runs on Databricks only; the evaluation above is unaffected.")

# COMMAND ----------

if registered_version is not None:
    served = mlflow.pyfunc.load_model(f"models:/{UC_MODEL}@champion")
    source = f"{UC_MODEL}@champion (version {registered_version})"
else:
    served, source = final_model, f"in-memory {champion} model"
now_row = data.iloc[[-1]]
forecast = float(np.asarray(served.predict(now_row[FEATURES])).ravel()[0])
tiles([
    ("Forecast, next 21 trading days", f"{forecast:.1f}%", f"SPY annualized volatility, from {now_row['date'].iloc[0]:%Y-%m-%d}"),
    ("VIX that day", f"{now_row['vix'].iloc[0]:.1f}", "the options market's 30-day forecast"),
    ("Realized, last 21 days", f"{now_row['rv_21d'].iloc[0]:.1f}%", "the persistence forecast"),
    ("Walk-forward RMSE", f"{pooled.set_index('model').loc[champion, 'rmse']:.2f}", f"{champion}, vol points"),
])
print(f"Scored with {source}.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Notes
# MAGIC * Every number above is computed when the notebook runs, from the dataset as it stands on
# MAGIC   Hugging Face, so rerunning it tomorrow shows tomorrow's data. Section 13 is the exception:
# MAGIC   it reads a CSV exported from the local evaluation registry.
# MAGIC * `prices` stores unadjusted closes. Section 2 treats single-day moves over 35% as splits and
# MAGIC   removes them, and section 3 is price return without dividends.
# MAGIC * Fund-holding sectors come from the S&P 500 reference table, so funds outside large US stocks
# MAGIC   are mostly "Unclassified".
# MAGIC * The Delta tables in `workspace.fdp_showcase` are overwritten on each run; their history
# MAGIC   (`DESCRIBE HISTORY`) keeps every version. Section 14's bronze layer is reset at the start of
# MAGIC   each run so the two-delivery walkthrough is the same every time.
# MAGIC * Some sources lag: the CFTC table and EIA storage are refreshed less often than prices, and
# MAGIC   each chart's subtitle states the latest date it shows.
# MAGIC * To refresh section 13 after more strategies are scored, run
# MAGIC   `python notebooks/databricks/export_strategy_results.py` from the repo root and commit the
# MAGIC   two files in `notebooks/databricks/data/`.
