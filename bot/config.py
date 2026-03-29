"""
Configuration loader for the 22B Strategy Engine.
All settings are read from environment variables (loaded from .env).
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=_PROJECT_ROOT / ".env")

logger = logging.getLogger(__name__)


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _get_bool(key: str, default: bool = False) -> bool:
    val = _get(key, str(default)).lower()
    return val in ("1", "true", "yes", "on")


def _get_int(key: str, default: int = 0) -> int:
    try:
        return int(_get(key, str(default)))
    except ValueError:
        return default


def _get_float(key: str, default: float = 0.0) -> float:
    try:
        return float(_get(key, str(default)))
    except ValueError:
        return default


def _get_list(key: str, default: str = "") -> List[str]:
    raw = _get(key, default)
    return [s.strip() for s in raw.split(",") if s.strip()]


@dataclass
class Config:
    # Binance
    binance_api_key: str = field(default_factory=lambda: _get("BINANCE_API_KEY"))
    binance_api_secret: str = field(default_factory=lambda: _get("BINANCE_API_SECRET"))
    binance_testnet: bool = field(default_factory=lambda: _get_bool("BINANCE_TESTNET", False))

    # Hyperliquid
    hyperliquid_wallet_address: str = field(default_factory=lambda: _get("HYPERLIQUID_WALLET_ADDRESS"))
    hyperliquid_private_key: str = field(default_factory=lambda: _get("HYPERLIQUID_PRIVATE_KEY"))
    hyperliquid_enabled: bool = field(default_factory=lambda: _get_bool("HYPERLIQUID_ENABLED", False))

    # Telegram
    telegram_bot_token: str = field(default_factory=lambda: _get("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _get("TELEGRAM_CHAT_ID"))

    # Dashboard
    dashboard_host: str = field(default_factory=lambda: _get("DASHBOARD_HOST", "0.0.0.0"))
    dashboard_port: int = field(default_factory=lambda: _get_int("DASHBOARD_PORT", 8000))
    dashboard_secret_key: str = field(
        default_factory=lambda: _get("DASHBOARD_SECRET_KEY", "changeme")
    )

    # Database
    db_path: str = field(default_factory=lambda: _get("DB_PATH", "./data/trading.db"))

    # System
    system_mode: str = field(default_factory=lambda: _get("SYSTEM_MODE", "OBSERVE"))
    log_level: str = field(default_factory=lambda: _get("LOG_LEVEL", "INFO"))

    # Offline / validation dataset mode
    validation_dataset_enabled: bool = field(default_factory=lambda: _get_bool("VALIDATION_DATASET_ENABLED", False))
    validation_dataset_root: str = field(default_factory=lambda: _get(
        "VALIDATION_DATASET_ROOT",
        "./data/import_staging/validation_datasets",
    ))
    validation_replay_enabled: bool = field(
        default_factory=lambda: _get_bool("VALIDATION_REPLAY_ENABLED", False)
    )
    validation_replay_warmup_bars: int = field(
        default_factory=lambda: _get_int("VALIDATION_REPLAY_WARMUP_BARS", 52)
    )
    validation_replay_step_delay_ms: int = field(
        default_factory=lambda: _get_int("VALIDATION_REPLAY_STEP_DELAY_MS", 0)
    )
    validation_replay_max_steps: int = field(
        default_factory=lambda: _get_int("VALIDATION_REPLAY_MAX_STEPS", 0)
    )

    # Cloudflare Tunnel
    tunnel_enabled: bool = field(default_factory=lambda: _get_bool("TUNNEL_ENABLED", False))
    tunnel_cloudflared_path: str = field(
        default_factory=lambda: _get("TUNNEL_CLOUDFLARED_PATH", "./cloudflared.exe")
    )

    # OpenClaw AI (Phase 4)
    openclaw_base_url: str = field(default_factory=lambda: _get("OPENCLAW_BASE_URL", "http://127.0.0.1:18789"))
    openclaw_token: str = field(default_factory=lambda: _get("OPENCLAW_TOKEN", ""))
    openclaw_agent_id: str = field(default_factory=lambda: _get("OPENCLAW_AGENT_ID", "main"))
    weekly_review_day: int = field(default_factory=lambda: _get_int("WEEKLY_REVIEW_DAY", 6))
    weekly_review_hour: int = field(default_factory=lambda: _get_int("WEEKLY_REVIEW_HOUR", 0))
    daily_review_hour: int = field(default_factory=lambda: _get_int("DAILY_REVIEW_HOUR", 22))
    ai_enabled: bool = field(default_factory=lambda: _get_bool("AI_ENABLED", True))

    # Replay simulation (backtest)
    replay_initial_balance: float = field(default_factory=lambda: _get_float("REPLAY_INITIAL_BALANCE", 10_000.0))
    replay_position_size_pct: float = field(default_factory=lambda: _get_float("REPLAY_POSITION_SIZE_PCT", 0.10))
    replay_fee_rate: float = field(default_factory=lambda: _get_float("REPLAY_FEE_RATE", 0.0004))
    replay_slippage_pct: float = field(default_factory=lambda: _get_float("REPLAY_SLIPPAGE_PCT", 0.0005))

    # Symbols
    tracked_symbols: List[str] = field(
        default_factory=lambda: _get_list("TRACKED_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT")
    )

    # Data collection
    candle_intervals: List[str] = field(
        default_factory=lambda: _get_list("CANDLE_INTERVALS", "1h,4h")
    )
    ticker_update_interval_sec: int = field(
        default_factory=lambda: _get_int("TICKER_UPDATE_INTERVAL_SEC", 5)
    )
    candle_limit: int = field(default_factory=lambda: _get_int("CANDLE_LIMIT", 200))

    @property
    def binance_rest_base(self) -> str:
        if self.binance_testnet:
            return "https://testnet.binancefuture.com"
        return "https://fapi.binance.com"

    @property
    def binance_ws_base(self) -> str:
        if self.binance_testnet:
            return "wss://stream.binancefuture.com"
        return "wss://fstream.binance.com"

    @property
    def active_binance_api_key(self) -> str:
        return self.binance_api_key

    @property
    def active_binance_api_secret(self) -> str:
        return self.binance_api_secret

    def validate(self) -> None:
        if not self.binance_api_key:
            logger.warning("BINANCE_API_KEY is not set — live orders will fail")
        if not self.telegram_bot_token:
            logger.warning("TELEGRAM_BOT_TOKEN is not set; Telegram alerts disabled")

        valid_modes = {"ACTIVE", "LIMITED", "OBSERVE", "BLOCKED"}
        if self.system_mode not in valid_modes:
            logger.warning(
                "SYSTEM_MODE '%s' is not valid; falling back to OBSERVE",
                self.system_mode,
            )
            self.system_mode = "OBSERVE"

        if self.validation_replay_enabled and not self.validation_dataset_enabled:
            logger.warning(
                "VALIDATION_REPLAY_ENABLED requires VALIDATION_DATASET_ENABLED=true; disabling replay"
            )
            self.validation_replay_enabled = False

        if self.validation_replay_warmup_bars < 0:
            logger.warning(
                "VALIDATION_REPLAY_WARMUP_BARS must be >= 0; falling back to 52"
            )
            self.validation_replay_warmup_bars = 52

        if self.validation_replay_step_delay_ms < 0:
            logger.warning(
                "VALIDATION_REPLAY_STEP_DELAY_MS must be >= 0; falling back to 0"
            )
            self.validation_replay_step_delay_ms = 0

        if self.validation_replay_max_steps < 0:
            logger.warning(
                "VALIDATION_REPLAY_MAX_STEPS must be >= 0; falling back to 0"
            )
            self.validation_replay_max_steps = 0


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
        _config.validate()
    return _config
