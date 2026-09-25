"""评选规则的冻结、合并指纹、资格回避判定与公开投影等纯函数。"""

from __future__ import annotations

import re
from typing import Any

from festival_foundation.audit import canonical_json, digest

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

SUBMITTER_ROLES = frozenset({"operator", "admin"})
ASSIGNER_ROLES = frozenset({"admin", "operator"})
REVIEWER_ROLE = "reviewer"
CONFIRMER_ROLES = frozenset({"admin", "operator"})

# 公示可以承载的全部字段；最终输出还要再次按候选人同意裁剪。
PUBLIC_FIELD_CHOICES = frozenset({"name", "organization", "duty_start", "duty_end", "story", "source_units"})
DEFAULT_PUBLIC_FIELDS = frozenset({"name", "organization", "duty_start", "duty_end", "story", "source_units"})
SENSITIVE_PUBLIC_FIELDS = frozenset({"name", "organization"})

DEFAULT_RULE_PAYLOAD: dict[str, Any] = {
    "min_proofs": 1,
    "min_accepted_sources": 1,
    "review_deadline_hours": 72,
    "supplement_deadline_hours": 48,
    "late_supplement_rejected": True,
    "require_distinct_confirmer": True,
    "disallow_self_review": True,
    "sensitive_fields": sorted(SENSITIVE_PUBLIC_FIELDS),
}


def normalize_rule(payload: dict[str, Any] | None) -> dict[str, Any]:
    """用默认值补齐规则并做范围校验，返回可冻结的规整结果。"""

    merged = {**DEFAULT_RULE_PAYLOAD, **(payload or {})}
    if not isinstance(merged["min_proofs"], int) or isinstance(merged["min_proofs"], bool):
        raise ValueError("min_proofs 必须是整数")
    if not isinstance(merged["min_accepted_sources"], int) or isinstance(merged["min_accepted_sources"], bool):
        raise ValueError("min_accepted_sources 必须是整数")
    if not 1 <= merged["min_proofs"] <= 20:
        raise ValueError("min_proofs 超出允许范围")
    if not 1 <= merged["min_accepted_sources"] <= 20:
        raise ValueError("min_accepted_sources 超出允许范围")
    for key in ("review_deadline_hours", "supplement_deadline_hours"):
        if not isinstance(merged[key], (int, float)) or isinstance(merged[key], bool):
            raise ValueError(f"{key} 必须是数字")
        if not 0 < merged[key] <= 24 * 30:
            raise ValueError(f"{key} 超出允许范围")
    for key in ("late_supplement_rejected", "require_distinct_confirmer", "disallow_self_review"):
        if not isinstance(merged[key], bool):
            raise ValueError(f"{key} 必须是布尔值")
    sensitive_fields = frozenset(merged["sensitive_fields"])
    if not sensitive_fields <= PUBLIC_FIELD_CHOICES:
        raise ValueError("sensitive_fields 含不支持的公开字段")
    merged["sensitive_fields"] = sorted(sensitive_fields)
    return merged


def event_fingerprint(candidate_id: str, site_id: str | None, duty_start: str, duty_end: str) -> str:
    """同一值守事件的稳定指纹：同人、同场所、同起止时间。"""

    material = {"candidate_id": candidate_id, "site_id": site_id or "",
                "duty_start": duty_start, "duty_end": duty_end}
    return digest(material)


def fact_dedup_key(submitter_actor: str, source_organization_id: str, reason: str,
                   proofs: list[dict[str, Any]]) -> str:
    """同一来源对同一事件重复提交的去重键。"""

    proof_material = [{"kind": str(p.get("kind", "")).strip(),
                       "reference": str(p.get("reference", "")).strip()} for p in proofs]
    return digest({"submitter": submitter_actor, "source": source_organization_id,
                   "reason": reason.strip(), "proofs": proof_material})


