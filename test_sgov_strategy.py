import requests
import re

r = requests.get('https://www.ishares.com/us/strategies/sgov', timeout=15)
print(f'Strategy page: {r.status_code}')
print(f'Final URL: {r.url}')

match = re.search(r'<title[^>]*>([^<]+)</title>', r.text)
if match:
    print(f'Title: {match.group(1)}')

# Look for product link
matches = re.findall(r'href=["\']([^"\']*products[^"\']*)["\']', r.text)
for m in matches[:10]:
    print(f'  Product link: {m}')

# Look for any numeric product ID
for m in re.finditer(r'/products/(\d+)', r.text):
    print(f'  Product ID: {m.group(1)}')