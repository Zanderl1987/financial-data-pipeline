#!/usr/bin/env python3
"""
Dividend Research Script
Fetches Finnhub metrics for a curated universe of ~90 dividend-focused symbols
and produces a ranked, scored analysis across REITs, BDCs, ETFs, MLPs, and Aristocrats.

Usage:
    python dividend_research.py              # fetch + analyze (saves JSON cache)
    python dividend_research.py --cache-only # re-run analysis from saved cache
"""

import os
import json
import time
import datetime
import argparse
import requests
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")
BASE_URL = "https://finnhub.io/api/v1"
REQUEST_INTERVAL = 1.1  # stay under 60 req/min free tier
CACHE_FILE = "storage/dividend_research_cache.json"

# ---------------------------------------------------------------------------
# Universe — curated by category
# ---------------------------------------------------------------------------
UNIVERSE = {
    "REIT": [
        "O",     # Realty Income — monthly payer, S&P 500
        "NNN",   # NNN REIT — net lease, 34yr dividend growth
        "STAG",  # STAG Industrial — monthly payer, industrial
        "VICI",  # VICI Properties — gaming/experiential
        "WPC",   # W. P. Carey — diversified net lease
        "EPRT",  # Essential Properties Realty
        "ADC",   # Agree Realty — convenience/pharmacy
        "NTST",  # Netstreit — net lease small-cap
        "GOOD",  # Gladstone Commercial — monthly
        "LTC",   # LTC Properties — healthcare REIT
        "OHI",   # Omega Healthcare — skilled nursing
        "MPW",   # Medical Properties Trust — high yield
        "MAIN_R", # placeholder skip
        "AMT",   # American Tower — cell towers
        "CCI",   # Crown Castle — towers/fiber
        "PLD",   # Prologis — logistics/industrial
        "REXR",  # Rexnord — industrial West Coast
        "FR",    # First Industrial Realty
    ],
    "BDC": [
        "MAIN",  # Main Street Capital — monthly + specials
        "ARCC",  # Ares Capital — largest BDC
        "HTGC",  # Hercules Capital — tech/life-sci focus
        "GBDC",  # Golub Capital BDC
        "OBDC",  # Blue Owl Capital BDC
        "TPVG",  # TriplePoint Venture Growth
        "PSEC",  # Prospect Capital
        "CSWC",  # Capital Southwest — monthly
        "GAIN",  # Gladstone Investment
        "NEWT",  # Newtek Business Services
    ],
    "MLP": [
        "EPD",   # Enterprise Products — 26yr growth
        "ET",    # Energy Transfer
        "MMP",   # Magellan Midstream (now ONEOK/OKE)
        "PAA",   # Plains All American
        "MPLX",  # MPLX LP — Marathon Petroleum MLP
        "WES",   # Western Midstream
    ],
    "Covered_Call_ETF": [
        "JEPI",  # JPMorgan Equity Premium Income
        "JEPQ",  # JPMorgan Nasdaq Equity Premium
        "XYLD",  # Global X S&P 500 Covered Call
        "QYLD",  # Global X Nasdaq 100 Covered Call
        "RYLD",  # Global X Russell 2000 Covered Call
        "DIVO",  # Amplify CWP Enhanced Dividend Income
        "SPYI",  # NEOS S&P 500 High Income ETF
        "GPIQ",  # Goldman Sachs Nasdaq Equity Premium
    ],
    "Dividend_ETF": [
        "SCHD",  # Schwab US Dividend Equity — quality + yield
        "VYM",   # Vanguard High Dividend Yield
        "HDV",   # iShares Core High Dividend
        "DVY",   # iShares Select Dividend
        "VIG",   # Vanguard Dividend Appreciation (growth)
        "DGRO",  # iShares Core Dividend Growth
        "SDY",   # SPDR S&P Dividend ETF
        "SPHD",  # Invesco S&P 500 High Dividend Low Vol
        "PEY",   # Invesco High Yield Equity Dividend Achievers
        "FDVV",  # Fidelity High Dividend ETF
    ],
    "High_Yield": [
        "MO",    # Altria — tobacco, ~9% yield
        "PM",    # Philip Morris International
        "T",     # AT&T
        "VZ",    # Verizon
        "INTC",  # Intel — recently cut but recovering
        "IBM",   # IBM — dividend payer
        "WBA",   # Walgreens Boots Alliance
        "PFE",   # Pfizer
        "ABBV",  # AbbVie — Dividend King (via Abbott split)
        "BMY",   # Bristol-Myers Squibb
        "CVX",   # Chevron — Dividend Aristocrat
        "XOM",   # ExxonMobil
        "KMI",   # Kinder Morgan — nat gas infrastructure
        "OKE",   # ONEOK — midstream
        "WMB",   # Williams Companies
    ],
    "Dividend_Aristocrat": [
        "KO",    # Coca-Cola — 62yr growth
        "PG",    # Procter & Gamble — 67yr growth
        "JNJ",   # Johnson & Johnson
        "MMM",   # 3M — high yield after cuts
        "CL",    # Colgate-Palmolive
        "SYY",   # Sysco
        "ADP",   # ADP — 49yr growth
        "ITW",   # Illinois Tool Works
        "GPC",   # Genuine Parts
        "LOW",   # Lowe's
        "HD",    # Home Depot
        "MCD",   # McDonald's
        "NEE",   # NextEra Energy — utility + growth
        "SO",    # Southern Company — utility
        "D",     # Dominion Energy
        "DUK",   # Duke Energy
        "WEC",   # WEC Energy
        "O",     # Realty Income (also REIT)
    ],
}

