import requests
import xml.etree.ElementTree as ET
import re

pid = '239748'
url = f'https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document?appType=PRODUCT_PAGE&appSubType=ONE&targetSite=one&locale=en_US&portfolioId={pid}&component=fundDownload&userType=individual'
r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)

# Fix the XML - the & characters need to be escaped to &
raw = r.content.decode('utf-8', errors='replace')
# This is the fix from the pipeline - replace unescaped & with &
fixed = re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;|#)', '&', raw)

# Now parse
root = ET.fromstring(fixed.encode('utf-8'))
ns = {'ss': 'urn:schemas-microsoft-com:office:spreadsheet'}

# Find the worksheet name
for ws in root.findall('{%s}Worksheet' % ns['ss']):
    name = ws.get('{%s}Name' % ns['ss'])
    print(f'Worksheet: {name}')
    if name == 'Holdings':
        # Get first few rows
        for i, row in enumerate(ws.iter('{%s}Row' % ns['ss'])):
            if i < 10:
                cells = []
                for cell in row.iter('{%s}Cell' % ns['ss']):
                    data_elem = cell.find('{%s}Data' % ns['ss'])
                    cells.append(data_elem.text if data_elem is not None else None)
                print(f'  Row {i}: {cells}')