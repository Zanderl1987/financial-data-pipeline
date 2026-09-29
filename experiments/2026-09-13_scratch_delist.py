import sys
sys.path.insert(0, '.')
import os
import query as q

print('Python path:', sys.path[:3])
print('Cwd:', os.getcwd())
print('Panel exists:', os.path.exists('storage/curated/prices/prices.parquet'))

ref = q.load('delisting_reference')
print('ref loaded: {} rows'.format(len(ref)))
sep = ref[ref['price_table'] == 'SEP']
print('SEP filter: {} rows'.format(len(sep)))
alive_mask = sep['isdelisted'] == 'N'
delisted_mask = sep['isdelisted'] == 'Y'
print('alive: {}, delisted: {}'.format(alive_mask.sum(), delisted_mask.sum()))