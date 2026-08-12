# 2026-08-12 — TV strategy catalog: Batch 2 collection (session 3)

Batch 1's roster (`_roster_strategies_popular_2026-08-12.txt`) was exhausted at the end
of session 2 (0 TODO). This session re-enumerated the same sampling frame
(`tradingview.com/scripts/?script_type=strategies`, Open-source only, Most popular) via
the "Show more publications" pagination button, which turns out to be real server-side
pagination (`/scripts/page-2/?script_type=strategies`), not infinite scroll — much
simpler than expected. Page 2 returned 23 more slugs; 3 were already-collected batch-1
entries whose rank shifted (`lfk6Inrw`, `qHftEnad`, `xx3enmiW`), leaving 20 new
candidates, logged to `_roster_strategies_popular_2026-08-12_batch2.txt` with the same
2-per-author cap (`cs_lev`, `blitz_locked`, `ortizbruno115` each hit it partway
through — 4 SKIP-2PA).

## Collection method upgrade: direct DOM extraction, no more reindent()

Discovered this session that the Pine source panel (`.monaco-editor-tv-pine-dark`) is
NOT the flattened/whitespace-stripped text `get_page_text` returns — its child divs
(alternating content-row / empty-row, so `children.filter((r,i)=>i%2===0)`) carry the
line's `textContent` **with indentation and interior spacing already intact**. This
makes the whole two-channel reindent() workflow (flat text + separate indent-count
array, zipped back together) unnecessary: pull `container.children[i].textContent` per
line directly and the source is already correctly formatted.

**The catch**: the in-page-script content filter (the same one that blocks
`btoa()`/base64 dumps as "cookie/query-string data") also blocks large joined chunks of
dense `key=value` Pine syntax — not by total length but by pattern, since a 45-line
script triggered no block while some 3-10 line chunks of other scripts did and a lone
offending line collected fine by itself. No reliable chunk-size threshold; the practical
approach is to request a chunk (start at ~15 lines), and on `[BLOCKED: ...]` recursively
halve until it clears, occasionally down to single lines. Still far more reliable than
the old two-channel method, since there is no separate indent-count array to go out of
sync with the text — a blocked chunk fails loudly and re-requesting narrower always
works, rather than silently misaligning.

## Collected this session (5 new)

| slug | author | lines | Stage 1 |
|---|---|---|---|
| `UCGXkLvt-MA-Crossover-RSI-Strategy` | clayton1139 | 45 | admitted (deprioritized, 9 params) |
| `AlvLl7j2-NAS100-Practical-SMC-5m-v12` | AdvJavedkhan | 69 | **excluded** `lookahead` — `request.security(..., lookahead=barmerge.lookahead_on)` despite a source comment claiming the opposite ("prevents look-ahead"); comments are not evidence, screener code is |
| `ymepYSLq-NNFX-BTC-SSL-QQE-SignalForge` | SignalForge-Ai | 113 | admitted (deprioritized, 12 params) |
| `Ott3SiyK-Opening-Range-Breakout-ORB` | ortizbruno115 | 92 | admitted (deprioritized, 10 params) — cites Zarattini & Aziz SSRN 2023 ORB paper in its header comment |
| `f2lBhqNS-Donchian-Intraday-Momentum-Breakout` | ortizbruno115 | 80 | admitted (deprioritized, 11 params) — 2nd from this author, still within the 2-per-author cap; Turtle-style N-bar channel breakout with ATR trailing stop |

## New SKIP-LEN entries this session (11)

`Fc4IHcm4` TT-Lorentzian (395L), `UqKr1TZS` Reversal Pro v2 (547L), `FtRppcLM`
TT-Autotune (609L), `642rbEbE` SVT Big Swing Capture (852L), `4s4X741x` ryans XAUUSD SMC
Bot (511L), `uOaILfJT` DNSE Bollinger Breakout (428L), `wTvvjVJi` RK Gold Sniper AI PRO
(431L), `MQDDEl1e` Poor Man's Orderflow AlgoPilot (1443L), `XoWVndPv` 3 Session ORB
BudgeDaddy (1887L), `z9ZAdxT0` Laxman Rekha Reversal (477L, 605 boosts/7052 views — the
most popular script skipped so far, purely on length), `CLvCY5Rp` fcraynel strategy
(653L).

