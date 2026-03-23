"""
22B Strategy Engine — Main async entry point.

Starts all components:
  Phase 1:
    1. Database init
    2. DataStore
    3. BinanceCollector (REST + WebSocket)
    4. RegimeDetector (polling loop)
    5. Dashboard (FastAPI + uvicorn in background thread)
    6. TelegramNotifier

  Phase 2 (additions):
    7. StrategyManager — runs 3 strategies in PAPER mode every cycle

  Phase 3 (additions):
    8. KillSwitch       — emergency stop
    9. OrderStateMachine — order lifecycle tracking
   10. RiskManager      — pre-trade risk checks
   11. Executor         — Binance Futures order execution
   12. Reconciler       — position reconciliation every 5 min

  Phase 4 (additions):
   13. ClaudeClient      — AI analysis (NEVER in execution path)
   14. RegimeInterpreter — explains regime changes via Claude
   15. DailyReviewer     — 22:00 UTC daily metrics review
   16. WeeklyReviewer    — Sunday 00:00 UTC AI-powered weekly review
"""

import asyncio
import logging
import signal
import sys
import time
import threading
from datetime import datetime, timezone
from typing import Optional

import uvicorn

from bot.config import get_config
from bot.data.collector import BinanceCollector
from bot.data.store import DataStore
from bot.data.validation_dataset_loader import ValidationDatasetLoader
from bot.data.validation_replay import ValidationReplaySession
from bot.notifications.telegram import TelegramNotifier
from bot.regime.detector import RegimeDetector
from bot.strategies.manager import StrategyManager
from bot.execution.kill_switch import KillSwitch
from bot.execution.state_machine import OrderStateMachine
from bot.execution.risk_manager import RiskManager
from bot.execution.executor import Executor
from bot.execution.reconciler import Reconciler
# Phase 4: AI analysis components (NOT in execution path)
# Conditional imports — Phase 4 modules may not exist yet in Phase 3 deployments
try:
    from bot.ai.claude_client import ClaudeClient
    from bot.ai.regime_interpreter import RegimeInterpreter
    from bot.ai.daily_reviewer import DailyReviewer
    from bot.ai.weekly_reviewer import WeeklyReviewer
    _PHASE4_AVAILABLE = True
except ImportError:
    ClaudeClient = None          # type: ignore[assignment,misc]
    RegimeInterpreter = None     # type: ignore[assignment,misc]
    DailyReviewer = None         # type: ignore[assignment,misc]
    WeeklyReviewer = None        # type: ignore[assignment,misc]
    _PHASE4_AVAILABLE = False

from db.schema import init_db

logger = logging.getLogger(__name__)

# How often to re-run regime detection AND strategy evaluation (seconds)
REGIME_DETECTION_INTERVAL = 60

# How often to refresh account balance from Binance (seconds)
BALANCE_REFRESH_INTERVAL = 60


def setup_logging(level: str) -> None:
    # Force UTF-8 on Windows (cp949 cannot encode many Unicode chars)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


