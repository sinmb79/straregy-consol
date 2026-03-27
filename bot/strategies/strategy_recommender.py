"""
StrategyRecommender — 봇+AI 전략 추천 코너 (Master Plan Extension)

매매 결과를 기반으로 어떤 전략을 활성화/비활성화/조정할지 추천하고
근거를 함께 제시한다. 사용자가 승인하면 실제로 적용된다.

추천 유형:
  ACTIVATE   — PAUSED 전략을 다시 PAPER로 복원 (성과/레짐 근거)
  PROMOTE    — PAPER → SHADOW 또는 SHADOW → LIVE 승격
  SUSPEND    — 성과 부진 전략을 PAUSED로 전환
  ADJUST     — 파라미터 변경 제안 (RSI 기준, TP/SL 등)
  FOCUS      — 현재 레짐에서 특히 유리한 전략 강조

추천 흐름:
  1. StrategyRecommender.generate_recommendations() 호출
  2. DataStore.save_strategy_recommendation() 로 저장
  3. 대시보드 전략 추천 패널에서 표시
  4. 사용자 승인/거절 → apply_recommendation() 실행
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from bot.data.store import DataStore
    from bot.strategies.manager import StrategyManager

logger = logging.getLogger(__name__)

# 최소 거래 수 (추천 생성 기준)
MIN_TRADES_FOR_RECOMMENDATION = 5

# 레짐별 유리한 전략 카테고리
REGIME_BEST_FIT: Dict[str, List[str]] = {
    "BTC_BULLISH":      ["trend", "breakout"],
    "BTC_BEARISH":      ["reversal"],
    "BTC_SIDEWAYS":     ["reversal", "breakout"],
    "HIGH_VOLATILITY":  ["reversal"],
    "LOW_VOLATILITY":   ["breakout"],
    "ALT_ROTATION":     ["trend", "reversal"],
    "EVENT_RISK":       [],
    "UNKNOWN":          [],
}

# 카테고리별 전략명 매핑
CATEGORY_STRATEGIES: Dict[str, List[str]] = {
    "reversal":  ["overreaction_reversal"],
    "breakout":  ["volatility_expansion_breakout"],
    "trend":     ["early_trend_capture"],
    "pattern":   ["image_pattern"],
}


class StrategyRecommendation:
    """단일 전략 추천 객체."""

    def __init__(
        self,
        rec_type: str,
        strategy: str,
        title: str,
        reasoning: str,
        supporting_data: dict,
        action_params: Optional[dict] = None,
    ) -> None:
        self.id = str(uuid.uuid4())
        self.ts = int(time.time() * 1000)
        self.rec_type = rec_type        # ACTIVATE | PROMOTE | SUSPEND | ADJUST | FOCUS
        self.strategy = strategy
        self.title = title
        self.reasoning = reasoning
        self.supporting_data = supporting_data
        self.action_params = action_params or {}
        self.status = "PENDING"         # PENDING | APPROVED | REJECTED
        self.decided_at: Optional[int] = None
        self.decided_by: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id":             self.id,
            "ts":             self.ts,
            "rec_type":       self.rec_type,
            "strategy":       self.strategy,
            "title":          self.title,
            "reasoning":      self.reasoning,
            "supporting_data": self.supporting_data,
            "action_params":  self.action_params,
            "status":         self.status,
            "decided_at":     self.decided_at,
            "decided_by":     self.decided_by,
        }


class StrategyRecommender:
    """
    전략 추천 엔진.

    Usage
    -----
    recommender = StrategyRecommender(store, strategy_manager)
    recs = recommender.generate_recommendations(regime)
    recommender.apply_recommendation(rec_id, approved=True, decided_by="operator")
    """

    CHECK_INTERVAL_MS = 30 * 60 * 1000   # 30분마다 자동 갱신

    def __init__(self, store: "DataStore", strategy_manager: "StrategyManager") -> None:
        self._store = store
        self._manager = strategy_manager
        self._pending: Dict[str, StrategyRecommendation] = {}   # id → recommendation
        self._history: List[dict] = []
        self._last_generated_ts: int = 0

    # ---------------------------------------------------------------------- #
    # Public API
    # ---------------------------------------------------------------------- #

    def maybe_generate(self, regime: dict) -> None:
        """주기 체크 후 필요하면 추천 갱신."""
        now = int(time.time() * 1000)
        if now - self._last_generated_ts >= self.CHECK_INTERVAL_MS:
            self.generate_recommendations(regime)

    def generate_recommendations(self, regime: dict) -> List[dict]:
        """
        전체 전략을 분석해 추천 목록을 생성한다.
        이미 PENDING인 추천이 있는 전략은 중복 생성하지 않는다.
        """
        self._last_generated_ts = int(time.time() * 1000)
        current_regime = regime.get("regime", "UNKNOWN")
        strategies = self._manager.get_strategy_list()
        new_recs: List[StrategyRecommendation] = []

        # 이미 PENDING인 전략 집합
        already_pending = {r.strategy for r in self._pending.values() if r.status == "PENDING"}

        for info in strategies:
            name = info["name"]
            mode = info.get("mode", "PAPER")
            category = info.get("category", "")

            if name in already_pending:
                continue

            stats = self._get_stats(name)
            n = stats.get("trade_count", 0)

            # 데이터 부족 전략은 건너뜀
            if n < MIN_TRADES_FOR_RECOMMENDATION and mode != "PAUSED":
                continue

            rec = self._analyze_strategy(
                name=name, mode=mode, category=category,
                stats=stats, current_regime=current_regime, regime=regime,
            )
            if rec is not None:
                new_recs.append(rec)
                self._pending[rec.id] = rec

        # 레짐 적합 전략 FOCUS 추천
        focus_rec = self._maybe_focus_recommendation(current_regime, strategies, already_pending)
        if focus_rec:
            new_recs.append(focus_rec)
            self._pending[focus_rec.id] = focus_rec

        if new_recs:
            logger.info(
                "[StrategyRecommender] Generated %d recommendations for regime %s",
                len(new_recs), current_regime,
            )

        return [r.to_dict() for r in new_recs]

    def get_pending(self) -> List[dict]:
        """현재 PENDING 상태 추천 목록."""
        return [
            r.to_dict() for r in self._pending.values()
            if r.status == "PENDING"
        ]

    def get_history(self, limit: int = 20) -> List[dict]:
        """처리 완료된 추천 이력 (최신순)."""
        return list(reversed(self._history[-limit:]))

    def apply_recommendation(
        self,
        rec_id: str,
        approved: bool,
        decided_by: str = "operator",
        reason: str = "",
    ) -> dict:
        """
        추천을 승인하거나 거절한다.
        승인하면 실제로 전략 모드를 변경한다.
        """
        rec = self._pending.get(rec_id)
        if rec is None:
            return {"ok": False, "error": "Recommendation not found"}
        if rec.status != "PENDING":
            return {"ok": False, "error": f"Already decided: {rec.status}"}

        rec.status = "APPROVED" if approved else "REJECTED"
        rec.decided_at = int(time.time() * 1000)
        rec.decided_by = decided_by

        result = {"ok": True, "rec_id": rec_id, "status": rec.status}

        if approved:
            applied = self._apply_action(rec)
            result["applied"] = applied

        # 이력으로 이동
        self._history.append({**rec.to_dict(), "decision_reason": reason})
        del self._pending[rec_id]

        logger.info(
            "[StrategyRecommender] Recommendation %s %s by %s: %s",
            rec_id, rec.status, decided_by, rec.title,
        )
        return result

    # ---------------------------------------------------------------------- #
    # Internal analysis
    # ---------------------------------------------------------------------- #

    def _analyze_strategy(
        self,
        name: str,
        mode: str,
        category: str,
        stats: dict,
        current_regime: str,
        regime: dict,
    ) -> Optional[StrategyRecommendation]:
        """단일 전략 분석 → 추천 or None."""

        pf10 = stats.get("recent_10_pf")
        pf20 = stats.get("recent_20_pf")
        mdd = stats.get("recent_mdd") or 0.0
        expectancy = stats.get("recent_expectancy") or 0.0
        win_rate = stats.get("win_rate", 0.0)
        n = stats.get("trade_count", 0)

        regime_fit = category in REGIME_BEST_FIT.get(current_regime, [])

        # ── 1. SUSPEND: 성과 부진 전략 ─────────────────────────────────── #
        if mode in ("PAPER", "SHADOW", "LIVE") and n >= MIN_TRADES_FOR_RECOMMENDATION:
            if pf10 is not None and pf10 < 0.8:
                return StrategyRecommendation(
                    rec_type="SUSPEND",
                    strategy=name,
                    title=f"{name} 일시 중단 권고",
                    reasoning=(
                        f"최근 10거래 Profit Factor가 {pf10:.2f}로 기준(0.8) 미달입니다. "
                        f"Win Rate: {win_rate:.0%}, MDD: {mdd:.1f}%, "
                        f"기댓값: {expectancy:.4f}. "
                        f"파라미터 재검토 또는 레짐 변화 확인이 필요합니다."
                    ),
                    supporting_data={
                        "recent_10_pf": pf10, "recent_20_pf": pf20,
                        "mdd": mdd, "win_rate": win_rate,
                        "trade_count": n, "current_regime": current_regime,
                    },
                    action_params={"target_mode": "PAUSED"},
                )

        # ── 2. ACTIVATE: 중단됐지만 레짐이 잘 맞는 전략 ─────────────────── #
        if mode == "PAUSED" and regime_fit and n >= MIN_TRADES_FOR_RECOMMENDATION:
            if pf10 is not None and pf10 >= 1.0:
                return StrategyRecommendation(
                    rec_type="ACTIVATE",
                    strategy=name,
                    title=f"{name} 재활성화 권고",
                    reasoning=(
                        f"현재 레짐 '{current_regime}'은 {category} 카테고리에 유리합니다. "
                        f"과거 Profit Factor: {pf10:.2f}, Win Rate: {win_rate:.0%}. "
                        f"성과가 기준 이상이므로 PAPER 모드 재개를 권장합니다."
                    ),
                    supporting_data={
                        "recent_10_pf": pf10, "mdd": mdd,
                        "win_rate": win_rate, "regime_fit": True,
                        "current_regime": current_regime,
                    },
                    action_params={"target_mode": "PAPER"},
                )

        # ── 3. PROMOTE: PAPER 전략이 승격 조건 충족 ───────────────────────── #
        if mode == "PAPER" and n >= 30 and pf10 is not None:
            if pf10 >= 1.2 and expectancy > 0 and mdd > -10.0:
                return StrategyRecommendation(
                    rec_type="PROMOTE",
                    strategy=name,
                    title=f"{name} SHADOW 승격 권고",
                    reasoning=(
                        f"PAPER 거래 {n}회 완료. "
                        f"Profit Factor: {pf10:.2f} (기준 ≥1.2), "
                        f"기댓값: {expectancy:.4f} (양수), "
                        f"MDD: {mdd:.1f}% (기준 >-10%). "
                        f"SHADOW 모드로 실제 시장 노출 없이 추가 검증을 권장합니다."
                    ),
                    supporting_data={
                        "recent_10_pf": pf10, "recent_20_pf": pf20,
                        "mdd": mdd, "win_rate": win_rate,
                        "trade_count": n, "expectancy": expectancy,
                    },
                    action_params={"target_mode": "SHADOW"},
                )

        return None

    def _maybe_focus_recommendation(
        self,
        current_regime: str,
        strategies: List[dict],
        already_pending: set,
    ) -> Optional[StrategyRecommendation]:
        """현재 레짐에서 가장 유리한 전략을 강조하는 FOCUS 추천."""
        best_categories = REGIME_BEST_FIT.get(current_regime, [])
        if not best_categories:
            return None

        focus_candidates = []
        for info in strategies:
            if info.get("category") in best_categories and info.get("mode") != "PAUSED":
                stats = self._get_stats(info["name"])
                pf = stats.get("recent_10_pf") or 0.0
                focus_candidates.append((info["name"], info.get("category"), pf))

        if not focus_candidates:
            return None

        # 최고 PF 전략 선택
        focus_candidates.sort(key=lambda x: x[2], reverse=True)
        best_name, best_cat, best_pf = focus_candidates[0]

        if best_name in already_pending:
            return None

        return StrategyRecommendation(
            rec_type="FOCUS",
            strategy=best_name,
            title=f"현재 레짐 최적 전략: {best_name}",
            reasoning=(
                f"레짐 '{current_regime}'에서는 {best_cat} 카테고리가 유리합니다. "
                f"{best_name}은 이 레짐에 가장 잘 맞는 전략입니다 (PF10: {best_pf:.2f}). "
                f"해당 전략의 신호를 우선 모니터링할 것을 권장합니다."
            ),
            supporting_data={
                "current_regime": current_regime,
                "best_fit_categories": best_categories,
                "recent_10_pf": best_pf,
            },
        )

    def _apply_action(self, rec: StrategyRecommendation) -> str:
        """승인된 추천의 실제 액션을 적용한다."""
        target_mode = rec.action_params.get("target_mode")
        if target_mode and rec.strategy:
            try:
                self._manager.set_strategy_mode(rec.strategy, target_mode)
                return f"모드 변경 완료: {rec.strategy} → {target_mode}"
            except Exception as exc:
                logger.error(
                    "[StrategyRecommender] apply_action error: %s", exc
                )
                return f"모드 변경 실패: {exc}"
        return "no_action"

    def _get_stats(self, strategy_name: str) -> dict:
        """전략 통계 조회 (strategy_state DB + paper_positions 분석)."""
        state = self._store.get_strategy_state(strategy_name) or {}
        base = {
            "trade_count":      0,
            "recent_10_pf":     state.get("recent_10_pf"),
            "recent_20_pf":     state.get("recent_20_pf"),
            "recent_mdd":       state.get("recent_mdd"),
            "recent_expectancy": state.get("recent_expectancy"),
            "win_rate":         0.0,
            "health_status":    state.get("health_status", "UNKNOWN"),
        }
        try:
            rows = self._store._conn.execute(
                """
                SELECT pnl_pct FROM paper_positions
                WHERE strategy = ? AND status = 'CLOSED' AND pnl_pct IS NOT NULL
                ORDER BY closed_at DESC LIMIT 30
                """,
                (strategy_name,),
            ).fetchall()
            pnl_list = [float(r[0]) for r in rows]
            base["trade_count"] = len(pnl_list)
            if pnl_list:
                wins = sum(1 for p in pnl_list if p > 0)
                base["win_rate"] = wins / len(pnl_list)
        except Exception:
            pass
        return base
