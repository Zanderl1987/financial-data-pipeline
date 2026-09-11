from curl_cffi import requests

r = requests.get('https://amplifyetfs.com/wp-content/plugins/amplify-firestore-shortcodes/includes/js/holdings-download.js?ver=1788496996', impersonate='chrome120', timeout=15)
with open('holdings-download.js', 'w', encoding='utf-8') as f:
    f.write(r.text)
print(f'Downloaded {len(r.text)} bytes')
print(r.text[:2000])