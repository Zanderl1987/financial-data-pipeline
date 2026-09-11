import requests
from curl_cffi import requests as cffi_requests
import re

# Check the DIVO-holdings page
r = cffi_requests.get('https://amplifyetfs.com/DIVO-holdings', impersonate="chrome120", timeout=15)
print(f'DIVO-holdings page: {r.status_code}')

# Look for CSV/download links
links = re.findall(r'href="([^"]*holding[^"]*)"', r.text, re.IGNORECASE)
print('Holding links:', links[:20])

csv_links = re.findall(r'href="([^"]*\.csv[^"]*)"', r.text, re.IGNORECASE)
print('CSV links:', csv_links[:20])

# Look for any data in the page
if 'holdings' in r.text.lower():
    print('Page contains "holdings"')

# Check if there's a table or JSON data embedded
import json
try:
    # Look for JSON data
    json_matches = re.findall(r'(\{.*?\})', r.text)
    for m in json_matches[:10]:
        try:
            data = json.loads(m)
            if isinstance(data, dict) and ('holdings' in str(data).lower() or 'portfolio' in str(data).lower()):
                print(f'Found JSON with holdings: {str(data)[:200]}')
                break
        except:
            pass
except:
    pass