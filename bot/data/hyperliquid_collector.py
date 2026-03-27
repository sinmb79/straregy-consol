"""
HyperliquidCollector — 하이퍼리퀴드 DEX에서 OHLCV 캔들 수집.

BinanceCollector와 병렬 실행. HYPERLIQUID_ENABLED=true 시 활성화.
DataStore에 "HL_{symbol}" 키로 저장하여 기존 Binance 데이터와 구분.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from bot.config import Config
    from bot.data.store import DataStore

logger = logging.getLogger(__name__)

# Binance symbol → Hyperliquid coin name
_COIN_MAP: Dict[str, str] = {
    "BTCUSDT":  "BTC",
    "ETHUSDT":  "ETH",
    "SOLUSDT":  "SOL",
    "BNBUSDT":  "BNB",
    "XRPUSDT":  "XRP",
    "DOGEUSDT": "DOGE",
    "ADAUSDT":  "ADA",
    "AVAXUSDT": "AVAX",
    "SUIUSDT":  "SUI",
    "PEPEUSDT": "PEPE",
    "WIFUSDT":  "WIF",
}

# interval → Hyperliquid resolution string
_INTERVAL_MAP: Dict[str, str] = {
    "1m": "1m", "5m": "5m", "15m": "15m",
    "1h": "1h", "4h": "4h", "1d": "1d",
}

# ms per bar for start_time calculation
_MS_PER_BAR: Dict[str, int] = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}

POLL_INTERVAL_SEC = 60


class HyperliquidCollector:
    """
    하이퍼리퀴드 메인넷 캔들 데이터 주기적 수집기.
    """

    def __init__(self, config: "Config", store: "DataStore") -> None:
        self._config = config
        self._store = store
        self._info = None
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        try:
            from hyperliquid.info import Info
            from hyperliquid.utils import constants
            loop = asyncio.get_event_loop()
            self._info = await loop.run_in_executor(
                None,
                lambda: Info(constants.MAINNET_API_URL, skip_ws=True),
            )
            logger.info("[HyperliquidCollector] Hyperliquid 메인넷 연결 완료.")
        except Exception as exc:
            logger.error("[HyperliquidCollector] 초기화 실패: %s", exc)
            return

        # 초기 전체 캔들 수집
        await self._fetch_all()

        # 주기적 폴링 태스크 시작
        self._task = asyncio.create_task(self._poll_loop(), name="hl_candle_poll")
        logger.info(
            "[HyperliquidCollector] 폴링 시작 (간격=%ds).", POLL_INTERVAL_SEC
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[HyperliquidCollector] 중지됨.")

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                await self._fetch_all()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("[HyperliquidCollector] 폴 오류: %s", exc)

    async def _fetch_all(self) -> None:
        symbols = self._config.tracked_symbols
        intervals = self._config.candle_intervals
        limit = getattr(self._config, "candle_limit", 200)
        loop = asyncio.get_event_loop()

        for symbol in symbols:
            coin = _COIN_MAP.get(symbol)
            if not coin:
                logger.debug(
                    "[HyperliquidCollector] 심볼 매핑 없음: %s — 건너뜀", symbol
                )
                continue

            for interval in intervals:
                hl_res = _INTERVAL_MAP.get(interval)
                if not hl_res:
                    continue

                try:
                    candles = await loop.run_in_executor(
                        None,
                        self._fetch_candles_sync,
                        coin, hl_res, limit,
                    )
                    for candle in candles:
                        await self._store.upsert_candle(f"HL_{symbol}", interval, candle)

                    if candles:
                        logger.debug(
                            "[HyperliquidCollector] %s/%s: %d개 캔들 저장.",
                            symbol, interval, len(candles),
                        )
                except Exception as exc:
                    logger.warning(
                        "[HyperliquidCollector] %s/%s 수집 오류: %s",
                        symbol, interval, exc,
                    )

    def _fetch_candles_sync(self, coin: str, resolution: str, limit: int) -> List[dict]:
        """동기 캔들 수집 — run_in_executor로 실행."""
        if self._info is None:
            return []

        end_time = int(time.time() * 1000)
        ms_per_bar = _MS_PER_BAR.get(resolution, 3_600_000)
        start_time = end_time - limit * ms_per_bar

        try:
            raw = self._info.candles_snapshot(coin, resolution, start_time, end_time)
        except Exception as exc:
            logger.warning(
                "[HyperliquidCollector] candles_snapshot(%s, %s) 오류: %s",
                coin, resolution, exc,
            )
            return []

        candles: List[dict] = []
        for bar in (raw or []):
            try:
                candles.append({
                    "ts": int(bar["t"]),
                    "o":  float(bar["o"]),
                    "h":  float(bar["h"]),
                    "l":  float(bar["l"]),
                    "c":  float(bar["c"]),
                    "v":  float(bar["v"]),
                })
            except (KeyError, TypeError, ValueError):
                continue

        return candles
