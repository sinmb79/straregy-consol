"""
Strategy E — Range Trader (레인지 양방향 매매)

BTC_SIDEWAYS 레짐에서 박스권 상단/하단을 인식하고
지지/저항에서 역방향으로 진입하는 양방향 매매 전략.

설계 원칙:
  - 실제 가격 범위(고점/저점)를 기반으로 지지/저항 설정.
  - 범위가 너무 좁으면 수수료 손실 → 최소 2% 박스만 대상.
  - 범위가 너무 넓으면 이미 추세 → 최대 8% 박스만 대상.
  - 거래량이 급증하면 돌파 가능성 → 진입 금지.

진입 조건 (LONG — 박스 하단 지지):
  - 24봉 최저가 = 지지선
  - 현재 가격이 지지선 + 1% 이내 (지지선 근접)
  - RSI(14) < 45 (과매도 쪽에 위치)
  - 거래량 < 1.5× 평균 (폭발적 거래량 없음 — 돌파 아님)
  - 이전 봉에서 지지선을 터치했다가 반등 (하위 꼬리)

진입 조건 (SHORT — 박스 상단 저항):
  - 24봉 최고가 = 저항선
  - 현재 가격이 저항선 - 1% 이내 (저항선 근접)
  - RSI(14) > 55 (과매수 쪽에 위치)
  - 거래량 < 1.5× 평균 (돌파 아님)
  - 이전 봉에서 저항선을 터치했다가 하락 (상위 꼬리)

TP = 박스 중간선 (50% 되돌림)
SL = 박스 외부 0.3% (범위 이탈 확인 시 즉시 손절)

보유 시간: 1시간~4시간 (Time Stop으로 외부 관리)
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


class RangeTraderStrategy(StrategyBase):
    """박스권 상단/하단 양방향 매매 전략."""

    name:          str       = "range_trader"
    category:      str       = "range"
    regime_filter: List[str] = ["BTC_SIDEWAYS"]

    LOOKBACK_BARS:    int   = 24     # 범위 정의 기간
    PROXIMITY_PCT:    float = 0.010  # 지지/저항 근접 기준 (1.0%)
    RANGE_MIN_PCT:    float = 0.025  # 유효 박스 최소 크기 (2.5%)
    RANGE_MAX_PCT:    float = 0.060  # 유효 박스 최대 크기 (6%)
    RSI_PERIOD:       int   = 14
    RSI_LONG_MAX:     float = 42.0   # LONG 진입 RSI 상한 (더 엄격)
    RSI_SHORT_MIN:    float = 58.0   # SHORT 진입 RSI 하한 (더 엄격)
    VOL_BURST_MULT:   float = 1.3    # 이 이상이면 돌파 의심 → 진입 금지 (더 엄격)
    SL_BUFFER_PCT:    float = 0.008  # SL = 범위 외부 0.8% (노이즈 여유)
    MIN_RR:           float = 1.5    # 최소 손익비 1.5
    INTERVAL:         str   = "1h"

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
        high   = df["h"].astype(float)
        low    = df["l"].astype(float)
        volume = df["v"].astype(float)

        # RSI(14)
        delta = close.diff()
        gain  = delta.clip(lower=0).ewm(span=self.RSI_PERIOD, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(span=self.RSI_PERIOD, adjust=False).mean()
        rs    = gain / loss.replace(0, np.nan)
        rsi   = (100 - (100 / (1 + rs))).fillna(50)

        # 현재 값
        price_cur = float(close.iloc[-1])
        rsi_cur   = float(rsi.iloc[-1])

        # 24봉 박스 정의 (최근 봉 제외)
        window = close.iloc[-(self.LOOKBACK_BARS + 1):-1]
        high_w = high.iloc[-(self.LOOKBACK_BARS + 1):-1]
        low_w  = low.iloc[-(self.LOOKBACK_BARS + 1):-1]

        resistance = float(high_w.max())  # 박스 상단
        support    = float(low_w.min())   # 박스 하단
        midpoint   = (resistance + support) / 2

        # 박스 유효성 검사
        range_pct = (resistance - support) / midpoint if midpoint > 0 else 0.0
        if range_pct < self.RANGE_MIN_PCT or range_pct > self.RANGE_MAX_PCT:
            return None  # 너무 좁거나 너무 넓은 박스

        # 거래량 (돌파 여부 판단)
        vols     = volume.iloc[-21:].values
        last_vol = vols[-1]
        avg_vol  = vols[:-1].mean() if len(vols) > 1 else last_vol
        vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

        # 돌파 거래량이면 레인지 무효 → 진입 금지
        if vol_ratio >= self.VOL_BURST_MULT:
            return None

        proximity_abs = midpoint * self.PROXIMITY_PCT

        # ─── LONG: 지지선 근접 반등 매수 ─────────────────────────────────── #
        # 현재가가 지지선 위 proximity_abs 이내에 있고 RSI가 낮은 편
        near_support = price_cur <= support + proximity_abs
        if near_support and rsi_cur < self.RSI_LONG_MAX:
            # 하위 꼬리 확인: 이전 봉의 저가가 지지선에 닿았다가 회복
            prev_low   = float(low.iloc[-2])
            prev_close = float(close.iloc[-2])
            tail_bounce = prev_low <= support * 1.005 and prev_close > support

            if tail_bounce:
                # TP = 박스 중간선, SL = 지지선 아래 0.3%
                tp = round(midpoint, 8)
                sl = round(support * (1 - self.SL_BUFFER_PCT), 8)
                rr = (tp - price_cur) / (price_cur - sl) if (price_cur - sl) > 0 else 0
                if rr < self.MIN_RR:
                    return None
                confidence = self._clamp(0.5 + (self.RSI_LONG_MAX - rsi_cur) / 100, 0.5, 0.85)
                return Signal(
                    strategy=self.name, symbol=symbol,
                    action="BUY", mode=self._PHASE2_MODE,
                    confidence=round(confidence, 4), regime=regime_str,
                    tp=tp, sl=sl,
                    reason=(
                        f"RangeTrader LONG: 지지선={support:.4f} 근접, "
                        f"RSI={rsi_cur:.1f}, 박스={range_pct*100:.1f}%, "
                        f"RR={rr:.2f}, Vol×{vol_ratio:.1f}"
                    ),
                )

        # ─── SHORT: 저항선 근접 반락 매도 ────────────────────────────────── #
        near_resistance = price_cur >= resistance - proximity_abs
        if near_resistance and rsi_cur > self.RSI_SHORT_MIN:
            # 상위 꼬리 확인: 이전 봉의 고가가 저항선에 닿았다가 하락
            prev_high  = float(high.iloc[-2])
            prev_close = float(close.iloc[-2])
            tail_reject = prev_high >= resistance * 0.995 and prev_close < resistance

            if tail_reject:
                # TP = 박스 중간선, SL = 저항선 위 0.3%
                tp = round(midpoint, 8)
                sl = round(resistance * (1 + self.SL_BUFFER_PCT), 8)
                rr = (price_cur - tp) / (sl - price_cur) if (sl - price_cur) > 0 else 0
                if rr < self.MIN_RR:
                    return None
                confidence = self._clamp(0.5 + (rsi_cur - self.RSI_SHORT_MIN) / 100, 0.5, 0.85)
                return Signal(
                    strategy=self.name, symbol=symbol,
                    action="SELL", mode=self._PHASE2_MODE,
                    confidence=round(confidence, 4), regime=regime_str,
                    tp=tp, sl=sl,
                    reason=(
                        f"RangeTrader SHORT: 저항선={resistance:.4f} 근접, "
                        f"RSI={rsi_cur:.1f}, 박스={range_pct*100:.1f}%, "
                        f"RR={rr:.2f}, Vol×{vol_ratio:.1f}"
                    ),
                )

        return None
