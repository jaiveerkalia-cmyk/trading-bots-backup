import sys
import unittest
from unittest.mock import MagicMock, patch, ANY
from datetime import datetime as real_datetime, timedelta
import statistics

# --- 1. MOCK DEPENDENCIES ---
sys.modules['nicegui'] = MagicMock()
sys.modules['kiteconnect'] = MagicMock()

import config
import auto_run

# --- 2. FAKE DATETIME ENGINE ---
class FakeDatetime(real_datetime):
    _current_time = real_datetime(2023, 10, 25, 9, 0, 0) 

    @classmethod
    def now(cls, tz=None):
        return cls._current_time

    @classmethod
    def today(cls):
        return cls._current_time

    @classmethod
    def set_time(cls, dt):
        cls._current_time = dt

# --- 3. TEST INFRASTRUCTURE ---
class MockLogicEngine:
    def log_action(self, msg, details=""): pass 
    def close_position(self, side, reason): 
        config.shared_state['active_trades'][side] = None
    def close_all_positions(self, reason, save_pnl=True): 
        config.shared_state['active_trades']['Call'] = None
        config.shared_state['active_trades']['Put'] = None

class MockInstrumentManager:
    def __init__(self): 
        self.kite = MagicMock()
        self.instruments = [
            {'name': 'NIFTY', 'expiry': '2023-10-26', 'strike': 19900, 'instrument_type': 'PE', 'instrument_token': 12345},
            {'name': 'NIFTY', 'expiry': '2023-10-26', 'strike': 20000, 'instrument_type': 'CE', 'instrument_token': 67890}
        ]
    
    def get_current_expiry(self, index): return '2023-10-26'
    
    def set_candles(self, candles_list): self.kite.historical_data.return_value = candles_list
    def simulate_api_error(self): self.kite.historical_data.side_effect = Exception("API Fail")
    def resolve_api_error(self, candles_list):
        self.kite.historical_data.side_effect = None
        self.kite.historical_data.return_value = candles_list

