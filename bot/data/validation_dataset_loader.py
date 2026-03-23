"""
Validation dataset loader for offline/backtest/paper workflows.

Loads staged candle datasets from:
  data/import_staging/validation_datasets/<SYMBOL>/<INTERVAL>-<LIMIT>.json

Safety properties:
- Read-only source ingestion (never writes back into staging files)
- Only populates DataStore candle/ticker caches + SQLite candle history
- Intended for offline/backtest/paper use only
- Does not place orders or enable any live exchange connectivity
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from bot.data.store import DataStore

logger = logging.getLogger(__name__)


@dataclass
class ValidationReplayDataset:
    symbol: str
    interval: str
    bars: List[dict] = field(default_factory=list)


@dataclass
class ValidationLoadSummary:
    dataset_root: str
    files_loaded: int = 0
    candles_loaded: int = 0
    symbols_loaded: int = 0
    intervals_loaded: int = 0
    warmup_bars_loaded: int = 0
    replay_bars_remaining: int = 0


class ValidationDatasetLoader:
    """Load staged validation datasets into the DataStore for offline use."""

    def __init__(self, store: DataStore, dataset_root: str) -> None:
        self._store = store
        self._root = Path(dataset_root).expanduser().resolve()
        self._datasets: Dict[tuple[str, str], ValidationReplayDataset] = {}

    async def load(self, warmup_bars: Optional[int] = None) -> ValidationLoadSummary:
        if not self._root.exists():
            raise FileNotFoundError(f"Validation dataset root does not exist: {self._root}")
        if not self._root.is_dir():
            raise NotADirectoryError(f"Validation dataset root is not a directory: {self._root}")

        summary = ValidationLoadSummary(dataset_root=str(self._root))
        symbols_seen: set[str] = set()
        intervals_seen: set[str] = set()

        for file_path in sorted(self._iter_dataset_files()):
            dataset = self._read_dataset_file(file_path)
            self._datasets[(dataset.symbol, dataset.interval)] = dataset

            warmup_count = await self._prime_dataset(dataset, warmup_bars)
            summary.files_loaded += 1
            summary.candles_loaded += len(dataset.bars)
            summary.warmup_bars_loaded += warmup_count
            summary.replay_bars_remaining += max(len(dataset.bars) - warmup_count, 0)
            symbols_seen.add(dataset.symbol)
            intervals_seen.add(dataset.interval)

            logger.info(
                "[ValidationDatasetLoader] %s %s loaded from %s (%d bars, warmup=%d, replay_remaining=%d)",
                dataset.symbol,
                dataset.interval,
                file_path.name,
                len(dataset.bars),
                warmup_count,
                max(len(dataset.bars) - warmup_count, 0),
            )

        summary.symbols_loaded = len(symbols_seen)
        summary.intervals_loaded = len(intervals_seen)
        logger.info(
            "[ValidationDatasetLoader] Loaded %d files / %d candles from %s",
            summary.files_loaded,
            summary.candles_loaded,
            self._root,
        )
        return summary

    def get_replay_datasets(self) -> List[ValidationReplayDataset]:
        return list(self._datasets.values())

    def _iter_dataset_files(self) -> Iterable[Path]:
        yield from self._root.glob("*/*.json")

    def _read_dataset_file(self, file_path: Path) -> ValidationReplayDataset:
        with file_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)

        symbol = str(payload.get("symbol") or file_path.parent.name).upper()
        interval = str(payload.get("interval") or file_path.stem.split("-")[0])
        raw_bars = payload.get("bars") or []

        if not isinstance(raw_bars, list):
            raise ValueError(f"Dataset bars must be a list: {file_path}")

        bars = [self._normalize_bar(bar) for bar in raw_bars]
        bars.sort(key=lambda item: item["ts"])
        return ValidationReplayDataset(symbol=symbol, interval=interval, bars=bars)

    async def _prime_dataset(self, dataset: ValidationReplayDataset, warmup_bars: Optional[int]) -> int:
        if warmup_bars is None:
            selected = dataset.bars
        else:
            selected = dataset.bars[: max(warmup_bars, 0)]

        last_candle = None
        for candle in selected:
            await self._store.upsert_candle(dataset.symbol, dataset.interval, candle)
            last_candle = candle

        if last_candle is not None:
            await self._store.update_ticker(
                dataset.symbol,
                {
                    "symbol": dataset.symbol,
                    "ts": last_candle["ts"],
                    "price": last_candle["c"],
                    "volume_24h": last_candle["v"],
                    "change_pct": self._calc_change_pct(last_candle["o"], last_candle["c"]),
                    "source": "validation_dataset",
                },
            )

        return len(selected)

    @staticmethod
    def _normalize_bar(bar: dict) -> dict:
        open_time = bar.get("open_time")
        if not open_time:
            raise ValueError(f"Dataset bar missing open_time: {bar}")

        ts = int(datetime.fromisoformat(open_time).timestamp() * 1000)
        return {
            "ts": ts,
            "o": float(bar.get("open", 0.0)),
            "h": float(bar.get("high", 0.0)),
            "l": float(bar.get("low", 0.0)),
            "c": float(bar.get("close", 0.0)),
            "v": float(bar.get("volume", 0.0)),
        }

    @staticmethod
    def _calc_change_pct(open_price: float, close_price: float) -> float:
        if open_price == 0:
            return 0.0
        return round((close_price - open_price) / open_price * 100, 4)
