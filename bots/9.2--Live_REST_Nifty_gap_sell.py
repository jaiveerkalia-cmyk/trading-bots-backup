import logging
from kiteconnect import KiteTicker
# logging.basicConfig(level=logging.DEBUG)
import threading
import time
import datetime
from collections import OrderedDict
# from datetime import time
# import sys  # To find out the script name (in argv[0])
import pandas as pd
from pprint import pprint
from datetime import datetime, timedelta
from kiteconnect import KiteConnect
from blackscholes import *
import talib
import requests
import pyotp
import SmartApi
import pytz
from SmartApi import SmartConnect, SmartWebSocket



def commission(quantity, buy_price, sell_price):
    """
    Calculates total charges for NSE Equity Options based on 
    the April 1, 2026 STT hike and current NSE transaction fees.
    """
    buy_turnover = quantity * buy_price
    sell_turnover = quantity * sell_price
    total_turnover = buy_turnover + sell_turnover

    # 1. Brokerage: Standard Rs. 20 per executed order
    # Note: Zerodha may charge 40/order if your cash collateral is < 50%
    zerodha_brokerage = 20 + 20 

    # 2. STT: 0.15% on SELL side premium (Budget 2026 update)
    stt = 0.0015 * sell_turnover

    # 3. NSE Transaction Charges: 0.03503% on premium turnover
    exchange_txn_charge = 0.0003503 * total_turnover

    # 4. SEBI Charges: 10 per crore (0.000001)
    sebi_charges = 0.000001 * total_turnover

    # 5. GST: 18% on (Brokerage + Exchange Charges + SEBI Charges)
    gst = 0.18 * (zerodha_brokerage + exchange_txn_charge + sebi_charges)

    # 6. Stamp Duty: 0.003% on BUY side premium only
    stamp_duty = 0.00003 * buy_turnover
    
    # 7. NSE IPFT: 0.05 per crore (0.0000005) on premium turnover
    ipft = 0.00000005 * total_turnover

    total_charges = (zerodha_brokerage + stt + exchange_txn_charge + 
                     sebi_charges + gst + stamp_duty + ipft)

    return round(total_charges, 2)

def get_contracts():

    with open('/app/config/auth.txt', 'r') as f:

        api_data = f.read()

    kite = KiteConnect(api_key = api_data.split(',')[0])

    kite.set_access_token(api_data.split(',')[1])

    while True:

        try:

            orderedDictList =  (kite.instruments(exchange='NFO'))

            break

        except Exception as e:

            time.sleep(10)

    option_df = pd.DataFrame(orderedDictList)

    return(option_df)




def old_place_order(kite, sym, qty, side):

    #######PLACE ORDER
    if side == 'BUY': order_side = kite.TRANSACTION_TYPE_BUY
    else: order_side = kite.TRANSACTION_TYPE_SELL
    
    ord_num = 0
    while True:
        try:
            kite.place_order(tradingsymbol= sym,
                                            exchange= 'NFO',
                                            transaction_type= side,
                                            quantity= qty,
                                            order_type= kite.ORDER_TYPE_MARKET,
                                            variety= kite.VARIETY_REGULAR,
                                            product= kite.PRODUCT_MIS)
            break
        except Exception as e:
            print('Entry_sell_order_error',e)
            ord_num += 1
            if ord_num == 3:
                print('order_placement_failed')
                break 
            time.sleep(3)

