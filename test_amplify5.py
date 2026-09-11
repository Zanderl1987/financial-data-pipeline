from curl_cffi import requests as cffi_requests
import json

# List all fund-holding posts
r = cffi_requests.get('https://amplifyetfs.com/wp-json/wp/v2/fund-holding?per_page=20&orderby=date&order=desc', impersonate='chrome120', timeout=15)
print(f'Fund-holding list: {r.status_code}')
if r.status_code == 200:
    data = r.json()
    for item in data:
        print(f'  ID={item["id"]} slug={item["slug"]} date={item["date"]} title={item["title"]["rendered"]}')