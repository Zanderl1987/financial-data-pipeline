import requests
import re
import json

# Try the product data API directly
url = 'https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v2/get-product-data?appType=PRODUCT_PAGE&appSubType=ISHARES&targetSite=us-ishares&locale=en_US&productId=239747&userType=individual'
r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
print(f'Product data API (239747): {r.status_code}')
if r.status_code == 200:
    data = r.json()
    print(json.dumps(data, indent=2)[:2000])

# Try with SGOV ticker
url2 = 'https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v2/get-product-data?appType=PRODUCT_PAGE&appSubType=ISHARES&targetSite=us-ishares&locale=en_US&productId=SGOV&userType=individual'
r2 = requests.get(url2, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
print(f'Product data API (SGOV): {r2.status_code}')
if r2.status_code == 200:
    data2 = r2.json()
    print(json.dumps(data2, indent=2)[:2000])