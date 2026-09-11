from curl_cffi import requests
import re

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://www.globalxetfs.com/',
}

ticker = 'AIQ'
fund_page = f'https://www.globalxetfs.com/funds/{ticker.lower()}/'

session = requests.Session()
session.headers.update(HEADERS)

r = session.get(fund_page, impersonate='chrome120', timeout=15)
print(f'Fund page: {r.status_code}')

# Look for ALL links on the page
links = re.findall(r'href=["\']([^"\']+)["\']', r.text)
for link in links:
    if 'holding' in link.lower() or 'csv' in link.lower() or 'download' in link.lower():
        print(f'Link: {link}')

# Check for any JavaScript that loads the data
if 'aiq_full-holdings' in r.text:
    print('Found CSV reference in page')
    
# Check for any API endpoints
api_links = re.findall(r'(https?://[^"\']*(?:api|ajax|json)[^"\']*)', r.text)
for link in api_links[:10]:
    print(f'API: {link}')