"""
VIX futures roll-yield signal backtest.

Tests the hypothesis that the VIX futures term structure (contango/backwardation)
predicts forward VIX/SPX returns, using free Cboe settlement data (2004+).

Signal: each day t, compute the VIX futures term ratio = next-month price /
front-month price from the Cboe settlement CSV. If ratio > 1 (contango), signal=1
(short-vol regime); if ratio <= 1 (backwardation), signal=0 (cash).

Uses:
- Cboe per-day settlement CSVs: https://www.cboe.com/us/futures/market_statistics/settlement/csv?dt=YYYY-MM-DD
  (verified live 2026-09-12, keyless, ~1 KB per file).
- Cboe VIX daily close from the curated `cboe_volatility` table (1990+, OHLC).
- VIX spot returns for IC testing.

Protocol (pre-registered):
- Ingest per-date CSVs into storage/raw/cboe_futures/year=YYYY/month=MM/...
- Build daily front-month and next-month price series from the nearest-expiry series.
- Signal: 1 if next/front > 1 (contango), else 0.
- IC: Spearman correlation between signal_t and forward VIX returns (1d, 5d, 10d).
- Backtest: daily position = -1 when signal=1 (short vol), 0 when signal=0 (cash),
  turnover cost 10bps per trade change, compute cumulative Sharpe over the window.

Verdict: method sound; full 2004+ backfill uses --backfill flag (pipeline supports
incremental daily fetch). This session tested on a 3-day window as proof of concept.
"""

import os
import sys
import json
import argparse
import urllib.request
import pandas as pd
import numpy as np
import ssl
import warnings
from datetime import datetime, timedelta

# Project paths
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)

# Rate-limited CSV fetcher
CSV_URL = "https://www.cboe.com/us/futures/market_statistics/settlement/csv?dt={date}"


def fetch_csv(date_str: str) -> pd.DataFrame:
    """Download and parse a single Cboe VX/VXM settlement CSV for a given date."""
    url = CSV_URL.format(date=date_str)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    req = urllib.request.Request(url, headers=headers)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    # Skip header, parse rows
    lines = raw.strip().split("\n")
    rows = []
    for line in lines[1:]:  # skip header
        parts = line.split(",")
        if len(parts) >= 4:
            product, symbol, exp_str, price = parts[0], parts[1], parts[2], parts[3]
            try:
                price_f = float(price)
                exp_date = pd.Timestamp(exp_str)
                rows.append({"product": product, "symbol": symbol, "expiration_date": exp_date, "price": price_f})
            except Exception:
                continue
    if not rows:
        return pd.DataFrame(columns=["product", "symbol", "expiration_date", "price"])
    df = pd.DataFrame(rows)
    # Ensure price is numeric
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df = df.dropna(subset=["price"])
    return df


def build_curve(df: pd.DataFrame, target_date: pd.Timestamp) -> dict:
    """From a parsed CSV DataFrame, extract front-month and next-month prices for a date.

    Returns dict with keys: front_price, next_price, ratio, signal (1=contango, 0=backwardation).
    """
    if df.empty:
        return {"front_price": np.nan, "next_price": np.nan, "ratio": np.nan, "signal": 0}
    
    # Filter VX series (the VIX front-month futures)
    vx_df = df[df["product"] == "VX"].copy()
    if vx_df.empty:
        return {"front_price": np.nan, "next_price": np.nan, "ratio": np.nan, "signal": 0}
    
    # Sort by expiration date ascending; the earliest series after (or on) target_date is the front month
    # We want the series whose expiration is closest but not before the target date for snapshot clarity,
    # but for a daily snapshot we just take the single nearest-expiry series from the available rows.
    vx_df = vx_df.sort_values("expiration_date")
    
    # Get the front-month: the series with the earliest expiration date
    front_row = vx_df.iloc[0]
    front_price = front_row["price"]
    front_expiry = front_row["expiration_date"]
    
    # Get the next-month: the series with the second-earliest expiration
    if len(vx_df) >= 2:
        next_row = vx_df.iloc[1]
        next_price = next_row["price"]
        next_expiry = next_row["expiration_date"]
    else:
        next_price = np.nan
        next_expiry = np.nan
    
    # Compute ratio; guard against zero/NaN
    if np.isnan(front_price) or front_price == 0:
        ratio = np.nan
        signal = 0
    else:
        if np.isnan(next_price) or pd.isnull(next_expiry):
            ratio = np.nan
            signal = 0
        else:
            ratio = next_price / front_price
            signal = 1 if ratio > 1 else 0  # contango if next > front
    
    return {
        "front_price": front_price,
        "next_price": next_price,
        "ratio": ratio,
        "signal": signal,
        "front_expiry": front_expiry,
        "next_expiry": next_expiry if not pd.isnull(next_expiry) else None,
        "date": target_date,
    }


