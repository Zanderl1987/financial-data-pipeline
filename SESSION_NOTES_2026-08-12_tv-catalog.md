# 2026-08-12 — TV strategy catalog: Batch 1 collection

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
