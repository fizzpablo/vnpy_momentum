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

    def update(self, tick: TickData, now: datetime, volume_multiplier: float = 1.0) -> MarketObservation:
        item = self.data.setdefault(tick.vt_symbol, MarketObservation())
        extra = getattr(tick, "extra", None) or {}
        # This is the documented gateway contract.  A bare TickData.turnover is
        # deliberately insufficient: it cannot prove realtime data or its unit.
        if extra.get("ib_market_data_type") != 1:
            return item
        timestamp = extra.get("ib_rt_time")
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            return item
        timestamp = timestamp.astimezone(timezone.utc)
        if abs((now.astimezone(timezone.utc) - timestamp).total_seconds()) > self.max_age_sec:
            return item
        if not (isfinite(tick.last_price) and tick.last_price > 0):
            return item
        item.price, item.timestamp = tick.last_price, timestamp
        cumulative_volume = extra.get("ib_rt_trade_volume")
        if cumulative_volume is None:
            cumulative_volume = extra.get("ib_rt_volume")
        if not isinstance(cumulative_volume, (int, float)) or not isfinite(cumulative_volume) or cumulative_volume < 0:
            return item
        if not isinstance(volume_multiplier, (int, float)) or not isfinite(volume_multiplier) or volume_multiplier <= 0:
            return item
        if isfinite(cumulative_volume) and cumulative_volume >= 0:
            second = int(timestamp.timestamp())
            delta = 0.0 if item.last_cumulative_volume is None else cumulative_volume - item.last_cumulative_volume
            item.last_cumulative_volume = cumulative_volume
            if delta < 0:
                item.volumes.clear()
            else:
                item.volumes.append((second, delta))
            while item.volumes and item.volumes[0][0] < second - 299:
                item.volumes.popleft()
        vwap = extra.get("ib_vwap")
        if not isinstance(vwap, (int, float)) or not isfinite(vwap) or vwap <= 0:
            return item
        item.turnover = float(cumulative_volume * vwap * volume_multiplier)
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
