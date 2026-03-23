from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional

from bot.data.store import DataStore
from bot.data.validation_dataset_loader import ValidationReplayDataset

logger = logging.getLogger(__name__)


@dataclass
class ReplayBar:
    symbol: str
    interval: str
    candle: dict
    step_index: int
    total_steps: int


class ValidationReplaySession:
    """Conservative bar-by-bar replay over staged validation datasets."""

    def __init__(
        self,
        store: DataStore,
        datasets: List[ValidationReplayDataset],
        warmup_bars: int,
        step_delay_ms: int = 0,
        max_steps: int = 0,
    ) -> None:
        self._store = store
        self._datasets = [d for d in datasets if d.bars]
        self._warmup_bars = max(warmup_bars, 0)
        self._step_delay_ms = max(step_delay_ms, 0)
        self._max_steps = max(max_steps, 0)
        self._timeline = self._build_timeline()
        self._cursor = 0

    def total_steps(self) -> int:
        return len(self._timeline)

    async def next_bar(self) -> Optional[ReplayBar]:
        if self._cursor >= len(self._timeline):
            return None

        item = self._timeline[self._cursor]
        self._cursor += 1
        await self._store.upsert_candle(item.symbol, item.interval, item.candle)
        await self._store.update_ticker(
            item.symbol,
            {
                "symbol": item.symbol,
                "ts": item.candle["ts"],
                "price": item.candle["c"],
                "volume_24h": item.candle["v"],
                "change_pct": self._calc_change_pct(item.candle["o"], item.candle["c"]),
                "source": "validation_replay",
            },
        )

        if self._step_delay_ms > 0:
            await asyncio.sleep(self._step_delay_ms / 1000)

        return item

    def _build_timeline(self) -> List[ReplayBar]:
        timeline: List[ReplayBar] = []
        for dataset in self._datasets:
            start_idx = min(self._warmup_bars, len(dataset.bars))
            for candle in dataset.bars[start_idx:]:
                timeline.append(
                    ReplayBar(
                        symbol=dataset.symbol,
                        interval=dataset.interval,
                        candle=candle,
                        step_index=0,
                        total_steps=0,
                    )
                )

        timeline.sort(key=lambda item: (item.candle["ts"], item.interval, item.symbol))
        if self._max_steps > 0:
            timeline = timeline[: self._max_steps]

        total_steps = len(timeline)
        for index, item in enumerate(timeline, start=1):
            item.step_index = index
            item.total_steps = total_steps

        logger.info(
            "[ValidationReplaySession] Prepared %d replay steps (warmup=%d, max_steps=%d)",
            total_steps,
            self._warmup_bars,
            self._max_steps,
        )
        return timeline

    @staticmethod
    def _calc_change_pct(open_price: float, close_price: float) -> float:
        if open_price == 0:
            return 0.0
        return round((close_price - open_price) / open_price * 100, 4)
