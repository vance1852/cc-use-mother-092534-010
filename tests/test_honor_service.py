import unittest
from datetime import datetime, timedelta, timezone

from festival_foundation.clock import FixedClock
from festival_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from festival_foundation.honor_service import HonorService
from festival_foundation.honor_storage import HonorDatabase
from festival_foundation.service import DomainService

START = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


class HonorTestBase(unittest.TestCase):
    def setUp(self):
        self.database = HonorDatabase()
        self.clock = FixedClock(START)
        self.foundation = DomainService(self.database, self.clock)
        self.honor = HonorService(self.database, self.clock)
        f, h = self.foundation, self.honor
        f.register_organization(request_id="org", actor_id="bootstrap",
                                organization_id="o1", name="联合工作组")
        for rid, aid, name, role in [
                ("r-adm", "adm", "管理员", "admin"),
                ("r-opa", "opA", "甲推荐人", "operator"),
                ("r-opb", "opB", "乙推荐人", "operator"),
                ("r-rvx", "rvX", "核验人甲", "reviewer"),
                ("r-rvy", "rvY", "核验人乙", "reviewer"),
                ("r-aud", "aud1", "审计员", "auditor")]:
            f.register_actor(request_id=rid, actor_id="bootstrap" if aid == "adm" else "adm",
                             new_actor_id=aid, display_name=name, role=role, organization_id="o1")
        h.publish_rules(request_id="rules-v1", actor_id="adm",
                        params={"min_independent_sources": 2, "verify_hours": 48})
        h.create_batch(request_id="b1", actor_id="adm", batch_id="b1", title="国庆公示")

    def tearDown(self):
        self.database.close()

    def consent(self, candidate="zhang", scope=None):
        self.honor.grant_consent(
            request_id=f"consent-{candidate}", actor_id="adm", candidate_id=candidate,
            scope=scope or ["candidate_name", "organization", "department", "post_name",
                           "event_date", "event_location", "deed_summary"])

    def nominate(self, request_id, actor="opA", org="甲部门", stype="duty_fact",
                 content=None, candidate="zhang", sensitive=False):
        return self.honor.submit_nomination(
            request_id=request_id, actor_id=actor, batch_id="b1",
            candidate_id=candidate, candidate_name="张坚守", organization="联合工作组",
            department="机要处", post_name="值班长", event_date="2026-10-01",
            event_location="一号保障点", deed_summary="连续值守 24 小时",
            sensitive=sensitive,
            facts=[{"source_org": org, "source_type": stype,
                    "content": content or {"shift": "24h"},
                    "recommendation": "建议表彰"}])

    def fact_ids(self, nomination_id):
        trace = self.honor.nomination_trace(actor_id="aud1", nomination_id=nomination_id)
        return {f["source_org"]: f["fact_id"] for f in trace["facts"]}


