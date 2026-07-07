# Session Notes — 2026-07-07 (continuation of 2026-07-06 session 2)

**Branch:** master (uncommitted work from 07-06 finished and committed this session)
**Session model:** Claude Sonnet 5

**Full writeup:** `experiments/2026-07-07_news-sentiment-null-result.md` — this section
is a summary; the experiment file has the complete method/results/limitations.

## What happened

Picked up the sentiment-evaluation groundwork from 07-06 session 2. The overnight
365-day news backfill had finished (157,179 total articles, 154,879 newly scored by
VADER) and the automated finish-chain had already run `sentiment_eval.py`, producing
`SENTIMENT_EVAL_RESULTS.txt` — the VADER baseline.

### VADER baseline (full corpus, unfiltered)
9,958 symbol-day signals, 31 symbols, 2025-07-12 → 2026-07-06. No horizon (1/3/5/10/21d)
clears the |t|>2 significance bar; spreads mostly negative (bearish articles slightly
outperforming bullish — wrong sign).

### FinBERT attempt (`finbert_eval.py`, new)
Scored the full corpus with `ProsusAI/finbert` (torch 2.8.0-cpu + transformers 4.56.2,
already installed — no new deps). Took 2,377s (~40 min) for 111,426 not-yet-cached
articles, cached to `storage/finbert_cache.parquet`. **Result: no better than VADER.**
No horizon significant; the only improvement is directionally-correct (positive) spreads
at every horizon, but none exceed noise. Full results in `FINBERT_EVAL_RESULTS.txt`.

### Relevance filter check (`relevance_filter_eval.py`, new) — the actual finding
Sampled `finnhub_news` and found only ~20-40% of articles actually mention the symbol
they're tagged with in the headline/summary (most are generic market wraps or off-topic
wire stories tagged to whichever company's news feed happened to return them). Filtered
both VADER and FinBERT scores to direct-mention-only articles (via
`analytics/relevance.py`'s alias matching) and re-ran the eval:

| | Full corpus | Direct-mention only (~40%) |
|---|---|---|
| VADER 10d dailyIC (t) | 0.005 (0.42) | 0.009 (0.71) |
| VADER 10d spread% (t) | +0.06 (0.17) | +0.27 (0.95) |
| VADER 21d spread% (t) | -0.00 (-0.01) | +0.28 (0.69) |
| FinBERT | no significant horizon | no significant horizon |

Relevance filtering nudges VADER's longer-horizon spreads in the right direction but
**nothing clears significance**. Per `signal-eval` skill guidance: sign flips across
horizons (VADER full corpus: +1d/-3d/-5d/+10d/-21d) mean noise, not momentum-then-
reversal, and a near-zero IC is a legitimate reportable result, not a failed eval.

**Caveat on rigor:** the VADER (9,958) and FinBERT (9,576) full-corpus signal counts
aren't perfectly identical (~4% different) because both pipelines' headline-based
dedup can drop a duplicate headline under a second symbol before the other scorer's
cache sees it. Didn't re-run to force identical sets given the gap is small and the
qualitative conclusion (no significant signal either way) is unlikely to flip — noting
this rather than treating the comparison as perfectly matched.

### Conclusion
Generic news-headline sentiment (VADER or FinBERT, with or without relevance filtering)
does not show a statistically significant predictive signal on this dataset (1 year of
news, ~30-symbol watchlist, ~8-10K matched symbol-days). This is a genuine negative
result, not a call for more lexicon tuning — noise dominates at this sample depth and
neither scorer's errors look like the bottleneck. `sentiment` factor stays in
`signal_panel()` (it's live and doesn't hurt), but treat it as unproven, not validated.

### Decision (Zander, 2026-07-07)
Stop tuning sentiment scoring further. Move effort to the event-impact module
(generalizing `event_backtest.py` to industry/company-level response to macro shocks —
e.g. oil shock → per-sector historical reaction), the 4th item in the 07-06 session-2
roadmap (eval harness → FinBERT → relevancy map → event impact). Not started yet —
needs its own scoping conversation before implementation.

## Files added/changed this session
- `finbert_eval.py`, `relevance_filter_eval.py` (new, experiment scripts — eval-only,
  no CATALOG/curated/run_all wiring since neither script's output is a production table)
- `FINBERT_EVAL_RESULTS.txt` (new)
- Committed the rest of 07-06 session 2's uncommitted work: `sentiment_eval.py`,
  `analytics/exposure.py`, `analytics/relevance.py`, `finnhub_pipeline.py --news-days`,
  `EXPERT_BRIEF.md`, `AUTOMATION.md`, `tests/test_exposure.py`,
  `tests/test_sentiment_eval.py`, `SENTIMENT_EVAL_RESULTS.txt`, `scripts/`.
- Full test suite: 256 passed before commit.
