"""
TradingView Lightweight Charts via JS bridge.
Indicators calculated in Python with pandas-ta, pushed as LineSeries.
Chart lib loaded from CDN in main.py head.
"""
from __future__ import annotations
import json
import logging
from typing import TYPE_CHECKING

import pandas as pd
import pandas_ta as ta
from nicegui import ui

from common import settings

if TYPE_CHECKING:
    from ui.state import UIState

logger = logging.getLogger('ui.price_chart')

_INDICATORS = {
    'EMA 20':  {'fn': lambda df: ta.ema(df['c'], length=20),  'color': '#3b82f6'},
    'EMA 50':  {'fn': lambda df: ta.ema(df['c'], length=50),  'color': '#f59e0b'},
    'EMA 200': {'fn': lambda df: ta.ema(df['c'], length=200), 'color': '#8b5cf6'},
    'BB':      {'fn': lambda df: ta.bbands(df['c'], length=20), 'color': '#6b7280', 'multi': True},
}

_INIT_JS = """
(function(){
  var el = document.getElementById('price-chart');
  if (!el || window.lwChart) return;
  window.lwChart = LightweightCharts.createChart(el, {
    width: el.offsetWidth, height: 380,
    layout: { background:{color:'#111827'}, textColor:'#9ca3af' },
    grid:   { vertLines:{color:'#1f2937'}, horzLines:{color:'#1f2937'} },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor:'#374151' },
    timeScale: { borderColor:'#374151', timeVisible:true, secondsVisible:false },
  });
  window.lwCandles = window.lwChart.addCandlestickSeries({
    upColor:'#22c55e', downColor:'#ef4444',
    borderUpColor:'#22c55e', borderDownColor:'#ef4444',
    wickUpColor:'#22c55e', wickDownColor:'#ef4444',
  });
  window.lwVol = window.lwChart.addHistogramSeries({
    priceFormat:{type:'volume'}, priceScaleId:'vol',
    scaleMargins:{top:0.82, bottom:0},
  });
  window.lwIndicators = {};
  window.addEventListener('resize', function(){
    window.lwChart.resize(el.offsetWidth, 380);
  });
})();
"""


