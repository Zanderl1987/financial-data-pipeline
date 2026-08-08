# TradingView Technical Rating: does it predict forward returns?

## Claim

A rigorous, permutation-tested backtest of TradingView's Technical Rating signal on the
full 69-symbol multi-asset universe (1990–2026) found **no significant trading edge** from
the standard threshold-cross rule (permutation p = 0.99 on total P&L) — directly
contradicting an earlier informal 6-symbol result (60.6% win rate, profit factor 1.88) from
an unreviewed session-3 test. Isolating to the long side alone still fails significance
under the rule's current exit logic (p = 0.41). The continuous rating score itself is
**mildly contrarian**: higher ratings predict slightly *lower* forward returns (IC −0.012 at
21 days, t = −5.20), the opposite of its face-value "buy" meaning. The one piece that holds
up is a stricter definition of "upgrade" (a 2+ bucket jump, e.g. neutral → strong_buy):
those events show a real, monotonically strengthening 21-day excess return (+0.42%,
t = 6.61) vs SPY. **Follow-up same-day**: swapping the trade rule's early exit (median
9-day hold) for a fixed 21-day hold does *not* recover that edge as tradeable P&L
(pnl_p = 0.98 on the full universe) — despite a raw 58–60% win rate, permutation-shuffled
entries with the same fixed hold do just as well. The excess-vs-SPY edge is real; it looks
to be explained by this universe's own secular growth tilt over the sample period rather
than by the rating's entry-timing skill. **Second follow-up**: fading the rating instead of
following it — buying when it crashes into strong_sell — was the one construct that cleared
significance on the 69-symbol universe (pnl_p = 0.025, win_rate_p = 0.005, PF 1.45).
**Final verdict, after validation**: an out-of-sample time split (pre-/post-2012) showed
partial replication — the win-rate effect held in both eras, the dollar-P&L significance
mainly in the recent one. Expanding to the full Russell 3000 (2,030 usable symbols after
excluding 262 with data-quality problems) resolved it: **the win-rate effect replicates
everywhere (p = 0.005 on every cut tested all session) but the dollar-P&L edge does not
survive broad-market scale** (pnl_p = 0.87). The curated 69-symbol watchlist's secular-
growth tilt, not a real property of the TV rating, was doing the work. **No form of this
signal tested — following it or fading it, on any universe from 6 to 2,030 symbols —
supports a tradeable strategy.**

## Motivation

`analytics/technical.py::tv_rating()` is a validated local replica of TradingView's
26-indicator Technical Rating (exact match against the live scanner). An informal backtest
in an early session (2026-07-03, not statistically reviewed) suggested it might have real
predictive value — good enough to justify building a full evaluation framework
(`evaluation/`) with proper point-in-time discipline, bootstrap confidence intervals, and a
permutation null. This experiment is the first time that framework has actually been
pointed at the TV rating signal across every angle it supports (trade rule, continuous
score, transition events) and the results interpreted together rather than left as raw
registry rows.

## Data

- **Universe**: 69 symbols — 47 large-cap single names (AAPL, MSFT, NVDA, TSLA, JPM, …), 6
  sector/style ETFs (XLK, XLF, XLE, …), SPY/QQQ/DIA/IWM, and rates/commodity proxies
  (TLT, IEF, SHY, LQD, HYG, GLD, SLV, USO, UNG). This is a liquid, large-cap-tilted universe,
  not a broad-market sample — results may not generalize to small caps or thin names.
