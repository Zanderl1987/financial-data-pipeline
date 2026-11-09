"""Tests for strategies/survivorship.py -- re-scoring the TV catalog on a complete CRSP population."""

import os
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation import execution as ev_execution  # noqa: E402
from evaluation import registry as ev_registry  # noqa: E402
from evaluation import stats as ev_stats  # noqa: E402
from evaluation import trades as ev_trades  # noqa: E402
from evaluation.contracts import TradeRule  # noqa: E402
from strategies import survivorship as sv  # noqa: E402
from strategies.stage3 import DEV_END, PRICE_FLOOR, _is_holdout_symbol, cost_config, with_price_floor  # noqa: E402

NOTIONAL = 10_000.0


def _frame(prices, start="2010-01-04", volume=1_000_000.0) -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=len(prices), name="date")
    p = np.asarray(prices, dtype=float)
    return pd.DataFrame({"open": p, "high": p, "low": p, "close": p,
                         "volume": np.full(len(p), volume)}, index=idx)


def _cframe(prices, start="2010-01-04", real=None, adv=1e7) -> pd.DataFrame:
    """A frame as crsp_frame() returns it: real traded price and trailing dollar volume attached."""
    df = _frame(prices, start)
    df["real_close"] = df["close"] if real is None else np.broadcast_to(real, len(df)).astype(float)
    df["adv"] = np.broadcast_to(adv, len(df)).astype(float)
    return df


def _wiggle(n, seed=1, level=20.0, sigma=0.02):
    rng = np.random.default_rng(seed)
    return level * np.exp(np.cumsum(rng.normal(0.0, sigma, n)))


def _positional_rule(entry_at=(), exit_at=(), side="long") -> TradeRule:
    def flags(positions):
        def f(d):
            out = pd.Series(False, index=d.index)
            for p in positions:
                out.iloc[p] = True
            return out
        return f
    return TradeRule(name="positional", entries=flags(entry_at), exits=flags(exit_at), side=side)


def _config(fee=0.0):
    return sv.crsp_config(borrow_fee_bps=fee)


def _cgroup(rule, frames, terminals=None, n_perm=20, seed=0, config=None, floor=PRICE_FLOOR, executor=None):
    frames = frames if isinstance(frames, dict) else {"X": frames}
    terminals = {s: 0.0 for s in frames} if terminals is None else terminals
    return sv.evaluate_group(rule, frames, terminals, first_idx=0, n_perm=n_perm, seed=seed,
                             config=_config() if config is None else config, entry_floor=floor,
                             executor=executor, corrected=True)


# ------------------------------------------------------------ the engine's new optional input

def test_engine_charges_impact_arrays_it_is_given():
    n = 40
    idx = pd.bdate_range("2010-01-04", periods=n)
    close = _wiggle(n)
    le = np.isin(np.arange(n), [10])
    lx = np.isin(np.arange(n), [20])
    none = np.zeros(n, bool)
    cfg = cost_config(10.0)
    rate = np.full(n, 0.01)
    rows = ev_trades.simulate_symbol(idx, close, le, lx, none, none, "X", NOTIONAL, config=cfg, impact=(rate, rate))
    gross = (rows[0]["exit_price"] / rows[0]["entry_price"] - 1) * 100
    charged = gross - rows[0]["pnl_pct"]
    assert charged == pytest.approx(2.0 + 100 * ev_execution.round_trip_rate(cfg.costs), abs=0.01)


def test_engine_default_is_unchanged_when_no_impact_is_given():
    """The Stage 3 numbers must not move because of this new input."""
    n = 60
    idx = pd.bdate_range("2010-01-04", periods=n)
    close, vol = _wiggle(n), np.full(n, 5e3)
    le, lx = np.isin(np.arange(n), [40]), np.isin(np.arange(n), [50])
    none = np.zeros(n, bool)
    cfg = cost_config(10.0)
    plain = ev_trades.simulate_symbol(idx, close, le, lx, none, none, "X", NOTIONAL, config=cfg, volume=vol)
    s = pd.Series(close, index=idx)
    arrays = ev_trades._compute_adv_impact_arrays(s, pd.Series(vol, index=idx), NOTIONAL, 0.6, n)
    given = ev_trades.simulate_symbol(idx, close, le, lx, none, none, "X", NOTIONAL, config=cfg, impact=arrays)
    assert plain == given


# ------------------------------------------------------------ closing positions at the end of a company

def test_open_long_is_dropped_by_the_plain_engine_but_closed_at_the_delisting_value():
    """The bug this module exists for: the engine silently forgets a position
    that is open when a stock's data ends, so a dead stock books no final loss."""
    df = _frame([10.0] * 6)
    rule = _positional_rule(entry_at=[1])
    assert ev_trades.simulate(rule, {"X": df}).empty

    rows = sv.simulate_with_death(rule, df, -0.5, "X", NOTIONAL)
    assert len(rows) == 1
    assert rows[0]["exit_price"] == pytest.approx(5.0)
    assert rows[0]["pnl_pct"] == pytest.approx(-50.0)
    assert rows[0]["pnl_dollars"] == pytest.approx(-5_000.0)


def test_position_open_when_the_data_ends_closes_at_the_last_price():
    """Change 5: dropping these hides losers still waiting for their exit."""
    df = _frame([10.0, 10.0, 8.0, 8.0, 6.0, 6.0])
    rows = sv.simulate_with_death(_positional_rule(entry_at=[1]), df, 0.0, "X", NOTIONAL)
    assert len(rows) == 1
    assert rows[0]["exit_price"] == pytest.approx(6.0)
    assert rows[0]["pnl_pct"] == pytest.approx(-25.0, abs=0.01)   # entered at 8 on the next close


def test_total_wipeout_still_books_the_trade():
    """The engine refuses a non-positive exit price, which would quietly delete
    the worst possible outcome. A -100% delisting must still reach the P&L."""
    rows = sv.simulate_with_death(_positional_rule(entry_at=[1]), _frame([10.0] * 6), -1.0, "X", NOTIONAL)
    assert len(rows) == 1
    assert rows[0]["pnl_pct"] == pytest.approx(-100.0, abs=0.01)


