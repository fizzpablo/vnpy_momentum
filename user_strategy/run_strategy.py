"""Interactive Paper/Live host.  Both environments start PAUSED."""

from __future__ import annotations

import argparse
import signal
from pathlib import Path

from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.object import SubscribeRequest

from .backup import backup_state
from .config import load_config
from .engine import StrategyEngine
from .instance_lock import InstanceLock


def _handle_shutdown(signum, frame) -> None:
    raise SystemExit(128 + signum)


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    parser = argparse.ArgumentParser(description="Run the gated vn.py IBKR stock strategy")
    parser.add_argument("config", type=Path)
    parser.add_argument("--backup-dir", type=Path, default=None)
    parser.add_argument("--log-dir", type=Path, default=None, help="directory containing rotated strategy logs")
    args = parser.parse_args()
    config = load_config(args.config)
    backup_dir = args.backup_dir or config.state_path.parent / "backups"
    lock = InstanceLock(config.state_path.with_suffix(config.state_path.suffix + ".lock"))
    lock.acquire()
    strategy: StrategyEngine | None = None
    main_engine: MainEngine | None = None
    abnormal = True
    try:
        backup_state(config.state_path, backup_dir, args.log_dir)
        try:
            from vnpy_ib import IbGateway
        except ImportError as exc:
            raise SystemExit("vnpy_ib and IB API must be installed in this environment") from exc
        event_engine = EventEngine()
        main_engine = MainEngine(event_engine)
        main_engine.add_gateway(IbGateway)
        strategy = StrategyEngine(main_engine, event_engine, config)
        strategy.start()
        main_engine.connect({"TWS地址": config.host, "TWS端口": config.port, "客户号": config.client_id, "交易账户": config.account}, config.gateway_name)
        for symbol in config.basket:
            main_engine.subscribe(SubscribeRequest(symbol.symbol, strategy._exchange(symbol)), config.gateway_name)
        print(f"{config.environment.upper()} process account={config.account} host={config.host} port={config.port} state=PAUSED")
        while True:
            command = input("> ").strip().lower()
            if command in {"quit", "exit"}:
                abnormal = False
                break
            strategy.command(command)
    except Exception as exc:
        if strategy:
            strategy.alerts.send_event("PROCESS_EXIT")
        raise
    finally:
        if strategy:
            strategy.close()
            backup_state(config.state_path, backup_dir, args.log_dir)
            if abnormal:
                strategy.alerts.send_event("PROCESS_EXIT")
        if main_engine:
            main_engine.close()
        lock.release()


if __name__ == "__main__":
    main()
