"""Run an AlphaStrategy backtest from a JSON configuration file.

This runner is deliberately separate from ``user_strategy.engine.StrategyEngine``.
The latter consumes live TickData and OMS events, while the Alpha backtesting
engine replays bar data and precomputed signals.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import polars as pl

from vnpy.alpha import AlphaLab, AlphaStrategy, BacktestingEngine
from vnpy.trader.constant import Interval


REQUIRED_SIGNAL_COLUMNS = {"datetime", "vt_symbol", "signal"}


def load_json(path: Path) -> dict[str, Any]:
    """Load a backtest configuration."""
    with path.open(encoding="utf-8") as file:
        config: dict[str, Any] = json.load(file)
    return config


def resolve_path(value: str, config_path: Path) -> Path:
    """Resolve configuration paths relative to the configuration file."""
    path = Path(value)
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()


def load_strategy(class_path: str) -> type[AlphaStrategy]:
    """Import an AlphaStrategy from ``module:ClassName`` notation."""
    try:
        module_name, class_name = class_path.split(":", maxsplit=1)
        strategy_class = getattr(importlib.import_module(module_name), class_name)
    except (AttributeError, ImportError, ValueError) as exc:
        raise ValueError(
            "strategy.class must use 'package.module:ClassName' notation"
        ) from exc

    if not isinstance(strategy_class, type) or not issubclass(strategy_class, AlphaStrategy):
        raise TypeError(f"{class_path} must be an AlphaStrategy subclass")
    return strategy_class


def load_signal(path: Path) -> pl.DataFrame:
    """Load a Parquet or CSV signal table and validate its required schema."""
    if not path.exists():
        raise FileNotFoundError(f"Signal file does not exist: {path}")

    if path.suffix.lower() == ".parquet":
        signal = pl.read_parquet(path)
    elif path.suffix.lower() == ".csv":
        signal = pl.read_csv(path, try_parse_dates=True)
    else:
        raise ValueError("Signal file must be a .parquet or .csv file")

    missing = REQUIRED_SIGNAL_COLUMNS.difference(signal.columns)
    if missing:
        raise ValueError(f"Signal file is missing columns: {', '.join(sorted(missing))}")

    if signal.schema["datetime"] == pl.String:
        signal = signal.with_columns(
            pl.col("datetime").str.to_datetime(strict=False).alias("datetime")
        )
    elif signal.schema["datetime"] == pl.Date:
        signal = signal.with_columns(pl.col("datetime").cast(pl.Datetime).alias("datetime"))

    if signal.schema["datetime"] != pl.Datetime:
        raise ValueError("signal.datetime must be a datetime column or an ISO-8601 datetime string")

    signal = signal.with_columns(
        pl.col("vt_symbol").cast(pl.String),
        pl.col("signal").cast(pl.Float64),
    ).drop_nulls(["datetime", "vt_symbol", "signal"])

    if signal.is_empty():
        raise ValueError("Signal file has no valid rows after schema validation")
    return signal


def as_text(value: Any) -> str:
    """Serialize values for human-readable CSV output."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    return str(value)


def write_records(path: Path, records: list[dict[str, Any]], fields: list[str]) -> None:
    """Write trade/order records, including an empty file with headers."""
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)


def export_results(engine: BacktestingEngine, statistics: dict[str, Any], output_dir: Path) -> None:
    """Export statistics, daily PnL, orders and trades for auditability."""
    output_dir.mkdir(parents=True, exist_ok=True)

    with output_dir.joinpath("statistics.json").open("w", encoding="utf-8") as file:
        json.dump(statistics, file, ensure_ascii=False, indent=2, default=as_text)

    engine.daily_df.write_csv(output_dir.joinpath("daily_results.csv"))

    order_fields = ["vt_orderid", "vt_symbol", "direction", "offset", "price", "volume", "traded", "status", "datetime"]
    orders = [
        {
            "vt_orderid": order.vt_orderid,
            "vt_symbol": order.vt_symbol,
            "direction": as_text(order.direction),
            "offset": as_text(order.offset),
            "price": order.price,
            "volume": order.volume,
            "traded": order.traded,
            "status": as_text(order.status),
            "datetime": as_text(order.datetime),
        }
        for order in engine.get_all_orders()
    ]
    write_records(output_dir.joinpath("orders.csv"), orders, order_fields)

    trade_fields = ["vt_tradeid", "vt_orderid", "vt_symbol", "direction", "offset", "price", "volume", "datetime"]
    trades = [
        {
            "vt_tradeid": trade.vt_tradeid,
            "vt_orderid": trade.vt_orderid,
            "vt_symbol": trade.vt_symbol,
            "direction": as_text(trade.direction),
            "offset": as_text(trade.offset),
            "price": trade.price,
            "volume": trade.volume,
            "datetime": as_text(trade.datetime),
        }
        for trade in engine.get_all_trades()
    ]
    write_records(output_dir.joinpath("trades.csv"), trades, trade_fields)


def run(config_path: Path, show_chart: bool = False) -> dict[str, Any]:
    """Load data, execute one backtest, and export its results."""
    config = load_json(config_path)
    lab = AlphaLab(str(resolve_path(str(config["lab_path"]), config_path)))
    vt_symbols = [str(symbol) for symbol in config["vt_symbols"]]
    signal = load_signal(resolve_path(str(config["signal_path"]), config_path))

    contract_settings = lab.load_contract_setttings()
    missing_contracts = [symbol for symbol in vt_symbols if symbol not in contract_settings]
    if missing_contracts:
        raise ValueError(
            "Missing contract settings in contract.json for: " + ", ".join(missing_contracts)
        )

    strategy_config = config["strategy"]
    strategy_class = load_strategy(str(strategy_config["class"]))
    setting = dict(strategy_config.get("setting", {}))

    try:
        interval = Interval(str(config["interval"]))
    except ValueError as exc:
        raise ValueError("interval must be 'd' (daily) or '1m' (minute)") from exc

    engine = BacktestingEngine(lab)
    engine.set_parameters(
        vt_symbols=vt_symbols,
        interval=interval,
        start=datetime.fromisoformat(str(config["start"])),
        end=datetime.fromisoformat(str(config["end"])),
        capital=int(config.get("capital", 1_000_000)),
        risk_free=float(config.get("risk_free", 0)),
        annual_days=int(config.get("annual_days", 240)),
    )
    engine.add_strategy(strategy_class, setting, signal)
    engine.load_data()

    if not engine.dts:
        raise RuntimeError("No bar data was loaded; check lab_path, vt_symbols, interval and date range")

    engine.run_backtesting()
    daily_df = engine.calculate_result()
    if daily_df is None:
        raise RuntimeError("The strategy produced no trades; no performance report was generated")

    statistics = engine.calculate_statistics()
    export_results(engine, statistics, resolve_path(str(config["output_dir"]), config_path))

    if show_chart:
        engine.show_chart()
    return statistics


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an AlphaStrategy bar-data backtest")
    parser.add_argument("config", type=Path, help="Path to a JSON backtest configuration")
    parser.add_argument("--show-chart", action="store_true", help="Open the Plotly performance chart after the run")
    args = parser.parse_args()

    statistics = run(args.config.resolve(), args.show_chart)
    print(json.dumps(statistics, ensure_ascii=False, indent=2, default=as_text))


if __name__ == "__main__":
    main()