def test_short_gets_no_credit_for_a_delisting_fall():
    """Change 9: borrow recalls and frozen positions make that gain unreliable."""
    rows = sv.simulate_with_death(_positional_rule(entry_at=[1], side="short"), _frame([10.0] * 6),
                                  -1.0, "X", NOTIONAL)
    assert len(rows) == 1
    assert rows[0]["exit_price"] == pytest.approx(10.0)
    assert rows[0]["pnl_pct"] == pytest.approx(0.0, abs=0.01)
    assert rows[0]["pnl_dollars"] == pytest.approx(0.0, abs=0.01)


def test_short_still_pays_a_delisting_rise():
    """A takeover premium is a real loss to a short. Only the fall gets no credit."""
    rows = sv.simulate_with_death(_positional_rule(entry_at=[1], side="short"), _frame([10.0] * 6),
                                  0.25, "X", NOTIONAL)
    assert rows[0]["exit_price"] == pytest.approx(12.5)
    assert rows[0]["pnl_pct"] == pytest.approx(-25.0)


def test_position_entered_on_the_last_real_bar_is_still_closed():
    df = _frame([10.0] * 6)
    rows = sv.simulate_with_death(_positional_rule(entry_at=[4]), df, -0.5, "X", NOTIONAL)
    assert len(rows) == 1
    assert rows[0]["entry_date"] == df.index[5]
    assert rows[0]["pnl_pct"] == pytest.approx(-50.0)


def test_signal_on_the_last_real_bar_does_not_enter_at_the_delisting_value():
    """There is no next real close to fill it at."""
    assert sv.simulate_with_death(_positional_rule(entry_at=[5]), _frame([10.0] * 6), -0.5, "X", NOTIONAL) == []


def test_trade_that_exits_before_death_is_untouched():
    df = _frame([10.0, 10.0, 10.0, 12.0, 12.0, 12.0])
    rows = sv.simulate_with_death(_positional_rule(entry_at=[0], exit_at=[2]), df, -0.9, "X", NOTIONAL)
    assert len(rows) == 1
    assert rows[0]["exit_price"] == pytest.approx(12.0)
    assert rows[0]["pnl_pct"] == pytest.approx(20.0)


def test_rule_never_sees_the_delisting_value():
    """A rule that could read the delisting price would be trading on the future."""
    seen = []

    def entries(d):
        seen.append(d["close"].to_numpy().copy())
        return pd.Series(False, index=d.index)

    rule = TradeRule(name="spy", entries=entries, exits=lambda d: pd.Series(False, index=d.index), side="long")
    df = _frame([10.0] * 6)
    sv.simulate_with_death(rule, df, -0.5, "X", NOTIONAL)
    assert len(seen) == 1 and len(seen[0]) == 6 and (seen[0] == 10.0).all()


# ------------------------------------------------------------ the null faces the same end

def test_random_timing_also_pays_the_delisting_loss():
    """If only the real strategy paid it, the check would be rigged against the
    strategy. If the shifted copies dodged it, a real timing edge would be
    overstated or understated depending on side. Both sides must face it."""
    df = _cframe([10.0] * 60)
    rule = _positional_rule(entry_at=[30])
    zero = _cgroup(rule, df, {"X": 0.0}, n_perm=40)
    half = _cgroup(rule, df, {"X": -0.5}, n_perm=40)
    # Column 0 is P&L, 1 is trade count. Shifted entries before the first
    # measurable bar are blocked, so compare per trade among those that happen.
    assert (half.null[:, 1] >= 1).sum() >= 15
    traded = half.null[:, 1] >= 1
    assert zero.null[traded, 0].mean() == pytest.approx(0.0, abs=5.0)
    assert half.null[traded, 0].mean() == pytest.approx(-0.5 * rule.notional, rel=0.1)


def test_forced_closes_are_counted_separately():
    g = _cgroup(_positional_rule(entry_at=[30]), _cframe([10.0] * 40), {"X": -0.5}, n_perm=5)
    assert g.forced_n == 1
    assert g.forced_pnl == pytest.approx(g.obs_pnl)


def test_forced_closes_on_shorts_are_reported_separately():
    g = _cgroup(_positional_rule(entry_at=[30], side="short"), _cframe([10.0] * 40), {"X": -0.5}, n_perm=5)
    assert g.short_forced_n == 1
    assert g.short_forced_pnl == pytest.approx(g.obs_pnl)


def _walks(n_symbols=8, n_bars=400, seed=7):
    rng = np.random.default_rng(seed)
    out = {}
    for i in range(n_symbols):
        path = 20.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.02, n_bars)))
        out[f"S{i}"] = _frame(path, volume=float(rng.integers(100_000, 5_000_000)))
    return out


def _dip(d):
    return d["close"] < d["close"].rolling(20).mean() * 0.97


def _dip_exit(d):
    return d["close"] > d["close"].rolling(20).mean()


def _dip_rule() -> TradeRule:
    return TradeRule(name="dip", entries=_dip, exits=_dip_exit, side="long")


def _legacy_group(rule, frames, n_perm, seed, config=None, first_idx=0):
    return sv.evaluate_group(rule, frames, {}, first_idx=first_idx, n_perm=n_perm, seed=seed,
                             config=config, entry_floor=PRICE_FLOOR)


def test_stage3_inputs_and_rules_reproduce_the_campaign_scoring_exactly():
    """Change 14. This checks the code only; it is no reason to keep the old rules."""
    rule, cache, cfg = with_price_floor(_dip_rule()), _walks(), cost_config(10.0)
    v2 = ev_stats.circular_shift_trades(rule, cache, n_perm=30, seed=3, config=cfg, entry_floor=PRICE_FLOOR)
    mine = sv.combine([_legacy_group(rule, cache, 30, 3, cfg)])
    assert v2["pnl_p"] is not None
    for key in ("obs_pnl_dollars", "obs_win_rate_pct", "pnl_p", "win_rate_p", "n_perm",
                "null_exposure_ratio", "exposure_mismatch"):
        assert mine[key] == v2[key], key


def test_batches_with_running_index_equal_one_big_run():
    rule, cache = with_price_floor(_dip_rule()), _walks()
    names = list(cache)
    a = {n: cache[n] for n in names[:3]}
    b = {n: cache[n] for n in names[3:]}
    whole = sv.combine([_legacy_group(rule, cache, 30, 3)])
    assert sv.combine([_legacy_group(rule, a, 30, 3), _legacy_group(rule, b, 30, 3, first_idx=3)]) == whole


