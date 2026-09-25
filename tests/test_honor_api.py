import unittest

from festival_foundation.honor_api import route_combined
from festival_foundation.honor_service import HonorService
from festival_foundation.honor_storage import HonorDatabase
from festival_foundation.service import DomainService


class HonorApiTest(unittest.TestCase):
    def setUp(self):
        self.database = HonorDatabase()
        self.foundation = DomainService(self.database)
        self.honor = HonorService(self.database)
        self.foundation.register_organization(request_id="org", actor_id="bootstrap",
                                              organization_id="o1", name="联合工作组")
        self.foundation.register_actor(request_id="adm", actor_id="bootstrap",
                                       new_actor_id="adm", display_name="管理员",
                                       role="admin", organization_id="o1")
        self.foundation.register_actor(request_id="opa", actor_id="adm", new_actor_id="opA",
                                       display_name="甲推荐人", role="operator",
                                       organization_id="o1")
        self.foundation.register_actor(request_id="rvy", actor_id="adm", new_actor_id="rvY",
                                       display_name="核验人", role="reviewer",
                                       organization_id="o1")
        self.honor.publish_rules(request_id="rules", actor_id="adm")
        self.honor.create_batch(request_id="b1", actor_id="adm", batch_id="b1", title="公示")
        self.honor.grant_consent(request_id="c1", actor_id="adm", candidate_id="zhang",
                                 scope=["candidate_name", "organization", "event_date",
                                        "deed_summary"])
        self.honor.submit_nomination(
            request_id="n1", actor_id="opA", batch_id="b1", candidate_id="zhang",
            candidate_name="张坚守", organization="联合工作组", department="值班处",
            post_name="值班长", event_date="2026-10-01", event_location="一号点",
            deed_summary="连续值守", sensitive=True,
            facts=[{"source_org": "甲部门", "source_type": "duty_fact", "content": {"h": 24}}])
        self.honor.submit_nomination(
            request_id="n2", actor_id="opA", batch_id="b1", candidate_id="zhang",
            candidate_name="张坚守", organization="联合工作组", department="值班处",
            post_name="值班长", event_date="2026-10-01", event_location="一号点",
            deed_summary="协作证明", sensitive=True,
            facts=[{"source_org": "乙部门", "source_type": "collab_proof", "content": {"ok": 1}}])

    def tearDown(self):
        self.database.close()

    def _route(self, method, path, body=None, actor=""):
        return route_combined(self.foundation, self.honor, method, path, body or {},
                              {"X-Actor-Id": actor})

    def test_public_honors_not_available_before_publication(self):
        status, payload = self._route("GET", "/public/honors?batch_id=b1")
        self.assertEqual(404, status)

    def test_internal_nomination_requires_privileged_role(self):
        status, payload = self._route("GET", "/internal/nominations?nomination_id=x",
                                      actor="opA")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_write_route_rejects_unknown_field_with_400(self):
        status, payload = self._route("POST", "/honor/batches",
                                      {"request_id": "x", "actor_id_typo": "adm"}, actor="adm")
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_full_flow_through_http_and_public_projection(self):
        status, _ = self._route("POST", "/verifications/assign",
                                {"request_id": "a1", "nomination_id": self._nomination_id(),
                                 "verifier_id": "rvY"}, actor="adm")
        self.assertEqual(201, status)
        for fact_id in self._fact_ids():
            status, payload = self._route(
                "POST", "/facts/decide",
                {"fact_id": fact_id, "outcome": "accepted"}, actor="rvY")
            self.assertEqual(201, status)
        status, _ = self._route("POST", "/confirmations",
                                {"request_id": "cf", "nomination_id": self._nomination_id(),
                                 "outcome": "selected", "reason": "双来源采信"}, actor="adm")
        self.assertEqual(201, status)
        self.assertEqual(201, self._route("POST", "/honor/batches/finalize",
                                          {"request_id": "fn", "batch_id": "b1"}, actor="adm")[0])
        self.assertEqual(201, self._route("POST", "/honor/batches/publish",
                                          {"request_id": "pb", "batch_id": "b1"}, actor="adm")[0])
        status, payload = self._route("GET", "/public/honors?batch_id=b1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        # 敏感遮罩：post_name/event_location/department 不输出。
        self.assertEqual(
            {"candidate_name", "organization", "event_date", "deed_summary"},
            set(payload["items"][0].keys()))
        # 公开接口无需身份，也不暴露内部字段。
        self.assertNotIn("facts", payload["items"][0])

    def _nomination_id(self):
        status, payload = self._route("GET", "/internal/batches?batch_id=b1", actor="adm")
        self.assertEqual(200, status)
        return payload["nominations"][0]["nomination_id"]

    def _fact_ids(self):
        status, payload = self._route(
            "GET", f"/internal/nominations?nomination_id={self._nomination_id()}", actor="adm")
        self.assertEqual(200, status)
        return [f["fact_id"] for f in payload["facts"]]


if __name__ == "__main__":
    unittest.main()
