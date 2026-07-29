# TODO — 2026-07-28

## Completed This Session

- [x] **HF dataset publish** — 148 tables, 59,291,129 rows, 2.2 GB uploaded to HF.
- [x] **Fed SOMA dedicated backfill run** — 30,815,213 rows across 1,203 weekly reports (2003–2026), curated + PASS.

## In Progress

- [ ] **AV earnings backfill pacing** — 9/30 DJI symbols. Quota exhausted today (custom_index_tool automations likely consumed it). Retry later/tomorrow.
- [ ] **Patents pipeline rewrite** — blocked: need ODP API key from USPTO.gov account.

## Backlog

- [ ] **FRED 9 broken series IDs** — find replacement series IDs for the 9 FRED series that returned 400 during the 2026-07-23 stage-1 backfill.
- [ ] **OECD MEI pipeline rewrite** — `oecd_pipeline.py`: OECD retired `stats.oecd.org/SDMX-JSON`; rewrite against new Data Explorer API.
