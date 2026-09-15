from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import Impulse


class NerveStore:
    """Durable audit trail. Nodes remain stateless between cycles."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS impulses (
                id TEXT PRIMARY KEY, chain TEXT, token TEXT, pool TEXT,
                score INTEGER, verdict TEXT, path TEXT, payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, impulse_id TEXT NOT NULL,
                node TEXT NOT NULL, verdict TEXT NOT NULL, note TEXT, ts TEXT NOT NULL,
                FOREIGN KEY(impulse_id) REFERENCES impulses(id)
            );
            CREATE TABLE IF NOT EXISTS intents (
                client_id TEXT PRIMARY KEY, impulse_id TEXT NOT NULL,
                status TEXT NOT NULL, nonce INTEGER, tx_hash TEXT,
                payload TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS positions (
                token TEXT PRIMARY KEY, pool TEXT NOT NULL, size_usd TEXT NOT NULL,
                entry_price TEXT NOT NULL, tx_hash TEXT NOT NULL, opened_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
            );
            CREATE INDEX IF NOT EXISTS idx_impulses_created ON impulses(created_at);
            CREATE INDEX IF NOT EXISTS idx_transitions_impulse ON transitions(impulse_id);
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def save(self, impulse: Impulse) -> None:
        self.conn.execute(
            """INSERT INTO impulses(id,chain,token,pool,score,verdict,path,payload,created_at)
               VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
               score=excluded.score, verdict=excluded.verdict, path=excluded.path, payload=excluded.payload""",
            (impulse.id, str(impulse.chain), impulse.token, impulse.pool, impulse.score,
             impulse.verdict.value, impulse.path, impulse.model_dump_json(), impulse.created_at.isoformat()),
        )
        self.conn.commit()

    def log_transition(self, impulse: Impulse) -> None:
        if not impulse.history:
            return
        transition = impulse.history[-1]
        exists = self.conn.execute(
            "SELECT 1 FROM transitions WHERE impulse_id=? AND node=? AND ts=?",
            (impulse.id, transition.node.value, transition.ts.isoformat()),
        ).fetchone()
        if exists is None:
            self.conn.execute(
                "INSERT INTO transitions(impulse_id,node,verdict,note,ts) VALUES(?,?,?,?,?)",
                (impulse.id, transition.node.value, transition.verdict.value,
                 transition.note, transition.ts.isoformat()),
            )
            self.conn.commit()

    def create_intent(self, client_id: str, impulse_id: str, nonce: int | None, payload: dict[str, Any]) -> bool:
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO intents(client_id,impulse_id,status,nonce,payload,updated_at) VALUES(?,?,?,?,?,?)",
            (client_id, impulse_id, "prepared", nonce, json.dumps(payload, sort_keys=True), datetime.now(UTC).isoformat()),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def update_intent(self, client_id: str, status: str, **fields: Any) -> None:
        row = self.conn.execute("SELECT payload FROM intents WHERE client_id=?", (client_id,)).fetchone()
        if row is None:
            raise KeyError(client_id)
        payload = json.loads(row["payload"])
        payload.update(fields)
        self.conn.execute(
            "UPDATE intents SET status=?,tx_hash=COALESCE(?,tx_hash),payload=?,updated_at=? WHERE client_id=?",
            (status, fields.get("tx_hash"), json.dumps(payload, sort_keys=True), datetime.now(UTC).isoformat(), client_id),
        )
        self.conn.commit()

    def get_intent(self, client_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM intents WHERE client_id=?", (client_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def unknown_intents(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM intents WHERE status IN ('prepared','unknown','submitted')").fetchall()
        return [dict(row) for row in rows]

    def record_position(self, impulse: Impulse) -> None:
        self.conn.execute(
            """INSERT INTO positions(token,pool,size_usd,entry_price,tx_hash,opened_at,status)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(token) DO UPDATE SET
               size_usd=excluded.size_usd, entry_price=excluded.entry_price,
               tx_hash=excluded.tx_hash, status='open'""",
            (impulse.token, impulse.pool, str(impulse.size_usd), str(impulse.entry_price),
             impulse.tx_hash, datetime.now(UTC).isoformat(), "open"),
        )
        self.conn.commit()

    def held_tokens(self) -> set[str]:
        rows = self.conn.execute("SELECT token FROM positions WHERE status='open'").fetchall()
        return {str(row["token"]) for row in rows}

    def open_position_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM positions WHERE status='open'").fetchone()
        return int(row["n"])

    def funnel(self, hours: int = 24) -> dict[str, Any]:
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        rows = self.conn.execute("SELECT verdict,COUNT(*) AS n FROM impulses WHERE created_at>=? GROUP BY verdict", (cutoff,)).fetchall()
        return {str(row["verdict"]): int(row["n"]) for row in rows}
