"""
Executor — Binance Futures order execution (Part 3.1 / Part 3.2).

Handles:
  - HMAC-SHA256 signed requests to fapi.binance.com
  - Market entries + STOP_MARKET SL + TAKE_PROFIT_MARKET TP
  - Idempotency via signal_id deduplication
  - Partial fill handling (Part 6.2)
  - API failure counting → 3 failures → KillSwitch
  - DB write failure queuing (Part 6.3)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
import uuid
from typing import TYPE_CHECKING, Dict, List, Optional, Set

import httpx

if TYPE_CHECKING:
    from bot.config import Config
    from bot.data.store import DataStore
    from bot.execution.state_machine import OrderStateMachine
    from bot.execution.kill_switch import KillSwitch
    from bot.strategies._base import Signal

logger = logging.getLogger(__name__)

# Maximum consecutive API failures before triggering kill switch
MAX_API_FAILURES = 3

# Whether to cancel the unfilled portion of a partial fill (configurable via .env)
PARTIAL_FILL_CANCEL = os.environ.get("PARTIAL_FILL_CANCEL", "true").lower() in ("1", "true", "yes")


class Executor:
    """
    Executes orders on Binance Futures (fapi.binance.com or testnet).

    All signed requests use HMAC-SHA256.
    Signal IDs are deduplicated to prevent double-entry.
    """

    def __init__(
        self,
        config: "Config",
        store: "DataStore",
        state_machine: "OrderStateMachine",
        kill_switch: "KillSwitch",
    ) -> None:
        self._config = config
        self._store = store
        self._sm = state_machine
        self._kill_switch = kill_switch

        self._api_key = config.active_binance_api_key
        self._api_secret = config.active_binance_api_secret
        self._base_url = config.binance_rest_base

        self._http: Optional[httpx.AsyncClient] = None

        # Failure counter — 3 consecutive failures → kill switch
        self._api_failure_count: int = 0

        # Idempotency set — signal_ids already submitted this session
        self._submitted_signals: Set[str] = set()

    # ---------------------------------------------------------------------- #
    # Lifecycle
    # ---------------------------------------------------------------------- #

    async def start(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=15.0,
            headers={"X-MBX-APIKEY": self._api_key},
        )
        logger.info("[Executor] Started. Base URL: %s", self._base_url)

    async def stop(self) -> None:
        if self._http:
            await self._http.aclose()
        logger.info("[Executor] Stopped.")

    # ---------------------------------------------------------------------- #
    # Main: submit order from signal
    # ---------------------------------------------------------------------- #

    async def submit_order(
        self,
        signal: "Signal",
        qty: Optional[float] = None,
    ) -> dict:
        """
        Submit a market entry order to Binance Futures.

        Steps:
          1. Check kill switch
          2. Check signal_id idempotency
          3. Submit MARKET order
          4. Record to DB immediately
          5. Register in state machine
          6. Attach SL and TP as separate orders
          7. Handle partial fill (Part 6.2)

        Returns the order result dict.
        """
        # Step 1: Kill switch check
        if self._kill_switch.is_active:
            logger.warning(
                "[Executor] KillSwitch ACTIVE — blocking new entry for %s", signal.symbol
            )
            return {"error": "kill_switch_active", "signal_id": signal.id}

        # Step 2: Idempotency — prevent double-entry from same signal
        if signal.id in self._submitted_signals:
            logger.warning(
                "[Executor] Signal %s already submitted — skipping duplicate.", signal.id
            )
            return {"error": "duplicate_signal", "signal_id": signal.id}

        self._submitted_signals.add(signal.id)

        # Generate internal order ID
        internal_order_id = str(uuid.uuid4())

        # Register in state machine
        regime = self._store.get_regime() or {}
        self._sm.create(
            order_id=internal_order_id,
            signal_id=signal.id,
            strategy=signal.strategy,
            regime_snapshot=regime,
        )

        # Step 3: Determine side
        side = "BUY" if signal.action == "BUY" else "SELL"

        # Determine quantity
        if qty is None or qty <= 0:
            logger.error("[Executor] qty=0 for signal %s — skipping order.", signal.id)
            self._sm.transition(internal_order_id, "REJECTED", reason="qty=0")
            return {"error": "qty_zero", "signal_id": signal.id}

        # Round qty to Binance precision (default 3 decimals)
        qty = round(qty, 3)

        order_params = {
            "symbol": signal.symbol,
            "side": side,
            "type": "MARKET",
            "quantity": qty,
            "newClientOrderId": internal_order_id[:36],
        }

        logger.info(
            "[Executor] Submitting %s MARKET %s qty=%.6f signal_id=%s",
            side, signal.symbol, qty, signal.id,
        )

        # Transition: SIGNAL_CREATED → ORDER_SUBMITTED
        self._sm.transition(
            internal_order_id, "ORDER_SUBMITTED",
            reason=f"Submitting MARKET {side} {qty} {signal.symbol}",
            order_params=order_params,
        )

        # Step 4: Call Binance API
        try:
            result = await self._signed_post("/fapi/v1/order", order_params)
        except Exception as exc:
            logger.error("[Executor] API error submitting order: %s", exc)
            self._handle_api_failure()
            self._sm.transition(internal_order_id, "REJECTED", reason=str(exc))
            self._store.save_order({
                "id": internal_order_id,
                "signal_id": signal.id,
                "ts": int(time.time() * 1000),
                "symbol": signal.symbol,
                "side": side,
                "type": "MARKET",
                "qty": qty,
                "status": "FAILED",
                "strategy": signal.strategy,
                "error": str(exc),
            })
            return {"error": str(exc), "signal_id": signal.id}

        # API success — reset failure counter
        self._api_failure_count = 0

        # Parse result
        binance_order_id = str(result.get("orderId", ""))
        executed_qty = float(result.get("executedQty", 0))
        orig_qty = float(result.get("origQty", qty))
        avg_price = float(result.get("avgPrice", 0) or result.get("price", 0))
        status_str = result.get("status", "NEW")

        is_partial = executed_qty > 0 and executed_qty < orig_qty
        is_filled  = executed_qty >= orig_qty or status_str == "FILLED"

        # Determine state machine status
        if is_partial:
            sm_status = "PARTIALLY_FILLED"
        elif is_filled:
            sm_status = "FILLED"
        else:
            sm_status = "REJECTED"

        self._sm.transition(
            internal_order_id, sm_status,
            reason=f"Binance status={status_str} executedQty={executed_qty}",
            execution_result=result,
        )

        # Step 4 (continued): Persist to DB immediately
        order_record = {
            "id": internal_order_id,
            "binance_order_id": binance_order_id,
            "signal_id": signal.id,
            "ts": int(time.time() * 1000),
            "symbol": signal.symbol,
            "side": side,
            "type": "MARKET",
            "qty": orig_qty,
            "price": avg_price,
            "status": sm_status,
            "filled_qty": executed_qty,
            "filled_price": avg_price,
            "fee": self._extract_fee(result),
            "strategy": signal.strategy,
            "regime": signal.regime,
            "partial_fill": is_partial,
        }
        self._store.save_order(order_record)

        # Broadcast to dashboard
        self._store._broadcast("order_update", order_record)

        # Step 5: Partial fill handling (Part 6.2)
        if is_partial:
            logger.warning(
                "[Executor] PARTIAL FILL: requested=%.6f executed=%.6f for %s",
                orig_qty, executed_qty, signal.symbol,
            )
            if PARTIAL_FILL_CANCEL and binance_order_id:
                try:
                    await self.cancel_order(binance_order_id, signal.symbol)
                    logger.info(
                        "[Executor] Cancelled unfilled portion of order %s", binance_order_id
                    )
                except Exception as exc:
                    logger.warning(
                        "[Executor] Failed to cancel partial fill remainder: %s", exc
                    )
            # Use filled qty for SL/TP
            qty_for_sl_tp = executed_qty
        else:
            qty_for_sl_tp = executed_qty if executed_qty > 0 else qty

        # Step 6: Attach SL and TP
        sl_order = None
        tp_order = None

        if sm_status in ("PARTIALLY_FILLED", "FILLED") and qty_for_sl_tp > 0:
            if signal.sl is not None:
                sl_order = await self._attach_sl(signal, qty_for_sl_tp, internal_order_id)

            if signal.tp is not None:
                tp_order = await self._attach_tp(signal, qty_for_sl_tp, internal_order_id)

        return {
            "internal_order_id": internal_order_id,
            "binance_order_id":  binance_order_id,
            "signal_id":         signal.id,
            "status":            sm_status,
            "filled_qty":        executed_qty,
            "avg_price":         avg_price,
            "sl_order":          sl_order,
            "tp_order":          tp_order,
            "partial_fill":      is_partial,
        }

    # ---------------------------------------------------------------------- #
    # SL / TP attachment
    # ---------------------------------------------------------------------- #

    async def _attach_sl(self, signal: "Signal", qty: float, parent_order_id: str) -> Optional[dict]:
        """Attach a STOP_MARKET order as the stop-loss."""
        sl_side = "SELL" if signal.action == "BUY" else "BUY"
        params = {
            "symbol":       signal.symbol,
            "side":         sl_side,
            "type":         "STOP_MARKET",
            "stopPrice":    signal.sl,
            "quantity":     round(qty, 3),
            "closePosition": "false",
            "newClientOrderId": f"sl-{parent_order_id[:28]}",
        }
        try:
            result = await self._signed_post("/fapi/v1/order", params)
            self._sm.transition(
                parent_order_id, "SL_ATTACHED",
                reason=f"SL order placed at {signal.sl}",
                execution_result=result,
            )
            logger.info(
                "[Executor] SL attached for %s at %.4f (binance_id=%s)",
                signal.symbol, signal.sl, result.get("orderId"),
            )
            return result
        except Exception as exc:
            logger.error("[Executor] Failed to attach SL for %s: %s", signal.symbol, exc)
            self._handle_api_failure()
            return None

    async def _attach_tp(self, signal: "Signal", qty: float, parent_order_id: str) -> Optional[dict]:
        """Attach a TAKE_PROFIT_MARKET order as the take-profit."""
        tp_side = "SELL" if signal.action == "BUY" else "BUY"
        params = {
            "symbol":       signal.symbol,
            "side":         tp_side,
            "type":         "TAKE_PROFIT_MARKET",
            "stopPrice":    signal.tp,
            "quantity":     round(qty, 3),
            "closePosition": "false",
            "newClientOrderId": f"tp-{parent_order_id[:28]}",
        }
        try:
            result = await self._signed_post("/fapi/v1/order", params)
            self._sm.transition(
                parent_order_id, "TP_ATTACHED",
                reason=f"TP order placed at {signal.tp}",
                execution_result=result,
            )
            logger.info(
                "[Executor] TP attached for %s at %.4f (binance_id=%s)",
                signal.symbol, signal.tp, result.get("orderId"),
            )
            return result
        except Exception as exc:
            logger.error("[Executor] Failed to attach TP for %s: %s", signal.symbol, exc)
            self._handle_api_failure()
            return None

    # ---------------------------------------------------------------------- #
    # Cancel orders
    # ---------------------------------------------------------------------- #

    async def cancel_order(self, order_id: str, symbol: str) -> dict:
        """Cancel a specific order on Binance."""
        params = {"symbol": symbol, "orderId": order_id}
        try:
            result = await self._signed_delete("/fapi/v1/order", params)
            self._api_failure_count = 0
            logger.info("[Executor] Cancelled order %s on %s", order_id, symbol)
            return result
        except Exception as exc:
            logger.error("[Executor] Cancel order %s failed: %s", order_id, exc)
            self._handle_api_failure()
            return {"error": str(exc)}

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> list:
        """
        Cancel all open orders.
        If symbol is provided, cancel only that symbol.
        Otherwise cancel all tracked symbols.
        """
        cancelled = []
        symbols = [symbol] if symbol else self._config.tracked_symbols

        for sym in symbols:
            params = {"symbol": sym}
            try:
                result = await self._signed_delete("/fapi/v1/allOpenOrders", params)
                self._api_failure_count = 0
                cancelled.append({"symbol": sym, "result": result})
                logger.info("[Executor] Cancelled all orders on %s", sym)
            except httpx.HTTPStatusError as exc:
                # 400 = no open orders — not a real failure
                if exc.response.status_code == 400:
                    logger.debug("[Executor] No open orders on %s", sym)
                else:
                    logger.error("[Executor] Cancel all orders on %s failed: %s", sym, exc)
                    self._handle_api_failure()
            except Exception as exc:
                logger.error("[Executor] Cancel all orders on %s failed: %s", sym, exc)
                self._handle_api_failure()

        return cancelled

    # ---------------------------------------------------------------------- #
    # Position / order queries
    # ---------------------------------------------------------------------- #

    async def get_open_positions(self) -> list:
        """Fetch open positions from Binance /fapi/v2/positionRisk."""
        try:
            result = await self._signed_get("/fapi/v2/positionRisk", {})
            self._api_failure_count = 0
            # Filter positions with non-zero quantity
            return [
                p for p in result
                if float(p.get("positionAmt", 0)) != 0
            ]
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 401):
                logger.warning(
                    "[Executor] get_open_positions unavailable (%d) — API key not authorized. Binance: %s",
                    exc.response.status_code,
                    exc.response.text,
                )
            else:
                logger.error("[Executor] get_open_positions error: %s", exc)
                self._handle_api_failure()
            return []
        except Exception as exc:
            if isinstance(exc, RuntimeError) and "event loop" in str(exc):
                logger.debug("[Executor] get_open_positions skipped — cross-loop call")
                return []
            logger.error("[Executor] get_open_positions error: %s", exc)
            self._handle_api_failure()
            return []

    async def get_open_orders(self) -> list:
        """Fetch all open orders from Binance /fapi/v1/openOrders."""
        try:
            result = await self._signed_get("/fapi/v1/openOrders", {})
            self._api_failure_count = 0
            return result if isinstance(result, list) else []
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 401):
                logger.warning(
                    "[Executor] get_open_orders unavailable (%d) — API key not authorized.",
                    exc.response.status_code,
                )
            else:
                logger.error("[Executor] get_open_orders error: %s", exc)
                self._handle_api_failure()
            return []
        except Exception as exc:
            if isinstance(exc, RuntimeError) and "event loop" in str(exc):
                logger.debug("[Executor] get_open_orders skipped — cross-loop call")
                return []
            logger.error("[Executor] get_open_orders error: %s", exc)
            self._handle_api_failure()
            return []

    async def close_position_reduce_only(self, symbol: str, side: str, qty: float) -> dict:
        """Place a reduce-only MARKET order to close a position."""
        close_side = "SELL" if side == "LONG" else "BUY"
        params = {
            "symbol":     symbol,
            "side":       close_side,
            "type":       "MARKET",
            "quantity":   round(qty, 3),
            "reduceOnly": "true",
            "newClientOrderId": f"close-{str(uuid.uuid4())[:28]}",
        }
        try:
            result = await self._signed_post("/fapi/v1/order", params)
            self._api_failure_count = 0
            logger.info("[Executor] Reduce-only close for %s side=%s qty=%.6f", symbol, side, qty)
            return result
        except Exception as exc:
            logger.error("[Executor] close_position_reduce_only error: %s", exc)
            self._handle_api_failure()
            return {"error": str(exc)}

    # ---------------------------------------------------------------------- #
    # Account info
    # ---------------------------------------------------------------------- #

    async def get_account_balance(self) -> float:
        """Return the USDT futures wallet balance."""
        try:
            result = await self._signed_get("/fapi/v2/account", {})
            self._api_failure_count = 0
            assets = result.get("assets", [])
            for asset in assets:
                if asset.get("asset") == "USDT":
                    return float(asset.get("walletBalance", 0))
            return 0.0
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 401):
                # Auth error — API key not authorized (testnet keys return 400, mainnet 401).
                # Not a transient failure — don't trigger kill switch.
                logger.warning(
                    "[Executor] Account balance unavailable (%d) — API key not authorized. Using cached.",
                    exc.response.status_code,
                )
            else:
                logger.error("[Executor] get_account_balance error: %s", exc)
                self._handle_api_failure()
            return self._store.get_account_balance()
        except Exception as exc:
            if isinstance(exc, RuntimeError) and "event loop" in str(exc):
                logger.debug("[Executor] get_account_balance skipped — cross-loop call")
                return self._store.get_account_balance()
            logger.error("[Executor] get_account_balance error: %s", exc)
            self._handle_api_failure()
            return self._store.get_account_balance()

    # ---------------------------------------------------------------------- #
    # API failure handling
    # ---------------------------------------------------------------------- #

    def _handle_api_failure(self) -> None:
        """Increment failure counter; trigger kill switch at threshold."""
        self._api_failure_count += 1
        logger.warning(
            "[Executor] API failure count: %d/%d",
            self._api_failure_count, MAX_API_FAILURES,
        )
        if self._api_failure_count >= MAX_API_FAILURES:
            logger.critical(
                "[Executor] %d consecutive API failures — scheduling kill switch.",
                self._api_failure_count,
            )
            asyncio.create_task(
                self._kill_switch.trigger(
                    reason=f"{self._api_failure_count} consecutive API failures",
                    triggered_by="executor",
                )
            )

    # ---------------------------------------------------------------------- #
    # Signed HTTP helpers (HMAC-SHA256)
    # ---------------------------------------------------------------------- #

    def _sign(self, params: dict) -> dict:
        """Add timestamp and HMAC-SHA256 signature to params."""
        params["timestamp"] = int(time.time() * 1000)
        query = urllib.parse.urlencode(params)
        signature = hmac.new(
            self._api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        params["signature"] = signature
        return params

    async def _signed_get(self, path: str, params: dict) -> dict:
        """GET with HMAC signature."""
        signed = self._sign(dict(params))
        resp = await self._http.get(path, params=signed)
        resp.raise_for_status()
        return resp.json()

    async def _signed_post(self, path: str, params: dict) -> dict:
        """POST with HMAC signature (params sent as form data)."""
        signed = self._sign(dict(params))
        resp = await self._http.post(path, data=signed)
        resp.raise_for_status()
        return resp.json()

    async def _signed_delete(self, path: str, params: dict) -> dict:
        """DELETE with HMAC signature."""
        signed = self._sign(dict(params))
        resp = await self._http.delete(path, params=signed)
        resp.raise_for_status()
        return resp.json()

    # ---------------------------------------------------------------------- #
    # Utility
    # ---------------------------------------------------------------------- #

    @staticmethod
    def _extract_fee(result: dict) -> float:
        """Extract commission/fee from Binance order result."""
        fills = result.get("fills", [])
        if fills:
            return sum(float(f.get("commission", 0)) for f in fills)
        return 0.0
