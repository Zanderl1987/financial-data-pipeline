# 2026-08-12 — TV strategy catalog: Batch 1 collection (session 2)

Continuation of the 2026-08-12 session 1 notes below (kept, not overwritten — session 1's
content follows after the `---`). All work is still Stage 0/1 only — collection and
source screening. **No strategy has been translated and no endpoint has been computed.**

## Collected this session (8 new)

`tradingview.com/scripts/?script_type=strategies`, Open-source only, Most popular —
same roster started 08-12 session 1, worked down from where it left off.

| slug | author | lines | Stage 1 |
|---|---|---|---|
| `mrr_mean_reversion_range` (1FgHp6Cv) | abdulrehmantatvacare | 121 | admitted (deprioritized, 11 params) |
| `elite_hybrid_orb_artillery` (SPvazS2g) | ArtilleryTrades | 216 | admitted (deprioritized, 15 params) |
| `pdh_pdl_break_0dte` (9o35mG93) | samirdave1992 | 147 | **excluded** `unconfirmed_htf` — H1 EMA via `request.security` with no `[1]`/`isconfirmed` guard, and it's an always-on entry gate (not a default-off filter like the 08-11 `rabiah6x` case), so it stays excluded |
| `bist30_sp500_atr_momentum_rider` (jd1KSVn7) | newton61 | 159 | admitted (deprioritized, 12 params) |
| `fvg_bos_confirmation` (jyhTizLX) | AIScripts | 101 | admitted (5 params, clean) |
| `tradleware_hodl` (wgsvzsT3) | cs_lev | 45 | admitted — **flag**: single-trade buy-and-hold benchmark, not really comparable under the campaign's `pnl_p` permutation test (relocating one entry per symbol is close to a no-op); worth a campaign-level discussion before Stage 3 |
| `tradleware_dca` (OmrCq7H3) | cs_lev | 87 | admitted (deprioritized, 10 params) — same buy-only-benchmark flag as HODL |
| `ema_fib_confluence_3targets` (diZ8Oes6) | alanshospitality | 104 | admitted (deprioritized, 11 params) |

cs_lev is now at the 2-per-author cap (HODL + DCA); the roster's `71yKm18B` (Ichimoku,
3rd by this author) stays `SKIP-2PA`.

Directory total is now **14 `.pine` files** (13 with `.meta.json`), 12 admitted to
Stage 2, 2 excluded.

## New SKIP-LEN entries this session

| slug | author | lines |
|---|---|---|
| `kYJLrfun` ORB Laboratory v1.0.12 | DominicFerri | 1209 |
| `VFP7QkqV` DNSE SMA 34/89 Dual Slope | DNSE | 389 |
| `U2DYOXw6` "Thinh" | zqzmvtonjz2567 | 886 |
| `fgESdFcb` Auto Pattern Detector Targets | zqzmvtonjz2567 | 886 |
| `lfk6Inrw` Cash Account Portfolio Trend System v2 | junseok_bong | 345 |

Line counts are read directly from the DOM (`monaco-editor` child count / 2) before
committing to the two-channel extraction, so the SKIP-LEN calls above cost one JS probe
each, not a full collection attempt.

## In-progress / left broken: `qHftEnad-PG-MCX-Silver-Long-V6-MTF-Momentum-Profit-Trailing`

Started but **not saved** — `reindent()` raised a line/indent mismatch (199 flat lines
vs 202 JS-reported indent values) and the partial flat-text scratch file was already
deleted by the follow-up `rm` before the mismatch could be debugged. The tab is still
open on this script's Source code panel. Next session: re-run both extraction channels
fresh (don't reuse this session's transcribed flat text — that's the likely error
source, this is a 202-line script with several multi-line expressions, e.g. the
`(not requireTwoCloses or ...) and` continuation lines, which are the easiest place to
accidentally merge or split a line by hand) and diff line counts before calling
`reindent()`.

## Roster state

`storage/tv_scripts/_roster_strategies_popular_2026-08-12.txt` — 2 TODO remain:
`qHftEnad` (above, needs a clean recollection) and `xx3enmiW-Cloud-Pro-Ichimoku-
Confluence` (blitz_locked, 2nd for this author, still within cap). Everything else on
the 23-entry roster is DONE, SKIP-LEN, or SKIP-2PA — this roster is nearly exhausted
and Batch 2 will need fresh enumeration (Most popular / Trending, or the next page of
Most popular) to keep collecting toward the 30-50 target.

## Other changes this session

- `storage/tv_scripts/INDICATORS_TO_WRITE_STRATEGIES.md` created — tracks
  `boosted_moving_average.pine` (an `indicator()`, not a `strategy()`, collected before
  the `.meta.json` provenance protocol existed, so its `tv_url`/`tv_author` are
  unrecoverable without re-finding the source page). It fails Stage 1 (`no_entry`) as-is
  and can't enter the pre-registered campaign; parked here as a candidate for someone to
  write explicit entry/exit rules around later, rather than deleted.
