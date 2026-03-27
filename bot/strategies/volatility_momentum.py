"""
Strategy F — Volatility Momentum (변동성 모멘텀)

HIGH_VOLATILITY 레짐 전용.
ATR이 폭발적으로 확대될 때 초기 방향을 빠르게 포착해 단기 추세를 탄다.

설계 원칙:
  - 변동성 폭발 직후 방향이 정해지는 첫 2~3봉이 가장 큰 모멘텀.
  - 볼린저 밴드 돌파 + 거래량 폭발 + 강한 캔들로 방향 확인.
  - 빠른 수익 실현 (짧은 보유): TP 3%, SL 1.5%.
  - 방향이 불분명하면 진입하지 않는다 (거래 안 하는 것이 최선).

진입 조건 (LONG — 상방 변동성 모멘텀):
  - ATR% > 3.0% (변동성 확인)
  - BB bandwidth가 20봉 평균의 1.5배 이상 (변동성 확장 중)
  - 현재 종가 > 볼린저 상단 (상방 돌파)
  - 현재 봉이 강한 양봉: (종가 - 시가) / (고가 - 저가) > 0.6
  - 거래량 > 2.0× 20봉 평균 (폭발적 거래량)
  - 직전 봉도 양봉 (1봉 이상 연속 상승)
  - 펀딩이 과도한 롱 아님 (< +0.05%)

진입 조건 (SHORT — 하방 변동성 모멘텀):
  - 위와 대칭 조건 (현재 종가 < 볼린저 하단, 강한 음봉)

보유 시간: 30분~2시간 (가장 짧은 포지션)
TP = +3.0%, SL = -1.5%
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List, Optional

import numpy as np
import pandas as pd

from bot.strategies._base import Signal, StrategyBase

if TYPE_CHECKING:
    from bot.data.store import DataStore

logger = logging.getLogger(__name__)

MIN_CANDLES = 40


class VolatilityMomentumStrategy(StrategyBase):
    """HIGH_VOLATILITY 레짐 전용 — 변동성 폭발 초기 방향 추종."""

    name:          str       = "volatility_momentum"
    category:      str       = "volatility"
    regime_filter: List[str] = ["HIGH_VOLATILITY"]

    BB_PERIOD:         int   = 20
    BB_STD:            float = 2.0
    ATR_PERIOD:        int   = 14
    ATR_MIN_PCT:       float = 3.0    # 최소 ATR% (변동성 확인)
    BB_EXPAND_MULT:    float = 1.5    # BB width가 평균의 1.5배 이상
    CANDLE_BODY_RATIO: float = 0.60   # 강한 캔들 최소 몸통 비율
    VOL_MULT:          float = 2.0    # 최소 거래량 배수
    TP_PCT:            float = 0.030  # 3.0%
    SL_PCT:            float = 0.015  # 1.5%
    FUNDING_LONG_MAX:  float = 0.0005  # +0.05% 이상이면 롱 과열
    FUNDING_SHORT_MIN: float = -0.0005 # -0.05% 이하면 숏 과열
    INTERVAL:          str   = "1h"

    def compute(self, store: "DataStore", regime: dict) -> List[Signal]:
        from bot.config import get_config
        config = get_config()
        current_regime = regime.get("regime", "UNKNOWN")
        signals: List[Signal] = []
        for symbol in config.tracked_symbols:
            sig = self._evaluate_symbol(store, symbol, current_regime, regime)
            if sig is not None:
                signals.append(sig)
        return signals

    def _evaluate_symbol(
        self, store: "DataStore", symbol: str, regime_str: str, regime: dict
    ) -> Optional[Signal]:
        candles = store.get_candles(symbol, self.INTERVAL, limit=MIN_CANDLES + 5)
        if len(candles) < MIN_CANDLES:
            return None

        df = pd.DataFrame(candles).sort_values("ts").reset_index(drop=True)
        close  = df["c"].astype(float)
        open_  = df["o"].astype(float)
        high   = df["h"].astype(float)
        low    = df["l"].astype(float)
        volume = df["v"].astype(float)

        # Bollinger Bands (20, 2)
        bb_mid   = close.rolling(self.BB_PERIOD).mean()
        bb_std   = close.rolling(self.BB_PERIOD).std()
        bb_upper = bb_mid + self.BB_STD * bb_std
        bb_lower = bb_mid - self.BB_STD * bb_std
        bb_bw    = (bb_upper - bb_lower) / bb_mid
        bb_bw_avg = bb_bw.rolling(self.BB_PERIOD).mean()

        # ATR(14)
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(span=self.ATR_PERIOD, adjust=False).mean()

        price_cur   = float(close.iloc[-1])
        open_cur    = float(open_.iloc[-1])
        high_cur    = float(high.iloc[-1])
        low_cur     = float(low.iloc[-1])
        open_prv    = float(open_.iloc[-2])
        close_prv   = float(close.iloc[-2])
        atr_cur     = float(atr.iloc[-1])
        atr_pct     = atr_cur / price_cur * 100 if price_cur > 0 else 0.0
        bb_up_cur   = float(bb_upper.iloc[-1])
        bb_lo_cur   = float(bb_lower.iloc[-1])
        bw_cur      = float(bb_bw.iloc[-1])
        bw_avg_cur  = float(bb_bw_avg.iloc[-1]) if not np.isnan(bb_bw_avg.iloc[-1]) else bw_cur

        # 거래량
        vols     = volume.iloc[-21:].values
        last_vol = vols[-1]
        avg_vol  = vols[:-1].mean() if len(vols) > 1 else last_vol
        vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

        # 캔들 몸통 비율
        candle_range = high_cur - low_cur
        body_size    = abs(price_cur - open_cur)
        body_ratio   = body_size / candle_range if candle_range > 0 else 0.0

        # 펀딩
        funding = store.get_funding(symbol) or regime.get("funding", 0.0) or 0.0

        tp_pct = self.get_param("tp_pct", self.TP_PCT)
        sl_pct = self.get_param("sl_pct", self.SL_PCT)

        # 기본 조건: 변동성 확장 확인
        volatility_expanding = (
            atr_pct >= self.ATR_MIN_PCT
            and bw_avg_cur > 0
            and bw_cur >= bw_avg_cur * self.BB_EXPAND_MULT
        )
        volume_burst = vol_ratio >= self.VOL_MULT

        if not (volatility_expanding and volume_burst):
            return None

        # ─── LONG: 상방 변동성 모멘텀 ──────────────────────────────────── #
        prev_bullish = close_prv > open_prv   # 직전 봉 양봉
        curr_bullish = price_cur > open_cur   # 현재 봉 양봉
        above_upper  = price_cur > bb_up_cur  # BB 상단 돌파

        if (
            above_upper
            and curr_bullish
            and body_ratio >= self.CANDLE_BODY_RATIO
            and prev_bullish
            and funding < self.FUNDING_LONG_MAX   # 롱 과열 아님
        ):
            confidence = self._clamp(
                0.6 + (vol_ratio - self.VOL_MULT) * 0.05 + body_ratio * 0.1,
                0.6, 0.95,
            )
            tp = round(price_cur * (1 + tp_pct), 8)
            sl = round(price_cur * (1 - sl_pct), 8)
            return Signal(
                strategy=self.name, symbol=symbol,
                action="BUY", mode=self._PHASE2_MODE,
                confidence=round(confidence, 4), regime=regime_str,
                tp=tp, sl=sl,
                reason=(
                    f"VolMomentum LONG: BB상단돌파={bb_up_cur:.4f}, "
                    f"ATR%={atr_pct:.1f}%, 몸통={body_ratio:.0%}, "
                    f"Vol×{vol_ratio:.1f}, funding={funding:.5f}"
                ),
            )

        # ─── SHORT: 하방 변동성 모멘텀 ─────────────────────────────────── #
        prev_bearish = close_prv < open_prv
        curr_bearish = price_cur < open_cur
        below_lower  = price_cur < bb_lo_cur  # BB 하단 이탈

        if (
            below_lower
            and curr_bearish
            and body_ratio >= self.CANDLE_BODY_RATIO
            and prev_bearish
            and funding > self.FUNDING_SHORT_MIN   # 숏 과열 아님
        ):
            confidence = self._clamp(
                0.6 + (vol_ratio - self.VOL_MULT) * 0.05 + body_ratio * 0.1,
                0.6, 0.95,
            )
            tp = round(price_cur * (1 - tp_pct), 8)
            sl = round(price_cur * (1 + sl_pct), 8)
            return Signal(
                strategy=self.name, symbol=symbol,
                action="SELL", mode=self._PHASE2_MODE,
                confidence=round(confidence, 4), regime=regime_str,
                tp=tp, sl=sl,
                reason=(
                    f"VolMomentum SHORT: BB하단이탈={bb_lo_cur:.4f}, "
                    f"ATR%={atr_pct:.1f}%, 몸통={body_ratio:.0%}, "
                    f"Vol×{vol_ratio:.1f}, funding={funding:.5f}"
                ),
            )

        return None
