"""
Dashboard FastAPI application — Phase 4.

Endpoints:
  GET  /                          — main dashboard page (SSR + WebSocket)
  GET  /api/snapshot              — full state JSON
  GET  /api/indicators            — per-symbol indicator values
  GET  /api/signals               — last 50 signals (Panel 2)
  GET  /api/strategy-stats        — per-strategy stats (Panel 4)
  GET  /api/strategies            — strategy list with lifecycle state
  GET  /api/open-positions        — open paper positions (legacy)

  Phase 3:
  GET  /api/live-positions        — open LIVE positions (from Binance)
  GET  /api/trade-log             — trade log with filters
  POST /api/kill-switch           — trigger kill switch
  POST /api/kill-switch/reset     — reset kill switch
  GET  /api/reconcile-status      — last reconciliation result
  POST /api/order/close/{pos_id}  — manual reduce-only close

  Phase 4 (Panel 6 — Regime + AI Analysis):
  GET  /api/regime-interpretation — latest AI regime interpretation
  GET  /api/recommendations       — pending recommendations
  POST /api/recommendations/{id}/decide — approve/reject/defer
  GET  /api/recommendations/history    — past decisions
  GET  /api/daily-review          — latest daily report
  GET  /api/weekly-review         — latest weekly report

  WS   /ws/live                   — real-time push updates
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from bot.config import Config
from bot.data.store import DataStore
from bot.regime.detector import RegimeDetector

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


# --------------------------------------------------------------------------- #
# Request/response models
# --------------------------------------------------------------------------- #

class KillSwitchRequest(BaseModel):
    reason: str = "Manual trigger from dashboard"
    authorized_by: str = "dashboard_operator"


class KillSwitchResetRequest(BaseModel):
    authorized_by: str = "dashboard_operator"


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #

def create_app(
    store: DataStore,
    config: Config,
    strategy_manager=None,
    executor=None,
    kill_switch=None,
    reconciler=None,
    regime_interpreter=None,
    daily_reviewer=None,
    weekly_reviewer=None,
    engine=None,
) -> FastAPI:
    """Factory that creates the FastAPI app with injected dependencies."""

    app = FastAPI(
        title="22B Strategy Engine Dashboard",
        version="4.0.0",
        docs_url="/api/docs",
    )

    # Static files
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    # Regime detector for indicator endpoint
    detector = RegimeDetector(store)

    # ------------------------------------------------------------------ #
    # HTTP routes — Phase 1 / 2 (unchanged)
    # ------------------------------------------------------------------ #

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        snapshot = store.get_dashboard_snapshot()
        # Build WebSocket URL — respect HTTPS/WSS for Cloudflare tunnels and reverse proxies
        scheme = "wss" if request.headers.get("x-forwarded-proto") == "https" else "ws"
        host = request.headers.get("host", "localhost:8000")
        ws_url = f"{scheme}://{host}/ws/live"
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "snapshot": snapshot,
                "tracked_symbols": config.tracked_symbols,
                "ws_url": ws_url,
                "kill_switch_active": (
                    kill_switch.is_active if kill_switch else False
                ),
            },
        )

    @app.get("/api/snapshot")
    async def api_snapshot():
        return JSONResponse(store.get_dashboard_snapshot())

    @app.get("/api/indicators")
    async def api_indicators():
        """Return computed indicator values for all tracked symbols."""
        result = {}
        for symbol in config.tracked_symbols:
            indicators = {}
            for interval in config.candle_intervals:
                ind = detector.compute_indicators(symbol, interval)
                if ind:
                    indicators[interval] = ind
            ticker = store.get_ticker(symbol)
            funding = store.get_funding(symbol)
            oi = store.get_open_interest(symbol)
            result[symbol] = {
                "indicators": indicators,
                "price": ticker["price"] if ticker else None,
                "volume_24h": ticker["volume_24h"] if ticker else None,
                "change_pct": ticker["change_pct"] if ticker else None,
                "funding_rate": funding,
                "open_interest": oi,
            }
        return JSONResponse(result)

    @app.get("/api/regime")
    async def api_regime():
        regime = store.get_regime()
        if regime is None:
            return JSONResponse({"regime": "UNKNOWN", "message": "No regime computed yet"})
        return JSONResponse(regime)

    @app.get("/api/tickers")
    async def api_tickers():
        return JSONResponse(store.get_all_tickers())

    @app.get("/health")
    async def health():
        return JSONResponse({
            "status": "ok",
            "ts": int(time.time() * 1000),
            "kill_switch": kill_switch.is_active if kill_switch else False,
        })

    # ------------------------------------------------------------------ #
    # Phase 2 — Signals + Strategy Board
    # ------------------------------------------------------------------ #

    @app.get("/api/signals")
    async def api_signals(limit: int = 50):
        signals = store.get_signals(limit=min(limit, 200))
        return JSONResponse(signals)

    @app.get("/api/strategy-stats")
    async def api_strategy_stats():
        stats = store.get_strategy_stats()
        return JSONResponse(stats)

    @app.get("/api/strategies")
    async def api_strategies():
        if strategy_manager is not None:
            strategies = strategy_manager.get_strategy_list()
            stats = store.get_strategy_stats()
            for s in strategies:
                name = s["name"]
                s["stats"] = stats.get(name, {
                    "win_rate":      0.0,
                    "profit_factor": None,
                    "trade_count":   0,
                    "mdd":           0.0,
                    "expectancy":    0.0,
                    "open_count":    0,
                })
        else:
            strategies = []
        return JSONResponse(strategies)

    @app.get("/api/open-positions")
    async def api_open_positions():
        """Return all currently open paper positions."""
        positions = store.get_open_paper_positions()
        return JSONResponse(positions)

    # ------------------------------------------------------------------ #
    # Phase 3 — Live Positions (Panel 3)
    # ------------------------------------------------------------------ #

    @app.get("/api/live-positions")
    async def api_live_positions():
        """
        Return live open positions.
        Fetches from Binance API (via executor) for real-time accuracy.
        Falls back to DB positions if executor not available.
        """
        live_positions = []
        paper_positions = store.get_open_paper_positions()

        if executor is not None:
            try:
                binance_positions = await executor.get_open_positions()
                tickers = store.get_all_tickers()

                for pos in binance_positions:
                    symbol = pos.get("symbol", "")
                    entry_price = float(pos.get("entryPrice", 0))
                    pos_amt = float(pos.get("positionAmt", 0))
                    unrealised_pnl = float(pos.get("unRealizedProfit", 0))
                    ticker = tickers.get(symbol, {})
                    current_price = ticker.get("price", entry_price)

                    # PnL %
                    pnl_pct = 0.0
                    if entry_price > 0:
                        if pos_amt > 0:
                            pnl_pct = (current_price - entry_price) / entry_price * 100
                        else:
                            pnl_pct = (entry_price - current_price) / entry_price * 100

                    live_positions.append({
                        "symbol":        symbol,
                        "side":          "LONG" if pos_amt > 0 else "SHORT",
                        "qty":           abs(pos_amt),
                        "entry_price":   entry_price,
                        "current_price": current_price,
                        "unrealised_pnl": unrealised_pnl,
                        "pnl_pct":       round(pnl_pct, 4),
                        "sl":            None,  # retrieved from DB orders
                        "tp":            None,
                        "mode":          "LIVE",
                        "source":        "binance",
                    })
            except Exception as exc:
                logger.warning("api_live_positions: executor error: %s", exc)
                # Fall back to DB
                live_positions = store.get_open_live_positions()
        else:
            live_positions = store.get_open_live_positions()

        return JSONResponse({
            "live":  live_positions,
            "paper": paper_positions,
        })

    # ------------------------------------------------------------------ #
    # Phase 3 — Trade Log (Panel 5)
    # ------------------------------------------------------------------ #

    @app.get("/api/trade-log")
    async def api_trade_log(
        limit:    int = 50,
        strategy: Optional[str] = None,
        period:   Optional[str] = None,
        mode:     Optional[str] = None,
    ):
        """
        Return filtered trade log.
        Params:
          strategy  — strategy name filter
          period    — 'today' | '7d' | '30d'
          mode      — 'LIVE' | 'PAPER' (currently only PAPER is populated)
        """
        trades = store.get_trade_log(
            limit=min(limit, 200),
            mode=mode,
            strategy=strategy,
            period=period,
        )
        return JSONResponse(trades)

    @app.get("/api/trade-log/{trade_id}/audit")
    async def api_trade_audit(trade_id: str):
        """Return the audit trail for a specific trade."""
        trail = store.get_audit_trail(trade_id)
        return JSONResponse(trail)

    # ------------------------------------------------------------------ #
    # Phase 3 — Kill Switch
    # ------------------------------------------------------------------ #

    @app.post("/api/kill-switch")
    async def api_kill_switch(request: Request):
        """Trigger the kill switch. Requires JSON body with 'reason'."""
        if kill_switch is None:
            raise HTTPException(status_code=503, detail="KillSwitch not available")

        try:
            body = await request.json()
            reason = body.get("reason", "Manual trigger from dashboard")
            authorized_by = body.get("authorized_by", "dashboard")
        except Exception:
            reason = "Manual trigger from dashboard"
            authorized_by = "dashboard"

        logger.warning("Kill switch triggered via API: reason='%s'", reason)

        await kill_switch.trigger(
            reason=reason,
            triggered_by=authorized_by,
        )

        return JSONResponse({
            "status": "triggered",
            "reason": reason,
            "ts": int(time.time() * 1000),
        })

    @app.post("/api/kill-switch/reset")
    async def api_kill_switch_reset(request: Request):
        """Reset the kill switch. Requires 'authorized_by' in body."""
        if kill_switch is None:
            raise HTTPException(status_code=503, detail="KillSwitch not available")

        try:
            body = await request.json()
            authorized_by = body.get("authorized_by", "dashboard_operator")
        except Exception:
            authorized_by = "dashboard_operator"

        if not kill_switch.is_active:
            return JSONResponse({
                "status": "not_active",
                "message": "Kill switch is not currently active",
            })

        kill_switch.reset(authorized_by=authorized_by)

        return JSONResponse({
            "status": "reset",
            "authorized_by": authorized_by,
            "ts": int(time.time() * 1000),
        })

    @app.get("/api/kill-switch/status")
    async def api_kill_switch_status():
        """Return current kill switch status."""
        if kill_switch is None:
            return JSONResponse({"active": False, "reason": "", "triggered_at": None})
        return JSONResponse(kill_switch.get_status())

    # ------------------------------------------------------------------ #
    # Phase 3 — Reconcile status
    # ------------------------------------------------------------------ #

    @app.get("/api/reconcile-status")
    async def api_reconcile_status():
        """Return the last reconciliation result."""
        if reconciler is None:
            cached = store.get_last_reconcile()
            if cached:
                return JSONResponse(cached)
            return JSONResponse({
                "status": "reconciler_not_available",
                "last_run": None,
            })
        return JSONResponse(reconciler.get_status())

    # ------------------------------------------------------------------ #
    # Phase 3 — Manual position close (reduce-only)
    # ------------------------------------------------------------------ #

    @app.post("/api/order/close/{position_id}")
    async def api_close_position(position_id: str, request: Request):
        """Manually close a position with a reduce-only market order."""
        if executor is None:
            raise HTTPException(status_code=503, detail="Executor not available")
        if kill_switch and kill_switch.is_active:
            raise HTTPException(
                status_code=409,
                detail="Kill switch is active — cannot place new orders. "
                       "SL/TP orders are protecting existing positions.",
            )

        # Fetch position details from store
        position = store.get_order(position_id)
        if not position:
            # Try paper positions
            paper_positions = store.get_open_paper_positions()
            position = next(
                (p for p in paper_positions if str(p.get("id")) == position_id),
                None,
            )
            if position:
                return JSONResponse({
                    "status": "paper_position",
                    "message": "Cannot reduce-only close a paper position via API.",
                })

        if not position:
            raise HTTPException(status_code=404, detail=f"Position {position_id} not found")

        symbol = position.get("symbol")
        side   = position.get("side", "BUY")
        qty    = float(position.get("qty", 0) or 0)

        if qty <= 0:
            raise HTTPException(status_code=400, detail="Position quantity is 0")

        try:
            result = await executor.close_position_reduce_only(
                symbol=symbol,
                side="LONG" if side == "BUY" else "SHORT",
                qty=qty,
            )
            return JSONResponse({
                "status": "submitted",
                "result": result,
                "position_id": position_id,
            })
        except Exception as exc:
            logger.error("api_close_position error: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))

    # ------------------------------------------------------------------ #
    # Phase 3 — Account info
    # ------------------------------------------------------------------ #

    @app.get("/api/account")
    async def api_account():
        """Return cached account balance and P&L summary."""
        balance = store.get_account_balance()
        daily_pnl, daily_pnl_pct = store.get_daily_pnl()
        weekly_pnl = store.get_weekly_pnl()
        return JSONResponse({
            "balance":      balance,
            "daily_pnl":    daily_pnl,
            "daily_pnl_pct": daily_pnl_pct,
            "weekly_pnl":   weekly_pnl,
        })

    # ------------------------------------------------------------------ #
    # Phase 4 — Panel 6: Regime Interpretation
    # ------------------------------------------------------------------ #

    @app.get("/api/regime-interpretation")
    async def api_regime_interpretation():
        """Return the latest AI regime interpretation."""
        if regime_interpreter is None:
            return JSONResponse({
                "available": False,
                "message": "RegimeInterpreter not initialized",
            })
        interp = regime_interpreter.get_last_interpretation_dict()
        if interp is None:
            return JSONResponse({
                "available": False,
                "message": "No interpretation available yet — awaiting first regime change",
            })
        return JSONResponse({"available": True, **interp})

    # ------------------------------------------------------------------ #
    # Phase 4 — Panel 6: Recommendations
    # ------------------------------------------------------------------ #

    @app.get("/api/recommendations")
    async def api_recommendations():
        """Return all pending recommendations."""
        pending = store.get_pending_recommendations()
        return JSONResponse(pending)

    @app.post("/api/recommendations/{rec_id}/decide")
    async def api_recommendation_decide(rec_id: str, request: Request):
        """
        Approve, reject, or defer a recommendation.

        Body:
          {
            "decision": "APPROVED" | "REJECTED" | "DEFERRED",
            "reason": "...",
            "decided_by": "operator_name"
          }
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        decision    = body.get("decision", "").upper()
        reason      = body.get("reason", "")
        decided_by  = body.get("decided_by", "dashboard_operator")

        valid_decisions = {"APPROVED", "REJECTED", "DEFERRED"}
        if decision not in valid_decisions:
            raise HTTPException(
                status_code=400,
                detail=f"'decision' must be one of {valid_decisions}",
            )
        if not reason.strip():
            raise HTTPException(status_code=400, detail="'reason' is required")

        ok = store.update_recommendation(rec_id, decision, reason, decided_by)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Recommendation {rec_id} not found")

        logger.info(
            "Recommendation %s → %s by %s: %s",
            rec_id, decision, decided_by, reason[:80],
        )
        return JSONResponse({
            "status":     "updated",
            "id":         rec_id,
            "decision":   decision,
            "decided_by": decided_by,
            "ts":         int(time.time() * 1000),
        })

    @app.get("/api/recommendations/history")
    async def api_recommendations_history(limit: int = 20):
        """Return past decided recommendations."""
        history = store.get_recommendation_history(limit=min(limit, 100))
        return JSONResponse(history)

    # ------------------------------------------------------------------ #
    # Phase 4 — Daily and Weekly Reviews
    # ------------------------------------------------------------------ #

    @app.get("/api/daily-review")
    async def api_daily_review():
        """Return the latest daily review report."""
        if daily_reviewer is not None:
            report = daily_reviewer.get_last_report_dict()
            if report:
                return JSONResponse({"available": True, **report})

        # Fall back to DB
        reviews = store.get_reviews(limit=1, type="daily")
        if reviews:
            row = reviews[0]
            content = row.get("content")
            if content:
                try:
                    data = json.loads(content)
                    return JSONResponse({"available": True, **data})
                except Exception:
                    pass

        return JSONResponse({"available": False, "message": "No daily review yet"})

    @app.get("/api/weekly-review")
    async def api_weekly_review():
        """Return the latest weekly review report."""
        if weekly_reviewer is not None:
            report = weekly_reviewer.get_last_report_dict()
            if report:
                return JSONResponse({"available": True, **report})

        # Fall back to DB
        reviews = store.get_reviews(limit=1, type="weekly")
        if reviews:
            row = reviews[0]
            content = row.get("content")
            if content:
                try:
                    data = json.loads(content)
                    return JSONResponse({"available": True, **data})
                except Exception:
                    pass

        return JSONResponse({"available": False, "message": "No weekly review yet"})

    @app.get("/api/daily-alert-count")
    async def api_daily_alert_count():
        """Return the current daily alert badge count."""
        return JSONResponse({"count": store.get_daily_alert_count()})

    # ------------------------------------------------------------------ #
    # System Health + Restart
    # ------------------------------------------------------------------ #

    @app.get("/api/system-health")
    async def api_system_health():
        """Return detailed system health for the status panel."""
        now = time.time()
        uptime_sec = int(now - engine._start_time) if engine else 0

        # OpenClaw
        openclaw_ok = False
        if engine and engine._claude:
            openclaw_ok = await engine._claude.is_available()

        # Telegram
        telegram_ok = bool(engine and engine._telegram and engine._telegram._enabled)

        # Binance WS (collector running)
        binance_ws_ok = bool(engine and engine._collector and engine._collector._running)

        # Last ticker timestamp (most recent ticker update)
        tickers = store.get_all_tickers()
        last_ticker_ts = None
        if tickers:
            ts_list = [v.get("ts") for v in tickers.values() if v.get("ts")]
            if ts_list:
                last_ticker_ts = max(ts_list)

        return JSONResponse({
            "status":          "running",
            "uptime_sec":      uptime_sec,
            "system_mode":     config.system_mode,
            "kill_switch":     kill_switch.is_active if kill_switch else False,
            "binance_ws":      binance_ws_ok,
            "openclaw":        openclaw_ok,
            "telegram":        telegram_ok,
            "testnet":         config.binance_testnet,
            "last_ticker_ts":  last_ticker_ts,
            "ts":              int(now * 1000),
        })

    @app.post("/api/restart")
    async def api_restart():
        """Restart the bot process. Spawns a new process then exits."""
        import os, subprocess, threading, sys
        base_dir = str(Path(__file__).parent.parent)
        logger.warning("[Dashboard] Restart requested via API")

        def _do_restart():
            time.sleep(1.5)
            subprocess.Popen(
                [sys.executable, "-m", "bot.main"],
                cwd=base_dir,
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
            time.sleep(0.5)
            os._exit(0)

        threading.Thread(target=_do_restart, daemon=True).start()
        return JSONResponse({"status": "restarting", "message": "Bot will restart in ~2s"})

    # ------------------------------------------------------------------ #
    # Settings (read + write)
    # ------------------------------------------------------------------ #

    @app.get("/api/settings")
    async def api_get_settings():
        """Return current runtime settings."""
        cfg = engine._config if engine is not None else None
        store = engine._store if engine is not None else None
        return JSONResponse({
            "system_mode":     store.get_system_mode() if store else (cfg.system_mode if cfg else "OBSERVE"),
            "testnet":         cfg.binance_testnet if cfg else True,
            "tracked_symbols": cfg.tracked_symbols if cfg else [],
            "candle_intervals": cfg.candle_intervals if cfg else ["1h", "4h"],
            "ai_enabled":      getattr(cfg, "ai_enabled", True) if cfg else True,
            "kill_switch":     engine._kill_switch.is_active if (engine and engine._kill_switch) else False,
        })

    @app.post("/api/settings")
    async def api_post_settings(request: Request):
        """
        Update runtime settings without restart.

        Accepted fields (all optional):
          system_mode   : "OBSERVE" | "LIMITED" | "ACTIVE" | "BLOCKED"
          ai_enabled    : bool
          kill_switch   : bool (true = trigger, false = reset)
          tracked_symbols : list[str]
        """
        if engine is None:
            raise HTTPException(status_code=503, detail="Engine not running")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        changed = []

        # --- system_mode ---
        if "system_mode" in body:
            new_mode = str(body["system_mode"]).upper()
            valid = {"OBSERVE", "LIMITED", "ACTIVE", "BLOCKED"}
            if new_mode not in valid:
                raise HTTPException(status_code=400, detail=f"Invalid system_mode. Must be one of {valid}")
            engine._config.system_mode = new_mode
            engine._store.set_system_mode(new_mode)
            changed.append(f"system_mode={new_mode}")
            logger.info("[Dashboard/Settings] system_mode changed to %s", new_mode)

        # --- ai_enabled ---
        if "ai_enabled" in body:
            val = bool(body["ai_enabled"])
            engine._config.ai_enabled = val
            changed.append(f"ai_enabled={val}")
            logger.info("[Dashboard/Settings] ai_enabled changed to %s", val)

        # --- kill_switch ---
        if "kill_switch" in body and engine._kill_switch is not None:
            if body["kill_switch"]:
                await engine._kill_switch.trigger(
                    reason="Dashboard settings panel",
                    triggered_by="dashboard",
                )
                changed.append("kill_switch=triggered")
            else:
                engine._kill_switch.reset(authorized_by="dashboard")
                changed.append("kill_switch=reset")

        # --- tracked_symbols ---
        if "tracked_symbols" in body:
            syms = [str(s).upper().strip() for s in body["tracked_symbols"] if str(s).strip()]
            if syms:
                engine._config.tracked_symbols = syms
                changed.append(f"tracked_symbols={syms}")
                logger.info("[Dashboard/Settings] tracked_symbols changed to %s", syms)

        return JSONResponse({"ok": True, "changed": changed})

    # ------------------------------------------------------------------ #
    # Backtest report
    # ------------------------------------------------------------------ #

    @app.get("/api/backtest-report")
    async def api_backtest_report():
        """Return the latest saved backtest report, or the live replay account metrics."""
        # 1) If engine has a live replay_account, return live metrics
        if engine is not None and hasattr(engine, "_replay_account") and engine._replay_account is not None:
            try:
                metrics = engine._replay_account.compute_metrics()
                return JSONResponse({
                    "source": "live_replay",
                    "metrics": metrics,
                    "equity_curve": [
                        {"ts_ms": ts, "balance": round(bal, 4)}
                        for ts, bal in engine._replay_account.equity_curve
                    ],
                })
            except Exception as exc:
                logger.warning("[Dashboard] Live replay metrics error: %s", exc)

        # 2) Fall back to latest saved JSON report
        try:
            from bot.ai.backtest_reporter import load_latest_report
            report = load_latest_report()
            if report:
                return JSONResponse({"source": "saved_report", **report})
        except Exception as exc:
            logger.warning("[Dashboard] Backtest report load error: %s", exc)

        return JSONResponse({"source": "none", "metrics": {}, "equity_curve": []})

    # ------------------------------------------------------------------ #
    # Strategy mode management
    # ------------------------------------------------------------------ #

    @app.post("/api/strategies/{name}/mode")
    async def set_strategy_mode(name: str, request: Request):
        """
        Change the mode of a strategy.

        Body:
          { "mode": "PAPER" | "SHADOW" | "PAUSED" | "LIVE" }
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        new_mode = body.get("mode")
        if new_mode not in ("PAPER", "SHADOW", "PAUSED", "LIVE"):
            raise HTTPException(status_code=400, detail="invalid mode — must be PAPER, SHADOW, PAUSED, or LIVE")

        if strategy_manager is None:
            raise HTTPException(status_code=503, detail="StrategyManager not available")

        ok = strategy_manager.set_strategy_mode(name, new_mode)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Strategy '{name}' not found")

        return JSONResponse({"ok": True, "strategy": name, "mode": new_mode})

    # ------------------------------------------------------------------ #
    # WebSocket endpoint
    # ------------------------------------------------------------------ #

    @app.websocket("/ws/live")
    async def websocket_live(websocket: WebSocket):
        await websocket.accept()
        queue = store.subscribe()

        # Send initial snapshot
        try:
            snapshot = store.get_dashboard_snapshot()
            await websocket.send_text(
                json.dumps({"type": "snapshot", "data": snapshot})
            )
        except Exception as exc:
            logger.debug("WS initial snapshot send failed: %s", exc)

        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=30)
                    await websocket.send_text(msg)
                    queue.task_done()
                except asyncio.TimeoutError:
                    await websocket.send_text(
                        json.dumps({"type": "ping", "ts": int(time.time() * 1000)})
                    )
        except WebSocketDisconnect:
            logger.debug("WebSocket client disconnected")
        except Exception as exc:
            logger.debug("WebSocket error: %s", exc)
        finally:
            store.unsubscribe(queue)

    return app
