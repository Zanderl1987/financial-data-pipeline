import requests
import re

# Get SGOV product page to find the correct PID for varnish API
r = requests.get('https://www.ishares.com/us/products/239756/ishares-0-3-month-treasury-bond-etf', timeout=15)
print(f'SGOV product page: {r.status_code}')

# Look for portfolioId or productId in the page
matches = re.findall(r'portfolioId["\']?\s*[:=]\s*["\']?(\d+)["\']?', r.text, re.IGNORECASE)
print(f'portfolioId matches: {matches}')

matches = re.findall(r'productId["\']?\s*[:=]\s*["\']?(\d+)["\']?', r.text, re.IGNORECASE)
print(f'productId matches: {matches}')

# Also check for the varnish API call pattern
matches = re.findall(r'get-fund-document[^"\']*portfolioId=(\d+)', r.text)
print(f'varnish portfolioId: {matches}')

# Check meta tags
matches = re.findall(r'<meta[^>]*name=["\']product["\'][^>]*content=["\']([^"\']*)', r.text)
print(f'meta product: {matches}')

# Look for any numeric ID near SGOV
for m in re.finditer(r'(\d{5,7})', r.text):
    pid = m.group(1)
    if int(pid) > 200000 and int(pid) < 400000:
        context = r.text[max(0,m.start()-50):m.end()+50]
        if 'SGOV' in context or '0-3' in context or 'Treasury' in context:
            print(f'  PID {pid}: {context}')