"""
Strategy A — Overreaction Reversal (v1.3 PART 3.2A)

급등/급락 + 과열 펀딩 + OI 급변 후 되돌림을 노린다.
기존 rsi_exhaustion을 강화 재구현.

진입 조건 (BUY — 과매도 되돌림):
  - RSI(14, 1H) < 28  (기존보다 더 극단적)
  - 직전 3봉 중 하나 이상이 -3% 이상 급락 (overreaction candle)
  - 펀딩이 음수(숏 과열) 또는 중립
  - price > EMA200 * 0.90 (극단 다운트렌드 제외)
  - RSI 회복 (rsi_cur > rsi_prev)

진입 조건 (SELL — 과매수 되돌림):
  - RSI(14, 1H) > 72
  - 직전 3봉 중 하나 이상이 +3% 이상 급등
  - 펀딩이 양수(롱 과열) 또는 중립
  - price < EMA200 * 1.10
  - RSI 하락

보유 시간: 30분~4시간 (time stop으로 외부 관리)
TP = +2.5%, SL = -1.2%
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

MIN_CANDLES = 220


class OverreactionReversalStrategy(StrategyBase):
    """급등/급락 오버리액션 되돌림 전략."""

    name:          str       = "overreaction_reversal"
    category:      str       = "reversal"
    regime_filter: List[str] = [
        "BTC_BEARISH", "BTC_SIDEWAYS", "HIGH_VOLATILITY",
        "ALT_ROTATION",
    ]

    RSI_PERIOD:          int   = 14
    RSI_OVERSOLD:        float = 28.0
    RSI_OVERBOUGHT:      float = 72.0
    EMA_LONG:            int   = 200
    EMA_BAND_LOW:        float = 0.90   # EMA200 * 0.90 이상이어야
    EMA_BAND_HIGH:       float = 1.10
    OVERREACTION_PCT:    float = 0.03   # 3% 급등/급락 캔들
    LOOKBACK_BARS:       int   = 3
    TP_PCT:              float = 0.025  # 2.5%
    SL_PCT:              float = 0.012  # 1.2%
    INTERVAL:            str   = "1h"
    # 펀딩 극단값 기준 (OpportunityNormalizer와 동일)
    FUNDING_EXTREME_LONG:  float =  0.0005   # +0.05% 이상 → 롱 과열
    FUNDING_EXTREME_SHORT: float = -0.0005   # -0.05% 이하 → 숏 과열
    FUNDING_EXTREME_BONUS: float =  0.10     # 극단 펀딩 시 confidence +10%

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
        close = df["c"].astype(float)
        open_ = df["o"].astype(float)

        # EMA200
        ema200 = close.ewm(span=self.EMA_LONG, adjust=False).mean()

        # RSI(14)
        delta = close.diff()
        gain  = delta.clip(lower=0).ewm(span=self.RSI_PERIOD, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(span=self.RSI_PERIOD, adjust=False).mean()
        rs    = gain / loss.replace(0, np.nan)
        rsi   = (100 - (100 / (1 + rs))).fillna(50)

        rsi_cur    = float(rsi.iloc[-1])
        rsi_prev   = float(rsi.iloc[-2])
        price_cur  = float(close.iloc[-1])
        ema200_cur = float(ema200.iloc[-1])

        # 런타임 파라미터 (params_store 우선, 클래스 상수 폴백)
        rsi_oversold     = self.get_param("rsi_oversold",     self.RSI_OVERSOLD)
        rsi_overbought   = self.get_param("rsi_overbought",   self.RSI_OVERBOUGHT)
        overreaction_pct = self.get_param("overreaction_pct", self.OVERREACTION_PCT)
        tp_pct           = self.get_param("tp_pct",           self.TP_PCT)
        sl_pct           = self.get_param("sl_pct",           self.SL_PCT)

        if ema200_cur <= 0:
            return None

        # 캔들 변동률 (최근 LOOKBACK_BARS)
        candle_rets = (
            (close - open_) / open_.replace(0, np.nan)
        ).fillna(0).iloc[-(self.LOOKBACK_BARS + 1): -1]
        max_drop = float(candle_rets.min())   # 음수 = 하락
        max_gain = float(candle_rets.max())   # 양수 = 상승

        # 펀딩
        funding = store.get_funding(symbol) or regime.get("funding", 0.0) or 0.0

        # ─── BUY (과매도 되돌림) ─────────────────────────────────────────── #
        if (
            rsi_cur < rsi_oversold
            and rsi_cur > rsi_prev                             # 회복 시작
            and max_drop <= -overreaction_pct                  # 급락 캔들 존재
            and price_cur > ema200_cur * self.EMA_BAND_LOW     # 너무 깊은 다운트렌드 제외
            and funding <= 0.0001                              # 숏 과열 or 중립
        ):
            confidence = self._clamp(1.0 - rsi_cur / 100.0 + abs(max_drop) * 2, 0.5, 1.0)
            # 펀딩 EXTREME_SHORT → 숏 청산 압력 ↑ → 반등 가능성 ↑ → confidence 보정
            funding_note = ""
            if funding <= self.FUNDING_EXTREME_SHORT:
                confidence = self._clamp(confidence + self.FUNDING_EXTREME_BONUS, 0.5, 1.0)
                funding_note = " [EXTREME_SHORT_FUNDING]"
            tp = round(price_cur * (1 + tp_pct), 8)
            sl = round(price_cur * (1 - sl_pct), 8)
            return Signal(
                strategy=self.name, symbol=symbol,
                action="BUY", mode=self._PHASE2_MODE,
                confidence=round(confidence, 4), regime=regime_str,
                tp=tp, sl=sl,
                reason=(
                    f"Overreaction reversal BUY: RSI={rsi_cur:.1f}(회복), "
                    f"최대낙폭={max_drop*100:.1f}%, funding={funding:.5f}{funding_note}"
                ),
            )

        # ─── SELL (과매수 되돌림) ────────────────────────────────────────── #
        if (
            rsi_cur > rsi_overbought
            and rsi_cur < rsi_prev                             # 하락 시작
            and max_gain >= overreaction_pct                   # 급등 캔들 존재
            and price_cur < ema200_cur * self.EMA_BAND_HIGH
            and funding >= -0.0001                             # 롱 과열 or 중립
        ):
            confidence = self._clamp(rsi_cur / 100.0 + max_gain * 2, 0.5, 1.0)
            # 펀딩 EXTREME_LONG → 롱 청산 압력 ↑ → 하락 가능성 ↑ → confidence 보정
            funding_note = ""
            if funding >= self.FUNDING_EXTREME_LONG:
                confidence = self._clamp(confidence + self.FUNDING_EXTREME_BONUS, 0.5, 1.0)
                funding_note = " [EXTREME_LONG_FUNDING]"
            tp = round(price_cur * (1 - tp_pct), 8)
            sl = round(price_cur * (1 + sl_pct), 8)
            return Signal(
                strategy=self.name, symbol=symbol,
                action="SELL", mode=self._PHASE2_MODE,
                confidence=round(confidence, 4), regime=regime_str,
                tp=tp, sl=sl,
                reason=(
                    f"Overreaction reversal SELL: RSI={rsi_cur:.1f}(하락), "
                    f"최대상승={max_gain*100:.1f}%, funding={funding:.5f}{funding_note}"
                ),
            )

        return None
