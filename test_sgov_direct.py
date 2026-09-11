import requests
import re

# Try the iShares search endpoint
r = requests.get('https://www.ishares.com/us/products/etf-investments', timeout=15)
print(f'ETF page: {r.status_code}')

# Look for SGOV in the page source
if 'SGOV' in r.text:
    print('SGOV found in ETF page')
    # Find the product link
    matches = re.findall(r'href=["\']([^"\']*products[^"\']*SGOV[^"\']*)["\']', r.text)
    for m in matches:
        print(f'  Link: {m}')
else:
    print('SGOV not found in ETF page')

# Try the product finder API
r = requests.get('https://www.ishares.com/us/products/etf-investments?fundType=ETF&assetClass=fixedIncome&view=keyFacts&dataView=keyFacts', timeout=15)
print(f'Screener page: {r.status_code}')

# Look for SGOV
if 'SGOV' in r.text:
    print('SGOV found in screener')
    # Extract the full context around SGOV
    idx = r.text.find('SGOV')
    print(f'Context: {r.text[max(0,idx-200):idx+200]}')