def test_workers_do_not_change_the_answer():
    frames = {f"S{i}": _cframe(_wiggle(200, seed=i)) for i in range(4)}
    terminals = {s: 0.0 for s in frames}
    terminals["S0"] = -0.5
    alone = _cgroup(_dip_rule(), frames, terminals, n_perm=15)
    with ProcessPoolExecutor(max_workers=2) as ex:
        pooled = _cgroup(_dip_rule(), frames, terminals, n_perm=15, executor=ex)
    assert sv.combine([alone]) == sv.combine([pooled])


# ------------------------------------------------------------ the $5 floor uses the real price (change 8)

def test_floor_blocks_a_real_two_dollar_stock_lifted_by_a_later_reverse_split():
    df = _cframe([20.0] * 40, real=2.0)
    g = _cgroup(_positional_rule(entry_at=[30]), df, n_perm=5)
    assert g.obs_n == 0
    assert g.floor_changed == 1


def test_floor_allows_a_real_hundred_dollar_stock_lowered_by_a_later_split():
    df = _cframe([1.0] * 40, real=100.0)
    g = _cgroup(_positional_rule(entry_at=[30]), df, n_perm=5)
    assert g.obs_n == 1
    assert g.floor_changed == 1


def test_floor_also_gates_every_shifted_copy_on_the_real_price():
    df = _cframe([20.0] * 40, real=2.0)
    g = _cgroup(_positional_rule(entry_at=[30], exit_at=[35]), df, n_perm=20)
    assert (g.null[:, 1] == 0).all()


@pytest.mark.parametrize("side", ["long", "short"])
def test_shifted_copies_cannot_enter_where_the_strategy_could_not(side):
    """The strategy is allowed in on one narrow stretch. A copy shifted off it must not fill,
    or the null would hold trades that could not have happened."""
    real = np.full(100, 2.0)
    real[60:66] = 20.0
    df = _cframe(_wiggle(100), real=real)
    g = _cgroup(_positional_rule(entry_at=[62], exit_at=[64], side=side), df, n_perm=60)
    assert g.obs_n == 1
    assert (g.null[:, 1] == 0).sum() >= 45      # only a few of the ~99 offsets land on the stretch


# ------------------------------------------------------------ costs and trades that could not happen (change 7)

@pytest.mark.parametrize("adv", [0.0, np.nan, NOTIONAL - 1.0])
def test_no_entry_where_liquidity_is_zero_missing_or_smaller_than_the_position(adv):
    df = _cframe(_wiggle(40), adv=adv)
    g = _cgroup(_positional_rule(entry_at=[30]), df, n_perm=20)
    assert g.obs_n == 0
    assert g.blocked_liquidity == 1
    assert (g.null[:, 1] == 0).all()


def test_entry_is_judged_on_the_bar_it_fills_on():
    adv = np.full(40, 1e7)
    adv[31] = np.nan                  # the signal is on bar 30, the fill on bar 31
    g = _cgroup(_positional_rule(entry_at=[30]), _cframe(_wiggle(40), adv=adv), n_perm=5)
    assert g.obs_n == 0


def test_no_entry_before_there_is_enough_history_to_price_it():
    g = _cgroup(_positional_rule(entry_at=[3]), _cframe(_wiggle(40)), n_perm=5)
    assert g.obs_n == 0
    assert g.blocked_liquidity == 1


def test_impact_is_the_square_root_law_on_the_trailing_measures():
    import event_backtest as eb
    df = _cframe(_wiggle(60), adv=1e6)
    rate = sv.impact_rates(df, NOTIONAL)
    rets = df["close"].pct_change()
    vol = rets.iloc[35:55].std(ddof=1) * 1e4       # 20 returns ending before bar 55
    assert rate[55] == pytest.approx(eb.ADV_SQRT_LAW_K * vol * np.sqrt(NOTIONAL / 1e6) / 1e4)


def test_exit_where_liquidity_is_unmeasured_pays_the_last_measured_impact():
    adv = np.full(60, 1e6)
    adv[50:] = np.nan
    rate = sv.impact_rates(_cframe(_wiggle(60), adv=adv), NOTIONAL)
    assert np.isfinite(rate[49]) and rate[49] > 0
    assert (rate[50:] == rate[49]).all()


def test_exit_where_volume_dried_up_to_zero_also_pays_the_last_measured_impact():
    adv = np.full(60, 1e6)
    adv[50:] = 0.0
    rate = sv.impact_rates(_cframe(_wiggle(60), adv=adv), NOTIONAL)
    assert (rate[50:] == rate[49]).all() and rate[49] > 0


def _costs_charged(rows):
    gross = 100 * (rows["exit_price"] / rows["entry_price"] - 1)
    return gross - rows["pnl_pct"]


def test_forced_close_of_a_position_entered_on_the_last_real_bar_pays_impact():
    """On the earlier code this exit was free, because the new bar's volume was missing."""
    df = _cframe(_wiggle(40), adv=1e6)
    cfg = _config()
    item = sv._prepare(_positional_rule(entry_at=[38]), df, "X", -0.3, NOTIONAL, PRICE_FLOOR, 0, corrected=True)
    rows = sv._simulate(item, item.le, item.lx, item.se, item.sx, NOTIONAL, cfg)
    rate = sv.impact_rates(df, NOTIONAL)
    expected = 2 * rate[39] + ev_execution.round_trip_rate(cfg.costs)
    assert _costs_charged(rows[0]) == pytest.approx(100 * expected, abs=0.01)


def test_shorts_pay_the_borrowing_fee_on_the_corrected_path():
    df = _cframe([10.0] * 300)
    rule = _positional_rule(entry_at=[30], exit_at=[282], side="short")
    free = _cgroup(rule, df, config=_config(0.0), n_perm=2)
    paid = _cgroup(rule, df, config=_config(100.0), n_perm=2)
    # 1% a year over 252 days held.
    assert free.obs_pnl - paid.obs_pnl == pytest.approx(0.01 * NOTIONAL, rel=0.01)


def test_the_approved_borrowing_fee_is_the_amendment_value():
    # Change 9: 270 bps a year, Muravyev, Pearson & Pollet, anomaly short-leg mean.
    assert sv.BORROW_FEE_BPS == 270.0
    assert sv.crsp_config().costs.borrow_fee_bps == 270.0


