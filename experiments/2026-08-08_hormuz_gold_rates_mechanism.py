"""
Follow-up to experiments/2026-08-07_hormuz-gold-oil-event-study.md: does the
gold/oil divergence around Iran-Hormuz flashpoints run through real yields
(nominal 10Y Treasury minus 10Y breakeven inflation)? Gold has no yield, so
theory says gold should move inversely with real yields, not with oil per se.

Companion script for experiments/2026-08-08_hormuz-gold-rates-mechanism.md.
Run: C:\\ProgramData\\anaconda3\\python.exe experiments/2026-08-08_hormuz_gold_rates_mechanism.py
"""
import pandas as pd

FUT = pd.read_parquet('storage/curated/futures/futures.parquet')
RATES = pd.read_parquet('storage/curated/fred_rates_gdp_interest_rates/fred_rates_gdp_interest_rates.parquet')

# same 12 events as the 2026-08-07 writeup; T10YIE/T5YIE only go back to 2003-01-02,
# which still covers all of them (earliest event is 2011-12-27)
EVENTS = {
    "2011-12-27 Iran threatens Hormuz closure": "2011-12-27",
    "2019-06-20 Iran shoots down US drone": "2019-06-20",
    "2019-09-16 Abqaiq-Khurais attack (Saudi oil, Sat->Mon)": "2019-09-16",
    "2020-01-03 Soleimani killed": "2020-01-03",
    "2020-01-08 Iran retaliates (Ain al-Asad)": "2020-01-08",
    "2024-04-15 Iran attacks Israel directly (overnight Sat->Mon)": "2024-04-15",
    "2024-04-19 Israel retaliates vs Iran": "2024-04-19",
    "2024-10-01 Iran missile barrage on Israel": "2024-10-01",
    "2024-10-28 Israel retaliates vs Iran (Sat->Mon)": "2024-10-28",
    "2026-03-02 US-Israel launch war on Iran (Sat 2/28->Mon)": "2026-03-02",
    "2026-03-04 Iran declares Strait closed": "2026-03-04",
    "2026-03-19 US aerial campaign to reopen Strait": "2026-03-19",
}


def prep_futures(symbol):
    s = FUT[FUT['symbol'] == symbol].sort_values('date').copy()
    s['date'] = pd.to_datetime(s['date']).dt.tz_localize(None)
    s = s.reset_index(drop=True)
    s['ret'] = s['close'].pct_change()
    return s


def prep_rate_series(series_id):
    s = RATES[RATES['series_id'] == series_id][['date', 'value']].sort_values('date').copy()
    s['date'] = pd.to_datetime(s['date'])
    return s.rename(columns={'value': series_id}).reset_index(drop=True)


def build_real_yield():
    """Real yield proxy = nominal 10Y (DGS10) - 10Y breakeven inflation (T10YIE), in %.
    FRED publishes both on the same daily calendar (business days, holidays as NaN-gapped
    rows are simply absent, not zero-filled) so an inner merge on date is safe here."""
    dgs10 = prep_rate_series('DGS10')
    t10yie = prep_rate_series('T10YIE')
    m = dgs10.merge(t10yie, on='date', how='inner').sort_values('date').reset_index(drop=True)
    m['real10y'] = m['DGS10'] - m['T10YIE']
    return m


def level_window_stats(series, value_col, anchor_date_str, horizons=(1, 3, 5, 10)):
    """Change in LEVEL (percentage points, i.e. basis points / 100) vs the prior close,
    not a pct-return -- these are rates/spreads that can cross zero."""
    anchor = pd.Timestamp(anchor_date_str)
    idx = series.index[series['date'] >= anchor]
    if len(idx) == 0:
        return None
    i0 = idx[0]
    base = series.loc[i0 - 1, value_col] if i0 > 0 else series.loc[i0, value_col]
    out = {'anchor_date': series.loc[i0, 'date'].date(), 'day0_chg_bp': (series.loc[i0, value_col] - base) * 100}
    for h in horizons:
        j = i0 + h
        if j < len(series):
            out[f'chg_{h}d_bp'] = (series.loc[j, value_col] - base) * 100
    return out