class NominationTests(HonorTestBase):
    def test_requires_consent_before_nomination(self):
        with self.assertRaises(PermissionDenied):
            self.nominate("n1")

    def test_duplicate_recommendation_merges_and_keeps_contributors(self):
        self.consent()
        first = self.nominate("n1", org="甲部门")
        second = self.nominate("n2", actor="opB", org="乙部门", stype="collab_proof")
        self.assertTrue(second["merged"])
        self.assertEqual(first["nomination_id"], second["nomination_id"])
        trace = self.honor.nomination_trace(actor_id="aud1", nomination_id=first["nomination_id"])
        self.assertEqual({"甲部门", "乙部门"}, set(trace["contributors"]))
        submitters = {f["submitted_by"] for f in trace["facts"]}
        self.assertEqual({"opA", "opB"}, submitters)

    def test_identical_resubmit_is_stable_dedup(self):
        self.consent()
        first = self.nominate("n1", org="甲部门")
        again = self.nominate("n1", org="甲部门")
        self.assertTrue(again["replayed"])
        self.assertEqual(first["nomination_id"], again["nomination_id"])
        distinct_request = self.nominate("n2", org="甲部门")
        self.assertEqual(1, distinct_request["duplicate_count"])
        trace = self.honor.nomination_trace(actor_id="aud1", nomination_id=first["nomination_id"])
        self.assertEqual(1, len(trace["facts"]))

    def test_same_org_distinct_content_stays_separate_fact(self):
        self.consent()
        first = self.nominate("n1", org="甲部门", content={"shift": "24h"})
        second = self.nominate("n2", org="甲部门", content={"log": "签到表"})
        self.assertTrue(second["merged"])
        trace = self.honor.nomination_trace(actor_id="aud1",
                                           nomination_id=first["nomination_id"])
        self.assertEqual(2, len(trace["facts"]))
        self.assertEqual(["甲部门", "甲部门"], [f["source_org"] for f in trace["facts"]])

    def test_nomination_deadline_is_enforced(self):
        self.consent()
        self.clock._value = START + timedelta(hours=73)
        with self.assertRaises(ConflictError):
            self.nominate("late")

    def test_different_event_same_candidate_separates(self):
        self.consent()
        a = self.nominate("n1", content={"shift": "day1"})
        b = self.honor.submit_nomination(
            request_id="n2", actor_id="opA", batch_id="b1",
            candidate_id="zhang", candidate_name="张坚守", organization="联合工作组",
            department="机要处", post_name="值班长", event_date="2026-10-02",
            event_location="一号保障点", deed_summary="次日继续值守",
            facts=[{"source_org": "甲部门", "source_type": "duty_fact",
                    "content": {"shift": "day2"}}])
        self.assertNotEqual(a["nomination_id"], b["nomination_id"])


