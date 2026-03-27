"""
RegimeDetector — pure rule-based market regime classification.

Rules follow the 22B Strategy Engine Master Plan v1.2, Part 2.
Priority order: EVENT_RISK > HIGH_VOLATILITY > BTC_BULLISH / BTC_BEARISH /
                BTC_SIDEWAYS / ALT_ROTATION / LOW_VOLATILITY > UNKNOWN

No AI, no ML. Same input always produces same output.
"""

import logging
import time
from enum import Enum
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from bot.data.store import DataStore

logger = logging.getLogger(__name__)


class Regime(str, Enum):
    BTC_BULLISH = "BTC_BULLISH"
    BTC_BEARISH = "BTC_BEARISH"
    BTC_SIDEWAYS = "BTC_SIDEWAYS"
    ALT_ROTATION = "ALT_ROTATION"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    EVENT_RISK = "EVENT_RISK"
    UNKNOWN = "UNKNOWN"


# Strategies allowed per regime
REGIME_ALLOW_TABLE: Dict[str, List[str]] = {
    Regime.BTC_BULLISH: ["trend", "breakout", "multi"],
    Regime.BTC_BEARISH: ["mean_reversion", "hedge"],
    Regime.BTC_SIDEWAYS: ["mean_reversion", "range"],
    Regime.ALT_ROTATION: ["regime", "trend"],
    Regime.HIGH_VOLATILITY: ["volatility", "micro"],
    Regime.LOW_VOLATILITY: ["breakout"],
    Regime.EVENT_RISK: [],
    Regime.UNKNOWN: [],
}

REGIME_BLOCK_TABLE: Dict[str, List[str]] = {
    Regime.BTC_BEARISH: ["trend_long"],
    Regime.HIGH_VOLATILITY: [],          # reduce new entries by 50% (handled elsewhere)
    Regime.LOW_VOLATILITY: ["momentum"],
    Regime.EVENT_RISK: ["*"],            # block ALL
    Regime.UNKNOWN: ["live"],            # block LIVE entries
}


# --------------------------------------------------------------------------- #
# Indicator calculation helpers
# --------------------------------------------------------------------------- #

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def _bollinger_bandwidth(close: pd.Series, period: int = 20, std_mult: float = 2.0) -> pd.Series:
    """Bollinger Band width as a fraction of the middle band."""
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    return (upper - lower) / mid


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """Session VWAP (cumulative for the dataset provided)."""
    typical = (high + low + close) / 3
    cumvol = volume.cumsum()
    cumvwap = (typical * volume).cumsum()
    return cumvwap / cumvol.replace(0, np.nan)


# --------------------------------------------------------------------------- #
# Regime Detector
# --------------------------------------------------------------------------- #

