# Tasks

## In progress (Zander handling manually)
- [ ] freight-rail: reconcile local uncommitted WIP vs origin/main (normalizer.py, schemas.py, freightos_fbx.py, usda_agtransport.py, storage.py) — rebase, don't lose the Freightos FCL/creds/date-param upgrade
- [ ] freight-rail: verify which USDA Socrata resource IDs are correct (local `tb7q-kn5i`/`axkm-yjzy` vs origin `swcm-ytjc`/`jvfn-6e7j`) before next sync
- [ ] freight-rail: delete stray `=` scratch file
- [ ] freight-rail: sync local to origin/main and re-run `upload_huggingface.py` to refresh HF (last synced Aug 3)

## Done this session
- [x] financial: port still-needed fixes from `fix/data-integrity-and-secrets` onto master (`e3512e3`, pushed): #3 discrete-quarter fundamentals filter + #6 Finnhub token redaction. #2/#4 already landed via curated.py / validate.py on master, so no rebase/merge was needed
- [x] financial: verify ported fixes (extract_concept order-independence checks pass; 101 tests pass, 2 pre-existing env failures)
- [x] shipping: wire HF sync step into `collect.yml` (`Sync to HuggingFace`, gated on HF_TOKEN) — committed locally, not yet pushed
- [x] financial: add user's HF write token to `.env` as a new `HF_TOKEN` line (existing line untouched; dotenv last-wins → new token effective; already present as `HF_WRITE_TOKEN`)
- [x] standing rule: "never delete or replace anything unless asked twice" recorded in `~/.claude/CLAUDE.md`

## Needs action (blocked on user / GitHub)
- [ ] shipping: add an `HF_TOKEN` write-token secret to the ShippingDataPipeline repo (currently only AISSTREAM_API_KEY + EIA_API_KEY exist), then push the collect.yml change
- [ ] shipping: re-run `upload_huggingface.py` or trigger workflow_dispatch to refresh `ZanderL1337/shipping-data-pipeline` (stale since Aug 3)
- [ ] financial: push un-pushed docs commit `5029780` (session notes + TASKS.md) on master — local is ahead of origin by 1
- [ ] REMINDER (deferred): revisit deleting the now-superseded `fix/data-integrity-and-secrets` branch once confident the ported fixes are complete