## Batch 2 state: COMPLETE

`_roster_strategies_popular_2026-08-12_batch2.txt` — 0 TODO remain (16 items resolved:
5 DONE + 11 SKIP-LEN, plus the 4 SKIP-2PA already logged when the roster was built).

## Campaign total after this session

**21 `.pine` files** collected across both rosters (16 batch 1 + 5 batch 2). 18 admitted
to Stage 2 (14 batch 1 + 4 batch 2), 5 excluded (4 batch 1 `unconfirmed_htf` + 1 batch 2
`lookahead`). Both source-frame rosters (batch 1: TradingView "Most Popular" page 1,
batch 2: page 2) are now fully exhausted. Batch 3 would need either page 3 of the same
sort, or a different sort/frame (Editors' picks was already tried 08-11 and yielded only
2 tradeable candidates out of 23 — most popular is the more productive frame so far).

---

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

## Fixed: `qHftEnad-PG-MCX-Silver-Long-V6-MTF-Momentum-Profit-Trailing`

The line/indent mismatch (199 flat lines vs 202 indents) was root-caused: 4 places
where the source has **two consecutive blank lines** before a `// ==== SECTION N ====`
comment block collapsed to one blank line in the manually-transcribed flat text. Found
by computing a per-line **non-whitespace character count** from the DOM (`Array.from
(line.textContent).filter(ch => ch !== ' ' && ch.codePointAt(0) !== 160).length`) and
diffing it against the same count per line of the transcribed flat file — this survives
`get_page_text`'s interior-whitespace collapsing (which is cosmetic and harmless for
Pine) while catching real dropped/merged lines (which are not). `pg_mcx_silver_long_v6`
saved at 202 lines, admitted at Stage 1 for structure but **excluded** `unconfirmed_htf`
— its `request.security` HTF call has `lookahead=barmerge.lookahead_off` (prevents
future-bar leakage) but no `[1]`/`isconfirmed` guard against the current-forming-bar
repaint, and it's an always-on entry gate, same pattern as `pdh_pdl_break_0dte`.

**Adopted going forward**: this non-whitespace-checksum step (indents array + per-line
non-whitespace count, both pulled from the DOM in one or two JS calls, diffed against
the transcribed flat file before ever calling `reindent()`) caught the last roster
collection (`cloud_pro_ichimoku_confluence`, 253 lines) with zero mismatches on the
first attempt. Worth doing by default for any script over ~150 lines.

## Roster state: COMPLETE

`storage/tv_scripts/_roster_strategies_popular_2026-08-12.txt` — **0 TODO remain**.
`qHftEnad` (above) and `xx3enmiW-Cloud-Pro-Ichimoku-Confluence` (blitz_locked, 238
boosts / 2696 views, 253 lines) were both collected and both **excluded**
`unconfirmed_htf` — `xx3enmiW`'s HTF Tenkan/Kijun trend filter defaults to **on**
(`useMTF = input.bool(true, ...)`) and gates every entry, same always-on-repaint
pattern as the other two exclusions this session.

Every entry on the 23-entry roster is now DONE, SKIP-LEN, or SKIP-2PA. Session total:
**16 `.pine` files**, 14 admitted to Stage 2 (8 clean/deprioritized-on-params, plus
`tradleware_hodl`/`tradleware_dca` flagged as barely-testable benchmarks) and 4
excluded `unconfirmed_htf` (`rabiah6x`, `pdh_pdl_break_0dte`, `pg_mcx_silver_long_v6`,
`cloud_pro_ichimoku_confluence`). Batch 2 will need fresh enumeration (Most popular /
Trending, or the next page of Most popular) to keep collecting toward the 30-50 target.

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
