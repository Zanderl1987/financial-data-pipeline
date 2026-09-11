import requests
import re

r = requests.get('https://www.ishares.com/us/products/239756/ishares-0-3-month-treasury-bond-etf', timeout=15)

# Find the actual fund download link
# Look for the "Download" button/link for holdings
matches = re.findall(r'href=["\']([^"\']*get-fund-document[^"\']*)["\']', r.text)
for m in matches:
    print(f'get-fund-document: {m}')

# Also check for the full URL with all parameters
matches = re.findall(r'(https://www\.blackrock\.com/varnish-api/[^"\']*fundDownload[^"\']*)', r.text)
for m in matches:
    print(f'varnish fundDownload: {m}')

# Check for any onclick or data attributes with the download URL
matches = re.findall(r'(?:onclick|data-url|data-href)=["\']([^"\']*get-fund-document[^"\']*)["\']', r.text, re.IGNORECASE)
for m in matches:
    print(f'onclick/data-url: {m}')