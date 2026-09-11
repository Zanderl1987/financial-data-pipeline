from curl_cffi import requests as cffi_requests
import json

# Search for DIVO
r = cffi_requests.get('https://amplifyetfs.com/wp-json/wp/v2/fund-holding?search=DIVO&per_page=10', impersonate='chrome120', timeout=15)
print(f'DIVO search: {r.status_code}')
if r.status_code == 200:
    data = r.json()
    for item in data:
        print(f'  ID={item["id"]} slug={item["slug"]} date={item["date"]}')