"""
StrategyManager — v1.3 Opportunity-driven pipeline.

기존 strategy-driven 흐름을 폐기하고 새 흐름으로 교체:
  Data → Strategy Signals
       → Opportunity Normalizer
       → Scoring Engine
       → Opportunity Queue (Ranking + Top-N)
       → Portfolio Constraint Check
       → PaperRecorder (PAPER) / Execution (LIVE)

이전 전략(ema_cross, rsi_exhaustion, range_breakout)도 하위 호환 유지.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Dict, List, Optional

from bot.strategies._base import Signal, StrategyBase
from bot.strategies.signal_bus import SignalBus
from bot.strategies.paper_recorder import PaperRecorder
from bot.strategies.opportunity import OpportunityNormalizer, Opportunity
from bot.strategies.scoring import ScoringEngine
from bot.strategies.opportunity_queue import OpportunityQueue
from bot.strategies.strategy_health import StrategyHealthEngine
from bot.market.symbol_universe import SymbolUniverse

# v1.3 새 전략 3개
from bot.strategies.overreaction_reversal import OverreactionReversalStrategy
from bot.strategies.volatility_expansion_breakout import VolatilityExpansionBreakoutStrategy
from bot.strategies.early_trend_capture import EarlyTrendCaptureStrategy
# 이미지 패턴 전략 (사용자 정의)
from bot.strategies.image_pattern_strategy import ImagePatternStrategy
# Phase 5 — 추가 전략 3개 (legacy PAUSED)
from bot.strategies.ema_cross import EmaCrossStrategy
from bot.strategies.rsi_exhaustion import RsiExhaustionStrategy
from bot.strategies.range_breakout import RangeBreakoutStrategy
# 방향성 강화 신규 전략 3개
from bot.strategies.bear_trend import BearTrendStrategy
from bot.strategies.range_trader import RangeTraderStrategy
from bot.strategies.volatility_momentum import VolatilityMomentumStrategy

if TYPE_CHECKING:
    from bot.data.store import DataStore

logger = logging.getLogger(__name__)

ALLOWED_LIFECYCLE = {"PAPER", "SHADOW", "ACTIVE"}

# 새 전략(overreaction_reversal 등)과 기능이 중복되는 레거시 전략
# initialize() 시 DB 상태와 무관하게 PAUSED로 고정
LEGACY_STRATEGIES = {"ema_cross", "rsi_exhaustion", "range_breakout"}

STRATEGY_FAMILY_MAP: Dict[str, str] = {
    "overreaction_reversal": "countertrend",
    "volatility_expansion_breakout": "expansion",
    "early_trend_capture": "trend_following",
    "image_pattern": "pattern_recognition",
    "ema_cross": "trend_following",
    "rsi_exhaustion": "countertrend",
    "range_breakout": "expansion",
    "bear_trend": "trend_following",
    "range_trader": "mean_reversion",
    "volatility_momentum": "momentum",
}

STRATEGY_FAILURE_PATTERN_MAP: Dict[str, List[str]] = {
    "overreaction_reversal": ["trend_persistence", "crowded_catch_falling_knife"],
    "volatility_expansion_breakout": ["false_breakout", "late_expansion_entry"],
    "early_trend_capture": ["whipsaw_reversal", "weak_follow_through"],
    "image_pattern": ["pattern_subjectivity", "confirmation_lag"],
    "ema_cross": ["lagging_entry", "sideways_whipsaw"],
    "rsi_exhaustion": ["persistent_overbought_oversold", "premature_reversal"],
    "range_breakout": ["range_fakeout", "low_volume_break"],
    "bear_trend": ["short_squeeze", "late_short_crowding"],
    "range_trader": ["range_break_regime_shift", "stop_cluster_hunt"],
    "volatility_momentum": ["volatility_compression_fakeout", "exhaustion_gap"],
}


class StrategyManager:
    """
    v1.3 Opportunity-driven StrategyManager.

    Usage
    -----
    manager = StrategyManager(store)
    manager.initialize()
    signals, opportunities = manager.run_all(regime)
    top = manager.opp_queue.top_n()
    """

    def __init__(self, store: "DataStore") -> None:
        self._store = store

        # Core components
        self._bus      = SignalBus(store)
        self._recorder = PaperRecorder(store)
        self._bus.set_paper_recorder(self._recorder)

        # Opportunity pipeline
        self._normalizer  = OpportunityNormalizer(store)
        self._scorer      = ScoringEngine()
        self._opp_queue   = OpportunityQueue(top_n_live=2)

        # v1.3 전략 3개 + 이미지 패턴 전략 + Phase 5 추가 전략 3개
        self._image_pattern_strategy = ImagePatternStrategy()
        self._strategies: List[StrategyBase] = [
            OverreactionReversalStrategy(),
            VolatilityExpansionBreakoutStrategy(),
            EarlyTrendCaptureStrategy(),
            self._image_pattern_strategy,
            # Phase 5 — legacy (PAUSED 자동 처리됨)
            EmaCrossStrategy(),
            RsiExhaustionStrategy(),
            RangeBreakoutStrategy(),
            # 방향성 강화 신규 전략
            BearTrendStrategy(),
            RangeTraderStrategy(),
            VolatilityMomentumStrategy(),
        ]

        self._state: Dict[str, dict] = {}

        # Phase E — Strategy Health Engine (initialized after manager is fully set up)
        self._health_engine: Optional["StrategyHealthEngine"] = None

        # Symbol Universe
        self._universe = SymbolUniverse()

    # ---------------------------------------------------------------------- #
    # Initialization
    # ---------------------------------------------------------------------- #

    def initialize(self) -> None:
        # Health engine needs 'self' to be fully constructed first
        self._health_engine = StrategyHealthEngine(self._store, self)

        # Symbol Universe — config에서 심볼 목록 로드
        try:
            from bot.config import get_config
            self._universe.initialize_from_config(get_config().tracked_symbols)
        except Exception:
            pass

        for strategy in self._strategies:
            strategy_profile = self.get_strategy_profile(strategy.name, strategy.category)
            existing = self._store.get_strategy_state(strategy.name)
            if existing is None:
                record = {
                    "name":            strategy.name,
                    "mode":            "PAPER",
                    "category":        strategy.category,
                    "regime_filter":   json.dumps(strategy.regime_filter),
                    "stats_json":      json.dumps(strategy_profile),
                    "last_signal_ts":  None,
                    "lifecycle_stage": "paper",
                }
                self._store.upsert_strategy_state(record)
                self._state[strategy.name] = record
                logger.info("[StrategyManager] Registered '%s' (PAPER)", strategy.name)
            else:
                self._state[strategy.name] = dict(existing)
                existing_meta = self._parse_stats_json(existing.get("stats_json"))
                merged_meta = {**strategy_profile, **existing_meta}
                self._state[strategy.name]["stats_json"] = json.dumps(merged_meta)
                self._store.upsert_strategy_state({
                    "name": strategy.name,
                    "stats_json": self._state[strategy.name]["stats_json"],
                })
                logger.info(
                    "[StrategyManager] Loaded '%s' (mode=%s)",
                    strategy.name, existing.get("mode", "PAPER"),
                )

            # 레거시 전략은 DB 값과 무관하게 PAUSED 강제
            if strategy.name in LEGACY_STRATEGIES:
                self._state[strategy.name]["mode"] = "PAUSED"
                self._store.upsert_strategy_state({
                    "name": strategy.name,
                    "mode": "PAUSED",
                })
                logger.info("[StrategyManager] '%s' → PAUSED (legacy/redundant)", strategy.name)

    # ---------------------------------------------------------------------- #
    # Main execution cycle
    # ---------------------------------------------------------------------- #

    def run_all(self, regime: dict) -> List[Signal]:
        """
        v1.3 파이프라인 실행:
          1. 각 전략 compute() → Signal[]
          2. Signal → Opportunity (정규화)
          3. Opportunity → 스코어
          4. OpportunityQueue에 추가 (순위 산정)
          5. PaperRecorder에 top-N 기회 전달 (PAPER 포지션 생성)
          6. 기존 포지션 TP/SL 체크

        Returns: 전체 Signal 목록 (대시보드/SignalBus 하위 호환용)
        """
        all_signals: List[Signal] = []
        run_ts = int(time.time() * 1000)
        recent_opps = self._opp_queue.get_recent(limit=50)

        for strategy in self._strategies:
            state = self._state.get(strategy.name, {})
            mode  = state.get("mode", "PAPER")

            if mode == "PAUSED":
                continue

            try:
                signals = strategy.compute(self._store, regime)
            except Exception as exc:
                logger.error("[StrategyManager] '%s' error: %s", strategy.name, exc, exc_info=True)
                signals = []

            for sig in signals:
                sig.mode = "PAPER" if mode != "LIVE" else "LIVE"
                sig.normalized_category = strategy.category
                sig.strategy_family = STRATEGY_FAMILY_MAP.get(strategy.name, strategy.category)
                sig.failure_pattern_labels = list(
                    STRATEGY_FAILURE_PATTERN_MAP.get(strategy.name, [])
                )

            # ── Opportunity 파이프라인 ─────────────────────────────────── #
            for sig in signals:
                if sig.action == "SKIP":
                    continue
                try:
                    opp = self._normalizer.normalize(sig, regime)
                    if opp is None:
                        continue
                    opp = self._scorer.score(opp, regime, recent_opps)
                    sig.normalized_category = opp.category_label
                    sig.failure_pattern_labels = list(opp.failure_pattern_labels)
                    sig.risk_warnings = list(opp.risk_warnings)
                    sig.score_total = opp.score_total
                    sig.score_breakdown = dict(opp.score_breakdown)
                    sig.opportunity_id = opp.id
                    self._opp_queue.add(opp)

                    # DB 저장
                    self._store.save_opportunity(opp.to_dict())

                    logger.info(
                        "[OppPipeline] %s %s %s/%s score=%d status=%s flags=%s failures=%s",
                        opp.symbol, opp.side, opp.category, opp.category_label,
                        opp.score_total,
                        "ACTIONABLE" if opp.is_actionable else ("WATCH" if opp.is_watch else "IGNORE"),
                        opp.supervision_flags[:3],
                        opp.failure_pattern_labels[:3],
                    )
                except Exception as exc:
                    logger.error("[OppPipeline] Normalize/score error: %s", exc)

            # ── 기존 SignalBus 경로 (하위 호환: 대시보드/audit trail용) ── #
            if signals:
                self._bus.publish(signals, strategy)
                actionable = [s for s in signals if s.action != "SKIP"]
                if actionable:
                    self._store.upsert_strategy_state({
                        "name": strategy.name,
                        "last_signal_ts": run_ts,
                    })
                    self._state[strategy.name]["last_signal_ts"] = run_ts

            all_signals.extend(signals)

        # ── Top-N → PaperRecorder ─────────────────────────────────────── #
        # 기본 min_score=8, 심볼 계층에 따라 개별 필터링
        top_opps = self._opp_queue.top_n(n=2, min_score=8)
        filtered_opps = []
        for opp in top_opps:
            sym_cfg = self._universe.get_symbol_config(opp.symbol)
            if opp.score_total >= sym_cfg["min_score"]:
                filtered_opps.append(opp)
            else:
                logger.debug(
                    "[Universe] '%s' score=%d < tier%d min_score=%d — skipped",
                    opp.symbol, opp.score_total, sym_cfg["tier"], sym_cfg["min_score"],
                )
        top_opps = filtered_opps

        for opp in top_opps:
            # PENDING 상태만 처리 (중복 방지)
            if opp.execution_status != "PENDING":
                continue
            # Paper 포지션 생성
            self._recorder.on_signal(
                _opp_to_signal(opp)
            )
            self._opp_queue.mark_executed(opp.id)
            logger.info(
                "[StrategyManager] Top-N paper executed: %s %s score=%d",
                opp.symbol, opp.side, opp.score_total,
            )

        # TP/SL 체크
        self._recorder.check_positions()

        # Phase E — Strategy Health check (15분 주기)
        if self._health_engine is not None:
            self._health_engine.run_health_check()

        # Stale approval 만료 처리 및 DB 동기화
        stale = self._opp_queue.expire_stale_approvals()
        for opp in stale:
            try:
                self._store.save_opportunity(opp.to_dict())
            except Exception as exc:
                logger.error("[StrategyManager] Stale approval DB sync error: %s", exc)

        return all_signals

    # ---------------------------------------------------------------------- #
    # Accessors
    # ---------------------------------------------------------------------- #

    @property
    def bus(self) -> SignalBus:
        return self._bus

    @property
    def recorder(self) -> PaperRecorder:
        return self._recorder

    @property
    def universe(self) -> SymbolUniverse:
        return self._universe

    @property
    def health_engine(self) -> Optional["StrategyHealthEngine"]:
        return self._health_engine

    @property
    def opp_queue(self) -> OpportunityQueue:
        return self._opp_queue

    def get_strategy_list(self) -> List[dict]:
        result = []
        for strategy in self._strategies:
            state = self._state.get(strategy.name, {})
            stats_meta = self._parse_stats_json(state.get("stats_json", "{}") or "{}")
            result.append({
                "name":           strategy.name,
                "category":       strategy.category,
                "category_label": stats_meta.get("category_label", strategy.category),
                "strategy_family": stats_meta.get("strategy_family", STRATEGY_FAMILY_MAP.get(strategy.name, strategy.category)),
                "failure_patterns": stats_meta.get("failure_patterns", []),
                "failure_pattern_labels": stats_meta.get("failure_patterns", []),
                "regime_filter":  strategy.regime_filter,
                "mode":           state.get("mode", "PAPER"),
                "last_signal_ts": state.get("last_signal_ts"),
                "lifecycle_stage": state.get("lifecycle_stage", "paper"),
                "supervision_stage": stats_meta.get("supervision_stage", "standard"),
                "health_status":  state.get("health_status", "OK"),
            })
        return result

    def set_strategy_mode(self, name: str, mode: str) -> bool:
        valid_modes = {"PAPER", "SHADOW", "PAUSED", "LIVE"}
        if mode not in valid_modes or name not in self._state:
            return False
        self._state[name]["mode"] = mode
        self._store.upsert_strategy_state({"name": name, "mode": mode})
        logger.info("[StrategyManager] '%s' mode → %s", name, mode)
        return True

    def get_bus_stats(self) -> dict:
        return self._bus.get_stats()

    def get_strategy_profile(self, name: str, category: str = "") -> dict:
        resolved_category = category or next(
            (s.category for s in self._strategies if s.name == name),
            "",
        )
        return {
            "category_label": self._build_category_label(name, resolved_category),
            "strategy_family": STRATEGY_FAMILY_MAP.get(name, resolved_category or "uncategorized"),
            "failure_patterns": list(STRATEGY_FAILURE_PATTERN_MAP.get(name, [])),
            "supervision_stage": "paper_bootstrap",
        }

    @staticmethod
    def _parse_stats_json(raw) -> dict:
        try:
            return json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except Exception:
            return {}

    @staticmethod
    def _build_category_label(name: str, category: str) -> str:
        if name == "overreaction_reversal":
            return "countertrend_reversal"
        if name == "volatility_expansion_breakout":
            return "range_expansion_breakout"
        if name == "early_trend_capture":
            return "directional_trend_follow"
        if name == "bear_trend":
            return "directional_bear_trend"
        if name == "range_trader":
            return "mean_reversion_range"
        if name == "volatility_momentum":
            return "volatility_momentum"
        return category or "uncategorized"


# --------------------------------------------------------------------------- #
# Helper: Opportunity → Signal (PaperRecorder 하위 호환)
# --------------------------------------------------------------------------- #

def _opp_to_signal(opp: Opportunity) -> Signal:
    """Opportunity를 PaperRecorder가 받을 수 있는 Signal로 변환."""
    return Signal(
        id       = opp.signal_id or str(uuid.uuid4()),
        ts       = opp.ts,
        strategy = opp.source_strategy,
        symbol   = opp.symbol,
        action   = "BUY" if opp.side == "LONG" else "SELL",
        mode     = "PAPER",
        confidence = opp.confidence_raw,
        regime   = json.loads(opp.regime_snapshot).get("regime", "UNKNOWN") if opp.regime_snapshot else "UNKNOWN",
        reason   = f"Opportunity score={opp.score_total} category={opp.category}",
        tp       = opp.tp,
        sl       = opp.sl,
        normalized_category = opp.category_label,
        strategy_family = opp.strategy_family,
        failure_pattern_labels = list(opp.failure_pattern_labels),
        risk_warnings = list(opp.risk_warnings),
        score_total = opp.score_total,
        score_breakdown = dict(opp.score_breakdown),
        opportunity_id = opp.id,
    )
