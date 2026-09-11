import requests

url = 'https://assets.globalxetfs.com/funds/holdings/aiq_full-holdings_20260904.csv'
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://www.globalxetfs.com/',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}
r = requests.get(url, headers=headers, timeout=15)
print(f'Status: {r.status_code}, len: {len(r.content)}')
ct = r.headers.get('Content-Type')
print(f'Content-Type: {ct}')