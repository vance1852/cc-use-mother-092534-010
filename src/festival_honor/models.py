"""荣誉核验领域使用的只读数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RuleVersion:
    """一次冻结的评选规则，内容永不修改。"""

    version_id: str
    payload: dict[str, Any]
    note: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class Batch:
    """按冻结规则开展的一批评选。"""

    batch_id: str
    name: str
    rule_version_id: str
    rule_snapshot: dict[str, Any]
    submission_deadline: str
    status: str
    created_by: str
    created_at: str
    closed_at: str | None


@dataclass(frozen=True)
class DutyEvent:
    """同一候选人同一值守事件合并后的候选记录。"""

    event_id: str
    batch_id: str
    candidate_id: str
    candidate_name: str
    site_id: str | None
    duty_start: str
    duty_end: str
    fingerprint: str
    rule_version_id: str
    created_at: str


@dataclass(frozen=True)
class DutyFact:
    """一个来源单位对值守事件提交的一条带来源事实。"""

    fact_id: str
    event_id: str
    batch_id: str
    submitter_actor: str
    source_organization_id: str
    candidate_id: str
    candidate_actor_id: str | None
    sensitive: bool
    reason: str
    story: str
    public_scope: frozenset[str]
    proofs: tuple[dict[str, Any], ...]
    dedup_key: str
    status: str
    status_reason: str
    received_at: str


@dataclass(frozen=True)
class AssignmentView:
    """一条限期核验分派。"""

    assignment_id: str
    event_id: str
    reviewer_actor: str
    assigned_by: str
    deadline: str
    status: str
    check_basis: dict[str, Any]
    created_at: str
    completed_at: str | None


@dataclass(frozen=True)
class PublicEntry:
    """公示条目的公开投影，只含候选人获准公开的字段。"""

    entry_id: str
    visible_fields: frozenset[str]
    name: str | None = None
    organization: str | None = None
    duty_start: str | None = None
    duty_end: str | None = None
    story: str | None = None
    source_units: tuple[str, ...] = field(default_factory=tuple)
