"""
Event study: gold (GC=F) and oil (WTI/Brent) reaction to Iran / Strait of Hormuz
flashpoints, 2011-2026, including the ongoing 2026 Strait of Hormuz crisis.

Companion script for experiments/2026-08-07_hormuz-gold-oil-event-study.md.
Run: C:\\ProgramData\\anaconda3\\python.exe experiments/2026-08-07_hormuz_gold_oil_event_study.py
"""
import pandas as pd

FUT = pd.read_parquet('storage/curated/futures/futures.parquet')
COMM = pd.read_parquet('storage/curated/commodities/commodities.parquet')
PRICES = pd.read_parquet('storage/curated/prices/prices.parquet', columns=['symbol', 'date', 'close', 'pct_change'])

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


def prep_fred(series_id):
    s = COMM[COMM['series_id'] == series_id].sort_values('date').copy()
    s['date'] = pd.to_datetime(s['date'])
    s = s.reset_index(drop=True).rename(columns={'value': 'close'})
    s['ret'] = s['close'].pct_change()
    return s


def prep_prices(symbol):
    s = PRICES[PRICES['symbol'] == symbol].sort_values('date').copy()
    s['date'] = pd.to_datetime(s['date'])
    return s.reset_index(drop=True)


def window_stats(series, anchor_date_str, horizons=(1, 3, 5, 10)):
    """cum_ret_Nd = close N trading days after the event / prior close - 1.
    'prior close' = last close strictly before the event's first available trading day
    (this is the pre-event baseline; day0_ret is that first trading day's own return)."""
    anchor = pd.Timestamp(anchor_date_str)
    idx = series.index[series['date'] >= anchor]
    if len(idx) == 0:
        return None
    i0 = idx[0]
    base_price = series.loc[i0 - 1, 'close'] if i0 > 0 else series.loc[i0, 'close']
    out = {'anchor_date': series.loc[i0, 'date'].date(), 'day0_ret': series.loc[i0, 'ret']}
    for h in horizons:
        j = i0 + h
        if j < len(series):
            out[f'cum_ret_{h}d'] = series.loc[j, 'close'] / base_price - 1
    return out


def main():
    gold = prep_futures('GC=F')
    wti = prep_futures('CL=F')
    brent = prep_fred('DCOILBRENTEU')

    rows = []
    for label, d in EVENTS.items():
        row = {'event': label}
        for name, series in [('gold', gold), ('wti', wti), ('brent', brent)]:
            st = window_stats(series, d)
            if st:
                for k, v in st.items():
                    if k != 'anchor_date':
                        row[f'{name}_{k}'] = v
        rows.append(row)

    res = pd.DataFrame(rows).set_index('event')
    pd.set_option('display.width', 220)
    pd.set_option('display.max_columns', 30)
    pd.set_option('display.float_format', lambda x: f'{x:.4f}')

    print("=" * 100)
    print("EVENT STUDY: gold vs WTI vs Brent, day-of / +3d / +10d cumulative return vs prior close")
    print("n =", len(res), "events, 2011-01-01 to 2026-03-19")
    print("=" * 100)
    cols = ['gold_day0_ret', 'wti_day0_ret', 'brent_day0_ret',
            'gold_cum_ret_3d', 'wti_cum_ret_3d', 'brent_cum_ret_3d',
            'gold_cum_ret_10d', 'wti_cum_ret_10d', 'brent_cum_ret_10d']
    print(res[cols].to_string())

    corr = res[['gold_cum_ret_10d', 'wti_cum_ret_10d']].corr().iloc[0, 1]
    print()
    print(f"Pearson corr(gold_10d, wti_10d) across the {len(res)} events: {corr:.3f}")

    # 2026 crisis: day-of reaction to the six specific developments discussed in the writeup
    print()
    print("=" * 100)
    print("2026 STRAIT OF HORMUZ CRISIS: gold day-of / next-day reaction to each development")
    print("=" * 100)
    crisis_dates = {
        "2026-03-02 war begins (Sat 2/28 -> Mon)": "2026-03-02",
        "2026-03-04 Iran declares Strait closed": "2026-03-04",
        "2026-03-19 US campaign to reopen Strait": "2026-03-19",
        "2026-04-08 2-week truce announced": "2026-04-08",
        "2026-06-17 ceasefire MOU signed": "2026-06-17",
        "2026-06-18 (day after MOU)": "2026-06-18",
        "2026-07-06 Iran fires on 3 tankers (MOU violation)": "2026-07-06",
    }
    for label, d in crisis_dates.items():
        row = gold[gold['date'] == d]
        if len(row):
            print(f"{label:55s} close={row['close'].values[0]:.1f}  day_ret={row['ret'].values[0]:+.4f}")
        else:
            print(f"{label:55s} no trading data")

    # cross-check the two largest single-day gold moves in the 2026 series against GLD
    # (rules out a continuous-futures contract-roll artifact)
    print()
    print("=" * 100)
    print("Data-quality cross-check: GC=F vs GLD on the two largest 2026 single-day gold moves")
    print("=" * 100)
    gld = prep_prices('GLD')
    for d in ['2026-01-30', '2026-03-19']:
        gc_row = gold[gold['date'] == d]
        gld_row = gld[gld['date'] == d]
        print(f"{d}  GC=F ret={gc_row['ret'].values[0]:+.4f}   GLD pct_change={gld_row['pct_change'].values[0]:+.4f}")

    # oil 2026 levels: pre-war baseline, peak, latest available
    print()
    print("=" * 100)
    print("WTI / Brent 2026 levels: pre-war baseline vs peak vs latest available")
    print("=" * 100)
    wti_2026 = wti[wti['date'] >= '2026-01-01']
    brent_2026 = brent[brent['date'] >= '2026-01-01']
    print("WTI  Jan-2 baseline:", wti_2026.iloc[0][['date', 'close']].to_dict())
    print("WTI  peak:          ", wti_2026.loc[wti_2026['close'].idxmax()][['date', 'close']].to_dict())
    print("WTI  latest:        ", wti_2026.iloc[-1][['date', 'close']].to_dict())
    print("Brent Jan-2 baseline:", brent_2026.iloc[0][['date', 'close']].to_dict())
    print("Brent peak:          ", brent_2026.loc[brent_2026['close'].idxmax()][['date', 'close']].to_dict())
    print("Brent latest:        ", brent_2026.iloc[-1][['date', 'close']].to_dict())


if __name__ == '__main__':
    main()
