# Paper-Trading Forward Test Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Six Alpaca paper accounts (3 TV-catalog strategies x $5k/$10k) traded daily by a GitHub Actions job, judged against a capital-limited backtest, watched on a private Streamlit dashboard.

**Architecture:** A new `paper/` package in financial-data-pipeline. Entry signals come from the unchanged `strategies/ports/*.compute()`; exits come from new held-position exit functions shared by a capital-limited simulator (laptop, one-time) and the live decision code (cloud, daily). Alpaca is the state store; a private GitHub repo holds the run log; the dashboard reads both.

**Tech Stack:** Python 3.11, pandas/numpy, yfinance 1.5.1 (repo pin), alpaca-py (new pin), Streamlit, GitHub Actions, pytest.

**Spec:** `docs/superpowers/specs/2026-11-23-paper-trading-design.md` (commit `2111e0f`). Read it first.

## Global Constraints

- Local Python: `C:\ProgramData\anaconda3\python.exe`, run from repo root. Actions: Python 3.11.
- Paper only. `TradingClient(..., paper=True)` hard-coded; no code path accepts a live endpoint.
- Strategies: `joey_stochrsi_atr` (long), `capitulation_stretch_reversion` (long; author defaults, shorts OFF), `rsi_bb_inside_strategy` (long+short). Author-default params only.
- Accounts: slots of `SLOT_USD = 1000.0`; 5 slots at $5,000, 10 slots at $10,000. Whole shares: `shares = floor(1000 / signal_day_close)`; skip if 0.
- Entry floor `PRICE_FLOOR = 5.0` on the signal-day close (both sides).
- Selection when signals exceed free slots: highest signal-day `close * volume` first; ties by symbol ascending.
- Timing (engine, `evaluation/trades.py:222`): signal on bar t -> fill at close of t+1. Exit checked on bars j >= entry_bar+1, fill at close of j+1. Exits and entries can fill on the same close; free slots are counted after that close's exits.
- Costs: commission 1.0 bps per side plus sqrt-law impact from `evaluation.trades._compute_adv_impact_arrays(close, volume, 1000.0, event_backtest.ADV_SQRT_LAW_K, n)`. Short borrow fee `BORROW_FEE_BPS_YR = 270.0`, accrued on entry notional x calendar days held / 365.
- Universe: dev symbols only (`not strategies.stage3._is_holdout_symbol(s)`) from `clean_symbols(q.symbols("yfinance_universe_prices"))`.
- Normal-range window: trading from `2010-01-01` through `2026-09-11`; frames loaded with full history for indicator warm-up. Horizon 63 trading days; band = 5th-95th percentile.
- Loss limit `LOSS_LIMIT_USD = 1000.0` per account on fee-adjusted equity (Alpaca equity minus accrued borrow fee).
- Test length: 63 trading days from the first trading day with an order (my reading of "3 months").
- Live price fetch: `yf.download(..., auto_adjust=False, group_by="ticker", threads=True)`, plain open/high/low/close/volume (split-adjusted, matches `_load_ohlcv`), 5 years of history, batches of 200, 1 s pause.
- fdp is PUBLIC: no account numbers, key fragments or equity values in Actions logs; nothing written to this repo at runtime.
- ASCII-only CLI output (cp1252 terminal).
- Do not modify `strategies/ports/*`, `evaluation/*` or `strategies/stage3.py` (campaign reproducibility).

## Review Focus

1. **yfinance returns today's unfinished bar during market hours** -> it must be dropped before signals are computed (same bug as backtester `da01a2f`). Test: Task 5 `test_drops_todays_partial_bar`.
2. **Stale data: the last bar is not the previous trading day** (Yahoo lag, a half-fetched batch) -> that account trades nothing and the run records `stale_data`. Test: Task 7 `test_stale_last_bar_refuses`.
3. **A held position with no tagged entry order** (manual trade, or an order Alpaca re-IDed) -> not exited by rule, counted as a slot, flagged `untracked`; no crash. Test: Task 7 `test_untracked_position_flagged`.
4. **Partial fill / expired MOC order** -> position quantity differs from intended; the exit sells the quantity actually held, the slot counts as occupied. Test: Task 7 `test_exit_uses_held_qty`.
5. **Two runs on the same day** (cron overlap, manual dispatch) -> second run submits nothing. Test: Task 8 `test_second_run_same_day_is_noop`.

