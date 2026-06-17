# Options Pricing Models — Research Log

> Research compiled for the **synthetic / theoretical historical options pricing** pipeline in
> `financial-data-pipeline`. Goal: estimate historical option prices (and Greeks) at any interval by
> sourcing each model input from data we already collect.

---

## 0. The core idea

Every closed-form equity-option model is a function:

```
price, greeks = f(S, K, T, r, q, σ [, model-specific params])
```

"Synthetic" pricing means: instead of reading a market quote, we **supply every input ourselves** and
compute the theoretical price. Five of the six inputs are observable/derivable from data we already
have. The sixth — **volatility σ** — is the modeling problem, because the "true" forward-looking vol is
unobservable. Everything below is organized around sourcing those inputs and choosing the function `f`.

### Universal input set (what *every* model needs)

| Symbol | Meaning | Units | Source in this repo |
|--------|---------|-------|---------------------|
| `S` | Underlying spot price | $ | `price_history` parquet (`close`); intraday from Schwab if needed |
| `K` | Strike price | $ | We generate a **moneyness grid** around S (e.g. 0.70–1.30 × S) |
| `T` | Time to expiration | years | Computed: `(expiry − asof) / 365` (or /252 for trading-day basis) |
| `r` | Risk-free rate (cont. comp.) | decimal/yr | FRED treasury curve `DGS1MO…DGS30`, **interpolated to T** |
| `q` | Continuous dividend yield | decimal/yr | `yfinance` dividends (TTM / S), or 0 for non-payers |
| `σ` | Volatility | decimal/yr | **The hard input** — see §7. v1 = realized vol from `price_history` |
| `option_type` | call / put | — | Both, per contract |

Model-specific extras: Heston needs `{v0, κ, θ, ξ, ρ}`; SABR needs `{α, β, ρ, ν}`; binomial needs `N` (steps).

---

## 1. Black–Scholes–Merton (BSM) with continuous dividend yield — **recommended v1**

European options. Merton (1973) extended Black–Scholes (1973) to a continuous dividend yield `q`.
Closed-form, instant, fully vectorizable with `numpy` + `scipy.stats.norm`. No external pricing library
required.

**Notation:** `N(·)` = standard normal CDF, `n(·)` = standard normal PDF.

```
d1 = [ ln(S/K) + (r − q + σ²/2)·T ] / (σ·√T)
d2 = d1 − σ·√T

Call C = S·e^(−qT)·N(d1) − K·e^(−rT)·N(d2)
Put  P = K·e^(−rT)·N(−d2) − S·e^(−qT)·N(−d1)
```

**Greeks** (these are exactly the fields Schwab returns per contract: delta, gamma, theta, vega, plus IV):

```
Delta  call = e^(−qT)·N(d1)
Delta  put  = −e^(−qT)·N(−d1)
Gamma       = e^(−qT)·n(d1) / (S·σ·√T)                      # same for call & put
Vega        = S·e^(−qT)·n(d1)·√T                            # same; /100 for "per 1 vol point"
Theta call  = −[S·n(d1)·σ·e^(−qT)]/(2√T) − r·K·e^(−rT)·N(d2)  + q·S·e^(−qT)·N(d1)
Theta put   = −[S·n(d1)·σ·e^(−qT)]/(2√T) + r·K·e^(−rT)·N(−d2) − q·S·e^(−qT)·N(−d1)   # /365 for per-day
Rho   call  = K·T·e^(−rT)·N(d2)
Rho   put   = −K·T·e^(−rT)·N(−d2)
```

**Generalized cost-of-carry form (`b`)** — one code path covers four models by swapping `b`:

```
d1 = [ ln(S/K) + (b + σ²/2)·T ] / (σ√T)
C  = S·e^((b−r)T)·N(d1) − K·e^(−rT)·N(d2)
  b = r        → Black-Scholes 1973 (no dividend)
  b = r − q    → Merton 1973 (continuous dividend)   ← our default
  b = 0        → Black-76 (options on futures)
  b = r − r_f  → Garman-Kohlhagen (FX)
```

**Assumptions / limits:** European exercise (no early exercise), constant σ and r, lognormal returns,
no jumps. For US single-name equity options (American), it slightly mis-prices in-the-money puts and
calls on dividend payers — acceptable for a v1 *estimate*; upgrade path is §3/§4.

**Inputs:** S, K, T, r, q, σ. **Refs:** Macroption formula sheet; Columbia FE notes (Hull); Merton 1973.

