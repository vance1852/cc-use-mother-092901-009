import unittest

from transport_coordination.payment_acceptance import run


class PaymentAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["balanced"])
        self.assertTrue(result["early_release_blocked"])
        self.assertTrue(result["original_entry_untouched"])
        self.assertEqual("pending", result["duplicate_status"])
        self.assertIn("duplicate_material", result["duplicate_reasons"])
        self.assertEqual({"central": 2880000, "local": 1920000}, result["submitted_split"])
        self.assertEqual({"central": 4620000, "local": 3780000}, result["crossed_split"])
        self.assertEqual(4656000, result["paid_amount_cents"])
        self.assertEqual(144000, result["retention_withheld_cents"])
        self.assertEqual("2026-11", result["correction_period"])
        self.assertEqual("2026-09", result["original_period"])
        balance = result["balance"]
        self.assertEqual(74400000, balance["contract_amount_cents"])
        self.assertEqual(4800000, balance["approved_amount_cents"])
        self.assertEqual(4506000, balance["cash_paid_cents"])
        self.assertEqual(44000, balance["retention_held_cents"])
        self.assertEqual(69850000, balance["remaining_obligation_cents"])
        self.assertEqual(
            balance["contract_amount_cents"] - balance["cash_paid_cents"]
            - balance["retention_held_cents"],
            balance["remaining_obligation_cents"])


if __name__ == "__main__":
    unittest.main()
