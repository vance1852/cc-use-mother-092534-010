"""荣誉后台 HTTP 路由测试。"""

import unittest

from festival_foundation.errors import PermissionDenied

from festival_honor.api import route
from festival_honor.clock import MutableClock
from festival_honor.service import HonorService
from festival_honor.storage import HonorDatabase

from datetime import datetime, timezone
import json


class HonorApiTest(unittest.TestCase):
    def setUp(self):
        self.database = HonorDatabase()
        self.service = HonorService(
            self.database, MutableClock(datetime(2026, 10, 1, tzinfo=timezone.utc)))
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="单位")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                    display_name="操作员", role="operator", organization_id="o1")

    def tearDown(self):
        self.database.close()

    def test_health_still_served_via_foundation_fallback(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_freeze_rule_route_rejects_non_admin(self):
        status, payload = route(self.service, "POST", "/rules",
                                {"request_id": "r1", "payload": {}},
                                {"X-Actor-Id": "op1"})
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_full_chain_over_http_routes(self):
        status, payload = route(self.service, "POST", "/rules",
                                {"request_id": "r1", "payload": None, "note": "规则"},
                                {"X-Actor-Id": "a1"})
        self.assertEqual(201, status)
        rule_id = payload["resource_id"]
        status, payload = route(self.service, "POST", "/batches",
                                {"request_id": "b1", "batch_id": "b1", "name": "批次",
                                 "rule_version_id": rule_id,
                                 "submission_deadline": "2026-10-03T00:00:00Z"},
                                {"X-Actor-Id": "a1"})
        self.assertEqual(201, status)
        # 幂等回放返回 200
        status, payload = route(self.service, "POST", "/batches",
                                {"request_id": "b1", "batch_id": "b1", "name": "批次",
                                 "rule_version_id": rule_id,
                                 "submission_deadline": "2026-10-03T00:00:00Z"},
                                {"X-Actor-Id": "a1"})
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])

    def test_publications_require_batch_id(self):
        status, payload = route(self.service, "GET", "/publications", None,
                                {"X-Actor-Id": "a1"})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_internal_event_missing_is_404(self):
        status, payload = route(self.service, "GET", "/internal/events?event_id=nope", None,
                                {"X-Actor-Id": "a1"})
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
