"""
SQLite schema definition and initialisation for the 22B Strategy Engine.
All tables follow the master plan Part 13 specification.
"""

import sqlite3
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# DDL statements
# --------------------------------------------------------------------------- #

DDL_CANDLES = """
CREATE TABLE IF NOT EXISTS candles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    interval    TEXT    NOT NULL,       -- e.g. '1h', '4h'
    ts          INTEGER NOT NULL,       -- open-time unix ms
    o           REAL    NOT NULL,
    h           REAL    NOT NULL,
    l           REAL    NOT NULL,
    c           REAL    NOT NULL,
    v           REAL    NOT NULL,
    UNIQUE(symbol, interval, ts)
);
"""

DDL_CANDLES_IDX = """
CREATE INDEX IF NOT EXISTS idx_candles_symbol_interval_ts
    ON candles (symbol, interval, ts DESC);
"""

DDL_TICKERS = """
CREATE TABLE IF NOT EXISTS tickers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    ts          INTEGER NOT NULL,
    price       REAL    NOT NULL,
    volume_24h  REAL,
    change_pct  REAL,
    UNIQUE(symbol, ts)
);
"""

DDL_TICKERS_IDX = """
CREATE INDEX IF NOT EXISTS idx_tickers_symbol_ts
    ON tickers (symbol, ts DESC);
"""

DDL_REGIMES = """
CREATE TABLE IF NOT EXISTS regimes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    regime      TEXT    NOT NULL,       -- BTC_BULLISH | BTC_BEARISH | etc.
    btc_ema50   REAL,
    btc_price   REAL,
    btc_atr     REAL,
    btc_atr_pct REAL,
    btc_ret_24h REAL,
    btc_ret_1h  REAL,
    funding     REAL,
    oi_change   REAL,
    bb_bw       REAL,                   -- Bollinger Bandwidth
    btc_dom_chg REAL,                   -- BTC.D 24h change
    alt_vol_ratio REAL,
    raw_json    TEXT                    -- full indicator snapshot as JSON
);
"""

DDL_REGIMES_IDX = """
CREATE INDEX IF NOT EXISTS idx_regimes_ts ON regimes (ts DESC);
"""

DDL_SIGNALS = """
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    strategy    TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    action      TEXT    NOT NULL,       -- BUY | SELL | CLOSE
    mode        TEXT    NOT NULL,       -- LIVE | SHADOW | BACKTEST
    confidence  REAL,
    regime      TEXT,
    tp          REAL,
    sl          REAL,
    reason      TEXT
);
"""

DDL_ORDERS = """
CREATE TABLE IF NOT EXISTS orders (
    id               TEXT    PRIMARY KEY,            -- UUID4 (internal)
    binance_order_id TEXT,                           -- Binance order ID
    signal_id        TEXT,                           -- Signal UUID
    ts               INTEGER NOT NULL,
    symbol           TEXT    NOT NULL,
    side             TEXT    NOT NULL,               -- BUY | SELL
    type             TEXT    NOT NULL,               -- MARKET | STOP_MARKET | TAKE_PROFIT_MARKET
    qty              REAL    NOT NULL,
    price            REAL,
    status           TEXT    NOT NULL,               -- state machine status
    filled_qty       REAL    DEFAULT 0,
    filled_price     REAL,
    fee              REAL    DEFAULT 0,
    strategy         TEXT,
    regime           TEXT,
    partial_fill     INTEGER DEFAULT 0,
    close_reason     TEXT,
    closed_at        INTEGER,
    error            TEXT
);
"""

DDL_POSITIONS = """
CREATE TABLE IF NOT EXISTS positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER REFERENCES orders(id),
    symbol      TEXT    NOT NULL,
    entry_price REAL    NOT NULL,
    qty         REAL    NOT NULL,
    sl          REAL,
    tp          REAL,
    highest     REAL,
    status      TEXT    NOT NULL,       -- OPEN | CLOSED | LIQUIDATED
    opened_at   INTEGER NOT NULL,
    closed_at   INTEGER,
    pnl_pct     REAL
);
"""

