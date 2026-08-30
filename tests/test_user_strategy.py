from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Direction, Exchange, Interval, OrderType, Product, Status
from vnpy.trader.object import AccountData, BarData, ContractData, LogData, OrderData, PositionData, TickData

from user_strategy.config import StrategyConfig, SymbolConfig, load_config
from user_strategy.engine import EngineState, StrategyEngine, SymbolState


class FakeMain:
    def __init__(self):
        self.orders = {}
        self.sent = []
        self.cancelled = []
        self.logs = []
        self.gateway = type("Gateway", (), {"query_position": lambda self: None})()

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
    def get_gateway(self, gateway_name): return self.gateway
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
    engine.notify_broker_snapshot_complete()
    engine.on_account(Event("account", AccountData(accountid="DU1.USD", gateway_name="IB")))
    engine.on_contract(Event("contract", ContractData(symbol="AAPL", exchange=Exchange.SMART, name="AAPL", product=Product.EQUITY, size=1, pricetick=.01, gateway_name="IB")))
    monkeypatch.setattr("user_strategy.engine.is_open", lambda market, now: True)
    return engine, main


def tick():
    now = datetime.now(timezone.utc)
    return SimpleNamespace(symbol="AAPL", exchange=Exchange.SMART, vt_symbol="AAPL.SMART", datetime=now, last_price=110,
                           volume=10, turnover=2, gateway_name="IB",
                           extra={"ib_market_data_type": 1, "ib_rt_time": now, "ib_rt_trade_volume": 10, "ib_vwap": 110})


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
    assert not [o for o in main.sent if o.type is OrderType.MARKET]
    engine.on_ib_position_snapshot(Event("snapshot", {"account": "DU1", "positions": {"AAPL.SMART": PositionData(symbol="AAPL", exchange=Exchange.SMART, direction=Direction.NET, volume=4, price=100, gateway_name="IB")}}))
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


def test_protective_stop_rejection_halts(tmp_path, monkeypatch):
    engine, main = setup(tmp_path, monkeypatch)
    runtime = engine.runtimes["AAPL.SMART"]
    runtime.state = SymbolState.ENTERED
    runtime.position = PositionData(symbol="AAPL", exchange=Exchange.SMART, direction=Direction.NET, volume=4, price=100, gateway_name="IB")
    stop = OrderData(symbol="AAPL", exchange=Exchange.SMART, orderid="stop", type=OrderType.STOP, direction=Direction.SHORT, volume=4, status=Status.REJECTED, gateway_name="IB")
    runtime.stop_order = stop.vt_orderid
    engine.on_order(Event("order", stop))
    assert engine.state is EngineState.HALTED


def test_startup_existing_position_halts(tmp_path, monkeypatch):
    class Existing(FakeMain):
        def get_all_positions(self):
            return [PositionData(symbol="AAPL", exchange=Exchange.SMART, direction=Direction.NET, volume=1, price=100, gateway_name="IB")]
    main, events = Existing(), EventEngine()
    engine = StrategyEngine(main, events, config(tmp_path))
    engine.start()
    engine.notify_broker_snapshot_complete()
    assert engine.state is EngineState.HALTED


def test_startup_existing_open_order_and_invalid_state_halt(tmp_path, monkeypatch):
    class Existing(FakeMain):
        def get_all_active_orders(self):
            return [OrderData(symbol="AAPL", exchange=Exchange.SMART, orderid="7", status=Status.NOTTRADED, gateway_name="IB")]
    main, events = Existing(), EventEngine()
    engine = StrategyEngine(main, events, config(tmp_path))
    engine.start()
    engine.notify_broker_snapshot_complete()
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


def test_wrong_gateway_account_halts(tmp_path, monkeypatch):
    engine, _ = setup(tmp_path, monkeypatch)
    engine.on_account(Event("account", AccountData(accountid="DU999.USD", gateway_name="IB")))
    assert engine.state is EngineState.HALTED


def test_gateway_log_disconnect_and_reconnect_stay_paused(tmp_path, monkeypatch):
    engine, _ = setup(tmp_path, monkeypatch)
    engine.command("start")
    engine.on_log(Event("log", LogData(gateway_name="IB", msg="connection closed")))
    assert engine.state is EngineState.PAUSED and not engine.reconciled
    engine.on_log(Event("log", LogData(gateway_name="IB", msg="connection established")))
    assert engine.state is EngineState.PAUSED and not engine.reconciled
    engine.notify_broker_snapshot_complete()
    assert engine.reconciled


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
    assert not main.sent
    engine.on_ib_position_snapshot(Event("snapshot", {"account": "DU1", "positions": {"AAPL.SMART": PositionData(symbol="AAPL", exchange=Exchange.SMART, direction=Direction.NET, volume=100, price=100, gateway_name="IB")}}))
    assert main.sent[-1].type is OrderType.MARKET


