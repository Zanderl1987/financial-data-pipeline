# Paper-trading forward test for the TV catalog candidates — design

Date: 2026-11-23. Status: draft for Zander's approval.

## 1. Intent

Zander wants to know whether the TradingView-catalog candidates would make real money
before risking actual capital. Paper trading at a broker, on free paper money, run in
the cloud, watched from a phone or computer.

The three candidates (all `strategies/ports/`):

| Strategy | Side | Backtest (dev split, $10k/trade, unlimited capital) |
|---|---|---|
| `joey_stochrsi_atr` | long | 348,287 trades, +0.29%/trade, sd 7.7% |
| `capitulation_stretch_reversion` | long (shorts OFF, author default) | 32,793 trades, +0.30%/trade, sd 4.7% |
| `rsi_bb_inside_strategy` | long + short | 116,077 trades, +0.15%/trade, sd 11.4% |

Source of those numbers: `%LOCALAPPDATA%\stage3_dashboard\profiles\<slug>.json`.

### What this test can and cannot show (agreed with Zander 2026-11-23)

At $5k/$10k an account holds a handful of positions and makes a few trades a week.
To tell each edge from luck at t=2 needs roughly 2,900 (joey), 1,000 (capitulation)
and 22,000 (rsi_bb_inside) trades: years to never. **A 3-month paper test cannot prove
profitability.** The statistical case is the backtest (plus the pending CRSP
survivorship re-score). The paper test answers: *run for real, does each strategy
behave the way a capital-limited backtest says it should?* — real fills, real costs,
real order plumbing, results inside the expected range.

Context: the TV campaign is not closed (stopping-rule decision open) and the CRSP
survivorship test is blocked on the WRDS login. The paper test runs alongside those;
it does not replace them. `rsi_bb_inside`'s holdout is spent, so this forward test is
the only fresh evidence it can get.

## 2. Goals, non-goals, anti-goals

Goals
- Six Alpaca paper accounts: each of the 3 strategies at $5k and at $10k.
- Fully cloud-run: no step depends on Zander's laptop being on.
- A private dashboard reachable from phone and computer, live during market hours.
- Zero cost.
- Pass/fail decided by a rule written down before launch (section 5).

Non-goals
- Intraday strategies. All three decide on daily closes; they stay daily.
- A pooled multi-strategy portfolio. Zander chose one account per strategy first;
  a pooled test may come later as a separate phase.
- Real-money trading, or any live-account keys anywhere in this system.
- Replacing the backtest or the CRSP survivorship test as the evidence of an edge.

Anti-goals (would make this a failure even if it runs)
- Paper logic that differs from the backtested logic, so a gap can't be attributed.
- A pass bar chosen or adjusted after results are seen.
- A missed trading day that looks like a quiet day.
- Keys or account numbers in a public place (fdp is a PUBLIC repo; Actions logs on a
  public repo are public).

## 3. Constraints

- Free: Alpaca paper-only accounts (email signup), GitHub Actions, Streamlit
  Community Cloud (one free private app).
- Alpaca caps paper accounts at 3 per login (staff forum posts; unconfirmed in
  official docs) -> **two Alpaca logins**: login A = $5k accounts, login B = $10k.
  Zander creates both logins and all API keys; Claude cannot.
- Alpaca paper: fills are simulated; dividends not paid; shorts only on easy-to-borrow
  names; no borrow fee charged. Paper `cls` (market-on-close) orders reportedly fill at
  the bid near the close rather than in the auction and can partially fill/expire
  (forum report). `cls` orders are rejected 3:50pm-7:00pm ET.
- GitHub Actions `schedule` runs can be delayed or occasionally skipped.
- Engine timing (`evaluation/trades.py:222`): signal on day t's close, entry at day
  t+1's close. Exits likewise act on the next close. So the earliest exit is the
  close after entry: no same-day round trips, so the pattern-day-trader rule (accounts
  under $25k) is never triggered. Builder: confirm this holds for exits as well as
  entries before relying on it.
- Backtest price source: `yfinance_universe_prices` (split-adjusted), `clean_symbols()`,
  $5 floor.

## 4. Approaches considered

1. **All on the laptop, phone via Tailscale.** Same data as the backtest, simplest.
   Rejected: laptop off/asleep/rebooting = no orders and no dashboard.
2. **Trading job on the laptop, dashboard on Streamlit Cloud.** Clean data match,
   dashboard always up. Rejected by Zander: still needs the laptop each trading day.
