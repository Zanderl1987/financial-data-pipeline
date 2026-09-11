# California Form 700 holdings-disclosure event study — NULL RESULT

**Date:** 2026-09-11
**Script:** `experiments/california_disclosures_event_study.py`
**Data:** `california_disclosures` (Schedule A-1, 4,876 rows, 2018-2026 filings)

## Question

Does the market move when a CA state legislator (Assembly/Senate) or staff first
discloses a stock holding on Form 700? If it does, disclosure dates are a tradeable
signal and belong in `signal_panel()`.

## Design

- **Events keyed on `filed_date` (the public disclosure date), never `acquired_date`.**
  Acquisition happens before filing by an unknown interval; keying off it is
  look-ahead — the error that reversed `oil_shock` to null on 2026-07-07.
- **`entry_lag=1`.** A filing is only known to have landed *sometime* that day, so
  day 0 is not tradeable; the earliest honest fill is the next close.
- **`price_table="prices"` pinned**, `benchmark=SPY`, window (-10, 63), one
  single-call `event_study` — the wide-universe pattern from the congressional study.
- **Common stocks only** (`nature_of_investment` ~ "Stock"), which the CA source
  reports on Schedule A-1.
- **Name → ticker resolution** (`name_to_ticker.NameResolver`): Form 700 discloses
  entity names, not tickers. `business_entity` is matched against the `securities`
  reference table (normalized exact + significant-token tiers, `require_listed=True`
  so junk legacy listings like DUUKU cannot collide) plus a curated canonical table
  for ADRs of foreign blue chips and split/rename collisions. Coverage on the Stock
  subset: **671 / 1,149 rows (58%)**, 364 distinct entities.
- **"First appearance" is the news.** Annual filings re-report the same holdings every
  year, so only a (filer, symbol) pair's earliest `filed_date` counts as a disclosure
  of new information. Dedupe: same-symbol same-day disclosures pool into one event
  with `n_filings`/`n_filers` counts.
- **Significance is date-level**: one mean CAR per disclosure date, two-tailed t-test
  across dates, Benjamini-Hochberg across horizons (same-day disclosures are not
  independent draws, so the pooled t-stat is optimistic).

## Result

448 first-appearance (symbol, filed_date) events across 254 symbols; 385 aligned to
the `prices` store (61 had no price coverage at the event date).

**DATE-LEVEL (the number to believe):**

| Horizon | n_dates | mean% | t | p_value | p_adj | significant |
|---|---|---|---|---|---|---|
| 1 | 57 | -0.17 | -0.63 | 0.534 | 0.801 | no |
| 3 | 57 | +0.04 | 0.11 | 0.913 | 0.913 | no |
| 5 | 57 | +0.13 | 0.27 | 0.786 | 0.913 | no |
| 10 | 57 | -0.34 | -0.78 | 0.439 | 0.801 | no |
| 21 | 57 | +2.03 | 1.44 | 0.156 | 0.801 | no |
| 63 | 57 | +8.59 | 1.09 | 0.280 | 0.801 | no |

**VARDIOR — NULL RESULT.** No horizon survives the date-level test (best p_adj = 0.80).
Nothing here is wired into `signal_panel()`.

## Caveats

- **Small disclosure-day corpus**: 57 distinct disclosure dates drive ~385 events;
  several events are the pooled filings of many filers on one day. The date-level
  test is honest but low-powered.
- **Resolution coverage ~58%** of Stock rows; unresolved rows are overwhelmingly
  foreign companies without a US listing (not actionable for a US-price event study)
  and private/LLC vehicles. `securities` is a current snapshot; a name that changed
  since filing (renames, ticker reuse) could misalign, and `require_listed` is the
  guard, not a cure.
- **Secretary-of-State-style reformulations**: the CA filings are OCR/auto-extracted;
  residue like "Apple Inc (AAPL)" and founder vehicles still pass through.
- The 63-day pooled mean (+35%) with a -4% median is the tail of a few high-vol names
  (e.g., speculative biotechs), not a signal — exactly what the date-level stats
  discount.

## Reproduce

    C:\ProgramData\anaconda3\python.exe experiments\california_disclosures_event_study.py
    C:\ProgramData\anaconda3\python.exe experiments\california_disclosures_event_study.py --min-gap-days 5

## What would change the answer

- **Better resolution** (higher-coverage securities universe, historical names):
  more events, but the pattern here (flat 0-10d, null date-level significance) is
  unlikely to flip by adding more common stocks.
- **Different event definition**: e.g., flagging amended filings that ADD a holding
  mid-term (recent purchases) rather than any first appearance; or subsetting to
  filers holding committee/leadership positions.
- **A real market-reaction channel**: a legislator's disclosed holding only becomes
  tradable if the market can trade it and cares — most of these names move on company
  fundamentals, not on Sacramento ethics forms.

**Design note for A1:** the name→ticker resolver (`name_to_ticker.py`) is committed
as a durable module and winds leakage_healthcheck's new `california_disclosures_holding`
roster entry — the null study itself is the "zero evaluation" milestone for the
backfilled A-1 table.