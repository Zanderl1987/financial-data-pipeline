import requests

# Check Amplify Firestore
url = 'https://firestore.googleapis.com/v1/projects/amplify-etfs-data-feed/databases/(default)/documents/funds/DIVO/holdings'
r = requests.get(url, timeout=15)
print(f'Amplify DIVO: {r.status_code}')
if r.status_code == 200:
    data = r.json()
    docs = data.get('documents', [])
    print(f'  Docs found: {len(docs)}')
    if docs:
        latest = docs[-1]['name'].split('/')[-1]
        print(f'  Latest doc: {latest}')
else:
    print(f'  Response: {r.text[:200]}')