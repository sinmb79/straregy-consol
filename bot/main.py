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
from bot.data.replay_account import ReplayAccount
from bot.notifications.telegram import TelegramNotifier
from bot.regime.detector import RegimeDetector
from bot.regime.fast_layer import FastLayer
from bot.strategies.manager import StrategyManager
from bot.strategies.approval_manager import ApprovalManager
from bot.strategies.params_store import StrategyParamsStore
from bot.strategies.strategy_recommender import StrategyRecommender
from bot.strategies.validation_tracker import ValidationTracker
from bot.execution.kill_switch import KillSwitch
from bot.execution.state_machine import OrderStateMachine
from bot.execution.risk_manager import RiskManager
from bot.execution.portfolio_constraints import PortfolioConstraintEngine
from bot.execution.executor import Executor
from bot.execution.reconciler import Reconciler
# Phase 6: Hyperliquid DEX (optional)
try:
    from bot.data.hyperliquid_collector import HyperliquidCollector
    from bot.execution.hyperliquid_executor import HyperliquidExecutor
    _PHASE6_AVAILABLE = True
except ImportError:
    HyperliquidCollector = None   # type: ignore[assignment,misc]
    HyperliquidExecutor = None    # type: ignore[assignment,misc]
    _PHASE6_AVAILABLE = False
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
        self._fast_layer: Optional[FastLayer] = None
        self._approval_manager: Optional[ApprovalManager] = None
        self._telegram: Optional[TelegramNotifier] = None
        self._strategy_manager: Optional[StrategyManager] = None

        # Phase 3 components
        self._kill_switch: Optional[KillSwitch] = None
        self._state_machine: Optional[OrderStateMachine] = None
        self._risk_manager: Optional[RiskManager] = None
        self._portfolio_engine: Optional[PortfolioConstraintEngine] = None
        self._executor: Optional[Executor] = None
        self._reconciler: Optional[Reconciler] = None

        # Phase 6: Hyperliquid DEX components
        self._hl_collector: Optional[HyperliquidCollector] = None
        self._hl_executor: Optional[HyperliquidExecutor] = None

        # Phase 4 AI components (NEVER in execution path)
        self._claude: Optional[ClaudeClient] = None
        self._regime_interpreter: Optional[RegimeInterpreter] = None
        self._daily_reviewer: Optional[DailyReviewer] = None
        self._weekly_reviewer: Optional[WeeklyReviewer] = None

        # Strategy Recommender (사용자에게 전략 추천 제공)
        self._strategy_recommender: Optional[StrategyRecommender] = None

        # Validation Tracker (실전 진입 전 검증 데이터 축적)
        self._validation_tracker: Optional[ValidationTracker] = None

        # Cloudflare Tunnel
        self._tunnel = None

        # Replay simulation account (set when replay is active)
        self._replay_account: Optional[ReplayAccount] = None

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
                # Initialize virtual account for replay simulation
                self._replay_account = ReplayAccount(
                    initial_balance=self._config.replay_initial_balance,
                    position_size_pct=self._config.replay_position_size_pct,
                    fee_rate=self._config.replay_fee_rate,
                    slippage_pct=self._config.replay_slippage_pct,
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

        # 5. Regime Detector + Fast Layer
        self._detector = RegimeDetector(self._store)
        self._fast_layer = FastLayer(self._store)

        # 6. Strategy Manager (Phase 2) + Approval Manager
        # params_store 싱글턴 초기화 (data/ 디렉터리 기준)
        StrategyParamsStore.get_instance()
        self._strategy_manager = StrategyManager(self._store)
        self._strategy_manager.initialize()
        self._approval_manager = ApprovalManager(self._store, self._strategy_manager)
        # Health Engine에 ApprovalManager 주입
        if self._strategy_manager.health_engine is not None:
            self._strategy_manager.health_engine.set_approval_manager(self._approval_manager)

        # Validation Tracker 초기화 및 Health Engine 주입
        self._validation_tracker = ValidationTracker(self._store)
        if self._strategy_manager.health_engine is not None:
            self._strategy_manager.health_engine.set_validation_tracker(self._validation_tracker)
        logger.info("ValidationTracker initialized.")

        # Strategy Recommender 초기화
        self._strategy_recommender = StrategyRecommender(self._store, self._strategy_manager)
        logger.info(
            "StrategyManager initialized with %d strategies",
            len(self._strategy_manager.get_strategy_list()),
        )

        # Inject replay_account into PaperRecorder if replay is active
        if self._replay_account is not None:
            self._strategy_manager.recorder._replay_account = self._replay_account
            logger.info("ReplayAccount injected into PaperRecorder.")

        # 7. Phase 3: Kill Switch
        self._kill_switch = KillSwitch(self._store, self._telegram)
        logger.info("KillSwitch initialized.")

        # 8. Phase 3: Order State Machine
        self._state_machine = OrderStateMachine(self._store)
        self._state_machine.load_from_db()
        logger.info("OrderStateMachine initialized.")

        # 9. Phase 3: Risk Manager
        self._risk_manager = RiskManager(self._store)
        self._portfolio_engine = PortfolioConstraintEngine(self._store)
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
            logger.info("Executor initialized. Network=MAINNET")

            # 11. Phase 3: Reconciler
            self._reconciler = Reconciler(
                store=self._store,
                executor=self._executor,
                kill_switch=self._kill_switch,
                telegram=self._telegram,
            )
            self._reconciler.start()
            logger.info("Reconciler started (interval=5min).")

            # Phase 6: Hyperliquid DEX (HYPERLIQUID_ENABLED=true 시)
            if _PHASE6_AVAILABLE and self._config.hyperliquid_enabled:
                try:
                    self._hl_collector = HyperliquidCollector(self._config, self._store)
                    await self._hl_collector.start()

                    self._hl_executor = HyperliquidExecutor(
                        config=self._config,
                        store=self._store,
                        state_machine=self._state_machine,
                        kill_switch=self._kill_switch,
                    )
                    await self._hl_executor.start()
                    logger.info("Phase 6: Hyperliquid executor 초기화 완료.")
                except Exception as exc:
                    logger.error(
                        "Phase 6: Hyperliquid 초기화 실패 — 비활성화: %s", exc
                    )
                    self._hl_collector = None
                    self._hl_executor = None
            else:
                if not _PHASE6_AVAILABLE:
                    logger.info("Phase 6: hyperliquid-python-sdk 미설치 — 건너뜀.")
                elif not self._config.hyperliquid_enabled:
                    logger.info("Phase 6: HYPERLIQUID_ENABLED=false — 건너뜀.")
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
        self._loop = loop  # store for thread-safe shutdown from _graceful_restart
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

            # Inject candle timestamp into PaperRecorder for replay-accurate timing
            if self._strategy_manager is not None:
                self._strategy_manager.recorder.set_replay_ts(bar.candle["ts"])

            last_regime = await self._run_engine_cycle(last_regime)

        # Generate backtest report after replay finishes
        if self._replay_account is not None:
            try:
                from bot.ai.backtest_reporter import BacktestReporter
                reporter = BacktestReporter(
                    account=self._replay_account,
                    telegram=self._telegram,
                )
                label = f"replay_{replay_session.total_steps()}steps"
                report = reporter.generate(label=label)
                logger.info(
                    "[ValidationReplay] Backtest report: trades=%d  return=%.2f%%  mdd=%.2f%%",
                    report["metrics"].get("trade_count", 0),
                    report["metrics"].get("total_return_pct", 0.0),
                    report["metrics"].get("mdd_pct", 0.0),
                )
            except Exception as exc:
                logger.error("[ValidationReplay] Backtest report generation failed: %s", exc)

        await self._shutdown()

    async def _run_engine_cycle(self, last_regime: str) -> str:
        result = self._detector.detect()
        new_regime = result["regime"]

        # Fast Layer 병합 (단기 보조 신호)
        if self._fast_layer is not None:
            fast = self._fast_layer.compute("BTCUSDT")
            result["fast_layer"] = fast
            result["research_risk_checklist"] = RegimeDetector.build_research_risk_checklist(result)
            # CAUTION 이상이면 로그 경고
            if fast.get("alert_level") == "CAUTION":
                logger.warning(
                    "[FastLayer] CAUTION: %s", fast.get("signals", [])
                )

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

        # 전략 추천 주기 갱신 (30분 인터벌)
        if self._strategy_recommender is not None:
            try:
                self._strategy_recommender.maybe_generate(result)
            except Exception as exc:
                logger.debug("[StrategyRecommender] Error: %s", exc)

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
                # Portfolio constraint check (before risk check)
                if self._portfolio_engine is not None:
                    _live = self._store.get_open_live_positions()
                    # Normalize BUY/SELL → LONG/SHORT for constraint engine
                    _side_map = {"BUY": "LONG", "SELL": "SHORT"}
                    _sig_side = _side_map.get(signal.action.upper(), signal.action.upper())
                    _norm_pos = [
                        {**p, "side": _side_map.get(p.get("side", "").upper(), p.get("side", ""))}
                        for p in _live
                    ]
                    from collections import namedtuple as _nt
                    _OppProxy = _nt("_OppProxy", ["side", "symbol"])
                    _pce = self._portfolio_engine.check(_OppProxy(_sig_side, signal.symbol), _norm_pos)
                    if not _pce.passed:
                        logger.info(
                            "[Engine] Portfolio constraint FAILED for %s %s: %s",
                            signal.action, signal.symbol, _pce.reason,
                        )
                        continue

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

                # 사용자 거래소 선택 적용
                exchange_mode = self._store.get_exchange_mode()  # BOTH | BINANCE_ONLY | HYPERLIQUID_ONLY

                # Submit order — Binance
                if exchange_mode in ("BOTH", "BINANCE_ONLY"):
                    order_result = await self._executor.submit_order(
                        signal=signal,
                        qty=risk_result.position_size,
                    )
                    if "error" not in order_result:
                        logger.info(
                            "[Engine] Binance order submitted: %s status=%s",
                            order_result.get("internal_order_id"),
                            order_result.get("status"),
                        )
                        if self._portfolio_engine is not None:
                            self._portfolio_engine.record_execution()

                # Phase 6: Hyperliquid 병렬 실행 (사용자 선택 반영)
                if exchange_mode in ("BOTH", "HYPERLIQUID_ONLY") and self._hl_executor is not None:
                    try:
                        hl_result = await self._hl_executor.submit_order(
                            signal=signal,
                            qty=risk_result.position_size,
                        )
                        if "error" not in hl_result:
                            logger.info(
                                "[Engine] Hyperliquid order submitted: %s",
                                hl_result.get("internal_order_id"),
                            )
                        else:
                            logger.warning(
                                "[Engine] Hyperliquid order error: %s",
                                hl_result.get("error"),
                            )
                    except Exception as hl_exc:
                        logger.error(
                            "[Engine] Hyperliquid execution error: %s", hl_exc
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
            self._store.set_account_balance(balance)
            logger.debug("[Engine] Account balance updated: %.2f USDT", balance)
            self._last_balance_refresh = time.time()
        except Exception as exc:
            logger.warning("[Engine] Balance refresh error: %s", exc)

    async def _telegram_command_loop(self) -> None:
        """
        텔레그램 명령어 처리 루프.
        지원 명령어: /help /status /kill /reset /mode /balance /regime
                     /positions /strategies /url /pause /resume /restart
                     /validation
        """
        import httpx
        import os, subprocess, sys

        if not self._telegram._enabled:
            return

        offset = 0
        base = f"https://api.telegram.org/bot{self._telegram._token}"
        _last_connected_ts: int = int(time.time() * 1000)
        _consecutive_failures: int = 0
        _reconnect_notified: bool = False

        async def send_control_menu(chat_id_target: int) -> None:
            """인라인 키보드 컨트롤박스 전송."""
            mode = self._store.get_system_mode() if self._store else "OBSERVE"
            ks   = self._kill_switch.is_active if self._kill_switch else False
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "📊 상태",    "callback_data": "cmd:status"},
                        {"text": "💰 잔고",    "callback_data": "cmd:balance"},
                        {"text": "📂 포지션",  "callback_data": "cmd:positions"},
                    ],
                    [
                        {"text": "🌐 시장국면", "callback_data": "cmd:regime"},
                        {"text": "🧠 전략성과",  "callback_data": "cmd:strategies"},
                        {"text": "📱 URL",      "callback_data": "cmd:url"},
                    ],
                    [
                        {"text": "📋 기회목록",  "callback_data": "cmd:opportunities"},
                        {"text": "📊 일간리포트","callback_data": "cmd:report_daily"},
                        {"text": "📅 주간리포트","callback_data": "cmd:report_weekly"},
                    ],
                    [
                        {"text": f"{'🔴' if mode=='OBSERVE' else '⚪'} OBSERVE",  "callback_data": "mode:OBSERVE"},
                        {"text": f"{'🔴' if mode=='LIMITED' else '⚪'} LIMITED",  "callback_data": "mode:LIMITED"},
                        {"text": f"{'🔴' if mode=='ACTIVE'  else '⚪'} ACTIVE",   "callback_data": "mode:ACTIVE"},
                    ],
                    [
                        {"text": f"⛔ Soft Kill {'[ON]' if ks else ''}",    "callback_data": "cmd:kill"},
                        {"text": f"🚨 Hard Kill {'[ON]' if ks else ''}",    "callback_data": "cmd:kill_hard"},
                        {"text": "✅ Kill Reset",                            "callback_data": "cmd:reset"},
                    ],
                    [
                        {"text": "🔄 재시작",  "callback_data": "cmd:restart"},
                    ],
                ]
            }
            try:
                await client.post(f"{base}/sendMessage", json={
                    "chat_id":      chat_id_target,
                    "text":         f"*🎛 22B Control Panel*\n현재 모드: `{mode}` | 네트워크: `실전`\nKill Switch: `{'🔴 활성' if ks else '🟢 해제'}`",
                    "parse_mode":   "Markdown",
                    "reply_markup": keyboard,
                })
            except Exception as exc:
                logger.warning("[Telegram] send_control_menu error: %s", exc)

        async def answer_callback(callback_query_id: str, text: str = "") -> None:
            try:
                await client.post(f"{base}/answerCallbackQuery", json={
                    "callback_query_id": callback_query_id,
                    "text":              text,
                    "show_alert":        False,
                })
            except Exception:
                pass

        async with httpx.AsyncClient(timeout=35.0) as client:
            while not self._shutdown_event.is_set():
                try:
                    resp = await client.get(
                        f"{base}/getUpdates",
                        params={"offset": offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]},
                    )
                    if resp.status_code == 404:
                        logger.warning("[Telegram] Invalid token (404) — command loop disabled.")
                        return
                    if resp.status_code != 200:
                        _consecutive_failures += 1
                        if _consecutive_failures >= 3:
                            _reconnect_notified = True
                        await asyncio.sleep(5)
                        continue

                    # Reconnect recovery: send missed-events summary
                    if _reconnect_notified and _consecutive_failures > 0:
                        gap_sec = max(0, int(time.time()) - _last_connected_ts // 1000)
                        gap_min = gap_sec // 60
                        try:
                            # Gather events that occurred during the outage
                            recent_signals = self._store.get_signals(limit=5)
                            recent_opps    = self._store.get_recent_opportunities(limit=5)
                            ks_active      = self._kill_switch.is_active if self._kill_switch else False
                            mode           = self._store.get_system_mode()
                            lines = [
                                f"*🔄 Telegram 재연결됨*",
                                f"오프라인 시간: 약 {gap_min}분",
                                f"현재 모드: `{mode}` | Kill: `{'활성' if ks_active else '해제'}`",
                            ]
                            if recent_signals:
                                lines.append(f"\n*최근 신호 {len(recent_signals)}건:*")
                                for s in recent_signals[:3]:
                                    lines.append(f"  {s.get('action')} {s.get('symbol')} [{s.get('strategy')}]")
                            if recent_opps:
                                lines.append(f"\n*최근 기회 {len(recent_opps)}건:*")
                                for o in recent_opps[:3]:
                                    lines.append(f"  {o.get('side')} {o.get('symbol')} score={o.get('score_total')} [{o.get('execution_status')}]")
                            await self._telegram.send_message("\n".join(lines))
                        except Exception:
                            pass
                        _reconnect_notified = False

                    _consecutive_failures = 0
                    _last_connected_ts = int(time.time() * 1000)

                    data = resp.json()
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1

                        # ── callback_query (인라인 버튼 클릭) ────────────
                        cq = update.get("callback_query")
                        if cq:
                            cq_id   = cq["id"]
                            cq_data = cq.get("data", "")
                            cq_chat = cq.get("message", {}).get("chat", {}).get("id")

                            # ── chat_id allowlist ──────────────────────────
                            _allowed = str(self._config.telegram_chat_id).strip()
                            if _allowed and str(cq_chat) != _allowed:
                                await answer_callback(cq_id, "Unauthorized")
                                continue

                            if cq_data.startswith("mode:"):
                                new_mode = cq_data.split(":", 1)[1]
                                # 중복 액션 방지: 5분 내 동일 모드 전환 무시
                                if self._store.is_duplicate_action("mode_change", new_mode, 5 * 60 * 1000):
                                    await answer_callback(cq_id, "이미 실행된 액션입니다")
                                else:
                                    self._config.system_mode = new_mode
                                    self._store.set_system_mode(new_mode)
                                    self._store.save_operator_action({
                                        "source": "telegram", "operator": str(cq_chat),
                                        "action_type": "mode_change", "target_id": new_mode,
                                        "result": "ok",
                                    })
                                    logger.warning("[Telegram] Mode changed to %s via inline button", new_mode)
                                    await answer_callback(cq_id, f"모드 변경: {new_mode}")
                                    await self._telegram.send_message(f"✅ 운영 모드 변경: `{new_mode}`")
                                    await send_control_menu(cq_chat)

                            elif cq_data == "cmd:status":
                                await answer_callback(cq_id)
                                # status 처리 — 아래 /status 로직 재사용
                                ks      = self._kill_switch.get_status() if self._kill_switch else {}
                                rec     = self._reconciler.get_status() if self._reconciler else {}
                                balance = self._store.get_account_balance()
                                mode    = self._store.get_system_mode()
                                uptime  = int(time.time() - self._start_time)
                                h, m_u  = divmod(uptime // 60, 60)
                                regime  = (self._store.get_regime() or {}).get("regime", "UNKNOWN")
                                tunnel_url = self._tunnel.url if self._tunnel else None
                                await self._telegram.send_message(
                                    f"*⚙️ 22B Engine 상태*\n\n"
                                    f"운영 모드: `{mode}`\n"
                                    f"시장 국면: `{regime}`\n"
                                    f"잔고: `{balance:.2f} USDT`\n"
                                    f"Kill Switch: `{'🔴 활성' if ks.get('active') else '🟢 해제'}`\n"
                                    f"업타임: `{h}시간 {m_u}분`\n"
                                    + (f"대시보드: {tunnel_url}" if tunnel_url else "")
                                )

                            elif cq_data == "cmd:balance":
                                await answer_callback(cq_id)
                                balance  = self._store.get_account_balance()
                                dpnl, dp = self._store.get_daily_pnl()
                                wpnl     = self._store.get_weekly_pnl()
                                sign_d   = "+" if dpnl >= 0 else ""
                                sign_w   = "+" if wpnl >= 0 else ""
                                await self._telegram.send_message(
                                    f"*💰 잔고 현황* (실전)\n\n"
                                    f"잔고: `{balance:.2f} USDT`\n"
                                    f"오늘 손익: `{sign_d}{dpnl:.2f} USDT ({sign_d}{dp:.2f}%)`\n"
                                    f"이번 주 손익: `{sign_w}{wpnl:.2f} USDT`"
                                )

                            elif cq_data == "cmd:positions":
                                await answer_callback(cq_id)
                                paper = self._store.get_open_paper_positions()
                                live  = self._store.get_open_live_positions()
                                if not paper and not live:
                                    await self._telegram.send_message("📂 현재 열린 포지션이 없습니다.")
                                else:
                                    lines = ["*📂 열린 포지션*\n"]
                                    for p in live:
                                        lines.append(f"🔴 LIVE `{p.get('symbol')}` {p.get('side')} PnL:`{p.get('pnl_pct',0):.2f}%`")
                                    for p in paper:
                                        lines.append(f"📄 PAPER `{p.get('symbol')}` {p.get('side')} 전략:`{p.get('strategy','—')}`")
                                    await self._telegram.send_message("\n".join(lines))

                            elif cq_data == "cmd:regime":
                                await answer_callback(cq_id)
                                r = self._store.get_regime() or {}
                                await self._telegram.send_message(
                                    f"*🌐 시장 국면*\n\n"
                                    f"현재: `{r.get('regime','UNKNOWN')}`\n"
                                    f"활성 전략: `{', '.join(r.get('allowed_strategies',[])) or '없음'}`\n"
                                    f"BTC 가격: `{r.get('btc_price','—')}`\n"
                                    f"RSI(4H): `{r.get('btc_rsi','—')}`"
                                )

                            elif cq_data == "cmd:strategies":
                                await answer_callback(cq_id)
                                strategies = self._strategy_manager.get_strategy_list() if self._strategy_manager else []
                                stats      = self._store.get_strategy_stats()
                                lines = ["*🧠 전략 성과*\n"]
                                for s in strategies:
                                    name = s["name"]
                                    st   = stats.get(name, {})
                                    lines.append(f"`{name}` [{s.get('mode','—')}] 승률:{st.get('win_rate',0):.1f}% 거래:{st.get('trade_count',0)}회")
                                await self._telegram.send_message("\n".join(lines) if lines else "전략 없음")

                            elif cq_data == "cmd:url":
                                await answer_callback(cq_id)
                                url = self._tunnel.url if self._tunnel else None
                                await self._telegram.send_message(
                                    f"*📱 대시보드 URL*\n`{url}`" if url else "터널이 비활성화 상태입니다."
                                )

                            elif cq_data == "cmd:kill":
                                if self._store.is_duplicate_action("kill_soft", "system", 60 * 1000):
                                    await answer_callback(cq_id, "이미 처리 중입니다")
                                else:
                                    await answer_callback(cq_id, "Soft Kill 활성화")
                                    self._store.save_operator_action({
                                        "source": "telegram", "operator": str(cq_chat),
                                        "action_type": "kill_soft", "target_id": "system",
                                        "result": "triggered",
                                    })
                                    await self._kill_switch.trigger_soft(
                                        reason="Inline button /kill (SOFT) via Telegram",
                                        triggered_by=f"telegram:{cq_chat}",
                                    )
                                    await self._telegram.send_message(
                                        "⛔ *Soft Kill 활성화*\n신규 진입 차단. 기존 포지션 SL/TP 유지.\n해제: ✅ Kill Reset 버튼"
                                    )
                                    await send_control_menu(cq_chat)

                            elif cq_data == "cmd:kill_hard":
                                if self._store.is_duplicate_action("kill_hard", "system", 60 * 1000):
                                    await answer_callback(cq_id, "이미 처리 중입니다")
                                else:
                                    await answer_callback(cq_id, "Hard Kill 활성화")
                                    self._store.save_operator_action({
                                        "source": "telegram", "operator": str(cq_chat),
                                        "action_type": "kill_hard", "target_id": "system",
                                        "result": "triggered",
                                    })
                                    await self._kill_switch.trigger_hard(
                                        reason="Inline button /killhard via Telegram",
                                        triggered_by=f"telegram:{cq_chat}",
                                    )
                                    await self._telegram.send_message(
                                        "🚨 *Hard Kill 활성화*\n신규 진입 차단 + 미체결 주문 전체 취소.\n해제: ✅ Kill Reset 버튼"
                                    )
                                    await send_control_menu(cq_chat)

                            elif cq_data == "cmd:reset":
                                if self._kill_switch and self._kill_switch.is_active:
                                    self._kill_switch.reset(authorized_by=f"telegram:{cq_chat}")
                                    await answer_callback(cq_id, "Kill Switch 해제됨")
                                    await self._telegram.send_message("✅ *Kill Switch 해제됨*\n정상 운영으로 복귀합니다.")
                                    await send_control_menu(cq_chat)
                                else:
                                    await answer_callback(cq_id, "Kill Switch가 활성화되어 있지 않음")

                            elif cq_data == "cmd:restart":
                                await answer_callback(cq_id, "재시작 중...")
                                await self._telegram.send_message("🔄 *봇을 재시작합니다...*\n약 10초 후 다시 시작됩니다.")
                                base_dir = str(__import__("pathlib").Path(__file__).parent.parent)
                                def _do_restart_cb():
                                    time.sleep(2)
                                    _graceful_restart(base_dir)
                                __import__("threading").Thread(target=_do_restart_cb, daemon=True).start()

                            elif cq_data == "cmd:opportunities":
                                await answer_callback(cq_id)
                                opps = self._store.get_recent_opportunities(limit=5, status="PENDING") if self._store else []
                                if not opps:
                                    await self._telegram.send_message("📋 현재 실행 후보 기회가 없습니다.")
                                else:
                                    import json as _j
                                    lines = ["*📋 실행 후보*\n"]
                                    for o in opps:
                                        bkd = {}
                                        try:
                                            bkd = _j.loads(o.get("score_breakdown_json") or "{}")
                                        except Exception:
                                            pass
                                        bkd_str = ", ".join(f"{k}:{v:+d}" for k, v in list(bkd.items())[:3])
                                        lines.append(
                                            f"`{o['symbol']}` {o['side']} [{o['category']}] "
                                            f"스코어:`{o['score_total']}` ({bkd_str})"
                                        )
                                    await self._telegram.send_message("\n".join(lines))

                            elif cq_data == "cmd:report_daily":
                                await answer_callback(cq_id)
                                dpnl, dp = self._store.get_daily_pnl()
                                stats = self._store.get_strategy_stats()
                                total_trades = sum(s.get("trade_count", 0) for s in stats.values())
                                sign = "+" if dpnl >= 0 else ""
                                await self._telegram.send_message(
                                    f"*📊 오늘 요약*\n\n"
                                    f"손익: `{sign}{dpnl:.2f} USDT ({sign}{dp:.2f}%)`\n"
                                    f"거래 수: `{total_trades}`회\n"
                                    f"레짐: `{(self._store.get_regime() or {}).get('regime','UNKNOWN')}`"
                                )
                                # 전략 건강도 카드 추가
                                if self._strategy_manager is not None:
                                    he = self._strategy_manager.health_engine
                                    if he is not None:
                                        card = he.build_health_card("오늘")
                                        await self._telegram.send_message(card)

                            elif cq_data == "cmd:report_weekly":
                                await answer_callback(cq_id)
                                stats = self._store.get_strategy_stats()
                                lines = ["*📅 주간 전략 요약*\n"]
                                for name_s, st in stats.items():
                                    lines.append(
                                        f"`{name_s}`: {st.get('trade_count',0)}건 "
                                        f"승률{st.get('win_rate',0)*100:.0f}% "
                                        f"PF:{st.get('profit_factor','—')}"
                                    )
                                await self._telegram.send_message(
                                    "\n".join(lines) if len(lines) > 1 else "데이터 없음"
                                )
                                # 전략 건강도 카드 추가
                                if self._strategy_manager is not None:
                                    he = self._strategy_manager.health_engine
                                    if he is not None:
                                        card = he.build_health_card("주간")
                                        await self._telegram.send_message(card)

                            continue  # callback_query 처리 완료, message 처리 건너뜀

                        msg    = update.get("message", {})
                        raw    = (msg.get("text") or "").strip()
                        text   = raw.lower()
                        chat_id = msg.get("chat", {}).get("id")
                        parts  = raw.split()          # 원본 대소문자 유지

                        # ── chat_id allowlist ──────────────────────────────
                        _allowed = str(self._config.telegram_chat_id).strip()
                        if _allowed and str(chat_id) != _allowed:
                            logger.warning("[Telegram] Ignored unauthorized message from chat_id=%s", chat_id)
                            continue

                        # ── /start /menu /help ────────────────────────────
                        if text in ("/help", "/start", "/menu"):
                            await send_control_menu(chat_id)

                        # ── /kill ─────────────────────────────────────────
                        elif text == "/kill":
                            logger.warning("[Telegram] /kill (SOFT) from chat_id=%s", chat_id)
                            await self._kill_switch.trigger_soft(
                                reason="Manual /kill (SOFT) via Telegram",
                                triggered_by=f"telegram:{chat_id}",
                            )
                            await self._telegram.send_message(
                                "⛔ *Soft Kill 활성화*\n"
                                "신규 진입이 차단됐습니다.\n"
                                "기존 포지션은 SL/TP로 보호됩니다.\n"
                                "Hard Kill: `/killhard` | 해제: `/reset`"
                            )

                        # ── /killhard ─────────────────────────────────────
                        elif text == "/killhard":
                            logger.warning("[Telegram] /killhard from chat_id=%s", chat_id)
                            await self._kill_switch.trigger_hard(
                                reason="Manual /killhard via Telegram",
                                triggered_by=f"telegram:{chat_id}",
                            )
                            await self._telegram.send_message(
                                "🚨 *Hard Kill 활성화*\n"
                                "신규 진입 차단 + 미체결 주문 전체 취소.\n"
                                "해제하려면 `/reset` 을 입력하세요."
                            )

                        # ── /reset ────────────────────────────────────────
                        elif text == "/reset":
                            if self._kill_switch and self._kill_switch.is_active:
                                self._kill_switch.reset(authorized_by=f"telegram:{chat_id}")
                                await self._telegram.send_message(
                                    "✅ *Kill Switch 해제됨*\n"
                                    "정상 운영으로 복귀합니다."
                                )
                            else:
                                await self._telegram.send_message(
                                    "ℹ️ Kill Switch가 활성화되어 있지 않습니다."
                                )

                        # ── /status ───────────────────────────────────────
                        elif text == "/status":
                            ks      = self._kill_switch.get_status() if self._kill_switch else {}
                            rec     = self._reconciler.get_status() if self._reconciler else {}
                            balance = self._store.get_account_balance()
                            mode    = self._store.get_system_mode()
                            uptime  = int(time.time() - self._start_time)
                            h, m    = divmod(uptime // 60, 60)
                            regime  = (self._store.get_regime() or {}).get("regime", "UNKNOWN")
                            tunnel_url = self._tunnel.url if self._tunnel else None

                            await self._telegram.send_message(
                                f"*⚙️ 22B Engine 상태*\n\n"
                                f"운영 모드: `{mode}`\n"
                                f"시장 국면: `{regime}`\n"
                                f"잔고: `{balance:.2f} USDT`\n"
                                f"Kill Switch: `{'🔴 활성' if ks.get('active') else '🟢 해제'}`\n"
                                f"마지막 대조: `{rec.get('age_sec', 'N/A')}초 전`\n"
                                f"업타임: `{h}시간 {m}분`\n"
                                + (f"대시보드: {tunnel_url}" if tunnel_url else "")
                            )

                        # ── /balance ──────────────────────────────────────
                        elif text == "/balance":
                            balance  = self._store.get_account_balance()
                            dpnl, dp = self._store.get_daily_pnl()
                            wpnl     = self._store.get_weekly_pnl()
                            sign_d   = "+" if dpnl >= 0 else ""
                            sign_w   = "+" if wpnl >= 0 else ""
                            await self._telegram.send_message(
                                f"*💰 잔고 현황* (실전)\n\n"
                                f"잔고: `{balance:.2f} USDT`\n"
                                f"오늘 손익: `{sign_d}{dpnl:.2f} USDT ({sign_d}{dp:.2f}%)`\n"
                                f"이번 주 손익: `{sign_w}{wpnl:.2f} USDT`"
                            )

                        # ── /regime ───────────────────────────────────────
                        elif text == "/regime":
                            r = self._store.get_regime() or {}
                            regime    = r.get("regime", "UNKNOWN")
                            allowed   = ", ".join(r.get("allowed_strategies", [])) or "없음"
                            btc_price = r.get("btc_price", "—")
                            atr_pct   = r.get("btc_atr_pct", "—")
                            rsi       = r.get("btc_rsi", "—")
                            await self._telegram.send_message(
                                f"*🌐 시장 국면*\n\n"
                                f"현재: `{regime}`\n"
                                f"활성 전략: `{allowed}`\n"
                                f"BTC 가격: `{btc_price}`\n"
                                f"ATR%: `{atr_pct}`\n"
                                f"RSI(4H): `{rsi}`"
                            )

                        # ── /positions ────────────────────────────────────
                        elif text == "/positions":
                            paper = self._store.get_open_paper_positions()
                            live  = self._store.get_open_live_positions()
                            if not paper and not live:
                                await self._telegram.send_message("📂 현재 열린 포지션이 없습니다.")
                            else:
                                lines = ["*📂 열린 포지션*\n"]
                                for p in live:
                                    lines.append(
                                        f"🔴 LIVE `{p.get('symbol')}` {p.get('side')} "
                                        f"진입가:`{p.get('entry_price','—')}` "
                                        f"PnL:`{p.get('pnl_pct',0):.2f}%`"
                                    )
                                for p in paper:
                                    lines.append(
                                        f"📄 PAPER `{p.get('symbol')}` {p.get('side')} "
                                        f"전략:`{p.get('strategy','—')}`"
                                    )
                                await self._telegram.send_message("\n".join(lines))

                        # ── /strategies ───────────────────────────────────
                        elif text == "/strategies":
                            strategies = self._strategy_manager.get_strategy_list() if self._strategy_manager else []
                            stats      = self._store.get_strategy_stats()
                            if not strategies:
                                await self._telegram.send_message("전략 정보 없음")
                            else:
                                lines = ["*🧠 전략 성과*\n"]
                                for s in strategies:
                                    name = s["name"]
                                    st   = stats.get(name, {})
                                    mode = s.get("mode", "—")
                                    wr   = st.get("win_rate", 0)
                                    cnt  = st.get("trade_count", 0)
                                    lines.append(
                                        f"`{name}` [{mode}]\n"
                                        f"  승률: {wr:.1f}% | 거래: {cnt}회"
                                    )
                                await self._telegram.send_message("\n".join(lines))

                        # ── /url ──────────────────────────────────────────
                        elif text == "/url":
                            url = self._tunnel.url if self._tunnel else None
                            if url:
                                await self._telegram.send_message(
                                    f"*📱 대시보드 URL*\n`{url}`"
                                )
                            else:
                                await self._telegram.send_message(
                                    "터널이 비활성화 상태입니다.\n"
                                    "(.env 에서 TUNNEL\\_ENABLED=true 로 설정 후 재시작)"
                                )

                        # ── /mode [값] ────────────────────────────────────
                        elif text.startswith("/mode"):
                            valid = {"observe", "limited", "active", "blocked"}
                            if len(parts) == 1:
                                current = self._store.get_system_mode()
                                await self._telegram.send_message(
                                    f"현재 모드: `{current}`\n\n"
                                    "변경: `/mode observe|limited|active|blocked`\n"
                                    "• `observe` — 관찰만 (거래 없음)\n"
                                    "• `limited` — 페이퍼 트레이딩만\n"
                                    "• `active`  — 실전 매매 활성\n"
                                    "• `blocked` — 전체 차단"
                                )
                            elif len(parts) == 2 and parts[1].lower() in valid:
                                new_mode = parts[1].upper()
                                self._config.system_mode = new_mode
                                self._store.set_system_mode(new_mode)
                                logger.warning("[Telegram] Mode changed to %s by chat_id=%s", new_mode, chat_id)
                                await self._telegram.send_message(
                                    f"✅ 운영 모드 변경: `{new_mode}`"
                                )
                            else:
                                await self._telegram.send_message(
                                    "❌ 잘못된 모드입니다.\n`/mode observe|limited|active|blocked`"
                                )

                        # ── /pause [전략명] ───────────────────────────────
                        elif text.startswith("/pause"):
                            if self._strategy_manager is None:
                                await self._telegram.send_message("전략 관리자가 초기화되지 않았습니다.")
                            elif len(parts) < 2:
                                names = [s["name"] for s in self._strategy_manager.get_strategy_list()]
                                await self._telegram.send_message(
                                    f"사용법: `/pause [전략명]`\n전략 목록: `{'`, `'.join(names)}`"
                                )
                            else:
                                name = parts[1]
                                ok = self._strategy_manager.set_strategy_mode(name, "PAUSED")
                                if ok:
                                    await self._telegram.send_message(f"⏸ `{name}` 전략 일시정지됨")
                                else:
                                    await self._telegram.send_message(f"❌ 전략 `{name}` 을 찾을 수 없습니다.")

                        # ── /resume [전략명] ──────────────────────────────
                        elif text.startswith("/resume"):
                            if self._strategy_manager is None:
                                await self._telegram.send_message("전략 관리자가 초기화되지 않았습니다.")
                            elif len(parts) < 2:
                                names = [s["name"] for s in self._strategy_manager.get_strategy_list()]
                                await self._telegram.send_message(
                                    f"사용법: `/resume [전략명]`\n전략 목록: `{'`, `'.join(names)}`"
                                )
                            else:
                                name = parts[1]
                                ok = self._strategy_manager.set_strategy_mode(name, "PAPER")
                                if ok:
                                    await self._telegram.send_message(f"▶️ `{name}` 전략 재개 (PAPER 모드)")
                                else:
                                    await self._telegram.send_message(f"❌ 전략 `{name}` 을 찾을 수 없습니다.")

                        # ── /restart ──────────────────────────────────────
                        elif text == "/restart":
                            await self._telegram.send_message(
                                "🔄 *봇을 재시작합니다...*\n약 10초 후 다시 시작됩니다."
                            )
                            logger.warning("[Telegram] Restart requested by chat_id=%s", chat_id)
                            base_dir = str(__import__("pathlib").Path(__file__).parent.parent)
                            def _do_restart():
                                time.sleep(2)
                                _graceful_restart(base_dir)
                            __import__("threading").Thread(target=_do_restart, daemon=True).start()

                        # ── /opportunities ────────────────────────────────
                        elif text == "/opportunities":
                            opps = self._store.get_recent_opportunities(limit=5, status="PENDING") if self._store else []
                            if not opps:
                                await self._telegram.send_message("📋 현재 실행 후보 기회가 없습니다.")
                            else:
                                lines = ["*📋 실행 후보 Top 5*\n"]
                                for o in opps:
                                    import json as _j
                                    bkd = {}
                                    try:
                                        bkd = _j.loads(o.get("score_breakdown_json") or "{}")
                                    except Exception:
                                        pass
                                    bkd_str = ", ".join(f"{k}:{v:+d}" for k, v in list(bkd.items())[:3])
                                    lines.append(
                                        f"`{o['symbol']}` {o['side']} [{o['category']}]\n"
                                        f"  스코어: `{o['score_total']}` ({bkd_str})\n"
                                        f"  보유: {o.get('expected_hold_window','—')}"
                                    )
                                await self._telegram.send_message("\n".join(lines))

                        # ── /close [position_id] ──────────────────────────
                        elif text.startswith("/close"):
                            if len(parts) < 2:
                                await self._telegram.send_message(
                                    "사용법: `/close [position_id]\n`\n"
                                    "position_id는 `/positions` 에서 확인하세요."
                                )
                            else:
                                pos_id = parts[1]
                                # PaperRecorder에서 포지션 찾기
                                if self._strategy_manager:
                                    recorder = self._strategy_manager.recorder
                                    pos = recorder._open.get(pos_id)
                                    if pos:
                                        entry_price = pos.entry_price
                                        ticker = self._store.get_ticker(pos.symbol)
                                        exit_price = float(ticker.get("price", entry_price)) if ticker else entry_price
                                        recorder._close_position(pos, exit_price, f"Manual close by {chat_id}")
                                        # Audit 기록
                                        self._store.save_operator_action({
                                            "source": "telegram",
                                            "operator": str(chat_id),
                                            "action_type": "CLOSE_POSITION",
                                            "target_type": "paper_position",
                                            "target_id": pos_id,
                                            "reason": "manual close via /close",
                                            "result": f"closed at {exit_price}",
                                        })
                                        await self._telegram.send_message(
                                            f"✅ 포지션 종료됨\n"
                                            f"`{pos.symbol}` {pos.side} @ `{exit_price:.4f}`"
                                        )
                                    else:
                                        await self._telegram.send_message(f"❌ 포지션 `{pos_id}` 를 찾을 수 없습니다.")
                                else:
                                    await self._telegram.send_message("전략 관리자가 초기화되지 않았습니다.")

                        # ── /report_daily ─────────────────────────────────
                        elif text == "/report_daily":
                            if self._daily_reviewer is not None:
                                await self._telegram.send_message("📊 일간 리뷰를 즉시 실행합니다...")
                                asyncio.create_task(
                                    self._daily_reviewer.run(),
                                    name="manual_daily_review",
                                )
                            else:
                                # AI 없이 간단 요약
                                dpnl, dp = self._store.get_daily_pnl()
                                stats = self._store.get_strategy_stats()
                                total_trades = sum(s.get("trade_count", 0) for s in stats.values())
                                sign = "+" if dpnl >= 0 else ""
                                await self._telegram.send_message(
                                    f"*📊 오늘 요약*\n\n"
                                    f"손익: `{sign}{dpnl:.2f} USDT ({sign}{dp:.2f}%)`\n"
                                    f"거래 수: `{total_trades}`회\n"
                                    f"레짐: `{(self._store.get_regime() or {}).get('regime','UNKNOWN')}`"
                                )
                            # 건강도 카드 항상 추가
                            if self._strategy_manager is not None:
                                he = self._strategy_manager.health_engine
                                if he is not None:
                                    card = he.build_health_card("오늘")
                                    await self._telegram.send_message(card)

                        # ── /report_weekly ────────────────────────────────
                        elif text == "/report_weekly":
                            if self._weekly_reviewer is not None:
                                await self._telegram.send_message("📅 주간 리뷰를 즉시 실행합니다...")
                                asyncio.create_task(
                                    self._weekly_reviewer.run(),
                                    name="manual_weekly_review",
                                )
                            else:
                                stats = self._store.get_strategy_stats()
                                lines = ["*📅 주간 전략 요약*\n"]
                                for name_s, st in stats.items():
                                    lines.append(
                                        f"`{name_s}`: {st.get('trade_count',0)}건 "
                                        f"승률{st.get('win_rate',0)*100:.0f}% "
                                        f"PF:{st.get('profit_factor','—')}"
                                    )
                                await self._telegram.send_message("\n".join(lines) if lines else "데이터 없음")
                            # 건강도 카드 항상 추가
                            if self._strategy_manager is not None:
                                he = self._strategy_manager.health_engine
                                if he is not None:
                                    card = he.build_health_card("주간")
                                    await self._telegram.send_message(card)

                        # ── /universe ─────────────────────────────────────
                        elif text == "/universe":
                            if self._strategy_manager is not None:
                                txt = self._strategy_manager.universe.build_summary_text()
                                await self._telegram.send_message(txt)
                            else:
                                await self._telegram.send_message("전략 관리자가 초기화되지 않았습니다.")

                        # ── /validation ───────────────────────────────────
                        elif text == "/validation":
                            if self._validation_tracker is not None:
                                report = self._validation_tracker.build_validation_report()
                                await self._telegram.send_message(report)
                            else:
                                await self._telegram.send_message("검증 트래커가 초기화되지 않았습니다.")

                        # ── /pending ──────────────────────────────────────
                        elif text == "/pending":
                            if self._approval_manager is not None:
                                card = self._approval_manager.build_pending_card()
                                await self._telegram.send_message(card)
                            else:
                                await self._telegram.send_message("승인 관리자가 초기화되지 않았습니다.")

                        # ── /approve <id> ─────────────────────────────────
                        elif text.startswith("/approve"):
                            if self._approval_manager is None:
                                await self._telegram.send_message("승인 관리자가 초기화되지 않았습니다.")
                            else:
                                rec_id_arg = parts[1] if len(parts) > 1 else ""
                                if not rec_id_arg:
                                    await self._telegram.send_message("사용법: `/approve <추천ID>`")
                                else:
                                    # ID prefix로 full ID 검색
                                    pending = self._approval_manager.get_pending_recommendations()
                                    matched = [r for r in pending if r.get("id", "").startswith(rec_id_arg)]
                                    if not matched:
                                        await self._telegram.send_message(f"❌ 추천 `{rec_id_arg}` 를 찾을 수 없습니다.")
                                    else:
                                        result = self._approval_manager.approve_recommendation(
                                            matched[0]["id"],
                                            decided_by=f"telegram:{chat_id}",
                                        )
                                        msg = result.get("msg", "")
                                        if result.get("pending"):
                                            # Level 3: 2단계 확인 요청
                                            await self._telegram.send_message(msg)
                                        else:
                                            await self._telegram.send_message(msg)

                        # ── /confirm ──────────────────────────────────────
                        elif text == "/confirm":
                            if self._approval_manager is None:
                                await self._telegram.send_message("승인 관리자가 초기화되지 않았습니다.")
                            else:
                                result = self._approval_manager.execute_confirmed(
                                    operator=f"telegram:{chat_id}"
                                )
                                await self._telegram.send_message(result.get("msg", "처리 완료"))

                        # ── /reject <id> ──────────────────────────────────
                        elif text.startswith("/reject"):
                            if self._approval_manager is None:
                                await self._telegram.send_message("승인 관리자가 초기화되지 않았습니다.")
                            else:
                                rec_id_arg = parts[1] if len(parts) > 1 else ""
                                if not rec_id_arg:
                                    await self._telegram.send_message("사용법: `/reject <추천ID>`")
                                else:
                                    pending = self._approval_manager.get_pending_recommendations()
                                    matched = [r for r in pending if r.get("id", "").startswith(rec_id_arg)]
                                    if not matched:
                                        await self._telegram.send_message(f"❌ 추천 `{rec_id_arg}` 를 찾을 수 없습니다.")
                                    else:
                                        result = self._approval_manager.reject_recommendation(
                                            matched[0]["id"],
                                            decided_by=f"telegram:{chat_id}",
                                        )
                                        await self._telegram.send_message(result.get("msg", ""))

                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.debug("[Telegram] Command loop error: %s", exc)
                    _consecutive_failures += 1
                    if _consecutive_failures >= 3:
                        _reconnect_notified = True
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
        if self._hl_executor:
            await self._hl_executor.stop()
        if self._hl_collector:
            await self._hl_collector.stop()
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
                minute_key_daily  = f"{now.year}-{now.month:02d}-{now.day:02d}T{now.hour:02d}:{now.minute:02d}"
                minute_key_weekly = f"{now.isocalendar()[0]}W{now.isocalendar()[1]}-{now.weekday()}-{now.hour:02d}:{now.minute:02d}"

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
            approval_manager=self._approval_manager,
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


_running_engine: Optional["Engine"] = None


def _graceful_restart(base_dir: str) -> None:
    """Spawn a new bot process then trigger graceful shutdown of the current one."""
    import subprocess as _sp
    _sp.Popen(
        [sys.executable, "-m", "bot.main"],
        cwd=base_dir,
        creationflags=getattr(_sp, "CREATE_NEW_CONSOLE", 0),
    )
    time.sleep(0.5)
    global _running_engine
    eng = _running_engine
    if eng is not None and eng._shutdown_event is not None:
        loop = getattr(eng, "_loop", None)
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(eng._shutdown_event.set)
        else:
            eng._shutdown_event.set()
    else:
        os._exit(0)  # fallback only


def main() -> None:
    global _running_engine
    engine = Engine()
    _running_engine = engine
    try:
        asyncio.run(engine.start())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — exiting.")


if __name__ == "__main__":
    main()
