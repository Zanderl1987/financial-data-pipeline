import requests
import re

# Search iShares for SGOV
r = requests.get('https://www.ishares.com/us/products/etf-investments?switchLocale=y&siteEntryPassthrough=true&dataView=keyFacts&fundType=ETF&assetClass=fixedIncome&view=keyFacts', timeout=15)

# Look for SGOV in the page
if 'SGOV' in r.text:
    print('SGOV found in screener')
    # Extract the product URL
    matches = re.findall(r'href=["\']([^"\']*SGOV[^"\']*)["\']', r.text)
    for m in matches:
        print(f'  Link: {m}')

# Also try the product page with SGOV ticker
r2 = requests.get('https://www.ishares.com/us/products/239747', timeout=15)
match = re.search(r'<title[^>]*>([^<]+)</title>', r2.text)
title = match.group(1) if match else 'No title'
print(f'Product 239747: {r2.status_code}: {title}')