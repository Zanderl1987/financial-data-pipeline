import requests
import time

# Test PIDs more carefully - check for SGOV / 0-3 Month Treasury
test_pids = [str(i) for i in range(239750, 239780)]

for pid in test_pids:
    url = f'https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document?appType=PRODUCT_PAGE&appSubType=ONE&targetSite=one&locale=en_US&portfolioId={pid}&component=fundDownload&userType=individual'
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        if r.status_code == 200:
            # Check for SGOV specifically
            content = r.text
            if 'SGOV' in content and ('0-3 Month' in content or 'Short Treasury' in content or 'Treasury Bond' in content):
                print(f'PID {pid}: SGOV CANDIDATE!')
                # Extract fund name
                import re
                # Look for fund name in the XML
                if 'iShares' in content:
                    # Find the fund name row
                    lines = content.split('\n')
                    for line in lines:
                        if 'iShares' in line and ('ETF' in line or 'Trust' in line) and 'Disclaimer' not in line:
                            print(f'  Name: {line.strip()[:200]}')
                            break
                break
            elif 'SGOV' in content:
                print(f'PID {pid}: Contains SGOV but not treasury')
        elif r.status_code != 400:
            print(f'PID {pid}: {r.status_code}')
    except Exception as e:
        print(f'PID {pid}: ERROR - {e}')
    time.sleep(0.3)