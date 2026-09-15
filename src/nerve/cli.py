from __future__ import annotations

import argparse
import json
import sys
import time

from .config import ExecutionMode, NerveConfig
from .desk import NerveDesk


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NERVE Protocol trading desk")
    parser.add_argument("command", choices=("paper-scan", "chain-check", "report", "kill", "run"))
    args = parser.parse_args(argv)
    config = NerveConfig.from_env()
    if args.command == "paper-scan":
        config = NerveConfig.model_validate({**config.model_dump(), "execution_mode": ExecutionMode.PAPER, "live_trading_enabled": False})
    if args.command == "chain-check":
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(config.rpc_url, request_kwargs={"timeout": 10}))
        connected = w3.is_connected()
        chain_id = int(w3.eth.chain_id) if connected else None
        print(json.dumps({"connected": connected, "chain_id": chain_id, "expected": config.chain_id}))
        return 0 if connected and chain_id == config.chain_id else 1
    desk = NerveDesk(config)
    try:
        if args.command == "paper-scan":
            print(json.dumps([item.model_dump(mode="json") for item in desk.run_scan_cycle()], indent=2, ensure_ascii=False))
        elif args.command == "report":
            print(desk.report())
        elif args.command == "kill":
            desk.stop()
            print(config.kill_switch_file)
        elif args.command == "run":
            while True:
                for impulse in desk.run_scan_cycle():
                    print(json.dumps({"id": impulse.id, "verdict": impulse.verdict.value, "path": impulse.path}, ensure_ascii=False), flush=True)
                time.sleep(config.scan_interval_sec)
    except KeyboardInterrupt:
        return 0
    finally:
        desk.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
