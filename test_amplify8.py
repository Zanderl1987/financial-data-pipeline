from curl_cffi import requests as cffi_requests
import json

# Check ROBX (recent) - ID 4470
r = cffi_requests.get('https://amplifyetfs.com/wp-json/wp/v2/fund-holding/4470', impersonate='chrome120', timeout=15)
print(f'ROBX post: {r.status_code}')
if r.status_code == 200:
    data = r.json()
    print(f'  Title: {data["title"]["rendered"]}')
    print(f'  Content: {data["content"]["rendered"][:500]}')
    print(f'  ACF: {data.get("acf", {})}')

# Check its attachments
r2 = cffi_requests.get('https://amplifyetfs.com/wp-json/wp/v2/media?parent=4470&per_page=20', impersonate='chrome120', timeout=15)
print(f'ROBX attachments: {r2.status_code}')
if r2.status_code == 200:
    data2 = r2.json()
    for item in data2:
        print(f'  ID={item["id"]} title={item["title"]["rendered"]} mime={item["mime_type"]} src={item["source_url"]}')