class MasterTestSuite(unittest.TestCase):
    
    def setUp(self):
        # 1. RESET CONFIG
        config.params['lots'] = 1
        config.params['trading_index'] = 'NIFTY'
        config.params['short_trigger_active'] = False
        config.params['long_trigger_active'] = False
        config.params['live_trading'] = 'Off'
        config.params['call_target_val'] = 0
        config.params['call_stop_val'] = 0
        
        config.shared_state['active_trades'] = {'Call': None, 'Put': None}
        config.shared_state['pnl'] = {'realized': 0.0, 'unrealized': 0.0}
        config.shared_state['chart_data'] = {'times': [], 'pnl': [], 'markers': []}
        config.shared_state['activity_log'] = []
        config.shared_state['NIFTY']['ltp'] = 20050
        
        # 2. AUTO-MOCK UI REFS
        all_ui_keys = [
            'pnl_realized', 'pnl_unrealized', 'pnl_chart', 'monitor_status', 'banner_card', 
            'last_action', 'calc_qty', 'activity_log_container',
            'call_status', 'call_info', 'call_trigger', 'call_pnl',
            'call_main_strike', 'call_main_open', 'call_main_curr',
            'call_hedge_strike', 'call_hedge_open', 'call_hedge_curr',
            'call_idx_open', 'call_idx_curr',
            'put_status', 'put_info', 'put_trigger', 'put_pnl',
            'put_main_strike', 'put_main_open', 'put_main_curr',
            'put_hedge_strike', 'put_hedge_open', 'put_hedge_curr',
            'put_idx_open', 'put_idx_curr'
        ]
        for key in all_ui_keys: config.ui_refs[key] = MagicMock()
        config.ui_refs['pnl_chart'].options = {'xAxis': {'data': []}, 'series': [{'data': [], 'markPoint': {'data': []}}]}

        # 3. INIT COMPONENTS
        self.mock_logic = MockLogicEngine()
        self.mock_inst = MockInstrumentManager()
        self.controller = auto_run.AutoController(self.mock_logic, self.mock_inst)
        self.controller.mode = 'ON'
        
        # 4. DEFAULT TIME & DATA
        self.start_time = real_datetime(2023, 10, 25, 11, 14, 0)
        FakeDatetime.set_time(self.start_time)
        
        self.base_history = []
        for i in range(15): 
            c = {'date': FakeDatetime(2023, 10, 25, 9, 15) + timedelta(hours=i),
                 'open': 20000, 'high': 20100, 'low': 20000, 'close': 20050}
            self.base_history.append(c)
        self.mock_inst.set_candles(self.base_history)

    def tick(self, seconds=0, ltp=None):
        if ltp: config.shared_state['NIFTY']['ltp'] = ltp
        FakeDatetime._current_time += timedelta(seconds=seconds)
        with patch('auto_run.datetime', FakeDatetime):
            self.controller.run_loop()

    # ==========================================
    #      NEW FEATURE TESTS (13-18)
    # ==========================================

    def test_feature_13_ui_sync(self):
        print("\n✨ FEAT_13: UI Parameter Sync")
        # Setup: In Position
        config.shared_state['active_trades']['Call'] = {'id': 't1', 'qty': 50, 'pnl': 0}
        self.controller.state = 'IN_POSITION'; self.controller.active_side = 'Call'
        self.controller.today_index = 'NIFTY' # <--- FIX: Must specify index
        config.params['lots'] = 10
        
        # Run Loop (Sync happens inside run_loop)
        self.tick(1)
        
        # Verify params were written
        expected_tgt = 3000 * 10
        self.assertEqual(config.params['call_target_val'], expected_tgt)
        self.assertTrue(config.params['call_target_active'])
        print(f"   -> Call Target Synced to {expected_tgt}")

    def test_feature_14_smart_reversal_profit(self):
        print("\n✨ FEAT_14: Smart Reversal (Stop in Profit -> Cancel Trigger)")
        config.params['long_trigger_active'] = True
        trade = {'main': {'entry_price': 100}, 'index_entry_price': 20050, 'pnl': 0}
        
        new_candle = self.base_history[-1].copy()
        new_candle.update({'high': 20000, 'low': 19900, 'close': 19905})
        history = self.base_history[:-1] + [new_candle, new_candle] 
        self.mock_inst.set_candles(history)
        
        self.controller.today_index = 'NIFTY'
        self.controller.process_hourly_trailing(trade, 'Call')
        
        self.assertFalse(config.params['long_trigger_active'])
        print("   -> Stop in Profit (20000 < 20050). Reversal Cancelled.")

    def test_feature_15_smart_reversal_loss(self):
        print("\n✨ FEAT_15: Smart Reversal (Stop in Loss -> Move Trigger)")
        config.params['long_trigger_active'] = True
        config.params['long_open_amount'] = 20200 
        
        trade = {'main': {'entry_price': 100}, 'index_entry_price': 20050, 'pnl': 0}
        
        new_candle = self.base_history[-1].copy()
        new_candle.update({'high': 20080, 'low': 19980, 'close': 19985})
        history = self.base_history[:-1] + [new_candle, new_candle]
        self.mock_inst.set_candles(history)
        
        self.controller.today_index = 'NIFTY'
        self.controller.process_hourly_trailing(trade, 'Call')
        
        self.assertEqual(config.params['long_open_amount'], 20081.0)
        self.assertTrue(config.params['long_trigger_active'])
        print("   -> Stop in Loss. Reversal Trigger Moved to 20081.")

    def test_feature_16_inside_candle_tightening(self):
        print("\n✨ FEAT_16: Inside Candle Tightening")
        self.controller.today_index = 'NIFTY'
        self.controller.state = 'ARMED'
        self.controller.ref_high = 20200
        self.controller.ref_low = 19900
        
        inside_candle = {'date': None, 'open': 20050, 'high': 20100, 'low': 20000, 'close': 20050}
        self.mock_inst.set_candles([inside_candle, inside_candle])
        
        self.controller.check_inside_candle()
        
        self.assertEqual(self.controller.ref_high, 20100)
        self.assertEqual(self.controller.ref_low, 20000)
        print("   -> Triggers Tightened to Inside Bar (20100/20000).")

    def test_feature_17_second_leg_premium_check(self):
        print("\n✨ FEAT_17: Second Leg Premium Check")
        self.controller.today_index = 'NIFTY'
        self.controller.last_trade_entry_price = 100.0 
        
        config.params['put_manual_strike'] = '19900'
        self.mock_inst.kite.quote.return_value = {'12345': {'last_price': 50.0}}
        
        self.controller.check_second_leg_premium()
        
        self.assertEqual(config.params['put_manual_strike'], '19950')
        print("   -> Premium Low (50 < 100). Strike Shifted to 19950.")

    def test_feature_18_chart_freeze_on_done(self):
        print("\n✨ FEAT_18: Chart Freeze")
        self.controller.state = 'DONE'
        
        # FIX: Inject our test controller into the auto_run module
        # Because update_ui() uses the global 'controller' instance
        auto_run.controller = self.controller
        
        auto_run.update_ui()
        
        config.ui_refs['pnl_chart'].update.assert_not_called()
        print("   -> State is DONE. Chart Update Skipped.")

if __name__ == '__main__':
    unittest.main(exit=False)
