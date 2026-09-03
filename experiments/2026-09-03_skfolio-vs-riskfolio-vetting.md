# skfolio vs. Riskfolio-Lib — GO/NO-GO vetting for correlation-aware HRP sizing

**Date:** 2026-09-03
**Trigger:** TASKS.md's inverse-vol/portfolio-allocation follow-up — "Revisit the
skfolio vs. Riskfolio-Lib vs. hand-rolled decision now that entry-time inverse-vol
sizing proves the 'hand-roll it' pattern works." Research only; nothing installed,
`requirements.txt` untouched, no code changed.

## Question

Entry-time inverse-vol sizing (`evaluation/execution.py`'s `Sizing.mode=
"inverse_vol"`, built earlier this session) deliberately ignores cross-symbol
correlation — it sizes each trade against its own trailing volatility only. A TRUE
risk-parity / Hierarchical Risk Parity (HRP) sizer needs correlation-based
clustering across concurrently-held names. Is `skfolio` or `Riskfolio-Lib` worth
adding as a real, declared dependency to build that, or should HRP be hand-rolled
the way the Statistical Jump Model and meta-labeling's logistic regression were
earlier today?

## Method

Checked each package's PyPI/GitHub metadata for license, current version and
release cadence, Python version support, and — the deciding factor for a repo with
a strict pinned-`requirements.txt` discipline — the FULL transitive dependency
list each would actually add, not just the headline library. Cross-checked those
dependency version floors against this repo's own pins in `requirements.txt`.
Confirmed both packages' APIs work off an already-in-hand returns/covariance
matrix (no live data feed requirement) since this repo's use case is backtesting,
not live trading.

## Findings

### skfolio

- **License**: BSD 3-Clause. Python >= 3.10 (this repo runs 3.11.5 — fine).
- **Maintenance**: actively developed (repo updated within the last month as of
  this vetting), backed by a company (Skfolio Labs) offering commercial support —
  a healthier institutional-backing signal than a solo-maintainer project.
  ~2.2k GitHub stars.
- **Dependencies** (from its own `pyproject.toml`): `numpy>=1.23.4`,
  `scipy>=1.8.0`, `pandas>=1.4.1`, `cvxpy-base>=1.5.0`, `clarabel>=0.9.0`,
  `scikit-learn>=1.6.0`, `joblib>=1.3.2`, `plotly>=5.22.0`.
- **Version conflicts against this repo's pins**: `plotly==5.9.0` pinned here vs.
  `>=5.22.0` required — a real but low-risk bump (plotly is broadly backward
  compatible; `backtest_app.py`/`generate_tv_rating_report.py` would need a
  regression pass, not a rewrite). `numpy`/`scipy`/`pandas` pins here already
  clear skfolio's floors.
- **The real cost: `scikit-learn>=1.6.0` becomes a HARD dependency.** sklearn is
  installed in this environment but deliberately kept UNDECLARED all session
  (regime detection, meta-labeling, and the leakage probes all specifically
  avoided it, using scipy.optimize/hand-rolled numpy instead, exactly to not
  cross this line). Pulling in skfolio means finally declaring it — and along
  with it `cvxpy-base` + `clarabel` (a convex-optimization interior-point
  solver stack, itself a real binary-wheel dependency) — to get access to ONE
  algorithm class (HRP) inside a much larger library built around convex
  portfolio optimization this repo doesn't otherwise need.

### Riskfolio-Lib

- **License**: BSD 3-Clause. Python >= 3.10 (wheels through 3.14 as of the
  latest 7.3.0 release, dated 2026-05-31).
- **Maintenance**: actively developed (CHANGELOG entries and CI runs through
  2026), ~3.8k GitHub stars (more than skfolio), but a much smaller contributor
  base (<=10) — a single-maintainer bus-factor risk skfolio's institutional
  backing doesn't share.
- **Dependencies**: `numpy>=1.26.4`, `scipy>=1.16.1`, `pandas>=2.2.2`,
  `matplotlib>=3.9.2`, `clarabel>=0.11.1`, `SCS>=3.2.7`, `cvxpy>=1.6.6`,
  `scikit-learn>=1.3.0`, `statsmodels>=0.14.5`, `arch>=7.2`,
  `xlsxwriter>=3.2.2`, `networkx>=3.4.2`, `astropy>=6.1.3`, `pybind11>=2.13.6`,
  **`vectorbt>=0.28.0`**.
- **Version conflicts against this repo's pins**: `scipy==1.10.1` pinned here vs.
  `>=1.16.1` required — a HARD, real conflict (not a formality: scipy is used
  throughout `evaluation/` for `optimize.minimize`, `stats`, and today's new
  regime/meta-label code; bumping 6 minor versions needs its own regression
  pass across everything that touches it, not a drive-by bump alongside an
  unrelated sizing feature).
- **The real cost is much larger than skfolio's**: on top of the same
  cvxpy/scikit-learn exposure, Riskfolio-Lib drags in **`vectorbt`** (a full,
  separate backtesting framework with its own numba JIT-compiled core — this
  repo already has its own backtest engines, `backtest.py` and
  `evaluation/trades.py`; a second, unrelated backtesting framework arriving as
  a TRANSITIVE dependency of a sizing feature is exactly the kind of dependency
  bloat this session's "no new deps" discipline exists to prevent), plus
  `astropy` (an astronomy library — almost certainly pulled in for one numerical
  routine, not because this is astronomy-adjacent), `pybind11`, `statsmodels`,
  `arch` (GARCH modeling), `networkx`, and `xlsxwriter`. None of these are
  needed for HRP specifically.

### Does either actually give a clean HRP API on a known covariance matrix?

Yes, both do — `skfolio`'s `HierarchicalRiskParity` estimator and
Riskfolio-Lib's `HCPortfolio` class both take a returns DataFrame (or covariance)
and return weights, no live feed required, both usable inside a backtest loop.
That part of the ask is satisfied by either. The blocker is entirely the
dependency cost of getting there, not a capability gap.

### Hand-rolled alternative

Lopez de Prado's original HRP algorithm (2016) is four well-defined steps: (1) a
distance matrix from the correlation matrix, (2) hierarchical clustering, (3)
quasi-diagonalization (reordering assets by the cluster dendrogram), (4) recursive
bisection allocating inverse-variance weights down the tree. Steps 1 and 4 are
pure numpy. Step 2 is exactly `scipy.cluster.hierarchy.linkage` — **already
available for free**, since `scipy` is already a declared, pinned dependency of
this repo and `scipy.cluster.hierarchy` ships in scipy's base install (no `[extra]`
needed). Step 3 is a straightforward recursive walk of the linkage tree. This is
the same shape of "well-understood algorithm, hand-rollable in a few hundred
lines against a dependency already in the repo" pattern this session already used
successfully for the Statistical Jump Model (`evaluation/regime.py`) and the
meta-labeling logistic regression (`evaluation/meta_label.py`) — both chose scipy
over pulling in a heavier library for the same reason.

## Verdict

**Riskfolio-Lib: NO-GO, not close.** A hard `scipy>=1.16.1` conflict against this
repo's pinned `scipy==1.10.1`, plus a transitive dependency list (`vectorbt`,
`astropy`, `pybind11`, `statsmodels`, `arch`, `networkx`, `xlsxwriter`, on top of
`cvxpy`/`scikit-learn`) wildly disproportionate to "compute HRP weights from a
correlation matrix." A second full backtesting framework (`vectorbt`) arriving as
a transitive dependency of a position-sizing feature is disqualifying on its own.

**skfolio: NO-GO for now, but the closer call.** Lighter and better-backed than
Riskfolio-Lib, and its dependency floors mostly clear this repo's pins except
`plotly` (a minor, low-risk bump). But it still forces declaring `scikit-learn`
and adding a convex-optimization solver stack (`cvxpy-base`/`clarabel`) — real,
permanent additions to `requirements.txt` and the install surface of every
pipeline in this repo — to reach one estimator class this repo needs, when the
underlying algorithm doesn't actually require a convex solver at all.

**Recommendation: hand-roll HRP with `scipy.cluster.hierarchy`, zero new
dependencies.** This is not a "buy vs. build" toss-up — it's the same call this
session already made twice today for comparably well-defined algorithms, and the
dependency math here is even more lopsided than those were. If a FUTURE feature
genuinely needs full convex portfolio optimization (mean-variance with real
constraints, CVaR-constrained optimization, multi-period rebalancing under
transaction-cost penalties — problems that actually need a solver, not just
clustering), that is the point to revisit `skfolio` specifically (not
Riskfolio-Lib), since its dependency footprint is the more defensible of the two
and its institutional backing is a better bus-factor bet. Not today, for HRP
alone.

### If a hand-rolled HRP is built next

Rough integration surface, for whoever picks this up: a new
`evaluation/hrp.py` (mirroring `evaluation/regime.py`'s shape — pure numpy/scipy,
own module, own tests) exposing `hrp_weights(returns: pd.DataFrame) -> pd.Series`,
then a new `Sizing.mode="hrp"` in `evaluation/execution.py` alongside the existing
`inverse_vol` mode — but sizing a SET of concurrently-held positions from one HRP
call is a different shape than `inverse_vol`'s per-trade-at-entry sizing, and
would need the `_portfolio_pass`/`simulate_symbol` admission-order rework this
same TASKS.md section already flags as its own design-doc-worthy project. HRP
weights and the single-pass engine rewrite are two separate pieces of work that
happen to share a dependency decision, not one task.

Sources:
- [skfolio GitHub](https://github.com/skfolio/skfolio)
- [skfolio: Portfolio Optimization in Python (arXiv)](https://arxiv.org/abs/2507.04176)
- [skfolio installation docs](https://skfolio.org/user_guide/install.html)
- [Riskfolio-Lib GitHub](https://github.com/dcajasn/Riskfolio-Lib)
- [Riskfolio-Lib PyPI](https://pypi.org/project/riskfolio-lib/)
- [Riskfolio-Lib install docs](https://riskfolio-lib.readthedocs.io/en/latest/install.html)
- [Riskfolio-Lib setup.py](https://github.com/dcajasn/Riskfolio-Lib/blob/master/setup.py)
