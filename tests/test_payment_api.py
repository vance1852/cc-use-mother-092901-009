import unittest

from transport_coordination.api import route
from transport_coordination.payment_service import PaymentService
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


class PaymentApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.domain = DomainService(self.database)
        self.payment = PaymentService(self.domain)
        self.domain.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="交通集团")
        self.domain.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin1",
                                   display_name="管理员", role="admin", organization_id="o1")
        self.domain.register_actor(request_id="op", actor_id="admin1", new_actor_id="op1",
                                   display_name="经办", role="operator", organization_id="o1")
        self.domain.register_site(request_id="site", actor_id="op1", site_id="s1",
                                  organization_id="o1", name="项目部", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def _route(self, method, path, body=None, actor="op1"):
        return route(self.domain, method, path, body, {"X-Actor-Id": actor},
                     payment_service=self.payment)

    def _contract(self):
        return self._route("POST", "/payment/contracts", {
            "request_id": "c1", "contract_id": "c1", "site_id": "s1", "contract_no": "HT-1",
            "name": "绕城高速工程", "contractor": "总包一局", "retention_rate_bp": 300,
            "defect_liability_end": "2026-10-31", "requires_quality_certificate": False,
            "funding_sources": [{"source_code": "central", "ratio_bp": 6000},
                                {"source_code": "local", "ratio_bp": 4000}],
            "items": [{"item_code": "I-1", "name": "路基", "unit": "m3",
                       "unit_price_cents": 12000, "quantity_milli": 1000000}]})

    def test_contract_registration_and_replay(self):
        status, payload = self._contract()
        self.assertEqual(201, status)
        self.assertFalse(payload["replayed"])
        status, payload = self._contract()
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])

    def test_full_http_flow_and_queries(self):
        self._contract()
        self._route("POST", "/payment/acceptances", {
            "request_id": "a1", "acceptance_id": "ac1", "contract_id": "c1",
            "acceptance_no": "AC-1", "item_code": "I-1", "quantity_milli": 400000,
            "accepted_on": "2026-09-20", "inspector": "监理甲", "independent": True})
        self._route("POST", "/payment/invoices", {
            "request_id": "i1", "invoice_id": "inv1", "contract_id": "c1",
            "invoice_no": "INV-1", "amount_cents": 6000000, "issued_on": "2026-09-21"})
        status, payload = self._route("POST", "/payment/applications", {
            "request_id": "app1", "application_id": "app1", "contract_id": "c1",
            "lines": [{"acceptance_id": "ac1", "invoice_id": "inv1"}]})
        self.assertEqual(201, status)
        self.assertEqual("submitted", payload["result"]["status"])
        status, payload = self._route("GET", "/payment/applications?application_id=app1")
        self.assertEqual(200, status)
        self.assertEqual("app1", payload["application"]["application_id"])
        self.assertEqual(1, len(payload["entries"]))
        status, payload = self._route("GET", "/payment/balance?contract_id=c1")
        self.assertEqual(200, status)
        self.assertTrue(payload["balanced"])
        status, payload = self._route("GET", "/payment/evidence?evidence_type=acceptance&evidence_id=ac1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["occupations"]))
        status, payload = self._route("GET", "/payment/applications?contract_id=c1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))

    def test_unknown_payment_route_returns_404(self):
        status, payload = self._route("GET", "/payment/unknown")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_missing_query_parameter_returns_400(self):
        status, payload = self._route("GET", "/payment/balance")
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_domain_error_maps_to_status(self):
        self._contract()
        status, payload = self._route("POST", "/payment/applications", {
            "request_id": "app1", "application_id": "app1", "contract_id": "c1",
            "lines": [{"acceptance_id": "missing", "invoice_id": "missing"}]})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_base_routes_still_work_with_payment_service(self):
        status, payload = self._route("GET", "/health", actor="")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])


if __name__ == "__main__":
    unittest.main()
