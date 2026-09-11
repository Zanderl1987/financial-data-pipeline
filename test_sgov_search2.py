import requests
import re

r = requests.get('https://www.ishares.com/us/products/etf-investments?switchLocale=y&siteEntryPassthrough=true&dataView=keyFacts&fundType=ETF&assetClass=fixedIncome&view=keyFacts', timeout=15)

# Look for SGOV links
matches = re.findall(r'href=["\']([^"\']*SGOV[^"\']*)["\']', r.text)
for m in matches:
    print(f'Link: {m}')

# Search for product ID near SGOV
for m in re.finditer(r'productId["\']?\s*[:=]\s*["\']?(\d+)["\']?', r.text):
    pid = m.group(1)
    context = r.text[max(0,m.start()-200):m.end()+200]
    if 'SGOV' in context:
        print(f'productId near SGOV: {pid}')