def place_order(kite, sym, qty, side):
    
    # Format symbol explicitly for NFO LTP fetching
    ltp_sym = f"NFO:{sym}"
    order_side = kite.TRANSACTION_TYPE_BUY if side == 'BUY' else kite.TRANSACTION_TYPE_SELL

    # --- Nested Helper 1: Fetch LTP ---
    def get_ltp():
        ltp_sleep = 0.5
        for attempt in range(10):
            try:
                resp = kite.ltp(ltp_sym)
                ltp = resp[ltp_sym]['last_price']
                if ltp > 0:
                    return ltp
            except Exception as e:
                print(f"LTP fetch error attempt {attempt + 1}: {e}")
            
            time.sleep(ltp_sleep)
            # Increase sleep time by 1s after the 3rd failed attempt
            if attempt >= 2: 
                ltp_sleep += 1.0
        return None

    # --- Nested Helper 2: Calculate Aggressive Price ---
    def get_limit_price(current_ltp):
        if side == 'BUY':
            price = current_ltp * 1.10 if current_ltp > 50 else current_ltp + 5.0
        else:
            price = current_ltp * 0.90 if current_ltp > 50 else current_ltp - 5.0
            
        # Ensure price doesn't drop below tick size (0.05)
        price = max(price, 0.05)
        return round(round(price / 0.05) * 0.05, 2)

    # 1. Fetch Initial LTP
    current_ltp = get_ltp()
    if current_ltp is None:
        print('order_placement_failed: Could not fetch initial LTP')
        return

    # 2. Calculate Initial Limit Price
    limit_price = get_limit_price(current_ltp)

    # 3. Place Initial Limit Order (10 retries, dynamic sleep)
    order_id = None
    place_retries = 0
    place_sleep = 1.0
    
    while place_retries < 10:
        try:
            order_id = kite.place_order(tradingsymbol=sym,
                                        exchange='NFO',
                                        transaction_type=order_side,
                                        quantity=qty,
                                        order_type=kite.ORDER_TYPE_LIMIT,
                                        price=limit_price,
                                        variety=kite.VARIETY_REGULAR,
                                        product=kite.PRODUCT_MIS)
            break
        except Exception as e:
            place_retries += 1
            print(f'Entry_order_error attempt {place_retries}:', e)
            if place_retries == 10:
                print('order_placement_failed')
                return 
            
            time.sleep(place_sleep)
            # Increase sleep time by 1s after 3 retries
            if place_retries >= 3:
                place_sleep += 1.0

    # Exit if order couldn't be placed
    if not order_id:
        return

    # 4. Check Fill Status and Chase Price if Partial/Unfilled
    max_modifications = 10 
    mod_count = 0
    mod_sleep = 1.0
    
    while mod_count < max_modifications:
        time.sleep(mod_sleep) # Dynamic wait before checking status
        
        try:
            # Fetch the history for this specific order ID
            order_history = kite.order_history(order_id)
            latest_state = order_history[-1] 
            status = latest_state['status']
            pending_qty = latest_state.get('pending_quantity', 0)
            
            if status == 'COMPLETE':
                break
            
            elif status in ['REJECTED', 'CANCELLED']:
                print(f"Order {status}. Reason: {latest_state.get('status_message', 'Unknown')}")
                break
                
            elif pending_qty > 0: # Order is OPEN or UPDATE (Partially filled)
                print(f"Partial/No Fill. Pending Qty: {pending_qty}. Fetching latest LTP to modify...")
                
                new_ltp = get_ltp()
                
                if new_ltp:
                    new_limit_price = get_limit_price(new_ltp)
                    
                    # Modify the order explicitly as LIMIT
                    kite.modify_order(variety=kite.VARIETY_REGULAR,
                                      order_id=order_id,
                                      order_type=kite.ORDER_TYPE_LIMIT,
                                      price=new_limit_price)
                else:
                    print("Could not fetch new LTP for modification. Retrying status check...")
                    
        except Exception as e:
            print('Order_modification_error:', e)
            
        # Increment counters and adjust sleep uniformly for the next loop iteration
        mod_count += 1
        if mod_count >= 3:
            mod_sleep += 1.0
            
    if mod_count >= max_modifications:
         print(f"Warning: Max modifications ({max_modifications}) reached. Order may still be pending.")

scrip = 'NIFTY 50'
ind = 'NIFTY'
min_delta = 0.15
sObject = slice(16)
day_off = 0
strike_gap = 10
min_buy_price = 15
entry_strike_gap = 0
strike_difference = 50
strike_hedge_gap = 10
lots_num = 10
lot_size = 65
lot_reducer = 1

stop_percent = 0.5
entry_price_gap = 5
live_mode = 1

gd_path = '/app/data/'