def build(state: 'UIState', shared: dict) -> dict:
    active_inds: set[str] = set()
    initialized           = {'ok': False}
    last_ts               = {'v': None}

    with ui.card().classes('w-full bg-gray-900 p-0 rounded-lg overflow-hidden'):

        # Toolbar
        with ui.row().classes('w-full items-center px-3 py-2 bg-gray-800 gap-2 flex-wrap'):
            ui.label('Price Chart').classes('text-gray-300 text-sm font-medium')

            iv = ui.select(
                options=settings.CHART_INTERVALS, value=state.watch_interval,
            ).props('dense dark outlined label=Interval').classes('w-20')

            def on_interval(e):
                state.watch_interval = e.args
                last_ts['v'] = None
                _reset_series()
            iv.on('update:model-value', on_interval)

            ui.label('Indicators:').classes('text-gray-500 text-xs ml-2')
            for name in _INDICATORS:
                btn = ui.button(name).props('unelevated size=xs').classes('bg-gray-700 text-gray-300 text-xs')

                def make_toggle(n, b):
                    def toggle():
                        if n in active_inds:
                            active_inds.discard(n)
                            b.classes(remove='bg-blue-700', add='bg-gray-700')
                            ui.run_javascript(
                                f"if(window.lwIndicators['{n}']){{window.lwChart.removeSeries(window.lwIndicators['{n}']);delete window.lwIndicators['{n}']}}"
                            )
                        else:
                            active_inds.add(n)
                            b.classes(remove='bg-gray-700', add='bg-blue-700')
                            _push_indicator(n, state)
                    return toggle

                btn.on('click', make_toggle(name, btn))

        # Chart container div
        container = ui.element('div').style('width:100%;height:380px;')
        container._props['id'] = 'price-chart'

    def _reset_series():
        ui.run_javascript("""
          if(window.lwCandles){ window.lwChart.removeSeries(window.lwCandles); }
          if(window.lwVol){     window.lwChart.removeSeries(window.lwVol); }
          Object.values(window.lwIndicators||{}).forEach(s=>window.lwChart.removeSeries(s));
          window.lwIndicators={};
          window.lwCandles = window.lwChart.addCandlestickSeries({
            upColor:'#22c55e',downColor:'#ef4444',
            borderUpColor:'#22c55e',borderDownColor:'#ef4444',
            wickUpColor:'#22c55e',wickDownColor:'#ef4444',
          });
          window.lwVol = window.lwChart.addHistogramSeries({
            priceFormat:{type:'volume'},priceScaleId:'vol',
            scaleMargins:{top:0.82,bottom:0},
          });
        """)

    def _push_indicator(name: str, state: 'UIState') -> None:
        candles = state.get_candles()
        if len(candles) < 20:
            return
        try:
            df = pd.DataFrame(candles)
            # Compact keys from publisher: e,s,i,o,h,l,c,v,ts
            df = df.rename(columns={'o':'o','h':'h','l':'l','c':'c','v':'v','ts':'ts'})
            df['c'] = pd.to_numeric(df.get('c', df.get('close', 0)))
            cfg     = _INDICATORS[name]
            result  = cfg['fn'](df)
            color   = cfg['color']

            if cfg.get('multi') and isinstance(result, pd.DataFrame):
                # Bollinger: BBL, BBM, BBU
                for col in result.columns:
                    series_name = f"{name}_{col}"
                    vals = result[col].dropna()
                    data = [{'time': int(pd.Timestamp(candles[i]['ts']).timestamp()),
                             'value': round(float(v), 4)}
                            for i, v in zip(vals.index, vals) if i < len(candles)]
                    _upsert_line(series_name, color, data)
            else:
                vals = result.dropna()
                data = [{'time': int(pd.Timestamp(candles[i]['ts']).timestamp()),
                         'value': round(float(v), 4)}
                        for i, v in zip(vals.index, vals) if i < len(candles)]
                _upsert_line(name, color, data)

        except Exception as ex:
            logger.warning(f"Indicator {name}: {ex}")

    def _upsert_line(name: str, color: str, data: list) -> None:
        js = f"""
          if(!window.lwIndicators['{name}']){{
            window.lwIndicators['{name}']=window.lwChart.addLineSeries({{
              color:'{color}',lineWidth:1,priceLineVisible:false,lastValueVisible:false,
            }});
          }}
          window.lwIndicators['{name}'].setData({json.dumps(data)});
        """
        ui.run_javascript(js)

    def update():
        # Init chart once DOM is ready
        if not initialized['ok']:
            ui.run_javascript(_INIT_JS)
            initialized['ok'] = True

        candles = state.get_candles()
        if not candles:
            return

        latest = candles[-1]
        ts_str  = latest.get('ts')
        if not ts_str:
            return

        try:
            ts = int(pd.Timestamp(ts_str).timestamp())
            bar = {
                'time':  ts,
                'open':  float(latest.get('o', 0)),
                'high':  float(latest.get('h', 0)),
                'low':   float(latest.get('l', 0)),
                'close': float(latest.get('c', 0)),
            }
            vol = {
                'time':  ts,
                'value': float(latest.get('v', 0)),
                'color': '#22c55e' if bar['close'] >= bar['open'] else '#ef4444',
            }
            ui.run_javascript(
                f"if(window.lwCandles){{window.lwCandles.update({json.dumps(bar)});}}"
                f"if(window.lwVol){{window.lwVol.update({json.dumps(vol)});}}"
            )

            # Refresh active indicators on each new candle timestamp
            if ts_str != last_ts['v']:
                last_ts['v'] = ts_str
                for name in active_inds:
                    _push_indicator(name, state)

        except Exception as ex:
            logger.debug(f"Chart update: {ex}")

    return {'update': update}
