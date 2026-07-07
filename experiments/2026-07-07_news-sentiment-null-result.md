# News sentiment vs. forward returns: a null result

**Repo:** financial-data-pipeline · **Date:** 2026-07-07 · **Status:** concluded (negative)

## Claim

A finance-tuned VADER lexicon scoring ~156K Dow-30 news articles shows no predictive
power for 1–21 trading-day excess returns (pooled IC ≈ 0.01, no horizon's daily
cross-sectional IC t-stat exceeds 1). Swapping in a finance-tuned transformer
(FinBERT) does not improve on this. Restricting to articles that actually name the
company they're tagged to (only ~40% do) nudges the sign of longer-horizon spreads
in the right direction but still clears no significance bar. The signal is
unproven at this sample depth — this justified trying a stronger model, but not
further lexicon tuning.

## Motivation

The repo runs a cross-sectional factor panel (`analytics/signals.py`) combining
momentum, value, quality, short-interest, insider flow, and news sentiment. The
`sentiment` factor went live 2026-07-06 (VADER + a finance lexicon, replacing an
earlier design that required a paid LLM API key) but had never been measured
against actual forward returns — it was live because it *ran*, not because it
*worked*. A positive result would validate keeping it in the composite signal and
justify further investment (a real-time news-sentiment trading signal); a null
result should stop further tuning effort from being sunk into it.

## Data

- **Source:** Finnhub company-news endpoint, Dow-30 watchlist, backfilled to
  ~365 days via `finnhub_pipeline.py --news-days 365` (5-day-chunked deep pull).
- **Universe:** 31 symbols (Dow-30 + one; today's constituents — no survivorship
  adjustment, so results are mildly optimistic relative to a point-in-time index).
- **Date range:** 2025-07-12 → 2026-07-06 (~1 year).
- **Volume at each step:**
  - 157,179 total articles fetched.
  - 156,114 scored by VADER (154,879 newly scored this session; some pre-existed
    from an earlier 4-day sample).
  - 9,958 (symbol, day) aggregated signals after confidence-weighted averaging.
  - 9,554–9,583 signals matched to price data per horizon (some drop near the
    series' recent edge where forward returns aren't yet observable).
  - Direct-mention filter (see Method) retains only ~40% of articles → 8,682
    (VADER) / 8,816 (FinBERT) symbol-day signals. **Note:** these two counts
    aren't identically matched (~4% apart) — both pipelines dedup on headline
    text independently, so a duplicate headline can be dropped under a different
    symbol between the two caches. The gap is small and didn't change the
    qualitative conclusion, but the FinBERT-vs-VADER comparison below is
    approximately, not exactly, on the same signal set.
- **Known gap:** no article-level ground truth for "is this actually about the
  company" beyond the relevance heuristic built for this experiment — a
  human-labeled sample was not created.

## Method

1. **Scoring.** Two scorers, same articles:
   - **VADER** (`news_sentiment_pipeline.py`): `vaderSentiment` + a ~70-term
     finance lexicon (beat/miss/upgrade/downgrade/etc., -4..+4 valence).
     Headline weighted 0.7, summary 0.3. Compound score in [-1, 1].
   - **FinBERT** (`finbert_eval.py`, this experiment): `ProsusAI/finbert`
     (transformers 4.56.2 + torch 2.8.0-cpu, already installed — no new deps).
     Headline only, max 64 tokens. Score = P(positive) − P(negative), in [-1, 1].
     Scoring 111,426 not-yet-cached articles took 2,377s (~40 min) on CPU.
2. **Aggregation.** Confidence-weighted mean score per (symbol, day); FinBERT
   confidence = max class probability, VADER confidence = a heuristic blend of
   (1 − neutral proportion) and |compound| (see `score_article()`).
3. **Relevance filter** (`analytics/relevance.py`, `relevance_filter_eval.py`,
   this experiment). Direct-mention check: does the company's ticker (in
   unambiguous forms — `$XOM`, `"NYSE: XOM"`, never bare letters) or name alias
   (from `finnhub_profile` + hand-written short forms) appear in the headline or
   summary? A random 20-article sample first suggested ~20% direct-mention rate;
   the full-corpus check found 40.0% (62,402 / 156,114). Articles failing this
   check are generic market wraps or off-topic wire stories that happened to be
   returned under a company's news feed.
4. **Point-in-time safeguards** (`sentiment_eval.py`, pre-existing, reused
   unchanged): entry at the close of the first trading day *after* the article
   date (never same-day); forward returns at 1/3/5/10/21 trading days, excess
   vs. SPY; benchmark return matched to the same entry/exit dates as the signal.
5. **Statistics reported:** pooled Spearman IC (signal vs. forward return, all
   pairs), mean daily cross-sectional IC with a t-stat over ~325-354 trading
   days, and bullish-minus-bearish bucket spread (±0.10 compound score
   threshold) with a Welch t-test.

## Results

**VADER, full corpus** (9,958 signals; from `SENTIMENT_EVAL_RESULTS.txt`):