3. **All in the cloud (chosen by Zander).** GitHub Actions runs the daily job;
   Streamlit Cloud hosts the dashboard; Alpaca holds positions. Cost: signals are
   computed from prices fetched in the cloud, not from the laptop's stored table, so a
   data-source gap must be ruled out before launch (section 6.2).

## 5. Pass bar (decided by Zander 2026-11-23, fixed before launch)

Per account, evaluated after **3 months** of trading. "Return" and "equity" below
always mean Alpaca equity minus the accrued 270 bps/yr borrow fee on short positions
(only rsi_bb_inside shorts; Alpaca paper charges no fee):
- **Pass:** cumulative return falls inside the range the matching capital-limited
  backtest (section 6.1) treats as normal for a 3-month window.
- **Pass, flagged:** return ABOVE that range. Counts as a pass, but the account is
  flagged "investigate before trusting": beating the backtest by that much is also a
  sign paper and backtest don't match.
- **Fail:** return below that range, OR the account is down **$1,000** from its start
  (20% of $5k, 10% of $10k). Hitting $1,000 stops that account's trading immediately
  and marks it failed; the other five continue.
- **Costs are tracked, not judged** (Zander 2026-11-23). Realized per-trade cost
  (fill vs. the close the backtest would have used) is shown on the dashboard next to
  the backtest's assumption, but is not part of pass/fail: Alpaca paper fills are
  simulated, so they measure Alpaca's simulator, not the market, and tens of trades
  can't settle it. Real cost evidence needs a small live-money test later.
- **Survivorship rule for the range** (Zander 2026-11-23): the range is built from
  2010-2026 on the survivor-only `yfinance_universe_prices` panel (biased upward; a
  below-range result is partly expected for that reason). If the CRSP survivorship
  data (live + dead companies) becomes available before the 3 months end, the range
  is recomputed from CRSP by exactly the same method and the CRSP range is the one
  used. This rule is committed before launch, so the choice of range cannot depend on
  results.
- The $1,000 limit is per account (confirmed by Zander 2026-11-23). It is set
  conservatively on purpose and may be changed later, but only as a dated, logged
  change that applies from that date forward; past results are judged by the limit in
  force when they happened.

Commit this section (and the range numbers from 6.1) before the first order, so the bar
is on record before any result exists.

## 6. Design

### 6.1 Capital-limited backtest (one-time, before launch; runs on the laptop)

The campaign backtest put $10k into every signal with no capital limit (joey averaged
160 open positions, max 1,256). A $5k account cannot do that, so the paper accounts
need a **selection rule**, and that rule must be backtested too, or the paper result
has nothing to be compared with.

- Sizing: fixed **$1,000 slots** -> 5 slots at $5k, 10 slots at $10k. Whole shares.
  (My call: keeps the $5k and $10k tests genuinely different — 5 vs 10 positions —
  rather than identical portfolios at two scales.)
- When signals exceed free slots: take the highest prior-day dollar volume first
  (my call: deterministic, and the most liquid names have the lowest slippage). The
  existing portfolio engine fills free slots alphabetically on ties, so this needs an
  engine change or a separate capital-limited simulator.
- **Exits anchor to the position actually held** (decided 2026-11-23). joey and
  capitulation currently compute stops/targets/time stops from the port's internal
  take-every-signal replay (`base.simulate_positions*`; joey's ATR is ffilled from
  the latest signal bar). With 5-10 slots many signals are skipped, so replay-anchored
  exits could fire on trades the account never made, and in production they would
  depend on how much history the job fetched. Instead: the stop/target/time limit is
  computed from the held position's own signal bar and entry. The capital-limited
  backtest and the live job use this same logic. (Under unlimited capital, one
  position per symbol, this should coincide with the campaign's results; builder
  verifies that on a sample before relying on it.) rsi_bb_inside is unaffected (its
  only exit is the Bollinger-midline cross).