class ConflictAndVerificationTests(HonorTestBase):
    def _ready(self):
        self.consent()
        first = self.nominate("n1", org="甲部门")
        self.nominate("n2", actor="opB", org="乙部门", stype="collab_proof",
                      content={"confirmed": 24})
        return first["nomination_id"]

    def test_declared_coi_blocks_verifier_assignment(self):
        nomination_id = self._ready()
        self.honor.declare_conflict(request_id="coi", actor_id="adm",
                                    party_a="opA", party_b="rvX", relation="直系亲属")
        with self.assertRaises(PermissionDenied):
            self.honor.assign_verifier(request_id="vx", actor_id="adm",
                                       nomination_id=nomination_id, verifier_id="rvX")

    def test_only_reviewer_role_can_be_assigned(self):
        nomination_id = self._ready()
        with self.assertRaises(PermissionDenied):
            self.honor.assign_verifier(request_id="vop", actor_id="adm",
                                       nomination_id=nomination_id, verifier_id="opA")

    def test_non_assigned_verifier_cannot_decide(self):
        nomination_id = self._ready()
        self.honor.assign_verifier(request_id="vy", actor_id="adm",
                                   nomination_id=nomination_id, verifier_id="rvY")
        fact_id = next(iter(self.fact_ids(nomination_id).values()))
        with self.assertRaises(PermissionDenied):
            self.honor.decide_fact(actor_id="rvX", fact_id=fact_id, outcome="accepted")

    def test_decision_is_stable_under_duplicate_calls(self):
        nomination_id = self._ready()
        self.honor.assign_verifier(request_id="vy", actor_id="adm",
                                   nomination_id=nomination_id, verifier_id="rvY")
        fact_id = self.fact_ids(nomination_id)["甲部门"]
        d1 = self.honor.decide_fact(actor_id="rvY", fact_id=fact_id, outcome="accepted")
        d2 = self.honor.decide_fact(actor_id="rvY", fact_id=fact_id, outcome="accepted")
        self.assertFalse(d1["replayed"])
        self.assertTrue(d2["replayed"])
        self.assertEqual(d1["decision_id"], d2["decision_id"])
        with self.assertRaises(ConflictError):
            self.honor.decide_fact(actor_id="rvY", fact_id=fact_id, outcome="excluded",
                                   reason="改判")

    def test_excluded_fact_requires_reason(self):
        nomination_id = self._ready()
        self.honor.assign_verifier(request_id="vy", actor_id="adm",
                                   nomination_id=nomination_id, verifier_id="rvY")
        fact_id = self.fact_ids(nomination_id)["甲部门"]
        with self.assertRaises(ValidationError):
            self.honor.decide_fact(actor_id="rvY", fact_id=fact_id, outcome="excluded")

    def test_expired_assignment_blocks_decide_and_allows_reassign(self):
        nomination_id = self._ready()
        assignment = self.honor.assign_verifier(
            request_id="vy", actor_id="adm", nomination_id=nomination_id, verifier_id="rvY")
        fact_id = self.fact_ids(nomination_id)["甲部门"]
        self.clock._value = START + timedelta(hours=49)
        with self.assertRaises(ConflictError):
            self.honor.decide_fact(actor_id="rvY", fact_id=fact_id, outcome="accepted")
        # 过期后可重新分派给另一核验人，原分派留痕。
        self.honor.assign_verifier(request_id="vx2", actor_id="adm",
                                   nomination_id=nomination_id, verifier_id="rvX")
        self.honor.decide_fact(actor_id="rvX", fact_id=fact_id, outcome="accepted")
        trace = self.honor.nomination_trace(actor_id="aud1", nomination_id=nomination_id)
        statuses = {v["status"] for v in trace["verifications"]}
        self.assertEqual({"expired", "active"}, statuses)
        self.assertEqual(assignment["verification_id"], trace["verifications"][0]["verification_id"])

    def test_supplement_extends_deadline_late_supplement_kept(self):
        nomination_id = self._ready()
        self.honor.assign_verifier(request_id="vy", actor_id="adm",
                                   nomination_id=nomination_id, verifier_id="rvY")
        fact_id = self.fact_ids(nomination_id)["甲部门"]
        self.honor.decide_fact(actor_id="rvY", fact_id=fact_id, outcome="supplement_requested",
                               reason="缺交接班记录")
        # 47 小时时补证：及时，期限从 48h 顺延 24h，到 71h 仍可核验。
        self.clock._value = START + timedelta(hours=47)
        result = self.honor.submit_supplement(request_id="s1", actor_id="opA",
                                              fact_id=fact_id, content={"handover": "A-12"})
        self.assertFalse(result["late"])
        self.clock._value = START + timedelta(hours=71)
        decided = self.honor.decide_fact(actor_id="rvY", fact_id=fact_id, outcome="accepted")
        self.assertFalse(decided["replayed"])
        # 另一条事实要求补证后逾期提交：标记迟到，留痕但不顺延。
        other = self.fact_ids(nomination_id)["乙部门"]
        self.honor.decide_fact(actor_id="rvY", fact_id=other, outcome="supplement_requested",
                               reason="缺协作函")
        self.clock._value = START + timedelta(hours=200)
        late = self.honor.submit_supplement(request_id="s2", actor_id="opB",
                                            fact_id=other, content={"letter": "C-3"})
        self.assertTrue(late["late"])
        trace = self.honor.nomination_trace(actor_id="aud1", nomination_id=nomination_id)
        other_fact = next(f for f in trace["facts"] if f["fact_id"] == other)
        self.assertTrue(other_fact["supplements"][0]["late"])


