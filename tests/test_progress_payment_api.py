import unittest

from progress_payment.api import route
from progress_payment.service import PaymentService
from progress_payment.storage import Database


class PaymentApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = PaymentService(self.database)

    def tearDown(self):
        self.database.close()

    def _bootstrap(self):
        route(self.service, "POST", "/organizations",
              {"request_id": "org", "organization_id": "o1", "name": "交通投资集团"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "admin", "new_actor_id": "ad1", "display_name": "管理员",
               "role": "admin", "organization_id": "o1"}, {"X-Actor-Id": "bootstrap"})

    def _actor(self, request_id, new_id, role):
        return route(self.service, "POST", "/actors",
                     {"request_id": request_id, "new_actor_id": new_id, "display_name": new_id,
                      "role": role, "organization_id": "o1"}, {"X-Actor-Id": "ad1"})

    def test_health_is_available_without_actor(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_route_returns_404(self):
        status, payload = route(self.service, "GET", "/missing", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_invalid_payload_returns_400(self):
        status, payload = route(self.service, "POST", "/organizations", {"request_id": "x"},
                                {"X-Actor-Id": "bootstrap"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_permission_denied_maps_to_403(self):
        self._bootstrap()
        self._actor("ap", "ap1", "applicant")
        status, payload = route(self.service, "POST", "/projects",
                                {"request_id": "proj", "project_id": "p1", "organization_id": "o1",
                                 "code": "GS-001", "name": "高架工程",
                                 "funding_sources": [{"code": "c", "name": "中央", "ratio_bp": 10000}]},
                                {"X-Actor-Id": "ap1"})
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_submit_flow_over_http(self):
        self._bootstrap()
        for req, new_id, role in [("ap", "ap1", "applicant"), ("in", "in1", "inspector"),
                                  ("rv", "rv1", "reviewer"), ("fi", "fi1", "finance")]:
            self._actor(req, new_id, role)
        status, _ = route(self.service, "POST", "/projects",
                          {"request_id": "proj", "project_id": "p1", "organization_id": "o1",
                           "code": "GS-001", "name": "高架工程",
                           "funding_sources": [{"code": "central", "name": "中央", "ratio_bp": 10000}]},
                          {"X-Actor-Id": "ad1"})
        self.assertEqual(201, status)
        status, _ = route(self.service, "POST", "/contracts",
                          {"request_id": "ctr", "contract_id": "c1", "project_id": "p1",
                           "code": "HT-001", "name": "路基合同", "contractor_name": "一局",
                           "retention_rate_bp": 300, "retention_release_after": "2026-01-01",
                           "boq_items": [{"item_code": "A-1", "name": "填方", "unit": "m3",
                                          "unit_price_cents": 100000, "quantity_milli": 100000}]},
                          {"X-Actor-Id": "ad1"})
        self.assertEqual(201, status)
        item_id = self.database.connection.execute(
            "SELECT item_id FROM boq_items WHERE contract_id='c1'").fetchone()["item_id"]
        status, receipt = route(self.service, "POST", "/evidence",
                                {"request_id": "ev", "project_id": "p1",
                                 "evidence_type": "site_acceptance", "external_key": "YS-001"},
                                {"X-Actor-Id": "in1"})
        self.assertEqual(201, status)
        status, application = route(self.service, "POST", "/payment-applications",
                                    {"request_id": "app", "contract_id": "c1",
                                     "lines": [{"item_id": item_id, "quantity_milli": 40000,
                                                "evidence_ids": [receipt["resource_id"]]}]},
                                    {"X-Actor-Id": "ap1"})
        self.assertEqual(201, status)
        self.assertEqual("submitted", application["status"])
        # 幂等重放返回 200 与同一申请
        status, replay = route(self.service, "POST", "/payment-applications",
                               {"request_id": "app", "contract_id": "c1",
                                "lines": [{"item_id": item_id, "quantity_milli": 40000,
                                           "evidence_ids": [receipt["resource_id"]]}]},
                               {"X-Actor-Id": "ap1"})
        self.assertEqual(200, status)
        self.assertEqual(application["application_id"], replay["application_id"])
        status, review = route(self.service, "POST", "/payment-application-reviews",
                               {"request_id": "rvw", "application_id": application["application_id"],
                                "decision": "approve"}, {"X-Actor-Id": "rv1"})
        self.assertEqual(201, status)
        self.assertEqual("approved", review["status"])
        status, payment = route(self.service, "POST", "/payments",
                                {"request_id": "pay1", "application_id": application["application_id"],
                                 "amount_cents": 3880000}, {"X-Actor-Id": "fi1"})
        self.assertEqual(201, status)
        self.assertEqual(0, payment["outstanding_cents"])
        status, detail = route(self.service, "GET",
                               f"/payment-application?id={application['application_id']}", None)
        self.assertEqual(200, status)
        self.assertEqual(["application.submitted", "application.approved",
                          "retention.withheld", "payment.made"],
                         [entry["kind"] for entry in detail["ledger"]])
        status, balance = route(self.service, "GET", "/contract-balance?contract_id=c1", None)
        self.assertEqual(200, status)
        self.assertTrue(balance["balanced"])
        self.assertEqual(10000000, balance["contract_amount_cents"])


if __name__ == "__main__":
    unittest.main()
