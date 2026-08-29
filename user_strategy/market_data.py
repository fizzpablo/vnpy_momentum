"""Fail-closed signal inputs based only on public vn.py TickData."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite

from vnpy.trader.object import TickData


@dataclass
class MarketObservation:
    price: float | None = None
    turnover: float | None = None
    timestamp: datetime | None = None
    volumes: deque[tuple[int, float]] = field(default_factory=deque)
    last_cumulative_volume: float | None = None
    signal_was_true: bool = False
    sequence: int = 0


class MarketDataAdapter:
    """Consumes TickData only; missing reliable turnover keeps BUY closed.

    A gateway may put verified cumulative turnover in ``tick.turnover`` or
    ``tick.extra['cumulative_turnover']``.  This class deliberately does not
    estimate it from price times volume, nor create an additional IB client.
    """

    def __init__(self, *, max_age_sec: float, warmup_seconds: int, volume_min_avg: float, surge_ratio: float) -> None:
        self.max_age_sec = max_age_sec
        self.warmup_seconds = warmup_seconds
        self.volume_min_avg = volume_min_avg
        self.surge_ratio = surge_ratio
        self.data: dict[str, MarketObservation] = {}

    def update(self, tick: TickData, now: datetime) -> MarketObservation:
        item = self.data.setdefault(tick.vt_symbol, MarketObservation())
        timestamp = tick.datetime
        if timestamp.tzinfo is None:
            return item
        timestamp = timestamp.astimezone(timezone.utc)
        if abs((now.astimezone(timezone.utc) - timestamp).total_seconds()) > self.max_age_sec:
            return item
        if not (isfinite(tick.last_price) and tick.last_price > 0):
            return item
        item.price, item.timestamp = tick.last_price, timestamp
        if isfinite(tick.volume) and tick.volume >= 0:
            second = int(timestamp.timestamp())
            delta = 0.0 if item.last_cumulative_volume is None else tick.volume - item.last_cumulative_volume
            item.last_cumulative_volume = tick.volume
            if delta < 0:
                item.volumes.clear()
            else:
                item.volumes.append((second, delta))
            while item.volumes and item.volumes[0][0] < second - 299:
                item.volumes.popleft()
        extra = tick.extra or {}
        candidate = extra.get("cumulative_turnover", tick.turnover)
        if isinstance(candidate, (int, float)) and isfinite(candidate) and candidate >= 0:
            item.turnover = float(candidate)
        return item

    def restore_sequence(self, vt_symbol: str, sequence: int) -> None:
        """Preserve monotonic business identities across a process restart."""
        if sequence < 0:
            raise ValueError("sequence must be nonnegative")
        self.data.setdefault(vt_symbol, MarketObservation()).sequence = sequence

    def signal(self, vt_symbol: str, reference_price: float, turnover_threshold: float, now: datetime, gain_threshold: float) -> tuple[bool, int]:
        item = self.data.get(vt_symbol)
        if not item or not item.price or not item.timestamp or item.turnover is None:
            return False, item.sequence if item else 0
        if (now.astimezone(timezone.utc) - item.timestamp).total_seconds() > self.max_age_sec:
            return False, item.sequence
        volumes = list(item.volumes)
        current = volumes[-1][1] if volumes else 0.0
        average = sum(value for _, value in volumes) / len(volumes) if volumes else 0.0
        valid = (
            item.turnover > turnover_threshold
            and item.price / reference_price - 1 >= gain_threshold
            and len(volumes) >= self.warmup_seconds
            and average >= self.volume_min_avg
            and current > self.surge_ratio * average
        )
        if valid and not item.signal_was_true:
            item.sequence += 1
        item.signal_was_true = valid
        return valid, item.sequence