DDL_AUDIT_TRAIL = """
CREATE TABLE IF NOT EXISTS audit_trail (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        TEXT,               -- internal UUID
    signal_id       TEXT,               -- signal UUID
    strategy        TEXT,
    regime_snapshot TEXT,               -- JSON
    risk_check      TEXT,               -- JSON (includes from/to status, risk result, params)
    decision_reason TEXT,
    ts              INTEGER NOT NULL
);
"""

DDL_AUDIT_TRAIL_IDX = """
CREATE INDEX IF NOT EXISTS idx_audit_trail_order_id
    ON audit_trail (order_id, ts ASC);
"""

DDL_REVIEWS = """
CREATE TABLE IF NOT EXISTS reviews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,
    type            TEXT    NOT NULL,   -- daily | weekly
    content         TEXT,
    recommendations TEXT
);
"""

DDL_STRATEGY_STATE = """
CREATE TABLE IF NOT EXISTS strategy_state (
    name            TEXT    PRIMARY KEY,
    mode            TEXT    NOT NULL,   -- LIVE | SHADOW | PAUSED
    category        TEXT,
    regime_filter   TEXT,               -- JSON list of allowed regimes
    stats_json      TEXT,               -- performance stats as JSON
    last_signal_ts  INTEGER,
    lifecycle_stage TEXT                -- stat | shadow_live | execution_validated
);
"""

DDL_PAPER_POSITIONS = """
CREATE TABLE IF NOT EXISTS paper_positions (
    id            TEXT    PRIMARY KEY,  -- UUID4
    strategy      TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    side          TEXT    NOT NULL,     -- LONG | SHORT
    entry_price   REAL    NOT NULL,
    qty           REAL    NOT NULL DEFAULT 1.0,
    tp            REAL,
    sl            REAL,
    opened_at     INTEGER NOT NULL,
    regime        TEXT,
    signal_id     TEXT,
    status        TEXT    NOT NULL DEFAULT 'OPEN',   -- OPEN | CLOSED
    closed_at     INTEGER,
    exit_price    REAL,
    pnl_pct       REAL,
    close_reason  TEXT
);
"""

DDL_PAPER_POSITIONS_IDX = """
CREATE INDEX IF NOT EXISTS idx_paper_positions_strategy
    ON paper_positions (strategy, status, opened_at DESC);
"""

DDL_RECOMMENDATIONS = """
CREATE TABLE IF NOT EXISTS recommendations (
    id                TEXT    PRIMARY KEY,
    ts                INTEGER NOT NULL,
    type              TEXT    NOT NULL,        -- PROMOTE / DEMOTE / MODIFY / RETIRE
    strategy          TEXT    NOT NULL,
    current_mode      TEXT,
    proposed_mode     TEXT,
    supporting_data   TEXT,                    -- JSON
    counter_arguments TEXT,                    -- JSON array
    expected_risk     TEXT,                    -- JSON
    validity_days     INTEGER DEFAULT 7,
    rollback_condition TEXT,
    status            TEXT    DEFAULT 'PENDING',  -- PENDING / APPROVED / REJECTED / DEFERRED
    created_at        INTEGER,
    decided_at        INTEGER,
    decided_by        TEXT,
    decision_reason   TEXT
);
"""

DDL_RECOMMENDATIONS_IDX = """
CREATE INDEX IF NOT EXISTS idx_recommendations_status_ts
    ON recommendations (status, created_at DESC);
"""

DDL_OPPORTUNITIES = """
CREATE TABLE IF NOT EXISTS opportunities (
    id                  TEXT    PRIMARY KEY,
    ts                  INTEGER NOT NULL,
    symbol              TEXT    NOT NULL,
    side                TEXT    NOT NULL,
    source_strategy     TEXT    NOT NULL,
    category            TEXT,
    regime_snapshot     TEXT,
    signal_strength     REAL,
    funding_state       TEXT,
    oi_state            TEXT,
    volume_state        TEXT,
    spread_state        TEXT,
    volatility_state    TEXT,
    liquidity_state     TEXT,
    expected_hold_window TEXT,
    invalidation_level  REAL,
    confidence_raw      REAL,
    score_total         INTEGER,
    score_breakdown_json TEXT,
    rank_global         INTEGER,
    rank_within_symbol  INTEGER,
    execution_status    TEXT    DEFAULT 'PENDING',
    approved_by         TEXT,
    approved_at         INTEGER,
    tp                  REAL,
    sl                  REAL,
    signal_id           TEXT
);
"""

