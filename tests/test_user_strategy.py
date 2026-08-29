from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Direction, Exchange, Interval, OrderType, Product, Status
from vnpy.trader.object import AccountData, BarData, ContractData, OrderData, PositionData, TickData

from user_strategy.config import StrategyConfig, SymbolConfig
from user_strategy.engine import EngineState, StrategyEngine, SymbolState


class FakeMain:
    def __init__(self):
        self.orders = {}
        self.sent = []
        self.cancelled = []
        self.logs = []

    def get_all_positions(self): return []
    def get_all_active_orders(self): return []
    def query_history(self, request, gateway):
        return [BarData(symbol=request.symbol, exchange=request.exchange, datetime=datetime.now(timezone.utc) - timedelta(days=1), interval=Interval.DAILY, close_price=100, gateway_name="IB")]
    def send_order(self, request, gateway):
        order_id = str(len(self.sent) + 1)
        order = request.create_order_data(order_id, gateway)
        self.orders[order.vt_orderid] = order
        self.sent.append(order)
        return order.vt_orderid
    def get_order(self, vt_orderid): return self.orders.get(vt_orderid)
    def cancel_order(self, request, gateway): self.cancelled.append(request)
    def write_log(self, message, source): self.logs.append((message, source))


def config(tmp_path: Path) -> StrategyConfig:
    return StrategyConfig(
        strategy_id="paper-test", gateway_name="IB", account="DU1", host="127.0.0.1", client_id=1, port=7497,
        capital=10_000, state_path=tmp_path / "state.json",
        basket=(SymbolConfig("AAPL", "SMART", "USD", "US", 8_000),),
        turnover_threshold=1, volume_warmup_seconds=1, volume_min_avg=0,
    )


def setup(tmp_path, monkeypatch):
    main, events = FakeMain(), EventEngine()
    engine = StrategyEngine(main, events, config(tmp_path))
    engine.start()
    engine.on_account(Event("account", AccountData(accountid="DU1.USD", gateway_name="IB")))
    engine.on_contract(Event("contract", ContractData(symbol="AAPL", exchange=Exchange.SMART, name="AAPL", product=Product.EQUITY, size=1, pricetick=.01, gateway_name="IB")))
    monkeypatch.setattr("user_strategy.engine.is_open", lambda market, now: True)
    return engine, main


def tick():
    return TickData(symbol="AAPL", exchange=Exchange.SMART, datetime=datetime.now(timezone.utc), last_price=110, volume=10, turnover=2, gateway_name="IB")


def test_paused_signal_never_submits_buy(tmp_path, monkeypatch):
    engine, main = setup(tmp_path, monkeypatch)
    engine.market.signal = lambda *args: (True, 1)
    engine.on_tick(Event("tick", tick()))
    assert main.sent == []
    assert engine.state is EngineState.PAUSED


def test_duplicate_signal_creates_one_order(tmp_path, monkeypatch):
    engine, main = setup(tmp_path, monkeypatch)
    engine.command("start")
    engine.market.signal = lambda *args: (True, 1)
    engine.on_tick(Event("tick", tick()))
    engine.on_tick(Event("tick", tick()))
    assert len(main.sent) == 1
    assert main.sent[0].type is OrderType.LIMIT


def test_start_requires_confirmed_equity_contract(tmp_path, monkeypatch):
    main, events = FakeMain(), EventEngine()
    engine = StrategyEngine(main, events, config(tmp_path))
    engine.start()
    engine.on_account(Event("account", AccountData(accountid="DU1.USD", gateway_name="IB")))
    engine.command("start")
    assert engine.state is EngineState.PAUSED
    wrong = ContractData(symbol="AAPL", exchange=Exchange.SMART, name="AAPL", product=Product.FUTURES, size=1, pricetick=.01, gateway_name="IB")
    engine.on_contract(Event("contract", wrong))
    assert engine.state is EngineState.HALTED