class ConfirmationTests(HonorTestBase):
    def _verified(self):
        self.consent()
        first = self.nominate("n1", org="甲部门")
        self.nominate("n2", actor="opB", org="乙部门", stype="collab_proof",
                      content={"confirmed": 24})
        nomination_id = first["nomination_id"]
        self.honor.assign_verifier(request_id="vy", actor_id="adm",
                                   nomination_id=nomination_id, verifier_id="rvY")
        for fact_id in self.fact_ids(nomination_id).values():
            self.honor.decide_fact(actor_id="rvY", fact_id=fact_id, outcome="accepted")
        return nomination_id

    def test_nobody_can_approve_own_recommendation(self):
        nomination_id = self._verified()
        with self.assertRaises(PermissionDenied):
            self.honor.confirm_selection(request_id="c-a", actor_id="opA",
                                         nomination_id=nomination_id, outcome="selected")
        with self.assertRaises(PermissionDenied):
            self.honor.confirm_selection(request_id="c-b", actor_id="opB",
                                         nomination_id=nomination_id, outcome="selected")

    def test_verifier_cannot_confirm_own_verification(self):
        nomination_id = self._verified()
        with self.assertRaises(PermissionDenied):
            self.honor.confirm_selection(request_id="c-y", actor_id="rvY",
                                         nomination_id=nomination_id, outcome="selected")

    def test_independent_orgs_counted_single_org_insufficient(self):
        self.consent()
        first = self.nominate("n1", org="甲部门", content={"a": 1})
        self.nominate("n2", org="甲部门", content={"b": 2})
        nomination_id = first["nomination_id"]
        self.honor.assign_verifier(request_id="vy", actor_id="adm",
                                   nomination_id=nomination_id, verifier_id="rvY")
        trace = self.honor.nomination_trace(actor_id="aud1", nomination_id=nomination_id)
        for fact in trace["facts"]:
            self.honor.decide_fact(actor_id="rvY", fact_id=fact["fact_id"], outcome="accepted")
        with self.assertRaises(ConflictError):
            self.honor.confirm_selection(request_id="c", actor_id="adm",
                                         nomination_id=nomination_id, outcome="selected")

    def test_unresolved_facts_block_selection(self):
        self.consent()
        first = self.nominate("n1", org="甲部门")
        self.nominate("n2", actor="opB", org="乙部门", stype="collab_proof")
        nomination_id = first["nomination_id"]
        self.honor.assign_verifier(request_id="vy", actor_id="adm",
                                   nomination_id=nomination_id, verifier_id="rvY")
        ids = self.fact_ids(nomination_id)
        self.honor.decide_fact(actor_id="rvY", fact_id=ids["甲部门"], outcome="accepted")
        with self.assertRaises(ConflictError):
            self.honor.confirm_selection(request_id="c", actor_id="adm",
                                         nomination_id=nomination_id, outcome="selected")

    def test_excluded_fact_can_be_overruled_by_two_other_sources(self):
        self.consent()
        first = self.nominate("n1", org="甲部门")
        self.nominate("n2", actor="opB", org="乙部门", stype="collab_proof",
                      content={"x": 1})
        # 第三方证明
        self.honor.submit_nomination(
            request_id="n3", actor_id="opA", batch_id="b1",
            candidate_id="zhang", candidate_name="张坚守", organization="联合工作组",
            department="机要处", post_name="值班长", event_date="2026-10-01",
            event_location="一号保障点", deed_summary="连续值守 24 小时",
            facts=[{"source_org": "丙单位", "source_type": "collab_proof",
                    "content": {"witness": "yes"}}])
        nomination_id = first["nomination_id"]
        self.honor.assign_verifier(request_id="vy", actor_id="adm",
                                   nomination_id=nomination_id, verifier_id="rvY")
        ids = self.fact_ids(nomination_id)
        self.honor.decide_fact(actor_id="rvY", fact_id=ids["甲部门"], outcome="excluded",
                               reason="排班记录与所述时间不符")
        self.honor.decide_fact(actor_id="rvY", fact_id=ids["乙部门"], outcome="accepted")
        self.honor.decide_fact(actor_id="rvY", fact_id=ids["丙单位"], outcome="accepted")
        result = self.honor.confirm_selection(request_id="c", actor_id="adm",
                                              nomination_id=nomination_id, outcome="selected")
        self.assertEqual("selected", result["outcome"])
        trace = self.honor.nomination_trace(actor_id="aud1", nomination_id=nomination_id)
        excluded = [f for f in trace["facts"] if f["verification_status"] == "excluded"]
        self.assertEqual("排班记录与所述时间不符", excluded[0]["excluded_reason"])

    def test_selection_is_idempotent_and_terminal(self):
        nomination_id = self._verified()
        c1 = self.honor.confirm_selection(request_id="c", actor_id="adm",
                                          nomination_id=nomination_id, outcome="selected")
        c2 = self.honor.confirm_selection(request_id="c", actor_id="adm",
                                          nomination_id=nomination_id, outcome="selected")
        self.assertTrue(c2["replayed"])
        self.assertEqual(c1["confirmation_id"], c2["confirmation_id"])
        with self.assertRaises(ConflictError):
            self.honor.confirm_selection(request_id="c2", actor_id="adm",
                                         nomination_id=nomination_id, outcome="rejected")


