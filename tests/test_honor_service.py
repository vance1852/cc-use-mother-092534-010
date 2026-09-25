"""节日荣誉核验领域的核心规则测试。"""

import unittest
from datetime import datetime, timezone
from threading import Barrier, Thread

from festival_foundation.errors import (
    ConflictError, NotFoundError, PermissionDenied, ValidationError,
)

from festival_honor.clock import MutableClock
from festival_honor.service import HonorService
from festival_honor.storage import HonorDatabase

START = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
FULL_SCOPE = ["name", "organization", "duty_start", "duty_end", "story", "source_units"]


class HonorFixture:
    def __init__(self):
        self.database = HonorDatabase()
        self.clock = MutableClock(START)
        self.service = HonorService(self.database, self.clock)
        s = self.service
        for oid, name in (("o1", "单位一"), ("o2", "单位二"), ("o3", "复核单位")):
            s.register_organization(request_id=f"org-{oid}", actor_id="bootstrap",
                                    organization_id=oid, name=name)
        s.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                         display_name="管理员", role="admin", organization_id="o3")
        s.register_actor(request_id="op1", actor_id="a1", new_actor_id="op1",
                         display_name="推荐人甲", role="operator", organization_id="o1")
        s.register_actor(request_id="op2", actor_id="a1", new_actor_id="op2",
                         display_name="推荐人乙", role="operator", organization_id="o2")
        s.register_actor(request_id="rv1", actor_id="a1", new_actor_id="rv1",
                         display_name="核验人甲", role="reviewer", organization_id="o1")
        s.register_actor(request_id="rv2", actor_id="a1", new_actor_id="rv2",
                         display_name="核验人乙", role="reviewer", organization_id="o3")
        s.register_actor(request_id="au1", actor_id="a1", new_actor_id="au1",
                         display_name="审计员", role="auditor", organization_id="o3")
        s.register_site(request_id="site", actor_id="op1", site_id="s1",
                        organization_id="o1", name="值守站", timezone_name="Asia/Shanghai")

    def close(self):
        self.database.close()


def freeze_and_batch(service: HonorService, *, rule_payload=None, batch_id="b1",
                     deadline="2026-10-03T00:00:00Z"):
    receipt = service.freeze_rule(request_id=f"rule-{batch_id}", actor_id="a1",
                                  payload=rule_payload, note="规则")
    service.create_batch(request_id=f"open-{batch_id}", actor_id="a1", batch_id=batch_id,
                         name="批次", rule_version_id=receipt.resource_id,
                         submission_deadline=deadline)
    return receipt.resource_id


def fact_kwargs(**overrides):
    base = {"batch_id": "b1", "candidate_id": "cand-1", "candidate_name": "张三",
            "site_id": "s1", "duty_start": "2026-10-02T08:00:00+08:00",
            "duty_end": "2026-10-02T16:00:00+08:00", "reason": "节日值守",
            "story": "坚守岗位一天", "public_scope": FULL_SCOPE, "sensitive": False,
            "proofs": [{"kind": "roster", "reference": "ref-1",
                        "issuer_organization_id": "o1"}]}
    base.update(overrides)
    return base


def receipt_response(service, request_id):
    import json
    row = service.database.connection.execute(
        "SELECT response_json FROM request_receipts WHERE request_id=?", (request_id,)
    ).fetchone()
    return json.loads(row["response_json"])


