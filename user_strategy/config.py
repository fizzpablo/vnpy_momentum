"""Strict, Paper-only configuration for the strategy layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


PAPER_PORTS = frozenset((7497, 4002))


@dataclass(frozen=True)
class SymbolConfig:
    symbol: str
    exchange: str
    currency: str
    market: str
    max_allocation: float
    lot_size: int = 1
    volume_multiplier: float = 1.0

    def __post_init__(self) -> None:
        if not self.symbol or self.market not in {"US", "HK"}:
            raise ValueError("symbol and market (US/HK) are required")
        if self.currency != ("USD" if self.market == "US" else "HKD"):
            raise ValueError("market/currency mismatch")
        if self.max_allocation <= 0 or self.lot_size <= 0 or self.volume_multiplier <= 0:
            raise ValueError("allocation, lot_size and volume_multiplier must be positive")


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
    slippage_pct: float = 0.01
    estimated_fee_pct: float = 0.0
    max_order_notional: float = 0.0
    max_active_orders: int = 20

    def __post_init__(self) -> None:
        if self.port not in PAPER_PORTS:
            raise ValueError("Paper-only strategy only permits TWS 7497 or IB Gateway 4002")
        if not self.strategy_id or not self.gateway_name or not self.account or not self.host or self.client_id < 0:
            raise ValueError("strategy_id, gateway_name, account, host and nonnegative client_id are required")
        if self.capital <= 0 or not self.basket:
            raise ValueError("capital and non-empty basket are required")
        if self.max_order_notional < 0 or self.max_active_orders <= 0:
            raise ValueError("max_order_notional must be nonnegative and max_active_orders positive")
        if self.catastrophe_stop_pct <= self.stop_loss_pct:
            raise ValueError("catastrophe_stop_pct must be greater than stop_loss_pct")
        if len({(s.symbol, s.exchange) for s in self.basket}) != len(self.basket):
            raise ValueError("duplicate basket symbol")
        if len({s.market for s in self.basket}) != 1 or len({s.currency for s in self.basket}) != 1:
            raise ValueError("one process may contain one market and one quote currency")


def load_config(path: str | Path) -> StrategyConfig:
    """Load the intentionally small YAML schema; Live configuration is rejected."""
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    ib = raw.get("ibkr", {})
    if ib.get("environment", "paper") != "paper":
        raise ValueError("Live configuration is not implemented or authorized")
    strategy = raw.get("strategy", {})
    basket = tuple(
        SymbolConfig(
            symbol=str(row["symbol"]), exchange=str(row["exchange"]),
            currency=str(row["currency"]), market=str(row["market"]),
            max_allocation=float(row.get("max_allocation", row.get("max_allocation_usd", 0))),
            lot_size=int(row.get("lot_size", 1)),
            volume_multiplier=float(row.get("volume_multiplier", row.get("ibkr_volume_multiplier", 1))),
        )
        for row in raw.get("basket", [])
    )
    return StrategyConfig(
        strategy_id=str(strategy["strategy_id"]), gateway_name=str(ib.get("gateway_name", "IB")),
        account=str(ib["account"]), port=int(ib["port"]), capital=float(strategy["capital"]),
        host=str(ib.get("host", "127.0.0.1")), client_id=int(ib.get("client_id", 1)),
        state_path=(Path(path).resolve().parent / Path(strategy["state_path"])).resolve(), basket=basket,
        **{key: strategy[key] for key in (
            "price_gain_threshold_pct", "volume_surge_ratio", "volume_warmup_seconds",
            "volume_min_avg", "turnover_threshold", "turnover_max_age_sec",
            "base_position_pct", "max_position_pct", "scale_in_gain_pct", "stop_loss_pct",
            "profit_drawdown_stop_pct", "catastrophe_stop_pct", "aum_loss_limit_pct",
            "slippage_pct", "estimated_fee_pct", "max_order_notional", "max_active_orders",
        ) if key in strategy},
    )
