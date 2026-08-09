# Session Notes — 2026-08-08

**Branch:** master (synced with origin at `532207f`; TV-rating writeup already pushed
this session — see Committed below — remaining work uncommitted)
**Session model:** Claude Sonnet 5

## What happened

User asked where the TradingView Technical Rating backtesting work stood, then to dive
into deeper backtests. This became a full rigorous re-audit of the signal — 13
permutation-tested backtests, an out-of-sample split, a full Russell 3000 expansion, a
real data-quality bug found and fixed along the way, live-slider wiring in the interactive
explorer, and a published visualization. Full detail lives in the writeup this session
produced: `experiments/2026-08-08_tv-technical-rating-signal-eval.md` — this file is the
session log; that file is the standing reference for the finding itself.

### 1. Status check (before any new code)

Found the TV rating work more built-out than expected: `analytics/technical.py::
tv_rating()` (validated replica of TradingView's 26-indicator score),
`evaluation/adapters.py::tv_threshold_rule()` (the trade-rule construct), and
`backtest_app.py` (interactive Dash explorer, merged 2026-08-03, never actually run).
Also found a result nobody had written up: the rigorous `tv_threshold` permutation test
from 08-03 (`storage/eval_registry`) showed **pnl_p = 0.99** on the full 69-symbol
universe — no edge — directly contradicting an informal 60.6%-win/PF-1.88 result from an
early, unreviewed session-3 test. That gap became the session's starting question.

### 2. First-pass rigorous eval (never run before)

- `tv_rating_all` (continuous score, full universe): daily cross-sectional IC **negative
  at every horizon**, strengthening from −0.0060 (t=−2.55, 1d) to −0.0116 (t=−5.20, 21d).
  Contrarian to the rating's face-value meaning.
- `tv_rating_changes` at `min_step=1` (any adjacent bucket move): 88k+ events per
  direction, upgrade and downgrade both showing the **same-sign** small positive excess
  return — a red flag (a signal whose bull/bear transitions predict the same thing has no
  directional information). Re-run at `min_step=2` (a real bucket jump, e.g.
  neutral→strong_buy): upgrade held up clean (t=6.61 at 21d), downgrade sign-flipped
  across horizon (reads as reversal noise, not a bearish signal).
- Basket-effect check (the original 6-symbol TSLA/LMT/NVDA/KEYS/GOOG/NFLX basket):
  long leg PF 1.87, short leg PF 0.79 — confirms the historical note that this basket's
  growth tilt structurally favors the long side, on any rule.

### 3. Fixed-21-day-hold follow-up

Tested whether swapping the trade rule's decay-based exit (median 9-day hold) for a fixed
21-day hold (matching the event-study window where the min_step=2 upgrade edge showed up)
would make it tradeable. It didn't: pnl_p=0.98 on the full universe despite a 58–60% raw
win rate — permutation-shuffled entries with the same fixed hold did just as well. Resolved
the apparent event-study/trade-rule disagreement: the event study measures "beats SPY,"
which this universe's own secular-growth tilt does on almost any window; the permutation
test measures "beats random timing in this universe," which is the real test and says no.

### 4. The fade discovery, and its collapse

Motivated by the negative IC: built a trade rule that **fades** the rating (buy when it
crashes into strong_sell, short when it surges into strong_buy — literally
`tv_threshold_rule()` with the two sides' trigger conditions swapped). Split by side:
short-fade lost money despite a >50% win rate (avg loss bigger than avg win); **long-fade
alone was the one construct that ever cleared significance** — pnl_p=0.025, win_rate_p=
0.005, PF 1.45, full 69-symbol universe.

Chased it two ways:

- **Out-of-sample split** (2012-01-01): win-rate effect replicated in both eras
  (p=0.005/0.005); dollar-P&L significance only in the recent half (pre: pnl_p=0.93, PF
  1.32; post: pnl_p=0.005, PF 1.66) — partial, not clean, replication.
- **Russell 3000 expansion** (the real test): first attempt was corrupted — $4.97B total
  P&L, 209% avg P&L/trade, 99.4% of it from 1.9% of trades with sub-$1 entry prices
  (`WSHP` entering at $0.0001, exiting at $39.51 — a 39.5-million-percent single trade).
  Root cause traced to the source, not a fluke — see §5. After excluding 262
  data-quality-flagged symbols (of 2,297 scanned, ~11%) and adding a $5 entry floor:
  **2,030 clean symbols, pnl_p=0.87. Decisive failure.** The curated 69-symbol
  watchlist's growth tilt was doing the work, not the signal.

Final verdict, closed: no form of this signal tested (follow, fade, any hold length, any
universe from 6 to 2,030 symbols) supports a tradeable strategy. Do not build on it.

### 5. Data-quality bug: Schwab returns unadjusted prices

Traced the corrupted `prices` table (27,759 symbols, 46.9M rows) to its actual source:
`schwab_universe_backfill.py` built the whole thing from the Schwab API
(`price_history_pipeline.fetch_symbol`, `schwabdev` client), and Schwab's
`price_history` endpoint returns **unadjusted OHLC** — no split-adjustment parameter is
requested or applied anywhere downstream. Answering the user's direct question: no,
Schwab isn't a better source to switch to — it already *is* the source, and this is a
characteristic of its raw history, not a vendor swap opportunity. No free
corporate-actions/split feed is currently wired in (Tiingo's needs a paid add-on).

Fix shipped: `evaluation/universe.py::flag_price_jumps()` / `clean_symbols()` — flags any
symbol with a single-day `|log return| > ln(3)` (>3x jump or <1/3x drop) anywhere in its
history, excluded wholesale (a bad split ratio corrupts the whole pre/post-jump history,
not just the jump day). 3 new tests in `tests/test_universe.py`, all passing. Any future
full-universe analysis reading from `prices` should run symbols through this first.

### 6. `backtest_app.py`: fade rules wired into live sliders

`KNOWN_TRADE_RULE_SIGNALS` restructured from `name -> cache_builder` to
`name -> (cache_builder, rule_builder)`. Added `build_tv_fade_rule()` /
`build_tv_fade_long_rule()` (same crossed-up/crossed-down shape as
`build_tv_threshold_rule()`, entries/exits swapped per side). `tv_threshold`, `tv_fade`,
`tv_fade_long` now all support live threshold tuning; basket/Russell-3000-scoped runs
intentionally NOT wired in (the shared cache builder always rebuilds the default full
universe — wiring a basket-named entry would silently show wrong live data). Verified live
against the already-running Dash server (`debug=True` auto-reloaded on save;
`/_dash-layout` confirmed the new signals appear). 4 new tests in
`tests/test_backtest_app.py`; 2 pre-existing tests updated for the new tuple shape.

**Caveat if you use this**: slider labels ("Bull entry"/"Bear entry") describe
`tv_threshold`'s semantics. For `tv_fade`, "Bull entry" drives the SHORT trigger and "Bear
entry" drives the LONG trigger — opposite of what the label implies. Not fixed this
session (would need per-signal dynamic labels, out of scope for "wire in the rules").

### 7. Interactive visualization published

Built a self-contained HTML report (hand-rolled inline SVG charts + vanilla JS tooltips,
validated categorical/status palette per the dataviz skill, light+dark theme) covering the
forest plot of all 13 pnl_p tests, the long/short PF asymmetry, the fade lead's 4-step
collapse, the IC-by-horizon chart, and the Russell 3000 before/after data-quality
comparison. Published: `https://claude.ai/code/artifact/3600d61f-3425-4021-b699-57f2729ffebe`.
Every number cross-checked against actual logged run output before publishing — two
profit-factor values in an early draft were wrong (guessed as direct mirrors; they're
actually reciprocals of the mirrored leg's PF — `PF_fade_leg = 1 / PF_original_leg` when
the fade leg is the exact sign-flip of the original's opposite side) and were corrected
against `trades.parquet` before shipping.

**Known gap**: no browser tool was connected in this environment
(`mcp__claude-in-chrome__tabs_context_mcp` failed — extension not connected), so the page
was verified by static review (JS syntax check, data cross-checks, manual text-overflow
risk check on the longest SVG labels) rather than an actual screenshot. Worth a visual
double-check next session if not already done.

### 8. User pushback: "did you actually fix the pricing data?"

Asked directly whether `clean_symbols()` adjusted the underlying prices or just excluded
bad symbols. Answered honestly: it's a screen, not a fix — `prices` itself is untouched,
still raw/unadjusted for every symbol including ones below the 3x-jump detection
threshold. This distinction mattered a lot for what followed (§9-13).

### 9. Sourcing real adjusted-close data (data-source-vetting skill)

User asked to find and add a proper adjusted-close source. Before touching any code, found
the fix was **partially already built and unused**: `tiingo_pipeline.py` already fetches
Tiingo's `adjClose`/`splitFactor`/`divCash`, and `analytics/technical.py::_load_ohlcv()`
already had a "prefer adjusted prices when the table carries them" rule — meaning every
69-symbol test all session was already split-adjusted via Tiingo; the corruption only hit
Russell 3000 because that scale had to fall back to the Schwab-only `prices` table, which
has no adjusted columns at all. So the real gap was *broad-market* coverage, not a fix to
the existing 69-symbol tests.

Vetted two candidate sources live before building anything (data-source-vetting skill):
- **Tiingo free tier**: confirmed via live API test call — 500 unique symbols/month cap.
  Hard NO-GO for Russell 3000 (2,298 symbols) in any reasonable timeframe. Power tier
  ($30/mo, 109,159 symbols/month) would work but costs money.
- **yfinance**: no per-symbol quota, already a repo dependency (`yfinance_pipeline.py`).
  Probed live: 50-symbol bulk `yf.download()` batch in 2.1s, 10-symbol full-history batch
  (back to 1962-1980) in 2.8s, correct AAPL 2020 4:1 split handling, no CARE-style spike
  (the corrupted-Schwab example symbol) in its data. User chose yfinance (free, verified
  working) over Tiingo Power (paid) or a hybrid.

### 10. `yfinance_universe_backfill.py` built, wired, run

New pipeline (`yfinance_universe_backfill.py`) — chunked/resumable like
`schwab_universe_backfill.py`, but using bulk `yf.download()` (not one `Ticker.history()`
call per symbol) for speed. Pulls Russell 3000 symbols from `securities.is_russell3000`
(not the full ~29k `symbol_universe.csv` — scoped to what full-universe backtesting
actually needs). Full wiring checklist done: `query.py` CATALOG (new
`yfinance_universe_prices` entry; **also fixed a pre-existing glob-collision bug** —
`market_history` globbed `yfinance/**/*.parquet` with no filename prefix, which would have
silently swept the new files into the wrong table), `validate.py` SCHEMAS,
`tests/test_catalog.py` EXPECTED_TABLES. `curated.py`/`run_all.py`/`tests/test_pipelines.py`
deliberately NOT touched, matching the `schwab_universe_backfill.py`/`market_history`
precedent (occasional manual backfill scripts aren't part of the daily pipeline cycle).

Live run: **2,285 of 2,298 symbols succeeded** (13 empty, 0 failed), 12.36M rows, back to
1962, in well under the ~2hr Schwab-equivalent runtime. Smoke-tested first on 5 symbols
including previously-corrupted `WSHP` — came back clean ($5.50, not $0.0001).

**Side finding, not fixed this session**: `securities.is_russell3000` itself has real gaps
— `AAPL`, `MSFT`, and `NVDA` are NOT flagged `is_russell3000=true`, discovered only because
`AAPL` came back empty during a sanity check. This means the "Russell 3000" universe used
for *every* Russell-3000-scale test all session (both Schwab and yfinance passes) was never
the true, complete index — missing several of the largest constituents. Doesn't invalidate
the null-result conclusion (2,000+ symbols is still a large, diverse sample), but it's a
real caveat worth fixing if this universe gets reused.

### 11. HuggingFace push

User asked whether the new table was on the public HF dataset. It wasn't yet — but the
mechanism needed no new code: `curated.py` and `upload_huggingface.py` both work generically
off `query.CATALOG` (confirmed via the `market_history` precedent, which also has no
special `curated.py` KEYS entry and still gets a curated snapshot). Ran
`curated.py --table yfinance_universe_prices` (255MB curated file) then
`upload_huggingface.py` (full ~3GB/180-table folder sync, matching what `run_all.py` does
automatically after every run per `AUTOMATION.md`). Live at
`https://huggingface.co/datasets/ZanderL1337/financial-data-pipeline` — 180 tables, 105.1M
rows, 2984MB.

### 12. Iceberg V3 / deletion-vectors question

User asked if this repo runs Iceberg V3 with deletion vectors to avoid metadata bloat.
Checked the installed library directly rather than answering from memory: this repo's
table-creation scripts (`create_securities_table.py`, `create_shipping_tables.py`,
`iceberg_pilot.py`) all explicitly set `"format-version": "2"`. The installed `pyiceberg`
(0.11.1) hard-blocks upgrading a table past v2 (`SUPPORTED_TABLE_FORMAT_VERSION = 2`,
enforced in `table/update/__init__.py` with a `ValueError` on any upgrade attempt) — some
V3/Puffin scaffolding exists (`table/puffin.py`, a `TableMetadataV3` read model) but reads
as forward-compat/read support, not confirmed write support. More importantly: this repo's
actual documented bloat pattern (per existing CLAUDE.md notes) is (a) one `overwrite()` per
loop item creating a new snapshot per item — already fixed by batching into transactions —
and (b) `pyiceberg` 0.11.1 having no on-disk orphan-file GC, so `expire_old_snapshots()`
trims logical history but never reclaims old data/manifest files. That's a missing-GC
problem, not a V2-vs-V3 delete-file-format problem — and this repo's writes are coarse
whole-partition `overwrite()`s, not row-level upserts, so V3 deletion vectors (which
target *that* pattern specifically) likely wouldn't even be the relevant lever here.
Not investigated further this session (whether a newer pyiceberg lifts the v2 cap or adds
orphan GC) — flagged as a follow-up if raised again.

### 13. Two more real bugs found chasing the Russell 3000 fade result on yfinance data

Re-ran the same long-fade Russell 3000 verification against the new `yfinance_universe_prices`
table (no Schwab-style exclusions this time) expecting confirmation of the null result.
Instead got a *strongly significant* pnl_p=0.005 — a red flag per this project's own
skepticism standard (surprising-good result → assume a leak, re-audit). Found two real,
independent bugs before trusting any number:

- **Dividend-adjustment distortion** (the deeper one). `_load_ohlcv()` and
  `event_backtest.py::load_close()` both had a pre-existing "prefer the adjusted price
  column when available" rule — fine for a total-return chart, wrong for a technical
  indicator or a discrete trade-rule backtest with no dividend-reinvestment model (this
  repo's `evaluation/trades.py` engine has none). Dividend adjustment compounds backward
  over decades: `DUK`'s 1990 dividend-adjusted close is 18.8% of that day's real traded
  price. Feeding that into a moving-average-based rating manufactures a fake long-run
  uptrend for high-yield names — exactly the kind of stock the original 69-symbol
  growth-tilted watchlist underrepresented, so this bug was latent all session and only
  surfaced at Russell 3000 scale. **Fixed**: new `analytics/technical.py::
  _split_only_adjust()` computes split-only adjustment from Tiingo's `split_factor`
  (Yahoo's plain `close` is already split-adjusted at the source and needs no extra work);
  neither function prefers a dividend-adjusted column anymore. Verified live: `DUK` now
  shows $6.43 (1980) → $124.85 (2026), a plausible 19x, not an artificially deflated value.
- **Ticker-reuse / bad historical data**, unrelated to adjustment math, found only because
  the fix above *didn't* eliminate the significant result (re-ran, still pnl_p=0.005).
  Traced to the largest contributing trades: `QUBT` entering at $20 in Dec 2007, exiting at
  $800 in May 2008 (3,900%); `DFTX` entering at $0.015 in Dec 2019, exiting at $0.84 twelve
  days later (5,500%, a $550,000 single-trade "win" on $10k notional). Even Yahoo's data
  isn't immune to an old delisted company's history being stitched to an unrelated later
  company under a recycled ticker. The existing `flag_price_jumps()` screen (built for the
  Schwab pass) caught it on yfinance data too: 146 of 2,298 symbols flagged (6.4%, vs
  Schwab's 262/2,297 / 11% — yfinance is somewhat but not entirely cleaner).

**Final, doubly-independent result** (split-only adjustment + 146 flagged symbols excluded
+ same $5 entry floor as the Schwab pass, 2,139 usable symbols): **pnl_p = 0.4577, win_rate_p
= 0.005.** Not significant — confirms the original Schwab-based null (0.87) via a fully
independent data source and independent bug-fixing path. Two vendors, two different sets of
real bugs, the same qualitative answer both times.

Full detail (numbers, code changes, reasoning) in the writeup's "Follow-up (2026-08-09)"
section — this was genuinely a multi-hour, multi-attempt chase (4 separate ~1.5-2.5hr
background permutation runs across the night to get from "first Russell 3000 attempt" to
"trustworthy final answer"), documented in full there rather than summarized further here.

## Verification

- Full project suite: **567 passed, 0 failed**, run twice more after the §9-13 changes
  (once after the yfinance pipeline wiring, once after the split-only-adjustment fix) —
  still 567/0 both times, no regressions from touching shared price-loading code.
- `backtest_app.py` confirmed live and responsive throughout (`curl` 200 on
  `http://127.0.0.1:8050/`); still running at end of session.
- All 13 permutation-test figures in the visualization traced to specific
  `storage/reports/eval/<name>_<timestamp>/results.json` files or terminal output
  captured during the session (listed in the writeup's Reproduce section).
- `validate.py` clean after the new table: 180 PASS / 0 FAIL.
- `yfinance_universe_prices` live-queried post-backfill: 12,360,877 rows, 2,285 symbols,
  1962-01-02 through 2026-08-07.
- HF upload verified via its own success output (180 tables, 105,116,263 rows, 2984.0 MB,
  `https://huggingface.co/datasets/ZanderL1337/financial-data-pipeline`).
- The `_split_only_adjust()` fix specifically verified live on two symbols before trusting
  it: `DUK` (dividend-heavy, confirms the fix) and re-confirmed `AAPL`'s 2020 split still
  handles correctly through the new code path.

## Notes / gotchas

- **Background job wall-clock reality check**: the Russell 3000 cache-build-plus-permutation
  run took ~2 hours (not the ~25-30 min estimated from a 5-symbol timing sample) — the
  200-permutation simulation loop over 2,000+ symbols is the slow part, not indicator
  computation, and it doesn't parallelize automatically. Budget accordingly for any future
  full-universe permutation test.
- **Redirected Python stdout stays empty until the process exits** when piped to a log
  file (block-buffered, not line-buffered) — every long background run's log looked empty
  the whole time it ran, which is expected, not a stall signal. Use process CPU time
  (`Get-Process ... | Select CPU`) to confirm liveness instead of tailing the log.
- `evaluation/adapters.py::from_rating_changes` still has no `min_gap_days`
  parameter — event studies with `min_step=1`-style dense transitions overstate
  independence (not fixed this session, noted in the writeup as a follow-up).
- `storage/curated/README.md` shows modified in `git status` from before this session
  started (not touched by this work) — left alone, not part of any commit here.
- Two untracked `experiments/2026-08-0{7,8}_hormuz-*` files are from a different,
  unrelated session's work (Hormuz gold/oil/rates event study) — also left alone.
- **A "surprisingly significant" result after a data-quality fix is not proof the fix
  worked — it can just as easily mean the fix uncovered or introduced a *different*
  distortion.** Happened twice in a row tonight (Schwab-unadjusted → looked null after
  cleanup; naive yfinance adj_close → looked *very* significant, which was itself wrong;
  fixed adjustment → still significant, which was *also* still wrong until the
  ticker-reuse screen was applied too). Each "fixed" result needs the same skepticism
  as the original, not less.
- `yfinance_universe_backfill_progress.json` added to `.gitignore` (matching the existing
  `schwab_universe_backfill_progress.json` precedent — regenerable local run-state, not
  repo content).

## Committed

- `483e375` — "Add TV Technical Rating signal evaluation writeup (null result)" — the
  writeup as of the first closed-loop conclusion (before the fade-rule follow-up chase).
  Pushed to `origin/master` via a merge commit (`532207f`) after fetching 3 commits that
  had landed from another session (`e3512e3`/`5029780`/`83076d9`, HF sync fixes — no file
  overlap, clean merge).
- This session's remaining work (§8-13, plus this file) — see the commit this session
  ends on; check `git log` for the actual hash rather than trusting a number written here.

## What to do next

1. Visually spot-check the published visualization artifact once a working browser tool
   is available — it was only statically verified this session.
2. Fix `securities.is_russell3000` — confirmed missing `AAPL`/`MSFT`/`NVDA` at minimum;
   the "Russell 3000" universe used all session (both Schwab and yfinance passes) was
   never the true, complete index. Check `index_constituents_pipeline.py`'s BlackRock IWV
   holdings pull for why (stale snapshot? pagination bug? holdings-file quirk?).
3. If picking the TV-rating thread back up: the one still-open lead is the mildly
   contrarian continuous-score IC (−0.0116 at 21d, t=−5.20) — never turned into an actual
   trade rule this session, only measured as an academic quintile-spread. Everything else
   is closed, and closed twice over (two independent data sources).
4. `min_gap_days` de-duplication for `evaluation/adapters.py::from_rating_changes` — a
   general framework gap, not specific to TV ratings, noted twice now.
5. Iceberg: check whether a pyiceberg release newer than 0.11.1 adds on-disk orphan-file
   GC (the actually-relevant gap for this repo's bloat pattern) and/or full V3 write
   support — not investigated this session, only the current 0.11.1 install's capabilities.
6. Consider whether `flag_price_jumps()`'s exclude-the-whole-symbol approach should
   become an actual split-adjustment (using the detected jump ratio to back-adjust rather
   than drop the symbol) for tables that don't have a `split_factor` column to work with
   directly (i.e. `prices`/Schwab) — would recover ~260-400 currently-excluded symbols
   per universe instead of dropping them.