def test_daily_loss_halts_and_starts_exit(tmp_path, monkeypatch):
    limited = StrategyConfig(**{**config(tmp_path).__dict__, "daily_loss_limit_pct": .01})
    main, events = FakeMain(), EventEngine()
    engine = StrategyEngine(main, events, limited)
    engine.start()
    runtime = engine.runtimes["AAPL.SMART"]
    runtime.state = SymbolState.ENTERED
    runtime.position = PositionData(symbol="AAPL", exchange=Exchange.SMART, direction=Direction.NET, volume=10, price=100, gateway_name="IB")
    engine.daily_realized_pnl = -101
    engine._evaluate_global_risk()
    assert engine.state is EngineState.HALTED and not main.sent
    engine.on_ib_position_snapshot(Event("snapshot", {"account": "DU1", "positions": {"AAPL.SMART": PositionData(symbol="AAPL", exchange=Exchange.SMART, direction=Direction.NET, volume=10, price=100, gateway_name="IB")}}))
    assert main.sent[-1].type is OrderType.MARKET


def test_exit_position_snapshot_timeout_halts(tmp_path, monkeypatch):
    limited = StrategyConfig(**{**config(tmp_path).__dict__, "exit_position_snapshot_timeout_sec": 1})
    engine, _ = setup(tmp_path, monkeypatch)
    engine.config = limited
    runtime = engine.runtimes["AAPL.SMART"]
    runtime.awaiting_exit_position = True
    runtime.exit_snapshot_requested_at = datetime.now(timezone.utc) - timedelta(seconds=2)
    engine.on_timer(Event("timer"))
    assert engine.state is EngineState.HALTED


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


def test_live_requires_approval_and_explicit_whitelist(tmp_path, monkeypatch):
    common = dict(strategy_id="live", gateway_name="IB", account="U1", host="127.0.0.1", client_id=81,
                  port=4001, capital=1, state_path=tmp_path / "live.json", basket=(SymbolConfig("AAPL", "SMART", "USD", "US", 1),),
                  environment="live", allowed_live_accounts=("U1",), allowed_live_client_ids=(81,), live_approval_env="LIVE_TEST_APPROVAL")
    with pytest.raises(ValueError):
        StrategyConfig(**common)
    monkeypatch.setenv("LIVE_TEST_APPROVAL", "approved-outside-git")
    assert StrategyConfig(**common).environment == "live"
    with pytest.raises(ValueError):
        StrategyConfig(**{**common, "account": "U2"})


def test_live_yaml_does_not_use_port_as_authorization(tmp_path, monkeypatch):
    live = tmp_path / "live.yaml"
    live.write_text("""ibkr:\n  environment: live\n  account: U1\n  client_id: 81\n  port: 4001\n  allowed_live_accounts: [U1]\n  allowed_live_client_ids: [81]\n  live_approval_env: LIVE_FILE_APPROVAL\nstrategy:\n  strategy_id: live\n  capital: 1\n  state_path: state.json\nbasket:\n  - symbol: AAPL\n    exchange: SMART\n    currency: USD\n    market: US\n    max_allocation: 1\n""", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(live)
    monkeypatch.setenv("LIVE_FILE_APPROVAL", "yes")
    assert load_config(live).environment == "live"


def test_hk_contract_identity_missing_or_wrong_halts(tmp_path):
    hk = StrategyConfig(strategy_id="hk", gateway_name="IB", account="DU1", host="127.0.0.1", client_id=1, port=7497,
                        capital=1, state_path=tmp_path / "hk.json", basket=(SymbolConfig("0700", "SEHK", "HKD", "HK", 1, conid=700, local_symbol="0700", trading_class="STK"),))
    engine = StrategyEngine(FakeMain(), EventEngine(), hk)
    engine.start()
    engine.on_contract(Event("contract", ContractData(symbol="0700", exchange=Exchange.SEHK, name="Tencent", product=Product.EQUITY, size=1, pricetick=.01, gateway_name="IB")))
    assert engine.state is EngineState.HALTED