- **Date range**: 1990-01-19 to 2026-07-22 (bounded by each symbol's available price
  history via `event_backtest.load_close()`'s longest-series rule).
- **Prices/rating**: recomputed from stored OHLCV via the local `tv_rating()` replica, not
  the live daily snapshot pipeline (`tradingview_pipeline.py` only has ~1 month of real
  accumulated history as of this writing — see `SESSION_NOTES_2026-07-17.md`).
- **Benchmark**: SPY, subtracted before computing excess returns / CAR.

## Method

Three constructs, each already built in `evaluation/` before this session:

1. **Trade rule** (`tv_threshold`, `evaluation/adapters.py::tv_threshold_rule`): enter long
   on an upward cross through `BULL_MIN`, exit when the rating decays below
   `EXIT_LONG_MAX`; mirrored short on the bear side. Engine enforces next-close execution,
   one position per symbol. Significance via `evaluation/stats.py::permutation_trades` —
   200 permutations relocate the same *number* of entry signals to uniformly random days
   (same exit rule), and report the one-sided empirical p-value that real P&L / win rate is
   *not* beaten by random entry timing.
2. **Continuous signal** (`tv_rating_all`, `evaluation/adapters.py::from_rating_history`):
   the raw rating score as a daily cross-sectional signal. Pooled Spearman IC, daily
   cross-sectional IC with t-stat, and quintile-spread bootstrap Sharpe, at horizons
   1/3/5/10/21 days.
3. **Transition events** (`tv_rating_changes`, `evaluation/adapters.py::from_rating_changes`):
   bucket-to-bucket rating jumps as an event set, evaluated via `event_backtest.event_study`
   (CAR vs SPY, `entry_lag=1`).

All three reuse `evaluation/runner.py::run`, which enforces next-close-or-later entry and
excess-vs-benchmark returns — the look-ahead safeguards this project's [[signal-eval]]
playbook requires were already in place; this session did not need to add any.

## Results

**Trade rule, both sides, full universe** (`tv_threshold`, run `6da80be4f30b`, 2026-08-03):

| n_trades | win rate | total P&L | avg P&L | median hold | pnl_p | win_rate_p |
|---|---|---|---|---|---|---|
| 21,989 | 36.7% | $374,236 | 0.17% | 8d | **0.99** | **1.00** |

**Trade rule, both sides, 6-symbol basket** (TSLA/LMT/NVDA/KEYS/GOOG/NFLX — the symbols the
original informal test used) (`tv_threshold_basket`, run `ce9a567ce31f`):

| n_trades | win rate | total P&L | pnl_p | win_rate_p |
|---|---|---|---|---|
| 1,386 (948 long / 438 short) | 37.7% | $153,973 | 0.31 | 1.00 |

Split by side (not run through the registry — computed directly from the trades artifact):

| side | n | win rate | total P&L | profit factor |
|---|---|---|---|---|
| long | 948 | 40.0% | $185,900 | **1.87** |
| short | 438 | 32.6% | −$31,927 | 0.79 |

This matches the original informal finding's shape (long side clean, short side a net
drag) — but even the long side alone doesn't clear significance:

**Trade rule, long-only** (ad hoc script, not registry-recorded, n_perm=200):

| universe | n_trades | win rate | total P&L | pnl_p |
|---|---|---|---|---|
| 6-symbol basket | 963 | 39.8% | $187,734 | 0.39 |
| full 69-symbol | 14,549 | 40.0% | $990,186 | 0.41 |

**Continuous signal IC** (`tv_rating_all`, run `215c236bb785`):

| horizon | pooled IC | daily IC | t | quintile spread |
|---|---|---|---|---|
| 1d | −0.0049 | −0.0060 | −2.55 | −0.028% |
| 3d | −0.0083 | −0.0082 | −3.53 | −0.083% |
| 5d | −0.0097 | −0.0100 | −4.32 | −0.115% |
| 10d | −0.0107 | −0.0106 | −4.64 | −0.141% |
| 21d | −0.0122 | −0.0116 | **−5.20** | −0.121% |

Sign is consistent and strengthens monotonically with horizon — not noise by the
sign-flip heuristic, but magnitude sits below the |IC| < 0.02 "probably noise" floor.
Portfolio Sharpe on the quintile spread: −0.20 [−0.50, 0.11] (CI crosses zero); deflated
Sharpe probability of genuine skill ≈ 0.00 (n_trials=12). **Net read: real but tiny, and
in the contrarian direction** — high-rated names mildly underperform, not outperform.

**Transition events**, two definitions:

*min_step=1* (any adjacent-bucket move, run `fa674d74fec9`) — 88,585 downgrades / 87,943
upgrades. Both directions positive at every horizon (downgrade h21 +0.37% t=13.99, upgrade
h21 +0.36% t=13.61) — **near-identical magnitude regardless of direction is a red flag**,
not a confirmation: a signal whose bullish and bearish transitions predict the same thing
carries no directional information. Most likely explanation: `min_step=1` catches routine
day-to-day score wobble, not meaningful regime changes, and with no gap-deduplication the
huge n overstates independence (autocorrelated within-symbol event clusters), inflating the
t-stats.