def test_a_run_without_an_approved_borrowing_fee_is_refused(monkeypatch):
    monkeypatch.setattr(sv, "BORROW_FEE_BPS", None)
    with pytest.raises(RuntimeError, match="borrow"):
        sv.crsp_config()


# ------------------------------------------------------------ delisting returns (change 6)

@pytest.mark.parametrize("dlret,code,exch,scenario,expected", [
    (-0.12, 574, 3, "shumway", -0.12),          # a real delisting return always wins
    (0.25, 233, 1, "missing_wipeout", 0.25),
    (None, 574, 1, "shumway", -0.30),           # failure, NYSE
    (None, 574, 2, "shumway", -0.30),           # failure, AMEX
    (None, 574, 3, "shumway", -0.55),           # failure, Nasdaq
    (None, 500, 3, "missing_zero", 0.0),
    (None, 520, 1, "missing_wipeout", -1.0),
    (None, 233, 3, "shumway", 0.0),             # merger with no return: no invented loss
    (None, 233, 3, "missing_wipeout", 0.0),
    (None, 331, 1, "missing_wipeout", 0.0),     # exchange with no return, same
    (None, 470, 1, "shumway", -0.30),           # liquidation: not assumed harmless
    (None, 470, 3, "missing_wipeout", -1.0),
    (None, 591, 3, "shumway", -0.55),           # outside the published failure range
    (float("nan"), 584, 1, "shumway", -0.30),
    (-55.0, 584, 1, "shumway", -0.30),          # CRSP numeric missing code is not a return
])
def test_terminal_return(dlret, code, exch, scenario, expected):
    assert sv.terminal_return(dlret, code, exch, scenario) == pytest.approx(expected)


def test_unknown_scenario_is_refused():
    with pytest.raises(ValueError):
        sv.terminal_return(None, 574, 1, "made_up")


@pytest.mark.parametrize("dlret,code,expected", [
    (None, 574, True), (-55.0, 470, True), (float("nan"), 591, True),
    (-0.2, 574, False), (None, 233, False), (None, 331, False), (None, 100, False),
])
def test_return_is_assumed(dlret, code, expected):
    assert sv.return_is_assumed(dlret, code) is expected


# ------------------------------------------------------------ CRSP frames

def _crsp(**over):
    base = {"date": pd.to_datetime(["2010-01-05", "2010-01-04", "2010-01-06", "2010-01-07"]),
            "prc": [20.0, 10.0, -25.0, 30.0], "openprc": [20.0, np.nan, 25.0, 30.0],
            "askhi": [21.0, 11.0, 26.0, 31.0], "bidlo": [19.0, 9.0, 24.0, 29.0],
            "vol": [100.0, 200.0, 300.0, -99.0], "cfacpr": [2.0, 2.0, 2.0, 1.0],
            "cfacshr": [2.0, 2.0, 2.0, 1.0]}
    base.update(over)
    return pd.DataFrame(base)


def test_crsp_frame_cleans_prices():
    f = sv.crsp_frame(_crsp())
    assert list(f.index) == list(pd.to_datetime(["2010-01-04", "2010-01-05", "2010-01-07"]))
    assert f["close"].tolist() == [5.0, 10.0, 30.0]
    assert f.loc["2010-01-04", "open"] == 5.0                  # missing open falls back to close
    assert f.loc["2010-01-05", "volume"] == 200.0              # share count adjusted with the same split
    assert np.isnan(f.loc["2010-01-07", "volume"])             # -99 is a missing code, not a volume
    assert (f["high"] >= f["close"]).all() and (f["low"] <= f["close"]).all()


def test_crsp_frame_removes_no_trade_days():
    """Change 8: a negative CRSP price is a quote midpoint nobody traded at."""
    f = sv.crsp_frame(_crsp())
    assert pd.Timestamp("2010-01-06") not in f.index


def test_crsp_frame_keeps_the_real_traded_price():
    f = sv.crsp_frame(_crsp())
    assert f["real_close"].tolist() == [10.0, 20.0, 30.0]


def _thin_raw(n=60, vol=1000.0, prc=10.0, start="2010-01-04"):
    dates = pd.bdate_range(start, periods=n)
    return pd.DataFrame({"date": dates, "prc": np.full(n, prc), "openprc": np.full(n, prc),
                         "askhi": np.full(n, prc), "bidlo": np.full(n, prc), "vol": np.full(n, vol),
                         "cfacpr": np.ones(n), "cfacshr": np.ones(n)})


def test_trailing_dollar_volume_excludes_the_day_itself():
    raw = _thin_raw(vol=1000.0)
    raw.loc[40, "vol"] = 1_000_000.0
    f = sv.crsp_frame(raw)
    assert f["adv"].iloc[40] == pytest.approx(10_000.0)
    assert f["adv"].iloc[41] > 10_000.0


def test_trailing_dollar_volume_needs_a_full_window():
    f = sv.crsp_frame(_thin_raw())
    assert f["adv"].iloc[:20].isna().all()
    assert f["adv"].iloc[20] == pytest.approx(10_000.0)


def test_no_trade_days_count_as_zero_in_trailing_dollar_volume():
    """Otherwise removing them would make a stock trading once a week look liquid."""
    raw = _thin_raw(vol=1000.0)
    raw.loc[raw.index % 2 == 1, "prc"] = -10.0
    raw.loc[raw.index % 2 == 1, "vol"] = 0.0
    f = sv.crsp_frame(raw)
    assert f["adv"].iloc[-1] == pytest.approx(5_000.0)


def test_a_missing_volume_on_a_traded_day_leaves_liquidity_unmeasured():
    raw = _thin_raw(vol=1000.0)
    raw.loc[30, "vol"] = -99.0
    f = sv.crsp_frame(raw)
    assert f["adv"].iloc[31:51].isna().all()
    assert np.isfinite(f["adv"].iloc[51])


def test_trailing_dollar_volume_is_real_dollars_across_a_split():
    raw = _thin_raw(prc=10.0, vol=1000.0)
    raw["cfacpr"] = 2.0                      # a later 2-for-1 changes adjusted price and shares,
    raw["cfacshr"] = 2.0                     # not the dollars that traded
    f = sv.crsp_frame(raw)
    assert f["adv"].iloc[-1] == pytest.approx(10_000.0)


