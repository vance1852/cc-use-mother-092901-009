import unittest
from datetime import datetime, timezone

from transport_coordination.clock import MutableClock
from transport_coordination.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from transport_coordination.payment_service import PaymentService
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


class PaymentServiceTest(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.database = Database()
        self.domain = DomainService(self.database, self.clock)
        self.service = PaymentService(self.domain)
        self.domain.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="交通集团")
        self.domain.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin1",
                                   display_name="管理员", role="admin", organization_id="o1")
        self.domain.register_actor(request_id="op", actor_id="admin1", new_actor_id="op1",
                                   display_name="经办", role="operator", organization_id="o1")
        self.domain.register_actor(request_id="rev", actor_id="admin1", new_actor_id="rev1",
                                   display_name="会签", role="reviewer", organization_id="o1")
        self.domain.register_actor(request_id="aud", actor_id="admin1", new_actor_id="aud1",
                                   display_name="审计", role="auditor", organization_id="o1")
        self.domain.register_site(request_id="site", actor_id="op1", site_id="s1",
                                  organization_id="o1", name="项目部", timezone_name="Asia/Shanghai")
        self._contract()

    def tearDown(self):
        self.database.close()

    def _contract(self, contract_id="c1", no="HT-1", items=None, funding=None, retention=300,
                  defect_end="2026-10-31", requires_cert=True):
        items = items or [
            {"item_code": "I-1", "name": "路基填筑", "unit": "m3",
             "unit_price_cents": 12000, "quantity_milli": 1000000},
            {"item_code": "I-2", "name": "沥青路面", "unit": "m2",
             "unit_price_cents": 30000, "quantity_milli": 2000000},
        ]
        funding = funding or [{"source_code": "central", "ratio_bp": 6000},
                              {"source_code": "local", "ratio_bp": 4000}]
        return self.service.register_contract(
            request_id=f"req-{contract_id}", actor_id="op1", contract_id=contract_id, site_id="s1",
            contract_no=no, name="绕城高速工程", contractor="总包一局", retention_rate_bp=retention,
            defect_liability_end=defect_end, requires_quality_certificate=requires_cert,
            funding_sources=funding, items=items)

    def _acceptance(self, acceptance_id, contract_id="c1", no=None, item="I-1", qty=400000,
                    independent=True):
        return self.service.register_acceptance(
            request_id=f"req-{acceptance_id}", actor_id="op1", acceptance_id=acceptance_id,
            contract_id=contract_id, acceptance_no=no or f"AC-{acceptance_id}", item_code=item,
            quantity_milli=qty, accepted_on="2026-09-20", inspector="监理甲",
            independent=independent)

    def _invoice(self, invoice_id, contract_id="c1", no=None, amount=6000000):
        return self.service.register_invoice(
            request_id=f"req-{invoice_id}", actor_id="op1", invoice_id=invoice_id,
            contract_id=contract_id, invoice_no=no or f"INV-{invoice_id}",
            amount_cents=amount, issued_on="2026-09-21")

    def _submit(self, application_id, lines, contract_id="c1", actor_id="op1"):
        return self.service.submit_application(
            request_id=f"req-{application_id}", actor_id=actor_id, application_id=application_id,
            contract_id=contract_id,
            lines=[{"acceptance_id": a, "invoice_id": i} for a, i in lines])

    def _paid_application(self, application_id="app1", acceptance_id="ac1", invoice_id="inv1",
                          qty=400000):
        self._acceptance(acceptance_id, qty=qty)
        self._invoice(invoice_id)
        self._submit(application_id, [(acceptance_id, invoice_id)])
        self.service.countersign_application(request_id=f"cs-{application_id}", actor_id="rev1",
                                             application_id=application_id)
        self.service.approve_application(request_id=f"ap-{application_id}", actor_id="rev1",
                                         application_id=application_id,
                                         approved_acceptance_ids=[acceptance_id])
        return self.service.record_payment(request_id=f"pay-{application_id}", actor_id="op1",
                                           application_id=application_id)

    def _occupations(self, application_id):
        return self.database.connection.execute(
            "SELECT * FROM evidence_occupations WHERE application_id=?", (application_id,)
        ).fetchall()

    # ------------------------------------------------------------------
    # 主数据
    # ------------------------------------------------------------------

    def test_register_contract_creates_first_versions_and_amount(self):
        contract = self.service.get_contract("c1")
        self.assertEqual(72000000, self.service.contract_balance("c1")["contract_amount_cents"])
        self.assertEqual(2, len(contract["items"]))
        self.assertEqual(1, contract["items"][0]["current_version"])
        self.assertEqual(1000000, contract["items"][0]["current_quantity_milli"])
        self.assertEqual([{"source_code": "central", "ratio_bp": 6000},
                          {"source_code": "local", "ratio_bp": 4000}], contract["funding_sources"])

    def test_funding_ratio_must_sum_to_10000(self):
        with self.assertRaises(ValidationError):
            self._contract(contract_id="c9", no="HT-9",
                           funding=[{"source_code": "central", "ratio_bp": 5000},
                                    {"source_code": "local", "ratio_bp": 4000}])

    def test_change_order_requires_funding_for_added_quantity(self):
        with self.assertRaises(ValidationError):
            self.service.register_change_order(
                request_id="co-x", actor_id="op1", change_id="co-x", contract_id="c1",
                change_no="BG-X", description="无出资比例的增量变更",
                lines=[{"item_code": "I-1", "quantity_delta_milli": 100000}])

    # ------------------------------------------------------------------
    # 申报：证据占用、累计量校验、资金拆分
    # ------------------------------------------------------------------

    def test_submit_occupies_evidence_and_splits_funding(self):
        self._acceptance("ac1")
        self._invoice("inv1")
        result = self._submit("app1", [("ac1", "inv1")])
        self.assertEqual("submitted", result["result"]["status"])
        self.assertEqual(4800000, result["result"]["claimed_amount_cents"])
        self.assertEqual({"central": 2880000, "local": 1920000}, result["result"]["funding_split"])
        occupations = self._occupations("app1")
        self.assertEqual(2, len(occupations))
        self.assertTrue(all(row["status"] == "active" for row in occupations))

    def test_submit_replay_is_idempotent(self):
        self._acceptance("ac1")
        self._invoice("inv1")
        first = self._submit("app1", [("ac1", "inv1")])
        second = self._submit("app1", [("ac1", "inv1")])
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["result"], second["result"])
        with self.assertRaises(ConflictError):
            self.service.submit_application(
                request_id="req-app1", actor_id="op1", application_id="app2",
                contract_id="c1", lines=[{"acceptance_id": "ac1", "invoice_id": "inv1"}])

    def test_duplicate_acceptance_goes_pending_without_occupation(self):
        self._acceptance("ac1")
        self._invoice("inv1")
        self._invoice("inv2")
        self._submit("app1", [("ac1", "inv1")])
        result = self._submit("app2", [("ac1", "inv2")])
        self.assertEqual("pending", result["result"]["status"])
        reasons = [item["reason"] for item in result["result"]["reasons"]]
        self.assertIn("duplicate_evidence", reasons)
        self.assertEqual(0, len(self._occupations("app2")))

    def test_same_acceptance_no_across_contracts_is_duplicate_material(self):
        self._acceptance("ac1", no="AC-1001")
        self._invoice("inv1")
        self._submit("app1", [("ac1", "inv1")])
        self._contract(contract_id="c2", no="HT-2",
                       items=[{"item_code": "S-1", "name": "预制梁", "unit": "片",
                               "unit_price_cents": 500000, "quantity_milli": 500000}],
                       funding=[{"source_code": "local", "ratio_bp": 10000}])
        self._acceptance("ac2", contract_id="c2", no="AC-1001", item="S-1", qty=100000)
        self._invoice("inv2", contract_id="c2")
        result = self._submit("app2", [("ac2", "inv2")], contract_id="c2")
        self.assertEqual("pending", result["result"]["status"])
        reasons = [item["reason"] for item in result["result"]["reasons"]]
        self.assertIn("duplicate_material", reasons)

    def test_non_independent_acceptance_goes_pending(self):
        self._acceptance("ac1", independent=False)
        self._invoice("inv1")
        result = self._submit("app1", [("ac1", "inv1")])
        self.assertEqual("pending", result["result"]["status"])
        reasons = [item["reason"] for item in result["result"]["reasons"]]
        self.assertIn("missing_independent_acceptance", reasons)

    def test_over_contract_quantity_goes_pending(self):
        self._acceptance("ac1", qty=1200000)
        self._invoice("inv1")
        result = self._submit("app1", [("ac1", "inv1")])
        self.assertEqual("pending", result["result"]["status"])
        reason = result["result"]["reasons"][0]
        self.assertEqual("exceeds_contract_quantity", reason["reason"])
        self.assertEqual(1000000, reason["available_milli"])

    def test_in_flight_quantity_blocks_second_application(self):
        self._acceptance("ac1", qty=600000)
        self._acceptance("ac2", qty=600000)
        self._invoice("inv1")
        self._invoice("inv2")
        self.assertEqual("submitted", self._submit("app1", [("ac1", "inv1")])["result"]["status"])
        result = self._submit("app2", [("ac2", "inv2")])
        self.assertEqual("pending", result["result"]["status"])
        self.assertEqual("exceeds_contract_quantity", result["result"]["reasons"][0]["reason"])

    def test_unknown_acceptance_is_rejected(self):
        self._invoice("inv1")
        with self.assertRaises(ValidationError):
            self._submit("app1", [("missing", "inv1")])

    # ------------------------------------------------------------------
    # 生命周期：撤回、驳回、部分批准
    # ------------------------------------------------------------------

    def test_withdraw_releases_evidence_for_resubmission(self):
        self._acceptance("ac1")
        self._invoice("inv1")
        self._submit("app1", [("ac1", "inv1")])
        self.service.withdraw_application(request_id="wd1", actor_id="op1", application_id="app1")
        self.assertTrue(all(row["status"] == "released" for row in self._occupations("app1")))
        self.assertEqual("submitted", self._submit("app2", [("ac1", "inv1")])["result"]["status"])

    def test_reject_releases_evidence(self):
        self._acceptance("ac1")
        self._invoice("inv1")
        self._submit("app1", [("ac1", "inv1")])
        self.service.reject_application(request_id="rj1", actor_id="rev1",
                                        application_id="app1", reason="发票金额不符")
        self.assertEqual("submitted", self._submit("app2", [("ac1", "inv1")])["result"]["status"])

    def test_partial_approval_releases_unapproved_lines(self):
        self._acceptance("ac1", qty=300000)
        self._acceptance("ac2", qty=200000)
        self._invoice("inv1")
        self._invoice("inv2")
        self._submit("app1", [("ac1", "inv1"), ("ac2", "inv2")])
        self.service.countersign_application(request_id="cs1", actor_id="rev1", application_id="app1")
        result = self.service.approve_application(request_id="ap1", actor_id="rev1",
                                                  application_id="app1",
                                                  approved_acceptance_ids=["ac1"])
        self.assertEqual(3600000, result["result"]["approved_amount_cents"])
        self.assertEqual("submitted", self._submit("app2", [("ac2", "inv2")])["result"]["status"])

    def test_approval_rechecks_cumulative_quantity(self):
        self._acceptance("ac1", qty=600000)
        self._invoice("inv1")
        self._submit("app1", [("ac1", "inv1")])
        self.service.countersign_application(request_id="cs1", actor_id="rev1", application_id="app1")
        self.service.register_change_order(
            request_id="co1", actor_id="op1", change_id="co1", contract_id="c1",
            change_no="BG-1", description="核减工程量",
            lines=[{"item_code": "I-1", "quantity_delta_milli": -500000}])
        self.service.approve_change_order(request_id="co1a", actor_id="rev1", change_id="co1")
        result = self.service.approve_application(request_id="ap1", actor_id="rev1",
                                                  application_id="app1",
                                                  approved_acceptance_ids=["ac1"])
        self.assertEqual("pending", result["result"]["status"])
        self.assertEqual("exceeds_contract_quantity", result["result"]["reasons"][0]["reason"])

    def test_countersign_requires_submitted_status(self):
        self._acceptance("ac1")
        self._invoice("inv1")
        self._submit("app1", [("ac1", "inv1")])
        self.service.countersign_application(request_id="cs1", actor_id="rev1", application_id="app1")
        with self.assertRaises(ConflictError):
            self.service.countersign_application(request_id="cs2", actor_id="rev1",
                                                 application_id="app1")

    # ------------------------------------------------------------------
    # 变更令与工程量版本
    # ------------------------------------------------------------------

    def test_change_order_creates_version_and_cross_funding_split(self):
        self._paid_application(application_id="app1", acceptance_id="ac1",
                               invoice_id="inv1", qty=600000)
        self.service.register_change_order(
            request_id="co1", actor_id="op1", change_id="co1", contract_id="c1",
            change_no="BG-1", description="软基处理范围扩大",
            lines=[{"item_code": "I-1", "quantity_delta_milli": 200000}],
            funding_sources=[{"source_code": "central", "ratio_bp": 2500},
                             {"source_code": "local", "ratio_bp": 7500}])
        self.service.approve_change_order(request_id="co1a", actor_id="rev1", change_id="co1")
        contract = self.service.get_contract("c1")
        item = next(row for row in contract["items"] if row["item_code"] == "I-1")
        self.assertEqual(2, item["current_version"])
        self.assertEqual(1200000, item["current_quantity_milli"])
        self._acceptance("ac2", qty=500000)
        self._invoice("inv2")
        result = self._submit("app2", [("ac2", "inv2")])
        self.assertEqual({"central": 3180000, "local": 2820000}, result["result"]["funding_split"])

    def test_negative_change_order_reduces_quantity(self):
        self.service.register_change_order(
            request_id="co1", actor_id="op1", change_id="co1", contract_id="c1",
            change_no="BG-1", description="核减工程量",
            lines=[{"item_code": "I-1", "quantity_delta_milli": -100000}])
        self.service.approve_change_order(request_id="co1a", actor_id="rev1", change_id="co1")
        item = next(row for row in self.service.get_contract("c1")["items"]
                    if row["item_code"] == "I-1")
        self.assertEqual(900000, item["current_quantity_milli"])

    def test_change_order_cannot_reduce_below_consumed(self):
        self._paid_application(application_id="app1", acceptance_id="ac1",
                               invoice_id="inv1", qty=950000)
        self.service.register_change_order(
            request_id="co1", actor_id="op1", change_id="co1", contract_id="c1",
            change_no="BG-1", description="核减工程量",
            lines=[{"item_code": "I-1", "quantity_delta_milli": -100000}])
        with self.assertRaises(ConflictError):
            self.service.approve_change_order(request_id="co1a", actor_id="rev1", change_id="co1")

    # ------------------------------------------------------------------
    # 支付、质保金、追回
    # ------------------------------------------------------------------

    def test_payment_splits_net_and_retention(self):
        result = self._paid_application()
        self.assertEqual(4656000, result["result"]["paid_amount_cents"])
        self.assertEqual(144000, result["result"]["retention_withheld_cents"])
        self.assertEqual({"central": 2793600, "local": 1862400}, result["result"]["payment_split"])
        self.assertEqual({"central": 86400, "local": 57600}, result["result"]["retention_split"])

    def test_retention_release_blocked_before_conditions(self):
        self._paid_application()
        with self.assertRaises(ConflictError):
            self.service.release_retention(request_id="rel1", actor_id="op1",
                                           contract_id="c1", amount_cents=100000)
        self.clock.set(datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc))
        with self.assertRaises(ConflictError):
            self.service.release_retention(request_id="rel2", actor_id="op1",
                                           contract_id="c1", amount_cents=100000)

    def test_retention_release_after_conditions(self):
        self._paid_application()
        self.clock.set(datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc))
        self.service.register_quality_certificate(request_id="qc1", actor_id="op1",
                                                  certificate_id="qc1", contract_id="c1",
                                                  certificate_no="ZM-1", issued_on="2026-10-20")
        result = self.service.release_retention(request_id="rel1", actor_id="op1",
                                                contract_id="c1", amount_cents=100000)
        self.assertEqual(44000, result["result"]["retention_held_cents"])
        self.assertEqual({"central": 60000, "local": 40000}, result["result"]["funding_split"])
        with self.assertRaises(ConflictError):
            self.service.release_retention(request_id="rel2", actor_id="op1",
                                           contract_id="c1", amount_cents=50000)

    def test_recovery_is_append_only_and_limited(self):
        self._paid_application()
        result = self.service.recover_payment(request_id="rec1", actor_id="aud1",
                                              application_id="app1", amount_cents=200000,
                                              reason="重复材料")
        self.assertEqual({"central": -120000, "local": -80000}, result["result"]["funding_split"])
        entries = self.service.explain_application("app1")["entries"]
        self.assertEqual(6, len(entries))
        self.assertEqual(-200000, entries[-1]["amount_cents"])
        with self.assertRaises(ConflictError):
            self.service.recover_payment(request_id="rec2", actor_id="aud1",
                                         application_id="app1", amount_cents=5000000,
                                         reason="超额追回")

    def test_recovery_requires_paid_application(self):
        self._acceptance("ac1")
        self._invoice("inv1")
        self._submit("app1", [("ac1", "inv1")])
        with self.assertRaises(ConflictError):
            self.service.recover_payment(request_id="rec1", actor_id="aud1",
                                         application_id="app1", amount_cents=1000, reason="测试")

    # ------------------------------------------------------------------
    # 关账与更正
    # ------------------------------------------------------------------

    def test_correction_after_close_lands_in_current_period(self):
        self._paid_application()
        self.clock.set(datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc))
        self.service.close_period(request_id="close1", actor_id="admin1", period="2026-09")
        self.service.close_period(request_id="close2", actor_id="admin1", period="2026-10")
        ledger = self.service.contract_ledger("c1")
        payment_entry = next(row for row in ledger if row["entry_type"] == "payment.recorded")
        result = self.service.correct_entry(request_id="fix1", actor_id="op1",
                                            entry_id=payment_entry["entry_id"],
                                            amount_cents=-50000, reason="审计核减")
        self.assertEqual("2026-11", result["result"]["correction_period"])
        self.assertEqual("2026-09", result["result"]["original_period"])
        after = self.service.contract_ledger("c1")
        original = next(row for row in after if row["entry_id"] == payment_entry["entry_id"])
        self.assertEqual(payment_entry["amount_cents"], original["amount_cents"])
        self.assertEqual("2026-09", original["period"])
        correction = next(row for row in after if row["entry_type"] == "correction")
        self.assertEqual({"central": -30000, "local": -20000}, correction["funding_split"])

    def test_closed_period_rejects_new_entries(self):
        self.service.close_period(request_id="close1", actor_id="admin1", period="2026-09")
        self._acceptance("ac1")
        self._invoice("inv1")
        with self.assertRaises(ConflictError):
            self._submit("app1", [("ac1", "inv1")])
        self.clock.set(datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc))
        self.assertEqual("submitted", self._submit("app1", [("ac1", "inv1")])["result"]["status"])

    def test_close_period_validations(self):
        with self.assertRaises(ValidationError):
            self.service.close_period(request_id="close1", actor_id="admin1", period="2027-01")
        with self.assertRaises(ValidationError):
            self.service.close_period(request_id="close2", actor_id="admin1", period="2026-9")
        self.service.close_period(request_id="close3", actor_id="admin1", period="2026-09")
        with self.assertRaises(ConflictError):
            self.service.close_period(request_id="close4", actor_id="admin1", period="2026-09")

    # ------------------------------------------------------------------
    # 解释、反查与平衡
    # ------------------------------------------------------------------

    def test_explain_application_shows_full_timeline(self):
        self._paid_application()
        explained = self.service.explain_application("app1")
        types = [entry["entry_type"] for entry in explained["entries"]]
        self.assertEqual(["application.submitted", "application.countersigned",
                          "application.approved", "payment.recorded", "retention.withheld"], types)
        self.assertEqual(4656000, explained["summary"]["paid_amount_cents"])
        self.assertEqual(144000, explained["summary"]["retention_withheld_cents"])
        self.assertEqual(2, len(explained["occupations"]))

    def test_trace_evidence_lists_all_occupations(self):
        self._paid_application()
        traced = self.service.trace_evidence("acceptance", "ac1")
        self.assertEqual(1, len(traced["occupations"]))
        self.assertEqual("app1", traced["occupations"][0]["application_id"])
        self.assertEqual("paid", traced["occupations"][0]["status"])
        with self.assertRaises(NotFoundError):
            self.service.trace_evidence("acceptance", "missing")
        with self.assertRaises(ValidationError):
            self.service.trace_evidence("contract", "ac1")

    def test_balance_components_stay_balanced(self):
        self._paid_application()
        self.clock.set(datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc))
        self.service.register_quality_certificate(request_id="qc1", actor_id="op1",
                                                  certificate_id="qc1", contract_id="c1",
                                                  certificate_no="ZM-1", issued_on="2026-10-20")
        self.service.release_retention(request_id="rel1", actor_id="op1",
                                       contract_id="c1", amount_cents=100000)
        self.service.recover_payment(request_id="rec1", actor_id="aud1",
                                     application_id="app1", amount_cents=200000, reason="追回")
        balance = self.service.contract_balance("c1")
        self.assertEqual(72000000, balance["contract_amount_cents"])
        self.assertEqual(4800000, balance["approved_amount_cents"])
        self.assertEqual(4556000, balance["cash_paid_cents"])
        self.assertEqual(44000, balance["retention_held_cents"])
        self.assertEqual(200000, balance["recovered_amount_cents"])
        self.assertEqual(72000000 - 4556000 - 44000, balance["remaining_obligation_cents"])
        self.assertTrue(balance["balanced"])
        self.assertTrue(all(balance["checks"].values()))
        central = next(row for row in balance["funding_sources"] if row["source_code"] == "central")
        self.assertEqual(2880000, central["approved_cents"])

    # ------------------------------------------------------------------
    # 权限
    # ------------------------------------------------------------------

    def test_role_permissions(self):
        self._acceptance("ac1")
        self._invoice("inv1")
        with self.assertRaises(PermissionDenied):
            self._submit("app1", [("ac1", "inv1")], actor_id="aud1")
        self._submit("app1", [("ac1", "inv1")])
        with self.assertRaises(PermissionDenied):
            self.service.countersign_application(request_id="cs1", actor_id="op1",
                                                 application_id="app1")
        with self.assertRaises(PermissionDenied):
            self.service.close_period(request_id="close1", actor_id="op1", period="2026-09")
        with self.assertRaises(PermissionDenied):
            self.service.recover_payment(request_id="rec1", actor_id="op1",
                                         application_id="app1", amount_cents=100, reason="越权")

    def test_cross_organization_access_denied(self):
        self.domain.register_organization(request_id="org2", actor_id="admin1",
                                          organization_id="o2", name="其他集团")
        self.domain.register_actor(request_id="op2", actor_id="admin1", new_actor_id="op2",
                                   display_name="外部经办", role="operator", organization_id="o2")
        self._acceptance("ac1")
        self._invoice("inv1")
        with self.assertRaises(PermissionDenied):
            self._submit("app1", [("ac1", "inv1")], actor_id="op2")


if __name__ == "__main__":
    unittest.main()
