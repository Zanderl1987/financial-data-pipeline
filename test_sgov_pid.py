import requests
import time

# Test PIDs around the known ones for SGOV
# Known: 239747 returned 400, 239748 was MXI (Global Materials)
# Try nearby PIDs

test_pids = [str(i) for i in range(239745, 239760)]

for pid in test_pids:
    url = f'https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document?appType=PRODUCT_PAGE&appSubType=ONE&targetSite=one&locale=en_US&portfolioId={pid}&component=fundDownload&userType=individual'
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
        if r.status_code == 200:
            if 'SGOV' in r.text or '0-3 Month' in r.text or 'Short Treasury' in r.text:
                print(f'PID {pid}: SGOV FOUND!')
                break
            else:
                # Extract fund name
                import re
                match = re.search(r'<ss:Data ss:Type="String">([^<]+)</ss:Data>', r.text)
                if match:
                    print(f'PID {pid}: {match.group(1)[:80]}')
                else:
                    print(f'PID {pid}: 200 (no name found)')
        elif r.status_code != 400:
            print(f'PID {pid}: {r.status_code}')
    except Exception as e:
        print(f'PID {pid}: ERROR - {e}')
    time.sleep(0.5)