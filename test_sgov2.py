import requests
import re

pid = '239748'
url = f'https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document?appType=PRODUCT_PAGE&appSubType=ONE&targetSite=one&locale=en_US&portfolioId={pid}&component=fundDownload&userType=individual'
r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
print(f'Status: {r.status_code}')

# Check for fund name in the XML
content = r.content.decode('utf-8', errors='replace')
# Look for fund name patterns
for pattern in ['SGOV', 'iShares 0-3 Month Treasury', '0-3 Month Treasury', 'Short Treasury']:
    if pattern in content:
        print(f'Found "{pattern}" in response')
        
# Check first 5000 chars for structure
print(content[:2000])