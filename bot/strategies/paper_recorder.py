"""
PaperRecorder — tracks paper trading positions and computes performance metrics.

Responsibilities:
  - Open a paper position when a BUY or SELL signal arrives
  - Monitor prices from DataStore to detect TP/SL hits
  - TIME STOP: close positions that exceed their maximum hold window
  - BREAK-EVEN STOP: move SL to entry price once 1R profit is reached
  - Close positions and record PnL to SQLite (paper_positions table)
  - Compute per-strategy stats: Win Rate, Profit Factor, MDD, Expectancy
  - Broadcast position updates to dashboard via DataStore._broadcast()
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from bot.data.store import DataStore
    from bot.data.replay_account import ReplayAccount
    from bot.strategies._base import Signal

logger = logging.getLogger(__name__)

# Fraction of price used as default SL/TP when strategy provides none
DEFAULT_SL_PCT = 0.015   # 1.5%
DEFAULT_TP_PCT = 0.030   # 3.0%

# --------------------------------------------------------------------------- #
# Time Stop Policy — 전략별 최대 보유 시간 (ms)
# --------------------------------------------------------------------------- #
TIME_STOP_MS: Dict[str, int] = {
    "overreaction_reversal":         4 * 3600 * 1000,   # 4h
    "volatility_expansion_breakout": 3 * 3600 * 1000,   # 3h
    "early_trend_capture":           6 * 3600 * 1000,   # 6h
    "image_pattern":                 2 * 3600 * 1000,   # 2h
    # 신규 전략
    "bear_trend":                    6 * 3600 * 1000,   # 6h (추세추종 — 더 긴 보유)
    "range_trader":                  4 * 3600 * 1000,   # 4h (박스권 레인지)
    "volatility_momentum":           2 * 3600 * 1000,   # 2h (빠른 모멘텀 — 짧게)
    # legacy PAUSED
    "ema_cross":                     6 * 3600 * 1000,   # 6h
    "rsi_exhaustion":                4 * 3600 * 1000,   # 4h
    "range_breakout":                3 * 3600 * 1000,   # 3h
}
DEFAULT_TIME_STOP_MS = 4 * 3600 * 1000   # 4h fallback


def _time_stop_label(strategy: str) -> str:
    ms = TIME_STOP_MS.get(strategy, DEFAULT_TIME_STOP_MS)
    return f"{ms // 3600000}h"


# --------------------------------------------------------------------------- #
# In-memory position model
# --------------------------------------------------------------------------- #

@dataclass
class PaperPosition:
    """Represents a single open or closed paper position."""

    id:           str
    strategy:     str
    symbol:       str
    side:         str         # LONG or SHORT
    entry_price:  float
    qty:          float       # nominal unit qty (1.0 for paper)
    tp:           Optional[float]
    sl:           Optional[float]
    opened_at:    int         # unix ms
    regime:       str
    signal_id:    str

    # Time Stop
    expiry_ts:         Optional[int] = None   # unix ms — 만료 시 강제 청산
    time_stop_policy:  str = ""               # e.g. "4h"

    # Mutable after open
    status:            str  = "OPEN"   # OPEN | CLOSED
    closed_at:         Optional[int]   = None
    exit_price:        Optional[float] = None
    pnl_pct:           Optional[float] = None
    close_reason:      str             = ""
    be_stop_activated: int             = 0    # 1 = break-even SL이 이동됨

    def to_dict(self) -> dict:
        return {
            "id":                self.id,
            "strategy":          self.strategy,
            "symbol":            self.symbol,
            "side":              self.side,
            "entry_price":       self.entry_price,
            "qty":               self.qty,
            "tp":                self.tp,
            "sl":                self.sl,
            "opened_at":         self.opened_at,
            "regime":            self.regime,
            "signal_id":         self.signal_id,
            "expiry_ts":         self.expiry_ts,
            "time_stop_policy":  self.time_stop_policy,
            "status":            self.status,
            "closed_at":         self.closed_at,
            "exit_price":        self.exit_price,
            "pnl_pct":           self.pnl_pct,
            "close_reason":      self.close_reason,
            "be_stop_activated": self.be_stop_activated,
        }


# --------------------------------------------------------------------------- #
# PaperRecorder
# --------------------------------------------------------------------------- #

class PaperRecorder:
    """
    Manages the lifecycle of paper positions.

    Usage
    -----
    recorder = PaperRecorder(store)
    recorder.on_signal(signal)           # called by StrategyManager
    recorder.check_positions()           # called by StrategyManager each cycle
    stats = recorder.get_strategy_stats()
    """

    def __init__(
        self,
        store: "DataStore",
        replay_account: Optional["ReplayAccount"] = None,
    ) -> None:
        self._store = store
        self._replay_account = replay_account
        # In-memory open positions: id -> PaperPosition
        self._open: Dict[str, PaperPosition] = {}
        # Replay clock: set by replay loop to use candle ts instead of wall-clock
        self._replay_ts_ms: Optional[int] = None

    def set_replay_ts(self, ts_ms: int) -> None:
        """Inject current candle timestamp for replay mode."""
        self._replay_ts_ms = ts_ms

    def _now_ms(self) -> int:
        if self._replay_ts_ms is not None:
            return self._replay_ts_ms
        return int(time.time() * 1000)

    # ---------------------------------------------------------------------- #
    # Signal intake
    # ---------------------------------------------------------------------- #

    def _get_entry_price(self, symbol: str) -> float:
        ticker = self._store.get_ticker(symbol)
        if ticker:
            price = float(ticker.get("price", 0.0))
            if price > 0:
                return price
        candles = self._store.get_candles(symbol, "1h", limit=1)
        if candles:
            price = float(candles[-1].get("c", 0.0))
            if price > 0:
                logger.debug(
                    "[PaperRecorder] Using candle fallback price %.4f for %s",
                    price, symbol,
                )
                return price
        return 0.0

    def on_signal(self, signal: "Signal") -> None:
        """
        BUY  → open LONG paper position
        SELL → open SHORT paper position (or close any open LONG)
        """
        entry_price = self._get_entry_price(signal.symbol)
        if entry_price <= 0:
            logger.warning(
                "[PaperRecorder] No price for %s — cannot open position",
                signal.symbol,
            )
            return

        existing = self._find_open(signal.symbol, signal.strategy)

        if signal.action == "BUY":
            if existing:
                logger.debug(
                    "[PaperRecorder] Already open LONG for %s/%s — ignoring BUY",
                    signal.symbol, signal.strategy,
                )
                return
            self._open_position(signal, entry_price, "LONG")

        elif signal.action == "SELL":
            if existing and existing.side == "LONG":
                self._close_position(existing, entry_price, "SELL signal")
            elif existing and existing.side == "SHORT":
                logger.debug(
                    "[PaperRecorder] Already open SHORT for %s/%s — ignoring SELL",
                    signal.symbol, signal.strategy,
                )
                return
            else:
                self._open_position(signal, entry_price, "SHORT")

    # ---------------------------------------------------------------------- #
    # Position monitoring (called each engine cycle)
    # ---------------------------------------------------------------------- #

    def check_positions(self) -> None:
        """
        매 사이클마다 오픈 포지션을 순회하여:
          1. Break-even SL 활성화 (1R 도달 시 SL → entry price)
          2. TP / SL 청산
          3. Time Stop 청산 (expiry_ts 초과 시 강제 청산)
        """
        now_ms = self._now_ms()
        to_close: List[tuple] = []   # (pos, price, reason)

        for pos in list(self._open.values()):
            ticker = self._store.get_ticker(pos.symbol)
            if ticker is None:
                continue
            price = float(ticker.get("price", 0.0))
            if price <= 0:
                continue

            # ── 1. Break-even SL 활성화 ─────────────────────────────────── #
            if not pos.be_stop_activated and pos.sl is not None:
                self._check_break_even(pos, price)

            # ── 2. TP / SL 청산 ──────────────────────────────────────────── #
            if pos.side == "LONG":
                if pos.tp is not None and price >= pos.tp:
                    to_close.append((pos, price, "TP hit"))
                elif pos.sl is not None and price <= pos.sl:
                    reason = "BE-SL hit" if pos.be_stop_activated else "SL hit"
                    to_close.append((pos, price, reason))
            elif pos.side == "SHORT":
                if pos.tp is not None and price <= pos.tp:
                    to_close.append((pos, price, "TP hit"))
                elif pos.sl is not None and price >= pos.sl:
                    reason = "BE-SL hit" if pos.be_stop_activated else "SL hit"
                    to_close.append((pos, price, reason))

            # ── 3. Time Stop ─────────────────────────────────────────────── #
            if pos.expiry_ts is not None and now_ms >= pos.expiry_ts:
                # TP/SL로 이미 청산 대기 중이 아닐 때만 TIME_EXIT 추가
                already_queued = any(p.id == pos.id for p, _, _ in to_close)
                if not already_queued:
                    pnl_sign = "+" if (
                        (pos.side == "LONG" and price > pos.entry_price) or
                        (pos.side == "SHORT" and price < pos.entry_price)
                    ) else "-"
                    to_close.append((pos, price, f"TIME_EXIT({pnl_sign})"))

        for pos, price, reason in to_close:
            self._close_position(pos, price, reason)

    def _check_break_even(self, pos: PaperPosition, current_price: float) -> None:
        """
        1R(= 최초 SL 거리) 이익 도달 시 SL을 진입가로 이동 (Break-even Stop).
        포지션이 손실이 되더라도 최소 본전 청산을 보장.
        """
        entry = pos.entry_price
        sl = pos.sl
        if sl is None:
            return

        risk = abs(entry - sl)   # 최초 리스크 = 진입가와 SL 거리
        if risk <= 0:
            return

        if pos.side == "LONG":
            be_trigger = entry + risk   # 1R 이익 도달 가격
            if current_price >= be_trigger and sl < entry:
                pos.sl = entry
                pos.be_stop_activated = 1
                self._store.update_paper_position(pos.id, {
                    "sl": pos.sl,
                    "be_stop_activated": 1,
                })
                logger.info(
                    "[PaperRecorder] BE-Stop: %s %s  SL moved to entry %.8f",
                    pos.side, pos.symbol, entry,
                )
        elif pos.side == "SHORT":
            be_trigger = entry - risk   # 1R 이익 도달 가격
            if current_price <= be_trigger and sl > entry:
                pos.sl = entry
                pos.be_stop_activated = 1
                self._store.update_paper_position(pos.id, {
                    "sl": pos.sl,
                    "be_stop_activated": 1,
                })
                logger.info(
                    "[PaperRecorder] BE-Stop: %s %s  SL moved to entry %.8f",
                    pos.side, pos.symbol, entry,
                )

    # ---------------------------------------------------------------------- #
    # Stats
    # ---------------------------------------------------------------------- #

    def get_strategy_stats(self) -> Dict[str, dict]:
        return self._store.get_strategy_stats()

    def get_open_positions(self) -> List[dict]:
        return [p.to_dict() for p in self._open.values()]

    # ---------------------------------------------------------------------- #
    # Internal helpers
    # ---------------------------------------------------------------------- #

    def _open_position(self, signal: "Signal", entry_price: float, side: str) -> None:
        """Create and register a new paper position."""
        tp = signal.tp
        sl = signal.sl

        if tp is None:
            tp = round(
                entry_price * (1 + DEFAULT_TP_PCT) if side == "LONG"
                else entry_price * (1 - DEFAULT_TP_PCT),
                8,
            )
        if sl is None:
            sl = round(
                entry_price * (1 - DEFAULT_SL_PCT) if side == "LONG"
                else entry_price * (1 + DEFAULT_SL_PCT),
                8,
            )

        now_ms = self._now_ms()

        # Time Stop 계산
        expiry_ms = TIME_STOP_MS.get(signal.strategy, DEFAULT_TIME_STOP_MS)
        expiry_ts = now_ms + expiry_ms
        policy = _time_stop_label(signal.strategy)

        pos = PaperPosition(
            id=str(uuid.uuid4()),
            strategy=signal.strategy,
            symbol=signal.symbol,
            side=side,
            entry_price=entry_price,
            qty=1.0,
            tp=tp,
            sl=sl,
            opened_at=now_ms,
            regime=signal.regime,
            signal_id=signal.id,
            expiry_ts=expiry_ts,
            time_stop_policy=policy,
        )
        self._open[pos.id] = pos

        # Virtual account (replay mode)
        if self._replay_account is not None:
            self._replay_account.open_position(
                position_id=pos.id,
                strategy=pos.strategy,
                symbol=pos.symbol,
                side=pos.side,
                entry_price=entry_price,
                opened_at_ms=now_ms,
            )

        self._store.save_paper_position(pos.to_dict())
        self._store._broadcast("paper_position_opened", pos.to_dict())

        logger.info(
            "[PaperRecorder] Opened %s %s @ %.8f  TP=%.8f  SL=%.8f  "
            "TimeStop=%s  [%s]",
            side, signal.symbol, entry_price, tp, sl, policy, signal.strategy,
        )

    def _close_position(
        self, pos: PaperPosition, exit_price: float, reason: str
    ) -> None:
        """Close an open position and record PnL."""
        if pos.side == "LONG":
            pnl_pct = (exit_price - pos.entry_price) / pos.entry_price * 100
        else:
            pnl_pct = (pos.entry_price - exit_price) / pos.entry_price * 100

        now_ms = self._now_ms()
        pos.status       = "CLOSED"
        pos.closed_at    = now_ms
        pos.exit_price   = exit_price
        pos.close_reason = reason

        if self._replay_account is not None:
            trade = self._replay_account.close_position(
                position_id=pos.id,
                exit_price=exit_price,
                closed_at_ms=now_ms,
                close_reason=reason,
            )
            pos.pnl_pct = trade.pnl_pct if trade is not None else round(pnl_pct, 4)
        else:
            pos.pnl_pct = round(pnl_pct, 4)

        self._open.pop(pos.id, None)

        self._store.update_paper_position(pos.id, {
            "status":       pos.status,
            "closed_at":    pos.closed_at,
            "exit_price":   pos.exit_price,
            "pnl_pct":      pos.pnl_pct,
            "close_reason": pos.close_reason,
        })

        self._store._broadcast("paper_position_closed", pos.to_dict())

        outcome = "WIN" if pnl_pct > 0 else "LOSS"
        is_time_exit = reason.startswith("TIME_EXIT")
        logger.info(
            "[PaperRecorder] Closed %s %s — %s  PnL=%.2f%%  reason=%s  [%s]%s",
            pos.side, pos.symbol, outcome, pnl_pct, reason, pos.strategy,
            "  ⏱ TIME_EXIT" if is_time_exit else "",
        )

    def _find_open(self, symbol: str, strategy: str) -> Optional[PaperPosition]:
        for pos in self._open.values():
            if pos.symbol == symbol and pos.strategy == strategy:
                return pos
        return None