DDL_OPPORTUNITIES_IDX = """
CREATE INDEX IF NOT EXISTS idx_opportunities_ts
    ON opportunities (ts DESC);
CREATE INDEX IF NOT EXISTS idx_opportunities_status
    ON opportunities (execution_status, ts DESC);
"""

DDL_OPERATOR_ACTIONS = """
CREATE TABLE IF NOT EXISTS operator_actions (
    id          TEXT    PRIMARY KEY,
    ts          INTEGER NOT NULL,
    source      TEXT    NOT NULL,   -- telegram | dashboard
    operator    TEXT,
    action_type TEXT    NOT NULL,
    target_type TEXT,
    target_id   TEXT,
    reason      TEXT,
    result      TEXT
);
"""

DDL_OPERATOR_ACTIONS_IDX = """
CREATE INDEX IF NOT EXISTS idx_operator_actions_ts
    ON operator_actions (ts DESC);
"""

DDL_TELEGRAM_EVENTS = """
CREATE TABLE IF NOT EXISTS telegram_events (
    id                  TEXT    PRIMARY KEY,
    ts                  INTEGER NOT NULL,
    chat_id             TEXT,
    event_type          TEXT    NOT NULL,
    payload_json        TEXT,
    related_entity_type TEXT,
    related_entity_id   TEXT,
    action_status       TEXT    DEFAULT 'PENDING',
    ack_by              TEXT,
    ack_ts              INTEGER
);
"""

DDL_IMAGE_PATTERNS = """
CREATE TABLE IF NOT EXISTS image_patterns (
    id                TEXT    PRIMARY KEY,          -- UUID4
    created_at        INTEGER NOT NULL,
    pattern_name      TEXT    NOT NULL,
    description       TEXT,
    symbol            TEXT    NOT NULL DEFAULT 'ALL',
    interval          TEXT    NOT NULL DEFAULT '1h',
    direction         TEXT    NOT NULL DEFAULT 'LONG',  -- LONG | SHORT
    conditions_json   TEXT    NOT NULL DEFAULT '[]',    -- JSON list of condition dicts
    conditions_logic  TEXT    NOT NULL DEFAULT 'AND',   -- AND | OR
    tp_pct            REAL    NOT NULL DEFAULT 3.0,
    sl_pct            REAL    NOT NULL DEFAULT 1.5,
    regime_filter_json TEXT,                            -- JSON list of regime strings (NULL = all)
    min_confidence    REAL    NOT NULL DEFAULT 0.65,
    cooldown_hours    REAL    NOT NULL DEFAULT 4.0,
    enabled           INTEGER NOT NULL DEFAULT 1,
    last_signal_ts    INTEGER,                          -- last time a signal was fired
    image_b64         TEXT,                             -- base64 thumbnail (optional)
    ai_warnings_json  TEXT,                             -- JSON list of AI warnings
    confidence_note   TEXT                              -- AI confidence explanation
);
"""

DDL_IMAGE_PATTERNS_IDX = """
CREATE INDEX IF NOT EXISTS idx_image_patterns_enabled
    ON image_patterns (enabled, created_at DESC);
"""

ALL_DDL = [
    DDL_CANDLES, DDL_CANDLES_IDX,
    DDL_TICKERS, DDL_TICKERS_IDX,
    DDL_REGIMES, DDL_REGIMES_IDX,
    DDL_SIGNALS,
    DDL_ORDERS,
    DDL_POSITIONS,
    DDL_AUDIT_TRAIL, DDL_AUDIT_TRAIL_IDX,
    DDL_REVIEWS,
    DDL_STRATEGY_STATE,
    DDL_PAPER_POSITIONS, DDL_PAPER_POSITIONS_IDX,
    DDL_RECOMMENDATIONS, DDL_RECOMMENDATIONS_IDX,
    DDL_OPPORTUNITIES, DDL_OPPORTUNITIES_IDX,
    DDL_OPERATOR_ACTIONS, DDL_OPERATOR_ACTIONS_IDX,
    DDL_TELEGRAM_EVENTS,
    DDL_IMAGE_PATTERNS, DDL_IMAGE_PATTERNS_IDX,
]

