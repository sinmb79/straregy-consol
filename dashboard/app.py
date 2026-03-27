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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Depends, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import APIKeyHeader
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
    approval_manager=None,
) -> FastAPI:
    """Factory that creates the FastAPI app with injected dependencies."""

    app = FastAPI(
        title="22B Strategy Engine Dashboard",
        version="4.0.0",
        docs_url="/api/docs",
    )

    # ── Write-API authentication ──────────────────────────────────────────── #
    _api_key_header = APIKeyHeader(name="X-Dashboard-Key", auto_error=False)

    async def _require_key(key: str = Depends(_api_key_header)):
        secret = getattr(config, "dashboard_secret_key", "")
        if not secret or secret in ("changeme", "change_me_to_a_random_secret"):
            logger.warning("[Dashboard] dashboard_secret_key not set — write APIs are unprotected")
            return  # backward compat: open if key not configured
        if key != secret:
            raise HTTPException(status_code=401, detail="Invalid or missing X-Dashboard-Key header")

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
                # Phase E — health info from strategy_state
                state = store.get_strategy_state(name) or {}
                s["health"] = {
                    "health_status":     state.get("health_status", "UNKNOWN"),
                    "recent_10_pf":      state.get("recent_10_pf"),
                    "recent_20_pf":      state.get("recent_20_pf"),
                    "recent_mdd":        state.get("recent_mdd"),
                    "live_eligibility":  state.get("live_eligibility", 0),
                    "last_pause_reason": state.get("last_pause_reason"),
                }
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

    @app.post("/api/kill-switch", dependencies=[Depends(_require_key)])
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

    @app.post("/api/kill-switch/reset", dependencies=[Depends(_require_key)])
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

    @app.post("/api/recommendations/{rec_id}/decide", dependencies=[Depends(_require_key)])
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

        # Route through ApprovalManager when available so strategy actions are executed
        if approval_manager is not None:
            if decision == "APPROVED":
                result = approval_manager.approve_recommendation(rec_id, decided_by=decided_by, reason=reason)
            elif decision == "REJECTED":
                result = approval_manager.reject_recommendation(rec_id, decided_by=decided_by, reason=reason)
            else:  # DEFERRED
                ok = store.update_recommendation(rec_id, {"status": "DEFERRED",
                    "decided_at": int(time.time() * 1000),
                    "decided_by": decided_by, "decision_reason": reason})
                result = {"ok": bool(ok), "msg": "Deferred"}
            if not result.get("ok"):
                status_code = 404 if "찾을 수 없" in result.get("msg", "") else 400
                raise HTTPException(status_code=status_code, detail=result.get("msg", "Failed"))
            logger.info("Recommendation %s → %s by %s via ApprovalManager: %s",
                        rec_id, decision, decided_by, reason[:80])
        else:
            ok = store.update_recommendation(rec_id, decision, reason, decided_by)
            if not ok:
                raise HTTPException(status_code=404, detail=f"Recommendation {rec_id} not found")
            logger.info("Recommendation %s → %s by %s: %s", rec_id, decision, decided_by, reason[:80])

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
            "last_ticker_ts":  last_ticker_ts,
            "ts":              int(now * 1000),
        })

    @app.post("/api/restart", dependencies=[Depends(_require_key)])
    async def api_restart():
        """Restart the bot process. Spawns a new process then exits."""
        import os, subprocess, threading, sys
        base_dir = str(Path(__file__).parent.parent)
        logger.warning("[Dashboard] Restart requested via API")

        def _do_restart():
            time.sleep(1.5)
            import bot.main as _bm
            if hasattr(_bm, "_graceful_restart"):
                _bm._graceful_restart(base_dir)
            else:
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
            "tracked_symbols": cfg.tracked_symbols if cfg else [],
            "candle_intervals": cfg.candle_intervals if cfg else ["1h", "4h"],
            "ai_enabled":      getattr(cfg, "ai_enabled", True) if cfg else True,
            "kill_switch":     engine._kill_switch.is_active if (engine and engine._kill_switch) else False,
            "exchange_mode":   store.get_exchange_mode() if store else "BOTH",
            "regime_override": store.get_regime_override() if store else None,
        })

    @app.post("/api/settings", dependencies=[Depends(_require_key)])
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
    # User Control — Regime Override
    # ------------------------------------------------------------------ #

    VALID_REGIMES = {
        "BTC_BULLISH", "BTC_BEARISH", "BTC_SIDEWAYS",
        "HIGH_VOLATILITY", "LOW_VOLATILITY", "ALT_ROTATION",
        "EVENT_RISK", "UNKNOWN",
    }

    @app.get("/api/regime/override")
    async def api_get_regime_override():
        """현재 레짐 오버라이드 상태 반환."""
        override = store.get_regime_override()
        bot_regime = store.get_regime() or {}
        return JSONResponse({
            "override":    override,
            "active":      override is not None,
            "bot_regime":  bot_regime.get("bot_regime") or bot_regime.get("regime", "UNKNOWN"),
            "ai_regime_note": (
                regime_interpreter.get_last_interpretation_dict().get("regime")
                if regime_interpreter and regime_interpreter.get_last_interpretation_dict()
                else None
            ),
        })

    @app.post("/api/regime/override")
    async def api_set_regime_override(request: Request):
        """사용자 레짐 수동 선택. body: {\"regime\": \"BTC_BULLISH\"} or {\"regime\": null}"""
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        regime_val = body.get("regime")
        if regime_val is None:
            store.set_regime_override(None)
            logger.info("[Dashboard] Regime override cleared — auto mode restored")
            return JSONResponse({"ok": True, "override": None, "mode": "auto"})

        regime_val = str(regime_val).upper()
        if regime_val not in VALID_REGIMES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid regime. Must be one of {sorted(VALID_REGIMES)}",
            )

        store.set_regime_override(regime_val)
        logger.info("[Dashboard] Regime override set to: %s", regime_val)
        return JSONResponse({"ok": True, "override": regime_val, "mode": "manual"})

    @app.delete("/api/regime/override")
    async def api_clear_regime_override():
        """레짐 오버라이드 해제 (자동 복원)."""
        store.set_regime_override(None)
        return JSONResponse({"ok": True, "override": None, "mode": "auto"})

    # ------------------------------------------------------------------ #
    # User Control — Exchange Mode
    # ------------------------------------------------------------------ #

    @app.get("/api/exchange-mode")
    async def api_get_exchange_mode():
        """현재 거래소 선택 모드 반환."""
        return JSONResponse({
            "exchange_mode": store.get_exchange_mode(),
            "hyperliquid_available": (
                engine._hl_executor is not None if engine else False
            ),
        })

    @app.post("/api/exchange-mode")
    async def api_set_exchange_mode(request: Request):
        """거래소 선택 변경. body: {\"mode\": \"BOTH\" | \"BINANCE_ONLY\" | \"HYPERLIQUID_ONLY\"}"""
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        mode = str(body.get("mode", "")).upper()
        valid = {"BOTH", "BINANCE_ONLY", "HYPERLIQUID_ONLY"}
        if mode not in valid:
            raise HTTPException(status_code=400, detail=f"mode must be one of {valid}")

        # Hyperliquid 미설치 시 HL 선택 방지
        if mode in ("BOTH", "HYPERLIQUID_ONLY") and engine and engine._hl_executor is None:
            raise HTTPException(
                status_code=409,
                detail="Hyperliquid executor is not initialized. Enable HYPERLIQUID_ENABLED in config.",
            )

        store.set_exchange_mode(mode)
        logger.info("[Dashboard] Exchange mode changed to: %s", mode)
        return JSONResponse({"ok": True, "exchange_mode": mode})

    # ------------------------------------------------------------------ #
    # User Control — Strategy Recommendations
    # ------------------------------------------------------------------ #

    @app.get("/api/strategy-recommendations")
    async def api_get_strategy_recommendations():
        """봇이 생성한 전략 추천 목록 반환."""
        recommender = getattr(engine, "_strategy_recommender", None) if engine else None
        if recommender is None:
            return JSONResponse({"pending": [], "history": []})
        return JSONResponse({
            "pending": recommender.get_pending(),
            "history": recommender.get_history(limit=20),
        })

    @app.post("/api/strategy-recommendations/refresh")
    async def api_refresh_strategy_recommendations():
        """즉시 전략 추천 재분석 트리거."""
        recommender = getattr(engine, "_strategy_recommender", None) if engine else None
        if recommender is None:
            raise HTTPException(status_code=503, detail="StrategyRecommender not initialized")
        regime = store.get_regime() or {"regime": "UNKNOWN"}
        new_recs = recommender.generate_recommendations(regime)
        return JSONResponse({"ok": True, "new_recommendations": len(new_recs)})

    @app.post("/api/strategy-recommendations/{rec_id}/decide")
    async def api_decide_strategy_recommendation(rec_id: str, request: Request):
        """
        전략 추천 승인 또는 거절.
        body: {\"approved\": true, \"decided_by\": \"operator\", \"reason\": \"...\"}
        """
        recommender = getattr(engine, "_strategy_recommender", None) if engine else None
        if recommender is None:
            raise HTTPException(status_code=503, detail="StrategyRecommender not initialized")

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        approved   = bool(body.get("approved", False))
        decided_by = str(body.get("decided_by", "dashboard_operator"))
        reason     = str(body.get("reason", ""))

        result = recommender.apply_recommendation(
            rec_id=rec_id,
            approved=approved,
            decided_by=decided_by,
            reason=reason,
        )
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=result.get("error", "Unknown error"))

        return JSONResponse(result)

    # ------------------------------------------------------------------ #
    # Strategy params (runtime editable)
    # ------------------------------------------------------------------ #

    @app.get("/api/strategy-params")
    async def api_get_strategy_params():
        """전략 파라미터 전체 조회 (기본값 + 오버라이드 병합)."""
        from bot.strategies.params_store import StrategyParamsStore
        store = StrategyParamsStore.get_instance()
        return JSONResponse(store.get_all())

    @app.post("/api/strategy-params/global")
    async def api_post_global_params(request: Request):
        """글로벌 파라미터 업데이트."""
        from bot.strategies.params_store import StrategyParamsStore
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
        store = StrategyParamsStore.get_instance()
        store.set_global(body)
        return JSONResponse({"ok": True, "updated": list(body.keys())})

    @app.post("/api/strategy-params/{strategy_name}")
    async def api_post_strategy_params(strategy_name: str, request: Request):
        """개별 전략 파라미터 업데이트."""
        from bot.strategies.params_store import StrategyParamsStore, DEFAULT_PARAMS
        if strategy_name not in DEFAULT_PARAMS.get("strategies", {}):
            raise HTTPException(status_code=404, detail=f"Unknown strategy: {strategy_name}")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
        store = StrategyParamsStore.get_instance()
        store.set_strategy(strategy_name, body)
        # enabled 변경 시 StrategyManager에도 반영
        if "enabled" in body and engine is not None:
            mgr = getattr(engine, "_strategy_manager", None)
            if mgr is not None:
                mode = "PAPER" if body["enabled"] else "PAUSED"
                mgr.set_strategy_mode(strategy_name, mode)
        return JSONResponse({"ok": True, "strategy": strategy_name, "updated": list(body.keys())})

    @app.post("/api/strategy-params/{strategy_name}/reset")
    async def api_reset_strategy_params(strategy_name: str):
        """전략 파라미터를 기본값으로 초기화."""
        from bot.strategies.params_store import StrategyParamsStore, DEFAULT_PARAMS
        if strategy_name not in DEFAULT_PARAMS.get("strategies", {}):
            raise HTTPException(status_code=404, detail=f"Unknown strategy: {strategy_name}")
        store = StrategyParamsStore.get_instance()
        with store._lock:
            if "strategies" in store._params:
                store._params["strategies"].pop(strategy_name, None)
            store._save()
        return JSONResponse({"ok": True, "strategy": strategy_name, "reset": True})

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

    @app.get("/api/paper-performance")
    async def api_paper_performance(capital: float = 1000.0, max_positions: int = 2):
        """
        Paper trading performance simulation.
        capital          — simulated starting balance (default $1000)
        max_positions    — max concurrent positions (used for per-trade sizing)
        """
        from collections import defaultdict

        trades = store.get_paper_performance_data()

        per_trade_alloc = capital / max(max_positions, 1)

        if not trades:
            return JSONResponse({
                "capital": capital,
                "per_trade_alloc": per_trade_alloc,
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "net_pnl_usd": 0.0,
                "net_pnl_pct": 0.0,
                "final_balance": capital,
                "avg_win_pct": 0.0,
                "avg_loss_pct": 0.0,
                "profit_factor": None,
                "max_drawdown_usd": 0.0,
                "expectancy_usd": 0.0,
                "per_strategy": {},
                "equity_curve": [],
            })

        wins_list   = [t for t in trades if float(t["pnl_pct"]) > 0]
        losses_list = [t for t in trades if float(t["pnl_pct"]) <= 0]
        total = len(trades)

        win_rate    = len(wins_list) / total
        avg_win_pct = sum(float(t["pnl_pct"]) for t in wins_list) / len(wins_list) if wins_list else 0.0
        avg_loss_pct = sum(float(t["pnl_pct"]) for t in losses_list) / len(losses_list) if losses_list else 0.0

        gross_profit = sum(float(t["pnl_pct"]) for t in wins_list)
        gross_loss   = abs(sum(float(t["pnl_pct"]) for t in losses_list))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

        # Equity curve — fixed per-trade allocation (non-compounding)
        balance  = capital
        peak     = capital
        max_dd   = 0.0
        equity_curve = []
        for i, t in enumerate(trades):
            pnl_usd = per_trade_alloc * float(t["pnl_pct"]) / 100
            balance += pnl_usd
            if balance > peak:
                peak = balance
            dd = balance - peak
            if dd < max_dd:
                max_dd = dd
            equity_curve.append({
                "i":        i + 1,
                "strategy": t["strategy"],
                "symbol":   t["symbol"],
                "pnl_pct":  round(float(t["pnl_pct"]), 4),
                "pnl_usd":  round(pnl_usd, 2),
                "balance":  round(balance, 2),
                "ts":       t["closed_at"],
            })

        net_pnl_usd = balance - capital
        net_pnl_pct = net_pnl_usd / capital * 100

        # Per-strategy breakdown
        groups = defaultdict(list)
        for t in trades:
            groups[t["strategy"]].append(float(t["pnl_pct"]))

        per_strategy_result = {}
        for name, pnls in groups.items():
            n   = len(pnls)
            w   = [p for p in pnls if p > 0]
            l   = [p for p in pnls if p <= 0]
            wr  = len(w) / n if n > 0 else 0.0
            net_usd = sum(per_trade_alloc * p / 100 for p in pnls)
            per_strategy_result[name] = {
                "trades":       n,
                "wins":         len(w),
                "losses":       len(l),
                "win_rate":     round(wr, 4),
                "net_pnl_usd":  round(net_usd, 2),
                "avg_win_pct":  round(sum(w) / len(w), 2) if w else 0.0,
                "avg_loss_pct": round(sum(l) / len(l), 2) if l else 0.0,
            }

        avg_win_usd  = avg_win_pct / 100 * per_trade_alloc
        avg_loss_usd = abs(avg_loss_pct) / 100 * per_trade_alloc
        expectancy_usd = (win_rate * avg_win_usd) - ((1 - win_rate) * avg_loss_usd)

        return JSONResponse({
            "capital":          capital,
            "per_trade_alloc":  round(per_trade_alloc, 2),
            "total_trades":     total,
            "wins":             len(wins_list),
            "losses":           len(losses_list),
            "win_rate":         round(win_rate, 4),
            "net_pnl_usd":      round(net_pnl_usd, 2),
            "net_pnl_pct":      round(net_pnl_pct, 2),
            "final_balance":    round(balance, 2),
            "avg_win_pct":      round(avg_win_pct, 2),
            "avg_loss_pct":     round(avg_loss_pct, 2),
            "profit_factor":    round(profit_factor, 2) if profit_factor is not None else None,
            "max_drawdown_usd": round(max_dd, 2),
            "expectancy_usd":   round(expectancy_usd, 2),
            "per_strategy":     per_strategy_result,
            "equity_curve":     equity_curve,
        })

    # ------------------------------------------------------------------ #
    # Image Pattern Strategy
    # ------------------------------------------------------------------ #

    # AI 분석 프롬프트 — OpenClaw에게 전달
    _CHART_ANALYSIS_PROMPT = """당신은 암호화폐 트레이딩 차트 패턴 분석 전문가입니다.

사용자가 차트 이미지에 직접 그린 표시(선, 화살표, 원, 텍스트 주석 등)를 분석하여
매매 조건을 아래 JSON 형식으로 반환하세요.

## 사용 가능한 조건 타입 (conditions 배열에서만 사용)
- rsi_below: {"type":"rsi_below","value":30,"period":14}
- rsi_above: {"type":"rsi_above","value":70,"period":14}
- rsi_recovering: {"type":"rsi_recovering","period":14}
- rsi_falling: {"type":"rsi_falling","period":14}
- price_near_level: {"type":"price_near_level","price":85000,"tol_pct":0.5}
- price_breakout_above: {"type":"price_breakout_above","price":85000}
- price_breakdown_below: {"type":"price_breakdown_below","price":85000}
- price_above_ema: {"type":"price_above_ema","period":50}
- price_below_ema: {"type":"price_below_ema","period":50}
- bollinger_squeeze: {"type":"bollinger_squeeze","threshold":0.05}
- bollinger_expansion: {"type":"bollinger_expansion","threshold":0.08}
- volume_spike: {"type":"volume_spike","multiplier":2.0}
- macd_cross_bullish: {"type":"macd_cross_bullish"}
- macd_cross_bearish: {"type":"macd_cross_bearish"}
- funding_below: {"type":"funding_below","value":0.0001}
- funding_above: {"type":"funding_above","value":-0.0001}
- candle_hammer: {"type":"candle_hammer"}
- candle_doji: {"type":"candle_doji"}
- candle_engulfing_bullish: {"type":"candle_engulfing_bullish"}
- candle_engulfing_bearish: {"type":"candle_engulfing_bearish"}

## 중요 규칙
1. conditions 배열은 위 타입만 사용 (다른 타입 불가)
2. price_near_level 등 가격 기반 조건의 price는 차트 Y축 눈금에서 읽거나, 읽을 수 없으면 omit
3. 가격 눈금이 없는 경우 price_near_level 대신 rsi, ema, candle 조건을 우선 사용
4. 반드시 유효한 JSON만 반환 (마크다운 코드블록 없이 JSON만)

## 응답 형식
{
  "pattern_name": "패턴명 (한국어)",
  "description": "패턴 설명 (1~2문장)",
  "direction": "LONG",
  "conditions": [...],
  "conditions_logic": "AND",
  "tp_pct": 3.0,
  "sl_pct": 1.5,
  "regime_filter": ["BTC_SIDEWAYS", "BTC_BULLISH"],
  "min_confidence": 0.65,
  "cooldown_hours": 4,
  "warnings": ["주의사항 목록"],
  "confidence_note": "신뢰도 판단 이유"
}"""

    @app.post("/api/image-pattern/analyze")
    async def api_image_pattern_analyze(
        file: UploadFile = File(...),
        symbol: str      = Form("ALL"),
        timeframe: str   = Form("1h"),
        description: str = Form(""),
    ):
        """
        차트 이미지를 업로드하면 연결된 AI(OpenClaw)가 분석하여
        조건 JSON을 반환합니다. (저장은 하지 않음 — 사전 검토용)
        """
        if engine is None or not hasattr(engine, "_claude") or engine._claude is None:
            raise HTTPException(status_code=503, detail="AI(OpenClaw) 연결 안 됨")

        # 파일 유효성 검사
        content_type = file.content_type or ""
        if not content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="이미지 파일만 허용됩니다 (PNG/JPEG/WEBP)")
        max_size = 10 * 1024 * 1024  # 10 MB
        image_bytes = await file.read()
        if len(image_bytes) > max_size:
            raise HTTPException(status_code=413, detail="이미지 크기 10MB 초과")

        user_prompt = (
            f"심볼: {symbol}, 타임프레임: {timeframe}\n"
            f"사용자 추가 설명: {description or '없음'}\n\n"
            f"{_CHART_ANALYSIS_PROMPT}"
        )

        try:
            raw = await engine._claude.analyze_image(
                image_bytes = image_bytes,
                text_prompt = user_prompt,
                mime_type   = content_type,
                max_tokens  = 2000,
            )
        except Exception as exc:
            logger.error("[ImagePattern] AI analyze error: %s", exc)
            raise HTTPException(status_code=500, detail=f"AI 분석 오류: {exc}")

        # JSON 파싱 시도 (마크다운 펜스 제거)
        import re as _re
        cleaned = _re.sub(r"```(?:json)?|```", "", raw).strip()
        try:
            parsed = json.loads(cleaned)
        except Exception:
            # 파싱 실패 시 raw 텍스트를 그대로 반환 (프론트에서 처리)
            return JSONResponse({"ok": False, "raw": raw, "error": "JSON 파싱 실패"})

        # image_b64 썸네일 (최대 200KB 로 축소 — 원본이 크면 skip)
        import base64 as _b64
        img_b64 = None
        if len(image_bytes) <= 200 * 1024:
            img_b64 = _b64.b64encode(image_bytes).decode("utf-8")

        return JSONResponse({
            "ok":       True,
            "analysis": parsed,
            "image_b64": img_b64,
            "content_type": content_type,
        })

    @app.post("/api/image-pattern/save")
    async def api_image_pattern_save(request: Request):
        """분석 결과를 승인하여 전략으로 저장."""
        import uuid as _uuid
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        analysis    = body.get("analysis", {})
        symbol      = body.get("symbol", "ALL")
        timeframe   = body.get("interval", "1h")
        image_b64   = body.get("image_b64")
        content_type = body.get("content_type", "image/png")

        if not analysis:
            raise HTTPException(status_code=400, detail="analysis 필드 누락")

        import json as _json
        pattern = {
            "id":               str(_uuid.uuid4()),
            "created_at":       int(time.time() * 1000),
            "pattern_name":     analysis.get("pattern_name", "Custom Pattern"),
            "description":      analysis.get("description", ""),
            "symbol":           symbol,
            "interval":         timeframe,
            "direction":        analysis.get("direction", "LONG"),
            "conditions_json":  _json.dumps(analysis.get("conditions", []), ensure_ascii=False),
            "conditions_logic": analysis.get("conditions_logic", "AND"),
            "tp_pct":           float(analysis.get("tp_pct", 3.0)),
            "sl_pct":           float(analysis.get("sl_pct", 1.5)),
            "regime_filter_json": (
                _json.dumps(analysis.get("regime_filter", []), ensure_ascii=False)
                if analysis.get("regime_filter") else None
            ),
            "min_confidence":   float(analysis.get("min_confidence", 0.65)),
            "cooldown_hours":   float(analysis.get("cooldown_hours", 4.0)),
            "enabled":          1,
            "image_b64":        image_b64,
            "ai_warnings_json": (
                _json.dumps(analysis.get("warnings", []), ensure_ascii=False)
                if analysis.get("warnings") else None
            ),
            "confidence_note":  analysis.get("confidence_note", ""),
        }
        store.save_image_pattern(pattern)

        # ImagePatternStrategy 캐시 무효화
        if strategy_manager is not None:
            ip_strat = getattr(strategy_manager, "_image_pattern_strategy", None)
            if ip_strat is not None:
                ip_strat.invalidate_cache()

        logger.info("[ImagePattern] Saved pattern '%s' (id=%s)", pattern["pattern_name"], pattern["id"])
        return JSONResponse({"ok": True, "id": pattern["id"]})

    @app.get("/api/image-patterns")
    async def api_list_image_patterns():
        """저장된 이미지 패턴 목록 반환."""
        patterns = store.get_all_image_patterns()
        # image_b64는 목록에서 제외 (크기 절약)
        for p in patterns:
            p.pop("image_b64", None)
        return JSONResponse(patterns)

    @app.patch("/api/image-pattern/{pattern_id}/toggle")
    async def api_toggle_image_pattern(pattern_id: str):
        """패턴 활성화/비활성화 토글."""
        patterns = store.get_all_image_patterns()
        pat = next((p for p in patterns if p["id"] == pattern_id), None)
        if not pat:
            raise HTTPException(status_code=404, detail="패턴을 찾을 수 없음")
        new_enabled = 0 if pat.get("enabled", 1) else 1
        store.update_image_pattern(pattern_id, {"enabled": new_enabled})
        if strategy_manager is not None:
            ip_strat = getattr(strategy_manager, "_image_pattern_strategy", None)
            if ip_strat:
                ip_strat.invalidate_cache()
        return JSONResponse({"ok": True, "enabled": new_enabled})

    @app.delete("/api/image-pattern/{pattern_id}")
    async def api_delete_image_pattern(pattern_id: str):
        """패턴 삭제."""
        ok = store.delete_image_pattern(pattern_id)
        if not ok:
            raise HTTPException(status_code=404, detail="패턴을 찾을 수 없음")
        if strategy_manager is not None:
            ip_strat = getattr(strategy_manager, "_image_pattern_strategy", None)
            if ip_strat:
                ip_strat.invalidate_cache()
        return JSONResponse({"ok": True})

    @app.get("/api/image-pattern/{pattern_id}/image")
    async def api_image_pattern_image(pattern_id: str):
        """패턴에 첨부된 차트 이미지 반환 (base64)."""
        try:
            row = store._conn.execute(
                "SELECT image_b64, confidence_note FROM image_patterns WHERE id = ?",
                (pattern_id,)
            ).fetchone()
        except Exception:
            row = None
        if not row or not row["image_b64"]:
            raise HTTPException(status_code=404, detail="이미지 없음")
        return JSONResponse({
            "image_b64":       row["image_b64"],
            "confidence_note": row["confidence_note"],
        })

    @app.get("/api/universe")
    async def api_universe():
        """Return symbol universe tier information."""
        if strategy_manager is not None and hasattr(strategy_manager, "universe"):
            return JSONResponse(strategy_manager.universe.to_dict())
        return JSONResponse([])

    @app.get("/api/pending-approvals")
    async def api_pending_approvals():
        """Return pending recommendation approvals."""
        try:
            rows = store.get_recommendations(status="PENDING", limit=20)
            return JSONResponse(rows)
        except Exception as exc:
            logger.warning("[Dashboard] pending approvals error: %s", exc)
            return JSONResponse([])

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

        loop = asyncio.get_event_loop()
        try:
            while True:
                try:
                    # queue is a stdlib thread-safe Queue; bridge to async via executor
                    msg = await asyncio.wait_for(
                        loop.run_in_executor(None, queue.get, True, 30),
                        timeout=35,
                    )
                    await websocket.send_text(msg)
                except (asyncio.TimeoutError, Exception):
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
