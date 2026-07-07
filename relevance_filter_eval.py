"""
Relevance-filtered sentiment evaluation.

Hypothesis check: a random sample of finnhub_news showed only ~20% of
articles actually mention the symbol they're tagged with (most are generic
market wraps or off-topic wire stories). This likely dilutes the sentiment
signal regardless of scorer quality (VADER or FinBERT).

This script filters news_sentiment down to DIRECT-mention articles only
(company name/ticker literally appears in headline or summary, via
analytics/relevance.py's alias matching) and re-runs sentiment_eval's
forward-return/eval machinery on that cleaner subset, for both the VADER
scores (already in news_sentiment) and the FinBERT cache (finbert_eval.py).

Usage:
  python relevance_filter_eval.py                # both VADER and FinBERT
  python relevance_filter_eval.py --min-articles 2
"""

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import query as q
from analytics import relevance as rel
from sentiment_eval import forward_returns, evaluate
from finbert_eval import daily_signals_from_cache, CACHE_PATH as FINBERT_CACHE
from finbert_eval import print_report


def direct_mention_mask(df: pd.DataFrame, aliases: dict) -> pd.Series:
    """True where `symbol` is textually mentioned in headline or summary."""
    mask = []
    for row in df.itertuples(index=False):
        text = f"{row.headline} {getattr(row, 'summary', '') or ''}"
        mentioned = rel.extract_tickers(text, aliases)
        mask.append(row.symbol in mentioned)
    return pd.Series(mask, index=df.index)


def vader_daily_signals(df: pd.DataFrame, min_articles: int = 1) -> pd.DataFrame:
    df = df.dropna(subset=["symbol", "date", "score"]).copy()
    df["date"] = pd.to_datetime(df["date"])
    w = df["confidence"].clip(lower=0.05) if "confidence" in df.columns else 1.0
    agg = (df.assign(_ws=df["score"] * w, _w=w)
             .groupby(["symbol", "date"])
             .agg(_ws=("_ws", "sum"), _w=("_w", "sum"), n_articles=("score", "size"))
             .reset_index())
    agg["sent_score"] = agg["_ws"] / agg["_w"]
    agg = agg[agg["n_articles"] >= min_articles]
    return agg[["symbol", "date", "sent_score", "n_articles"]].sort_values(["date", "symbol"])


def run_eval(sig: pd.DataFrame, label: str, benchmark: str, min_names: int):
    if sig.empty:
        print(f"\n[{label}] no signals after filtering.")
        return
    print(f"\n[{label}] {len(sig):,} symbol-day signals | {sig['symbol'].nunique()} symbols | "
          f"{sig['date'].min().date()} -> {sig['date'].max().date()}")
    panel = forward_returns(sig, benchmark=benchmark)
    print(f"  {len(panel):,} signals matched to prices")
    results = evaluate(panel, min_names=min_names)
    print_report(f"{label} (relevance-filtered, excess vs {benchmark})", results)


def main():
    parser = argparse.ArgumentParser(description="Re-evaluate sentiment on direct-mention-only articles")
    parser.add_argument("--min-articles", type=int, default=1)
    parser.add_argument("--min-names", type=int, default=5)
    parser.add_argument("--benchmark", default="SPY")
    args = parser.parse_args()

    print("[relevance_filter_eval] loading news_sentiment + finnhub_news...")
    sent = q.load("news_sentiment")
    news = q.load("finnhub_news")[["headline", "summary"]].drop_duplicates("headline")
    sent = sent.merge(news, on="headline", how="left")

    aliases = rel.load_company_aliases()
    print(f"  {len(sent):,} scored articles; checking direct mentions...")
    sent["_direct"] = direct_mention_mask(sent, aliases)
    rate = sent["_direct"].mean()
    print(f"  {sent['_direct'].sum():,} / {len(sent):,} ({rate:.1%}) directly mention their tagged symbol")

    filtered = sent[sent["_direct"]].copy()

    # VADER, filtered
    vader_sig = vader_daily_signals(filtered, min_articles=args.min_articles)
    run_eval(vader_sig, "VADER", args.benchmark, args.min_names)

    # FinBERT, filtered (join the finbert cache onto the same direct-mention set)
    if os.path.exists(FINBERT_CACHE):
        fb = pd.read_parquet(FINBERT_CACHE)
        fb_filtered = fb.merge(filtered[["headline"]].drop_duplicates(), on="headline", how="inner")
        fb_sig = daily_signals_from_cache(fb_filtered, min_articles=args.min_articles)
        run_eval(fb_sig, "FINBERT", args.benchmark, args.min_names)
    else:
        print("\n[FINBERT] no cache found, skipping.")


if __name__ == "__main__":
    main()
