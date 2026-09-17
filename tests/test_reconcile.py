from decimal import Decimal
from typing import Any

import pytest

from nerve.cli import main
from nerve.models import ChainName, PoolObservation
from nerve.reconcile import reconcile, render
from nerve.store import NerveStore

WALLET = "0x9999999999999999999999999999999999999999"
TOKEN = "0x1111111111111111111111111111111111111111"


class FakeChain:
    def __init__(self, pending: int, latest: int, receipts: dict[str, dict[str, Any]] | None = None,
                 balance: int = 0) -> None:
        self.pending, self.latest, self.receipts, self.balance = pending, latest, receipts or {}, balance

    def nonce(self, wallet: str, tag: str) -> int:
        return self.pending if tag == "pending" else self.latest

    def receipt(self, tx_hash: str) -> dict[str, Any] | None:
        return self.receipts.get(tx_hash)

    def token_balance(self, token: str, wallet: str) -> int:
        return self.balance


def unknown_intent(store: NerveStore, client_id: str, nonce: int, tx_hash: str = "") -> None:
    store.create_intent(client_id, client_id, None, {"token": TOKEN, "size_usd": "500"})
    store.update_intent(client_id, "unknown", nonce=nonce, tx_hash=tx_hash or None, leg="swap")


def test_reconcile_resolves_unknown_intents_by_nonce(tmp_path) -> None:
    store = NerveStore(tmp_path / "r.db")
    unknown_intent(store, "nerve-free", nonce=9)
    unknown_intent(store, "nerve-reverted", nonce=3, tx_hash="0xdead")
    chain = FakeChain(pending=9, latest=9, receipts={"0xdead": {"status": "0x0", "blockNumber": "0x10"}})
    rows = reconcile(store, lambda: chain, WALLET)
    assert store.get_intent("nerve-free")["status"] == "never_sent"  # type: ignore[index]
    assert store.get_intent("nerve-reverted")["status"] == "reverted"  # type: ignore[index]
    assert not any(row.divergent for row in rows)
    store.close()


def test_reconcile_flags_unbacked_position_without_correcting_it(tmp_path) -> None:
    store = NerveStore(tmp_path / "r.db")
    impulse = PoolObservation(chain=ChainName.ROBINHOOD, token=TOKEN, pool="0xpool", liquidity_usd=Decimal("1"),
                              volume_1h_usd=Decimal("0"), volume_24h_usd=Decimal("0")).to_impulse()
    impulse.tx_hash, impulse.size_usd = "0xabc", Decimal("500")
    store.record_position(impulse)
    rows = reconcile(store, lambda: FakeChain(pending=0, latest=0, balance=0), WALLET)
    assert [row.divergent for row in rows] == [True]
    assert "NOT BACKED" in render(rows)
    assert store.open_position_count() == 1
    store.close()


def test_reconcile_cli_on_empty_store_needs_no_rpc(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "empty.db"))
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "KILL"))
    monkeypatch.setenv("RPC_URL", "http://127.0.0.1:9")
    assert main(["reconcile"]) == 0
    assert "nothing to reconcile" in capsys.readouterr().out


def test_reconcile_requires_wallet_when_there_is_work(tmp_path) -> None:
    store = NerveStore(tmp_path / "r.db")
    unknown_intent(store, "nerve-x", nonce=1)
    with pytest.raises(ValueError, match="WALLET_ADDRESS"):
        reconcile(store, lambda: FakeChain(pending=0, latest=0), "")
    store.close()