---

### Task 1: Strategy specs with held-position exits

**Files:**
- Create: `paper/__init__.py` (empty), `paper/accounts.py`, `paper/strategies.py`
- Test: `tests/test_paper_strategies.py`

**Interfaces:**
- Produces (`paper/accounts.py`): constants `SLOT_USD, LOSS_LIMIT_USD, BORROW_FEE_BPS_YR, PRICE_FLOOR`; `@dataclass(frozen=True) class AccountSpec(key: str, slug: str, start_equity: float, slots: int, login: str)` with properties `env_key -> f"APCA_KEY_{key.upper()}"`, `env_secret -> f"APCA_SECRET_{key.upper()}"`; `ACCOUNTS: tuple[AccountSpec, ...]` with keys `joey_5k, cap_5k, rsibb_5k` (login "A", 5000.0, 5 slots) and `joey_10k, cap_10k, rsibb_10k` (login "B", 10000.0, 10 slots).
- Produces (`paper/strategies.py`): `ExitFn = Callable[[int, int, float, str], bool]` called as `exit_fn(j, sig_i, entry_price, side)`; `@dataclass(frozen=True) class StrategySpec(slug: str, entries: Callable[[pd.DataFrame], dict[str, pd.Series]], held_exit: Callable[[pd.DataFrame], ExitFn])`; `STRATEGIES: dict[str, StrategySpec]`. `entries(df)` returns `{"long": bool Series, "short": bool Series}` (short all-False for long-only), already ANDed with `close >= PRICE_FLOOR`.

