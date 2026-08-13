# Session Notes — 2026-07-03 (Event Backtester, TradingView Rating, New Sources)

**Repo:** `C:\Users\zande\PycharmProjects\financial-data-pipeline` (branch `master`)
**Commits this session:** `82573d9`, `45e19e4` (pushed), `f418ab9` (Schwab expansion — **NOT pushed**, see Part 2 below)
**Python:** `C:\ProgramData\anaconda3\python.exe`
**Tests:** 223 passed, 5 skipped (schwabdev now installed; remaining skips = anthropic)

---

## For You (Zander) — What You Need to Know

### The three goals, all delivered

**1. New data sources (sample-verified, fully wired into run_all/validate/tests):**

| Pipeline | Table | What it gives you | Key? |
|---|---|---|---|
| `yfinance_pipeline.py` | `market_history` | S&P 500 **to 1927**, VIX to 1990, WTI/Brent/gold/natgas futures to 2000, EUR/JPY/GBP/DXY, TLT/IEF/HYG/LQD, BTC | No |
| `tradingview_pipeline.py` | `tv_ratings` | TradingView's actual aggregate Technical Rating (Strong Buy…Strong Sell) + RSI/MACD/MAs/sector for top-N US stocks + 20 ETFs | No |
| `sec_filings_pipeline.py` | `sec_filings` | EDGAR daily filing index: 8-K, 10-K/Q, S-1, SC 13D/G, DEF 14A, ticker-mapped (~84%) | No (EDGAR_USER_AGENT) |

Also ran the **existing** Tiingo pipeline with `--backfill` for 6 symbols (AAPL, MSFT, SPY, XOM, JPM, NVDA → 1990+, 52k rows). It always supported deep history; nobody had run it that way.

**2. Backtesting tool — `event_backtest.py`** (new, repo root):
- `event_study(events, symbols, window, benchmark, entry_lag)` → CAR curves, hit rates, t-stats, and the **unconditional baseline** so you can see edge vs base rate
- `scenario(events, holding_days, stop_loss_pct, take_profit_pct, side)` → trade list, win rate, avg return, profit factor, equity curve
- Event generators: `earnings_events` (beats/misses/surprise threshold), `filing_events`, `drawdown_events` ("market down X% in N days"), `price_move_events` (oil surge), `threshold_events` (VIX crosses 30), `technical_events` (golden/death cross, RSI, MACD, TV-rating transitions, or any custom lambda)

**3. TradingView signal:** TV doesn't serve rating history, so we did both:
- The pipeline snapshots their live ratings daily (accumulates history going forward)
- `analytics/technical.py` → `tv_rating()` **replicates their exact 26-signal formula locally** from our own OHLCV — validated exact against their live scanner on completed bars. Fully backtestable over decades.

### Demo findings (real data, this session)

- **TV rating turns Strong Buy** (6 deep-history symbols, 1,619 trades, 21d hold): **60.6% win, +1.98% avg, PF 1.88** — your hunch that it's profitable checks out historically
- **S&P −5% in 5 days** (178 events since 1928): keeps falling ~10 sessions, recovered by day 63. Buy-the-dip w/ 21d hold + 8% stop: 61% win, +1.46% avg, PF 1.56
- **VIX crosses 30 → SPY**: weak near-term, +2.9% by day 63 (68% hit rate)
- **Oil +15% in 10 days** (35 events): SPY +2.0%/21d (t=2.8), gold +1.2%, **TLT −1.4%** (bonds sell off on oil shocks)
- **Golden cross**: no edge vs SPY (CAR63 −4.5% vs baseline) — on this small universe at least

### What to run next (in priority order)

```bash
# 1. Deep history for the whole watchlist (~62 symbols, fast) — unlocks broader backtests
python tiingo_pipeline.py --backfill
python curated.py --table tiingo_prices          # REQUIRED after any manual backfill

# 2. Full market-asset universe (19 remaining of 25)
python yfinance_pipeline.py --backfill

# 3. Start accumulating real TV rating history (run daily; already in run_all.py)
python tradingview_pipeline.py

# 4. Wider filing history for filing event studies
python sec_filings_pipeline.py --backfill        # 90 days
```

**Known gap:** earnings event studies have zero usable events right now — `earnings_calendar` only holds ±6 weeks of events and none of those reporters overlap the price store. Fix = historical earnings pull (Finnhub range endpoint, needs a small pipeline extension) + price backfills above.

