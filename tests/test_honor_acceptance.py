import unittest

from festival_honor.acceptance import run


class HonorAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["replay_replayed"])
        self.assertEqual(3, result["merged_fact_count"])
        self.assertEqual(["o1", "o2"], result["merged_sources"])
        self.assertIn("declared_interest", result["blocked_assignment_message"])
        self.assertTrue(result["self_confirm_blocked"])
        self.assertTrue(result["reviewer_confirm_blocked"])
        self.assertEqual("张坚守", result["zhang_public_name"])
        self.assertTrue(result["wang_has_no_name"])
        self.assertNotIn("name", result["wang_visible_fields"])
        self.assertEqual(2, result["first_publication_entries"])
        self.assertEqual(1, result["second_publication_entries"])
        self.assertTrue(result["zhang_absent_from_second"])
        self.assertEqual("selected", result["zhang_final_decision"])
        self.assertTrue(result["zhang_consent_revoked"])
        self.assertEqual(1, result["zhang_publication_links"])
        self.assertTrue(result["has_supplement_trace"])
        self.assertIn("accepted", result["excluded_fact_statuses"])
        self.assertIn("excluded", result["excluded_fact_statuses"])


if __name__ == "__main__":
    unittest.main()
