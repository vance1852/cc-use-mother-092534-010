"""运行节日坚守人员贡献核验与荣誉公示的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from festival_foundation.errors import ConflictError, PermissionDenied

from .clock import MutableClock
from .service import HonorService
from .storage import HonorDatabase

START = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
DEADLINE = "2026-10-03T00:00:00Z"


def run() -> dict[str, object]:
    """执行一条完整的联合推荐、核验、终审与公示链并返回核对结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = HonorDatabase(Path(directory) / "honor_acceptance.sqlite3")
        clock = MutableClock(START)
        service = HonorService(database, clock)

        # ---- 建档：三家单位、推荐人、核验人、终审人与候选人本人 ----
        service.register_organization(request_id="org-1", actor_id="bootstrap",
                                      organization_id="o1", name="联合保障中心")
        service.register_organization(request_id="org-2", actor_id="bootstrap",
                                      organization_id="o2", name="协作医院")
        service.register_organization(request_id="org-3", actor_id="bootstrap",
                                      organization_id="o3", name="荣誉复核办公室")
        service.register_actor(request_id="admin-1", actor_id="bootstrap", new_actor_id="a1",
                               display_name="中心管理员", role="admin", organization_id="o1")
        service.register_actor(request_id="op-1", actor_id="a1", new_actor_id="op1",
                               display_name="甲部门推荐人", role="operator", organization_id="o1")
        service.register_actor(request_id="op-2", actor_id="a1", new_actor_id="op2",
                               display_name="协作单位推荐人", role="operator", organization_id="o2")
        service.register_actor(request_id="rv-1", actor_id="a1", new_actor_id="rv1",
                               display_name="核验人甲", role="reviewer", organization_id="o1")
        service.register_actor(request_id="rv-2", actor_id="a1", new_actor_id="rv2",
                               display_name="核验人乙", role="reviewer", organization_id="o3")
        service.register_actor(request_id="au-1", actor_id="a1", new_actor_id="au1",
                               display_name="审计员", role="auditor", organization_id="o3")
        service.register_actor(request_id="cf-1", actor_id="a1", new_actor_id="a2",
                               display_name="授权终审人", role="admin", organization_id="o3")
        service.register_actor(request_id="cand-1", actor_id="a1", new_actor_id="c1",
                               display_name="张坚守", role="operator", organization_id="o1")
        service.register_site(request_id="site-1", actor_id="op1", site_id="s1",
                              organization_id="o1", name="节日值守站", timezone_name="Asia/Shanghai")

        # ---- 冻结规则并开批次 ----
        rule = service.freeze_rule(request_id="rule-v1", actor_id="a1", payload=None,
                                   note="2026 节日坚守评选规则")
        rule_version_id = rule.resource_id
        service.create_batch(request_id="batch-1", actor_id="a1", batch_id="b1",
                             name="国庆坚守批次", rule_version_id=rule_version_id,
                             submission_deadline=DEADLINE)

        # ---- 回避声明：核验人甲与推荐人甲存在直接利益关系 ----
        service.declare_conflict(request_id="conf-1", actor_id="a1", subject_actor="rv1",
                                 other_actor="op1", relation="同一科室上下级")

        # ---- 多单位对同一事件的带来源事实 ----
        common_event = {"batch_id": "b1", "candidate_id": "cand-zhang", "candidate_name": "张坚守",
                        "site_id": "s1", "duty_start": "2026-10-02T08:00:00+08:00",
                        "duty_end": "2026-10-02T16:00:00+08:00", "candidate_actor_id": "c1"}
        full_scope = ["name", "organization", "duty_start", "duty_end", "story", "source_units"]
        fact1 = service.submit_fact(
            request_id="fact-z1", actor_id="op1", reason="甲部门排班值守",
            story="全天保障现场调度，处理突发三起", public_scope=full_scope, sensitive=False,
            proofs=[{"kind": "duty_roster", "reference": "roster-o1-1002",
                     "issuer_organization_id": "o1"}], **common_event)
        replay = service.submit_fact(
            request_id="fact-z1", actor_id="op1", reason="甲部门排班值守",
            story="全天保障现场调度，处理突发三起", public_scope=full_scope, sensitive=False,
            proofs=[{"kind": "duty_roster", "reference": "roster-o1-1002",
                     "issuer_organization_id": "o1"}], **common_event)
        fact2 = service.submit_fact(
            request_id="fact-z2", actor_id="op2", reason="协作医院联合值守",
            story="协助医疗保障点全时段运行", public_scope=full_scope, sensitive=False,
            proofs=[{"kind": "joint_duty_cert", "reference": "cert-o2-1002",
                     "issuer_organization_id": "o2"}], **common_event)
        fact3 = service.submit_fact(
            request_id="fact-z3", actor_id="op2", reason="夜间补位记录",
            story="交班后继续留守两小时", public_scope=full_scope, sensitive=False,
            proofs=[{"kind": "shift_log", "reference": "log-o2-night",
                     "issuer_organization_id": "o2"}], **common_event)
        assert replay.replayed and replay.resource_id == fact1.resource_id
        assert fact1.resource_id != fact2.resource_id != fact3.resource_id
        merged_event_id = _event_id(service, "fact-z1")
        assert _event_id(service, "fact-z2") == merged_event_id
        assert _event_id(service, "fact-z3") == merged_event_id

        # 同一候选人的另一起值守（敏感岗位），公开范围收窄
        service.submit_fact(
            request_id="fact-w1", actor_id="op1", batch_id="b1", candidate_id="cand-wang",
            candidate_name="王默默", site_id="s1",
            duty_start="2026-10-02T20:00:00+08:00", duty_end="2026-10-03T00:00:00+08:00",
            reason="敏感岗位应急值守", story="敏感岗位事迹（不公开）",
            public_scope=["duty_start", "duty_end", "source_units"], sensitive=True,
            proofs=[{"kind": "duty_roster", "reference": "roster-o1-wang",
                     "issuer_organization_id": "o1"}])
        wang_event_id = _event_id(service, "fact-w1")

        # ---- 资格与回避：核验人甲被拦截，核验人乙在期分派 ----
        blocked = None
        try:
            service.assign_reviewer(request_id="asg-blocked", actor_id="a1",
                                    event_id=merged_event_id, reviewer_actor="rv1")
        except PermissionDenied as exc:
            blocked = str(exc)
        service.assign_reviewer(request_id="asg-z", actor_id="a1", event_id=merged_event_id,
                                reviewer_actor="rv2")
        asg_z = _receipt_resource(service, "asg-z")

        # 采信 z1；对 z2 要求补证；z3 表述重复予以排除
        service.decide_fact(request_id="dec-z1", actor_id="rv2", assignment_id=asg_z,
                            fact_id=_fact_id(service, "fact-z1"), decision="accepted",
                            note="排班表与值守记录一致")
        service.decide_fact(request_id="dec-z2", actor_id="rv2", assignment_id=asg_z,
                            fact_id=_fact_id(service, "fact-z2"), decision="supplement",
                            note="请补充协作单位盖章证明")
        # 限期内补证：事实回到待核验后采信
        clock.advance(hours=10)
        service.submit_supplement(request_id="sup-z2", actor_id="op2",
                                  fact_id=_fact_id(service, "fact-z2"),
                                  content="补交盖章扫描件",
                                  proofs=[{"kind": "sealed_cert", "reference": "cert-o2-sealed",
                                           "issuer_organization_id": "o2"}])
        service.decide_fact(request_id="dec-z2b", actor_id="rv2", assignment_id=asg_z,
                            fact_id=_fact_id(service, "fact-z2"), decision="accepted",
                            note="补证齐备，予以采信")
        service.decide_fact(request_id="dec-z3", actor_id="rv2", assignment_id=asg_z,
                            fact_id=_fact_id(service, "fact-z3"), decision="excluded",
                            note="与主时段事实重复，不单独采信")
        service.complete_assignment(request_id="done-z", actor_id="rv2", assignment_id=asg_z)

        # 敏感岗位事件：分派、采信、完成
        service.assign_reviewer(request_id="asg-w", actor_id="a1", event_id=wang_event_id,
                                reviewer_actor="rv2")
        asg_w = _receipt_resource(service, "asg-w")
        service.decide_fact(request_id="dec-w1", actor_id="rv2", assignment_id=asg_w,
                            fact_id=_fact_id(service, "fact-w1"), decision="accepted",
                            note="敏感岗位值守属实")
        service.complete_assignment(request_id="done-w", actor_id="rv2", assignment_id=asg_w)

        # ---- 候选人公开同意 ----
        service.grant_consent(request_id="consent-z", actor_id="a1", candidate_id="cand-zhang")
        service.grant_consent(request_id="consent-w", actor_id="a1", candidate_id="cand-wang")

        # ---- 终审：推荐人不能批准自己的推荐，须由另一名授权人员确认 ----
        self_confirm_blocked = False
        try:
            service.confirm_selection(request_id="final-self", actor_id="op1",
                                      event_id=merged_event_id, decision="selected")
        except PermissionDenied:
            self_confirm_blocked = True
        reviewer_confirm_blocked = False
        try:
            service.confirm_selection(request_id="final-rv", actor_id="rv2",
                                      event_id=merged_event_id, decision="selected")
        except PermissionDenied:
            reviewer_confirm_blocked = True
        service.confirm_selection(request_id="final-z", actor_id="a2",
                                  event_id=merged_event_id, decision="selected",
                                  note="两个来源单位事实均采信")
        service.confirm_selection(request_id="final-w", actor_id="a2",
                                  event_id=wang_event_id, decision="selected",
                                  note="敏感岗位事迹内部确认，公开范围受限")

        # ---- 第一次公示 ----
        service.publish(request_id="pub-1", actor_id="a1", batch_id="b1", name="国庆坚守光荣榜（一）")
        first = service.list_publications("b1")[0]
        zhang_entry = next(item for item in first["entries"] if item["name"] == "张坚守")
        wang_entry = next(item for item in first["entries"] if item["name"] is None)

        # ---- 张坚守撤回公开同意：新公示不得出现，既有决定继续留痕 ----
        service.revoke_consent(request_id="revoke-z", actor_id="a1", candidate_id="cand-zhang")
        service.publish(request_id="pub-2", actor_id="a1", batch_id="b1", name="国庆坚守光荣榜（二）")
        publications = service.list_publications("b1")
        second = publications[1]
        second_names = [item["name"] for item in second["entries"]]

        trace = service.get_event_trace(actor_id="au1", event_id=merged_event_id)
        wang_trace = service.get_event_trace(actor_id="au1", event_id=wang_event_id)
        valid, event_count = service.verify_audit()

        result = {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "merged_fact_count": len(trace["facts"]),
            "merged_sources": trace["final_decision"]["basis"]["accepted_sources"],
            "replay_replayed": replay.replayed,
            "blocked_assignment_message": blocked,
            "self_confirm_blocked": self_confirm_blocked,
            "reviewer_confirm_blocked": reviewer_confirm_blocked,
            "zhang_public_name": zhang_entry["name"],
            "zhang_visible_fields": sorted(zhang_entry["visible_fields"]),
            "wang_visible_fields": sorted(wang_entry["visible_fields"]),
            "wang_has_no_name": wang_entry["name"] is None,
            "first_publication_entries": len(first["entries"]),
            "second_publication_entries": len(second["entries"]),
            "zhang_absent_from_second": "张坚守" not in second_names,
            "zhang_final_decision": trace["final_decision"]["decision"],
            "zhang_consent_revoked": trace["consent_ledger"][-1]["revoked_at"] is not None,
            "zhang_publication_links": len(trace["publication_links"]),
            "wang_publication_links": len(wang_trace["publication_links"]),
            "excluded_fact_statuses": sorted({fact["status"] for fact in trace["facts"]}),
            "has_supplement_trace": any(fact["supplement_requests"] for fact in trace["facts"]),
            "supplement_on_time": any(
                sup["accepted_status"] == "on_time"
                for fact in trace["facts"]
                for req in fact["supplement_requests"]
                for sup in req["supplements"]),
            "rule_version_id": rule_version_id,
        }
        database.close()
        return result


def _receipt_resource(service: HonorService, request_id: str) -> str:
    row = service.database.connection.execute(
        "SELECT resource_id FROM request_receipts WHERE request_id=?", (request_id,)
    ).fetchone()
    return row["resource_id"]


def _fact_id(service: HonorService, request_id: str) -> str:
    return _receipt_resource(service, request_id)


def _event_id(service: HonorService, request_id: str) -> str:
    row = service.database.connection.execute(
        "SELECT response_json FROM request_receipts WHERE request_id=?", (request_id,)
    ).fetchone()
    return json.loads(row["response_json"])["event_id"]


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
