#!/usr/bin/env python3
"""
Fed Sentiment Pipeline (federalreserve.gov RSS + Claude API):
  Pulls FOMC statements and Fed official speeches from federalreserve.gov RSS
  feeds (no API key), scrapes the full text of each new item, and scores
  hawkish/dovish tone with Claude (same claude-haiku-4-5 approach as
  news_sentiment_pipeline.py).

  Incremental — only items not yet seen (by link) are fetched/scored.

  Requires:
    pip install anthropic beautifulsoup4 lxml
    ANTHROPIC_API_KEY set in .env

CLI:
  python fed_sentiment_pipeline.py              # process all new RSS items
  python fed_sentiment_pipeline.py --backfill   # same — RSS feeds only carry
                                                 # the most recent ~15 items each;
                                                 # there is no deeper history
                                                 # available without key-based
                                                 # access to FRASER archives.

Output:
  storage/raw/fed/speeches/fed_speeches_{mode}_{YYYYMMDD}.parquet
  storage/raw/fed/sentiment/fed_sentiment_{mode}_{YYYYMMDD}.parquet

Schema (fed_speeches):
  doc_id | doc_type | speaker | title | link | date | text | fetched_at

  doc_type : "speech" | "statement"
  speaker  : parsed from title for speeches (e.g. "Waller"); "" for statements

Schema (fed_sentiment):
  doc_id | doc_type | speaker | title | date | hawkish_score | stance |
  confidence | key_topics | fetched_at

  hawkish_score : float -1.0 (very dovish) to +1.0 (very hawkish), 0 = neutral
  stance        : "hawkish" | "dovish" | "neutral"
  confidence    : float 0.0-1.0
  key_topics    : comma-separated lowercase tags (e.g. "rate-hold,inflation,labor-market")
"""

import argparse
import datetime
import glob as _glob_mod
import hashlib
import json
import os
import re
import time
import xml.etree.ElementTree as ET

import anthropic
import pandas as pd
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from storage_utils import write_partitioned

load_dotenv()

SPEECHES_DIR  = os.path.join("storage", "raw", "fed", "speeches")
SENTIMENT_DIR = os.path.join("storage", "raw", "fed", "sentiment")

MODEL       = "claude-haiku-4-5-20251001"
BATCH_SIZE  = 5          # full-text documents are long — smaller batches than headline scoring
MAX_RETRIES = 3
BACKOFF_SECONDS = 30
REQUEST_INTERVAL = 0.5
TEXT_CHAR_CAP = 6000      # cap per-document text sent to Claude

RSS_FEEDS = {
    "speech":    "https://www.federalreserve.gov/feeds/speeches.xml",
    "statement": "https://www.federalreserve.gov/feeds/press_monetary.xml",
}

HEADERS = {"User-Agent": "Mozilla/5.0 financial-data-pipeline/1.0"}

SYSTEM_PROMPT = """\
You are a monetary policy analyst. Given a list of Federal Reserve documents
(speeches, FOMC statements, testimony), classify the monetary-policy stance
each one signals: hawkish (favors tighter policy / higher rates / inflation
concern) or dovish (favors looser policy / lower rates / growth concern).

Return a JSON array with one object per document, in the same order as the
input. Each object must have exactly these fields:
  "id"            : the document id from the input (integer)
  "stance"        : "hawkish", "dovish", or "neutral"
  "hawkish_score" : float from -1.0 (very dovish) to +1.0 (very hawkish), 0.0 = neutral
  "confidence"    : float from 0.0 to 1.0 (how confident you are)
  "key_topics"    : comma-separated lowercase tags, max 5 (e.g. "rate-hold,inflation,labor-market")

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


# ---------------------------------------------------------------------------
# RSS + page fetch
# ---------------------------------------------------------------------------

def fetch_feed_items(doc_type: str, url: str) -> list[dict]:
    """Parse an RSS feed's <item> entries into dicts with title/link/date."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            print(f"  HTTP {r.status_code} for {url}")
            return []
        root = ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError) as e:
        print(f"  Error fetching/parsing {url}: {e}")
        return []

    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        if not link:
            continue
        try:
            date = pd.to_datetime(pub_date).strftime("%Y-%m-%d") if pub_date else None
        except (ValueError, TypeError):
            date = None
        speaker = ""
        if doc_type == "speech" and "," in title:
            speaker = title.split(",", 1)[0].strip()
        items.append({
            "doc_id": hashlib.sha1(link.encode()).hexdigest()[:16],
            "doc_type": doc_type,
            "speaker": speaker,
            "title": title,
            "link": link,
            "date": date,
        })
    return items