call_token, put_token,hedge_call_token,hedge_put_token,mid_loop,prev_call_token, prev_put_token,put_stop_price,call_stop_price = 0,0,0,0,0,0,0,0,0
entry_call_price,entry_put_price,entry_put,entry_call,hedge_put,hedge_call,hedge_put_price,hedge_call_price,profit,final_close = 0,0,0,0,0,0,0,0,0,0
current_call_buy,current_put_buy,total_premium,opening_underlying_price, call_open, put_open = 0,0,0,0,1,1
sell_put_flag, sell_call_flag, buy_put_flag, buy_call_flag = 0,0,0,0
tick_list = []
token_list = []

def get_best_atm_strike(kite, atm_strike, zerodha_instruments_list):
    """
    Finds the strike with the lowest premium difference (Straddle Delta ~ 0).
    Optimized for speed (batch fetching) and reliability (retries).
    """
    start = time.time()
    
    # ---------------------------------------------------------
    # 1. PREPARE SYMBOLS LIST (Batch Preparation)
    # ---------------------------------------------------------
    symbols_to_fetch = []
    strike_symbol_map = {} 

    # We check strikes from -150 to +200 relative to input ATM
    for x1 in range(-3, 5):
        test_strike = int(atm_strike) + (x1 * 50)
        
        try:
            # We filter the DataFrame to find the specific CE and PE for this strike.
            # NOTE: Ensure 'zerodha_instruments_list' is already filtered for the 
            # correct expiry/symbol to ensure this runs fast.
            
            # Find PE Symbol
            sym_put = next(item for item in zerodha_instruments_list[
                zerodha_instruments_list['strike'] == int(test_strike)
            ]['tradingsymbol'].values if 'PE' in item)
            
            # Find CE Symbol
            sym_call = next(item for item in zerodha_instruments_list[
                zerodha_instruments_list['strike'] == int(test_strike)
            ]['tradingsymbol'].values if 'CE' in item)
            
            # Append to our batch list
            symbols_to_fetch.append('NFO:' + sym_put)
            symbols_to_fetch.append('NFO:' + sym_call)
            
            # Map strike to symbols for the calculation phase later
            strike_symbol_map[test_strike] = {'PE': 'NFO:' + sym_put, 'CE': 'NFO:' + sym_call}
            
        except StopIteration:
            # Strike not found in the instrument list (e.g. out of range)
            continue
        except Exception as e:
            print(f"Skipping strike {test_strike} due to data error: {e}")
            continue

    # Safety: If no symbols were found, return original ATM to avoid API errors
    if not symbols_to_fetch:
        print("CRITICAL: No matching symbols found in instrument list. Returning input ATM.")
        return atm_strike

    # ---------------------------------------------------------
    # 2. FETCH ALL DATA IN ONE API CALL (With 20 Retries)
    # ---------------------------------------------------------
    all_ltp = {}
    success = False

    for attempt in range(20):
        try:
            all_ltp = kite.ltp(symbols_to_fetch)
            success = True
            break # Success! Exit the retry loop
        except Exception as e:
            print(f"API Attempt {attempt+1}/20 failed: {e}")
            time.sleep(1) # Wait 1 second before retrying
    
    if not success:
        print("CRITICAL: Could not fetch quotes after 20 attempts. Returning input ATM.")
        return atm_strike

    # ---------------------------------------------------------
    # 3. CALCULATE BEST ATM (The Comparison Logic)
    # ---------------------------------------------------------
    org_price_difference = 9999999.0 
    new_atm_strike = atm_strike

    print(f"\n{'Strike':<10} | {'Call LTP':<10} | {'Put LTP':<10} | {'Diff':<10}")
    print("-" * 50)

    for x1 in range(-3, 5):
        test_strike = int(atm_strike) + (x1 * 50)
        
        # Only process if we successfully identified symbols for this strike
        if test_strike in strike_symbol_map:
            sym_put = strike_symbol_map[test_strike]['PE']
            sym_call = strike_symbol_map[test_strike]['CE']
            
            # Only process if API returned data for both
            if sym_put in all_ltp and sym_call in all_ltp:
                put_price = all_ltp[sym_put]['last_price']
                call_price = all_ltp[sym_call]['last_price']
                
                price_difference = abs(put_price - call_price)
                
                print(f"{test_strike:<10} | {call_price:<10} | {put_price:<10} | {price_difference:<10.2f}")
                
                # Update if we found a smaller difference
                if price_difference < org_price_difference:
                    org_price_difference = price_difference
                    new_atm_strike = test_strike

    print('It took {0:0.4f} seconds'.format(time.time() - start))
    print(f'Selected Best ATM: {new_atm_strike} (Diff: {org_price_difference:.2f})\n')
    
    return new_atm_strike

    
