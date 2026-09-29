import urllib.request
import ssl
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
for dt in ['2026-09-11', '2026-09-10', '2026-09-09']:
    url = f'https://www.cboe.com/us/futures/market_statistics/settlement/csv?dt={dt}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            content = resp.read().decode('utf-8')[:400].replace('\n', ' | ')
            print(f'{dt}: {resp.status} {content}')
    except Exception as e:
        print(f'{dt}: ERROR {e}')