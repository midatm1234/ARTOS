"""SQLite source of truth for runs, events, resources, sessions, and jobs.

Design rules:

* Every state change is a single ``BEGIN IMMEDIATE`` transaction, so two
  controllers cannot interleave a read-check-write on the same run.
* Events are append-only and hash-chained (each event stores the hash of the
  previous one), so silent edits to history are detectable with
  :meth:`Store.verify_event_chain`.
* Jobs are keyed by an idempotency key derived from the experiment spec. A
  resumed workflow looks the key up before submitting, which is what prevents
  duplicate training jobs.
* Run transitions go through :mod:`artos.state_machine` *and* the scientific
  gates in :mod:`artos.gates` unless a caller explicitly opts out (tests only).
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .models import canonical_sha256
from .state_machine import validate_transition
from .utils import utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    route TEXT,
    run_dir TEXT NOT NULL,
    source_type TEXT NOT NULL,
    mother_question_sha256 TEXT NOT NULL,
    contract_sha256 TEXT,
    repair_rounds INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, seq);
CREATE TABLE IF NOT EXISTS resources (
    resource_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    session_name TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    role TEXT NOT NULL,
    tmux_name TEXT,
    spec_path TEXT NOT NULL,
    work_dir TEXT NOT NULL,
    model TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    idempotency_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    variant_id TEXT NOT NULL,
    seed INTEGER NOT NULL,
    executor TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    external_job_id TEXT,
    spec TEXT NOT NULL,
    record TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_run ON jobs(run_id);
CREATE TABLE IF NOT EXISTS leases (
    run_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    expires_at REAL NOT NULL
);
"""

GENESIS_HASH = "0" * 64


class StoreError(RuntimeError):
    pass


