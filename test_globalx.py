import requests
import re

# Check GlobalX fund page for holdings links
r = requests.get('https://www.globalxetfs.com/funds/aiq/', timeout=10)
print(f'GlobalX AIQ page: {r.status_code}')

# Search for download/holdings links
links = re.findall(r'href="([^"]*holdings[^"]*)"', r.text, re.IGNORECASE)
print('Holdings links:', links[:20])

# Also check for any CSV links
csv_links = re.findall(r'href="([^"]*\.csv[^"]*)"', r.text, re.IGNORECASE)
print('CSV links:', csv_links[:20])

# Check other GlobalX funds
for ticker in ['COPX', 'DIV', 'QYLD']:
    r = requests.get(f'https://www.globalxetfs.com/funds/{ticker.lower()}/', timeout=10)
    print(f'{ticker} page: {r.status_code}')
    links = re.findall(r'href="([^"]*holdings[^"]*)"', r.text, re.IGNORECASE)
    if links:
        print(f'  {ticker} holdings links: {links[:5]}')
    csv_links = re.findall(r'href="([^"]*\.csv[^"]*)"', r.text, re.IGNORECASE)
    if csv_links:
        print(f'  {ticker} csv links: {csv_links[:5]}')