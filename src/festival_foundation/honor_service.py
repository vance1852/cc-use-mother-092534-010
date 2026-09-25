"""实现节日坚守人员贡献核验与荣誉公示的领域工作流。

工作流：发布冻结规则 → 申报回避关系 → 开启批次 → 候选人授予公开同意 →
多方提交带来源的事实（同一事件自动合并、各方贡献独立保留）→ 资格与回避检查后
分派有期限核验人 → 逐条采信/补证/排除 → 另一名无关联授权人员终局确认 →
冻结批次并公示。所有写操作幂等、串行化并写入哈希审计链。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .honor_rules import (
    PUBLIC_FIELDS,
    build_public_entry,
    merge_key,
    normalize_params,
    validate_event_date,
)
from .honor_storage import HonorDatabase
from .models import Actor

FACT_STATUSES = ("pending", "accepted", "supplement_requested", "excluded")
TERMINAL_FACT_STATUSES = frozenset({"accepted", "excluded"})
NOMINATION_TERMINAL = frozenset({"selected", "rejected"})


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class HonorService:
    """协调规则冻结、合并、回避、核验、确认与公示。"""

    def __init__(self, database: HonorDatabase, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础工具

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _now_dt(self) -> datetime:
        return self.clock.now()

    def _text(self, value: Any, field: str, limit: int = 500) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
        request_id = self._text(request_id, "request_id", 64)
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {"request_id": request_id, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True,
                    **json.loads(row["response_json"])}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {"request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False, **response}

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def _rule(self, connection, version: int) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM rule_versions WHERE version=?", (version,)).fetchone()
        if row is None:
            raise NotFoundError("规则版本不存在")
        return {"version": row["version"], "params": json.loads(row["params_json"]),
                "published_by": row["published_by"], "published_at": row["published_at"],
                "note": row["note"]}

    def _current_rule(self, connection) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM rule_versions ORDER BY version DESC LIMIT 1").fetchone()
        if row is None:
            raise ConflictError("尚未发布任何评选规则")
        return self._rule(connection, row["version"])

    def _batch(self, connection, batch_id: str) -> Any:
        row = connection.execute("SELECT * FROM honor_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return row

    def _nomination(self, connection, nomination_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM nominations WHERE nomination_id=?", (nomination_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("候选记录不存在")
        return row

    def _active_consent(self, connection, candidate_id: str) -> Any:
        return connection.execute(
            "SELECT * FROM consent_records WHERE candidate_id=? AND active=1", (candidate_id,)
        ).fetchone()

    def _has_coi(self, connection, party_a: str, party_b: str) -> Any:
        if party_a == party_b:
            return None
        low, high = sorted((party_a, party_b))
        return connection.execute(
            "SELECT * FROM conflicts_of_interest WHERE party_low=? AND party_high=? AND active=1",
            (low, high),
        ).fetchone()

    def _assert_no_coi(self, connection, person: str, parties: list[str], label: str) -> None:
        for party in sorted(set(parties)):
            if person == party:
                raise PermissionDenied(f"与{label}存在直接利害关系，必须回避")
            if self._has_coi(connection, person, party):
                raise PermissionDenied(f"与{label}存在已申报的回避关系，必须回避")

    def _expire_if_due(self, connection, nomination_id: str) -> None:
        """把已过期限但未处理的生效分派员懒标记为过期。"""

        row = connection.execute(
            "SELECT * FROM verifications WHERE nomination_id=? AND status='active'",
            (nomination_id,),
        ).fetchone()
        if row and self._now_dt() > _parse_ts(row["deadline"]):
            connection.execute("UPDATE verifications SET status='expired' WHERE verification_id=?",
                               (row["verification_id"],))
            self._audit(connection, actor_id="system", action="verification.expired",
                        resource_type="verification", resource_id=row["verification_id"],
                        detail={"nomination_id": nomination_id, "deadline": row["deadline"]})

    # ------------------------------------------------------------------ 规则与回避

    def publish_rules(self, *, request_id: str, actor_id: str,
                      params: dict[str, Any] | None = None, note: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "params": params or {}, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            try:
                normalized = normalize_params(params)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc

            def create() -> tuple[str, str, dict[str, Any]]:
                cur = connection.execute("SELECT MAX(version) AS v FROM rule_versions").fetchone()
                version = (cur["v"] or 0) + 1
                connection.execute(
                    "INSERT INTO rule_versions(version,params_json,published_by,published_at,note) "
                    "VALUES(?,?,?,?,?)",
                    (version, canonical_json(normalized), actor_id, self._now(), str(note or "")),
                )
                self._audit(connection, actor_id=actor_id, action="rules.published",
                            resource_type="rule_version", resource_id=str(version),
                            detail={"version": version, "params": normalized})
                return "rule_version", str(version), {"version": version}

            return self._idempotent(connection, request_id=request_id, action="publish_rules",
                                    payload=payload, create=create)

    def declare_conflict(self, *, request_id: str, actor_id: str, party_a: str,
                         party_b: str, relation: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "party_a": party_a, "party_b": party_b,
                   "relation": relation}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            party_a = self._text(party_a, "party_a", 64)
            party_b = self._text(party_b, "party_b", 64)
            if party_a == party_b:
                raise ValidationError("不能对本人申报回避关系")
            relation = self._text(relation, "relation", 200)
            low, high = sorted((party_a, party_b))

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM conflicts_of_interest WHERE party_low=? AND party_high=?",
                    (low, high),
                ).fetchone()
                if existing and existing["active"]:
                    return "conflict_of_interest", existing["coi_id"], {"coi_id": existing["coi_id"]}
                if existing:
                    connection.execute(
                        "UPDATE conflicts_of_interest SET active=1, relation=?, declared_by=?, "
                        "created_at=? WHERE coi_id=?",
                        (relation, actor_id, self._now(), existing["coi_id"]),
                    )
                    coi_id = existing["coi_id"]
                else:
                    coi_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO conflicts_of_interest(coi_id,party_low,party_high,relation,"
                        "declared_by,active,created_at) VALUES(?,?,?,?,?,1,?)",
                        (coi_id, low, high, relation, actor_id, self._now()),
                    )
                self._audit(connection, actor_id=actor_id, action="coi.declared",
                            resource_type="conflict_of_interest", resource_id=coi_id,
                            detail={"party_a": party_a, "party_b": party_b, "relation": relation})
                return "conflict_of_interest", coi_id, {"coi_id": coi_id}

            return self._idempotent(connection, request_id=request_id, action="declare_conflict",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 批次与同意

    def create_batch(self, *, request_id: str, actor_id: str, batch_id: str, title: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "batch_id": batch_id, "title": title}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            batch_id = self._text(batch_id, "batch_id", 64)
            title = self._text(title, "title")
            rule = self._current_rule(connection)
            deadline = self._now_dt() + timedelta(hours=rule["params"]["nomination_window_hours"])

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO honor_batches(batch_id,title,rule_version,nomination_deadline,"
                        "status,created_by,created_at) VALUES(?,?,?,?, 'open', ?,?)",
                        (batch_id, title, rule["version"], deadline.isoformat().replace("+00:00", "Z"),
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("批次编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="batch.created",
                            resource_type="batch", resource_id=batch_id,
                            detail={"title": title, "rule_version": rule["version"],
                                    "nomination_deadline": deadline.isoformat()})
                return "batch", batch_id, {"batch_id": batch_id, "rule_version": rule["version"]}

            return self._idempotent(connection, request_id=request_id, action="create_batch",
                                    payload=payload, create=create)

    def refresh_batch_rules(self, *, request_id: str, actor_id: str, batch_id: str) -> dict[str, Any]:
        """把未完成（open）批次刷新到最新规则；终局批次拒绝刷新。"""

        payload = {"actor_id": actor_id, "batch_id": batch_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            batch = self._batch(connection, batch_id)
            if batch["status"] != "open":
                raise ConflictError("批次已结束，规则版本随终局决定锁定，不能刷新")
            rule = self._current_rule(connection)
            if rule["version"] == batch["rule_version"]:
                return {"request_id": request_id, "resource_type": "batch",
                        "resource_id": batch_id, "replayed": True,
                        "batch_id": batch_id, "rule_version": rule["version"]}

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE honor_batches SET rule_version=? WHERE batch_id=?",
                    (rule["version"], batch_id),
                )
                self._audit(connection, actor_id=actor_id, action="batch.rules_refreshed",
                            resource_type="batch", resource_id=batch_id,
                            detail={"old_version": batch["rule_version"],
                                    "new_version": rule["version"]})
                return "batch", batch_id, {"batch_id": batch_id, "rule_version": rule["version"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="refresh_batch_rules", payload=payload, create=create)

    def grant_consent(self, *, request_id: str, actor_id: str, candidate_id: str,
                      scope: list[str]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "candidate_id": candidate_id, "scope": scope}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            candidate_id = self._text(candidate_id, "candidate_id", 64)
            if not isinstance(scope, list) or not scope or any(f not in PUBLIC_FIELDS for f in scope):
                raise ValidationError("scope 必须是公开字段白名单内的非空数组")
            scope = sorted(set(scope))

            def create() -> tuple[str, str, dict[str, Any]]:
                if self._active_consent(connection, candidate_id) is not None:
                    raise ConflictError("候选人已有生效中的公开同意")
                consent_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO consent_records(consent_id,candidate_id,scope_json,active,"
                    "granted_by,granted_at) VALUES(?,?,?,1,?,?)",
                    (consent_id, candidate_id, canonical_json(scope), actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="consent.granted",
                            resource_type="consent", resource_id=consent_id,
                            detail={"candidate_id": candidate_id, "scope": scope})
                return "consent", consent_id, {"consent_id": consent_id, "scope": scope}

            return self._idempotent(connection, request_id=request_id, action="grant_consent",
                                    payload=payload, create=create)

    def revoke_consent(self, *, request_id: str, actor_id: str, candidate_id: str,
                       reason: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "candidate_id": candidate_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            candidate_id = self._text(candidate_id, "candidate_id", 64)
            active = self._active_consent(connection, candidate_id)
            if active is None:
                raise NotFoundError("候选人没有生效中的公开同意")

            def create() -> tuple[str, str, dict[str, Any]]:
                # 撤回只让同意失效：候选记录、确认与既有公示快照全部留痕。
                connection.execute(
                    "UPDATE consent_records SET active=0, revoked_at=?, revoked_by=?, "
                    "revoke_reason=? WHERE consent_id=?",
                    (self._now(), actor_id, str(reason or ""), active["consent_id"]),
                )
                self._audit(connection, actor_id=actor_id, action="consent.revoked",
                            resource_type="consent", resource_id=active["consent_id"],
                            detail={"candidate_id": candidate_id, "reason": reason})
                return "consent", active["consent_id"], {"consent_id": active["consent_id"]}

            return self._idempotent(connection, request_id=request_id, action="revoke_consent",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 推荐与合并

    def submit_nomination(self, *, request_id: str, actor_id: str, batch_id: str,
                          candidate_id: str, candidate_name: str, organization: str,
                          department: str, post_name: str, event_date: str,
                          event_location: str, deed_summary: str, sensitive: bool = False,
                          facts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        facts = facts or []
        payload = {"actor_id": actor_id, "batch_id": batch_id, "candidate_id": candidate_id,
                   "candidate_name": candidate_name, "organization": organization,
                   "department": department, "post_name": post_name, "event_date": event_date,
                   "event_location": event_location, "deed_summary": deed_summary,
                   "sensitive": bool(sensitive),
                   "facts": [{k: f.get(k) for k in ("source_org", "source_type", "content",
                                                    "recommendation")} for f in facts]}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            batch = self._batch(connection, batch_id)
            if batch["status"] != "open":
                raise ConflictError("批次已结束，不再接收推荐")
            if self._now() > batch["nomination_deadline"]:
                raise ConflictError("已超过批次推荐截止时间")
            candidate_id = self._text(candidate_id, "candidate_id", 64)
            candidate_name = self._text(candidate_name, "candidate_name", 100)
            organization = self._text(organization, "organization", 200)
            department = self._text(department, "department", 200)
            post_name = self._text(post_name, "post_name", 200)
            event_location = self._text(event_location, "event_location", 200)
            deed_summary = self._text(deed_summary, "deed_summary", 2000)
            event_date = validate_event_date(event_date)
            if not isinstance(facts, list) or not facts:
                raise ValidationError("facts 至少包含一条带来源的值守事实或协作单位证明")

            cleaned_facts: list[dict[str, Any]] = []
            for item in facts:
                if not isinstance(item, dict):
                    raise ValidationError("facts 条目必须是对象")
                source_org = self._text(item.get("source_org"), "source_org", 200)
                source_type = item.get("source_type")
                if source_type not in ("duty_fact", "collab_proof"):
                    raise ValidationError("source_type 必须是 duty_fact 或 collab_proof")
                content = item.get("content")
                if not isinstance(content, dict) or not content:
                    raise ValidationError("每条事实必须携带非空 content")
                recommendation = str(item.get("recommendation") or "").strip()[:2000]
                cleaned_facts.append({"source_org": source_org, "source_type": source_type,
                                      "content": content, "content_hash": digest(content),
                                      "recommendation": recommendation})
            if self._active_consent(connection, candidate_id) is None:
                raise PermissionDenied("候选人未授予生效中的公开同意，不能进入公示流程")
            key = merge_key(candidate_id=candidate_id, event_date=event_date,
                            event_location=event_location)

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM nominations WHERE batch_id=? AND merge_key=?",
                    (batch_id, key),
                ).fetchone()
                merged = False
                if existing is None:
                    nomination_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO nominations(nomination_id,batch_id,merge_key,candidate_id,"
                        "candidate_name,organization,department,post_name,sensitive,event_date,"
                        "event_location,deed_summary,status,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'verifying',?,?)",
                        (nomination_id, batch_id, key, candidate_id, candidate_name, organization,
                         department, post_name, 1 if sensitive else 0, event_date, event_location,
                         deed_summary, self._now(), self._now()),
                    )
                    self._audit(connection, actor_id=actor_id, action="nomination.created",
                                resource_type="nomination", resource_id=nomination_id,
                                detail={"batch_id": batch_id, "merge_key": key,
                                        "candidate_id": candidate_id})
                else:
                    nomination_id = existing["nomination_id"]
                    merged = True
                    if existing["status"] in NOMINATION_TERMINAL:
                        raise ConflictError("该事件已经终局决定，不能再追加事实")
                    for field, value in (("candidate_name", candidate_name),
                                         ("organization", organization),
                                         ("post_name", post_name)):
                        if existing[field] != value:
                            raise ConflictError(f"同一事件的 {field} 与既有记录不一致")
                fact_ids: list[str] = []
                duplicate_count = 0
                for item in cleaned_facts:
                    duplicate = connection.execute(
                        "SELECT fact_id FROM duty_facts WHERE nomination_id=? AND source_org=? "
                        "AND source_type=? AND content_hash=?",
                        (nomination_id, item["source_org"], item["source_type"],
                         item["content_hash"]),
                    ).fetchone()
                    if duplicate:
                        # 重复提交产生稳定结果：指向既有事实，不新增贡献行。
                        fact_ids.append(duplicate["fact_id"])
                        duplicate_count += 1
                        continue
                    fact_id = uuid.uuid4().hex
                    try:
                        connection.execute(
                            "INSERT INTO duty_facts(fact_id,nomination_id,batch_id,source_org,"
                            "source_type,content_json,content_hash,recommendation,submitted_by,"
                            "submitted_at,verification_status) VALUES(?,?,?,?,?,?,?,?,?,?,'pending')",
                            (fact_id, nomination_id, batch_id, item["source_org"],
                             item["source_type"], canonical_json(item["content"]),
                             item["content_hash"], item["recommendation"], actor_id, self._now()),
                        )
                    except Exception:
                        # 并发提交同一来源同一内容：回退到既有事实，结果保持稳定。
                        raced = connection.execute(
                            "SELECT fact_id FROM duty_facts WHERE nomination_id=? AND source_org=? "
                            "AND source_type=? AND content_hash=?",
                            (nomination_id, item["source_org"], item["source_type"],
                             item["content_hash"]),
                        ).fetchone()
                        if raced is None:
                            raise
                        fact_ids.append(raced["fact_id"])
                        duplicate_count += 1
                        continue
                    fact_ids.append(fact_id)
                connection.execute("UPDATE nominations SET updated_at=? WHERE nomination_id=?",
                                   (self._now(), nomination_id))
                self._audit(connection, actor_id=actor_id,
                            action="nomination.facts_merged" if merged else "nomination.facts_added",
                            resource_type="nomination", resource_id=nomination_id,
                            detail={"merged": merged, "fact_ids": fact_ids,
                                    "duplicate_count": duplicate_count,
                                    "source_orgs": sorted({f["source_org"] for f in cleaned_facts})})
                return ("nomination", nomination_id,
                        {"nomination_id": nomination_id, "merged": merged,
                         "fact_ids": fact_ids, "duplicate_count": duplicate_count})

            return self._idempotent(connection, request_id=request_id, action="submit_nomination",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 核验分派与结论

    def assign_verifier(self, *, request_id: str, actor_id: str, nomination_id: str,
                        verifier_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "nomination_id": nomination_id,
                   "verifier_id": verifier_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            nomination = self._nomination(connection, nomination_id)
            batch = self._batch(connection, nomination["batch_id"])
            if batch["status"] != "open":
                raise ConflictError("批次已结束，不能再分派核验")
            if nomination["status"] != "verifying":
                raise ConflictError("候选记录不在核验中状态")
            verifier = self._actor(connection, verifier_id)
            self._require(verifier, "reviewer")
            submitters = [row["submitted_by"] for row in connection.execute(
                "SELECT DISTINCT submitted_by FROM duty_facts WHERE nomination_id=?",
                (nomination_id,))]
            # 回避检查：核验人与候选人、所有推荐人不得有利害关系。
            self._assert_no_coi(connection, verifier_id,
                                [nomination["candidate_id"], *submitters], "候选人或推荐人")
            self._expire_if_due(connection, nomination_id)
            active = connection.execute(
                "SELECT * FROM verifications WHERE nomination_id=? AND status='active'",
                (nomination_id,),
            ).fetchone()
            if active is not None:
                raise ConflictError("候选记录已有在期限内的核验人")
            rule = self._rule(connection, batch["rule_version"])
            deadline = self._now_dt() + timedelta(hours=rule["params"]["verify_hours"])

            def create() -> tuple[str, str, dict[str, Any]]:
                last = connection.execute(
                    "SELECT MAX(round) AS r FROM verifications WHERE nomination_id=?",
                    (nomination_id,),
                ).fetchone()
                round_no = (last["r"] or 0) + 1
                verification_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO verifications(verification_id,nomination_id,verifier_id,"
                        "assigned_by,assigned_at,deadline,status,round) VALUES(?,?,?,?,?,?,'active',?)",
                        (verification_id, nomination_id, verifier_id, actor_id, self._now(),
                         deadline.isoformat().replace("+00:00", "Z"), round_no),
                    )
                except Exception as exc:
                    raise ConflictError("候选记录已有在期限内的核验人") from exc
                self._audit(connection, actor_id=actor_id, action="verification.assigned",
                            resource_type="verification", resource_id=verification_id,
                            detail={"nomination_id": nomination_id, "verifier_id": verifier_id,
                                    "deadline": deadline.isoformat(), "round": round_no})
                return "verification", verification_id, {
                    "verification_id": verification_id,
                    "deadline": deadline.isoformat().replace("+00:00", "Z"), "round": round_no}

            return self._idempotent(connection, request_id=request_id, action="assign_verifier",
                                    payload=payload, create=create)

    def decide_fact(self, *, actor_id: str, fact_id: str, outcome: str,
                    reason: str = "") -> dict[str, Any]:
        """核验人对单条事实作出采信、要求补证或排除的结论。"""

        if outcome not in ("accepted", "supplement_requested", "excluded"):
            raise ValidationError("outcome 必须是 accepted、supplement_requested 或 excluded")
        reason = str(reason or "").strip()
        if outcome != "accepted" and not reason:
            raise ValidationError("要求补证或排除事实必须填写理由")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            fact = connection.execute("SELECT * FROM duty_facts WHERE fact_id=?", (fact_id,)).fetchone()
            if fact is None:
                raise NotFoundError("值守事实不存在")
            nomination = self._nomination(connection, fact["nomination_id"])
            self._expire_if_due(connection, nomination["nomination_id"])
            assignment = connection.execute(
                "SELECT * FROM verifications WHERE nomination_id=? AND status='active'",
                (nomination["nomination_id"],),
            ).fetchone()
            if assignment is None:
                raise ConflictError("候选记录没有生效中的核验分派（可能已过期限，需重新分派）")
            if assignment["verifier_id"] != actor_id:
                raise PermissionDenied("只有被分派的核验人能给出结论")
            if self._now_dt() > _parse_ts(assignment["deadline"]):
                raise ConflictError("核验期限已过，需重新分派")
            current = fact["verification_status"]
            if current in TERMINAL_FACT_STATUSES:
                if current == outcome:
                    # 并发或重试产生稳定结果：重放既有结论。
                    prior = connection.execute(
                        "SELECT * FROM fact_decisions WHERE fact_id=? ORDER BY created_at DESC LIMIT 1",
                        (fact_id,)).fetchone()
                    return {"decision_id": prior["decision_id"], "fact_id": fact_id,
                            "outcome": prior["outcome"],
                            "verification_id": prior["verification_id"], "replayed": True}
                raise ConflictError(f"事实已经{current}，结论不可更改")
            if current == "supplement_requested" and outcome == "supplement_requested":
                last_decision = connection.execute(
                    "SELECT created_at FROM fact_decisions WHERE fact_id=? "
                    "ORDER BY created_at DESC LIMIT 1", (fact_id,)).fetchone()
                fresh = connection.execute(
                    "SELECT 1 FROM fact_supplements WHERE fact_id=? AND created_at>?",
                    (fact_id, last_decision["created_at"])).fetchone()
                if fresh is None:
                    raise ConflictError("尚无新补证材料，不能重复要求补证")
            excluded_reason = reason if outcome == "excluded" else ""
            cursor = connection.execute(
                "UPDATE duty_facts SET verification_status=?, excluded_reason=? "
                "WHERE fact_id=? AND verification_status=?",
                (outcome, excluded_reason, fact_id, current),
            )
            # 条件更新保证并发核验时同一条事实只有第一个结论生效。
            if cursor.rowcount == 0:
                raise ConflictError("事实结论已被其他核验请求抢先确定")
            decision_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO fact_decisions(decision_id,fact_id,verification_id,verifier_id,"
                "outcome,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                (decision_id, fact_id, assignment["verification_id"], actor_id, outcome,
                 reason, self._now()),
            )
            self._audit(connection, actor_id=actor_id, action=f"fact.{outcome}",
                        resource_type="duty_fact", resource_id=fact_id,
                        detail={"nomination_id": nomination["nomination_id"], "reason": reason,
                                "verification_id": assignment["verification_id"]})
            return {"decision_id": decision_id, "fact_id": fact_id, "outcome": outcome,
                    "verification_id": assignment["verification_id"], "replayed": False}

    def submit_supplement(self, *, request_id: str, actor_id: str, fact_id: str,
                          content: dict[str, Any]) -> dict[str, Any]:
        """提交补证材料；超过核验期限则标记为迟到，留痕但不影响终局结论。"""

        payload = {"actor_id": actor_id, "fact_id": fact_id, "content": content}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            if not isinstance(content, dict) or not content:
                raise ValidationError("补证内容必须是非空对象")
            fact = connection.execute("SELECT * FROM duty_facts WHERE fact_id=?", (fact_id,)).fetchone()
            if fact is None:
                raise NotFoundError("值守事实不存在")
            if fact["verification_status"] != "supplement_requested":
                raise ConflictError("只有被要求补证的事实可以提交补证材料")
            nomination = self._nomination(connection, fact["nomination_id"])
            assignment = connection.execute(
                "SELECT * FROM verifications WHERE nomination_id=? AND status='active'",
                (nomination["nomination_id"],),
            ).fetchone()
            late = 1
            extension = 0
            if assignment is not None and self._now_dt() <= _parse_ts(assignment["deadline"]):
                late = 0
                batch = self._batch(connection, nomination["batch_id"])
                rule = self._rule(connection, batch["rule_version"])
                extension = rule["params"]["supplement_extension_hours"]

            def create() -> tuple[str, str, dict[str, Any]]:
                supplement_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO fact_supplements(supplement_id,fact_id,content_json,submitted_by,"
                    "late,created_at) VALUES(?,?,?,?,?,?)",
                    (supplement_id, fact_id, canonical_json(content), actor_id, late, self._now()),
                )
                if late == 0:
                    new_deadline = (_parse_ts(assignment["deadline"])
                                    + timedelta(hours=extension)).isoformat().replace("+00:00", "Z")
                    connection.execute("UPDATE verifications SET deadline=? WHERE verification_id=?",
                                       (new_deadline, assignment["verification_id"]))
                self._audit(connection, actor_id=actor_id, action="supplement.submitted",
                            resource_type="fact_supplement", resource_id=supplement_id,
                            detail={"fact_id": fact_id, "late": bool(late),
                                    "content_hash": digest(content)})
                return "fact_supplement", supplement_id, {
                    "supplement_id": supplement_id, "late": bool(late)}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_supplement", payload=payload, create=create)

    # ------------------------------------------------------------------ 终局确认

    def confirm_selection(self, *, request_id: str, actor_id: str, nomination_id: str,
                          outcome: str, reason: str = "") -> dict[str, Any]:
        """由另一名授权人员终局确认入选/不入选；任何人都不能批准自己的推荐。"""

        if outcome not in ("selected", "rejected"):
            raise ValidationError("outcome 必须是 selected 或 rejected")
        payload = {"actor_id": actor_id, "nomination_id": nomination_id, "outcome": outcome,
                   "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            nomination = self._nomination(connection, nomination_id)
            fact_rows = connection.execute(
                "SELECT * FROM duty_facts WHERE nomination_id=?", (nomination_id,)
            ).fetchall()
            submitters = sorted({row["submitted_by"] for row in fact_rows})
            verifiers = [row["verifier_id"] for row in connection.execute(
                "SELECT DISTINCT verifier_id FROM fact_decisions WHERE fact_id IN "
                "(SELECT fact_id FROM duty_facts WHERE nomination_id=?)",
                (nomination_id,))]
            # 确认人与候选人、推荐人、核验人均需无回避关系，且不得批准自己的推荐。
            self._assert_no_coi(connection, actor_id,
                                [nomination["candidate_id"], *submitters, *verifiers],
                                "候选人、推荐人或核验人")
            batch = self._batch(connection, nomination["batch_id"])
            rule = self._rule(connection, batch["rule_version"])

            eligibility: dict[str, Any] = {}
            if outcome == "selected":
                accepted_orgs = {row["source_org"] for row in fact_rows
                                 if row["verification_status"] == "accepted"}
                unresolved = [row["fact_id"] for row in fact_rows
                              if row["verification_status"] in ("pending", "supplement_requested")]
                eligibility = {"independent_sources": len(accepted_orgs),
                               "required": rule["params"]["min_independent_sources"],
                               "unresolved_facts": unresolved}
                if unresolved:
                    raise ConflictError("仍有事实未完成核验（待核验或待补证），不能确认入选")
                if len(accepted_orgs) < rule["params"]["min_independent_sources"]:
                    raise ConflictError(
                        f"采信的独立来源不足：{len(accepted_orgs)} < "
                        f"{rule['params']['min_independent_sources']}")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM confirmations WHERE nomination_id=?", (nomination_id,)
                ).fetchone()
                if existing is not None:
                    raise ConflictError("候选记录已有终局决定")
                confirmation_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO confirmations(confirmation_id,nomination_id,confirmer_id,outcome,"
                    "reason,rule_version,created_at) VALUES(?,?,?,?,?,?,?)",
                    (confirmation_id, nomination_id, actor_id, outcome, str(reason or ""),
                     rule["version"], self._now()),
                )
                connection.execute(
                    "UPDATE nominations SET status=?, updated_at=? WHERE nomination_id=?",
                    (outcome, self._now(), nomination_id),
                )
                self._audit(connection, actor_id=actor_id, action=f"selection.{outcome}",
                            resource_type="confirmation", resource_id=confirmation_id,
                            detail={"nomination_id": nomination_id, "rule_version": rule["version"],
                                    "eligibility": eligibility, "reason": reason})
                return "confirmation", confirmation_id, {
                    "confirmation_id": confirmation_id, "outcome": outcome,
                    "rule_version": rule["version"], **eligibility}

            return self._idempotent(connection, request_id=request_id,
                                    action="confirm_selection", payload=payload, create=create)

    # ------------------------------------------------------------------ 批次冻结与公示

    def finalize_batch(self, *, request_id: str, actor_id: str, batch_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "batch_id": batch_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            batch = self._batch(connection, batch_id)
            if batch["status"] != "open":
                raise ConflictError("批次只有在开放状态才能冻结")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE honor_batches SET status='finalized', finalized_at=? WHERE batch_id=?",
                    (self._now(), batch_id),
                )
                counts = {status: connection.execute(
                    "SELECT COUNT(*) AS c FROM nominations WHERE batch_id=? AND status=?",
                    (batch_id, status)).fetchone()["c"]
                    for status in ("verifying", "selected", "rejected")}
                self._audit(connection, actor_id=actor_id, action="batch.finalized",
                            resource_type="batch", resource_id=batch_id, detail=counts)
                return "batch", batch_id, {"batch_id": batch_id, "status": "finalized", **counts}

            return self._idempotent(connection, request_id=request_id, action="finalize_batch",
                                    payload=payload, create=create)

    def publish_batch(self, *, request_id: str, actor_id: str, batch_id: str) -> dict[str, Any]:
        """把批次入选者按白名单、同意范围与敏感遮罩裁剪后生成公示快照。"""

        payload = {"actor_id": actor_id, "batch_id": batch_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            batch = self._batch(connection, batch_id)
            if batch["status"] not in ("finalized", "published"):
                raise ConflictError("批次必须先冻结才能公示")
            rule = self._rule(connection, batch["rule_version"])

            def create() -> tuple[str, str, dict[str, Any]]:
                publication_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO publications(publication_id,batch_id,published_by,published_at) "
                    "VALUES(?,?,?,?)",
                    (publication_id, batch_id, actor_id, self._now()),
                )
                entries = 0
                excluded_no_consent = 0
                for nomination in connection.execute(
                        "SELECT * FROM nominations WHERE batch_id=? AND status='selected'",
                        (batch_id,)):
                    consent = self._active_consent(connection, nomination["candidate_id"])
                    if consent is None:
                        # 撤回公开同意后不得出现在新公示中，但内部决定仍留痕。
                        excluded_no_consent += 1
                        continue
                    scope = json.loads(consent["scope_json"])
                    projected = build_public_entry(
                        candidate_name=nomination["candidate_name"],
                        organization=nomination["organization"],
                        department=nomination["department"],
                        post_name=nomination["post_name"],
                        event_date=nomination["event_date"],
                        event_location=nomination["event_location"],
                        deed_summary=nomination["deed_summary"],
                        consent_scope=scope,
                        sensitive=bool(nomination["sensitive"]),
                        params=rule["params"],
                    )
                    connection.execute(
                        "INSERT INTO publication_entries(entry_id,publication_id,nomination_id,"
                        "payload_json) VALUES(?,?,?,?)",
                        (uuid.uuid4().hex, publication_id, nomination["nomination_id"],
                         canonical_json(projected)),
                    )
                    entries += 1
                connection.execute(
                    "UPDATE honor_batches SET status='published', published_at=? WHERE batch_id=?",
                    (self._now(), batch_id),
                )
                self._audit(connection, actor_id=actor_id, action="batch.published",
                            resource_type="publication", resource_id=publication_id,
                            detail={"entries": entries,
                                    "excluded_no_consent": excluded_no_consent})
                return "publication", publication_id, {
                    "publication_id": publication_id, "entries": entries,
                    "excluded_no_consent": excluded_no_consent}

            return self._idempotent(connection, request_id=request_id, action="publish_batch",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 查询

    def public_honors(self, batch_id: str) -> dict[str, Any]:
        """公开接口：只输出获准字段，并实时尊重候选人当前的公开同意。"""

        connection_ctx = self.database.access()
        with connection_ctx as connection:
            batch = connection.execute("SELECT * FROM honor_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if batch is None or batch["status"] != "published":
            raise NotFoundError("批次尚未公示")
        publication = connection.execute(
            "SELECT * FROM publications WHERE batch_id=? ORDER BY published_at DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
        rule = self._rule(connection, batch["rule_version"])
        items: list[dict[str, Any]] = []
        for entry in connection.execute(
                "SELECT * FROM publication_entries WHERE publication_id=? ORDER BY entry_id",
                (publication["publication_id"],)):
            nomination = connection.execute(
                "SELECT * FROM nominations WHERE nomination_id=?", (entry["nomination_id"],)
            ).fetchone()
            consent = self._active_consent(connection, nomination["candidate_id"])
            if consent is None:
                # 公示后撤回同意：从公开输出移除，快照仍在内部留痕。
                continue
            items.append(build_public_entry(
                candidate_name=nomination["candidate_name"],
                organization=nomination["organization"],
                department=nomination["department"],
                post_name=nomination["post_name"],
                event_date=nomination["event_date"],
                event_location=nomination["event_location"],
                deed_summary=nomination["deed_summary"],
                consent_scope=json.loads(consent["scope_json"]),
                sensitive=bool(nomination["sensitive"]),
                params=rule["params"],
            ))
        return {"batch_id": batch_id, "published_at": publication["published_at"], "items": items}

    def public_batches(self) -> list[dict[str, Any]]:
        with self.database.access() as connection:
            rows = connection.execute(
                "SELECT batch_id, published_at FROM honor_batches WHERE status='published' "
                "ORDER BY published_at"
            ).fetchall()
            return [{"batch_id": row["batch_id"], "published_at": row["published_at"]}
                    for row in rows]

    def batch_detail(self, *, actor_id: str, batch_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "auditor")
            batch = self._batch(connection, batch_id)
            rule = self._rule(connection, batch["rule_version"])
            nominations = [dict(row) for row in connection.execute(
                "SELECT nomination_id,candidate_id,candidate_name,status,merge_key FROM nominations "
                "WHERE batch_id=? ORDER BY created_at", (batch_id,))]
            return {"batch_id": batch_id, "title": batch["title"], "status": batch["status"],
                    "rule_version": rule["version"], "rule_params": rule["params"],
                    "nomination_deadline": batch["nomination_deadline"],
                    "created_at": batch["created_at"], "finalized_at": batch["finalized_at"],
                    "published_at": batch["published_at"], "nominations": nominations}

    def nomination_trace(self, *, actor_id: str, nomination_id: str) -> dict[str, Any]:
        """内部查询：还原合并、回避、补证和终局决定的完整依据。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "auditor")
            nomination = self._nomination(connection, nomination_id)
            batch = self._batch(connection, nomination["batch_id"])
            rule = self._rule(connection, batch["rule_version"])

            facts = []
            for fact in connection.execute(
                    "SELECT * FROM duty_facts WHERE nomination_id=? ORDER BY submitted_at, fact_id",
                    (nomination_id,)):
                decisions = [dict(row) for row in connection.execute(
                    "SELECT decision_id,verification_id,verifier_id,outcome,reason,created_at "
                    "FROM fact_decisions WHERE fact_id=? ORDER BY created_at", (fact["fact_id"],))]
                supplements = [{"supplement_id": row["supplement_id"],
                                "content": json.loads(row["content_json"]),
                                "submitted_by": row["submitted_by"], "late": bool(row["late"]),
                                "created_at": row["created_at"]}
                               for row in connection.execute(
                                   "SELECT * FROM fact_supplements WHERE fact_id=? ORDER BY created_at",
                                   (fact["fact_id"],))]
                facts.append({"fact_id": fact["fact_id"], "source_org": fact["source_org"],
                              "source_type": fact["source_type"],
                              "content": json.loads(fact["content_json"]),
                              "recommendation": fact["recommendation"],
                              "submitted_by": fact["submitted_by"],
                              "submitted_at": fact["submitted_at"],
                              "verification_status": fact["verification_status"],
                              "excluded_reason": fact["excluded_reason"],
                              "decisions": decisions, "supplements": supplements})

            verifications = []
            for verification in connection.execute(
                    "SELECT * FROM verifications WHERE nomination_id=? ORDER BY round",
                    (nomination_id,)):
                item = dict(verification)
                item["conflicts"] = []
                for party in [nomination["candidate_id"],
                              *(f["submitted_by"] for f in facts)]:
                    coi = self._has_coi(connection, verification["verifier_id"], party)
                    if coi:
                        item["conflicts"].append({"party": party, "relation": coi["relation"]})
                verifications.append(item)

            confirmation_row = connection.execute(
                "SELECT * FROM confirmations WHERE nomination_id=?", (nomination_id,)
            ).fetchone()
            confirmation = dict(confirmation_row) if confirmation_row else None

            consents = [{"consent_id": row["consent_id"], "scope": json.loads(row["scope_json"]),
                         "active": bool(row["active"]), "granted_by": row["granted_by"],
                         "granted_at": row["granted_at"], "revoked_at": row["revoked_at"],
                         "revoked_by": row["revoked_by"], "revoke_reason": row["revoke_reason"]}
                        for row in connection.execute(
                            "SELECT * FROM consent_records WHERE candidate_id=? ORDER BY granted_at",
                            (nomination["candidate_id"],))]

            contributors = {}
            for fact in facts:
                contributors.setdefault(fact["source_org"], []).append(fact["fact_id"])

            accepted_orgs = {f["source_org"] for f in facts
                             if f["verification_status"] == "accepted"}
            return {
                "nomination_id": nomination_id,
                "batch_id": nomination["batch_id"],
                "merge_key": nomination["merge_key"],
                "candidate": {"candidate_id": nomination["candidate_id"],
                              "candidate_name": nomination["candidate_name"],
                              "organization": nomination["organization"],
                              "department": nomination["department"],
                              "post_name": nomination["post_name"],
                              "sensitive": bool(nomination["sensitive"]),
                              "event_date": nomination["event_date"],
                              "event_location": nomination["event_location"],
                              "deed_summary": nomination["deed_summary"]},
                "status": nomination["status"],
                "rule_version": rule["version"],
                "rule_params": rule["params"],
                "contributors": contributors,
                "facts": facts,
                "verifications": verifications,
                "confirmation": confirmation,
                "consent_history": consents,
                "eligibility": {
                    "accepted_independent_sources": len(accepted_orgs),
                    "required": rule["params"]["min_independent_sources"],
                    "qualified": len(accepted_orgs) >= rule["params"]["min_independent_sources"]},
                "created_at": nomination["created_at"],
                "updated_at": nomination["updated_at"],
            }
