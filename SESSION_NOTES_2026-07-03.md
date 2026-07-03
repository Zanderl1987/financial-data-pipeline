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
