# Indicators awaiting entry/exit logic

Scripts collected as `indicator()` (visualization/signal only, no `strategy.entry`/
`strategy.exit`). These fail Stage 1 screening (`no_entry`) as-is and cannot enter the
TV strategy catalog campaign directly — but the underlying signal could become a
`TradeRule` if someone writes explicit entry/exit rules around it. Track candidates here
rather than deleting them outright.

| slug | file | signal | provenance |
|---|---|---|---|
| boosted_moving_average | `boosted_moving_average.pine` | bullish/bearish crossover of a sensitivity-boosted EMA vs. its own WMA | **unknown** — collected before the `.meta.json` provenance protocol was locked in (see `experiments/2026-08-11_tv-strategy-catalog-preregistration.md`); no `tv_url`/`tv_author`/`collected_at` recorded. Cannot be entered into the pre-registered campaign without a source record — if pursued later, re-find the source page and collect provenance fresh, or drop it. |

## What "writing entry/exit logic" means here

Per the pre-registration, a `TradeRule` needs both a specific entry condition and a
specific exit condition — not just "the line turned green." For `boosted_moving_average`
that means deciding (and pre-registering, before any test) things like: enter long on the
bullish flip, exit on the bearish flip vs. a fixed stop/target vs. a holding period. That
decision is exactly the kind of adaptive choice the pre-registration's Stage 4 FDR
correction exists to guard against, so any indicator promoted from this list needs its own
one-line rule spec written down before it's translated, same as a strategy script.