---

## 2. Black-76 — options on futures/forwards

Special case of the generalized form with `b = 0`, priced off the **forward/futures price F** instead
of spot:

```
d1 = [ ln(F/K) + (σ²/2)T ] / (σ√T),  d2 = d1 − σ√T
C = e^(−rT)·[ F·N(d1) − K·N(d2) ]
P = e^(−rT)·[ K·N(−d2) − F·N(−d1) ]
```

**Use:** relevant if we ever price options on the `futures_pipeline` contracts. **Inputs:** F, K, T, r, σ.

---

## 3. Discrete dividends (escrowed-dividend adjustment)

Real equities pay **discrete** dividends, not a continuous yield. Cheap correction without leaving BSM:
replace spot with spot net of the present value of dividends paid before expiry.

```
S_adj = S − Σ Dᵢ·e^(−r·tᵢ)     (tᵢ = time to each ex-div date < T)
```

then price with BSM using `S_adj` and `q = 0`. More accurate than a flat `q` for names with lumpy
dividends. **Inputs:** dividend schedule (amounts + ex-dates) from `yfinance`.

---

## 4. American exercise models (early-exercise premium)

US single-name equity options are **American**. Three practical routes, increasing fidelity/cost:

### 4a. Cox–Ross–Rubinstein binomial tree (1979) — exact-ish, simple

```
dt = T/N
u = e^(σ√dt),  d = 1/u
p = (e^((r−q)dt) − d) / (u − d)              # risk-neutral up-prob
```
Build price lattice forward; at maturity payoff = max(±(S−K), 0); roll back discounting by `e^(−r·dt)`,
and at each node take `max(intrinsic, continuation)` to capture early exercise. Converges to BSM as
`N→∞` (use N≈200–1000). Greeks via finite differences on the tree. **Pros:** handles American + discrete
divs naturally. **Cons:** slower, not closed-form. **Ref:** Cox-Ross-Rubinstein 1979 "Option Pricing: A Simplified Approach."

### 4b. Barone–Adesi–Whaley (1987) — quadratic approximation

Splits value into European (BSM) part + an early-exercise premium of power-function form, solving a
quadratic for the critical exercise price S*. Fast, analytic, accurate for short/medium maturities.
Works for futures options (set `b=0`). **Ref:** Barone-Adesi & Whaley 1987, *J. Finance*.

### 4c. Bjerksund–Stensland 2002 — closed-form American, **recommended American upgrade**

Closed-form approximation that splits time to maturity into **two periods, each with a flat early-exercise
boundary (trigger price)**. More accurate than Barone-Adesi-Whaley and BS-1993, and "extremely computer
efficient." Prices American calls/puts with continuous dividend yield (put via the BS put-call
transformation). Building blocks: trigger prices `I1, I2`, parameter `β`, and the `φ` (phi) and `ψ`
(psi) helper functions built on the bivariate/uni normal CDF.

- Put-call transformation: value an American put as an American call with S↔K and rate/carry swapped
  (standard BS2002 trick).
- **Pros:** closed-form, fast, dividend-aware, good accuracy. **Cons:** more code than BSM; approximation
  degrades for very long-dated/deep options.
- **Ref:** Bjerksund & Stensland (2002) "Closed Form Valuation of American Options," NHH Bergen.
  Also implemented in MATLAB `optstockbybjs` and `py_vollib`-adjacent libs.

---

## 5. Stochastic-volatility & surface models (skew/smile — advanced)

These exist because real markets show a **volatility smile/skew** (IV varies by strike and maturity),
which flat-σ BSM cannot reproduce. Relevant only if we want synthetic prices that match the *shape* of
real chains, not just the ATM level.

### 5a. Heston (1993) — stochastic volatility, closed-form via characteristic function

Variance follows a mean-reverting CIR process correlated with spot:

```
dS = (r−q)·S·dt + √v·S·dW1
dv = κ(θ − v)·dt + ξ·√v·dW2,    corr(dW1,dW2) = ρ
```
Parameters: `v0` (initial variance), `κ` (mean-reversion speed), `θ` (long-run variance), `ξ` (vol of
vol), `ρ` (spot/vol correlation). European price via semi-closed-form integral of the **characteristic
function** (Heston 1993; Carr–Madan FFT for speed). Feller condition `2κθ ≥ ξ²` keeps variance positive.
**Calibration:** fit the 5 params to a snapshot of real chain IVs (needs stored real chains). **Ref:**
Heston 1993, *Review of Financial Studies*.

