"""
Fast Layer v1.3 PART 8.4.

Short-horizon warning layer added on top of the slower regime detector.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Dict, List

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from bot.data.store import DataStore

logger = logging.getLogger(__name__)

VOLATILITY_SPIKE_MULT = 2.0
MOMENTUM_BURST_PCT = 1.5
SPREAD_EXPANSION_MULT = 1.8
OI_CHANGE_PCT_THRESHOLD = 5.0
FUNDING_SURGE_THRESHOLD = 0.0005
PRICE_DISLOCATION_THRESHOLD = 0.012


class FastLayer:
    def __init__(self, store: "DataStore") -> None:
        self._store = store
        self._oi_snapshot: Dict[str, float] = {}
        self._oi_snapshot_ts: int = 0

    def compute(self, symbol: str = "BTCUSDT") -> dict:
        ts = int(time.time() * 1000)
        signals: List[str] = []
        warning_tags: List[str] = []

        vol_spike = self._check_volatility_spike(symbol)
        mom_burst = self._check_momentum_burst(symbol)
        spread_det = self._check_spread_deterioration(symbol)
        oi_rapid = self._check_rapid_oi_change(symbol)
        funding_surg = self._check_funding_surge(symbol)
        funding_dislocation = self._check_funding_dislocation(symbol)
        market_dislocation = self._check_market_dislocation(vol_spike, spread_det)
        liquidation_proxy = self._check_liquidation_risk_proxy(
            mom_burst, vol_spike, funding_surg, oi_rapid
        )
        price_vwap_dislocation = self._check_price_vwap_dislocation(symbol)
        oi_funding_crowding = oi_rapid and funding_surg

        if vol_spike:
            signals.append("15m_volatility_spike")
        if mom_burst:
            signals.append("1h_momentum_burst")
        if spread_det:
            signals.append("15m_spread_deterioration")
        if oi_rapid:
            signals.append("rapid_oi_change")
        if funding_surg:
            signals.append("funding_surge")
        if funding_dislocation:
            signals.append("funding_dislocation")
        if market_dislocation:
            signals.append("market_dislocation")
        if liquidation_proxy:
            signals.append("liquidation_risk_proxy")
        if price_vwap_dislocation:
            signals.append("price_vwap_dislocation")

        if oi_funding_crowding:
            warning_tags.append("oi_funding_crowding")
        if spread_det:
            warning_tags.append("spread_stress")
        if price_vwap_dislocation:
            warning_tags.append("price_dislocation")

        n = len(signals) + len(warning_tags)
        if n == 0:
            alert_level = "NONE"
        elif n == 1:
            alert_level = "WARN"
        else:
            alert_level = "CAUTION"

        if alert_level != "NONE":
            logger.info("[FastLayer] %s alert=%s signals=%s", symbol, alert_level, signals)

        return {
            "15m_volatility_spike": vol_spike,
            "1h_momentum_burst": mom_burst,
            "15m_spread_deterioration": spread_det,
            "rapid_oi_change": oi_rapid,
            "funding_surge": funding_surg,
            "funding_dislocation": funding_dislocation,
            "market_dislocation": market_dislocation,
            "liquidation_risk_proxy": liquidation_proxy,
            "price_vwap_dislocation": price_vwap_dislocation,
            "oi_funding_crowding": oi_funding_crowding,
            "liquidation_signal_available": False,
            "oi_baseline_ready": symbol in self._oi_snapshot,
            "warning_summary": self._build_warning_summary(signals, alert_level),
            "alert_level": alert_level,
            "signals": signals,
            "warning_tags": warning_tags,
            "unavailable_signals": ["liquidation_feed"],
            "ts": ts,
        }

    def _check_volatility_spike(self, symbol: str) -> bool:
        candles = self._store.get_candles(symbol, "15m", limit=30)
        if len(candles) < 22:
            return False
        try:
            df = pd.DataFrame(candles).sort_values("ts").reset_index(drop=True)
            high = df["h"].astype(float)
            low = df["l"].astype(float)
            close = df["c"].astype(float)

            tr = pd.concat(
                [
                    high - low,
                    (high - close.shift(1)).abs(),
                    (low - close.shift(1)).abs(),
                ],
                axis=1,
            ).max(axis=1)
            atr = tr.rolling(14).mean()
            atr_cur = float(atr.iloc[-1])
            atr_avg = float(atr.iloc[-21:-1].mean())
            return atr_avg > 0 and atr_cur >= atr_avg * VOLATILITY_SPIKE_MULT
        except Exception as exc:
            logger.debug("[FastLayer] volatility_spike error: %s", exc)
            return False

    def _check_momentum_burst(self, symbol: str) -> bool:
        candles = self._store.get_candles(symbol, "1h", limit=5)
        if len(candles) < 2:
            return False
        try:
            df = pd.DataFrame(candles).sort_values("ts").reset_index(drop=True)
            close = df["c"].astype(float)
            ret_1h = abs((float(close.iloc[-1]) - float(close.iloc[-2])) / float(close.iloc[-2]) * 100)
            return ret_1h >= MOMENTUM_BURST_PCT
        except Exception as exc:
            logger.debug("[FastLayer] momentum_burst error: %s", exc)
            return False

    def _check_spread_deterioration(self, symbol: str) -> bool:
        candles = self._store.get_candles(symbol, "15m", limit=50)
        if len(candles) < 25:
            return False
        try:
            df = pd.DataFrame(candles).sort_values("ts").reset_index(drop=True)
            close = df["c"].astype(float)
            mid = close.rolling(20).mean()
            std = close.rolling(20).std()
            bb_bw = (2 * 2 * std) / mid
            bb_cur = float(bb_bw.iloc[-1])
            bb_avg = float(bb_bw.iloc[-21:-1].mean())
            return (
                not np.isnan(bb_cur)
                and not np.isnan(bb_avg)
                and bb_avg > 0
                and bb_cur >= bb_avg * SPREAD_EXPANSION_MULT
            )
        except Exception as exc:
            logger.debug("[FastLayer] spread_deterioration error: %s", exc)
            return False

    def _check_rapid_oi_change(self, symbol: str) -> bool:
        current_oi = self._store.get_open_interest(symbol)
        if current_oi is None or current_oi <= 0:
            return False

        now = int(time.time() * 1000)
        prev_oi = self._oi_snapshot.get(symbol)
        if prev_oi is None or (now - self._oi_snapshot_ts) > 60 * 60 * 1000:
            self._oi_snapshot[symbol] = current_oi
            self._oi_snapshot_ts = now
            return False

        change_pct = abs(current_oi - prev_oi) / prev_oi * 100
        return change_pct >= OI_CHANGE_PCT_THRESHOLD

    def _check_funding_surge(self, symbol: str) -> bool:
        funding = self._store.get_funding(symbol)
        if funding is None:
            return False
        return abs(funding) >= FUNDING_SURGE_THRESHOLD

    def _check_funding_dislocation(self, symbol: str) -> bool:
        funding = self._store.get_funding(symbol)
        if funding is None:
            return False
        return abs(funding) >= FUNDING_SURGE_THRESHOLD * 2

    def _check_price_vwap_dislocation(self, symbol: str) -> bool:
        candles = self._store.get_candles(symbol, "1h", limit=24)
        if len(candles) < 10:
            return False
        try:
            df = pd.DataFrame(candles).sort_values("ts").reset_index(drop=True)
            high = df["h"].astype(float)
            low = df["l"].astype(float)
            close = df["c"].astype(float)
            volume = df["v"].astype(float)

            typical = (high + low + close) / 3.0
            cum_volume = volume.cumsum()
            vwap = ((typical * volume).cumsum() / cum_volume.replace(0, np.nan)).iloc[-1]
            if np.isnan(vwap) or float(vwap) == 0.0:
                return False

            price = float(close.iloc[-1])
            return abs(price - float(vwap)) / float(vwap) >= PRICE_DISLOCATION_THRESHOLD
        except Exception as exc:
            logger.debug("[FastLayer] price_vwap_dislocation error: %s", exc)
            return False

    @staticmethod
    def _check_market_dislocation(vol_spike: bool, spread_det: bool) -> bool:
        return vol_spike and spread_det

    @staticmethod
    def _check_liquidation_risk_proxy(
        mom_burst: bool,
        vol_spike: bool,
        funding_surg: bool,
        oi_rapid: bool,
    ) -> bool:
        signal_count = sum(1 for value in (mom_burst, vol_spike, funding_surg, oi_rapid) if value)
        return signal_count >= 3

    @staticmethod
    def _build_warning_summary(signals: List[str], alert_level: str) -> str:
        if not signals:
            return "stable"
        return f"{alert_level.lower()}:" + ",".join(signals[:4])
