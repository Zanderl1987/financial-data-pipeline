import requests
import xml.etree.ElementTree as ET
import re

# The varnish API might use a different parameter for bond ETFs
# Let me check the SGOV product page for the actual varnish API call

r = requests.get('https://www.ishares.com/us/products/239756/ishares-0-3-month-treasury-bond-etf', timeout=15)

# Look for the actual fund download URL
matches = re.findall(r'fundDownload[^"\']*', r.text)
print(f'fundDownload matches: {matches}')

matches = re.findall(r'component=fundDownload[^"\']*', r.text)
print(f'component=fundDownload: {matches}')

# Look for the varnish API base URL
matches = re.findall(r'varnish-api[^"\']*', r.text)
print(f'varnish-api: {matches}')

# Check the download link on the page
matches = re.findall(r'href=["\']([^"\']*fund-download[^"\']*)["\']', r.text, re.IGNORECASE)
print(f'fund-download href: {matches}')

matches = re.findall(r'href=["\']([^"\']*download[^"\']*SGOV[^"\']*)["\']', r.text, re.IGNORECASE)
print(f'download SGOV href: {matches}')

# Try the varnish API with different appSubType
for app_subtype in ['ONE', 'PRODUCT', 'FUND']:
    url = f'https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document?appType=PRODUCT_PAGE&appSubType={app_subtype}&targetSite=one&locale=en_US&portfolioId=239756&component=fundDownload&userType=individual'
    r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
    if r.status_code == 200:
        if 'SGOV' in r.text or '0-3 Month' in r.text or 'Short Treasury' in r.text:
            print(f'appSubType={app_subtype}: SGOV FOUND!')
            break
        else:
            # Check fund name
            import re
            match = re.search(r'<ss:Data ss:Type="String">iShares[^<]*ETF', r.text)
            if match:
                print(f'appSubType={app_subtype}: {match.group(0)[:100]}')
    else:
        print(f'appSubType={app_subtype}: {r.status_code}')