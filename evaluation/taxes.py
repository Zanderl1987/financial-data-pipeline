"""
evaluation/taxes.py -- realized-P&L tax accounting added ON TOP of a
realized-trade frame (evaluation/trades.py). Pure post-hoc reporting: it does
not change simulation outputs, the equity curve, or the registry's accounting
P&L. The same realized trades are re-expensive for tax purposes.

Two layers:

1. wash_adjust() -- US wash-sale basis accounting per symbol. A loss sale whose
   symbol is re-acquired within wash_window_days is deferred: the disallowed
   loss is added to the replacement lot's basis, exactly as the IRS does
   (sale date + 30-day outer window). Moves WHEN a loss is recognized, never
   the total -- sum(adj_pnl_dollars) == sum(pnl_dollars) holds unconditionally
   (the added basis and the deferred loss telescope row to row; a deferred
   loss is always realized on the same-symbol lot that follows within the
   window, so nothing hangs open at the end of the frame).

   WHY only the AFTER side of the IRS rule (sell at a loss, then re-buy
   within 30 days) is modeled -- a deliberate, engine-invariant-driven scope:
   the discrete simulator keeps ONE position per symbol at a time and only
   ever round-trips, so the before-side case (acquire replacement WITHIN 30
   days BEFORE the loss sale) can only be the same lot the sale is closing.
   No partial lots, no averaging-in, no holding through a sale of a subset --
   all IRS wash cases require share-level detail this engine never produces.
   Callers with real share-level trades can pass a `shares` column and the
   share-ratio cap in the rule still applies; the before-side still needs a
   lot-level tracker and is out of scope here.

2. tax_year_summary() -- annual bucketing (short-term <= short_term_days held,
   long-term otherwise; bucket by exit year), within-year netting with ST/LT
   cross-offset, a capital-loss deduction floor (loss_deduction_annual), and
   tax due at stated effective rates.

All behavior is opt-in and additive: no engine code reads this module. The
runner (evaluation/runner.py --tax) and evaluate.py register `trades_tax` /
`trades_wash` rows on request, mirroring meta-label's opt-in shape.

Config philosophy matches evaluation/execution.py: a frozen dataclass with
today-values defaults that preserve accounting P&L when wash_window_days=0.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

REQUIRED_COLS = {"symbol", "side", "entry_date", "exit_date",
                 "entry_price", "exit_price", "pnl_dollars"}


@dataclass(frozen=True)
class TaxConfig:
    """st_rate / lt_rate: EFFECTIVE all-in tax rates on net short-term and
    long-term gains (not marginal brackets -- plug your own blended rate).
    short_term_days: 365 (the default) means "one calendar year" and is
    compared leap-correctly (DateOffset years=1) -- a to-the-day one-year
    hold is short-term in every year pair including leap years, and a
    366-day hold that spans one year+one day is long. Any other value is a
    literal max-held-days short-term cutoff.
    wash_window_days: 0 disables the wash rule entirely (accounting P&L).
    loss_deduction_annual: excess capital losses (after offsetting gains)
    reported as deductible against ORDINARY income up to this annual cap
    (US $3k). This module prices capital-gains tax only, so the deduction is
    REPORTED loss_deducted, not netted off tax_due (its benefit accrues to
    ordinary income this module does not model). Nothing beyond the cap is
    carried forward (carryforwards need an external account across years).
    apply_to_shorts: shorts go through the same symmetric wash logic on
    their covering purchases."""
    st_rate: float = 0.24
    lt_rate: float = 0.15
    short_term_days: int = 365
    wash_window_days: int = 30
    loss_deduction_annual: float = 3000.0
    apply_to_shorts: bool = True

    def __post_init__(self):
        for name in ("st_rate", "lt_rate"):
            v = getattr(self, name)
            if not (0.0 <= v <= 1.0):
                raise ValueError(f"{name} must be a rate in [0, 1]")
        if self.short_term_days < 0:
            raise ValueError("short_term_days must be >= 0")
        if self.wash_window_days < 0:
            raise ValueError("wash_window_days must be >= 0")
        if self.loss_deduction_annual < 0:
            raise ValueError("loss_deduction_annual must be >= 0")


def resolve(config: "TaxConfig | None") -> TaxConfig:
    """None means defaults -- same resolve() convention as execution.py."""
    return TaxConfig() if config is None else config


def _shares_series(df: pd.DataFrame) -> pd.Series:
    """Exact share count per trade when a `shares` column is provided;
    otherwise inferred from the frame's own rounded P&L fields:

        notional ~= pnl_dollars / (pnl_pct / 100)          (rounded to the
        cent / 0.001%p, so this is approximate), and
        shares   = notional / entry_price.

    NaN where it cannot be inferred (zero pnl_pct at any rounding). A NaN
    share count is only ever USED as a wash ratio denominator, where it
    falls back to full-replacement on the rule's behalf (documented in
    wash_adjust)."""
    if "shares" in df.columns:
        return pd.to_numeric(df["shares"], errors="coerce")
    if "pnl_pct" not in df.columns:
        raise ValueError(
            "cannot infer shares: trades lack both a 'shares' column and "
            "'pnl_pct' (needed for notional = pnl_dollars / (pnl_pct/100))")
    pct = pd.to_numeric(df["pnl_pct"], errors="coerce")
    pnl = pd.to_numeric(df["pnl_dollars"], errors="coerce")
    px = pd.to_numeric(df["entry_price"], errors="coerce")
    notional = pnl / pct.where(pct.abs() >= 1e-9, np.nan) * 100.0
    return notional.abs() / px


def _bucket_days(entry: pd.Timestamp, exit_dt: pd.Timestamp,
                 short_term_days: int) -> str:
    """Short-term vs long-term held duration. short_term_days==365 is the
    sentinel "one calendar year" -- compared with DateOffset(years=1), which
    is leap-correct: a to-the-day one-year hold is short-term in every
    year-pair, including 2024-01-01 -> 2025-01-01 (366 days), and a 366-day
    hold that is NOT a full year (2023-01-01 -> 2024-01-02, one year + one
    day) is long. Any OTHER short_term_days value is a literal max-day
    cutoff used as-is."""
    if short_term_days == 365:
        one_year = pd.Timestamp(entry) + pd.DateOffset(years=1)
        return "short" if pd.Timestamp(exit_dt) <= one_year else "long"
    held = (pd.Timestamp(exit_dt) - pd.Timestamp(entry)).days
    return "short" if held <= short_term_days else "long"


def wash_adjust(trades: pd.DataFrame,
                config: "TaxConfig | None" = None) -> pd.DataFrame:
    """
    Returns a copy of `trades` with the basis accounting columns appended:
      shares             exact or inferred share count per trade
      gross_pnl_dollars  accounting pnl MINUS any deferred losses attaching
                         to this lot from a prior wash (i.e. this lot's
                         realizable P&L on its adjusted basis)
      wash_disallowed    dollars of this lot's loss deferred to the
                         replacement lot (positive; 0 when nothing is
                         disallowed)
      adj_pnl_dollars    P&L recognized for tax after wash adjustment
      tax_bucket         "short" | "long" by held days

    Columns are RELATIVE to trade order per symbol after a (symbol,
    entry_date) sort -- results are independent of input row order. Ties in
    (symbol, entry_date) (impossible from the engine, possible in a
    hand-built frame) break deterministically on exit_date, so the result is
    also independent of the INPUT frame's own row order ticking the sort.

    Per-symbol serial rule (correct under the engine's one-position-at-a-time
    invariant): a lot with gross_pnl_dollars < 0 defers min(loss, loss *
    replacement_shares / sold_shares) when the NEXT same-symbol entry falls
    within [exit_date, exit_date + wash_window_days]. The deferred loss
    attaches to that next lot's basis (pending accumulator), so chained
    wash-sales roll forward until a gap > wash_window_days or the data ends
    -- at which point the accumulated deferred loss is finally recognized.
    Short sales are symmetric on their covering purchases.
    """
    cfg = resolve(config)
    missing = REQUIRED_COLS - set(trades.columns)
    if missing:
        raise ValueError(f"trades missing required columns {sorted(missing)}")
    if not cfg.apply_to_shorts and (trades["side"] == "short").any():
        raise ValueError(
            "apply_to_shorts=False with short trades present: configure "
            "the engine's short side or filter shorts before calling")

    df = trades.copy()
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce")
    df["exit_date"] = pd.to_datetime(df["exit_date"], errors="coerce")
    pnl_check = pd.to_numeric(df["pnl_dollars"], errors="coerce")
    entry_px = pd.to_numeric(df["entry_price"], errors="coerce")
    exit_px = pd.to_numeric(df["exit_price"], errors="coerce")
    if (df["entry_date"].isna() | df["exit_date"].isna()).any():
        raise ValueError("trades contain NaT entry_date/exit_date")
    if not np.isfinite(pnl_check).all():
        raise ValueError("pnl_dollars must be finite (no NaN/inf) for every trade")
    if ((~np.isfinite(entry_px.to_numpy(dtype=float)))
            | (~np.isfinite(exit_px.to_numpy(dtype=float)))
            | (entry_px <= 0) | (exit_px <= 0)).any():
        raise ValueError("entry_price/exit_price must be finite and > 0")

    # Deterministic ordering: distinct (symbol, entry_date) keys are the
    # wash adjacency contract; ties (impossible from the engine, possible
    # from a hand-built frame) break on exit_date so the result never
    # depends on pandas' stable-sort input order.
    df = df.sort_values(["symbol", "entry_date", "exit_date"]).reset_index(drop=True)
    shares = _shares_series(df)
    pnl = pd.to_numeric(df["pnl_dollars"], errors="coerce")

    n = len(df)
    syms = df["symbol"].to_numpy()
    exits = pd.DatetimeIndex(df["exit_date"])
    entries = pd.DatetimeIndex(df["entry_date"])
    del pnl_check, entry_px, exit_px
    pnl = pnl.to_numpy(dtype=float)

    gross = np.full(n, np.nan)
    disallowed = np.zeros(n)
    adj = np.full(n, np.nan)
    bucket = np.empty(n, dtype=object)
    pending = {}

    for i in range(n):
        sym = syms[i]
        basis_in = pending.pop(sym, 0.0)
        g = float(pnl[i]) - basis_in
        loss = min(g, 0.0)

        ratio = 0.0
        if loss < 0 and cfg.wash_window_days > 0 and i + 1 < n and syms[i + 1] == sym:
            gap_days = int((entries[i + 1] - exits[i]).days)
            if not (0 <= gap_days <= cfg.wash_window_days):
                ratio = 0.0
            else:
                sold_sh, repl_sh = shares.iloc[i], shares.iloc[i + 1]
                if (np.isfinite(sold_sh) and sold_sh > 0 and np.isfinite(repl_sh)
                        and repl_sh >= 0):
                    ratio = min(1.0, repl_sh / sold_sh)
                else:
                    # Unknowable share ratio -> assume full replacement. Only
                    # reachable with the inferred-notional heuristic (a real
                    # -0.0/-tiny pct), and only shifts a loss's timing, never
                    # its total. See _shares_series docstring.
                    ratio = 1.0
        d = -loss * ratio if loss < 0 and ratio > 0 else 0.0

        gross[i] = g
        disallowed[i] = d
        adj[i] = g + d
        bucket[i] = _bucket_days(entries[i], exits[i], cfg.short_term_days)
        if d > 0:
            pending[sym] = d    # attaches to the replacement lot's basis

    df["shares"] = shares
    df["gross_pnl_dollars"] = gross
    df["wash_disallowed"] = disallowed
    df["adj_pnl_dollars"] = adj
    df["tax_bucket"] = bucket
    return df


def wash_report(trades: pd.DataFrame,
                config: "TaxConfig | None" = None) -> pd.DataFrame:
    """One row per wash event (a loss whose disallowed portion rolled to a
    replacement lot): the sold lot, the loss deferred, and the replacement
    lot it attaches to. Empty frame when no wash events occurred."""
    df = wash_adjust(trades, config)
    ew = df.index[df["wash_disallowed"] > 0]
    rows = []
    for i in ew:
        repl = i + 1
        if repl >= len(df):
            continue
        left, right = df.iloc[i], df.iloc[repl]
        rows.append({
            "symbol": left["symbol"],
            "sold_exit_date": left["exit_date"],
            "sold_shares": left["shares"],
            "loss_deferred": left["wash_disallowed"],
            "loss_total": left["gross_pnl_dollars"],
            "replacement_entry_date": right["entry_date"],
            "replacement_shares": right["shares"],
            "replacement_side": right["side"],
        })
    if not rows:
        return pd.DataFrame(columns=[
            "symbol", "sold_exit_date", "sold_shares", "loss_deferred",
            "loss_total", "replacement_entry_date", "replacement_shares",
            "replacement_side"])
    return pd.DataFrame(rows)


def tax_year_summary(trades: pd.DataFrame,
                     config: "TaxConfig | None" = None) -> pd.DataFrame:
    """
    Per-calendar-year (by exit date) tax accounting over the wash-adjusted
    frame:
      realized_pnl_tax     sum of adj_pnl_dollars recognized that year
      net_short/net_long   bucket net P&L BEFORE cross-bucket offsetting
      taxable_short/long   after ST losses offset LT gains and vice versa
      loss_deducted        excess capital loss claimed (loss_deduction_annual
                           cap; nothing beyond is carried forward)
      tax_due              short*tax_st + long*tax_lt (capital gains tax;
                           excess-loss benefit against ORDINARY income is
                           reported loss_deducted, not subtracted here --
                           ordinary income is out of this module's scope)
      after_tax_pnl        realized - tax_due
      tax_drag_pct         100 * tax_due / realized (None when no gain)

    Netting model (a simplification, stated plainly): within each bucket the
    year's gains and losses net; a net loss in one bucket offsets the other
    bucket's net gain; any residual loss is deductible against ordinary
    income up to loss_deduction_annual -- reported as loss_deducted since its
    benefit accrues to ordinary income, which is not modeled here.
    """
    df = wash_adjust(trades, config)
    if df.empty:
        return pd.DataFrame(columns=[
            "year", "n_trades", "realized_pnl_tax", "net_short", "net_long",
            "taxable_short", "taxable_long", "loss_deducted", "tax_due",
            "after_tax_pnl", "tax_drag_pct"])
    df["year"] = df["exit_date"].dt.year
    out = []
    cfg = resolve(config)
    for year, g in df.groupby("year", sort=True):
        short = g.loc[g["tax_bucket"] == "short", "adj_pnl_dollars"].sum()
        long = g.loc[g["tax_bucket"] == "long", "adj_pnl_dollars"].sum()
        taxable_s, taxable_l, leftover = _offset(short, long)
        deduction = min(leftover, cfg.loss_deduction_annual)
        tax = max(taxable_s, 0.0) * cfg.st_rate \
            + max(taxable_l, 0.0) * cfg.lt_rate
        realized = float(g["adj_pnl_dollars"].sum())
        after = realized - tax
        out.append({
            "year": int(year),
            "n_trades": int(len(g)),
            "realized_pnl_tax": round(realized, 2),
            "net_short": round(float(short), 2),
            "net_long": round(float(long), 2),
            "taxable_short": round(float(taxable_s), 2),
            "taxable_long": round(float(taxable_l), 2),
            "loss_deducted": round(float(deduction), 2),
            "tax_due": round(float(tax), 2),
            "after_tax_pnl": round(after, 2),
            "tax_drag_pct": round(100 * float(tax / realized), 2) if realized > 0 else None,
        })
    return pd.DataFrame(out)


def _offset(short_net: float, long_net: float) -> "tuple[float, float, float]":
    """Cross-bucket loss offsetting: a net loss in one bucket reduces the
    other's net gain. Returns (taxable_short, taxable_long, leftover_loss)
    where leftover_loss holds any residual net loss remaining after both
    buckets are netted -- e.g. short=-2000, long=+1500 -> (0, 0, 500).
    Inside a single bucket there is nothing to offset, so a both-loss year
    keeps its full combined loss as leftover (deductible against ordinary
    income, capped per-year downstream)."""
    g, l = float(short_net), float(long_net)
    if g > 0 and l < 0:
        combined = g + l
        g = max(0.0, combined)
        leftover = max(0.0, -combined)
        l = 0.0
    elif l > 0 and g < 0:
        combined = l + g
        l = max(0.0, combined)
        leftover = max(0.0, -combined)
        g = 0.0
    elif g <= 0 and l <= 0:
        leftover = -g - l
        g = 0.0
        l = 0.0
    else:  # both buckets gained: nothing to offset, nothing left over
        leftover = 0.0
    if g < 0:
        leftover += -g
        g = 0.0
    if l < 0:
        leftover += -l
        l = 0.0
    return g, l, leftover


def tax_summary(trades: pd.DataFrame,
                config: "TaxConfig | None" = None) -> dict:
    """One flat dict, sized to mirror trades.trade_summary()'s shape so the
    registry can register the numeric leaves as `trades_tax` rows.
    n_wash_events counts ROWS that deferred any amount. wash_disallowed_total
    is the cumulative deferred-flow sum -- on a wash chain it can exceed the
    year's total loss (a loss deferred twice through two replacement lots is
    counted in both the disallow and the attach), matching wash_report()'s
    one-row-per-event semantics."""
    years = tax_year_summary(trades, config)
    adj = wash_adjust(trades, config)
    realized = float(adj["adj_pnl_dollars"].sum())
    tax = float(years["tax_due"].sum()) if not years.empty else 0.0
    net_short = float(adj.loc[adj["tax_bucket"] == "short", "adj_pnl_dollars"].sum())
    net_long = float(adj.loc[adj["tax_bucket"] == "long", "adj_pnl_dollars"].sum())
    return {
        "n_trades": int(len(adj)),
        "total_pnl_dollars": round(float(trades["pnl_dollars"].sum()), 2),
        "after_tax_pnl_dollars": round(realized - tax, 2),
        "tax_due_total": round(tax, 2),
        "wash_disallowed_total": round(float(adj["wash_disallowed"].sum()), 2),
        "n_wash_events": int((adj["wash_disallowed"] > 0).sum()),
        "net_short_term": round(net_short, 2),
        "net_long_term": round(net_long, 2),
        "tax_drag_pct": round(100 * tax / realized, 2) if realized > 0 else None,
    }