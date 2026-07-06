#!/usr/bin/env python3
"""
News Sentiment Pipeline (local VADER):
  Scores financial news headlines + summaries already stored in the
  finnhub_news table using VADER (vaderSentiment) with a finance-tuned
  lexicon. Fully offline — no API key, no cost, deterministic output.
  Runs incrementally — only articles not yet scored are processed.

  Requires:
    pip install vaderSentiment

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
  key_topics: comma-separated topic tags (e.g. "earnings,guidance,analyst")
"""

import os
import re
import datetime
import argparse
import glob as _glob_mod
import pandas as pd
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from storage_utils import write_partitioned

OUTPUT_DIR = os.path.join("storage", "raw", "finnhub", "news_sentiment")

# Classification thresholds on the VADER compound score
BULLISH_THRESHOLD = 0.10
BEARISH_THRESHOLD = -0.10

# Finance-specific lexicon overrides/additions (VADER valence scale: -4 .. +4).
# VADER's base lexicon is social-media English; financial headline vocabulary
# ("beat", "miss", "downgrade") is directional in ways it doesn't know.
FINANCE_LEXICON = {
    # bullish
    "beat": 2.0, "beats": 2.0, "exceeds": 2.0, "tops": 1.8, "outperform": 2.0,
    "outperforms": 2.0, "upgrade": 2.5, "upgraded": 2.5, "upgrades": 2.5,
    "overweight": 1.5, "surge": 2.5, "surges": 2.5, "soar": 3.0, "soars": 3.0,
    "rally": 2.0, "rallies": 2.0, "jumps": 2.0, "climbs": 1.5, "gains": 1.5,
    "bullish": 2.5, "buyback": 1.5, "dividend": 1.0, "profitable": 1.8,
    "breakout": 1.5, "raises": 1.5, "raised": 1.5, "record": 1.2,
    "beat-and-raise": 3.0, "accretive": 1.5,
    # bearish
    "miss": -2.0, "misses": -2.0, "missed": -2.0, "shortfall": -2.0,
    "downgrade": -2.5, "downgraded": -2.5, "downgrades": -2.5,
    "underperform": -2.0, "underweight": -1.5, "plunge": -3.0, "plunges": -3.0,
    "tumble": -2.5, "tumbles": -2.5, "slump": -2.2, "slumps": -2.2,
    "sinks": -2.2, "slides": -1.8, "drops": -1.8, "falls": -1.5,
    "bearish": -2.5, "selloff": -2.5, "sell-off": -2.5, "lawsuit": -2.0,
    "probe": -2.0, "investigation": -2.0, "subpoena": -2.5, "recall": -2.0,
    "bankruptcy": -3.5, "default": -2.5, "layoffs": -2.0, "warns": -2.0,
    "warning": -1.8, "fraud": -3.0, "halted": -2.0, "delisting": -3.0,
    "dilution": -1.8, "dilutive": -1.8, "writedown": -2.0, "impairment": -2.0,
    "cuts": -1.5, "slashes": -2.2, "weak": -1.5, "weakness": -1.5,
}

# Topic tagging: tag -> regex matched against headline+summary (case-insensitive)
TOPIC_PATTERNS = {
    "earnings":    r"\bearnings?\b|\beps\b|\bquarterly results?\b|\bq[1-4]\b",
    "guidance":    r"\bguidance\b|\boutlook\b|\bforecasts?\b",
    "analyst":     r"\banalysts?\b|\bupgrade[ds]?\b|\bdowngrade[ds]?\b|\bprice target\b|\brating\b",
    "merger":      r"\bmergers?\b|\bacquisitions?\b|\bacquires?\b|\btakeovers?\b|\bbuyout\b|\bdeal\b",
    "dividend":    r"\bdividends?\b|\bpayout\b",
    "buyback":     r"\bbuybacks?\b|\brepurchase\b",
    "legal":       r"\blawsuits?\b|\bsettlement\b|\bsues?\b|\blitigation\b|\bprobe\b|\binvestigation\b",
    "regulation":  r"\bsec\b|\bftc\b|\bdoj\b|\bregulat|\bantitrust\b|\btariffs?\b",
    "insider":     r"\binsiders?\b|\bceo\b|\bcfo\b|\bexecutives?\b|\bresigns?\b|\bappoints?\b",
    "product":     r"\blaunch(es)?\b|\bunveils?\b|\bproducts?\b|\bpatents?\b",
    "contract":    r"\bcontracts?\b|\bpartnership\b|\bcollaborat|\bagreements?\b",
    "macro":       r"\bfed\b|\binflation\b|\brates?\b|\brecession\b|\bgdp\b",
    "debt":        r"\bdebt\b|\bbonds?\b|\bnotes offering\b|\brefinanc",
    "ai":          r"\bai\b|\bartificial intelligence\b|\bmachine learning\b",
    "short":       r"\bshort sellers?\b|\bshort interest\b|\bsqueeze\b",
    "bankruptcy":  r"\bbankruptcy\b|\bchapter 11\b|\bdefault\b|\bdelisting\b",
    "workforce":   r"\blayoffs?\b|\bjob cuts\b|\bhiring\b|\brestructuring\b",
}
_TOPIC_RE = {tag: re.compile(pat, re.IGNORECASE) for tag, pat in TOPIC_PATTERNS.items()}
MAX_TOPICS = 5

