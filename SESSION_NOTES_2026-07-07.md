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

---

# Session 2 (same day) — Post-restart triage + methodology skills

**Session model:** Claude Fable 5
**Scope:** global skill-building in `~/.claude/skills/` + cross-repo triage after the computer
restarted. No pipeline code changed. Ran in parallel with (and initially unaware of) the Sonnet
session above — its notes supersede anything this session said earlier about "uncommitted work."

## Built 4 global methodology skills (at Zander's request)

All in `~/.claude/skills/`, encoding this project's hard-won lessons so any model/session
applies them; memory index updated so they load proactively:

- **signal-eval** — PIT-safe evaluation methodology: publication-lag ASOF joins, next-close
  entry, excess-vs-benchmark, pooled + daily IC with t-stats, skepticism defaults (|IC|<0.02 is
  noise; sign flips across horizons = noise; suspiciously good = hunt the leak). The Sonnet
  session above cited it during the FinBERT eval — first real use, same day.
- **data-source-vetting** — 15-minute pre-build source spike: ToS veto first, WAF/dead-key
  probes, rate-limit semantics (per-key vs per-IP), backfill depth; GO/NO-GO always recorded.
- **experiment-writeup** — portfolio-ready `experiments/YYYY-MM-DD_<slug>.md` writeups (null
  results included). First artifact exists: `experiments/2026-07-07_news-sentiment-null-result.md`
  (untracked as of this writing — **commit it**).
- **parquet-store-audit** — the 5 storage corruption patterns (Hive year-shadowing, raw dupes,
  stale curated, empty-but-wired, schema drift) as an audit procedure with a verdict-table
  deliverable.

## Open items found in triage (still open as of end of session)

1. **Transcript pull STALLED (custom_index_tool, 25/125, PULL_STALLED.txt raised).** Today's
   9:00 AM run got AV quota-exhausted on BOTH keys (F78Y…, 9KPJ…) on the FIRST request despite
   a fresh day. Working hypothesis: AV's 25/day free limit is enforced per IP, so key rotation
   never doubled quota, and reset timing (UTC vs ET vs rolling-24h) may not align with 9 AM.
   Undiagnosed — one manual test request would confirm. If per-IP: ~5 more daily runs to finish
   (125−25 pulls at ~25/day). Side note: `scripts/daily_transcript_pull.ps1`'s `>>` redirection
   appends UTF-16 to a UTF-8 log (mojibake in pull_log.txt) — add `-Encoding utf8` when editing.
2. **Weekly quality check FAILs (QUALITY_FAIL.txt raised).** `options_history` (1 error,
   PLTR 06-17 file) and `synthetic_options` (1 error, 06-16 file) + 60 NO DATA tables. Not
   investigated — load `parquet-store-audit` and clear the flag when done.
3. **Next pipeline work per Zander's decision above:** scope the event-impact module
   (event_backtest.py generalization). Needs its own conversation before implementation.

---

# Session 3 (same day) — Event-impact module: scoped, built, PIT-fixed, wired

**Session model:** Claude Sonnet 5

## What happened

Scoped and built the event-impact module (`analytics/event_impact.py`, new) per Zander's
three decisions: oil driver only for v1, auto-select exposed symbols via
`analytics/exposure.py`'s significance threshold (|t_ex_mkt| > 3), and build both a
research module/CLI report AND (conditionally) a live `signal_panel()` factor.

### First pass — caught two problems before trusting the result

1. **Sign-mixing.** The first version pooled positively- and negatively-exposed names
   into one event study. An oil surge should push positive-exposure names (USO, XLE, CVX)
   up and negative-exposure names (TLT, staples, tech) down — pooling them averages the
   effect away. Fixed by running each sign as a separate event study.
2. **Look-ahead in symbol selection.** `exposure_map()` by default uses full-history OLS
   betas, so a symbol's "oil-exposed" label was computed using data from *after* many of
   the events being tested — the same class of bug `signal-eval` warns about, just at the
   symbol-selection layer instead of the entry-timing layer. Fixed with `_rolling_grouping()`:
   each event date gets its own exposure classification from a strictly trailing 3-year
   window, so membership varies date to date (point-in-time, not a fixed list).
3. **Date clustering inflates pooled significance.** Symbols sharing the same event date
   share the same shock — they aren't independent draws. Added `_date_level_stats()`
   (aggregate to one mean CAR per event date first, t-test across dates) as the honest
   companion to the existing (symbol,event)-pooled stat — same fix pattern as pooled-vs-
   daily IC in `sentiment_eval.py`.

### Result (oil, ±15% over 10d, 3y trailing exposure window, both directions tested)

Real, PIT-safe, economically-signed effect — much smaller than the naive full-history
version, but genuine:
- **Positive-exposure names** (oil/energy/materials): significant 1-3 day co-movement
  WITH the shock in both directions (date-level t = 4.13 at h1 for the surge case,
  t = -2.23 at h1 for the drop case). Decays to noise by ~day 5.
- **Negative-exposure names** (bonds, staples, tech, healthcare): correctly-signed
  reaction in both directions but inconsistent timing (significant at 21d for the surge,
  at 1d/5d for the drop) — not trusted yet, not wired into the live factor.

### Live factor: `oil_shock`, weight 0.5

Wired into `analytics/signals.py`'s `_raw_signals()` — deliberately narrow: only the
validated positive-exposure/1-3-day leg. Sparse by design (NaN except in the reaction
window after a qualifying oil shock, ~44-46 episodes over 25 years). Merged directly by
(symbol, date) rather than sourced from `feature_matrix()` like the other factors, since
it's event-triggered rather than continuously observable.

**Known gap:** `feature_matrix()`'s own symbol/date filtering (unrelated to this change)
dropped every oil-exposed name (CVX, XOM, XLE, DOW, CAT) out of every test universe tried
— only a handful of names (AAPL, GE, KO) survived whatever join `feature_matrix()` does.
The `oil_shock` merge code is verified correct in isolation (`oil_shock_signal()` tested
standalone and via `tests/test_event_impact.py`), but end-to-end confirmation that it
actually populates in a real `signal_panel()` run is still open — worth checking why
`feature_matrix()` excludes these names next time factor coverage is investigated.

## Files added/changed this session
- `analytics/event_impact.py` (new) — `_rolling_grouping()`, `driver_event_study()`,
  `oil_shock_signal()`, `_date_level_stats()`, CLI (`python -m analytics.event_impact
  --driver oil --pct 15 --days 10`).
- `analytics/signals.py` — added `oil_shock` factor (weight 0.5), `oil_shock()` wrapper.
- `tests/test_event_impact.py` (new) — synthetic-data tests for point-in-time
  classification and the date-level stat; 5 tests, no live data required.
- Full suite: 261 passed (was 256).
