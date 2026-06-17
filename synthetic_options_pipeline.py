"""
synthetic_options_pipeline.py — theoretical/synthetic historical option prices.

Computes estimated option prices and Greeks for a moneyness x days-to-expiration
grid by sourcing every pricing input ourselves, then pricing with closed-form
models. This yields a deep, free history of option theoreticals (real captured
chains are shallow and only accumulate going forward).

Inputs & sourcing (parquet-first, Yahoo chart API fallback — no keys/auth needed)
  S (spot OHLC) : storage/raw/prices/prices_*.parquet  -> else Yahoo chart API
  r (risk-free) : storage/raw/macro/macro_*.parquet (DGS* curve) -> else Yahoo
                  ^IRX/^FVX/^TNX/^TYX, interpolated to each contract's T
  q (div yield) : Yahoo chart API dividend events (TTM sum / spot; 0 for non-payers)
  sigma         : three methods, all emitted -> cc (close-to-close realized),
                  yz (Yang-Zhang realized), vix (cc scaled by VIX regime)
  K, T          : generated moneyness grid x DTE set
Models          : bsm (European BSM+dividend) and bs2002 (American). All in pricing_models.py.

Output (long format; 3 vol methods x 2 models = 6 rows per contract)
  storage/raw/synthetic_options/synthetic_options_{mode}_{YYYYMMDD}.parquet
  Schema: date, symbol, contract_type, strike_price, expiration_date,
          days_to_expiration, underlying_price, moneyness, t_years, r, q,
          vol_method, volatility, model, theo_price, delta, gamma, theta,
          vega, rho, fetched_at

Conventions: T = calendar days / 365; sigma annualized with sqrt(252).

Usage
  python synthetic_options_pipeline.py                      # incremental: latest date per symbol
  python synthetic_options_pipeline.py --backfill           # every trading day in history
  python synthetic_options_pipeline.py --symbols AAPL,MSFT  # subset of symbols
  python synthetic_options_pipeline.py --models bsm --vol-methods cc   # trim for speed
  python synthetic_options_pipeline.py --moneyness-min 0.85 --moneyness-max 1.15 --moneyness-step 0.05 --dte 7,30,90
"""

import argparse
import datetime
import glob
import json
import os
import time
import urllib.request

import numpy as np
import pandas as pd

import pricing_models as pm

OUTPUT_DIR = os.path.join("storage", "raw", "synthetic_options")
PRICES_GLOB = os.path.join("storage", "raw", "prices", "prices_*.parquet")
MACRO_GLOB = os.path.join("storage", "raw", "macro", "macro_*.parquet")

# DJI-30 default universe (matches price_history_pipeline fallback list).
DEFAULT_SYMBOLS = [
    "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS",
    "GS", "HD", "HON", "IBM", "JNJ", "JPM", "KO", "MCD", "MMM", "MRK",
    "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH", "V", "VZ", "WMT",
]

DEFAULT_DTE = [7, 14, 30, 60, 90, 180, 365]
VOL_METHODS = ["cc", "yz", "vix"]          # -> column sigma_cc / sigma_yz / sigma_vix
MODELS = ["bsm", "bs2002"]
REQUEST_INTERVAL = 0.5                      # seconds between Yahoo API calls
PRICE_CHUNK = 100_000                       # rows per pricing block (bounds memory for bs2002)
MAX_RETRIES = 3
BACKOFF_SECONDS = 30

# Yahoo chart API: treasury yield proxies -> tenor in years.
# ^IRX = 13-week T-bill (~3mo); ^FVX = 5yr; ^TNX = 10yr; ^TYX = 30yr.
YAHOO_TENORS = {"^IRX": 0.25, "^FVX": 5.0, "^TNX": 10.0, "^TYX": 30.0}

_YAHOO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


# ---------------------------------------------------------------------------
# Yahoo chart API helper
# ---------------------------------------------------------------------------

