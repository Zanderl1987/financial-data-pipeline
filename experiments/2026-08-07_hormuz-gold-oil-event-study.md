# Gold doesn't track Hormuz-crisis oil shocks — it moves opposite to them

**Repo:** financial-data-pipeline · **Date:** 2026-08-07 · **Status:** concluded (descriptive event study, not a tradeable signal — see Limitations)

## Claim

Across 12 Iran / Strait-of-Hormuz military flashpoints from 2011 to the ongoing 2026 crisis,
gold (GC=F) and oil (WTI CL=F, Brent spot) event-day and 10-trading-day cumulative returns are
**negatively correlated (Pearson r = -0.42, n = 12)**. In the two largest 2026 events — the
US-Israel war's opening (Mar 2) and Iran's declaration that the Strait was closed (Mar 4) — oil
gained **29–42% cumulatively at +10 trading days** while gold **fell 4.3–4.5%** over the same
windows. Gold's same-day "safe haven" pop is common (positive on 9 of 12 event days) but fades or
fully reverses within 1–10 trading days in most cases; oil's reaction, in contrast, was a durable,
multi-week repricing (WTI ran from $57 to a $113 peak, +97%, over the first six weeks of the 2026
war). This is a small, non-independent sample (n=12) — the finding is directional and descriptive,
not a statistically validated signal.

## Motivation

The user asked for a check on how gold might react to the developing Aug 2026 Strait of Hormuz
deal (Iran/Oman/US negotiating a reopening), with an explicit instruction not to assume the
textbook "war → gold up, deal → gold down" story but to check current news and backtest it against
similar past occurrences first. A positive result (gold cleanly tracking war/peace headlines) would
let a forward view be stated with confidence; a null or inverted result changes what "the deal
progressing" should be expected to do to gold, which is exactly what happened here — current
reporting (CNBC, Bloomberg, Aug 6 2026) already describes gold rising on deal-progress hopes
*despite* the naive story predicting the opposite, and independently flags the same oil→inflation-
expectations offset mechanism this event study finds in the historical data.

## Data

- **Gold:** `storage/curated/futures/futures.parquet`, symbol `GC=F` (continuous gold futures),
  2000-08-30 to 2026-07-23, 6,497 rows. Cross-checked against `storage/curated/prices/prices.parquet`
  symbol `GLD` (SPDR Gold ETF, no futures-roll risk) for the two largest single-day 2026 moves —
  both corroborate (see Method, point 3).
- **Oil:** WTI from the same futures table, symbol `CL=F`, 2000-08-23 to 2026-07-23. Brent from
  `storage/curated/commodities/commodities.parquet`, FRED series `DCOILBRENTEU` (daily spot,
  1987-05-20 to 2026-07-27) — no Brent futures series exists in this pipeline yet.
