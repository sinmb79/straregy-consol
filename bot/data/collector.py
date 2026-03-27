"""
BinanceCollector — fetches candles, tickers, funding rates via REST + WebSocket.

REST endpoints used:
  GET /fapi/v1/klines          — historical candles
  GET /fapi/v1/premiumIndex    — mark price + funding rate
  GET /fapi/v2/ticker/24hr     — 24h ticker
  GET /fapi/v1/openInterest    — open interest

WebSocket streams used:
  <symbol>@aggTrade  (or @ticker) — real-time price
  !miniTicker@arr                 — all-market mini-tickers
"""

import asyncio
import json
import logging
import time
from typing import Dict, List, Optional

import httpx
import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from bot.config import Config
from bot.data.store import DataStore

logger = logging.getLogger(__name__)

RECONNECT_DELAY = 5   # seconds before WebSocket reconnect
REST_TIMEOUT = 15     # seconds


class BinanceCollector:
    """Collects market data from Binance Futures and writes it to DataStore."""

    def __init__(self, config: Config, store: DataStore) -> None:
        self._config = config
        self._store = store
        self._running = False
        self._http: Optional[httpx.AsyncClient] = None
        self._tasks: List[asyncio.Task] = []

    # ---------------------------------------------------------------------- #
    # Lifecycle
    # ---------------------------------------------------------------------- #

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._http = httpx.AsyncClient(
            base_url=self._config.binance_rest_base,
            timeout=REST_TIMEOUT,
        )
        logger.info("BinanceCollector starting …")

        # Fetch historical candles for all symbols/intervals on startup
        await self._fetch_all_history()

        # Mark exchange as reachable
        self._store.set_exchange_status(True)

        # Launch background tasks
        self._tasks = [
            asyncio.create_task(self._rest_poller(), name="rest_poller"),
            asyncio.create_task(self._ws_mini_ticker(), name="ws_mini_ticker"),
        ]
        logger.info("BinanceCollector started.")

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._http:
            await self._http.aclose()
        logger.info("BinanceCollector stopped.")

    async def reconnect(self) -> None:
        """Restart all connections with current config (e.g. after testnet/mainnet toggle)."""
        logger.info(
            "BinanceCollector reconnecting (testnet=%s) …", self._config.binance_testnet
        )
        # Cancel running tasks
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        # Recreate HTTP client with new base URL
        if self._http:
            await self._http.aclose()
        self._http = httpx.AsyncClient(
            base_url=self._config.binance_rest_base,
            timeout=REST_TIMEOUT,
        )
        # Restart tasks
        self._tasks = [
            asyncio.create_task(self._rest_poller(), name="rest_poller"),
            asyncio.create_task(self._ws_mini_ticker(), name="ws_mini_ticker"),
        ]
        logger.info("BinanceCollector reconnected.")

    # ---------------------------------------------------------------------- #
    # Historical REST fetch
    # ---------------------------------------------------------------------- #

    async def _fetch_all_history(self) -> None:
        """Fetch candle history for every tracked symbol × interval."""
        coros = [
            self._fetch_candles(symbol, interval)
            for symbol in self._config.tracked_symbols
            for interval in self._config.candle_intervals
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)
        for res in results:
            if isinstance(res, Exception):
                logger.error("History fetch error: %s", res)

    async def _fetch_candles(self, symbol: str, interval: str) -> None:
        """Fetch historical klines and write to store."""
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": self._config.candle_limit,
        }
        try:
            resp = await self._http.get("/fapi/v1/klines", params=params)
            resp.raise_for_status()
            raw: List[list] = resp.json()
        except Exception as exc:
            logger.error("Candle fetch failed %s/%s: %s", symbol, interval, exc)
            return

        for kline in raw:
            candle = _parse_kline(kline)
            await self._store.upsert_candle(symbol, interval, candle)

        logger.info(
            "Fetched %d %s/%s candles from Binance REST", len(raw), symbol, interval
        )

    # ---------------------------------------------------------------------- #
    # REST poller (funding rates, open interest, tickers)
    # ---------------------------------------------------------------------- #

    async def _rest_poller(self) -> None:
        """Periodically poll REST endpoints for funding / OI / 24h tickers."""
        while self._running:
            try:
                await self._poll_once()
            except Exception as exc:
                logger.error("REST poll error: %s", exc)
            await asyncio.sleep(self._config.ticker_update_interval_sec * 6)  # every 30 s

    async def _poll_once(self) -> None:
        symbols = self._config.tracked_symbols
        tasks = [self._fetch_funding_and_ticker(s) for s in symbols]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_funding_and_ticker(self, symbol: str) -> None:
        # Premium index gives funding rate + mark price
        try:
            resp = await self._http.get(
                "/fapi/v1/premiumIndex", params={"symbol": symbol}
            )
            resp.raise_for_status()
            data = resp.json()
            funding = float(data.get("lastFundingRate", 0))
            await self._store.update_funding(symbol, funding)
        except Exception as exc:
            logger.debug("Funding fetch %s: %s", symbol, exc)

        # Open interest
        try:
            resp = await self._http.get(
                "/fapi/v1/openInterest", params={"symbol": symbol}
            )
            resp.raise_for_status()
            data = resp.json()
            oi = float(data.get("openInterest", 0))
            await self._store.update_open_interest(symbol, oi)
        except Exception as exc:
            logger.debug("OI fetch %s: %s", symbol, exc)

        # 24h ticker
        try:
            resp = await self._http.get(
                "/fapi/v1/ticker/24hr", params={"symbol": symbol}
            )
            resp.raise_for_status()
            data = resp.json()
            ticker = {
                "symbol": symbol,
                "ts": int(time.time() * 1000),
                "price": float(data.get("lastPrice", 0)),
                "volume_24h": float(data.get("quoteVolume", 0)),
                "change_pct": float(data.get("priceChangePercent", 0)),
            }
            await self._store.update_ticker(symbol, ticker)
        except Exception as exc:
            logger.debug("Ticker fetch %s: %s", symbol, exc)

    # ---------------------------------------------------------------------- #
    # WebSocket — mini ticker stream (all symbols real-time)
    # ---------------------------------------------------------------------- #

    async def _ws_mini_ticker(self) -> None:
        """Subscribe to !miniTicker@arr for real-time price updates."""
        while self._running:
            # Re-evaluate URL each iteration so testnet toggle takes effect on reconnect
            url = f"{self._config.binance_ws_base}/stream?streams=!miniTicker@arr"
            symbols_set = set(self._config.tracked_symbols)
            try:
                logger.info("WebSocket connecting: %s", url)
                async with websockets.connect(url, ping_interval=20, ping_timeout=30) as ws:
                    self._store.set_exchange_status(True)
                    logger.info("WebSocket connected.")
                    async for message in ws:
                        if not self._running:
                            break
                        await self._handle_mini_ticker(message, symbols_set)
            except (ConnectionClosedError, ConnectionClosedOK) as exc:
                logger.warning("WebSocket closed: %s — reconnecting in %ds", exc, RECONNECT_DELAY)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("WebSocket error: %s — reconnecting in %ds", exc, RECONNECT_DELAY)
                self._store.set_exchange_status(False)

            if self._running:
                await asyncio.sleep(RECONNECT_DELAY)

    async def _handle_mini_ticker(self, message: str, symbols_set: set) -> None:
        try:
            data = json.loads(message)
            # data is {"stream": "...", "data": [...]}
            items = data.get("data", data) if isinstance(data, dict) else data
            if not isinstance(items, list):
                return
            for item in items:
                symbol = item.get("s", "")
                if symbol not in symbols_set:
                    continue
                ticker = {
                    "symbol": symbol,
                    "ts": item.get("E", int(time.time() * 1000)),
                    "price": float(item.get("c", 0)),
                    "volume_24h": float(item.get("q", 0)),
                    "change_pct": _calc_change_pct(
                        float(item.get("o", 0)), float(item.get("c", 0))
                    ),
                }
                await self._store.update_ticker(symbol, ticker)
        except Exception as exc:
            logger.debug("Mini-ticker parse error: %s", exc)

    # ---------------------------------------------------------------------- #
    # Live candle updates via WebSocket (kline stream)
    # ---------------------------------------------------------------------- #

    async def start_kline_streams(self) -> None:
        """Subscribe to kline streams for real-time candle updates."""
        streams = [
            f"{s.lower()}@kline_{i}"
            for s in self._config.tracked_symbols
            for i in self._config.candle_intervals
        ]
        url = (
            f"{self._config.binance_ws_base}/stream?streams=" + "/".join(streams)
        )

        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=30) as ws:
                    logger.info("Kline stream connected (%d streams)", len(streams))
                    async for message in ws:
                        if not self._running:
                            break
                        await self._handle_kline(message)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Kline stream error: %s — reconnecting in %ds", exc, RECONNECT_DELAY)
            if self._running:
                await asyncio.sleep(RECONNECT_DELAY)

    async def _handle_kline(self, message: str) -> None:
        try:
            data = json.loads(message)
            kline_data = data.get("data", {}).get("k", {})
            if not kline_data:
                return
            symbol = kline_data.get("s", "")
            interval = kline_data.get("i", "")
            candle = {
                "ts": int(kline_data.get("t", 0)),
                "o": float(kline_data.get("o", 0)),
                "h": float(kline_data.get("h", 0)),
                "l": float(kline_data.get("l", 0)),
                "c": float(kline_data.get("c", 0)),
                "v": float(kline_data.get("v", 0)),
            }
            await self._store.upsert_candle(symbol, interval, candle)
        except Exception as exc:
            logger.debug("Kline parse error: %s", exc)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _parse_kline(kline: list) -> dict:
    """Convert Binance REST kline array to candle dict."""
    return {
        "ts": int(kline[0]),
        "o": float(kline[1]),
        "h": float(kline[2]),
        "l": float(kline[3]),
        "c": float(kline[4]),
        "v": float(kline[5]),
    }


def _calc_change_pct(open_price: float, close_price: float) -> float:
    if open_price == 0:
        return 0.0
    return round((close_price - open_price) / open_price * 100, 4)
