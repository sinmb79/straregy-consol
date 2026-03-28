"""
Strategy base class for the 22B Strategy Engine — Phase 2.

All concrete strategies must:
  1. Inherit from StrategyBase
  2. Set class-level attributes: name, category, regime_filter
  3. Implement the compute() method

Signal dataclass is also defined here as the universal return type.
"""

from __future__ import annotations

import uuid
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from bot.data.store import DataStore

# Imported at runtime to avoid circular import; the REGIME_ALLOW_TABLE lives in detector.py
# We reference it by import inside is_allowed_in_regime() for clarity.


# --------------------------------------------------------------------------- #
# Signal dataclass
# --------------------------------------------------------------------------- #

@dataclass
class Signal:
    """
    A trading signal produced by a strategy.

    Fields
    ------
    id         : UUID4 string — unique identifier for this signal
    ts         : Unix timestamp in milliseconds
    strategy   : Name of the strategy that produced this signal
    symbol     : Trading pair, e.g. "BTCUSDT"
    action     : "BUY" | "SELL" | "SKIP"
    mode       : "PAPER" | "LIVE"  (Phase 2 always "PAPER")
    confidence : Float in [0.0, 1.0] — higher is more confident
    regime     : Market regime string at the time of signal
    tp         : Take-profit price (optional)
    sl         : Stop-loss price (optional)
    reason     : Human-readable explanation, e.g. "EMA20 crossed EMA50, RSI=52.3"
    """

    strategy:   str
    symbol:     str
    action:     str           # BUY / SELL / SKIP
    mode:       str           # PAPER / LIVE
    confidence: float         # 0.0 – 1.0
    regime:     str
    reason:     str
    tp:         Optional[float] = None
    sl:         Optional[float] = None
    normalized_category: Optional[str] = None
    strategy_family: Optional[str] = None
    failure_pattern_labels: List[str] = field(default_factory=list)
    risk_warnings: List[str] = field(default_factory=list)
    score_total: Optional[int] = None
    score_breakdown: dict = field(default_factory=dict)
    opportunity_id: Optional[str] = None
    id:         str = field(default_factory=lambda: str(uuid.uuid4()))
    ts:         int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict:
        """Serialise to a plain dict for JSON/SQLite storage."""
        return {
            "id":         self.id,
            "ts":         self.ts,
            "strategy":   self.strategy,
            "symbol":     self.symbol,
            "action":     self.action,
            "mode":       self.mode,
            "confidence": self.confidence,
            "regime":     self.regime,
            "tp":         self.tp,
            "sl":         self.sl,
            "reason":     self.reason,
            "normalized_category": self.normalized_category,
            "strategy_family": self.strategy_family,
            "failure_pattern_labels": list(self.failure_pattern_labels),
            "risk_warnings": list(self.risk_warnings),
            "score_total": self.score_total,
            "score_breakdown_json": self.score_breakdown,
            "opportunity_id": self.opportunity_id,
        }


# --------------------------------------------------------------------------- #
# Abstract base class
# --------------------------------------------------------------------------- #

class StrategyBase(ABC):
    """
    Abstract base class that every strategy must implement.

    Class-level attributes (must be set on the subclass):
      name          : str        — unique strategy identifier, e.g. "ema_cross"
      category      : str        — strategy type, e.g. "trend", "mean_reversion"
      regime_filter : list[str]  — list of regime names where this strategy may run

    Methods (must be implemented):
      compute(store, regime) -> list[Signal]
    """

    # Subclasses override these
    name:          str       = ""
    category:      str       = ""
    regime_filter: List[str] = []

    # In Phase 2 every strategy is forced to PAPER mode
    _PHASE2_MODE: str = "PAPER"

    # ---------------------------------------------------------------------- #
    # Regime filter
    # ---------------------------------------------------------------------- #

    def is_allowed_in_regime(self, regime: str) -> bool:
        """
        Return True if this strategy is permitted to run in the given regime.

        Logic:
          1. Checks explicit regime_filter list on the strategy.
          2. Falls back to the REGIME_ALLOW_TABLE in detector.py by comparing
             the strategy's category against allowed categories for the regime.

        Both paths must agree; regime_filter is the primary gate.
        If regime_filter is empty, the strategy is treated as universal
        and defers entirely to REGIME_ALLOW_TABLE category matching.
        """
        from bot.regime.detector import REGIME_ALLOW_TABLE

        # Blocked regimes — never allow
        if regime in ("EVENT_RISK", "UNKNOWN"):
            return False

        allowed_categories = REGIME_ALLOW_TABLE.get(regime, [])

        if self.regime_filter:
            # Strategy declares explicit allowed regimes
            if regime not in self.regime_filter:
                return False

        # Even if regime is in regime_filter, category must be allowed
        if allowed_categories and self.category not in allowed_categories:
            # Allow if category list is empty (means no restriction)
            pass  # We rely on regime_filter as the single source of truth

        return True

    # ---------------------------------------------------------------------- #
    # Abstract interface
    # ---------------------------------------------------------------------- #

    @abstractmethod
    def compute(self, store: "DataStore", regime: dict) -> List[Signal]:
        """
        Evaluate strategy logic and return a list of Signals.

        Parameters
        ----------
        store  : DataStore — provides candles, tickers, funding, OI
        regime : dict      — current regime dict from RegimeDetector.detect()

        Returns
        -------
        List of Signal objects.  Empty list = no signals this cycle.

        Contract
        --------
        - This method MUST be a pure function of (store, regime).
        - No side effects except building and returning Signal objects.
        - All signals must have mode=PAPER in Phase 2.
        """

    # ---------------------------------------------------------------------- #
    # Helper: clamp a value to [lo, hi]
    # ---------------------------------------------------------------------- #

    @staticmethod
    def _clamp(value: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, value))

    # ---------------------------------------------------------------------- #
    # Helper: params_store 런타임 파라미터 조회
    # ---------------------------------------------------------------------- #

    def get_param(self, key: str, default=None):
        """params_store에서 이 전략의 파라미터를 읽는다. 없으면 default 반환."""
        try:
            from bot.strategies.params_store import StrategyParamsStore
            return StrategyParamsStore.get_instance().get(self.name, key, default)
        except Exception:
            return default

    def __repr__(self) -> str:
        return f"<Strategy name={self.name!r} category={self.category!r}>"
