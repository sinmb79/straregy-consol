"""
ScoringEngine — v1.3 Master Plan PART 5

기회의 실행 가치를 수치화한다.
모든 점수 계산과 판단 근거는 Audit Trail에 남긴다.

점수표 (PART 5.2):
  +2  volume spike
  +2  funding extreme (reversal 방향과 일치할 때)
  +3  OI divergence / spike
  +2  regime alignment
  +2  volatility expansion
  +1  spread GOOD
  +1  liquidity OK
  -2  최근 동일 심볼 과잉 진입
  -2  직전 30분 동일 방향 기회 중복
  -4  event risk window
  -3  UNKNOWN / 불완전 데이터

실행 문턱:
  >= 8 : 실행 후보
  6~7  : 관찰
  <= 5 : 무시
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from bot.strategies.opportunity import Opportunity

logger = logging.getLogger(__name__)


class ScoringEngine:
    """
    Opportunity 객체에 score_total과 score_breakdown을 채워 반환한다.

    Usage
    -----
    engine = ScoringEngine()
    opp = engine.score(opp, regime, recent_opps)
    """

    # ── 실행 문턱 ───────────────────────────────────────────────────────────
    THRESHOLD_EXECUTE = 8
    THRESHOLD_WATCH   = 6

    # ── 점수 가중치 (PART 5.2) ──────────────────────────────────────────────
    SCORE_VOLUME_SPIKE     = 2
    SCORE_FUNDING_EXTREME  = 2
    SCORE_OI_STRUCTURE     = 3
    SCORE_REGIME_ALIGN     = 2
    SCORE_VOLATILITY_EXP   = 2
    SCORE_SPREAD_GOOD      = 1
    SCORE_LIQUIDITY_OK     = 1

    PENALTY_OVERSATURATED  = -2   # 최근 동일 심볼 과잉 진입
    PENALTY_DUPLICATE_DIR  = -2   # 30분 내 동일 방향 중복
    PENALTY_EVENT_RISK     = -4   # event risk 구간
    PENALTY_UNKNOWN_DATA   = -3   # UNKNOWN / 불완전 데이터
    SCORE_MTF_ALIGNED      = 2    # 4H 컨텍스트와 신호 방향 일치
    PENALTY_MTF_CONFLICT   = -2   # 4H 컨텍스트와 신호 방향 충돌

    # 동일 방향 중복 탐지 윈도우 (ms)
    DUPLICATE_WINDOW_MS = 30 * 60 * 1000   # 30분

    def score(
        self,
        opp: "Opportunity",
        regime: dict,
        recent_opps: List["Opportunity"],
    ) -> "Opportunity":
        """
        opp의 score_total과 score_breakdown을 계산하여 채운다.
        recent_opps는 최근 생성된 기회 목록 (중복/과잉 탐지에 사용).
        """
        breakdown: dict = {}
        total = 0

        # ── +2  거래량 급증 ──────────────────────────────────────────────── #
        if opp.volume_state == "HIGH":
            total += self.SCORE_VOLUME_SPIKE
            breakdown["volume_spike"] = self.SCORE_VOLUME_SPIKE

        # ── +2  펀딩 극값 (방향 일치) ────────────────────────────────────── #
        funding_bonus = self._score_funding(opp)
        if funding_bonus:
            total += self.SCORE_FUNDING_EXTREME
            breakdown["funding_extreme"] = self.SCORE_FUNDING_EXTREME

        # ── +3  OI 구조 ──────────────────────────────────────────────────── #
        if opp.oi_state in ("SPIKE", "DIVERGENCE"):
            total += self.SCORE_OI_STRUCTURE
            breakdown["oi_structure"] = self.SCORE_OI_STRUCTURE

        # ── +2  레짐 정합성 ───────────────────────────────────────────────── #
        regime_bonus = self._score_regime_alignment(opp, regime)
        if regime_bonus:
            total += self.SCORE_REGIME_ALIGN
            breakdown["regime_alignment"] = self.SCORE_REGIME_ALIGN

        # ── +2  변동성 확장 ───────────────────────────────────────────────── #
        if opp.volatility_state == "EXPANDING":
            total += self.SCORE_VOLATILITY_EXP
            breakdown["volatility_expansion"] = self.SCORE_VOLATILITY_EXP

        # ── +1  스프레드 양호 ─────────────────────────────────────────────── #
        if opp.spread_state == "GOOD":
            total += self.SCORE_SPREAD_GOOD
            breakdown["spread_good"] = self.SCORE_SPREAD_GOOD

        # ── +1  유동성 충족 ───────────────────────────────────────────────── #
        if opp.liquidity_state == "OK":
            total += self.SCORE_LIQUIDITY_OK
            breakdown["liquidity_ok"] = self.SCORE_LIQUIDITY_OK

        # ── -2  동일 심볼 과잉 진입 ───────────────────────────────────────── #
        if self._is_oversaturated(opp, recent_opps):
            total += self.PENALTY_OVERSATURATED
            breakdown["oversaturated"] = self.PENALTY_OVERSATURATED

        # ── -2  30분 내 동일 방향 중복 ───────────────────────────────────── #
        if self._is_duplicate_direction(opp, recent_opps):
            total += self.PENALTY_DUPLICATE_DIR
            breakdown["duplicate_direction"] = self.PENALTY_DUPLICATE_DIR

        # ── -4  event risk ────────────────────────────────────────────────── #
        current_regime = regime.get("regime", "UNKNOWN")
        if current_regime == "EVENT_RISK":
            total += self.PENALTY_EVENT_RISK
            breakdown["event_risk"] = self.PENALTY_EVENT_RISK

        # ── -2  Fast Layer CAUTION (단기 구조 이상) ───────────────────────── #
        fast = regime.get("fast_layer", {})
        if fast.get("alert_level") == "CAUTION":
            total += self.PENALTY_OVERSATURATED  # -2 (재사용)
            breakdown["fast_layer_caution"] = self.PENALTY_OVERSATURATED

        # ── -3  불완전 데이터 ─────────────────────────────────────────────── #
        if self._is_incomplete_data(opp, regime):
            total += self.PENALTY_UNKNOWN_DATA
            breakdown["unknown_data"] = self.PENALTY_UNKNOWN_DATA

        # ── ±2  4H 멀티타임프레임 컨텍스트 정합성 ────────────────────────── #
        mtf_score = self._score_mtf_alignment(opp, regime)
        if mtf_score > 0:
            total += mtf_score
            breakdown["mtf_aligned"] = mtf_score
        elif mtf_score < 0:
            total += mtf_score
            breakdown["mtf_conflict"] = mtf_score

        opp.score_total    = total
        opp.score_breakdown = breakdown

        logger.debug(
            "[Scoring] %s %s %s  score=%d  breakdown=%s",
            opp.symbol, opp.side, opp.category, total, breakdown,
        )
        return opp

    # ---------------------------------------------------------------------- #
    # Internal helpers
    # ---------------------------------------------------------------------- #

    def _score_funding(self, opp: "Opportunity") -> bool:
        """
        reversal 전략에서 펀딩 극값이 방향과 일치하면 보너스.
        - LONG reversal + EXTREME_SHORT funding → 과매도 숏 커버 가능
        - SHORT reversal + EXTREME_LONG funding → 과매수 롱 청산 가능
        """
        if opp.category != "reversal":
            return opp.funding_state in ("EXTREME_LONG", "EXTREME_SHORT")
        if opp.side == "LONG" and opp.funding_state == "EXTREME_SHORT":
            return True
        if opp.side == "SHORT" and opp.funding_state == "EXTREME_LONG":
            return True
        return False

    def _score_regime_alignment(self, opp: "Opportunity", regime: dict) -> bool:
        """카테고리 + 방향(side)과 레짐의 방향성이 일치하면 보너스."""
        r = regime.get("regime", "UNKNOWN")
        if r in ("EVENT_RISK", "UNKNOWN"):
            return False

        # ── reversal ────────────────────────────────────────────────────── #
        if opp.category == "reversal":
            # BTC_BEARISH에서는 SHORT reversal만 레짐 정합
            # (하락장에서 LONG 역추세는 고위험 — 보너스 미부여)
            if r == "BTC_BEARISH":
                return opp.side == "SHORT"
            # BTC_BULLISH에서는 SHORT reversal만 정합
            if r == "BTC_BULLISH":
                return opp.side == "SHORT"
            return r in ("BTC_SIDEWAYS", "HIGH_VOLATILITY", "ALT_ROTATION")

        # ── breakout ────────────────────────────────────────────────────── #
        if opp.category == "breakout":
            return r in ("BTC_BULLISH", "LOW_VOLATILITY", "BTC_SIDEWAYS")

        # ── trend (LONG 추세) ────────────────────────────────────────────── #
        if opp.category == "trend":
            return r in ("BTC_BULLISH", "ALT_ROTATION")

        # ── bear_trend (SHORT 추세 — 신규) ──────────────────────────────── #
        if opp.category == "bear_trend":
            if r == "BTC_BEARISH":
                return opp.side == "SHORT"   # SHORT만 레짐 정합
            if r == "HIGH_VOLATILITY":
                return True   # 양방향 허용 (LONG 반전도 HV에서 의미)
            return False

        # ── range (박스권 양방향 — 신규) ────────────────────────────────── #
        if opp.category == "range":
            return r == "BTC_SIDEWAYS"

        # ── volatility (변동성 모멘텀 — 신규) ───────────────────────────── #
        if opp.category == "volatility":
            return r == "HIGH_VOLATILITY"

        # ── pattern ─────────────────────────────────────────────────────── #
        if opp.category == "pattern":
            return r not in ("EVENT_RISK", "UNKNOWN")

        return False

    def _is_oversaturated(
        self, opp: "Opportunity", recent_opps: List["Opportunity"]
    ) -> bool:
        """최근 기회 목록에서 동일 심볼이 2개 이상 있으면 과잉 진입."""
        count = sum(
            1 for o in recent_opps
            if o.symbol == opp.symbol
            and o.execution_status in ("PENDING", "APPROVED", "EXECUTED")
        )
        return count >= 2

    def _is_duplicate_direction(
        self, opp: "Opportunity", recent_opps: List["Opportunity"]
    ) -> bool:
        """30분 내 같은 방향 기회가 이미 있으면 중복."""
        now = opp.ts
        for o in recent_opps:
            if (
                o.id != opp.id
                and o.side == opp.side
                and (now - o.ts) <= self.DUPLICATE_WINDOW_MS
                and o.execution_status not in ("IGNORED", "EXPIRED")
            ):
                return True
        return False

    def _is_incomplete_data(self, opp: "Opportunity", regime: dict) -> bool:
        """필수 데이터가 없거나 레짐이 UNKNOWN이면 불완전."""
        if regime.get("regime", "UNKNOWN") == "UNKNOWN":
            return True
        if opp.liquidity_state == "CRITICAL":
            return True
        return False

    def _score_mtf_alignment(self, opp: "Opportunity", regime: dict) -> int:
        """
        4H 컨텍스트(regime 지표)와 신호 방향의 정합성 점수.
        btc_price > btc_ema50 → 4H 상승 컨텍스트
        btc_price < btc_ema50 → 4H 하락 컨텍스트
        Returns: +2 (aligned), 0 (neutral/unclear), -2 (conflict)
        """
        btc_price = regime.get("btc_price")
        btc_ema50 = regime.get("btc_ema50")
        if btc_price is None or btc_ema50 is None or btc_ema50 == 0:
            return 0

        four_h_bullish = btc_price > btc_ema50
        signal_long = opp.side == "LONG"

        # trend/breakout/bear_trend: 4H 방향과 일치해야 유리
        if opp.category in ("trend", "breakout", "bear_trend"):
            if four_h_bullish and signal_long:
                return self.SCORE_MTF_ALIGNED
            if not four_h_bullish and not signal_long:
                return self.SCORE_MTF_ALIGNED
            return self.PENALTY_MTF_CONFLICT

        # reversal: 역방향이 맞음 (4H 하락 + LONG = 반등 포착)
        if opp.category == "reversal":
            if not four_h_bullish and signal_long:
                return self.SCORE_MTF_ALIGNED
            if four_h_bullish and not signal_long:
                return self.SCORE_MTF_ALIGNED
            return self.PENALTY_MTF_CONFLICT

        # range: 4H 방향 무관 (박스 안에서 양방향 모두 유효)
        # volatility: 4H 방향과 일치하면 가산
        if opp.category == "volatility":
            if four_h_bullish and signal_long:
                return self.SCORE_MTF_ALIGNED
            if not four_h_bullish and not signal_long:
                return self.SCORE_MTF_ALIGNED
            return 0   # 충돌해도 감점 없음 (변동성은 방향 무관할 수 있음)

        # pattern/range: 중립
        return 0