- Unrelated to the TV catalog: `C:\Users\zande\Cyclical Trading Backtester.ipynb`
  (a separate, older, standalone seasonality-backtest notebook, not part of this repo)
  had a real bug fixed this session — `backtest_strategy()` was writing trade-return
  columns to the first N rows of the frame by position (`df.index[:len(returns)]`)
  instead of to the actual trade rows, so every result showed NaN. Rewritten to return
  one row per actual trade; verified against a synthetic 5-year series (5 trades, all
  returns populated, cumulative return monotonic). Not executed end-to-end in the
  notebook itself — this machine's `jupyter nbconvert` is broken
  (`lxml.html.clean` import error, unrelated to the fix) — so the notebook's own SPX
  cell outputs are still stale until re-run by hand.

## State

All of `strategies/`, `storage/tv_scripts/`, `tests/test_strategy_screen.py`,
`SESSION_NOTES_2026-08-12_tv-catalog.md`, and the pre-registration are still
**untracked/uncommitted** in git — nothing from this campaign has been committed yet.

---

# 2026-08-12 — TV strategy catalog: Batch 1 collection (session 1)

Continuation of the 2026-08-11 campaign (pre-registration:
`experiments/2026-08-11_tv-strategy-catalog-preregistration.md`). All work below is
Stage 0/1 only — collection and source screening. **No strategy has been translated
and no endpoint has been computed.**

## Collected this session (4 new, all from the amended priority-1 frame)

`tradingview.com/scripts/?script_type=strategies`, Open-source only, Most popular.
The listing's filters had persisted from the 08-11 session.

| slug | author | boosts | lines | Stage 1 |
|---|---|---|---|---|
| `rabiah6x_ut_bot_scalper` | Soheibhussen357 | 22 | 141 | **excluded** `unconfirmed_htf` |
| `supertrend_entry_tp123` | jlockhart1316 | 18 | 257 | admitted (28 params) |
| `vegas_channel_tunnel_v11` | yubinzhang802 | 10 | 225 | admitted (26 params) |
| `hybrid_breakout_vcp` | blitz_locked | 23 | 115 | admitted (19 params) |

Directory total is now 6 `.pine` files, 5 admitted to Stage 2.

The one exclusion is a true positive but worth a hand-check before it is final: the
script's `request.security` calls have no `[1]` offset and no `barstate.isconfirmed`
guard, so they repaint — but they feed an HTF filter that is **off by default**
(`htfFilterOn = input.bool(false, ...)`). Under author defaults the repainting path is
not reached. Per the pre-registration the screener errs toward over-exclusion, so it
stays excluded unless the campaign later decides default-off filters do not count.

## Roster

`storage/tv_scripts/_roster_strategies_popular_2026-08-12.txt` — the full 23-entry
listing with per-slug status (DONE / TODO / SKIP-LEN / SKIP-2PA). 13 remain TODO, which
is enough to finish Batch 1 and start Batch 2 without re-enumerating the site.

## New pre-registration amendment: collection-size limit

Appended to the pre-registration as amendment 2026-08-12. Pine source can only leave a
script page through the two-channel workaround, and both channels pass through the
collecting session's context, so collection cost scales with source length. Scripts over
roughly 300 lines are now excluded as `SKIP-LEN` and logged by slug. First case:
`mikVFwAu-Alpha-S-R-Channel-Strategy` (801 lines).

Two escape hatches were tested and closed this session:
- **Base64 out of the page**: `btoa(...)` returns are rejected outright
  (`[BLOCKED: Base64 encoded data]`), so the indentation-preserving channel still cannot
  carry the source itself.
- **Plain HTTP fetch**: `requests.get()` on a script page returns 200 / ~518 KB of HTML
  with **no** `@version` anywhere — the source is client-rendered, so there is no
  server-side shortcut around the browser.

One useful correction to the 08-11 notes: the source block is **not** a live Monaco
editor (`.view-line` count is 0), only a static block carrying Monaco's theme class.
It is therefore not virtualized, and the whole file is present in the DOM at once — no
scrolling or chunking is needed regardless of length.

## Open provenance gap

`boosted_moving_average.pine` has no `.meta.json` beside it, so it has no recorded
`tv_url`/`tv_author`/`collected_at`. It also is not from the strategies frame — it is
an `indicator()`, `@version=5`. It must either get provenance re-collected or be
dropped before the catalog is assembled; it cannot enter a pre-registered campaign
without a source record.

## State

All of `strategies/`, `storage/tv_scripts/`, `tests/test_strategy_screen.py`, and the
pre-registration are still **untracked** in git — nothing from this campaign has been
committed yet.
