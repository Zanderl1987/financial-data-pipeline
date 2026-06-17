"""
validate_synthetic_options.py — compare synthetic theoretical prices against real Schwab chains.

Loads the most recent synthetic_options_*.parquet and options_chain_raw_*.parquet files,
joins them on (symbol, contract_type, strike_price, expiration_date, date), and reports
error metrics (MAE, bias, IV match) broken down by vol_method x model x moneyness bucket
x DTE bucket.

NOTE: only produces populated rows where a synthetic file and a real chain file share the
same capture date. The options_chain_raw_* files start accumulating from when
options_chain_pipeline.py is run with the raw-chain patch applied (2026-06-16+), so the
validation becomes meaningful as history builds up.

Usage
  python validate_synthetic_options.py
  python validate_synthetic_options.py --synthetic storage/raw/synthetic_options/synthetic_options_backfill_20260616.parquet
  python validate_synthetic_options.py --chains storage/raw/options/options_chain_raw_20260616.parquet
"""

import argparse
import glob
import os

import numpy as np
import pandas as pd

import pricing_models as pm

SYNTHETIC_GLOB = os.path.join("storage", "raw", "synthetic_options", "synthetic_options_*.parquet")
CHAINS_GLOB = os.path.join("storage", "raw", "options", "options_chain_raw_*.parquet")


def _latest(pattern):
    files = sorted(glob.glob(pattern))
    if not files:
        return None
    return pd.read_parquet(files[-1])


def _moneyness_bucket(m):
    if m < 0.90:
        return "deep-OTM"
    if m < 0.975:
        return "OTM"
    if m <= 1.025:
        return "ATM"
    if m <= 1.10:
        return "ITM"
    return "deep-ITM"


def _dte_bucket(d):
    if d <= 14:
        return "0-14d"
    if d <= 45:
        return "15-45d"
    if d <= 90:
        return "46-90d"
    return "90d+"


def main(synthetic_path=None, chains_path=None):
    syn = pd.read_parquet(synthetic_path) if synthetic_path else _latest(SYNTHETIC_GLOB)
    real = pd.read_parquet(chains_path) if chains_path else _latest(CHAINS_GLOB)

    if syn is None:
        print("No synthetic options file found. Run synthetic_options_pipeline.py first.")
        return
    if real is None:
        print("No raw chain file found. Run options_chain_pipeline.py with the raw-chain patch.")
        return

    print(f"Synthetic: {len(syn):,} rows, dates {syn['date'].min()}..{syn['date'].max()}")
    print(f"Real chain: {len(real):,} rows, dates {real['date'].min()}..{real['date'].max()}")

    real = real.rename(columns={"expiration_date": "expiration_date"})  # ensure name match
    # Real chain uses the Schwab mark price as the reference.
    real_slim = real[["symbol", "contract_type", "strike_price", "expiration_date",
                       "date", "mark", "underlying_price", "volatility",
                       "delta", "gamma", "theta", "vega"]].copy()
    real_slim = real_slim.rename(columns={
        "mark": "real_mark",
        "underlying_price": "real_spot",
        "volatility": "real_iv",
        "delta": "real_delta", "gamma": "real_gamma",
        "theta": "real_theta", "vega": "real_vega",
    })
    real_slim = real_slim.dropna(subset=["real_mark"])

    joined = syn.merge(real_slim, on=["symbol", "contract_type", "strike_price",
                                       "expiration_date", "date"], how="inner")
    if joined.empty:
        print("\nNo overlapping (date, symbol, strike, expiry, type) rows between synthetic and real.")
        print("This is expected until options_chain_pipeline.py has accumulated real captures on the")
        print("same dates that the synthetic pipeline has priced. Run both pipelines daily.")
        return

    print(f"\nMatched rows: {len(joined):,}")

    # Moneyness and DTE buckets.
    joined["moneyness_bucket"] = joined["moneyness"].apply(_moneyness_bucket)
    joined["dte_bucket"] = joined["days_to_expiration"].apply(_dte_bucket)

    # Price errors.
    joined["price_error"] = joined["theo_price"] - joined["real_mark"]
    joined["abs_price_error"] = joined["price_error"].abs()

    # Implied vol from real mark via BSM inversion.
    print("Computing implied vols from real marks (Newton-Raphson/Brent)...")
    def _iv(row):
        return pm.implied_vol(
            row["real_mark"], row["real_spot_x"] if "real_spot_x" in row.index else row["real_spot"],
            row["strike_price"], row["t_years"], row["r"], row["q"],
            row["contract_type"].lower()
        )
    joined["real_iv_inverted"] = joined.apply(_iv, axis=1)
    joined["iv_error"] = joined["volatility"] - joined["real_iv_inverted"]

    # Greek errors.
    joined["delta_error"] = joined["delta"] - joined["real_delta"]
    joined["gamma_error"] = joined["gamma"] - joined["real_gamma"]
    joined["theta_error"] = joined["theta"] - joined["real_theta"]
    joined["vega_error"] = joined["vega"] - joined["real_vega"]

    # Summary tables.
    metrics = {
        "price_MAE": ("abs_price_error", "mean"),
        "price_bias": ("price_error", "mean"),
        "iv_MAE": ("iv_error", lambda x: x.abs().mean()),
        "delta_MAE": ("delta_error", lambda x: x.abs().mean()),
    }

    print("\n=== Price MAE by vol_method x model ===")
    print(joined.groupby(["vol_method", "model"])["abs_price_error"].mean().round(4).to_string())

    print("\n=== Price bias (synthetic - real) by vol_method x model ===")
    print(joined.groupby(["vol_method", "model"])["price_error"].mean().round(4).to_string())

    print("\n=== Price MAE by moneyness bucket (all models combined) ===")
    print(joined.groupby("moneyness_bucket")["abs_price_error"].mean().round(4).to_string())

    print("\n=== Price MAE by DTE bucket (all models combined) ===")
    print(joined.groupby("dte_bucket")["abs_price_error"].mean().round(4).to_string())

    print("\n=== IV error (our sigma - real IV from mark) by vol_method ===")
    iv_valid = joined.dropna(subset=["real_iv_inverted"])
    if not iv_valid.empty:
        print(iv_valid.groupby("vol_method")["iv_error"].apply(
            lambda x: pd.Series({"MAE": x.abs().mean(), "bias": x.mean()})
        ).round(4).to_string())
    else:
        print("  No valid IV inversions (all real marks may be at intrinsic/degenerate).")

    print("\n=== Delta MAE by vol_method x model ===")
    print(joined.dropna(subset=["delta_error"]).groupby(["vol_method", "model"])
          ["delta_error"].apply(lambda x: x.abs().mean()).round(4).to_string())

    print(f"\nTotal matched rows: {len(joined):,} across {joined['symbol'].nunique()} symbols, "
          f"{joined['date'].nunique()} dates.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Validate synthetic options against real Schwab chains")
    p.add_argument("--synthetic", default=None,
                   help="Path to synthetic_options_*.parquet (default: latest).")
    p.add_argument("--chains", default=None,
                   help="Path to options_chain_raw_*.parquet (default: latest).")
    args = p.parse_args()
    main(synthetic_path=args.synthetic, chains_path=args.chains)
