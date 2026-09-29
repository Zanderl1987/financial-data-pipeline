import sys, os
sys.path.insert(0, '.')
import query as q
import pandas as pd
import numpy as np

ref = q.load('delisting_reference')
ref = ref[ref['price_table'] == 'SEP'].copy()
ref['firstpricedate'] = pd.to_datetime(ref['firstpricedate'], errors='coerce')
ref['lastpricedate'] = pd.to_datetime(ref['lastpricedate'], errors='coerce')
ref = ref[ref['firstpricedate'].notna() & ref['lastpricedate'].notna()]
sep = ref

panel = pd.read_parquet('storage/curated/prices/prices.parquet')
print(f'panel: {len(panel)} symbols')

de = sep[sep['isdelisted'] == 'Y'].copy()
print(f'de: {len(de)} alive: {len(sep[sep['isdelisted']==\'N\'])}")

# build genuine recovered
END_TOL = 370
ran = panel.groupby('symbol').agg(mn=('date','min'), mx=('date','max')).reset_index()
merged = de.merge(ran, left_on='ticker', right_on='symbol', how='inner')
merged['gap_end'] = (merged['mx'] - merged['lastpricedate']).dt.days
merged['gap_start'] = (merged['mn'] - merged['firstpricedate']).dt.days
merged['genuine'] = (merged['gap_end'] <= END_TOL) & (merged['gap_start'] >= -END_TOL)
genuine = merged[merged['genuine']]
rec_tickers = set(genuine['ticker'].tolist())
print(f'genuine recovered: {len(rec_tickers)}')

# compute next-month returns
panel['ret_30d'] = panel.groupby('symbol')['close'].pct_change(30)
ret_series = panel[panel['symbol'].isin(rec_tickers)]['ret_30d'].dropna()
print(f'recovered names with returns: {len(ret_series)}')
if len(ret_series) > 0:
    print(f'mean: {ret_series.mean()*100:.4f}%')
    print(f'% negative: {(ret_series<0).sum()/len(ret_series)*100:.1f}%')
    print(f'min: {ret_series.min()*100:.4f}%')
else:
    print('no returns computable')