def test_crsp_frame_refuses_a_different_schema():
    """CRSP has two table formats. Guessing at renamed columns could produce a
    plausible wrong panel, so the wrong one fails loudly."""
    with pytest.raises(ValueError, match="cfacpr"):
        sv.crsp_frame(_crsp().drop(columns=["cfacpr"]))


def test_zero_volume_company_gets_no_free_trades_end_to_end():
    f = sv.crsp_frame(_thin_raw(vol=0.0))
    g = _cgroup(_positional_rule(entry_at=[40]), f, n_perm=5)
    assert g.obs_n == 0 and g.blocked_liquidity == 1


# ------------------------------------------------------------ split (change 4)

def _permno(holdout: bool, nth: int = 0) -> int:
    hits = (p for p in range(10_000, 12_000) if _is_holdout_symbol(sv.crsp_symbol(p)) == holdout)
    return [next(hits) for _ in range(nth + 1)][-1]


def _symbol(holdout: bool, skip=()) -> str:
    return next(f"T{i}" for i in range(1000) if _is_holdout_symbol(f"T{i}") == holdout and f"T{i}" not in skip)


def _companies(rows):
    cols = ["permno", "ticker", "begdt", "dlstdt", "dlstcd", "dlret", "exchcd"]
    df = pd.DataFrame(rows, columns=cols)
    for c in ("begdt", "dlstdt"):
        df[c] = pd.to_datetime(df[c])
    return df


ALIVE = dict(dlstdt=pd.NaT, dlstcd=100, dlret=None, exchcd=1)
LIVE_RANGE = (pd.Timestamp("2000-01-03"), pd.Timestamp("2026-10-30"))


def _alive(permno, ticker, begdt="2000-01-03"):
    return (permno, ticker, begdt, pd.NaT, 100, None, 1)


def _assign(rows, ranges):
    assigned, unmatched = sv.assign_holdout(_companies(rows), ranges)
    return assigned.set_index("permno"), unmatched


def test_company_matching_a_stage3_symbol_keeps_that_symbols_assignment():
    """The permno hash disagrees for at least one of these, so this catches using it."""
    hsym, dsym = _symbol(True), _symbol(False)
    a, _ = _assign([_alive(_permno(False), hsym), _alive(_permno(True), dsym)],
                   {hsym: LIVE_RANGE, dsym: LIVE_RANGE})
    assert a.loc[_permno(False), "holdout"]
    assert not a.loc[_permno(True), "holdout"]
    assert set(a["basis"]) == {"stage3_match"}


def test_company_outside_stage3_uses_the_permno_hash():
    a, _ = _assign([_alive(_permno(True), "NEW1"), _alive(_permno(False), "NEW2")], {})
    assert a.loc[_permno(True), "holdout"]
    assert not a.loc[_permno(False), "holdout"]
    assert set(a["basis"]) == {"permno_hash"}


def test_dead_company_ending_near_the_symbols_end_is_the_same_company():
    sym = _symbol(True)
    last = pd.Timestamp("2015-06-01")
    a, _ = _assign([(_permno(False), sym, "2000-01-03", "2015-05-01", 574, None, 1)],
                   {sym: (pd.Timestamp("2000-01-03"), last)})
    assert a.loc[_permno(False), "basis"] == "stage3_match"
    assert a.loc[_permno(False), "holdout"]


def test_recycled_ticker_that_is_ruled_out_uses_the_permno_hash():
    """WM was Washington Mutual and is now Waste Management: ended long before the symbol began."""
    sym = _symbol(True)
    a, _ = _assign([(_permno(False), sym, "1990-01-02", "2005-06-01", 574, None, 1)],
                   {sym: (pd.Timestamp("2010-01-04"), pd.Timestamp("2026-10-30"))})
    assert a.loc[_permno(False), "basis"] == "recycled"
    assert not a.loc[_permno(False), "holdout"]


def test_ticker_overlap_that_cannot_be_ruled_out_goes_to_holdout():
    """Dates overlap but the ends disagree, so it may be either company."""
    sym = _symbol(False)
    a, _ = _assign([(_permno(False), sym, "2000-01-03", "2015-06-01", 574, None, 1)],
                   {sym: (pd.Timestamp("2010-01-04"), pd.Timestamp("2026-10-30"))})
    assert a.loc[_permno(False), "basis"] == "ambiguous"
    assert a.loc[_permno(False), "holdout"]


def test_two_companies_claiming_one_symbol_both_go_to_holdout():
    sym = _symbol(False)
    a, _ = _assign([_alive(_permno(False, 0), sym), _alive(_permno(False, 1), sym)], {sym: LIVE_RANGE})
    assert a["holdout"].all()
    assert set(a["basis"]) == {"ambiguous"}


def test_stage3_symbols_with_no_crsp_company_are_reported():
    """A renamed company lands here. Its hash may not match, so the count is a stated limit."""
    sym = _symbol(False)
    _, unmatched = _assign([_alive(_permno(False), sym)], {sym: LIVE_RANGE, "GONE": LIVE_RANGE})
    assert unmatched == ["GONE"]


def test_pull_end_caps_only_holdout_companies():
    assert sv.pull_end(True) == DEV_END
    assert sv.pull_end(False) is None


def test_dev_view_of_development_company_keeps_everything():
    df = _frame([10.0] * 5, start="2019-01-07")
    out, ret, died = sv.dev_view(df, -0.5, False, "2019-02-01")
    assert len(out) == 5 and ret == -0.5 and died


def test_dev_view_of_living_company_closes_at_the_last_price():
    df = _frame([10.0] * 5, start="2019-01-07")
    out, ret, died = sv.dev_view(df, 0.0, False, pd.NaT)
    assert len(out) == 5 and ret == 0.0 and not died


def test_dev_view_of_holdout_company_that_died_early_keeps_the_death():
    df = _frame([10.0] * 5, start="2010-01-04")
    out, ret, died = sv.dev_view(df, -0.5, True, "2010-02-01")
    assert len(out) == 5 and ret == -0.5 and died


def test_dev_view_of_holdout_company_that_died_later_hides_the_death():
    df = _frame([10.0] * 20, start="2017-12-18")
    out, ret, died = sv.dev_view(df, -0.5, True, "2018-06-01")
    assert out.index.max() <= pd.Timestamp(DEV_END)
    assert ret == 0.0 and not died


def test_holdout_view_starts_in_2018_and_keeps_a_later_death():
    df = _frame([10.0] * 20, start="2017-12-18")
    out, ret, died = sv.holdout_view(df, -0.5, "2018-06-01")
    assert out.index.min() >= pd.Timestamp("2018-01-01")
    assert ret == -0.5 and died


