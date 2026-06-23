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



# schwab.auth.client_from_login_flow(api_key=api_key,
#                                    app_secret=app_secret,
#                                    callback_url=callback_url,
#                                    token_path=token_path,
#                                    asyncio=asyncio,
#                                    enforce_enums=enforce_enums,
#                                    token_write_func=token_write_func,
#                                    callback_timeout=callback_timeout,
#                                    interactive=interactive,
#                                    requested_browser='chrome')

schwab.auth.client_from_manual_flow(api_key,
                                    app_secret,
                                    callback_url,
                                    token_path,
                                    asyncio=False,
                                    token_write_func=None,
                                    enforce_enums=True)