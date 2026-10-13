from config import shared_state, params, TRADEBOOK_FILE, DAILY_PNL_FILE
from datetime import datetime
import pandas as pd
import os

class CsvManager:
    def __init__(self): self.init_files()
    def init_files(self):
        if not os.path.exists(TRADEBOOK_FILE):
            cols = ['Trade_ID', 'Date', 'Index_Type', 'Expiry', 'Type', 'Direction', 'Qty', 'Main_Strike', 'Hedge_Strike', 'Open_Main_Price', 'Open_Hedge_Price', 'Open_Index_Price', 'Open_Time', 'Close_Main_Price', 'Close_Hedge_Price', 'Close_Index_Price', 'Close_Time', 'Profit', 'Status']
            pd.DataFrame(columns=cols).to_csv(TRADEBOOK_FILE, index=False)
        if not os.path.exists(DAILY_PNL_FILE):
            cols = ['Date', 'Net_Profit', 'Trade_Num', 'Trades_List']
            pd.DataFrame(columns=cols).to_csv(DAILY_PNL_FILE, index=False)
    def log_open(self, trade_id, trade_data):
        try: df = pd.read_csv(TRADEBOOK_FILE)
        except: self.init_files(); df = pd.read_csv(TRADEBOOK_FILE)
        expiry = shared_state['current_expiry'].get(params['trading_index'], 'N/A')
        hedge_strike = trade_data['hedge']['strike'] if trade_data['hedge'] else 0
        hedge_entry  = trade_data['hedge']['entry_price'] if trade_data['hedge'] else 0
        new_row = pd.DataFrame([{
            'Trade_ID': str(trade_id), 'Date': datetime.now().strftime('%Y-%m-%d'), 'Index_Type': params['trading_index'], 'Expiry': str(expiry),
            'Type': trade_data['type'], 'Direction': trade_data.get('direction', 'SELL'), 'Qty': int(trade_data['qty']), 'Main_Strike': float(trade_data['main']['strike']), 'Hedge_Strike': float(hedge_strike),
            'Open_Main_Price': float(trade_data['main']['entry_price']), 'Open_Hedge_Price': float(hedge_entry), 'Open_Index_Price': float(trade_data['index_entry_price']),
            'Open_Time': datetime.now().strftime('%H:%M:%S'), 'Close_Main_Price': 0.0, 'Close_Hedge_Price': 0.0, 'Close_Index_Price': 0.0, 'Close_Time': 'Active', 'Profit': 0.0, 'Status': 'OPEN'
        }])
        df = pd.concat([df, new_row], ignore_index=True)
        df.to_csv(TRADEBOOK_FILE, index=False)
    def update_entry_log(self, trade_id, main_p, hedge_p, idx_p):
        try:
            df = pd.read_csv(TRADEBOOK_FILE)
            idx = df[df['Trade_ID'].astype(str) == str(trade_id)].index
            if not idx.empty:
                i = idx[0]
                df.at[i, 'Open_Main_Price'] = float(main_p); df.at[i, 'Open_Hedge_Price'] = float(hedge_p); df.at[i, 'Open_Index_Price'] = float(idx_p)
                df.to_csv(TRADEBOOK_FILE, index=False)
        except: pass
    def log_close(self, trade_id, close_data, pnl):
        try:
            df = pd.read_csv(TRADEBOOK_FILE)
            idx = df[df['Trade_ID'].astype(str) == str(trade_id)].index
            if not idx.empty:
                i = idx[0]
                df['Close_Main_Price'] = df['Close_Main_Price'].astype(float); df['Close_Hedge_Price'] = df['Close_Hedge_Price'].astype(float); df['Profit'] = df['Profit'].astype(float)
                df.at[i, 'Close_Main_Price'] = float(close_data['main_price']); df.at[i, 'Close_Hedge_Price'] = float(close_data['hedge_price']); df.at[i, 'Close_Index_Price'] = float(close_data['index_price'])
                df.at[i, 'Close_Time'] = datetime.now().strftime('%H:%M:%S'); df.at[i, 'Profit'] = float(pnl); df.at[i, 'Status'] = 'CLOSED'
                df.to_csv(TRADEBOOK_FILE, index=False)
        except: pass
    def save_daily_report(self):
        trades = shared_state['pnl']['trades_history']; pnl_list = [t['pnl'] for t in trades]; net_profit = sum(pnl_list)
        try: df = pd.read_csv(DAILY_PNL_FILE)
        except: self.init_files(); df = pd.read_csv(DAILY_PNL_FILE)
        new_row = pd.DataFrame([{'Date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'Net_Profit': round(net_profit, 2), 'Trade_Num': len(trades), 'Trades_List': str(pnl_list)}])
        df = pd.concat([df, new_row], ignore_index=True)
        df.to_csv(DAILY_PNL_FILE, index=False)
