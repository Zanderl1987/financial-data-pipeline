import requests
import re

r = requests.get('https://www.globalxetfs.com/funds/aiq/', timeout=15)
links = re.findall(r'href=["\']([^"\']*holdings[^"\']*)["\']', r.text, re.IGNORECASE)
print('Holdings links:', links)

csv_links = re.findall(r'href=["\']([^"\']*\.csv[^"\']*)["\']', r.text, re.IGNORECASE)
print('CSV links:', csv_links)