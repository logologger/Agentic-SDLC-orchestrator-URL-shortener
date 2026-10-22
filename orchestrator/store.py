"""Durable run state: artifacts (versioned), task state, and a tamper-evident audit log.

Every event carries `hash = sha256(prev_hash + canonical(event))`, so editing or deleting any row
breaks verification for everything after it (audit-grade traceability).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, scenario TEXT, requirement TEXT, status TEXT, started REAL, ended REAL);
CREATE TABLE IF NOT EXISTS artifacts (
    run_id TEXT, name TEXT, version INTEGER, hash TEXT, produced_by TEXT, content TEXT, created REAL,
    PRIMARY KEY (run_id, name, version));
CREATE TABLE IF NOT EXISTS tasks (
    run_id TEXT, task_id TEXT, state TEXT, attempts INTEGER, updated REAL, PRIMARY KEY (run_id, task_id));
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts REAL, kind TEXT, task_id TEXT,
    payload TEXT, prev_hash TEXT, hash TEXT);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
"""


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def digest(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


class Store:
    def __init__(self, path: str = ":memory:", clock=time.time):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.RLock()
        self._clock = clock
        self._tails: Dict[str, str] = {}
        with self._lock:
            self._conn.executescript(SCHEMA)

    # ---- runs -------------------------------------------------------------------------------
    def create_run(self, run_id: str, scenario: str, requirement: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,NULL)",
                               (run_id, scenario, requirement, "RUNNING", self._clock()))

    def finish_run(self, run_id: str, status: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE runs SET status=?, ended=? WHERE run_id=?", (status, self._clock(), run_id))

    def run_row(self, run_id: str) -> Dict[str, Any]:
        with self._lock:
            r = self._conn.execute("SELECT run_id, scenario, requirement, status, started, ended FROM runs WHERE run_id=?",
                                   (run_id,)).fetchone()
        return dict(zip(("run_id", "scenario", "requirement", "status", "started", "ended"), r)) if r else {}

    def run_ids(self) -> List[str]:
        with self._lock:
            return [r[0] for r in self._conn.execute("SELECT run_id FROM runs ORDER BY started")]

    # ---- events (hash chained) ----------------------------------------------------------------
    def log(self, run_id: str, kind: str, task_id: str = "", **payload: Any) -> Dict[str, Any]:
        with self._lock, self._conn:
            prev = self._tails.get(run_id)
            if prev is None:
                row = self._conn.execute("SELECT hash FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,)).fetchone()
                prev = row[0] if row else GENESIS
            ts = self._clock()
            body = canonical(payload)
            h = hashlib.sha256((prev + run_id + f"{ts:.6f}" + kind + task_id + body).encode()).hexdigest()
            cur = self._conn.execute(
                "INSERT INTO events (run_id, ts, kind, task_id, payload, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
                (run_id, ts, kind, task_id, body, prev, h))
            self._tails[run_id] = h
            return {"seq": cur.lastrowid, "ts": ts, "kind": kind, "task_id": task_id, "payload": payload}

    def events(self, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        q, args = "SELECT seq, run_id, ts, kind, task_id, payload FROM events", ()
        if run_id:
            q, args = q + " WHERE run_id=?", (run_id,)
        with self._lock:
            rows = self._conn.execute(q + " ORDER BY seq", args).fetchall()
        return [{"seq": s, "run_id": r, "ts": t, "kind": k, "task_id": tid, "payload": json.loads(p)}
                for s, r, t, k, tid, p in rows]

    def verify_chain(self, run_id: str) -> Tuple[bool, Optional[int]]:
        """Return (ok, first_bad_seq)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, ts, kind, task_id, payload, prev_hash, hash FROM events WHERE run_id=? ORDER BY seq",
                (run_id,)).fetchall()
        prev = GENESIS
        for seq, ts, kind, task_id, payload, prev_hash, h in rows:
            expect = hashlib.sha256((prev + run_id + f"{ts:.6f}" + kind + task_id + payload).encode()).hexdigest()
            if prev_hash != prev or h != expect:
                return False, seq
            prev = h
        return True, None

    # ---- artifacts ----------------------------------------------------------------------------
    def put_artifact(self, run_id: str, name: str, content: Any, produced_by: str) -> Tuple[int, bool, str]:
        """Store content; a new version is created only if the content hash changed."""
        h = digest(content)
        with self._lock, self._conn:
            row = self._conn.execute("SELECT version, hash FROM artifacts WHERE run_id=? AND name=? ORDER BY version DESC LIMIT 1",
                                     (run_id, name)).fetchone()
            if row and row[1] == h:
                return row[0], False, h
            version = (row[0] + 1) if row else 1
            self._conn.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?)",
                               (run_id, name, version, h, produced_by, canonical(content), self._clock()))
            return version, True, h

    def get_artifact(self, run_id: str, name: str, version: Optional[int] = None) -> Optional[Tuple[Any, int, str]]:
        q = "SELECT content, version, hash FROM artifacts WHERE run_id=? AND name=?"
        args: tuple = (run_id, name)
        if version is not None:
            q, args = q + " AND version=?", args + (version,)
        with self._lock:
            row = self._conn.execute(q + " ORDER BY version DESC LIMIT 1", args).fetchone()
        return (json.loads(row[0]), row[1], row[2]) if row else None

    def latest_version(self, run_id: str, name: str) -> int:
        got = self.get_artifact(run_id, name)
        return got[1] if got else 0

    def artifact_names(self, run_id: str, prefix: str = "") -> List[str]:
        with self._lock:
            rows = self._conn.execute("SELECT DISTINCT name FROM artifacts WHERE run_id=? AND name LIKE ? ORDER BY name",
                                      (run_id, prefix + "%")).fetchall()
        return [r[0] for r in rows]

    # ---- tasks --------------------------------------------------------------------------------
    def set_task(self, run_id: str, task_id: str, state: str, attempts: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?)", (run_id, task_id, state, attempts, self._clock()))

    def tasks(self, run_id: str) -> Dict[str, Tuple[str, int]]:
        with self._lock:
            rows = self._conn.execute("SELECT task_id, state, attempts FROM tasks WHERE run_id=?", (run_id,)).fetchall()
        return {t: (s, a) for t, s, a in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