def sell_fn(kite, zerodha_instruments_list, expiry):
    global total_premium,put_stop_price,call_stop_price,opening_underlying_price, call_open, put_open,option_df,call_token,token,put_token,hedge_call_token,hedge_put_token,current_call_buy,current_put_buy,entry_call_price,entry_put_price,entry_put,entry_call,hedge_put,hedge_call,hedge_put_price,hedge_call_price
    sell_put_flag, sell_call_flag, buy_put_flag, buy_call_flag = 0,0,0,0
    current_profit, final_profit = 0,0
    sell_put_profit, sell_call_profit, buy_put_profit, buy_call_profit = 0,0,0,0
    initial_entry_put_price, initial_entry_call_price, initial_buy_put_price, initial_buy_call_price, current_entry_put_price, current_entry_call_price, current_buy_put_price, current_buy_call_price, entry_put_price, entry_call_price, buy_put_price, buy_call_price = 0,0,0,0,0,0,0,0,0,0,0,0
    exit_sell_call_price, exit_sell_put_price, exit_buy_call_price, exit_buy_put_price = 0,0,0,0
    buy_entry_call, buy_entry_put = 0,0
    intraday_positions = pd.read_csv(gd_path + 'nifty_buy_sell_results/Intraday_options_tradebook.csv')
    final_position_df = pd.read_csv(gd_path + 'nifty_buy_sell_results/Final_daily_pnl.csv')
    strike_balance_mode = 1
    ##### EXPIRY DAY CHANGES
    if datetime.now().date() == pd.to_datetime(expiry).date():
        return()
    elif datetime.now().strftime('%w') == '1':
        lots = math.floor(0.4*lots_num)
        qty = lots*lot_size
        entry_price_gap = 5
        time.sleep(2703 - time.localtime().tm_sec)
    elif datetime.now().strftime('%w') == '3':
        lots = math.floor(0.1*lots_num)
        qty = lots*lot_size
        entry_price_gap = 8
        time.sleep(5403 - time.localtime().tm_sec)
    elif datetime.now().strftime('%w') == '4':
        return()
    elif datetime.now().strftime('%w') == '5':
        lots = math.floor(0.8*lots_num)
        qty = lots*lot_size
        entry_price_gap = 8
        time.sleep(3600 - time.localtime().tm_sec)
    else:
        lots = lots_num
        qty = lots*lot_size
        time.sleep(64 - time.localtime().tm_sec )

    profit_target = 45000 * lots
    loss_target = 3000 * lots    
    print(datetime.now(), 'Qty:', qty)
    
    ###GET NIFTY CURRENT PRICE
    while True:
        try:
            current_price = kite.ltp('NSE:'+ scrip)['NSE:'+ scrip]['last_price']
            break
        except Exception as e:
            time.sleep(1.5)
    pprint(current_price)
    atm_strike = int(round(current_price / strike_difference) * strike_difference)
    print('ATM Strike', atm_strike, 'Underlying:', current_price)

    ###### ADVANCED ATM FINDER #####
    atm_strike = get_best_atm_strike(kite, atm_strike, zerodha_instruments_list)
    ################################

    print('ATM Strike', atm_strike, 'Underlying:', current_price)        
    entry_put = atm_strike 
    entry_call = atm_strike 

    ####### STRIKE BALANCE CHECK ########
    
    if strike_balance_mode == 1:
        # #####GET INITIAL ENTRY PUT PRICE
        while True:
            sym_put = next(item for item in zerodha_instruments_list[zerodha_instruments_list['strike'] == int(entry_put)]['tradingsymbol'].values if 'PE' in item)
            for _ in range(10):
                try:
                    a1 = kite.ltp('NFO:'+sym_put)
                    put_price = (a1['NFO:'+sym_put]['last_price'])
                    break
                except Exception as e:
                    print(e)
                    time.sleep(1)
            break
        
        #####GET INITIAL ENTRY CALL PRICE    
        while True:
            sym_call = next(item for item in zerodha_instruments_list[zerodha_instruments_list['strike'] == int(entry_call)]['tradingsymbol'].values if 'CE' in item)
            for _ in range(10):
                try:
                    a1 = kite.ltp('NFO:'+sym_call)
                    call_price = (a1['NFO:'+sym_call]['last_price'])
                    break
                except Exception as e:
                    time.sleep(1)
            break
        print('IP:',put_price, 'IC:',call_price)
        if put_price > 1.25*call_price: entry_call = entry_call - strike_difference
        
        if 1.25*put_price < call_price: entry_put = entry_put + strike_difference
    
    
    ####### GET ENTRY SELL PUTS AND BUY PUTS

    ##### GET SELL STRIKES
    hedge_put = entry_put-strike_hedge_gap*strike_difference
    hedge_call = entry_call+strike_hedge_gap*strike_difference

    #####GET INITIAL ENTRY PUT PRICE
    while True:
        sym_put = next(item for item in zerodha_instruments_list[zerodha_instruments_list['strike'] == int(entry_put)]['tradingsymbol'].values if 'PE' in item)
        sym_hedge_put = next(item for item in zerodha_instruments_list[zerodha_instruments_list['strike'] == int(hedge_put)]['tradingsymbol'].values if 'PE' in item)
        print(sym_put, sym_hedge_put)
        for _ in range(10):
            try:
                a1 = kite.ltp('NFO:'+sym_put)
                initial_entry_put_price = (a1['NFO:'+sym_put]['last_price'])
                break
            except Exception as e:
                print(e)
                time.sleep(1)
        break

    #####GET INITIAL ENTRY CALL PRICE    
    while True:
        sym_call = next(item for item in zerodha_instruments_list[zerodha_instruments_list['strike'] == int(entry_call)]['tradingsymbol'].values if 'CE' in item)
        sym_hedge_call = next(item for item in zerodha_instruments_list[zerodha_instruments_list['strike'] == int(hedge_call)]['tradingsymbol'].values if 'CE' in item)
        print(sym_call, sym_hedge_call)
        
        for _ in range(10):
            try:
                a1 = kite.ltp('NFO:'+sym_call)
                initial_entry_call_price = (a1['NFO:'+sym_call]['last_price'])
                break
            except Exception as e:
                time.sleep(1)
        break


    threshold_call_price = initial_entry_call_price - entry_price_gap
    threshold_put_price = initial_entry_put_price - entry_price_gap

    stop_call_price = 1.5*threshold_call_price
    stop_put_price =  1.5*threshold_put_price

    print(datetime.now() ,'Entry Put:', entry_put, hedge_put, 'Entry Call:', entry_call, hedge_call, 'Initial Entry put price:', initial_entry_put_price, 'Initial Entry call price:', initial_entry_call_price)
    print('Threshold Call Price:', threshold_call_price, 'Threshold Put Price:', threshold_put_price, 'Stop Call Price:', stop_call_price, 'Stop Put Price:', stop_put_price)
    
    ######### START CHECKING THE ENTRY CONDITIONS
    counter = 0
    while True:

        counter+=1

        now = datetime.now()
        time.sleep(61 - now.second)
        
        ####### GET CURRENT PRICES FOR ALL THE SYMBOLS
        
        for _ in range(10):
            try:
                a1 = kite.quote(['NFO:'+sym_put, 'NFO:'+sym_call])
                current_entry_put_price = (a1['NFO:'+sym_put]['last_price'])
                current_entry_call_price = (a1['NFO:'+sym_call]['last_price'])
                break
            except Exception as e:
                time.sleep(1)


        ############################### CHECK ENTRIES #############################
        
        ###### CHECK SELL PUT ENTRY
        if current_entry_put_price < threshold_put_price and sell_put_flag == 0:

            if live_mode == 1:
                ####PLACE BUY HEDGE ORDER
                place_order(kite, sym_hedge_put, qty, 'BUY')
                ####PLACE SELL ACTUAL ORDER
                place_order(kite, sym_put, qty, 'SELL')
            
            #####GET ENTRY PUT PRICE
            for _ in range(10):
                try:
                    a1 = kite.ltp('NFO:'+sym_put)
                    entry_put_price = (a1['NFO:'+sym_put]['last_price'])
                    break
                except Exception as e:
                    print(e)
            #####GET ENTRY HEDGE PUT PRICE
            for _ in range(10):
                try:
                    a1 = kite.ltp('NFO:'+sym_hedge_put)
                    entry_hedge_put_price = (a1['NFO:'+sym_hedge_put]['last_price'])
                    break
                except Exception as e:
                    print(e)
                    time.sleep(1)
            print(datetime.now(), 'Put_Sold', threshold_put_price, entry_put_price, entry_hedge_put_price)
            intraday_positions.loc[intraday_positions.shape[0]] = [str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0], entry_put, 'PE', initial_entry_put_price, entry_put_price, 0, 0 , final_profit, 'Open - Put_Sold']
            intraday_positions.to_csv(gd_path+'nifty_buy_sell_results/Intraday_options_tradebook.csv', index=False)
            sell_put_flag = 1

        ###### CHECK SELL CALL ENTRY
        if current_entry_call_price < threshold_call_price and sell_call_flag == 0:
            
            if live_mode == 1:
                ####PLACE BUY HEDGE ORDER
                place_order(kite, sym_hedge_call, qty, 'BUY')
                ####PLACE SELL ACTUAL ORDER
                place_order(kite, sym_call, qty, 'SELL')
            
            #####GET ENTRY CALL PRICE
            for _ in range(10):
                try:
                    a1 = kite.ltp('NFO:'+sym_call)
                    entry_call_price = (a1['NFO:'+sym_call]['last_price'])
                    break
                except Exception as e:
                    print(e)
            #####GET ENTRY HEDGE CALL PRICE
            for _ in range(10):
                try:
                    a1 = kite.ltp('NFO:'+sym_hedge_call)
                    entry_hedge_call_price = (a1['NFO:'+sym_hedge_call]['last_price'])
                    break
                except Exception as e:
                    print(e)
                    time.sleep(1)
            print(datetime.now(), 'Call_Sold', threshold_call_price, entry_call_price, entry_hedge_call_price)
            intraday_positions.loc[intraday_positions.shape[0]] = [str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0], entry_call, 'CE', initial_entry_call_price, entry_call_price, 0, 0 , final_profit, 'Open - Call_Sold']
            intraday_positions.to_csv(gd_path+'nifty_buy_sell_results/Intraday_options_tradebook.csv', index=False)
            sell_call_flag = 1


        ###################### CHECK PROFIT AND EXITS ################################

        ######### CALCULATE CURRENT PROFIT
        if sell_put_flag == 1: sell_put_profit = qty*(entry_put_price - current_entry_put_price) - commission(qty, current_entry_put_price, entry_put_price)
        if sell_call_flag == 1: sell_call_profit = qty*(entry_call_price - current_entry_call_price) - commission(qty, current_entry_call_price, entry_call_price)

        current_profit = sell_put_profit+sell_call_profit
        print(datetime.now(), 'Current_profit:', round(current_profit,2), 'PE:', current_entry_put_price, 'CE:',current_entry_call_price, sell_put_profit,sell_call_profit)

        ######## EXIT IF STOP LOSS / TARGET IS HIT
        if sell_put_flag == 1 and current_entry_put_price > stop_put_price:
            exit_sell_put_price =  current_entry_put_price
            
            if live_mode == 1:
                #####CLOSE THE SELL ORDER
                place_order(kite, sym_put, qty, 'BUY')
                ####CLOSE THE HEDGE ORDER
                place_order(kite, sym_hedge_put, qty, 'SELL')
            
            #####GET EXIT HEDGE PUT PRICE
            for _ in range(10):
                try:
                    a1 = kite.ltp('NFO:'+sym_hedge_put)
                    exit_hedge_put_price = (a1['NFO:'+sym_hedge_put]['last_price'])
                    break
                except Exception as e:
                    print(e)
            final_profit = final_profit + qty*(exit_hedge_put_price-entry_hedge_put_price) - commission(qty, entry_hedge_put_price, exit_hedge_put_price)
