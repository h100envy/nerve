from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .lab.models import (
    DecisionStatus,
    ExecutionObservation,
    ExperimentDecision,
    ForwardOutcome,
    ObservationSnapshot,
    OutcomeStatus,
    canonical_json,
)
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

            CREATE TABLE IF NOT EXISTS observation_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                impulse_id TEXT NOT NULL,
                chain TEXT NOT NULL,
                token TEXT NOT NULL,
                pool TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                stage TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS experiment_decisions (
                decision_id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                arm_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                provider TEXT,
                requested_model_id TEXT,
                returned_model_id TEXT,
                question_version TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                deadline_at TEXT NOT NULL,
                applied_at TEXT,
                latency_ms INTEGER NOT NULL,
                raw_answer_json TEXT,
                reason TEXT NOT NULL,
                flags_json TEXT NOT NULL,
                FOREIGN KEY(snapshot_id) REFERENCES observation_snapshots(snapshot_id),
                UNIQUE(snapshot_id, arm_id)
            );
            CREATE TABLE IF NOT EXISTS forward_outcomes (
                outcome_id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL,
                horizon_seconds INTEGER NOT NULL,
                target_at TEXT NOT NULL,
                resolved_at TEXT,
                reference_price TEXT,
                outcome_price TEXT,
                return_pct TEXT,
                realized_label INTEGER,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                FOREIGN KEY(snapshot_id) REFERENCES observation_snapshots(snapshot_id),
                UNIQUE(snapshot_id, horizon_seconds, status)
            );
            CREATE TABLE IF NOT EXISTS execution_observations (
                observation_id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL,
                arm_id TEXT NOT NULL,
                signal_price TEXT,
                quote_at TEXT,
                quote_price TEXT,
                obtainable_quantity TEXT,
                fee TEXT,
                impact_bps INTEGER,
                executable_entry TEXT,
                executable_exit TEXT,
                net_return TEXT,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                FOREIGN KEY(snapshot_id) REFERENCES observation_snapshots(snapshot_id)
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_observed
                ON observation_snapshots(observed_at);
            CREATE INDEX IF NOT EXISTS idx_snapshots_impulse
                ON observation_snapshots(impulse_id);
            CREATE INDEX IF NOT EXISTS idx_decisions_snapshot
                ON experiment_decisions(snapshot_id);
            CREATE INDEX IF NOT EXISTS idx_decisions_group
                ON experiment_decisions(returned_model_id, requested_model_id, question_version);
            CREATE INDEX IF NOT EXISTS idx_outcomes_target
                ON forward_outcomes(status, target_at);
            CREATE INDEX IF NOT EXISTS idx_execution_snapshot
                ON execution_observations(snapshot_id);
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
            "UPDATE intents SET status=?,tx_hash=COALESCE(?,tx_hash),nonce=COALESCE(?,nonce),payload=?,updated_at=? WHERE client_id=?",
            (status, fields.get("tx_hash"), fields.get("nonce"), json.dumps(payload, sort_keys=True),
             datetime.now(UTC).isoformat(), client_id),
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

    def intents_with_status(self, status: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM intents WHERE status=? ORDER BY updated_at", (status,)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def open_positions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM positions WHERE status='open' ORDER BY opened_at").fetchall()
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

    # Measurement sidecar -------------------------------------------------

    def record_observation_snapshot(self, snapshot: ObservationSnapshot) -> None:
        self.conn.execute(
            """INSERT INTO observation_snapshots(
                   snapshot_id,impulse_id,chain,token,pool,observed_at,captured_at,
                   stage,input_hash,payload_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                snapshot.snapshot_id,
                snapshot.impulse_id,
                snapshot.chain,
                snapshot.token,
                snapshot.pool,
                snapshot.observed_at.isoformat(),
                snapshot.captured_at.isoformat(),
                snapshot.stage,
                snapshot.input_hash,
                snapshot.payload_json,
            ),
        )
        self.conn.commit()

    def get_observation_snapshot(self, snapshot_id: str) -> ObservationSnapshot | None:
        row = self.conn.execute(
            "SELECT * FROM observation_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        return self._snapshot_from_row(row) if row is not None else None

    def list_observation_snapshots(self, limit: int | None = None) -> list[ObservationSnapshot]:
        sql = "SELECT * FROM observation_snapshots ORDER BY observed_at DESC"
        params: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        return [self._snapshot_from_row(row) for row in self.conn.execute(sql, params).fetchall()]

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> ObservationSnapshot:
        return ObservationSnapshot(
            snapshot_id=str(row["snapshot_id"]),
            impulse_id=str(row["impulse_id"]),
            chain=str(row["chain"]),
            token=str(row["token"]),
            pool=str(row["pool"]),
            observed_at=datetime.fromisoformat(str(row["observed_at"])),
            captured_at=datetime.fromisoformat(str(row["captured_at"])),
            stage=str(row["stage"]),
            input_hash=str(row["input_hash"]),
            payload_json=str(row["payload_json"]),
        )

    def record_experiment_decision(self, decision: ExperimentDecision) -> None:
        snapshot = self.conn.execute(
            "SELECT input_hash FROM observation_snapshots WHERE snapshot_id=?",
            (decision.snapshot_id,),
        ).fetchone()
        if snapshot is None:
            raise ValueError("experiment decision references an unknown snapshot")
        if str(snapshot["input_hash"]) != decision.input_hash:
            raise ValueError("experiment decision input_hash does not match its snapshot")
        self.conn.execute(
            """INSERT INTO experiment_decisions(
                   decision_id,snapshot_id,input_hash,arm_id,strategy_id,provider,
                   requested_model_id,returned_model_id,question_version,status,
                   started_at,completed_at,deadline_at,applied_at,latency_ms,
                   raw_answer_json,reason,flags_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                decision.decision_id,
                decision.snapshot_id,
                decision.input_hash,
                decision.arm_id,
                decision.strategy_id,
                decision.provider,
                decision.requested_model_id,
                decision.returned_model_id,
                decision.question_version,
                decision.status.value,
                decision.started_at.isoformat(),
                decision.completed_at.isoformat(),
                decision.deadline_at.isoformat(),
                decision.applied_at.isoformat() if decision.applied_at else None,
                decision.latency_ms,
                canonical_json(decision.raw_answer) if decision.raw_answer is not None else None,
                decision.reason,
                canonical_json(decision.flags),
            ),
        )
        self.conn.commit()

    def list_experiment_decisions(self, snapshot_id: str | None = None) -> list[ExperimentDecision]:
        if snapshot_id is None:
            rows = self.conn.execute(
                "SELECT * FROM experiment_decisions ORDER BY started_at"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM experiment_decisions WHERE snapshot_id=? ORDER BY started_at",
                (snapshot_id,),
            ).fetchall()
        return [self._decision_from_row(row) for row in rows]

    @staticmethod
    def _decision_from_row(row: sqlite3.Row) -> ExperimentDecision:
        return ExperimentDecision(
            decision_id=str(row["decision_id"]),
            snapshot_id=str(row["snapshot_id"]),
            input_hash=str(row["input_hash"]),
            arm_id=str(row["arm_id"]),
            strategy_id=str(row["strategy_id"]),
            provider=row["provider"],
            requested_model_id=row["requested_model_id"],
            returned_model_id=row["returned_model_id"],
            question_version=str(row["question_version"]),
            status=DecisionStatus(str(row["status"])),
            started_at=datetime.fromisoformat(str(row["started_at"])),
            completed_at=datetime.fromisoformat(str(row["completed_at"])),
            deadline_at=datetime.fromisoformat(str(row["deadline_at"])),
            applied_at=(datetime.fromisoformat(str(row["applied_at"])) if row["applied_at"] else None),
            latency_ms=int(row["latency_ms"]),
            raw_answer=(json.loads(str(row["raw_answer_json"])) if row["raw_answer_json"] else None),
            reason=str(row["reason"]),
            flags=json.loads(str(row["flags_json"])),
        )

    def record_forward_outcome(self, outcome: ForwardOutcome) -> None:
        self.conn.execute(
            """INSERT INTO forward_outcomes(
                   outcome_id,snapshot_id,horizon_seconds,target_at,resolved_at,reference_price,
                   outcome_price,return_pct,realized_label,source,status
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                outcome.outcome_id,
                outcome.snapshot_id,
                outcome.horizon_seconds,
                outcome.target_at.isoformat(),
                outcome.resolved_at.isoformat() if outcome.resolved_at else None,
                str(outcome.reference_price) if outcome.reference_price is not None else None,
                str(outcome.outcome_price) if outcome.outcome_price is not None else None,
                str(outcome.return_pct) if outcome.return_pct is not None else None,
                int(outcome.realized_label) if outcome.realized_label is not None else None,
                outcome.source,
                outcome.status.value,
            ),
        )
        self.conn.commit()

    def list_forward_outcomes(self, snapshot_id: str | None = None) -> list[ForwardOutcome]:
        """Return the latest event for each snapshot/horizon pair."""
        if snapshot_id is None:
            rows = self.conn.execute(
                """SELECT current.* FROM forward_outcomes current
                   WHERE current.rowid=(
                       SELECT MAX(candidate.rowid) FROM forward_outcomes candidate
                       WHERE candidate.snapshot_id=current.snapshot_id
                         AND candidate.horizon_seconds=current.horizon_seconds
                   )
                   ORDER BY current.target_at,current.horizon_seconds"""
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT current.* FROM forward_outcomes current
                   WHERE current.snapshot_id=? AND current.rowid=(
                       SELECT MAX(candidate.rowid) FROM forward_outcomes candidate
                       WHERE candidate.snapshot_id=current.snapshot_id
                         AND candidate.horizon_seconds=current.horizon_seconds
                   )
                   ORDER BY current.horizon_seconds""",
                (snapshot_id,),
            ).fetchall()
        return [self._outcome_from_row(row) for row in rows]

    def list_forward_outcome_events(self, snapshot_id: str | None = None) -> list[ForwardOutcome]:
        if snapshot_id is None:
            rows = self.conn.execute(
                "SELECT * FROM forward_outcomes ORDER BY rowid"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM forward_outcomes WHERE snapshot_id=? ORDER BY rowid",
                (snapshot_id,),
            ).fetchall()
        return [self._outcome_from_row(row) for row in rows]

    @staticmethod
    def _outcome_from_row(row: sqlite3.Row) -> ForwardOutcome:
        return ForwardOutcome(
            outcome_id=str(row["outcome_id"]),
            snapshot_id=str(row["snapshot_id"]),
            horizon_seconds=int(row["horizon_seconds"]),
            target_at=datetime.fromisoformat(str(row["target_at"])),
            resolved_at=(datetime.fromisoformat(str(row["resolved_at"])) if row["resolved_at"] else None),
            reference_price=row["reference_price"],
            outcome_price=row["outcome_price"],
            return_pct=row["return_pct"],
            realized_label=(bool(row["realized_label"]) if row["realized_label"] is not None else None),
            source=str(row["source"]),
            status=OutcomeStatus(str(row["status"])),
        )

    def record_execution_observation(self, observation: ExecutionObservation) -> None:
        if observation.executable_entry is not None:
            decision = self.conn.execute(
                """SELECT status,completed_at FROM experiment_decisions
                   WHERE snapshot_id=? AND arm_id=?""",
                (observation.snapshot_id, observation.arm_id),
            ).fetchone()
            if decision is None:
                raise ValueError("an executable observation requires its audited experiment decision")
            if str(decision["status"]) == DecisionStatus.EXPIRED.value:
                raise ValueError("an expired experiment decision cannot become executable")
            completed_at = datetime.fromisoformat(str(decision["completed_at"]))
            if observation.quote_at is None or observation.quote_at < completed_at:
                raise ValueError("executable quote cannot precede decision completion")
        self.conn.execute(
            """INSERT INTO execution_observations(
                   observation_id,snapshot_id,arm_id,signal_price,quote_at,quote_price,
                   obtainable_quantity,fee,impact_bps,executable_entry,executable_exit,
                   net_return,source,status
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                observation.observation_id,
                observation.snapshot_id,
                observation.arm_id,
                str(observation.signal_price) if observation.signal_price is not None else None,
                observation.quote_at.isoformat() if observation.quote_at else None,
                str(observation.quote_price) if observation.quote_price is not None else None,
                (str(observation.obtainable_quantity) if observation.obtainable_quantity is not None else None),
                str(observation.fee) if observation.fee is not None else None,
                observation.impact_bps,
                (str(observation.executable_entry) if observation.executable_entry is not None else None),
                (str(observation.executable_exit) if observation.executable_exit is not None else None),
                str(observation.net_return) if observation.net_return is not None else None,
                observation.source,
                observation.status,
            ),
        )
        self.conn.commit()

    def list_execution_observations(self, snapshot_id: str | None = None) -> list[ExecutionObservation]:
        if snapshot_id is None:
            rows = self.conn.execute(
                "SELECT * FROM execution_observations ORDER BY rowid"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM execution_observations WHERE snapshot_id=? ORDER BY rowid",
                (snapshot_id,),
            ).fetchall()
        return [self._execution_from_row(row) for row in rows]

    @staticmethod
    def _execution_from_row(row: sqlite3.Row) -> ExecutionObservation:
        return ExecutionObservation(
            observation_id=str(row["observation_id"]),
            snapshot_id=str(row["snapshot_id"]),
            arm_id=str(row["arm_id"]),
            signal_price=row["signal_price"],
            quote_at=(datetime.fromisoformat(str(row["quote_at"])) if row["quote_at"] else None),
            quote_price=row["quote_price"],
            obtainable_quantity=row["obtainable_quantity"],
            fee=row["fee"],
            impact_bps=row["impact_bps"],
            executable_entry=row["executable_entry"],
            executable_exit=row["executable_exit"],
            net_return=row["net_return"],
            source=str(row["source"]),
            status=str(row["status"]),
        )

    def arm_sample_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT arm_id,COUNT(*) AS n FROM experiment_decisions GROUP BY arm_id"
        ).fetchall()
        return {str(row["arm_id"]): int(row["n"]) for row in rows}

    def calibration_samples(self) -> dict[tuple[str, str, int], list[tuple[float, bool]]]:
        """Return strictly versioned probability/label pairs for reporting."""
        rows = self.conn.execute(
            """SELECT d.requested_model_id,d.returned_model_id,d.question_version,
                      d.raw_answer_json,o.horizon_seconds,o.realized_label
               FROM experiment_decisions d
               JOIN forward_outcomes o ON o.snapshot_id=d.snapshot_id
               WHERE o.status='resolved' AND o.realized_label IS NOT NULL
                 AND d.raw_answer_json IS NOT NULL"""
        ).fetchall()
        result: dict[tuple[str, str, int], list[tuple[float, bool]]] = {}
        for row in rows:
            answer = json.loads(str(row["raw_answer_json"]))
            probabilities = answer.get("probabilities")
            if not isinstance(probabilities, dict):
                continue
            probability = probabilities.get(str(row["horizon_seconds"]))
            if not isinstance(probability, (int, float)) or isinstance(probability, bool):
                continue
            if not 0 <= float(probability) <= 1:
                continue
            model_id = row["returned_model_id"] or row["requested_model_id"]
            if not model_id:
                continue
            key = (str(model_id), str(row["question_version"]), int(row["horizon_seconds"]))
            result.setdefault(key, []).append((float(probability), bool(row["realized_label"])))
        return result
