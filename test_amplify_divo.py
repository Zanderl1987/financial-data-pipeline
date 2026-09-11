from curl_cffi import requests
import re

# Check DIVO page with curl_cffi
session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
})

r = session.get('https://amplifyetfs.com/funds/divo/', impersonate='chrome120', timeout=15)
print(f'DIVO page: {r.status_code}')

# Look for holdings table in HTML
if 'holding' in r.text.lower():
    print('Page contains "holding"')
    
# Find all tables
tables = re.findall(r'<table[^>]*>.*?</table>', r.text, re.DOTALL | re.IGNORECASE)
for i, t in enumerate(tables[:5]):
    if 'holding' in t.lower() or 'ticker' in t.lower() or 'cusip' in t.lower():
        print(f'Table {i}: {t[:500]}')
        print('---')

# Look for any data attributes or embedded JSON
json_data = re.findall(r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>', r.text, re.DOTALL)
for j in json_data[:3]:
    if 'holding' in j.lower() or 'portfolio' in j.lower():
        print(f'JSON script: {j[:500]}')
        print('---')

# Check for any download links
links = re.findall(r'href=["\']([^"\']*(?:csv|xlsx|download)[^"\']*)["\']', r.text, re.IGNORECASE)
for link in links[:10]:
    print(f'Download link: {link}')