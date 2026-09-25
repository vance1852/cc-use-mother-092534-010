"""荣誉公示领域的 SQLite 表结构与连接。"""

from __future__ import annotations

from pathlib import Path

from .storage import Database

HONOR_SCHEMA = """
PRAGMA foreign_keys = ON;
-- 冻结的规则版本：批次创建时绑定，未完成批次可显式刷新。
CREATE TABLE IF NOT EXISTS rule_versions (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    params_json TEXT NOT NULL,
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
-- 回避关系：对称存储为 (低位编号, 高位编号)，任意一方均被视为相关方。
CREATE TABLE IF NOT EXISTS conflicts_of_interest (
    coi_id TEXT PRIMARY KEY,
    party_low TEXT NOT NULL,
    party_high TEXT NOT NULL,
    relation TEXT NOT NULL,
    declared_by TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE(party_low, party_high)
);
-- 评选批次。
CREATE TABLE IF NOT EXISTS honor_batches (
    batch_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    rule_version INTEGER NOT NULL REFERENCES rule_versions(version),
    nomination_deadline TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'finalized', 'published', 'closed')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finalized_at TEXT,
    published_at TEXT
);
-- 候选人公开同意记录：同一候选人至多一条有效同意；撤回仅留痕行，不再生效。
CREATE TABLE IF NOT EXISTS consent_records (
    consent_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    granted_by TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_by TEXT,
    revoke_reason TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_consent_active
    ON consent_records(candidate_id) WHERE active = 1;
-- 合并后的候选记录（同一候选人同一值守事件只有一条）。
CREATE TABLE IF NOT EXISTS nominations (
    nomination_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES honor_batches(batch_id),
    merge_key TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    candidate_name TEXT NOT NULL,
    organization TEXT NOT NULL,
    department TEXT NOT NULL,
    post_name TEXT NOT NULL,
    sensitive INTEGER NOT NULL CHECK(sensitive IN (0, 1)),
    event_date TEXT NOT NULL,
    event_location TEXT NOT NULL,
    deed_summary TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('verifying', 'excluded', 'selected', 'rejected')),
    excluded_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(batch_id, merge_key)
);
-- 带来源的值守事实与协作单位证明，每条都是一方独立贡献。
CREATE TABLE IF NOT EXISTS duty_facts (
    fact_id TEXT PRIMARY KEY,
    nomination_id TEXT NOT NULL REFERENCES nominations(nomination_id),
    batch_id TEXT NOT NULL REFERENCES honor_batches(batch_id),
    source_org TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK(source_type IN ('duty_fact', 'collab_proof')),
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    recommendation TEXT NOT NULL DEFAULT '',
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    verification_status TEXT NOT NULL
        CHECK(verification_status IN ('pending', 'accepted', 'supplement_requested', 'excluded')),
    excluded_reason TEXT NOT NULL DEFAULT '',
    UNIQUE(nomination_id, source_org, source_type, content_hash)
);
-- 补证材料：晚于核验期限提交时 late=1，留痕但不改变终局结论。
CREATE TABLE IF NOT EXISTS fact_supplements (
    supplement_id TEXT PRIMARY KEY,
    fact_id TEXT NOT NULL REFERENCES duty_facts(fact_id),
    content_json TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    late INTEGER NOT NULL CHECK(late IN (0, 1)),
    created_at TEXT NOT NULL
);
-- 核验分派：同一候选记录同时只有一条生效分派；过期或重新分派后旧行留痕。
CREATE TABLE IF NOT EXISTS verifications (
    verification_id TEXT PRIMARY KEY,
    nomination_id TEXT NOT NULL REFERENCES nominations(nomination_id),
    verifier_id TEXT NOT NULL,
    assigned_by TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'completed', 'expired', 'revoked')),
    round INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_verification_active
    ON verifications(nomination_id) WHERE status = 'active';
-- 逐条事实的核验结论（采信 / 要求补证 / 排除），仅追加，完整保留历史。
CREATE TABLE IF NOT EXISTS fact_decisions (
    decision_id TEXT PRIMARY KEY,
    fact_id TEXT NOT NULL REFERENCES duty_facts(fact_id),
    verification_id TEXT NOT NULL REFERENCES verifications(verification_id),
    verifier_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('accepted', 'supplement_requested', 'excluded')),
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
-- 终局入选确认：另一名授权人员作出，且不能是任何推荐人或核验人。
CREATE TABLE IF NOT EXISTS confirmations (
    confirmation_id TEXT PRIMARY KEY,
    nomination_id TEXT NOT NULL REFERENCES nominations(nomination_id),
    confirmer_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('selected', 'rejected')),
    reason TEXT NOT NULL DEFAULT '',
    rule_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(nomination_id)
);
-- 公示批次与其公开快照；撤回同意后快照仍留痕，但公开接口实时过滤。
CREATE TABLE IF NOT EXISTS publications (
    publication_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES honor_batches(batch_id),
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    UNIQUE(batch_id)
);
CREATE TABLE IF NOT EXISTS publication_entries (
    entry_id TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL REFERENCES publications(publication_id),
    nomination_id TEXT NOT NULL REFERENCES nominations(nomination_id),
    payload_json TEXT NOT NULL
);
"""


class HonorDatabase(Database):
    """在建库时一并创建荣誉公示领域的表。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        super().__init__(path)
        self.connection.executescript(HONOR_SCHEMA)