class RulesAndConsentTests(HonorTestBase):
    def test_rule_change_only_refreshes_open_batch(self):
        self.consent()
        first = self.nominate("n1", org="甲部门")
        self.nominate("n2", actor="opB", org="乙部门", stype="collab_proof")
        nomination_id = first["nomination_id"]
        self.honor.assign_verifier(request_id="vy", actor_id="adm",
                                   nomination_id=nomination_id, verifier_id="rvY")
        for fact_id in self.fact_ids(nomination_id).values():
            self.honor.decide_fact(actor_id="rvY", fact_id=fact_id, outcome="accepted")
        confirmed = self.honor.confirm_selection(
            request_id="c", actor_id="adm", nomination_id=nomination_id, outcome="selected")
        self.assertEqual(1, confirmed["rule_version"])
        self.honor.publish_rules(request_id="v2", actor_id="adm",
                                 params={"min_independent_sources": 3, "verify_hours": 24})
        refreshed = self.honor.refresh_batch_rules(request_id="r1", actor_id="adm", batch_id="b1")
        self.assertEqual(2, refreshed["rule_version"])
        self.honor.finalize_batch(request_id="f", actor_id="adm", batch_id="b1")
        with self.assertRaises(ConflictError):
            self.honor.refresh_batch_rules(request_id="r2", actor_id="adm", batch_id="b1")
        # 终局决定仍记录当时的规则版本 1。
        trace = self.honor.nomination_trace(actor_id="aud1", nomination_id=nomination_id)
        self.assertEqual(1, trace["confirmation"]["rule_version"])

    def test_revoked_consent_blocks_new_nomination_but_keeps_decision(self):
        self.consent()
        first = self.nominate("n1", org="甲部门")
        self.nominate("n2", actor="opB", org="乙部门", stype="collab_proof")
        nomination_id = first["nomination_id"]
        self.honor.assign_verifier(request_id="vy", actor_id="adm",
                                   nomination_id=nomination_id, verifier_id="rvY")
        for fact_id in self.fact_ids(nomination_id).values():
            self.honor.decide_fact(actor_id="rvY", fact_id=fact_id, outcome="accepted")
        self.honor.confirm_selection(request_id="c", actor_id="adm",
                                     nomination_id=nomination_id, outcome="selected")
        self.honor.finalize_batch(request_id="f", actor_id="adm", batch_id="b1")
        self.honor.publish_batch(request_id="p", actor_id="adm", batch_id="b1")
        self.assertEqual(1, len(self.honor.public_honors("b1")["items"]))
        self.honor.revoke_consent(request_id="rev", actor_id="adm", candidate_id="zhang")
        # 既有内部决定与公示快照仍留痕，但公开接口不再输出。
        self.assertEqual(0, len(self.honor.public_honors("b1")["items"]))
        trace = self.honor.nomination_trace(actor_id="aud1", nomination_id=nomination_id)
        self.assertEqual("selected", trace["confirmation"]["outcome"])
        # 新批次不得再推荐未同意的候选人。
        self.honor.publish_rules(request_id="v2b", actor_id="adm")
        self.honor.create_batch(request_id="b2", actor_id="adm", batch_id="b2", title="次年公示")
        with self.assertRaises(PermissionDenied):
            self.honor.submit_nomination(
                request_id="nx", actor_id="opA", batch_id="b2",
                candidate_id="zhang", candidate_name="张坚守", organization="联合工作组",
                department="机要处", post_name="值班长", event_date="2027-01-01",
                event_location="一号保障点", deed_summary="值守",
                facts=[{"source_org": "甲部门", "source_type": "duty_fact", "content": {"a": 1}}])


