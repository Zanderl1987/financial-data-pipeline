# Real yields, not just breakeven inflation, explain gold's Hormuz-event underperformance

**Repo:** financial-data-pipeline · **Date:** 2026-08-08 · **Status:** concluded (descriptive, follow-up to `2026-08-07_hormuz-gold-oil-event-study.md`)

## Claim

Real 10-year Treasury yields (nominal `DGS10` minus 10Y breakeven inflation `T10YIE`) move
against gold's 10-day post-event return with about the same strength as oil does
(**r = -0.401** vs. the prior writeup's oil correlation of **r = -0.420**, both n=12, same
event set). Decomposing the real-yield move: the **nominal-rate leg correlates more strongly
with gold (r = -0.328) than the breakeven-inflation leg alone (r = -0.198)** — the "oil spike →
inflation expectations → headwind for gold" story from the prior writeup is directionally real
but incomplete; rising *nominal* rates (consistent with fiscal/war-spending concerns or
Fed-hawkishness expectations, not purely inflation breakevens) carry at least as much of the
effect. In the 2026 crisis specifically, the real yield rose **monotonically** through every
major escalation date tracked — 1.72% (pre-war) → 1.80% (Strait closed) → 1.88% (US campaign to
reopen) → 2.23% (June 17 ceasefire MOU), a **+51bp** move — while gold fell over the same
stretch. Still n=12, still correlational, not causal.

## Motivation

The `2026-08-07_hormuz-gold-oil-event-study.md` writeup found gold moving opposite to oil across
12 Iran/Hormuz flashpoints and proposed (without testing directly) that the mechanism runs
through inflation expectations pushing up real yields, which raises the opportunity cost of
holding non-yielding gold. That writeup's "Decision & next step" section named this as the
concrete follow-up: pull actual rate/breakeven series into the same windows instead of inferring
the mechanism from the gold/oil sign flip alone.

## Data

- **Real yield, nominal yield, breakeven inflation:**
  `storage/curated/fred_rates_gdp_interest_rates/fred_rates_gdp_interest_rates.parquet`, FRED
  series `DGS10` (10-Year Treasury Constant Maturity, 1962-01-02 to 2026-07-10) and `T10YIE`
  (10-Year Breakeven Inflation Rate, 2003-01-02 to 2026-07-13). `real10y = DGS10 - T10YIE`,
  computed after an inner join on date (both series publish on the same FRED business-day
  calendar, so no fill/interpolation was needed — an inner join drops only the small number of
  dates where either series is independently missing).
- **Gold:** same `GC=F` continuous futures series used in the 2026-08-07 writeup, for the
  10-day cumulative return used in the correlation.
