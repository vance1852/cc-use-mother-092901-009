import unittest
from datetime import datetime, timezone

from progress_payment.clock import FixedClock
from progress_payment.errors import ConflictError, PermissionDenied, ValidationError
from progress_payment.service import PaymentService
from progress_payment.storage import Database


class PaymentServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = PaymentService(
            self.database, FixedClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)))
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="交通投资集团")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="ad1",
                                    display_name="管理员", role="admin", organization_id="o1")
        for req, new_id, role in [("applicant", "ap1", "applicant"), ("inspector", "in1", "inspector"),
                                  ("reviewer", "rv1", "reviewer"), ("finance", "fi1", "finance"),
                                  ("auditor", "au1", "auditor")]:
            self.service.register_actor(request_id=req, actor_id="ad1", new_actor_id=new_id,
                                        display_name=new_id, role=role, organization_id="o1")
        self.service.create_project(request_id="proj", actor_id="ad1", project_id="p1",
                                    organization_id="o1", code="GS-001", name="高架一期工程",
                                    funding_sources=[
                                        {"code": "central", "name": "中央补助", "ratio_bp": 6000},
                                        {"code": "local", "name": "地方配套", "ratio_bp": 4000},
                                    ])
        self.service.create_contract(request_id="ctr", actor_id="ad1", contract_id="c1",
                                     project_id="p1", code="HT-001", name="路基路面合同",
                                     contractor_name="第一工程局", retention_rate_bp=300,
                                     retention_release_after="2026-09-01",
                                     boq_items=[
                                         {"item_code": "A-101", "name": "路基填方", "unit": "m3",
                                          "unit_price_cents": 100000, "quantity_milli": 100000},
                                         {"item_code": "A-102", "name": "沥青面层", "unit": "m2",
                                          "unit_price_cents": 50000, "quantity_milli": 200000},
                                     ])
        rows = self.database.connection.execute(
            "SELECT item_id, item_code FROM boq_items WHERE contract_id='c1'").fetchall()
        self.item_a = next(r["item_id"] for r in rows if r["item_code"] == "A-101")
        self.item_b = next(r["item_id"] for r in rows if r["item_code"] == "A-102")
        self.sources = {r["code"]: r["funding_source_id"] for r in self.database.connection.execute(
            "SELECT funding_source_id, code FROM funding_sources WHERE project_id='p1'")}

    def tearDown(self):
        self.database.close()

    def _evidence(self, request_id, actor_id, evidence_type, key, **kwargs):
        receipt = self.service.register_evidence(
            request_id=request_id, actor_id=actor_id, project_id="p1",
            evidence_type=evidence_type, external_key=key, **kwargs)
        return receipt.resource_id

    def _independent_acceptance(self, request_id="ev-site", key="YS-001"):
        return self._evidence(request_id, "in1", "site_acceptance", key, quantity_milli=100000)

    def _submit(self, request_id="app-1", quantity=40000, contract_id="c1", **overrides):
        site = overrides.pop("site_evidence", None) or self._independent_acceptance(
            f"{request_id}-site", f"YS-{request_id}")
        lines = overrides.pop("lines", None) or [
            {"item_id": self.item_a, "quantity_milli": quantity, "evidence_ids": [site]}]
        return self.service.submit_payment_application(
            request_id=request_id, actor_id="ap1", contract_id=contract_id,
            lines=lines, invoice_ids=overrides.pop("invoice_ids", []))

    def _approved(self, request_id="app-1", quantity=40000, **kwargs):
        result = self._submit(request_id=request_id, quantity=quantity, **kwargs)
        assert result["status"] == "submitted", result
        self.service.review_application(request_id=f"{request_id}-review", actor_id="rv1",
                                        application_id=result["application_id"], decision="approve")
        return result["application_id"]

    # ------------------------------------------------------------------
    # 建档与权限
    # ------------------------------------------------------------------

    def test_funding_sources_must_sum_to_full_basis(self):
        with self.assertRaises(ValidationError):
            self.service.create_project(request_id="bad-proj", actor_id="ad1", project_id="p2",
                                        organization_id="o1", code="GS-002", name="比例错误",
                                        funding_sources=[
                                            {"code": "a", "name": "甲", "ratio_bp": 6000},
                                            {"code": "b", "name": "乙", "ratio_bp": 3000},
                                        ])

    def test_applicant_cannot_review(self):
        app_id = self._submit()["application_id"]
        with self.assertRaises(PermissionDenied):
            self.service.review_application(request_id="rv-x", actor_id="ap1",
                                            application_id=app_id, decision="approve")

    def test_auditor_cannot_submit_application(self):
        site = self._independent_acceptance("ev-a", "YS-A")
        with self.assertRaises(PermissionDenied):
            self.service.submit_payment_application(
                request_id="app-x", actor_id="au1", contract_id="c1",
                lines=[{"item_id": self.item_a, "quantity_milli": 1000, "evidence_ids": [site]}])

    # ------------------------------------------------------------------
    # 申报事务：证据占用、累计量校验、资金份额拆分
    # ------------------------------------------------------------------

    def test_submit_occupies_evidence_and_splits_funding(self):
        result = self._submit(quantity=40000)
        self.assertEqual("submitted", result["status"])
        # 40 单位 × 100000 分 = 4,000,000 分；质保金 3% = 120,000；净额 3,880,000
        self.assertEqual(4000000, result["gross_cents"])
        self.assertEqual(120000, result["retention_cents"])
        self.assertEqual(3880000, result["net_cents"])
        self.assertEqual({self.sources["central"]: 2328000, self.sources["local"]: 1552000},
                         result["planned_funding"])
        detail = self.service.application_detail(result["application_id"])
        self.assertEqual(1, detail["quantity_version_no"])
        self.assertEqual("P-0001", detail["period"]["name"])

    def test_same_request_replays_original_application(self):
        first = self._submit(request_id="app-rep")
        second = self._submit(request_id="app-rep")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["application_id"], second["application_id"])

    def test_replay_with_changed_payload_conflicts(self):
        site = self._independent_acceptance("ev-rc", "YS-RC")
        self.service.submit_payment_application(
            request_id="app-rc", actor_id="ap1", contract_id="c1",
            lines=[{"item_id": self.item_a, "quantity_milli": 40000, "evidence_ids": [site]}])
        with self.assertRaises(ConflictError):
            self.service.submit_payment_application(
                request_id="app-rc", actor_id="ap1", contract_id="c1",
                lines=[{"item_id": self.item_a, "quantity_milli": 30000, "evidence_ids": [site]}])

    def test_duplicate_evidence_enters_pending_instead_of_payment(self):
        # 同一批材料验收单被再次用于另一笔申请（总包/分包重复申报场景）
        site = self._independent_acceptance("ev-dup", "YS-DUP")
        material = self._evidence("ev-mat", "ap1", "material_acceptance", "CL-001")
        used = self.service.submit_payment_application(
            request_id="app-dup-1", actor_id="ap1", contract_id="c1",
            lines=[{"item_id": self.item_a, "quantity_milli": 10000, "evidence_ids": [site, material]}])
        self.assertEqual("submitted", used["status"])
        duplicate = self.service.submit_payment_application(
            request_id="app-dup-2", actor_id="ap1", contract_id="c1",
            lines=[{"item_id": self.item_a, "quantity_milli": 10000, "evidence_ids": [material]}])
        self.assertEqual("pending", duplicate["status"])
        self.assertIn("duplicate_evidence", {r["reason"] for r in duplicate["reasons"]})
        # 待处理申请不产生任何付款义务
        balance = self.service.contract_balance("c1")
        self.assertEqual(0, balance["payments_cents"])
        self.assertEqual(0, balance["approved_gross_cents"])

    def test_invoice_cannot_be_reused_across_applications(self):
        site1 = self._independent_acceptance("ev-i1", "YS-I1")
        site2 = self._independent_acceptance("ev-i2", "YS-I2")
        invoice = self._evidence("ev-inv", "ap1", "invoice", "FP-001", amount_cents=1000000)
        first = self.service.submit_payment_application(
            request_id="app-i1", actor_id="ap1", contract_id="c1",
            lines=[{"item_id": self.item_a, "quantity_milli": 10000, "evidence_ids": [site1]}],
            invoice_ids=[invoice])
        self.assertEqual("submitted", first["status"])
        second = self.service.submit_payment_application(
            request_id="app-i2", actor_id="ap1", contract_id="c1",
            lines=[{"item_id": self.item_a, "quantity_milli": 10000, "evidence_ids": [site2]}],
            invoice_ids=[invoice])
        self.assertEqual("pending", second["status"])
        self.assertIn("duplicate_evidence", {r["reason"] for r in second["reasons"]})

    def test_over_contract_quantity_enters_pending(self):
        # 合同量 100 单位，本次申报 101 单位
        result = self._submit(request_id="app-over", quantity=101000)
        self.assertEqual("pending", result["status"])
        self.assertIn("quantity_exceeds_contract", {r["reason"] for r in result["reasons"]})

    def test_cumulative_quantity_includes_earlier_applications(self):
        self._approved(request_id="app-cum-1", quantity=80000)
        result = self._submit(request_id="app-cum-2", quantity=30000)
        self.assertEqual("pending", result["status"])
        self.assertIn("quantity_exceeds_contract", {r["reason"] for r in result["reasons"]})
        ok = self._submit(request_id="app-cum-3", quantity=20000)
        self.assertEqual("submitted", ok["status"])

    def test_missing_independent_acceptance_enters_pending(self):
        # 施工方自己登记的材料验收单不具备独立验收效力
        material = self._evidence("ev-own", "ap1", "material_acceptance", "CL-OWN")
        result = self.service.submit_payment_application(
            request_id="app-noind", actor_id="ap1", contract_id="c1",
            lines=[{"item_id": self.item_a, "quantity_milli": 1000, "evidence_ids": [material]}])
        self.assertEqual("pending", result["status"])
        self.assertIn("missing_independent_acceptance", {r["reason"] for r in result["reasons"]})

    def test_pending_application_occupies_nothing(self):
        material = self._evidence("ev-free", "ap1", "material_acceptance", "CL-FREE")
        result = self.service.submit_payment_application(
            request_id="app-free", actor_id="ap1", contract_id="c1",
            lines=[{"item_id": self.item_a, "quantity_milli": 1000, "evidence_ids": [material]}])
        self.assertEqual("pending", result["status"])
        self.assertEqual([], self.service.evidence_occupations(material)["occupations"])

    def test_pending_application_can_be_withdrawn(self):
        material = self._evidence("ev-pw", "ap1", "material_acceptance", "CL-PW")
        result = self.service.submit_payment_application(
            request_id="app-pw", actor_id="ap1", contract_id="c1",
            lines=[{"item_id": self.item_a, "quantity_milli": 1000, "evidence_ids": [material]}])
        self.assertEqual("pending", result["status"])
        withdrawn = self.service.withdraw_application(request_id="wd-pw", actor_id="ap1",
                                                      application_id=result["application_id"])
        self.assertEqual("withdrawn", withdrawn["status"])

    # ------------------------------------------------------------------
    # 撤回、驳回、部分批准
    # ------------------------------------------------------------------

    def test_withdraw_releases_evidence_for_reuse(self):
        site = self._independent_acceptance("ev-wd", "YS-WD")
        result = self.service.submit_payment_application(
            request_id="app-wd", actor_id="ap1", contract_id="c1",
            lines=[{"item_id": self.item_a, "quantity_milli": 1000, "evidence_ids": [site]}])
        self.service.withdraw_application(request_id="wd-1", actor_id="ap1",
                                          application_id=result["application_id"])
        again = self.service.submit_payment_application(
            request_id="app-wd-2", actor_id="ap1", contract_id="c1",
            lines=[{"item_id": self.item_a, "quantity_milli": 1000, "evidence_ids": [site]}])
        self.assertEqual("submitted", again["status"])
        occupations = self.service.evidence_occupations(site)["occupations"]
        self.assertEqual(2, len(occupations))
        self.assertEqual("withdrawn", occupations[0]["release_reason"])
        self.assertIsNone(occupations[1]["released_at"])

    def test_other_applicant_cannot_withdraw(self):
        self.service.register_actor(request_id="applicant-2", actor_id="ad1", new_actor_id="ap2",
                                    display_name="另一经办", role="applicant", organization_id="o1")
        result = self._submit(request_id="app-other")
        with self.assertRaises(PermissionDenied):
            self.service.withdraw_application(request_id="wd-x", actor_id="ap2",
                                              application_id=result["application_id"])

    def test_reject_releases_all_evidence_and_allows_resubmission(self):
        site = self._independent_acceptance("ev-rj", "YS-RJ")
        invoice = self._evidence("ev-rj-inv", "ap1", "invoice", "FP-RJ", amount_cents=4000000)
        result = self.service.submit_payment_application(
            request_id="app-rj", actor_id="ap1", contract_id="c1",
            lines=[{"item_id": self.item_a, "quantity_milli": 40000, "evidence_ids": [site]}],
            invoice_ids=[invoice])
        review = self.service.review_application(request_id="rj-1", actor_id="rv1",
                                                 application_id=result["application_id"],
                                                 decision="reject")
        self.assertEqual("rejected", review["status"])
        for evidence_id in (site, invoice):
            occupations = self.service.evidence_occupations(evidence_id)["occupations"]
            self.assertEqual(1, len(occupations))
            self.assertIsNotNone(occupations[0]["released_at"])
        again = self.service.submit_payment_application(
            request_id="app-rj-2", actor_id="ap1", contract_id="c1",
            lines=[{"item_id": self.item_a, "quantity_milli": 40000, "evidence_ids": [site]}],
            invoice_ids=[invoice])
        self.assertEqual("submitted", again["status"])

    def test_partial_approval_releases_only_rejected_line_evidence(self):
        site_a = self._independent_acceptance("ev-pa", "YS-PA")
        site_b = self._independent_acceptance("ev-pb", "YS-PB")
        result = self.service.submit_payment_application(
            request_id="app-part", actor_id="ap1", contract_id="c1",
            lines=[{"item_id": self.item_a, "quantity_milli": 40000, "evidence_ids": [site_a]},
                   {"item_id": self.item_b, "quantity_milli": 100000, "evidence_ids": [site_b]}])
        self.assertEqual("submitted", result["status"])
        detail = self.service.application_detail(result["application_id"])
        line_a = next(l for l in detail["lines"] if l["item_id"] == self.item_a)
        review = self.service.review_application(
            request_id="part-1", actor_id="rv1", application_id=result["application_id"],
            decision="partial", approved_line_ids=[line_a["line_id"]])
        self.assertEqual("partially_approved", review["status"])
        self.assertEqual(4000000, review["approved_gross_cents"])
        self.assertEqual(3880000, review["approved_net_cents"])
        # 批准行证据仍被占用，驳回行证据已释放
        self.assertIsNone(self.service.evidence_occupations(site_a)["occupations"][0]["released_at"])
        self.assertIsNotNone(self.service.evidence_occupations(site_b)["occupations"][0]["released_at"])
        # 实际资金拆分只覆盖批准行净额
        detail = self.service.application_detail(result["application_id"])
        actual = {a["funding_source_id"]: a["amount_cents"]
                  for a in detail["funding_allocations"] if a["stage"] == "actual"}
        self.assertEqual({self.sources["central"]: 2328000, self.sources["local"]: 1552000}, actual)
        # 驳回行不再占用累计量，可重新申报
        followup = self._submit(request_id="app-part-2", quantity=100000,
                                lines=[{"item_id": self.item_b, "quantity_milli": 100000,
                                        "evidence_ids": [site_b]}])
        self.assertEqual("submitted", followup["status"])

    # ------------------------------------------------------------------
    # 支付与质保金
    # ------------------------------------------------------------------

    def test_payment_cannot_exceed_outstanding(self):
        app_id = self._approved()
        with self.assertRaises(ValidationError):
            self.service.record_payment(request_id="pay-x", actor_id="fi1",
                                        application_id=app_id, amount_cents=3880001)

    def test_full_payment_waits_for_retention_release(self):
        app_id = self._approved()
        paid = self.service.record_payment(request_id="pay-1", actor_id="fi1",
                                           application_id=app_id, amount_cents=3880000)
        self.assertEqual("approved", paid["status"])  # 质保金仍留置，未结清
        self.service.release_retention(request_id="rel-1", actor_id="rv1",
                                       application_id=app_id, amount_cents=120000)
        done = self.service.record_payment(request_id="pay-2", actor_id="fi1",
                                           application_id=app_id, amount_cents=120000)
        self.assertEqual("paid", done["status"])

    def test_retention_cannot_be_released_before_condition_date(self):
        self.service.create_contract(
            request_id="ctr2", actor_id="ad1", contract_id="c2", project_id="p1",
            code="HT-002", name="绿化合同", contractor_name="园林公司",
            retention_rate_bp=500, retention_release_after="2027-01-01",
            boq_items=[{"item_code": "B-1", "name": "绿化种植", "unit": "m2",
                        "unit_price_cents": 20000, "quantity_milli": 100000}])
        item = self.database.connection.execute(
            "SELECT item_id FROM boq_items WHERE contract_id='c2'").fetchone()["item_id"]
        site = self._independent_acceptance("ev-c2", "YS-C2")
        result = self.service.submit_payment_application(
            request_id="app-c2", actor_id="ap1", contract_id="c2",
            lines=[{"item_id": item, "quantity_milli": 10000, "evidence_ids": [site]}])
        self.service.review_application(request_id="rv-c2", actor_id="rv1",
                                        application_id=result["application_id"], decision="approve")
        with self.assertRaises(ConflictError):
            self.service.release_retention(request_id="rel-early", actor_id="rv1",
                                           application_id=result["application_id"],
                                           amount_cents=1000)

    def test_retention_release_cannot_exceed_held(self):
        app_id = self._approved()
        with self.assertRaises(ValidationError):
            self.service.release_retention(request_id="rel-over", actor_id="rv1",
                                           application_id=app_id, amount_cents=120001)

    # ------------------------------------------------------------------
    # 审计追回
    # ------------------------------------------------------------------

    def test_recovery_reduces_paid_and_restores_obligation(self):
        app_id = self._approved()
        self.service.record_payment(request_id="pay-r", actor_id="fi1",
                                    application_id=app_id, amount_cents=3880000)
        self.service.release_retention(request_id="rel-r", actor_id="rv1",
                                       application_id=app_id, amount_cents=120000)
        self.service.record_payment(request_id="pay-r2", actor_id="fi1",
                                    application_id=app_id, amount_cents=120000)
        before = self.service.contract_balance("c1")
        self.service.record_recovery(request_id="rec-1", actor_id="au1",
                                     application_id=app_id, amount_cents=500000,
                                     reason="审计抽查核减")
        after = self.service.contract_balance("c1")
        self.assertEqual(before["paid_net_cents"] - 500000, after["paid_net_cents"])
        self.assertEqual(before["remaining_obligation_cents"] + 500000,
                         after["remaining_obligation_cents"])
        self.assertTrue(after["balanced"])

    def test_recovery_cannot_exceed_paid_amount(self):
        app_id = self._approved()
        self.service.record_payment(request_id="pay-r3", actor_id="fi1",
                                    application_id=app_id, amount_cents=1000000)
        with self.assertRaises(ValidationError):
            self.service.record_recovery(request_id="rec-x", actor_id="au1",
                                         application_id=app_id, amount_cents=1000001,
                                         reason="超额追回")

    def test_only_auditor_can_recover(self):
        app_id = self._approved()
        with self.assertRaises(PermissionDenied):
            self.service.record_recovery(request_id="rec-p", actor_id="fi1",
                                         application_id=app_id, amount_cents=1, reason="越权")

    # ------------------------------------------------------------------
    # 会计期间与关账后更正
    # ------------------------------------------------------------------

    def test_correction_after_close_lands_in_new_period(self):
        app_id = self._approved()
        self.service.record_payment(request_id="pay-c", actor_id="fi1",
                                    application_id=app_id, amount_cents=1000000)
        entry = next(e for e in self.service.contract_ledger("c1") if e["kind"] == "payment.made")
        self.assertEqual("P-0001", entry["period"])
        self.service.close_period(request_id="close-1", actor_id="ad1",
                                  project_id="p1", new_period_name="P-0002")
        correction = self.service.post_correction(request_id="corr-1", actor_id="fi1",
                                                  corrects_entry_id=entry["entry_id"],
                                                  amount_cents=-1000, reason="付款手续费重分类")
        self.assertEqual("P-0002", correction["posted_period"])
        self.assertEqual("closed", correction["original_period_status"])
        # 原期间分录不被改写，更正以新分录落在开放期间
        ledger = self.service.contract_ledger("c1")
        original = next(e for e in ledger if e["entry_id"] == entry["entry_id"])
        self.assertEqual("P-0001", original["period"])
        posted = next(e for e in ledger if e["kind"] == "correction.posted")
        self.assertEqual(entry["entry_id"], posted["corrects_entry_id"])
        self.assertEqual("P-0002", posted["period"])

    def test_close_period_keeps_exactly_one_open_period(self):
        self.service.close_period(request_id="close-a", actor_id="ad1",
                                  project_id="p1", new_period_name="P-0002")
        self.service.close_period(request_id="close-b", actor_id="ad1",
                                  project_id="p1", new_period_name="P-0003")
        rows = self.database.connection.execute(
            "SELECT name, status FROM periods WHERE project_id='p1' ORDER BY name").fetchall()
        self.assertEqual([("P-0001", "closed"), ("P-0002", "closed"), ("P-0003", "open")],
                         [(r["name"], r["status"]) for r in rows])

    # ------------------------------------------------------------------
    # 变更令与工程量版本
    # ------------------------------------------------------------------

    def _change_order(self, request_id="co-1", code="BG-001", delta=50000, ratio_central=5000):
        self.service.create_change_order(
            request_id=request_id, actor_id="ad1", change_order_id=request_id, contract_id="c1",
            code=code, reason="设计变更",
            funding_shares=[{"funding_source_id": self.sources["central"], "ratio_bp": ratio_central},
                            {"funding_source_id": self.sources["local"],
                             "ratio_bp": 10000 - ratio_central}],
            adjustments=[{"item_id": self.item_a, "delta_quantity_milli": delta}])
        return request_id

    def test_approved_change_order_bumps_quantity_version(self):
        change_order_id = self._change_order()
        self.service.approve_change_order(request_id="co-ok", actor_id="rv1",
                                          change_order_id=change_order_id)
        # 变更后合同量 150 单位，超过原合同 100 单位的申报现在可以通过
        site = self._independent_acceptance("ev-co", "YS-CO")
        result = self.service.submit_payment_application(
            request_id="app-co", actor_id="ap1", contract_id="c1",
            lines=[{"item_id": self.item_a, "quantity_milli": 120000, "evidence_ids": [site],
                    "change_order_id": change_order_id}])
        self.assertEqual("submitted", result["status"])
        self.assertEqual(2, result["quantity_version_no"])
        balance = self.service.contract_balance("c1")
        self.assertEqual(25000000, balance["contract_amount_cents"])

    def test_change_order_line_uses_its_own_funding_split(self):
        change_order_id = self._change_order(ratio_central=5000)
        self.service.approve_change_order(request_id="co-ok", actor_id="rv1",
                                          change_order_id=change_order_id)
        site = self._independent_acceptance("ev-co2", "YS-CO2")
        result = self.service.submit_payment_application(
            request_id="app-co2", actor_id="ap1", contract_id="c1",
            lines=[{"item_id": self.item_a, "quantity_milli": 10000, "evidence_ids": [site],
                    "change_order_id": change_order_id}])
        self.assertEqual("submitted", result["status"])
        # 变更令跨越不同资金来源：50/50 而不是项目默认的 60/40
        self.assertEqual({self.sources["central"]: 485000, self.sources["local"]: 485000},
                         result["planned_funding"])

    def test_unapproved_change_order_cannot_be_referenced(self):
        change_order_id = self._change_order()
        site = self._independent_acceptance("ev-co3", "YS-CO3")
        with self.assertRaises(ValidationError):
            self.service.submit_payment_application(
                request_id="app-co3", actor_id="ap1", contract_id="c1",
                lines=[{"item_id": self.item_a, "quantity_milli": 1000, "evidence_ids": [site],
                        "change_order_id": change_order_id}])

    def test_change_order_cannot_reduce_below_reserved_quantity(self):
        self._approved(request_id="app-res", quantity=80000)
        change_order_id = self._change_order(request_id="co-neg", code="BG-NEG", delta=-50000)
        with self.assertRaises(ValidationError):
            self.service.approve_change_order(request_id="co-neg-ok", actor_id="rv1",
                                              change_order_id=change_order_id)

    # ------------------------------------------------------------------
    # 平衡与可追溯
    # ------------------------------------------------------------------

    def test_balance_identity_holds_through_lifecycle(self):
        app_id = self._approved()
        self.service.record_payment(request_id="pay-b1", actor_id="fi1",
                                    application_id=app_id, amount_cents=2000000)
        balance = self.service.contract_balance("c1")
        self.assertTrue(balance["balanced"])
        self.assertEqual(20000000, balance["contract_amount_cents"])
        self.assertEqual(2000000, balance["paid_net_cents"])
        self.assertEqual(120000, balance["retention_held_cents"])
        self.assertEqual(1880000, balance["payable_outstanding_cents"])
        self.assertEqual(16000000, balance["remaining_obligation_cents"])
        total = (balance["paid_net_cents"] + balance["retention_held_cents"]
                 + balance["payable_outstanding_cents"] + balance["remaining_obligation_cents"])
        self.assertEqual(balance["contract_amount_cents"], total)

    def test_finance_can_explain_full_lifecycle(self):
        app_id = self._approved()
        self.service.record_payment(request_id="pay-t", actor_id="fi1",
                                    application_id=app_id, amount_cents=3880000)
        self.service.release_retention(request_id="rel-t", actor_id="rv1",
                                       application_id=app_id, amount_cents=120000)
        self.service.record_payment(request_id="pay-t2", actor_id="fi1",
                                    application_id=app_id, amount_cents=120000)
        detail = self.service.application_detail(app_id)
        self.assertEqual("paid", detail["status"])
        self.assertEqual(["application.submitted", "application.approved",
                          "retention.withheld", "payment.made",
                          "retention.released", "payment.made"],
                         [entry["kind"] for entry in detail["ledger"]])
        self.assertEqual("rv1", detail["decided_by"])
        self.assertEqual(0, detail["money"]["outstanding_cents"])
        self.assertEqual(0, detail["money"]["retention_held_cents"])

    def test_auditor_can_trace_evidence_to_all_occupations(self):
        site = self._independent_acceptance("ev-tr", "YS-TR")
        first = self.service.submit_payment_application(
            request_id="app-tr-1", actor_id="ap1", contract_id="c1",
            lines=[{"item_id": self.item_a, "quantity_milli": 1000, "evidence_ids": [site]}])
        self.service.withdraw_application(request_id="wd-tr", actor_id="ap1",
                                          application_id=first["application_id"])
        second = self.service.submit_payment_application(
            request_id="app-tr-2", actor_id="ap1", contract_id="c1",
            lines=[{"item_id": self.item_a, "quantity_milli": 1000, "evidence_ids": [site]}])
        occupations = self.service.evidence_occupations(site)["occupations"]
        self.assertEqual([first["application_id"], second["application_id"]],
                         [o["application_id"] for o in occupations])
        self.assertEqual("withdrawn", occupations[0]["release_reason"])
        self.assertIsNone(occupations[1]["released_at"])

    def test_audit_chain_stays_valid(self):
        app_id = self._approved()
        self.service.record_payment(request_id="pay-v", actor_id="fi1",
                                    application_id=app_id, amount_cents=1000000)
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