# Phase 3 migration: add new columns to existing tables
# Safe to run on any DB version — ALTER TABLE IF NOT EXISTS columns
V13_MIGRATIONS = [
    # signals 테이블 확장
    "ALTER TABLE signals ADD COLUMN normalized_category TEXT",
    "ALTER TABLE signals ADD COLUMN score_total INTEGER",
    "ALTER TABLE signals ADD COLUMN score_breakdown_json TEXT",
    "ALTER TABLE signals ADD COLUMN opportunity_id TEXT",
    # strategy_state 확장
    "ALTER TABLE strategy_state ADD COLUMN recent_10_pf REAL",
    "ALTER TABLE strategy_state ADD COLUMN recent_20_pf REAL",
    "ALTER TABLE strategy_state ADD COLUMN recent_expectancy REAL",
    "ALTER TABLE strategy_state ADD COLUMN recent_mdd REAL",
    "ALTER TABLE strategy_state ADD COLUMN health_status TEXT DEFAULT 'OK'",
    "ALTER TABLE strategy_state ADD COLUMN live_eligibility INTEGER DEFAULT 0",
    "ALTER TABLE strategy_state ADD COLUMN last_pause_reason TEXT",
    "ALTER TABLE strategy_state ADD COLUMN last_health_update_ts INTEGER",
]

PHASE3_MIGRATIONS = [
    "ALTER TABLE orders ADD COLUMN binance_order_id TEXT",
    "ALTER TABLE orders ADD COLUMN strategy TEXT",
    "ALTER TABLE orders ADD COLUMN regime TEXT",
    "ALTER TABLE orders ADD COLUMN partial_fill INTEGER DEFAULT 0",
    "ALTER TABLE orders ADD COLUMN close_reason TEXT",
    "ALTER TABLE orders ADD COLUMN closed_at INTEGER",
    "ALTER TABLE orders ADD COLUMN error TEXT",
]

# v1.4 migrations — Time Stop + Break-even SL
V14_MIGRATIONS = [
    "ALTER TABLE paper_positions ADD COLUMN expiry_ts INTEGER",
    "ALTER TABLE paper_positions ADD COLUMN time_stop_policy TEXT",
    "ALTER TABLE paper_positions ADD COLUMN be_stop_activated INTEGER DEFAULT 0",
    # image_patterns.enabled may be missing on DBs created before this column was added
    "ALTER TABLE image_patterns ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1",
]


# --------------------------------------------------------------------------- #
# Public helpers
# --------------------------------------------------------------------------- #

def get_connection(db_path: str) -> sqlite3.Connection:
    """Return a SQLite connection with WAL mode and row_factory set."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(db_path: str) -> sqlite3.Connection:
    """Create all tables (if not exists) and return connection."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(db_path)
    cursor = conn.cursor()
    for ddl in ALL_DDL:
        cursor.executescript(ddl)
    conn.commit()

    # image_patterns table (may not exist on older DBs — safe to run DDL again)
    try:
        cursor.executescript(DDL_IMAGE_PATTERNS)
        cursor.executescript(DDL_IMAGE_PATTERNS_IDX)
        conn.commit()
    except Exception:
        pass

    # Phase 3 + v1.3 + v1.4 migrations (safe — ignore errors for columns that already exist)
    for migration in PHASE3_MIGRATIONS + V13_MIGRATIONS + V14_MIGRATIONS:
        try:
            cursor.execute(migration)
            conn.commit()
        except Exception:
            pass  # Column already exists — safe to ignore

    logger.info("Database initialised at %s", db_path)
    return conn
