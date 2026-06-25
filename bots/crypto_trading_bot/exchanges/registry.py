from exchanges.base import BaseExchangeAdapter
from exchanges.binance_adapter import BinanceAdapter
from exchanges.binance_futures_adapter import BinanceFuturesAdapter
from exchanges.delta_adapter import DeltaAdapter

ADAPTERS: dict[str, type[BaseExchangeAdapter]] = {
    'binance':         BinanceAdapter,
    'binance_futures': BinanceFuturesAdapter,
    'delta':           DeltaAdapter,
}


def get_adapter(
    exchange: str, api_key: str, api_secret: str, testnet: bool = False
) -> BaseExchangeAdapter:
    cls = ADAPTERS.get(exchange.lower())
    if not cls:
        raise ValueError(
            f"Unsupported exchange: '{exchange}'. Supported: {list(ADAPTERS.keys())}"
        )
    return cls(api_key=api_key, api_secret=api_secret, testnet=testnet)