class RegimeDetector:
    """
    Evaluates market regime from DataStore indicators.
    Call `detect()` to get the current regime dict.
    """

    def __init__(self, store: DataStore) -> None:
        self._store = store
        self._event_risk_active: bool = False   # set externally if macro event detected

    # ---------------------------------------------------------------------- #
    # Public API
    # ---------------------------------------------------------------------- #

    def set_event_risk(self, active: bool) -> None:
        """Manually flag EVENT_RISK (e.g., detected maintenance window)."""
        self._event_risk_active = active

    def detect_bot_regime(self) -> dict:
        """
        항상 봇의 규칙 기반 레짐을 계산해 반환한다.
        사용자 오버라이드와 무관하게 봇 판단을 알고 싶을 때 사용.
        """
        return self._run_detection()

    def compute_indicators(self, symbol: str, interval: str) -> Optional[dict]:
        """
        Compute EMA50, ATR, RSI, BB, VWAP for the given symbol/interval.
        Returns None if not enough data.
        """
        candles = self._store.get_candles(symbol, interval, limit=200)
        if len(candles) < 52:   # need at least 52 bars for EMA50
            return None

        df = pd.DataFrame(candles)
        df = df.sort_values("ts").reset_index(drop=True)

        close = df["c"].astype(float)
        high = df["h"].astype(float)
        low = df["l"].astype(float)
        volume = df["v"].astype(float)

        ema50 = _ema(close, 50).iloc[-1]
        ema20 = _ema(close, 20).iloc[-1]
        atr_series = _atr(high, low, close, 14)
        atr = atr_series.iloc[-1]
        rsi = _rsi(close, 14).iloc[-1]
        bb_bw_series = _bollinger_bandwidth(close, 20)
        bb_bw = bb_bw_series.iloc[-1]
        bb_bw_avg = bb_bw_series.rolling(20).mean().iloc[-1]
        vwap = _vwap(high, low, close, volume).iloc[-1]

        current_price = close.iloc[-1]
        prev_24h_price = close.iloc[-25] if len(close) >= 25 else close.iloc[0]
        prev_1h_price = close.iloc[-2] if len(close) >= 2 else close.iloc[0]

        ret_24h = (current_price - prev_24h_price) / prev_24h_price * 100
        ret_1h = (current_price - prev_1h_price) / prev_1h_price * 100
        atr_pct = atr / current_price * 100 if current_price else 0.0

        return {
            "symbol": symbol,
            "interval": interval,
            "price": round(current_price, 8),
            "ema50": round(ema50, 8),
            "ema20": round(ema20, 8),
            "atr": round(atr, 8),
            "atr_pct": round(atr_pct, 4),
            "rsi": round(rsi, 2),
            "bb_bw": round(bb_bw, 6),
            "bb_bw_avg": round(bb_bw_avg, 6) if not np.isnan(bb_bw_avg) else None,
            "vwap": round(vwap, 8),
            "ret_24h": round(ret_24h, 4),
            "ret_1h": round(ret_1h, 4),
        }

    def detect(self) -> dict:
        """
        Run all regime rules and return the winning regime + indicator snapshot.

        사용자 오버라이드가 있으면 `regime` 필드를 오버라이드 값으로 교체하되
        봇의 판단은 `bot_regime` 필드로 함께 포함한다.

        Returns a dict:
        {
            "ts": unix ms,
            "regime": "BTC_BULLISH",        # 실제 사용 레짐 (오버라이드 or 봇)
            "bot_regime": "BTC_BEARISH",    # 봇의 규칙 기반 판단 (항상 포함)
            "user_override": True/False,    # 오버라이드 활성 여부
            "allowed_strategies": [...],
            "btc_price": ...,
            ...
        }
        """
        result = self._run_detection()

        # 사용자 오버라이드 확인
        override = self._store.get_regime_override() if hasattr(self._store, "get_regime_override") else None
        result["bot_regime"] = result["regime"]
        result["user_override"] = False

        if override and override in [r.value for r in Regime]:
            result["regime"] = override
            result["user_override"] = True
            result["allowed_strategies"] = REGIME_ALLOW_TABLE.get(override, [])
            result["new_entry_allowed"] = override not in (
                Regime.EVENT_RISK.value, Regime.UNKNOWN.value
            )
            logger.info("[RegimeDetector] User override active: %s (bot: %s)", override, result["bot_regime"])

        return result

    def _run_detection(self) -> dict:
        """봇 규칙 기반 레짐 계산 (오버라이드 무시)."""
        ts = int(time.time() * 1000)

        # EVENT_RISK has highest priority
        if self._event_risk_active:
            return self._make_result(ts, Regime.EVENT_RISK, {})

        # Compute BTC indicators on 4H (primary) and 1H
        btc_4h = self.compute_indicators("BTCUSDT", "4h")
        btc_1h = self.compute_indicators("BTCUSDT", "1h")

        if btc_4h is None:
            logger.warning("Not enough BTC 4H candles for regime detection — UNKNOWN")
            return self._make_result(ts, Regime.UNKNOWN, {})

        # Pull live data from store
        funding = self._store.get_funding("BTCUSDT") or 0.0
        btc_ticker = self._store.get_ticker("BTCUSDT")
        btc_price = btc_4h["price"]

        # BTC 24H return and 1H return
        ret_24h = btc_4h["ret_24h"]
        ret_1h = btc_1h["ret_1h"] if btc_1h else btc_4h["ret_1h"]

        atr_pct = btc_4h["atr_pct"]
        ema50 = btc_4h["ema50"]
        bb_bw = btc_4h["bb_bw"]
        bb_bw_avg = btc_4h["bb_bw_avg"]

        # HIGH_VOLATILITY — second highest priority
        if atr_pct > 5.0 or abs(ret_1h) > 3.0:
            indicators = self._build_indicators(btc_4h, btc_1h, funding)
            return self._make_result(ts, Regime.HIGH_VOLATILITY, indicators)

        # BTC_BULLISH
        # Conditions: EMA50 below price (price above EMA50), 24H ret > 0, funding < 0.05%, atr_pct <= 5
        if (
            btc_price > ema50
            and ret_24h > 0.0
            and funding < 0.05
            and atr_pct <= 5.0
        ):
            indicators = self._build_indicators(btc_4h, btc_1h, funding)
            return self._make_result(ts, Regime.BTC_BULLISH, indicators)

        # BTC_BEARISH
        # Conditions: EMA50 above price (price below EMA50), 24H ret < -1, not (1H ret > 3)
        if (
            btc_price < ema50
            and ret_24h < -1.0
            and ret_1h <= 3.0
        ):
            indicators = self._build_indicators(btc_4h, btc_1h, funding)
            return self._make_result(ts, Regime.BTC_BEARISH, indicators)

        # BTC_SIDEWAYS
        # Conditions: 24H ret in [-1, +1], atr_pct < 3, BB bandwidth < 70% of avg
        bb_below_avg = (
            bb_bw < 0.7 * bb_bw_avg
            if bb_bw_avg and bb_bw_avg > 0
            else True
        )
        if -1.0 <= ret_24h <= 1.0 and atr_pct < 3.0 and bb_below_avg:
            indicators = self._build_indicators(btc_4h, btc_1h, funding)
            return self._make_result(ts, Regime.BTC_SIDEWAYS, indicators)

        # ALT_ROTATION (requires SIDEWAYS or BULLISH base — checked below)
        # We check this after main regimes; approximate here by checking BTC dominance
        # (BTC.D not directly available from Binance Futures, tracked separately)
        # For now: skip if no signal matched above

        # LOW_VOLATILITY
        if atr_pct < 2.0 and bb_bw_avg and bb_bw < 0.5 * bb_bw_avg:
            indicators = self._build_indicators(btc_4h, btc_1h, funding)
            return self._make_result(ts, Regime.LOW_VOLATILITY, indicators)

        # No rule matched
        indicators = self._build_indicators(btc_4h, btc_1h, funding)
        return self._make_result(ts, Regime.UNKNOWN, indicators)

    # ---------------------------------------------------------------------- #
    # Private helpers
    # ---------------------------------------------------------------------- #

    def _build_indicators(
        self,
        btc_4h: Optional[dict],
        btc_1h: Optional[dict],
        funding: float,
    ) -> dict:
        ind: dict = {}
        if btc_4h:
            ind.update({
                "btc_price": btc_4h["price"],
                "btc_ema50": btc_4h["ema50"],
                "btc_ema20": btc_4h["ema20"],
                "btc_atr": btc_4h["atr"],
                "btc_atr_pct": btc_4h["atr_pct"],
                "btc_rsi": btc_4h["rsi"],
                "btc_bb_bw": btc_4h["bb_bw"],
                "btc_ret_24h": btc_4h["ret_24h"],
                "btc_vwap": btc_4h["vwap"],
            })
        if btc_1h:
            ind.update({
                "btc_ret_1h": btc_1h["ret_1h"],
                "btc_rsi_1h": btc_1h["rsi"],
            })
        ind["funding"] = round(funding, 6)
        return ind

    @staticmethod
    def _make_result(ts: int, regime: Regime, indicators: dict) -> dict:
        result = {
            "ts": ts,
            "regime": regime.value,
            "allowed_strategies": REGIME_ALLOW_TABLE.get(regime, []),
            "new_entry_allowed": regime not in (Regime.EVENT_RISK, Regime.UNKNOWN),
        }
        result.update(indicators)
        return result
