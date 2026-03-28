"""
ValidationTracker — 전략 검증 데이터 축적기 (v1.5)

실전 매매 전 전략 성과를 주기적으로 스냅샷하고,
누적된 검증 데이터를 기반으로 준비도 점수(0-100)를 계산한다.

Validation Score 산정 기준 (합계 100점):
  trade_count ≥50   → +25점  (충분한 샘플)
  trade_count ≥30   → +15점  (부분 인정)
  trade_count ≥10   → +5점   (기본 샘플)
  PF10      ≥1.5    → +20점
  PF10      ≥1.2    → +12점
  PF10      ≥1.0    → +5점
  win_rate  ≥45%    → +15점
  win_rate  ≥40%    → +10점
  win_rate  ≥35%    → +5점
  MDD       ≥-8%    → +15점
  MDD       ≥-10%   → +10점
  MDD       ≥-15%   → +5점
  expectancy > 0    → +10점
  health_status=OK  → +15점; WARN → +5점

SHADOW 기준 : score ≥ 50
LIVE   기준 : score ≥ 75
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from bot.data.store import DataStore

logger = logging.getLogger(__name__)

SHADOW_SCORE_THRESHOLD = 50
LIVE_SCORE_THRESHOLD   = 75

# 동일 전략에 대한 스냅샷 최소 간격 (ms) — trade_count 변화 없어도 최대 1시간마다 1회
MIN_SNAPSHOT_INTERVAL_MS = 60 * 60 * 1000


class ValidationTracker:
    """
    StrategyHealthEngine 에서 주기적으로 호출받아
    전략별 검증 스냅샷을 strategy_validation_log 테이블에 저장한다.

    Usage
    -----
    tracker = ValidationTracker(store)
    tracker.take_snapshot(strategy_name, health_data, mode)
    report  = tracker.build_validation_report()
    """

    def __init__(self, store: "DataStore") -> None:
        self._store = store
        # strategy → (last_trade_count, last_snapshot_ts_ms)
        self._last_snapshot: Dict[str, tuple] = {}

    # ---------------------------------------------------------------------- #
    # Snapshot
    # ---------------------------------------------------------------------- #

    def take_snapshot(
        self,
        strategy: str,
        health_data: dict,
        mode: str = "PAPER",
    ) -> bool:
        """
        전략의 현재 성과를 스냅샷으로 저장.

        Parameters
        ----------
        strategy    : 전략 이름
        health_data : StrategyHealthEngine._evaluate_strategy() 반환값
        mode        : 전략 현재 모드 (PAPER / SHADOW / LIVE)

        Returns True if snapshot was saved, False if skipped.
        """
        trade_count = health_data.get("trade_count", 0) or 0
        now_ms = int(time.time() * 1000)

        last_count, last_ts = self._last_snapshot.get(strategy, (None, 0))

        # Skip if trade_count unchanged AND within minimum interval
        if (
            last_count is not None
            and last_count == trade_count
            and (now_ms - last_ts) < MIN_SNAPSHOT_INTERVAL_MS
        ):
            return False

        score = self.compute_validation_score(health_data)
        meets_shadow = 1 if score >= SHADOW_SCORE_THRESHOLD else 0
        meets_live   = 1 if score >= LIVE_SCORE_THRESHOLD   else 0

        record = {
            "ts":                    now_ms,
            "strategy":              strategy,
            "mode":                  mode,
            "trade_count":           trade_count,
            "win_rate":              health_data.get("win_rate_10"),
            "profit_factor":         health_data.get("recent_10_pf"),
            "recent_10_pf":          health_data.get("recent_10_pf"),
            "recent_20_pf":          health_data.get("recent_20_pf"),
            "expectancy":            health_data.get("recent_expectancy"),
            "max_drawdown":          health_data.get("recent_mdd"),
            "validation_score":      score,
            "meets_shadow_criteria": meets_shadow,
            "meets_live_criteria":   meets_live,
            "regime_breakdown":      health_data.get("regime_breakdown_json", "{}"),
            "health_status":         health_data.get("health_status", "UNKNOWN"),
        }

        self._store.save_validation_snapshot(record)
        self._last_snapshot[strategy] = (trade_count, now_ms)

        logger.info(
            "[ValidationTracker] Snapshot saved: '%s' mode=%s score=%d/%d "
            "trades=%d pf10=%s mdd=%s meets_shadow=%d meets_live=%d",
            strategy, mode, score, 100,
            trade_count,
            health_data.get("recent_10_pf"),
            health_data.get("recent_mdd"),
            meets_shadow, meets_live,
        )
        return True

    # ---------------------------------------------------------------------- #
    # Score
    # ---------------------------------------------------------------------- #

    @staticmethod
    def compute_validation_score(health_data: dict) -> int:
        """
        전략 준비도 점수 계산 (0-100).
        health_data는 StrategyHealthEngine._evaluate_strategy() 반환값.
        """
        score = 0
        trade_count = health_data.get("trade_count", 0) or 0
        pf10        = health_data.get("recent_10_pf") or 0.0
        win_rate    = health_data.get("win_rate_10") or 0.0
        mdd         = health_data.get("recent_mdd") or 0.0
        expectancy  = health_data.get("recent_expectancy") or 0.0
        health_st   = health_data.get("health_status", "UNKNOWN")

        # ── trade_count (max 25) ─────────────────────────────────────────── #
        if trade_count >= 50:
            score += 25
        elif trade_count >= 30:
            score += 15
        elif trade_count >= 10:
            score += 5

        # ── Profit Factor (max 20) ───────────────────────────────────────── #
        if pf10 >= 1.5:
            score += 20
        elif pf10 >= 1.2:
            score += 12
        elif pf10 >= 1.0:
            score += 5

        # ── Win Rate (max 15) ────────────────────────────────────────────── #
        if win_rate >= 0.45:
            score += 15
        elif win_rate >= 0.40:
            score += 10
        elif win_rate >= 0.35:
            score += 5

        # ── MDD (max 15) ─────────────────────────────────────────────────── #
        if mdd >= -8.0:
            score += 15
        elif mdd >= -10.0:
            score += 10
        elif mdd >= -15.0:
            score += 5

        # ── Expectancy (max 10) ──────────────────────────────────────────── #
        if expectancy > 0:
            score += 10

        # ── Health status (max 15) ───────────────────────────────────────── #
        if health_st == "OK":
            score += 15
        elif health_st == "WARN":
            score += 5

        return min(score, 100)

    # ---------------------------------------------------------------------- #
    # Report
    # ---------------------------------------------------------------------- #

    def build_validation_report(self, strategies: Optional[List[str]] = None) -> str:
        """
        Telegram용 전략 검증 리포트 생성.
        strategies=None 이면 로그가 있는 모든 전략 표시.
        """
        snapshots_by_strategy = self._store.get_latest_validation_snapshots()

        if not snapshots_by_strategy:
            return "📋 축적된 검증 데이터가 없습니다.\nPAPER 거래가 누적되면 자동으로 기록됩니다."

        lines = ["*📊 전략 검증 현황 (실전 진입 전)*\n"]

        status_icon = {
            "OK": "✅", "WARN": "⚠️", "PAUSE": "🔴", "UNKNOWN": "❓"
        }

        for name, snap in sorted(snapshots_by_strategy.items()):
            score     = snap.get("validation_score", 0)
            mode      = snap.get("mode", "PAPER")
            health    = snap.get("health_status", "UNKNOWN")
            trades    = snap.get("trade_count", 0)
            pf10      = snap.get("recent_10_pf")
            mdd       = snap.get("max_drawdown")
            win_rate  = snap.get("win_rate")
            exp       = snap.get("expectancy")
            ts_ms     = snap.get("ts", 0)
            m_shadow  = snap.get("meets_shadow_criteria", 0)
            m_live    = snap.get("meets_live_criteria", 0)

            icon = status_icon.get(health, "❓")

            # Score bar
            filled = score // 10
            bar = "█" * filled + "░" * (10 - filled)
            gate = ""
            if m_live:
                gate = "  🟢 LIVE 준비됨"
            elif m_shadow:
                gate = "  🟡 SHADOW 준비됨"
            else:
                needed_shadow = max(0, SHADOW_SCORE_THRESHOLD - score)
                gate = f"  ⏳ SHADOW까지 +{needed_shadow}점 필요"

            ts_str = ""
            if ts_ms:
                dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                ts_str = dt.strftime("%m/%d %H:%M")

            pf10_str = f"{pf10:.2f}" if pf10 is not None else "N/A"
            mdd_str  = f"{mdd:.1f}%" if mdd  is not None else "N/A"
            wr_str   = f"{win_rate*100:.0f}%" if win_rate is not None else "N/A"
            exp_str  = f"{exp:.4f}" if exp is not None else "N/A"

            lines.append(
                f"{icon} *{name}* [{mode}]{gate}\n"
                f"  점수: {score}/100  [{bar}]\n"
                f"  거래:{trades}회  PF10:{pf10_str}  MDD:{mdd_str}  WR:{wr_str}  Exp:{exp_str}\n"
                + (f"  마지막 갱신: {ts_str}" if ts_str else "")
            )
            lines.append("")

        lines.append(
            f"_SHADOW 기준: {SHADOW_SCORE_THRESHOLD}점 이상_\n"
            f"_LIVE 기준: {LIVE_SCORE_THRESHOLD}점 이상_"
        )
        return "\n".join(lines)

    def get_snapshot_history(self, strategy: str, limit: int = 20) -> List[dict]:
        """전략별 스냅샷 이력 조회."""
        return self._store.get_validation_snapshots(strategy, limit=limit)
