import pandas as pd
import numpy as np
import schwabdev
import json
import datetime
import time

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

client = schwabdev.Client(app_key=appKey, app_secret=appSecret, callback_url=callback_url)

#client.update_tokens_auto()

symbol = '$SPX'

#print(client.quote('AMD').json())
#hist_prices = client.price_history(symbol='NVDA',periodType='day',period=1,frequencyType='minute',frequency=1,startDate='1735708338',endDate='1740978738')
hist_prices = client.price_history(symbol=symbol,periodType='year',period=1,frequencyType='daily',frequency=1,startDate='52200000',endDate=int(time.time()*1000))
#print(hist_prices.json())

#df1 = pd.DataFrame.from_dict(hist_prices)
#print(df1.head())
#print(hist_prices)

print(json.dumps(hist_prices.json(), indent=4))
print(type(hist_prices))
hist_prices_json = hist_prices.json()

#with open('hist_prices.json()', 'w') as file:
#    json.dump(hist_prices_json,file,indent=4)

#print(hist_prices_json['candles'])

df1 = pd.DataFrame(hist_prices_json['candles'])

# Format the DataFrame
df1['datetime'] = pd.to_datetime(df1['datetime'],unit='ms')
df1['month'] = df1['datetime'].dt.month
df1['day'] = df1['datetime'].dt.day
df1['year'] = df1['datetime'].dt.year
df1['symbol'] = symbol

print(df1.head())
print(df1.info())


