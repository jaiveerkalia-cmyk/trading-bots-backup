from config import shared_state, params, INDICES, TRADEBOOK_FILE, DAILY_PNL_FILE, FORCE_EXIT_TIME, AUTO_SQUAREOFF_TIME
from datetime import datetime, time as dtime
from nicegui import ui
import pandas as pd
import os
import uuid
import time

class CsvManager:
    def __init__(self): self.init_files()
    def init_files(self):
        if not os.path.exists(TRADEBOOK_FILE):
            cols = ['Trade_ID', 'Date', 'Index_Type', 'Expiry', 'Type', 'Qty', 'Main_Strike', 'Hedge_Strike', 'Open_Main_Price', 'Open_Hedge_Price', 'Open_Index_Price', 'Open_Time', 'Close_Main_Price', 'Close_Hedge_Price', 'Close_Index_Price', 'Close_Time', 'Profit', 'Status']
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
            'Type': trade_data['type'], 'Qty': int(trade_data['qty']), 'Main_Strike': float(trade_data['main']['strike']), 'Hedge_Strike': float(hedge_strike),
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

class LogicEngine:
    def __init__(self, ticker_client, instrument_manager):
        self.ticker = ticker_client; self.inst_manager = instrument_manager; self.csv_manager = CsvManager()
        self.last_trigger_time = {'1m': None, '5m': None, '15m': None, '60m': None, 'chart': None}
        self.alert_triggered = {'upper': False, 'lower': False}
        self.trading_active = True

    def log_action(self, message, details=""):
        timestamp = datetime.now().strftime("%H:%M:%S")
        full_msg = f"[{timestamp}] {message}"
        if details: full_msg += f" | {details}"
        shared_state['activity_log'].insert(0, full_msg)
        shared_state['activity_log'] = shared_state['activity_log'][:100]
        print(full_msg)

    def play_sound(self, type='alert'):
        if params['mute_sound']: return
        shared_state['sound_queue'].append(type)

    def play_alert_sound(self, sound_name, duration):
        """Pushes a user-selectable (sound, duration) alert sound. Distinct tuple shape from
        the legacy string entries ('open'/'close'/'error'/'alert') so existing sound_queue
        consumers keep working unchanged; consumers check for this tuple shape additively."""
        if params['mute_sound']: return
        try: dur = float(duration)
        except (ValueError, TypeError): dur = 5
        if dur <= 0: dur = 5
        shared_state['sound_queue'].append(('alert_custom', sound_name, dur))

    def add_chart_marker(self, text, value):
        t = datetime.now().strftime("%H:%M")
        shared_state['chart_data']['markers'].append({
            'name': text, 'coord': [t, value], 'value': value,
            'itemStyle': {'color': 'red' if 'Close' in text else 'green'}
        })

    def update_chart_data(self):
        now = datetime.now(); curr_min = now.minute
        if self.last_trigger_time['chart'] != curr_min:
            t_str = now.strftime("%H:%M")
            total_pnl = shared_state['pnl']['realized'] + shared_state['pnl']['unrealized']
            shared_state['chart_data']['times'].append(t_str)
            shared_state['chart_data']['pnl'].append(round(total_pnl, 2))
            self.last_trigger_time['chart'] = curr_min

    def commission(self, amount, buy_price, sell_price):
        buy_turnover = amount * buy_price
        sell_turnover = amount * sell_price
        total_turnover = buy_turnover + sell_turnover

        brokerage = 20 + 20
        stt = 0.0015 * sell_turnover
        exchange_txn = 0.0003503 * total_turnover
        sebi = 0.000001 * total_turnover
        gst = 0.18 * (brokerage + exchange_txn + sebi)
        stamp_duty = 0.00003 * buy_turnover
        ipft = 0.00000005 * total_turnover

        return round(brokerage + stt + exchange_txn + sebi + gst + stamp_duty + ipft, 2)
    
    def _place_live_order(self, symbol, txn_type, qty, product, exchange):
        kite = self.inst_manager.kite
        attempts = 0
        while attempts < 3:
            try:
                order_id = kite.place_order(variety=kite.VARIETY_REGULAR, exchange=exchange, tradingsymbol=symbol, transaction_type=txn_type, quantity=qty, product=product, order_type=kite.ORDER_TYPE_MARKET)
                self.log_action(f"✅ ORDER: {symbol} {txn_type}", f"ID: {order_id}")
                return True
            except Exception as e:
                attempts += 1; self.log_action(f"⚠️ RETRY {attempts}: {symbol}", str(e)); time.sleep(1)
        self.log_action(f"❌ FAILED: {symbol}", "Manual Check Req"); self.play_sound('error'); return False

    def open_position(self, side, manual_strike=None, reason="Manual", qty_override=None, strike_offset=None):
        if not self.trading_active: return False, "Trading Stopped"
        index_name = params['trading_index']; index_ltp = shared_state[index_name]['ltp']
        step = INDICES[index_name]['step']; lot_size = INDICES[index_name]['lot_size']
        segment = INDICES[index_name]['segment']
        hedgeless = params.get('hedgeless_mode', False)

        if index_ltp == 0: return False, "Index Price 0"
        if shared_state['active_trades'][side] is not None: return False, "Position Open"

        if manual_strike:
            main_strike = manual_strike
        elif strike_offset is not None:
            # Unified card strike selection: 0 = ATM, positive = ITM steps, negative = OTM steps.
            try: offset = float(strike_offset)
            except: offset = 0
            atm = round(index_ltp / step) * step
            main_strike = (atm - (offset * step)) if side == 'Call' else (atm + (offset * step))
        else:
            entry_mode = params['call_entry_mode'] if side == 'Call' else params['put_entry_mode']
            manual_key = 'call_manual_strike' if side == 'Call' else 'put_manual_strike'
            if entry_mode == 'ATM': main_strike = round(index_ltp / step) * step
            else:
                try: main_strike = float(params[manual_key])
                except: return False, "Invalid Strike"

        opt_type = 'CE' if side == 'Call' else 'PE'
        main_token, main_symbol = self.inst_manager.get_atm_token(index_name, main_strike, opt_type)
        if not main_token: return False, "Token Not Found"

        qty = int(qty_override) * lot_size if qty_override is not None else int(params['lots']) * lot_size
        kite = self.inst_manager.kite

        if hedgeless:
            # Hedgeless: only place sell on main, no hedge lookup or order
            if params['live_trading'] == 'On':
                if not self._place_live_order(main_symbol, kite.TRANSACTION_TYPE_SELL, qty, kite.PRODUCT_MIS, segment):
                    return False, "Main Fail"

            self.ticker.subscribe_new([main_token])
            if main_token not in shared_state['option_chain']:
                shared_state['option_chain'][main_token] = {'ltp': 0.0, 'symbol': main_symbol}

            m_pr = shared_state['option_chain'][main_token]['ltp']
            trade_id = str(uuid.uuid4())[:8]
            trade = {
                'id': trade_id, 'type': opt_type, 'qty': qty, 'status': 'OPEN', 'pnl': 0.0,
                'index_entry_price': index_ltp, 'index_current_price': index_ltp, 'csv_synced': False,
                'entry_time': datetime.now().strftime("%H:%M:%S"), 'trigger': reason,
                'main': {'symbol': main_symbol, 'token': main_token, 'strike': main_strike, 'entry_price': m_pr, 'current_price': m_pr},
                'hedge': None
            }
            shared_state['active_trades'][side] = trade
            self.csv_manager.log_open(trade_id, trade)
            dtls = f"Idx: {index_ltp:.2f} | M: {main_strike} ({m_pr:.2f}) | HEDGELESS"
            self.log_action(f"OPENED {side} HEDGELESS ({reason})", dtls)
            self.play_sound('open')
            self.add_chart_marker(f"Open {side}", shared_state['pnl']['realized'])
            return True, f"Opened {side} (Hedgeless)"

        else:
            # Normal mode: buy hedge + sell main
            hedge_strike = main_strike + (10 * step) if side == 'Call' else main_strike - (10 * step)
            hedge_token, hedge_symbol = self.inst_manager.get_atm_token(index_name, hedge_strike, opt_type)
            if not hedge_token: return False, "Hedge Token Not Found"

            if params['live_trading'] == 'On':
                if not self._place_live_order(hedge_symbol, kite.TRANSACTION_TYPE_BUY, qty, kite.PRODUCT_MIS, segment): return False, "Hedge Fail"
                time.sleep(1)
                if not self._place_live_order(main_symbol, kite.TRANSACTION_TYPE_SELL, qty, kite.PRODUCT_MIS, segment): return False, "Main Fail"

            self.ticker.subscribe_new([main_token, hedge_token])
            for t, s in [(main_token, main_symbol), (hedge_token, hedge_symbol)]:
                if t not in shared_state['option_chain']: shared_state['option_chain'][t] = {'ltp': 0.0, 'symbol': s}

            m_pr = shared_state['option_chain'][main_token]['ltp']
            h_pr = shared_state['option_chain'][hedge_token]['ltp']

            trade_id = str(uuid.uuid4())[:8]
            trade = {
                'id': trade_id, 'type': opt_type, 'qty': qty, 'status': 'OPEN', 'pnl': 0.0,
                'index_entry_price': index_ltp, 'index_current_price': index_ltp, 'csv_synced': False,
                'entry_time': datetime.now().strftime("%H:%M:%S"), 'trigger': reason,
                'main': {'symbol': main_symbol, 'token': main_token, 'strike': main_strike, 'entry_price': m_pr, 'current_price': m_pr},
                'hedge': {'symbol': hedge_symbol, 'token': hedge_token, 'strike': hedge_strike, 'entry_price': h_pr, 'current_price': h_pr}
            }
            shared_state['active_trades'][side] = trade
            self.csv_manager.log_open(trade_id, trade)
            dtls = f"Idx: {index_ltp:.2f} | M: {main_strike} ({m_pr:.2f}) | H: {hedge_strike} ({h_pr:.2f})"
            self.log_action(f"OPENED {side} ({reason})", dtls)
            self.play_sound('open')
            self.add_chart_marker(f"Open {side}", shared_state['pnl']['realized'])
            return True, f"Opened {side}"

    def close_position(self, side, reason="Manual"):
        trade = shared_state['active_trades'][side]
        if not trade: return False, "No Position"
        m = trade['main']
        h = trade['hedge']  # may be None in hedgeless mode

        kite = self.inst_manager.kite
        segment = INDICES[params['trading_index']]['segment']

        if params['live_trading'] == 'On':
            # Always cover the main (short) leg
            if not self._place_live_order(m['symbol'], kite.TRANSACTION_TYPE_BUY, trade['qty'], kite.PRODUCT_MIS, segment):
                return False, "Cover Fail"
            # Only sell hedge if it exists
            if h:
                time.sleep(1)
                if not self._place_live_order(h['symbol'], kite.TRANSACTION_TYPE_SELL, trade['qty'], kite.PRODUCT_MIS, segment):
                    self.log_action("⚠️ Hedge Exit Fail"); self.play_sound('error')

        m_curr = shared_state['option_chain'].get(m['token'], {}).get('ltp', 0)
        m_entry = m['entry_price'] if m['entry_price'] > 0 else m_curr
        idx_curr = shared_state[params['trading_index']]['ltp']

        if h:
            h_curr = shared_state['option_chain'].get(h['token'], {}).get('ltp', 0)
            h_entry = h['entry_price'] if h['entry_price'] > 0 else h_curr
            net_pnl = ((m_entry - m_curr) * trade['qty']) + ((h_curr - h_entry) * trade['qty'])
            net_pnl -= (self.commission(trade['qty'], m_curr, m_entry) + self.commission(trade['qty'], h_curr, h_entry))
            h_exit_price = h_curr
        else:
            h_curr = 0; h_entry = 0; h_exit_price = 0
            net_pnl = (m_entry - m_curr) * trade['qty']
            net_pnl -= self.commission(trade['qty'], m_curr, m_entry)

        shared_state['pnl']['realized'] += net_pnl
        shared_state['pnl']['trades_history'].append({'symbol': f"{m['symbol']}", 'pnl': round(net_pnl, 2), 'reason': reason})
        self.csv_manager.log_close(trade['id'], {'main_price': m_curr, 'hedge_price': h_exit_price, 'index_price': idx_curr}, round(net_pnl, 2))
        shared_state['active_trades'][side] = None

        dtls = f"Idx: {idx_curr:.2f} | PnL: {net_pnl:.0f} | M_Ex: {m_curr:.2f}" + (f" | H_Ex: {h_curr:.2f}" if h else " | HEDGELESS")
        self.log_action(f"CLOSED {side} ({reason})", dtls)
        self.play_sound('close')
        self.add_chart_marker(f"Close {side}", shared_state['pnl']['realized'])
        shared_state['reset_queue'].append(side)
        return True, "Closed"

    def close_all_positions(self, reason="Global Exit", save_pnl=True):
        for side in ['Call', 'Put']:
            if shared_state['active_trades'][side]: self.close_position(side, reason)
        if save_pnl:
            self.csv_manager.save_daily_report()
            self.log_action("📊 Daily Report Saved", f"{reason}")
        else:
            self.log_action("ℹ️ All Positions Closed", f"{reason}")

    def update_pnl(self):
        unrealized = 0.0
        idx_ltp = shared_state[params['trading_index']]['ltp']
        for side in ['Call', 'Put']:
            trade = shared_state['active_trades'][side]
            if trade:
                trade['index_current_price'] = idx_ltp
                m_ltp = shared_state['option_chain'].get(trade['main']['token'], {}).get('ltp', 0)
                trade['main']['current_price'] = m_ltp
                if trade['main']['entry_price'] == 0 and m_ltp > 0: trade['main']['entry_price'] = m_ltp

                if trade['hedge']:
                    h_ltp = shared_state['option_chain'].get(trade['hedge']['token'], {}).get('ltp', 0)
                    trade['hedge']['current_price'] = h_ltp
                    if trade['hedge']['entry_price'] == 0 and h_ltp > 0: trade['hedge']['entry_price'] = h_ltp
                    if not trade['csv_synced'] and trade['main']['entry_price'] > 0:
                        self.csv_manager.update_entry_log(trade['id'], trade['main']['entry_price'], trade['hedge']['entry_price'], idx_ltp)
                        self.log_action("📝 Entry Prices Confirmed", f"M: {trade['main']['entry_price']:.2f} | H: {trade['hedge']['entry_price']:.2f}")
                        trade['csv_synced'] = True
                    m_pnl = (trade['main']['entry_price'] - m_ltp) * trade['qty'] if m_ltp > 0 else 0
                    h_pnl = (h_ltp - trade['hedge']['entry_price']) * trade['qty'] if h_ltp > 0 else 0
                    trade['pnl'] = m_pnl + h_pnl
                else:
                    # Hedgeless: PnL is only from the short main leg
                    if not trade['csv_synced'] and trade['main']['entry_price'] > 0:
                        self.csv_manager.update_entry_log(trade['id'], trade['main']['entry_price'], 0, idx_ltp)
                        self.log_action("📝 Entry Price Confirmed (Hedgeless)", f"M: {trade['main']['entry_price']:.2f}")
                        trade['csv_synced'] = True
                    m_pnl = (trade['main']['entry_price'] - m_ltp) * trade['qty'] if m_ltp > 0 else 0
                    trade['pnl'] = m_pnl

                unrealized += trade['pnl']
        shared_state['pnl']['unrealized'] = unrealized
        self.update_chart_data()

    def check_triggers(self):
        if not self.trading_active: return
        now = datetime.now(); idx_ltp = shared_state[params['trading_index']]['ltp']
        is_new_min = now.second < 5; curr_min = now.minute
        fire_1m = is_new_min and self.last_trigger_time['1m'] != curr_min
        fire_5m = is_new_min and (curr_min % 5 == 0) and self.last_trigger_time['5m'] != curr_min
        fire_15m = is_new_min and (curr_min % 15 == 0) and self.last_trigger_time['15m'] != curr_min
        fire_60m = is_new_min and (curr_min == 0) and self.last_trigger_time['60m'] != curr_min

        # 15:19 Auto-Squareoff (SAVES PNL)
        if now.time() >= AUTO_SQUAREOFF_TIME and now.time() < dtime(15, 20) and not shared_state['auto_sq_done']:
            self.close_all_positions("15:19 Auto-SQ", save_pnl=True)
            shared_state['auto_sq_done'] = True
            self.log_action("⚠️ 15:19 Day End Executed")

        if shared_state['active_trades']['Call'] is None and params['short_trigger_active']:
            self._check_single_open('Call', 'short', idx_ltp, fire_1m, fire_5m)
        if shared_state['active_trades']['Put'] is None and params['long_trigger_active']:
            self._check_single_open('Put', 'long', idx_ltp, fire_1m, fire_5m)

        # Unified Open Short/Long cards (index-based, order-type + fire-on aware)
        if shared_state['active_trades']['Call'] is None and params.get('call_armed'):
            self._check_unified_open('Call', idx_ltp, fire_1m, fire_5m, fire_15m, fire_60m)
        if shared_state['active_trades']['Put'] is None and params.get('put_armed'):
            self._check_unified_open('Put', idx_ltp, fire_1m, fire_5m, fire_15m, fire_60m)

        self._check_exits(idx_ltp, fire_1m, fire_5m)
        self._check_global_limits()
        self._check_alerts(idx_ltp, fire_1m, fire_5m)

        if fire_1m: self.last_trigger_time['1m'] = curr_min
        if fire_5m: self.last_trigger_time['5m'] = curr_min
        if fire_15m: self.last_trigger_time['15m'] = curr_min
        if fire_60m: self.last_trigger_time['60m'] = curr_min

    def _check_single_open(self, side, prefix, idx_ltp, fire_1m, fire_5m):
        mode = params[f'{prefix}_open_mode']
        try: trigger_price = float(params[f'{prefix}_open_amount'])
        except: return
        try: strike = float(params[f'{prefix}_open_strike'])
        except: strike = 0
        should_fire = False
        reason = f"{mode} < {trigger_price}" if side == 'Call' else f"{mode} > {trigger_price}"

        if side == 'Call':
            if mode == 'Current' and idx_ltp > 0 and idx_ltp < trigger_price: should_fire = True
            elif mode == '1m' and fire_1m and idx_ltp < trigger_price: should_fire = True
            elif mode == '5m' and fire_5m and idx_ltp < trigger_price: should_fire = True
            elif mode == 'Loss':
                opp = shared_state['active_trades']['Put']
                if opp and opp['pnl'] < -abs(trigger_price): should_fire = True; reason = f"Put Loss {opp['pnl']}"
        else:
            if mode == 'Current' and idx_ltp > 0 and idx_ltp > trigger_price: should_fire = True
            elif mode == '1m' and fire_1m and idx_ltp > trigger_price: should_fire = True
            elif mode == '5m' and fire_5m and idx_ltp > trigger_price: should_fire = True
            elif mode == 'Loss':
                opp = shared_state['active_trades']['Call']
                if opp and opp['pnl'] < -abs(trigger_price): should_fire = True; reason = f"Call Loss {opp['pnl']}"

        if should_fire:
            self.log_action(f"⚡ TRIGGER FIRED: {side} ({reason})")
            success, msg = self.open_position(side, manual_strike=strike if strike > 0 else None, reason=reason)
            if not success:
                self.log_action(f"⚠️ Trigger Fired but Open Failed: {msg}")
                params[f'{prefix}_trigger_active'] = False
                self.play_sound('error')

    def _check_unified_open(self, side, idx_ltp, fire_1m, fire_5m, fire_15m, fire_60m):
        """Checks/fires the new unified Open Short/Long card (order type + fire-on timeframe)."""
        prefix = 'call' if side == 'Call' else 'put'
        order_type = params.get(f'{prefix}_order_type', 'Market')
        fire_on = params.get(f'{prefix}_fire_on', 'Live')

        try: trigger_price = float(params.get(f'{prefix}_trigger_price', 0))
        except: trigger_price = 0
        try: strike_offset = float(params.get(f'{prefix}_strike_offset', 1))
        except: strike_offset = 1
        try: qty = int(float(params.get(f'{prefix}_qty', 4)))
        except: qty = 4

        # Gate by the selected candle-close timeframe. 'Live' checks every tick.
        if fire_on == 'Live': timing_ok = True
        elif fire_on == '1m': timing_ok = fire_1m
        elif fire_on == '5m': timing_ok = fire_5m
        elif fire_on == '15m': timing_ok = fire_15m
        elif fire_on == '60m': timing_ok = fire_60m
        else: timing_ok = True

        if idx_ltp <= 0: return

        should_fire = False
        reason = f"{order_type} @ {trigger_price} ({fire_on})"

        if order_type == 'Market':
            # Market orders fire immediately via the UI callback, not through this polling
            # path. Kept here defensively in case armed is ever set for a Market order.
            should_fire = True
            reason = "Market (Immediate)"
        elif not timing_ok:
            return
        elif order_type == 'Stop-Market':
            # Breakout confirmation in the direction of the trade's original bias.
            if side == 'Call' and idx_ltp <= trigger_price: should_fire = True
            elif side == 'Put' and idx_ltp >= trigger_price: should_fire = True
        elif order_type == 'Limit':
            # Wait for a better (opposite-direction) entry price.
            if side == 'Call' and idx_ltp >= trigger_price: should_fire = True
            elif side == 'Put' and idx_ltp <= trigger_price: should_fire = True

        if should_fire:
            self.log_action(f"⚡ UNIFIED TRIGGER FIRED: {side} ({reason})")
            success, msg = self.open_position(side, reason=reason, qty_override=qty, strike_offset=strike_offset)
            if success:
                params[f'{prefix}_armed'] = False
                # Optional stop/target set at entry, applied via the existing PnL exit engine.
                try:
                    new_stop = float(params.get(f'{prefix}_new_stop', ''))
                    if new_stop > 0:
                        params[f'{prefix}_stop_val'] = new_stop
                        params[f'{prefix}_stop_active'] = True
                except (ValueError, TypeError): pass
                try:
                    new_target = float(params.get(f'{prefix}_new_target', ''))
                    if new_target > 0:
                        params[f'{prefix}_target_val'] = new_target
                        params[f'{prefix}_target_active'] = True
                except (ValueError, TypeError): pass
            else:
                self.log_action(f"⚠️ Unified Trigger Fired but Open Failed: {msg}")
                params[f'{prefix}_armed'] = False
                self.play_sound('error')

    def _check_exits(self, idx_ltp, fire_1m, fire_5m):
        for side in ['Call', 'Put']:
            trade = shared_state['active_trades'][side]
            if not trade: continue

            # --- PNL EXITS ---
            try: tgt = float(params[f'{side.lower()}_target_val']) if str(params[f'{side.lower()}_target_val']).strip() != '' else 0.0
            except ValueError: tgt = 0.0
            try: stp = float(params[f'{side.lower()}_stop_val']) if str(params[f'{side.lower()}_stop_val']).strip() != '' else 0.0
            except ValueError: stp = 0.0

            if params[f'{side.lower()}_target_active'] and tgt > 0 and trade['pnl'] >= tgt:
                self.close_position(side, f"Auto Profit {tgt}")
            if params[f'{side.lower()}_stop_active'] and stp > 0 and trade['pnl'] <= -stp:
                self.close_position(side, f"Auto Loss {stp}")

            # Re-check if position still open after PnL exits
            trade = shared_state['active_trades'][side]
            if not trade: continue

            # --- INDEX EXITS ---
            try: s_val = float(params[f'{side.lower()}_index_stop_val']) if str(params[f'{side.lower()}_index_stop_val']).strip() != '' else 0.0
            except ValueError: s_val = 0.0
            try: t_val = float(params[f'{side.lower()}_index_target_val']) if str(params[f'{side.lower()}_index_target_val']).strip() != '' else 0.0
            except ValueError: t_val = 0.0

            st_key = f'{side.lower()}_index_stop_time'; check_stop = (params[st_key] == 'Current') or (params[st_key] == '1m' and fire_1m) or (params[st_key] == '5m' and fire_5m)
            tt_key = f'{side.lower()}_index_target_time'; check_tgt = (params[tt_key] == 'Current') or (params[tt_key] == '1m' and fire_1m) or (params[tt_key] == '5m' and fire_5m)
            s_active = params[f'{side.lower()}_index_stop_active']; t_active = params[f'{side.lower()}_index_tgt_active']

            if side == 'Call':
                if check_stop and s_active and s_val > 0 and idx_ltp >= s_val: self.close_position(side, f"Idx Stop {s_val}")
                if check_tgt and t_active and t_val > 0 and idx_ltp <= t_val: self.close_position(side, f"Idx Target {t_val}")
            else:
                if check_stop and s_active and s_val > 0 and idx_ltp <= s_val: self.close_position(side, f"Idx Stop {s_val}")
                if check_tgt and t_active and t_val > 0 and idx_ltp >= t_val: self.close_position(side, f"Idx Target {t_val}")

            # Re-check if position still open after index exits
            trade = shared_state['active_trades'][side]
            if not trade: continue

            # --- PREMIUM EXITS ---
            main_ltp = shared_state['option_chain'].get(trade['main']['token'], {}).get('ltp', 0)
            if main_ltp <= 0: continue

            ps_key = f'{side.lower()}_prem_stop_time'
            pt_key = f'{side.lower()}_prem_target_time'
            check_ps = (params[ps_key] == 'Current') or (params[ps_key] == '1m' and fire_1m) or (params[ps_key] == '5m' and fire_5m)
            check_pt = (params[pt_key] == 'Current') or (params[pt_key] == '1m' and fire_1m) or (params[pt_key] == '5m' and fire_5m)

            try: ps_val = float(params[f'{side.lower()}_prem_stop_val']) if str(params[f'{side.lower()}_prem_stop_val']).strip() != '' else 0.0
            except ValueError: ps_val = 0.0
            try: pt_val = float(params[f'{side.lower()}_prem_target_val']) if str(params[f'{side.lower()}_prem_target_val']).strip() != '' else 0.0
            except ValueError: pt_val = 0.0

            # Stop: premium rises above stop value (loss on short)
            if check_ps and params[f'{side.lower()}_prem_stop_active'] and ps_val > 0 and main_ltp >= ps_val:
                self.close_position(side, f"Prem Stop {ps_val}")

            trade = shared_state['active_trades'][side]
            if not trade: continue

            # Target: premium falls below target value (profit on short)
            if check_pt and params[f'{side.lower()}_prem_tgt_active'] and pt_val > 0 and main_ltp <= pt_val:
                self.close_position(side, f"Prem Target {pt_val}")

    def _check_global_limits(self):
        total = shared_state['pnl']['realized'] + shared_state['pnl']['unrealized']

        try: stop = float(params['global_stop_value']) if str(params['global_stop_value']).strip() != '' else 0.0
        except ValueError: stop = 0.0
        try: target = float(params['global_target_value']) if str(params['global_target_value']).strip() != '' else 0.0
        except ValueError: target = 0.0

        if params['global_stop_active'] and stop > 0 and total <= -stop:
            self.close_all_positions("Global Stop", save_pnl=True)
            self.trading_active = False
            self.play_sound('error')
        if params['global_tgt_active'] and target > 0 and total >= target:
            self.close_all_positions("Global Target", save_pnl=True)
            self.trading_active = False
            self.play_sound('error')

    def _check_alerts(self, idx_ltp, fire_1m, fire_5m):
        up = params['alert_upper']; low = params['alert_lower']

        upper_mode = params.get('alert_upper_period', 'Current')
        lower_mode = params.get('alert_lower_period', 'Current')
        check_upper = (upper_mode == 'Current') or (upper_mode == '1m' and fire_1m) or (upper_mode == '5m' and fire_5m)
        check_lower = (lower_mode == 'Current') or (lower_mode == '1m' and fire_1m) or (lower_mode == '5m' and fire_5m)

        if check_upper and params['alert_upper_active'] and up > 0 and idx_ltp >= up:
            ui.notify(f"ALERT: Price {idx_ltp} > {up}", type='warning', close_button=True)
            params['alert_upper_active'] = False; params['alert_upper_input'] = 0
            self.log_action(f"🔔 ALERT: {idx_ltp} >= {up}")
            self.play_alert_sound(params.get('alert_upper_sound', 'Wood Plank'), params.get('alert_upper_duration', 5))

        if check_lower and params['alert_lower_active'] and low > 0 and idx_ltp <= low:
            ui.notify(f"ALERT: Price {idx_ltp} < {low}", type='warning', close_button=True)
            params['alert_lower_active'] = False; params['alert_lower_input'] = 0
            self.log_action(f"🔔 ALERT: {idx_ltp} <= {low}")
            self.play_alert_sound(params.get('alert_lower_sound', 'Wood Plank'), params.get('alert_lower_duration', 5))
