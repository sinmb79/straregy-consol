"""
Strategy D — Bear Trend (하방 추세추종)

BTC_BEARISH / HIGH_VOLATILITY 레짐에서 SHORT 포지션을 추세 방향으로 진입한다.
기존 시스템에 없던 핵심 방향 — 하락 추세를 타고 내려가는 전략.

설계 원칙:
  - 칼끝 잡기 방지: RSI가 과매도(< 35)면 진입하지 않는다.
  - 추세 확인 후 눌림 반등을 기다려 진입 (낮은 리스크 위치).
  - 거래량 확인 필수: 매도 압력이 실제 발생한 것을 확인.

진입 조건 (SHORT — 하락 추세 추종):
  - EMA20 < EMA50 (하락 추세 구조 확인)
  - 현재 가격 < EMA50 (추세 방향 확인)
  - 최근 3봉 중 2봉 이상 음봉 (모멘텀 지속)
  - RSI(14) 35~55 사이 (과매도 아님 — 추세 중간 지점)
  - 거래량 > 1.3× 20봉 평균 (매도 압력 확인)
  - 펀딩이 과도한 음수가 아님 (< -0.05% 이면 숏 과열이라 패스)

진입 조건 (LONG — 하락 추세 종료 포착):
  - HIGH_VOLATILITY 레짐에서만 허용
  - EMA20이 EMA50을 상향 돌파 (골든 크로스 발생)
  - RSI(14) 45~65 (반전 초기)
  - 거래량 > 1.5× 평균 (강한 매수세 확인)

보유 시간: 2시간~6시간 (Time Stop으로 외부 관리)
TP = -3.0% (SHORT 기준), SL = +1.5%
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

MIN_CANDLES = 70


class BearTrendStrategy(StrategyBase):
    """하락 추세 추종 — SHORT 포지션 중심."""

    name:          str       = "bear_trend"
    category:      str       = "bear_trend"
    regime_filter: List[str] = ["BTC_BEARISH", "HIGH_VOLATILITY"]

    EMA_FAST:       int   = 20
    EMA_SLOW:       int   = 50
    RSI_PERIOD:     int   = 14
    RSI_LOW:        float = 35.0   # 이하면 과매도 — SHORT 진입 금지
    RSI_HIGH:       float = 55.0   # SHORT 진입 허용 상한
    RSI_LONG_LOW:   float = 45.0   # LONG 전환 진입 허용 하한
    RSI_LONG_HIGH:  float = 65.0
    VOL_MULT_SHORT: float = 1.3    # SHORT 진입 최소 거래량 배수
    VOL_MULT_LONG:  float = 1.5
    TP_PCT:         float = 0.030  # 3.0%
    SL_PCT:         float = 0.015  # 1.5%
    INTERVAL:       str   = "1h"

    # 펀딩 숏 과열 기준 (이 이하면 숏이 너무 몰린 것)
    FUNDING_SHORT_CROWDED: float = -0.0005  # -0.05%

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
        candles = store.get_candles(symbol, self.INTERVAL, limit=MIN_CANDLES + 10)
        if len(candles) < MIN_CANDLES:
            return None

        df = pd.DataFrame(candles).sort_values("ts").reset_index(drop=True)
        close  = df["c"].astype(float)
        open_  = df["o"].astype(float)
        volume = df["v"].astype(float)

        # EMA20, EMA50
        ema_fast = close.ewm(span=self.EMA_FAST, adjust=False).mean()
        ema_slow = close.ewm(span=self.EMA_SLOW, adjust=False).mean()

        # RSI(14)
        delta = close.diff()
        gain  = delta.clip(lower=0).ewm(span=self.RSI_PERIOD, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(span=self.RSI_PERIOD, adjust=False).mean()
        rs    = gain / loss.replace(0, np.nan)
        rsi   = (100 - (100 / (1 + rs))).fillna(50)

        price_cur    = float(close.iloc[-1])
        ema_fast_cur = float(ema_fast.iloc[-1])
        ema_fast_prv = float(ema_fast.iloc[-2])
        ema_slow_cur = float(ema_slow.iloc[-1])
        ema_slow_prv = float(ema_slow.iloc[-2])
        rsi_cur      = float(rsi.iloc[-1])

        # 거래량 (최근 1봉 vs 20봉 평균)
        vols     = volume.iloc[-21:].values
        last_vol = vols[-1]
        avg_vol  = vols[:-1].mean() if len(vols) > 1 else last_vol
        vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

        # 최근 3봉 음봉 수
        recent_candles = 3
        bearish_count = sum(
            1 for i in range(-recent_candles, 0)
            if float(close.iloc[i]) < float(open_.iloc[i])
        )

        # 펀딩
        funding = store.get_funding(symbol) or regime.get("funding", 0.0) or 0.0

        tp_pct = self.get_param("tp_pct", self.TP_PCT)
        sl_pct = self.get_param("sl_pct", self.SL_PCT)

        # ─── SHORT: 하락 추세 추종 ────────────────────────────────────────── #
        if (
            ema_fast_cur < ema_slow_cur                    # 하락 구조
            and price_cur < ema_slow_cur                   # 추세 방향 확인
            and self.RSI_LOW < rsi_cur < self.RSI_HIGH     # 과매도 아님
            and bearish_count >= 2                         # 모멘텀 지속
            and vol_ratio >= self.VOL_MULT_SHORT           # 매도 압력 확인
            and funding > self.FUNDING_SHORT_CROWDED       # 숏 과열 아님
        ):
            # confidence: RSI가 55에 가까울수록(추세 초기) 높고, 35에 가까울수록 낮음
            confidence = self._clamp(
                (rsi_cur - self.RSI_LOW) / (self.RSI_HIGH - self.RSI_LOW) * 0.5 + 0.5,
                0.5, 0.95,
            )
            tp = round(price_cur * (1 - tp_pct), 8)
            sl = round(price_cur * (1 + sl_pct), 8)
            return Signal(
                strategy=self.name, symbol=symbol,
                action="SELL", mode=self._PHASE2_MODE,
                confidence=round(confidence, 4), regime=regime_str,
                tp=tp, sl=sl,
                reason=(
                    f"BearTrend SHORT: EMA{self.EMA_FAST}={ema_fast_cur:.4f} < "
                    f"EMA{self.EMA_SLOW}={ema_slow_cur:.4f}, "
                    f"RSI={rsi_cur:.1f}, Vol×{vol_ratio:.1f}, "
                    f"음봉={bearish_count}/3, funding={funding:.5f}"
                ),
            )

        # ─── LONG: 하락 추세 종료 포착 (HIGH_VOLATILITY 한정) ──────────────── #
        # 골든 크로스 발생 + 거래량 폭발 → 추세 전환 초기 LONG
        if regime_str == "HIGH_VOLATILITY":
            golden_cross = (ema_fast_prv <= ema_slow_prv) and (ema_fast_cur > ema_slow_cur)
            if (
                golden_cross
                and self.RSI_LONG_LOW < rsi_cur < self.RSI_LONG_HIGH
                and vol_ratio >= self.VOL_MULT_LONG
            ):
                confidence = self._clamp(0.55 + (rsi_cur - self.RSI_LONG_LOW) / 100, 0.55, 0.85)
                tp = round(price_cur * (1 + tp_pct), 8)
                sl = round(price_cur * (1 - sl_pct), 8)
                return Signal(
                    strategy=self.name, symbol=symbol,
                    action="BUY", mode=self._PHASE2_MODE,
                    confidence=round(confidence, 4), regime=regime_str,
                    tp=tp, sl=sl,
                    reason=(
                        f"BearTrend REVERSAL LONG: 골든크로스 발생, "
                        f"RSI={rsi_cur:.1f}, Vol×{vol_ratio:.1f} [HIGH_VOL]"
                    ),
                )

        return None