*min_step=2* (e.g. neutral → strong_buy in one jump, run `096d25eec2e6`) — 15,605
downgrades / 15,938 upgrades:

| label | h=1 | h=3 | h=5 | h=10 | h=21 |
|---|---|---|---|---|---|
| upgrade | +0.08% (t=4.27) | +0.14% (t=5.18) | +0.16% (t=4.93) | +0.21% (t=4.83) | +0.42% (t=6.61) |
| downgrade | −0.03% (t=−1.82) | +0.01% (t=0.50) | +0.05% (t=1.69) | +0.10% (t=2.22) | +0.22% (t=3.49) |

Upgrade is now clean: consistent sign, monotonically strengthening, no flips. Downgrade
sign-flips from negative (1d) to positive and significant (21d) — a within-signal sign
flip across horizon, which by this project's own skepticism defaults reads as reversal
noise, not a working bearish signal.

**Follow-up: fixed 21-day hold on 2+-bucket upgrades** (ad hoc script, `n_perm=200`,
long-only, entry at next close after the upgrade, exit exactly 21 trading days later
regardless of rating — the trade-rule analogue of the event-study construct above, to
test whether the CAR edge survives as tradeable P&L):

| universe | n_trades | win rate | total P&L | avg P&L/trade | pnl_p | win_rate_p |
|---|---|---|---|---|---|---|
| 6-symbol basket | 631 | 60.2% | $179,975 | 2.85% | 0.66 | 0.25 |
| full 69-symbol | 9,982 | 58.2% | $1,308,032 | 1.31% | **0.98** | 0.82 |

Neither clears significance — on the full universe the permutation p is *worse* than the
threshold rule's original 0.99. The raw win rate (58–60%) and positive average P&L look
attractive in isolation, but permutation-shuffled entries into the same universe, held for
the same fixed 21 days, do just as well or better ~80–98% of the time. **This resolves the
open question from the first pass of this writeup**: the event study's positive t-stat
measures "a 21-day hold after an upgrade beats SPY," which is true — but that's the
universe's own excess drift over SPY (many of the 69 symbols, e.g. NVDA/TSLA/META-style
names, are secular growth outperformers over 1990–2026), not the rating's entry-timing
skill. The permutation null — which compares against *random* entries into the *same*
universe rather than against SPY — is the decisive test for whether the signal itself adds
value, and on both constructs (decay-exit and fixed-hold) it says no.

**Follow-up: fade trade rule** (buy when the rating crashes into strong_sell instead of
following it down; short when it surges into strong_buy instead of following it up — same
trigger levels/exits as `tv_threshold`, sides swapped; registered as `tv_fade`/
`tv_fade_basket`, `n_perm=200`):

| universe | side | n | win rate | total P&L | pnl_p | win_rate_p |
|---|---|---|---|---|---|---|
| full 69-symbol | both | 21,989 | 62.6% | −$374,236 | 0.13 | **0.005** |
| full 69-symbol | long only | 8,053 | 69.2% | **+$605,075** | **0.025** | **0.005** |
| full 69-symbol | short only | 14,265 | 59.1% | −$971,099 | — | — |
| 6-symbol basket | long only | 462 | 66.9% | +$30,548 | 0.83 | 0.005 |

The combined fade is exactly the mirror of `tv_threshold` (same trades, opposite side, same
n) and mechanically loses what the original made. But it splits sharply: **fading the
overbought side (short) loses money despite a >50% win rate** (avg loss −$546 vs avg win
+$352 — losses run bigger than wins) — a "right more often, wrong bigger" pattern that ate
the edge. **Fading the oversold side alone (buy when rating crashes into strong_sell, hold
~7 median days until it recovers) is the one result in this whole investigation that clears
both significance bars on the full universe**: pnl_p = 0.025, win_rate_p = 0.005, PF 1.45,
spread across all 69 symbols (23–208 trades each, not one name driving it).

