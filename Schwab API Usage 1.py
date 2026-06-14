import pandas as pd
import numpy as np
import polars as pl
import schwab
import schwab.auth
import seaborn as sns
from schwab import auth, client
import json
import requests
import time
import base64

api_key = 'K7hswzbeRdT1RExrrCZ8RHZix7rBy4xl'
app_secret = 'p3wuDNYN1WAcre1M'

callback_url = 'https://127.0.0.1:8182'
token_path = "D:/Data Science/API Keys/Schwab API/token.json"
asyncio = False
enforce_enums = False
token_write_func = None
callback_timeout = 300.0
interactive = True
requested_browser = None

appKey = 'K7hswzbeRdT1RExrrCZ8RHZix7rBy4xl'
appSecret = 'p3wuDNYN1WAcre1M'

authUrl = f'https://api.schwabapi.com/v1/oauth/authorize?client_id={appKey}&redirect_uri={callback_url}'

print(f"Click to authenticate: {authUrl}")

returnedLink = input("paste the redirect URL here: ")

code = f"{returnedLink[returnedLink.index('code=')+5:returnedLink.index('%40')]}@"

headers = {'Authroization': f'Basic {base64.b64encode(bytes(f"{appKey}:{appSecret}", "utf-8")).decode("utf-8")}', 'Content-Type': 'application/x-www-form-urlencoded'}

data = {'grant_type': 'authorization_code', 'code': code, 'redirect_url': 'https://127.0.0.1:8182'}

response = requests.post('https://api.schwabapi.com/v1/oauth/token', headers=headers, data=data)

tD = response.json()

access_token = tD['access_token']

refresh_token = tD['refresh_token']

base_url = "https://api.schwabapi.com/trader/v1"

response = requests.get(f"{base_url}/accounts/accountNumbers", headers={'Authorization': f'Bearer {access_token}'})

print(f"Response ok?: {response.ok}")

print(response.json())