def test_persisted_signal_sequence_stays_monotonic_after_restart(tmp_path, monkeypatch):
    engine, main = setup(tmp_path, monkeypatch)
    engine.command("start")
    engine.market.signal = lambda *args: (True, 4)
    engine.on_tick(Event("tick", tick()))
    engine.close()
    restored, events = StrategyEngine(FakeMain(), EventEngine(), config(tmp_path)), EventEngine()
    # The store itself proves the sequence needed for restart idempotency.
    assert restored.store.load()["signal_sequences"]["AAPL.SMART"] == 4


def test_max_order_notional_blocks_bad_quantity(tmp_path, monkeypatch):
    limited = StrategyConfig(**{**config(tmp_path).__dict__, "max_order_notional": 100})
    main, events = FakeMain(), EventEngine()
    engine = StrategyEngine(main, events, limited)
    engine.start()
    engine.on_account(Event("account", AccountData(accountid="DU1.USD", gateway_name="IB")))
    engine.on_contract(Event("contract", ContractData(symbol="AAPL", exchange=Exchange.SMART, name="AAPL", product=Product.EQUITY, size=1, pricetick=.01, gateway_name="IB")))
    engine.command("start")
    monkeypatch.setattr("user_strategy.engine.is_open", lambda market, now: True)
    engine.market.signal = lambda *args: (True, 1)
    engine.on_tick(Event("tick", tick()))
    assert main.sent == []


def test_rejected_signal_requires_new_identity(tmp_path, monkeypatch):
    engine, main = setup(tmp_path, monkeypatch)
    engine.command("start")
    engine.market.signal = lambda *args: (True, 1)
    engine.on_tick(Event("tick", tick()))
    order = main.sent[0]
    order.status = Status.REJECTED
    engine.on_order(Event("order", order))
    engine.on_tick(Event("tick", tick()))
    assert len(main.sent) == 1
    engine.market.signal = lambda *args: (True, 2)
    engine.on_tick(Event("tick", tick()))
    assert len(main.sent) == 2


def test_partial_fill_uses_position_and_creates_stop(tmp_path, monkeypatch):
    engine, main = setup(tmp_path, monkeypatch)
    engine.command("start")
    engine.market.signal = lambda *args: (True, 1)
    engine.on_tick(Event("tick", tick()))
    position = PositionData(symbol="AAPL", exchange=Exchange.SMART, direction=Direction.NET, volume=7, price=110, gateway_name="IB")
    engine.on_position(Event("position", position))
    runtime = engine.runtimes["AAPL.SMART"]
    assert runtime.state is SymbolState.ENTERED
    assert main.sent[-1].type is OrderType.STOP
    assert main.sent[-1].volume == 7


def test_stop_cancel_before_market_exit(tmp_path, monkeypatch):
    engine, main = setup(tmp_path, monkeypatch)
    runtime = engine.runtimes["AAPL.SMART"]
    runtime.state = SymbolState.ENTERED
    runtime.position = PositionData(symbol="AAPL", exchange=Exchange.SMART, direction=Direction.NET, volume=4, price=100, gateway_name="IB")
    stop = OrderData(symbol="AAPL", exchange=Exchange.SMART, orderid="9", type=OrderType.STOP, direction=Direction.SHORT, volume=4, status=Status.NOTTRADED, gateway_name="IB")
    main.orders[stop.vt_orderid] = stop
    runtime.stop_order = stop.vt_orderid
    engine.command("close_all")
    assert main.cancelled and not [o for o in main.sent if o.type is OrderType.MARKET]
    stop.status = Status.CANCELLED
    engine.on_order(Event("order", stop))
    assert main.sent[-1].type is OrderType.MARKET
    assert main.sent[-1].volume == 4


def test_uncertain_stop_status_halts_instead_of_selling(tmp_path, monkeypatch):
    engine, main = setup(tmp_path, monkeypatch)
    runtime = engine.runtimes["AAPL.SMART"]
    runtime.state = SymbolState.ENTERED
    runtime.position = PositionData(symbol="AAPL", exchange=Exchange.SMART, direction=Direction.NET, volume=4, price=100, gateway_name="IB")
    runtime.stop_order = "IB.missing"
    engine.command("close_all")
    assert engine.state is EngineState.HALTED
    assert not main.sent