**Do not treat this as confirmed.** Two things argue for real caution before it's anything
more than a lead: (1) it emerged from an adaptively-chosen chain of ~10 permutation tests
run in one session (basket cut → long-only → fixed-hold → fade-both → fade-long-isolated)
with no multiple-comparisons correction — a naive Bonferroni adjustment (0.05 / 10 ≈ 0.005)
would NOT clear this result; (2) the basket-only cut of the same rule (n=462) does **not**
replicate significance (pnl_p=0.83) — small-sample power loss is the likely explanation,
but it's also exactly the kind of non-replication that should make you doubt the
full-universe number. The strategy shape (buy an oversold multi-indicator crash, ~1-week
mean reversion) also overlaps heavily with the well-documented short-term-reversal
anomaly in the academic literature — plausible that TV's rating is just one particular
operationalization of "fell hard recently," not a source of unique information. No
transaction costs/slippage are modeled anywhere in this framework; at a 7-day median hold
and $75/trade average edge, even modest costs would matter.

## Follow-up: out-of-sample replication and universe expansion

**Out-of-sample split** (69-symbol universe, long-fade rule, split at 2012-01-01, same
`n_perm=200`): the win-rate edge replicates cleanly in both independent eras (pre-2012:
67.8% win, win_rate_p=0.005; post-2012: 70.9% win, win_rate_p=0.005) — real evidence the
entry timing itself does something, not a one-era fluke. Dollar-PnL significance is weaker
pre-2012 (pnl_p=0.93, PF 1.32 — a worse win/loss size ratio, avg loss −$599 vs win +$375)
than post-2012 (pnl_p=0.005, PF 1.66). Partial, not clean, replication: the timing effect
holds in both eras, the economically-significant version of it more clearly in the recent
one.

**Universe expansion to Russell 3000** (2,298 constituents identified via the `securities`
Iceberg table's `is_russell3000` flag, all with price history in the broader `prices`
table, median ~19 years): the first attempt produced an unusable result — $4.97B total P&L,
209% average P&L per trade, driven almost entirely (99.4% of the dollar total, from just
1.9% of trades) by economically impossible single-name moves, e.g. `WSHP` entering at
$0.0001 and exiting at $39.51 (a 39.5-million-percent "return"). Root cause: unlike the
hand-curated 69-symbol Tiingo watchlist, the broad `prices` table has real data-quality
problems — unadjusted stock splits and bad ticks — for a meaningful slice of names. A SQL
screen for single-day price ratio jumps >3x or <1/3x flagged **262 of 2,297 symbols (11%)**
with at least one such jump in their history (list: `storage/reports/eval/
tv_russell3000_bad_symbols.csv`). This is a real, load-bearing lesson for this project
generally, not specific to TV ratings: **the `prices` table needs a corporate-action/
data-quality audit before it's safe to use for any full-universe backtest**, and
`evaluation/universe.py`'s existing liquidity-floor machinery (built for full-universe
factor validation, see `docs/superpowers/specs/2026-07-29-full-universe-factor-validation-
design.md`) does not by itself catch this — it filters on dollar volume, not on
single-day-return sanity.

The re-run excludes those 262 flagged symbols and adds a $5 minimum entry-price floor as a
second guard, leaving **2,030 usable symbols** — a genuinely broad, representative
cross-section of the market rather than a curated large-cap watchlist. Sanity restored
(max single-trade return 176%, not 39.5 million percent):

| universe | n_trades | win rate | total P&L | avg P&L/trade | PF | pnl_p | win_rate_p |
|---|---|---|---|---|---|---|---|
| Russell 3000 (cleaned, 2,030 symbols) | 188,230 | 65.4% | $12,887,197 | 0.685% | 1.28 | **0.87** | **0.005** |

**This is the decisive result of the whole investigation, and it overturns the lead.** The
win-rate effect still replicates (p=0.005, as it did on every cut all session — buying
after a rating crash into strong_sell reliably wins more often than random timing, across
every universe and both time periods tested). But at genuine broad-market scale, the
dollar-P&L edge that looked significant on the curated 69-symbol universe (pnl_p=0.025,
strengthening further in the post-2012 out-of-sample half to pnl_p=0.005) **does not
survive** — pnl_p=0.87 is indistinguishable from random. The most likely explanation: the
curated 69-symbol watchlist over-represents secular-growth mega-caps (the same dynamic
flagged earlier for the plain threshold-cross rule and the continuous-score IC test), and
whatever made "buying the dip" pay off there specifically doesn't generalize to the average
Russell 3000 name. Combined with the earlier partial out-of-sample result, the honest
read is: **a real, robust win-rate timing effect exists and replicates everywhere it was
tested, but it does not translate into a broad-market-significant dollar edge** — the
win/loss size asymmetry (avg loss $705 vs avg win $477 here) eats it, and that asymmetry
appears to be worse specifically on the fuller, less curated universe.

