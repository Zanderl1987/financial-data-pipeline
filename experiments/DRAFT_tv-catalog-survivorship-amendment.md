# DRAFT amendment: dead-company check. Not in force.

Not in force until reviewed by Zander, moved under "Amendments" in
`2026-08-11_tv-strategy-catalog-preregistration.md` and committed, all before any strategy is
scored on CRSP prices. Date the heading on the day it is committed, then delete this header and
this file.

**Governing rule for every choice below:** a method is chosen because it measures the truth, never
because it protects an existing result, keeps a convenient check or helps a strategy. Where the
truth is unknown, the assumption must not favour the strategy.

**Open before commit (none needs CRSP results):**

1. Done: the yearly borrowing fee in change 9 (270 bps, approved by Zander 2026-11-08). Zander
   confirmed the 2.70% in the published Journal of Finance version the same day.
2. The first date in change 3, read from the CRSP tables before any strategy is scored.
3. `strategies/survivorship.py` and `strategies/stage5.py` follow this text, tested on made-up prices
   only. The CRSP pull script, `strategies/crsp_pull.py`, is written from CRSP's published legacy
   daily layout and tested on made-up rows only. Its column choices must be checked against the
   real tables (`--tables-only`) before any price is pulled. Its population rules are recorded
   under change 3 and its holdout rules under change 4.

### YYYY-MM-DD — all tested strategies re-scored on a complete CRSP population before Stage 5

**What was found.**

- `yfinance_universe_prices` (section 2) holds only companies alive today, and it is a snapshot of
  today's Russell 3000. Membership is decided by how a company turned out, which is a selection on
  outcome beyond survival alone. Section 8 declares survivorship in the sampling frame, not in the
  price table.
- `evaluation.trades` drops a position still open when a symbol's data ends, so neither the
  strategy nor its circular-shift null has ever faced a delisting. The same drop applies to any
  position still open at the end of the data, and a position held open is more likely to be a loser
  waiting for its exit.
- `clean_symbols()` deletes a whole symbol's history for one real one-day crash.
- The cost model charges no market impact when trailing volume is zero or missing
  (`_compute_adv_impact_arrays` turns those cases into 0). On made-up prices, a $10,000 round trip
  cost about 596 bps on a stock trading $5,000 a day, and 2 bps on a stock with no volume. One missing
  volume day in the 20-day window removes the impact on any fill it reaches. The least liquid
  stocks can therefore be the cheapest to trade. How many Stage 3 v2 trades this touched has not
  been measured.
- Short positions pay no borrowing cost (`borrow_fee_bps` is 0).
- The $5 entry floor reads a split-adjusted price, so a later reverse split can lift a real $2
  price over the floor and a later forward split can drop a real $100 price under it.

**Disclosure.** Before writing this amendment the author saw the following.

1. A replay of the real development-split trades of `capitulation_stretch_reversion`,
   `joey_stochrsi_atr` and `rsi_bb_inside_strategy`, tagged by the fall from the 252-day high
   and by share price. Long gains rise with the size of the fall for `joey_stochrsi_atr`
   (+0.19% under a 15% fall, +2.30% over 75%) and `rsi_bb_inside_strategy` (+0.52% to +6.49%).
   They do not for `capitulation_stretch_reversion`. Longs on falls over 50% made more than
   all of `rsi_bb_inside_strategy`'s net P&L.
2. Stage 3 v2 results for all 35 tested strategies.
3. Raw prices for a 12-stock Tiingo sample of dead companies. No strategy was run on them.
4. A rough break-even estimate from assumed failure rates.
5. An earlier draft of this amendment proposed thinning the dead companies and keeping the survivor
   panel, partly to keep an exact match with the registry. That was withdrawn because it put a
   convenient check ahead of a correct sample.
6. While choosing the borrowing fee: published fee studies, and the repo's Interactive Brokers
   borrow-fee table (a 2026 snapshot) for 1,771 development-split Stage 3 symbols priced $5 or more,
   mean 0.60% a year, median 0.39%. No strategy was run at any fee, and the fee was chosen
   before any result with it existed.

No strategy has been scored on any dead-company panel. No holdout data was touched, and no
price after 2017-12-31 for a holdout-split dead company has been requested.

**Changes.**

