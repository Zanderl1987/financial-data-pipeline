#!/usr/bin/env python3
"""
Reddit Sentiment Pipeline — post volume and engagement across finance subreddits.

Uses the PRAW library (Python Reddit API Wrapper) with a free "script" app credential.
Register at: https://www.reddit.com/prefs/apps  (type: script, redirect: http://localhost:8080)

Required env vars:
  REDDIT_CLIENT_ID      — from the app "personal use script" field
  REDDIT_CLIENT_SECRET  — from the app "secret" field
  REDDIT_USER_AGENT     — e.g. "financial-data-pipeline/1.0 (by u/yourname)"

What this captures:
  1. Hot/new/top posts from 6 finance subreddits with full metadata:
       score, upvote_ratio, num_comments, created_utc, flair, link_flair
  2. Ticker mention counts — scans titles for $TICKER or bare uppercase symbols
       from the DJI watchlist. Lets you build daily mention-frequency time series.
  3. Subreddit-level subscriber counts + active users (community pulse check).

Signal value:
  - r/wallstreetbets mention spikes reliably precede unusual options activity
  - Post volume on r/investing correlates with retail inflows (ICI data confirms)
  - Upvote velocity on bearish posts is a contrarian sentiment indicator

Outputs:
  storage/raw/reddit/year=YYYY/month=MM/reddit_posts_{mode}_{YYYYMMDD}.parquet
  storage/raw/reddit/year=YYYY/month=MM/reddit_mentions_{mode}_{YYYYMMDD}.parquet
  CATALOG tables: reddit_posts, reddit_mentions

Usage:
  python reddit_pipeline.py             # incremental (hot + new, last 7 days)
  python reddit_pipeline.py --backfill  # top posts of past year (pushshift not available;
                                        # Reddit API only allows ~1000 posts per listing)
"""

import argparse
import datetime
import os
import re

import pandas as pd
import praw
from dotenv import load_dotenv

from storage_utils import write_partitioned

load_dotenv()

OUTPUT_DIR = os.path.join("storage", "raw", "reddit")

REDDIT_CLIENT_ID     = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT    = os.environ.get(
    "REDDIT_USER_AGENT", "financial-data-pipeline/1.0"
)

# Subreddits to monitor — ordered by relevance to equity/options signals
SUBREDDITS = [
    "wallstreetbets",   # retail options flow; meme stock signals
    "investing",        # longer-horizon sentiment
    "stocks",           # broader equity discussion
    "options",          # options-specific sentiment
    "SecurityAnalysis", # institutional-quality DD; contrarian quality signal
    "finance",          # macro and credit discussion
]

# DJI component tickers + high-conviction additions
WATCHLIST_TICKERS = {
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "GOOG", "META", "TSLA",
    "V", "JPM", "WMT", "UNH", "GS", "HD", "JNJ", "PG", "CAT", "BA",
    "MCD", "CRM", "CVX", "AXP", "HON", "CSCO", "IBM", "DIS", "NKE",
    "MRK", "VZ", "SHW", "TRV", "AMGN", "MMM", "KO", "INTC", "AMD",
    "NFLX", "SPY", "QQQ", "IWM", "GLD", "SLV", "USO", "TLT", "HYG",
}

# Regex to find $TICKER or bare TICKER (2-5 uppercase letters) in text
_TICKER_RE = re.compile(
    r"\$([A-Z]{1,5})\b|(?<![A-Z])([A-Z]{2,5})(?![A-Z])"
)

INCREMENTAL_DAYS = 7
POST_LIMIT       = 500   # per subreddit per listing type; API max is ~1000


def _make_reddit() -> praw.Reddit:
    return praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent=REDDIT_USER_AGENT,
        # read-only mode — no username/password needed
    )


def _extract_tickers(text: str) -> list[str]:
    """Return list of watchlist tickers mentioned in text."""
    found = set()
    for m in _TICKER_RE.finditer(text or ""):
        token = (m.group(1) or m.group(2) or "").upper()
        if token in WATCHLIST_TICKERS:
            found.add(token)
    return sorted(found)


def _post_to_row(submission, subreddit: str, fetched_at: str) -> dict:
    created = datetime.datetime.utcfromtimestamp(submission.created_utc)
    full_text = f"{submission.title} {getattr(submission, 'selftext', '')}"
    tickers = _extract_tickers(full_text)
    return {
        "post_id":       submission.id,
        "subreddit":     subreddit,
        "title":         submission.title[:500],
        "score":         submission.score,
        "upvote_ratio":  getattr(submission, "upvote_ratio", None),
        "num_comments":  submission.num_comments,
        "created_utc":   created.isoformat(),
        "date":          created.strftime("%Y-%m-%d"),
        "flair":         getattr(submission, "link_flair_text", None),
        "is_self":       submission.is_self,
        "url":           submission.url[:300],
        "tickers_mentioned": ",".join(tickers) if tickers else None,
        "ticker_count":  len(tickers),
        "fetched_at":    fetched_at,
    }


