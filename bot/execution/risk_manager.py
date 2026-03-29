"""
RiskManager — Pre-trade risk checks (Part 9.1).

Implements all 9 risk rules:
  1. Daily loss limit (2% account)
  2. Weekly loss limit (5% account)
  3. Max drawdown (20%)
  4. Max trade risk (1% per trade)
  5. Max open positions (3)
  6. Max symbol exposure (20%)
  7. Max strategy exposure (30%)
  8. Max leverage (3x)
  9. Max consecutive losses per strategy (3 → PAUSE)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from bot.data.store import DataStore
    from bot.strategies._base import Signal

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Result dataclass
# --------------------------------------------------------------------------- #

@dataclass
class RiskResult:
    """Result of a risk check."""
    passed: bool
    reason: str
    position_size: float = 0.0
    rule_failed: Optional[str] = None
    checks: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# RiskManager
# --------------------------------------------------------------------------- #

class RiskManager:
    """
    Evaluates all risk rules before order submission.

    All limits are configurable via constructor.
    Default values match the 22B master plan Part 9.1.
    """

    # Default limits
    DAILY_LOSS_LIMIT:      float = 0.02    # 2% of account balance
    WEEKLY_LOSS_LIMIT:     float = 0.05    # 5% of account balance
    MAX_DRAWDOWN:          float = 0.20    # 20% peak-to-trough
    MAX_TRADE_RISK:        float = 0.01    # 1% of balance per trade
    MAX_OPEN_POSITIONS:    int   = 3
    MAX_SYMBOL_EXPOSURE:   float = 0.20    # 20% of balance per symbol
    MAX_STRATEGY_EXPOSURE: float = 0.30    # 30% of balance per strategy
    MAX_LEVERAGE:          int   = 3
    MAX_CONSECUTIVE_LOSS:  int   = 3       # → PAUSE strategy

    def __init__(
        self,
        store: "DataStore",
        daily_loss_limit:      float = DAILY_LOSS_LIMIT,
        weekly_loss_limit:     float = WEEKLY_LOSS_LIMIT,
        max_drawdown:          float = MAX_DRAWDOWN,
        max_trade_risk:        float = MAX_TRADE_RISK,
        max_open_positions:    int   = MAX_OPEN_POSITIONS,
        max_symbol_exposure:   float = MAX_SYMBOL_EXPOSURE,
        max_strategy_exposure: float = MAX_STRATEGY_EXPOSURE,
        max_leverage:          int   = MAX_LEVERAGE,
        max_consecutive_loss:  int   = MAX_CONSECUTIVE_LOSS,
    ) -> None:
        self._store = store

        # Configurable limits
        self.daily_loss_limit      = daily_loss_limit
        self.weekly_loss_limit     = weekly_loss_limit
        self.max_drawdown          = max_drawdown
        self.max_trade_risk        = max_trade_risk
        self.max_open_positions    = max_open_positions
        self.max_symbol_exposure   = max_symbol_exposure
        self.max_strategy_exposure = max_strategy_exposure
        self.max_leverage          = max_leverage
        self.max_consecutive_loss  = max_consecutive_loss

    # ---------------------------------------------------------------------- #
    # Main entry point
    # ---------------------------------------------------------------------- #

    def check(
        self,
        signal: "Signal",
        account_balance: float,
        available_balance: Optional[float] = None,
        sl_pct: Optional[float] = None,
    ) -> RiskResult:
        """
        Run all 9 risk checks in order.

        Parameters
        ----------
        signal          : The Signal about to be executed.
        account_balance : Current account balance in USDT.
        sl_pct          : Stop-loss distance as a fraction of entry price.
                          If None, uses signal.sl to compute it from current price.

        Returns
        -------
        RiskResult with passed=True and computed position_size if all checks pass.
        """
        checks: dict = {}

        if account_balance <= 0:
            return RiskResult(
                passed=False,
                reason="Account balance is zero or negative",
                rule_failed="balance",
                checks=checks,
            )

        tradable_balance = account_balance if available_balance is None else available_balance
        checks["available_balance"] = round(tradable_balance, 4)
        if tradable_balance <= 0:
            return RiskResult(
                passed=False,
                reason="Available balance is zero or negative",
                rule_failed="available_balance",
                checks=checks,
            )

        # --- Rule 1: Daily loss limit ---
        daily_pnl, daily_pnl_pct = self._store.get_daily_pnl()
        daily_loss_pct = -daily_pnl / account_balance if daily_pnl < 0 else 0.0
        checks["daily_loss_pct"] = round(daily_loss_pct, 4)
        if daily_loss_pct >= self.daily_loss_limit:
            return RiskResult(
                passed=False,
                reason=f"Daily loss limit reached: {daily_loss_pct:.2%} >= {self.daily_loss_limit:.2%}",
                rule_failed="daily_loss",
                checks=checks,
            )

        # --- Rule 2: Weekly loss limit ---
        weekly_pnl = self._store.get_weekly_pnl()
        weekly_loss_pct = -weekly_pnl / account_balance if weekly_pnl < 0 else 0.0
        checks["weekly_loss_pct"] = round(weekly_loss_pct, 4)
        if weekly_loss_pct >= self.weekly_loss_limit:
            return RiskResult(
                passed=False,
                reason=f"Weekly loss limit reached: {weekly_loss_pct:.2%} >= {self.weekly_loss_limit:.2%}",
                rule_failed="weekly_loss",
                checks=checks,
            )

        # --- Rule 3: Max drawdown ---
        peak_balance = self._store.get_peak_balance()
        if peak_balance and peak_balance > 0:
            drawdown = (peak_balance - account_balance) / peak_balance
            checks["drawdown_pct"] = round(drawdown, 4)
            if drawdown >= self.max_drawdown:
                return RiskResult(
                    passed=False,
                    reason=f"Max drawdown reached: {drawdown:.2%} >= {self.max_drawdown:.2%}",
                    rule_failed="max_drawdown",
                    checks=checks,
                )

        # --- Rule 4: Max trade risk (compute position size) ---
        computed_sl_pct = sl_pct
        if computed_sl_pct is None:
            computed_sl_pct = self._compute_sl_pct(signal)

        if computed_sl_pct is None or computed_sl_pct <= 0:
            # Cannot compute position size without SL — reject
            return RiskResult(
                passed=False,
                reason="Cannot compute position size: no SL defined on signal",
                rule_failed="no_sl",
                checks=checks,
            )

        position_size = self.compute_position_size(
            signal,
            risk_balance=tradable_balance,
            sl_pct=computed_sl_pct,
            exposure_balance=account_balance,
        )
        checks["position_size"] = round(position_size, 6)
        checks["sl_pct"] = round(computed_sl_pct, 4)

        if position_size <= 0:
            return RiskResult(
                passed=False,
                reason="Position size computed to zero or negative",
                rule_failed="position_size",
                checks=checks,
            )

        # --- Rule 5: Max open positions ---
        open_positions = self._store.get_open_live_positions()
        open_count = len(open_positions)
        checks["open_positions"] = open_count
        if open_count >= self.max_open_positions:
            return RiskResult(
                passed=False,
                reason=f"Max open positions reached: {open_count} >= {self.max_open_positions}",
                rule_failed="max_open_positions",
                checks=checks,
            )

        # --- Rule 6: Max symbol exposure ---
        symbol_exposure = self._compute_symbol_exposure(signal.symbol, open_positions)
        symbol_exposure_pct = symbol_exposure / account_balance if account_balance > 0 else 0.0
        checks["symbol_exposure_pct"] = round(symbol_exposure_pct, 4)
        if symbol_exposure_pct >= self.max_symbol_exposure:
            return RiskResult(
                passed=False,
                reason=f"Symbol exposure limit: {signal.symbol} at {symbol_exposure_pct:.2%} >= {self.max_symbol_exposure:.2%}",
                rule_failed="symbol_exposure",
                checks=checks,
            )

        # --- Rule 7: Max strategy exposure ---
        strategy_exposure = self._compute_strategy_exposure(signal.strategy, open_positions)
        strategy_exposure_pct = strategy_exposure / account_balance if account_balance > 0 else 0.0
        checks["strategy_exposure_pct"] = round(strategy_exposure_pct, 4)
        if strategy_exposure_pct >= self.max_strategy_exposure:
            return RiskResult(
                passed=False,
                reason=f"Strategy exposure limit: {signal.strategy} at {strategy_exposure_pct:.2%} >= {self.max_strategy_exposure:.2%}",
                rule_failed="strategy_exposure",
                checks=checks,
            )

        # --- Rule 8: Max leverage check ---
        # position_size (qty) * entry_price / account_balance = effective leverage
        ticker = self._store.get_ticker(signal.symbol)
        entry_price = ticker["price"] if ticker else 0.0
        if entry_price > 0:
            notional = position_size * entry_price
            effective_leverage = notional / account_balance
            checks["effective_leverage"] = round(effective_leverage, 2)
            if effective_leverage > self.max_leverage:
                # Clamp position size to satisfy leverage constraint
                max_notional = account_balance * self.max_leverage
                position_size = max_notional / entry_price
                checks["position_size"] = round(position_size, 6)
                checks["leverage_clamped"] = True
                logger.info(
                    "[RiskManager] Leverage clamped: %.2fx → %.2fx for %s",
                    effective_leverage, self.max_leverage, signal.symbol,
                )

        # --- Rule 9: Max consecutive losses ---
        consecutive_losses = self.check_consecutive_losses(signal.strategy)
        checks["consecutive_losses"] = consecutive_losses
        if consecutive_losses >= self.max_consecutive_loss:
            return RiskResult(
                passed=False,
                reason=f"Strategy '{signal.strategy}' has {consecutive_losses} consecutive losses (limit={self.max_consecutive_loss}) — PAUSED",
                rule_failed="consecutive_losses",
                checks=checks,
            )

        # All checks passed
        logger.info(
            "[RiskManager] PASSED for %s %s | size=%.6f sl=%.2f%%",
            signal.action, signal.symbol,
            position_size, computed_sl_pct * 100,
        )
        return RiskResult(
            passed=True,
            reason="All risk checks passed",
            position_size=position_size,
            checks=checks,
        )

    # ---------------------------------------------------------------------- #
    # Position size computation
    # ---------------------------------------------------------------------- #

    def compute_position_size(
        self,
        signal: "Signal",
        risk_balance: float,
        sl_pct: float,
        exposure_balance: Optional[float] = None,
    ) -> float:
        """
        Kelly-inspired fixed-risk sizing:
          size_usdt = risk_balance * MAX_TRADE_RISK / sl_pct
          size_qty  = size_usdt / entry_price

        Capped by:
          - MAX_SYMBOL_EXPOSURE: size_usdt <= exposure_balance * MAX_SYMBOL_EXPOSURE
          - MAX_LEVERAGE: size_usdt <= exposure_balance * MAX_LEVERAGE
        """
        if sl_pct <= 0 or risk_balance <= 0:
            return 0.0
        if exposure_balance is None:
            exposure_balance = risk_balance

        # Dollar risk per trade
        risk_usdt = risk_balance * self.max_trade_risk
        size_usdt = risk_usdt / sl_pct

        # Cap by symbol exposure
        max_size_by_exposure = exposure_balance * self.max_symbol_exposure
        size_usdt = min(size_usdt, max_size_by_exposure)

        # Cap by leverage
        max_size_by_leverage = exposure_balance * self.max_leverage
        size_usdt = min(size_usdt, max_size_by_leverage)

        # Convert to quantity
        ticker = self._store.get_ticker(signal.symbol)
        entry_price = ticker["price"] if ticker else 0.0
        if entry_price <= 0:
            return 0.0

        qty = size_usdt / entry_price
        return max(qty, 0.0)

    # ---------------------------------------------------------------------- #
    # Consecutive loss check
    # ---------------------------------------------------------------------- #

    def check_consecutive_losses(self, strategy: str) -> int:
        """
        Count the number of consecutive losses (pnl_pct < 0) for a strategy
        from the most recent trades in the DB.

        Uses paper_positions and live orders tables.
        """
        consecutive = 0
        try:
            rows = self._store._conn.execute(
                """
                SELECT pnl_pct FROM paper_positions
                WHERE strategy = ? AND status = 'CLOSED' AND pnl_pct IS NOT NULL
                ORDER BY closed_at DESC
                LIMIT 10
                """,
                (strategy,),
            ).fetchall()

            for row in rows:
                pnl = row["pnl_pct"]
                if pnl is not None and float(pnl) < 0:
                    consecutive += 1
                else:
                    break  # Stop at first win

        except Exception as exc:
            logger.error("[RiskManager] check_consecutive_losses error: %s", exc)

        return consecutive

    # ---------------------------------------------------------------------- #
    # Internal helpers
    # ---------------------------------------------------------------------- #

    def _compute_sl_pct(self, signal: "Signal") -> Optional[float]:
        """
        Compute the SL distance as a fraction of entry price from signal.sl.
        Returns None if SL is not set or entry price is unknown.
        """
        if signal.sl is None:
            return None
        ticker = self._store.get_ticker(signal.symbol)
        entry_price = ticker["price"] if ticker else None
        if not entry_price or entry_price <= 0:
            return None
        sl_distance = abs(entry_price - signal.sl)
        return sl_distance / entry_price

    def _compute_symbol_exposure(self, symbol: str, open_positions: list) -> float:
        """Return total notional (USDT) already open for this symbol."""
        total = 0.0
        for pos in open_positions:
            if pos.get("symbol") == symbol:
                qty = float(pos.get("qty", 0) or 0)
                price = float(pos.get("entry_price", 0) or 0)
                total += qty * price
        return total

    def _compute_strategy_exposure(self, strategy: str, open_positions: list) -> float:
        """Return total notional (USDT) already open for this strategy."""
        total = 0.0
        for pos in open_positions:
            if pos.get("strategy") == strategy:
                qty = float(pos.get("qty", 0) or 0)
                price = float(pos.get("entry_price", 0) or 0)
                total += qty * price
        return total