# Flatten to unique symbols (skip placeholders)
ALL_SYMBOLS = sorted({
    s for syms in UNIVERSE.values() for s in syms
    if not s.endswith("_R") and "_" not in s[1:]
})

# Map each symbol to its primary category
SYMBOL_CATEGORY = {}
for cat, syms in UNIVERSE.items():
    for s in syms:
        if s not in SYMBOL_CATEGORY:
            SYMBOL_CATEGORY[s] = cat


def get_metrics(symbol: str) -> dict | None:
    url = f"{BASE_URL}/stock/metric"
    params = {"symbol": symbol, "metric": "all", "token": FINNHUB_API_KEY}
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            m = data.get("metric", {})
            if m:
                return m
    except Exception as e:
        print(f"    ERROR {symbol}: {e}")
    return None


def get_quote(symbol: str) -> dict | None:
    url = f"{BASE_URL}/quote"
    params = {"symbol": symbol, "token": FINNHUB_API_KEY}
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def fetch_all(symbols: list) -> dict:
    cache = {}
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    total = len(symbols)
    for i, sym in enumerate(symbols, 1):
        print(f"  [{i:>3}/{total}] {sym:<8}", end=" ", flush=True)
        m = get_metrics(sym)
        time.sleep(REQUEST_INTERVAL)
        q = get_quote(sym)
        time.sleep(REQUEST_INTERVAL)
        if m:
            cache[sym] = {"metrics": m, "quote": q or {}}
            print(f"yield={m.get('currentDividendYieldTTM', 0):.2f}%  "
                  f"dps={m.get('dividendPerShareAnnual', 0):.2f}")
        else:
            cache[sym] = None
            print("no data")
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)
    print(f"\n  Cache saved -> {CACHE_FILE}")
    return cache