##            final_profit = final_profit + sell_put_profit
            intraday_positions.loc[intraday_positions.shape[0]] = [str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0], entry_put, 'PE', initial_entry_put_price, entry_put_price, current_entry_put_price, sell_put_profit , final_profit, 'Close - Sell_Put_Stopped']
            intraday_positions.to_csv(gd_path+'nifty_buy_sell_results/Intraday_options_tradebook.csv', index=False)

            print(datetime.now(), 'Sell_Put_Stopped', 'Put_Profit:', sell_put_profit, 'Hedge_Profit:', final_profit, 'Stop_Price:', stop_put_price, 'Actual_Stop_Price:', exit_sell_put_price, exit_hedge_put_price)
            sell_put_flag = 2
##            profit_target = 0
            continue
        
        if sell_call_flag == 1 and current_entry_call_price > stop_call_price:
            exit_sell_call_price = current_entry_call_price
            
            if live_mode == 1:
                #####CLOSE THE SELL ORDER
                place_order(kite, sym_call, qty, 'BUY')
                ####CLOSE THE HEDGE ORDER
                place_order(kite, sym_hedge_call, qty, 'SELL')
            
            #####GET EXIT HEDGE CALL PRICE
            for _ in range(10):
                try:
                    a1 = kite.ltp('NFO:'+sym_hedge_call)
                    exit_hedge_call_price = (a1['NFO:'+sym_hedge_call]['last_price'])
                    break
                except Exception as e:
                    print(e)
            final_profit = final_profit + qty*(exit_hedge_call_price-entry_hedge_call_price) - commission(qty, entry_hedge_call_price, exit_hedge_call_price)