Held-exit semantics (per strategy, all indices are positions in `df`):
- joey: exit when `high[j] >= p + r or low[j] <= p - r`, `r = atr_wilder(...)[sig_i] * 1.5` (ATR at THIS position's signal bar, not ffilled from later signals), `p = entry_price`.
- capitulation (long): `low[j] <= close[sig_i] - 3.0 * atr[sig_i]` or `close[j] >= ema(close, 5)[j]` or `j - sig_i >= 10`. Same formulas as `capitulation_stretch_reversion.long_exit_trigger`.
- rsi_bb: `close` crosses the 20-bar SMA basis at j, either direction (the port's `basis_cross`), both sides.
Reuse each port's own helpers (`atr_wilder`, `_ema`, `_sma`, `_rsi`, port `DEFAULT_PARAMS`, `rsi_bb_inside_strategy._bb/_cross`); call `port.compute(df)` for entries.

- [ ] **Step 1: Write failing tests**

```python
def test_six_accounts():
    from paper.accounts import ACCOUNTS
    assert {a.key for a in ACCOUNTS} == {"joey_5k","cap_5k","rsibb_5k","joey_10k","cap_10k","rsibb_10k"}
    assert all((a.start_equity, a.slots) in {(5000.0, 5), (10000.0, 10)} for a in ACCOUNTS)
    assert {a.login for a in ACCOUNTS if a.start_equity == 5000.0} == {"A"}

def test_entries_match_port_and_floor():  # synthetic frame with a sub-$5 stretch
    spec = STRATEGIES["rsi_bb_inside_strategy"]; out = spec.entries(df)
    port = rsi_bb_inside_strategy.compute(df)
    assert (out["long"] == (port["entries"] & (df.close >= 5))).all()
    assert (out["short"] == (port["short_entries"] & (df.close >= 5))).all()
    assert not STRATEGIES["joey_stochrsi_atr"].entries(df)["short"].any()

def test_joey_atr_locked_at_own_signal_bar():
    # frame with signals at bars 50 and 53, ATR differs at the two bars;
    # a position from sig_i=50 must use atr[50] even at j=55
    fn = STRATEGIES["joey_stochrsi_atr"].held_exit(df)
    assert fn(55, 50, p, "long") == expected_with_atr50

def test_capitulation_time_stop():
    fn = STRATEGIES["capitulation_stretch_reversion"].held_exit(df_flat)
    assert fn(sig + 9, sig, p, "long") is False and fn(sig + 10, sig, p, "long") is True

def test_held_exit_equals_port_replay_when_no_overlap():
    # for every replayed position in port.compute(df) on 3 seeded random-walk frames
    # (capitulation, rsi_bb), the first j with held_exit True == the port's exit flag bar
```

- [ ] **Step 2: Run, verify FAIL** — `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_paper_strategies.py -v` -> ImportError on `paper`.
- [ ] **Step 3: Implement `paper/accounts.py` and `paper/strategies.py`** per Interfaces.
- [ ] **Step 4: Run, verify PASS** (same command).
- [ ] **Step 5: Commit** — `git add paper/__init__.py paper/accounts.py paper/strategies.py tests/test_paper_strategies.py; git commit -m "paper: account specs and held-position strategy exits"`

---

### Task 2: Capital-limited simulator

**Files:**
- Create: `paper/sim.py`
- Test: `tests/test_paper_sim.py`

**Interfaces:**
- Consumes: `StrategySpec`, constants from Task 1.
- Produces: `@dataclass class SimResult(trades: pd.DataFrame, equity: pd.Series, open_positions: pd.DataFrame)`. `trades` columns: `symbol, side, sig_date, entry_date, entry_price, shares, exit_sig_date, exit_date, exit_price, gross_pnl, cost_usd, borrow_usd, net_pnl, cost_bps`. `equity`: daily on the union of trading dates in `[start, end]`, starts at `start_equity`, marked to market at close.
- Produces: `simulate_account(frames: dict[str, pd.DataFrame], spec: StrategySpec, *, start_equity: float, slots: int | None, slot_usd: float = SLOT_USD, start: str = "2010-01-01", end: str = "2026-09-11", commission_bps: float = 1.0, borrow_bps_yr: float = BORROW_FEE_BPS_YR) -> SimResult`. `slots=None` = unlimited (used by Task 3).

Day loop (the order a reviewer will check): at the end of bar t, (1) for each held position, if `t >= entry_bar + 1` and `held_exit(t, sig_i, entry_price, side)` -> pending exit at t+1; (2) `free = slots - held + pending_exits`; (3) candidates = symbols with an entry signal on t, not held, not pending; rank by `close*volume` desc, symbol asc; take `free`; (4) at close t+1 fill exits then entries. A symbol whose exit fills at t+1 cannot re-enter from a signal on t (engine's `next_free = exit_sig + 2`). Cost per trade: `shares*entry_price*(c + imp[entry_bar]) + shares*exit_price*(c + imp[exit_bar])`, `c = commission_bps/1e4`.

- [ ] **Step 1: Write failing tests** using a fake `StrategySpec` with explicit entry flags and an exit-after-k-bars `held_exit`:

```python
def test_fill_timing():            # signal bar 10 -> entry_date = bar 11 close; exit true at 13 -> exit at bar 14
def test_slot_cap_and_dollar_volume_rank():  # 3 signals same day, slots=2 -> two highest close*volume taken
def test_freed_slot_reused_same_close():     # exit fills at t+1, new signal on t fills at t+1 with slots=1
def test_no_reentry_on_exit_fill_bar():      # signal on the exit-signal bar is skipped for that symbol
def test_shares_floor_and_skip_zero():       # close 333 -> 3 shares; close 1500 -> no trade
def test_borrow_fee_short():                 # short held 30 calendar days on $990 -> borrow_usd == 990*0.027*30/365
def test_equity_marks_to_market():           # open long's unrealized P&L appears in equity, start == start_equity
def test_unlimited_slots():                  # slots=None takes every non-overlapping signal
```

- [ ] **Step 2: Run, verify FAIL** — `... -m pytest tests/test_paper_sim.py -v`
- [ ] **Step 3: Implement `simulate_account`** in `paper/sim.py`.
- [ ] **Step 4: Run, verify PASS.**
- [ ] **Step 5: Commit** — `git commit -m "paper: capital-limited account simulator"`

---

### Task 3: Equivalence check against the campaign engine

**Files:**
- Create: `paper/check_equivalence.py`

**Interfaces:**
- Consumes: `simulate_account(..., slots=None)`, `strategies.stage3.load_rule_for`, `with_price_floor`, `evaluation.trades.simulate`, `strategies.stage3.dev_cache`.

The design says unlimited-slot held-position exits "should coincide" with the campaign. This proves or disproves it before any range is built.

- [ ] **Step 1: Implement `main(n_symbols: int = 100, seed: int = 0)`**: sample dev symbols, run both engines per strategy over full history, match trades on `(symbol, side, entry_date)`, print per strategy: campaign trades, sim trades, matched with identical `exit_date`, mismatches (first 10 listed).
- [ ] **Step 2: Run** — `C:\ProgramData\anaconda3\python.exe -m paper.check_equivalence`
  Expected: capitulation and rsi_bb 100% matched. joey may differ ONLY on positions where another joey signal fired while the position was open (the port ffills ATR from the latest signal). Print that classification.
- [ ] **Step 3: Gate.** Any other mismatch: STOP, report to Zander, do not continue. joey differences of the expected kind: report the count and the campaign-level impact to Zander (it may mean the campaign's joey number used a shifting ATR), then continue.
- [ ] **Step 4: Commit** — `git commit -m "paper: equivalence check vs campaign engine"`

---

### Task 4: Expected ranges (the pre-launch record)

**Files:**
- Create: `paper/expected.py`, `paper/build_expected.py`, output `paper/expected_ranges.json`, `paper/universe.txt`
- Test: `tests/test_paper_expected.py`

**Interfaces:**
- Produces: `normal_band(equity: pd.Series, start_equity: float, max_h: int = 63) -> pd.DataFrame` (index h=1..63, columns `p5, p50, p95`, fraction of start_equity, from all rolling h-day windows `(equity[t+h]-equity[t])/start_equity`).
- Produces `paper/expected_ranges.json`: `{"method": {...window, horizon, band, universe_sha256, code_commit, n_symbols}, "accounts": {key: {"band": {"h": [...], "p5": [...], "p50": [...], "p95": [...]}, "trades_per_63d_median": float, "cost_bps_mean": float, "short_share": float}}}`. Task 9 reads it.
- Produces `paper/universe.txt`: one dev symbol per line, sorted. Task 7/8 read it.

- [ ] **Step 1: Failing tests**

```python
def test_band_linear_equity():   # equity rising 1/day from 5000 -> p5==p50==p95 == h/5000 at every h
def test_band_shape():           # random equity -> index 1..63, p5 <= p50 <= p95
```

- [ ] **Step 2: Run, verify FAIL**; **Step 3: implement `normal_band`**; **Step 4: PASS.**
- [ ] **Step 5: Implement `build_expected.main(price_table: str = "yfinance_universe_prices", out: str = "paper/expected_ranges.json")`** — the parameter exists so the spec-5 survivorship rule (recompute from CRSP by the same method if CRSP lands before day 63) is one call, not new code. Dev universe, frames via `analytics.technical._load_ohlcv(sym, "yfinance_universe_prices", None, "2026-09-11")`, `simulate_account` for each of the six accounts, write both files. Run in the background with `python -u` and log to `storage/logs/paper_build_expected.log`; it takes hours (laptop memory note: don't run beside the 8 AM / 02:00 jobs).
- [ ] **Step 6: Sanity check before committing**: each account's `trades_per_63d_median` > 0, band p50 sign matches the strategy's full-period net. Show Zander the six bands (p5/p50/p95 at h=63, in dollars).
- [ ] **Step 7: Commit** — `git add paper/expected.py paper/build_expected.py paper/expected_ranges.json paper/universe.txt tests/test_paper_expected.py; git commit -m "paper: capital-limited expected ranges (pre-launch record)"`

---

### Task 5: Cloud price fetch

**Files:**
- Create: `paper/data.py`
- Test: `tests/test_paper_data.py`

**Interfaces:**
- Produces: `fetch_daily_bars(symbols: list[str], start: date, end: date, today: date | None = None, batch: int = 200) -> dict[str, pd.DataFrame]` — frames indexed by date with lowercase `open, high, low, close, volume`; any bar dated `>= today` dropped (today defaults to America/New_York date); all-NaN rows dropped; symbols with no data omitted. Wraps `yf.download(batch_syms, start=start, end=end, auto_adjust=False, group_by="ticker", threads=True, progress=False)`.

- [ ] **Step 1: Failing tests** (monkeypatch `yf.download`):

```python
def test_drops_todays_partial_bar():   # fake download includes today -> last index == yesterday
def test_single_symbol_shape():        # flat columns (1 symbol) normalized like the multi-symbol case
def test_batches_and_omits_empty():    # 450 symbols -> 3 calls; an all-NaN symbol is absent from output
```

- [ ] **Step 2: FAIL**; **Step 3: implement**; **Step 4: PASS**; **Step 5: Commit** — `git commit -m "paper: daily bar fetch for the cloud job"`

---

### Task 6: Data-source launch gate

**Files:**
- Create: `paper/gate_signals.py`, `paper/check_data_gate.py`, `.github/workflows/paper_data_gate.yml`, `paper/requirements-job.txt`

**Interfaces:**
- `gate_signals.main(out: str)`: on the runner, `fetch_daily_bars(universe, start=2021-09-11, end=2026-09-12)`, write `(strategy, symbol, date, side)` for every entry signal dated 2026-03-11..2026-09-11 to `out` (CSV).
- `check_data_gate.main(runner_csv: str)`: same signals from `_load_ohlcv(..., "yfinance_universe_prices", ...)` locally; per strategy print `agreement = |A&B| / |A|B|` and every disagreement. Pass if every strategy >= 0.99 (builder's threshold per spec 6.2, fixed here before the run).
- `paper/requirements-job.txt`: what the job needs to import `paper.*` and `strategies.ports.*` (start from `requirements.txt`; trim only if the dry run in Task 8 shows the install is slow). Add `alpaca-py` pinned to a release at least two weeks old.

- [ ] **Step 1: Verify the ports import without `storage/`**: `git worktree add ../fdp-paper-clean HEAD` then, from that dir, `C:\ProgramData\anaconda3\python.exe -c "import paper.strategies"`. If it fails because of storage/DuckDB on import, fix in `paper/` (lazy import), not in ports. Remove the worktree after.
- [ ] **Step 2: Write the workflow** (`workflow_dispatch` only; uploads the CSV as an artifact — public, which is fine: public prices and public code).
- [ ] **Step 3: Zander pushes the branch; dispatch the workflow** (`gh workflow run paper_data_gate.yml --ref paper-trading`), download the artifact (`gh run download`).
- [ ] **Step 4: Run** `C:\ProgramData\anaconda3\python.exe -m paper.check_data_gate <csv>`. Expected: all three >= 0.99. If yfinance was blocked/rate-limited on the runner or agreement < 0.99: STOP and report to Zander (Alpaca bars fallback is a new decision, per spec 6.2).
- [ ] **Step 5: Record** the result in `paper/launch_record.json` (`{"data_gate": {"date", "agreement": {...}, "run_id"}}`) and commit everything.

---

### Task 7: Daily decision logic (pure)

**Files:**
- Create: `paper/decide.py`
- Test: `tests/test_paper_decide.py`

**Interfaces:**
- Consumes: `AccountSpec`, `StrategySpec`, constants.
- Produces: `@dataclass class Held(symbol: str, side: str, qty: int, avg_entry_price: float, sig_date: date | None, opened: date | None)` (`sig_date None` = untracked).
- Produces: `@dataclass class OrderIntent(symbol: str, side: str, qty: int, action: str, client_order_id: str)` with `action in {"entry_long","entry_short","exit","stop_all"}`.
- Produces: `@dataclass class Decision(orders: list[OrderIntent], notes: list[str], stopped: bool, status: str)` with `status in {"ok","stale_data","stopped"}`.
- Produces: `client_order_id(account_key: str, today: date, action: str, symbol: str, sig_date: date) -> str` = `f"pt|{account_key}|{today:%Y%m%d}|{action}|{symbol}|{sig_date:%Y%m%d}"`, and `parse_client_order_id(s: str) -> dict | None` (None for any id not starting `pt|`).
- Produces: `accrued_borrow_fee(short_lots: list[tuple[float, date, date | None]], today: date, bps_yr: float = BORROW_FEE_BPS_YR) -> float` — lots are `(entry_notional, opened, closed_or_None)`.
- Produces: `decide_orders(account: AccountSpec, spec: StrategySpec, frames: dict[str, pd.DataFrame], held: list[Held], today: date, prev_trading_day: date, fee_adj_equity: float, shortable: Callable[[str], bool]) -> Decision`.

Rules: if `fee_adj_equity <= account.start_equity - LOSS_LIMIT_USD` -> one `stop_all` intent per held position (qty = held qty), `stopped=True`. If any frame used has last bar != `prev_trading_day`, drop that symbol; if more than 5% of the universe is stale -> `status="stale_data"`, no orders. Exits: for each tracked held position, `sig_i` = frame position of `sig_date`, `j` = last bar; evaluate only if `j >= sig_i + 2`. Entries per Task 2's ranking with `free = slots - len(held) + len(exits)`; shorts only if `shortable(sym)`, else note `skipped_unshortable:<sym>`.

- [ ] **Step 1: Failing tests**

```python
def test_loss_limit_stops_all():           # equity 3999.99 on a 5k account -> stopped, stop_all for each held
def test_loss_limit_boundary():            # equity 4000.00 -> stopped (<=); 4000.01 -> not
def test_exit_fires_and_frees_slot():      # 5 held, one exits, 2 signals -> 1 exit + 1 entry (higher close*volume)
def test_no_exit_on_entry_bar():           # position filled yesterday (j == sig_i + 1) -> no exit even if trigger true
def test_exit_uses_held_qty():             # held qty 2 (partial fill of 3) -> exit qty 2
def test_untracked_position_flagged():     # Held(sig_date=None) -> no exit, counts as a slot, note "untracked:<sym>"
def test_stale_last_bar_refuses():         # 10% of frames end before prev_trading_day -> status "stale_data", no orders
def test_unshortable_skipped():            # rsi_bb short signal, shortable False -> no order, note
def test_floor_and_zero_shares():          # close 4.99 -> no entry; close 1200 -> no entry
def test_client_order_id_roundtrip():      # parse(make(...)) returns the same fields; parse("abc") is None
def test_accrued_borrow_fee():             # (1000, d0, None), today=d0+73 -> 1000*0.027*73/365
```

- [ ] **Step 2: FAIL**; **Step 3: implement**; **Step 4: PASS**; **Step 5: Commit** — `git commit -m "paper: daily order decision logic"`

---

### Task 8: Broker adapter, run log, live runner, daily workflow

**Files:**
- Create: `paper/broker.py`, `paper/runlog.py`, `paper/live.py`, `.github/workflows/paper_trade.yml`
- Test: `tests/test_paper_live.py`

**Interfaces:**
- `paper/broker.py`: `class Broker: __init__(self, key: str, secret: str)` (builds `TradingClient(key, secret, paper=True)`); methods `clock() -> (now_et: datetime, is_open_day: bool)`, `prev_trading_day(d: date) -> date` (Alpaca calendar), `fee_inputs() -> tuple[float, list[tuple[float, date, date | None]]]` (equity, short lots from tagged filled orders), `held() -> list[Held]` (positions joined to their latest filled `pt|...|entry_*` order for `sig_date`), `orders_today(prefix: str) -> list[str]`, `shortable(symbol) -> bool` (`asset.shortable and asset.easy_to_borrow`), `submit(intent: OrderIntent) -> str` (market, `TimeInForce.CLS`).
- `paper/runlog.py`: `append_run(record: dict, repo: str, token: str, path: str = "runs.jsonl") -> None` via the GitHub contents API (read, append one JSON line, PUT with sha; retry once on 409). Record: `{"date", "account", "status", "n_orders", "notes", "run_id"}` — no equity, no account number.
- `paper/live.py`: `run(today: date | None = None, dry_run: bool = False, broker_factory=Broker, log=append_run, fetch=fetch_daily_bars) -> int` (exit code). Acts only if Alpaca says it is a trading day and ET time is 09:35-15:40; otherwise logs nothing and returns 0. Fetches bars once for `paper/universe.txt` (start = today - 5 years). Per account: skip if `orders_today(f"pt|{key}|{today:%Y%m%d}")` is non-empty; else decide, submit (unless dry_run), log. One account's exception is logged as `status="error"` and does not stop the others; return 1 if any errored. Stdout: one line per account `"<key>: <status>, <n> orders"` only.
- Workflow: `on: schedule: - cron: "0 14-21 * * 1-5"` plus `workflow_dispatch` with input `dry_run` (default true); `concurrency: paper-trade` (no cancel-in-progress); secrets `APCA_KEY_*/APCA_SECRET_*` (12), `PAPER_LOG_REPO`, `PAPER_LOG_TOKEN`; runs `python -m paper.live` (`--dry-run` when the input says so).

- [ ] **Step 1: Failing tests** with a `FakeBroker` and fake log/fetch:

```python
def test_second_run_same_day_is_noop():    # orders_today non-empty -> submit never called, nothing logged twice
def test_outside_window_does_nothing():    # 15:45 ET -> no fetch, no submit, return 0
def test_one_account_error_others_continue():  # broker 2 raises -> 5 accounts submit, log has status "error", return 1
def test_rejected_order_continues():       # submit raises for one symbol -> other intents still submitted, note recorded
def test_dry_run_submits_nothing():        # dry_run -> submit never called, log status "dry_run"
def test_stdout_has_no_secrets(capsys):    # fake key "PKTESTSECRET123" and equity 5123.45 never appear in output
def test_broker_is_paper_only():           # Broker(...) constructs TradingClient with paper=True (monkeypatched class)
```

- [ ] **Step 2: FAIL**; **Step 3: implement**; **Step 4: PASS** — also run the full suite: `C:\ProgramData\anaconda3\python.exe -m pytest tests/ -q` (expect only the known `test_execution_golden` float-noise failures).
- [ ] **Step 5: Commit** — `git commit -m "paper: broker adapter, run log and daily live job"`

---

### Task 9: Dashboard

**Files:**
- Create: `paper/dashboard_data.py`, `paper/dashboard_app.py`, `paper/requirements.txt` (Streamlit Cloud reads the entrypoint folder's file: streamlit, alpaca-py, pandas, yfinance, requests, plotly)
- Test: `tests/test_paper_dashboard.py`

**Interfaces:**
- Consumes: `expected_ranges.json` (Task 4), run log format (Task 8), `parse_client_order_id`, `accrued_borrow_fee` (Task 7).
- Produces (`dashboard_data.py`, pure): `account_status(day_index: int, ret: float, band: dict, stopped: bool) -> str` returning `"stopped" | "behind" | "on track" | "above"` against the band row at `h = min(day_index, 63)`; `missed_days(trading_days: list[date], log_rows: list[dict], account: str) -> list[date]` (a trading day since launch with no `ok`/`stale_data`/`stopped` row for that account); `trade_costs(fills: pd.DataFrame, closes: pd.DataFrame) -> pd.DataFrame` (bps vs that day's close, sign so positive = worse than close); `verdict(final_ret: float, band_63: tuple[float, float], stopped: bool) -> str` returning `"FAIL" | "PASS" | "PASS (flagged: above range)"`.
- App: six cards (equity line vs p5/p50/p95 band scaled to dollars; distance to the $1,000 limit; open positions with live P&L from Alpaca latest IEX trade; today's orders and fills; cost per trade vs `cost_bps_mean`), one summary table with status, missed days and (after day 63) verdict. Short P&L shown net of accrued borrow fee. `st.set_page_config(layout="centered")`; no horizontal tables wider than a phone (use `st.dataframe(..., use_container_width=True)`). Data refreshed via `st.cache_data(ttl=60)`.

- [ ] **Step 1: Failing tests**

```python
def test_status_bands():         # ret below p5 -> "behind"; inside -> "on track"; above p95 -> "above"; stopped wins
def test_status_after_63_days(): # day_index 80 uses the h=63 row
def test_missed_days():          # 5 trading days, log rows for 3 -> the other 2 returned
def test_verdict():              # below -> FAIL; inside -> PASS; above -> "PASS (flagged: above range)"; stopped -> FAIL
def test_trade_cost_sign():      # buy filled 10.01 vs close 10.00 -> +10 bps; sell filled 9.99 vs 10.00 -> +10 bps
```

- [ ] **Step 2: FAIL**; **Step 3: implement both files**; **Step 4: PASS.**
- [ ] **Step 5: Local smoke**: `C:\ProgramData\anaconda3\python.exe -m streamlit run paper/dashboard_app.py` with `.streamlit/secrets.toml` (gitignored; add the entry) once keys exist (Task 10). Check at phone width in browser dev tools.
- [ ] **Step 6: Commit** — `git commit -m "paper: Streamlit dashboard"`

---

### Task 10: Setup and launch (needs Zander)

**Files:**
- Modify: `paper/launch_record.json`, `docs/AUTOMATION.md` (one section: what runs, where, secrets list, how to stop it)

- [ ] **Step 1: Zander's setup** (hand him this list with exact names):
  1. Alpaca login A: three paper accounts at $5,000; login B: three at $10,000. Keys per account. (Confirm the 3-per-login cap holds.)
  2. A PRIVATE GitHub repo `paper-trading-log` with an empty `runs.jsonl`; a fine-grained token with Contents read/write on that repo only.
  3. GitHub Actions secrets on fdp: `APCA_KEY_<KEY>`/`APCA_SECRET_<KEY>` for the six keys, `PAPER_LOG_REPO`, `PAPER_LOG_TOKEN`.
  4. Streamlit Community Cloud: new app from fdp, branch with the code, main file `paper/dashboard_app.py`, the same secrets, sharing = only Zander's email.
- [ ] **Step 2: Dry run**: `gh workflow run paper_trade.yml -f dry_run=true`; check the run log repo got six `dry_run` rows and the Actions log shows no secrets or equity values.
- [ ] **Step 3: Lock the bar**: write `launch_record.json` `{"launch_date", "expected_ranges_sha256", "data_gate", "pass_bar": "spec section 5 at commit <sha>"}`; commit and push BEFORE the first live run.
- [ ] **Step 4: Go live**: merge to master (the schedule runs from the default branch), confirm the next scheduled run submits orders and the dashboard shows them.
- [ ] **Step 5: Session notes** in `work-notes/financial-data-pipeline/SESSION_NOTES_<date>.md` and the TODO entry (test end date = launch + 63 trading days); commit work-notes.
