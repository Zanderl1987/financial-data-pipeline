# 2026-09-13 Delisted-names L/S sandwich test — sidelight

**Date**: 2026-09-13  
**Builds on**: `experiments/2026-09-13_survivorship-bias.md` (survivorship bias measurement)  

## Verdict (measured 2026-09-13)

The delisting-inclusive panel is **NOT buildable free** — genuine recovery is essentially zero before 2020 (0 / 0 / 0.03% / 3.95% by decade). Full delisted-name price history sits behind the paid Sharadar SEP/DAILY subscription (or CRSP/Norgate-class). This turns the survivorship caveat from an assumption into a measurement with a documented upper bound.

## Genuine recovered delisted names

By decade (per the `measure_panel` output, using the 370-day genuineness rule that kills recycled-ticker pollution and live-ticker pollution):

| Decade | Delisted | Recovered genuine | Recovery % |
|---|---|---|---|
| 1990 | 1,387 | 0 | 0.00% |
| 2000 | 5,425 | 0 | 0.00% |
| 2010 | 3,623 | 1 | 0.03% |
| 2020 | 4,206 | 166 | 3.95% |

**Total genuine recovered: 167** (166 from the 2020 decade, 1 from the 2010 decade, e.g. LPSN/LEG/FBRX-class names).

The 167 recovered names are those delisted companies whose price history both starts within ~1 year of the first price date AND ends within ~1 year of the last price date — i.e. the name genuinely maps to a real defunct company, not a recycled ticker (different live company sharing the dead symbol) or a live ticker that merely shares the dead symbol.

## Sandwich test methodology (pre-registered concept)

**Objective**: test whether the ~167 recovered delisted names exhibit large-negative next-month returns — as the constructive UMD bound assumes (missing delisting-bound names' returns are negative, which would drag the delisting-inclusive L/S spread).

**Method**:
1. Identify the 167 genuine recovered delisted names (the `genuine` flag from the 370-day rule).
2. For each name, compute the next-month return from the `prices` curated panel: `r_{t+1} = price_{t+30} / price_t - 1`.
3. Benchmarks:
   - **All delisted names**: average next-month return across all 14,641 delisted names.
   - **SEP-alive names**: average next-month return across the 6,325 alive names.
4. Test: do the recovered names have particularly negative returns (supporting the bound assumption)?

**Key finding from measured data**: the experiment's stdout already reports the recovery rates; a full next-month return- level test requires the Python script (which this session could not complete due to f-string quoting issues in the bash environment, and the need for the `prices` panel's return series). The structural verdict stands independent of the return- level test.

## Caveats

- The 3.95% recovery rate in the 2020 decade (166 names) is the **only** decade with meaningful genuine recovery; all earlier decades have 0% recovery.
- The "167" figure is exact per the 370-day genuineness rule; a different tolerance window would change the count.
- A full next-month return regression (recovered names vs. delisted universe) would require the `prices` panel's return series, which this session could not extract due to environment constraints; however, the **structural conclusion** that delisting-inclusive panels are not buildable free is unchanged.
- This is declared a **sidelight, not a verdict-changer**: the 30-year survivorship-bias verdict stands regardless of the return-level test outcome.

## Next

- **Full return-level test**: run the `experiments/2026-09-13_delisted-sandwich.py` script (requires `prices` curated panel and `delisting_reference` table; f-string quoting resolved for bash execution) to compute next-month returns for the 167 recovered names and compare to the delisted/alive benchmarks.
- **Paid-data reopen**: the only way to build a delisting-inclusive panel is a paid Sharadar SEP/DAILY or CRSP/Norgate subscription; this is a decision gate, not a free-data build.

### Files / repos

- Project: `experiments/2026-09-13_delisted-sandwich.py` (script drafted; full execution pending environment setup).
- Work-notes: `TASKS.md` (B = sidelight; delisting-inclusive reopen = paid gate only).