---

## For Claude — Technical Pickup Notes

### Files created this session
- `yfinance_pipeline.py`, `tradingview_pipeline.py`, `sec_filings_pipeline.py` (repo root, standard pipeline conventions)
- `event_backtest.py` (repo root, alongside `backtest.py` which is the older quantile-portfolio engine — they're complementary)
- `analytics/technical.py` (indicator library + TV rating replica)
- `tests/test_event_backtest.py` (13 synthetic-data tests, no keys/data needed)

### Files modified
- `query.py` — CATALOG +3: `market_history` (yfinance/), `tv_ratings` (tradingview/), `sec_filings` (sec_filings/) → now 109 tables
- `run_all.py` — 3 new Stage-1 PipelineSpecs (yfinance, tradingview, sec_filings)
- `validate.py` — 3 new SCHEMAS entries
- `tests/test_catalog.py` (EXPECTED_TABLES +3), `tests/test_pipelines.py` (PIPELINE_MODULES +3)
- `analytics/events.py` — fixed stale camelCase `epsActual`/`epsEstimate` → `eps_actual`/`eps_estimate` (schema drift)
- `CLAUDE_SESSION_NOTES.md` — Session 3 section appended (same content as this file, shorter)

### Critical implementation details
- **`event_backtest.load_close()` picks the LONGEST series across price tables** (tiingo_prices → prices → market_history → sector_etfs). Do not revert to first-hit: a shallow 90-day watchlist pull would shadow 24 years of market_history (this was a real bug, fixed).
- **Event snap tolerance:** events whose nearest trading day is >10 days away (i.e., before the symbol's history starts) are dropped — otherwise 1928 events silently alias to a symbol's first bar (also a real bug, fixed).
- **CAR convention:** CAR(h) = cum return from close(day −1) through close(day h); day 0 = event-day reaction. Use `entry_lag=1` for earnings/filings (timestamp granularity is day-level).
- **Benchmark self-events dropped:** an event symbol equal to the benchmark is excluded (abnormal return identically 0, distorts medians).
- **Curated staleness:** `query.py` prefers `storage/curated/<table>/` snapshots. After ANY manual pipeline run, `python curated.py --table <t>` or reads are stale (bit us with tiingo_prices this session; run_all.py auto-compacts, manual runs don't).
- **TV rating formula:** `Recommend.All = mean(rating_MA, rating_osc)`; MA group = 15 votes (SMA/EMA 10-200, Hull9, VWMA20, Ichimoku), osc group = 11 votes (RSI, Stoch, CCI, ADX, AO, Mom, MACD, StochRSI, W%R, BBPower w/ EMA50 trend filter, UO); votes ±1/0 per tradingview-ta logic. Thresholds: ±0.1 / ±0.5. Scanner exposes per-indicator `Rec.*` columns for re-validation. Residual mismatches vs live scanner = partial intraday bar on cross conditions only.
- **TradingView scanner:** POST `https://scanner.tradingview.com/america/scan`, JSON `{columns:[...], filter:[...], sort:{...}, range:[a,b]}` or `{symbols:{tickers:[...]}}`. No auth. Page size 500 works.
- **EDGAR form.idx quirks:** header row wraps unpredictably → parse data rows right-to-left (`rsplit(None, 3)`, then split form/company on 2+ spaces). HTTP 403 = index not yet published (treat like 404 weekend). CIK→ticker map: `https://www.sec.gov/files/company_tickers.json`.
- **Stooq is dead** as a source (JS proof-of-work anti-bot wall since ~2026). Tiingo + yfinance cover the need.
- **Tiingo free tier:** prices to 1990 work; News API returns 403 (paid tier) — pipeline handles gracefully.

### State of data on disk (this clone)
- `tiingo_prices`: 6 symbols deep (1990+), 56 symbols shallow (90 days, thru 2026-06-22)
- `market_history`: 6 of 25 assets (^GSPC, ^VIX, CL=F, GC=F, EURUSD=X, TLT), full history
- `tv_ratings`: single snapshot 2026-07-03 (top-100 + 20 ETFs, 120 rows)
- `sec_filings`: 4 business days (2026-06-29 → 07-02, 1,198 filings)
- `earnings_calendar`: 2026-06-12 → 07-23 only, 26 events with actuals, **zero overlap with price store**

### If continuing interrupted work
Nothing is half-done. All five session tasks completed. The natural continuation is the "What to run next" list above, then: historical earnings source (Finnhub `/calendar/earnings` accepts from/to ranges — extend `finnhub_events_pipeline.py` with a backfill loop), and optionally an `earnings drift` demo once coverage lands.

---

# Part 2 (same day) — Backfills Run + Schwab API Expansion

## For You (Zander)

### Backfills — ALL DONE (and pushed: `82573d9` + `45e19e4`)
- **tiingo_prices**: all 63 watchlist symbols deep (456,069 rows, most to 1990; ETFs to inception). Curated snapshot refreshed — queries are current.
- **market_history**: all 25 assets full history (197,192 rows; S&P to 1927, Nikkei 1965, DXY/Nasdaq 1971, commodities 2000).
- **tv_ratings**: 2026-07-03 snapshot, 520 rows (175 buy / 145 strong_buy / 141 sell / 51 neutral / 8 strong_sell).
- **sec_filings**: 90 business days backfilled (~300–900 filings/day). Filing event studies now feasible.
- Remaining data gap: historical earnings (unchanged from Part 1).

### Schwab expansion — 4 new capabilities (commit `f418ab9`, NOT pushed)
Your hunch was right: the old pipeline hardcoded 1 year, but Schwab serves each stock's full listed history (daily bars to ~1985).

| What | How to run | Table |
|---|---|---|
| Full-history daily prices | `python price_history_pipeline.py --full --watchlist` (or `--symbols A B`, `--start YYYY-MM-DD`) | `prices` |
| Intraday minute bars | `python schwab_intraday_pipeline.py --backfill` (5-min ~9mo; `--freq 1` = 1-min ~48d) | `schwab_intraday` |
| Top-10 movers snapshot | `python schwab_movers_pipeline.py` (daily; in run_all) | `schwab_movers` |
| Portfolio mirror | `python schwab_portfolio_pipeline.py --backfill --years 10` | `schwab_positions`, `schwab_transactions` |

**⚠ BLOCKED ON YOU — one-time OAuth.** No `tokens.json` in this clone. From the repo dir in a real terminal:
`C:\ProgramData\anaconda3\python.exe schwab_movers_pipeline.py`
→ open the printed URL, log in to Schwab, paste the redirected `https://127.0.0.1...` URL back. Then Claude can run sample pulls + push `f418ab9`.

Full backfills deliberately NOT run yet (your call, pending storage sizing). `--full` mode prints per-symbol date-range/row-count so you can estimate size first.

## For Claude — Part 2 Pickup Notes
- `schwabdev` 3.0.4 now installed in `C:\ProgramData\anaconda3`. Client methods verified: `linked_accounts()`, `account_details_all(fields)`, `transactions(hash, start_dt, end_dt, type)` (accepts datetimes, 1-yr max span — pipeline chunks), `movers(symbol, sort)`, `price_history(...)`.
- **price_history full-history trick**: omit `period`, pass `startDate=1970 epoch ms` — date range wins over period. Existing code passed `period=1`, which capped everything at 1 yr. NOT yet live-verified (blocked on OAuth) — verify AAPL/KO depth on first authenticated run.
- Movers response parsed defensively (`screeners[]`, field names vary). Portfolio masks account numbers to last-4; parquet gitignored.
- New tables wired everywhere: query.py CATALOG (113), validate.py SCHEMAS, run_all.py Stage-2 specs (`schwab_intraday`, `schwab_movers`, `schwab_portfolio`), tests/test_catalog.py, tests/test_pipelines.py. New storage dirs have `.gitkeep`.
- After OAuth: run movers (auth trigger), then `price_history_pipeline.py --full --symbols AAPL KO GE` (depth probe + storage estimate), `schwab_intraday_pipeline.py --days 2` sample, `schwab_portfolio_pipeline.py` (30d), then validate + push.
- **Schwab OAuth still pending** as of Part 3/4 below — none of the Schwab expansion pipelines (`schwab_intraday`, `schwab_movers`, `schwab_portfolio`) have been live-run yet. Not blocking — the scanner/monitor/backtest work below runs entirely off Tiingo/yfinance data.

---

# Part 3 (same day) — Signal-Change Scanner + Signal Health Monitor (commit `c536691`, pushed)

## For You (Zander)

Two new tools on top of the event backtester, both requested this session:

**1. Daily scanner — "who changed their TA rating today?"**
```bash
python signal_scan.py                     # today's changes, 63-symbol watchlist
python signal_scan.py --date 2026-06-15
python signal_scan.py --upgrades --min-step 2     # only big jumps (e.g. neutral -> strong_buy)
python signal_scan.py --source tv                 # diff TradingView's own daily snapshots instead
python signal_scan.py --history 30                # all changes in the last N days
```
Read-only — writes nothing, not in `run_all.py`. Confirmed live: 648 bucket changes found across the watchlist over the last 30 days.

**2. Maintained backtest — tracks whether the signal's edge is holding up**
```bash
python signal_monitor.py            # re-scores tv_strong_buy/tv_buy/tv_sell/tv_strong_sell/golden_cross
python signal_monitor.py --history 10
```
Writes a new **`signal_health`** table (win rate / avg return / profit factor / CAR21 per signal per trailing window: full, 3y, 1y, 180d) and flags `DEGRADED` when a signal's edge is fading. Wired into `run_all.py` so it refreshes on every full run.

**Already caught something real:** the first live run flagged `tv_sell` and `tv_strong_sell` as DEGRADED — both short-side signals have a trailing-1y profit factor below 1.0 (0.55 / 0.40). Their full-history PF was already weak (0.62 / 0.65), so this reads more like "the short side of this signal never had a clean edge" than "an edge that decayed" — but exactly the kind of thing you'd want surfaced automatically rather than assumed away.

## For Claude — Part 3 Pickup Notes
- `event_backtest.rating_changes()` / `tv_snapshot_changes()` reuse `analytics.technical.rating_history()` for all indicator math — the scanner only diffs `rating_label` day-over-day. `_RATING_ORDER` gives the 0-4 ordinal used for step/direction.
- `signal_monitor.py` calls `technical_events()` with **no** `start=` (full price history, so 200-day SMA warm-up is never truncated), then filters the resulting *events* by date per trailing window before scoring — restricting event dates, not price history, is the trick that keeps windowed runs correct.
- **Runtime cost**: each `signal_monitor.py` run took ~15 minutes (5 signals x 63 symbols = 315 `rating_history()` calls, no caching — indicators recomputed from scratch every call). Fine for a daily cron via `run_all.py`, but worth caching per-symbol indicator results if faster ad-hoc reruns matter later.
- CATALOG 132→133 (`signal_health`); `validate.py`/`test_catalog.py`/`test_pipelines.py` updated; 11 new tests in `test_event_backtest.py` (bucket-change detection, direction/min_step filters, date-mode isolation, empty-result shape). Full suite: **234 passed, 5 skipped.**
- `FinancialDataPipeline_Future_Improvements.md` brought current in the same commit — documented Part 1/2 (event backtester + Schwab expansion) and Part 3 (scanner/monitor), closed out candidates A (movers — largely covered by schwab_movers/finviz_movers/sa_movers) and B (portfolio tracking — done), added candidates for historical earnings backfill and full Schwab price backfill.

---

# Part 4 (same day) — TV-Rating Backtest on TSLA/LMT/NVDA/KEYS/GOOG/NFLX

## For You (Zander)

Ran the comprehensive TV-rating backtest you asked for on this basket. Full report (tables + takeaways): https://claude.ai/code/artifact/a947fa3a-ab4a-4a53-8121-a15afe4fb395

**Headline:** long side works cleanly on all 6 names (Strong Buy/Buy: ~59-60% win rate, PF ~2.0, t-stats in the 20s out to 63 days). Short side (Sell/Strong Sell) doesn't — every name's PF stays below 1 on a 21-day hold, because these are strong-drift growth/quality names that keep grinding up even after a sell signal. NVDA has the best raw payoff; KEYS/GOOG have the highest win rates but smaller moves; LMT is the weakest (still profitable) long performer, consistent with it being the lowest-beta name in the set.

**Data gap found and fixed along the way:** LMT and KEYS had no price history anywhere in the store, and GOOG only existed as GOOGL (different ticker). Backfilled all three from Tiingo (`tiingo_pipeline.py --backfill --symbols LMT,KEYS,GOOG`, 15,219 rows, LMT back to 1990) and refreshed the curated snapshot before running the backtest.

## For Claude — Part 4 Pickup Notes
- Before trusting any basket-level backtest request, check `q.symbols('tiingo_prices')` (or whichever table) for coverage first — this session it silently would have run on 3/6 symbols if the gap hadn't been caught.
- Backtest used `event_backtest.technical_events()` + `scenario()` (21d hold) + `event_study()` (CAR curve, window=(0,63)) per signal, both pooled across the basket and broken out per-symbol. No new code — pure application of the existing engine.
- Nothing was committed for Part 4 (backfilled data is gitignored parquet; no code changed) — only the Tiingo backfill + curated refresh on disk.

---

# Part 5 (same day) — Schwab OAuth Completed + `schwabdev` 3.0.4 Compatibility Fix

## For You (Zander)

You ran the Schwab OAuth flow (in your own terminal, not through Claude — logging in requires your credentials/MFA). It initially failed twice:
1. `python` wasn't found — you were typing the bare command instead of the full path; fixed by using `C:\ProgramData\anaconda3\python.exe` explicitly.
2. `TypeError: Client.__init__() got an unexpected keyword argument 'tokens_file'` — the installed `schwabdev` (3.0.4) had switched from a JSON tokens file to a SQLite database (`tokens_db` replaces `tokens_file`) since these pipelines were originally written. Real breaking API change, not a typo — fixed across all 8 Schwab pipelines (see below).
3. Then a redirect-code timing issue (`invalid_grant`) — Schwab's auth code expires in ~30 seconds, and routing it through chat burned that window. Fixed by doing the whole browser→paste step directly in your terminal, no detour.

**OAuth is now done — `tokens.db` exists and is gitignored.** Verified live:
- ✅ `schwab_movers_pipeline.py` — works (today's data is degraded/placeholder since 2026-07-04 is a market holiday — Schwab's `/movers` endpoint itself returns `lastPrice: 0.0`/`netPercentChange: -1.0` when markets are closed, not a bug on our end)
- ✅ `price_history_pipeline.py --full` — **verified full history back to 1985-01-02** for AAPL/KO/GE (previously untested, was capped at 1 year before this session). Storage estimate: 3 symbols = 1.68MB → full 63-symbol watchlist ≈ **35MB**, trivial.
- ✅ `schwab_intraday_pipeline.py --days 2` — 78 five-minute bars/symbol/day (correct for a 6.5hr session), 1,092 bars across 14 symbols.
- ❌ `schwab_portfolio_pipeline.py` — **blocked on a Schwab-side permission**, not code: `linked_accounts()` returns `401 Client not authorized`. Movers/price-history/intraday all hit the Market Data API (working); accounts/positions/transactions hit the separate Trader API product, which isn't enabled on your registered Schwab app yet. Fix: enable "Trader API - Individual" for the app at developer.schwab.com — may need Schwab-side re-approval, not instant.

## For Claude — Part 5 Pickup Notes
- **Root cause**: `schwabdev.Client.__init__` in the installed 3.0.4 no longer accepts `tokens_file`; it now takes `tokens_db` (SQLite path, default `~/.schwabdev/tokens.db`). Source: `schwabdev/tokens.py` `Tokens.__init__` signature.
- **Fix applied** (not yet committed as of this note): renamed `tokens_file=TOKEN_PATH` → `tokens_db=TOKEN_PATH` and changed each pipeline's `TOKEN_PATH` default from `"tokens.json"` → `"tokens.db"` in all 8 files that construct a `schwabdev.Client`: `schwab_portfolio_pipeline.py`, `schwab_movers_pipeline.py`, `schwab_intraday_pipeline.py`, `price_history_pipeline.py`, `options_chain_pipeline.py`, `schwab_options_pipeline.py`, `schwab_quotes_pipeline.py`, `sector_etf_pipeline.py`. Also updated `.env` (`SCHWAB_TOKEN_PATH=tokens.db`) and `.gitignore` (added `tokens.db` alongside the existing `tokens.json` entry — kept both since old clones/scratch scripts may still reference the json path).
- Full test suite re-run after the fix: **234 passed, 5 skipped** — unaffected, since none of the schwab pipelines' unit tests actually construct a live `Client()`.
- Confirmed via direct `client.movers(...)` call that the `lastPrice: 0.0`/`netPercentChange: -1.0` pattern originates in Schwab's raw JSON response itself (not our `_screener_rows()` parsing) — dated 2026-07-04, a market holiday (July 4th observed Friday 2026-07-03; today's Saturday). Re-verify movers data quality on the next trading day.
- **Not yet committed/pushed** — the `tokens_file`→`tokens_db` fix across the 8 files, `.env`, and `.gitignore` are still local changes only.