class Engine:
    """Orchestrates all Phase 1 + Phase 2 + Phase 3 + Phase 4 components."""

    def __init__(self) -> None:
        self._config = get_config()
        self._store: Optional[DataStore] = None
        self._collector: Optional[BinanceCollector] = None
        self._detector: Optional[RegimeDetector] = None
        self._telegram: Optional[TelegramNotifier] = None
        self._strategy_manager: Optional[StrategyManager] = None

        # Phase 3 components
        self._kill_switch: Optional[KillSwitch] = None
        self._state_machine: Optional[OrderStateMachine] = None
        self._risk_manager: Optional[RiskManager] = None
        self._executor: Optional[Executor] = None
        self._reconciler: Optional[Reconciler] = None

        # Phase 4 AI components (NEVER in execution path)
        self._claude: Optional[ClaudeClient] = None
        self._regime_interpreter: Optional[RegimeInterpreter] = None
        self._daily_reviewer: Optional[DailyReviewer] = None
        self._weekly_reviewer: Optional[WeeklyReviewer] = None

        # Cloudflare Tunnel
        self._tunnel = None

        self._running = False
        self._shutdown_event = asyncio.Event()
        self._last_balance_refresh: float = 0.0
        self._start_time: float = time.time()

        # Phase 4: prevent duplicate review firings within the same minute
        self._last_daily_review_minute: Optional[str] = None
        self._last_weekly_review_minute: Optional[str] = None

    async def start(self) -> None:
        setup_logging(self._config.log_level)
        logger.info("=" * 60)
        logger.info("22B Strategy Engine — Phase 3 (+ Phase 4 if available)")
        logger.info("System mode: %s", self._config.system_mode)
        logger.info("AI enabled: %s", getattr(self._config, "ai_enabled", False))
        logger.info("Validation dataset mode: %s", self._config.validation_dataset_enabled)
        logger.info("Validation replay mode: %s", self._config.validation_replay_enabled)
        logger.info("=" * 60)

        # 1. Database
        conn = init_db(self._config.db_path)

        # 2. DataStore
        self._store = DataStore(conn)

        offline_validation_mode = self._config.validation_dataset_enabled
        effective_mode = self._config.system_mode
        if offline_validation_mode and effective_mode == "ACTIVE":
            logger.warning(
                "Validation dataset mode requested with SYSTEM_MODE=ACTIVE — forcing OBSERVE for safety."
            )
            effective_mode = "OBSERVE"
        self._store.set_system_mode(effective_mode)

        replay_session: Optional[ValidationReplaySession] = None
        if offline_validation_mode:
            loader = ValidationDatasetLoader(self._store, self._config.validation_dataset_root)
            warmup_bars = (
                self._config.validation_replay_warmup_bars
                if self._config.validation_replay_enabled
                else None
            )
            summary = await loader.load(warmup_bars=warmup_bars)
            logger.info(
                "Validation datasets loaded: files=%d candles=%d symbols=%d intervals=%d warmup=%d replay_remaining=%d",
                summary.files_loaded,
                summary.candles_loaded,
                summary.symbols_loaded,
                summary.intervals_loaded,
                summary.warmup_bars_loaded,
                summary.replay_bars_remaining,
            )
            if self._config.validation_replay_enabled:
                replay_session = ValidationReplaySession(
                    store=self._store,
                    datasets=loader.get_replay_datasets(),
                    warmup_bars=self._config.validation_replay_warmup_bars,
                    step_delay_ms=self._config.validation_replay_step_delay_ms,
                    max_steps=self._config.validation_replay_max_steps,
                )
            self._store.set_exchange_status(False)

        # 3. Telegram
        self._telegram = TelegramNotifier(self._config)
        await self._telegram.start()

        # 4. Collector
        if not offline_validation_mode:
            self._collector = BinanceCollector(self._config, self._store)
            await self._collector.start()
        else:
            logger.info("Offline validation dataset mode enabled — skipping live Binance collector startup.")

        # 5. Regime Detector
        self._detector = RegimeDetector(self._store)

        # 6. Strategy Manager (Phase 2)
        self._strategy_manager = StrategyManager(self._store)
        self._strategy_manager.initialize()
        logger.info(
            "StrategyManager initialized with %d strategies",
            len(self._strategy_manager.get_strategy_list()),
        )

        # 7. Phase 3: Kill Switch
        self._kill_switch = KillSwitch(self._store, self._telegram)
        logger.info("KillSwitch initialized.")

        # 8. Phase 3: Order State Machine
        self._state_machine = OrderStateMachine(self._store)
        self._state_machine.load_from_db()
        logger.info("OrderStateMachine initialized.")

        # 9. Phase 3: Risk Manager
        self._risk_manager = RiskManager(self._store)
        logger.info("RiskManager initialized.")

        # 10. Phase 3: Executor
        if not offline_validation_mode:
            self._executor = Executor(
                config=self._config,
                store=self._store,
                state_machine=self._state_machine,
                kill_switch=self._kill_switch,
            )
            await self._executor.start()
            self._kill_switch.set_executor(self._executor)
            logger.info("Executor initialized. Testnet=%s", self._config.binance_testnet)

            # 11. Phase 3: Reconciler
            self._reconciler = Reconciler(
                store=self._store,
                executor=self._executor,
                kill_switch=self._kill_switch,
                telegram=self._telegram,
            )
            self._reconciler.start()
            logger.info("Reconciler started (interval=5min).")
        else:
            logger.info("Offline validation dataset mode enabled — executor/reconciler remain disabled.")

        # 12. Telegram: start command handler
        asyncio.create_task(
            self._telegram_command_loop(), name="telegram_commands"
        )

        # 13. Phase 4: AI components (only if module is available)
        if _PHASE4_AVAILABLE and ClaudeClient is not None:
            ai_enabled = getattr(self._config, "ai_enabled", True)
            self._claude = ClaudeClient(
                base_url=getattr(self._config, "openclaw_base_url", "http://127.0.0.1:18789"),
                token=getattr(self._config, "openclaw_token", ""),
                agent_id=getattr(self._config, "openclaw_agent_id", "main"),
                ai_enabled=ai_enabled,
            )
            self._regime_interpreter = RegimeInterpreter(self._claude)
            self._daily_reviewer = DailyReviewer(self._store, self._telegram)
            self._weekly_reviewer = WeeklyReviewer(self._store, self._claude, self._telegram)
            logger.info(
                "Phase 4 AI components initialized (ai_enabled=%s, openclaw=%s agent=%s)",
                ai_enabled,
                getattr(self._config, "openclaw_base_url", "http://127.0.0.1:18789"),
                getattr(self._config, "openclaw_agent_id", "main"),
            )
            # 14. Review scheduler (Phase 4)
            asyncio.create_task(self._schedule_reviews(), name="review_scheduler")
        else:
            logger.info("Phase 4 AI components not available — running Phase 3 only.")

        # 15. Dashboard in a background thread
        self._start_dashboard_thread()

        # 16. Cloudflare Tunnel (optional — TUNNEL_ENABLED=true 시 활성)
        if getattr(self._config, "tunnel_enabled", False):
            await self._start_tunnel()

        # Notify started
        self._telegram.notify_system_started(self._store.get_system_mode())
        self._running = True

        # Register OS signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._handle_signal)
            except NotImplementedError:
                pass  # Windows does not support add_signal_handler for all signals

        # Initial balance fetch
        if not offline_validation_mode:
            await self._refresh_balance()
        else:
            logger.info("Offline validation dataset mode enabled — skipping initial live balance fetch.")

        logger.info(
            "Engine running. Dashboard: http://%s:%d",
            self._config.dashboard_host, self._config.dashboard_port,
        )

        # Main loop
        if replay_session is not None:
            await self._run_validation_replay(replay_session)
        else:
            await self._main_loop()

    def _handle_signal(self) -> None:
        logger.info("Shutdown signal received.")
        self._shutdown_event.set()

    async def _main_loop(self) -> None:
        """Run regime detection + strategy evaluation on a schedule until shutdown."""
        last_regime = "UNKNOWN"

        while not self._shutdown_event.is_set():
            try:
                last_regime = await self._run_engine_cycle(last_regime)

                # --- Phase 3: Periodic balance refresh ---
                now = time.time()
                if now - self._last_balance_refresh >= BALANCE_REFRESH_INTERVAL:
                    await self._refresh_balance()

            except Exception as exc:
                logger.error("Main loop error: %s", exc)

            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=REGIME_DETECTION_INTERVAL,
                )
            except asyncio.TimeoutError:
                pass  # normal — keep looping

        await self._shutdown()

    async def _run_validation_replay(self, replay_session: ValidationReplaySession) -> None:
        """Replay staged validation candles bar-by-bar for offline paper/regime evaluation."""
        last_regime = "UNKNOWN"
        logger.info(
            "Starting validation replay: total_steps=%d warmup=%d delay_ms=%d",
            replay_session.total_steps(),
            self._config.validation_replay_warmup_bars,
            self._config.validation_replay_step_delay_ms,
        )

        if replay_session.total_steps() == 0:
            logger.info("Validation replay has no remaining bars after warmup; falling back to static offline loop.")
            await self._main_loop()
            return

        while not self._shutdown_event.is_set():
            bar = await replay_session.next_bar()
            if bar is None:
                logger.info("Validation replay complete.")
                break

            logger.info(
                "[ValidationReplay] Step %d/%d %s %s ts=%d close=%.8f",
                bar.step_index,
                bar.total_steps,
                bar.symbol,
                bar.interval,
                bar.candle["ts"],
                bar.candle["c"],
            )
            last_regime = await self._run_engine_cycle(last_regime)

        await self._shutdown()

    async def _run_engine_cycle(self, last_regime: str) -> str:
        result = self._detector.detect()
        new_regime = result["regime"]

        if new_regime != last_regime:
            logger.info("Regime change: %s → %s", last_regime, new_regime)
            self._telegram.notify_regime_change(last_regime, new_regime, result)
            last_regime = new_regime

            if self._regime_interpreter is not None:
                asyncio.create_task(
                    self._interpret_regime(result),
                    name="regime_interpret",
                )

        await self._store.update_regime(result)

        if self._strategy_manager is not None:
            try:
                signals = self._strategy_manager.run_all(result)
                if signals:
                    actionable = [s for s in signals if s.action != "SKIP"]
                    logger.info(
                        "Strategy cycle complete: %d total, %d actionable",
                        len(signals), len(actionable),
                    )

                    if (self._store.get_system_mode() == "ACTIVE"
                            and not self._kill_switch.is_active):
                        await self._execute_live_signals(actionable, result)

            except Exception as exc:
                logger.error("Strategy manager run_all error: %s", exc)

        return last_regime

    async def _execute_live_signals(self, signals, regime: dict) -> None:
        """
        Execute actionable LIVE signals through the full risk + execution pipeline.
        Only called when system_mode == ACTIVE and kill switch is NOT active.
        """
        balance = self._store.get_account_balance()
        if balance <= 0:
            logger.warning("[Engine] Account balance=0 — cannot execute live signals.")
            return

        for signal in signals:
            # Skip non-BUY/SELL signals
            if signal.action not in ("BUY", "SELL"):
                continue

            # Only execute LIVE-mode signals
            if signal.mode != "LIVE":
                continue

            try:
                # Risk check
                risk_result = self._risk_manager.check(signal, balance)

                if not risk_result.passed:
                    logger.info(
                        "[Engine] Risk check FAILED for %s %s: %s",
                        signal.action, signal.symbol, risk_result.reason,
                    )
                    # Transition to RISK_CHECKED then REJECTED
                    # (state machine create + transition handled in submit_order)
                    continue

                logger.info(
                    "[Engine] Risk check PASSED: %s %s size=%.6f",
                    signal.action, signal.symbol, risk_result.position_size,
                )

                # Submit order
                order_result = await self._executor.submit_order(
                    signal=signal,
                    qty=risk_result.position_size,
                )

                if "error" not in order_result:
                    logger.info(
                        "[Engine] Order submitted: %s status=%s",
                        order_result.get("internal_order_id"),
                        order_result.get("status"),
                    )

            except Exception as exc:
                logger.error(
                    "[Engine] Error executing signal %s %s: %s",
                    signal.action, signal.symbol, exc,
                )

    async def _refresh_balance(self) -> None:
        """Fetch and cache account balance from Binance."""
        if self._executor is None:
            return
        try:
            balance = await self._executor.get_account_balance()
            if balance > 0:
                self._store.set_account_balance(balance)
                logger.debug("[Engine] Account balance updated: %.2f USDT", balance)
            self._last_balance_refresh = time.time()
        except Exception as exc:
            logger.warning("[Engine] Balance refresh error: %s", exc)

    async def _telegram_command_loop(self) -> None:
        """
        Poll for Telegram updates and handle /kill and /status commands.
        Runs as a background asyncio task.
        """
        import httpx
        if not self._telegram._enabled:
            return

        offset = 0
        base = f"https://api.telegram.org/bot{self._telegram._token}"

        async with httpx.AsyncClient(timeout=35.0) as client:
            while not self._shutdown_event.is_set():
                try:
                    resp = await client.get(
                        f"{base}/getUpdates",
                        params={"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
                    )
                    if resp.status_code == 404:
                        logger.warning("[Telegram] Invalid token (404) — command loop disabled.")
                        return
                    if resp.status_code == 200:
                        data = resp.json()
                        for update in data.get("result", []):
                            offset = update["update_id"] + 1
                            msg = update.get("message", {})
                            text = (msg.get("text") or "").strip().lower()
                            chat_id = msg.get("chat", {}).get("id")

                            if text == "/kill":
                                logger.warning(
                                    "[Telegram] /kill command received from chat_id=%s", chat_id
                                )
                                await self._kill_switch.trigger(
                                    reason="Manual /kill via Telegram",
                                    triggered_by=f"telegram:{chat_id}",
                                )
                                await self._telegram.send_message(
                                    "*Kill Switch Activated*\nAll new entries BLOCKED.\n"
                                    "Existing positions protected by SL/TP."
                                )

                            elif text == "/status":
                                ks = self._kill_switch.get_status()
                                rec = self._reconciler.get_status() if self._reconciler else {}
                                balance = self._store.get_account_balance()
                                mode = self._store.get_system_mode()
                                status_msg = (
                                    f"*22B Engine Status*\n"
                                    f"Mode: `{mode}`\n"
                                    f"Balance: `{balance:.2f} USDT`\n"
                                    f"Kill Switch: `{'ACTIVE' if ks.get('active') else 'OFF'}`\n"
                                    f"Last Reconcile: `{rec.get('age_sec', 'N/A')}s ago`\n"
                                    f"Reconcile errors: `{len(rec.get('errors', []))}`"
                                )
                                await self._telegram.send_message(status_msg)

                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.debug("[Telegram] Command loop error: %s", exc)
                    await asyncio.sleep(5)

    async def _start_tunnel(self) -> None:
        """Cloudflare Quick Tunnel을 시작하고 URL을 텔레그램으로 알림."""
        from bot.tunnel import CloudflareTunnel

        async def _on_url_ready(url: str) -> None:
            logger.info("[Tunnel] Dashboard URL: %s", url)
            msg = (
                f"*📱 모바일 대시보드 접속 URL*\n"
                f"`{url}`\n\n"
                f"이 URL로 어디서든 대시보드에 접속할 수 있습니다.\n"
                f"_(재시작 시 URL이 변경됩니다)_"
            )
            await self._telegram.send_message(msg)

        exe = getattr(self._config, "tunnel_cloudflared_path", "./cloudflared.exe")
        port = self._config.dashboard_port

        self._tunnel = CloudflareTunnel(
            cloudflared_path=exe,
            local_port=port,
            on_url_ready=_on_url_ready,
        )
        self._tunnel.start()
        logger.info("[Tunnel] Cloudflare tunnel starting (port=%d)…", port)

    async def _shutdown(self) -> None:
        logger.info("Shutting down Engine …")
        if self._tunnel:
            await self._tunnel.stop()
        if self._reconciler:
            self._reconciler.stop()
        if self._executor:
            await self._executor.stop()
        if self._collector:
            await self._collector.stop()
        if self._telegram:
            self._telegram.notify_system_stopped()
            await asyncio.sleep(1)
            await self._telegram.stop()
        logger.info("Engine stopped.")

    async def _interpret_regime(self, regime_result: dict) -> None:
        """Run regime interpretation in background; broadcasts result to dashboard."""
        try:
            interp = await self._regime_interpreter.interpret(regime_result)
            self._store._broadcast("regime_interpretation", interp.to_dict())
            logger.info(
                "Regime interpretation done: %s (ai_available=%s)",
                interp.regime, interp.ai_available,
            )
        except Exception as exc:
            logger.error("Regime interpretation failed: %s", exc)

    async def _schedule_reviews(self) -> None:
        """
        Background scheduler: checks every minute whether a review should run.

        Daily review:  every day at config.daily_review_hour:00 UTC
        Weekly review: every config.weekly_review_day at config.weekly_review_hour:00 UTC
        """
        while not self._shutdown_event.is_set():
            try:
                now = datetime.now(timezone.utc)
                minute_key_daily  = f"{now.hour:02d}:{now.minute:02d}"
                minute_key_weekly = f"{now.weekday()}-{now.hour:02d}:{now.minute:02d}"

                # Daily review
                daily_hour = getattr(self._config, "daily_review_hour", 22)
                if (
                    now.hour == daily_hour
                    and now.minute == 0
                    and self._last_daily_review_minute != minute_key_daily
                ):
                    self._last_daily_review_minute = minute_key_daily
                    logger.info("[Scheduler] Triggering daily review at %s UTC", minute_key_daily)
                    asyncio.create_task(self._run_daily_review(), name="daily_review")

                # Weekly review
                weekly_day  = getattr(self._config, "weekly_review_day", 6)
                weekly_hour = getattr(self._config, "weekly_review_hour", 0)
                if (
                    now.weekday() == weekly_day
                    and now.hour == weekly_hour
                    and now.minute == 0
                    and self._last_weekly_review_minute != minute_key_weekly
                ):
                    self._last_weekly_review_minute = minute_key_weekly
                    logger.info("[Scheduler] Triggering weekly review at %s UTC", minute_key_weekly)
                    asyncio.create_task(self._run_weekly_review(), name="weekly_review")

            except Exception as exc:
                logger.error("[Scheduler] Error: %s", exc)

            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    async def _run_daily_review(self) -> None:
        if self._daily_reviewer is None:
            return
        try:
            report = await self._daily_reviewer.run()
            logger.info("[Engine] Daily review: %d warnings", report.alert_count)
        except Exception as exc:
            logger.error("[Engine] Daily review failed: %s", exc)

    async def _run_weekly_review(self) -> None:
        if self._weekly_reviewer is None:
            return
        try:
            report = await self._weekly_reviewer.run()
            logger.info("[Engine] Weekly review: %d recommendations", len(report.recommendations))
        except Exception as exc:
            logger.error("[Engine] Weekly review failed: %s", exc)

    def _start_dashboard_thread(self) -> None:
        """Run FastAPI/uvicorn in a daemon thread so it doesn't block the event loop."""
        from dashboard.app import create_app

        app = create_app(
            store=self._store,
            config=self._config,
            strategy_manager=self._strategy_manager,
            executor=self._executor,
            kill_switch=self._kill_switch,
            reconciler=self._reconciler,
            regime_interpreter=self._regime_interpreter,
            daily_reviewer=self._daily_reviewer,
            weekly_reviewer=self._weekly_reviewer,
            engine=self,
        )

        config = uvicorn.Config(
            app,
            host=self._config.dashboard_host,
            port=self._config.dashboard_port,
            log_level=self._config.log_level.lower(),
            access_log=False,
        )
        server = uvicorn.Server(config)

        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(server.serve())

        thread = threading.Thread(target=_run, daemon=True, name="dashboard")
        thread.start()
        logger.info(
            "Dashboard thread started on http://%s:%d",
            self._config.dashboard_host,
            self._config.dashboard_port,
        )


def main() -> None:
    engine = Engine()
    try:
        asyncio.run(engine.start())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — exiting.")


if __name__ == "__main__":
    main()
