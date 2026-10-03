import unittest

from progress_payment.acceptance import run


class PaymentAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["balanced"])
        self.assertTrue(result["duplicate_blocked"])
        self.assertTrue(result["correction_in_new_period"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertEqual("paid", result["application_status"])
        self.assertEqual(20000000, result["contract_amount_cents"])
        self.assertEqual(3500000, result["paid_net_cents"])
        self.assertEqual(16500000, result["remaining_obligation_cents"])


if __name__ == "__main__":
    unittest.main()