##            final_profit = final_profit + sell_call_profit
            intraday_positions.loc[intraday_positions.shape[0]] = [str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0], entry_call, 'CE', initial_entry_call_price, entry_call_price, current_entry_call_price, sell_call_profit , final_profit, 'Close - Sell_Call_Stopped']
            intraday_positions.to_csv(gd_path+'nifty_buy_sell_results/Intraday_options_tradebook.csv', index=False)

            print(datetime.now(), 'Sell_Call_Stopped', 'Call_Profit:', sell_call_profit, 'Hedge_Profit:', final_profit, 'Stop_Price:', stop_call_price, 'Actual_Stop_Price:', exit_sell_call_price, exit_hedge_call_price)
            sell_call_flag = 2
##            profit_target = 0
            continue


        ######### EXIT IF OVERALL PROFIT OR LOSS HIT OR AT THE END OF DAY
        if (abs(current_profit) > 0 and (current_profit > profit_target or current_profit < -loss_target)) or (datetime.now().hour == 14 and datetime.now().minute >= 45):
            final_profit = final_profit+current_profit
            if sell_put_flag == 1: 
                exit_sell_put_price =  current_entry_put_price

                if live_mode == 1:
                    #####CLOSE THE SELL ORDER
                    place_order(kite, sym_put, qty, 'BUY')
                    ####CLOSE THE HEDGE ORDER
                    place_order(kite, sym_hedge_put, qty, 'SELL')
                    
                #####GET EXIT HEDGE PUT PRICE
                for _ in range(10):
                    try:
                        a1 = kite.ltp('NFO:'+sym_hedge_put)
                        exit_hedge_put_price = (a1['NFO:'+sym_hedge_put]['last_price'])
                        break
                    except Exception as e:
                        print(e)
                        time.sleep(1)
                final_profit = final_profit + qty*(exit_hedge_put_price-entry_hedge_put_price) - commission(qty, entry_hedge_put_price, exit_hedge_put_price)
                print(datetime.now(), 'Sell_Put_closed', sell_put_profit, exit_sell_put_price, exit_hedge_put_price)
                intraday_positions.loc[intraday_positions.shape[0]] = [str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0], entry_put, 'PE', initial_entry_put_price, entry_put_price, current_entry_put_price, sell_put_profit , final_profit, 'Close - Sell_Put_Closed']
                intraday_positions.to_csv(gd_path+'nifty_buy_sell_results/Intraday_options_tradebook.csv', index=False)
                
                
            if sell_call_flag == 1: 
                exit_sell_call_price = current_entry_call_price
                
                if live_mode == 1:
                    #####CLOSE THE SELL ORDER
                    place_order(kite, sym_call, qty, 'BUY')
                    ####CLOSE THE HEDGE ORDER
                    place_order(kite, sym_hedge_call, qty, 'SELL')
                
                #####GET EXIT HEDGE CALL PRICE
                for _ in range(10):
                    try:
                        a1 = kite.ltp('NFO:'+sym_hedge_call)
                        exit_hedge_call_price = (a1['NFO:'+sym_hedge_call]['last_price'])
                        break
                    except Exception as e:
                        print(e)
                        time.sleep(1)
                final_profit = final_profit + qty*(exit_hedge_call_price-entry_hedge_call_price) - commission(qty, entry_hedge_call_price, exit_hedge_call_price)
                print(datetime.now(), 'Sell_Call_closed', sell_call_profit, exit_sell_call_price, exit_hedge_call_price)
                intraday_positions.loc[intraday_positions.shape[0]] = [str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0], entry_call, 'CE', initial_entry_call_price, entry_call_price, current_entry_call_price, sell_call_profit , final_profit, 'Close - Sell_Call_Closed']
                intraday_positions.to_csv(gd_path+'nifty_buy_sell_results/Intraday_options_tradebook.csv', index=False)
                
                
            print('All_positions_closed', 'Final_profit:', final_profit)
            print('xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx')
            final_position_df.loc[final_position_df.shape[0]] = [str(datetime.now(pytz.timezone('Asia/Kolkata'))).split('.')[0], entry_call, entry_put, buy_entry_call, buy_entry_put, entry_call_price, entry_put_price, buy_put_price, buy_call_price, exit_sell_call_price, exit_sell_put_price, exit_buy_call_price, exit_buy_put_price,  final_profit ]
            final_position_df.to_csv(gd_path +'nifty_buy_sell_results/Final_daily_pnl.csv', index=False)
            
            return()


