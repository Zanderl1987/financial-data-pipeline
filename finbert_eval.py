"""
FinBERT sentiment evaluation — does ProsusAI/finbert beat the VADER baseline?

Standalone experiment script: scores finnhub_news headlines with FinBERT
(transformers, CPU, already installed in the conda env), caches results to
storage/finbert_cache.parquet (skips already-scored headlines like the VADER
pipeline does), then reuses sentiment_eval's forward-return/eval machinery so
the report is directly comparable to SENTIMENT_EVAL_RESULTS.txt.

This is eval-only — it does not write to storage/raw or touch query.py's
CATALOG. If FinBERT wins, wire it into news_sentiment_pipeline.py properly.

Usage:
  python finbert_eval.py                  # score + eval full history
  python finbert_eval.py --min-articles 2
"""

import argparse
import datetime
import os
import sys

import numpy as np
import pandas as pd
import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from news_sentiment_pipeline import load_news
from sentiment_eval import forward_returns, evaluate, BULLISH_MIN, BEARISH_MAX

CACHE_PATH = os.path.join("storage", "finbert_cache.parquet")
MODEL_NAME = "ProsusAI/finbert"
BATCH_SIZE = 64

_tokenizer = None
_model = None


def _get_model():
    global _tokenizer, _model
    if _model is None:
        _tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
        _model = AutoModelForSequenceClassification.from_pretrained(MODEL_NAME)
        _model.eval()
    return _tokenizer, _model


def score_batch(texts: list) -> pd.DataFrame:
    """FinBERT id2label is {0: positive, 1: negative, 2: neutral}."""
    tok, model = _get_model()
    with torch.no_grad():
        inputs = tok(texts, return_tensors="pt", padding=True, truncation=True, max_length=64)
        probs = torch.softmax(model(**inputs).logits, dim=-1).numpy()
    score = probs[:, 0] - probs[:, 1]  # -1..+1, positive minus negative probability
    sentiment = np.where(score >= BULLISH_MIN, "bullish",
                 np.where(score <= BEARISH_MAX, "bearish", "neutral"))
    confidence = probs.max(axis=1)
    return pd.DataFrame({"score": score, "sentiment": sentiment, "confidence": confidence})


def score_all(df: pd.DataFrame) -> pd.DataFrame:
    cached = pd.read_parquet(CACHE_PATH) if os.path.exists(CACHE_PATH) else pd.DataFrame(columns=["headline"])
    already = set(cached["headline"]) if not cached.empty else set()
    todo = df[~df["headline"].isin(already)].drop_duplicates(subset=["headline"]).reset_index(drop=True)
    print(f"  {len(df):,} total articles, {len(todo):,} not yet scored by FinBERT")
    if todo.empty:
        return cached

    new_rows = []
    for i in range(0, len(todo), BATCH_SIZE):
        batch = todo.iloc[i:i + BATCH_SIZE]
        scored = score_batch(batch["headline"].astype(str).tolist())
        scored.index = batch.index
        new_rows.append(pd.concat(
            [batch[["headline", "symbol", "date"]].reset_index(drop=True),
             scored.reset_index(drop=True)], axis=1))
        done = i + len(batch)
        if (i // BATCH_SIZE) % 20 == 0:
            print(f"    {done:,}/{len(todo):,}")
    new_df = pd.concat(new_rows, ignore_index=True)
    out = pd.concat([cached, new_df], ignore_index=True) if not cached.empty else new_df
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    out.to_parquet(CACHE_PATH, compression="snappy")
    return out


def daily_signals_from_cache(cache: pd.DataFrame, min_articles: int = 1) -> pd.DataFrame:
    df = cache.dropna(subset=["symbol", "date", "score"]).copy()
    df["date"] = pd.to_datetime(df["date"])
    w = df["confidence"].clip(lower=0.05)
    agg = (df.assign(_ws=df["score"] * w, _w=w)
             .groupby(["symbol", "date"])
             .agg(_ws=("_ws", "sum"), _w=("_w", "sum"), n_articles=("score", "size"))
             .reset_index())
    agg["sent_score"] = agg["_ws"] / agg["_w"]
    agg = agg[agg["n_articles"] >= min_articles]
    return agg[["symbol", "date", "sent_score", "n_articles"]].sort_values(["date", "symbol"])


def print_report(title: str, results: dict):
    print(f"\n=== {title} ===")
    hdr = (f"{'h':>3} {'n':>6} {'pooledIC':>9} {'p':>7} {'dailyIC':>8} {'t':>6} "
           f"{'days':>5} {'%pos':>5} {'bull%':>7} {'bear%':>7} {'spread%':>8} {'t':>6}")
    print(hdr)
    for h, r in results.items():
        print(f"{h:>3} {r['n']:>6} {r.get('pooled_ic', float('nan')):>9} "
              f"{r.get('pooled_p', float('nan')):>7} "
              f"{str(r.get('mean_daily_ic', '-')):>8} {str(r.get('ic_t_stat', '-')):>6} "
              f"{str(r.get('ic_days', '-')):>5} {str(r.get('ic_pct_positive', '-')):>5} "
              f"{str(r.get('bull_mean_pct', '-')):>7} {str(r.get('bear_mean_pct', '-')):>7} "
              f"{str(r.get('spread_pct', '-')):>8} {str(r.get('spread_t', '-')):>6}")


def main():
    parser = argparse.ArgumentParser(description="Evaluate FinBERT sentiment vs forward returns")
    parser.add_argument("--min-articles", type=int, default=1)
    parser.add_argument("--min-names", type=int, default=5)
    parser.add_argument("--benchmark", default="SPY")
    args = parser.parse_args()

    print("[finbert_eval] loading news...")
    raw = load_news(days=None).dropna(subset=["headline"]).copy()
    raw["date"] = pd.to_datetime(raw["datetime"], unit="s", errors="coerce").dt.strftime("%Y-%m-%d")

    print(f"[finbert_eval] scoring with FinBERT ({MODEL_NAME})...")
    t0 = datetime.datetime.now()
    cache = score_all(raw)
    print(f"  scoring took {(datetime.datetime.now() - t0).total_seconds():.0f}s")

    sig = daily_signals_from_cache(cache, min_articles=args.min_articles)
    print(f"  {len(sig):,} symbol-day signals | {sig['symbol'].nunique()} symbols | "
          f"{sig['date'].min().date()} -> {sig['date'].max().date()}")

    bench = args.benchmark or None
    print(f"[finbert_eval] computing forward returns (benchmark: {bench or 'none'})...")
    panel = forward_returns(sig, benchmark=bench)
    print(f"  {len(panel):,} signals matched to prices")

    results = evaluate(panel, min_names=args.min_names)
    print_report("FINBERT SENTIMENT PREDICTIVE POWER" + (f" (excess vs {bench})" if bench else ""), results)
    print("\nGuide: dailyIC > 0.02 with |t| > 2 = real signal; spread% should be")
    print("positive (bullish outperforms bearish) and grow with horizon.")
    print("\nCompare against VADER baseline in SENTIMENT_EVAL_RESULTS.txt.")


if __name__ == "__main__":
    main()
