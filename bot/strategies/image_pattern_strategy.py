"""
ImagePatternStrategy — 사용자가 차트 이미지를 업로드하고 AI(OpenClaw)가 분석하여
자동 생성된 매매 조건으로 실행되는 전략.

각 "이미지 패턴"은 image_patterns 테이블에 저장되며,
StrategyBase.compute()에서 활성화된 패턴의 조건을 평가하여 신호를 생성한다.
"""
from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, List

from bot.strategies._base import Signal, StrategyBase
from bot.strategies.condition_evaluator import evaluate_conditions

if TYPE_CHECKING:
    from bot.data.store import DataStore

logger = logging.getLogger(__name__)

# compute() 호출마다 DB를 읽지 않도록 짧은 메모리 캐시 (60초)
_CACHE_TTL_S = 60


class ImagePatternStrategy(StrategyBase):
    """
    사용자 정의 이미지 패턴 전략.

    - image_patterns 테이블에서 enabled=1 인 패턴을 로드
    - 각 패턴의 conditions_json 을 evaluate_conditions()로 평가
    - 조건 충족 시 Signal 발생, 쿨다운 업데이트
    """

    name          = "image_pattern"
    category      = "custom"
    regime_filter = []   # 패턴 단위로 필터 적용 — 클래스 레벨에서는 모두 허용

    def __init__(self) -> None:
        self._cache: List[dict] = []
        self._cache_ts: float   = 0.0

    # ---------------------------------------------------------------------- #
    # StrategyBase interface
    # ---------------------------------------------------------------------- #

    def compute(self, store: "DataStore", regime: dict) -> List[Signal]:
        regime_str = regime.get("regime", "UNKNOWN")
        now_ms     = int(time.time() * 1000)
        signals: List[Signal] = []

        patterns = self._load_patterns(store)
        if not patterns:
            return signals

        try:
            from bot.config import get_config
            all_symbols = get_config().tracked_symbols
        except Exception:
            all_symbols = []

        for pat in patterns:
            # ── 쿨다운 체크 ──────────────────────────────────────────────────
            cooldown_ms = int(float(pat.get("cooldown_hours", 4)) * 3600 * 1000)
            last_ts     = pat.get("last_signal_ts") or 0
            if now_ms - int(last_ts) < cooldown_ms:
                continue

            # ── 레짐 필터 ────────────────────────────────────────────────────
            rf_json = pat.get("regime_filter_json")
            if rf_json:
                try:
                    rf_list = json.loads(rf_json)
                    if rf_list and regime_str not in rf_list:
                        continue
                except Exception:
                    pass

            # ── 심볼 결정 ────────────────────────────────────────────────────
            pat_symbol = pat.get("symbol", "ALL")
            target_syms = all_symbols if pat_symbol == "ALL" else [pat_symbol]

            # ── 조건 파싱 ────────────────────────────────────────────────────
            try:
                conditions = json.loads(pat.get("conditions_json", "[]"))
            except Exception:
                conditions = []
            if not conditions:
                continue

            logic    = pat.get("conditions_logic", "AND")
            interval = pat.get("interval", "1h")

            for sym in target_syms:
                try:
                    met = evaluate_conditions(conditions, logic, store, sym, interval)
                except Exception as exc:
                    logger.debug("[ImagePattern] condition eval error %s: %s", sym, exc)
                    met = False

                if not met:
                    continue

                # ── 신호 생성 ────────────────────────────────────────────────
                ticker = store.get_ticker(sym)
                if not ticker:
                    continue
                price = float(ticker.get("price", 0))
                if price <= 0:
                    continue

                direction = pat.get("direction", "LONG")
                action    = "BUY" if direction == "LONG" else "SELL"
                tp_pct    = float(pat.get("tp_pct", 3.0)) / 100
                sl_pct    = float(pat.get("sl_pct", 1.5)) / 100

                if action == "BUY":
                    tp = round(price * (1 + tp_pct), 8)
                    sl = round(price * (1 - sl_pct), 8)
                else:
                    tp = round(price * (1 - tp_pct), 8)
                    sl = round(price * (1 + sl_pct), 8)

                reason = (
                    f"[이미지패턴] {pat.get('pattern_name','Custom')}"
                    f" | {pat.get('description','')}"
                )
                sig = Signal(
                    strategy  = self.name,
                    symbol    = sym,
                    action    = action,
                    mode      = "PAPER",
                    confidence= float(pat.get("min_confidence", 0.65)),
                    regime    = regime_str,
                    tp        = tp,
                    sl        = sl,
                    reason    = reason,
                )
                signals.append(sig)

                # 쿨다운을 위해 last_signal_ts 갱신 + 캐시 무효화
                store.update_image_pattern_last_signal(pat["id"], now_ms)
                self._cache_ts = 0.0   # 다음 cycle에 재로드

                logger.info(
                    "[ImagePattern] Signal %s %s @ %.4f  pattern='%s'",
                    action, sym, price, pat.get("pattern_name"),
                )

        return signals

    # ---------------------------------------------------------------------- #
    # Internal helpers
    # ---------------------------------------------------------------------- #

    def _load_patterns(self, store: "DataStore") -> List[dict]:
        """활성 패턴 목록 (60초 캐시)."""
        now = time.time()
        if now - self._cache_ts < _CACHE_TTL_S and self._cache:
            return self._cache
        try:
            self._cache    = store.get_active_image_patterns()
            self._cache_ts = now
        except Exception as exc:
            logger.error("[ImagePattern] load_patterns error: %s", exc)
        return self._cache

    def invalidate_cache(self) -> None:
        """패턴이 추가/수정/삭제될 때 대시보드 API에서 호출."""
        self._cache_ts = 0.0
        self._cache    = []
