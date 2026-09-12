# Options & volatility data sources — vetting pass (2026-09-12)

Verdicts from the "Options data + options-strategy testing capability" TODO's
research phase. Three routes: **already-free-in-store**, **free-buildable**,
**NO-GO free (paid only)**. Recorded so the no-gos are never re-litigated.

## GO — already in the store (free, keyless, wired)

| table | contents | depth | source |
|---|---|---|---|
| `cboe_volatility` | VIX, VIX9D, VIX3M, VIX6M, VVIX, SKEW daily OHLC | VIX/SKEW 1990-01, VIX6M 2008-01, VIX3M 2009-09, VIX9D 2011-01, VVIX 2006-03 | `cboe_pipeline.py` -> `cdn.cboe.com/api/global/us_indices/daily_prices/<name>_History.csv` (always full history, ~1s gap) |
| `cboe_strategy_indices` | BXM/BXMD/PUT/PUTR/CLL/CNDR/BFLY/CMBO/BXD/BXN/WPUT index levels | 1986-06 onward | `cboe_strategy_pipeline.py` |

These make **three option-family strategies testable today with standard
evaluation tooling** (no chain data required):

1. **VIX term-structure slope (VTSL) overlay** — VIX vs VIX3M/VIX6M since
   2008-2009. Inversion (VIX near/above VIX3M) is the classic short-vol
   /equity-regime signal, Tier-2 in STRATEGY_CATALOG.md.
2. **Strategy-index replicas** — BXM (covered-call), PUT (cash-secured
   putwrite), BXMD/PUTR (30-delta variants), CLL (collar), CNDR (iron
   condor), BFLY (iron butterfly) are *implemented, fees-inclusive* monthly-
   expiry S&P 500 option strategies published as index levels. Long
   CNDR/BFLY short-vol, BXM/PUT income + lower-end/data: test the shape
   directly, then copy construction onto our own expirations if wanted.
3. **SKEW / VVIX / VIX9D signals** on forward SPX/ES returns via the
   evaluation/IC harness.

## GO — free but must be built (pipeline candidate)

4. **VIX futures curve 2004+** — Cboe daily settlement CSVs
   `https://www.cboe.com/us/futures/market_statistics/settlement/csv?dt=YYYY-MM-DD`
   work keyless (`User-Agent` normal browser header). One ~1 KB file per day of
   VX and VXM contract rows (Product, Symbol, Expiration Date, Price). Per-day
   backfill gives the *full VIX futures term structure* -> VIX-futures roll
   yield / term-trading (genuine short-vol on the futures curve, not just the
   index proxy). Verified live 2026-09-12 (HTTP 200, VX/K6..X6 + VXM rows).
   Volume/open-interest detail separately archived 2004-2013
   (`/us/futures/market_statistics/historical_data`), price+volume detail
   2013+.
5. Keep accruing `options_history` (2023-12 -> now), `options_chain` (current
   snapshot), `synthetic_options` (2026-06 -> now) — the put/call-ratio and
   near-term brute-force chain work gets a longer sample each day. `analytics/
   options.py::put_call_ratio` is wired (volume-based) and returns empty
   gracefully.

## NO-GO — free historical US option CHAINS / IV surfaces (paid only)

- **OptionMetrics IvyDB US** (1996+) — institutional license / WRDS, no free tier.
- **ORATS** — historical surfaces via Nasdaq Data Link (OPT/OPT2, 2010+);
  paid feed. (Nasdaq Data Link itself is Incapsula-403'd for us anyway.)
- **Databento** — US equity options all-exchanges 2013+; sandbox credits are
  free but durable history is per-GB paid. Also VX futures/options 2018+.
- Cboe's own delayed-quotes API + `options_chain` are snapshot-only (history
  only exists from the day you start running `cboe`/`schwab` chain pulls).
- yfinance option chains: present-day snapshot only; its historical
  endpoints-by-name do not cover chains for arbitrary past dates.

**Consequence (state once, don't re-litigate):** an academically-proper
historical IV-surface backtest (OptionMetrics-style) is not buildable free.
The free strategy-level path uses 1-3 above; the free chain-adjacent path is
5 (accrual) plus the VIX-futures curve (4). If/when single-name or full-chain
IV surface work becomes the priority, budget for a paid source — candidates
ORATS (2010+) or Databento (2013+).

## Next actions for the options TODO (concrete builds)

- [ ] `vix_tsl_pipeline.py` — ingest Cboe VX/VXM settlement CSVs by date into
      `storage/raw/cboe_futures/`; curated `vix_futures_curve`; PIT VTSL +
      front/2nd-month roll yield signal.
- [ ] Event/eval harness pass on the three free in-store families (1-3 above)
      in `evaluation/`.
- [ ] Re-verify `analytics/options.py::put_call_ratio` accuracy on the
      accrued 2023-12+ sample, then promote to a scored signal.