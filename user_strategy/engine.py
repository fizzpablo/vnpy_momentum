"""A deliberately small event-driven strategy using vn.py's OMS as truth."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from shutil import disk_usage
from typing import Any

from vnpy.event import Event, EventEngine, EVENT_TIMER
from vnpy.trader.constant import Direction, Exchange, Interval, Offset, OrderType, Product, Status
from vnpy.trader.event import EVENT_ACCOUNT, EVENT_CONTRACT, EVENT_LOG, EVENT_ORDER, EVENT_POSITION, EVENT_TICK, EVENT_TRADE
from vnpy.trader.object import AccountData, ContractData, HistoryRequest, LogData, OrderData, OrderRequest, PositionData, TickData, TradeData

from .calendar import is_open
from .config import StrategyConfig, SymbolConfig
from .alerts import TelegramAlerts
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
    replacing_stop: bool = False
    exit_requested: bool = False
    awaiting_exit_position: bool = False
    position_version: int = 0
    exit_position_version: int = 0
    exit_snapshot_requested_at: datetime | None = None
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
        self.snapshot_complete = False
        self.realized_pnl = 0.0
        self.daily_realized_pnl = 0.0
        self.pnl_day: date = datetime.now(timezone.utc).date()
        self._seen_trades: set[str] = set()
        self._registered = False
        self._last_disk_check: datetime | None = None
        self.alerts = TelegramAlerts(config.alerts)

    @staticmethod
    def _exchange(symbol: SymbolConfig) -> Exchange:
        return Exchange(symbol.exchange)

    def _vt(self, symbol: SymbolConfig) -> str:
        return f"{symbol.symbol}.{symbol.exchange}"

    def start(self) -> None:
        """Register handlers and reconcile before accepting the manual start command."""
        persisted = self.store.load()
        if persisted.get("pnl_day") == datetime.now(timezone.utc).date().isoformat():
            self.realized_pnl = float(persisted.get("realized_pnl", 0.0))
            self.daily_realized_pnl = float(persisted.get("daily_realized_pnl", 0.0))
        self._register()
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
            if self.state != EngineState.PAUSED or not self.snapshot_complete or not self.reconciled or not self.account_ready or not self.cash_ready or not all(item.contract_ready for item in self.runtimes.values()):
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
        elif command in {"kill", "stop_new_orders"}:
            # A kill switch is deliberately narrow: it never submits a closing order.
            if self.state != EngineState.HALTED:
                self.state = EngineState.PAUSED
            self._log("kill switch: new entry orders disabled")
        else:
            raise ValueError("command must be start, pause, close_all, reset, kill, or stop_new_orders")
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

    def notify_broker_snapshot_complete(self) -> None:
        """Gateway integration hook: only a completed broker snapshot may set READY.

        The current upstream public API does not expose this completion signal, so the
        normal host deliberately never calls it for Live.  A verified thin vnpy_ib
        extension may call this after account/position/open-order end callbacks.
        """
        self.reconcile()
        if self.reconciled:
            self.snapshot_complete = True

    def notify_gateway_disconnected(self) -> None:
        """Called by the application host when its gateway health check reports loss."""
        self.reconciled = False
        self.snapshot_complete = False
        if self.state == EngineState.RUNNING:
            self.state = EngineState.PAUSED
        self._log("gateway disconnected: new BUY orders paused")
        self._persist()

    def notify_gateway_reconnected(self) -> None:
        """Reconciliation is required again; this never resumes RUNNING automatically."""
        self.reconciled = False
        self.snapshot_complete = False
        if self.state == EngineState.RUNNING:
            self.state = EngineState.PAUSED
        self._log("gateway reconnected: waiting for broker snapshot before manual start")
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
                (EVENT_CONTRACT, self.on_contract), (EVENT_LOG, self.on_log),
                ("eIbConnection", self.on_ib_connection), ("eIbSnapshot", self.on_ib_snapshot),
                ("eIbPositionSnapshot", self.on_ib_position_snapshot),
                (EVENT_TIMER, self.on_timer))

    def _register(self) -> None:
        if not self._registered:
            for event_type, handler in self._handlers():
                self.event_engine.register(event_type, handler)
            self._registered = True

    def on_account(self, event: Event) -> None:
        account: AccountData = event.data
        if account.gateway_name != self.config.gateway_name:
            return
        received = account.accountid.split(".", 1)[0]
        if received == self.config.account:
            self.account_ready = True
            if self.config.basket[0].market == "HK":
                cash = (account.extra or {}).get("ib_cash_balance")
                self.cash_ready = isinstance(cash, (int, float)) and cash >= 0
        else:
            self.halt("unexpected IBKR account event")

    def on_contract(self, event: Event) -> None:
        contract: ContractData = event.data
        runtime = self.runtimes.get(contract.vt_symbol)
        if not runtime:
            return
        if contract.product != Product.EQUITY or contract.exchange != self._exchange(runtime.config):
            self.halt("contract does not match configured equity")
            return
        if self.config.environment == "live" and (contract.extra or {}).get("currency") != runtime.config.currency:
            self.halt("contract currency does not match whitelist")
            return
        if runtime.config.market == "HK":
            extra = contract.extra or {}
            if (extra.get("conId", extra.get("conid")) != runtime.config.conid
                    or extra.get("localSymbol") != runtime.config.local_symbol
                    or extra.get("tradingClass") != runtime.config.trading_class):
                self.halt("HK contract identity does not match whitelist")
                return
        runtime.contract_ready = True

    def on_ib_connection(self, event: Event) -> None:
        data = event.data
        if isinstance(data, dict) and data.get("connected") is True:
            self.notify_gateway_reconnected()
        else:
            self.notify_gateway_disconnected()

    def on_ib_snapshot(self, event: Event) -> None:
        data = event.data
        if isinstance(data, dict) and data.get("complete") is True and data.get("account") == self.config.account:
            self.notify_broker_snapshot_complete()

    def on_ib_position_snapshot(self, event: Event) -> None:
        data = event.data
        if not isinstance(data, dict) or data.get("account") != self.config.account:
            return
        positions = data.get("positions")
        if not isinstance(positions, dict):
            self.halt("invalid broker position snapshot")
            return
        for vt_symbol, runtime in self.runtimes.items():
            if not runtime.awaiting_exit_position:
                continue
            position = positions.get(vt_symbol)
            runtime.awaiting_exit_position = False
            if position is None or position.volume <= 0:
                runtime.exit_requested = False
                runtime.position = None
                runtime.state = SymbolState.HALTED
            else:
                runtime.position = position
                self._send_exit(runtime)
            runtime.exit_snapshot_requested_at = None

    def on_log(self, event: Event) -> None:
        """Map existing vn.py gateway logs to the strategy's connection gate.

        This is intentionally a thin listener, not a replacement reconnect client.
        ``vnpy_ib`` emits gateway errors/status through its normal log event.
        """
        log: LogData = event.data
        if log.gateway_name != self.config.gateway_name:
            return
        message = log.msg.lower()
        if any(text in message for text in ("disconnected", "connection closed", "not connected", "连接断开")):
            self.notify_gateway_disconnected()
        elif any(text in message for text in ("connected", "connection established", "连接成功")):
            self.notify_gateway_reconnected()

    def on_tick(self, event: Event) -> None:
        tick: TickData = event.data
        runtime = self.runtimes.get(tick.vt_symbol)
        if not runtime:
            return
        now = datetime.now(timezone.utc)
        observation = self.market.update(tick, now, runtime.config.volume_multiplier)
        is_fresh_tick = (
            tick.datetime.tzinfo is not None
            and observation.timestamp == (getattr(tick, "extra", None) or {}).get("ib_rt_time", tick.datetime).astimezone(timezone.utc)
        )
        if observation.price and is_fresh_tick:
            self._evaluate_exit(runtime, observation.price)
            self._evaluate_global_risk()
        if self.state != EngineState.RUNNING or runtime.state not in {SymbolState.IDLE, SymbolState.ENTERED}:
            return
        if not is_fresh_tick or not self.reconciled or not self.account_ready or not runtime.reference_price:
            return
        if is_open(runtime.config.market, now) is not True:
            return
        valid, sequence = self.market.signal(
            tick.vt_symbol, runtime.reference_price, self.config.turnover_threshold, now,
            self.config.price_gain_threshold_pct,
        )
        scale_allowed = runtime.state == SymbolState.IDLE or (
            runtime.position is not None and observation.price >= runtime.position.price * (1 + self.config.scale_in_gain_pct)
        )
        if valid and scale_allowed and not runtime.entry_order and not runtime.exit_requested and sequence > runtime.consumed_signal_sequence:
            self._send_entry(runtime, observation.price, sequence)

    def on_order(self, event: Event) -> None:
        order: OrderData = event.data
        runtime = self.runtimes.get(order.vt_symbol)
        if not runtime:
            return
        if order.vt_orderid == runtime.entry_order and order.status in {Status.REJECTED, Status.CANCELLED, Status.ALLTRADED}:
            runtime.entry_order = ""
            if runtime.exit_requested:
                self._advance_exit(runtime)
        if order.vt_orderid == runtime.stop_order and order.status in {Status.REJECTED, Status.CANCELLED}:
            runtime.stop_order = ""
            if order.status == Status.REJECTED and self._position_volume(runtime):
                self.halt("protective STOP rejected")
            elif runtime.replacing_stop:
                runtime.replacing_stop = False
                self._ensure_stop(runtime)
            elif runtime.canceling_stop:
                runtime.canceling_stop = False
                runtime.awaiting_exit_position = True
                self._request_position_snapshot(runtime)
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
                pnl = (trade.price - runtime.position.price) * trade.volume
                pnl -= abs(trade.price * trade.volume) * self.config.estimated_fee_pct
                self.realized_pnl += pnl
                self.daily_realized_pnl += pnl
            self._log(f"trade {trade.vt_orderid} {trade.volume}")
            self._persist()

    def on_position(self, event: Event) -> None:
        position: PositionData = event.data
        runtime = self.runtimes.get(position.vt_symbol)
        if not runtime:
            return
        if position.direction not in {Direction.NET, Direction.LONG}:
            self.halt("unexpected short position")
            return
        runtime.position_version += 1
        if position.volume > 0 and runtime.state == SymbolState.IDLE and not runtime.entry_order:
            self.halt("broker/local position mismatch")
            return
        runtime.position = position
        if position.volume < 0:
            self.halt("negative broker position")
            return
        if position.volume > 0:
            runtime.state = SymbolState.ENTERED
            if not runtime.awaiting_exit_position and not runtime.exit_requested:
                self._ensure_stop(runtime)
        elif runtime.state == SymbolState.ENTERED and not runtime.exit_order:
            runtime.state = SymbolState.HALTED
        self._persist()

    def on_timer(self, event: Event) -> None:
        if self.state == EngineState.RUNNING and not self.account_ready:
            self.state = EngineState.PAUSED
            self._persist()
        now = datetime.now(timezone.utc)
        if now.date() != self.pnl_day:
            self.pnl_day, self.daily_realized_pnl = now.date(), 0.0
        if self._last_disk_check is None or (now - self._last_disk_check).total_seconds() >= 60:
            self._last_disk_check = now
            free_mb = disk_usage(self.config.state_path.parent).free // (1024 * 1024)
            if free_mb < self.config.alerts.disk_free_min_mb:
                self.halt("disk free space below configured minimum")
        for runtime in self.runtimes.values():
            requested_at = runtime.exit_snapshot_requested_at
            if runtime.awaiting_exit_position and requested_at and (now - requested_at).total_seconds() > self.config.exit_position_snapshot_timeout_sec:
                runtime.exit_snapshot_requested_at = None
                self.halt("exit broker position snapshot timed out; manual IBKR takeover required")

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

    def _ensure_stop(self, runtime: SymbolRuntime) -> None:
        """Keep exactly one active STOP whose quantity equals broker-reported position."""
        position = runtime.position
        if not position or position.volume <= 0 or runtime.exit_order or runtime.canceling_stop:
            return
        if not runtime.stop_order:
            self._send_stop(runtime)
            return
        order = self.main_engine.get_order(runtime.stop_order)
        if not order or not order.is_active():
            self.halt("protective STOP status uncertain")
            return
        if order.volume == position.volume:
            return
        runtime.replacing_stop = True
        self.state = EngineState.PAUSED
        self.main_engine.cancel_order(order.create_cancel_request(), self.config.gateway_name)

    def _begin_exit(self, runtime: SymbolRuntime) -> None:
        if runtime.exit_order or runtime.exit_requested:
            return
        runtime.exit_requested = True
        if self.state != EngineState.HALTED:
            self.state = EngineState.PAUSED
        if runtime.entry_order:
            entry = self.main_engine.get_order(runtime.entry_order)
            if not entry:
                self.halt("entry order status uncertain during exit")
                return
            if entry.is_active():
                self.main_engine.cancel_order(entry.create_cancel_request(), self.config.gateway_name)
                return
            runtime.entry_order = ""
        self._advance_exit(runtime)

    def _advance_exit(self, runtime: SymbolRuntime) -> None:
        """Entry is terminal.  Cancel the STOP before requesting a fresh position event."""
        if not self._position_volume(runtime):
            runtime.exit_requested = False
            return
        if runtime.stop_order:
            order = self.main_engine.get_order(runtime.stop_order)
            if not order or not order.is_active():
                self.halt("protective STOP status uncertain")
                return
            runtime.canceling_stop = True
            self.main_engine.cancel_order(order.create_cancel_request(), self.config.gateway_name)
        else:
            runtime.awaiting_exit_position = True
            self._request_position_snapshot(runtime)

    def _request_position_snapshot(self, runtime: SymbolRuntime) -> None:
        gateway = self.main_engine.get_gateway(self.config.gateway_name)
        if not gateway:
            self.halt("gateway unavailable for exit position refresh")
            return
        runtime.exit_snapshot_requested_at = datetime.now(timezone.utc)
        gateway.query_position()
        self._log("exit requested: broker position snapshot requested")

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
        loss = self.realized_pnl + unrealized
        if loss <= -self.config.aum_loss_limit_pct * self.config.capital or self.daily_realized_pnl + unrealized <= -self.config.daily_loss_limit_pct * self.config.capital:
            self.state = EngineState.HALTED
            self._log("HALTED: strategy or daily loss limit")
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
        self.alerts.send_event("DISK_LOW" if reason.startswith("disk ") else "HALTED")
        self._persist()

    def _persist(self) -> None:
        self.store.save({
            "state": self.state.value,
            "symbols": {key: item.state.value for key, item in self.runtimes.items()},
            "signal_sequences": {key: item.consumed_signal_sequence for key, item in self.runtimes.items()},
            "pnl_day": self.pnl_day.isoformat(), "realized_pnl": self.realized_pnl,
            "daily_realized_pnl": self.daily_realized_pnl,
        })

    def _log(self, message: str) -> None:
        self.main_engine.write_log(f"[{self.config.strategy_id}] {message}", "user_strategy")
        lowered = message.lower()
        if "rejected" in lowered:
            self.alerts.send_event("ORDER_REJECTED")
        elif "disconnected" in lowered:
            self.alerts.send_event("GATEWAY_DISCONNECTED")
        elif "loss limit" in lowered:
            self.alerts.send_event("LOSS_LIMIT")
