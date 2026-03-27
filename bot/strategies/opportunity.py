"""
Opportunity Engine — v1.3 Master Plan PART 4

Signal → Opportunity 정규화.
모든 전략의 신호를 공통 포맷(Opportunity)으로 변환하고
시장 미시구조 상태를 주입한다.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from bot.data.store import DataStore
    from bot.strategies._base import Signal

logger = logging.getLogger(__name__)

# 전략 카테고리 → opportunity category 매핑
STRATEGY_CATEGORY_MAP: Dict[str, str] = {
    "overreaction_reversal":         "reversal",
    "volatility_expansion_breakout": "breakout",
    "early_trend_capture":           "trend",
    # image_pattern: 내부 direction에 따라 분기하되 기본값은 "pattern"
    "image_pattern":                 "pattern",
    # 신규 전략 (방향성 강화)
    "bear_trend":                    "bear_trend",
    "range_trader":                  "range",
    "volatility_momentum":           "volatility",
    # 하위 호환 — 구 전략명 (legacy PAUSED)
    "rsi_exhaustion":                "reversal",
    "ema_cross":                     "trend",
    "range_breakout":                "breakout",
}

# 전략 카테고리별 기본 보유 윈도우
HOLD_WINDOW_MAP: Dict[str, str] = {
    "reversal":   "30m~4h",
    "breakout":   "15m~3h",
    "trend":      "1h~6h",
    "pattern":    "1h~4h",
    "bear_trend": "2h~6h",
    "range":      "1h~4h",
    "volatility": "30m~2h",
}


# --------------------------------------------------------------------------- #
# Opportunity dataclass
# --------------------------------------------------------------------------- #

@dataclass
class Opportunity:
    """
    실행 후보(Opportunity).
    Signal 하나에서 생성되며, 스코어 / 순위 / 실행 상태를 담는다.
    """

    # ── Identity ──────────────────────────────────────────────────────────── #
    id:              str = field(default_factory=lambda: str(uuid.uuid4()))
    ts:              int = field(default_factory=lambda: int(time.time() * 1000))

    # ── Trading ───────────────────────────────────────────────────────────── #
    symbol:          str = ""
    side:            str = ""           # LONG | SHORT
    source_strategy: str = ""
    category:        str = ""           # reversal | breakout | trend
    signal_id:       str = ""
    regime_snapshot: str = ""           # JSON
    tp:   Optional[float] = None
    sl:   Optional[float] = None

    # ── Microstructure state ──────────────────────────────────────────────── #
    signal_strength:   float = 0.0
    funding_state:     str = "NEUTRAL"   # EXTREME_LONG | EXTREME_SHORT | NEUTRAL
    oi_state:          str = "NEUTRAL"   # SPIKE | DIVERGENCE | NEUTRAL
    volume_state:      str = "NORMAL"    # HIGH | NORMAL | LOW
    spread_state:      str = "GOOD"      # GOOD | WIDE | CRITICAL
    volatility_state:  str = "NORMAL"    # EXPANDING | CONTRACTING | NORMAL
    liquidity_state:   str = "OK"        # OK | LOW | CRITICAL

    # ── Timing ────────────────────────────────────────────────────────────── #
    expected_hold_window: str = ""
    invalidation_level:   Optional[float] = None

    # ── Scoring ───────────────────────────────────────────────────────────── #
    confidence_raw:   float = 0.0
    score_total:      int   = 0
    score_breakdown:  dict  = field(default_factory=dict)

    # ── Ranking ───────────────────────────────────────────────────────────── #
    rank_global:        int = 0
    rank_within_symbol: int = 0

    # ── Execution ─────────────────────────────────────────────────────────── #
    # PENDING → APPROVED / PAPER_ONLY / IGNORED / EXECUTED / EXPIRED
    execution_status: str            = "PENDING"
    approved_by:      Optional[str]  = None
    approved_at:      Optional[int]  = None

    # ── Helpers ───────────────────────────────────────────────────────────── #

    @property
    def is_actionable(self) -> bool:
        """score >= 8 → 실행 후보."""
        return self.score_total >= 8

    @property
    def is_watch(self) -> bool:
        """score 6~7 → 관찰."""
        return 6 <= self.score_total <= 7

    @property
    def score_breakdown_json(self) -> str:
        return json.dumps(self.score_breakdown)

    def to_dict(self) -> dict:
        return {
            "id":               self.id,
            "ts":               self.ts,
            "symbol":           self.symbol,
            "side":             self.side,
            "source_strategy":  self.source_strategy,
            "category":         self.category,
            "signal_id":        self.signal_id,
            "signal_strength":  self.signal_strength,
            "funding_state":    self.funding_state,
            "oi_state":         self.oi_state,
            "volume_state":     self.volume_state,
            "spread_state":     self.spread_state,
            "volatility_state": self.volatility_state,
            "liquidity_state":  self.liquidity_state,
            "expected_hold_window": self.expected_hold_window,
            "invalidation_level":   self.invalidation_level,
            "confidence_raw":   self.confidence_raw,
            "score_total":      self.score_total,
            "score_breakdown":  self.score_breakdown,
            "rank_global":      self.rank_global,
            "execution_status": self.execution_status,
            "approved_by":      self.approved_by,
            "tp":               self.tp,
            "sl":               self.sl,
        }


# --------------------------------------------------------------------------- #
# OpportunityNormalizer
# --------------------------------------------------------------------------- #

class OpportunityNormalizer:
    """
    Signal → Opportunity 변환기.

    DataStore에서 시장 미시구조 상태(펀딩/OI/거래량/변동성)를 읽어
    Opportunity 공통 포맷으로 정규화한다.
    """

    # 펀딩 극값 기준
    FUNDING_EXTREME_LONG  =  0.0005   # 0.05% 이상 → 과열 롱
    FUNDING_EXTREME_SHORT = -0.0005   # -0.05% 이하 → 과열 숏

    # 거래량 배수 기준
    VOLUME_HIGH_MULTIPLIER = 1.5      # 20주기 평균 대비 1.5배 이상
    VOLUME_LOW_MULTIPLIER  = 0.7

    def __init__(self, store: "DataStore") -> None:
        self._store = store

    def normalize(self, signal: "Signal", regime: dict) -> Optional[Opportunity]:
        """Signal 하나를 Opportunity로 변환. SKIP 신호는 None 반환."""
        if signal.action == "SKIP":
            return None

        side = "LONG" if signal.action == "BUY" else "SHORT"
        category = STRATEGY_CATEGORY_MAP.get(signal.strategy, "trend")

        opp = Opportunity(
            ts              = signal.ts,
            symbol          = signal.symbol,
            side            = side,
            source_strategy = signal.strategy,
            category        = category,
            signal_id       = signal.id,
            regime_snapshot = json.dumps({
                "regime":       regime.get("regime", "UNKNOWN"),
                "btc_price":    regime.get("btc_price"),
                "funding":      regime.get("funding"),
                "btc_atr_pct":  regime.get("btc_atr_pct"),
            }),
            tp               = signal.tp,
            sl               = signal.sl,
            signal_strength  = signal.confidence,
            confidence_raw   = signal.confidence,
            expected_hold_window = HOLD_WINDOW_MAP.get(category, "1h~4h"),
        )

        # 미시구조 상태 주입
        self._inject_funding_state(opp, signal.symbol, regime)
        self._inject_oi_state(opp, signal.symbol)
        self._inject_volume_state(opp, signal.symbol)
        self._inject_volatility_state(opp, regime)
        self._inject_liquidity_state(opp, signal.symbol)

        return opp

    # ---------------------------------------------------------------------- #
    # Microstructure injectors
    # ---------------------------------------------------------------------- #

    def _inject_funding_state(self, opp: Opportunity, symbol: str, regime: dict) -> None:
        funding = self._store.get_funding(symbol)
        if funding is None:
            # Regime 스냅샷에서 fallback
            funding = regime.get("funding", 0.0) or 0.0
        if funding >= self.FUNDING_EXTREME_LONG:
            opp.funding_state = "EXTREME_LONG"
        elif funding <= self.FUNDING_EXTREME_SHORT:
            opp.funding_state = "EXTREME_SHORT"
        else:
            opp.funding_state = "NEUTRAL"

    def _inject_oi_state(self, opp: Opportunity, symbol: str) -> None:
        oi = self._store.get_open_interest(symbol)
        if oi is None or oi <= 0:
            opp.oi_state = "NEUTRAL"
            return
        # 간단한 기준: OI 절대값 변화율은 현재 구조에서 단일 값이므로
        # 향후 OI history 추가 시 개선. 지금은 중립 유지.
        opp.oi_state = "NEUTRAL"

    def _inject_volume_state(self, opp: Opportunity, symbol: str) -> None:
        candles = self._store.get_candles(symbol, "1h", limit=21)
        if not candles or len(candles) < 5:
            opp.volume_state = "NORMAL"
            return
        vols = [float(c.get("v", 0)) for c in candles]
        last_vol = vols[-1]
        avg_vol  = sum(vols[:-1]) / max(len(vols) - 1, 1)
        if avg_vol <= 0:
            opp.volume_state = "NORMAL"
            return
        ratio = last_vol / avg_vol
        if ratio >= self.VOLUME_HIGH_MULTIPLIER:
            opp.volume_state = "HIGH"
        elif ratio <= self.VOLUME_LOW_MULTIPLIER:
            opp.volume_state = "LOW"
        else:
            opp.volume_state = "NORMAL"

    def _inject_volatility_state(self, opp: Opportunity, regime: dict) -> None:
        atr_pct = regime.get("btc_atr_pct") or 0.0
        if atr_pct >= 3.0:
            opp.volatility_state = "EXPANDING"
        elif atr_pct <= 0.5:
            opp.volatility_state = "CONTRACTING"
        else:
            opp.volatility_state = "NORMAL"

    def _inject_liquidity_state(self, opp: Opportunity, symbol: str) -> None:
        ticker = self._store.get_ticker(symbol)
        if ticker is None:
            opp.liquidity_state = "CRITICAL"
            return
        vol_24h = float(ticker.get("volume_24h", 0) or 0)
        # 기본 임계: 24H 거래량 $10M 이상 OK, $2M 미만 CRITICAL
        if vol_24h >= 10_000_000:
            opp.liquidity_state = "OK"
        elif vol_24h >= 2_000_000:
            opp.liquidity_state = "LOW"
        else:
            opp.liquidity_state = "CRITICAL"