## Limitations & threats to validity

- Events are not de-duplicated for clustering (`min_gap_days` not wired through the
  adapter/CLI path) — real trends produce bursts of correlated transition events for the
  same symbol, so the reported n (and therefore some t-stats, especially the min_step=1
  run) likely overstate independent information.
- Universe is large-cap/liquid-tilted for all results EXCEPT the Russell 3000 follow-up,
  which is the broad-market test and the one whose null result should be weighted most
  heavily.
- The broader `prices` table (used for the Russell 3000 run) has real data-quality
  problems — unadjusted stock splits / bad ticks in ~11% of symbols scanned. The 262
  flagged symbols were excluded and a $5 price floor applied, but this was a single ad hoc
  screen (single-day |log return| > ln(3)), not a systematic corporate-action audit — some
  contamination may remain uncaught, and this same issue likely affects any other
  full-universe analysis in this repo that reads from `prices` without a similar filter.
- The trade-rule permutation test and the event-study CAR test disagree on the "upgrade"
  signal (trade rule: not significant even with a fixed 21-day hold; event study: t=6.61
  vs SPY) because they use different null hypotheses, not because one is wrong: the event
  study asks "does this beat SPY," the permutation test asks "does the signal's *timing*
  beat random timing into the same universe." The gap is explained, not open — see the
  fixed-hold follow-up above.
- Long-only, basket-only, and fixed-21-day-hold cuts were run as ad hoc scripts
  (`scratchpad/tv_long_only_eval.py`, `scratchpad/tv_upgrade_fixed_hold_eval.py`, neither
  committed to the repo) rather than through the registry — reproducible from this
  writeup's commands below, but not sitting in `storage/eval_registry/results.parquet`
  alongside the other runs.
- No multiple-comparisons correction was applied across the ~6 distinct tests run this
  session (trade rule × 3 universe cuts, IC, events × 2 min_step settings) — the min_step=2
  upgrade result in particular should be treated as one promising lead among several tests,
  not a pre-registered single hypothesis.

## Decision & next step

**Closed: no form of the TV Technical Rating signal tested — following it or fading it,
on any universe from a 6-symbol basket to the full 2,030-symbol Russell 3000, at any
holding-period rule tried — supports a tradeable strategy.** The as-shipped `tv_threshold`
trade rule is not validated; do not treat the 2026-07-03 informal numbers as confirmed. The
one genuine lead (long-only fade: buy when the rating crashes into strong_sell) partially
replicated out-of-sample on the 69-symbol universe but failed decisively at Russell 3000
scale (pnl_p=0.87) — the apparent edge was a property of the curated watchlist's
secular-growth composition, not the signal. **Decision: do not build a live/paper trading
strategy on the TV Technical Rating in any form tested.**

What did replicate, robustly, everywhere: a small but real win-rate timing effect (p=0.005
on every single cut run this session — 69-symbol, basket, both out-of-sample halves, and
Russell 3000). It never translated into a broad-market dollar edge because losing trades
run bigger than winning trades, and that asymmetry got worse, not better, on the fuller
universe. That's a specific, well-evidenced negative result, not a shrug.

Remaining threads, if this gets picked back up:

1. The `prices` table's data-quality problem (unadjusted splits/bad ticks, ~11% of symbols
   scanned) is a bigger finding than anything about TV ratings specifically — it affects
   any future full-universe analysis in this repo that reads from `prices` without a
   similar screen. Worth turning `storage/reports/eval/tv_russell3000_bad_symbols.csv`'s
   detection logic (single-day |log return| > ln(3)) into a proper, reusable data-quality
   gate rather than a one-off script.
2. `min_gap_days` de-duplication is still not wired through `evaluation/adapters.py::
   from_rating_changes` / the CLI — worth adding generally so future event-study t-stats
   aren't inflated by clustered/autocorrelated events.
