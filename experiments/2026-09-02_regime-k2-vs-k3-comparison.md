# Statistical Jump Model regime detection — k=2 vs k=3

**Date:** 2026-09-02
**Script:** `experiments/regime_k_comparison.py`
**Data:** SPY daily closes, 1993-02-01 .. 2026-09-01 (8,454 return days)

## Question

`evaluation/regime.py`'s `label_regimes()` already takes `k` as a free parameter.
TASKS.md's ask: try k=3 (e.g. calm/choppy/crisis) and check whether it's actually
better than k=2, "rather than assuming more regimes is better just because it's
prettier."

## Why not literal CPCV

`robustness.cpcv_splits()` builds combinatorial, non-contiguous train/test groups with
purge+embargo gaps between them. The Statistical Jump Model's entire reason to exist
over plain k-means is `jump_penalty`, a cost charged for the fitted state changing
between **consecutive calendar days**. Feed it a CPCV fold with chunks removed and
array-position adjacency silently stops meaning calendar adjacency — the penalty starts
charging single-day-flip cost across gaps of days or weeks, corrupting the exact
mechanic being fit. A combinatorial CV is the wrong tool for an adjacency-dependent
model. This comparison uses a plain chronological split (first half vs second half)
instead, and says so rather than mislabeling it CPCV to check a box.

## Results

**Full-sample fit (in-sample):**

| k | regime | n_days | ann_return | ann_vol |
|---|--------|-------:|-----------:|--------:|
| 2 | 0 (worst) | 1,404 | -27.2% | 34.0% |
| 2 | 1 (best)  | 7,030 |  17.7% | 13.5% |
| 3 | 0 (worst) |   173 | -19.1% | 61.6% |
| 3 | 1         | 2,234 | -12.1% | 25.2% |
| 3 | 2 (best)  | 6,027 |  19.3% | 11.8% |

k=2's split lines up cleanly with the intuitive calm/stressed story (and, per the
2026-09-02 regime.py build note, the stressed regime's dates land on 2008 GFC / 2020
COVID). k=3 splits the stressed regime into a small, high-vol tail cluster (173 days,
61.6% vol — plausibly a genuine "crisis" tail) plus a larger "choppy" middle cluster.

**Chronological walk-forward stability (fit independently on each half):**

| k | half | n_switches | best-worst spread |
|---|------|-----------:|-------------------:|
| 2 | first  | 22 | 45.7 pp/yr |
| 2 | second | 40 | 45.2 pp/yr |
| 3 | first  | 54 | 49.5 pp/yr |
| 3 | second | 74 | 32.6 pp/yr |

k=2 is stable: both halves independently find a calm regime around 11-20%/yr and a
stressed regime around -25% to -35%/yr, with a consistent ~45pp/yr spread and a small
minority of days in the stressed state.

k=3 is NOT stable. In the first half the third cluster is a genuine small crisis tail
(110 days, -32.1%/yr, 62.9% vol) — consistent with the full-sample story. In the
**second half**, the worst-return cluster is a tiny 63-day grab-bag with +3.5%/yr
return but 59.6% vol (a noisy volatility cluster, not a coherent "crisis" regime), and
the actual moderate-drawdown cluster (980 days, -11.7%/yr, 24.8% vol) isn't clearly
distinguishable in character from k=2's single stressed regime. n_switches also grew
faster than k=2's (74 vs 40 in the second half) — more, choppier state flips, the
signature of a model finding structure in noise rather than a persistent third regime.

## Verdict

**k=2 is the more robust default.** It reproduces the same calm/stressed split with a
consistent spread on both independent halves of 30+ years of data. k=3's third cluster
is real in one half and noise in the other — exactly the "prettier, not necessarily
better" failure TASKS.md was skeptical of. k=3 is not being wired in as a default;
`label_regimes(k=3)` remains available for a caller who wants to try it on a specific
window, with this instability documented as the reason to look hard at `n_switches`
and regime character (not just count) before trusting a k=3 fit on new data.

**No code change from this experiment** — `label_regimes()` already supports arbitrary
`k`. This closes the "try k=3 and compare" TASKS.md item as an empirical finding.
