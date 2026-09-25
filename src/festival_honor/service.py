"""实现规则冻结、批次、事实合并、回避核验、补证、终审与公示流程。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from festival_foundation.audit import append_event, canonical_json, digest
from festival_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from festival_foundation.service import DomainService

from .rules import (
    ASSIGNER_ROLES,
    CONFIRMER_ROLES,
    DEFAULT_PUBLIC_FIELDS,
    PUBLIC_FIELD_CHOICES,
    REVIEWER_ROLE,
    SUBMITTER_ROLES,
    confirmer_conflict,
    event_fingerprint,
    fact_dedup_key,
    load_declared_conflicts,
    normalize_rule,
    public_projection,
    reviewer_conflict,
    snapshot_hash,
    validate_proofs,
    validate_public_scope,
)
from .models import AssignmentView, Batch, DutyEvent, DutyFact, PublicEntry, RuleVersion


class HonorService(DomainService):
    """协调节日坚守人员贡献核验与荣誉公示的全部业务规则。"""

    # ---- 规则与批次 -----------------------------------------------------

    def freeze_rule(self, *, request_id: str, actor_id: str, payload: dict[str, Any] | None = None,
                    note: str = ""):
        payload_out = {"actor_id": actor_id, "payload": payload or {}, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            try:
                normalized = normalize_rule(payload)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            note = self._text(note, "note", 500) if note else ""

            def create():
                version_id = "rule-" + uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO rule_versions(version_id,payload_json,payload_hash,note,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (version_id, canonical_json(normalized), digest(normalized), note, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="rule.frozen",
                             resource_type="rule_version", resource_id=version_id,
                             detail={"payload_hash": digest(normalized), "note": note},
                             occurred_at=self._now())
                return "rule_version", version_id, {"rule_version_id": version_id}

            return self._idempotent(connection, request_id=request_id, action="freeze_rule",
                                    payload=payload_out, create=create)

    def get_rule(self, version_id: str) -> RuleVersion:
        row = self.database.connection.execute(
            "SELECT * FROM rule_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("规则版本不存在")
        return RuleVersion(row["version_id"], json.loads(row["payload_json"]), row["note"],
                           row["created_by"], row["created_at"])

    def create_batch(self, *, request_id: str, actor_id: str, batch_id: str, name: str,
                     rule_version_id: str, submission_deadline: str):
        payload = {"actor_id": actor_id, "batch_id": batch_id, "name": name,
                   "rule_version_id": rule_version_id, "submission_deadline": submission_deadline}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            batch_id = self._identifier(batch_id, "batch_id")
            name = self._text(name, "name")
            rule_row = connection.execute(
                "SELECT * FROM rule_versions WHERE version_id=?", (rule_version_id,)
            ).fetchone()
            if rule_row is None:
                raise NotFoundError("规则版本不存在")
            rule = json.loads(rule_row["payload_json"])
            deadline = self._parse_time(submission_deadline, "submission_deadline")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO batches(batch_id,name,rule_version_id,rule_snapshot_json,"
                        "submission_deadline,status,created_by,created_at) VALUES(?,?,?,?,?,'open',?,?)",
                        (batch_id, name, rule_version_id, canonical_json(rule),
                                 deadline.isoformat().replace("+00:00", "Z"), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("批次编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="batch.created",
                             resource_type="batch", resource_id=batch_id,
                             detail={"name": name, "rule_version_id": rule_version_id,
                                     "submission_deadline": submission_deadline},
                             occurred_at=self._now())
                return "batch", batch_id, {"batch_id": batch_id, "rule_version_id": rule_version_id}

            return self._idempotent(connection, request_id=request_id, action="create_batch",
                                    payload=payload, create=create)

    def close_batch(self, *, request_id: str, actor_id: str, batch_id: str):
        payload = {"actor_id": actor_id, "batch_id": batch_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            batch = self._batch(connection, batch_id)
            if batch["status"] != "open":
                raise ConflictError("批次已结束")

            def create():
                connection.execute(
                    "UPDATE batches SET status='completed', closed_at=? WHERE batch_id=?",
                    (self._now(), batch_id),
                )
                append_event(connection, actor_id=actor_id, action="batch.closed",
                             resource_type="batch", resource_id=batch_id,
                             detail={"rule_version_id": batch["rule_version_id"]},
                             occurred_at=self._now())
                return "batch", batch_id, {"batch_id": batch_id, "status": "completed"}

            return self._idempotent(connection, request_id=request_id, action="close_batch",
                                    payload=payload, create=create)

    def get_batch(self, batch_id: str) -> Batch:
        row = self.database.connection.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return self._batch_view(row)

    def rebind_rule(self, *, request_id: str, actor_id: str, batch_id: str,
                    rule_version_id: str):
        """把未完成批次改绑到新冻结的规则版本；已有终局决定或已结束的批次禁止改绑。"""

        payload = {"actor_id": actor_id, "batch_id": batch_id, "rule_version_id": rule_version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            batch = self._batch(connection, batch_id)
            if batch["status"] != "open":
                raise ConflictError("批次已结束，规则永久冻结，不能改绑")
            finals = connection.execute(
                "SELECT COUNT(*) AS count FROM final_decisions WHERE event_id IN "
                "(SELECT event_id FROM duty_events WHERE batch_id=?)",
                (batch_id,),
            ).fetchone()["count"]
            if finals:
                raise ConflictError("批次内已有终局决定，不能再变更规则")
            rule_row = connection.execute(
                "SELECT * FROM rule_versions WHERE version_id=?", (rule_version_id,)
            ).fetchone()
            if rule_row is None:
                raise NotFoundError("规则版本不存在")
            if rule_version_id == batch["rule_version_id"]:
                raise ValidationError("批次已绑定该规则版本")
            new_rule = json.loads(rule_row["payload_json"])

            def create():
                connection.execute(
                    "UPDATE batches SET rule_version_id=?, rule_snapshot_json=? WHERE batch_id=?",
                    (rule_version_id, canonical_json(new_rule), batch_id),
                )
                append_event(connection, actor_id=actor_id, action="batch.rebound",
                             resource_type="batch", resource_id=batch_id,
                             detail={"old_rule_version_id": batch["rule_version_id"],
                                     "new_rule_version_id": rule_version_id},
                             occurred_at=self._now())
                return "batch", batch_id, {"batch_id": batch_id,
                                           "rule_version_id": rule_version_id}

            return self._idempotent(connection, request_id=request_id, action="rebind_rule",
                                    payload=payload, create=create)

    # ---- 回避关系与公开同意 ---------------------------------------------

    def declare_conflict(self, *, request_id: str, actor_id: str, subject_actor: str,
                         other_actor: str, relation: str):
        payload = {"actor_id": actor_id, "subject_actor": subject_actor,
                   "other_actor": other_actor, "relation": relation}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            subject_actor = self._identifier(subject_actor, "subject_actor")
            other_actor = self._identifier(other_actor, "other_actor")
            if subject_actor == other_actor:
                raise ValidationError("利益关系的两方不能相同")
            relation = self._text(relation, "relation", 100)
            for identifier in (subject_actor, other_actor):
                if connection.execute("SELECT 1 FROM actors WHERE actor_id=?", (identifier,)).fetchone() is None:
                    raise NotFoundError(f"操作者 {identifier} 不存在")

            def create():
                connection.execute(
                    "INSERT OR IGNORE INTO conflict_relations(subject_actor,other_actor,relation,"
                    "declared_by,declared_at) VALUES(?,?,?,?,?)",
                    (subject_actor, other_actor, relation, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="conflict.declared",
                             resource_type="conflict_relation",
                             resource_id=f"{subject_actor}:{other_actor}:{relation}",
                             detail={"subject_actor": subject_actor, "other_actor": other_actor,
                                     "relation": relation}, occurred_at=self._now())
                return "conflict_relation", f"{subject_actor}:{other_actor}", {"subject_actor": subject_actor,
                                                                               "other_actor": other_actor}

            return self._idempotent(connection, request_id=request_id, action="declare_conflict",
                                    payload=payload, create=create)

    def grant_consent(self, *, request_id: str, actor_id: str, candidate_id: str,
                      granted_fields: list[str] | None = None):
        granted_fields = granted_fields if granted_fields is not None else sorted(DEFAULT_PUBLIC_FIELDS)
        payload = {"actor_id": actor_id, "candidate_id": candidate_id, "granted_fields": granted_fields}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            candidate_id = self._identifier(candidate_id, "candidate_id")
            profile = connection.execute(
                "SELECT * FROM candidate_profiles WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            if profile is None:
                raise NotFoundError("候选人不存在")
            fields = frozenset(granted_fields)
            if not fields or not fields <= PUBLIC_FIELD_CHOICES:
                raise ValidationError("granted_fields 含不支持的字段或为空")

            def create():
                connection.execute(
                    "UPDATE candidate_consents SET revoked_at=?, revoked_by=? "
                    "WHERE candidate_id=? AND revoked_at IS NULL",
                    (self._now(), actor_id, candidate_id),
                )
                consent_id = "consent-" + uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO candidate_consents(consent_id,candidate_id,granted,granted_fields_json,"
                    "recorded_by,created_at) VALUES(?,?,'1',?,?,?)",
                    (consent_id, candidate_id, canonical_json(sorted(fields)), actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="consent.granted",
                             resource_type="candidate_consent", resource_id=consent_id,
                             detail={"candidate_id": candidate_id, "granted_fields": sorted(fields)},
                             occurred_at=self._now())
                return "candidate_consent", consent_id, {"consent_id": consent_id, "granted_fields": sorted(fields)}

            return self._idempotent(connection, request_id=request_id, action="grant_consent",
                                    payload=payload, create=create)

    def revoke_consent(self, *, request_id: str, actor_id: str, candidate_id: str):
        payload = {"actor_id": actor_id, "candidate_id": candidate_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            candidate_id = self._identifier(candidate_id, "candidate_id")
            if connection.execute("SELECT 1 FROM candidate_profiles WHERE candidate_id=?",
                                  (candidate_id,)).fetchone() is None:
                raise NotFoundError("候选人不存在")
            active = connection.execute(
                "SELECT 1 FROM candidate_consents WHERE candidate_id=? AND granted=1 AND revoked_at IS NULL",
                (candidate_id,),
            ).fetchone()
            if active is None:
                raise ConflictError("候选人当前没有有效的公开同意")

            def create():
                connection.execute(
                    "UPDATE candidate_consents SET revoked_at=?, revoked_by=? "
                    "WHERE candidate_id=? AND revoked_at IS NULL",
                    (self._now(), actor_id, candidate_id),
                )
                append_event(connection, actor_id=actor_id, action="consent.revoked",
                             resource_type="candidate_consent", resource_id=candidate_id,
                             detail={"candidate_id": candidate_id}, occurred_at=self._now())
                return "candidate_consent", candidate_id, {"candidate_id": candidate_id, "granted": False}

            return self._idempotent(connection, request_id=request_id, action="revoke_consent",
                                    payload=payload, create=create)

    # ---- 值守事实提交与合并 ---------------------------------------------

    def submit_fact(self, *, request_id: str, actor_id: str, batch_id: str, candidate_id: str,
                    candidate_name: str, duty_start: str, duty_end: str, reason: str, story: str,
                    public_scope: list[str], proofs: list[dict[str, Any]],
                    site_id: str | None = None, candidate_actor_id: str | None = None,
                    sensitive: bool = False):
        payload = {"actor_id": actor_id, "batch_id": batch_id, "candidate_id": candidate_id,
                   "candidate_name": candidate_name, "site_id": site_id, "duty_start": duty_start,
                   "duty_end": duty_end, "reason": reason, "story": story,
                   "public_scope": public_scope, "proofs": proofs,
                   "candidate_actor_id": candidate_actor_id, "sensitive": bool(sensitive)}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *sorted(SUBMITTER_ROLES))
            batch = self._batch(connection, batch_id)
            if batch["status"] != "open":
                raise ConflictError("批次已结束，不能再提交事实")
            rule = json.loads(batch["rule_snapshot_json"])
            now = self.clock.now()
            deadline = self._parse_time(batch["submission_deadline"], "submission_deadline")
            if now > deadline:
                raise ConflictError("已超过批次推荐截止时间，迟到提交不予接收")
            candidate_id = self._identifier(candidate_id, "candidate_id")
            candidate_name = self._text(candidate_name, "candidate_name", 100)
            reason = self._text(reason, "reason", 1000)
            story = self._text(story, "story", 5000)
            duty_start_dt = self._parse_time(duty_start, "duty_start")
            duty_end_dt = self._parse_time(duty_end, "duty_end")
            if duty_end_dt <= duty_start_dt:
                raise ValidationError("duty_end 必须晚于 duty_start")
            if site_id is not None:
                site_id = self._identifier(site_id, "site_id")
                if connection.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
                    raise NotFoundError("场所不存在")
            if candidate_actor_id is not None:
                candidate_actor_id = self._identifier(candidate_actor_id, "candidate_actor_id")
                if connection.execute("SELECT 1 FROM actors WHERE actor_id=?",
                                      (candidate_actor_id,)).fetchone() is None:
                    raise NotFoundError("候选人绑定的操作者不存在")
            try:
                proofs_norm = validate_proofs(proofs, rule["min_proofs"])
                scope_fields = validate_public_scope(public_scope, bool(sensitive), rule)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            for proof in proofs_norm:
                if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                      (proof["issuer_organization_id"],)).fetchone() is None:
                    raise NotFoundError(f"证明出具组织 {proof['issuer_organization_id']} 不存在")
            fingerprint = event_fingerprint(candidate_id, site_id, duty_start, duty_end)
            dedup_key = fact_dedup_key(actor_id, actor.organization_id, reason, proofs_norm)

            def create():
                profile = connection.execute(
                    "SELECT * FROM candidate_profiles WHERE candidate_id=?", (candidate_id,)
                ).fetchone()
                if profile is None:
                    connection.execute(
                        "INSERT INTO candidate_profiles(candidate_id,name,organization_id,created_at) "
                        "VALUES(?,?,?,?)",
                        (candidate_id, candidate_name, actor.organization_id, self._now()),
                    )
                elif profile["name"] != candidate_name:
                    raise ValidationError("同一候选人编号的姓名与既有记录不一致")
                event_row = connection.execute(
                    "SELECT * FROM duty_events WHERE batch_id=? AND fingerprint=?",
                    (batch_id, fingerprint),
                ).fetchone()
                if event_row is None:
                    event_id = "event-" + uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO duty_events(event_id,batch_id,candidate_id,candidate_name,site_id,"
                        "duty_start,duty_end,fingerprint,rule_version_id,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (event_id, batch_id, candidate_id, candidate_name, site_id,
                         duty_start, duty_end, fingerprint, batch["rule_version_id"], self._now()),
                    )
                else:
                    event_id = event_row["event_id"]
                    bound_actor = connection.execute(
                        "SELECT candidate_actor_id FROM duty_facts "
                        "WHERE event_id=? AND candidate_actor_id IS NOT NULL LIMIT 1",
                        (event_id,),
                    ).fetchone()
                    if candidate_actor_id is not None and bound_actor is not None \
                            and bound_actor["candidate_actor_id"] != candidate_actor_id:
                        raise ValidationError("同一事件绑定的候选人操作者前后不一致")
                duplicate = connection.execute(
                    "SELECT fact_id FROM duty_facts WHERE event_id=? AND dedup_key=?",
                    (event_id, dedup_key),
                ).fetchone()
                if duplicate is not None:
                    return "duty_fact", duplicate["fact_id"], {"fact_id": duplicate["fact_id"],
                                                               "event_id": event_id, "duplicate": True}
                fact_id = "fact-" + uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO duty_facts(fact_id,event_id,batch_id,submitter_actor,source_organization_id,"
                    "candidate_id,candidate_actor_id,sensitive,reason,story,public_scope_json,proofs_json,"
                    "dedup_key,status,received_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
                    (fact_id, event_id, batch_id, actor_id, actor.organization_id, candidate_id,
                     candidate_actor_id, 1 if sensitive else 0, reason, story,
                     canonical_json(sorted(scope_fields)), canonical_json(proofs_norm),
                     dedup_key, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="fact.submitted",
                             resource_type="duty_fact", resource_id=fact_id,
                             detail={"event_id": event_id, "batch_id": batch_id,
                                     "candidate_id": candidate_id, "source_organization_id": actor.organization_id,
                                     "sensitive": bool(sensitive), "fingerprint": fingerprint},
                             occurred_at=self._now())
                return "duty_fact", fact_id, {"fact_id": fact_id, "event_id": event_id, "duplicate": False}

            return self._idempotent(connection, request_id=request_id, action="submit_fact",
                                    payload=payload, create=create)

    # ---- 核验分派 -------------------------------------------------------

    def assign_reviewer(self, *, request_id: str, actor_id: str, event_id: str,
                        reviewer_actor: str, deadline_hours: float | None = None):
        payload = {"actor_id": actor_id, "event_id": event_id, "reviewer_actor": reviewer_actor,
                   "deadline_hours": deadline_hours}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *sorted(ASSIGNER_ROLES))
            event = self._event(connection, event_id)
            batch = self._batch(connection, event["batch_id"])
            if batch["status"] != "open":
                raise ConflictError("批次已结束，不能再分派核验")
            rule = json.loads(batch["rule_snapshot_json"])
            reviewer = self._actor(connection, reviewer_actor)
            if reviewer.role != REVIEWER_ROLE:
                raise ValidationError("被分派者必须具有 reviewer 角色")
            self._expire_assignments(connection)
            active = connection.execute(
                "SELECT 1 FROM review_assignments WHERE event_id=? AND status='active'", (event_id,)
            ).fetchone()
            if active is not None:
                raise ConflictError("该事件已有在期核验人，请先改派")
            submitters = {row["submitter_actor"] for row in connection.execute(
                "SELECT DISTINCT submitter_actor FROM duty_facts WHERE event_id=?", (event_id,))}
            candidate_actor_row = connection.execute(
                "SELECT candidate_actor_id FROM duty_facts WHERE event_id=? AND candidate_actor_id IS NOT NULL LIMIT 1",
                (event_id,),
            ).fetchone()
            candidate_actor_id = candidate_actor_row["candidate_actor_id"] if candidate_actor_row else None
            declared = load_declared_conflicts(connection)
            reason = reviewer_conflict(reviewer_actor, submitters, candidate_actor_id, declared, rule)
            if reason is not None:
                raise PermissionDenied(f"命中回避规则：{reason}")
            hours = deadline_hours if deadline_hours is not None else rule["review_deadline_hours"]
            if not isinstance(hours, (int, float)) or hours <= 0:
                raise ValidationError("deadline_hours 必须是正数")
            deadline = (self.clock.now() + timedelta(hours=float(hours))).isoformat().replace("+00:00", "Z")
            related = sorted(declared.get(reviewer_actor, frozenset()) & submitters)
            basis = {"rule_version_id": batch["rule_version_id"], "submitters": sorted(submitters),
                     "candidate_actor_id": candidate_actor_id, "declared_related": related,
                     "conflict": None, "checked_at": self._now()}

            def create():
                assignment_id = "asg-" + uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO review_assignments(assignment_id,event_id,reviewer_actor,assigned_by,"
                    "deadline,status,check_basis_json,created_at) VALUES(?,?,?,?,?,'active',?,?)",
                    (assignment_id, event_id, reviewer_actor, actor_id, deadline,
                     canonical_json(basis), self._now()),
                )
                append_event(connection, actor_id=actor_id, action="assignment.created",
                             resource_type="review_assignment", resource_id=assignment_id,
                             detail={"event_id": event_id, "reviewer_actor": reviewer_actor,
                                     "deadline": deadline, "check_basis": basis},
                             occurred_at=self._now())
                return "review_assignment", assignment_id, {"assignment_id": assignment_id, "deadline": deadline}

            return self._idempotent(connection, request_id=request_id, action="assign_reviewer",
                                    payload=payload, create=create)

    def cancel_assignment(self, *, request_id: str, actor_id: str, assignment_id: str, reason: str = ""):
        payload = {"actor_id": actor_id, "assignment_id": assignment_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *sorted(ASSIGNER_ROLES))
            row = connection.execute(
                "SELECT * FROM review_assignments WHERE assignment_id=?", (assignment_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("核验分派不存在")
            if row["status"] != "active":
                raise ConflictError("分派不在在期状态，不能撤销")

            def create():
                connection.execute(
                    "UPDATE review_assignments SET status='cancelled' WHERE assignment_id=?",
                    (assignment_id,),
                )
                append_event(connection, actor_id=actor_id, action="assignment.cancelled",
                             resource_type="review_assignment", resource_id=assignment_id,
                             detail={"event_id": row["event_id"], "reason": reason},
                             occurred_at=self._now())
                return "review_assignment", assignment_id, {"assignment_id": assignment_id,
                                                             "status": "cancelled"}

            return self._idempotent(connection, request_id=request_id, action="cancel_assignment",
                                    payload=payload, create=create)

    def decide_fact(self, *, request_id: str, actor_id: str, assignment_id: str, fact_id: str,
                    decision: str, note: str = ""):
        if decision not in ("accepted", "excluded", "supplement"):
            raise ValidationError("decision 只能是 accepted、excluded 或 supplement")
        payload = {"actor_id": actor_id, "assignment_id": assignment_id, "fact_id": fact_id,
                   "decision": decision, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, REVIEWER_ROLE)
            self._expire_assignments(connection)
            assignment = connection.execute(
                "SELECT * FROM review_assignments WHERE assignment_id=?", (assignment_id,)
            ).fetchone()
            if assignment is None:
                raise NotFoundError("核验分派不存在")
            if assignment["status"] != "active":
                raise ConflictError("核验分派已结束或超期，不能再作出结论")
            if assignment["reviewer_actor"] != actor_id:
                raise PermissionDenied("只有被分派的核验人可以作出结论")
            fact = connection.execute("SELECT * FROM duty_facts WHERE fact_id=?", (fact_id,)).fetchone()
            if fact is None:
                raise NotFoundError("值守事实不存在")
            if fact["event_id"] != assignment["event_id"]:
                raise ValidationError("事实与分派不属于同一事件")
            batch = self._batch(connection, fact["batch_id"])
            if batch["status"] != "open":
                raise ConflictError("批次已结束")
            if fact["status"] in ("accepted", "excluded"):
                raise ConflictError("该事实已有终局核验结论")
            if decision == "supplement" and fact["status"] == "supplement_pending":
                raise ConflictError("该事实已在补证中")
            note = self._text(note, "note", 1000) if note else ""
            if decision in ("excluded", "supplement") and not note:
                raise ValidationError("排除事实或要求补证必须填写理由")
            rule = json.loads(batch["rule_snapshot_json"])

            def create():
                decision_id = "dec-" + uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO fact_decisions(decision_id,fact_id,assignment_id,decision,note,"
                    "decided_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (decision_id, fact_id, assignment_id, decision, note, actor_id, self._now()),
                )
                detail = {"fact_id": fact_id, "event_id": fact["event_id"], "decision": decision, "note": note}
                if decision == "accepted":
                    connection.execute(
                        "UPDATE duty_facts SET status='accepted', status_reason=? WHERE fact_id=?",
                        (note, fact_id),
                    )
                    action = "fact.accepted"
                elif decision == "excluded":
                    connection.execute(
                        "UPDATE duty_facts SET status='excluded', status_reason=? WHERE fact_id=?",
                        (note, fact_id),
                    )
                    action = "fact.excluded"
                else:
                    deadline = (self.clock.now()
                                + timedelta(hours=float(rule["supplement_deadline_hours"])))
                    deadline_text = deadline.isoformat().replace("+00:00", "Z")
                    request_id_value = "sup-" + uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO supplement_requests(supplement_request_id,fact_id,requested_by,"
                        "note,deadline,status,created_at) VALUES(?,?,?,?,?,'open',?)",
                        (request_id_value, fact_id, actor_id, note, deadline_text, self._now()),
                    )
                    connection.execute(
                        "UPDATE duty_facts SET status='supplement_pending', status_reason=? WHERE fact_id=?",
                        (note, fact_id),
                    )
                    detail["supplement_request_id"] = request_id_value
                    detail["supplement_deadline"] = deadline_text
                    action = "fact.supplement_requested"
                append_event(connection, actor_id=actor_id, action=action,
                             resource_type="fact_decision", resource_id=decision_id,
                             detail=detail, occurred_at=self._now())
                return "fact_decision", decision_id, {"decision_id": decision_id, "decision": decision}

            return self._idempotent(connection, request_id=request_id, action="decide_fact",
                                    payload=payload, create=create)

    def submit_supplement(self, *, request_id: str, actor_id: str, fact_id: str,
                          content: str, proofs: list[dict[str, Any]]):
        payload = {"actor_id": actor_id, "fact_id": fact_id, "content": content, "proofs": proofs}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *sorted(SUBMITTER_ROLES))
            fact = connection.execute("SELECT * FROM duty_facts WHERE fact_id=?", (fact_id,)).fetchone()
            if fact is None:
                raise NotFoundError("值守事实不存在")
            if fact["submitter_actor"] != actor_id and actor.role != "admin":
                raise PermissionDenied("只有原推荐人可以补交证明")
            batch = self._batch(connection, fact["batch_id"])
            if batch["status"] != "open":
                raise ConflictError("批次已结束")
            request_row = connection.execute(
                "SELECT * FROM supplement_requests WHERE fact_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (fact_id,),
            ).fetchone()
            if request_row is None or request_row["status"] != "open":
                raise ConflictError("该事实没有待处理的补证要求")
            content = self._text(content, "content", 5000)
            try:
                proofs_norm = validate_proofs(proofs, 1)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            late = self.clock.now() > self._parse_time(request_row["deadline"], "deadline")

            def create():
                supplement_id = "add-" + uuid.uuid4().hex
                accepted_status = "late_rejected" if late else "on_time"
                connection.execute(
                    "INSERT INTO supplements(supplement_id,supplement_request_id,submitter_actor,"
                    "content,proofs_json,accepted_status,created_at) VALUES(?,?,?,?,?,?,?)",
                    (supplement_id, request_row["supplement_request_id"], actor_id, content,
                     canonical_json(proofs_norm), accepted_status, self._now()),
                )
                connection.execute(
                    "UPDATE supplement_requests SET status=? WHERE supplement_request_id=?",
                    ("late" if late else "closed", request_row["supplement_request_id"]),
                )
                if late:
                    connection.execute(
                        "UPDATE duty_facts SET status='excluded', status_reason=? WHERE fact_id=?",
                        ("超过补证期限，迟到证明不予采信", fact_id),
                    )
                else:
                    connection.execute(
                        "UPDATE duty_facts SET status='pending', status_reason='' WHERE fact_id=?",
                        (fact_id,),
                    )
                append_event(connection, actor_id=actor_id,
                             action="supplement.late_rejected" if late else "supplement.received",
                             resource_type="supplement", resource_id=supplement_id,
                             detail={"fact_id": fact_id, "accepted_status": accepted_status,
                                     "deadline": request_row["deadline"], "submitted_at": self._now()},
                             occurred_at=self._now())
                return "supplement", supplement_id, {"supplement_id": supplement_id,
                                                     "accepted_status": accepted_status}

            return self._idempotent(connection, request_id=request_id, action="submit_supplement",
                                    payload=payload, create=create)

    def complete_assignment(self, *, request_id: str, actor_id: str, assignment_id: str):
        payload = {"actor_id": actor_id, "assignment_id": assignment_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, REVIEWER_ROLE)
            self._expire_assignments(connection)
            assignment = connection.execute(
                "SELECT * FROM review_assignments WHERE assignment_id=?", (assignment_id,)
            ).fetchone()
            if assignment is None:
                raise NotFoundError("核验分派不存在")
            if assignment["status"] != "active":
                raise ConflictError("分派不在在期状态")
            if assignment["reviewer_actor"] != actor_id:
                raise PermissionDenied("只有被分派的核验人可以结束核验")
            unfinished = connection.execute(
                "SELECT COUNT(*) AS count FROM duty_facts WHERE event_id=? "
                "AND status IN ('pending','supplement_pending')",
                (assignment["event_id"],),
            ).fetchone()["count"]
            if unfinished:
                raise ConflictError("仍有事实未形成采信或排除结论")

            def create():
                connection.execute(
                    "UPDATE review_assignments SET status='completed', completed_at=? WHERE assignment_id=?",
                    (self._now(), assignment_id),
                )
                append_event(connection, actor_id=actor_id, action="assignment.completed",
                             resource_type="review_assignment", resource_id=assignment_id,
                             detail={"event_id": assignment["event_id"]}, occurred_at=self._now())
                return "review_assignment", assignment_id, {"assignment_id": assignment_id,
                                                            "status": "completed"}

            return self._idempotent(connection, request_id=request_id, action="complete_assignment",
                                    payload=payload, create=create)

    # ---- 终审确认 -------------------------------------------------------

    def confirm_selection(self, *, request_id: str, actor_id: str, event_id: str,
                          decision: str, note: str = ""):
        if decision not in ("selected", "not_selected"):
            raise ValidationError("decision 只能是 selected 或 not_selected")
        payload = {"actor_id": actor_id, "event_id": event_id, "decision": decision, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *sorted(CONFIRMER_ROLES))
            self._expire_assignments(connection)
            event = self._event(connection, event_id)
            batch = self._batch(connection, event["batch_id"])
            if batch["status"] != "open":
                raise ConflictError("批次已结束，不能再作出终局决定")
            if connection.execute("SELECT 1 FROM final_decisions WHERE event_id=?", (event_id,)).fetchone():
                raise ConflictError("该事件已有终局决定")
            assignment = connection.execute(
                "SELECT * FROM review_assignments WHERE event_id=? AND status='completed' "
                "ORDER BY completed_at DESC, rowid DESC LIMIT 1",
                (event_id,),
            ).fetchone()
            if assignment is None:
                raise ConflictError("核验尚未完成，不能进入终审")
            unfinished = connection.execute(
                "SELECT COUNT(*) AS count FROM duty_facts WHERE event_id=? "
                "AND status IN ('pending','supplement_pending')",
                (event_id,),
            ).fetchone()["count"]
            if unfinished:
                raise ConflictError("仍有事实未形成采信或排除结论，不能进入终审")
            rule = json.loads(batch["rule_snapshot_json"])
            submitters = {row["submitter_actor"] for row in connection.execute(
                "SELECT DISTINCT submitter_actor FROM duty_facts WHERE event_id=?", (event_id,))}
            candidate_actor_row = connection.execute(
                "SELECT candidate_actor_id FROM duty_facts WHERE event_id=? AND candidate_actor_id IS NOT NULL LIMIT 1",
                (event_id,),
            ).fetchone()
            candidate_actor_id = candidate_actor_row["candidate_actor_id"] if candidate_actor_row else None
            declared = load_declared_conflicts(connection)
            reason = confirmer_conflict(actor_id, submitters, assignment["reviewer_actor"],
                                        candidate_actor_id, declared, rule)
            if reason is not None:
                raise PermissionDenied(f"命中回避规则：{reason}")
            accepted = connection.execute(
                "SELECT * FROM duty_facts WHERE event_id=? AND status='accepted' ORDER BY received_at, fact_id",
                (event_id,),
            ).fetchall()
            excluded = connection.execute(
                "SELECT fact_id FROM duty_facts WHERE event_id=? AND status='excluded'", (event_id,)
            ).fetchall()
            sources = {row["source_organization_id"] for row in accepted}
            if decision == "selected" and len(sources) < rule["min_accepted_sources"]:
                raise ValidationError(
                    f"采信来源单位只有 {len(sources)} 个，未达到规则要求的 {rule['min_accepted_sources']} 个")
            consent_row = connection.execute(
                "SELECT * FROM candidate_consents WHERE candidate_id=? AND granted=1 AND revoked_at IS NULL",
                (event["candidate_id"],),
            ).fetchone()
            consent_snapshot = (None if consent_row is None
                                else {"granted_fields": json.loads(consent_row["granted_fields_json"]),
                                      "consent_id": consent_row["consent_id"]})
            note = self._text(note, "note", 1000) if note else ""

            def create():
                basis = {"rule_version_id": batch["rule_version_id"],
                         "assignment_id": assignment["assignment_id"],
                         "reviewer_actor": assignment["reviewer_actor"],
                         "confirmer_actor": actor_id,
                         "fact_ids_accepted": [row["fact_id"] for row in accepted],
                         "fact_ids_excluded": [row["fact_id"] for row in excluded],
                         "accepted_sources": sorted(sources),
                         "submitters": sorted(submitters),
                         "consent_at_decision": consent_snapshot}
                connection.execute(
                    "INSERT INTO final_decisions(event_id,decision,decided_by,note,rule_version_id,"
                    "accepted_fact_ids_json,basis_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (event_id, decision, actor_id, note, batch["rule_version_id"],
                     canonical_json(basis["fact_ids_accepted"]), canonical_json(basis), self._now()),
                )
                append_event(connection, actor_id=actor_id, action="final.confirmed",
                             resource_type="final_decision", resource_id=event_id,
                             detail={"event_id": event_id, "decision": decision, "basis": basis},
                             occurred_at=self._now())
                return "final_decision", event_id, {"event_id": event_id, "decision": decision}

            return self._idempotent(connection, request_id=request_id, action="confirm_selection",
                                    payload=payload, create=create)

    # ---- 公示 -----------------------------------------------------------

    def publish(self, *, request_id: str, actor_id: str, batch_id: str, name: str):
        payload = {"actor_id": actor_id, "batch_id": batch_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            batch = self._batch(connection, batch_id)
            name = self._text(name, "name")

            def create():
                publication_id = "pub-" + uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO publications(publication_id,batch_id,name,published_by,published_at) "
                    "VALUES(?,?,?,?,?)",
                    (publication_id, batch_id, name, actor_id, self._now()),
                )
                finals = connection.execute(
                    "SELECT * FROM final_decisions WHERE event_id IN "
                    "(SELECT event_id FROM duty_events WHERE batch_id=?) ORDER BY created_at, event_id",
                    (batch_id,),
                ).fetchall()
                events = connection.execute(
                    "SELECT * FROM duty_events WHERE batch_id=? ORDER BY duty_start, event_id", (batch_id,)
                ).fetchall()
                selected = {row["event_id"]: row for row in finals if row["decision"] == "selected"}
                position = 0
                for event_row in events:
                    final_row = selected.get(event_row["event_id"])
                    if final_row is None:
                        continue
                    consent_row = connection.execute(
                        "SELECT * FROM candidate_consents WHERE candidate_id=? AND granted=1 AND revoked_at IS NULL",
                        (event_row["candidate_id"],),
                    ).fetchone()
                    if consent_row is None:
                        # 撤回公开同意后不得出现在新公示中；终局决定仍在内部留痕。
                        continue
                    accepted_facts = connection.execute(
                        "SELECT * FROM duty_facts WHERE event_id=? AND status='accepted' "
                        "ORDER BY received_at, fact_id", (event_row["event_id"],)
                    ).fetchall()
                    if not accepted_facts:
                        continue
                    rule = json.loads(batch["rule_snapshot_json"])
                    granted_fields = frozenset(json.loads(consent_row["granted_fields_json"]))
                    scope_intersection: frozenset[str] | None = None
                    sensitive = False
                    for fact_row in accepted_facts:
                        scope_intersection = (frozenset(json.loads(fact_row["public_scope_json"]))
                                              if scope_intersection is None
                                              else scope_intersection & frozenset(
                                                  json.loads(fact_row["public_scope_json"])))
                        sensitive = sensitive or bool(fact_row["sensitive"])
                    scope_intersection = scope_intersection or frozenset()
                    source_units = sorted({row["source_organization_id"] for row in accepted_facts})
                    story_fact = next(
                        (row for row in accepted_facts if "story" in frozenset(
                            json.loads(row["public_scope_json"]))),
                        accepted_facts[0],
                    )
                    projected = public_projection(
                        name=event_row["candidate_name"],
                        organization_id=source_units[0] if source_units else "",
                        duty_start=event_row["duty_start"], duty_end=event_row["duty_end"],
                        story=story_fact["story"], source_units=source_units,
                        granted_fields=granted_fields, scope_fields=scope_intersection,
                        sensitive=sensitive, rule=rule,
                    )
                    entry_id = "entry-" + uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO publication_entries(entry_id,publication_id,event_id,candidate_id,"
                        "visible_fields_json,name,organization,duty_start,duty_end,story,source_units_json,"
                        "position) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (entry_id, publication_id, event_row["event_id"], event_row["candidate_id"],
                         canonical_json(projected["visible_fields"]),
                         projected.get("name"), projected.get("organization"),
                         projected.get("duty_start"), projected.get("duty_end"),
                         projected.get("story"),
                         canonical_json(projected.get("source_units", [])), position),
                    )
                    position += 1
                append_event(connection, actor_id=actor_id, action="publication.published",
                             resource_type="publication", resource_id=publication_id,
                             detail={"batch_id": batch_id, "name": name, "entries": position},
                             occurred_at=self._now())
                return "publication", publication_id, {"publication_id": publication_id, "entries": position}

            return self._idempotent(connection, request_id=request_id, action="publish",
                                    payload=payload, create=create)

    def list_publications(self, batch_id: str) -> list[dict[str, Any]]:
        """公开接口：只返回公示元数据与获准字段，不暴露任何内部依据。"""

        result = []
        for pub in self.database.connection.execute(
                "SELECT * FROM publications WHERE batch_id=? ORDER BY rowid",
                (batch_id,)):
            entries = []
            for row in self.database.connection.execute(
                    "SELECT * FROM publication_entries WHERE publication_id=? ORDER BY position, entry_id",
                    (pub["publication_id"],)):
                entries.append(PublicEntry(
                    entry_id=row["entry_id"],
                    visible_fields=frozenset(json.loads(row["visible_fields_json"])),
                    name=row["name"], organization=row["organization"],
                    duty_start=row["duty_start"], duty_end=row["duty_end"], story=row["story"],
                    source_units=tuple(json.loads(row["source_units_json"])),
                ).__dict__)
            result.append({"publication_id": pub["publication_id"], "batch_id": pub["batch_id"],
                           "name": pub["name"], "published_at": pub["published_at"], "entries": entries})
        return result

    # ---- 内部查询：还原完整依据 -----------------------------------------

    def get_event_trace(self, *, actor_id: str, event_id: str) -> dict[str, Any]:
        """内部查询：还原合并、回避、补证与终局决定的完整依据。"""

        connection = self.database.connection
        actor_row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if actor_row is None:
            raise NotFoundError("操作者不存在")
        if not actor_row["active"]:
            raise PermissionDenied("操作者已停用")
        if actor_row["role"] not in ("admin", "auditor"):
            raise PermissionDenied("只有管理员或审计员可以查看完整核验依据")
        event = self._event(connection, event_id)
        batch = self._batch(connection, event["batch_id"])
        facts = []
        for fact in connection.execute("SELECT * FROM duty_facts WHERE event_id=? ORDER BY received_at, fact_id",
                                       (event_id,)):
            decisions = []
            for dec in connection.execute(
                    "SELECT * FROM fact_decisions WHERE fact_id=? ORDER BY created_at, rowid", (fact["fact_id"],)):
                decisions.append({"decision_id": dec["decision_id"], "decision": dec["decision"],
                                  "note": dec["note"], "decided_by": dec["decided_by"],
                                  "assignment_id": dec["assignment_id"], "created_at": dec["created_at"]})
            requests = []
            for req in connection.execute(
                    "SELECT * FROM supplement_requests WHERE fact_id=? ORDER BY created_at, rowid",
                    (fact["fact_id"],)):
                supplements = []
                for sup in connection.execute(
                        "SELECT * FROM supplements WHERE supplement_request_id=? ORDER BY created_at, rowid",
                        (req["supplement_request_id"],)):
                    supplements.append({"supplement_id": sup["supplement_id"],
                                        "submitter_actor": sup["submitter_actor"],
                                        "content": sup["content"],
                                        "proofs": json.loads(sup["proofs_json"]),
                                        "accepted_status": sup["accepted_status"],
                                        "created_at": sup["created_at"]})
                requests.append({"supplement_request_id": req["supplement_request_id"],
                                 "requested_by": req["requested_by"], "note": req["note"],
                                 "deadline": req["deadline"], "status": req["status"],
                                 "created_at": req["created_at"], "supplements": supplements})
            facts.append(self._fact_view(fact).__dict__ | {"decisions": decisions,
                                                           "supplement_requests": requests})
        assignments = []
        for asg in connection.execute(
                "SELECT * FROM review_assignments WHERE event_id=? ORDER BY created_at, rowid", (event_id,)):
            assignments.append(self._assignment_view(asg).__dict__)
        final_row = connection.execute(
            "SELECT * FROM final_decisions WHERE event_id=?", (event_id,)).fetchone()
        final = None
        if final_row is not None:
            final = {"decision": final_row["decision"], "decided_by": final_row["decided_by"],
                     "note": final_row["note"], "rule_version_id": final_row["rule_version_id"],
                     "accepted_fact_ids": json.loads(final_row["accepted_fact_ids_json"]),
                     "basis": json.loads(final_row["basis_json"]), "created_at": final_row["created_at"]}
        consents = []
        for consent in connection.execute(
                "SELECT * FROM candidate_consents WHERE candidate_id=? ORDER BY created_at, rowid",
                (event["candidate_id"],)):
            consents.append({"consent_id": consent["consent_id"], "granted": bool(consent["granted"]),
                             "granted_fields": json.loads(consent["granted_fields_json"]),
                             "recorded_by": consent["recorded_by"], "created_at": consent["created_at"],
                             "revoked_at": consent["revoked_at"], "revoked_by": consent["revoked_by"]})
        publication_links = []
        for entry in connection.execute(
                "SELECT pe.*, p.batch_id FROM publication_entries pe JOIN publications p "
                "ON pe.publication_id=p.publication_id WHERE pe.event_id=? ORDER BY p.published_at",
                (event_id,)):
            publication_links.append({"publication_id": entry["publication_id"],
                                      "batch_id": entry["batch_id"],
                                      "visible_fields": json.loads(entry["visible_fields_json"])})
        submitters = sorted({fact["submitter_actor"] for fact in
                             connection.execute("SELECT DISTINCT submitter_actor FROM duty_facts WHERE event_id=?",
                                                (event_id,))})
        declared = load_declared_conflicts(connection)
        conflict_map = {submitter: sorted(declared.get(submitter, frozenset())) for submitter in submitters}
        return {"event": self._event_view(event).__dict__,
                "batch": {"batch_id": batch["batch_id"], "name": batch["name"], "status": batch["status"],
                          "rule_version_id": batch["rule_version_id"],
                          "submission_deadline": batch["submission_deadline"]},
                "rule_snapshot": json.loads(batch["rule_snapshot_json"]),
                "rule_snapshot_hash": snapshot_hash(json.loads(batch["rule_snapshot_json"])),
                "facts": facts, "assignments": assignments, "consent_ledger": consents,
                "final_decision": final, "publication_links": publication_links,
                "conflict_relations": conflict_map}

    def get_fact(self, fact_id: str) -> DutyFact:
        row = self.database.connection.execute("SELECT * FROM duty_facts WHERE fact_id=?", (fact_id,)).fetchone()
        if row is None:
            raise NotFoundError("值守事实不存在")
        return self._fact_view(row)

    def list_assignments(self, event_id: str) -> list[AssignmentView]:
        rows = self.database.connection.execute(
            "SELECT * FROM review_assignments WHERE event_id=? ORDER BY created_at, rowid", (event_id,)
        ).fetchall()
        return [self._assignment_view(row) for row in rows]

    # ---- 内部辅助 -------------------------------------------------------

    def _expire_assignments(self, connection) -> None:
        connection.execute(
            "UPDATE review_assignments SET status='expired' WHERE status='active' AND deadline<=?",
            (self._now(),),
        )

    @staticmethod
    def _parse_time(value: str, field: str):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 ISO 8601 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return parsed.astimezone()

    @staticmethod
    def _batch(connection, batch_id: str):
        row = connection.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return row

    @staticmethod
    def _event(connection, event_id: str):
        row = connection.execute("SELECT * FROM duty_events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise NotFoundError("候选事件不存在")
        return row

    @staticmethod
    def _batch_view(row) -> Batch:
        return Batch(row["batch_id"], row["name"], row["rule_version_id"],
                     json.loads(row["rule_snapshot_json"]), row["submission_deadline"],
                     row["status"], row["created_by"], row["created_at"], row["closed_at"])

    @staticmethod
    def _event_view(row) -> DutyEvent:
        return DutyEvent(row["event_id"], row["batch_id"], row["candidate_id"], row["candidate_name"],
                         row["site_id"], row["duty_start"], row["duty_end"], row["fingerprint"],
                         row["rule_version_id"], row["created_at"])

    @staticmethod
    def _fact_view(row) -> DutyFact:
        return DutyFact(row["fact_id"], row["event_id"], row["batch_id"], row["submitter_actor"],
                        row["source_organization_id"], row["candidate_id"], row["candidate_actor_id"],
                        bool(row["sensitive"]), row["reason"], row["story"],
                        frozenset(json.loads(row["public_scope_json"])),
                        tuple(json.loads(row["proofs_json"])), row["dedup_key"],
                        row["status"], row["status_reason"], row["received_at"])

    @staticmethod
    def _assignment_view(row) -> AssignmentView:
        return AssignmentView(row["assignment_id"], row["event_id"], row["reviewer_actor"],
                              row["assigned_by"], row["deadline"], row["status"],
                              json.loads(row["check_basis_json"]), row["created_at"], row["completed_at"])
