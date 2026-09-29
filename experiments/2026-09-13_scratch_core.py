import sys
sys.path.insert(0, '.')
import os
import pandas as pd
import numpy as np

# Test the core logic manually
ref = __import__('query').load('delisting_reference')
ref = ref[ref['price_table'] == 'SEP'].copy()
ref['firstpricedate'] = pd.to_datetime(ref['firstpricedate'], errors='coerce')
ref['lastpricedate'] = pd.to_datetime(ref['lastpricedate'], errors='coerce')
ref = ref[ref['firstpricedate'].notna() & ref['lastpricedate'].notna()]
sep = ref

panel = pd.read_parquet('storage/curated/prices/prices.parquet')
print('panel: {} symbols'.format(len(panel)))

de = sep[sep['isdelisted'] == 'Y'].copy()
print('de: {} alive: {}'.format(len(de), len(sep[sep['isdelisted'] == 'N'])))

# build genuine recovered
END_TOL = 370
ran = panel.groupby('symbol').agg(mn=('date', 'min'), mx=('date', 'max')).reset_index()
merged = de.merge(ran, left_on='ticker', right_on='symbol', how='inner')
merged['gap_end'] = (merged['mx'] - merged['lastpricedate']).dt.days
merged['gap_start'] = (merged['mn'] - merged['firstpricedate']).dt.days
cond_end = merged['gap_end'] <= END_TOL
cond_start = merged['gap_start'] >= -END_TOL
merged['genuine'] = cond_end & cond_start
genuine = merged[merged['genuine']]
rec_tickers = set(genuine['ticker'].tolist())
print('genuine recovered: {}'.format(len(rec_tickers)))

# compute next-month returns
panel['ret_30d'] = panel.groupby('symbol')['close'].pct_change(30)
ret_series = panel[panel['symbol'].isin(rec_tickers)]['ret_30d'].dropna()
print('recovered names with returns: {}'.format(len(ret_series)))
if len(ret_series) > 0:
    print('mean: {:.4f}%'.format(ret_series.mean()*100))
    print('% negative: {:.1f}%'.format((ret_series<0).sum()/len(ret_series)*100))
    print('min: {:.4f}%'.format(ret_series.min()*100))
else:
    print('no returns computable')

# Benchmarks
de_panel = panel[panel['symbol'].isin(sep[sep['isdelisted']=='Y']['ticker'])]
de_ret = de_panel.groupby('symbol')['close'].pct_change(30)
de_ret_series = de_ret.dropna()
print('delisted mean: {:.4f}%'.format(de_ret_series.mean()*100))

alive_tickers = set(sep[sep['isdelisted']=='N']['ticker'].tolist())
alive_panel = panel[panel['symbol'].isin(alive_tickers)]
alive_ret = alive_panel.groupby('symbol')['close'].pct_change(30)
alive_ret_series = alive_ret.dropna()
print('alive mean: {:.4f}%'.format(alive_ret_series.mean()*100))

# Key test
print('--- Comparison ---')
print('Recovered mean: {:.4f}%'.format(ret_series.mean()*100))
print('Delisted mean: {:.4f}%'.format(de_ret_series.mean()*100))
if ret_series.mean() < de_ret_series.mean():
    print('Recovered names have MORE NEGATIVE mean returns than the delisted universe average.')
    print('This SUPPORTS the constructive UMD bound assumption.')
else:
    print('Recovered names have LESS NEGATIVE (or more positive) mean returns than the delisted universe average.')
    print('This does NOT support the assumption.')