"""Small, explicit configuration model for Paper and deliberately gated Live."""

from __future__ import annotations

import os
from math import isfinite
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

PAPER_PORTS = frozenset((7497, 4002))
LIVE_PORTS = frozenset((7496, 4001))


@dataclass(frozen=True)
class SymbolConfig:
    symbol: str
    exchange: str
    currency: str
    market: str
    max_allocation: float
    lot_size: int = 1
    volume_multiplier: float = 1.0
    conid: int | None = None
    local_symbol: str | None = None
    trading_class: str | None = None

    def __post_init__(self) -> None:
        if not self.symbol or self.market not in {"US", "HK"}:
            raise ValueError("symbol and market (US/HK) are required")
        if self.currency != ("USD" if self.market == "US" else "HKD"):
            raise ValueError("market/currency mismatch")
        if self.max_allocation <= 0 or self.lot_size <= 0 or self.volume_multiplier <= 0:
            raise ValueError("allocation, lot_size and volume_multiplier must be positive")
        if self.market == "HK" and (not self.conid or not self.local_symbol or not self.trading_class):
            raise ValueError("HK symbols require conid, local_symbol and trading_class")


@dataclass(frozen=True)
class AlertConfig:
    telegram_token_env: str = ""
    telegram_chat_id: str = ""
    disk_free_min_mb: int = 1024


@dataclass(frozen=True)
class StrategyConfig:
    strategy_id: str
    gateway_name: str
    account: str
    host: str
    client_id: int
    port: int
    capital: float
    state_path: Path
    basket: tuple[SymbolConfig, ...]
    environment: Literal["paper", "live"] = "paper"
    allowed_live_accounts: tuple[str, ...] = ()
    allowed_live_client_ids: tuple[int, ...] = ()
    live_approval_env: str = ""
    alerts: AlertConfig = AlertConfig()
    price_gain_threshold_pct: float = 0.08
    volume_surge_ratio: float = 3.0
    volume_warmup_seconds: int = 120
    volume_min_avg: float = 1.0
    turnover_threshold: float = 1_000_000.0
    turnover_max_age_sec: float = 15.0
    base_position_pct: float = 0.25
    max_position_pct: float = 0.65
    scale_in_gain_pct: float = 0.10
    stop_loss_pct: float = 0.10
    profit_drawdown_stop_pct: float = 0.60
    catastrophe_stop_pct: float = 0.15
    aum_loss_limit_pct: float = 0.05
    daily_loss_limit_pct: float = 0.03
    slippage_pct: float = 0.01
    estimated_fee_pct: float = 0.0
    max_order_notional: float = 0.0
    max_active_orders: int = 20
    exit_position_snapshot_timeout_sec: float = 15.0

    def __post_init__(self) -> None:
        if self.environment == "paper":
            if self.port not in PAPER_PORTS:
                raise ValueError("Paper requires TWS 7497 or IB Gateway 4002")
        elif self.environment == "live":
            if self.port not in LIVE_PORTS:
                raise ValueError("Live requires TWS 7496 or IB Gateway 4001")
            if self.account not in self.allowed_live_accounts or self.client_id not in self.allowed_live_client_ids:
                raise ValueError("Live account or client_id is not explicitly whitelisted")
            if not self.live_approval_env or not os.environ.get(self.live_approval_env):
                raise ValueError("Live requires its separate deployment approval environment variable")
        else:
            raise ValueError("environment must be paper or live")
        if not self.strategy_id or not self.gateway_name or not self.account or not self.host or self.client_id < 0:
            raise ValueError("strategy_id, gateway_name, account, host and nonnegative client_id are required")
        if self.capital <= 0 or not self.basket:
            raise ValueError("capital and non-empty basket are required")
        if self.max_order_notional < 0 or self.max_active_orders <= 0 or self.daily_loss_limit_pct <= 0:
            raise ValueError("invalid risk limits")
        numeric = (self.capital, self.price_gain_threshold_pct, self.volume_surge_ratio, self.volume_min_avg,
                   self.turnover_threshold, self.turnover_max_age_sec, self.base_position_pct,
                   self.max_position_pct, self.scale_in_gain_pct, self.stop_loss_pct,
                   self.profit_drawdown_stop_pct, self.catastrophe_stop_pct, self.aum_loss_limit_pct,
                   self.daily_loss_limit_pct, self.slippage_pct, self.estimated_fee_pct, self.max_order_notional,
                   self.exit_position_snapshot_timeout_sec)
        if not all(isfinite(value) for value in numeric):
            raise ValueError("all numeric strategy values must be finite")
        if not (0 <= self.base_position_pct <= self.max_position_pct <= 1 and 0 <= self.slippage_pct <= 1
                and 0 <= self.estimated_fee_pct <= 1 and 0 < self.stop_loss_pct < 1
                and 0 < self.catastrophe_stop_pct < 1 and 0 < self.aum_loss_limit_pct <= 1
                and 0 < self.daily_loss_limit_pct <= 1 and self.turnover_max_age_sec > 0
                and self.volume_warmup_seconds > 0 and self.volume_surge_ratio > 0
                and 1 <= self.exit_position_snapshot_timeout_sec <= 60):
            raise ValueError("strategy values are outside their safe range")
        if self.catastrophe_stop_pct <= self.stop_loss_pct:
            raise ValueError("catastrophe_stop_pct must be greater than stop_loss_pct")
        if len({(s.symbol, s.exchange) for s in self.basket}) != len(self.basket):
            raise ValueError("duplicate basket symbol")
        if len({s.market for s in self.basket}) != 1 or len({s.currency for s in self.basket}) != 1:
            raise ValueError("one process may contain one market and one quote currency")


