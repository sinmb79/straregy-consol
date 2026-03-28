"""
Strategy Health Engine — v1.3 Phase E (PART 11)

전략별 최근 성과를 분석하여 health_status를 계산하고,
성과 불량 전략을 자동 PAUSE 후보로 지정한다.

Health Status:
  OK      — 정상 운영
  WARN    — 성과 저하 경고 (recent_10_pf 1.0~1.2 범위)
  PAUSE   — 자동 PAUSE 후보 (recent_10_pf < 1.0)
  RECOVER — PAUSE 후 재검증 중
  UNKNOWN — 거래 데이터 부족 (< MIN_TRADES)

업데이트 주기: 매 engine cycle (run_health_check 호출 시)
자동 PAUSE: StrategyManager.set_strategy_mode() 호출
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from bot.data.store import DataStore
    from bot.strategies.manager import StrategyManager

logger = logging.getLogger(__name__)

# Thresholds — 판단 기준
MIN_TRADES = 10          # 판단을 위한 최소 거래 수
WARN_PF_THRESHOLD = 1.2  # recent_10_pf < 1.2 → WARN
PAUSE_PF_THRESHOLD = 1.0  # recent_10_pf < 1.0 → PAUSE 후보
PAUSE_MDD_THRESHOLD = -15.0  # recent_mdd < -15% → 추가 경고
WARN_WIN_RATE = 0.35     # win_rate < 35% → 경고

# ── LIVE 승격 기준 (Master Plan PART 11) ────────────────────────────────────
# PAPER → SHADOW
PROMOTE_PAPER_MIN_SIGNALS   = 30    # 최소 신호(거래) 수
PROMOTE_PAPER_MIN_PF        = 1.2   # Profit Factor 최소값
PROMOTE_PAPER_MIN_EXPECTANCY = 0.0  # 기댓값 > 0 (양수 기대수익)
PROMOTE_PAPER_MAX_MDD       = -10.0 # MDD -10% 이내

# SHADOW → LIVE
PROMOTE_SHADOW_MIN_SIGNALS   = 50
PROMOTE_SHADOW_MIN_PF        = 1.5
PROMOTE_SHADOW_MIN_EXPECTANCY = 0.0
PROMOTE_SHADOW_MAX_MDD       = -8.0
PROMOTE_SHADOW_MIN_WIN_RATE  = 0.40  # 승률 40% 이상


class StrategyHealthEngine:
    """
    전략 건강도를 주기적으로 계산하고 strategy_state를 업데이트.

    Usage
    -----
    health = StrategyHealthEngine(store, manager)
    health.run_health_check()          # engine loop에서 주기적으로 호출
    card = health.build_health_card()  # Telegram 리포트용
    """

    def __init__(self, store: "DataStore", manager: "StrategyManager") -> None:
        self._store = store
        self._manager = manager
        self._last_check_ts: int = 0
        self._CHECK_INTERVAL_MS = 15 * 60 * 1000  # 15분마다 실행
        # ApprovalManager는 순환 의존 방지를 위해 외부 주입
        self._approval_manager = None
        # ValidationTracker는 외부 주입 (circular dependency 방지)
        self._validation_tracker = None

    def set_approval_manager(self, approval_manager) -> None:
        """ApprovalManager 주입 (circular dependency 방지)."""
        self._approval_manager = approval_manager

    def set_validation_tracker(self, validation_tracker) -> None:
        """ValidationTracker 주입."""
        self._validation_tracker = validation_tracker

    # ---------------------------------------------------------------------- #
    # Main cycle
    # ---------------------------------------------------------------------- #

    def run_health_check(self) -> None:
        """15분 주기로 전략 건강도 계산 및 strategy_state 업데이트."""
        now_ms = int(time.time() * 1000)
        if now_ms - self._last_check_ts < self._CHECK_INTERVAL_MS:
            return
        self._last_check_ts = now_ms
        self._evaluate_all()

    def force_check(self) -> Dict[str, dict]:
        """즉시 건강도 계산 실행 (리포트/명령어 트리거용)."""
        self._evaluate_all()
        return self._build_health_summary()

    # ---------------------------------------------------------------------- #
    # Evaluation
    # ---------------------------------------------------------------------- #

    def _evaluate_all(self) -> None:
        """모든 전략 건강도 계산 후 DB 업데이트."""
        strategy_list = self._manager.get_strategy_list()
        for info in strategy_list:
            name = info["name"]
            try:
                health = self._evaluate_strategy(name)
                self._apply_health(name, health, info.get("mode", "PAPER"))
            except Exception as exc:
                logger.error("[HealthEngine] Error evaluating '%s': %s", name, exc)

    def _evaluate_strategy(self, strategy_name: str) -> dict:
        """
        단일 전략의 건강도 지표를 계산.

        Returns dict with:
          recent_10_pf, recent_20_pf, recent_expectancy, recent_mdd,
          win_rate_10, trade_count, health_status, regime_breakdown_json
        """
        rows = self._get_recent_trades(strategy_name, limit=30)
        n = len(rows)

        if n < MIN_TRADES:
            return {
                "trade_count": n,
                "health_status": "UNKNOWN",
                "recent_10_pf": None,
                "recent_20_pf": None,
                "recent_expectancy": None,
                "recent_mdd": None,
                "regime_breakdown_json": "{}",
            }

        pnl_list = [float(r["pnl_pct"]) for r in rows if r.get("pnl_pct") is not None]
        recent_10 = pnl_list[:10]
        recent_20 = pnl_list[:20]

        pf_10 = self._profit_factor(recent_10)
        pf_20 = self._profit_factor(recent_20)
        mdd = self._max_drawdown(pnl_list)
        expectancy = self._expectancy(pnl_list)
        win_rate_10 = len([p for p in recent_10 if p > 0]) / len(recent_10) if recent_10 else 0.0

        # Regime breakdown
        regime_map: dict = defaultdict(lambda: {"wins": 0, "losses": 0, "pf": 0.0})
        for r in rows:
            reg = r.get("regime") or "UNKNOWN"
            if float(r.get("pnl_pct") or 0) > 0:
                regime_map[reg]["wins"] += 1
            else:
                regime_map[reg]["losses"] += 1
        for reg, data in regime_map.items():
            all_pnl = [float(r["pnl_pct"]) for r in rows
                       if (r.get("regime") or "UNKNOWN") == reg and r.get("pnl_pct")]
            regime_map[reg]["pf"] = round(self._profit_factor(all_pnl), 3)
        regime_breakdown = {k: dict(v) for k, v in regime_map.items()}

        # Health status decision
        health_status = self._compute_health_status(pf_10, win_rate_10, mdd)

        return {
            "trade_count": n,
            "health_status": health_status,
            "recent_10_pf": round(pf_10, 3) if pf_10 is not None else None,
            "recent_20_pf": round(pf_20, 3) if pf_20 is not None else None,
            "recent_expectancy": round(expectancy, 4),
            "recent_mdd": round(mdd, 3),
            "win_rate_10": round(win_rate_10, 3),
            "regime_breakdown_json": json.dumps(regime_breakdown),
        }

    def _compute_health_status(
        self,
        pf_10: Optional[float],
        win_rate_10: float,
        mdd: float,
    ) -> str:
        if pf_10 is None:
            return "UNKNOWN"
        if pf_10 < PAUSE_PF_THRESHOLD or (win_rate_10 < WARN_WIN_RATE and mdd < PAUSE_MDD_THRESHOLD):
            return "PAUSE"
        if pf_10 < WARN_PF_THRESHOLD or win_rate_10 < WARN_WIN_RATE:
            return "WARN"
        return "OK"

    def _apply_health(self, name: str, health: dict, current_mode: str) -> None:
        """건강도 결과를 strategy_state에 저장하고, PAUSE 후보면 모드 전환."""
        health_status = health.get("health_status", "UNKNOWN")
        now_ms = int(time.time() * 1000)

        update = {
            "name": name,
            "health_status": health_status,
            "last_health_update_ts": now_ms,
        }
        if health.get("recent_10_pf") is not None:
            update["recent_10_pf"] = health["recent_10_pf"]
        if health.get("recent_20_pf") is not None:
            update["recent_20_pf"] = health["recent_20_pf"]
        if health.get("recent_expectancy") is not None:
            update["recent_expectancy"] = health["recent_expectancy"]
        if health.get("recent_mdd") is not None:
            update["recent_mdd"] = health["recent_mdd"]

        self._store.upsert_strategy_state(update)

        # Auto-pause: LIVE/SHADOW 전략이 PAUSE 상태면 모드 전환
        if health_status == "PAUSE" and current_mode in ("LIVE", "SHADOW"):
            pause_reason = (
                f"Auto-pause: recent_10_pf={health.get('recent_10_pf')}, "
                f"win_rate={health.get('win_rate_10')}, mdd={health.get('recent_mdd')}"
            )
            self._store.upsert_strategy_state({
                "name": name,
                "last_pause_reason": pause_reason,
            })
            self._manager.set_strategy_mode(name, "PAUSED")
            logger.warning(
                "[HealthEngine] Auto-paused '%s': %s", name, pause_reason
            )
        elif health_status in ("OK", "WARN"):
            # live_eligibility 업데이트
            pf10 = health.get("recent_10_pf") or 0.0
            eligible = 1 if health_status == "OK" and pf10 >= PROMOTE_PAPER_MIN_PF else 0
            self._store.upsert_strategy_state({
                "name": name,
                "live_eligibility": eligible,
            })

            if self._approval_manager is not None:
                pending = self._approval_manager.get_pending_recommendations()

                # ── PAPER → SHADOW 승격 기준 ─────────────────────────────── #
                if current_mode == "PAPER" and self._check_paper_to_shadow(health):
                    already = any(
                        r.get("type") == "PROMOTE"
                        and r.get("strategy") == name
                        and r.get("proposed_mode") == "SHADOW"
                        for r in pending
                    )
                    if not already:
                        self._approval_manager.create_recommendation(
                            type_="PROMOTE",
                            strategy=name,
                            current_mode="PAPER",
                            proposed_mode="SHADOW",
                            supporting_data={
                                "trade_count":   health.get("trade_count"),
                                "recent_10_pf":  health.get("recent_10_pf"),
                                "recent_20_pf":  health.get("recent_20_pf"),
                                "recent_mdd":    health.get("recent_mdd"),
                                "win_rate_10":   health.get("win_rate_10"),
                                "expectancy":    health.get("recent_expectancy"),
                            },
                            expected_risk={"shadow_mode": True, "live_money_risk": False},
                            rollback_condition=(
                                f"recent_10_pf < {PAUSE_PF_THRESHOLD} 이면 즉시 PAUSED"
                            ),
                        )
                        logger.info(
                            "[HealthEngine] PROMOTE 추천 생성: '%s' PAPER → SHADOW "
                            "(trades=%d, pf10=%.2f, mdd=%.1f%%)",
                            name, health.get("trade_count", 0),
                            health.get("recent_10_pf") or 0,
                            health.get("recent_mdd") or 0,
                        )

                # ── SHADOW → LIVE 승격 기준 ──────────────────────────────── #
                elif current_mode == "SHADOW" and self._check_shadow_to_live(health):
                    already = any(
                        r.get("type") == "PROMOTE"
                        and r.get("strategy") == name
                        and r.get("proposed_mode") == "LIVE"
                        for r in pending
                    )
                    if not already:
                        self._approval_manager.create_recommendation(
                            type_="PROMOTE",
                            strategy=name,
                            current_mode="SHADOW",
                            proposed_mode="LIVE",
                            supporting_data={
                                "trade_count":   health.get("trade_count"),
                                "recent_10_pf":  health.get("recent_10_pf"),
                                "recent_20_pf":  health.get("recent_20_pf"),
                                "recent_mdd":    health.get("recent_mdd"),
                                "win_rate_10":   health.get("win_rate_10"),
                                "expectancy":    health.get("recent_expectancy"),
                            },
                            expected_risk={"live_money_risk": True},
                            rollback_condition=(
                                f"recent_10_pf < {PAUSE_PF_THRESHOLD} 이면 즉시 PAUSED"
                            ),
                        )
                        logger.info(
                            "[HealthEngine] PROMOTE 추천 생성: '%s' SHADOW → LIVE "
                            "(trades=%d, pf10=%.2f, win_rate=%.0f%%, mdd=%.1f%%)",
                            name, health.get("trade_count", 0),
                            health.get("recent_10_pf") or 0,
                            (health.get("win_rate_10") or 0) * 100,
                            health.get("recent_mdd") or 0,
                        )

        # ── ValidationTracker 스냅샷 저장 ──────────────────────────────── #
        if self._validation_tracker is not None:
            try:
                self._validation_tracker.take_snapshot(name, health, current_mode)
            except Exception as exc:
                logger.error("[HealthEngine] ValidationTracker snapshot error for '%s': %s", name, exc)

        level = {"OK": "info", "WARN": "warning", "PAUSE": "warning", "UNKNOWN": "debug"}.get(health_status, "info")
        getattr(logger, level)(
            "[HealthEngine] '%s' → %s  pf10=%s  mdd=%s",
            name, health_status,
            health.get("recent_10_pf"), health.get("recent_mdd"),
        )

    # ---------------------------------------------------------------------- #
    # Report building
    # ---------------------------------------------------------------------- #

    def _build_health_summary(self) -> Dict[str, dict]:
        """현재 전략 건강도 요약 (Telegram/Dashboard용)."""
        strategy_list = self._manager.get_strategy_list()
        result = {}
        for info in strategy_list:
            name = info["name"]
            state = self._store.get_strategy_state(name) or {}
            result[name] = {
                "mode":           info.get("mode", "PAPER"),
                "health_status":  state.get("health_status", "UNKNOWN"),
                "recent_10_pf":   state.get("recent_10_pf"),
                "recent_20_pf":   state.get("recent_20_pf"),
                "recent_mdd":     state.get("recent_mdd"),
                "recent_expectancy": state.get("recent_expectancy"),
                "live_eligibility": state.get("live_eligibility", 0),
                "last_pause_reason": state.get("last_pause_reason"),
                "last_health_ts": state.get("last_health_update_ts"),
            }
        return result

    def build_health_card(self, period_label: str = "now") -> str:
        """
        Telegram용 건강도 카드 텍스트 생성.

        period_label: "now" | "daily" | "weekly"
        """
        summary = self._build_health_summary()
        lines = [f"*전략 건강도 리포트* ({period_label})\n"]

        status_icon = {"OK": "✅", "WARN": "⚠️", "PAUSE": "🔴", "RECOVER": "🔄", "UNKNOWN": "❓"}

        for name, data in summary.items():
            icon = status_icon.get(data.get("health_status", "UNKNOWN"), "❓")
            mode = data.get("mode", "PAPER")
            pf10 = data.get("recent_10_pf")
            pf20 = data.get("recent_20_pf")
            mdd  = data.get("recent_mdd")
            exp  = data.get("recent_expectancy")
            eligible = data.get("live_eligibility", 0)

            pf10_str  = f"{pf10:.2f}"  if pf10 is not None else "N/A"
            pf20_str  = f"{pf20:.2f}"  if pf20 is not None else "N/A"
            mdd_str   = f"{mdd:.1f}%"  if mdd  is not None else "N/A"
            exp_str   = f"{exp:.4f}"   if exp  is not None else "N/A"
            elig_str  = "LIVE가능" if eligible else ""

            lines.append(
                f"{icon} *{name}* [{mode}] {elig_str}\n"
                f"  PF10={pf10_str}  PF20={pf20_str}  MDD={mdd_str}  Exp={exp_str}"
            )
            if data.get("last_pause_reason"):
                lines.append(f"  ⚠️ {data['last_pause_reason']}")
            lines.append("")

        return "\n".join(lines)

    # ---------------------------------------------------------------------- #
    # Helpers
    # ---------------------------------------------------------------------- #

    def _check_paper_to_shadow(self, health: dict) -> bool:
        """PAPER → SHADOW 승격 조건을 모두 충족하는지 확인."""
        if (health.get("trade_count") or 0) < PROMOTE_PAPER_MIN_SIGNALS:
            return False
        if (health.get("recent_10_pf") or 0.0) < PROMOTE_PAPER_MIN_PF:
            return False
        if (health.get("recent_expectancy") or 0.0) <= PROMOTE_PAPER_MIN_EXPECTANCY:
            return False
        if (health.get("recent_mdd") or 0.0) < PROMOTE_PAPER_MAX_MDD:
            return False
        return True

    def _check_shadow_to_live(self, health: dict) -> bool:
        """SHADOW → LIVE 승격 조건을 모두 충족하는지 확인."""
        if (health.get("trade_count") or 0) < PROMOTE_SHADOW_MIN_SIGNALS:
            return False
        if (health.get("recent_10_pf") or 0.0) < PROMOTE_SHADOW_MIN_PF:
            return False
        if (health.get("recent_expectancy") or 0.0) <= PROMOTE_SHADOW_MIN_EXPECTANCY:
            return False
        if (health.get("recent_mdd") or 0.0) < PROMOTE_SHADOW_MAX_MDD:
            return False
        if (health.get("win_rate_10") or 0.0) < PROMOTE_SHADOW_MIN_WIN_RATE:
            return False
        return True

    def _get_recent_trades(self, strategy: str, limit: int = 30) -> List[dict]:
        """최근 closed paper positions 조회."""
        try:
            rows = self._store._conn.execute(
                """
                SELECT pnl_pct, regime, closed_at
                FROM paper_positions
                WHERE strategy = ? AND status = 'CLOSED' AND pnl_pct IS NOT NULL
                ORDER BY closed_at DESC
                LIMIT ?
                """,
                (strategy, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.error("[HealthEngine] DB error for '%s': %s", strategy, exc)
            return []

    @staticmethod
    def _profit_factor(pnl_list: List[float]) -> Optional[float]:
        """Profit Factor 계산 (총이익 / |총손실|)."""
        if not pnl_list:
            return None
        gains  = sum(p for p in pnl_list if p > 0)
        losses = abs(sum(p for p in pnl_list if p < 0))
        if losses == 0:
            return float("inf") if gains > 0 else 1.0
        return gains / losses

    @staticmethod
    def _max_drawdown(pnl_list: List[float]) -> float:
        """최대 드로다운 계산 (누적 수익 기준)."""
        if not pnl_list:
            return 0.0
        eq = 0.0
        peak = 0.0
        mdd = 0.0
        for p in pnl_list:
            eq += p
            if eq > peak:
                peak = eq
            dd = eq - peak
            if dd < mdd:
                mdd = dd
        return mdd

    @staticmethod
    def _expectancy(pnl_list: List[float]) -> float:
        """기댓값 = 평균 PnL."""
        if not pnl_list:
            return 0.0
        return sum(pnl_list) / len(pnl_list)
