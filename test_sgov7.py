import requests
import re

pid = '239748'
url = f'https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document?appType=PRODUCT_PAGE&appSubType=ONE&targetSite=one&locale=en_US&portfolioId={pid}&component=fundDownload&userType=individual'
r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)

raw = r.content.decode('utf-8', errors='replace')
fixed = re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;|#)', '&', raw)

# Check line 47
lines = fixed.split('\n')
print(f'Fixed line 47: {repr(lines[46])}')

# Check raw line 47
raw_lines = raw.split('\n')
print(f'Raw line 47: {repr(raw_lines[46])}')