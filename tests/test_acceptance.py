import unittest

from festival_foundation.acceptance import run as run_foundation
from festival_foundation.honor_acceptance import run as run_honor


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run_foundation()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertEqual(1, result["records"])

    def test_honor_offline_acceptance(self):
        result = run_honor()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["merged"])
        self.assertEqual(0, result["public_entries_after_revoke"])
        self.assertTrue(result["internal_decision_preserved"])


if __name__ == "__main__":
    unittest.main()
