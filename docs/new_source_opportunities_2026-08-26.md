# New source opportunities — vetted 2026-08-26

Live-probed per data-source-vetting discipline (15-min probes before any pipeline code).
Verdicts recorded so dead ends are not re-litigated. Companion to
`new_data_sources_research.md` (2026-06-22) and CLAUDE.md's ruled-out list.

## A. Direct free replacements for the 403'd Finnhub alt-data suite

The free Finnhub tier killed ~10 endpoints (see CLAUDE.md). The CATALOG tables exist but
are NO DATA. All four replacements probed live 2026-08-26 from this box:

| Source | Replaces / fills | Verdict | Probe evidence + quirks |
|---|---|---|---|
| **USAspending API** (`api.usaspending.gov`) | `finnhub_usa_spending`, federal-contract alt data | **GO** | Keyless POST `/api/v2/search/spending_by_award_count/` returned 65,356 contracts for one month window. Award-level search back to 2007-10-01; older via `bulk_download` endpoints. Recipient-level awards = firm-level contract signal (defense, infra). |
| **Senate LDA lobbying API** (`lda.senate.gov/api/v1/filings/`) | `finnhub_lobbying` | **GO** | Keyless JSON, `filing_year=2026&page_size=1` returned live filings. Rate-limited but generous; quarterly filings, registrant/client/amount ranges. |
| **House Clerk FD/PTR XML ZIP** (`disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip`) | `congressional_trades` (aggregators 403'd since 07-23; official source instead) | **GO** | HEAD 200 OK, 56 KB zip, Last-Modified daily. Annual periodic electronic filings XML. Senate PTR side has no equivalent clean bulk file — House-first scope. |
| **USPTO ODP / PatentsView** | `finnhub_uspto_patents` + queued patents rewrite | **GO w/ free key** | Needs self-service ODP API key (already a TODO user action). Not probed (key-gated). |
| EPA TRI basic data files ("E"-pillar partial ESG replacement) | new environmental-risk facility table | **candidate — spike needed** | Guessed Envirofacts endpoint 404'd; documented path is bulk CSV downloads (`tri-basic-plus-data-files`). Spike the real URL before building. |

## B. New asset classes

| Asset | Source | Verdict | Notes |
|---|---|---|---|
| **Treasury yield curve** (fixes NO-DATA table) | home.treasury.gov daily par-yield-curve CSV | **GO — probed** | Full tenor set (1 Mo..30 Yr) daily CSV, keyless, verified returning current rows. Direct replacement for dead Nasdaq Data Link USTREASURY/YIELD. Either rewire `nasdaq_data_link_pipeline.run_yield_curve()` or add a small treasury.gov writer to the same `treasury_yield_curve` CATALOG glob. History back to ~1990 via per-year URLs. |
| **Municipal bonds (index level)** | FRED ICE BofA muni series (`BAMLCC0A0M2` master, plus AAA/BAA-rated muni yields) added to existing FRED SERIES dicts | **GO trivial** | No new pipeline — extend `commodity_macro_pipeline.py` / `analytics/macro.py` exactly like the existing BAML credit series. MSRB EMMA bulk trade data itself is subscription/academic-only — **NO-GO for programmatic bulk**; do not revisit without academic access. |
| **Carbon allowances (EUA/UKA/global ETS)** | ICAP Allowance Price Explorer downloads (`icapcarbonaction.com/en/ets-prices`) | **candidate — spike needed** | Ember's old `api.ember-energy.org/v1/carbon-price/latest` endpoint returns `{"detail":"Not Found"}` — **dead, don't use** (third-party scripts still cite it). ICAP site is server-rendered Drupal; download endpoint needs a real probe. OilPriceAPI carries EUA futures but indices/futures were plan-gated on its free tier (same finding as shipping repo's BDI check). |
| **DeFi protocol fundamentals** (TVL, fees, revenue, stablecoin flows, yields) | DefiLlama API family (`api.llama.fi`, `fees`/`summary/fees/<protocol>`, `stablecoins.llama.fi`, `yields.llama.fi`) | **GO — probed** | Keyless. `/tvl/uniswap` and `/summary/fees/uniswap` both returned live data. Complements CoinGecko price tables with crypto "fundamentals" — enables value-style factors (fees/TVL ratio) on the crypto asset class already collected. |
| Corporate bond transaction prices | FINRA TRACE public | key-registration item | Same developer.finra.org OAuth registration that unlocks NMS short interest. No keyless path. |

## C. Alternative data (new categories)

| Signal | Source | Verdict |
|---|---|---|
| **Global news events + tone** (GDELT 2.0 events/mentions/GKG, 15-min cadence, back to 2015) | `data.gdeltproject.org/gdeltv2/lastupdate.txt` | **GO — probed** (live file list returned). CC-BY. Enables country/actor-level event studies and a non-US complement to the Finnhub-news VADER sentiment. Volume is large — design for daily-file pulls, not 15-min. |
| Federal contracts by company (defense/infra beneficiaries) | USAspending (section A) | **GO** — doubles as alt-data. |
| Lobbying activity by firm/issue | LDA API (section A) | **GO**. |
| Air-travel capacity | OpenSky ADS-B anonymous API | **weak NO-GO from this box** — TLS handshake fails and anonymous tier was discontinued (now OAuth account); TSA checkpoint table already covers travel demand. Deprioritize unless a specific question appears. |

## D. Free-key registrations that unblock ALREADY-BUILT pipelines (user actions)

Each is instant/self-service except FINRA:

1. **BLS_API_KEY** — https://data.bls.gov/registrationEngine/ → raises quota 25→500/day,
   batch 25→50 series; fixes `bls_oes_qcew_pipeline.py` failing immediately on shared
   anonymous quota (v2 code path already in the pipeline).
2. **CENSUS_API_KEY** — census.data.gov → unlocks Census-dependent tables.
3. **USDA_NASS fresh keys** — current .env keys return 401 → fertilizer pipeline.
4. **Reddit app credentials** → `reddit_posts`/`reddit_mentions`.
5. **Comtrade key renew** → comtrade tables at full rate.
6. **AISStream key** → `ais_positions`.
7. **USPTO ODP key** → patents rewrite (queued).
8. **FINRA developer registration** (OAuth client creds) → full-market short interest +
   dark pool NMS (+ TRACE corporate bonds if wanted).

## Candidates not yet probed (spike before building)

- WTO Tariff Download Facility tariff-rate data (timely given tariff environment;
  complements Comtrade flows with policy variable).
- NFIB Small Business Economic Trends historical XLSX (small-business optimism factor;
  URL stability unknown).
- NASA FIRMS active-fire feed (free MAP_KEY; agri/utility supply-shock proxy).
- ICAP carbon download endpoint (above).

## Explicitly NOT recommended

- MSRB EMMA programmatic bulk (subscription/academic gate).
- OpenSky anonymous (discontinued).
- Ember carbon-price API endpoint (404 — references found online are stale).
- Any re-attempt of the CLAUDE.md ruled-out list (Nasdaq Data Link WAF, Baker Hughes SPA,
  AAR member gate, Stooq PoW, Motley Fool ToS, UKMTO headless-required).
