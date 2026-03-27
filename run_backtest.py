"""
Standalone backtest / strategy validation runner.

사용법:
    python run_backtest.py

결과:
    - 전략별 Win Rate / Profit Factor / MDD / Expectancy
    - 전체 포트폴리오 요약
    - 실전 투입 추천 전략 목록
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List

# Force UTF-8 output on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Setup ────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.WARNING,   # 백테스트 중 불필요한 로그 억제
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# 핵심 로거만 INFO로
for lg_name in ("__main__", "bot.strategies.paper_recorder", "bot.data.replay_account"):
    logging.getLogger(lg_name).setLevel(logging.INFO)

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
DATASET_ROOT = PROJECT_ROOT / "data" / "import_staging" / "validation_datasets"
WARMUP_BARS   = 52           # 전략 지표 계산에 필요한 최소 워밍업 바
INITIAL_BALANCE = 10_000.0   # 가상 자본 ($)

# LIVE 승격 기준 (strategy_health.py 와 동일)
PAPER_TO_SHADOW = {"min_signals": 30, "min_pf": 1.2, "max_mdd": -10.0}
SHADOW_TO_LIVE  = {"min_signals": 50, "min_pf": 1.5, "min_wr": 0.40, "max_mdd": -8.0}


# ── DB helper — in-memory SQLite for backtest ────────────────────────────────
def _create_backtest_db() -> sqlite3.Connection:
    """Create a minimal in-memory DB that matches the schema DataStore needs."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    # Minimal schema — same as production
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS candles (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol   TEXT NOT NULL,
            interval TEXT NOT NULL,
            ts       INTEGER NOT NULL,
            o        REAL, h REAL, l REAL, c REAL, v REAL,
            UNIQUE(symbol, interval, ts)
        );
        CREATE TABLE IF NOT EXISTS tickers (
            symbol      TEXT PRIMARY KEY,
            ts          INTEGER,
            price       REAL,
            volume_24h  REAL,
            change_pct  REAL,
            source      TEXT
        );
        CREATE TABLE IF NOT EXISTS regimes (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      INTEGER,
            regime  TEXT,
            payload TEXT
        );
        CREATE TABLE IF NOT EXISTS signals (
            id       TEXT PRIMARY KEY,
            ts       INTEGER,
            strategy TEXT,
            symbol   TEXT,
            action   TEXT,
            mode     TEXT,
            confidence REAL,
            regime   TEXT,
            reason   TEXT,
            tp       REAL,
            sl       REAL
        );
        CREATE TABLE IF NOT EXISTS paper_positions (
            id               TEXT PRIMARY KEY,
            strategy         TEXT,
            symbol           TEXT,
            side             TEXT,
            entry_price      REAL,
            qty              REAL,
            tp               REAL,
            sl               REAL,
            opened_at        INTEGER,
            regime           TEXT,
            signal_id        TEXT,
            expiry_ts        INTEGER,
            time_stop_policy TEXT,
            status           TEXT DEFAULT 'OPEN',
            closed_at        INTEGER,
            exit_price       REAL,
            pnl_pct          REAL,
            close_reason     TEXT DEFAULT '',
            be_stop_activated INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS strategy_state (
            name                  TEXT PRIMARY KEY,
            mode                  TEXT DEFAULT 'PAPER',
            category              TEXT,
            regime_filter         TEXT,
            stats_json            TEXT DEFAULT '{}',
            last_signal_ts        INTEGER,
            lifecycle_stage       TEXT DEFAULT 'paper',
            recent_10_pf          REAL,
            recent_20_pf          REAL,
            recent_expectancy     REAL,
            recent_mdd            REAL,
            health_status         TEXT DEFAULT 'OK',
            live_eligibility      TEXT,
            last_pause_reason     TEXT,
            last_health_update_ts INTEGER,
            params_json           TEXT DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS opportunities (
            id                   TEXT PRIMARY KEY,
            ts                   INTEGER,
            symbol               TEXT,
            side                 TEXT,
            source_strategy      TEXT,
            category             TEXT,
            regime_snapshot      TEXT,
            signal_id            TEXT,
            signal_strength      REAL,
            funding_state        TEXT,
            oi_state             TEXT,
            volume_state         TEXT,
            spread_state         TEXT,
            volatility_state     TEXT,
            liquidity_state      TEXT,
            expected_hold_window TEXT,
            invalidation_level   REAL,
            confidence_raw       REAL,
            score_total          INTEGER,
            score_breakdown_json TEXT,
            rank_global          INTEGER,
            rank_within_symbol   INTEGER,
            execution_status     TEXT DEFAULT 'PENDING',
            approved_by          TEXT,
            approved_at          INTEGER,
            tp                   REAL,
            sl                   REAL
        );
        CREATE TABLE IF NOT EXISTS image_patterns (
            id         TEXT PRIMARY KEY,
            ts         INTEGER,
            symbol     TEXT,
            pattern    TEXT,
            direction  TEXT,
            confidence REAL,
            status     TEXT DEFAULT 'ACTIVE'
        );
        CREATE TABLE IF NOT EXISTS audit_trail (
            id TEXT PRIMARY KEY,
            ts INTEGER,
            event_type TEXT,
            payload TEXT
        );
        CREATE TABLE IF NOT EXISTS operator_actions (
            id TEXT PRIMARY KEY,
            ts INTEGER,
            action_type TEXT,
            payload TEXT
        );
    """)
    conn.commit()
    return conn


# ── Dataset loader (reads JSON files we exported) ────────────────────────────
def _load_datasets(root: Path, warmup_bars: int):
    """Load JSON datasets; keep only the largest file per (symbol, interval)."""
    from bot.data.validation_dataset_loader import ValidationReplayDataset

    # Collect all candidate files
    candidates: dict = {}  # (symbol, interval) -> (bar_count, path, payload)
    for json_file in sorted(root.glob("*/*.json")):
        with json_file.open(encoding="utf-8") as f:
            payload = json.load(f)
        symbol   = str(payload.get("symbol", json_file.parent.name)).upper()
        interval = str(payload.get("interval", json_file.stem.split("-")[0]))
        raw_bars = payload.get("bars", [])
        key = (symbol, interval)
        if key not in candidates or len(raw_bars) > candidates[key][0]:
            candidates[key] = (len(raw_bars), json_file, payload)

    datasets: List[ValidationReplayDataset] = []
    for (symbol, interval), (bar_count, json_file, payload) in sorted(candidates.items()):
        raw_bars = payload.get("bars", [])
        bars = []
        for bar in raw_bars:
            open_time = bar["open_time"]
            ts = int(datetime.fromisoformat(open_time).timestamp() * 1000)
            bars.append({
                "ts": ts,
                "o": float(bar["open"]),
                "h": float(bar["high"]),
                "l": float(bar["low"]),
                "c": float(bar["close"]),
                "v": float(bar["volume"]),
            })
        bars.sort(key=lambda b: b["ts"])

        ds = ValidationReplayDataset(symbol=symbol, interval=interval, bars=bars)
        datasets.append(ds)
        print(f"  Loaded {symbol} {interval}: {len(bars)} bars  ({json_file.name})")

    return datasets


# ── Timeline builder (ordered by ts) ────────────────────────────────────────
def _build_timeline(datasets, warmup_bars: int) -> list:
    """Skip first warmup_bars per dataset; sort remaining by ts."""
    from bot.data.validation_replay import ReplayBar

    timeline = []
    for ds in datasets:
        start = min(warmup_bars, len(ds.bars))
        for candle in ds.bars[start:]:
            timeline.append(ReplayBar(
                symbol=ds.symbol,
                interval=ds.interval,
                candle=candle,
                step_index=0,
                total_steps=0,
            ))

    timeline.sort(key=lambda b: (b.candle["ts"], b.interval, b.symbol))
    for i, item in enumerate(timeline, 1):
        item.step_index = i
        item.total_steps = len(timeline)
    return timeline


# ── Prime warmup bars into DataStore ────────────────────────────────────────
async def _prime_warmup(store, datasets, warmup_bars: int) -> None:
    """Load the first warmup_bars candles into the store so strategies have data."""
    for ds in datasets:
        warmup = ds.bars[:warmup_bars]
        for candle in warmup:
            await store.upsert_candle(ds.symbol, ds.interval, candle)
        if warmup:
            last = warmup[-1]
            await store.update_ticker(
                ds.symbol,
                {
                    "symbol": ds.symbol,
                    "ts": last["ts"],
                    "price": last["c"],
                    "volume_24h": last["v"],
                    "change_pct": 0.0,
                    "source": "backtest_warmup",
                },
            )


# ── Main backtest loop ───────────────────────────────────────────────────────
async def run_backtest():
    print("\n" + "=" * 65)
    print("  22B 전략 백테스트 — Strategy Validation Runner")
    print("=" * 65)

    # 1. Load datasets
    print("\n[1] 데이터셋 로드...")
    datasets = _load_datasets(DATASET_ROOT, WARMUP_BARS)
    if not datasets:
        print("  ERROR: 데이터셋이 없습니다. run_backtest.py 먼저 export하세요.")
        return

    # 2. Setup in-memory DataStore
    print("\n[2] 백테스트 DataStore 초기화...")
    conn = _create_backtest_db()
    from bot.data.store import DataStore
    store = DataStore(conn)
    # Restore candle history from DB (no-op for :memory:, just primes cache)

    # 3. Warmup
    print(f"\n[3] 워밍업 ({WARMUP_BARS} bars per symbol/interval)...")
    await _prime_warmup(store, datasets, WARMUP_BARS)

    # 4. Setup strategy manager + paper recorder + replay account
    print("\n[4] 전략 엔진 초기화...")
    from bot.strategies.manager import StrategyManager
    from bot.data.replay_account import ReplayAccount

    replay_account = ReplayAccount(
        initial_balance=INITIAL_BALANCE,
        position_size_pct=0.10,
        fee_rate=0.0004,
        slippage_pct=0.0005,
    )

    manager = StrategyManager(store)
    manager.initialize()

    # Inject replay account into paper recorder
    manager.recorder._replay_account = replay_account

    # 5. Setup regime detector
    from bot.regime.detector import RegimeDetector
    detector = RegimeDetector(store)

    # 6. Build replay timeline
    timeline = _build_timeline(datasets, WARMUP_BARS)
    print(f"\n[5] 리플레이 타임라인: {len(timeline)} 스텝")

    # 7. Bar-by-bar replay
    print("\n[6] 백테스트 실행 중...\n")

    bar_count = 0
    signal_count = 0
    # Track regime distribution
    regime_counts: dict = {}

    # We run strategy only on 1h bars (1h is the primary signal timeframe)
    # 4h bars update the store but don't trigger strategy evaluation
    SIGNAL_INTERVAL = "1h"

    for bar in timeline:
        # Feed candle to store
        await store.upsert_candle(bar.symbol, bar.interval, bar.candle)
        await store.update_ticker(
            bar.symbol,
            {
                "symbol": bar.symbol,
                "ts": bar.candle["ts"],
                "price": bar.candle["c"],
                "volume_24h": bar.candle["v"],
                "change_pct": 0.0,
                "source": "backtest",
            },
        )

        # Inject replay timestamp into paper recorder
        manager.recorder.set_replay_ts(bar.candle["ts"])

        # Run strategies only on 1h bars to avoid duplicate signals
        if bar.interval == SIGNAL_INTERVAL:
            bar_count += 1

            # Detect regime
            regime = detector.detect()
            regime_counts[regime.get("regime", "UNKNOWN")] = (
                regime_counts.get(regime.get("regime", "UNKNOWN"), 0) + 1
            )

            # Run all strategies
            signals = manager.run_all(regime)
            signal_count += len([s for s in signals if s.action != "SKIP"])

            # Progress indicator
            if bar_count % 50 == 0:
                ts_dt = datetime.fromtimestamp(
                    bar.candle["ts"] / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d")
                open_pos = manager.recorder.open_count() if hasattr(manager.recorder, 'open_count') else len(manager.recorder._open)
                trades_done = len(replay_account.trades)
                balance = replay_account.balance
                print(
                    f"  [{bar_count:4d}/{len([b for b in timeline if b.interval==SIGNAL_INTERVAL])}]"
                    f"  {ts_dt}  regime={regime.get('regime','?'):18s}"
                    f"  trades={trades_done:3d}  open={open_pos}  balance=${balance:,.2f}"
                )

    print("\n[7] 백테스트 완료. 결과 집계 중...")

    # 8. Force-close all remaining open positions at last known price
    for pos in list(manager.recorder._open.values()):
        ticker = store.get_ticker(pos.symbol)
        if ticker:
            price = float(ticker.get("price", pos.entry_price))
        else:
            price = pos.entry_price
        manager.recorder._close_position(pos, price, "BACKTEST_END")

    # 9. Compute metrics
    metrics = replay_account.compute_metrics()

    # ── Report ───────────────────────────────────────────────────────────── #
    print("\n" + "=" * 65)
    print("  백테스트 결과 요약")
    print("=" * 65)
    print(f"  총 1h 바 처리:     {bar_count}")
    print(f"  총 신호 수:        {signal_count}")
    print(f"  총 거래 수:        {metrics.get('trade_count', 0)}")
    print(f"  초기 자본:         ${metrics.get('initial_balance', 0):,.2f}")
    print(f"  최종 자본:         ${metrics.get('final_balance', 0):,.2f}")
    pnl = metrics.get('total_pnl_usdt', 0)
    ret = metrics.get('total_return_pct', 0)
    print(f"  총 수익:           ${pnl:+,.2f}  ({ret:+.2f}%)")
    print(f"  승률:              {metrics.get('win_rate', 0)*100:.1f}%")
    print(f"  Profit Factor:    {metrics.get('profit_factor') or 'N/A'}")
    print(f"  기대값(per trade): ${metrics.get('expectancy_usdt', 0):+.2f}")
    print(f"  최대낙폭 (MDD):    {metrics.get('mdd_pct', 0):+.2f}%")
    print(f"  Sharpe Ratio:     {metrics.get('sharpe_ratio', 0):.3f}")
    print(f"  평균 보유시간:     {metrics.get('avg_duration_hours', 0):.1f}h")
    fee = metrics.get('total_fee_usdt', 0)
    slip = metrics.get('total_slippage_usdt', 0)
    print(f"  총 수수료:         ${fee:,.4f}")
    print(f"  총 슬리피지:       ${slip:,.4f}")

    print("\n  ── 레짐 분포 ─────────────────────────────────────────────")
    total_bars = sum(regime_counts.values())
    for r, cnt in sorted(regime_counts.items(), key=lambda x: -x[1]):
        print(f"  {r:20s} {cnt:4d} bars ({cnt/total_bars*100:4.1f}%)")

    print("\n" + "=" * 65)
    print("  전략별 성과")
    print("=" * 65)
    per_strat = metrics.get("per_strategy", {})
    if not per_strat:
        print("  (거래 없음 — 신호가 발생하지 않았습니다)")
    else:
        print(f"  {'전략명':<35} {'거래':>5} {'승률':>7} {'PF':>7} {'PnL($)':>10}")
        print("  " + "-" * 67)
        for sname, sm in sorted(per_strat.items(), key=lambda x: -(x[1].get("total_pnl_usdt") or 0)):
            pf_str = f"{sm['profit_factor']:.2f}" if sm.get("profit_factor") else "N/A"
            print(
                f"  {sname:<35} {sm['trade_count']:>5}"
                f"  {sm['win_rate']*100:>5.1f}%  {pf_str:>6}  ${sm['total_pnl_usdt']:>+9.2f}"
            )

    print("\n" + "=" * 65)
    print("  실전 투입 판단 (PAPER→SHADOW→LIVE 기준)")
    print("=" * 65)
    promote_shadow = []
    promote_live   = []
    watch_list     = []

    for sname, sm in per_strat.items():
        n  = sm["trade_count"]
        wr = sm["win_rate"]
        pf = sm.get("profit_factor") or 0
        # MDD는 전략별로 별도 계산 안 함 (ReplayAccount가 전체 MDD만 추적)
        # 간이 판단: PF + WR + 신호 수로만 평가

        shadow_ok = (
            n  >= PAPER_TO_SHADOW["min_signals"] and
            pf >= PAPER_TO_SHADOW["min_pf"]
        )
        live_ok = (
            n  >= SHADOW_TO_LIVE["min_signals"] and
            pf >= SHADOW_TO_LIVE["min_pf"] and
            wr >= SHADOW_TO_LIVE["min_wr"]
        )

        if live_ok:
            promote_live.append(sname)
        elif shadow_ok:
            promote_shadow.append(sname)
        elif n >= 5 and pf >= 1.0:
            watch_list.append(sname)

    if promote_live:
        print(f"\n  [LIVE 승격 후보]")
        for s in promote_live:
            print(f"    ✓ {s}")
    if promote_shadow:
        print(f"\n  [SHADOW 승격 후보]")
        for s in promote_shadow:
            print(f"    ○ {s}")
    if watch_list:
        print(f"\n  [관찰 (데이터 축적 필요)]")
        for s in watch_list:
            print(f"    · {s}")
    if not (promote_live or promote_shadow or watch_list):
        print("\n  현재 기준 승격 조건 미충족 — 데이터 축적 후 재평가 필요")
        print("  (4개월 데이터는 전략당 신호가 충분하지 않을 수 있음)")

    print("\n  ※ 참고: 본 백테스트는 2025-11~2026-03 구간 데이터 기반.")
    print("         신호 수가 기준(30/50개) 미달 시 더 많은 데이터 필요.")
    print("=" * 65 + "\n")

    return metrics


if __name__ == "__main__":
    asyncio.run(run_backtest())
