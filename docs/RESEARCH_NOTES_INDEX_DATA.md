# Research notes: S&P Global indexes + Bloomberg indices data

Vetted 2026-08-31. Goal: get constituents (preferred) or index-level data
(fallback) for S&P Global indexes and Bloomberg indices.

## Verdict (TL;DR)

| Target | Constituents | Index level |
|---|---|---|
| S&P 500 / sector (GICS) | **GO** - already in `index_members` | **GO** - `sector_etfs` + `^GSPC` in `market_history` |
| Bloomberg fixed income | **NO-GO** (proprietary, no free source) | **PROXY** - AGG/BND ETFs + FRED ICE BofA OAS/TR (`fred_credit`) |

## S&P sector constituents: GO (no new source)

Every S&P Select Sector Index is, by definition, the GICS-sector subset of the
S&P 500. `index_members` already carries `gics_sector` + `gics_sub_industry`
per S&P 500 member (from Wikipedia, 100% populated -- 9,557 rows across 19
snapshots, 0 nulls in latest). Derive sector membership with a
`GROUP BY gics_sector` on the SPX slice; exposed as the `sector_members`
analytics view in query.py.

- Weights per constituent are NOT available free (S&P DJI paywalled). SPDR
  Select Sector ETFs (`sector_etfs` table) are the free proxy for sector
  *levels*; their holdings pages give weights (top-25 free, full list behind
  Pro on stockanalysis.com).
- Yahoo `^SP500-XX` sector symbols are unreliable (Energy 404s; Financials /
  Health Care quote-only, no history). Don't build on them.

## Bloomberg indices: constituents NO-GO, level = credit proxies

No free source of bond-level members for the Bloomberg (legacy Barclays)
fixed-income indices -- proprietary terminal/enterprise data. Literal Bloomberg
index levels are NOT on FRED: `BBUSATR` and `LBUSTRUU` both return 400
"series does not exist" (confirmed live).

Free fallback (all wired):
1. **Aggregate / total-bond level**: AGG (2003+) and BND (2007+) added to
   `market_history` via `yfinance_pipeline.py`. LQD/HYG/TLT/IEF already present.
2. **Credit OAS spreads + total return**: new `fred_credit` table from
   `fred_credit_pipeline.py` (ICE BofA family, the standard free proxy):

   - OAS: `BAMLC0A0CM` (Corp), `BAMLC0A1CAAA` (AAA), `BAMLC0A4CBBB` (BBB),
     `BAMLH0A0HYM2` (HY), `BAMLH0A1HYBB` (BB), `BAMLH0A3HYC` (CCC)
   - Total return: `BAMLCC0A0CMTRIV` (Corp), `BAMLHYH0A0HYM2TRIV` (HY)

   Note the OLD high-yield OAS ID `BAMLH0A0HYM` is dead (400); the current
   series is `BAMLH0A0HYM2`.

## Constraint discovered

FRED's free observations endpoint currently serves the ICE BofA OAS/TR series
**only back to ~2023-09-01** (~793 obs/series), even with explicit
`observation_start=1996-01-01`. Control series (e.g. DGS10) return full history
to 1962, so this is specific to the ICE BofA series, not a systemic API issue.
Deeper historical credit-bond proxies in the store: Moody's `BAA10Y`/`AAA10Y`
(`fred_rates_gdp`) and the LQD/HYG/TLT/IEF ETF levels (`market_history`).

## Keep / don't re-litigate

- Don't re-attempt Bloomberg constituent scraping: no free access model exists.
- Don't re-add `BAMLH0A0HYM`; use `BAMLH0A0HYM2`.
- Bloomberg's own Aggregate total-return (`LBUSTRUU`) sometimes appears on
  Yahoo/Investing with full history -- worth a one-off probe if literal
  Bloomberg-Agg level (not the AGG ETF) is ever required, but not relied on.
