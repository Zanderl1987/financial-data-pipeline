from curl_cffi import requests as cffi_requests
import re

# Check ROBX page source
r = cffi_requests.get('https://amplifyetfs.com/robx-holdings/', impersonate='chrome120', timeout=15)
print(f'ROBX page: {r.status_code}')

# Look for any data patterns
if 'holding' in r.text.lower():
    # Find script tags with data
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', r.text, re.DOTALL)
    for i, s in enumerate(scripts):
        if 'holding' in s.lower() and len(s) > 100:
            print(f'Script {i} (holding): {s[:300]}')
            print('---')

# Look for any JSON data
json_matches = re.findall(r'(\{.*?"holding".*?\})', r.text)
for m in json_matches[:5]:
    print(f'JSON match: {m[:200]}')
    print('---')

# Look for table
if '<table' in r.text.lower():
    tables = re.findall(r'<table[^>]*>.*?</table>', r.text, re.DOTALL | re.IGNORECASE)
    for t in tables[:2]:
        print(f'Table: {t[:500]}')
        print('---')