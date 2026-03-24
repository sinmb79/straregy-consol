"""
Fast Layer — v1.3 PART 8.4

기존 4H/24H 레짐 외 보조 단기 신호 레이어.
RegimeDetector.detect() 결과에 fast_layer 키를 추가한다.

감지 신호:
  - 15m_volatility_spike:  15분봉 ATR이 20봉 평균 대비 2배 이상
  - 1h_momentum_burst:     1시간봉 가격 변동률 절대값 >= 1.5%
  - 15m_spread_deterioration: 스프레드 지표 급악화 (볼린저밴드 폭 급확장)
  - rapid_oi_change:        OI 1시간 내 5% 이상 변동
  - funding_surge:          펀딩률 절대값 >= 0.05% (임계치 초과)

출력 형태 (fast_layer dict):
  {
    "15m_volatility_spike":       True/False,
    "1h_momentum_burst":          True/False,
    "15m_spread_deterioration":   True/False,
    "rapid_oi_change":            True/False,
    "funding_surge":              True/False,
    "alert_level":                "NONE" / "WARN" / "CAUTION",
    "signals":                    ["15m_volatility_spike", ...],
    "ts":                         unix_ms,
  }

alert_level:
  NONE    — 신호 없음
  WARN    — 1개 신호 (주의)
  CAUTION — 2개 이상 신호 (진입 억제 권고)
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Dict, List, Optional

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from bot.data.store import DataStore

logger = logging.getLogger(__name__)

# Thresholds
VOLATILITY_SPIKE_MULT   = 2.0    # ATR이 20봉 평균의 2배 이상
MOMENTUM_BURST_PCT      = 1.5    # 1H 가격 변동 1.5% 이상
SPREAD_EXPANSION_MULT   = 1.8    # BB폭이 20봉 평균의 1.8배 이상 (급확장)
OI_CHANGE_PCT_THRESHOLD = 5.0    # OI 5% 이상 변동
FUNDING_SURGE_THRESHOLD = 0.0005  # 펀딩률 절대값 0.05%


class FastLayer:
    """
    단기 시장 구조 이상 감지 레이어.

    Usage
    -----
    fl = FastLayer(store)
    fast = fl.compute()          # → fast_layer dict
    regime["fast_layer"] = fast  # RegimeDetector 결과에 병합
    """

    def __init__(self, store: "DataStore") -> None:
        self._store = store
        # OI 스냅샷 (rapid_oi_change 계산용)
        self._oi_snapshot: Dict[str, float] = {}
        self._oi_snapshot_ts: int = 0

    def compute(self, symbol: str = "BTCUSDT") -> dict:
        """Fast layer 신호를 계산하여 dict로 반환."""
        ts = int(time.time() * 1000)
        signals: List[str] = []

        vol_spike    = self._check_volatility_spike(symbol)
        mom_burst    = self._check_momentum_burst(symbol)
        spread_det   = self._check_spread_deterioration(symbol)
        oi_rapid     = self._check_rapid_oi_change(symbol)
        funding_surg = self._check_funding_surge(symbol)

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

        n = len(signals)
        if n == 0:
            alert_level = "NONE"
        elif n == 1:
            alert_level = "WARN"
        else:
            alert_level = "CAUTION"

        if alert_level != "NONE":
            logger.info(
                "[FastLayer] %s alert=%s signals=%s",
                symbol, alert_level, signals,
            )

        return {
            "15m_volatility_spike":      vol_spike,
            "1h_momentum_burst":         mom_burst,
            "15m_spread_deterioration":  spread_det,
            "rapid_oi_change":           oi_rapid,
            "funding_surge":             funding_surg,
            "alert_level":               alert_level,
            "signals":                   signals,
            "ts":                        ts,
        }

    # ---------------------------------------------------------------------- #
    # Individual signal detectors
    # ---------------------------------------------------------------------- #

    def _check_volatility_spike(self, symbol: str) -> bool:
        """15m ATR이 20봉 평균 대비 2배 이상이면 spike."""
        candles = self._store.get_candles(symbol, "15m", limit=30)
        if len(candles) < 22:
            return False
        try:
            df = pd.DataFrame(candles).sort_values("ts").reset_index(drop=True)
            high  = df["h"].astype(float)
            low   = df["l"].astype(float)
            close = df["c"].astype(float)

            tr = pd.concat([
                high - low,
                (high - close.shift(1)).abs(),
                (low  - close.shift(1)).abs(),
            ], axis=1).max(axis=1)
            atr = tr.rolling(14).mean()

            atr_cur  = float(atr.iloc[-1])
            atr_avg  = float(atr.iloc[-21:-1].mean())

            return atr_avg > 0 and atr_cur >= atr_avg * VOLATILITY_SPIKE_MULT
        except Exception as exc:
            logger.debug("[FastLayer] volatility_spike error: %s", exc)
            return False

    def _check_momentum_burst(self, symbol: str) -> bool:
        """1H 최근 봉 가격 변동률 절대값이 임계치 이상."""
        candles = self._store.get_candles(symbol, "1h", limit=5)
        if len(candles) < 2:
            return False
        try:
            df     = pd.DataFrame(candles).sort_values("ts").reset_index(drop=True)
            close  = df["c"].astype(float)
            ret_1h = abs((float(close.iloc[-1]) - float(close.iloc[-2])) / float(close.iloc[-2]) * 100)
            return ret_1h >= MOMENTUM_BURST_PCT
        except Exception as exc:
            logger.debug("[FastLayer] momentum_burst error: %s", exc)
            return False

    def _check_spread_deterioration(self, symbol: str) -> bool:
        """15m 볼린저밴드 폭이 20봉 평균 대비 급확장 (스프레드 악화 대리 지표)."""
        candles = self._store.get_candles(symbol, "15m", limit=50)
        if len(candles) < 25:
            return False
        try:
            df    = pd.DataFrame(candles).sort_values("ts").reset_index(drop=True)
            close = df["c"].astype(float)

            mid   = close.rolling(20).mean()
            std   = close.rolling(20).std()
            bb_bw = (2 * 2 * std) / mid  # (upper - lower) / mid

            bb_cur = float(bb_bw.iloc[-1])
            bb_avg = float(bb_bw.iloc[-21:-1].mean())

            return (
                not np.isnan(bb_cur) and not np.isnan(bb_avg)
                and bb_avg > 0
                and bb_cur >= bb_avg * SPREAD_EXPANSION_MULT
            )
        except Exception as exc:
            logger.debug("[FastLayer] spread_deterioration error: %s", exc)
            return False

    def _check_rapid_oi_change(self, symbol: str) -> bool:
        """OI가 직전 스냅샷 대비 5% 이상 변동했으면 True."""
        current_oi = self._store.get_open_interest(symbol)
        if current_oi is None or current_oi <= 0:
            return False

        now = int(time.time() * 1000)
        prev_oi = self._oi_snapshot.get(symbol)

        # 1시간마다 스냅샷 갱신
        if prev_oi is None or (now - self._oi_snapshot_ts) > 60 * 60 * 1000:
            self._oi_snapshot[symbol] = current_oi
            self._oi_snapshot_ts = now
            return False

        change_pct = abs(current_oi - prev_oi) / prev_oi * 100
        return change_pct >= OI_CHANGE_PCT_THRESHOLD

    def _check_funding_surge(self, symbol: str) -> bool:
        """펀딩률 절대값이 임계치 이상이면 surge."""
        funding = self._store.get_funding(symbol)
        if funding is None:
            return False
        return abs(funding) >= FUNDING_SURGE_THRESHOLD
