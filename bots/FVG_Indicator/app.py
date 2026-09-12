import asyncio
import csv
import json
import logging
import math
import os
import sys
import time
import traceback
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from aiohttp import ClientSession
from nicegui import ui, app

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("FVG-Scanner")

# --- Timezone: Indian Standard Time (IST) ---
IST = timezone(timedelta(hours=5, minutes=30))

def get_ist_str(timestamp_ms: Optional[int] = None, full: bool = True) -> str:
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=IST) if timestamp_ms else datetime.now(tz=IST)
    return dt.strftime("%Y-%m-%d %H:%M:%S IST") if full else dt.strftime("%H:%M:%S IST")

# --- Storage Configuration ---
DATA_DIR = Path("/app/data") if Path("/app/data").exists() else Path("./data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = DATA_DIR / "settings.json"
CSV_FILE = DATA_DIR / "fvgs.csv"

CSV_HEADERS = [
    "id", "timestamp_ist", "closed_timestamp_ist", "symbol", "timeframe",
    "type", "top", "bottom", "gap_size", "total_volume",
    "high_volume", "low_volume", "balance_pct", "combined",
    "source", "status", "closed_price"
]

TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1D"]
TF_BINANCE_MAP = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
    "1D": "1d"
}
TF_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1D": 86400
}

PORT = 8080

# --- 8 High-Clarity Sound Options (Google Actions CDN) ---
SOUND_PRESETS = {
    "Siren": "https://actions.google.com/sounds/v1/emergency/ambulance_siren.ogg",
    "Short Siren": "https://actions.google.com/sounds/v1/emergency/emergency_siren_short_burst.ogg",
    "Spaceship Alarm": "https://actions.google.com/sounds/v1/alarms/spaceship_alarm.ogg",
    "Digital Alarm": "https://actions.google.com/sounds/v1/alarms/digital_watch_alarm_long.ogg",
    "Air Horn": "https://actions.google.com/sounds/v1/transportation/air_horn_in_close_hall_series.ogg",
    "Bell Ring": "https://actions.google.com/sounds/v1/alarms/medium_bell_ringing_near.ogg",
    "Beep Short": "https://actions.google.com/sounds/v1/alarms/beep_short.ogg",
    "Wood Plank": "https://actions.google.com/sounds/v1/cartoon/wood_plank_flicks.ogg",
}

DEFAULT_SETTINGS = {
    "symbol": "BTCUSDT",
    "audio_preset": "Siren",
    "audio_duration": 5,
    "timeframes": {
        tf: {
            "muted": False,
            "filter_method": "Volume Threshold",
            "vol_thresh_pct": 70,
            "sens_level": "Low",
            "sens_enabled": True,
            "fvg_bars": "All",
            "end_method": "Close",
            "allow_gaps": True,
        } for tf in TIMEFRAMES
    }
}

SENS_MAP = {"Extreme": 6.0, "High": 2.0, "Normal": 1.5, "Low": 1.0}

def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("symbol", DEFAULT_SETTINGS["symbol"])
                preset = data.get("audio_preset", "Siren")
                if preset not in SOUND_PRESETS:
                    data["audio_preset"] = "Siren"
                data.setdefault("audio_duration", DEFAULT_SETTINGS["audio_duration"])
                data.setdefault("timeframes", {})
                for tf in TIMEFRAMES:
                    if tf not in data["timeframes"]:
                        data["timeframes"][tf] = DEFAULT_SETTINGS["timeframes"][tf].copy()
                    else:
                        if data["timeframes"][tf].get("fvg_bars") == "Same Type":
                            data["timeframes"][tf]["fvg_bars"] = "All"
                        if data["timeframes"][tf].get("filter_method") == "Average Range":
                            data["timeframes"][tf]["filter_method"] = "Volume Threshold"
                            data["timeframes"][tf]["vol_thresh_pct"] = 70
                            data["timeframes"][tf]["sens_level"] = "Low"
                return data
        except Exception as e:
            logger.error(f"Error loading {SETTINGS_FILE}: {e}")
    return json.loads(json.dumps(DEFAULT_SETTINGS))

def save_settings():
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(active_settings, f, indent=2)
        logger.info("Saved persistent settings to disk.")
    except Exception as e:
        logger.error(f"Failed to save settings: {e}")

active_settings = load_settings()
csv_lock = asyncio.Lock()

# --- Decoupled Alert & Toast Queues ---
sound_queue = []
toast_queue = []

async def init_csv():
    async with csv_lock:
        if not CSV_FILE.exists():
            with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(CSV_HEADERS)
            logger.info(f"Initialized CSV log header at {CSV_FILE}")

