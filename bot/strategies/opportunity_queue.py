"""
OpportunityQueue — v1.3 Master Plan PART 4/5/7

스코어된 Opportunity를 관리하고 Top-N을 선출한다.

기능:
  - 새 Opportunity 추가 + 포트폴리오 제약 사전 검사
  - 순위 결정 (score_total > liquidity > spread > RR)
  - Top-N 선출 (LIVE는 상위 1~2개만)
  - 만료(EXPIRED) 처리
  - 텔레그램 알림용 요약 제공
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import List, Optional

from bot.strategies.opportunity import Opportunity

logger = logging.getLogger(__name__)

# 기회 유효 시간 (ms) — PENDING이 이 시간 지나면 EXPIRED
OPPORTUNITY_TTL_MS = 60 * 60 * 1000   # 1시간

# 승인 후 미실행 만료 시간 (ms) — APPROVED 상태가 이 시간 지나면 STALE_APPROVAL
APPROVAL_TTL_MS = 15 * 60 * 1000      # 15분

# 큐 최대 크기
MAX_QUEUE_SIZE = 200


class OpportunityQueue:
    """
    스코어된 Opportunity의 순위 관리 및 Top-N 선출.

    Usage
    -----
    queue = OpportunityQueue(top_n_live=2)
    queue.add(opp)
    top = queue.top_n(n=2, min_score=8)
    """

    def __init__(self, top_n_live: int = 2) -> None:
        self._top_n_live = top_n_live
        # 최근 기회 목록 (최신순, 최대 MAX_QUEUE_SIZE)
        self._queue: deque[Opportunity] = deque(maxlen=MAX_QUEUE_SIZE)

    # ---------------------------------------------------------------------- #
    # Public API
    # ---------------------------------------------------------------------- #

    def add(self, opp: Opportunity) -> None:
        """Opportunity를 큐에 추가하고 순위를 재산정한다."""
        self._queue.appendleft(opp)
        self._expire_old()
        self._rank_all()
        logger.debug(
            "[OppQueue] Added %s %s %s  score=%d  status=%s  queue_size=%d",
            opp.symbol, opp.side, opp.category,
            opp.score_total, opp.execution_status, len(self._queue),
        )

    def top_n(self, n: Optional[int] = None, min_score: int = 8) -> List[Opportunity]:
        """
        score >= min_score이고 PENDING 상태인 기회 중 상위 n개를 반환한다.
        n이 None이면 self._top_n_live를 사용.
        """
        n = n if n is not None else self._top_n_live
        candidates = [
            o for o in self._queue
            if o.score_total >= min_score
            and o.execution_status == "PENDING"
        ]
        candidates.sort(key=self._rank_key, reverse=True)
        return candidates[:n]

    def watch_list(self) -> List[Opportunity]:
        """score 6~7인 PENDING 기회 목록."""
        return [
            o for o in self._queue
            if o.is_watch and o.execution_status == "PENDING"
        ]

    def get_recent(self, limit: int = 20) -> List[Opportunity]:
        """최근 기회 limit개 (만료 포함)."""
        return list(self._queue)[:limit]

    def mark_executed(self, opp_id: str) -> None:
        for o in self._queue:
            if o.id == opp_id:
                o.execution_status = "EXECUTED"
                return

    def mark_paper_only(self, opp_id: str) -> None:
        for o in self._queue:
            if o.id == opp_id:
                o.execution_status = "PAPER_ONLY"
                return

    def mark_ignored(self, opp_id: str) -> None:
        for o in self._queue:
            if o.id == opp_id:
                o.execution_status = "IGNORED"
                return

    def approve(self, opp_id: str, approved_by: str) -> Optional[Opportunity]:
        for o in self._queue:
            if o.id == opp_id and o.execution_status == "PENDING":
                o.execution_status = "APPROVED"
                o.approved_by = approved_by
                o.approved_at = int(time.time() * 1000)
                logger.info(
                    "[OppQueue] Approved %s %s by %s",
                    o.symbol, o.side, approved_by,
                )
                return o
        return None

    def find(self, opp_id: str) -> Optional[Opportunity]:
        for o in self._queue:
            if o.id == opp_id:
                return o
        return None

    def pending_count(self) -> int:
        return sum(1 for o in self._queue if o.execution_status == "PENDING")

    # ---------------------------------------------------------------------- #
    # Internal
    # ---------------------------------------------------------------------- #

    def _expire_old(self) -> None:
        """
        두 가지 만료 처리:
          1. PENDING: ts 기준 1시간 초과 → EXPIRED
          2. APPROVED: approved_at 기준 15분 초과 미실행 → STALE_APPROVAL
        """
        now = int(time.time() * 1000)
        for o in self._queue:
            if o.execution_status == "PENDING" and (now - o.ts) > OPPORTUNITY_TTL_MS:
                o.execution_status = "EXPIRED"
                logger.info(
                    "[OppQueue] Expired (TTL): %s %s  score=%d",
                    o.symbol, o.side, o.score_total,
                )
            elif (
                o.execution_status == "APPROVED"
                and o.approved_at is not None
                and (now - o.approved_at) > APPROVAL_TTL_MS
            ):
                o.execution_status = "STALE_APPROVAL"
                logger.warning(
                    "[OppQueue] Stale approval: %s %s  approved_at=%d  age=%.0fmin",
                    o.symbol, o.side, o.approved_at,
                    (now - o.approved_at) / 60000,
                )

    def expire_stale_approvals(self) -> List[Opportunity]:
        """만료된 APPROVED 기회 목록 반환 (외부 알림용)."""
        self._expire_old()
        return [o for o in self._queue if o.execution_status == "STALE_APPROVAL"]

    def _rank_all(self) -> None:
        """전체 PENDING 기회를 순위 재산정."""
        pending = [o for o in self._queue if o.execution_status == "PENDING"]
        pending.sort(key=self._rank_key, reverse=True)

        # 심볼별 순위
        sym_counters: dict = {}
        for i, o in enumerate(pending, start=1):
            o.rank_global = i
            sym_counters[o.symbol] = sym_counters.get(o.symbol, 0) + 1
            o.rank_within_symbol = sym_counters[o.symbol]

    @staticmethod
    def _rank_key(o: Opportunity) -> tuple:
        """
        정렬 키 (내림차순):
        1. score_total
        2. liquidity (OK=2, LOW=1, CRITICAL=0)
        3. spread (GOOD=2, WIDE=1, CRITICAL=0)
        4. signal_strength
        """
        liq_map   = {"OK": 2, "LOW": 1, "CRITICAL": 0}
        spr_map   = {"GOOD": 2, "WIDE": 1, "CRITICAL": 0}
        return (
            o.score_total,
            liq_map.get(o.liquidity_state, 0),
            spr_map.get(o.spread_state, 0),
            o.signal_strength,
        )
