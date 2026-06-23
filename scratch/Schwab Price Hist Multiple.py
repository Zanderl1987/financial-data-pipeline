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

#symbol_list = ['$SPX','NVDA','ABBV','MO','SPY','BA','PLTR','ISRG','GOOG','SOFI','X','VST','VRT','AMGN','ISRG','IBM','AAPL','AMZN','NOC','LMT','RDDT','INTC','OIL','TLT']
df_dji = pd.read_html(r"https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average")
symbol_list = df_dji[2]['Symbol']
print(symbol_list)
# Valid schwab indexes that have price data
#symbol_list = ['$BANK','$SPX','$BTK','$CMR','$COMPX','$CYC','$DJI','$DJIT','$DJTT','$DJU','$DJUT','$DJX','$DOT','$DRG','$DTX','$DUX','$FVX','$HCX','$HKX','$INDS','	$INSR','$IRX','$IUX','$IXCO','$IXF','$IXTC','$JPN','$MID','$MSH','$MXY','$NCMP','$ND','$NDX','$NHB','$NIND','$NNA','$NV','$NYA','$OEX','$OFIN','$OSX','$OTX','$PSE','$RLX','$RUA','$RUI','$RUT','$SGX','$SOX','$SVX','$TNX','$TOP','$TRANX','$TXX','$TYTX','$UTY','$XAL','$XAU','$XBD','$XBD','$XCI','$XII','$XMI','$XNG','$XOI','$XTC']
#symbol_list = ['/ES','/GC','/ZN']

df_list = []
#print(client.quote('AMD').json())
#hist_prices = client.price_history(symbol='NVDA',periodType='day',period=1,frequencyType='minute',frequency=1,startDate='1735708338',endDate='1740978738')
for symbol in symbol_list:

    hist_prices = client.price_history(symbol=symbol,periodType='year',period=1,frequencyType='daily',frequency=1,startDate='52200000',endDate=int(time.time()*1000))
    #print(hist_prices.json())

    #df1 = pd.DataFrame.from_dict(hist_prices)
    #print(df1.head())
    #print(hist_prices)

    #print(json.dumps(hist_prices.json(), indent=4))
    #print(type(hist_prices))
    hist_prices_json = hist_prices.json()

    #with open('hist_prices.json()', 'w') as file:
    #    json.dump(hist_prices_json,file,indent=4)

    #print(hist_prices_json['candles'])

    df = pd.DataFrame(hist_prices_json['candles'])
    df['symbol'] = symbol
    df_list.append(df)

df1 = pd.concat(df_list)

# Format the DataFrame
df1['datetime'] = pd.to_datetime(df1['datetime'],unit='ms')
df1['month'] = df1['datetime'].dt.month
df1['day'] = df1['datetime'].dt.day
df1['year'] = df1['datetime'].dt.year
#df1['close_diff_1'] = np.log(df1['close'] - df1['close'].shift(1))
df1['close_diff_1'] = df1['close'] - df1['close'].shift(1)
df1['intraday_change'] = df1['close'] - df1['open']
df1['intraday_range'] = df1['high'] - df1['low']



print(df1.head())
print(df1.info())

fp1 = r"D:\Data Science\Datasets\Schwab Historical Securities Data\HistData.csv"
#df1.to_csv(r"D:\Data Science\Datasets\Schwab Historical Securities Data\HisData.csv",index=False)
#df1.to_csv(fp1,index=False)

#print('Finished writing to CSV')

#df2 = df1.set_index('symbol',inplace=False)
#print(df2.head())
#print(df1.loc['symbol'][['NVDA','GOOG']])
#print(df1[['symbol','close']])

df1.to_csv(r"D:\Data Science\Datasets\Securities Data Raw\DJI_Hist_Data.csv",index=False)

