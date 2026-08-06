# Session Notes — 2026-08-05

**Branch:** master (clean, synced with origin at `71be288`; no commits yet this session)
**Session model:** opencode / big-pickle

## What happened

User asked to expand the public HuggingFace dataset
`ZanderL1337/financial-fundamentals` with *more* data (continuing the 08-04
refresh/append work), then to review modern database-design best practices
before implementing, then to read a relevant paper. This session = research +
design + documentation; no pipeline code changed yet.

### 1. Coverage gap analysis (live EDGAR probes)

Confirmed the dataset currently only covers **US domestic filers filing 10-K /
10-Q with us-gaap tags**. Two structural gaps found in
`fundamentals_pipeline.py`:

- **Taxonomy gate** (`~line 211`): only `facts.us-gaap` is read. Foreign
  private issuers (TSM, RY, NOK, INFY, TM, BABA, PDD, NIO, ...) store ALL their
  facts under `facts.ifrs-full`, so they currently contribute zero rows.
- **Form gate** (`extract_company`, `~lines 227-231`): only `10-K` and `10-Q`
  accepted. Live probing of companyfacts showed `20-F`, `20-F/A`, `40-F`,
  `6-K`, `10-K/A`, `10-Q/A`, `8-K` all carry tagged facts and are worth adding.

**S-1 / S-1/A ruled out**: companyfacts contains no facts tagged against S-1
filings (verified on RDDT, RBRK, CART). 8-K kept but low-value (most tagged
facts are ephemeral one-offs).

### 2. IFRS filer tag mapping (verified against live data)

`ifrs-full` uses different tag names, and 2 of the 10 us-gaap concepts have no
IFRS equivalent:

- Revenue/RevenueFromContractsWithCustomers, ProfitLoss (shared w/ us-gaap),
  GrossProfit (shared), ProfitLossFromOperatingActivities, Assets (shared),
  Liabilities (shared), CashFlowsFromUsedInOperatingActivities,
  NumberOfSharesIssuedAndFullyPaid (proxy for share count).
- **EPS is NOT tagged in ifrs-full** — IFRS filers will get 8/10 metrics (all
  except EPS). Accepted tradeoff; document in the metrics reference.

### 3. Database design review → Option D (APPROVED)

Reviewed current structure (long atomic fact tables, wide derived views,
curated parquet + Iceberg pilot layer) against modern best practices (Kimball,
long atomic fact stores, columnar serving). Presented 4 options; user approved
**Option D** — 5 files per snapshot:

1. `facts.parquet` — long atomic fact table (generalization of the current
   annual/quarterly long tables, one table for all fiscal periods).
2. `companies.parquet` — company master (CIK, symbol, names, form-language).
3. `filings.parquet` — filing master (accession number, form, CIK, dates).
4. `financials_annual_latest.parquet` / `financials_quarterly_latest.parquet`
   — wide, pivoted, latest-filing-wins (current HF filenames preserved so the
   live dataset + verify_hf baselines keep working).
5. `metrics.parquet` — static reference (concept, IFRS mapping, definition).

Per-filing wide tables (Option B) explicitly rejected.

### 4. Paper: arXiv 2605.00676v1 "Living Databases"

Read in full. No structural change to Option D, but two governing rules adopted:

1. **One coherent run per snapshot** — every file in a given HF revision is
   built from the same pipeline run, so any revision is internally consistent
   (no cross-run mixing).
2. **Additive-only schema changes** — new files/columns are always nullable
   additions; never rename/drop existing columns. Old revisions stay loadable
   by current readers.

### 5. Grain issue flagged (to fix in implementation)

10-K filings contain quarterly-duration facts and 10-Q filings contain both
3-month and YTD facts; current schema can't distinguish them and has no
accession number (restatements are indistinguishable). Plan adds `duration`,
`start_date`, `accession_number`, `taxonomy`, `form` stays.

## Decisions locked

- Foreign filers IN (ifrs-full + us-gaap, 20-F/40-F/6-K/10-K/A/10-Q/A/8-K).
- EPS missing for IFRS filers — accepted, documented.
- Option D schema, paper rules, additive-only evolution.
- Old long filenames preserved (HF + verify_hf baselines unchanged).
- Session notes + TODO updated BEFORE any code (this file).

## Next steps (see TODO.md)