| h (days) | n | pooled IC | daily IC | t | spread% | t |
|---|---|---|---|---|---|---|
| 1 | 9,554 | 0.0086 | 0.010 | 0.88 | -0.059 | -0.52 |
| 3 | 9,497 | -0.0113 | -0.021 | -1.95 | -0.308 | -1.67 |
| 5 | 9,394 | -0.0054 | -0.011 | -0.92 | -0.194 | -0.89 |
| 10 | 9,179 | 0.0175 | 0.005 | 0.42 | 0.056 | 0.17 |
| 21 | 8,789 | 0.0045 | -0.015 | -1.40 | -0.002 | -0.01 |

**FinBERT, full corpus** (9,576 signals; from `FINBERT_EVAL_RESULTS.txt`):

| h (days) | n | pooled IC | daily IC | t | spread% | t |
|---|---|---|---|---|---|---|
| 1 | 9,241 | -0.0042 | -0.0065 | -0.58 | 0.025 | 0.52 |
| 3 | 9,185 | -0.0082 | -0.0037 | -0.31 | 0.022 | 0.25 |
| 5 | 9,085 | 0.0032 | 0.0054 | 0.45 | 0.15 | 1.29 |
| 10 | 8,880 | -0.0067 | -0.0033 | -0.27 | 0.093 | 0.57 |
| 21 | 8,499 | -0.0019 | -0.0062 | -0.51 | 0.152 | 0.64 |

**Direct-mention-only subset** (from `relevance_filter_eval.py`, VADER n=8,682
signals / FinBERT n=8,816 signals):

| h (days) | VADER daily IC (t) | VADER spread% (t) | FinBERT daily IC (t) | FinBERT spread% (t) |
|---|---|---|---|---|
| 1 | 0.0058 (0.46) | 0.016 (0.15) | 0.0015 (0.12) | -0.010 (-0.20) |
| 3 | -0.0077 (-0.64) | -0.063 (-0.39) | -0.0002 (-0.02) | -0.046 (-0.51) |
| 5 | -0.0025 (-0.20) | 0.104 (0.51) | 0.0058 (0.47) | 0.071 (0.60) |
| 10 | 0.0087 (0.71) | 0.268 (0.95) | -0.0056 (-0.47) | -0.024 (-0.14) |
| 21 | -0.0015 (-0.12) | 0.279 (0.69) | -0.0058 (-0.47) | -0.294 (-1.21) |

No cell in any table clears the |t| ≥ 2 significance bar. VADER's full-corpus 3d
horizon comes closest (t = -1.95) but with the wrong sign (higher sentiment →
lower returns) and no theoretical reason to expect a 3-day-specific effect,
consistent with noise rather than a real 3-day reversal.

## Limitations & threats to validity

- **Multiple comparisons.** 5 horizons × 3 configurations × 2 scorers ≈ 30
  reported statistics; a t ≈ 2 by chance somewhere in that set is expected. None
  reached that bar, which is itself informative, but a single marginal result
  in a smaller table should not be over-read.
- **Sample depth.** ~9,000-9,600 matched signals over 1 year and 31 symbols is
  thin for detecting a small factor. A longer history or a broader universe
  (beyond Dow-30) could change the picture — this wasn't tested.
- **Survivorship.** Universe is today's Dow-30, not the historical index
  membership at each date.
- **Coverage mismatch.** VADER and FinBERT signal counts differ by ~4% (see
  Data) due to independent headline-deduplication; the comparison is close but
  not exactly apples-to-apples.
- **Relevance heuristic is unvalidated.** The direct-mention filter was not
  checked against human-labeled ground truth; it's a reasonable proxy (ticker/
  name string match) but its own false-negative rate (missing indirect or
  pronoun-referenced mentions) is unmeasured.
- **Single benchmark.** Excess returns are vs. SPY only; sector-relative excess
  returns weren't tried and might behave differently for sector-driven news.

## Decision & next step

Stop tuning the sentiment scorer (lexicon or model). Keep the `sentiment` factor
live in `signal_panel()` since it doesn't hurt the composite, but treat it as
unproven, not validated, until either more history accumulates or a
fundamentally different specification (e.g. surprise-relative scoring, or
restricting to earnings/guidance-tagged articles specifically) is tried. Move
effort to the event-impact module — generalizing `event_backtest.py` to
industry/company-level responses to macro shocks (e.g. oil price shocks →
per-sector historical reaction) — which has a clearer causal story and doesn't
depend on noisy off-the-shelf sentiment scoring.

## Reproduce

```
# from repo root, C:\ProgramData\anaconda3\python.exe on all commands
python news_sentiment_pipeline.py --backfill        # VADER scores (writes news_sentiment table)
python curated.py --table news_sentiment            # rebuild curated snapshot
python sentiment_eval.py                            # VADER baseline -> SENTIMENT_EVAL_RESULTS.txt
python finbert_eval.py                              # FinBERT scoring + eval -> FINBERT_EVAL_RESULTS.txt
python relevance_filter_eval.py                     # direct-mention-filtered re-eval, both scorers
```

Requires `storage/raw/finnhub/news/**` populated (`finnhub_pipeline.py --news-days 365`)
and `finnhub_profile` populated for alias resolution.
