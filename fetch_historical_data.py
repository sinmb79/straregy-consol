"""
Binance Futures 1h/4h 과거 데이터 다운로드 스크립트.
2024-01-01 ~ 현재까지 BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT
"""

from __future__ import annotations

import io
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

BASE_URL = "https://fapi.binance.com/fapi/v1/klines"
SYMBOLS   = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
INTERVALS = ["1h", "4h"]
LIMIT     = 1500

START_DT = datetime(2024, 1, 1, tzinfo=timezone.utc)
END_DT   = datetime.now(tz=timezone.utc)

OUT_ROOT = Path("D:/workspace/blockchain-tranding/data/import_staging/validation_datasets")


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list:
    """Download all klines from start_ms to end_ms, handling pagination."""
    all_bars = []
    cur_start = start_ms

    while cur_start < end_ms:
        params = {
            "symbol":    symbol,
            "interval":  interval,
            "limit":     LIMIT,
            "startTime": cur_start,
            "endTime":   end_ms,
        }
        try:
            r = requests.get(BASE_URL, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  ERROR fetching {symbol} {interval}: {e}", flush=True)
            time.sleep(2.0)
            continue

        if not data:
            break

        all_bars.extend(data)
        last_open_ms = int(data[-1][0])
        next_start   = last_open_ms + _interval_ms(interval)

        if next_start <= cur_start:
            break
        cur_start = next_start

        # Rate limit respect
        time.sleep(0.15)

    return all_bars


def _interval_ms(interval: str) -> int:
    if interval == "1h":
        return 3_600_000
    if interval == "4h":
        return 14_400_000
    if interval == "1d":
        return 86_400_000
    return 3_600_000


def klines_to_bars(raw: list) -> list:
    """Convert Binance kline tuples to our bar format."""
    bars = []
    for row in raw:
        open_time_ms = int(row[0])
        dt = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc).isoformat()
        bars.append({
            "open_time": dt,
            "open":   float(row[1]),
            "high":   float(row[2]),
            "low":    float(row[3]),
            "close":  float(row[4]),
            "volume": float(row[5]),
        })
    return bars


def main():
    start_ms = int(START_DT.timestamp() * 1000)
    end_ms   = int(END_DT.timestamp() * 1000)

    print(f"\nBinance Futures 과거 데이터 다운로드")
    print(f"기간: {START_DT.strftime('%Y-%m-%d')} ~ {END_DT.strftime('%Y-%m-%d')}")
    print(f"심볼: {SYMBOLS}")
    print(f"타임프레임: {INTERVALS}\n")

    for symbol in SYMBOLS:
        for interval in INTERVALS:
            print(f"[{symbol} {interval}] 다운로드 중...", end=" ", flush=True)

            raw = fetch_klines(symbol, interval, start_ms, end_ms)
            if not raw:
                print("데이터 없음!")
                continue

            bars = klines_to_bars(raw)
            # Deduplicate
            seen: set = set()
            deduped = []
            for b in bars:
                if b["open_time"] not in seen:
                    seen.add(b["open_time"])
                    deduped.append(b)
            bars = sorted(deduped, key=lambda b: b["open_time"])

            # Save
            sym_dir  = OUT_ROOT / symbol
            sym_dir.mkdir(parents=True, exist_ok=True)
            out_file = sym_dir / f"{interval}-{len(bars)}.json"

            payload = {
                "symbol":   symbol,
                "interval": interval,
                "bars":     bars,
            }
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(payload, f)

            first_dt = bars[0]["open_time"][:10]
            last_dt  = bars[-1]["open_time"][:10]
            print(f"{len(bars)} bars  [{first_dt} ~ {last_dt}]  => {out_file.name}")

    print("\n완료!")


if __name__ == "__main__":
    main()
