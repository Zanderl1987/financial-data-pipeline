import requests

pid = '239748'
url = f'https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document?appType=PRODUCT_PAGE&appSubType=ONE&targetSite=one&locale=en_US&portfolioId={pid}&component=fundDownload&userType=individual'
r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)

# Check the raw content around line 47
raw = r.content.decode('utf-8', errors='replace')
lines = raw.split('\n')
for i, line in enumerate(lines):
    if 45 <= i <= 50:
        print(f'Line {i}: {repr(line)}')