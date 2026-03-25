"""
Condition Evaluator — image_pattern_strategy 에서 사용하는 조건 평가 엔진.

지원 조건 타입:
  rsi_below / rsi_above / rsi_recovering / rsi_falling
  price_near_level / price_breakout_above / price_breakdown_below
  price_above_ema / price_below_ema
  bollinger_squeeze / bollinger_expansion
  volume_spike
  macd_cross_bullish / macd_cross_bearish
  funding_below / funding_above
  candle_hammer / candle_doji / candle_engulfing_bullish / candle_engulfing_bearish
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List

import pandas as pd

if TYPE_CHECKING:
    from bot.data.store import DataStore

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
    rs    = gain / loss.replace(0, 1e-9)
    return (100 - (100 / (1 + rs))).fillna(50)


def _candles_df(store: "DataStore", symbol: str, interval: str, limit: int) -> pd.DataFrame | None:
    candles = store.get_candles(symbol, interval, limit=limit)
    if not candles or len(candles) < 5:
        return None
    df = pd.DataFrame(candles).sort_values("ts").reset_index(drop=True)
    for col in ("o", "h", "l", "c", "v"):
        if col in df.columns:
            df[col] = df[col].astype(float)
    return df


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def evaluate_conditions(
    conditions: List[dict],
    logic: str,
    store: "DataStore",
    symbol: str,
    interval: str = "1h",
) -> bool:
    """
    Evaluate conditions list against current market data.
    logic = "AND" → all conditions must be True
    logic = "OR"  → at least one condition must be True
    """
    if not conditions:
        return False

    results = []
    for cond in conditions:
        try:
            results.append(_eval_single(cond, store, symbol, interval))
        except Exception as exc:
            logger.debug("[ConditionEval] %s failed: %s", cond.get("type"), exc)
            results.append(False)

    return any(results) if logic == "OR" else all(results)


# --------------------------------------------------------------------------- #
# Single condition dispatch
# --------------------------------------------------------------------------- #

def _eval_single(cond: dict, store: "DataStore", symbol: str, default_interval: str) -> bool:
    ctype = cond.get("type", "")
    iv    = cond.get("interval", default_interval)

    # ── RSI ──────────────────────────────────────────────────────────────────
    if ctype == "rsi_below":
        df = _candles_df(store, symbol, iv, 35)
        if df is None:
            return False
        rsi = _rsi(df["c"], int(cond.get("period", 14)))
        return float(rsi.iloc[-1]) < float(cond["value"])

    if ctype == "rsi_above":
        df = _candles_df(store, symbol, iv, 35)
        if df is None:
            return False
        rsi = _rsi(df["c"], int(cond.get("period", 14)))
        return float(rsi.iloc[-1]) > float(cond["value"])

    if ctype == "rsi_recovering":
        df = _candles_df(store, symbol, iv, 35)
        if df is None:
            return False
        rsi = _rsi(df["c"], int(cond.get("period", 14)))
        return float(rsi.iloc[-1]) > float(rsi.iloc[-2])

    if ctype == "rsi_falling":
        df = _candles_df(store, symbol, iv, 35)
        if df is None:
            return False
        rsi = _rsi(df["c"], int(cond.get("period", 14)))
        return float(rsi.iloc[-1]) < float(rsi.iloc[-2])

    # ── Price level ───────────────────────────────────────────────────────────
    if ctype == "price_near_level":
        ticker = store.get_ticker(symbol)
        if not ticker:
            return False
        price  = float(ticker.get("price", 0))
        level  = float(cond["price"])
        tol    = level * float(cond.get("tol_pct", 0.5)) / 100
        return abs(price - level) <= tol

    if ctype == "price_breakout_above":
        df = _candles_df(store, symbol, iv, 5)
        if df is None or len(df) < 2:
            return False
        level = float(cond["price"])
        return float(df["c"].iloc[-2]) < level <= float(df["c"].iloc[-1])

    if ctype == "price_breakdown_below":
        df = _candles_df(store, symbol, iv, 5)
        if df is None or len(df) < 2:
            return False
        level = float(cond["price"])
        return float(df["c"].iloc[-2]) > level >= float(df["c"].iloc[-1])

    # ── EMA ───────────────────────────────────────────────────────────────────
    if ctype == "price_above_ema":
        period = int(cond.get("period", 50))
        df = _candles_df(store, symbol, iv, period + 15)
        if df is None:
            return False
        ema = df["c"].ewm(span=period, adjust=False).mean()
        return float(df["c"].iloc[-1]) > float(ema.iloc[-1])

    if ctype == "price_below_ema":
        period = int(cond.get("period", 50))
        df = _candles_df(store, symbol, iv, period + 15)
        if df is None:
            return False
        ema = df["c"].ewm(span=period, adjust=False).mean()
        return float(df["c"].iloc[-1]) < float(ema.iloc[-1])

    # ── Bollinger Bands ───────────────────────────────────────────────────────
    if ctype == "bollinger_squeeze":
        df = _candles_df(store, symbol, iv, 30)
        if df is None:
            return False
        period = int(cond.get("period", 20))
        sma = df["c"].rolling(period).mean()
        std = df["c"].rolling(period).std()
        bw  = (std * 4) / sma.replace(0, float("nan"))
        threshold = float(cond.get("threshold", 0.05))
        return float(bw.iloc[-1]) < threshold

    if ctype == "bollinger_expansion":
        df = _candles_df(store, symbol, iv, 30)
        if df is None:
            return False
        period = int(cond.get("period", 20))
        sma = df["c"].rolling(period).mean()
        std = df["c"].rolling(period).std()
        bw  = (std * 4) / sma.replace(0, float("nan"))
        threshold = float(cond.get("threshold", 0.08))
        return float(bw.iloc[-1]) > threshold

    # ── Volume spike ──────────────────────────────────────────────────────────
    if ctype == "volume_spike":
        df = _candles_df(store, symbol, iv, 25)
        if df is None or len(df) < 5:
            return False
        multiplier = float(cond.get("multiplier", 2.0))
        avg_vol = df["v"].iloc[:-1].mean()
        return float(df["v"].iloc[-1]) > avg_vol * multiplier

    # ── MACD ──────────────────────────────────────────────────────────────────
    if ctype == "macd_cross_bullish":
        df = _candles_df(store, symbol, iv, 60)
        if df is None or len(df) < 35:
            return False
        ema12  = df["c"].ewm(span=12, adjust=False).mean()
        ema26  = df["c"].ewm(span=26, adjust=False).mean()
        macd   = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        return (float(macd.iloc[-2]) < float(signal.iloc[-2]) and
                float(macd.iloc[-1]) > float(signal.iloc[-1]))

    if ctype == "macd_cross_bearish":
        df = _candles_df(store, symbol, iv, 60)
        if df is None or len(df) < 35:
            return False
        ema12  = df["c"].ewm(span=12, adjust=False).mean()
        ema26  = df["c"].ewm(span=26, adjust=False).mean()
        macd   = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        return (float(macd.iloc[-2]) > float(signal.iloc[-2]) and
                float(macd.iloc[-1]) < float(signal.iloc[-1]))

    # ── Funding rate ──────────────────────────────────────────────────────────
    if ctype == "funding_below":
        funding = store.get_funding(symbol)
        return funding is not None and float(funding) < float(cond["value"])

    if ctype == "funding_above":
        funding = store.get_funding(symbol)
        return funding is not None and float(funding) > float(cond["value"])

    # ── Candle patterns ───────────────────────────────────────────────────────
    if ctype == "candle_hammer":
        df = _candles_df(store, symbol, iv, 5)
        if df is None:
            return False
        r = df.iloc[-1]
        body        = abs(r["c"] - r["o"])
        lower_wick  = min(r["c"], r["o"]) - r["l"]
        upper_wick  = r["h"] - max(r["c"], r["o"])
        return lower_wick > body * 2 and upper_wick < body * 0.5

    if ctype == "candle_doji":
        df = _candles_df(store, symbol, iv, 5)
        if df is None:
            return False
        r    = df.iloc[-1]
        body = abs(r["c"] - r["o"])
        rng  = r["h"] - r["l"]
        return rng > 0 and body < rng * 0.1

    if ctype == "candle_engulfing_bullish":
        df = _candles_df(store, symbol, iv, 5)
        if df is None or len(df) < 2:
            return False
        prev, curr = df.iloc[-2], df.iloc[-1]
        return (prev["c"] < prev["o"] and curr["c"] > curr["o"] and
                curr["o"] <= prev["c"] and curr["c"] >= prev["o"])

    if ctype == "candle_engulfing_bearish":
        df = _candles_df(store, symbol, iv, 5)
        if df is None or len(df) < 2:
            return False
        prev, curr = df.iloc[-2], df.iloc[-1]
        return (prev["c"] > prev["o"] and curr["c"] < curr["o"] and
                curr["o"] >= prev["c"] and curr["c"] <= prev["o"])

    logger.debug("[ConditionEval] Unknown condition type: %s", ctype)
    return False