- **Universe = dev symbols only** (the 75% where `sha256(symbol) % 4 != 0`), for the
  backtest, the data check and the live accounts. The other 25% are joey's and
  capitulation's unused one-shot holdout; keeping them out keeps that holdout clean and
  makes the backtest's candidate pool identical to the live one. Window: 2010 through
  2026-09-11 (the table's last date); see the survivorship rule in section 5.
- Same costs as the campaign primary (sqrt-law), plus 270 bps/yr borrow fee on
  rsi_bb_inside shorts (the fee Zander approved 2026-11-08).
- Output per account: distribution of 3-month returns over all rolling 3-month windows
  in the dev period -> the "normal range" (e.g. 5th-95th percentile; builder may
  propose a different band, but it is fixed before launch), plus the expected cost
  per trade. Stored as a small JSON committed to the repo.
- This is a re-run on the dev split only. No holdout is touched.

### 6.2 Data-source check (launch gate)

The cloud job must fetch its own daily bars. First choice: yfinance from the GitHub
runner (same source as the backtest). Fallback: Alpaca's market-data API daily bars
(free-plan historical coverage and adjustment options are NOT yet verified).

Gate before launch: for the ~6 months ending 2026-09-11 (`yfinance_universe_prices`
was a one-off download and ends there), on dev symbols only, compute each strategy's
signals from the cloud source and from `yfinance_universe_prices` on the laptop, and
compare. Launch
only if they agree on essentially every signal (builder proposes the threshold; any
disagreements are listed and explained). If yfinance is blocked or rate-limited from
GitHub runners, use Alpaca bars only if they pass the same gate.

### 6.3 Daily trading job (GitHub Actions)

- Runs every trading day. Scheduled several times (e.g. ~10:30, 12:30, 14:30 ET)
  because scheduled runs can slip; each run is idempotent — it does nothing if that
  day's orders for an account are already placed.
- Steps per account: fetch bars through yesterday's close -> run the strategy port ->
  decide exits for held positions and entries into free slots -> submit `cls` orders
  to that account -> record what it did.
- Every order carries a `client_order_id` encoding date, strategy, symbol and action,
  so Alpaca's own order history is a complete audit trail.
- Market holidays/half-days from Alpaca's calendar API.
- A day with no successful run by the 3:50pm cutoff is recorded as MISSED and shown
  on the dashboard; it is never silent.
- Loss limit: if an account's equity is <= start - $1,000, close everything and stop
  trading that account.
- Strategy code: reuse the entry signals from `strategies/ports/*` unchanged; exits
  use the held-position logic from 6.1, shared with the capital-limited backtest, so
  paper logic IS that backtest's logic. The ports import `analytics.technical` and
  `strategies.ports.base`; the job installs only what those need.
- Day-trader rule: confirmed by review that the earliest exit is two sessions after
  entry for exits as well as entries (`trades.py` `_next_candidate`), so it never
  triggers.

### 6.4 State

Alpaca is the source of truth: positions, orders, fills, equity history. Anything a
strategy needs that Alpaca doesn't hold (e.g. a position's entry signal date for
time-based exits) is derived from the tagged order history. Nothing is committed back
to the public repo except code and the static expected-range JSON. Builder decides
whether a private store is needed for run logs/MISSED days (e.g. a private gist or a
small private repo); it must not be public.

### 6.5 Dashboard (Streamlit Community Cloud, one private app)

- Private, invite-only by Zander's email. Reads Alpaca directly with read-capable keys
  for all six accounts (Streamlit secrets), plus the expected-range JSON.
- Six account cards: equity curve against the backtest's expected range band;
  distance to the $1,000 limit; open positions with live P&L (IEX real-time, free);
  today's orders and fills; cost per trade vs. the backtest assumption.
- One summary table: all six side by side, each marked on track / behind / stopped,
  plus MISSED days.
- Short P&L shown net of the 270 bps/yr borrow fee Alpaca paper doesn't charge.
- Must work on a phone screen.

### 6.6 Where it lives

Code in financial-data-pipeline (it reuses `strategies/ports/` and `analytics/`), e.g.
`paper/` plus `.github/workflows/paper_trade.yml` and the Streamlit app file. fdp is
PUBLIC, so: keys only in GitHub/Streamlit secrets; workflow logs print counts and
statuses, never account numbers or key fragments.

## 7. Setup Zander has to do

1. Create Alpaca login A and login B; in each, three paper accounts; set balances to
   $5k (A) and $10k (B). Generate paper API keys per account.
2. Add the keys as GitHub Actions secrets and Streamlit secrets (builder supplies the
   exact names).
3. Create the Streamlit Community Cloud app (builder supplies steps) and accept the
   invite.

## 8. Left to the builder

Module layout, exact schedule times, the normal-range band and signal-agreement
threshold (both fixed and committed before launch), test design, the private log
store, dashboard visual layout, and dependency trimming for the Actions runner.
