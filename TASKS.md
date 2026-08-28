# Tasks

## In progress (Zander handling manually)
- [ ] freight-rail: reconcile local uncommitted WIP vs origin/main (normalizer.py, schemas.py, freightos_fbx.py, usda_agtransport.py, storage.py) — rebase, don't lose the Freightos FCL/creds/date-param upgrade
- [ ] freight-rail: verify which USDA Socrata resource IDs are correct (local `tb7q-kn5i`/`axkm-yjzy` vs origin `swcm-ytjc`/`jvfn-6e7j`) before next sync
- [ ] freight-rail: delete stray `=` scratch file
- [ ] freight-rail: sync local to origin/main and re-run `upload_huggingface.py` to refresh HF (last synced Aug 3)

## Done this session
- [x] shipping: fix root cause of stale HF dataset — CI had been failing at the `ruff check` step since before Aug 3 due to 4 lint errors in `upload_huggingface.py` (3× E402 imports, 1× UP017). Fixed imports + `datetime.UTC` (as direct import — class has no `.UTC` attr), pushed (`6695730`, `c069785`, `61b69db`).
- [x] shipping: add `HF_TOKEN` write-token secret to `Zanderl1987/ShippingDataPipeline` (was missing; only AISSTREAM/EIA keys existed) — set via `gh secret set`.
- [x] shipping: push collect.yml HF sync change and re-run workflow — `ZanderL1337/shipping-data-pipeline` refreshed 2026-08-08T03:49:22Z.
- [x] financial: push docs commits (`5029780` + `83076d9`) to master.
- [x] financial: port still-needed fixes from `fix/data-integrity-and-secrets` onto master (`e3512e3`, pushed): #3 discrete-quarter fundamentals filter + #6 Finnhub token redaction. #2/#4 already landed via curated.py / validate.py on master, so no rebase/merge was needed
- [x] financial: verify ported fixes (extract_concept order-independence checks pass; 101 tests pass, 2 pre-existing env failures)
- [x] shipping: wire HF sync step into `collect.yml` (`Sync to HuggingFace`, gated on HF_TOKEN) — committed locally, not yet pushed
- [x] financial: add user's HF write token to `.env` as a new `HF_TOKEN` line (existing line untouched; dotenv last-wins → new token effective; already present as `HF_WRITE_TOKEN`)
- [x] standing rule: "never delete or replace anything unless asked twice" recorded in `~/.claude/CLAUDE.md`

## Needs action (blocked on user / GitHub)
- [ ] shipping: (OPTIONAL) Node 20 deprecation warnings in CI — bump `actions/checkout@v4`/`actions/upload-artifact@v4`/`astral-sh/setup-uv@v4` to `@v5`
- [ ] REMINDER (deferred): revisit deleting the now-superseded `fix/data-integrity-and-secrets` branch once confident the ported fixes are complete