class PublicationTests(HonorTestBase):
    def _publish(self, sensitive=False):
        self.consent()
        first = self.nominate("n1", org="甲部门", sensitive=sensitive)
        self.nominate("n2", actor="opB", org="乙部门", stype="collab_proof",
                      content={"confirmed": 24}, sensitive=sensitive)
        nomination_id = first["nomination_id"]
        self.honor.assign_verifier(request_id="vy", actor_id="adm",
                                   nomination_id=nomination_id, verifier_id="rvY")
        for fact_id in self.fact_ids(nomination_id).values():
            self.honor.decide_fact(actor_id="rvY", fact_id=fact_id, outcome="accepted")
        self.honor.confirm_selection(request_id="c", actor_id="adm",
                                     nomination_id=nomination_id, outcome="selected")
        self.honor.finalize_batch(request_id="f", actor_id="adm", batch_id="b1")
        self.honor.publish_batch(request_id="p", actor_id="adm", batch_id="b1")
        return nomination_id

    def test_public_interface_only_outputs_allowed_fields(self):
        self._publish()
        items = self.honor.public_honors("b1")["items"]
        self.assertEqual(
            {"candidate_name", "organization", "department", "post_name",
             "event_date", "event_location", "deed_summary"},
            set(items[0].keys()))

    def test_sensitive_post_masks_configured_fields(self):
        self._publish(sensitive=True)
        entry = self.honor.public_honors("b1")["items"][0]
        self.assertNotIn("department", entry)
        self.assertNotIn("post_name", entry)
        self.assertNotIn("event_location", entry)
        self.assertIn("candidate_name", entry)

    def test_consent_scope_is_respected(self):
        # 收窄同意范围后再走流程：候选人只同意公开姓名。
        self.consent(scope=["candidate_name"])
        first = self.nominate("n1", org="甲部门")
        self.nominate("n2", actor="opB", org="乙部门", stype="collab_proof")
        nomination_id = first["nomination_id"]
        self.honor.assign_verifier(request_id="vy", actor_id="adm",
                                   nomination_id=nomination_id, verifier_id="rvY")
        for fact_id in self.fact_ids(nomination_id).values():
            self.honor.decide_fact(actor_id="rvY", fact_id=fact_id, outcome="accepted")
        self.honor.confirm_selection(request_id="c", actor_id="adm",
                                     nomination_id=nomination_id, outcome="selected")
        self.honor.finalize_batch(request_id="f", actor_id="adm", batch_id="b1")
        self.honor.publish_batch(request_id="p", actor_id="adm", batch_id="b1")
        self.assertEqual(["candidate_name"], list(self.honor.public_honors("b1")["items"][0].keys()))

    def test_unpublished_batch_not_public(self):
        with self.assertRaises(NotFoundError):
            self.honor.public_honors("b1")


class AccessControlTests(HonorTestBase):
    def test_internal_queries_require_admin_or_auditor(self):
        self.consent()
        first = self.nominate("n1", org="甲部门")
        with self.assertRaises(PermissionDenied):
            self.honor.nomination_trace(actor_id="opA", nomination_id=first["nomination_id"])
        with self.assertRaises(PermissionDenied):
            self.honor.batch_detail(actor_id="rvY", batch_id="b1")
        detail = self.honor.batch_detail(actor_id="aud1", batch_id="b1")
        self.assertEqual("open", detail["status"])

    def test_operator_cannot_publish_rules_or_finalize(self):
        with self.assertRaises(PermissionDenied):
            self.honor.publish_rules(request_id="x", actor_id="opA")
        with self.assertRaises(PermissionDenied):
            self.honor.finalize_batch(request_id="x", actor_id="opA", batch_id="b1")


if __name__ == "__main__":
    unittest.main()
