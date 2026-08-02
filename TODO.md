# TODO — 2026-08-01

## Completed This Session (see SESSION_NOTES_2026-08-01.md for detail)

- [x] **FRED 9 broken series IDs** — already fixed in a prior session, undocumented; verified live.
- [x] **OECD MEI pipeline rewrite** — already done 2026-07-29, undocumented; verified live.
- [x] **pandas 3.0.3 `read_html` regression** — fixed in `finviz_pipeline.py` + `stockanalysis_pipeline.py`.
- [x] **`dark_pool_pipeline.py` rewrite** — source endpoint retired; rewired to `api.finra.org`.
- [x] **`eia_expansion_pipeline.py`** — 5 distinct API param bugs fixed, all 7 sub-datasets live.
- [x] **`open_meteo_pipeline.py`** — not a bug; incremental mode works, backfill needs chunking (see Backlog).

## In Progress

- [ ] **AV earnings backfill pacing** — still 9/30 DJI symbols. 2026-08-01 10:05am retry also hit
      full quota, zero progress (shared-IP rolling-24h conflict with earnings_sentiment_tool,
      confirmed structural, not transient). 4-option decision still not made by user.
- [ ] **Patents pipeline rewrite** — blocked: need ODP API key from USPTO.gov account.
- [ ] **Full Schwab price-history backfill** — sizing + running as of 2026-08-01 session end.

## Backlog

- [x] **`finra_short_interest` rewrite — RULED OUT (2026-08-02)**: the keyless
      `api.finra.org/data/group/otcMarket/name/equityShortInterest` endpoint flagged
      2026-08-01 turned out to be OTC/pink-sheet-scoped, not NMS — verified live,
      AAPL/MSFT/TSLA/SPY all return 204 (no rows). The real NMS-consolidated dataset
      (what `short_pressure` actually needs, matching the old dead CNMSshvol CDN file)
      lives at `equityMarket/equityShortInterest`, confirmed 401 keyless — still needs
      registered FINRA Query API credentials from developer.finra.org, unchanged from
      the 2026-07-06 finding. No further action without those credentials; yfinance
      fallback in `analytics/features.py` remains the correct active source.
- [ ] **`open_meteo_pipeline.py --backfill`** — needs date-range chunking to fit Open-Meteo's
      per-request size cap (35yr x 5 locations x 11 vars in one call is too much).
- [ ] **BLS retry** — `bls_oes_qcew_pipeline.py`/`bls_expansion_pipeline.py` hit daily quota
      2026-08-01; retry on a day with fresh headroom.
- [ ] **Reddit/Comtrade/Census/USDA/AISStream** — all NO-DATA, all blocked purely on the user
      obtaining/renewing an API key or app registration (no code issue).