def test_holdout_view_of_a_living_company_closes_at_the_last_price():
    out, ret, died = sv.holdout_view(_frame([10.0] * 5, start="2019-01-07"), 0.0, pd.NaT)
    assert len(out) == 5 and ret == 0.0 and not died


def test_tripwire_refuses_reserved_holdout_prices():
    prices = pd.DataFrame({"permno": [7], "date": pd.to_datetime(["2018-01-02"])})
    with pytest.raises(RuntimeError, match="holdout"):
        sv.assert_no_reserved_rows(prices, {7})


def test_tripwire_allows_development_prices():
    prices = pd.DataFrame({"permno": [1, 7], "date": pd.to_datetime(["2020-01-02", "2017-12-29"])})
    sv.assert_no_reserved_rows(prices, {7})


# ------------------------------------------------------------ one population, scored in pieces

def _raw_company(permno, closes, start="2010-01-04", vol=500_000.0):
    p = np.asarray(closes, dtype=float)
    dates = pd.bdate_range(start, periods=len(p))
    return pd.DataFrame({"permno": permno, "date": dates, "prc": p, "openprc": p, "askhi": p, "bidlo": p,
                         "vol": vol, "cfacpr": 1.0, "cfacshr": 1.0})


@pytest.fixture
def crsp_root(tmp_path):
    """Six development companies: alive, delisted with a real return, delisted with a
    missing one, and one with a real 4x crash that the price screen would delete."""
    alive1, alive2, real, missing, merged = [_permno(False, i) for i in range(5)]
    crashed = _permno(False, 5)
    crash_path = _wiggle(300, seed=6)
    crash_path[200:] = crash_path[199] / 4 * crash_path[200:] / crash_path[200]
    rows = [(alive1, "A1", "2010-01-04", pd.NaT, 100, None, 1),
            (alive2, "A2", "2010-01-04", pd.NaT, 100, None, 3),
            (real, "RR", "2010-01-04", "2011-02-15", 574, -0.4, 1),
            (missing, "MM", "2010-01-04", "2011-02-15", 574, None, 3),
            (merged, "CC", "2010-01-04", "2011-02-15", 233, None, 1),
            (crashed, "XX", "2010-01-04", pd.NaT, 100, None, 2)]
    prices = pd.concat([_raw_company(alive1, _wiggle(300, seed=1)), _raw_company(alive2, _wiggle(300, seed=2)),
                        _raw_company(real, _wiggle(290, seed=3)), _raw_company(missing, _wiggle(290, seed=4)),
                        _raw_company(merged, _wiggle(290, seed=5)), _raw_company(crashed, crash_path)])
    root = tmp_path / "crsp"
    root.mkdir()
    _companies(rows).to_parquet(root / "companies.parquet")
    prices.to_parquet(root / "prices.parquet")
    return str(root)


def _score(panel, n_perm=12, **kw):
    return sv.score_panel({"dip": _dip_rule()}, panel, n_perm=n_perm, seed=0, config=_config(),
                          log=lambda *_: None, **kw)["dip"]


def test_score_panel_uses_every_company_with_no_thinning(crsp_root):
    panel = sv.load_dev_panel({}, root=crsp_root)
    assert _score(panel)["shumway"]["n_symbols"] == 6


def test_scenarios_only_move_companies_with_a_missing_return(crsp_root):
    panel = sv.load_dev_panel({}, root=crsp_root)
    out = _score(panel)
    assert out["missing_zero"]["obs_pnl_dollars"] >= out["shumway"]["obs_pnl_dollars"] \
        >= out["missing_wipeout"]["obs_pnl_dollars"]
    assert out["classes"]["assumed"]["n_symbols"] == 1
    assert out["classes"]["real_return"]["n_symbols"] == 2     # the real return and the merger
    assert out["classes"]["alive"]["n_symbols"] == 3


def test_scoring_in_pieces_equals_scoring_everything_at_once(crsp_root):
    """The classes are only an optimisation, so they must not change any number."""
    panel = sv.load_dev_panel({}, root=crsp_root)
    out = _score(panel)["shumway"]
    frames, terminals = {}, {}
    for permno, frame in panel.load_batch(list(panel.companies["permno"])).items():
        row = panel.companies.set_index("permno").loc[permno]
        ret = sv.terminal_return(row["dlret"], row["dlstcd"], row["exchcd"], "shumway")
        frames[sv.crsp_symbol(permno)] = frame
        terminals[sv.crsp_symbol(permno)] = ret
    whole = sv.evaluate_group(_dip_rule(), frames, terminals, first_idx=0, n_perm=12, seed=0, config=_config(),
                              entry_floor=PRICE_FLOOR, corrected=True)
    for key, value in sv.combine([whole]).items():
        assert out[key] == value, key


def test_batch_size_does_not_change_the_answer(crsp_root):
    panel = sv.load_dev_panel({}, root=crsp_root)
    assert _score(panel, batch=2)["shumway"] == _score(panel, batch=100)["shumway"]


def test_price_screen_run_drops_only_the_crashed_company_and_is_reported_separately(crsp_root):
    panel = sv.load_dev_panel({}, root=crsp_root)
    out = _score(panel)
    assert out["shumway"]["n_symbols"] == 6                     # never in the primary run
    assert out["shumway_with_jump_screen"]["n_symbols"] == 5


def test_trades_across_a_big_move_are_listed():
    path = _wiggle(100, seed=1)
    path[60:] = path[59] / 4 * path[60:] / path[60]
    g = _cgroup(_positional_rule(entry_at=[50], exit_at=[70]), _cframe(path), n_perm=2)
    assert len(g.big_moves) == 1
    assert g.big_moves[0]["symbol"] == "X"
    assert sv.combine([g])["big_move_trades"] == g.big_moves


def test_a_delisting_wipeout_counts_as_a_big_move():
    g = _cgroup(_positional_rule(entry_at=[30]), _cframe([10.0] * 40), {"X": -0.9}, n_perm=2)
    assert len(g.big_moves) == 1


