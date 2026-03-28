"""
Opportunity Engine v1.3 Master Plan PART 4.

Signal -> Opportunity normalization with lightweight market-structure context.
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

STRATEGY_CATEGORY_MAP: Dict[str, str] = {
    "overreaction_reversal": "reversal",
    "volatility_expansion_breakout": "breakout",
    "early_trend_capture": "trend",
    "image_pattern": "pattern",
    "bear_trend": "bear_trend",
    "range_trader": "range",
    "volatility_momentum": "volatility",
    "rsi_exhaustion": "reversal",
    "ema_cross": "trend",
    "range_breakout": "breakout",
}

HOLD_WINDOW_MAP: Dict[str, str] = {
    "reversal": "30m~4h",
    "breakout": "15m~3h",
    "trend": "1h~6h",
    "pattern": "1h~4h",
    "bear_trend": "2h~6h",
    "range": "1h~4h",
    "volatility": "30m~2h",
}


@dataclass
class Opportunity:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: int = field(default_factory=lambda: int(time.time() * 1000))

    symbol: str = ""
    side: str = ""
    source_strategy: str = ""
    category: str = ""
    category_label: str = ""
    strategy_family: str = ""
    signal_id: str = ""
    regime_snapshot: str = ""
    tp: Optional[float] = None
    sl: Optional[float] = None

    signal_strength: float = 0.0
    funding_state: str = "NEUTRAL"
    oi_state: str = "NEUTRAL"
    volume_state: str = "NORMAL"
    spread_state: str = "GOOD"
    volatility_state: str = "NORMAL"
    liquidity_state: str = "OK"
    supervision_flags: List[str] = field(default_factory=list)
    failure_pattern_labels: List[str] = field(default_factory=list)
    risk_warnings: List[str] = field(default_factory=list)

    expected_hold_window: str = ""
    invalidation_level: Optional[float] = None

    confidence_raw: float = 0.0
    score_total: int = 0
    score_breakdown: dict = field(default_factory=dict)

    rank_global: int = 0
    rank_within_symbol: int = 0

    execution_status: str = "PENDING"
    approved_by: Optional[str] = None
    approved_at: Optional[int] = None

    @property
    def is_actionable(self) -> bool:
        return self.score_total >= 8

    @property
    def is_watch(self) -> bool:
        return 6 <= self.score_total <= 7

    @property
    def score_breakdown_json(self) -> str:
        return json.dumps(self.score_breakdown)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts,
            "symbol": self.symbol,
            "side": self.side,
            "source_strategy": self.source_strategy,
            "category": self.category,
            "category_label": self.category_label,
            "strategy_family": self.strategy_family,
            "signal_id": self.signal_id,
            "regime_snapshot": self.regime_snapshot,
            "signal_strength": self.signal_strength,
            "funding_state": self.funding_state,
            "oi_state": self.oi_state,
            "volume_state": self.volume_state,
            "spread_state": self.spread_state,
            "volatility_state": self.volatility_state,
            "liquidity_state": self.liquidity_state,
            "supervision_flags": self.supervision_flags,
            "failure_pattern_labels": self.failure_pattern_labels,
            "risk_warnings": self.risk_warnings,
            "expected_hold_window": self.expected_hold_window,
            "invalidation_level": self.invalidation_level,
            "confidence_raw": self.confidence_raw,
            "score_total": self.score_total,
            "score_breakdown": self.score_breakdown,
            "rank_global": self.rank_global,
            "execution_status": self.execution_status,
            "approved_by": self.approved_by,
            "tp": self.tp,
            "sl": self.sl,
        }


class OpportunityNormalizer:
    FUNDING_EXTREME_LONG = 0.0005
    FUNDING_EXTREME_SHORT = -0.0005
    VOLUME_HIGH_MULTIPLIER = 1.5
    VOLUME_LOW_MULTIPLIER = 0.7

    def __init__(self, store: "DataStore") -> None:
        self._store = store

    def normalize(self, signal: "Signal", regime: dict) -> Optional[Opportunity]:
        if signal.action == "SKIP":
            return None

        side = "LONG" if signal.action == "BUY" else "SHORT"
        category = STRATEGY_CATEGORY_MAP.get(signal.strategy, "trend")
        category_label = self._category_label(category, side)

        opp = Opportunity(
            ts=signal.ts,
            symbol=signal.symbol,
            side=side,
            source_strategy=signal.strategy,
            category=category,
            category_label=category_label,
            strategy_family=signal.strategy_family or category,
            signal_id=signal.id,
            regime_snapshot=json.dumps({
                "regime": regime.get("regime", "UNKNOWN"),
                "btc_price": regime.get("btc_price"),
                "funding": regime.get("funding"),
                "btc_atr_pct": regime.get("btc_atr_pct"),
                "fast_layer": regime.get("fast_layer", {}),
            }),
            tp=signal.tp,
            sl=signal.sl,
            signal_strength=signal.confidence,
            confidence_raw=signal.confidence,
            expected_hold_window=HOLD_WINDOW_MAP.get(category, "1h~4h"),
            failure_pattern_labels=list(signal.failure_pattern_labels),
            risk_warnings=list(signal.risk_warnings),
        )

        self._inject_funding_state(opp, signal.symbol, regime)
        self._inject_oi_state(opp, signal.symbol)
        self._inject_volume_state(opp, signal.symbol)
        self._inject_volatility_state(opp, regime)
        self._inject_spread_state(opp, regime)
        self._inject_liquidity_state(opp, signal.symbol)
        self._inject_supervision_context(opp, regime)
        return opp

    def _inject_funding_state(self, opp: Opportunity, symbol: str, regime: dict) -> None:
        funding = self._store.get_funding(symbol)
        if funding is None:
            funding = regime.get("funding", 0.0) or 0.0
        if funding >= self.FUNDING_EXTREME_LONG:
            opp.funding_state = "EXTREME_LONG"
        elif funding <= self.FUNDING_EXTREME_SHORT:
            opp.funding_state = "EXTREME_SHORT"
        else:
            opp.funding_state = "NEUTRAL"

    def _inject_oi_state(self, opp: Opportunity, symbol: str) -> None:
        oi = self._store.get_open_interest(symbol)
        fast = self._get_fast_layer(opp)
        if oi is None or oi <= 0:
            opp.oi_state = "SPIKE" if fast.get("rapid_oi_change") else "NEUTRAL"
            return
        if fast.get("oi_funding_crowding"):
            opp.oi_state = "DIVERGENCE"
        elif fast.get("rapid_oi_change"):
            opp.oi_state = "SPIKE"
        else:
            opp.oi_state = "NEUTRAL"

    def _inject_volume_state(self, opp: Opportunity, symbol: str) -> None:
        candles = self._store.get_candles(symbol, "1h", limit=21)
        if not candles or len(candles) < 5:
            opp.volume_state = "NORMAL"
            return
        vols = [float(c.get("v", 0)) for c in candles]
        last_vol = vols[-1]
        avg_vol = sum(vols[:-1]) / max(len(vols) - 1, 1)
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

    def _inject_spread_state(self, opp: Opportunity, regime: dict) -> None:
        fast = regime.get("fast_layer", {}) or {}
        if fast.get("15m_spread_deterioration"):
            opp.spread_state = "CRITICAL" if fast.get("market_dislocation") else "WIDE"
            opp.risk_warnings.append("spread_deterioration")
            return
        if fast.get("price_vwap_dislocation"):
            opp.spread_state = "WIDE"
            opp.risk_warnings.append("price_dislocation")
            return
        opp.spread_state = "GOOD"

    def _inject_liquidity_state(self, opp: Opportunity, symbol: str) -> None:
        ticker = self._store.get_ticker(symbol)
        if ticker is None:
            opp.liquidity_state = "CRITICAL"
            return
        vol_24h = float(ticker.get("volume_24h", 0) or 0)
        if vol_24h >= 10_000_000:
            opp.liquidity_state = "OK"
        elif vol_24h >= 2_000_000:
            opp.liquidity_state = "LOW"
        else:
            opp.liquidity_state = "CRITICAL"

    def _inject_supervision_context(self, opp: Opportunity, regime: dict) -> None:
        fast = regime.get("fast_layer", {}) or {}
        opp.supervision_flags = list(fast.get("signals", []))

        failure_labels = list(opp.failure_pattern_labels)
        if opp.liquidity_state == "CRITICAL":
            failure_labels.append("liquidity_breakdown")
        elif opp.liquidity_state == "LOW":
            failure_labels.append("thin_liquidity")

        if opp.spread_state == "CRITICAL":
            failure_labels.append("spread_dislocation")
        elif opp.spread_state == "WIDE":
            failure_labels.append("wide_spread")

        if opp.funding_state in ("EXTREME_LONG", "EXTREME_SHORT"):
            failure_labels.append("crowded_positioning")
        if opp.oi_state in ("SPIKE", "DIVERGENCE"):
            failure_labels.append("oi_instability")

        if fast.get("market_dislocation"):
            failure_labels.append("market_dislocation")
        if fast.get("liquidation_risk_proxy"):
            failure_labels.append("liquidation_risk_proxy")
            opp.risk_warnings.append("liquidation_risk_proxy")
        if fast.get("rapid_oi_change"):
            failure_labels.append("rapid_oi_change")
        if fast.get("funding_surge") or fast.get("funding_dislocation"):
            failure_labels.append("funding_dislocation")
        if fast.get("price_vwap_dislocation"):
            failure_labels.append("price_dislocation")

        opp.failure_pattern_labels = list(dict.fromkeys(failure_labels))
        opp.risk_warnings = list(dict.fromkeys(opp.risk_warnings))

        snapshot = self._get_regime_snapshot(opp)
        snapshot["category_label"] = opp.category_label
        snapshot["strategy_family"] = opp.strategy_family
        snapshot["supervision_flags"] = opp.supervision_flags
        snapshot["failure_pattern_labels"] = opp.failure_pattern_labels
        snapshot["risk_warnings"] = opp.risk_warnings
        snapshot["fast_alert_level"] = fast.get("alert_level", "NONE")
        opp.regime_snapshot = json.dumps(snapshot)

    @staticmethod
    def _category_label(category: str, side: str) -> str:
        label_map = {
            "reversal": "countertrend_reversal",
            "breakout": "range_expansion_breakout",
            "trend": "directional_trend_follow",
            "pattern": "discretionary_pattern",
            "bear_trend": "directional_bear_trend",
            "range": "mean_reversion_range",
            "volatility": "volatility_momentum",
        }
        label = label_map.get(category, category or "uncategorized")
        if side in ("LONG", "SHORT") and category in ("trend", "bear_trend", "reversal", "breakout", "volatility"):
            return f"{label}:{side.lower()}"
        return label

    @staticmethod
    def _get_regime_snapshot(opp: Opportunity) -> dict:
        try:
            return json.loads(opp.regime_snapshot) if opp.regime_snapshot else {}
        except Exception:
            return {}

    @classmethod
    def _get_fast_layer(cls, opp: Opportunity) -> dict:
        return cls._get_regime_snapshot(opp).get("fast_layer", {}) or {}
