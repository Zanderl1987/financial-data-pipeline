from curl_cffi import requests
import re

session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
})

# Check the DIVO-holdings page
r = session.get('https://amplifyetfs.com/divo-holdings/', impersonate='chrome120', timeout=15)
print(f'DIVO-holdings page: {r.status_code}')

# Look for tables
tables = re.findall(r'<table[^>]*>.*?</table>', r.text, re.DOTALL | re.IGNORECASE)
for i, t in enumerate(tables[:10]):
    if 'holding' in t.lower() or 'ticker' in t.lower() or 'cusip' in t.lower() or 'name' in t.lower():
        print(f'Table {i}: {t[:800]}')
        print('---')

# Look for embedded data
json_data = re.findall(r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>', r.text, re.DOTALL)
for j in json_data[:5]:
    if 'holding' in j.lower() or 'portfolio' in j.lower() or 'data' in j.lower():
        print(f'JSON script: {j[:500]}')
        print('---')

# Check for any data attributes
data_attrs = re.findall(r'data-(\w+)="([^"]*)"', r.text)
for k, v in data_attrs[:20]:
    if 'holding' in k.lower() or 'fund' in k.lower() or 'portfolio' in k.lower():
        print(f'data-{k}="{v[:100]}"')