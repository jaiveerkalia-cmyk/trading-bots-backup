from config import shared_state, params
from datetime import datetime
import csv
import os

class DailyLogger:
    def __init__(self):
        self.log_dir = "daily_logs"
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)
        self.filename = f"{self.log_dir}/action_log_{datetime.now().strftime('%Y-%m-%d')}.csv"
        
        self.headers = [
            'Time', 'Mode', 
            'Total_Realized_PnL', 'Total_Unrealized_PnL',
            'Active_Call_PnL', 'Active_Put_PnL',
            'Call_LTP', 'Call_Entry', 'Call_Strike',
            'Put_LTP', 'Put_Entry', 'Put_Strike',
            'Index_LTP'
        ]
        self._ensure_header()

    def _ensure_header(self):
        # CHECK: If Saturday (5) or Sunday (6), do nothing
        if datetime.today().weekday() in [5, 6]: return 

        if not os.path.exists(self.filename):
            with open(self.filename, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(self.headers)

    def log_snapshot(self, mode):
        # CHECK: If Saturday (5) or Sunday (6), do nothing
        if datetime.today().weekday() in [5, 6]: return

        now_str = datetime.now().strftime('%H:%M:%S')
        
        real_pnl = round(shared_state['pnl']['realized'], 2)
        unreal_pnl = round(shared_state['pnl']['unrealized'], 2)
        
        c_trade = shared_state['active_trades']['Call']
        p_trade = shared_state['active_trades']['Put']
        
        c_pnl = round(c_trade['pnl'], 2) if c_trade else 0.0
        c_ltp = c_trade['main']['current_price'] if c_trade else 0
        c_ent = c_trade['main']['entry_price'] if c_trade else 0
        c_str = c_trade['main']['strike'] if c_trade else 0
        
        p_pnl = round(p_trade['pnl'], 2) if p_trade else 0.0
        p_ltp = p_trade['main']['current_price'] if p_trade else 0
        p_ent = p_trade['main']['entry_price'] if p_trade else 0
        p_str = p_trade['main']['strike'] if p_trade else 0
        
        idx_name = params['trading_index']
        idx_ltp = 0
        if idx_name in shared_state:
            idx_ltp = shared_state[idx_name]['ltp']

        row = [
            now_str, mode,
            real_pnl, unreal_pnl,
            c_pnl, p_pnl,
            c_ltp, c_ent, c_str,
            p_ltp, p_ent, p_str,
            idx_ltp
        ]
        
        with open(self.filename, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(row)
