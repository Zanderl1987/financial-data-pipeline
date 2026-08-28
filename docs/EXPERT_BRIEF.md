# Expert Brief — financial-data-pipeline

Written by Fable 5, 2026-07-06. CLAUDE.md is the operating manual (commands, wiring,
gotchas); this file is the judgment layer — where the value is, what to do next and why,
and the strategic traps that aren't visible from any single file. Update it when the
strategy changes, not on every commit.

## What this system actually is

An alpha-research data platform: ~30 free/public sources → point-in-time-correct feature
matrix → cross-sectional factor signals → quantile + event backtests. The breadth (133
tables) is done and is no longer where the value is. **The value now concentrates in three
places:**

1. **PIT correctness** — `analytics/features.py` ASOF-joins on *filing/publication* dates
   with explicit lags. This is the hard, rare property that makes backtests meaningful.
   Guard it: any new feature block must join on when the data was *knowable*, not when it
   was *observed*. Look-ahead bugs are silent and fatal to every downstream result.
2. **Time-series accumulation** — several tables only exist if pulled daily
   (`tv_ratings`, `schwab_movers`, `short_interest` filings, `earnings_calendar`).
   Missed days are permanent holes; no backfill exists. Continuity now matters more than
   any new source.
3. **The evaluation loop** — `backtest.py` + `event_backtest.py` + `signal_monitor.py`
   exist but the factor set has never been systematically evaluated. That's the unrealized
   payoff of everything built so far.

## Prioritized roadmap (with the reasoning, so reprioritize intelligently)

1. **Run the pipeline daily, automatically.** Everything in (2) above decays without it.
   The transcript-dataset incident (planned daily run silently never ran for a week)
   is the failure mode to design against: automation must *verify output grew*, not just
   execute. As of 2026-07-06 a weekly quality check is automated; the daily accumulator
   run is specced but awaits Zander's go-ahead — see `docs/AUTOMATION.md` at repo root.
2. **Schwab OAuth (user-interactive, ~5 min of Zander's time).** Blocks 4 built-but-
   unverified pipelines and the full-history depth probe. Highest unblock-per-minute in
   the repo. Trader API endpoints additionally need enabling at developer.schwab.com.
3. **Historical earnings dates (Finnhub range queries) + matching price depth.** The
   event-study engine is the most differentiated analytics in the repo and it is starved:
   `earnings_calendar` holds ±6 weeks. Until this lands, earnings event studies — the
   obvious first real research product — are impossible.
4. **Factor evaluation pass.** All 9 signal factors are now live (short_pressure and
   sentiment activated 2026-07-06). Run each through `backtest.py` over the deepest
   available window; record quantile spreads, hit rates, turnover; prune or down-weight
   dead factors in the composite. Do this BEFORE adding factor #10 — an unevaluated
   factor library is inventory, not capability.
5. **Cheap one-off backfills when idle** — FDIC financials (1992+), Fed SOMA (~2002+,
   slow), fama_french/shiller/cboe/fear_greed deep history. Low urgency: these sources
   are fully backfillable anytime, unlike the snapshot sources.
6. **FINRA Query API registration** (developer.finra.org) — upgrades short_pressure from
   watchlist-only (yfinance fallback) to full-market. Only matters if research expands
   beyond the watchlist.

## Judgment calls a future session should respect (or consciously overturn)

- **Stop adding sources by default.** 133 tables; each new one adds permanent maintenance
  surface (scraped headers drift, URLs rotate monthly, WAFs appear). The bar now: a new
  source must feed a specific factor, event study, or research question. "Free and
  available" is no longer sufficient.
- **The health metric that matters is signal-panel coverage** — non-null counts per factor
  in `signals.signal_panel()` (60/69 for short_pressure and sentiment as of 2026-07-06).
  `validate.py` checks schemas; it cannot see a factor silently starving. Check coverage
  after any change touching features/signals, and watch `signal_monitor.py` DEGRADED flags.
- **Free-source APIs rot; budget for it.** ~1 in 3 sources needed a fix at first live run
  or broke later (FDIC field renames, CBOE column drift, FINRA CDN removal, Pink Sheet URL
  rotation, Incapsula WAFs). When a pipeline breaks: check the API/site changed FIRST,
  before suspecting the code — and never "fix" a scraper by loosening validation.
- **Respect the ruled-out list in CLAUDE.md.** Each dead end (Baker Hughes, AAR, Stooq,
  Motley Fool, nasdaq-data-link WAF) cost real time; don't re-attempt without a genuinely
  new access path. Do not attempt WAF/bot-detection bypasses — that boundary was chosen
  deliberately.
- **Storage sizing gates the Schwab full price backfill** — deferred by Zander's explicit
  choice, not oversight. Ask before pulling.
- **`.env` handling:** the repo .env is the live key store here; the D:-drive master .env
  is Zander-managed — always ask before touching either beyond reading key names.

## Cross-repo synergy (easy to miss)

`custom_index_tool`'s earnings-call verbosity study needs an *independent* "bad news"
label per (ticker, quarter). This repo already has it: `earnings_surprise` analytics
(Finnhub) and price reaction via `event_backtest.earnings_events`. Joining the two repos'
data makes the NLP study's methodology sound instead of circular. See
`custom_index_tool/EXPERT_BRIEF.md`.