### 5b. SABR (Hagan et al. 2002) — implied-vol skew model

```
dF = α·F^β·dW1
dα = ν·α·dW2,    corr(dW1,dW2) = ρ
```
Parameters: `α` (level of vol), `β` (CEV exponent, fixed in [0,1], often 0.5 or 1), `ρ` (skew slope),
`ν` (vol of vol → smile curvature). Hagan's asymptotic formula gives a **Black implied vol** for any
(K,T), which you then feed into Black-76/BSM. Industry standard for interpolating/extrapolating a vol
surface. Breaks down for very long T or deep OTM. **Ref:** Hagan, Kumar, Lesniewski, Woodward 2002,
"Managing Smile Risk."

### 5c. Dupire local volatility & Monte Carlo — mention only

- **Local vol (Dupire):** σ becomes a deterministic function σ(S,t) calibrated to fit the entire
  observed surface exactly. Needs a dense, arbitrage-free real surface as input — out of scope until we
  store real chains.
- **Monte Carlo:** simulate many GBM/Heston paths, average discounted payoff. Universal (path-dependent,
  exotic) but slow; only needed for non-vanilla payoffs. Not required here.

---

## 6. Risk-free rate `r` — interpolating the treasury curve to T

We have the full curve in the FRED macro parquet: `DGS1MO, DGS3MO, DGS6MO, DGS1, DGS2, DGS5, DGS7,
DGS10, DGS20, DGS30` (annualized **par yields, in percent**).

1. Map each tenor to years: 1MO=0.0833, 3MO=0.25, 6MO=0.5, 1, 2, 5, 7, 10, 20, 30.
2. For a contract with maturity `T`, **linearly interpolate** the yield at `T` (`numpy.interp`), using
   the curve **as of the same date** as the spot.
3. Convert percent → decimal (`/100`). Optionally convert to continuous compounding:
   `r_cc = ln(1 + r_annual)` (small effect at these tenors; document the choice).

---

## 7. Volatility `σ` — the hard input (decision point)

Forward-looking vol is unobservable. Options, cheapest → richest:

### 7a. Historical / realized volatility (close-to-close) — **recommended v1, self-contained**
```
rᵢ = ln(Sᵢ / Sᵢ₋₁)
σ_daily = sqrt( (1/(n−1)) · Σ (rᵢ − r̄)² )
σ_annual = σ_daily · √252
```
Computed directly from `price_history` log returns over a rolling window (e.g. 20/30/60/90 trading
days). Simple, transparent, free. **Caveat:** backward-looking, no skew, no term structure — produces a
*flat* surface. Good enough for a first synthetic estimate.

### 7b. Range-based estimators (more efficient than close-to-close, use OHLC we already store)
- **Parkinson:** uses high–low only. ~5× more efficient than close-to-close.
- **Garman–Klass:** uses OHLC. More efficient still; assumes no drift/jumps.
- **Rogers–Satchell:** drift-independent.
- **Yang–Zhang:** combines overnight + open-close; **most efficient**, handles drift and opening jumps.
  Best single realized-vol estimator if we want one. (We have O/H/L/C in `price_history`.)

### 7c. EWMA (RiskMetrics) — reactive, recent-weighted
```
σ²_t = λ·σ²_{t−1} + (1−λ)·r²_{t−1}     (λ = 0.94 daily standard)
```
Weights recent returns more; no mean reversion.

### 7d. GARCH(1,1) — forecasts with mean reversion
```
σ²_t = ω + α·r²_{t−1} + β·σ²_{t−1}
long-run variance = ω / (1 − α − β)     (requires α+β<1)
```
Best for multi-day *forecast* vol; needs `arch` library to fit. Adds a dependency.

### 7e. VIX overlay — market-level implied vol regime
We already capture `VIXCLS`. Use it to **scale/blend** per-name realized vol so synthetic prices breathe
with market-wide implied-vol regime (e.g. `σ_used = σ_realized · (VIX_t / mean(VIX))`). Index-level only;
no per-name skew.

### 7f. Implied vol from real chains — richest, needs stored real data
Invert observed option prices to IV and build a surface (then interpolate via SABR §5b). **Blocker:**
`options_chain_pipeline.py` currently **discards the raw per-contract chain** and only saves the daily
metrics summary (`options_metrics_*.parquet`). To use real IV we must first persist raw chains, and
coverage starts only from when capture began (recent, shallow history).

