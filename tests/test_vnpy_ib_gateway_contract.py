"""Offline contract tests for the deliberately thin vnpy_ib extension."""

from datetime import datetime

from vnpy.trader.constant import Exchange
from vnpy.trader.object import TickData
from vnpy_ib.ib_gateway import (
    EVENT_IB_CONNECTION,
    EVENT_IB_SNAPSHOT,
    IbApi,
)


class FakeClient:
    def __init__(self) -> None:
        self.positions = self.orders = 0

    def reqPositions(self) -> None: self.positions += 1
    def reqOpenOrders(self) -> None: self.orders += 1
    def cancelOrder(self, order_id, manual_cancel_time) -> None: self.cancel = (order_id, manual_cancel_time)


class FakeGateway:
    gateway_name = "IB"

    def __init__(self) -> None:
        self.events = []
        self.ticks = []
        self.accounts = []
        self.logs = []

    def on_event(self, event_type, data) -> None: self.events.append((event_type, data))
    def on_tick(self, tick) -> None: self.ticks.append(tick)
    def on_account(self, account) -> None: self.accounts.append(account)
    def write_log(self, message) -> None: self.logs.append(message)


def make_api():
    gateway = FakeGateway()
    api = IbApi(gateway)
    api.client = FakeClient()
    api.load_contract_data = lambda: None
    api.account = "DU1"
    return api, gateway


def test_connect_and_all_snapshot_end_callbacks_publish_structured_readiness():
    api, gateway = make_api()
    api.connectAck()
    assert gateway.events == [(EVENT_IB_CONNECTION, {"connected": True})]
    assert api.client.positions == api.client.orders == 1
    api.accountDownloadEnd("DU1")
    api.positionEnd()
    assert not [event for event in gateway.events if event[0] == EVENT_IB_SNAPSHOT]
    api.openOrderEnd()
    assert gateway.events[-1] == (EVENT_IB_SNAPSHOT, {"account": "DU1", "complete": True})


def test_rt_trade_volume_exposes_realtime_metadata_and_delayed_type_is_preserved():
    api, gateway = make_api()
    tick = TickData(symbol="AAPL", exchange=Exchange.SMART, datetime=datetime.now(), gateway_name="IB")
    tick.extra = {}
    api.ticks[7] = tick
    api.marketDataType(7, 1)
    api.tickString(7, 77, "101.5;2;1700000000;1000;100.25;true")
    published = gateway.ticks[-1]
    assert published.extra["ib_market_data_type"] == 1
    assert published.extra["ib_rt_trade_volume"] == 1000
    assert published.extra["ib_vwap"] == 100.25
    assert published.extra["ib_rt_time"].tzinfo is not None
    api.marketDataType(7, 3)
    api.tickString(7, 48, "101.5;2;1700000001;1100;100.5;true")
    assert gateway.ticks[-1].extra["ib_market_data_type"] == 3
    assert gateway.ticks[-1].extra["ib_rt_volume"] == 1100


def test_cash_balance_is_exposed_without_overwriting_vnpy_balance():
    api, gateway = make_api()
    api.updateAccountValue("CashBalance", "1234.5", "HKD", "DU1")
    account = api.accounts["DU1.HKD"]
    assert account.extra["ib_cash_balance"] == 1234.5
    assert account.balance == 0


def test_position_from_another_visible_account_is_not_published():
    from ibapi.contract import Contract

    api, gateway = make_api()
    contract = Contract()
    contract.symbol, contract.secType, contract.exchange, contract.currency, contract.multiplier = "AAPL", "STK", "SMART", "USD", "1"
    api.updatePortfolio(contract, 3, 0, 0, 100, 0, 0, "DU_OTHER")
    assert gateway.events == []


def test_cancel_uses_official_ibapi_signature():
    from vnpy.trader.object import CancelRequest

    api, _ = make_api()
    api.status = True
    api.cancel_order(CancelRequest(orderid="12", symbol="AAPL", exchange=Exchange.SMART))
    assert api.client.cancel == (12, "")
