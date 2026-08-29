# Stage 2 translation exclusions

`strategies.screen.screen_source()` only pattern-matches Pine syntax (repaint
guards, entry/exit vocabulary, mechanism-family keywords) -- it cannot catch a
domain mismatch (session-gated intraday logic against this repo's daily-bar
equity panel) or a problem only visible once someone actually sits down to
write the `TradeRule` port. These 4 slugs collected a `.pine` file and passed
the automated Stage 1 screen (`admitted_slugs()` would otherwise list them),
but were excluded by hand during the 2026-08-28 Stage 2 translation push,
before any port was written. `strategies/stage3.py`'s `MANUAL_OVERRIDE_EXCLUDE`
enforces this in code so `admitted_slugs()` -- the function `catalog.py` and
the campaign's FDR family size both depend on -- does not perpetually list
them as "admitted, awaiting Stage 2."

| slug | reason | detail |
|---|---|---|
| `boosted_moving_average` | `no_provenance` | Collected before the `.meta.json` provenance protocol was locked in -- no `tv_url`/`tv_author`/`collected_at` recorded, and the source itself is an `indicator()` with no author-specified entry/exit rule (see `INDICATORS_TO_WRITE_STRATEGIES.md`, which already flagged this). Cannot enter a pre-registered campaign without a source record. |
| `f2lbhqns_donchian_intraday_momentum_breakout` | `intraday_domain_mismatch` | Explicit `input.session("0930-1600", ...)` trading session, a force-flat window, and a hardcoded session timezone; author's own header comment: "Recommended chart: 5-min or 15-min, NQ/MNQ, CL, GC." Session-hours logic (`inSession`/`inFlat`) has no meaning against this repo's daily OHLCV bars -- there is no intrabar session structure to gate on. Same class of mismatch the collection-time SKIP-INTRADAY convention applied to dozens of other candidates; this one slipped through because it was collected in an early batch (2026-08-12) before that convention was systematic, and the automated screen doesn't check for it. |
| `ott3siyk_opening_range_breakout_orb` | `intraday_domain_mismatch` | Same collection batch as the entry above (both carry a `// N) STRATEGY NAME` numbered-header comment, part of a small set of day-trading strategies collected together). "Opening Range Breakout" is inherently an intraday concept -- it requires an intraday opening range, which a daily bar does not have. Same `input.session`/force-flat-window pattern. |
| `tradleware_dca` | `engine_incompatible_pyramiding` | `pyramiding=500`: the strategy's entire mechanism is deploying fixed tranches across many stacked entries over time (dollar-cost averaging) until `initial_capital` is exhausted. This repo's `TradeRule` contract and `evaluation/trades.py` engine are built around ONE position at a time per symbol -- collapsing a DCA accumulation strategy to a single approximate entry would misrepresent the tested mechanism, not translate it. (Same class of contract mismatch as the 3Commas grid/DCA-bot family SKIP-DOMAIN'd during collection -- see `_roster_strategies_popular_2026-08-28_batch8.txt` and earlier rosters.) The author's own sibling script, `tradleware_hodl.pine` (a single buy-and-hold trade), fits the one-position engine fine and WAS ported (`strategies/ports/tradleware_hodl.py`) -- only the multi-entry DCA variant is excluded. |
| `mzyk8jsg_gold_intraday_ema_bb_vwap_atr` | `intraday_domain_mismatch` | Title says "Gold Intraday"; uses `ta.vwap()` (resets per session, meaningless as a single daily-bar value) plus an explicit UTC-hour session filter (`sessionAsia`/`sessionLondon`/`sessionNY`, `hour(time, "UTC")`) and a per-weekday toggle. Both the VWAP concept and the session-hour gate require intrabar/session structure a daily OHLCV bar does not have. Found during the routine smallest-first pass over the remaining Stage 2 backlog (2026-08-29), before writing a port -- same pattern as the two intraday exclusions above, just collected later (2026-08-28 batch4-8) after the SKIP-INTRADAY collection convention already existed, so it should have been screened out at collection time and wasn't. |

`tradleware_hodl` and `tradleware_dca` were both flagged back at collection
time (2026-08-12 commit `eed8264`) as "barely testable under the pnl_p
permutation test" since a buy-and-hold baseline produces very few trades.
`tradleware_hodl` was ported anyway (single trade, `pnl_p` will have
essentially no statistical power but is still an honest -- if low-power --
record); `tradleware_dca` is excluded outright for the structural reason
above, independent of that testability concern.
