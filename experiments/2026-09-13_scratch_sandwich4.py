import sys
sys.path.insert(0, '.')
import pandas as pd
import numpy as np

ref = __import__('query').load('delisting_reference')
ref = ref[ref['price_table'] == 'SEP'].copy()
ref['firstpricedate'] = pd.to_datetime(ref['firstpricedate'], errors='coerce')
ref['lastpricedate'] = pd.to_datetime(ref['lastpricedate'], errors='coerce')
ref = ref[ref['firstpricedate'].notna() & ref['lastpricedate'].notna()]
sep = ref

panel = pd.read_parquet('storage/curated/prices/prices.parquet')
print('panel: {} symbols'.format(len(panel)))

de = sep[sep['isdelisted'] == 'Y'].copy()
print('delisted: {} alive: {}'.format(len(de), len(sep[sep['isdelisted'] == 'N'])))

# Build per-symbol min/max dates from the panel
ran = panel.groupby('symbol').agg(mn=('date', 'min'), mx=('date', 'max')).reset_index()
ran['mn'] = pd.to_datetime(ran['mn'], errors='coerce')
ran['mx'] = pd.to_datetime(ran['mx'], errors='coerce')

merged = de.merge(ran, left_on='ticker', right_on='symbol', how='inner')

# Compute gap metrics
merged['gap_end_days'] = (merged['mx'] - merged['lastpricedate']).dt.days
merged['gap_start_days'] = (merged['mn'] - merged['firstpricedate']).dt.days

END_TOL = 370
cond_end = merged['gap_end_days'] <= END_TOL
cond_start = merged['gap_start_days'] >= -END_TOL
merged['genuine'] = cond_end & cond_start

# Count by decade
merged['dec'] = merged['lastpricedate'].dt.year // 10 * 10
tot = merged.groupby('dec').size()
rec = merged[merged['genuine']].groupby('dec').size()
alloc = pd.DataFrame({'delisted': tot, 'recovered_genuine': rec}).fillna(0)
alloc['recovery_pct'] = (alloc['recovered_genuine'] / alloc['delisted'] * 100).round(2)
print('\\nGenuine recovered delisted names by decade:')
print(alloc.to_string())

# Extract the genuine recovered names' tickers
genuine_df = merged[merged['genuine']]
rec_tickers = set(genuine_df['ticker'].tolist())
n_rec = len(rec_tickers)
print('\\nGenuine recovered tickers: {} (from {} merged delisted names)'.format(n_rec, len(merged)))

if n_rec == 0:
    print('No genuine recovered names found - cannot compute returns.')
    sys.exit(0)

# Compute next-month returns for recovered names
panel['ret_30d'] = panel.groupby('symbol')['close'].pct_change(30)
ret_series = panel[panel['symbol'].isin(rec_tickers)]['ret_30d'].dropna()
print('\\nNext-month returns for {} recovered names:'.format(len(ret_series)))
if len(ret_series) > 0:
    print('mean: {:.4f}%'.format(ret_series.mean()*100))
    print('median: {:.4f}%'.format(ret_series.median()*100))
    print('% negative: {:.1f}%'.format((ret_series<0).sum()/len(ret_series)*100))
    print('min: {:.4f}%'.format(ret_series.min()*100))
    print('5th pctile: {:.4f}%'.format(ret_series.quantile(0.05)*100))

# Benchmark: all delisted names' next-month returns
# de has 'ticker' column, not 'symbol'; use the ticker column directly
de_ret = panel[panel['symbol'].isin(de['ticker'].tolist())].groupby('symbol')['close'].pct_change(30)
de_ret_series = de_ret.dropna()
print('\\nAll delisted names next-month return stats:')
print('mean: {:.4f}%'.format(de_ret_series.mean()*100))
print('% negative: {:.1f}%'.format((de_ret_series<0).sum()/len(de_ret_series)*100))
print('min: {:.4f}%'.format(de_ret_series.min()*100))

# Benchmark: SEP-alive names
alive_tickers = set(sep[sep['isdelisted']=='N']['ticker'].tolist())
alive_panel = panel[panel['symbol'].isin(alive_tickers)]
alive_ret = alive_panel.groupby('symbol')['close'].pct_change(30)
alive_ret_series = alive_ret.dropna()
print('\\nSEP-alive names next-month return stats:')
print('mean: {:.4f}%'.format(alive_ret_series.mean()*100))
print('% negative: {:.1f}%'.format((alive_ret_series<0).sum()/len(alive_ret_series)*100))
print('min: {:.4f}%'.format(alive_ret_series.min()*100))

# Key test
print('\\n--- Comparison ---')
print('Recovered mean return: {:.4f}%'.format(ret_series.mean()*100))
print('Delisted mean return: {:.4f}%'.format(de_ret_series.mean()*100))
if ret_series.mean() < de_ret_series.mean():
    print('Recovered names have MORE NEGATIVE mean returns than the delisted universe average.')
    print('This SUPPORTS the constructive UMD bound assumption that missing delisting-bound')
    print('names have negative returns, which would drag the delisting-inclusive L/S spread.')
else:
    print('Recovered names have LESS NEGATIVE (or more positive) mean returns than the')
    print('delisted universe average.')
    print('This does NOT support the assumption that missing delisting-bound names are')
    print('overwhelmingly negative; the 30-year verdict remains unchanged as a sidelight.')

# Also: what fraction of recovered names have returns below the delisted median?
below_median = (ret_series < de_ret_series.median()).sum()
print('\\nRecovered names with return below delisted median: {}/{} ({:.1f}%)'.format(
    below_median, len(ret_series), below_median/len(ret_series)*100))

# Overall verdict
print('\\n--- Overall Verdict ---')
if ret_series.mean() < de_ret_series.mean():
    print('Recovered names are more negative than the delisted average: supports UMD bound assumption.')
elif ret_series.mean() > 0:
    print('Recovered names have positive mean returns: does not support UMD bound assumption.')
else:
    print('Recovered names have mean near zero: neutral; UMD bound assumption not empirically supported,')
    print('but delisting-inclusive panels still require paid data to build.')