def test_panel_refuses_a_file_with_reserved_prices(tmp_path):
    p = _permno(True)
    root = tmp_path / "crsp"
    root.mkdir()
    _companies([_alive(p, "HH", "2017-01-03")]).to_parquet(root / "companies.parquet")
    _raw_company(p, _wiggle(300), start="2017-01-03").to_parquet(root / "prices.parquet")
    panel = sv.load_dev_panel({}, root=str(root), exchanges=(1,))
    with pytest.raises(RuntimeError, match="holdout"):
        panel.load_batch([p])


def test_dev_panel_starts_on_the_first_date_all_exchanges_have_real_opens(tmp_path):
    a, b = _permno(False, 0), _permno(False, 1)
    root = tmp_path / "crsp"
    root.mkdir()
    _companies([(a, "AA", "2010-01-04", pd.NaT, 100, None, 1),
                (b, "BB", "2010-01-04", pd.NaT, 100, None, 3)]).to_parquet(root / "companies.parquet")
    pa, pb = _raw_company(a, _wiggle(60)), _raw_company(b, _wiggle(60, seed=2))
    pb.loc[pb.index[:30], "openprc"] = np.nan             # Nasdaq opens begin later
    pd.concat([pa, pb]).to_parquet(root / "prices.parquet")
    panel = sv.load_dev_panel({}, root=str(root), exchanges=(1, 3))
    assert panel.start == pb["date"].iloc[30]
    assert panel.load_batch([a])[a].index.min() == panel.start


def test_first_open_date_refuses_an_exchange_with_no_opens():
    first = pd.DataFrame({"permno": [1], "first_open": pd.to_datetime(["2000-01-03"])})
    with pytest.raises(ValueError, match="exchange"):
        sv.first_common_open_date(first, pd.DataFrame({"permno": [1], "exchcd": [1]}), exchanges=(1, 3))


def test_holdout_panel_holds_only_holdout_companies_from_2018(tmp_path):
    h, d = _permno(True), _permno(False)
    root = tmp_path / "crsp"
    root.mkdir()
    _companies([_alive(h, "HH", "2017-01-03"), _alive(d, "DD", "2017-01-03")]).to_parquet(root / "companies.parquet")
    pd.concat([_raw_company(h, _wiggle(300), start="2017-06-01"),
               _raw_company(d, _wiggle(300), start="2017-06-01")]).to_parquet(root / "holdout_prices.parquet")
    panel = sv.load_holdout_panel({}, root=str(root))
    assert list(panel.companies["permno"]) == [h]
    assert list(panel.keys()) == [sv.crsp_symbol(h)]
    frame = panel.load_batch([h])[h]
    assert frame.index.min() >= pd.Timestamp("2018-01-01")


def test_score_panel_applies_the_holdout_view(tmp_path):
    h = _permno(True)
    root = tmp_path / "crsp"
    root.mkdir()
    _companies([(h, "HH", "2017-06-01", "2018-09-14", 574, -0.4, 1)]).to_parquet(root / "companies.parquet")
    _raw_company(h, _wiggle(330), start="2017-06-01").to_parquet(root / "holdout_prices.parquet")
    out = _score(sv.load_holdout_panel({}, root=str(root)))
    assert out["classes"]["real_return"]["n_symbols"] == 1


# ------------------------------------------------------------ the pass rule (change 2)

def _result(p, pnl=100.0, mismatch=False, wipe_p=None, wipe_pnl=None):
    primary = {"pnl_p": p, "obs_pnl_dollars": pnl, "exposure_mismatch": mismatch, "n_trades": 10}
    wipe = {"pnl_p": p if wipe_p is None else wipe_p, "obs_pnl_dollars": pnl if wipe_pnl is None else wipe_pnl,
            "exposure_mismatch": mismatch, "n_trades": 10}
    return {"shumway": primary, "missing_wipeout": wipe}


def _family(**strategies):
    return {k: v for k, v in strategies.items()}


def _fillers(n, start=0):
    return {f"f{i}": _result(0.9) for i in range(start, start + n)}


def test_family_correction_covers_everything_rescored():
    """p = 0.01 would pass alone but not across 35: the search must be counted."""
    results = {"a": _result(0.01), **_fillers(34)}
    v = sv.family_verdict(results, required=list(results), stage4_passed={"a"})
    assert not v["a"]["bh_primary"] and not v["a"]["rescore_pass"]


def test_strong_result_passes_across_the_family():
    results = {"a": _result(0.001), **_fillers(34)}
    v = sv.family_verdict(results, required=list(results), stage4_passed={"a"})
    assert v["a"]["bh_primary"] and v["a"]["rescore_pass"]


def test_must_also_have_passed_the_existing_stage4_rule():
    """So the re-score can only remove candidates."""
    results = {"a": _result(0.001), **_fillers(34)}
    v = sv.family_verdict(results, required=list(results), stage4_passed=set())
    assert v["a"]["bh_primary"] and not v["a"]["rescore_pass"]


@pytest.mark.parametrize("kw", [dict(pnl=-1.0), dict(pnl=0.0), dict(mismatch=True), dict(wipe_p=0.9), dict(wipe_pnl=-1.0)])
def test_each_condition_is_required(kw):
    results = {"a": _result(0.001, **kw), **_fillers(34)}
    v = sv.family_verdict(results, required=list(results), stage4_passed={"a"})
    assert not v["a"]["rescore_pass"]


def test_strategy_with_no_p_value_still_counts_in_the_family():
    """bh_fdr leaves None out of m, which would make the correction easier."""
    results = {"a": _result(0.004), "b": {"shumway": {"pnl_p": None, "obs_pnl_dollars": 0.0, "exposure_mismatch": True},
                                          "missing_wipeout": {"pnl_p": None, "obs_pnl_dollars": 0.0,
                                                              "exposure_mismatch": True}}, **_fillers(33)}
    v = sv.family_verdict(results, required=list(results), stage4_passed={"a"})
    assert v["a"]["bh_q"] == pytest.approx(0.004 * 35)
    assert not v["b"]["rescore_pass"]


def test_a_partial_family_is_refused():
    with pytest.raises(ValueError, match="missing"):
        sv.family_verdict({"a": _result(0.001)}, required=["a", "b"], stage4_passed={"a"})


def test_holdout_success_needs_both_scenarios():
    good = _result(0.01)
    assert sv.holdout_success(good) is True
    assert sv.holdout_success(_result(0.05)) is False
    assert sv.holdout_success(_result(0.01, pnl=-1.0)) is False
    assert sv.holdout_success(_result(0.01, wipe_p=0.2)) is False