class MergeAndSubmitTest(unittest.TestCase):
    def setUp(self):
        self.helper = HonorFixture()
        self.service = self.helper.service
        freeze_and_batch(self.service)

    def tearDown(self):
        self.helper.close()

    def test_same_event_from_two_units_merges_without_losing_facts(self):
        r1 = self.service.submit_fact(request_id="f1", actor_id="op1", **fact_kwargs(
            reason="甲单位排班", proofs=[{"kind": "roster", "reference": "r1",
                                        "issuer_organization_id": "o1"}]))
        r2 = self.service.submit_fact(request_id="f2", actor_id="op2", **fact_kwargs(
            reason="乙单位联合证明", proofs=[{"kind": "cert", "reference": "r2",
                                            "issuer_organization_id": "o2"}]))
        self.assertEqual(receipt_response(self.service, "f1")["event_id"],
                         receipt_response(self.service, "f2")["event_id"])
        event_id = receipt_response(self.service, "f1")["event_id"]
        rows = self.helper.database.connection.execute(
            "SELECT COUNT(*) AS c FROM duty_facts WHERE event_id=?", (event_id,)
        ).fetchone()["c"]
        self.assertEqual(2, rows)

    def test_duplicate_submission_replays_stable_fact(self):
        first = self.service.submit_fact(request_id="dup", actor_id="op1", **fact_kwargs())
        second = self.service.submit_fact(request_id="dup", actor_id="op1", **fact_kwargs())
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)

    def test_identical_resubmission_with_new_request_id_is_marked_duplicate(self):
        self.service.submit_fact(request_id="ra", actor_id="op1", **fact_kwargs())
        again = self.service.submit_fact(request_id="rb", actor_id="op1", **fact_kwargs())
        self.assertTrue(receipt_response(self.service, "rb")["duplicate"])
        self.assertEqual(again.resource_id,
                         self.service.database.connection.execute(
                             "SELECT fact_id FROM duty_facts").fetchone()["fact_id"])

    def test_different_time_window_is_a_separate_event(self):
        self.service.submit_fact(request_id="f1", actor_id="op1", **fact_kwargs())
        self.service.submit_fact(request_id="f2", actor_id="op1", **fact_kwargs(
            duty_start="2026-10-03T08:00:00+08:00",
            duty_end="2026-10-03T16:00:00+08:00"))
        self.assertNotEqual(receipt_response(self.service, "f1")["event_id"],
                            receipt_response(self.service, "f2")["event_id"])

    def test_submission_after_deadline_is_rejected(self):
        self.helper.clock.advance(days=3)
        with self.assertRaises(ConflictError):
            self.service.submit_fact(request_id="late", actor_id="op1", **fact_kwargs())

    def test_sensitive_record_cannot_request_sensitive_fields(self):
        with self.assertRaises(ValidationError):
            self.service.submit_fact(request_id="sensitive-bad", actor_id="op1",
                                     **fact_kwargs(sensitive=True, public_scope=["name", "story"]))

    def test_proof_issuer_must_exist(self):
        with self.assertRaises(NotFoundError):
            self.service.submit_fact(request_id="bad-proof", actor_id="op1", **fact_kwargs(
                proofs=[{"kind": "roster", "reference": "x",
                         "issuer_organization_id": "unknown"}]))

    def test_concurrent_identical_submissions_yield_single_fact(self):
        barrier = Barrier(2)
        errors: list[Exception] = []

        def submit(request_id):
            try:
                barrier.wait(timeout=5)
                self.service.submit_fact(request_id=request_id, actor_id="op1",
                                         **fact_kwargs())
            except Exception as exc:  # noqa: BLE001 - 记录线程内错误
                errors.append(exc)

        threads = [Thread(target=submit, args=(f"parallel-{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual([], errors)
        count = self.helper.database.connection.execute(
            "SELECT COUNT(*) AS c FROM duty_facts").fetchone()["c"]
        events = self.helper.database.connection.execute(
            "SELECT COUNT(*) AS c FROM duty_events").fetchone()["c"]
        self.assertEqual(1, count)
        self.assertEqual(1, events)

    def test_concurrent_distinct_sources_merge_into_one_event(self):
        barrier = Barrier(2)
        errors: list[Exception] = []

        def submit(actor, request_id, reference, issuer):
            try:
                barrier.wait(timeout=5)
                self.service.submit_fact(
                    request_id=request_id, actor_id=actor,
                    **fact_kwargs(proofs=[{"kind": "roster", "reference": reference,
                                          "issuer_organization_id": issuer}]))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            Thread(target=submit, args=("op1", "c1", "r1", "o1")),
            Thread(target=submit, args=("op2", "c2", "r2", "o2")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual([], errors)
        self.assertEqual(2, self.helper.database.connection.execute(
            "SELECT COUNT(*) AS c FROM duty_facts").fetchone()["c"])
        self.assertEqual(1, self.helper.database.connection.execute(
            "SELECT COUNT(*) AS c FROM duty_events").fetchone()["c"])


class ConflictAndReviewTest(unittest.TestCase):
    def setUp(self):
        self.helper = HonorFixture()
        self.service = self.helper.service
        freeze_and_batch(self.service)
        self.service.submit_fact(request_id="f1", actor_id="op1", **fact_kwargs())
        self.event_id = receipt_response(self.service, "f1")["event_id"]
        self.fact_id = self.service.database.connection.execute(
            "SELECT fact_id FROM duty_facts").fetchone()["fact_id"]

    def tearDown(self):
        self.helper.close()

    def test_reviewer_who_submitted_is_blocked(self):
        # op1 具有 operator 角色，不能当核验人；rv1 与 op1 声明利益关系同样应被拦截
        self.service.declare_conflict(request_id="rel", actor_id="a1",
                                      subject_actor="rv1", other_actor="op1",
                                      relation="近亲属")
        with self.assertRaises(PermissionDenied):
            self.service.assign_reviewer(request_id="asg-bad", actor_id="a1",
                                         event_id=self.event_id, reviewer_actor="rv1")

    def test_only_reviewer_role_can_be_assigned(self):
        with self.assertRaises(ValidationError):
            self.service.assign_reviewer(request_id="asg-op", actor_id="a1",
                                         event_id=self.event_id, reviewer_actor="op2")

    def test_expired_assignment_blocks_decisions_and_can_reassign(self):
        self.service.assign_reviewer(request_id="asg", actor_id="a1",
                                     event_id=self.event_id, reviewer_actor="rv2")
        asg_id = self.service.database.connection.execute(
            "SELECT assignment_id FROM review_assignments").fetchone()["assignment_id"]
        self.helper.clock.advance(hours=73)
        with self.assertRaises(ConflictError):
            self.service.decide_fact(request_id="d-late", actor_id="rv2", assignment_id=asg_id,
                                     fact_id=self.fact_id, decision="accepted")
        # 超期后允许重新分派给新核验人
        self.service.assign_reviewer(request_id="asg2", actor_id="a1",
                                     event_id=self.event_id, reviewer_actor="rv2")

    def test_supplement_on_time_reopens_fact_but_late_proof_is_excluded(self):
        self.service.assign_reviewer(request_id="asg", actor_id="a1",
                                     event_id=self.event_id, reviewer_actor="rv2")
        asg_id = self.service.database.connection.execute(
            "SELECT assignment_id FROM review_assignments WHERE status='active'").fetchone()[
            "assignment_id"]
        self.service.decide_fact(request_id="d1", actor_id="rv2", assignment_id=asg_id,
                                 fact_id=self.fact_id, decision="supplement",
                                 note="需要补充盖章件")
        self.helper.clock.advance(hours=49)
        result = self.service.submit_supplement(
            request_id="sup-late", actor_id="op1", fact_id=self.fact_id,
            content="迟到的盖章件",
            proofs=[{"kind": "sealed", "reference": "late-1",
                     "issuer_organization_id": "o1"}])
        self.assertEqual("late_rejected", receipt_response(self.service, "sup-late")["accepted_status"])
        status = self.helper.database.connection.execute(
            "SELECT status FROM duty_facts WHERE fact_id=?", (self.fact_id,)
        ).fetchone()["status"]
        self.assertEqual("excluded", status)
        self.assertFalse(result.replayed)

    def test_reviewer_cannot_complete_with_unresolved_facts(self):
        self.service.assign_reviewer(request_id="asg", actor_id="a1",
                                     event_id=self.event_id, reviewer_actor="rv2")
        asg_id = self.service.database.connection.execute(
            "SELECT assignment_id FROM review_assignments").fetchone()["assignment_id"]
        with self.assertRaises(ConflictError):
            self.service.complete_assignment(request_id="done", actor_id="rv2",
                                             assignment_id=asg_id)

    def test_open_batch_can_rebind_new_rule_and_it_takes_effect(self):
        rule2 = self.service.freeze_rule(request_id="rule-v2", actor_id="a1",
                                         payload={"min_proofs": 2}, note="更严格证明要求")
        self.service.rebind_rule(request_id="rebind", actor_id="a1", batch_id="b1",
                                 rule_version_id=rule2.resource_id)
        with self.assertRaises(ValidationError):
            self.service.submit_fact(request_id="f2", actor_id="op2", **fact_kwargs(
                reason="乙单位证明",
                proofs=[{"kind": "cert", "reference": "r2", "issuer_organization_id": "o2"}]))
        batch = self.service.get_batch("b1")
        self.assertEqual(2, batch.rule_snapshot["min_proofs"])
        self.assertEqual(rule2.resource_id, batch.rule_version_id)


class FinalAndPublicationTest(unittest.TestCase):
    def setUp(self):
        self.helper = HonorFixture()
        self.service = self.helper.service
        freeze_and_batch(self.service)
        self.service.submit_fact(request_id="f1", actor_id="op1", **fact_kwargs(
            proofs=[{"kind": "roster", "reference": "r1", "issuer_organization_id": "o1"}]))
        self.event_id = receipt_response(self.service, "f1")["event_id"]
        self.fact_id = self.service.database.connection.execute(
            "SELECT fact_id FROM duty_facts").fetchone()["fact_id"]
        self.service.assign_reviewer(request_id="asg", actor_id="a1",
                                     event_id=self.event_id, reviewer_actor="rv2")
        self.asg_id = self.service.database.connection.execute(
            "SELECT assignment_id FROM review_assignments").fetchone()["assignment_id"]
        self.service.decide_fact(request_id="d1", actor_id="rv2", assignment_id=self.asg_id,
                                 fact_id=self.fact_id, decision="accepted", note="属实")
        self.service.complete_assignment(request_id="done", actor_id="rv2",
                                         assignment_id=self.asg_id)
        self.service.grant_consent(request_id="consent", actor_id="a1",
                                   candidate_id="cand-1")

    def tearDown(self):
        self.helper.close()

    def _confirm(self, actor, request_id="final", decision="selected"):
        return self.service.confirm_selection(request_id=request_id, actor_id=actor,
                                              event_id=self.event_id, decision=decision)

    def test_submitter_cannot_confirm_own_recommendation(self):
        with self.assertRaises(PermissionDenied):
            self._confirm("op1", "self")
        with self.assertRaises(PermissionDenied):
            self._confirm("rv2", "reviewer")
        self._confirm("a1", "ok")

    def test_rule_change_only_affects_new_batches(self):
        # 老批次按冻结时的一份来源要求已经入选
        self._confirm("a1", "final-b1")
        rule2 = self.service.freeze_rule(
            request_id="rule-v2", actor_id="a1",
            payload={"min_accepted_sources": 2}, note="更严格规则")
        self.service.create_batch(request_id="open-b2", actor_id="a1", batch_id="b2",
                                  name="新批次", rule_version_id=rule2.resource_id,
                                  submission_deadline="2026-10-10T00:00:00Z")
        receipt = self.service.submit_fact(request_id="b2-f1", actor_id="op1",
                                           **fact_kwargs(batch_id="b2"))
        new_event = receipt_response(self.service, "b2-f1")["event_id"]
        new_fact = self.service.database.connection.execute(
            "SELECT fact_id FROM duty_facts WHERE event_id=?", (new_event,)
        ).fetchone()["fact_id"]
        self.service.assign_reviewer(request_id="b2-asg", actor_id="a1",
                                     event_id=new_event, reviewer_actor="rv2")
        asg = self.service.database.connection.execute(
            "SELECT assignment_id FROM review_assignments WHERE event_id=?", (new_event,)
        ).fetchone()["assignment_id"]
        self.service.decide_fact(request_id="b2-dec", actor_id="rv2", assignment_id=asg,
                                 fact_id=new_fact, decision="accepted")
        self.service.complete_assignment(request_id="b2-done", actor_id="rv2", assignment_id=asg)
        self.service.grant_consent(request_id="b2-consent", actor_id="a1",
                                   candidate_id="cand-1")
        with self.assertRaises(ValidationError):
            self.service.confirm_selection(request_id="b2-final", actor_id="a1",
                                           event_id=new_event, decision="selected")
        trace = self.service.get_event_trace(actor_id="au1", event_id=self.event_id)
        self.assertEqual(1, trace["rule_snapshot"]["min_accepted_sources"])

    def test_revoked_consent_keeps_decision_but_hides_new_publication(self):
        self._confirm("a1", "final")
        self.service.publish(request_id="pub1", actor_id="a1", batch_id="b1", name="第一榜")
        before = self.service.list_publications("b1")[0]
        self.assertEqual("张三", before["entries"][0]["name"])

        self.service.revoke_consent(request_id="revoke", actor_id="a1", candidate_id="cand-1")
        self.service.publish(request_id="pub2", actor_id="a1", batch_id="b1", name="第二榜")
        publications = self.service.list_publications("b1")
        self.assertEqual(0, len(publications[1]["entries"]))

        trace = self.service.get_event_trace(actor_id="au1", event_id=self.event_id)
        self.assertEqual("selected", trace["final_decision"]["decision"])
        self.assertIsNotNone(trace["consent_ledger"][-1]["revoked_at"])
        self.assertEqual(1, len(trace["publication_links"]))

    def test_sensitive_fields_are_removed_from_public_projection(self):
        self.service.submit_fact(
            request_id="f2", actor_id="op1",
            **fact_kwargs(candidate_id="cand-2", candidate_name="李四",
                          duty_start="2026-10-02T20:00:00+08:00",
                          duty_end="2026-10-03T00:00:00+08:00", sensitive=True,
                          public_scope=["duty_start", "duty_end", "source_units"],
                          proofs=[{"kind": "roster", "reference": "r2",
                                   "issuer_organization_id": "o1"}]))
        event2 = receipt_response(self.service, "f2")["event_id"]
        fact2 = self.service.database.connection.execute(
            "SELECT fact_id FROM duty_facts WHERE event_id=?", (event2,)
        ).fetchone()["fact_id"]
        self.service.assign_reviewer(request_id="asg2", actor_id="a1", event_id=event2,
                                     reviewer_actor="rv2")
        asg2 = self.service.database.connection.execute(
            "SELECT assignment_id FROM review_assignments WHERE event_id=?", (event2,)
        ).fetchone()["assignment_id"]
        self.service.decide_fact(request_id="d2", actor_id="rv2", assignment_id=asg2,
                                 fact_id=fact2, decision="accepted")
        self.service.complete_assignment(request_id="done2", actor_id="rv2", assignment_id=asg2)
        self.service.grant_consent(request_id="consent2", actor_id="a1", candidate_id="cand-2")
        self.service.confirm_selection(request_id="final2", actor_id="a1", event_id=event2,
                                       decision="selected")
        self.service.publish(request_id="pub", actor_id="a1", batch_id="b1", name="光荣榜")
        entries = self.service.list_publications("b1")[0]["entries"]
        li = next(item for item in entries if item["name"] is None)
        self.assertNotIn("name", li["visible_fields"])
        self.assertNotIn("organization", li["visible_fields"])
        self.assertIn("duty_start", li["visible_fields"])

    def test_internal_trace_requires_admin_or_auditor_and_public_hides_basis(self):
        self._confirm("a1", "final")
        with self.assertRaises(PermissionDenied):
            self.service.get_event_trace(actor_id="rv2", event_id=self.event_id)
        trace = self.service.get_event_trace(actor_id="au1", event_id=self.event_id)
        self.assertEqual("accepted", trace["facts"][0]["status"])
        self.assertEqual("rv2", trace["final_decision"]["basis"]["reviewer_actor"])
        self.service.publish(request_id="pub", actor_id="a1", batch_id="b1", name="榜")
        public = self.service.list_publications("b1")[0]
        entry = public["entries"][0]
        self.assertNotIn("fact_id", entry)
        self.assertNotIn("proofs", entry)
        self.assertNotIn("submitter_actor", entry)

    def test_closed_batch_freezes_further_writes_but_keeps_decisions(self):
        self._confirm("a1", "final")
        rule2 = self.service.freeze_rule(request_id="rule-v2", actor_id="a1",
                                         payload={"min_proofs": 2}, note="更严格证明要求")
        with self.assertRaises(ConflictError):
            self.service.rebind_rule(request_id="rebind-final", actor_id="a1",
                                     batch_id="b1", rule_version_id=rule2.resource_id)
        self.service.close_batch(request_id="close", actor_id="a1", batch_id="b1")
        with self.assertRaises(ConflictError):
            self.service.rebind_rule(request_id="rebind-closed", actor_id="a1",
                                     batch_id="b1", rule_version_id=rule2.resource_id)
        with self.assertRaises(ConflictError):
            self.service.submit_fact(request_id="after", actor_id="op1",
                                     **fact_kwargs(duty_start="2026-10-04T08:00:00+08:00",
                                                   duty_end="2026-10-04T16:00:00+08:00"))
        trace = self.service.get_event_trace(actor_id="au1", event_id=self.event_id)
        self.assertEqual("selected", trace["final_decision"]["decision"])


if __name__ == "__main__":
    unittest.main()