def validate_proofs(proofs: Any, min_proofs: int) -> list[dict[str, Any]]:
    if not isinstance(proofs, list) or not proofs:
        raise ValueError("proofs 必须是非空列表")
    normalized: list[dict[str, Any]] = []
    for proof in proofs:
        if not isinstance(proof, dict):
            raise ValueError("每条证明必须是对象")
        kind = str(proof.get("kind", "")).strip()
        reference = str(proof.get("reference", "")).strip()
        issuer = str(proof.get("issuer_organization_id", "")).strip()
        if not kind or not reference or not issuer:
            raise ValueError("证明缺少 kind、reference 或 issuer_organization_id")
        normalized.append({"kind": kind, "reference": reference,
                           "issuer_organization_id": issuer,
                           "note": str(proof.get("note", "")).strip()})
    if len(normalized) < min_proofs:
        raise ValueError(f"证明数量少于规则要求的 {min_proofs} 份")
    return normalized


def validate_public_scope(scope: Any, sensitive: bool, rule: dict[str, Any]) -> frozenset[str]:
    if not isinstance(scope, list):
        raise ValueError("public_scope 必须是字段名列表")
    fields = frozenset(str(item).strip() for item in scope)
    if not fields <= PUBLIC_FIELD_CHOICES:
        raise ValueError("public_scope 含不支持的字段")
    if not fields:
        raise ValueError("public_scope 不能为空")
    if sensitive:
        forbidden = fields & frozenset(rule["sensitive_fields"])
        if forbidden:
            raise ValueError(f"敏感岗位记录不允许公开 {sorted(forbidden)} 字段")
    return fields


def reviewer_conflict(reviewer_actor: str, submitters: set[str], candidate_actor_id: str | None,
                      declared: dict[str, frozenset[str]], rule: dict[str, Any]) -> str | None:
    """返回命中的回避原因；无回避时返回 None。"""

    if rule["disallow_self_review"]:
        if reviewer_actor in submitters:
            return "reviewer_is_submitter"
        if candidate_actor_id and reviewer_actor == candidate_actor_id:
            return "reviewer_is_candidate"
    if reviewer_actor in declared and declared[reviewer_actor] & submitters:
        return "declared_interest"
    return None


def confirmer_conflict(confirmer_actor: str, submitters: set[str], decider_actor: str | None,
                       candidate_actor_id: str | None, declared: dict[str, frozenset[str]],
                       rule: dict[str, Any]) -> str | None:
    """终审确认的回避：不能确认自己的推荐，且须与核验人不同。"""

    if confirmer_actor in submitters:
        return "confirmer_is_submitter"
    if candidate_actor_id and confirmer_actor == candidate_actor_id:
        return "confirmer_is_candidate"
    if rule["require_distinct_confirmer"] and decider_actor and confirmer_actor == decider_actor:
        return "confirmer_is_reviewer"
    if confirmer_actor in declared and declared[confirmer_actor] & submitters:
        return "declared_interest"
    return None


def load_declared_conflicts(connection) -> dict[str, frozenset[str]]:
    """读取对称的利益关系声明，供回避判定使用。"""

    declared: dict[str, set[str]] = {}
    for row in connection.execute("SELECT subject_actor, other_actor FROM conflict_relations"):
        declared.setdefault(row["subject_actor"], set()).add(row["other_actor"])
        declared.setdefault(row["other_actor"], set()).add(row["subject_actor"])
    return {key: frozenset(value) for key, value in declared.items()}


def public_projection(*, name: str, organization_id: str, duty_start: str, duty_end: str,
                      story: str, source_units: list[str], granted_fields: frozenset[str],
                      scope_fields: frozenset[str], sensitive: bool,
                      rule: dict[str, Any]) -> dict[str, Any]:
    """同时按候选人同意、记录公开范围与敏感裁剪规则输出获准字段。"""

    allowed = granted_fields & scope_fields
    if sensitive:
        allowed -= frozenset(rule["sensitive_fields"])
    candidates = {"name": name, "organization": organization_id, "duty_start": duty_start,
                  "duty_end": duty_end, "story": story, "source_units": source_units}
    return {"visible_fields": sorted(allowed),
            **{key: candidates[key] for key in sorted(allowed)}}


def snapshot_hash(rule: dict[str, Any]) -> str:
    return digest(canonical_json(rule))
