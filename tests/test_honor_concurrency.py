"""并发核验、重复提交与迟到证明的稳定性测试。"""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from festival_foundation.clock import FixedClock
from festival_foundation.errors import ConflictError, PermissionDenied
from festival_foundation.honor_service import HonorService
from festival_foundation.honor_storage import HonorDatabase
from festival_foundation.service import DomainService

START = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.database = HonorDatabase()
        self.clock = FixedClock(START)
        self.foundation = DomainService(self.database, self.clock)
        self.honor = HonorService(self.database, self.clock)
        f, h = self.foundation, self.honor
        f.register_organization(request_id="org", actor_id="bootstrap",
                                organization_id="o1", name="联合工作组")
        f.register_actor(request_id="r-adm", actor_id="bootstrap", new_actor_id="adm",
                         display_name="adm", role="admin", organization_id="o1")
        for rid, aid, role in [("r-opa", "opA", "operator"),
                               ("r-opb", "opB", "operator"), ("r-rvx", "rvX", "reviewer"),
                               ("r-rvy", "rvY", "reviewer"), ("r-aud", "aud1", "auditor")]:
            f.register_actor(request_id=rid, actor_id="adm", new_actor_id=aid,
                             display_name=aid, role=role, organization_id="o1")
        h.publish_rules(request_id="rules", actor_id="adm",
                        params={"min_independent_sources": 2, "verify_hours": 48})
        h.create_batch(request_id="b1", actor_id="adm", batch_id="b1", title="公示")
        h.grant_consent(request_id="c", actor_id="adm", candidate_id="zhang",
                        scope=["candidate_name", "organization", "event_date", "deed_summary"])

    def tearDown(self):
        self.database.close()

    def _nom(self, request_id, actor, org, stype="duty_fact", content=None):
        return self.honor.submit_nomination(
            request_id=request_id, actor_id=actor, batch_id="b1",
            candidate_id="zhang", candidate_name="张坚守", organization="联合工作组",
            department="值班处", post_name="值班长", event_date="2026-10-01",
            event_location="一号点", deed_summary="连续值守 24 小时",
            facts=[{"source_org": org, "source_type": stype,
                    "content": content or {"shift": "24h"}}])

    def test_concurrent_identical_nominations_dedup_to_one_fact(self):
        barrier = threading.Barrier(8)

        def submit(i: int):
            barrier.wait()
            return self._nom(f"dup-{i}", "opA", "甲部门")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, range(8)))
        nomination_ids = {r["nomination_id"] for r in results}
        self.assertEqual(1, len(nomination_ids))
        trace = self.honor.nomination_trace(actor_id="aud1",
                                           nomination_id=results[0]["nomination_id"])
        self.assertEqual(1, len(trace["facts"]))

    def test_concurrent_verifier_assignments_only_one_active(self):
        self._nom("n1", "opA", "甲部门")
        self._nom("n2", "opB", "乙部门", "collab_proof", {"ok": 1})
        trace = self.honor.nomination_trace(
            actor_id="aud1",
            nomination_id=self.honor.batch_detail(actor_id="adm", batch_id="b1")
            ["nominations"][0]["nomination_id"])
        nomination_id = trace["nomination_id"]
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def assign(verifier: str, request_id: str):
            barrier.wait()
            try:
                self.honor.assign_verifier(request_id=request_id, actor_id="adm",
                                           nomination_id=nomination_id, verifier_id=verifier)
                outcomes.append("assigned")
            except (ConflictError, PermissionDenied):
                outcomes.append("rejected")

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(assign, ["rvX", "rvY"], ["ax", "ay"]))
        self.assertEqual(1, outcomes.count("assigned"))
        self.assertEqual(1, outcomes.count("rejected"))
        trace = self.honor.nomination_trace(actor_id="aud1", nomination_id=nomination_id)
        active = [v for v in trace["verifications"] if v["status"] == "active"]
        self.assertEqual(1, len(active))

    def test_concurrent_decisions_on_same_fact_resolve_once(self):
        self._nom("n1", "opA", "甲部门")
        self._nom("n2", "opB", "乙部门", "collab_proof", {"ok": 1})
        nomination_id = self.honor.batch_detail(actor_id="adm", batch_id="b1") \
            ["nominations"][0]["nomination_id"]
        self.honor.assign_verifier(request_id="a", actor_id="adm",
                                   nomination_id=nomination_id, verifier_id="rvY")
        fact_id = self.honor.nomination_trace(actor_id="aud1",
                                              nomination_id=nomination_id)["facts"][0]["fact_id"]
        barrier = threading.Barrier(4)

        def decide(i: int):
            barrier.wait()
            return self.honor.decide_fact(actor_id="rvY", fact_id=fact_id, outcome="accepted")

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(decide, range(4)))
        decision_ids = {r["decision_id"] for r in results}
        self.assertEqual(1, len(decision_ids))
        self.assertEqual(3, sum(1 for r in results if r["replayed"]))
        trace = self.honor.nomination_trace(actor_id="aud1", nomination_id=nomination_id)
        decisions = [d for f in trace["facts"] if f["fact_id"] == fact_id for d in f["decisions"]]
        self.assertEqual(1, len(decisions))

    def test_conflicting_concurrent_decisions_leave_one_winner(self):
        self._nom("n1", "opA", "甲部门")
        nomination_id = self.honor.batch_detail(actor_id="adm", batch_id="b1") \
            ["nominations"][0]["nomination_id"]
        self.honor.assign_verifier(request_id="a", actor_id="adm",
                                   nomination_id=nomination_id, verifier_id="rvY")
        fact_id = self.honor.nomination_trace(actor_id="aud1",
                                              nomination_id=nomination_id)["facts"][0]["fact_id"]
        barrier = threading.Barrier(2)

        def decide(outcome: str):
            barrier.wait()
            try:
                return self.honor.decide_fact(actor_id="rvY", fact_id=fact_id,
                                              outcome=outcome,
                                              reason="证据不足" if outcome == "excluded" else "")
            except ConflictError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(decide, ["accepted", "excluded"]))
        winners = [r for r in results if r is not None]
        self.assertEqual(1, len(winners))
        status = self.honor.nomination_trace(actor_id="aud1",
                                             nomination_id=nomination_id)["facts"][0]
        self.assertEqual(winners[0]["outcome"], status["verification_status"])

    def test_late_supplement_never_reopens_deadline(self):
        self._nom("n1", "opA", "甲部门")
        nomination_id = self.honor.batch_detail(actor_id="adm", batch_id="b1") \
            ["nominations"][0]["nomination_id"]
        self.honor.assign_verifier(request_id="a", actor_id="adm",
                                   nomination_id=nomination_id, verifier_id="rvY")
        fact_id = self.honor.nomination_trace(actor_id="aud1",
                                              nomination_id=nomination_id)["facts"][0]["fact_id"]
        self.honor.decide_fact(actor_id="rvY", fact_id=fact_id,
                               outcome="supplement_requested", reason="缺交接班记录")
        self.clock._value = START + timedelta(hours=49)
        late = self.honor.submit_supplement(request_id="late", actor_id="opA",
                                            fact_id=fact_id, content={"handover": "A-1"})
        self.assertTrue(late["late"])
        # 迟到后仍不能核验：分派已过期。
        with self.assertRaises(ConflictError):
            self.honor.decide_fact(actor_id="rvY", fact_id=fact_id, outcome="accepted")
        trace = self.honor.nomination_trace(actor_id="aud1", nomination_id=nomination_id)
        self.assertEqual("supplement_requested", trace["facts"][0]["verification_status"])
        self.assertTrue(trace["facts"][0]["supplements"][0]["late"])


if __name__ == "__main__":
    unittest.main()
