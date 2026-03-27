"""
HyperliquidExecutor — 하이퍼리퀴드 DEX 주문 실행.

Binance Executor와 동일한 인터페이스를 제공.
HYPERLIQUID_ENABLED=true 시 _execute_live_signals에서 호출됨.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING, Dict, List, Optional, Set

if TYPE_CHECKING:
    from bot.config import Config
    from bot.data.store import DataStore
    from bot.execution.state_machine import OrderStateMachine
    from bot.execution.kill_switch import KillSwitch
    from bot.strategies._base import Signal

logger = logging.getLogger(__name__)

# Binance symbol → Hyperliquid coin name
_COIN_MAP: Dict[str, str] = {
    "BTCUSDT":  "BTC",
    "ETHUSDT":  "ETH",
    "SOLUSDT":  "SOL",
    "BNBUSDT":  "BNB",
    "XRPUSDT":  "XRP",
    "DOGEUSDT": "DOGE",
    "ADAUSDT":  "ADA",
    "AVAXUSDT": "AVAX",
    "SUIUSDT":  "SUI",
    "PEPEUSDT": "PEPE",
    "WIFUSDT":  "WIF",
}

MAX_API_FAILURES = 3


class HyperliquidExecutor:
    """
    하이퍼리퀴드 퍼페추얼 선물 주문 실행기.
    hyperliquid-python-sdk로 EIP-712 서명 처리.
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

        self._wallet = None
        self._exchange = None
        self._info = None

        self._api_failure_count: int = 0
        self._submitted_signals: Set[str] = set()

    # ---------------------------------------------------------------------- #
    # 생명주기
    # ---------------------------------------------------------------------- #

    async def start(self) -> None:
        try:
            import eth_account
            from hyperliquid.exchange import Exchange
            from hyperliquid.info import Info
            from hyperliquid.utils import constants

            private_key = self._config.hyperliquid_private_key
            if not private_key:
                raise ValueError("HYPERLIQUID_PRIVATE_KEY 설정 필요")

            loop = asyncio.get_event_loop()
            self._wallet = eth_account.Account.from_key(private_key)
            self._info = await loop.run_in_executor(
                None,
                lambda: Info(constants.MAINNET_API_URL, skip_ws=True),
            )
            self._exchange = await loop.run_in_executor(
                None,
                lambda: Exchange(self._wallet, constants.MAINNET_API_URL),
            )

            addr = self._config.hyperliquid_wallet_address
            logger.info(
                "[HyperliquidExecutor] 시작됨. 지갑: %s…",
                addr[:10] if addr else "N/A",
            )
        except Exception as exc:
            logger.error("[HyperliquidExecutor] 초기화 실패: %s", exc)
            raise

    async def stop(self) -> None:
        logger.info("[HyperliquidExecutor] 중지됨.")

    # ---------------------------------------------------------------------- #
    # 주문 제출
    # ---------------------------------------------------------------------- #

    async def submit_order(
        self,
        signal: "Signal",
        qty: Optional[float] = None,
    ) -> dict:
        """하이퍼리퀴드에 마켓 오더 제출."""
        if self._kill_switch.is_active:
            logger.warning(
                "[HyperliquidExecutor] KillSwitch 활성 — %s 차단", signal.symbol
            )
            return {"error": "kill_switch_active", "signal_id": signal.id}

        if signal.id in self._submitted_signals:
            logger.warning(
                "[HyperliquidExecutor] 중복 신호 %s — 건너뜀", signal.id
            )
            return {"error": "duplicate_signal", "signal_id": signal.id}

        self._submitted_signals.add(signal.id)

        coin = _COIN_MAP.get(signal.symbol)
        if not coin:
            logger.warning(
                "[HyperliquidExecutor] 미지원 심볼: %s", signal.symbol
            )
            return {
                "error": f"unsupported_symbol:{signal.symbol}",
                "signal_id": signal.id,
            }

        if not qty or qty <= 0:
            return {"error": "qty_zero", "signal_id": signal.id}

        is_buy = signal.action == "BUY"
        internal_order_id = str(uuid.uuid4())

        # State machine 등록
        regime = self._store.get_regime() or {}
        self._sm.create(
            order_id=internal_order_id,
            signal_id=signal.id,
            strategy=signal.strategy,
            regime_snapshot=regime,
        )

        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._exchange.market_open(coin, is_buy, qty, slippage=0.01),
            )

            logger.info(
                "[HyperliquidExecutor] %s %s qty=%.6f → %s",
                "BUY" if is_buy else "SELL",
                coin,
                qty,
                result.get("status", result),
            )

            self._api_failure_count = 0
            self._sm.transition(internal_order_id, "FILLED")

            # TP/SL 부착
            if signal.tp or signal.sl:
                await self._attach_tp_sl(coin, is_buy, qty, signal.tp, signal.sl)

            # DB 저장
            self._store.save_order({
                "id":        internal_order_id,
                "signal_id": signal.id,
                "ts":        int(time.time() * 1000),
                "symbol":    signal.symbol,
                "side":      "BUY" if is_buy else "SELL",
                "type":      "MARKET",
                "qty":       qty,
                "price":     None,
                "status":    "FILLED",
            })

            return {
                "internal_order_id": internal_order_id,
                "status":            "FILLED",
                "exchange":          "hyperliquid",
                "result":            result,
            }

        except Exception as exc:
            self._api_failure_count += 1
            logger.error("[HyperliquidExecutor] 주문 실패: %s", exc)
            self._sm.transition(internal_order_id, "FAILED", reason=str(exc))

            if self._api_failure_count >= MAX_API_FAILURES:
                logger.critical(
                    "[HyperliquidExecutor] %d회 연속 실패 — 킬스위치 활성화",
                    self._api_failure_count,
                )
                await self._kill_switch.activate("HL_API_FAILURES")

            return {"error": str(exc), "signal_id": signal.id}

    # ---------------------------------------------------------------------- #
    # TP / SL
    # ---------------------------------------------------------------------- #

    async def _attach_tp_sl(
        self,
        coin: str,
        entry_is_buy: bool,
        qty: float,
        tp: Optional[float],
        sl: Optional[float],
    ) -> None:
        """TP/SL reduce-only 오더 부착."""
        close_is_buy = not entry_is_buy

        if tp:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._exchange.order(
                        coin,
                        close_is_buy,
                        qty,
                        tp,
                        {"limit": {"tif": "Gtc"}},
                        reduce_only=True,
                    ),
                )
                logger.info("[HyperliquidExecutor] TP 오더: %.6f", tp)
            except Exception as exc:
                logger.warning("[HyperliquidExecutor] TP 오더 실패: %s", exc)

        if sl:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._exchange.order(
                        coin,
                        close_is_buy,
                        qty,
                        sl,
                        {
                            "trigger": {
                                "triggerPx": sl,
                                "isMarket": True,
                                "tpsl": "sl",
                            }
                        },
                        reduce_only=True,
                    ),
                )
                logger.info("[HyperliquidExecutor] SL 오더: %.6f", sl)
            except Exception as exc:
                logger.warning("[HyperliquidExecutor] SL 오더 실패: %s", exc)

    # ---------------------------------------------------------------------- #
    # 계정 정보
    # ---------------------------------------------------------------------- #

    async def get_account_balance(self) -> float:
        """하이퍼리퀴드 계정 잔고(USDT) 조회."""
        if self._info is None or self._wallet is None:
            return 0.0
        try:
            state = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._info.user_state(self._wallet.address),
            )
            return float(
                state.get("crossMarginSummary", {}).get("accountValue", 0)
            )
        except Exception as exc:
            logger.warning("[HyperliquidExecutor] 잔고 조회 실패: %s", exc)
            return 0.0

    async def get_open_positions(self) -> List[dict]:
        """하이퍼리퀴드 오픈 포지션 조회."""
        if self._info is None or self._wallet is None:
            return []
        try:
            state = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._info.user_state(self._wallet.address),
            )
            positions: List[dict] = []
            for asset_pos in state.get("assetPositions", []):
                pos = asset_pos.get("position", {})
                szi = float(pos.get("szi", 0))
                if szi == 0:
                    continue
                coin = pos.get("coin", "")
                positions.append({
                    "symbol":        coin + "USDT",
                    "positionAmt":   str(szi),
                    "entryPrice":    pos.get("entryPx", "0"),
                    "unrealizedPnl": pos.get("unrealizedPnl", "0"),
                    "exchange":      "hyperliquid",
                })
            return positions
        except Exception as exc:
            logger.warning("[HyperliquidExecutor] 포지션 조회 실패: %s", exc)
            return []
