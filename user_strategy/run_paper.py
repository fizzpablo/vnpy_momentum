"""Manual Paper launcher.  It has no Live mode and never sends start itself."""

from __future__ import annotations

import argparse
from pathlib import Path

from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.object import SubscribeRequest

from .config import load_config
from .engine import StrategyEngine
from .instance_lock import InstanceLock


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Paper-only vn.py stock strategy")
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    lock = InstanceLock(config.state_path.with_suffix(config.state_path.suffix + ".lock"))
    lock.acquire()
    try:
        try:
            from vnpy_ib import IbGateway
        except ImportError as exc:
            raise SystemExit("vnpy_ib and IB API must be installed in this environment") from exc
        event_engine = EventEngine()
        main_engine = MainEngine(event_engine)
        main_engine.add_gateway(IbGateway)
        strategy = StrategyEngine(main_engine, event_engine, config)
        main_engine.connect({"TWS地址": config.host, "TWS端口": config.port, "客户号": config.client_id, "交易账户": config.account}, config.gateway_name)
        for symbol in config.basket:
            main_engine.subscribe(SubscribeRequest(symbol.symbol, strategy._exchange(symbol)), config.gateway_name)
        strategy.start()
        print(f"Paper process account={config.account} host={config.host} port={config.port} state=PAUSED")
        while True:
            command = input("> ").strip().lower()
            if command in {"quit", "exit"}:
                break
            strategy.command(command)
    finally:
        if "strategy" in locals():
            strategy.close()
        if "main_engine" in locals():
            main_engine.close()
        lock.release()


if __name__ == "__main__":
    main()
