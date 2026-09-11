import requests
import time

# Test specific PIDs for SGOV
# From BlackRock's website, SGOV is "iShares 0-3 Month Treasury Bond ETF"
# Let's check the product page directly

# First check the product page for SGOV
r = requests.get('https://www.ishares.com/us/products/239756/ishares-0-3-month-treasury-bond-etf', timeout=15)
print(f'SGOV product page: {r.status_code}')

# The PID in the URL is 239756 but that was AOR... Let me check other known PIDs
# From the fund_holdings_pipeline.py:
# SHV: 239466 (Short Treasury)
# IEI: 239455 (3-7 Year Treasury)
# SGOV should be similar range

# Let me check the iShares website for SGOV
r = requests.get('https://www.ishares.com/us/products/239747/ishares-0-3-month-treasury-bond-etf', timeout=15)
print(f'SGOV 239747 page: {r.status_code}')

# Check the BlackRock varnish API for the product page PID
# The varnish API uses portfolioId which might be different from the product page ID