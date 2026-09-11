import requests
import re

# Check Amplify website for DIVO holdings
r = requests.get('https://amplifyetfs.com/funds/divo/', timeout=15)
print(f'Amplify DIVO page: {r.status_code}')

# Look for download/holdings links
links = re.findall(r'href="([^"]*holding[^"]*)"', r.text, re.IGNORECASE)
print('Holding links:', links[:20])

csv_links = re.findall(r'href="([^"]*\.csv[^"]*)"', r.text, re.IGNORECASE)
print('CSV links:', csv_links[:20])

# Check for any API calls in page source
api_links = re.findall(r'(https?://[^"\s]*amplify[^"\s]*)', r.text, re.IGNORECASE)
print('Amplify API links:', list(set(api_links))[:20])

# Check for XHR/fetch patterns
xhr = re.findall(r'(fetch\(|axios\.|xmlhttprequest)', r.text, re.IGNORECASE)
print('XHR patterns:', len(xhr))