### Implied-vol inversion methodology (needed for 7f and for validation)
Price→IV has no closed form; solve `BSM(σ) − market_price = 0` numerically:
- **Newton–Raphson** using Vega as the derivative — fast (few iterations) but can fail when Vega ≈ 0
  (deep ITM/OTM, near expiry).
- **Brent's method** (`scipy.optimize.brentq`) — bracketing, derivative-free, robust fallback.
- Practical: try Newton, fall back to Brent; or use `py_vollib` (Jäckel "Let's Be Rational", analytic,
  no iteration). **Refs:** QuantStart NR guide; Interactive Brokers IV note; Jäckel 2015.

---

## 8. Recommended approach for the pipeline (synthesis)

**v1 (this build):**
- Model: **BSM with continuous dividend yield** (§1), hand-rolled vectorized `numpy`/`scipy` — zero new deps.
- σ: **close-to-close realized vol** (§7a) over a configurable window; structure code so the σ function is
  swappable (Yang-Zhang §7b, EWMA, VIX overlay can drop in later).
- r: **linear interpolation of the FRED treasury curve to T** (§6).
- q: TTM dividends / spot from `yfinance` (§3), `0` for non-payers.
- Grid: moneyness × DTE grid per (date, symbol); emit call + put rows with all five Greeks + the σ used,
  mirroring Schwab's per-contract schema so synthetic ↔ real are directly comparable.

**Upgrade path (later, in priority order):**
1. Persist raw real chains (small change to `options_chain_pipeline.py`) → enables validation + real IV.
2. Swap σ to Yang-Zhang or EWMA/GARCH; add VIX overlay.
3. American pricing via **Bjerksund-Stensland 2002** (§4c) for dividend payers.
4. Skew/surface via **SABR** (§5b) calibrated to stored real chains; **Heston** (§5a) for full stoch-vol.

### What we still need / open data gaps
- **Dividend data**: `fundamentals` has shares outstanding but **no dividend tags** → must pull from
  `yfinance` (`.dividends`) or add SEC/extra source. Decide continuous `q` vs discrete escrowed (§3).
- **Real per-contract chains not stored** → blocks validation, real-IV, and SABR/Heston calibration.
- **Spot history depth**: `price_history` backfill is only **365 days**, so synthetic history is ~1yr
  unless extended.
- **Trading calendar / day-count**: pin a convention (calendar/365 vs trading/252) for `T` and σ
  annualization and use it consistently.

---

## 9. References (canonical)

- Black & Scholes (1973); **Merton (1973)** "Theory of Rational Option Pricing" — BSM + dividend yield.
- Macroption — Black-Scholes formulas & Greeks: https://www.macroption.com/black-scholes-formula/
- Columbia FE notes (Hull-based): https://www.columbia.edu/~mh2078/FoundationsFE/BlackScholes.pdf
- **Cox, Ross & Rubinstein (1979)** "Option Pricing: A Simplified Approach" (binomial).
- **Barone-Adesi & Whaley (1987)** "Efficient Analytic Approximation of American Option Values," *J. Finance*.
- **Bjerksund & Stensland (2002)** "Closed Form Valuation of American Options," NHH Bergen —
  https://derivativesacademy.com/storage/uploads/files/modules/resources/1703192811_bjerksund_stensland_2002_closed_form_valuation_of_american_options.pdf
- **Heston (1993)** "A Closed-Form Solution for Options with Stochastic Volatility," *Rev. Financial Studies*.
- **Hagan, Kumar, Lesniewski, Woodward (2002)** "Managing Smile Risk" (SABR).
- Volatility estimation overview (EWMA/GARCH): https://ryanoconnellfinance.com/volatility-estimation-garch/
- Columbia — volatility behavior & forecasting: http://www.columbia.edu/~amm26/lecture%20files/volatilityBehaviorForecasting.pdf
- Implied vol inversion (Newton/Brent): https://www.quantstart.com/articles/Implied-Volatility-in-C-using-Template-Functions-and-Newton-Raphson/ ;
  https://www.interactivebrokers.com/campus/ibkr-quant-news/implied-volatility-formulation-computation-and-robust-numerical-methods/
- Library landscape: `py_vollib` / `py_vollib_vectorized` (BSM/Black-76 + Jäckel IV), `QuantLib`
  (American/exotic/binomial), `arch` (GARCH).