1. Extend `fundamentals_pipeline.py`: ifrs-full + form allowlist + new columns.
2. New `build_fundamentals_dataset.py`: companies/filings/wide/metrics.
3. Rework HF push to single coherent revision per run.
4. Update `scripts/fundamentals_hf_refresh.ps1` + `verify_hf.py`.
5. Live test + verify on HF.

## Open questions

- Whether 6-K quarterly data should merge into the wide quarterly table or stay
  facts-only until more IFRS filers accumulate (decide during implementation).

---

## Implementation + live push (completed 2026-08-05/06)

All next steps DONE; live dataset updated.

### Code changes

- **`fundamentals_pipeline.py`**: loops both `facts.us-gaap` and `facts.ifrs-full`
  (IFRS tag map verified live on TSM/RY before writing), form allowlist =
  `ANNUAL_FORMS {10-K,10-K/A,20-F,20-F/A,40-F}` + `QUARTERLY_FORMS
  {10-Q,10-Q/A,6-K,8-K,8-K/A}`, adds `taxonomy`/`accession_number`/`start_date`/
  `duration_days` columns, extract dedup key widened to
  `(taxonomy,start,end,form,accn)`. Removed the HF-cache short-circuit and the
  `hf_push`/`hf_pull`/`hf_append` helpers — the pipeline is extraction-only now
  (the HF dataset is assembled + pushed by `build_fundamentals_dataset.py`).
  `main()` signature simplified (no `hf_repo`/`use_hf_cache`).
- **`build_fundamentals_dataset.py` (new)**: reads curated annual/quarterly,
  builds facts.parquet (long, one `period` bucket col), companies.parquet,
  filings.parquet (grain = accession x period x fiscal_year x fiscal_period),
  wide `financials_annual_latest.parquet`/`financials_quarterly_latest.parquet`
  (latest-filing-wins on (symbol, period_end), per-metric value + `_unit`
  columns), metrics.parquet (from CONCEPTS/IFRS_CONCEPTS), plus README.md +
  snapshot.json. `hf_push_revision()` pushes all files in ONE atomic
  `create_commit` (one coherent revision per run). Old wide filenames preserved
  so `financials_*_latest.parquet` still exist.
- **`verify_hf.py`**: rewritten for the new files — per-kind checks (long:
  fetched_at recency + dup rate + taxonomy breakdown; wide: zero dup
  (symbol, period_end); master: cik / composite accession key uniqueness;
  reference: row count), plus snapshot.json-vs-actual coherence check and
  `--no-min-rows` for scratch smoke tests.
- **`scripts/fundamentals_hf_refresh.ps1`**: chain is now pipeline (no
  `--no-cache` — always extracts) -> curated -> build (pushes) -> verify.
- **`AUTOMATION.md`**: ClaudeAuto-FundamentalsHFRefresh description updated.

### Verification

- 559/559 tests pass (was 532; the 27 delta are from the 08-04 hf-sync work
  already merged). Scratch repo push + verify PASS, then deleted.
- Full-market run: 20,151 companies, 0 failed. Annual raw 3,212,906 rows,
  quarterly raw 6,687,759 rows (up from 1.38M/2.79M on 08-04 — new forms +
  IFRS). Curated rebuild: 179 tables, 27.2M dupes removed.
- Live push to `ZanderL1337/financial-fundamentals` -> `verify_hf.py`
  **VERIFY PASS**: facts 5,180,975 (us-gaap 5,120,697 + ifrs-full 60,231),
  companies 16,881, filings 415,581, annual-latest 141,995,
  quarterly-latest 246,878, metrics 10, all snapshot.json/actual counts match.

### Notes / gotchas

- BABA is us-gaap, not IFRS (verified live: 358 us-gaap tags, 0 ifrs-full).
  TM (Toyota) files **us-gaap 20-F** — now captured by the 20-F allowlist.
- Foreign ADRs resolve to OTC tickers in EDGAR's map (TSM -> TSMWF); data is
  present under that symbol. Pre-existing pipeline behavior, not changed.
- Wide tables key on (symbol, period_end) with latest FILED accession winning;
  restatements remain in facts.parquet under their accession_number.
- verify_hf's filings dup check must use the composite key
  (accession, period, fiscal_year, fiscal_period) — one 10-K accession
  legitimately covers multiple fiscal years.