def score_stock(sym: str, m: dict, cat: str) -> dict:
    """Score each stock on dividend quality. Higher = better."""
    score = 0
    notes = []

    yield_ttm = m.get("currentDividendYieldTTM") or 0
    yield_ind  = m.get("dividendYieldIndicatedAnnual") or 0
    div_growth = m.get("dividendGrowthRate5Y") or 0
    payout_ann = m.get("payoutRatioAnnual") or 0
    payout_ttm = m.get("payoutRatioTTM") or 0
    dps_annual = m.get("dividendPerShareAnnual") or 0
    beta       = m.get("beta") or 1.0
    pe         = m.get("peExclExtraTTM") or 0
    roe        = m.get("roeTTM") or 0
    net_margin = m.get("netProfitMarginTTM") or 0
    fcf_yield  = m.get("pfcfShareTTM") or 0  # price/FCF (lower = more FCF)
    debt_eq    = m.get("totalDebt/totalEquityAnnual") or 0

    # 1. Yield scoring (sweet spot 4–10%; penalize extremes)
    if yield_ttm >= 3:
        score += min(yield_ttm * 3, 25)  # cap at 25 pts
        if yield_ttm > 12:
            score -= 10; notes.append("yield >12% (risk flag)")
        elif yield_ttm > 9:
            score -= 5; notes.append("yield >9% (elevated risk)")
    else:
        notes.append("yield <3%")

    # 2. Dividend growth (5yr CAGR)
    if div_growth >= 10:
        score += 20; notes.append(f"strong div growth {div_growth:.1f}%/yr")
    elif div_growth >= 5:
        score += 12; notes.append(f"solid div growth {div_growth:.1f}%/yr")
    elif div_growth >= 2:
        score += 6
    elif div_growth < 0:
        score -= 15; notes.append("div cut/reduction in 5yr")

    # 3. Payout sustainability (skip for ETFs — payout ratio not meaningful)
    if cat not in ("Covered_Call_ETF", "Dividend_ETF"):
        payout = payout_ttm if payout_ttm else payout_ann
        if 0 < payout <= 60:
            score += 15; notes.append(f"payout {payout:.0f}% (sustainable)")
        elif 60 < payout <= 80:
            score += 8
        elif payout > 100:
            score -= 10; notes.append(f"payout {payout:.0f}% (exceeds earnings)")

    # 4. Low volatility bonus
    if 0 < beta <= 0.7:
        score += 10; notes.append("low beta (defensive)")
    elif beta <= 1.0:
        score += 5
    elif beta > 1.5:
        score -= 5; notes.append("high beta")

    # 5. Financial quality
    if roe >= 15:
        score += 8; notes.append(f"ROE {roe:.1f}%")
    elif roe >= 8:
        score += 4

    if net_margin >= 20:
        score += 5
    elif net_margin < 0:
        score -= 8; notes.append("negative margin")

    # 6. Category bonus (reliability premium)
    cat_bonus = {
        "Dividend_Aristocrat": 8,
        "Covered_Call_ETF":    5,
        "Dividend_ETF":        5,
        "REIT":                3,
        "BDC":                 2,
        "MLP":                 2,
    }
    score += cat_bonus.get(cat, 0)

    # 7. DPS existence sanity check
    if dps_annual <= 0:
        score -= 20; notes.append("no dividend detected")

    return {
        "symbol":          sym,
        "category":        cat,
        "yield_ttm":       round(yield_ttm, 2),
        "yield_indicated": round(yield_ind, 2),
        "dps_annual":      round(dps_annual, 2),
        "div_growth_5y":   round(div_growth, 1),
        "payout_ttm":      round(payout_ttm, 1),
        "beta":            round(beta, 2),
        "roe_ttm":         round(roe, 1),
        "net_margin_ttm":  round(net_margin, 1),
        "score":           round(score, 1),
        "notes":           "; ".join(notes) if notes else "",
    }