_analyzer: SentimentIntensityAnalyzer | None = None


def _get_analyzer() -> SentimentIntensityAnalyzer:
    global _analyzer
    if _analyzer is None:
        _analyzer = SentimentIntensityAnalyzer()
        _analyzer.lexicon.update(FINANCE_LEXICON)
    return _analyzer


def score_article(headline: str, summary: str = "") -> dict:
    """
    Score one article locally. Returns:
      {"sentiment": str, "score": float, "confidence": float, "key_topics": str}

    Headline carries the signal in financial news; the summary often repeats
    boilerplate, so headline gets 70% weight when both are scored.
    """
    analyzer = _get_analyzer()
    text = f"{headline}. {(summary or '')[:300]}".strip()

    h = analyzer.polarity_scores(headline)
    if summary:
        s = analyzer.polarity_scores(summary[:300])
        compound = 0.7 * h["compound"] + 0.3 * s["compound"]
        neu = 0.7 * h["neu"] + 0.3 * s["neu"]
    else:
        compound, neu = h["compound"], h["neu"]

    if compound >= BULLISH_THRESHOLD:
        sentiment = "bullish"
    elif compound <= BEARISH_THRESHOLD:
        sentiment = "bearish"
    else:
        sentiment = "neutral"

    # Confidence: for directional calls, how much of the text was sentiment-
    # bearing (1 - neutral proportion) scaled by signal strength; for neutral,
    # how uniformly neutral the text was.
    if sentiment == "neutral":
        confidence = round(neu, 3)
    else:
        confidence = round(min(1.0, (1.0 - neu) * 0.5 + abs(compound) * 0.5), 3)

    topics = [tag for tag, rx in _TOPIC_RE.items() if rx.search(text)][:MAX_TOPICS]

    return {
        "sentiment": sentiment,
        "score": round(compound, 4),
        "confidence": confidence,
        "key_topics": ",".join(topics),
    }


def load_news(days: int | None = None) -> pd.DataFrame:
    """Load finnhub news from parquet files directly (no DuckDB dependency)."""
    news_dir = os.path.join("storage", "raw", "finnhub", "news")
    if not os.path.exists(news_dir):
        return pd.DataFrame()

    # News is Hive-partitioned (year=YYYY/month=MM/) — search recursively
    files = _glob_mod.glob(os.path.join(news_dir, "**", "*.parquet"), recursive=True)
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
    parser = argparse.ArgumentParser(description="Score news sentiment locally with VADER")
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

    df = df.dropna(subset=["headline"]).reset_index(drop=True)
    df["_article_id"] = range(len(df))

    # Derive date from unix timestamp if present
    if "datetime" in df.columns:
        df["date"] = pd.to_datetime(df["datetime"], unit="s", errors="coerce").dt.strftime("%Y-%m-%d")
    elif "date" not in df.columns:
        df["date"] = datetime.date.today().isoformat()

    print(f"  Scoring {len(df):,} articles locally (VADER + finance lexicon)...")
    scores = [
        score_article(str(row.headline), str(getattr(row, "summary", "") or ""))
        for row in df.itertuples(index=False)
    ]
    scores_df = pd.DataFrame(scores)
    out_df = pd.concat([df.reset_index(drop=True), scores_df], axis=1)

    keep_cols = [c for c in [
        "symbol", "_article_id", "headline", "date", "source",
        "sentiment", "score", "confidence", "key_topics",
    ] if c in out_df.columns]
    out_df = out_df[keep_cols].rename(columns={"_article_id": "article_id"})
    out_df["fetched_at"] = datetime.datetime.utcnow().isoformat()

    today_str = datetime.datetime.utcnow().strftime("%Y%m%d")
    out_path  = write_partitioned(out_df, OUTPUT_DIR, f"news_sentiment_{mode}_{today_str}.parquet")

    print(f"\n--- COMPLETE ---")
    print(f"Scored {len(out_df):,} articles -> {out_path}")

    dist = out_df["sentiment"].value_counts().to_dict()
    print(f"Sentiment distribution: {dist}")
    print(out_df[["symbol", "date", "headline", "sentiment", "score"]].head(10).to_string(index=False))


if __name__ == "__main__":
    main()
