from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from vnpy.trader.constant import Exchange
from vnpy.trader.object import TickData

from user_strategy.market_data import MarketDataAdapter


def make_tick(at, *, price=110, volume=0, turnover=0):
    vwap = turnover / volume if volume else 1
    return SimpleNamespace(symbol="AAPL", exchange=Exchange.SMART, vt_symbol="AAPL.SMART", datetime=at, last_price=price,
                           volume=volume, turnover=turnover, gateway_name="IB",
                           extra={"ib_market_data_type": 1, "ib_rt_time": at, "ib_rt_trade_volume": volume, "ib_vwap": vwap})


def ready_adapter():
    return MarketDataAdapter(max_age_sec=15, warmup_seconds=2, volume_min_avg=1, surge_ratio=2)


def test_turnover_requires_strictly_greater_boundary():
    now = datetime.now(timezone.utc)
    adapter = ready_adapter()
    for index, volume in enumerate((0, 1, 10)):
        adapter.update(make_tick(now + timedelta(seconds=index), volume=volume, turnover=100), now + timedelta(seconds=index))
    valid, _ = adapter.signal("AAPL.SMART", 100, 100, now + timedelta(seconds=2), 0.08)
    assert valid is False
    adapter.update(make_tick(now + timedelta(seconds=3), volume=25, turnover=100.01), now + timedelta(seconds=3))
    valid, sequence = adapter.signal("AAPL.SMART", 100, 100, now + timedelta(seconds=3), 0.08)
    assert valid is True
    assert sequence == 1


def test_stale_or_naive_tick_cannot_unlock_gate():
    now = datetime.now(timezone.utc)
    adapter = ready_adapter()
    adapter.update(make_tick(now - timedelta(seconds=16), volume=20, turnover=1_000), now)
    valid, _ = adapter.signal("AAPL.SMART", 100, 1, now, 0.08)
    assert valid is False
    adapter.update(make_tick(datetime.now(), volume=30, turnover=1_000), now)
    valid, _ = adapter.signal("AAPL.SMART", 100, 1, now, 0.08)
    assert valid is False


def test_volume_reset_and_duplicate_signal_are_safe():
    now = datetime.now(timezone.utc)
    adapter = ready_adapter()
    for index, volume in enumerate((0, 1, 10, 25)):
        adapter.update(make_tick(now + timedelta(seconds=index), volume=volume, turnover=200), now + timedelta(seconds=index))
    assert adapter.signal("AAPL.SMART", 100, 100, now + timedelta(seconds=3), .08) == (True, 1)
    assert adapter.signal("AAPL.SMART", 100, 100, now + timedelta(seconds=3), .08) == (True, 1)
    adapter.update(make_tick(now + timedelta(seconds=4), volume=1, turnover=200), now + timedelta(seconds=4))
    assert adapter.signal("AAPL.SMART", 100, 100, now + timedelta(seconds=4), .08)[0] is False
