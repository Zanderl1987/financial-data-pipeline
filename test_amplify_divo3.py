from curl_cffi import requests
import re

session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
})

# Check the fund page for any download/export links
r = session.get('https://amplifyetfs.com/funds/divo/', impersonate='chrome120', timeout=15)

# Look for all links
links = re.findall(r'href=["\']([^"\']+)["\']', r.text)
for link in links:
    if any(x in link.lower() for x in ['holding', 'download', 'csv', 'xlsx', 'export', 'portfolio']):
        print(f'Link: {link}')

# Check for any JavaScript that loads holdings
scripts = re.findall(r'<script[^>]*src=["\']([^"\']+)["\']', r.text)
for s in scripts:
    if 'holding' in s.lower() or 'firestore' in s.lower() or 'portfolio' in s.lower():
        print(f'Script: {s}')

# Check for any inline JS with data
inline_scripts = re.findall(r'<script[^>]*>(.*?)</script>', r.text, re.DOTALL)
for s in inline_scripts[:20]:
    if 'holding' in s.lower() and len(s) > 100:
        print(f'Inline script: {s[:500]}')
        print('---')