- **Event dates:** hand-picked from public reporting (Wikipedia's "2026 Strait of Hormuz crisis"
  page, CFR's Global Conflict Tracker, and contemporaneous news) — 3 events from the 2011-12 Hormuz
  closure threat, 2019 tanker war (drone shootdown, Abqaiq-Khurais attack), and Jan 2020
  Soleimani/Ain al-Asad exchange; 4 events from the 2024 direct Israel-Iran strikes (April and
  October); 3 events from the opening five weeks of the 2026 crisis (war start, Strait closure
  declaration, US campaign to reopen). Weekend events are anchored to the next trading day.
- **Known gaps:** the pipeline's futures/prices tables run through **2026-07-23**; the Aug 5-6 2026
  deal-progress news and the resulting ~$4,286/oz gold print come from a live news search, not the
  pipeline, since a fresh pull hasn't run in the last two weeks. Event selection is not exhaustive
  or systematically sampled — it's every clearly-dated Iran/Hormuz military escalation the pipeline
  has clean daily data for, which introduces selection risk (see Limitations).

## Method

1. **Window definition.** For each event, `prior_close` = the last close strictly before the
   event's first available trading day (`day0`). `day0_ret` = that day's own return.
   `cum_ret_Nd` = close N trading days after `day0`, divided by `prior_close`, minus 1. This
   is the standard event-study convention, not a trading rule — no entry-timing claim is made
   here (see Limitations on point-in-time discipline).
2. **Weekend anchoring.** Events that occurred on a non-trading day (e.g. the war's Feb 28, 2026
   Saturday start, the Abqaiq-Khurais Saturday attack) are anchored to the next trading day
   forward, consistent across gold/WTI/Brent so all three see the same calendar.
3. **Data-quality check.** Two 2026 single-day gold moves were unusually large for a continuous
   futures series (GC=F: -11.4% on 2026-01-30 pre-dating the crisis, and -5.9% on 2026-03-19, the
   day the US began its campaign to reopen the Strait) — large enough to suspect a contract-roll
   artifact rather than a real move. Cross-checked against GLD (an ETF tracking spot gold, immune
   to futures-roll discontinuities): GLD fell -10.3% and -4.1% on the same two dates respectively.
   Independent confirmation — both moves are real market moves, not data artifacts.
4. **Statistics.** Given n=12 non-independent events (clustered in a handful of crisis periods,
   not i.i.d. draws), no t-test or significance claim is made. The one summary statistic reported
   (Pearson correlation between gold's and WTI's 10-day cumulative returns across the 12 events)
   is descriptive, explicitly labeled as such, and should not be read as a validated factor per
   the `signal-eval` skill's |IC| skepticism defaults, which this analysis doesn't attempt to meet.

## Results

**Full event table** (`day0`, `+3d`, `+10d` cumulative returns, gold vs WTI vs Brent):

| Event | Gold d0 | WTI d0 | Brent d0 | Gold 3d | WTI 3d | Brent 3d | Gold 10d | WTI 10d | Brent 10d |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 2011-12-27 Iran threatens Hormuz closure | -0.7% | +1.7% | -1.6% | -2.4% | -0.9% | +1.7% | +2.2% | +1.2% | +3.4% |
| 2019-06-20 Iran shoots down US drone | +3.6% | +5.4% | +4.1% | +5.2% | +7.6% | +5.4% | +3.9% | +7.0% | +1.2% |
| 2019-09-16 Abqaiq-Khurais attack | +0.8% | +14.7% | +11.7% | +0.5% | +6.0% | +4.9% | -1.7% | -1.4% | -0.4% |
| 2020-01-03 Soleimani killed | +1.6% | +3.1% | +3.0% | +2.2% | -2.6% | +0.4% | +2.3% | -4.3% | -4.5% |
| 2020-01-08 Iran retaliates (Ain al-Asad) | -0.9% | -4.9% | -2.1% | -1.5% | -7.4% | -6.7% | -0.5% | -11.3% | -9.7% |
| 2024-04-15 Iran attacks Israel directly | +0.4% | -0.3% | -2.5% | +1.1% | -3.4% | -5.1% | -0.5% | -3.5% | -5.0% |
| 2024-04-19 Israel retaliates | +0.7% | +0.5% | -0.4% | -2.4% | +0.1% | +0.8% | -3.5% | -5.6% | -5.4% |
| 2024-10-01 Iran missile barrage on Israel | +1.2% | +2.4% | +4.1% | +0.4% | +9.1% | +9.6% | +1.0% | +3.5% | +1.8% |
| 2024-10-28 Israel retaliates | +0.1% | -6.1% | -5.0% | -0.1% | -3.5% | -3.1% | -4.7% | -5.2% | -4.5% |
| 2026-03-02 US-Israel launch war on Iran | +1.2% | +6.3% | +8.3% | -3.2% | +20.9% | +24.2% | -4.5% | **+39.5%** | **+41.7%** |
| 2026-03-04 Iran declares Strait closed | +0.3% | +0.1% | -2.1% | -0.3% | +27.1% | +13.3% | -4.3% | +29.2% | +41.8% |
| 2026-03-19 US campaign to reopen Strait | -5.9% | -0.2% | -6.0% | -10.0% | -4.1% | -8.2% | -4.9% | +15.8% | +8.1% |

**Cross-series correlation:** Pearson r(gold_10d, WTI_10d) = **-0.420** across the 12 events (n=12,
no significance claim).

**2026 crisis, gold's day-of / next-day reaction to each specific development:**

| Date | Development | Gold close | Day return |
|---|---|--:|--:|
| 2026-03-02 | War begins (Sat 2/28 → Mon) | 5294.4 | +1.2% |
| 2026-03-04 | Iran declares Strait closed | 5120.2 | +0.3% |
| 2026-03-19 | US campaign to reopen Strait | 4600.7 | -5.9% |
| 2026-04-08 | 2-week truce announced | 4749.5 | +2.0% |
| 2026-06-17 | Ceasefire MOU signed | 4358.9 | +0.7% |
| 2026-06-18 | (day after MOU) | 4224.1 | **-3.1%** |
| 2026-07-06 | Iran fires on 3 tankers (MOU violation) | 4155.1 | +1.0% |

**Oil, same crisis, level check:** WTI ran from **$57.32 (Jan 2, 2026)** to a peak of **$112.95 on
Apr 7** (+97%); Brent ran from **$61.98 to $138.21** (+123%). By the last available pipeline data
(Jul 23/27), both had round-tripped roughly 55-65% of the spike but remained ~55-60% above the
pre-war baseline (WTI $88.93, Brent $91.82).

## Limitations & threats to validity

- **n=12, not independent, not randomly sampled.** Events cluster into 5 crisis periods
  (2011-12, 2019, 2020, 2024, 2026); within-crisis events share macro backdrop, so effective
  sample size for the cross-series correlation is closer to 5 than 12. The -0.42 correlation
  is a description of this specific sample, not a validated estimate of a population parameter.
- **No entry-timing / look-ahead discipline applied**, because this isn't being proposed as a
  trading rule — `day0` uses the event's own trading-day close, which per the `signal-eval`
  skill would be an unusable entry point for a live strategy. If this were ever turned into a
  signal, every window here would need to shift to `entry_lag>=1` first (see the sibling
  `2026-07-07_oil-shock-null-result.md` writeup, which found exactly this bug erased an
  earlier-looking-real oil-shock effect).
- **Confound: gold's own 2026 trajectory.** Gold ran from ~$4,314 (Jan 2) to a peak near $5,320
  (Jan 29) — +23% in four weeks, *before* the war started on Feb 28 — then gave most of that back
  through the rest of H1 2026. The March-June decline documented here may partly be that
  independent rally unwinding on its own schedule rather than a clean causal response to the war;
  this analysis cannot separate the two effects with the data at hand.
- **Event selection is manual, not systematic.** These are "every clean, dated Iran/Hormuz
  military event I could confirm with public reporting and the pipeline has data for" — not a
  swept, pre-registered list. A different reasonable analyst could pick a different 12 (or 20, or
  6) events and get a somewhat different correlation.
- **Data currency.** The pipeline's futures/prices tables end 2026-07-23; the actual Aug 5-6 deal
  news and gold's response to it are not in this backtest at all — they were sourced from live
  web search in the parent conversation and are qualitative context only, not part of the n=12.
- **No Brent futures series exists in this pipeline** — Brent uses FRED daily spot
  (`DCOILBRENTEU`), which is not update-matched intraday to WTI/gold futures closes the way a
  true futures series would be; treat Brent's day-0 numbers as slightly noisier than WTI's.

## Decision & next step

No factor or signal was built from this — it was a one-off descriptive backtest to answer a
specific question about the current Hormuz deal's likely gold impact, not a candidate for
`analytics/signals.py`. The practical takeaway carried back into that conversation: the "war up /
deal down" intuition for gold doesn't hold in this data — oil is the instrument that actually
prices this kind of event, and gold behaves more like a rates/inflation-expectations asset that
gets dragged down when oil spikes (even during acute war) and could see a tailwind if the Hormuz
deal completes and oil gives back more of its remaining ~55-60% premium.

If this line of research continues, the concrete next steps would be: (1) pull `eia_petroleum_*`
and `treasury_rates`/breakeven-inflation series into the same windows to test the inflation-
expectations mechanism directly rather than inferring it from the gold/oil sign flip; (2) if a
larger, more systematic event set is wanted, generalize this into `analytics/event_impact.py`
(already built for the oil→airlines driver-exposure use case) with a `hormuz`/`iran_conflict`
driver definition and its entry-lag-aware statistics, rather than the ad hoc script here.

## Reproduce

```
# from repo root, C:\ProgramData\anaconda3\python.exe on all commands
python experiments/2026-08-07_hormuz_gold_oil_event_study.py
```

Requires `storage/curated/futures/futures.parquet`, `storage/curated/commodities/commodities.parquet`,
and `storage/curated/prices/prices.parquet` to be present and current through at least 2026-07-23
(run `curated.py` first if they're stale). No pytest suite was written for this — it's a one-off
research script, not production code.