async def record_fvg_csv(fvg_dict: dict):
    async with csv_lock:
        try:
            with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
                writer.writerow(fvg_dict)
        except Exception as e:
            logger.error(f"CSV append error: {e}")

async def update_fvg_close_csv(fvg_id: str, closed_ts: str, closed_price: float):
    async with csv_lock:
        try:
            if not CSV_FILE.exists():
                return
            rows = []
            with open(CSV_FILE, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    if "timestamp" in r and "timestamp_ist" not in r:
                        r["timestamp_ist"] = r.pop("timestamp")
                    if "closed_timestamp" in r and "closed_timestamp_ist" not in r:
                        r["closed_timestamp_ist"] = r.pop("closed_timestamp")

                    if r.get("id") == fvg_id:
                        r["status"] = "CLOSED"
                        r["closed_timestamp_ist"] = closed_ts
                        r["closed_price"] = f"{closed_price:.4f}"
                    rows.append(r)

            with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
        except Exception as e:
            logger.error(f"CSV update error: {e}")

@dataclass
class FVG:
    id: str
    timestamp_ist: str
    closed_timestamp_ist: str
    symbol: str
    timeframe: str
    type: str
    top: float
    bottom: float
    gap_size: float
    total_volume: float
    high_volume: float
    low_volume: float
    balance_pct: int
    combined: bool
    source: str
    status: str
    closed_price: str
    last_touched_bar: int

class TimeframeEngine:
    def __init__(self, tf: str):
        self.tf = tf
        self.bars = deque(maxlen=200)
        self.fvg_list: List[FVG] = []
        self.bar_index = 0
        self.atr = 0.0
        self.last_closed_bar_time = 0

    def get_cfg(self) -> dict:
        return active_settings["timeframes"].get(self.tf, DEFAULT_SETTINGS["timeframes"][self.tf])

    def calculate_atr(self):
        if len(self.bars) < 2:
            return
        c, p = self.bars[-1], self.bars[-2]
        tr = max(c['h'] - c['l'], abs(c['h'] - p['c']), abs(c['l'] - p['c']))
        self.atr = tr if self.atr == 0.0 else (self.atr * 9.0 + tr) / 10.0

    def check_invalidations(self, curr_high: float, curr_low: float, curr_close: float, ts_str: str) -> List[FVG]:
        cfg = self.get_cfg()
        end_method = cfg.get("end_method", "Close")
        closed_fvgs = []
        surviving = []

        for f in self.fvg_list:
            if f.type == "BULLISH" and curr_low <= f.top:
                f.last_touched_bar = self.bar_index
            elif f.type == "BEARISH" and curr_high >= f.bottom:
                f.last_touched_bar = self.bar_index

            is_closed = False
            trigger_px = 0.0

            if f.type == "BULLISH":
                if (end_method == "Close" and curr_close < f.bottom) or (end_method == "Wick" and curr_low < f.bottom):
                    is_closed = True
                    trigger_px = curr_close if end_method == "Close" else curr_low
            else:
                if (end_method == "Close" and curr_close > f.top) or (end_method == "Wick" and curr_high > f.top):
                    is_closed = True
                    trigger_px = curr_close if end_method == "Close" else curr_high

            if (self.bar_index - f.last_touched_bar) > 200:
                is_closed = True
                trigger_px = curr_close

            if is_closed:
                f.status = "CLOSED"
                f.closed_timestamp_ist = ts_str
                f.closed_price = f"{trigger_px:.4f}"
                closed_fvgs.append(f)
            else:
                surviving.append(f)

        self.fvg_list = surviving
        return closed_fvgs

    def detect_fvg(self, source: str = "LIVE") -> Optional[FVG]:
        if len(self.bars) < 16:
            return None

        cfg = self.get_cfg()
        sens_mult = SENS_MAP.get(cfg.get("sens_level", "Low"), 1.0)
        sens_enabled = cfg.get("sens_enabled", True)
        allow_gaps = cfg.get("allow_gaps", True)
        filter_method = cfg.get("filter_method", "Volume Threshold")
        vol_thresh_pct = cfg.get("vol_thresh_pct", 70)
        fvg_bars = cfg.get("fvg_bars", "All")

        b0, b1, b2 = self.bars[-1], self.bars[-2], self.bars[-3]

        first_size = abs(b0['o'] - b0['c'])
        second_size = abs(b1['o'] - b1['c'])
        third_size = abs(b2['o'] - b2['c'])
        bar_size_sum = first_size + second_size + third_size

        if fvg_bars == "Same Type":
            bars_match = (b0['o'] > b0['c'] and b1['o'] > b1['c'] and b2['o'] > b2['c']) or \
                         (b0['o'] <= b0['c'] and b1['o'] <= b1['c'] and b2['o'] <= b2['c'])
        else:
            bars_match = True

        if not bars_match:
            return None

        max_co_diff = max(abs(b2['c'] - b1['o']), abs(b1['c'] - b0['o']))
        gap_cond = allow_gaps or (max_co_diff <= self.atr)

        if filter_method == "Average Range":
            bear_cond = ((bar_size_sum * sens_mult > (self.atr / 1.5)) or not sens_enabled) and gap_cond
            bull_cond = ((bar_size_sum * sens_mult > (self.atr / 1.5)) or not sens_enabled) and gap_cond
        else:
            vols = [b['v'] for b in self.bars]
            short_vol = sum(vols[-5:]) / 5.0
            long_vol = sum(vols[-15:]) / 15.0
            threshold = long_vol * (vol_thresh_pct / 100.0)
            bear_cond = (short_vol > threshold) and gap_cond
            bull_cond = (short_vol > threshold) and gap_cond

        bear_fvg = (b0['h'] < b2['l']) and (b1['c'] < b2['l']) and bear_cond
        bull_fvg = (b0['l'] > b2['h']) and (b1['c'] > b2['h']) and bull_cond

        if not (bear_fvg or bull_fvg):
            return None

        gap_size = (b0['l'] - b2['h']) if bull_fvg else (b2['l'] - b0['h'])
        if sens_enabled and (gap_size * sens_mult <= self.atr):
            return None

        tot_vol = b0['v'] + b1['v'] + b2['v']
        hi_v = (b0['v'] + b1['v']) if bull_fvg else b2['v']
        lo_v = b2['v'] if bull_fvg else (b0['v'] + b1['v'])
        bal = int((min(hi_v, lo_v) / max(hi_v, lo_v, 1.0)) * 100.0)
        ts_ist = get_ist_str(b0['t'], full=True)

        fvg = FVG(
            id=f"{active_settings['symbol']}_{self.tf}_{b0['t']}",
            timestamp_ist=ts_ist,
            closed_timestamp_ist="",
            symbol=active_settings['symbol'],
            timeframe=self.tf,
            type="BULLISH" if bull_fvg else "BEARISH",
            top=b0['l'] if bull_fvg else b2['l'],
            bottom=b2['h'] if bull_fvg else b0['h'],
            gap_size=round(gap_size, 4),
            total_volume=round(tot_vol, 2),
            high_volume=round(hi_v, 2),
            low_volume=round(lo_v, 2),
            balance_pct=bal,
            combined=False,
            source=source,
            status="ACTIVE",
            closed_price="",
            last_touched_bar=self.bar_index
        )
        self.fvg_list.insert(0, fvg)
        return fvg

# Global State
engines: Dict[str, TimeframeEngine] = {tf: TimeframeEngine(tf) for tf in TIMEFRAMES}
event_log = deque(maxlen=100)
selected_event_tf = "ALL"
top_50_symbols: List[str] = ["BTCUSDT"]
clock_worker_task: Optional[asyncio.Task] = None

def load_recent_events_from_csv():
    if not CSV_FILE.exists():
        return
    try:
        with open(CSV_FILE, "r", newline="", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            for r in reversed(reader[-60:]):
                t_ist = r.get("timestamp_ist") or r.get("timestamp") or "N/A"
                c_ist = r.get("closed_timestamp_ist") or r.get("closed_timestamp") or ""
                status = r.get("status", "FORMED")
                time_display = c_ist if status == "CLOSED" and c_ist else t_ist
                desc = f"Invalidated at {r.get('closed_price')}" if status == "CLOSED" else f"Vol {r.get('total_volume', '0')} ({r.get('balance_pct', '0')}%)"
                event_log.append({
                    "time_ist": time_display,
                    "tf": r.get("timeframe", "1m"),
                    "type": r.get("type", "BULLISH"),
                    "range": f"{float(r.get('bottom', 0)):.2f} — {float(r.get('top', 0)):.2f}",
                    "vol": f"{float(r.get('total_volume', 0)):.1f}",
                    "bal": f"{r.get('balance_pct', 0)}%",
                    "status": status,
                    "desc": desc
                })
        logger.info(f"Loaded {len(event_log)} previous events from CSV.")
    except Exception as e:
        logger.error(f"Error loading events from CSV: {e}")

# Client Autoplay Unlock Script
AUDIO_UNLOCK_JS = """
['click', 'keydown', 'touchstart'].forEach(evt => {
    document.addEventListener(evt, () => {
        try {
            const silent = new Audio("https://actions.google.com/sounds/v1/alarms/beep_short.ogg");
            silent.volume = 0.01;
            silent.play().then(() => {
                silent.pause();
                silent.currentTime = 0;
            }).catch(() => {});
        } catch(e) {}
    }, { once: true });
});
"""

async def fetch_top_50_symbols():
    global top_50_symbols
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    try:
        async with ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    usdt_pairs = [d for d in data if d.get("symbol", "").endswith("USDT")]
                    usdt_pairs.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
                    top_50_symbols = [d["symbol"] for d in usdt_pairs[:50]]
                    if active_settings["symbol"] not in top_50_symbols:
                        top_50_symbols.insert(0, active_settings["symbol"])
                    logger.info(f"Loaded top {len(top_50_symbols)} Binance Futures contracts.")
    except Exception as e:
        logger.error(f"Failed to fetch top symbols: {e}")
        top_50_symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]

async def seed_historical():
    symbol = active_settings["symbol"].upper()
    logger.info(f"Seeding historical bars for {symbol} across 6 timeframes...")
    async with ClientSession() as session:
        for tf in TIMEFRAMES:
            api_interval = TF_BINANCE_MAP[tf]
            eng = engines[tf]
            eng.bars.clear()
            eng.fvg_list.clear()
            eng.bar_index = 0
            eng.atr = 0.0

            url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={api_interval}&limit=120"
            try:
                async with session.get(url, timeout=12) as resp:
                    if resp.status == 200:
                        raw = await resp.json()
                        for k in raw[:-1]:
                            eng.bar_index += 1
                            bar = {
                                't': int(k[0]), 'o': float(k[1]), 'h': float(k[2]),
                                'l': float(k[3]), 'c': float(k[4]), 'v': float(k[5])
                            }
                            eng.bars.append(bar)
                            eng.calculate_atr()

                            ts_now = get_ist_str(bar['t'], full=True)
                            closed = eng.check_invalidations(bar['h'], bar['l'], bar['c'], ts_now)
                            for c in closed:
                                await update_fvg_close_csv(c.id, c.closed_timestamp_ist, float(c.closed_price))

                            fvg = eng.detect_fvg(source="HISTORICAL")
                            if fvg:
                                await record_fvg_csv(asdict(fvg))

                        if eng.bars:
                            eng.last_closed_bar_time = eng.bars[-1]['t']
                        logger.info(f"[{tf}] Seeded {len(eng.bars)} bars. Active FVGs: {len(eng.fvg_list)}")
            except Exception as e:
                logger.error(f"Historical seed error on {tf}: {e}")

async def process_new_candle(tf: str, k: list):
    eng = engines[tf]
    bar_time = int(k[0])
    if bar_time <= eng.last_closed_bar_time:
        return

    eng.last_closed_bar_time = bar_time
    eng.bar_index += 1
    bar = {
        't': bar_time, 'o': float(k[1]), 'h': float(k[2]),
        'l': float(k[3]), 'c': float(k[4]), 'v': float(k[5])
    }
    eng.bars.append(bar)
    eng.calculate_atr()
    ts_now = get_ist_str(bar['t'], full=True)
    cfg = eng.get_cfg()

    # 1. Check Invalidations (Clean Event Logging)
    closed = eng.check_invalidations(bar['h'], bar['l'], bar['c'], ts_now)
    for c in closed:
        await update_fvg_close_csv(c.id, c.closed_timestamp_ist, float(c.closed_price))
        event_log.appendleft({
            "time_ist": c.closed_timestamp_ist,
            "tf": tf,
            "type": c.type,
            "range": f"{c.bottom:.2f} — {c.top:.2f}",
            "vol": f"{c.total_volume:.1f}",
            "bal": f"{c.balance_pct}%",
            "status": "CLOSED",
            "desc": f"Invalidated at {c.closed_price}"
        })
        logger.info(f"[{tf}] FVG CLOSED at {c.closed_price}")
        if not cfg.get("muted", False):
            sound_queue.append(('close', active_settings.get("audio_preset", "Siren"), active_settings.get("audio_duration", 5)))
        toast_queue.append((f"[{tf.upper()}] FVG Mitigated & Closed!", 'warning'))

    # 2. Detect Formed FVG (Clean Event Logging)
    new_fvg = eng.detect_fvg(source="LIVE")
    if new_fvg:
        await record_fvg_csv(asdict(new_fvg))
        event_log.appendleft({
            "time_ist": new_fvg.timestamp_ist,
            "tf": tf,
            "type": new_fvg.type,
            "range": f"{new_fvg.bottom:.2f} — {new_fvg.top:.2f}",
            "vol": f"{new_fvg.total_volume:.1f}",
            "bal": f"{new_fvg.balance_pct}%",
            "status": "FORMED",
            "desc": f"Vol {new_fvg.total_volume:.1f} ({new_fvg.balance_pct}%)"
        })
        logger.info(f"[{tf}] NEW {new_fvg.type} FVG: {new_fvg.bottom:.2f} - {new_fvg.top:.2f}")
        if not cfg.get("muted", False):
            sound_queue.append(('formed', active_settings.get("audio_preset", "Siren"), active_settings.get("audio_duration", 5)))
        toast_queue.append((f"[{tf.upper()}] New {new_fvg.type} FVG Formed!", 'positive' if new_fvg.type == "BULLISH" else 'negative'))

    render_grid.refresh()
    render_event_log.refresh()

async def clock_aligned_poller():
    """Silent background poller: avoids heavy candle-by-candle logging."""
    retries: Dict[str, int] = {tf: 0 for tf in TIMEFRAMES}

    while True:
        try:
            now_sec = int(time.time())
            current_symbol = active_settings["symbol"].upper()

            async with ClientSession() as session:
                for tf in TIMEFRAMES:
                    sec = TF_SECONDS[tf]
                    expected_open_sec = ((now_sec // sec) - 1) * sec
                    expected_open_ms = expected_open_sec * 1000
                    eng = engines[tf]

                    if eng.last_closed_bar_time < expected_open_ms:
                        api_interval = TF_BINANCE_MAP[tf]
                        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={current_symbol}&interval={api_interval}&limit=5"
                        async with session.get(url, timeout=8) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                matched = None
                                for candle in data[:-1]:
                                    if int(candle[0]) == expected_open_ms:
                                        matched = candle
                                        break

                                if matched:
                                    # Process candle silently without dumping routine candle logs
                                    await process_new_candle(tf, matched)
                                    retries[tf] = 0
                                else:
                                    retries[tf] += 1
                                    if retries[tf] > 12 and len(data) >= 2:
                                        last_closed = data[-2]
                                        if int(last_closed[0]) > eng.last_closed_bar_time:
                                            await process_new_candle(tf, last_closed)
                                            retries[tf] = 0
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Poller error: {e}")

        await asyncio.sleep(0.5)

async def switch_symbol(new_symbol: str):
    global clock_worker_task
    if new_symbol == active_settings["symbol"]:
        return
    logger.info(f"Switching contract to {new_symbol}")
    active_settings["symbol"] = new_symbol
    save_settings()
    toast_queue.append((f"Switching contract to {new_symbol}...", 'info'))
    if clock_worker_task and not clock_worker_task.done():
        clock_worker_task.cancel()
    await seed_historical()
    clock_worker_task = asyncio.create_task(clock_aligned_poller())
    render_grid.refresh()
    render_event_log.refresh()

# --- UI Presentation ---

def make_candles_html(candles_list: list) -> str:
    if not candles_list:
        return "<div style='color:#64748b; padding:8px; text-align:center;'>No candle data yet</div>"
    rows = ""
    for b in reversed(candles_list[-5:]):
        color = "#34d399" if b['c'] >= b['o'] else "#f87171"
        t_str = datetime.fromtimestamp(b['t'] / 1000, tz=IST).strftime("%H:%M:%S")
        rows += f"""
        <tr style="border-bottom: 1px solid rgba(51, 65, 85, 0.3);">
            <td style="padding: 4px 6px; color: #94a3b8;">{t_str}</td>
            <td style="padding: 4px 6px;">{b['o']:.1f}</td>
            <td style="padding: 4px 6px;">{b['h']:.1f}</td>
            <td style="padding: 4px 6px;">{b['l']:.1f}</td>
            <td style="padding: 4px 6px; color: {color}; font-weight: bold;">{b['c']:.1f}</td>
            <td style="padding: 4px 6px; color: #cbd5e1;">{int(b['v'])}</td>
        </tr>
        """
    return f"""
    <div style="overflow-x:auto;">
      <table style="width:100%; font-family:ui-monospace, monospace; font-size:11px; text-align:left; border-collapse:collapse;">
        <thead>
          <tr style="color:#64748b; border-bottom:1px solid #334155;">
            <th style="padding:4px 6px;">Time (IST)</th>
            <th style="padding:4px 6px;">O</th>
            <th style="padding:4px 6px;">H</th>
            <th style="padding:4px 6px;">L</th>
            <th style="padding:4px 6px;">C</th>
            <th style="padding:4px 6px;">Vol</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    """

@ui.refreshable
def render_grid():
    with ui.grid().classes('w-full grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-5'):
        for tf in TIMEFRAMES:
            eng = engines[tf]
            cfg = eng.get_cfg()
            is_muted = cfg.get("muted", False)
            active_open_fvgs = [f for f in eng.fvg_list if f.status == "ACTIVE"][:5]

            with ui.card().classes('bg-slate-900/95 border border-slate-800 p-4 rounded-xl shadow-xl flex flex-col justify-between'):
                # Card Header
                with ui.row().classes('w-full justify-between items-center border-b border-slate-800 pb-2.5 mb-2'):
                    with ui.row().classes('items-center gap-2'):
                        ui.label(tf.upper()).classes('text-xl font-black tracking-wider text-sky-400')
                        ui.badge(f"{len(eng.fvg_list)} Active", color='slate-800').classes('text-[11px] font-mono border border-slate-700')

                    with ui.row().classes('items-center gap-1'):
                        mute_icon = 'notifications_off' if is_muted else 'notifications_active'
                        mute_col = 'text-rose-400' if is_muted else 'text-emerald-400'
                        m_btn = ui.button(icon=mute_icon).props('flat round dense').classes(f'text-xs {mute_col}')
                        
                        def toggle_mute(target_tf=tf):
                            c = active_settings["timeframes"][target_tf]
                            c["muted"] = not c.get("muted", False)
                            save_settings()
                            render_grid.refresh()
                            ui.notify(f"{target_tf.upper()} alerts {'MUTED' if c['muted'] else 'UNMUTED'}", type='info')
                        m_btn.on('click', toggle_mute)

                        # Settings Modal
                        with ui.dialog() as diag, ui.card().classes('bg-slate-900 border border-slate-700 text-slate-200 p-5 w-96'):
                            ui.label(f"Settings — {tf.upper()}").classes('text-base font-bold text-sky-400 mb-2')
                            
                            f_method = ui.select(["Average Range", "Volume Threshold"], value=cfg.get("filter_method", "Volume Threshold"), label="Zone Filtering").classes('w-full')
                            v_thresh = ui.number(label="Volume Threshold %", value=cfg.get("vol_thresh_pct", 70), min=1, max=200).classes('w-full')
                            s_level = ui.select(["Extreme", "High", "Normal", "Low"], value=cfg.get("sens_level", "Low"), label="Sensitivity").classes('w-full')
                            s_enable = ui.checkbox("Enable Sensitivity Threshold", value=cfg.get("sens_enabled", True))
                            f_bars = ui.select(["Same Type", "All"], value=cfg.get("fvg_bars", "All"), label="FVG Detection Bars").classes('w-full')
                            e_method = ui.select(["Close", "Wick"], value=cfg.get("end_method", "Close"), label="Zone Invalidation").classes('w-full')
                            a_gaps = ui.checkbox("Allow Gaps Between Bars", value=cfg.get("allow_gaps", True))

                            with ui.row().classes('w-full justify-end gap-2 mt-4'):
                                ui.button("Cancel", on_click=diag.close).props('flat text-xs')
                                def save_tf_cfg(t=tf, d=diag, fm=f_method, vt=v_thresh, sl=s_level, se=s_enable, fb=f_bars, em=e_method, ag=a_gaps):
                                    active_settings["timeframes"][t].update({
                                        "filter_method": fm.value,
                                        "vol_thresh_pct": int(vt.value or 70),
                                        "sens_level": sl.value,
                                        "sens_enabled": bool(se.value),
                                        "fvg_bars": fb.value,
                                        "end_method": em.value,
                                        "allow_gaps": bool(ag.value),
                                    })
                                    save_settings()
                                    d.close()
                                    render_grid.refresh()
                                    ui.notify(f"Configuration saved for {t.upper()}", type='positive')
                                ui.button("Save", on_click=save_tf_cfg).props('unelevated color=primary text-xs')
                        
                        ui.button(icon='tune', on_click=diag.open).props('flat round dense').classes('text-xs text-slate-400 hover:text-white')

                # Dynamic Last 5 Completed Candles
                with ui.column().classes('w-full mb-3 bg-slate-950/80 p-2.5 rounded-lg border border-slate-800'):
                    with ui.row().classes('w-full justify-between items-center mb-1'):
                        ui.label('Last 5 Completed Candles').classes('text-xs font-bold text-slate-400')
                        ui.icon('update', size='xs').classes('text-slate-500')
                    ui.html(make_candles_html(list(eng.bars)))

                # Active Open FVGs (Up to 5)
                with ui.column().classes('w-full gap-2 max-h-56 overflow-y-auto pr-1'):
                    if not active_open_fvgs:
                        ui.label("No active zones").classes('text-xs text-slate-500 italic py-3 text-center w-full')
                    else:
                        for f in active_open_fvgs:
                            border_col = 'border-emerald-500 bg-emerald-950/30' if f.type == "BULLISH" else 'border-rose-500 bg-rose-950/30'
                            text_col = 'text-emerald-400' if f.type == "BULLISH" else 'text-rose-400'

                            with ui.element('div').classes(f'w-full p-2.5 rounded-lg border-l-4 {border_col} flex justify-between items-center shadow'):
                                with ui.column().classes('gap-0.5'):
                                    with ui.row().classes('items-center gap-1.5'):
                                        ui.label(f.type).classes(f'text-xs font-black {text_col}')
                                        if f.combined:
                                            ui.badge('COMBINED', color='amber-700').classes('text-[9px] px-1 py-0 font-bold')
                                    ui.label(f"{f.bottom:.2f} — {f.top:.2f}").classes('text-xs font-mono font-bold text-slate-200')
                                    ui.label(f"Formed: {f.timestamp_ist}").classes('text-[10px] text-slate-400 font-mono')
                                
                                with ui.column().classes('items-end gap-0.5'):
                                    ui.label(f"Vol: {f.total_volume:.1f}").classes('text-[11px] font-mono text-slate-300 font-semibold')
                                    ui.badge(f"{f.balance_pct}% Bal", color='slate-800').classes('text-[10px] text-slate-400 border border-slate-700')

@ui.refreshable
def render_event_log():
    global selected_event_tf
    with ui.card().classes('w-full bg-slate-900 border border-slate-800 p-5 rounded-xl shadow-xl mt-2'):
        # Header with Timeframe Filter Bar
        with ui.row().classes('w-full justify-between items-center mb-3 flex-wrap gap-3'):
            with ui.row().classes('items-center gap-2'):
                ui.icon('history', size='sm').classes('text-sky-400')
                ui.label('Live FVG Event Stream').classes('text-base font-bold text-white')
                ui.label(f"Persistent CSV: {CSV_FILE}").classes('text-xs font-mono text-slate-500 hidden sm:inline ml-2')

            # Interactive Timeframe Filter Buttons
            with ui.row().classes('items-center gap-2'):
                ui.label('Filter:').classes('text-xs text-slate-400 font-bold uppercase tracking-wider')
                def set_filter(e):
                    global selected_event_tf
                    selected_event_tf = e.value
                    render_event_log.refresh()
                ui.toggle(
                    ["ALL"] + TIMEFRAMES,
                    value=selected_event_tf,
                    on_change=set_filter
                ).props('dense no-caps toggle-color=sky-600').classes('text-xs font-mono bg-slate-950/80 border border-slate-800 rounded-lg')

        # Filter events based on active selection
        filtered_events = [
            ev for ev in list(event_log)
            if selected_event_tf == "ALL" or ev['tf'].upper() == selected_event_tf.upper()
        ][:20]

        if not filtered_events:
            msg = f"No events recorded for {selected_event_tf}..." if selected_event_tf != "ALL" else "Waiting for bar-close detection events..."
            ui.label(msg).classes('text-xs text-slate-500 italic py-4 text-center w-full')
        else:
            with ui.column().classes('w-full gap-2 font-mono text-xs'):
                for ev in filtered_events:
                    is_formed = ev['status'] == "FORMED"
                    badge_col = 'bg-emerald-900/60 text-emerald-300 border border-emerald-700' if is_formed else 'bg-rose-950/70 text-rose-300 border border-rose-800'
                    type_col = 'text-emerald-400' if ev['type'] == "BULLISH" else 'text-rose-400'

                    with ui.element('div').classes('w-full p-2.5 rounded-lg bg-slate-950/80 border border-slate-800 flex flex-wrap items-center justify-between gap-2'):
                        with ui.row().classes('items-center gap-2'):
                            ui.label(f"[{ev['time_ist']}]").classes('text-slate-400 font-bold')
                            ui.label(active_settings['symbol']).classes('text-amber-400 font-bold')
                            ui.badge(ev['tf'].upper(), color='sky-900').classes('text-xs font-bold')
                            ui.label(ev['type']).classes(f'font-black {type_col}')
                            ui.badge(ev['status']).classes(f'text-[10px] px-1.5 py-0.5 {badge_col}')
                        with ui.row().classes('items-center gap-3 text-right'):
                            ui.label(ev['range']).classes('text-slate-200 font-semibold')
                            ui.label(ev['desc']).classes('text-slate-400')

@ui.page('/')
async def index():
    ui.dark_mode().enable()
    ui.add_head_html(f"<script>{AUDIO_UNLOCK_JS}</script>")

    # Decoupled Queue Processor (Processes audio and popups in client context)[cite: 1]
    def process_queues():
        while toast_queue:
            msg, t_type = toast_queue.pop(0)
            ui.notify(msg, type=t_type, position='top-right')

        while sound_queue:
            _, s_name, s_dur = sound_queue.pop(0)
            url = SOUND_PRESETS.get(s_name, SOUND_PRESETS["Siren"])
            try:
                dur_ms = max(1000, min(15000, int(float(s_dur or 5) * 1000)))
            except:
                dur_ms = 5000
            js_code = f"""
            try {{
                const a = new Audio("{url}");
                a.loop = true;
                a.play().catch(e => console.log("Audio play blocked by browser:", e));
                setTimeout(() => {{
                    a.pause();
                    a.currentTime = 0;
                }}, {dur_ms});
            }} catch(e) {{
                console.error(e);
            }}
            """
            ui.run_javascript(js_code)

    ui.timer(0.5, process_queues)

    with ui.header().classes('bg-slate-950 border-b border-slate-800 px-6 py-3 flex justify-between items-center flex-wrap gap-4 shadow-md'):
        with ui.row().classes('items-center gap-4'):
            with ui.row().classes('items-center gap-2'):
                ui.icon('radar', size='md').classes('text-amber-400 animate-pulse')
                ui.label('Volumized FVG Radar').classes('text-lg font-black text-white tracking-wider')

            sym_select = ui.select(top_50_symbols, value=active_settings["symbol"], label="Binance Futures Symbol").classes('w-48 text-xs font-mono').props('dense outlined options-dense')
            sym_select.on('update:value', lambda e: asyncio.create_task(switch_symbol(e.value)))

        with ui.row().classes('items-center gap-3 flex-wrap'):
            preset_select = ui.select(list(SOUND_PRESETS.keys()), value=active_settings.get("audio_preset", "Siren"), label="Alert Tone").classes('w-36 text-xs').props('dense outlined options-dense')
            dur_input = ui.number(label="Duration (s)", value=active_settings.get("audio_duration", 5), min=1, max=15).classes('w-24 text-xs').props('dense outlined')

            # Preview Button
            preview_btn = ui.button('Preview Sound', icon='play_arrow').props('dense unelevated').classes('bg-slate-800 hover:bg-slate-700 text-xs px-2.5 py-1.5')
            def test_preview():
                s_name = preset_select.value
                s_dur = dur_input.value or 5
                url = SOUND_PRESETS.get(s_name, SOUND_PRESETS["Siren"])
                try:
                    dur_ms = max(1000, min(15000, int(float(s_dur) * 1000)))
                except:
                    dur_ms = 5000
                js_code = f"""
                try {{
                    const a = new Audio("{url}");
                    a.loop = true;
                    a.play().catch(e => console.log("Audio play blocked:", e));
                    setTimeout(() => {{
                        a.pause();
                        a.currentTime = 0;
                    }}, {dur_ms});
                }} catch(e) {{
                    console.error(e);
                }}
                """
                ui.run_javascript(js_code)
                ui.notify(f"Previewing: {s_name} ({s_dur}s)", type='info')
            preview_btn.on('click', test_preview)

            # Set Sound Button
            set_sound_btn = ui.button('Set Sound', icon='check').props('dense unelevated').classes('bg-sky-600 hover:bg-sky-500 text-xs px-3 py-1.5 font-bold')
            def save_sound():
                active_settings["audio_preset"] = preset_select.value
                active_settings["audio_duration"] = int(dur_input.value or 5)
                save_settings()
                ui.notify(f"Saved sound: {preset_select.value} ({dur_input.value}s)", type='positive')
            set_sound_btn.on('click', save_sound)

            # Autoplay Unlock Button[cite: 1]
            enable_snd_btn = ui.button('🔊 Enable Sound', icon='volume_up').props('dense unelevated').classes('bg-emerald-700 hover:bg-emerald-600 text-xs px-3 py-1.5 font-bold')
            def unlock_sound():
                ui.run_javascript('try { const a = new Audio("https://actions.google.com/sounds/v1/cartoon/pop.ogg"); a.volume = 0.4; a.play().catch(()=>{}); } catch(e) {}')
                ui.notify("Browser audio unlocked successfully!", type='positive')
            enable_snd_btn.on('click', unlock_sound)

    with ui.column().classes('w-full max-w-7xl mx-auto p-4 md:p-6 gap-6'):
        render_grid()
        render_event_log()

async def on_startup():
    logger.info("Initializing system...")
    await init_csv()
    load_recent_events_from_csv()
    await fetch_top_50_symbols()
    await seed_historical()
    global clock_worker_task
    clock_worker_task = asyncio.create_task(clock_aligned_poller())

app.on_startup(on_startup)

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(port=PORT, title="Volumized FVG Radar — Binance Futures", reload=False, show=False)