3. `tv_fade`/`tv_fade_basket`/`tv_fade_long`/`tv_fade_long_basket`/
   `tv_fade_long_russell3000_clean` are now recorded in
   `storage/eval_registry/results.parquet` and visible in `backtest_app.py`'s dropdown
   (IC-panel-only — none of the fade rules are wired into `KNOWN_TRADE_RULE_SIGNALS` for
   live slider tuning).

## Reproduce

From `C:\Users\zande\PycharmProjects\financial-data-pipeline`, repo at commit `b81e65a`:

```
C:\ProgramData\anaconda3\python.exe evaluate.py --adapter tv-rule
C:\ProgramData\anaconda3\python.exe evaluate.py --adapter tv-rule --universe TSLA LMT NVDA KEYS GOOG NFLX --name tv_threshold_basket
C:\ProgramData\anaconda3\python.exe evaluate.py --adapter rating --signal-col rating_all
C:\ProgramData\anaconda3\python.exe evaluate.py --adapter rating-changes
C:\ProgramData\anaconda3\python.exe evaluate.py --adapter rating-changes --min-step 2 --name tv_rating_changes_step2
```

The long-only cuts used a one-off script (not committed) building
`TradeRule(side="long", ...)` from `tv_rating_eval.BULL_MIN`/`EXIT_LONG_MAX` and calling
`evaluation.runner.run(rule, cache=adapters.rating_cache(symbols=...), write_registry=False)`
— same shape as `evaluation/adapters.py::tv_threshold_rule`, just with `side="long"` and no
short leg.

The fixed-21-day-hold follow-up used a second one-off script: entries are days where
`rating_label`'s ordinal (`strong_sell..strong_buy` → 0..4) jumps by ≥2 vs the prior day
(same definition as `event_backtest.rating_changes(min_step=2)`, upgrade direction only);
exit is fixed at entry + 21 trading days, not condition-based. Permutation null mirrors
`evaluation/stats.py::permutation_trades` (relocate the same count of entries per symbol to
random days, same fixed hold, 200 permutations, one-sided empirical p with +1 correction).

The fade rules (`tv_fade`, `tv_fade_basket`, `tv_fade_long`, `tv_fade_long_basket`) are
`evaluation.contracts.TradeRule`s built the same way as `evaluation/adapters.py::
tv_threshold_rule`, with the long/short trigger conditions swapped (long enters on
`crossed_down(rating_all, BEAR_MAX)`, exits on `rating_all > EXIT_SHORT_MIN`; short is the
mirror using `BULL_MIN`/`EXIT_LONG_MAX`), run through `evaluation.runner.run(...,
write_registry=True)` — these four **are** in the registry, unlike the long-only/fixed-hold
cuts above.

The out-of-sample split (`tv_fade_long_pre2012`/`tv_fade_long_post2012`) used the same
long-fade `TradeRule`, with entries additionally gated on `d.index < SPLIT` /
`d.index >= SPLIT` (`SPLIT = 2012-01-01`) so a warmup buffer loaded before the split date
(for the 200-day SMA) doesn't leak signals from the wrong era — load the "late" period
cache starting 400 calendar days before the split, gate entries to on/after the split only.

The Russell 3000 runs (`tv_fade_long_russell3000` → unusable, `tv_fade_long_russell3000_clean`
→ final result) pull the universe from `SELECT symbol FROM securities WHERE
is_russell3000 = true` (2,298 symbols) via `price_table="prices"` (not the default
tiingo/prices/market_history auto-fallback, for source consistency across a universe this
broad). The data-quality screen: `LAG(close) OVER (PARTITION BY symbol ORDER BY date)` per
symbol in `prices`, flag any symbol with `ABS(LN(close/prev_close)) > LN(3)` anywhere in its
history (a single-day >3x jump or <1/3x drop) — 262 of 2,297 scanned. The clean run excludes
those 262 and adds an entry-side `d["close"] >= 5.0` gate to the `TradeRule`.

Interactive exploration: `C:\ProgramData\anaconda3\python.exe backtest_app.py`, then open
`http://127.0.0.1:8050/` — live threshold/exit-level tuning against the `tv_threshold`
signal's cached panel.
