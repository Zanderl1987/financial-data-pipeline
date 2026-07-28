# TODO — 2026-07-28

## High Priority

- [ ] **AV earnings backfill pacing** — resume daily runs to complete DJI coverage (was 9/30 symbols). Run `alpha_vantage_fundamentals_pipeline.py` in default incremental mode each day.
  - 2026-07-28: 20/20 requests hit quota (all "Information" responses). Retry later/tomorrow.
- [x] **HF dataset publish** — sync full-universe backfill (46.95M rows, 27,759 symbols) to HuggingFace via `upload_huggingface.py`.
  - 2026-07-28: done. 148 tables, 59,291,129 rows, 2.2 GB uploaded to ZanderL1337/financial-data-pipeline.

## Medium Priority

- [ ] **Fed SOMA dedicated backfill run** — needs >3600s timeout. Run `fed_soma_pipeline.py --backfill` with extended timeout outside of `run_all.py`.
- [ ] **Patents pipeline rewrite** — `patents_pipeline.py`: PatentsView migrated to USPTO ODP API (~March 2026); old endpoint is dead. Rewrite against `data.uspto.gov`.

## Low Priority

- [ ] **FRED 9 broken series IDs** — find replacement series IDs for the 9 FRED series that returned 400 during the 2026-07-23 stage-1 backfill.
- [ ] **OECD MEI pipeline rewrite** — `oecd_pipeline.py`: OECD retired `stats.oecd.org/SDMX-JSON`; rewrite against new Data Explorer API.