1. **Why a re-score of everything.** All 35 tested strategies are re-scored, not only the three
   current candidates. Those three were picked because they did best on the biased panel, so testing
   only them would inherit that selection, and the multiple-testing correction would no longer
   cover the search that actually happened.
2. **Pass rule.** A strategy enters Stage 5, or may be proposed for the forward test in amendment
   2026-10-28, only if it meets all of these: it passes the existing Stage 4 rule on the Stage 3 v2
   results; it passes BH-FDR at q = 0.10 across all re-scored strategies; its net P&L is above 0;
   and its null exposure ratio is within [0.8, 1.25]. The re-score can therefore only remove
   candidates. Any campaign strategies tested later are re-scored the same way and join the same
   BH family.
3. **Population.** One source for everyone: all CRSP common stocks (share codes 10 and 11) on NYSE,
   AMEX or Nasdaq (exchange codes 1, 2 and 3), alive or dead, keyed by `permno`. No thinning, and no
   selection that uses a company's later outcome. History starts on the first date all three
   exchanges carry real open prices, and ends on CRSP's last date. Prices are split-adjusted only,
   as in section 2. The column mapping for the available tables is recorded here before scoring.
   Population rules (approved by Zander 2026-11-08, before any CRSP data was seen):
   - A company is in the population on a day when that day's CRSP name record has share code 10
     or 11 and exchange code 1, 2 or 3. A trading halt or suspension (exchange code -2 or -1)
     that continues such a period keeps it in, so the last real price before a delisting is
     kept; a halt never starts a period.
   - A company that leaves the population other than by delisting, for example because its share
     code changes, closes at its last close inside the population, as in change 5. A later CRSP
     delisting is not applied, because that fall happened outside the population. CRSP's own
     delisting code is kept alongside for reporting.
   - A company is matched to a Stage 3 symbol by its last ticker inside the population. A share
     class is added ("BF-B") only when Stage 3 holds that form.
4. **Split.** A company matching a Stage 3 symbol keeps that symbol's assignment, so a development
   company stays in development and a holdout company stays reserved. All others use
   `sha256("CRSP" + permno) % 4 == 0`. A match that is ambiguous, or a recycled ticker that cannot
   be ruled out, is treated as holdout. A holdout company is used only through 2017-12-31 and its
   later prices are not requested. If it delisted after that date, this run shows no delisting.
   A renamed company may not match its old Stage 3 symbol. So for each holdout Stage 3 symbol
   no company matched by ticker, a development-assigned company not matched to any Stage 3
   symbol is moved to the holdout if: any ticker in its CRSP name history is that symbol; or
   its 2016-2017 closes track the symbol's Yahoo closes (60 or more shared days, 90% of them
   within 2% of the median price ratio); or, when the symbol has fewer than 60 such days, its
   first CRSP date is within 7 days of the symbol's first Yahoo date. These thresholds were
   set on 2026-11-08, before any CRSP data was seen, and are not changed after. The rules only
   move companies into the holdout, and use no price after 2017-12-31, no delisting and no
   strategy result. To allow this, every company's prices are requested through 2017-12-31
   first, and later prices only for companies still in development after the rules run. How
   many companies each rule moved, and the holdout symbols no rule resolved, are reported;
   unresolved symbols are a stated limit.
5. **Closing positions.** A position open when a company delists closes at last close x (1 +
   terminal return). A position open when the data ends for any other reason closes at the last close.
   Signals are computed on real bars only. Closes are added after the circular shift, so every
   shifted copy faces the same end as the strategy.
6. **Terminal return.** A valid CRSP delisting return is always used. If it is missing, a merger or
   exchange (codes 2xx and 3xx) gets 0, and every other delisting gets the value below.

   | Scenario | Missing return | Role |
   |---|---|---|
   | `shumway` | -30% NYSE/AMEX, -55% Nasdaq | **Primary** |
   | `missing_zero` | 0% | Reported only |
   | `missing_wipeout` | -100% | **Must also pass** |

   A strategy that passes the primary scenario and fails `missing_wipeout` counts as failed, because
   its pass then depends on an unknown number. Results also report how many trades and dollars rest
   on a missing return.
7. **Costs and trades that could not have happened.** Impact is never 0 because liquidity is
   unmeasured. No entry fills where trailing 20-day dollar volume is missing, zero or smaller than the
   position, or where trailing volatility cannot be measured. An exit where it cannot be measured,
   including a forced close, pays the most recent measured impact on that company. No-trade days
   count as zero volume in the trailing average. This applies to the strategy and to every shifted copy.
