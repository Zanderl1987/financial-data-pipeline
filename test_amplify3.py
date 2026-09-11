import requests
from curl_cffi import requests as cffi_requests

# Try with curl_cffi Chrome impersonation
url = 'https://firestore.googleapis.com/v1/projects/amplify-etfs-data-feed/databases/(default)/documents/funds/DIVO/holdings'
r = cffi_requests.get(url, impersonate="chrome120", timeout=15)
print(f'Amplify Firestore (curl_cffi): {r.status_code}')
if r.status_code == 200:
    data = r.json()
    docs = data.get('documents', [])
    print(f'  Docs found: {len(docs)}')

# Try the website with curl_cffi
r2 = cffi_requests.get('https://amplifyetfs.com/funds/divo/', impersonate="chrome120", timeout=15)
print(f'Amplify DIVO page (curl_cffi): {r2.status_code}')

# Check for download links
import re
links = re.findall(r'href="([^"]*holding[^"]*)"', r2.text, re.IGNORECASE)
print('Holding links:', links[:10])

csv_links = re.findall(r'href="([^"]*\.csv[^"]*)"', r2.text, re.IGNORECASE)
print('CSV links:', csv_links[:10])