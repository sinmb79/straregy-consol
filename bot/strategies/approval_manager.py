"""
ApprovalManager — v1.3 PART 15

Telegram/Dashboard 승인 체계.

승인 대상:
  - PAPER → LIVE 전환
  - LIVE → PAUSED
  - 주간 추천 승인/거부
  - 실행 대기 기회 수동 승인

승인 레벨:
  Level 1: 일반 운영 (pause, resume)   — 즉시 실행
  Level 2: 리스크 액션 (mode change, close) — 1단계 확인
  Level 3: 치명적 액션 (hard kill, paper→live) — 2단계 확인 (confirm 코드)

2단계 확인:
  1. 버튼 클릭 → pending_confirm 상태 저장 (60초 유효)
  2. "확인" 버튼 클릭 → 실제 실행
  시간 초과 시 자동 취소

DB: recommendations 테이블 활용
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
    from bot.strategies.manager import StrategyManager

logger = logging.getLogger(__name__)

# 승인 레벨
LEVEL_1 = 1   # 일반 운영
LEVEL_2 = 2   # 리스크 액션
LEVEL_3 = 3   # 치명적 액션

# 2단계 확인 유효 시간 (초)
CONFIRM_TIMEOUT_SEC = 60

# 추천 유효 기간 (일)
RECOMMENDATION_VALIDITY_DAYS = 7

CHECKLIST_SECTIONS = (
    "market_context",
    "strategy_fit",
    "risk_guards",
    "execution_readiness",
)


def build_research_risk_checklist(
    strategy: str,
    current_mode: str = "",
    proposed_mode: str = "",
    supporting_data: Optional[dict] = None,
    expected_risk: Optional[dict] = None,
    strategy_labels: Optional[dict] = None,
) -> dict:
    supporting_data = supporting_data or {}
    expected_risk = expected_risk or {}
    strategy_labels = strategy_labels or {}
    fast = supporting_data.get("fast_layer", {}) or {}
    regime = supporting_data.get("regime") or supporting_data.get("bot_regime") or "UNKNOWN"
    blockers: List[str] = []
    warnings: List[str] = []

    if regime in ("UNKNOWN", "EVENT_RISK"):
        blockers.append(f"regime={regime}")
    if fast.get("alert_level") == "CAUTION":
        blockers.append("fast_layer=CAUTION")
    elif fast.get("alert_level") == "WARN":
        warnings.append("fast_layer=WARN")

    expected_drawdown = expected_risk.get("expected_drawdown_pct")
    if isinstance(expected_drawdown, (int, float)) and expected_drawdown >= 3.0:
        warnings.append(f"expected_drawdown_pct={expected_drawdown}")

    sections = {
        "market_context": {
            "status": "PASS" if regime not in ("UNKNOWN", "EVENT_RISK") else "BLOCK",
            "items": [f"regime={regime}", f"fast_alert={fast.get('alert_level', 'NONE')}"] + [f"warning:{s}" for s in fast.get("signals", [])[:4]],
        },
        "strategy_fit": {
            "status": "PASS" if strategy else "WARN",
            "items": [
                f"strategy={strategy or 'n/a'}",
                f"mode={current_mode or '?'}->{proposed_mode or '?'}",
                f"category_label={strategy_labels.get('category_label', 'n/a')}",
                f"strategy_family={strategy_labels.get('strategy_family', 'n/a')}",
            ],
        },
        "risk_guards": {
            "status": "BLOCK" if blockers else ("WARN" if warnings else "PASS"),
            "items": blockers + warnings + [f"rollback={supporting_data.get('rollback_condition', 'manual_review')}"],
        },
        "execution_readiness": {
            "status": "PASS" if proposed_mode != "LIVE" or not blockers else "BLOCK",
            "items": ["two_step_confirm_for_level3" if proposed_mode == "LIVE" else "paper_or_shadow_path"],
        },
    }
    approval_ready = not blockers
    return {
        "approval_ready": approval_ready,
        "blocking_issues": blockers,
        "warning_issues": warnings,
        "sections": sections,
        "strategy_labels": strategy_labels,
        "rapid_risk_warnings": list(dict.fromkeys(list(fast.get("warning_tags", [])) + list(fast.get("signals", [])))),
        "summary": "PASS" if approval_ready and not warnings else ("WARN" if approval_ready else "BLOCK"),
    }


# 액션별 레벨 매핑
ACTION_LEVELS: Dict[str, int] = {
    "pause_strategy":    LEVEL_1,
    "resume_strategy":   LEVEL_1,
    "watch_strategy":    LEVEL_1,
    "mode_change":       LEVEL_2,
    "close_position":    LEVEL_2,
    "promote_to_live":   LEVEL_3,
    "demote_from_live":  LEVEL_2,
    "hard_kill":         LEVEL_3,
    "force_close_all":   LEVEL_3,
}


@dataclass
class PendingConfirm:
    """Level 3 액션의 1단계 확인 상태."""
    action_type:  str
    target_id:    str
    operator:     str
    created_at:   float = field(default_factory=time.time)
    payload:      dict  = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > CONFIRM_TIMEOUT_SEC


class ApprovalManager:
    """
    운영 액션의 승인 흐름 관리.

    Usage
    -----
    mgr = ApprovalManager(store, strategy_manager)

    # 추천 생성 (예: strategy_health 자동 생성 또는 수동)
    rec_id = mgr.create_recommendation(type_="PROMOTE", strategy="overreaction_reversal", ...)

    # Telegram 승인 처리
    result = mgr.approve_recommendation(rec_id, decided_by="telegram:12345")

    # Level 3 2단계 확인
    mgr.request_confirm(action_type="promote_to_live", target_id="strategy_name", operator="telegram:123", payload={...})
    mgr.execute_confirmed(operator="telegram:123")  # 두 번째 확인 시
    """

    def __init__(self, store: "DataStore", manager: "StrategyManager") -> None:
        self._store = store
        self._manager = manager
        # Level 3 2단계 확인 대기 상태 (operator → PendingConfirm)
        self._pending_confirms: Dict[str, PendingConfirm] = {}

    # ---------------------------------------------------------------------- #
    # Recommendation 생성
    # ---------------------------------------------------------------------- #

    def create_recommendation(
        self,
        type_: str,
        strategy: str,
        current_mode: str = "",
        proposed_mode: str = "",
        supporting_data: dict = None,
        counter_arguments: list = None,
        expected_risk: dict = None,
        rollback_condition: str = "",
        validity_days: int = RECOMMENDATION_VALIDITY_DAYS,
    ) -> str:
        """새 추천을 DB에 저장하고 ID를 반환."""
        rec_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        strategy_profile = self._manager.get_strategy_profile(strategy)
        supporting_data = dict(supporting_data or {})
        current_regime = self._store.get_regime() or {}
        supporting_data.setdefault("regime", current_regime.get("regime"))
        supporting_data.setdefault("bot_regime", current_regime.get("bot_regime"))
        supporting_data.setdefault("fast_layer", current_regime.get("fast_layer", {}))
        checklist = build_research_risk_checklist(
            strategy=strategy,
            current_mode=current_mode,
            proposed_mode=proposed_mode,
            supporting_data=supporting_data,
            expected_risk=expected_risk,
            strategy_labels=strategy_profile,
        )
        supporting_data.setdefault("review_checklist", checklist)
        supporting_data.setdefault("strategy_profile", strategy_profile)
        supporting_data.setdefault("rapid_risk_warnings", checklist.get("rapid_risk_warnings", []))
        supporting_data.setdefault("rollback_condition", rollback_condition or "manual_review")

        record = {
            "id":                 rec_id,
            "ts":                 now_ms,
            "type":               type_,
            "strategy":           strategy,
            "current_mode":       current_mode,
            "proposed_mode":      proposed_mode,
            "supporting_data":    json.dumps(supporting_data or {}),
            "counter_arguments":  json.dumps(counter_arguments or []),
            "expected_risk":      json.dumps(expected_risk or {}),
            "validity_days":      validity_days,
            "rollback_condition": rollback_condition,
            "status":             "PENDING",
            "created_at":         now_ms,
        }
        self._store.save_recommendation(record)
        logger.info(
            "[ApprovalManager] Created recommendation %s: %s for '%s'",
            type_, proposed_mode, strategy,
        )
        return rec_id

    # ---------------------------------------------------------------------- #
    # Recommendation 승인 / 거부
    # ---------------------------------------------------------------------- #

    def approve_recommendation(self, rec_id: str, decided_by: str, reason: str = "") -> dict:
        """
        추천을 승인하고 해당 액션을 실행.
        Returns: {"ok": bool, "msg": str}
        """
        rec = self._store.get_recommendation(rec_id)
        if rec is None:
            return {"ok": False, "msg": "추천을 찾을 수 없습니다."}
        if rec.get("status") != "PENDING":
            return {"ok": False, "msg": f"이미 처리된 추천입니다: {rec.get('status')}"}

        # 유효기간 체크
        created_at = rec.get("created_at", 0)
        validity_ms = rec.get("validity_days", 7) * 86400 * 1000
        if int(time.time() * 1000) - created_at > validity_ms:
            self._store.update_recommendation(rec_id, {
                "status": "DEFERRED",
                "decided_at": int(time.time() * 1000),
                "decided_by": decided_by,
                "decision_reason": "Expired",
            })
            return {"ok": False, "msg": "유효기간이 만료된 추천입니다."}

        # Level 3 액션은 2단계 확인 필요
        action_type = rec.get("type", "").lower()
        level = ACTION_LEVELS.get(action_type, LEVEL_1)
        if level == LEVEL_3 and not self._has_pending_confirm(decided_by, action_type):
            # 1단계: 확인 요청 저장
            self._pending_confirms[decided_by] = PendingConfirm(
                action_type=action_type,
                target_id=rec.get("strategy", ""),
                operator=decided_by,
                payload={"rec_id": rec_id, "reason": reason},
            )
            return {
                "ok":      False,
                "pending": True,
                "msg":     f"⚠️ 이 액션은 Level 3 확인이 필요합니다.\n60초 내에 다시 '확인' 버튼을 눌러주세요.",
            }

        # 실행
        result = self._execute_recommendation(rec, decided_by)
        self._pending_confirms.pop(decided_by, None)

        self._store.update_recommendation(rec_id, {
            "status":          "APPROVED",
            "decided_at":      int(time.time() * 1000),
            "decided_by":      decided_by,
            "decision_reason": reason or "Approved via Telegram",
        })
        return result

    def reject_recommendation(self, rec_id: str, decided_by: str, reason: str = "") -> dict:
        """추천을 거부."""
        rec = self._store.get_recommendation(rec_id)
        if rec is None:
            return {"ok": False, "msg": "추천을 찾을 수 없습니다."}
        self._store.update_recommendation(rec_id, {
            "status":          "REJECTED",
            "decided_at":      int(time.time() * 1000),
            "decided_by":      decided_by,
            "decision_reason": reason or "Rejected via Telegram",
        })
        logger.info("[ApprovalManager] Rejected recommendation %s by %s", rec_id, decided_by)
        return {"ok": True, "msg": "추천이 거부되었습니다."}

    # ---------------------------------------------------------------------- #
    # Level 3 2단계 확인
    # ---------------------------------------------------------------------- #

    def execute_confirmed(self, operator: str) -> dict:
        """Level 3 2단계 확인 실행. pending_confirm이 없거나 만료되면 실패."""
        confirm = self._pending_confirms.get(operator)
        if confirm is None:
            return {"ok": False, "msg": "확인 대기 중인 액션이 없습니다."}
        if confirm.is_expired:
            self._pending_confirms.pop(operator, None)
            return {"ok": False, "msg": "확인 시간이 초과됐습니다 (60초). 다시 시도해 주세요."}

        # 실제 실행
        payload = confirm.payload
        rec_id = payload.get("rec_id")
        if rec_id:
            rec = self._store.get_recommendation(rec_id)
            if rec:
                result = self._execute_recommendation(rec, operator)
                self._store.update_recommendation(rec_id, {
                    "status":          "APPROVED",
                    "decided_at":      int(time.time() * 1000),
                    "decided_by":      operator,
                    "decision_reason": "Level 3 2-step confirmed",
                })
                self._pending_confirms.pop(operator, None)
                return result

        self._pending_confirms.pop(operator, None)
        return {"ok": False, "msg": "실행할 추천을 찾을 수 없습니다."}

    def cancel_confirm(self, operator: str) -> None:
        """Level 3 확인 취소."""
        self._pending_confirms.pop(operator, None)

    def get_pending_confirm(self, operator: str) -> Optional[PendingConfirm]:
        c = self._pending_confirms.get(operator)
        if c and c.is_expired:
            self._pending_confirms.pop(operator, None)
            return None
        return c

    # ---------------------------------------------------------------------- #
    # 추천 조회
    # ---------------------------------------------------------------------- #

    def get_pending_recommendations(self, limit: int = 10) -> List[dict]:
        """현재 PENDING 상태 추천 목록."""
        return self._store.get_recommendations(status="PENDING", limit=limit)

    def build_pending_card(self) -> str:
        """Telegram용 승인 대기 추천 카드."""
        recs = self.get_pending_recommendations(limit=5)
        if not recs:
            return "✅ 승인 대기 항목이 없습니다."

        lines = ["*📋 승인 대기 항목*\n"]
        for r in recs:
            validity_left = max(0, r.get("validity_days", 7) - (
                (int(time.time() * 1000) - r.get("created_at", 0)) // 86400000
            ))
            checklist = (r.get("supporting_data") or {}).get("review_checklist", {})
            ck = checklist.get("summary", "N/A")
            blockers = checklist.get("blocking_issues", [])
            lines.append(
                f"• [{r.get('type')}] `{r.get('strategy')}` "
                f"`{r.get('current_mode','?')}` → `{r.get('proposed_mode','?')}`\n"
                f"  ID: `{r.get('id','')[:8]}...` | 유효: {validity_left}일 남음 | checklist={ck}"
                + (f"\n  blockers: {', '.join(blockers[:2])}" if blockers else "")
            )
        lines.append("\n`/approve <id>` 또는 `/reject <id>` 로 처리")
        return "\n".join(lines)

    # ---------------------------------------------------------------------- #
    # Internal execution
    # ---------------------------------------------------------------------- #

    def _execute_recommendation(self, rec: dict, decided_by: str) -> dict:
        """추천 유형에 따라 실제 액션 실행."""
        type_    = rec.get("type", "")
        strategy = rec.get("strategy", "")
        proposed = rec.get("proposed_mode", "")

        if type_ == "PROMOTE" and proposed:
            ok = self._manager.set_strategy_mode(strategy, proposed)
            msg = f"✅ `{strategy}` → {proposed}" if ok else f"❌ 모드 전환 실패: {strategy}"
            logger.info("[ApprovalManager] PROMOTE %s → %s: %s", strategy, proposed, "OK" if ok else "FAIL")
            return {"ok": ok, "msg": msg}

        if type_ == "DEMOTE" and proposed:
            ok = self._manager.set_strategy_mode(strategy, proposed)
            msg = f"✅ `{strategy}` → {proposed}" if ok else f"❌ 모드 전환 실패"
            return {"ok": ok, "msg": msg}

        if type_ == "RETIRE":
            ok = self._manager.set_strategy_mode(strategy, "PAUSED")
            msg = f"✅ `{strategy}` 은퇴(PAUSED)" if ok else "❌ 은퇴 처리 실패"
            return {"ok": ok, "msg": msg}

        if type_ == "MODIFY":
            # MODIFY는 내용을 supporting_data로 전달 — 현재는 로그만
            logger.info("[ApprovalManager] MODIFY %s — supporting_data=%s", strategy, rec.get("supporting_data"))
            return {"ok": True, "msg": f"ℹ️ `{strategy}` 수정 추천 기록됨 (수동 반영 필요)"}

        return {"ok": False, "msg": f"알 수 없는 추천 유형: {type_}"}

    def _has_pending_confirm(self, operator: str, action_type: str) -> bool:
        """operator가 이미 1단계를 클릭한 상태인지 확인."""
        c = self._pending_confirms.get(operator)
        if c is None or c.is_expired:
            return False
        return c.action_type == action_type