8. **No-trade days.** CRSP rows with no real trade (negative price, meaning a bid/ask midpoint) are
   removed, so nothing fills at a quote no one traded at. The $5 floor reads the real traded price
   on the day, not the split-adjusted one, for the strategy and every shifted copy.
9. **Shorts.** Every short pays a yearly borrowing fee of 270 bps (2.70%), charged per trading day
   held, for the strategy and every shifted copy. Source: Muravyev, Pearson and Pollet, "Anomalies
   and Their Short-Sale Costs", Journal of Finance (2025): the mean borrower-side fee (Markit
   indicative fee) on the short legs of published anomalies, CRSP common stocks, July 2006 to
   December 2020. Their all-stock mean, 1.67%, was rejected because rule-picked shorts lean toward
   costly names; lender-side figures (Drechsler and Drechsler; D'Avolio) were rejected because
   they leave out the broker's markup. The same 270 bps applies to years outside 2006-2020. A short open at a delisting closes at the last real close and gets no credit for the
   delisting fall, since borrow recalls and frozen positions make that gain unreliable. It still pays a
   delisting rise, such as a takeover premium. Forced closes on shorts are reported separately. So
   that `missing_wipeout` is a worst case, it never pays a short.
10. **Price screen.** `clean_symbols()` is not applied in the primary run: it would delete the crashes
    being measured, and its purpose, catching bad vendor splits, does not apply to CRSP's split
    factors. A sensitivity run with it applied is reported only, along with every trade over a
    3x one-day move.
11. **Unchanged from Stage 3 v2.** Rules, author parameters, next-close execution, the square-root-law
    formula and its coefficient, and seed 0. `n_perm` rises from 200 to 1,000: the smallest p-value 200
    copies can give is about 0.005, while a lone strategy needs 0.10 / 35 = 0.0029 to pass BH.
12. **Failure is final for this campaign.** A failing strategy does not enter Stage 5. No scenario,
    cost rule or population rule is revised after results. A defect found afterwards is fixed only
    through a new amendment that re-scores every strategy and discloses the earlier result.
13. **Reported with every result.** Companies used, alive and dead; delistings, forced closes and their
    P&L; missing returns; entries blocked by change 7; entries changed by the real-price floor;
    Stage 3 symbols no company matched; and the Stage 3 v2 number next to the new one with the difference shown. A difference from
    Stage 3 v2 is a finding, not a failure, and does not stop a result being used.
14. **Data and code checks, before scoring.** CRSP prices for a sample of companies are compared with
    Yahoo to catch mapping errors, and any large disagreement is explained first. The code keeps its
    separate test that, given Stage 3 inputs and rules, it reproduces the registry row. That test
    checks the code only; it is no reason to keep the old population or rules.
15. **Stage 5 data.** A strategy reaching Stage 5 is scored on all holdout-split CRSP companies from
    2018-01-01 under these same rules, requested only when Stage 5 runs, under its existing one-shot
    rule. `strategies/stage5.py` must be changed to do this before any Stage 5 run.
16. **Storage.** CRSP data stays under `storage/licensed/`, never `storage/curated/`, which is
    published publicly. Results go in the registry under `tv_strategy_catalog_survivorship_v1`.
    Existing rows are kept.

**Declared limits that remain.** Dividends are ignored (section 2), which understates longs and spares
shorts from paying them. The cost formula is an estimate, and a $10,000 fill at up to 100% of daily
volume may be optimistic for thin stocks. CRSP may end earlier than the Yahoo panel, which shortens the
Stage 5 window. The borrowing fee is one flat rate: it overcharges easy-to-borrow shorts and
undercharges hard-to-borrow ones (in the source, 10% of stocks cost 3% or more a year and 1% cost
30% or more), so a strategy whose short profits come from costly names is still flattered. It
assumes every stock could be borrowed. The source dropped stocks under $50M in market value, which
this population keeps and which tend to cost more to borrow. Per-stock historical fees would fix
this; whether the campaign can get them (Markit through WRDS) is unconfirmed.

**Not changed:** the primary endpoint, BH-FDR at q = 0.10 campaign-wide, the stopping rule, translation
fidelity, and the spent holdouts in amendment 2026-10-28.