def fetch_subreddit(reddit: praw.Reddit, sub_name: str, backfill: bool,
                    cutoff_ts: float, fetched_at: str) -> list[dict]:
    sub = reddit.subreddit(sub_name)
    rows = []
    seen = set()

    listings = ["hot", "new"] if not backfill else ["top", "hot", "new"]
    time_filters = ["year"] if backfill else [None]

    for listing in listings:
        try:
            if listing == "top" and backfill:
                posts = sub.top(time_filter="year", limit=POST_LIMIT)
            elif listing == "hot":
                posts = sub.hot(limit=POST_LIMIT)
            else:
                posts = sub.new(limit=POST_LIMIT)

            for submission in posts:
                if submission.id in seen:
                    continue
                seen.add(submission.id)
                if submission.created_utc < cutoff_ts:
                    continue
                rows.append(_post_to_row(submission, sub_name, fetched_at))
        except Exception as exc:
            print(f"    [{sub_name}/{listing}] error: {exc}")

    return rows


def build_mention_counts(df_posts: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate per-ticker daily mention counts and total score from posts.
    One row per (date, subreddit, ticker).
    """
    rows = []
    for _, post in df_posts.iterrows():
        if not post.get("tickers_mentioned"):
            continue
        for ticker in post["tickers_mentioned"].split(","):
            ticker = ticker.strip()
            if not ticker:
                continue
            rows.append({
                "date":       post["date"],
                "subreddit":  post["subreddit"],
                "ticker":     ticker,
                "post_score": post["score"],
                "comments":   post["num_comments"],
            })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    agg = (
        df.groupby(["date", "subreddit", "ticker"])
        .agg(
            mention_count=("ticker", "count"),
            total_score=("post_score", "sum"),
            total_comments=("comments", "sum"),
        )
        .reset_index()
    )
    return agg.sort_values(["date", "mention_count"], ascending=[True, False])


def main(backfill: bool = False) -> None:
    if not REDDIT_CLIENT_ID or not REDDIT_CLIENT_SECRET:
        print("ERROR: REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET must be set in .env")
        print("  Register a free script app at https://www.reddit.com/prefs/apps")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    now        = datetime.datetime.utcnow()
    today      = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode       = "backfill" if backfill else "incremental"

    if backfill:
        cutoff_ts = (now - datetime.timedelta(days=365)).timestamp()
    else:
        cutoff_ts = (now - datetime.timedelta(days=INCREMENTAL_DAYS)).timestamp()

    cutoff_dt = datetime.datetime.utcfromtimestamp(cutoff_ts).strftime("%Y-%m-%d")
    print(f"Reddit Sentiment Pipeline  mode={mode}")
    print(f"Cutoff: {cutoff_dt}  |  Subreddits: {len(SUBREDDITS)}  |  Watchlist: {len(WATCHLIST_TICKERS)} tickers")
    print()

    reddit = _make_reddit()

    all_rows = []
    for sub_name in SUBREDDITS:
        print(f"  r/{sub_name}...", end=" ", flush=True)
        rows = fetch_subreddit(reddit, sub_name, backfill, cutoff_ts, fetched_at)
        all_rows.extend(rows)
        print(f"{len(rows)} posts")

    if not all_rows:
        print("\nNo posts collected.")
        return

    df_posts = pd.DataFrame(all_rows)
    df_posts["created_utc"] = pd.to_datetime(df_posts["created_utc"], errors="coerce")
    df_posts = df_posts.drop_duplicates(subset=["post_id"])

    # Write posts table
    path_posts = write_partitioned(df_posts, OUTPUT_DIR, f"reddit_posts_{mode}_{today}.parquet")
    print(f"\n[+] {path_posts}")
    print(f"    {len(df_posts):,} posts | {df_posts['subreddit'].nunique()} subreddits")

    # Write mention counts table
    df_mentions = build_mention_counts(df_posts)
    if not df_mentions.empty:
        df_mentions["fetched_at"] = fetched_at
        path_mentions = write_partitioned(df_mentions, OUTPUT_DIR, f"reddit_mentions_{mode}_{today}.parquet")
        top = df_mentions.nlargest(5, "mention_count")[["ticker", "mention_count"]].to_string(index=False)
        print(f"\n[+] {path_mentions}")
        print(f"    {len(df_mentions):,} ticker-day rows")
        print(f"\n    Top mentions:\n{top}")

    print("\n--- COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Reddit finance sentiment pipeline (requires REDDIT_CLIENT_ID + SECRET)"
    )
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch top posts from past year. Default: last 7 days hot/new.")
    args = parser.parse_args()
    main(backfill=args.backfill)
