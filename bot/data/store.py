"""
DataStore — in-memory cache backed by SQLite persistence.

Design:
- All writes go to both the in-memory dict and SQLite.
- Reads always hit memory first (fast path).
- SQLite is used on startup to restore state and for historical queries.
"""

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

import sqlite3

logger = logging.getLogger(__name__)

# Maximum candle rows to keep in memory per (symbol, interval) key
MAX_CANDLES_MEMORY = 500


class DataStore:
    """Thread-safe async data store with memory cache + SQLite backend."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()

        # Memory caches
        # key: (symbol, interval)  → deque of candle dicts (sorted by ts asc)
        self._candles: Dict[tuple, Deque[dict]] = {}

        # key: symbol → latest ticker dict
        self._tickers: Dict[str, dict] = {}

        # Latest regime dict
        self._regime: Optional[dict] = None

        # Latest funding rates — key: symbol → float
        self._funding: Dict[str, float] = {}

        # Latest open interest — key: symbol → float
        self._open_interest: Dict[str, float] = {}

        # System state
        self._system_mode: str = "OBSERVE"
        self._exchange_ok: bool = False
        self._daily_pnl: float = 0.0
        self._exposure_pct: float = 0.0

        # WebSocket subscribers for dashboard push
        # Each subscriber is an asyncio.Queue
        self._subscribers: List[asyncio.Queue] = []

    # ---------------------------------------------------------------------- #
    # Subscriber management (for dashboard WebSocket push)
    # ---------------------------------------------------------------------- #

    def subscribe(self) -> asyncio.Queue:
        """Register a new subscriber; returns a queue that receives update events."""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def _broadcast(self, event_type: str, data: Any) -> None:
        """Non-blocking broadcast to all subscribers."""
        payload = json.dumps({"type": event_type, "data": data})
        dead = []
        for q in self._subscribers:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.unsubscribe(q)

    # ---------------------------------------------------------------------- #
    # System state
    # ---------------------------------------------------------------------- #

    def set_system_mode(self, mode: str) -> None:
        self._system_mode = mode
        self._broadcast("system_mode", {"mode": mode})

    def get_system_mode(self) -> str:
        return self._system_mode

    def set_exchange_status(self, ok: bool) -> None:
        self._exchange_ok = ok
        self._broadcast("exchange_status", {"ok": ok})

    def get_exchange_status(self) -> bool:
        return self._exchange_ok

    def set_daily_pnl(self, pnl: float, pnl_pct: float) -> None:
        self._daily_pnl = pnl
        self._daily_pnl_pct = pnl_pct
        self._broadcast("daily_pnl", {"pnl": pnl, "pnl_pct": pnl_pct})

    def get_daily_pnl(self) -> tuple:
        return self._daily_pnl, getattr(self, "_daily_pnl_pct", 0.0)

    def set_exposure(self, pct: float) -> None:
        self._exposure_pct = pct

    def get_exposure(self) -> float:
        return self._exposure_pct

    # ---------------------------------------------------------------------- #
    # Candles
    # ---------------------------------------------------------------------- #

    async def upsert_candle(self, symbol: str, interval: str, candle: dict) -> None:
        """Insert or replace a candle in memory and SQLite."""
        key = (symbol, interval)
        async with self._lock:
            if key not in self._candles:
                self._candles[key] = deque(maxlen=MAX_CANDLES_MEMORY)

            # Update memory (replace if same ts exists)
            dq = self._candles[key]
            existing = next((c for c in dq if c["ts"] == candle["ts"]), None)
            if existing:
                existing.update(candle)
            else:
                dq.append(candle)

            # Persist to SQLite
            try:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO candles (symbol, interval, ts, o, h, l, c, v)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        symbol,
                        interval,
                        candle["ts"],
                        candle["o"],
                        candle["h"],
                        candle["l"],
                        candle["c"],
                        candle["v"],
                    ),
                )
                self._conn.commit()
            except Exception as exc:
                logger.error("SQLite candle upsert error: %s", exc)

        self._broadcast("candle", {"symbol": symbol, "interval": interval, "candle": candle})

    def get_candles(self, symbol: str, interval: str, limit: int = 200) -> List[dict]:
        """Return the most recent `limit` candles from memory (sorted asc by ts)."""
        key = (symbol, interval)
        dq = self._candles.get(key, deque())
        items = list(dq)
        return items[-limit:] if len(items) > limit else items

    async def load_candles_from_db(self, symbol: str, interval: str, limit: int = 200) -> None:
        """Warm in-memory cache from SQLite (called on startup)."""
        rows = self._conn.execute(
            """
            SELECT ts, o, h, l, c, v FROM candles
            WHERE symbol = ? AND interval = ?
            ORDER BY ts DESC LIMIT ?
            """,
            (symbol, interval, limit),
        ).fetchall()

        key = (symbol, interval)
        if key not in self._candles:
            self._candles[key] = deque(maxlen=MAX_CANDLES_MEMORY)

        for row in reversed(rows):  # oldest first
            self._candles[key].append(
                {
                    "ts": row["ts"],
                    "o": row["o"],
                    "h": row["h"],
                    "l": row["l"],
                    "c": row["c"],
                    "v": row["v"],
                }
            )
        logger.debug("Loaded %d %s/%s candles from DB", len(rows), symbol, interval)

    # ---------------------------------------------------------------------- #
    # Tickers
    # ---------------------------------------------------------------------- #

    async def update_ticker(self, symbol: str, ticker: dict) -> None:
        """Update latest ticker in memory and persist a snapshot to SQLite."""
        async with self._lock:
            self._tickers[symbol] = ticker
            try:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO tickers (symbol, ts, price, volume_24h, change_pct)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        symbol,
                        ticker.get("ts", int(time.time() * 1000)),
                        ticker.get("price", 0.0),
                        ticker.get("volume_24h", 0.0),
                        ticker.get("change_pct", 0.0),
                    ),
                )
                self._conn.commit()
            except Exception as exc:
                logger.error("SQLite ticker upsert error: %s", exc)

        self._broadcast("ticker", {"symbol": symbol, "ticker": ticker})

    def get_ticker(self, symbol: str) -> Optional[dict]:
        return self._tickers.get(symbol)

    def get_all_tickers(self) -> Dict[str, dict]:
        return dict(self._tickers)

    # ---------------------------------------------------------------------- #
    # Funding rates
    # ---------------------------------------------------------------------- #

    async def update_funding(self, symbol: str, rate: float) -> None:
        async with self._lock:
            self._funding[symbol] = rate
        self._broadcast("funding", {"symbol": symbol, "rate": rate})

    def get_funding(self, symbol: str) -> Optional[float]:
        return self._funding.get(symbol)

    def get_all_funding(self) -> Dict[str, float]:
        return dict(self._funding)

    # ---------------------------------------------------------------------- #
    # Open Interest
    # ---------------------------------------------------------------------- #

    async def update_open_interest(self, symbol: str, oi: float) -> None:
        async with self._lock:
            self._open_interest[symbol] = oi
        self._broadcast("open_interest", {"symbol": symbol, "oi": oi})

    def get_open_interest(self, symbol: str) -> Optional[float]:
        return self._open_interest.get(symbol)

    # ---------------------------------------------------------------------- #
    # Regime
    # ---------------------------------------------------------------------- #

    async def update_regime(self, regime_data: dict) -> None:
        """Persist regime record to SQLite and broadcast."""
        async with self._lock:
            self._regime = regime_data
            try:
                self._conn.execute(
                    """
                    INSERT INTO regimes
                        (ts, regime, btc_ema50, btc_price, btc_atr, btc_atr_pct,
                         btc_ret_24h, btc_ret_1h, funding, oi_change,
                         bb_bw, btc_dom_chg, alt_vol_ratio, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        regime_data.get("ts", int(time.time() * 1000)),
                        regime_data.get("regime", "UNKNOWN"),
                        regime_data.get("btc_ema50"),
                        regime_data.get("btc_price"),
                        regime_data.get("btc_atr"),
                        regime_data.get("btc_atr_pct"),
                        regime_data.get("btc_ret_24h"),
                        regime_data.get("btc_ret_1h"),
                        regime_data.get("funding"),
                        regime_data.get("oi_change"),
                        regime_data.get("bb_bw"),
                        regime_data.get("btc_dom_chg"),
                        regime_data.get("alt_vol_ratio"),
                        json.dumps(regime_data),
                    ),
                )
                self._conn.commit()
            except Exception as exc:
                logger.error("SQLite regime insert error: %s", exc)

        self._broadcast("regime", regime_data)

    def get_regime(self) -> Optional[dict]:
        return self._regime

    # ---------------------------------------------------------------------- #
    # Full state snapshot (for dashboard initial load)
    # ---------------------------------------------------------------------- #

    def get_dashboard_snapshot(self) -> dict:
        """Return a complete snapshot of current state for dashboard init."""
        return {
            "system_mode":      self._system_mode,
            "exchange_ok":      self._exchange_ok,
            "daily_pnl":        self._daily_pnl,
            "daily_pnl_pct":    getattr(self, "_daily_pnl_pct", 0.0),
            "exposure_pct":     self._exposure_pct,
            "regime":           self._regime,
            "tickers":          self.get_all_tickers(),
            "funding":          self.get_all_funding(),
            "account_balance":  self.get_account_balance(),
            "kill_switch":      self.get_kill_switch_status(),
            "last_reconcile":   self.get_last_reconcile(),
        }

    # ---------------------------------------------------------------------- #
    # Signals (Phase 2)
    # ---------------------------------------------------------------------- #

    def save_signal(self, signal: dict) -> None:
        """Persist a signal dict to the signals table."""
        try:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO signals
                    (ts, strategy, symbol, action, mode, confidence, regime, tp, sl, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.get("ts"),
                    signal.get("strategy"),
                    signal.get("symbol"),
                    signal.get("action"),
                    signal.get("mode"),
                    signal.get("confidence"),
                    signal.get("regime"),
                    signal.get("tp"),
                    signal.get("sl"),
                    signal.get("reason"),
                ),
            )
            self._conn.commit()
        except Exception as exc:
            logger.error("SQLite signal insert error: %s", exc)

    def get_signals(self, limit: int = 50) -> List[dict]:
        """Return the most recent signals from the database (newest first)."""
        try:
            rows = self._conn.execute(
                """
                SELECT ts, strategy, symbol, action, mode, confidence, regime, tp, sl, reason
                FROM signals
                ORDER BY ts DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.error("SQLite signal fetch error: %s", exc)
            return []

    def get_strategy_stats(self) -> dict:
        """
        Compute per-strategy stats from closed paper positions.

        Returns a dict keyed by strategy name with keys:
          win_rate, profit_factor, trade_count, mdd, expectancy, open_count
        """
        try:
            rows = self._conn.execute(
                """
                SELECT strategy, pnl_pct, status
                FROM paper_positions
                WHERE status IN ('CLOSED', 'OPEN')
                """,
            ).fetchall()
        except Exception as exc:
            logger.error("SQLite strategy stats fetch error: %s", exc)
            return {}

        # Group by strategy
        from collections import defaultdict
        groups: dict = defaultdict(lambda: {"closed": [], "open": 0})
        for row in rows:
            name = row["strategy"]
            if row["status"] == "CLOSED":
                pnl = row["pnl_pct"]
                if pnl is not None:
                    groups[name]["closed"].append(float(pnl))
            else:
                groups[name]["open"] += 1

        result = {}
        for name, data in groups.items():
            closed = data["closed"]
            n = len(closed)
            wins   = [p for p in closed if p > 0]
            losses = [p for p in closed if p <= 0]

            win_rate = len(wins) / n if n > 0 else 0.0
            avg_win  = sum(wins)   / len(wins)   if wins   else 0.0
            avg_loss = abs(sum(losses) / len(losses)) if losses else 1.0
            profit_factor = (avg_win * len(wins)) / max(abs(avg_loss * len(losses)), 1e-9) if losses else float("inf")

            # MDD: maximum drawdown on equity curve (cumulative pnl)
            mdd = 0.0
            if closed:
                eq = 0.0
                peak = 0.0
                for p in closed:
                    eq += p
                    if eq > peak:
                        peak = eq
                    dd = (eq - peak)
                    if dd < mdd:
                        mdd = dd

            expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss) if n > 0 else 0.0

            result[name] = {
                "win_rate":      round(win_rate, 4),
                "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else None,
                "trade_count":   n,
                "mdd":           round(mdd, 4),
                "expectancy":    round(expectancy, 4),
                "open_count":    data["open"],
            }

        return result

    # ---------------------------------------------------------------------- #
    # Paper positions (Phase 2)
    # ---------------------------------------------------------------------- #

    def save_paper_position(self, pos: dict) -> None:
        """Insert a new paper position record."""
        try:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO paper_positions
                    (id, strategy, symbol, side, entry_price, qty,
                     tp, sl, opened_at, regime, signal_id, status,
                     closed_at, exit_price, pnl_pct, close_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pos.get("id"),
                    pos.get("strategy"),
                    pos.get("symbol"),
                    pos.get("side"),
                    pos.get("entry_price"),
                    pos.get("qty"),
                    pos.get("tp"),
                    pos.get("sl"),
                    pos.get("opened_at"),
                    pos.get("regime"),
                    pos.get("signal_id"),
                    pos.get("status", "OPEN"),
                    pos.get("closed_at"),
                    pos.get("exit_price"),
                    pos.get("pnl_pct"),
                    pos.get("close_reason"),
                ),
            )
            self._conn.commit()
        except Exception as exc:
            logger.error("SQLite paper position insert error: %s", exc)

    def update_paper_position(self, position_id: str, updates: dict) -> None:
        """Update fields on an existing paper position by its UUID id."""
        if not updates:
            return
        # Build dynamic SET clause from provided keys
        allowed_keys = {
            "status", "closed_at", "exit_price", "pnl_pct", "close_reason"
        }
        fields = {k: v for k, v in updates.items() if k in allowed_keys}
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [position_id]
        try:
            self._conn.execute(
                f"UPDATE paper_positions SET {set_clause} WHERE id = ?",
                values,
            )
            self._conn.commit()
        except Exception as exc:
            logger.error("SQLite paper position update error: %s", exc)

    def get_open_paper_positions(self) -> List[dict]:
        """Return all open paper positions."""
        try:
            rows = self._conn.execute(
                """
                SELECT * FROM paper_positions WHERE status = 'OPEN'
                ORDER BY opened_at DESC
                """,
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.error("SQLite open paper positions fetch error: %s", exc)
            return []

    # ---------------------------------------------------------------------- #
    # Strategy state (Phase 2)
    # ---------------------------------------------------------------------- #

    # ---------------------------------------------------------------------- #
    # Phase 3 — Orders / Live Positions
    # ---------------------------------------------------------------------- #

    def save_order(self, order: dict) -> None:
        """Persist a new order record to the orders table."""
        # Memory write queue for DB failure resilience (Part 6.3)
        self._queue_db_write("save_order", order)
        try:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO orders
                    (id, signal_id, ts, symbol, side, type, qty, price,
                     status, filled_qty, filled_price, fee)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.get("id"),
                    order.get("signal_id"),
                    order.get("ts", int(time.time() * 1000)),
                    order.get("symbol"),
                    order.get("side"),
                    order.get("type"),
                    order.get("qty"),
                    order.get("price"),
                    order.get("status"),
                    order.get("filled_qty", 0),
                    order.get("filled_price"),
                    order.get("fee", 0),
                ),
            )
            self._conn.commit()
            self._dequeue_db_write()  # flush any queued writes
        except Exception as exc:
            logger.error("SQLite order insert error: %s", exc)
            self._handle_db_failure()

    def update_order(self, order_id: str, updates: dict) -> None:
        """Update fields on an existing order."""
        if not updates:
            return
        allowed_keys = {
            "status", "filled_qty", "filled_price", "fee",
            "closed_at", "close_reason", "binance_order_id",
        }
        fields = {k: v for k, v in updates.items() if k in allowed_keys}
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [order_id]
        try:
            self._conn.execute(
                f"UPDATE orders SET {set_clause} WHERE id = ?",
                values,
            )
            self._conn.commit()
        except Exception as exc:
            logger.error("SQLite order update error: %s", exc)

    def get_order(self, order_id: str) -> Optional[dict]:
        """Fetch a single order by ID."""
        try:
            row = self._conn.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            return dict(row) if row else None
        except Exception as exc:
            logger.error("SQLite get_order error: %s", exc)
            return None

    def get_open_live_positions(self) -> List[dict]:
        """Return all open live orders (status = MONITORING or FILLED or SL_ATTACHED or TP_ATTACHED)."""
        try:
            rows = self._conn.execute(
                """
                SELECT * FROM orders
                WHERE status IN ('MONITORING', 'FILLED', 'SL_ATTACHED', 'TP_ATTACHED',
                                 'PARTIALLY_FILLED', 'OPEN')
                ORDER BY ts DESC
                """,
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.error("SQLite get_open_live_positions error: %s", exc)
            return []

    def save_audit_trail(self, record: dict) -> None:
        """INSERT only — audit trail is immutable (no UPDATE)."""
        import json as _json
        try:
            self._conn.execute(
                """
                INSERT INTO audit_trail
                    (order_id, signal_id, strategy, regime_snapshot,
                     risk_check, decision_reason, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.get("order_id"),
                    record.get("signal_id"),
                    record.get("strategy"),
                    _json.dumps(record.get("regime_snapshot")) if record.get("regime_snapshot") else None,
                    _json.dumps({
                        "from_status":      record.get("from_status"),
                        "to_status":        record.get("to_status"),
                        "risk_check":       record.get("risk_check_result"),
                        "order_params":     record.get("order_params"),
                        "execution_result": record.get("execution_result"),
                    }),
                    record.get("decision_reason"),
                    record.get("ts", int(time.time() * 1000)),
                ),
            )
            self._conn.commit()
        except Exception as exc:
            logger.error("SQLite audit_trail insert error: %s", exc)

    def get_audit_trail(self, order_id: str) -> List[dict]:
        """Return all audit trail entries for an order (chronological)."""
        try:
            rows = self._conn.execute(
                """
                SELECT * FROM audit_trail
                WHERE order_id = ?
                ORDER BY ts ASC
                """,
                (order_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.error("SQLite get_audit_trail error: %s", exc)
            return []

    def get_trade_log(
        self,
        limit: int = 50,
        mode: Optional[str] = None,
        strategy: Optional[str] = None,
        period: Optional[str] = None,
    ) -> List[dict]:
        """
        Return closed trades from paper_positions (+ future live orders).
        Supports filtering by mode (LIVE/PAPER), strategy, and period (today/7d/30d).
        """
        import time as _time
        conditions = ["status = 'CLOSED'"]
        params: List = []

        if strategy:
            conditions.append("strategy = ?")
            params.append(strategy)

        if period:
            now_ms = int(_time.time() * 1000)
            if period == "today":
                import datetime
                today_start = int(datetime.datetime.utcnow().replace(
                    hour=0, minute=0, second=0, microsecond=0
                ).timestamp() * 1000)
                conditions.append("opened_at >= ?")
                params.append(today_start)
            elif period == "7d":
                conditions.append("opened_at >= ?")
                params.append(now_ms - 7 * 86400 * 1000)
            elif period == "30d":
                conditions.append("opened_at >= ?")
                params.append(now_ms - 30 * 86400 * 1000)

        where = " AND ".join(conditions)
        params.append(limit)

        try:
            rows = self._conn.execute(
                f"""
                SELECT id, strategy, symbol, side, entry_price, exit_price,
                       pnl_pct, opened_at, closed_at, close_reason, regime,
                       signal_id, 'PAPER' AS mode
                FROM paper_positions
                WHERE {where}
                ORDER BY closed_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.error("SQLite get_trade_log error: %s", exc)
            return []

    # ---------------------------------------------------------------------- #
    # Phase 3 — Account balance
    # ---------------------------------------------------------------------- #

    def get_account_balance(self) -> float:
        """Return cached account balance (updated by Executor)."""
        return getattr(self, "_account_balance", 0.0)

    def set_account_balance(self, balance: float) -> None:
        """Cache account balance + update peak balance."""
        self._account_balance = balance
        current_peak = getattr(self, "_peak_balance", 0.0)
        if balance > current_peak:
            self._peak_balance = balance
        self._broadcast("account_balance", {"balance": balance})

    def get_peak_balance(self) -> Optional[float]:
        return getattr(self, "_peak_balance", None)

    def get_weekly_pnl(self) -> float:
        """Return weekly P&L in USDT (negative = loss)."""
        import time as _time
        import datetime
        week_start_ms = int(
            (datetime.datetime.utcnow() - datetime.timedelta(days=7))
            .timestamp() * 1000
        )
        try:
            row = self._conn.execute(
                """
                SELECT COALESCE(SUM(
                    (pnl_pct / 100.0) * entry_price * qty
                ), 0.0) AS weekly_pnl
                FROM paper_positions
                WHERE status = 'CLOSED'
                  AND closed_at >= ?
                """,
                (week_start_ms,),
            ).fetchone()
            return float(row["weekly_pnl"]) if row else 0.0
        except Exception as exc:
            logger.error("SQLite get_weekly_pnl error: %s", exc)
            return 0.0

    # ---------------------------------------------------------------------- #
    # Phase 3 — Reconcile status
    # ---------------------------------------------------------------------- #

    def set_last_reconcile(self, result: dict) -> None:
        """Cache the last reconcile result."""
        self._last_reconcile = result

    def get_last_reconcile(self) -> Optional[dict]:
        return getattr(self, "_last_reconcile", None)

    # ---------------------------------------------------------------------- #
    # Phase 3 — DB write failure queue (Part 6.3)
    # ---------------------------------------------------------------------- #

    def _queue_db_write(self, operation: str, data: dict) -> None:
        """Queue a write in memory if DB is failing."""
        # Only queue when DB failure mode is active
        if not getattr(self, "_db_failing", False):
            return
        queue = getattr(self, "_db_write_queue", None)
        if queue is None:
            from collections import deque
            self._db_write_queue: "deque" = deque(maxlen=100)
            queue = self._db_write_queue
        queue.append({"op": operation, "data": data, "ts": int(time.time() * 1000)})
        logger.debug("[DataStore] Queued DB write: %s", operation)

    def _dequeue_db_write(self) -> None:
        """Flush the write queue after DB recovery."""
        queue = getattr(self, "_db_write_queue", None)
        if not queue:
            self._db_failing = False
            return
        recovered = []
        while queue:
            item = queue.popleft()
            try:
                op = item["op"]
                data = item["data"]
                if op == "save_order":
                    self._conn.execute(
                        """
                        INSERT OR IGNORE INTO orders
                            (id, signal_id, ts, symbol, side, type, qty, price,
                             status, filled_qty, filled_price, fee)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            data.get("id"), data.get("signal_id"),
                            data.get("ts"), data.get("symbol"),
                            data.get("side"), data.get("type"),
                            data.get("qty"), data.get("price"),
                            data.get("status"), data.get("filled_qty", 0),
                            data.get("filled_price"), data.get("fee", 0),
                        ),
                    )
                recovered.append(item)
            except Exception as exc:
                logger.error("[DataStore] Failed to flush queued write: %s", exc)
                queue.appendleft(item)
                break

        if recovered:
            self._conn.commit()
            logger.info("[DataStore] Flushed %d queued DB writes.", len(recovered))

        self._db_failing = bool(queue)

    def _handle_db_failure(self) -> None:
        """Track DB failures; switch to OBSERVE mode after 3 min continuous failure."""
        if not hasattr(self, "_db_fail_start"):
            self._db_fail_start = time.time()
        self._db_failing = True

        elapsed = time.time() - self._db_fail_start
        if elapsed > 180:  # 3 minutes
            logger.critical(
                "[DataStore] DB failure for >3 min — switching to OBSERVE mode."
            )
            self.set_system_mode("OBSERVE")
            self._broadcast("db_failure", {"duration_sec": int(elapsed)})

    # ---------------------------------------------------------------------- #
    # Phase 3 — Kill switch status in snapshot
    # ---------------------------------------------------------------------- #

    def set_kill_switch_status(self, status: dict) -> None:
        self._kill_switch_status = status

    def get_kill_switch_status(self) -> dict:
        return getattr(self, "_kill_switch_status", {"active": False})

    # ---------------------------------------------------------------------- #
    # Strategy state (Phase 2)
    # ---------------------------------------------------------------------- #

    def get_strategy_state(self, name: str) -> Optional[dict]:
        """Return the strategy_state row for the given strategy name."""
        try:
            row = self._conn.execute(
                "SELECT * FROM strategy_state WHERE name = ?",
                (name,),
            ).fetchone()
            return dict(row) if row else None
        except Exception as exc:
            logger.error("SQLite strategy state fetch error: %s", exc)
            return None

    def upsert_strategy_state(self, record: dict) -> None:
        """Insert or update a row in strategy_state."""
        name = record.get("name")
        if not name:
            return
        existing = self.get_strategy_state(name)
        if existing is None:
            try:
                self._conn.execute(
                    """
                    INSERT INTO strategy_state
                        (name, mode, category, regime_filter, stats_json,
                         last_signal_ts, lifecycle_stage)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        record.get("mode", "PAPER"),
                        record.get("category", ""),
                        record.get("regime_filter", "[]"),
                        record.get("stats_json", "{}"),
                        record.get("last_signal_ts"),
                        record.get("lifecycle_stage", "paper"),
                    ),
                )
                self._conn.commit()
            except Exception as exc:
                logger.error("SQLite strategy state insert error: %s", exc)
        else:
            # Only update fields that are provided
            fields = {
                k: v for k, v in record.items()
                if k != "name" and v is not None
            }
            if fields:
                set_clause = ", ".join(f"{k} = ?" for k in fields)
                values = list(fields.values()) + [name]
                try:
                    self._conn.execute(
                        f"UPDATE strategy_state SET {set_clause} WHERE name = ?",
                        values,
                    )
                    self._conn.commit()
                except Exception as exc:
                    logger.error("SQLite strategy state update error: %s", exc)

    # ---------------------------------------------------------------------- #
    # v1.3 — Opportunities
    # ---------------------------------------------------------------------- #

    def save_opportunity(self, opp: dict) -> None:
        """Persist an Opportunity record."""
        import json as _json
        try:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO opportunities
                    (id, ts, symbol, side, source_strategy, category,
                     regime_snapshot, signal_strength, funding_state, oi_state,
                     volume_state, spread_state, volatility_state, liquidity_state,
                     expected_hold_window, invalidation_level, confidence_raw,
                     score_total, score_breakdown_json, rank_global,
                     rank_within_symbol, execution_status, approved_by, approved_at,
                     tp, sl, signal_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    opp.get("id"),
                    opp.get("ts"),
                    opp.get("symbol"),
                    opp.get("side"),
                    opp.get("source_strategy"),
                    opp.get("category"),
                    opp.get("regime_snapshot"),
                    opp.get("signal_strength"),
                    opp.get("funding_state"),
                    opp.get("oi_state"),
                    opp.get("volume_state"),
                    opp.get("spread_state"),
                    opp.get("volatility_state"),
                    opp.get("liquidity_state"),
                    opp.get("expected_hold_window"),
                    opp.get("invalidation_level"),
                    opp.get("confidence_raw"),
                    opp.get("score_total"),
                    _json.dumps(opp.get("score_breakdown", {})),
                    opp.get("rank_global"),
                    opp.get("rank_within_symbol"),
                    opp.get("execution_status", "PENDING"),
                    opp.get("approved_by"),
                    opp.get("approved_at"),
                    opp.get("tp"),
                    opp.get("sl"),
                    opp.get("signal_id"),
                ),
            )
            self._conn.commit()
        except Exception as exc:
            logger.error("SQLite opportunity insert error: %s", exc)

    def save_operator_action(self, action: dict) -> None:
        """Persist an operator action to operator_actions table."""
        import uuid as _uuid
        try:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO operator_actions
                    (id, ts, source, operator, action_type,
                     target_type, target_id, reason, result)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    action.get("id", str(_uuid.uuid4())),
                    action.get("ts", int(time.time() * 1000)),
                    action.get("source", "telegram"),
                    action.get("operator"),
                    action.get("action_type"),
                    action.get("target_type"),
                    action.get("target_id"),
                    action.get("reason"),
                    action.get("result"),
                ),
            )
            self._conn.commit()
        except Exception as exc:
            logger.error("SQLite operator_action insert error: %s", exc)

    def is_duplicate_action(
        self,
        action_type: str,
        target_id: str,
        window_ms: int = 5 * 60 * 1000,   # 5분
    ) -> bool:
        """
        동일 (action_type, target_id) 쌍이 window_ms 내에 이미 실행됐으면 True.
        idempotent 운영 액션 보장용.
        """
        try:
            since = int(time.time() * 1000) - window_ms
            row = self._conn.execute(
                """
                SELECT id FROM operator_actions
                WHERE action_type = ? AND target_id = ? AND ts >= ?
                LIMIT 1
                """,
                (action_type, target_id, since),
            ).fetchone()
            return row is not None
        except Exception as exc:
            logger.error("SQLite is_duplicate_action error: %s", exc)
            return False

    def get_recent_opportunities(self, limit: int = 10, status: str = "") -> list:
        """최근 Opportunity 조회."""
        try:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM opportunities WHERE execution_status=? ORDER BY ts DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM opportunities ORDER BY ts DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.error("SQLite opportunities fetch error: %s", exc)
            return []

    # ---------------------------------------------------------------------- #
    # Phase 4 — Reviews (daily + weekly)
    # ---------------------------------------------------------------------- #

    def save_review(self, review: dict) -> None:
        """Persist a review record (type='daily' or 'weekly') to the reviews table."""
        try:
            self._conn.execute(
                """
                INSERT INTO reviews (ts, type, content, recommendations)
                VALUES (?, ?, ?, ?)
                """,
                (
                    review.get("ts"),
                    review.get("type"),
                    review.get("content"),
                    review.get("recommendations", "[]"),
                ),
            )
            self._conn.commit()
        except Exception as exc:
            logger.error("SQLite review insert error: %s", exc)

    def get_reviews(self, limit: int = 10, type: Optional[str] = None) -> List[dict]:
        """Return the most recent review records, optionally filtered by type."""
        try:
            if type:
                rows = self._conn.execute(
                    """
                    SELECT * FROM reviews WHERE type = ?
                    ORDER BY ts DESC LIMIT ?
                    """,
                    (type, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM reviews ORDER BY ts DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.error("SQLite reviews fetch error: %s", exc)
            return []

    # ---------------------------------------------------------------------- #
    # Phase 4 — Recommendations
    # ---------------------------------------------------------------------- #

    def save_recommendation(self, rec: dict) -> None:
        """Insert a new recommendation record into the recommendations table."""
        try:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO recommendations
                    (id, ts, type, strategy, current_mode, proposed_mode,
                     supporting_data, counter_arguments, expected_risk,
                     validity_days, rollback_condition, status,
                     created_at, decided_at, decided_by, decision_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec.get("id"),
                    rec.get("created_at", int(time.time() * 1000)),
                    rec.get("type"),
                    rec.get("strategy"),
                    rec.get("current_mode"),
                    rec.get("proposed_mode"),
                    json.dumps(rec.get("supporting_data", {})),
                    json.dumps(rec.get("counter_arguments", [])),
                    json.dumps(rec.get("expected_risk", {})),
                    rec.get("validity_days", 7),
                    rec.get("rollback_condition"),
                    rec.get("status", "PENDING"),
                    rec.get("created_at", int(time.time() * 1000)),
                    rec.get("decided_at"),
                    rec.get("decided_by"),
                    rec.get("decision_reason"),
                ),
            )
            self._conn.commit()
        except Exception as exc:
            logger.error("SQLite recommendation insert error: %s", exc)

    def update_recommendation(
        self,
        rec_id: str,
        status: str,
        reason: str,
        decided_by: str,
    ) -> bool:
        """
        Update a recommendation's status after a decision (approve/reject/defer).
        Returns True on success.
        """
        try:
            self._conn.execute(
                """
                UPDATE recommendations
                SET status = ?, decision_reason = ?, decided_by = ?, decided_at = ?
                WHERE id = ?
                """,
                (status, reason, decided_by, int(time.time() * 1000), rec_id),
            )
            self._conn.commit()
            self._broadcast("recommendation_decided", {
                "id":         rec_id,
                "status":     status,
                "reason":     reason,
                "decided_by": decided_by,
            })
            return True
        except Exception as exc:
            logger.error("SQLite recommendation update error: %s", exc)
            return False

    def get_pending_recommendations(self) -> List[dict]:
        """Return all recommendations with status=PENDING."""
        try:
            rows = self._conn.execute(
                """
                SELECT * FROM recommendations
                WHERE status = 'PENDING'
                ORDER BY created_at DESC
                """,
            ).fetchall()
            return [self._deserialize_recommendation(dict(r)) for r in rows]
        except Exception as exc:
            logger.error("SQLite pending recommendations fetch error: %s", exc)
            return []

    def get_recommendation_history(self, limit: int = 20) -> List[dict]:
        """Return the most recent decided recommendations."""
        try:
            rows = self._conn.execute(
                """
                SELECT * FROM recommendations
                WHERE status != 'PENDING'
                ORDER BY decided_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._deserialize_recommendation(dict(r)) for r in rows]
        except Exception as exc:
            logger.error("SQLite recommendation history fetch error: %s", exc)
            return []

    def get_all_recommendations(self, limit: int = 50) -> List[dict]:
        """Return all recommendations (pending + decided) sorted by created_at desc."""
        try:
            rows = self._conn.execute(
                """
                SELECT * FROM recommendations
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._deserialize_recommendation(dict(r)) for r in rows]
        except Exception as exc:
            logger.error("SQLite all recommendations fetch error: %s", exc)
            return []

    @staticmethod
    def _deserialize_recommendation(row: dict) -> dict:
        """Parse JSON columns back to Python objects."""
        for col in ("supporting_data", "counter_arguments", "expected_risk"):
            raw = row.get(col)
            if isinstance(raw, str):
                try:
                    row[col] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    row[col] = {} if col != "counter_arguments" else []
        return row

    # ---------------------------------------------------------------------- #
    # Phase 4 — Daily alert badge
    # ---------------------------------------------------------------------- #

    def get_daily_alert_count(self) -> int:
        """Return the in-memory daily alert count."""
        return getattr(self, "_daily_alert_count", 0)

    def increment_daily_alert(self) -> None:
        """Increment the daily alert badge counter and broadcast."""
        count = getattr(self, "_daily_alert_count", 0) + 1
        self._daily_alert_count = count
        self._broadcast("daily_alert_count", {"count": count})

    def reset_daily_alert_count(self) -> None:
        """Reset the daily alert counter (call at midnight)."""
        self._daily_alert_count = 0
        self._broadcast("daily_alert_count", {"count": 0})

    # ---------------------------------------------------------------------- #
    # Phase 4 — Weekly stats aggregation
    # ---------------------------------------------------------------------- #

    def get_weekly_stats(self, days: int = 7) -> dict:
        """
        Compute per-strategy performance aggregates over the last `days` days.

        Returns dict: strategy_name -> {
            trade_count, win_count, win_rate, profit_factor,
            mdd, expectancy, mode
        }
        """
        since_ms = int((time.time() - days * 86400) * 1000)
        try:
            rows = self._conn.execute(
                """
                SELECT strategy, pnl_pct, status
                FROM paper_positions
                WHERE status = 'CLOSED' AND closed_at >= ?
                """,
                (since_ms,),
            ).fetchall()
        except Exception as exc:
            logger.error("SQLite weekly stats fetch error: %s", exc)
            return {}

        from collections import defaultdict
        groups: dict = defaultdict(list)
        for row in rows:
            pnl = row["pnl_pct"]
            if pnl is not None:
                groups[row["strategy"]].append(float(pnl))

        result = {}
        for name, closed in groups.items():
            n      = len(closed)
            wins   = [p for p in closed if p > 0]
            losses = [p for p in closed if p <= 0]

            win_rate     = len(wins) / n if n > 0 else 0.0
            avg_win      = sum(wins) / len(wins) if wins else 0.0
            avg_loss_abs = abs(sum(losses) / len(losses)) if losses else 1e-9
            pf = (
                (avg_win * len(wins)) / max(avg_loss_abs * len(losses), 1e-9)
                if losses else float("inf")
            )

            # MDD from equity curve
            mdd = 0.0
            if closed:
                eq = peak = 0.0
                for p in closed:
                    eq += p
                    if eq > peak:
                        peak = eq
                    dd = eq - peak
                    if dd < mdd:
                        mdd = dd

            expectancy = (
                (win_rate * avg_win) - ((1 - win_rate) * avg_loss_abs)
                if n > 0 else 0.0
            )

            # Current mode from strategy_state
            state = self.get_strategy_state(name)
            mode  = state.get("mode", "PAPER") if state else "PAPER"

            result[name] = {
                "trade_count":   n,
                "win_count":     len(wins),
                "win_rate":      round(win_rate, 4),
                "profit_factor": round(pf, 4) if pf != float("inf") else None,
                "mdd":           round(mdd, 4),
                "expectancy":    round(expectancy, 4),
                "mode":          mode,
            }

        return result

    def get_strategy_stats_since(self, since_ms: int) -> dict:
        """
        Compute per-strategy stats for trades closed after `since_ms`.

        Returns same shape as get_weekly_stats().
        """
        try:
            rows = self._conn.execute(
                """
                SELECT strategy, pnl_pct
                FROM paper_positions
                WHERE status = 'CLOSED' AND closed_at >= ?
                """,
                (since_ms,),
            ).fetchall()
        except Exception as exc:
            logger.error("SQLite strategy_stats_since fetch error: %s", exc)
            return {}

        from collections import defaultdict
        groups: dict = defaultdict(list)
        for row in rows:
            pnl = row["pnl_pct"]
            if pnl is not None:
                groups[row["strategy"]].append(float(pnl))

        result = {}
        for name, closed in groups.items():
            n      = len(closed)
            wins   = [p for p in closed if p > 0]
            losses = [p for p in closed if p <= 0]

            win_rate     = len(wins) / n if n > 0 else 0.0
            avg_win      = sum(wins) / len(wins) if wins else 0.0
            avg_loss_abs = abs(sum(losses) / len(losses)) if losses else 1e-9
            pf = (
                (avg_win * len(wins)) / max(avg_loss_abs * len(losses), 1e-9)
                if losses else float("inf")
            )
            expectancy = (
                (win_rate * avg_win) - ((1 - win_rate) * avg_loss_abs)
                if n > 0 else 0.0
            )

            result[name] = {
                "trade_count":   n,
                "win_count":     len(wins),
                "win_rate":      round(win_rate, 4),
                "profit_factor": round(pf, 4) if pf != float("inf") else None,
                "expectancy":    round(expectancy, 4),
                "mode":          "PAPER",
            }

        return result
