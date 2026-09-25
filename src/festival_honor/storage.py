"""荣誉公示领域在基础 SQLite 库之上追加的表结构。"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from festival_foundation.storage import Database, SCHEMA as FOUNDATION_SCHEMA

HONOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_versions (
    version_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    note TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    rule_version_id TEXT NOT NULL REFERENCES rule_versions(version_id),
    rule_snapshot_json TEXT NOT NULL,
    submission_deadline TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','completed')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS candidate_profiles (
    candidate_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_consents (
    consent_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidate_profiles(candidate_id),
    granted INTEGER NOT NULL CHECK(granted IN (0,1)),
    granted_fields_json TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_by TEXT
);
CREATE TABLE IF NOT EXISTS conflict_relations (
    subject_actor TEXT NOT NULL,
    other_actor TEXT NOT NULL,
    relation TEXT NOT NULL,
    declared_by TEXT NOT NULL,
    declared_at TEXT NOT NULL,
    PRIMARY KEY(subject_actor, other_actor, relation)
);
CREATE TABLE IF NOT EXISTS duty_events (
    event_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    candidate_id TEXT NOT NULL,
    candidate_name TEXT NOT NULL,
    site_id TEXT,
    duty_start TEXT NOT NULL,
    duty_end TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    rule_version_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(batch_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS duty_facts (
    fact_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES duty_events(event_id),
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    submitter_actor TEXT NOT NULL,
    source_organization_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    candidate_actor_id TEXT,
    sensitive INTEGER NOT NULL CHECK(sensitive IN (0,1)),
    reason TEXT NOT NULL,
    story TEXT NOT NULL,
    public_scope_json TEXT NOT NULL,
    proofs_json TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','accepted','excluded','supplement_pending')),
    status_reason TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL,
    UNIQUE(event_id, dedup_key)
);
CREATE TABLE IF NOT EXISTS review_assignments (
    assignment_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES duty_events(event_id),
    reviewer_actor TEXT NOT NULL,
    assigned_by TEXT NOT NULL,
    deadline TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','completed','expired','cancelled')),
    check_basis_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_assignment
    ON review_assignments(event_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS fact_decisions (
    decision_id TEXT PRIMARY KEY,
    fact_id TEXT NOT NULL REFERENCES duty_facts(fact_id),
    assignment_id TEXT NOT NULL REFERENCES review_assignments(assignment_id),
    decision TEXT NOT NULL CHECK(decision IN ('accepted','excluded','supplement')),
    note TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS supplement_requests (
    supplement_request_id TEXT PRIMARY KEY,
    fact_id TEXT NOT NULL REFERENCES duty_facts(fact_id),
    requested_by TEXT NOT NULL,
    note TEXT NOT NULL,
    deadline TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','closed','late')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS supplements (
    supplement_id TEXT PRIMARY KEY,
    supplement_request_id TEXT NOT NULL REFERENCES supplement_requests(supplement_request_id),
    submitter_actor TEXT NOT NULL,
    content TEXT NOT NULL,
    proofs_json TEXT NOT NULL,
    accepted_status TEXT NOT NULL CHECK(accepted_status IN ('on_time','late_rejected')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS final_decisions (
    event_id TEXT PRIMARY KEY REFERENCES duty_events(event_id),
    decision TEXT NOT NULL CHECK(decision IN ('selected','not_selected')),
    decided_by TEXT NOT NULL,
    note TEXT NOT NULL,
    rule_version_id TEXT NOT NULL,
    accepted_fact_ids_json TEXT NOT NULL,
    basis_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publications (
    publication_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    name TEXT NOT NULL,
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publication_entries (
    entry_id TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL REFERENCES publications(publication_id),
    event_id TEXT NOT NULL REFERENCES duty_events(event_id),
    candidate_id TEXT NOT NULL,
    visible_fields_json TEXT NOT NULL,
    name TEXT,
    organization TEXT,
    duty_start TEXT,
    duty_end TEXT,
    story TEXT,
    source_units_json TEXT NOT NULL,
    position INTEGER NOT NULL
);
"""


class _LockedConnection:
    """把共享连接上的每次 execute 调用串行化的代理。

    事务期间持有的是可重入锁，事务内部的 execute 直接重入；其他线程的读写
    会等到事务提交后执行。
    """

    def __init__(self, connection, lock: threading.RLock) -> None:
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_lock", lock)

    def execute(self, *args, **kwargs):
        with self._lock:
            return self._connection.execute(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        with self._lock:
            return self._connection.executescript(*args, **kwargs)

    def commit(self) -> None:
        with self._lock:
            self._connection.commit()

    def rollback(self) -> None:
        with self._lock:
            self._connection.rollback()

    def __getattr__(self, name):
        return getattr(self._connection, name)


class HonorDatabase(Database):
    """同时具备基础表与荣誉核验表的 SQLite 库。

    基础库在进程内共享一个连接，因此写事务在整个 BEGIN IMMEDIATE 到 COMMIT
    期间串行执行：并发核验与重复提交要么先到者建对象、后到者合并/回放，
    要么由唯一约束兜底，不会产生两个事务交错的不确定结果。
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.write_lock = threading.RLock()
        super().__init__(path)
        self.connection = _LockedConnection(self.connection, self.write_lock)
        self.connection.executescript(HONOR_SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator:
        with self.write_lock:
            with super().transaction(immediate=immediate) as connection:
                yield connection