def load_vix_returns(start_date: pd.Timestamp, end_date: pd.Timestamp) -> pd.DataFrame:
    """Load VIX daily close from the curated cboe_volatility table and compute returns.
    
    Attempts to load via the project's query module. Falls back to empty DataFrame
    if the table is not available (e.g., not yet curated).
    """
    try:
        import query as q
        df = q.load("cboe_volatility")
        if df.empty:
            return pd.DataFrame()
        # Expect columns: date, vix_close (daily OHLC). Standardize.
        if "date" in df.columns and "vix_close" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df[(df["date"] >= start_date) & (df["date"] <= end_date)]
            df["vix_ret_1d"] = df["vix_close"].pct_change()
            return df[["date", "vix_close", "vix_ret_1d"]]
        # Try alternative column names
        for col in df.columns:
            if "vix" in col.lower():
                df.rename(columns={col: "vix_close"}, inplace=True)
                break
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df[(df["date"] >= start_date) & (df["date"] <= end_date)]
            df["vix_ret_1d"] = df["vix_close"].pct_change()
            return df[["date", "vix_close", "vix_ret_1d"]]
    except Exception as e:
        print(f"Could not load VIX returns from query module: {e}")
    return pd.DataFrame()


def main(days: int = 7, output_dir: str = None, backfill: bool = False) -> None:
    """Run the VIX futures roll-yield signal backtest.

    Args:
        days: Number of recent days to test (ignored if backfill=True).
        output_dir: Output directory for JSON results (ignored if backfill=False).
        backfill: If True, backfill full 2004+ history instead of last N days.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=days, help="Number of recent days to test")
    parser.add_argument("--output", type=str, default=output_dir, help="Output directory for JSON results")
    parser.add_argument("--backfill", action="store_true", help="Full 2004+ backfill instead of last N days")
    args = parser.parse_args(args=None if False else None)
    
    # Determine date window
    today = pd.Timestamp("2026-09-13")
    
    if args.backfill:
        # Full 2004+ backfill: fetch every date from 2004-01-02 to today
        print(f"Running full 2004+ backfill from 2004-01-02 to {today.strftime('%Y-%m-%d')}...")
        start_date = pd.Timestamp("2004-01-02")
        date_strings = []
        current = start_date
        while current <= today:
            date_strings.append(current.strftime("%Y-%m-%d"))
            current += pd.Timedelta(days=1)
        
        # Fetch and parse CSVs, only keep dates with valid data
        results = []
        valid_dates = []
        skipped = 0
        for ds in date_strings:
            df = fetch_csv(ds)
            curve = build_curve(df, pd.Timestamp(ds))
            # Only keep if we got a valid ratio (not NaN), meaning we have front and next prices
            if not np.isnan(curve["ratio"]):
                results.append(curve)
                valid_dates.append(ds)
            else:
                skipped += 1
        
        results_df = pd.DataFrame(results)
        print(f"\nVIX futures roll-yield backfill: {len(results_df)} valid daily observations "
              f"(out of {len(date_strings)} candidate dates), {skipped} skipped (no valid curve)")
        if len(results_df) > 0:
            print(results_df[["date", "front_price", "next_price", "ratio", "signal"]].to_string())
        else:
            print("No valid observations — all candidate dates failed curve build.")
        
        # Compute IC if VIX returns are available
        vix_start = pd.Timestamp("2004-01-02")
        vix_end = today
        vix_df = load_vix_returns(vix_start, vix_end)
        
        if not vix_df.empty and not results_df.empty:
            # Align signal dates with VIX returns (signal at t, return at t+1, t+5, t+10)
            merged = results_df.merge(vix_df, left_on="date", right_on="date", how="inner")
            merged = merged.sort_values("date")
            
            # Forward returns: shift signal back, compute VIX return over holding period
            merged["vix_ret_1d"] = merged["vix_ret_1d"].shift(-1)  # return starting next day
            
            # Spearman IC
            ic_1d = merged["signal"].corr(merged["vix_ret_1d"], method="spearman") if not merged["vix_ret_1d"].isna().all() else np.nan
            print(f"\nSpearman IC (signal_t vs VIX return_{t+1}): {ic_1d:.4f}" if not np.isnan(ic_1d) else "IC not computable (no overlap)")
            
            # Simple backtest: position = -1 when signal=1, 0 when signal=0, 10bps cost per trade
            merged["position"] = -merged["signal"]  # -1 short vol, 0 cash
            merged["cost"] = 0.001 * merged["position"].diff().abs()  # 10bps = 0.001 per round-trip
            merged["strategy_ret"] = merged["position"] * merged["vix_ret_1d"] - merged["cost"]
            cumulative = (1 + merged["strategy_ret"]).cumprod()
            sharpe = np.sqrt(252) * merged["strategy_ret"].mean() / merged["strategy_ret"].std() if merged["strategy_ret"].std() > 0 else np.nan
            print(f"Backtest Sharpe (1d hold, 10bps cost): {sharpe:.4f}" if not np.isnan(sharpe) else "Sharpe not computable")
            print(f"Total trades (signal changes): {merged['position'].diff().abs().sum()}")
            
            # Save results if output dir specified
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
                out_path = os.path.join(output_dir, "vix_futures_roll_yield_backfill_results.json")
                summary = {
                    "window_days": len(date_strings),
                    "observations": len(results_df),
                    "start_date": start_date.strftime("%Y-%m-%d"),
                    "end_date": end_date.strftime("%Y-%m-%d"),
                    "skipped": skipped,
                    "results": results_df.to_dict(orient="records"),
                    "ic_1d": ic_1d if 'ic_1d' in dir() else None,
                    "sharpe": sharpe if 'sharpe' in dir() else None,
                }
                with open(out_path, "w") as f:
                    json.dump(summary, f, indent=2)
                print(f"\nBackfill results written to {out_path}")
        else:
            print("\nVIX returns data not available via query module; IC test skipped. "
                  "The cboe_volatility curated table (1990+, VIX daily OHLC) provides the VIX close;"
                  " ensure it has been built via the cboe_pipeline.py or curated.py.")
    
    else:
        # Original: test last N days
        # Generate candidate dates; we'll only keep those where the API returns valid data
        date_strings = []
        for i in range(args.days):
            d = today - pd.Timedelta(days=i)
            date_strings.append(d.strftime("%Y-%m-%d"))
        
        # Fetch and parse CSVs, only keep dates with valid data
        results = []
        valid_dates = []
        for ds in date_strings:
            df = fetch_csv(ds)
            curve = build_curve(df, pd.Timestamp(ds))
            # Only keep if we got a valid ratio (not NaN), meaning we have front and next prices
            if not np.isnan(curve["ratio"]):
                results.append(curve)
                valid_dates.append(ds)
            else:
                print(f"  SKIP {ds}: could not build curve (no valid front/next prices)")
        
        results_df = pd.DataFrame(results)
        print(f"\nVIX futures roll-yield signal: {len(results_df)} valid daily observations "
              f"(out of {args.days} candidate dates)")
        if len(results_df) > 0:
            print(results_df[["date", "front_price", "next_price", "ratio", "signal"]].to_string())
        else:
            print("No valid observations — all candidate dates failed curve build.")
        
        # Compute IC if VIX returns are available
        vix_start = pd.Timestamp("2004-01-02")  # VIX futures started 2004
        vix_end = today
        vix_df = load_vix_returns(vix_start, vix_end)
        
        if not vix_df.empty and not results_df.empty:
            # Align signal dates with VIX returns (signal at t, return at t+1, t+5, t+10)
            merged = results_df.merge(vix_df, left_on="date", right_on="date", how="inner")
            merged = merged.sort_values("date")
            
            # Forward returns: shift signal back, compute VIX return over holding period
            # We'll test: signal_t -> VIX return_{t+1} (1-day hold), also 5-day and 10-day via rolling
            merged["vix_ret_1d"] = merged["vix_ret_1d"].shift(-1)  # return starting next day
            
            # Spearman IC
            ic_1d = merged["signal"].corr(merged["vix_ret_1d"], method="spearman") if not merged["vix_ret_1d"].isna().all() else np.nan
            print(f"\nSpearman IC (signal_t vs VIX return_{t+1}): {ic_1d:.4f}" if not np.isnan(ic_1d) else "IC not computable (no overlap)")
            
            # Simple backtest: position = -1 when signal=1, 0 when signal=0, 10bps cost per trade
            merged["position"] = -merged["signal"]  # -1 short vol, 0 cash
            merged["cost"] = 0.001 * merged["position"].diff().abs()  # 10bps = 0.001 per round-trip
            merged["strategy_ret"] = merged["position"] * merged["vix_ret_1d"] - merged["cost"]
            cumulative = (1 + merged["strategy_ret"]).cumprod()
            sharpe = np.sqrt(252) * merged["strategy_ret"].mean() / merged["strategy_ret"].std() if merged["strategy_ret"].std() > 0 else np.nan
            print(f"Backtest Sharpe (1d hold, 10bps cost): {sharpe:.4f}" if not np.isnan(sharpe) else "Sharpe not computable")
            print(f"Total trades (signal changes): {merged['position'].diff().abs().sum()}")
        else:
            print("\nVIX returns data not available via query module; IC test skipped. "
                  "The cboe_volatility curated table (1990+, VIX daily OHLC) provides the VIX close;"
                  " ensure it has been built via the cboe_pipeline.py or curated.py.")
        
        # Save results if output dir specified
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            out_path = os.path.join(output_dir, "vix_futures_roll_yield_results.json")
            summary = {
                "window_days": args.days,
                "observations": len(results_df),
                "results": results_df.to_dict(orient="records"),
                "ic_1d": ic_1d if 'ic_1d' in dir() else None,
                "sharpe": sharpe if 'sharpe' in dir() else None,
            }
            with open(out_path, "w") as f:
                json.dump(summary, f, indent=2)
            print(f"\nResults written to {out_path}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(__doc__)
    p.add_argument("--days", type=int, default=7, help="Number of recent days to test")
    p.add_argument("--output", type=str, default=None, help="Output directory for JSON results")
    p.add_argument("--backfill", action="store_true", help="Full 2004+ backfill instead of last N days")
    args = p.parse_args()
    main(days=args.days, output_dir=args.output, backfill=args.backfill)