def main():
    gold = prep_futures('GC=F')
    real = build_real_yield()
    breakeven = prep_rate_series('T10YIE')
    nominal = prep_rate_series('DGS10')

    rows = []
    for label, d in EVENTS.items():
        row = {'event': label}
        for name, series, col in [
            ('real10y', real, 'real10y'),
            ('nominal10y', nominal, 'DGS10'),
            ('breakeven10y', breakeven, 'T10YIE'),
        ]:
            st = level_window_stats(series, col, d)
            if st:
                for k, v in st.items():
                    if k != 'anchor_date':
                        row[f'{name}_{k}'] = v
        rows.append(row)

    res = pd.DataFrame(rows).set_index('event')
    pd.set_option('display.width', 220)
    pd.set_option('display.max_columns', 30)
    pd.set_option('display.float_format', lambda x: f'{x:.1f}')

    print("=" * 110)
    print("REAL YIELD (DGS10 - T10YIE), NOMINAL 10Y, AND BREAKEVEN INFLATION around the same 12 events")
    print("all values in basis points (bp) change vs. prior close level; n =", len(res))
    print("=" * 110)
    cols = ['real10y_day0_chg_bp', 'real10y_chg_3d_bp', 'real10y_chg_10d_bp',
            'nominal10y_chg_10d_bp', 'breakeven10y_chg_10d_bp']
    print(res[cols].to_string())

    # pull gold's 10d cumulative return (same formula as the 2026-08-07 script) for correlation
    def gold_10d_ret(d):
        anchor = pd.Timestamp(d)
        idx = gold.index[gold['date'] >= anchor]
        if len(idx) == 0:
            return None
        i0 = idx[0]
        base = gold.loc[i0 - 1, 'close'] if i0 > 0 else gold.loc[i0, 'close']
        j = i0 + 10
        if j >= len(gold):
            return None
        return gold.loc[j, 'close'] / base - 1

    res['gold_10d_ret'] = [gold_10d_ret(d) for d in EVENTS.values()]

    print()
    corr_real = res[['gold_10d_ret', 'real10y_chg_10d_bp']].corr().iloc[0, 1]
    corr_break = res[['gold_10d_ret', 'breakeven10y_chg_10d_bp']].corr().iloc[0, 1]
    corr_nom = res[['gold_10d_ret', 'nominal10y_chg_10d_bp']].corr().iloc[0, 1]
    print(f"Pearson corr(gold_10d_ret, real10y_10d_chg_bp)      n={res['real10y_chg_10d_bp'].notna().sum()}: {corr_real:.3f}")
    print(f"Pearson corr(gold_10d_ret, breakeven10y_10d_chg_bp) n={res['breakeven10y_chg_10d_bp'].notna().sum()}: {corr_break:.3f}")
    print(f"Pearson corr(gold_10d_ret, nominal10y_10d_chg_bp)   n={res['nominal10y_chg_10d_bp'].notna().sum()}: {corr_nom:.3f}")

    # 2026 crisis specific: what happened to real yields around the war-start / closure /
    # reopening-campaign events, where gold fell hardest despite the war
    print()
    print("=" * 110)
    print("2026 crisis close-up: real yield level and change on each key date")
    print("=" * 110)
    for label, d in {
        "2026-02-27 (pre-war baseline, Fri before war)": "2026-02-27",
        "2026-03-02 war begins": "2026-03-02",
        "2026-03-04 Iran declares Strait closed": "2026-03-04",
        "2026-03-19 US campaign to reopen Strait": "2026-03-19",
        "2026-06-17 ceasefire MOU signed": "2026-06-17",
    }.items():
        row = real[real['date'] == d]
        if len(row):
            print(f"{label:50s} real10y={row['real10y'].values[0]:.2f}%  DGS10={row['DGS10'].values[0]:.2f}%  T10YIE={row['T10YIE'].values[0]:.2f}%")
        else:
            print(f"{label:50s} no data for this date")


if __name__ == '__main__':
    main()