class LeaseHeld(StoreError):
    pass


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        self._db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '2')")

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _tx(self):
        return _Transaction(self._db)

    # -- events -----------------------------------------------------------
    def _append_event(self, event_type: str, payload: dict[str, Any], run_id: str | None) -> None:
        row = self._db.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        prev = row["hash"] if row else GENESIS_HASH
        created = utc_now()
        body = {"run_id": run_id, "event_type": event_type, "payload": payload, "created_at": created, "prev_hash": prev}
        digest = canonical_sha256(body)
        self._db.execute(
            "INSERT INTO events(run_id, event_type, payload, created_at, prev_hash, hash) VALUES (?,?,?,?,?,?)",
            (run_id, event_type, json.dumps(payload, sort_keys=True, default=str), created, prev, digest),
        )

    def record_event(self, event_type: str, payload: dict[str, Any], run_id: str | None = None) -> None:
        with self._tx():
            self._append_event(event_type, payload, run_id)

    def events(self, run_id: str | None = None) -> list[dict[str, Any]]:
        if run_id is None:
            rows = self._db.execute("SELECT * FROM events ORDER BY seq").fetchall()
        else:
            rows = self._db.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    def verify_event_chain(self) -> bool:
        prev = GENESIS_HASH
        for row in self._db.execute("SELECT * FROM events ORDER BY seq"):
            body = {
                "run_id": row["run_id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload"]),
                "created_at": row["created_at"],
                "prev_hash": row["prev_hash"],
            }
            if row["prev_hash"] != prev or canonical_sha256(body) != row["hash"]:
                return False
            prev = row["hash"]
        return True

    # -- runs -------------------------------------------------------------
    def create_run(self, record: dict[str, Any]) -> None:
        now = utc_now()
        with self._tx():
            if self._db.execute("SELECT 1 FROM runs WHERE run_id=?", (record["run_id"],)).fetchone():
                raise StoreError(f"run {record['run_id']} already exists")
            self._db.execute(
                "INSERT INTO runs(run_id, project, title, status, run_dir, source_type, mother_question_sha256, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    record["run_id"], record["project"], record["title"], "intake_created", record["run_dir"],
                    record["source_type"], record["mother_question_sha256"], now, now,
                ),
            )
            self._append_event("run_created", {k: v for k, v in record.items() if k != "source_text"}, record["run_id"])

    def get_run(self, run_id: str) -> dict[str, Any]:
        row = self._db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown run {run_id}")
        return dict(row)

    def list_runs(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT * FROM runs ORDER BY created_at DESC, run_id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def transition(
        self,
        run_id: str,
        requested: str,
        payload: dict[str, Any] | None = None,
        *,
        enforce_gates: bool = True,
        config: dict[str, Any] | None = None,
    ) -> None:
        from .gates import check_transition_gate

        payload = dict(payload or {})
        with self._tx():
            run = self.get_run(run_id)
            current = run["status"]
            validate_transition(current, requested)
            if enforce_gates:
                gate = check_transition_gate(run, requested, config or {}, self)
                payload["gate"] = gate
            else:
                payload["gate"] = {"enforced": False}
            updates = {"status": requested, "updated_at": utc_now()}
            if requested in {"routed_direct", "routed_hybrid", "routed_hypothesis"}:
                updates["route"] = requested.removeprefix("routed_")
            if current == "repair_required" and requested == "primary_running":
                updates["repair_rounds"] = int(run["repair_rounds"]) + 1
            if requested == "contract_frozen" and payload.get("sha256"):
                updates["contract_sha256"] = payload["sha256"]
            assignments = ", ".join(f"{k}=?" for k in updates)
            self._db.execute(f"UPDATE runs SET {assignments} WHERE run_id=?", (*updates.values(), run_id))
            self._append_event("transition", {"from": current, "to": requested, **payload}, run_id)

    def set_contract_hash(self, run_id: str, digest: str) -> None:
        with self._tx():
            run = self.get_run(run_id)
            if run["contract_sha256"] and run["contract_sha256"] != digest:
                raise StoreError(
                    f"run {run_id} already has frozen contract {run['contract_sha256'][:12]}; "
                    "a frozen contract cannot be replaced (create a new run or a repair contract)"
                )
            self._db.execute("UPDATE runs SET contract_sha256=?, updated_at=? WHERE run_id=?", (digest, utc_now(), run_id))
            self._append_event("contract_hash_recorded", {"sha256": digest}, run_id)

    def request_cancel(self, run_id: str, reason: str) -> None:
        with self._tx():
            self.get_run(run_id)
            self._db.execute("UPDATE runs SET cancel_requested=1, updated_at=? WHERE run_id=?", (utc_now(), run_id))
            self._append_event("cancel_requested", {"reason": reason}, run_id)

    # -- leases (one controller per run) ----------------------------------
    def acquire_lease(self, run_id: str, owner: str | None = None, ttl_seconds: float = 600, *, takeover_dead: bool = False) -> str:
        owner = owner or f"{socket.gethostname()}:{os.getpid()}"
        now = time.time()
        with self._tx():
            row = self._db.execute("SELECT owner, expires_at FROM leases WHERE run_id=?", (run_id,)).fetchone()
            if row and row["owner"] != owner and row["expires_at"] > now:
                if not (takeover_dead and _owner_is_dead_local(row["owner"])):
                    raise LeaseHeld(f"run {run_id} is being controlled by {row['owner']} until {row['expires_at']:.0f}"
                                    " (use --takeover if that controller is no longer running on this host)")
                self._append_event("lease_taken_over", {"previous_owner": row["owner"], "new_owner": owner}, run_id)
            self._db.execute(
                "INSERT INTO leases(run_id, owner, expires_at) VALUES (?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at",
                (run_id, owner, now + ttl_seconds),
            )
        return owner

    def release_lease(self, run_id: str, owner: str) -> None:
        with self._tx():
            self._db.execute("DELETE FROM leases WHERE run_id=? AND owner=?", (run_id, owner))

    # -- resources --------------------------------------------------------
    def upsert_resource(self, resource: dict[str, Any]) -> None:
        with self._tx():
            self._db.execute(
                "INSERT INTO resources(resource_id, kind, path, payload, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(resource_id) DO UPDATE SET kind=excluded.kind, path=excluded.path, "
                "payload=excluded.payload, updated_at=excluded.updated_at",
                (resource["resource_id"], resource["kind"], resource["path"], json.dumps(resource, sort_keys=True), utc_now()),
            )

    def list_resources(self, kinds: Iterable[str] | None = None) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT payload FROM resources ORDER BY path").fetchall()
        items = [json.loads(r["payload"]) for r in rows]
        if kinds:
            wanted = set(kinds)
            items = [i for i in items if i["kind"] in wanted]
        return items

    # -- sessions ---------------------------------------------------------
    def record_session(self, session: dict[str, Any]) -> None:
        with self._tx():
            self._db.execute(
                "INSERT OR REPLACE INTO sessions(session_name, run_id, role, tmux_name, spec_path, work_dir, model, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    session["session_name"], session["run_id"], session["role"], session.get("tmux_name"),
                    session["spec_path"], session["work_dir"], session.get("model"), session.get("created_at", utc_now()),
                ),
            )
            self._append_event("session_recorded", session, session["run_id"])

    def sessions_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT * FROM sessions WHERE run_id=? ORDER BY created_at", (run_id,)).fetchall()
        return [dict(r) for r in rows]

    # -- jobs -------------------------------------------------------------
    def get_job(self, key: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM jobs WHERE idempotency_key=?", (key,)).fetchone()
        return _job_row(row) if row else None

    def jobs_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT * FROM jobs WHERE run_id=? ORDER BY created_at, idempotency_key", (run_id,)).fetchall()
        return [_job_row(r) for r in rows]

    def claim_job(self, key: str, run_id: str, spec: dict[str, Any], record: dict[str, Any], max_attempts: int) -> tuple[str, dict[str, Any]]:
        """Atomically decide whether a job may be (re)submitted.

        Returns ``(decision, row)`` where decision is one of:

        ``new``        no row existed; a row in state ``submitting`` was written.
        ``retry``      the previous attempt failed and attempts remain; row moved
                       to ``submitting`` with ``attempt + 1``.
        ``existing``   a live or succeeded job exists; do not submit.
        ``exhausted``  failed and no attempts remain.
        ``blocked``    state is ``unknown_after_crash`` or ``submitting``; a human
                       or reconciler must resolve it before anything is submitted.
        """
        now = utc_now()
        with self._tx():
            row = self._db.execute("SELECT * FROM jobs WHERE idempotency_key=?", (key,)).fetchone()
            if row is None:
                record = dict(record, state="submitting", attempt=1)
                self._db.execute(
                    "INSERT INTO jobs(idempotency_key, run_id, variant_id, seed, executor, state, attempt, max_attempts,"
                    " external_job_id, spec, record, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        key, run_id, spec["variant_id"], int(spec["seed"]), spec["executor"], "submitting", 1,
                        max_attempts, None, json.dumps(spec, sort_keys=True), json.dumps(record, sort_keys=True, default=str), now, now,
                    ),
                )
                self._append_event("job_claimed", {"key": key, "attempt": 1}, run_id)
                return "new", self.get_job(key)  # type: ignore[return-value]
            job = _job_row(row)
            state = job["state"]
            if state in {"submitted", "running", "succeeded", "planned"}:
                return "existing", job
            if state in {"submitting", "unknown_after_crash"}:
                return "blocked", job
            if state in {"failed", "cancelled"}:
                if job["attempt"] >= job["max_attempts"]:
                    return "exhausted", job
                attempt = job["attempt"] + 1
                rec = dict(job["record"], state="submitting", attempt=attempt, external_job_id=None, return_code=None)
                self._db.execute(
                    "UPDATE jobs SET state='submitting', attempt=?, external_job_id=NULL, record=?, updated_at=? WHERE idempotency_key=?",
                    (attempt, json.dumps(rec, sort_keys=True, default=str), now, key),
                )
                self._append_event("job_claimed", {"key": key, "attempt": attempt, "previous_state": state}, run_id)
                return "retry", self.get_job(key)  # type: ignore[return-value]
            raise StoreError(f"job {key} has unexpected state {state!r}")

    def update_job(self, key: str, **changes: Any) -> dict[str, Any]:
        with self._tx():
            row = self._db.execute("SELECT * FROM jobs WHERE idempotency_key=?", (key,)).fetchone()
            if row is None:
                raise KeyError(f"unknown job {key}")
            job = _job_row(row)
            record = dict(job["record"])
            record.update(changes)
            columns = {"record": json.dumps(record, sort_keys=True, default=str), "updated_at": utc_now()}
            for column in ("state", "external_job_id", "attempt"):
                if column in changes:
                    columns[column] = changes[column]
            assignments = ", ".join(f"{k}=?" for k in columns)
            self._db.execute(f"UPDATE jobs SET {assignments} WHERE idempotency_key=?", (*columns.values(), key))
            if "state" in changes and changes["state"] != job["state"]:
                self._append_event("job_state", {"key": key, "from": job["state"], "to": changes["state"]}, job["run_id"])
        return self.get_job(key)  # type: ignore[return-value]


def _owner_is_dead_local(owner: str) -> bool:
    """True only when the lease owner is a process on this host that no longer exists."""
    from .procutil import pid_alive

    host, _, pid = owner.rpartition(":")
    if host != socket.gethostname() or not pid.isdigit():
        return False
    return not pid_alive(int(pid))


def _job_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["spec"] = json.loads(data["spec"])
    data["record"] = json.loads(data["record"])
    return data


class _Transaction:
    """Re-entrant ``BEGIN IMMEDIATE`` transaction on an autocommit connection."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.outer = False

    def __enter__(self):
        if not self.db.in_transaction:
            self.db.execute("BEGIN IMMEDIATE")
            self.outer = True
        return self

    def __exit__(self, exc_type, *_):
        if self.outer:
            self.db.execute("ROLLBACK" if exc_type else "COMMIT")
        return False