def test_startup_existing_position_halts(tmp_path, monkeypatch):
    class Existing(FakeMain):
        def get_all_positions(self):
            return [PositionData(symbol="AAPL", exchange=Exchange.SMART, direction=Direction.NET, volume=1, price=100, gateway_name="IB")]
    main, events = Existing(), EventEngine()
    engine = StrategyEngine(main, events, config(tmp_path))
    engine.start()
    assert engine.state is EngineState.HALTED


def test_startup_existing_open_order_and_invalid_state_halt(tmp_path, monkeypatch):
    class Existing(FakeMain):
        def get_all_active_orders(self):
            return [OrderData(symbol="AAPL", exchange=Exchange.SMART, orderid="7", status=Status.NOTTRADED, gateway_name="IB")]
    main, events = Existing(), EventEngine()
    engine = StrategyEngine(main, events, config(tmp_path))
    engine.start()
    assert engine.state is EngineState.HALTED
    config(tmp_path).state_path.write_text("not json", encoding="utf-8")
    with pytest.raises(RuntimeError):
        StrategyEngine(FakeMain(), EventEngine(), config(tmp_path)).start()


def test_wrong_account_and_market_closed_block_start_and_buy(tmp_path, monkeypatch):
    engine, main = setup(tmp_path, monkeypatch)
    engine.account_ready = False
    engine.command("start")
    assert engine.state is EngineState.PAUSED
    engine.account_ready = True
    engine.command("start")
    monkeypatch.setattr("user_strategy.engine.is_open", lambda market, now: False)
    engine.market.signal = lambda *args: (True, 1)
    engine.on_tick(Event("tick", tick()))
    assert not main.sent


def test_disconnect_reconnect_pauses_and_never_resends(tmp_path, monkeypatch):
    engine, main = setup(tmp_path, monkeypatch)
    engine.command("start")
    engine.market.signal = lambda *args: (True, 1)
    engine.on_tick(Event("tick", tick()))
    engine.notify_gateway_disconnected()
    engine.notify_gateway_reconnected()
    engine.on_tick(Event("tick", tick()))
    assert engine.state is EngineState.PAUSED
    assert len(main.sent) == 1


def test_broker_local_mismatch_halts(tmp_path, monkeypatch):
    engine, main = setup(tmp_path, monkeypatch)
    position = PositionData(symbol="AAPL", exchange=Exchange.SMART, direction=Direction.NET, volume=2, price=100, gateway_name="IB")
    engine.on_position(Event("position", position))
    assert engine.state is EngineState.HALTED


def test_global_loss_halts_and_starts_exit(tmp_path, monkeypatch):
    engine, main = setup(tmp_path, monkeypatch)
    runtime = engine.runtimes["AAPL.SMART"]
    runtime.state = SymbolState.ENTERED
    runtime.position = PositionData(symbol="AAPL", exchange=Exchange.SMART, direction=Direction.NET, volume=100, price=100, gateway_name="IB")
    engine.market.update(tick(), datetime.now(timezone.utc))
    engine.market.data["AAPL.SMART"].price = 40
    engine._evaluate_global_risk()
    assert engine.state is EngineState.HALTED
    assert main.sent[-1].type is OrderType.MARKET


def test_shutdown_unregisters_handlers_and_persists(tmp_path, monkeypatch):
    engine, main = setup(tmp_path, monkeypatch)
    engine.close()
    assert not engine._registered
    assert config(tmp_path).state_path.exists()


@pytest.mark.parametrize("port", [7496, 4001, 9999])
def test_live_and_unknown_ports_are_rejected(tmp_path, port):
    with pytest.raises(ValueError):
        StrategyConfig(strategy_id="x", gateway_name="IB", account="DU1", host="127.0.0.1", client_id=1, port=port, capital=1,
                       state_path=tmp_path / "x.json", basket=(SymbolConfig("AAPL", "SMART", "USD", "US", 1),))