def fetch_page_text(url: str) -> str:
    """Fetch and strip HTML from a federalreserve.gov article page."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.content, "lxml")
        article = soup.select_one("#article") or soup.find("main")
        if not article:
            return ""
        text = article.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text)
        return text[:TEXT_CHAR_CAP]
    except requests.RequestException as e:
        print(f"    Error fetching page {url}: {e}")
        return ""


# ---------------------------------------------------------------------------
# Claude scoring
# ---------------------------------------------------------------------------

def _score_batch(batch: list[dict]) -> list[dict] | None:
    """batch item: {"id": int, "title": str, "text": str}"""
    lines = []
    for item in batch:
        text = item["text"].replace('"', "'")
        lines.append(f'{{"id": {item["id"]}, "title": "{item["title"]}", "text": "{text}"}}')
    user_content = "Documents to score:\n" + "\n".join(lines)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            msg = _get_client().messages.create(
                model=MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            raw = msg.content[0].text.strip()
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


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

def load_already_seen() -> set[str]:
    seen = set()
    for path in _glob_mod.glob(os.path.join(SPEECHES_DIR, "**", "*.parquet"), recursive=True):
        try:
            df = pd.read_parquet(path, columns=["doc_id"])
            seen.update(df["doc_id"].tolist())
        except Exception:
            pass
    return seen


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Score Fed hawkish/dovish sentiment with Claude")
    parser.add_argument("--backfill", action="store_true",
                        help="Same as default — RSS feeds only expose recent history")
    args = parser.parse_args()

    os.makedirs(SPEECHES_DIR, exist_ok=True)
    os.makedirs(SENTIMENT_DIR, exist_ok=True)
    mode = "backfill" if args.backfill else "incremental"

    print("[fed_sentiment] Fetching RSS feeds...")
    all_items = []
    for doc_type, url in RSS_FEEDS.items():
        items = fetch_feed_items(doc_type, url)
        print(f"  {doc_type}: {len(items)} items")
        all_items.extend(items)
        time.sleep(REQUEST_INTERVAL)

    if not all_items:
        print("  No RSS items found.")
        return

    already_seen = load_already_seen()
    new_items = [it for it in all_items if it["doc_id"] not in already_seen]
    print(f"  {len(all_items)} total items, {len(new_items)} not yet seen")

    if not new_items:
        print("  Nothing new to process.")
        return

    now = datetime.datetime.utcnow()
    speeches_rows = []
    for i, item in enumerate(new_items, 1):
        print(f"  [{i}/{len(new_items)}] Fetching text: {item['title'][:60]}...")
        text = fetch_page_text(item["link"])
        row = dict(item)
        row["text"] = text
        row["fetched_at"] = now.isoformat()
        speeches_rows.append(row)
        time.sleep(REQUEST_INTERVAL)

    speeches_df = pd.DataFrame(speeches_rows)
    speeches_df = speeches_df[speeches_df["text"].str.len() > 0].reset_index(drop=True)

    today_str = now.strftime("%Y%m%d")
    speeches_path = write_partitioned(speeches_df, SPEECHES_DIR, f"fed_speeches_{mode}_{today_str}.parquet")
    print(f"\nfed_speeches -> {speeches_path} ({len(speeches_df):,} rows)")

    if speeches_df.empty:
        print("\nNo document text extracted; skipping sentiment scoring.")
        return

    # Score with Claude
    speeches_df["_score_id"] = range(len(speeches_df))
    to_score = [
        {"id": r["_score_id"], "title": r["title"], "text": r["text"]}
        for r in speeches_df.to_dict("records")
    ]

    results: list[dict] = []
    total_batches = (len(to_score) + BATCH_SIZE - 1) // BATCH_SIZE
    for i in range(0, len(to_score), BATCH_SIZE):
        batch = to_score[i : i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        print(f"  Batch {batch_num}/{total_batches} ({len(batch)} documents)...")
        scored = _score_batch(batch)
        if scored:
            results.extend(scored)
        else:
            print(f"    Warning: batch {batch_num} returned no results.")

    if not results:
        print("  No documents scored successfully.")
        return

    scores_df = pd.DataFrame(results).rename(columns={"id": "_score_id"})
    sentiment_df = speeches_df.merge(scores_df, on="_score_id", how="inner")
    keep_cols = [c for c in [
        "doc_id", "doc_type", "speaker", "title", "date",
        "stance", "hawkish_score", "confidence", "key_topics",
    ] if c in sentiment_df.columns]
    sentiment_df = sentiment_df[keep_cols]
    sentiment_df["fetched_at"] = now.isoformat()

    sentiment_path = write_partitioned(sentiment_df, SENTIMENT_DIR, f"fed_sentiment_{mode}_{today_str}.parquet")
    print(f"fed_sentiment -> {sentiment_path} ({len(sentiment_df):,} rows)")

    print("\n--- COMPLETE ---")
    dist = sentiment_df["stance"].value_counts().to_dict()
    print(f"Stance distribution: {dist}")
    print(sentiment_df[["date", "speaker", "title", "stance", "hawkish_score"]].head(10).to_string(index=False))


if __name__ == "__main__":
    main()
