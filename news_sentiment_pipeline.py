#!/usr/bin/env python3
"""
News Sentiment Pipeline (Claude API):
  Scores financial news headlines + summaries already stored in the
  finnhub_news table using Claude. Runs incrementally — only articles
  not yet scored are processed.

  Uses claude-haiku-4-5 for cost efficiency (bulk classification task).
  Articles are batched 20 per API call to minimise request count.

  Requires:
    pip install anthropic
    ANTHROPIC_API_KEY set in .env

CLI:
  python news_sentiment_pipeline.py              # last 3 days of news
  python news_sentiment_pipeline.py --days 14    # last N days
  python news_sentiment_pipeline.py --backfill   # all available news

Output:
  storage/raw/finnhub/news_sentiment/news_sentiment_{mode}_{YYYYMMDD}.parquet

Schema:
  symbol | article_id | headline | sentiment | score | confidence |
  key_topics | date | source | fetched_at

  sentiment : "bullish" | "bearish" | "neutral"
  score     : float -1.0 (very bearish) to +1.0 (very bullish)
  confidence: float 0.0–1.0
  key_topics: comma-separated topic tags (e.g. "earnings,guidance,beat")
"""

import os
import json
import datetime
import argparse
import time
import glob as _glob_mod
import pandas as pd
import anthropic
from dotenv import load_dotenv
from storage_utils import write_partitioned

load_dotenv()

OUTPUT_DIR = os.path.join("storage", "raw", "finnhub", "news_sentiment")
MODEL      = "claude-haiku-4-5-20251001"
BATCH_SIZE = 20       # articles per API call
MAX_RETRIES = 3
BACKOFF_SECONDS = 30

