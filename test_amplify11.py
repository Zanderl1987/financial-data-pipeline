from curl_cffi import requests as cffi_requests
import re

# Check the ROBX page more carefully for data sources
r = cffi_requests.get('https://amplifyetfs.com/robx-holdings/', impersonate='chrome120', timeout=15)

# Look for any file URLs (csv, xlsx, json)
file_patterns = [
    r'href=["\']([^"\']+\.csv[^"\']*)["\']',
    r'href=["\']([^"\']+\.xlsx[^"\']*)["\']',
    r'href=["\']([^"\']+\.json[^"\']*)["\']',
    r'src=["\']([^"\']+\.(csv|xlsx|json)[^"\']*)["\']',
    r'data-url=["\']([^"\']*)["\']',
    r'data-src=["\']([^"\']*)["\']',
]

for pattern in file_patterns:
    matches = re.findall(pattern, r.text, re.IGNORECASE)
    if matches:
        print(f'Pattern: {pattern[:50]}')
        for m in matches:
            if isinstance(m, tuple):
                print(f'  {m}')
            else:
                print(f'  {m}')

# Look for fund ticker in page
if 'ROBX' in r.text:
    print('ROBX found in page')

# Check if there's a specific holdings table with data
tables = re.findall(r'<table[^>]*class=["\']([^"\']*amplify[^"\']*)["\'][^>]*>.*?</table>', r.text, re.DOTALL | re.IGNORECASE)
for t in tables:
    print(f'Table class: {t[:200]}')