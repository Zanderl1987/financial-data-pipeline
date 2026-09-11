import requests
import xml.etree.ElementTree as ET
import re

# Test with productId 314116 (from strategy page)
pid = '314116'

# Try the get-fund-document API
url = f'https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document?appType=PRODUCT_PAGE&appSubType=ISHARES&targetSite=us-ishares&locale=en_US&portfolioId={pid}&component=fundDownload&userType=individual'
r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)

print(f'get-fund-document Status: {r.status_code}')

if r.status_code == 200:
    # Fix the XML
    raw = r.content.decode('utf-8', errors='replace')
    fixed = re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;|#)', chr(38) + 'amp;', raw)
    
    root = ET.fromstring(fixed.encode('utf-8'))
    ns = {'ss': 'urn:schemas-microsoft-com:office:spreadsheet'}
    
    for ws in root.findall('{%s}Worksheet' % ns['ss']):
        name = ws.get('{%s}Name' % ns['ss'])
        print(f'Worksheet: {name}')
        if name == 'Holdings':
            for i, row in enumerate(ws.iter('{%s}Row' % ns['ss'])):
                if i < 10:
                    cells = []
                    for cell in row.iter('{%s}Cell' % ns['ss']):
                        data_elem = cell.find('{%s}Data' % ns['ss'])
                        cells.append(data_elem.text if data_elem is not None else None)
                    print(f'  Row {i}: {cells}')
else:
    print(f'Response: {r.text[:500]}')

# Also try the product data API
url2 = f'https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v2/get-product-data?appType=PRODUCT_PAGE&appSubType=ISHARES&targetSite=us-ishares&locale=en_US&productId={pid}&userType=individual'
r2 = requests.get(url2, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
print(f'\nProduct data API Status: {r2.status_code}')
if r2.status_code == 200:
    import json
    data = r2.json()
    print(json.dumps(data, indent=2)[:2000])