def _yahoo_chart(ticker, range_str="2y", interval="1d", include_dividends=False):
    """Fetch Yahoo Finance v8 chart API for one ticker. Returns parsed JSON or None.

    range_str: '1y', '2y', '5y', 'max', etc.
    include_dividends: set True to include events.dividends in the response.
    """
    events = "dividends" if include_dividends else "history"
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?range={range_str}&interval={interval}&events={events}&includePrePost=false"
    )
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=_YAHOO_HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  429 rate limit for {ticker}, waiting {wait}s (attempt {attempt}).")
                time.sleep(wait)
            elif e.code == 404:
                return None
            else:
                print(f"  HTTP {e.code} for {ticker}: {e.reason}")
                return None
        except Exception as exc:
            print(f"  Error fetching {ticker} (attempt {attempt}): {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_SECONDS)
    return None


def _parse_yahoo_ohlc(data, symbol):
    """Parse v8 chart response into a DataFrame with [symbol, date, open, high, low, close]."""
    try:
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        quotes = result["indicators"]["quote"][0]
        df = pd.DataFrame({
            "symbol": symbol,
            "date": pd.to_datetime(timestamps, unit="s").strftime("%Y-%m-%d"),
            "open": quotes["open"],
            "high": quotes["high"],
            "low": quotes["low"],
            "close": quotes["close"],
        })
        return df.dropna(subset=["close"])
    except (KeyError, IndexError, TypeError):
        return None


def _parse_yahoo_dividends(data):
    """Extract TTM dividend sum from a v8 chart response with events.dividends."""
    try:
        result = data["chart"]["result"][0]
        divs = result.get("events", {}).get("dividends", {})
        if not divs:
            return 0.0
        cutoff = time.time() - 365 * 24 * 3600
        ttm = sum(v["amount"] for v in divs.values() if v.get("date", 0) >= cutoff)
        return float(ttm)
    except (KeyError, TypeError):
        return 0.0


def _parse_yahoo_close_series(data, ticker):
    """Parse v8 chart response into a date->close Series (for rate indices / VIX)."""
    try:
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
        s = pd.Series(closes, index=pd.to_datetime(timestamps, unit="s").strftime("%Y-%m-%d"),
                      dtype=float, name=ticker)
        return s.dropna()
    except (KeyError, IndexError, TypeError):
        return pd.Series(dtype=float)


# ---------------------------------------------------------------------------
# Input loaders
# ---------------------------------------------------------------------------

def _latest_concat(pattern):
    files = sorted(glob.glob(pattern))
    if not files:
        return None
    frames = [pd.read_parquet(f) for f in files]
    return pd.concat(frames, ignore_index=True)


def load_underlying(symbols, backfill):
    """Return long OHLC frame [symbol, date, open, high, low, close].
    Tries local prices parquet first; falls back to Yahoo chart API."""
    df = _latest_concat(PRICES_GLOB)
    if df is not None and not df.empty:
        df = df.sort_values("fetched_at").drop_duplicates(["symbol", "date"], keep="last")
        df = df[df["symbol"].isin(symbols)]
        if not df.empty:
            print(f"Underlying: {df['symbol'].nunique()} symbols from prices parquet "
                  f"({df['date'].min()}..{df['date'].max()}).")
            return df[["symbol", "date", "open", "high", "low", "close"]].copy()

    print("Underlying: no prices parquet -> fetching OHLC from Yahoo chart API.")
    range_str = "2y" if backfill else "1y"
    rows = []
    failed = []
    for i, sym in enumerate(symbols, 1):
        data = _yahoo_chart(sym, range_str=range_str)
        if data:
            parsed = _parse_yahoo_ohlc(data, sym)
            if parsed is not None and not parsed.empty:
                rows.append(parsed)
            else:
                failed.append(sym)
        else:
            failed.append(sym)
        time.sleep(REQUEST_INTERVAL)
    if not rows:
        raise SystemExit("No underlying price data available (prices parquet empty and Yahoo API failed).")
    out = pd.concat(rows, ignore_index=True)
    print(f"Underlying: {out['symbol'].nunique()} symbols from Yahoo "
          f"({out['date'].min()}..{out['date'].max()})."
          + (f" Failed: {failed}" if failed else ""))
    return out


def load_rate_and_vix(date_index):
    """Return (rate_curve_df, vix_series) aligned+ffilled to date_index.

    rate_curve_df: index=date(str), columns=tenor_years(float), values=yield percent.
    vix_series:    index=date(str), value=VIX level (percent).
    Tries macro parquet first; falls back to Yahoo chart API for ^IRX/^FVX/^TNX/^TYX + ^VIX.
    """
    macro = _latest_concat(MACRO_GLOB)
    if macro is not None and not macro.empty:
        macro = macro.sort_values("fetched_at").drop_duplicates(["series_id", "date"], keep="last")
        macro["date"] = pd.to_datetime(macro["date"]).dt.strftime("%Y-%m-%d")
        dgs = macro[macro["series_id"].isin(pm.TREASURY_TENORS)]
        curve = dgs.pivot_table(index="date", columns="series_id", values="value")
        curve = curve.rename(columns=pm.TREASURY_TENORS)
        vix = macro[macro["series_id"] == "VIXCLS"].set_index("date")["value"]
        if not curve.empty:
            print(f"Rates: treasury curve from macro parquet ({len(curve.columns)} tenors).")
            return _align(curve, date_index), _align_series(vix, date_index)

    print("Rates: no macro parquet -> fetching treasury yields + VIX from Yahoo chart API.")
    cols = {}
    for ticker, tenor in YAHOO_TENORS.items():
        data = _yahoo_chart(ticker, range_str="2y")
        if data:
            s = _parse_yahoo_close_series(data, ticker)
            if not s.empty:
                # ^IRX is quoted as annualized %; guard against legacy x10 quoting (> 25%)
                if ticker == "^IRX":
                    s = s.where(s < 25, s / 10.0)
                cols[tenor] = s
        time.sleep(REQUEST_INTERVAL)
    curve = pd.DataFrame(cols) if cols else pd.DataFrame()
    curve.index.name = None

    vix_data = _yahoo_chart("^VIX", range_str="2y")
    vix = _parse_yahoo_close_series(vix_data, "^VIX") if vix_data else pd.Series(dtype=float)
    print(f"Rates: {len(cols)} tenors fetched, VIX {'OK' if not vix.empty else 'unavailable'}.")
    return _align(curve, date_index), _align_series(vix, date_index)


def _align(df, date_index):
    if df.empty:
        return df.reindex(sorted(date_index))
    return df.reindex(sorted(set(date_index) | set(df.index))).sort_index().ffill().bfill()


def _align_series(s, date_index):
    if s.empty:
        return pd.Series(index=sorted(set(date_index)), dtype=float)
    return s.reindex(sorted(set(date_index) | set(s.index))).sort_index().ffill().bfill()


def dividend_yields(symbols, spot_by_symbol):
    """Trailing-12m dividend / latest spot per symbol via Yahoo chart API (0 on failure/non-payer)."""
    q = {}
    for sym in symbols:
        spot = spot_by_symbol.get(sym, 0.0)
        if not spot or spot <= 0:
            q[sym] = 0.0
            continue
        data = _yahoo_chart(sym, range_str="2y", include_dividends=True)
        if data:
            ttm_div = _parse_yahoo_dividends(data)
            q[sym] = max(0.0, ttm_div / spot) if ttm_div > 0 else 0.0
        else:
            q[sym] = 0.0
        time.sleep(REQUEST_INTERVAL)
    print(f"Dividends: resolved q for {len(q)} symbols "
          f"({sum(v > 0 for v in q.values())} payers).")
    return q


# ---------------------------------------------------------------------------
# Volatility panel
# ---------------------------------------------------------------------------

def build_sigma_panel(ohlc, window, vix_series):
    """Per (symbol,date) sigma table with columns sigma_cc, sigma_yz, sigma_vix."""
    frames = []
    for sym, g in ohlc.groupby("symbol"):
        g = g.sort_values("date").set_index("date")
        cc = pm.realized_vol_cc(g["close"], window)
        yz = pm.realized_vol_yang_zhang(g, window)
        vix_aligned = vix_series.reindex(g.index).ffill().bfill()
        vix_ov = pm.vix_overlay(cc, vix_aligned, window) if not vix_aligned.isna().all() else cc
        frames.append(pd.DataFrame({
            "symbol": sym, "date": g.index,
            "sigma_cc": cc.values, "sigma_yz": yz.values, "sigma_vix": vix_ov.values,
        }))
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Grid construction
# ---------------------------------------------------------------------------

def build_grid(ohlc, sigma_panel, rate_curve, q_by_symbol, dtes, moneyness, backfill):
    """Long base-contract frame with all inputs except (vol_method, model) resolved."""
    spot = ohlc.rename(columns={"close": "underlying_price"})[["symbol", "date", "underlying_price"]]
    base = spot.merge(sigma_panel, on=["symbol", "date"], how="left")
    base = base.dropna(subset=["sigma_cc"])  # need at least one vol estimate -> enough history

    if not backfill:
        latest = base.groupby("symbol")["date"].transform("max")
        base = base[base["date"] == latest]

    base["q"] = base["symbol"].map(q_by_symbol).fillna(0.0)

    # Cross with strikes (moneyness) x DTE x option type.
    base = base.merge(pd.DataFrame({"moneyness": moneyness}), how="cross")
    base = base.merge(pd.DataFrame({"days_to_expiration": dtes}), how="cross")
    base = base.merge(pd.DataFrame({"contract_type": ["CALL", "PUT"]}), how="cross")

    base["strike_price"] = (base["moneyness"] * base["underlying_price"]).round(2)
    base["t_years"] = base["days_to_expiration"] / 365.0
    base["expiration_date"] = (
        pd.to_datetime(base["date"]) + pd.to_timedelta(base["days_to_expiration"], unit="D")
    ).dt.strftime("%Y-%m-%d")

    # Risk-free rate: interpolate the curve (as of each date) to each contract T.
    tenors = np.array(rate_curve.columns, dtype=float)
    base["r"] = np.nan
    for d, grp in base.groupby("date"):
        if d in rate_curve.index:
            rates_pct = rate_curve.loc[d].values
            ok = ~np.isnan(rates_pct)
            if ok.sum() >= 2:
                base.loc[grp.index, "r"] = pm.interp_rate(tenors[ok], rates_pct[ok],
                                                          grp["t_years"].values)
    base["r"] = base["r"].fillna(0.04)  # last-resort flat fallback
    return base.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

_MODEL_FN = {"bsm": pm.bsm, "bs2002": pm.bjerksund_stensland_2002}


def price_frame(base, model, sigma_col):
    """Price the whole base frame under one model + one sigma column (chunked)."""
    fn = _MODEL_FN[model]
    S = base["underlying_price"].values
    K = base["strike_price"].values
    T = base["t_years"].values
    r = base["r"].values
    q = base["q"].values
    sig = base[sigma_col].values
    ot = base["contract_type"].values
    n = len(base)
    out = {k: np.empty(n) for k in ("theo_price", "delta", "gamma", "theta", "vega", "rho")}
    for start in range(0, n, PRICE_CHUNK):
        sl = slice(start, min(start + PRICE_CHUNK, n))
        res = fn(S[sl], K[sl], T[sl], r[sl], q[sl], sig[sl], ot[sl])
        out["theo_price"][sl] = res["price"]
        for g in ("delta", "gamma", "theta", "vega", "rho"):
            out[g][sl] = res[g]
    return out


def main(backfill=False, symbols=None, dtes=None, moneyness=None,
         vol_methods=None, models=None, window=30):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    symbols = symbols or DEFAULT_SYMBOLS
    dtes = dtes or DEFAULT_DTE
    if moneyness is None:
        moneyness = np.round(np.arange(0.80, 1.2001, 0.05), 4).tolist()
    vol_methods = vol_methods or VOL_METHODS
    models = models or MODELS
    print(f"Mode: {'BACKFILL' if backfill else 'INCREMENTAL'} | {len(symbols)} symbols | "
          f"{len(moneyness)} strikes x {len(dtes)} DTEs | vols={vol_methods} models={models}")

    ohlc = load_underlying(symbols, backfill)
    date_index = sorted(ohlc["date"].unique())
    rate_curve, vix_series = load_rate_and_vix(date_index)

    spot_by_symbol = (
        ohlc.sort_values("date").groupby("symbol")["close"].last().to_dict()
    )
    q_by_symbol = dividend_yields(symbols, spot_by_symbol)

    sigma_panel = build_sigma_panel(ohlc, window, vix_series)
    base = build_grid(ohlc, sigma_panel, rate_curve, q_by_symbol, dtes, moneyness, backfill)
    if base.empty:
        print("No priceable contracts (insufficient history for volatility). Exiting.")
        return
    print(f"Base contracts: {len(base):,} (per vol_method x model).")

    sigma_cols = {"cc": "sigma_cc", "yz": "sigma_yz", "vix": "sigma_vix"}
    keep = ["date", "symbol", "contract_type", "strike_price", "expiration_date",
            "days_to_expiration", "underlying_price", "moneyness", "t_years", "r", "q"]
    fetched_at = datetime.datetime.utcnow().isoformat()
    results = []
    for vm in vol_methods:
        scol = sigma_cols[vm]
        valid = base.dropna(subset=[scol])
        valid = valid[valid[scol] > 0]
        if valid.empty:
            print(f"  vol_method={vm}: no valid sigma, skipping.")
            continue
        for model in models:
            priced = price_frame(valid, model, scol)
            block = valid[keep].copy()
            block["vol_method"] = vm
            block["volatility"] = valid[scol].values
            block["model"] = model
            for k, v in priced.items():
                block[k] = v
            results.append(block)
            print(f"  priced vol_method={vm} model={model}: {len(block):,} rows.")

    out = pd.concat(results, ignore_index=True)
    out["fetched_at"] = fetched_at

    today = datetime.datetime.utcnow().strftime("%Y%m%d")
    mode_tag = "backfill" if backfill else "incremental"
    path = os.path.join(OUTPUT_DIR, f"synthetic_options_{mode_tag}_{today}.parquet")
    out.to_parquet(path, index=False)

    print(f"\n--- COMPLETE ---")
    print(f"Saved {len(out):,} rows -> {path}")
    print(f"Dates {out['date'].min()}..{out['date'].max()} | symbols {out['symbol'].nunique()}")
    sample = out[(out["moneyness"] == 1.0) & (out["days_to_expiration"] == 30) &
                 (out["contract_type"] == "CALL")]
    if not sample.empty:
        print("\nATM 30-DTE call sample (theo_price by vol_method x model):")
        print(sample.groupby(["vol_method", "model"])["theo_price"].mean().round(3).to_string())


def _parse_floats(s):
    return [float(x) for x in s.split(",") if x.strip()]


def _parse_ints(s):
    return [int(x) for x in s.split(",") if x.strip()]


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Synthetic historical options pricing pipeline")
    p.add_argument("--backfill", action="store_true",
                   help="Price every trading day in history (default: latest date per symbol).")
    p.add_argument("--symbols", type=lambda s: [x.strip().upper() for x in s.split(",")],
                   default=None, help="Comma-separated symbols (default: DJI-30).")
    p.add_argument("--dte", type=_parse_ints, default=None,
                   help=f"Comma-separated days-to-expiration (default: {DEFAULT_DTE}).")
    p.add_argument("--moneyness-min", type=float, default=0.80)
    p.add_argument("--moneyness-max", type=float, default=1.20)
    p.add_argument("--moneyness-step", type=float, default=0.05)
    p.add_argument("--vol-methods", type=lambda s: [x.strip() for x in s.split(",")],
                   default=None, help="Subset of cc,yz,vix (default: all).")
    p.add_argument("--models", type=lambda s: [x.strip() for x in s.split(",")],
                   default=None, help="Subset of bsm,bs2002 (default: all).")
    p.add_argument("--vol-window", type=int, default=30, help="Realized-vol window (trading days).")
    args = p.parse_args()

    mny = np.round(np.arange(args.moneyness_min, args.moneyness_max + 1e-9,
                             args.moneyness_step), 4).tolist()
    main(backfill=args.backfill, symbols=args.symbols, dtes=args.dte, moneyness=mny,
         vol_methods=args.vol_methods, models=args.models, window=args.vol_window)
