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

if TYPE_CHECKING:
    from bot.data.store import DataStore

logger = logging.getLogger(__name__)

ALLOWED_LIFECYCLE = {"PAPER", "SHADOW", "ACTIVE"}


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

        # v1.3 전략 3개 + 이미지 패턴 전략 (우선순위 순)
        self._image_pattern_strategy = ImagePatternStrategy()
        self._strategies: List[StrategyBase] = [
            OverreactionReversalStrategy(),
            VolatilityExpansionBreakoutStrategy(),
            EarlyTrendCaptureStrategy(),
            self._image_pattern_strategy,
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
            existing = self._store.get_strategy_state(strategy.name)
            if existing is None:
                record = {
                    "name":            strategy.name,
                    "mode":            "PAPER",
                    "category":        strategy.category,
                    "regime_filter":   json.dumps(strategy.regime_filter),
                    "stats_json":      "{}",
                    "last_signal_ts":  None,
                    "lifecycle_stage": "paper",
                }
                self._store.upsert_strategy_state(record)
                self._state[strategy.name] = record
                logger.info("[StrategyManager] Registered '%s' (PAPER)", strategy.name)
            else:
                self._state[strategy.name] = dict(existing)
                logger.info(
                    "[StrategyManager] Loaded '%s' (mode=%s)",
                    strategy.name, existing.get("mode", "PAPER"),
                )

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

            # ── Opportunity 파이프라인 ─────────────────────────────────── #
            for sig in signals:
                if sig.action == "SKIP":
                    continue
                try:
                    opp = self._normalizer.normalize(sig, regime)
                    if opp is None:
                        continue
                    opp = self._scorer.score(opp, regime, recent_opps)
                    self._opp_queue.add(opp)

                    # DB 저장
                    self._store.save_opportunity(opp.to_dict())

                    logger.info(
                        "[OppPipeline] %s %s %s  score=%d  status=%s",
                        opp.symbol, opp.side, opp.category,
                        opp.score_total,
                        "ACTIONABLE" if opp.is_actionable else ("WATCH" if opp.is_watch else "IGNORE"),
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
            result.append({
                "name":           strategy.name,
                "category":       strategy.category,
                "regime_filter":  strategy.regime_filter,
                "mode":           state.get("mode", "PAPER"),
                "last_signal_ts": state.get("last_signal_ts"),
                "lifecycle_stage": state.get("lifecycle_stage", "paper"),
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
    )
