from curl_cffi import requests as cffi_requests
import json

# Check attachments for DIVO holdings post (ID 996)
r = cffi_requests.get('https://amplifyetfs.com/wp-json/wp/v2/media?parent=996&per_page=20', impersonate='chrome120', timeout=15)
print(f'Attachments for DIVO: {r.status_code}')
if r.status_code == 200:
    data = r.json()
    for item in data:
        print(f'  ID={item["id"]} title={item["title"]["rendered"]} mime={item["mime_type"]} src={item["source_url"]}')