- **Events:** the identical 12-event set from the prior writeup (2011-12 Hormuz threat, 2019
  tanker war, Jan 2020 Soleimani/Ain al-Asad, 2024 Israel-Iran direct strikes ×4, and the 2026
  crisis's first three escalation dates). `T10YIE` starts 2003-01-02, which still covers all 12
  (earliest event 2011-12-27).
- **Known gap:** same as the prior writeup — this stops at the pipeline's last refresh
  (`DGS10`/`T10YIE` through mid-July 2026), so it doesn't cover the Aug 2026 deal-progress
  period at all.

## Method

1. **Level changes, not returns.** Rates and spreads can cross zero, so a percent-change
   formula (used for gold/oil prices) doesn't apply. Instead: `chg_Nd_bp = (level at day0+N -
   level at prior close) * 100`, i.e. basis points moved from the same pre-event baseline
   convention as the price event study.
2. **Same anchor-date logic** as the 2026-08-07 script: each event's `day0` is its first
   available trading day at/after the nominal date, weekend events roll forward to the next
   business day, and `prior close` is the last observation strictly before `day0`.
3. **Decomposition.** Because `real10y` is defined as `DGS10 - T10YIE`, its basis-point change
   over any window is exactly `nominal10y_chg - breakeven10y_chg` by construction (verified
   row-by-row in the script's output, e.g. 2026-03-02: nominal +26bp, breakeven +11bp, real
   +15bp = 26-11). Reporting all three separately isolates which leg is doing the work rather
   than treating "real yields" as a single opaque number.
4. **Statistics.** Same n=12, non-independent-events caveat as the prior writeup — three more
   Pearson correlations reported (gold vs. real/breakeven/nominal 10-day change), all
   descriptive, no significance claimed.

## Results

**Real yield, nominal yield, and breakeven inflation moves around the same 12 events** (bp
change vs. prior-close level):

| Event | Real10y d0 | Real10y 3d | Real10y 10d | Nominal10y 10d | Breakeven10y 10d |
|---|--:|--:|--:|--:|--:|
| 2011-12-27 Iran threatens Hormuz closure | +2.0 | -3.0 | -6.0 | -10.0 | -4.0 |
| 2019-06-20 Iran shoots down US drone | -9.0 | -3.0 | -1.0 | +1.0 | +2.0 |
| 2019-09-16 Abqaiq-Khurais attack | -6.0 | -5.0 | -8.0 | -22.0 | -14.0 |
| 2020-01-03 Soleimani killed | -5.0 | +4.0 | +1.0 | -4.0 | -5.0 |
| 2020-01-08 Iran retaliates (Ain al-Asad) | +3.0 | -1.0 | -5.0 | -9.0 | -4.0 |
| 2024-04-15 Iran attacks Israel directly | +9.0 | +13.0 | +11.0 | +13.0 | +2.0 |
| 2024-04-19 Israel retaliates | -3.0 | 0.0 | -9.0 | -14.0 | -5.0 |
| 2024-10-01 Iran missile barrage on Israel | -8.0 | +12.0 | +12.0 | +21.0 | +9.0 |
| 2024-10-28 Israel retaliates | +3.0 | -1.0 | +12.0 | +18.0 | +6.0 |
| 2026-03-02 US-Israel launch war on Iran | +4.0 | +10.0 | **+15.0** | **+26.0** | +11.0 |
| 2026-03-04 Iran declares Strait closed | +3.0 | +1.0 | +9.0 | +20.0 | +11.0 |
| 2026-03-19 US campaign to reopen Strait | +2.0 | +20.0 | +11.0 | +5.0 | -6.0 |

**Correlations with gold's 10-day cumulative return (n=12 each):**

| Series | Pearson r |
|---|--:|
| Real 10Y yield, 10d change (bp) | **-0.401** |
| Breakeven inflation, 10d change (bp) | -0.198 |
| Nominal 10Y yield, 10d change (bp) | -0.328 |

**2026 crisis close-up — real yield rose at every escalation checkpoint tracked:**

| Date | Development | Real10y | Nominal (DGS10) | Breakeven (T10YIE) |
|---|---|--:|--:|--:|
| 2026-02-27 | Pre-war baseline | 1.72% | 3.97% | 2.25% |
| 2026-03-02 | War begins | 1.76% | 4.05% | 2.29% |
| 2026-03-04 | Iran declares Strait closed | 1.80% | 4.09% | 2.29% |
| 2026-03-19 | US campaign to reopen Strait | 1.88% | 4.25% | 2.37% |
| 2026-06-17 | Ceasefire MOU signed | 2.23% | 4.49% | 2.26% |

Two things stand out: (1) real yields climbed through the entire acute-war period, tracking
gold's decline over the same stretch; and (2) by the June ceasefire, breakeven inflation had
actually come back down to roughly its pre-war level (2.26% vs. 2.25%) — the entire +51bp real-
yield rise by that point was a **nominal-rate** move, not an inflation-expectations move. That
matches the correlation table: nominal (r=-0.328) dominates breakeven (r=-0.198) in explaining
gold's cross-event weakness.

## Limitations & threats to validity

- **Same n=12, non-independent-events caveat as the parent writeup** — carries over unchanged.
  Effective independent sample is closer to 5 crisis periods than 12 event-days.
- **Correlational, not causal, and no attempt to control for confounds.** Fed meeting
  calendars, other macro releases (payrolls, CPI prints), and the dollar all move real yields
  too and weren't controlled for — a rate move coinciding with a Hormuz event isn't proven to be
  *caused by* that event. The "war-spending/fiscal-hawkishness" read on the nominal-leg
  dominance is a plausible story consistent with the numbers, not a tested one.
- **`real10y = DGS10 - T10YIE` is a standard textbook proxy, not the market's actual TIPS real
  yield** (`DFII10`, which isn't in this pipeline's FRED table yet) — close in practice but not
  identical; if `DFII10` gets added to the pipeline this should be rerun against it directly.
- **Same data-currency gap as the parent writeup**: doesn't reach the Aug 2026 deal-progress
  period, and event selection is the same non-systematic manual list.

## Decision & next step

No signal or factor was built — this is still a research note answering "why does gold move
against oil in this event set," not a `analytics/signals.py` candidate. It does firm up the
mechanism claim in the parent writeup: the story should be stated as "rising real yields
(increasingly a nominal-rate story, not just inflation breakevens, as the 2026 crisis wore on)"
rather than "inflation expectations" alone. If this line of research continues: (1) swap in
`DFII10` (market TIPS real yield) if/when it's added to the pipeline instead of the constructed
proxy; (2) check whether the nominal-leg dominance in the 2026 crisis specifically ties to actual
Fed communications/meeting dates in that window (a real causal test would need event-free control
windows, which this hasn't attempted); (3) as before, if this becomes a repeated research pattern
rather than a one-off, migrate into `analytics/event_impact.py` with an entry-lag-aware driver
rather than continuing as ad hoc scripts per writeup.

## Reproduce

```
# from repo root, C:\ProgramData\anaconda3\python.exe on all commands
python experiments/2026-08-08_hormuz_gold_rates_mechanism.py
```

Requires `storage/curated/futures/futures.parquet` and
`storage/curated/fred_rates_gdp_interest_rates/fred_rates_gdp_interest_rates.parquet` present and
current (run `curated.py` first if stale). Depends on the same event-date list as
`experiments/2026-08-07_hormuz_gold_oil_event_study.py` (duplicated inline here rather than
imported, to keep each experiment script standalone and reproducible on its own).