def print_report(df: pd.DataFrame):
    divider = "=" * 110

    print(f"\n{divider}")
    print("  DIVIDEND RESEARCH REPORT  —  " + datetime.date.today().isoformat())
    print(divider)

    # --- Top 20 overall ---
    top20 = df[df["yield_ttm"] > 0].nlargest(20, "score")
    print("\n[ TOP 20 OVERALL — by composite score ]")
    print(f"  {'#':<3} {'Symbol':<8} {'Category':<22} {'Yield%':>7} {'DPS':>6} "
          f"{'Grwth%':>7} {'Payout':>7} {'Beta':>5} {'ROE%':>6} {'Score':>6}")
    print("  " + "-" * 100)
    for rank, (_, r) in enumerate(top20.iterrows(), 1):
        print(f"  {rank:<3} {r.symbol:<8} {r.category:<22} {r.yield_ttm:>6.2f}% "
              f"{r.dps_annual:>6.2f} {r.div_growth_5y:>6.1f}% {r.payout_ttm:>6.1f}% "
              f"{r.beta:>5.2f} {r.roe_ttm:>5.1f}% {r.score:>6.1f}")

    # --- Best by category ---
    print(f"\n{divider}")
    print("[ BEST PICK PER CATEGORY ]")
    for cat in sorted(df["category"].unique()):
        sub = df[(df["category"] == cat) & (df["yield_ttm"] > 0)].nlargest(3, "score")
        if sub.empty:
            continue
        print(f"\n  {cat.replace('_', ' ').upper()}")
        print(f"  {'Symbol':<8} {'Yield%':>7} {'DPS':>6} {'Grwth%':>7} {'Payout':>7} "
              f"{'Beta':>5} {'Score':>6}  Notes")
        print("  " + "-" * 90)
        for _, r in sub.iterrows():
            print(f"  {r.symbol:<8} {r.yield_ttm:>6.2f}% {r.dps_annual:>6.2f} "
                  f"{r.div_growth_5y:>6.1f}% {r.payout_ttm:>6.1f}% "
                  f"{r.beta:>5.2f} {r.score:>6.1f}  {r.notes[:60]}")

    # --- High yield (>7%) with positive growth ---
    print(f"\n{divider}")
    hiy = df[(df["yield_ttm"] >= 7) & (df["div_growth_5y"] >= 0)].nlargest(15, "yield_ttm")
    print("[ HIGH YIELD (>=7%) WITH STABLE/GROWING DIVIDEND ]")
    if hiy.empty:
        print("  None found meeting criteria.")
    else:
        for _, r in hiy.iterrows():
            flag = " *** INCOME STANDOUT ***" if r.yield_ttm >= 9 and r.div_growth_5y > 0 else ""
            print(f"  {r.symbol:<8} {r.yield_ttm:>6.2f}%  growth={r.div_growth_5y:>5.1f}%  "
                  f"payout={r.payout_ttm:>5.1f}%  score={r.score:>5.1f}{flag}")

    # --- Dividend growth stars (<5% yield but strong growth) ---
    print(f"\n{divider}")
    grw = df[(df["yield_ttm"] >= 2) & (df["div_growth_5y"] >= 8)].nlargest(10, "div_growth_5y")
    print("[ DIVIDEND GROWTH STARS (>= 8%/yr growth, yield >= 2%) ]")
    if grw.empty:
        print("  None found meeting criteria.")
    else:
        for _, r in grw.iterrows():
            print(f"  {r.symbol:<8} yield={r.yield_ttm:.2f}%  "
                  f"5yr-growth={r.div_growth_5y:.1f}%/yr  payout={r.payout_ttm:.1f}%  score={r.score:.1f}")

    # --- ETF comparison ---
    print(f"\n{divider}")
    etfs = df[df["category"].isin(["Covered_Call_ETF", "Dividend_ETF"])].sort_values("yield_ttm", ascending=False)
    print("[ ETF COMPARISON — Covered Call vs Dividend ETFs ]")
    print(f"  {'Symbol':<8} {'Type':<22} {'Yield%':>7} {'DPS':>6} {'Grwth%':>7} {'Beta':>5} {'Score':>6}")
    print("  " + "-" * 70)
    for _, r in etfs.iterrows():
        if r.yield_ttm > 0:
            print(f"  {r.symbol:<8} {r.category:<22} {r.yield_ttm:>6.2f}% "
                  f"{r.dps_annual:>6.2f} {r.div_growth_5y:>6.1f}% {r.beta:>5.2f} {r.score:>6.1f}")

    # --- No data ---
    no_data = df[df["yield_ttm"] == 0]["symbol"].tolist()
    if no_data:
        print(f"\n{divider}")
        print(f"[ NO DATA / ZERO YIELD ] {', '.join(no_data)}")

    print(f"\n{divider}")
    print(f"  Total symbols analyzed: {len(df)}  |  With dividend data: {(df.yield_ttm > 0).sum()}")
    print(divider)


def main():
    parser = argparse.ArgumentParser(description="Dividend stock research via Finnhub")
    parser.add_argument("--cache-only", action="store_true",
                        help="Skip fetching; re-analyze from saved cache")
    args = parser.parse_args()

    if not FINNHUB_API_KEY and not args.cache_only:
        print("ERROR: FINNHUB_API_KEY not set in .env")
        return

    if args.cache_only and os.path.exists(CACHE_FILE):
        print(f"Loading cache from {CACHE_FILE} ...")
        with open(CACHE_FILE) as f:
            cache = json.load(f)
    else:
        print(f"Fetching metrics for {len(ALL_SYMBOLS)} symbols via Finnhub ...")
        print(f"  Rate limit: {REQUEST_INTERVAL}s/req  |  Est. time: ~{len(ALL_SYMBOLS)*REQUEST_INTERVAL*2/60:.1f} min\n")
        cache = fetch_all(ALL_SYMBOLS)

    # Build scored DataFrame
    rows = []
    for sym in ALL_SYMBOLS:
        cat = SYMBOL_CATEGORY.get(sym, "Other")
        raw = cache.get(sym)
        if raw and raw.get("metrics"):
            row = score_stock(sym, raw["metrics"], cat)
        else:
            row = {
                "symbol": sym, "category": cat, "yield_ttm": 0,
                "yield_indicated": 0, "dps_annual": 0, "div_growth_5y": 0,
                "payout_ttm": 0, "beta": 0, "roe_ttm": 0,
                "net_margin_ttm": 0, "score": -99, "notes": "no data",
            }
        rows.append(row)

    df = pd.DataFrame(rows)

    # Save CSV
    csv_path = "storage/dividend_research_results.csv"
    df.sort_values("score", ascending=False).to_csv(csv_path, index=False)
    print(f"\nFull results saved -> {csv_path}")

    print_report(df)


if __name__ == "__main__":
    main()
