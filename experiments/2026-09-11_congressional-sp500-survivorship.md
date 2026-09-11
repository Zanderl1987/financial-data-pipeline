# Congressional disclosure study — SP500 point-in-time survivorship check

**Date:** 2026-09-11
**Script:** `experiments/congressional_disclosure_event_study.py --sp500-only`
**Data:** `congressional_trades` (54,966 rows full backfill) → 27,102 (symbol, disclosure_date, side) events

## Question

Does restricting to symbols that were **actually S&P 500 members on their
disclosure date** change the NULL result of the 2026-08-28 congressional
disclosure study? If the full-universe result were biased by delisted
non-members (survivorship) or diluted by small-caps that don't move on
disclosure, the point-in-time member universe could show a signal.

## Design

- **Point-in-time membership** via `historical_constituents()` semantics:
  the `sp500_membership` changelog (reconstructed from Wikipedia changes)
  gives intervals `start_date`–`end_date` per symbol. An event at
  `disclosure_date` is kept iff `start_date <= disclosure_date < end_date`
  (both bounds open when NULL, matching the as-of query the membership
  pipeline's tests assert). Current `is_sp500` flag would be look-ahead —
  this filter uses the historical log.
- `--sp500-only` flag filters events after `build_events()` and before the
  close-matrix build. Injected membership frame for unit tests.
- **Event study identical otherwise:** `entry_lag=1`, `benchmark=SPY`,
  window (-10, 63), `price_table="prices"`, pooled + date-level BH stats.

## Bug fixed en route

The first filter implementation (`how="left"` merge) treated symbols with
**no membership row at all** as open-bounded members (NaT `<= date` is
False, but `start_date.isna()` is True, so the OR passed). Fixed by
adding `indicator=True` and requiring `_merge == "both"` — only symbols
with an actual row in the changelog can pass.

## Result

| Metric | Full universe (2026-08-28) | SP500 PIT (2026-09-11) |
|---|---|---|
| Events pre-alignment | 27,102 | 16,682 (61.6%) |
| Aligned events | 23,184 | 15,304 |
| BUY aligned / dates | 11,007 / 1,528 | 7,394 / 1,267 |
| SELL aligned / dates | 12,177 / 1,485 | 7,910 / 1,254 |
| Smallest BUY p_adj | 0.61 | 0.458 (h3) |
| Smallest SELL p_adj | 0.61 | 0.355 (h10) |
| Date-level verdict | NULL | NULL |

**The SP500 PIT universe is 61.6% of the full event set** — congressional
trades are already heavily concentrated in index names. The date-level
p-values remain nowhere near significance (smallest p_adj 0.355 vs the
original 0.61). No horizon survives in either direction.

**Conclusion:** The congressional NULL result is **robust to
survivorship-bias correction via point-in-time index membership**.
Nothing here wires into `signal_panel()`.

## Caveats

- Membership floor is the Wikipedia log's 1976-07-01 start; all
  congressional disclosures post-date this.
- The `sp500_membership` table has 885 unique symbols over its history —
  some names enter/exit multiple times (e.g., AAL 2015→2024). Events
  outside those windows are correctly dropped.
- The price store `prices` is itself a survivor set (current symbols
  with history); this filter only addresses *index-inclusion* bias,
  not the delisting bias that `prices` already absorbs (a separate
  question for another study).

## Reproduce

    C:\ProgramData\anaconda3\python.exe experiments\congressional_disclosure_event_study.py --sp500-only

## What would change the answer

- A real signal concentrated **exclusively** in the 38% of events from
  non-S&P names (the ones this filter drops). The date-level stats on
  the full universe already show nothing, so this would require a
  perfectly offsetting pattern — unlikely.
- A different membership definition (e.g., Russell 3000 PIT membership)
  if the signal is in mid-caps. But the original universe already
  contains them and shows nothing.