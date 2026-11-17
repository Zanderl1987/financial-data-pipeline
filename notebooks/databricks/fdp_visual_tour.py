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
# MAGIC
# MAGIC Use the widgets at the top to change the options symbol, the fund, the lookback window, and
# MAGIC whether section 2 writes Delta tables.

# COMMAND ----------

# MAGIC %pip install -q "duckdb>=1.1" "plotly>=5.22" "huggingface_hub>=0.24"

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

import math
import time
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
# MAGIC ## Notes
# MAGIC * Every number above is computed when the notebook runs, from the dataset as it stands on
# MAGIC   Hugging Face, so rerunning it tomorrow shows tomorrow's data.
# MAGIC * `prices` stores unadjusted closes. Section 2 treats single-day moves over 35% as splits and
# MAGIC   removes them, and section 3 is price return without dividends.
# MAGIC * Fund-holding sectors come from the S&P 500 reference table, so funds outside large US stocks
# MAGIC   are mostly "Unclassified".
# MAGIC * The Delta tables in `workspace.fdp_showcase` are overwritten on each run; their history
# MAGIC   (`DESCRIBE HISTORY`) keeps every version.
