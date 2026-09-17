from __future__ import annotations

import argparse
import json
import sys
import time

from .config import ExecutionMode, NerveConfig
from .desk import NerveDesk


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NERVE Protocol trading desk")
    parser.add_argument("command", choices=("paper-scan", "chain-check", "preflight", "reconcile", "report", "kill", "run"))
    args = parser.parse_args(argv)
    config = NerveConfig.from_env()
    if args.command == "paper-scan":
        config = NerveConfig.model_validate({**config.model_dump(), "execution_mode": ExecutionMode.PAPER, "live_trading_enabled": False})
    if args.command in {"chain-check", "preflight"}:
        from web3 import Web3

        from .rpc import RetryingHTTPProvider

        w3 = Web3(RetryingHTTPProvider(config.rpc_url, request_kwargs={"timeout": 10}))
        connected = w3.is_connected()
        chain_id = int(w3.eth.chain_id) if connected else None
        result: dict[str, object] = {"connected": connected, "chain_id": chain_id, "expected": config.chain_id}
        if args.command == "preflight" and connected and chain_id == config.chain_id:
            addresses = {
                "weth": config.weth_address, "factory": config.factory_address,
                "quoter": config.quoter_address, "router": config.router_address,
            }
            result["block"] = int(w3.eth.block_number)
            result["gas_gwei"] = float(w3.from_wei(w3.eth.gas_price, "gwei"))
            bytecode = {
                name: bool(w3.eth.get_code(Web3.to_checksum_address(address)))
                for name, address in addresses.items()
            }
            result["bytecode"] = bytecode
            result["preflight_ok"] = all(bytecode.values())
        print(json.dumps(result))
        return 0 if connected and chain_id == config.chain_id and result.get("preflight_ok", True) else 1
    if args.command == "reconcile":
        return _reconcile(config)
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
            last_report = 0.0
            while True:
                for impulse in desk.run_scan_cycle():
                    print(json.dumps({"id": impulse.id, "verdict": impulse.verdict.value, "path": impulse.path}, ensure_ascii=False), flush=True)
                unresolved = desk.reconcile()
                if unresolved:
                    print(json.dumps({"event": "reconcile_required", "intents": unresolved}, ensure_ascii=False), flush=True)
                if time.monotonic() - last_report >= config.report_interval_sec:
                    print(json.dumps({"event": "report", "text": desk.report()}, ensure_ascii=False), flush=True)
                    last_report = time.monotonic()
                time.sleep(config.scan_interval_sec)
    except KeyboardInterrupt:
        return 0
    finally:
        desk.close()
    return 0


def _reconcile(config: NerveConfig) -> int:
    """Read-only: opens the store and an RPC reader, never the signer or the desk."""
    from .chainread import JsonRpcReader
    from .reconcile import RpcReconcileChain, reconcile, render, report_divergences
    from .store import NerveStore

    config.ensure_runtime_dirs()
    store = NerveStore(config.db_path)
    try:
        rows = reconcile(store, lambda: RpcReconcileChain(JsonRpcReader(config.rpc_url)), config.wallet_address)
    except Exception as exc:
        print(f"reconcile failed: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()
    print(render(rows))
    report_divergences(rows)
    return 1 if any(row.divergent for row in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
