"""A deliberately small event-driven strategy using vn.py's OMS as truth."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from vnpy.event import Event, EventEngine, EVENT_TIMER
from vnpy.trader.constant import Direction, Exchange, Interval, Offset, OrderType, Product, Status
from vnpy.trader.event import EVENT_ACCOUNT, EVENT_CONTRACT, EVENT_ORDER, EVENT_POSITION, EVENT_TICK, EVENT_TRADE
from vnpy.trader.object import AccountData, ContractData, HistoryRequest, OrderData, OrderRequest, PositionData, TickData, TradeData

from .calendar import is_open
from .config import StrategyConfig, SymbolConfig
from .market_data import MarketDataAdapter
from .state import StateStore


class EngineState(str, Enum):
    PAUSED = "PAUSED"
    RUNNING = "RUNNING"
    HALTED = "HALTED"


class SymbolState(str, Enum):
    IDLE = "IDLE"
    ENTERED = "ENTERED"
    HALTED = "HALTED"


@dataclass
class SymbolRuntime:
    config: SymbolConfig
    state: SymbolState = SymbolState.IDLE
    reference_price: float | None = None
    position: PositionData | None = None
    peak_price: float = 0.0
    peak_unrealized: float = 0.0
    entry_order: str = ""
    stop_order: str = ""
    exit_order: str = ""
    canceling_stop: bool = False
    consumed_signal_sequence: int = 0
    contract_ready: bool = False


class StrategyEngine:
    """Application layer: no broker connection, OMS, or order-ID implementation."""

    def __init__(self, main_engine: Any, event_engine: EventEngine, config: StrategyConfig) -> None:
        self.main_engine = main_engine
        self.event_engine = event_engine
        self.config = config
        self.state = EngineState.PAUSED
        self.runtimes = {
            self._vt(symbol): SymbolRuntime(symbol) for symbol in config.basket
        }
        self.market = MarketDataAdapter(
            max_age_sec=config.turnover_max_age_sec,
            warmup_seconds=config.volume_warmup_seconds,
            volume_min_avg=config.volume_min_avg,
            surge_ratio=config.volume_surge_ratio,
        )
        self.store = StateStore(config.state_path)
        self.account_ready = False
        self.cash_ready = config.basket[0].market != "HK"
        self.reconciled = False
        self.realized_pnl = 0.0
        self._seen_trades: set[str] = set()
        self._registered = False

    @staticmethod
    def _exchange(symbol: SymbolConfig) -> Exchange:
        return Exchange(symbol.exchange)

    def _vt(self, symbol: SymbolConfig) -> str:
        return f"{symbol.symbol}.{symbol.exchange}"

    def start(self) -> None:
        """Register handlers and reconcile before accepting the manual start command."""
        persisted = self.store.load()
        self._register()
        self.reconcile()
        for vt_symbol, runtime in self.runtimes.items():
            sequence = persisted.get("signal_sequences", {}).get(vt_symbol, 0)
            if not isinstance(sequence, int) or sequence < 0:
                self.halt("persisted signal identity is invalid")
                continue
            runtime.consumed_signal_sequence = sequence
            self.market.restore_sequence(vt_symbol, sequence)
        if persisted.get("state") not in (None, EngineState.PAUSED.value):
            self.halt("persisted non-paused strategy state requires manual reset")
        self._load_references()
        self._persist()

    def close(self) -> None:
        if not self._registered:
            return
        for event_type, handler in self._handlers():
            self.event_engine.unregister(event_type, handler)
        self._registered = False
        self._persist()

    def command(self, command: str) -> None:
        command = command.lower()
        if command == "start":
            if self.state != EngineState.PAUSED or not self.reconciled or not self.account_ready or not self.cash_ready or not all(item.contract_ready for item in self.runtimes.values()):
                self._log("start refused: preflight is incomplete")
                return
            self.state = EngineState.RUNNING
        elif command == "pause":
            if self.state != EngineState.HALTED:
                self.state = EngineState.PAUSED
        elif command == "close_all":
            self.state = EngineState.PAUSED
            for runtime in self.runtimes.values():
                self._begin_exit(runtime)
        elif command == "reset":
            if self.state == EngineState.HALTED and all(not self._position_volume(item) for item in self.runtimes.values()):
                self.state = EngineState.PAUSED
                for item in self.runtimes.values():
                    item.state = SymbolState.IDLE
        else:
            raise ValueError("command must be start, pause, close_all, or reset")
        self._persist()

    def reconcile(self) -> None:
        """Reject any startup basket position/open order whose ownership is unknown."""
        try:
            positions = self.main_engine.get_all_positions()
            active = self.main_engine.get_all_active_orders()
        except Exception:
            self.halt("OMS snapshot unavailable")
            return
        basket = set(self.runtimes)
        unknown_positions = [p for p in positions if p.vt_symbol in basket and p.volume]
        unknown_orders = [o for o in active if o.vt_symbol in basket]
        if unknown_positions or unknown_orders:
            self.halt("startup broker position or open order requires manual handling")
            return
        self.reconciled = True

    def notify_gateway_disconnected(self) -> None:
        """Called by the application host when its gateway health check reports loss."""
        self.reconciled = False
        if self.state == EngineState.RUNNING:
            self.state = EngineState.PAUSED
        self._log("gateway disconnected: new BUY orders paused")
        self._persist()

    def notify_gateway_reconnected(self) -> None:
        """Reconciliation is required again; this never resumes RUNNING automatically."""
        self.reconciled = False
        self.reconcile()
        if self.state == EngineState.RUNNING:
            self.state = EngineState.PAUSED
        self._log("gateway reconnected: manual start required after reconciliation")
        self._persist()

    def _load_references(self) -> None:
        now = datetime.now(timezone.utc)
        for vt_symbol, runtime in self.runtimes.items():
            request = HistoryRequest(
                symbol=runtime.config.symbol, exchange=self._exchange(runtime.config),
                start=now - timedelta(days=10), end=now, interval=Interval.DAILY,
            )
            try:
                bars = self.main_engine.query_history(request, self.config.gateway_name)
            except Exception:
                continue
            completed = [bar for bar in bars if bar.datetime.date() < now.date() and bar.close_price > 0]
            if completed:
                runtime.reference_price = max(completed, key=lambda bar: bar.datetime).close_price
                self._log(f"reference loaded {vt_symbol}")

    def _handlers(self) -> tuple[tuple[str, Any], ...]:
        return ((EVENT_TICK, self.on_tick), (EVENT_ORDER, self.on_order), (EVENT_TRADE, self.on_trade),
                (EVENT_POSITION, self.on_position), (EVENT_ACCOUNT, self.on_account),
                (EVENT_CONTRACT, self.on_contract), (EVENT_TIMER, self.on_timer))

    def _register(self) -> None:
        if not self._registered:
            for event_type, handler in self._handlers():
                self.event_engine.register(event_type, handler)
            self._registered = True

    def on_account(self, event: Event) -> None:
        account: AccountData = event.data
        expected = f"{self.config.gateway_name}.{self.config.account}."
        if account.vt_accountid.startswith(expected):
            self.account_ready = True
            if self.config.basket[0].market == "HK":
                cash = (account.extra or {}).get("ib_cash_balance")
                self.cash_ready = isinstance(cash, (int, float)) and cash >= 0

    def on_contract(self, event: Event) -> None:
        contract: ContractData = event.data
        runtime = self.runtimes.get(contract.vt_symbol)
        if not runtime:
            return
        if contract.product != Product.EQUITY or contract.exchange != self._exchange(runtime.config):
            self.halt("contract does not match configured equity")
            return
        runtime.contract_ready = True

    def on_tick(self, event: Event) -> None:
        tick: TickData = event.data
        runtime = self.runtimes.get(tick.vt_symbol)
        if not runtime:
            return
        now = datetime.now(timezone.utc)
        observation = self.market.update(tick, now)
        is_fresh_tick = (
            tick.datetime.tzinfo is not None
            and observation.timestamp == tick.datetime.astimezone(timezone.utc)
        )
        if observation.price and is_fresh_tick:
            self._evaluate_exit(runtime, observation.price)
            self._evaluate_global_risk()
        if self.state != EngineState.RUNNING or runtime.state != SymbolState.IDLE:
            return
        if not is_fresh_tick or not self.reconciled or not self.account_ready or not runtime.reference_price:
            return
        if is_open(runtime.config.market, now) is not True:
            return
        valid, sequence = self.market.signal(
            tick.vt_symbol, runtime.reference_price, self.config.turnover_threshold, now,
            self.config.price_gain_threshold_pct,
        )
        if valid and not runtime.entry_order and sequence > runtime.consumed_signal_sequence:
            self._send_entry(runtime, observation.price, sequence)

    def on_order(self, event: Event) -> None:
        order: OrderData = event.data
        runtime = self.runtimes.get(order.vt_symbol)
        if not runtime:
            return
        if order.vt_orderid == runtime.entry_order and order.status in {Status.REJECTED, Status.CANCELLED}:
            runtime.entry_order = ""
        if order.vt_orderid == runtime.stop_order and order.status in {Status.REJECTED, Status.CANCELLED}:
            runtime.stop_order = ""
            if runtime.canceling_stop:
                runtime.canceling_stop = False
                self._send_exit(runtime)
            elif self._position_volume(runtime):
                self.halt("protective STOP missing")
        if order.vt_orderid == runtime.exit_order and order.status == Status.REJECTED:
            self.halt("exit order rejected")
        self._persist()

    def on_trade(self, event: Event) -> None:
        trade: TradeData = event.data
        runtime = self.runtimes.get(trade.vt_symbol)
        if runtime:
            if trade.vt_tradeid in self._seen_trades:
                return
            self._seen_trades.add(trade.vt_tradeid)
            if trade.direction == Direction.SHORT and runtime.position:
                self.realized_pnl += (trade.price - runtime.position.price) * trade.volume
                self.realized_pnl -= abs(trade.price * trade.volume) * self.config.estimated_fee_pct
            self._log(f"trade {trade.vt_orderid} {trade.volume}")

    def on_position(self, event: Event) -> None:
        position: PositionData = event.data
        runtime = self.runtimes.get(position.vt_symbol)
        if not runtime:
            return
        if position.direction not in {Direction.NET, Direction.LONG}:
            self.halt("unexpected short position")
            return
        if position.volume > 0 and runtime.state == SymbolState.IDLE and not runtime.entry_order:
            self.halt("broker/local position mismatch")
            return
        runtime.position = position
        if position.volume < 0:
            self.halt("negative broker position")
            return
        if position.volume > 0:
            runtime.state = SymbolState.ENTERED
            if not runtime.stop_order and not runtime.canceling_stop and not runtime.exit_order:
                self._send_stop(runtime)
        elif runtime.state == SymbolState.ENTERED and not runtime.exit_order:
            runtime.state = SymbolState.HALTED
        self._persist()

    def on_timer(self, event: Event) -> None:
        if self.state == EngineState.RUNNING and not self.account_ready:
            self.state = EngineState.PAUSED
            self._persist()

    def _send_entry(self, runtime: SymbolRuntime, price: float, sequence: int) -> None:
        qty = self._quantity(runtime, price, self.config.base_position_pct)
        if not qty:
            return
        order_notional = qty * price * (1 + self.config.slippage_pct)
        if self.config.max_order_notional and order_notional > self.config.max_order_notional:
            self._log("entry refused: max_order_notional")
            return
        if len(self.main_engine.get_all_active_orders()) >= self.config.max_active_orders:
            self._log("entry refused: max_active_orders")
            return
        request = OrderRequest(
            symbol=runtime.config.symbol, exchange=self._exchange(runtime.config), direction=Direction.LONG,
            type=OrderType.LIMIT, volume=qty, price=price * (1 + self.config.slippage_pct),
            offset=Offset.NONE, reference=f"{self.config.strategy_id}:entry:{sequence}",
        )
        runtime.entry_order = self.main_engine.send_order(request, self.config.gateway_name)
        runtime.consumed_signal_sequence = sequence
        if not runtime.entry_order:
            self.halt("entry submission unconfirmed")
        self._persist()

    def _send_stop(self, runtime: SymbolRuntime) -> None:
        position = runtime.position
        if not position or position.volume <= 0 or position.price <= 0:
            self.halt("cannot size protective STOP")
            return
        request = OrderRequest(
            symbol=runtime.config.symbol, exchange=self._exchange(runtime.config), direction=Direction.SHORT,
            type=OrderType.STOP, volume=position.volume, price=position.price * (1 - self.config.catastrophe_stop_pct),
            offset=Offset.NONE, reference=f"{self.config.strategy_id}:stop",
        )
        runtime.stop_order = self.main_engine.send_order(request, self.config.gateway_name)
        if not runtime.stop_order:
            self.halt("STOP submission unconfirmed")
        self._persist()

    def _begin_exit(self, runtime: SymbolRuntime) -> None:
        if not self._position_volume(runtime) or runtime.exit_order:
            return
        if runtime.stop_order:
            order = self.main_engine.get_order(runtime.stop_order)
            if not order or not order.is_active():
                self.halt("protective STOP status uncertain")
                return
            runtime.canceling_stop = True
            self.main_engine.cancel_order(order.create_cancel_request(), self.config.gateway_name)
        else:
            self._send_exit(runtime)

    def _send_exit(self, runtime: SymbolRuntime) -> None:
        position = runtime.position
        if not position or position.volume <= 0:
            return
        request = OrderRequest(
            symbol=runtime.config.symbol, exchange=self._exchange(runtime.config), direction=Direction.SHORT,
            type=OrderType.MARKET, volume=position.volume, offset=Offset.NONE,
            reference=f"{self.config.strategy_id}:exit",
        )
        runtime.exit_order = self.main_engine.send_order(request, self.config.gateway_name)
        if not runtime.exit_order:
            self.halt("exit submission unconfirmed")
        self._persist()

    def _evaluate_exit(self, runtime: SymbolRuntime, price: float) -> None:
        position = runtime.position
        if not position or position.volume <= 0:
            return
        runtime.peak_price = max(runtime.peak_price, price)
        unrealized = (price - position.price) * position.volume
        runtime.peak_unrealized = max(runtime.peak_unrealized, unrealized)
        stop = price <= runtime.peak_price * (1 - self.config.stop_loss_pct)
        drawdown = runtime.peak_unrealized > 0 and unrealized <= runtime.peak_unrealized * (1 - self.config.profit_drawdown_stop_pct)
        if stop or drawdown:
            self._begin_exit(runtime)

    def _evaluate_global_risk(self) -> None:
        unrealized = 0.0
        for vt_symbol, runtime in self.runtimes.items():
            position = runtime.position
            observation = self.market.data.get(vt_symbol)
            if position and position.volume > 0 and observation and observation.price:
                unrealized += (observation.price - position.price) * position.volume
        if self.realized_pnl + unrealized <= -self.config.aum_loss_limit_pct * self.config.capital:
            self.state = EngineState.HALTED
            self._log("HALTED: global strategy loss limit")
            for runtime in self.runtimes.values():
                self._begin_exit(runtime)
            self._persist()

    def _quantity(self, runtime: SymbolRuntime, price: float, allocation_pct: float) -> float:
        unit_cost = price * (1 + self.config.slippage_pct) * (1 + self.config.estimated_fee_pct)
        budget = min(self.config.capital * allocation_pct, runtime.config.max_allocation)
        max_budget = min(self.config.capital * self.config.max_position_pct, runtime.config.max_allocation)
        held = self._position_volume(runtime)
        symbol_remaining = max(0.0, max_budget - held * unit_cost)
        total_cost = sum(
            self._position_volume(item) * item.position.price
            for item in self.runtimes.values() if item.position
        )
        total_remaining = max(0.0, self.config.capital - total_cost)
        raw = min(budget, symbol_remaining, total_remaining) / unit_cost
        return float(int(raw // runtime.config.lot_size) * runtime.config.lot_size)

    @staticmethod
    def _position_volume(runtime: SymbolRuntime) -> float:
        return runtime.position.volume if runtime.position else 0.0

    def halt(self, reason: str) -> None:
        self.state = EngineState.HALTED
        self._log(f"HALTED: {reason}")
        self._persist()

    def _persist(self) -> None:
        self.store.save({
            "state": self.state.value,
            "symbols": {key: item.state.value for key, item in self.runtimes.items()},
            "signal_sequences": {key: item.consumed_signal_sequence for key, item in self.runtimes.items()},
        })

    def _log(self, message: str) -> None:
        self.main_engine.write_log(f"[{self.config.strategy_id}] {message}", "user_strategy")