def test_big_jump_screen_for_the_sensitivity_run():
    assert sv.has_big_jump(_frame([30.0, 30.0, 9.0, 9.0]))       # fell by more than 3x in a day
    assert not sv.has_big_jump(_frame([30.0, 30.0, 11.0, 11.0]))


# ------------------------------------------------------------ registry, guards and data checks

def test_registry_rows_survive_a_round_trip(tmp_path):
    results = {"a": _result(0.001), **_fillers(34)}
    verdict = sv.family_verdict(results, required=list(results), stage4_passed={"a"})
    path = str(tmp_path / "reg.parquet")
    ev_registry.append(sv.registry_rows(results, verdict, run_id="r", universe_hash="u", date_range="x",
                                        created_at="t", execution_hash="e"), path=path)
    reg = ev_registry.load(path)
    assert set(reg["evaluation"]) == {sv.EVALUATION_NAME}
    a = reg[reg["input_name"] == "pine_a"].set_index("statistic")["value"]
    assert a["rescore_pass"] == 1.0
    assert a["pnl_p"] == 0.001
    assert a["wipeout_pnl_p"] == 0.001


def _repo(tmp_path, prereg_text, draft=False):
    exp = tmp_path / "experiments"
    exp.mkdir()
    (exp / sv.PREREG_FILE).write_text(prereg_text, encoding="utf-8")
    if draft:
        (exp / sv.DRAFT_FILE).write_text("draft", encoding="utf-8")
    return str(tmp_path)


def test_real_run_is_refused_while_the_amendment_is_only_a_draft(tmp_path):
    root = _repo(tmp_path, f"### x - {sv.AMENDMENT_MARKER}\n", draft=True)
    with pytest.raises(RuntimeError, match="amendment"):
        sv.assert_amendment_in_force(root)


def test_real_run_is_refused_when_the_amendment_is_not_in_the_preregistration(tmp_path):
    with pytest.raises(RuntimeError, match="amendment"):
        sv.assert_amendment_in_force(_repo(tmp_path, "nothing here\n"))


def test_real_run_is_allowed_once_the_amendment_is_in_force(tmp_path):
    sv.assert_amendment_in_force(_repo(tmp_path, f"### x - {sv.AMENDMENT_MARKER}\n"))


def test_compare_with_yahoo_flags_a_mapping_error():
    a = pd.Series([10.0, 11.0, 12.0], index=pd.bdate_range("2010-01-04", periods=3))
    same = sv.compare_with_yahoo(a, a * 1.001)
    wrong = sv.compare_with_yahoo(a, a * 3.0)
    assert same["n_days"] == 3 and same["median_gap"] == pytest.approx(0.001, abs=1e-6) and not same["large"]
    assert wrong["large"]


def test_compare_with_yahoo_reports_no_overlap():
    a = pd.Series([10.0], index=pd.bdate_range("2010-01-04", periods=1))
    b = pd.Series([10.0], index=pd.bdate_range("2012-01-04", periods=1))
    out = sv.compare_with_yahoo(a, b)
    assert out["n_days"] == 0 and out["large"]


def test_data_check_compares_stage3_matched_companies_only(crsp_root):
    """Change 14: run before scoring, to catch a wrong column or a wrong company."""
    companies = pd.read_parquet(os.path.join(crsp_root, "companies.parquet"))
    a1 = int(companies.loc[companies["ticker"] == "A1", "permno"].iloc[0])
    live = (pd.Timestamp("2010-01-04"), pd.Timestamp("2026-10-30"))
    sym_a1 = "A1"
    panel = sv.load_dev_panel({sym_a1: live}, root=crsp_root)
    close = panel.load_batch([a1])[a1]["close"]
    out = sv.run_data_check(panel, lambda sym, end: close * 3.0, n=10, seed=0)
    assert [r["symbol"] for r in out] == [sym_a1]
    assert out[0]["large"] is True


def test_tested_strategies_reads_stage3_v2_results():
    reg = pd.DataFrame({"evaluation": ["tv_strategy_catalog_stage3_v2", "tv_strategy_catalog_stage3_v2",
                                       "tv_strategy_catalog_stage3"],
                        "input_name": ["pine_a", "pine_a", "pine_old"],
                        "statistic": ["pnl_p", "n_trades", "pnl_p"], "value": [0.1, 5, 0.2]})
    assert sv.tested_strategies(reg) == ["a"]


def test_rescore_gives_no_verdict_on_part_of_the_family(crsp_root):
    panel = sv.load_dev_panel({}, root=crsp_root)
    out = sv.run_rescore({"dip": _dip_rule()}, panel, required=["dip", "other"], stage4_passed={"dip"},
                         n_perm=5, seed=0, config=_config(), log=lambda *_: None)
    assert out["verdict"] is None
    assert "dip" in out["results"]
    assert out["panel"]["n_companies"] == 6


def test_rescore_gives_a_verdict_on_the_whole_family(crsp_root):
    panel = sv.load_dev_panel({}, root=crsp_root)
    out = sv.run_rescore({"dip": _dip_rule()}, panel, required=["dip"], stage4_passed=set(),
                         n_perm=5, seed=0, config=_config(), log=lambda *_: None)
    assert out["verdict"]["dip"]["rescore_pass"] is False


def test_active_code_with_a_date_is_not_read_as_a_death(tmp_path):
    """CRSP can carry its last date on an active company's row (code 100)."""
    p = _permno(False)
    root = tmp_path / "crsp"
    root.mkdir()
    _companies([(p, "AA", "2010-01-04", "2024-12-31", 100, None, 1)]).to_parquet(root / "companies.parquet")
    _raw_company(p, _wiggle(60)).to_parquet(root / "prices.parquet")
    panel = sv.load_dev_panel({}, root=str(root), exchanges=(1,))
    assert panel.report["n_alive"] == 1 and panel.report["n_delisted"] == 0
    assert _score(panel, n_perm=2)["classes"]["alive"]["n_symbols"] == 1


def test_panel_report_counts_alive_and_dead_and_unmatched(crsp_root):
    panel = sv.load_dev_panel({"NOPE": LIVE_RANGE}, root=crsp_root)
    assert panel.report["n_alive"] == 3 and panel.report["n_delisted"] == 3
    assert panel.report["stage3_unmatched"] == ["NOPE"]
