# Tasks

- [ ] freight-rail: reconcile local uncommitted WIP vs origin/main (normalizer.py, schemas.py, freightos_fbx.py, usda_agtransport.py, storage.py) — rebase, don't lose the Freightos FCL/creds/date-param upgrade
- [ ] freight-rail: verify which USDA Socrata resource IDs are correct (local `tb7q-kn5i`/`axkm-yjzy` vs origin `swcm-ytjc`/`jvfn-6e7j`) before next sync
- [ ] freight-rail: delete stray `=` scratch file
- [ ] freight-rail: sync local to origin/main and re-run `upload_huggingface.py` to refresh HF (last synced Aug 3)
- [ ] financial: merge `fix/data-integrity-and-secrets` (fdp-review) into master — #2 dedup, #3 discrete-quarter fundamentals, #4 validator drift, #6 Finnhub token leak; rebase from `9cb0c4a` first
- [ ] shipping: wire HF upload into `collect.yml` or refresh `ZanderL1337/shipping-data-pipeline` manually (stale since Aug 3)