def load_config(path: str | Path) -> StrategyConfig:
    """Load configuration; a Live file is unusable without its out-of-Git approval."""
    config_path = Path(path).resolve()
    raw: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    ib, strategy, alerts = raw.get("ibkr", {}), raw.get("strategy", {}), raw.get("alerts", {})
    basket = tuple(SymbolConfig(
        symbol=str(row["symbol"]), exchange=str(row["exchange"]), currency=str(row["currency"]),
        market=str(row["market"]), max_allocation=float(row.get("max_allocation", row.get("max_allocation_usd", 0))),
        lot_size=int(row.get("lot_size", 1)), volume_multiplier=float(row.get("volume_multiplier", row.get("ibkr_volume_multiplier", 1))),
        conid=int(row["conid"]) if row.get("conid") is not None else None,
        local_symbol=str(row["local_symbol"]) if row.get("local_symbol") else None,
        trading_class=str(row["trading_class"]) if row.get("trading_class") else None,
    ) for row in raw.get("basket", []))
    return StrategyConfig(
        strategy_id=str(strategy["strategy_id"]), gateway_name=str(ib.get("gateway_name", "IB")),
        account=str(ib["account"]), port=int(ib["port"]), capital=float(strategy["capital"]),
        host=str(ib.get("host", "127.0.0.1")), client_id=int(ib.get("client_id", 1)),
        environment=str(ib.get("environment", "paper")),
        allowed_live_accounts=tuple(map(str, ib.get("allowed_live_accounts", []))),
        allowed_live_client_ids=tuple(map(int, ib.get("allowed_live_client_ids", []))),
        live_approval_env=str(ib.get("live_approval_env", "")),
        alerts=AlertConfig(str(alerts.get("telegram_token_env", "")), str(alerts.get("telegram_chat_id", "")), int(alerts.get("disk_free_min_mb", 1024))),
        state_path=(config_path.parent / Path(strategy["state_path"])).resolve(), basket=basket,
        **{key: strategy[key] for key in (
            "price_gain_threshold_pct", "volume_surge_ratio", "volume_warmup_seconds", "volume_min_avg",
            "turnover_threshold", "turnover_max_age_sec", "base_position_pct", "max_position_pct",
            "scale_in_gain_pct", "stop_loss_pct", "profit_drawdown_stop_pct", "catastrophe_stop_pct",
            "aum_loss_limit_pct", "daily_loss_limit_pct", "slippage_pct", "estimated_fee_pct",
            "max_order_notional", "max_active_orders", "exit_position_snapshot_timeout_sec",
        ) if key in strategy},
    )
