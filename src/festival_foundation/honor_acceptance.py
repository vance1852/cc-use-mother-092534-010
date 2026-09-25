"""荣誉核验与荣誉公示后台的离线端到端验收。

覆盖：规则冻结、回避拦截、多方推荐合并、重复提交稳定、有期限核验、
补证与迟到补证、他人终局确认、禁止批准自己的推荐、规则版本锁定、
撤回同意后公开接口过滤而内部决定留痕。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FixedClock
from .errors import ConflictError, PermissionDenied
from .honor_service import HonorService
from .honor_storage import HonorDatabase
from .service import DomainService

START = datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc)


def _seed(foundation: DomainService) -> None:
    foundation.register_organization(request_id="org", actor_id="bootstrap",
                                     organization_id="o1", name="节日保障联合工作组")
    foundation.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="adm",
                              display_name="管理员", role="admin", organization_id="o1")
    foundation.register_actor(request_id="op-a", actor_id="adm", new_actor_id="opA",
                              display_name="甲部门推荐人", role="operator", organization_id="o1")
    foundation.register_actor(request_id="op-b", actor_id="adm", new_actor_id="opB",
                              display_name="乙部门推荐人", role="operator", organization_id="o1")
    foundation.register_actor(request_id="rv-x", actor_id="adm", new_actor_id="rvX",
                              display_name="核验人甲", role="reviewer", organization_id="o1")
    foundation.register_actor(request_id="rv-y", actor_id="adm", new_actor_id="rvY",
                              display_name="核验人乙", role="reviewer", organization_id="o1")
    foundation.register_actor(request_id="aud", actor_id="adm", new_actor_id="aud1",
                              display_name="审计员", role="auditor", organization_id="o1")


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = HonorDatabase(Path(directory) / "honor_acceptance.sqlite3")
        clock = FixedClock(START)
        foundation = DomainService(database, clock)
        honor = HonorService(database, clock)
        _seed(foundation)

        # 1) 发布冻结规则并开启批次。
        rules = honor.publish_rules(request_id="rules-v1", actor_id="adm",
                                    params={"min_independent_sources": 2, "verify_hours": 48})
        batch = honor.create_batch(request_id="batch-1", actor_id="adm",
                                   batch_id="b1", title="2026 国庆坚守荣誉公示")
        # 2) 回避关系：推荐人 opA 与核验人 rvX 存在直接利益关系。
        honor.declare_conflict(request_id="coi-1", actor_id="adm",
                               party_a="opA", party_b="rvX", relation="直系亲属")
        # 3) 候选人授予可公开范围。
        honor.grant_consent(request_id="consent-zhang", actor_id="adm",
                            candidate_id="zhang",
                            scope=["candidate_name", "organization", "post_name",
                                   "event_date", "deed_summary"])

        # 4) 甲部门推荐并带来源事实。
        first = honor.submit_nomination(
            request_id="nom-a", actor_id="opA", batch_id="b1",
            candidate_id="zhang", candidate_name="张坚守", organization="联合工作组",
            department="机要值班处", post_name="值班长", event_date="2026-10-01",
            event_location="一号保障点", deed_summary="连续值守 24 小时保障调度",
            sensitive=True,
            facts=[{"source_org": "甲部门", "source_type": "duty_fact",
                    "content": {"shift": "10-01 08:00 至次日 08:00"},
                    "recommendation": "建议公示表彰"}])
        nomination_id = first["nomination_id"]
        # 5) 乙部门重复推荐同一事件：自动合并，各自贡献独立保留。
        second = honor.submit_nomination(
            request_id="nom-b", actor_id="opB", batch_id="b1",
            candidate_id="zhang", candidate_name="张坚守", organization="联合工作组",
            department="协作调度处", post_name="值班长", event_date="2026-10-01",
            event_location="一号保障点", deed_summary="协作单位证明其全程在岗",
            sensitive=True,
            facts=[{"source_org": "乙部门", "source_type": "collab_proof",
                    "content": {"confirmed_hours": 24}}])
        # 6) 完全相同的重复提交返回稳定结果（同 nomination，重复计数）。
        replay = honor.submit_nomination(
            request_id="nom-b-replay", actor_id="opB", batch_id="b1",
            candidate_id="zhang", candidate_name="张坚守", organization="联合工作组",
            department="协作调度处", post_name="值班长", event_date="2026-10-01",
            event_location="一号保障点", deed_summary="协作单位证明其全程在岗",
            sensitive=True,
            facts=[{"source_org": "乙部门", "source_type": "collab_proof",
                    "content": {"confirmed_hours": 24}}])

        # 7) 有回避关系的 rvX 不能被分派。
        blocked = False
        try:
            honor.assign_verifier(request_id="assign-x", actor_id="adm",
                                  nomination_id=nomination_id, verifier_id="rvX")
        except PermissionDenied:
            blocked = True
        # 8) 分派无关联的 rvY，带 48 小时期限。
        assignment = honor.assign_verifier(request_id="assign-y", actor_id="adm",
                                           nomination_id=nomination_id, verifier_id="rvY")

        # 直接通过内部追踪取得事实编号。
        trace = honor.nomination_trace(actor_id="aud1", nomination_id=nomination_id)
        fact_by_org = {f["source_org"]: f["fact_id"] for f in trace["facts"]}

        # 9) 核验人对甲方事实要求补证。
        d1 = honor.decide_fact(actor_id="rvY", fact_id=fact_by_org["甲部门"],
                               outcome="supplement_requested", reason="缺交接班记录")
        # 10) 甲方在期限内补证，核验期限顺延。
        supp = honor.submit_supplement(request_id="supp-1", actor_id="opA",
                                       fact_id=fact_by_org["甲部门"],
                                       content={"handover": "签到表编号 A-12"})
        # 11) 采信两条事实；重复调用产生稳定重放。
        honor.decide_fact(actor_id="rvY", fact_id=fact_by_org["甲部门"], outcome="accepted")
        accept_again = honor.decide_fact(actor_id="rvY", fact_id=fact_by_org["甲部门"],
                                         outcome="accepted")
        honor.decide_fact(actor_id="rvY", fact_id=fact_by_org["乙部门"], outcome="accepted")

        # 12) 推荐人本人不能确认；核验人也不能确认自己核验的记录。
        self_blocked = False
        try:
            honor.confirm_selection(request_id="conf-self", actor_id="opA",
                                    nomination_id=nomination_id, outcome="selected")
        except PermissionDenied:
            self_blocked = True
        verifier_blocked = False
        try:
            honor.confirm_selection(request_id="conf-rv", actor_id="rvY",
                                    nomination_id=nomination_id, outcome="selected")
        except PermissionDenied:
            verifier_blocked = True
        # 13) 另一名无关联授权人员终局确认入选（规则版本随决定固化）。
        confirmation = honor.confirm_selection(
            request_id="conf-final", actor_id="adm", nomination_id=nomination_id,
            outcome="selected", reason="两方独立来源均已采信")

        # 14) 第二条候选记录用于演示迟到补证：先要求补证，再把时钟拨到期限之后。
        honor.grant_consent(request_id="consent-li", actor_id="adm", candidate_id="li",
                            scope=["candidate_name", "organization", "event_date", "deed_summary"])
        li = honor.submit_nomination(
            request_id="nom-li", actor_id="opA", batch_id="b1",
            candidate_id="li", candidate_name="李值守", organization="联合工作组",
            department="后勤处", post_name="驾驶员", event_date="2026-10-02",
            event_location="二号保障点", deed_summary="节日期间运送保障物资",
            facts=[{"source_org": "甲部门", "source_type": "duty_fact",
                    "content": {"trips": 6}},
                   {"source_org": "乙部门", "source_type": "collab_proof",
                    "content": {"cargo": "应急物资"}}])
        honor.assign_verifier(request_id="assign-li", actor_id="adm",
                              nomination_id=li["nomination_id"], verifier_id="rvY")
        li_trace = honor.nomination_trace(actor_id="aud1", nomination_id=li["nomination_id"])
        li_fact = next(f["fact_id"] for f in li_trace["facts"] if f["source_org"] == "甲部门")
        honor.decide_fact(actor_id="rvY", fact_id=li_fact, outcome="supplement_requested",
                          reason="缺行车记录")

        # 15) 规则变化只影响未完成批次：v2 发布后开放批次可刷新，已终局决定锁定 v1。
        honor.publish_rules(request_id="rules-v2", actor_id="adm",
                            params={"min_independent_sources": 3, "verify_hours": 24})
        refreshed = honor.refresh_batch_rules(request_id="refresh-b1", actor_id="adm", batch_id="b1")

        # 时钟越过核验期限：补证被标记为迟到，留痕但不顺延期限。
        honor.clock = FixedClock(START + timedelta(hours=72))
        late_supp = honor.submit_supplement(request_id="supp-late", actor_id="opA",
                                            fact_id=li_fact, content={"log": "行车单 B-7"})

        # 16) 冻结后规则版本锁定，不能再刷新。
        honor.finalize_batch(request_id="finalize-b1", actor_id="adm", batch_id="b1")
        refresh_blocked = False
        try:
            honor.refresh_batch_rules(request_id="refresh-b1-again", actor_id="adm", batch_id="b1")
        except ConflictError:
            refresh_blocked = True

        # 17) 公示：敏感岗位遮罩 + 同意范围裁剪（li 未完成核验，不进入公示）。
        publication = honor.publish_batch(request_id="publish-b1", actor_id="adm", batch_id="b1")
        public_before = honor.public_honors("b1")

        # 18) 撤回公开同意：新公开输出移除该人，内部决定仍留痕。
        honor.revoke_consent(request_id="revoke-zhang", actor_id="adm", candidate_id="zhang",
                             reason="候选人个人原因撤回")
        public_after = honor.public_honors("b1")
        internal = honor.nomination_trace(actor_id="aud1", nomination_id=nomination_id)
        li_internal = honor.nomination_trace(actor_id="aud1", nomination_id=li["nomination_id"])
        from .audit import verify_chain
        chain_ok, chain_count = verify_chain(database.connection)

        database.close()

    result = {
        "status": "ok",
        "rule_version": rules["version"],
        "merged": bool(second["merged"]) and second["nomination_id"] == first["nomination_id"],
        "contributors": sorted(internal["contributors"].keys()),
        "duplicate_count": replay["duplicate_count"],
        "coi_blocked_assignment": blocked,
        "deadline_extended": supp["late"] is False,
        "late_supplement_marked": late_supp["late"] is True,
        "late_supplement_traced": li_internal["facts"][0]["supplements"][0]["late"] is True,
        "decision_replayed": accept_again["replayed"],
        "self_confirmation_blocked": self_blocked,
        "verifier_confirmation_blocked": verifier_blocked,
        "confirmed_rule_version": confirmation["rule_version"],
        "batch_rule_refreshed_while_open": refreshed["rule_version"],
        "rule_refresh_blocked_after_final": refresh_blocked,
        "independent_sources": internal["eligibility"]["accepted_independent_sources"],
        "public_entries": publication["entries"],
        "public_fields": sorted(public_before["items"][0].keys()) if public_before["items"] else [],
        "sensitive_masked": "post_name" not in public_before["items"][0],
        "public_entries_after_revoke": len(public_after["items"]),
        "internal_decision_preserved": internal["confirmation"]["outcome"] == "selected",
        "consent_revoke_kept": any(not c["active"] for c in internal["consent_history"]),
        "audit_valid": chain_ok,
        "audit_events": chain_count,
    }
    expected = {
        "status": "ok", "rule_version": 1, "merged": True,
        "contributors": ["乙部门", "甲部门"], "duplicate_count": 1,
        "coi_blocked_assignment": True, "deadline_extended": True,
        "late_supplement_marked": True, "late_supplement_traced": True,
        "decision_replayed": True, "self_confirmation_blocked": True,
        "verifier_confirmation_blocked": True, "confirmed_rule_version": 1,
        "batch_rule_refreshed_while_open": 2,
        "rule_refresh_blocked_after_final": True, "independent_sources": 2,
        "public_entries": 1,
        "public_fields": ["candidate_name", "deed_summary", "event_date",
                          "organization"],
        "sensitive_masked": True, "public_entries_after_revoke": 0,
        "internal_decision_preserved": True, "consent_revoke_kept": True,
        "audit_valid": True,
    }
    result["ok"] = all(result[key] == value for key, value in expected.items())
    result["status"] = "ok" if result["ok"] else "failed"
    return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
