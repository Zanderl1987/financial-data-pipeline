from curl_cffi import requests as cffi_requests
import re

# Check ROBX page for API endpoints
r = cffi_requests.get('https://amplifyetfs.com/robx-holdings/', impersonate='chrome120', timeout=15)

# Look for any API/endpoint patterns
api_patterns = [
    r'(https?://[^"\s]*api[^"\s]*)',
    r'(https?://[^"\s]*wp-json[^"\s]*)',
    r'(https?://[^"\s]*ajax[^"\s]*)',
    r'fetch\(["\']([^"\']+)["\']',
    r'axios\.(get|post)\(["\']([^"\']+)["\']',
    r'\.ajax\([{].*?url["\']:\s*["\']([^"\']+)["\']',
]

for pattern in api_patterns:
    matches = re.findall(pattern, r.text, re.IGNORECASE)
    if matches:
        print(f'Pattern {pattern[:40]}:')
        for m in matches[:5]:
            if isinstance(m, tuple):
                print(f'  {m}')
            else:
                print(f'  {m}')

# Check if there's a specific holdings table endpoint
# The tables have class "amplify-fund-details-table" - might be populated by JS
# Look for data attributes
data_attrs = re.findall(r'data-(\w+)="([^"]*)"', r.text)
for k, v in data_attrs[:20]:
    if 'holding' in k.lower() or 'fund' in k.lower() or 'table' in k.lower():
        print(f'data-{k}="{v[:100]}"')