SYSTEM_PROMPT = """\
You are a financial news analyst. Given a list of news articles (headline + summary),
classify each as bullish, bearish, or neutral for the associated stock.

Return a JSON array with one object per article, in the same order as the input.
Each object must have exactly these fields:
  "id"         : the article id from the input (integer)
  "sentiment"  : "bullish", "bearish", or "neutral"
  "score"      : float from -1.0 (very bearish) to +1.0 (very bullish), 0.0 = neutral
  "confidence" : float from 0.0 to 1.0 (how confident you are)
  "key_topics" : comma-separated lowercase tags, max 5 (e.g. "earnings,beat,guidance")

Return ONLY the JSON array, no explanation or markdown fencing.
"""

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set in environment / .env")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def _score_batch(batch: list[dict]) -> list[dict] | None:
    """
    Send a batch of articles to Claude and return parsed results.

    Each item in batch: {"id": int, "symbol": str, "headline": str, "summary": str}
    """
    lines = []
    for item in batch:
        summary = (item.get("summary") or "")[:300]  # cap summary length
        lines.append(
            f'{{"id": {item["id"]}, "symbol": "{item["symbol"]}", '
            f'"headline": "{item["headline"]}", "summary": "{summary}"}}'
        )
    user_content = "Articles to score:\n" + "\n".join(lines)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            msg = _get_client().messages.create(
                model=MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            raw = msg.content[0].text.strip()
            # Strip optional markdown fencing
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
            return json.loads(raw)

        except json.JSONDecodeError as e:
            print(f"  JSON parse error (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)
        except anthropic.RateLimitError:
            wait = BACKOFF_SECONDS * attempt
            print(f"  Rate limit hit. Backing off {wait}s (attempt {attempt}/{MAX_RETRIES}).")
            time.sleep(wait)
        except Exception as e:
            print(f"  API error (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)

    return None


def load_news(days: int | None = None) -> pd.DataFrame:
    """Load finnhub news from parquet files directly (no DuckDB dependency)."""
    news_dir = os.path.join("storage", "raw", "finnhub", "news")
    if not os.path.exists(news_dir):
        return pd.DataFrame()

    files = [
        os.path.join(news_dir, f)
        for f in os.listdir(news_dir)
        if f.endswith(".parquet")
    ]
    if not files:
        return pd.DataFrame()

    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)

    if days is not None:
        cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        if "datetime" in df.columns:
            df["_date_str"] = pd.to_datetime(df["datetime"], unit="s", errors="coerce").dt.strftime("%Y-%m-%d")
            df = df[df["_date_str"] >= cutoff].drop(columns=["_date_str"])

    return df


def load_already_scored() -> set[str]:
    """Return set of headline strings already scored (dedup key)."""
    scored = set()
    for path in _glob_mod.glob(os.path.join(OUTPUT_DIR, "**", "*.parquet"), recursive=True):
        try:
            df = pd.read_parquet(path, columns=["headline"])
            scored.update(df["headline"].tolist())
        except Exception:
            pass
    return scored


def main():
    parser = argparse.ArgumentParser(description="Score news sentiment with Claude")
    parser.add_argument("--days", type=int, default=3,
                        help="How many days of recent news to process (default: 3)")
    parser.add_argument("--backfill", action="store_true",
                        help="Process all available news regardless of date")
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    days = None if args.backfill else args.days
    mode = "backfill" if args.backfill else "incremental"
    print(f"[news_sentiment] {mode}: loading news...")

    raw_df = load_news(days=days)
    if raw_df.empty:
        print("  No news data found. Run finnhub_pipeline.py first.")
        return

    already_scored = load_already_scored()
    df = raw_df[~raw_df["headline"].isin(already_scored)].copy()
    print(f"  {len(raw_df):,} total articles, {len(df):,} not yet scored")

    if df.empty:
        print("  Nothing new to score.")
        return

    # Build article list: need symbol, headline, summary, date
    df = df.dropna(subset=["headline"]).reset_index(drop=True)
    df["_article_id"] = range(len(df))

    # Derive date from unix timestamp if present
    if "datetime" in df.columns:
        df["date"] = pd.to_datetime(df["datetime"], unit="s", errors="coerce").dt.strftime("%Y-%m-%d")
    elif "date" not in df.columns:
        df["date"] = datetime.date.today().isoformat()

    articles = df[["_article_id", "symbol", "headline", "summary"]].to_dict("records")
    articles = [
        {"id": r["_article_id"], "symbol": r.get("symbol", ""),
         "headline": str(r["headline"]), "summary": str(r.get("summary", "") or "")}
        for r in articles
    ]

    # Process in batches
    results: list[dict] = []
    total_batches = (len(articles) + BATCH_SIZE - 1) // BATCH_SIZE
    for i in range(0, len(articles), BATCH_SIZE):
        batch = articles[i : i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        print(f"  Batch {batch_num}/{total_batches} ({len(batch)} articles)...")
        scored = _score_batch(batch)
        if scored:
            results.extend(scored)
        else:
            print(f"    Warning: batch {batch_num} returned no results.")

    if not results:
        print("  No articles scored successfully.")
        return

    # Merge scores back to original rows
    scores_df = pd.DataFrame(results).rename(columns={"id": "_article_id"})
    out_df = df.merge(scores_df, on="_article_id", how="inner")

    keep_cols = [c for c in [
        "symbol", "_article_id", "headline", "date", "source",
        "sentiment", "score", "confidence", "key_topics",
    ] if c in out_df.columns]
    out_df = out_df[keep_cols].rename(columns={"_article_id": "article_id"})
    out_df["fetched_at"] = datetime.datetime.utcnow().isoformat()

    today_str = datetime.datetime.utcnow().strftime("%Y%m%d")
    out_path  = write_partitioned(out_df, OUTPUT_DIR, f"news_sentiment_{mode}_{today_str}.parquet")

    print(f"\n--- COMPLETE ---")
    print(f"Scored {len(out_df):,} articles → {out_path}")

    dist = out_df["sentiment"].value_counts().to_dict()
    print(f"Sentiment distribution: {dist}")
    print(out_df[["symbol", "date", "headline", "sentiment", "score"]].head(10).to_string(index=False))


if __name__ == "__main__":
    main()
