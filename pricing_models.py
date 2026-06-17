"""
pricing_models.py — shared, dependency-free option-pricing math.

Pure, vectorized (numpy) functions reused by synthetic_options_pipeline.py and
validate_synthetic_options.py. No external pricing library required (py_vollib /
QuantLib are NOT installed); everything is built on numpy + scipy.

Contents
--------
- bsm(...)                      European Black-Scholes-Merton + 5 Greeks (cont. dividend yield)
- bjerksund_stensland_2002(...) American closed-form approximation + Greeks (finite difference)
- implied_vol(...)             invert a price -> sigma (Newton-Raphson, Brent fallback)
- realized_vol_cc / _yang_zhang / vix_overlay   the three sigma estimators
- interp_rate(...)             interpolate the treasury curve to maturity T (continuous comp.)

Conventions
-----------
- T is in YEARS (calendar days / 365).
- Volatility sigma is ANNUALIZED (realized estimators use sqrt(252)).
- Greeks follow common broker conventions: theta is PER DAY (/365), vega is
  PER 1 VOL POINT (/100), rho is PER 1% RATE (/100); delta/gamma are raw.

See docs/options_pricing_research.md for the underlying formulas and references.
"""

import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq

TRADING_DAYS = 252.0
SQRT_TD = np.sqrt(TRADING_DAYS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _call_mask(option_type, shape):
    """Broadcast an option_type (scalar str or array of str) to a boolean is-call mask."""
    ot = np.asarray(option_type)
    is_call = np.char.lower(ot.astype(str).astype("U8")) == "call"
    return np.broadcast_to(is_call, shape)


# ---------------------------------------------------------------------------
# European Black-Scholes-Merton (continuous dividend yield q)
# ---------------------------------------------------------------------------

def bsm(S, K, T, r, q, sigma, option_type="call"):
    """Vectorized European BSM price + Greeks.

    Returns a dict of numpy arrays: price, delta, gamma, theta, vega, rho.
    Cost-of-carry form uses b = r - q internally. Degenerate inputs
    (T <= 0 or sigma <= 0) collapse to intrinsic value with zero/step Greeks.
    """
    S, K, T, r, q, sigma = np.broadcast_arrays(
        *[np.asarray(x, dtype=float) for x in (S, K, T, r, q, sigma)]
    )
    is_call = _call_mask(option_type, S.shape)

    sqrtT = np.sqrt(np.maximum(T, 0.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_sqrtT = sigma * sqrtT
        d1 = (np.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / vol_sqrtT
        d2 = d1 - vol_sqrtT

    Nd1, Nd2 = norm.cdf(d1), norm.cdf(d2)
    Nnd1, Nnd2 = norm.cdf(-d1), norm.cdf(-d2)
    nd1 = norm.pdf(d1)
    disc_r = np.exp(-r * T)
    disc_q = np.exp(-q * T)

    call_price = S * disc_q * Nd1 - K * disc_r * Nd2
    put_price = K * disc_r * Nnd2 - S * disc_q * Nnd1
    price = np.where(is_call, call_price, put_price)

    delta = np.where(is_call, disc_q * Nd1, -disc_q * Nnd1)
    with np.errstate(divide="ignore", invalid="ignore"):
        gamma = disc_q * nd1 / (S * vol_sqrtT)
    vega = S * disc_q * nd1 * sqrtT / 100.0

    theta_common = -(S * nd1 * sigma * disc_q) / (2.0 * np.where(sqrtT > 0, sqrtT, np.nan))
    theta_call = (theta_common - r * K * disc_r * Nd2 + q * S * disc_q * Nd1) / 365.0
    theta_put = (theta_common + r * K * disc_r * Nnd2 - q * S * disc_q * Nnd1) / 365.0
    theta = np.where(is_call, theta_call, theta_put)

    rho = np.where(is_call, K * T * disc_r * Nd2, -K * T * disc_r * Nnd2) / 100.0

    # Degenerate: no time value -> intrinsic, Greeks ~ 0 (delta = step)
    degen = (T <= 0) | (sigma <= 0)
    if np.any(degen):
        intrinsic = np.where(is_call, np.maximum(S - K, 0.0), np.maximum(K - S, 0.0))
        step = np.where(is_call, (S > K).astype(float), -(S < K).astype(float))
        price = np.where(degen, intrinsic, price)
        delta = np.where(degen, step, delta)
        gamma = np.where(degen, 0.0, gamma)
        vega = np.where(degen, 0.0, vega)
        theta = np.where(degen, 0.0, theta)
        rho = np.where(degen, 0.0, rho)

    return {"price": price, "delta": delta, "gamma": gamma,
            "theta": theta, "vega": vega, "rho": rho}


# ---------------------------------------------------------------------------
# Bivariate normal CDF (vectorized via integration over the correlation)
#   Phi2(h,k,rho) = Phi(h)Phi(k) + integral_0^rho phi2(h,k,r) dr
# Accurate and fully vectorized; rho in BS2002 is fixed ~0.786, well away from +-1.
# ---------------------------------------------------------------------------

_GL_NODES, _GL_WEIGHTS = np.polynomial.legendre.leggauss(24)


def cbnd(h, k, rho):
    h, k, rho = np.broadcast_arrays(
        np.asarray(h, float), np.asarray(k, float), np.asarray(rho, float)
    )
    base = norm.cdf(h) * norm.cdf(k)
    # Map Gauss-Legendre nodes from [-1,1] to [0, rho] elementwise.
    half = rho[..., None] / 2.0
    rr = half * (_GL_NODES + 1.0)
    h_ = h[..., None]
    k_ = k[..., None]
    denom = 1.0 - rr ** 2
    with np.errstate(divide="ignore", invalid="ignore"):
        integrand = np.exp(-(h_ ** 2 - 2.0 * rr * h_ * k_ + k_ ** 2) / (2.0 * denom)) / (
            2.0 * np.pi * np.sqrt(denom)
        )
    integral = (half * _GL_WEIGHTS * integrand).sum(axis=-1)
    return base + integral


# ---------------------------------------------------------------------------
# Bjerksund-Stensland (2002) American approximation
# Internal functions work in (S, X, T, r, b, v) space; b = cost of carry.
# ---------------------------------------------------------------------------

def _gbs_call(S, X, T, r, b, v):
    """Generalized Black-Scholes European call (cost of carry b)."""
    vt = v * np.sqrt(T)
    d1 = (np.log(S / X) + (b + 0.5 * v ** 2) * T) / vt
    d2 = d1 - vt
    return S * np.exp((b - r) * T) * norm.cdf(d1) - X * np.exp(-r * T) * norm.cdf(d2)


def _phi(S, T, gamma, H, I, r, b, v):
    v2 = v ** 2
    vt = v * np.sqrt(T)
    lam = -r + gamma * b + 0.5 * gamma * (gamma - 1.0) * v2
    d = -(np.log(S / H) + (b + (gamma - 0.5) * v2) * T) / vt
    kappa = 2.0 * b / v2 + (2.0 * gamma - 1.0)
    return np.exp(lam * T) * S ** gamma * (
        norm.cdf(d) - (I / S) ** kappa * norm.cdf(d - 2.0 * np.log(I / S) / vt)
    )


def _psi(S, T2, gamma, H, I2, I1, t1, r, b, v):
    v2 = v ** 2
    vt1 = v * np.sqrt(t1)
    vT2 = v * np.sqrt(T2)
    drift = b + (gamma - 0.5) * v2

    e1 = (np.log(S / I1) + drift * t1) / vt1
    e2 = (np.log(I2 ** 2 / (S * I1)) + drift * t1) / vt1
    e3 = (np.log(S / I1) - drift * t1) / vt1
    e4 = (np.log(I2 ** 2 / (S * I1)) - drift * t1) / vt1

    f1 = (np.log(S / H) + drift * T2) / vT2
    f2 = (np.log(I2 ** 2 / (S * H)) + drift * T2) / vT2
    f3 = (np.log(I1 ** 2 / (S * H)) + drift * T2) / vT2
    f4 = (np.log(S * I1 ** 2 / (H * I2 ** 2)) + drift * T2) / vT2

    rho = np.sqrt(t1 / T2)
    lam = -r + gamma * b + 0.5 * gamma * (gamma - 1.0) * v2
    kappa = 2.0 * b / v2 + (2.0 * gamma - 1.0)

    return np.exp(lam * T2) * S ** gamma * (
        cbnd(-e1, -f1, rho)
        - (I2 / S) ** kappa * cbnd(-e2, -f2, rho)
        - (I1 / S) ** kappa * cbnd(-e3, -f3, -rho)
        + (I1 / I2) ** kappa * cbnd(-e4, -f4, -rho)
    )


def _bs2002_call(S, X, T, r, b, v):
    """American call via Bjerksund-Stensland 2002 (arrays ok).

    Where b >= r the call is never exercised early, so the BS2002 trigger
    formula is bypassed in favor of the European value. The formula branch is
    still evaluated for those cells (then discarded by np.where), which can
    divide by zero (beta-1 == 0, r-b == 0); errors are silenced accordingly.
    """
    v2 = v ** 2
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        return _bs2002_call_inner(S, X, T, r, b, v, v2)


def _bs2002_call_inner(S, X, T, r, b, v, v2):
    beta = (0.5 - b / v2) + np.sqrt((b / v2 - 0.5) ** 2 + 2.0 * r / v2)
    b_inf = beta / (beta - 1.0) * X
    # r - b > 0 here (only used where b < r); guard anyway
    b0 = np.maximum(X, np.where(np.abs(r - b) > 1e-12, r / (r - b) * X, X))

    t1 = 0.5 * (np.sqrt(5.0) - 1.0) * T
    spread = (b_inf - b0) * b0
    ht1 = -(b * t1 + 2.0 * v * np.sqrt(t1)) * X ** 2 / spread
    ht2 = -(b * T + 2.0 * v * np.sqrt(T)) * X ** 2 / spread
    i1 = b0 + (b_inf - b0) * (1.0 - np.exp(ht1))
    i2 = b0 + (b_inf - b0) * (1.0 - np.exp(ht2))
    alpha1 = (i1 - X) * i1 ** (-beta)
    alpha2 = (i2 - X) * i2 ** (-beta)

    formula = (
        alpha2 * S ** beta
        - alpha2 * _phi(S, t1, beta, i2, i2, r, b, v)
        + _phi(S, t1, 1.0, i2, i2, r, b, v)
        - _phi(S, t1, 1.0, i1, i2, r, b, v)
        - X * _phi(S, t1, 0.0, i2, i2, r, b, v)
        + X * _phi(S, t1, 0.0, i1, i2, r, b, v)
        + alpha1 * _phi(S, t1, beta, i1, i2, r, b, v)
        - alpha1 * _psi(S, T, beta, i1, i2, i1, t1, r, b, v)
        + _psi(S, T, 1.0, i1, i2, i1, t1, r, b, v)
        - _psi(S, T, 1.0, X, i2, i1, t1, r, b, v)
        - X * _psi(S, T, 0.0, i1, i2, i1, t1, r, b, v)
        + X * _psi(S, T, 0.0, X, i2, i1, t1, r, b, v)
    )
    price = np.where(S >= i2, S - X, formula)
    # When b >= r early exercise is never optimal -> European value.
    eur = _gbs_call(S, X, T, r, b, v)
    return np.where(b >= r, eur, price)


def _amer_price(S, K, T, r, q, v, is_call):
    """American price for calls and puts (put via BS symmetry transformation)."""
    b = r - q
    call_p = _bs2002_call(S, K, T, r, b, v)
    # American put(S,K,r,b) = American call(K,S, r'=r-b=q, b'=-b=q-r)
    put_p = _bs2002_call(K, S, T, q, q - r, v)
    return np.where(is_call, call_p, put_p)


def bjerksund_stensland_2002(S, K, T, r, q, sigma, option_type="call"):
    """Vectorized American price + Greeks (Greeks by central finite difference).

    Returns a dict of numpy arrays: price, delta, gamma, theta, vega, rho.
    """
    S, K, T, r, q, sigma = np.broadcast_arrays(
        *[np.asarray(x, dtype=float) for x in (S, K, T, r, q, sigma)]
    )
    is_call = _call_mask(option_type, S.shape)

    valid = (T > 0) & (sigma > 0)
    # Use safe inputs for the formula, then overwrite degenerate cells.
    Ts = np.where(valid, T, 1.0)
    vs = np.where(valid, sigma, 0.2)

    def price_at(s=S, k=K, t=Ts, rr=r, qq=q, vv=vs):
        return _amer_price(s, k, t, rr, qq, vv, is_call)

    price = price_at()

    hS = 0.01 * S
    p_up = price_at(s=S + hS)
    p_dn = price_at(s=S - hS)
    delta = (p_up - p_dn) / (2.0 * hS)
    gamma = (p_up - 2.0 * price + p_dn) / (hS ** 2)

    dv = 0.01
    vega = (price_at(vv=vs + dv) - price_at(vv=vs - dv)) / (2.0 * dv) * 0.01  # per 1 vol pt

    one_day = 1.0 / 365.0
    t_dn = np.maximum(Ts - one_day, 1e-6)
    theta = price_at(t=t_dn) - price  # one-day decay

    dr = 1e-4
    rho = (price_at(rr=r + dr) - price_at(rr=r - dr)) / (2.0 * dr) * 0.01  # per 1% rate

    if np.any(~valid):
        intrinsic = np.where(is_call, np.maximum(S - K, 0.0), np.maximum(K - S, 0.0))
        step = np.where(is_call, (S > K).astype(float), -(S < K).astype(float))
        price = np.where(valid, price, intrinsic)
        delta = np.where(valid, delta, step)
        gamma = np.where(valid, gamma, 0.0)
        vega = np.where(valid, vega, 0.0)
        theta = np.where(valid, theta, 0.0)
        rho = np.where(valid, rho, 0.0)

    return {"price": price, "delta": delta, "gamma": gamma,
            "theta": theta, "vega": vega, "rho": rho}


# ---------------------------------------------------------------------------
# Implied volatility (invert a market price -> sigma)
# ---------------------------------------------------------------------------

def implied_vol(price, S, K, T, r, q, option_type="call", tol=1e-6, max_iter=100):
    """Scalar implied vol via Newton-Raphson with a Brent fallback.

    Returns np.nan if the price is below intrinsic or no root is found.
    """
    price = float(price); S = float(S); K = float(K); T = float(T)
    r = float(r); q = float(q)
    if not np.isfinite(price) or T <= 0 or price <= 0:
        return np.nan
    intrinsic = max(S - K, 0.0) if str(option_type).lower() == "call" else max(K - S, 0.0)
    intrinsic *= np.exp(-r * T)
    if price < intrinsic - 1e-8:
        return np.nan

    def f(sig):
        return float(bsm(S, K, T, r, q, sig, option_type)["price"]) - price

    # Newton-Raphson seeded near typical equity vol.
    sig = 0.25
    for _ in range(max_iter):
        res = bsm(S, K, T, r, q, sig, option_type)
        diff = float(res["price"]) - price
        v = float(res["vega"]) * 100.0  # back to per-1.0-vol
        if abs(diff) < tol:
            return sig
        if v < 1e-8:
            break
        sig -= diff / v
        if sig <= 0 or sig > 10:
            break
    try:
        return brentq(f, 1e-4, 10.0, xtol=tol, maxiter=max_iter)
    except (ValueError, RuntimeError):
        return np.nan


# ---------------------------------------------------------------------------
# Volatility estimators  (input: pandas Series/DataFrame indexed by date)
# ---------------------------------------------------------------------------

def realized_vol_cc(close, window):
    """Close-to-close annualized realized volatility (rolling)."""
    logret = np.log(close / close.shift(1))
    return logret.rolling(window).std(ddof=1) * SQRT_TD


def realized_vol_yang_zhang(df, window, o="open", h="high", l="low", c="close"):
    """Yang-Zhang annualized realized volatility (rolling). df has OHLC columns."""
    ln_ho = np.log(df[h] / df[o])
    ln_lo = np.log(df[l] / df[o])
    ln_co = np.log(df[c] / df[o])
    rs = ln_ho * (ln_ho - ln_co) + ln_lo * (ln_lo - ln_co)         # Rogers-Satchell daily
    overnight = np.log(df[o] / df[c].shift(1))                     # close -> open
    openclose = ln_co                                             # open -> close

    var_o = overnight.rolling(window).var(ddof=1)
    var_c = openclose.rolling(window).var(ddof=1)
    var_rs = rs.rolling(window).mean()
    k = 0.34 / (1.34 + (window + 1.0) / (window - 1.0))
    yz_var = var_o + k * var_c + (1.0 - k) * var_rs
    return np.sqrt(yz_var.clip(lower=0)) * SQRT_TD


def vix_overlay(sigma_cc, vix, window):
    """Scale close-to-close realized vol by the VIX level relative to its rolling mean.

    sigma_cc and vix are pandas Series aligned on date. Result reflects the
    market-wide implied-vol regime: sigma_cc * (VIX_t / mean(VIX)).
    """
    ratio = vix / vix.rolling(window, min_periods=max(2, window // 4)).mean()
    return sigma_cc * ratio


# ---------------------------------------------------------------------------
# Risk-free rate from the treasury curve
# ---------------------------------------------------------------------------

# Treasury tenors (years) matching FRED DGS* series ids.
TREASURY_TENORS = {
    "DGS1MO": 1.0 / 12.0, "DGS3MO": 0.25, "DGS6MO": 0.5,
    "DGS1": 1.0, "DGS2": 2.0, "DGS5": 5.0, "DGS7": 7.0,
    "DGS10": 10.0, "DGS20": 20.0, "DGS30": 30.0,
}


def interp_rate(tenor_years, rate_pct, T):
    """Interpolate the par-yield curve to maturity T and return a continuously
    compounded decimal rate. tenor_years/rate_pct are 1-D arrays (sorted by tenor);
    T may be scalar or array. Yields beyond the curve ends are clamped (numpy.interp).
    """
    tenor_years = np.asarray(tenor_years, float)
    rate_pct = np.asarray(rate_pct, float)
    order = np.argsort(tenor_years)
    y = np.interp(np.asarray(T, float), tenor_years[order], rate_pct[order]) / 100.0
    return np.log1p(y)  # continuous compounding
