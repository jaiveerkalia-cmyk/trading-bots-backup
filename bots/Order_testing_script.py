from kiteconnect import KiteTicker
from kiteconnect import KiteConnect


with open('/app/config/' + 'auth.txt', 'r') as f:
        api_data = f.read()
kite = KiteConnect(api_key = api_data.split(',')[0])
kite.set_access_token(api_data.split(',')[1])

import time

# 1. Place the order
order_id = kite.place_order(
    variety=kite.VARIETY_REGULAR,          # use kite.VARIETY_AMO if placing after market hours
    exchange=kite.EXCHANGE_NSE,            # SBIN is equity, not NFO
    tradingsymbol="SBIN",
    transaction_type=kite.TRANSACTION_TYPE_BUY,
    quantity=1,                            # SBIN lot size = 1, change to your qty
    product=kite.PRODUCT_MIS,              # MIS = intraday
    order_type=kite.ORDER_TYPE_LIMIT,      # LIMIT instead of MARKET
    price=1010.5,                           # your limit price here
    validity=kite.VALIDITY_DAY
)

# 2. Brief wait (100-200ms) for OMS processing (optional but recommended)
time.sleep(0.2) 

# 3. Fetch status immediately
order_history = kite.order_history(order_id)
current_status = order_history[-1]["status"]

print(f"Order ID: {order_id} | Status: {current_status}")