print('started')

while True:
    now_min = time.localtime().tm_min
    s = (14 - (now_min % 15))*60
    now_sec = time.localtime().tm_sec
    time.sleep(60 + s - now_sec )  # `now` is between 0 and 59, so we always sleep
    now = (datetime.now())
    current_day = now.strftime('%w')

    if current_day != '0' and current_day != '6':
        
        if '07:00' in str(now):print("Active")
        
        if  '09:15' in str(now):
            print(now)
            with open('/app/config/'+'auth.txt', 'r') as f:
                api_data = f.read()
            kite = KiteConnect(api_key = api_data.split(',')[0])
            kite.set_access_token(api_data.split(',')[1])
            
            ###########PICK UP THE ZERODHA INSTRUMENT LIST#########
            zerodha_instruments_list = pd.read_csv('instrument_tokens.csv')
            zerodha_instruments_list = zerodha_instruments_list[(zerodha_instruments_list['name'] == ind) & (zerodha_instruments_list['segment'] == 'NFO-OPT')].reset_index(drop=True)
            zerodha_instruments_list = zerodha_instruments_list[zerodha_instruments_list['expiry'] == zerodha_instruments_list['expiry'].iloc[0]]
            expiry = zerodha_instruments_list['expiry'].iloc[0]
            sell_fn(kite, zerodha_instruments_list,expiry)
