"""进度支付核验服务的 HTTP/JSON 路由。"""

from __future__ import annotations

from typing import Any

from .errors import ValidationError
from .payment_service import PaymentService


def _created(result: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return (200 if result["replayed"] else 201), result


def _query_one(query: dict[str, list[str]], name: str) -> str:
    value = query.get(name, [""])[0]
    if not value:
        raise ValidationError(f"{name} 不能为空")
    return value


def route_payment(service: PaymentService, method: str, path: str,
                  query: dict[str, list[str]], body: dict[str, Any],
                  actor_id: str) -> tuple[int, dict[str, Any]]:
    """把 /payment/ 前缀的 HTTP 语义请求分派到支付核验服务。"""

    if method == "POST" and path == "/payment/contracts":
        return _created(service.register_contract(actor_id=actor_id, **body))
    if method == "POST" and path == "/payment/change-orders":
        return _created(service.register_change_order(actor_id=actor_id, **body))
    if method == "POST" and path == "/payment/change-order-approvals":
        return _created(service.approve_change_order(actor_id=actor_id, **body))
    if method == "POST" and path == "/payment/acceptances":
        return _created(service.register_acceptance(actor_id=actor_id, **body))
    if method == "POST" and path == "/payment/invoices":
        return _created(service.register_invoice(actor_id=actor_id, **body))
    if method == "POST" and path == "/payment/quality-certificates":
        return _created(service.register_quality_certificate(actor_id=actor_id, **body))
    if method == "POST" and path == "/payment/applications":
        return _created(service.submit_application(actor_id=actor_id, **body))
    if method == "POST" and path == "/payment/countersigns":
        return _created(service.countersign_application(actor_id=actor_id, **body))
    if method == "POST" and path == "/payment/approvals":
        return _created(service.approve_application(actor_id=actor_id, **body))
    if method == "POST" and path == "/payment/rejections":
        return _created(service.reject_application(actor_id=actor_id, **body))
    if method == "POST" and path == "/payment/withdrawals":
        return _created(service.withdraw_application(actor_id=actor_id, **body))
    if method == "POST" and path == "/payment/payments":
        return _created(service.record_payment(actor_id=actor_id, **body))
    if method == "POST" and path == "/payment/retention-releases":
        return _created(service.release_retention(actor_id=actor_id, **body))
    if method == "POST" and path == "/payment/recoveries":
        return _created(service.recover_payment(actor_id=actor_id, **body))
    if method == "POST" and path == "/payment/corrections":
        return _created(service.correct_entry(actor_id=actor_id, **body))
    if method == "POST" and path == "/payment/period-closes":
        return _created(service.close_period(actor_id=actor_id, **body))
    if method == "GET" and path == "/payment/contracts":
        return 200, service.get_contract(_query_one(query, "contract_id"))
    if method == "GET" and path == "/payment/applications":
        application_id = query.get("application_id", [""])[0]
        if application_id:
            return 200, service.explain_application(application_id)
        return 200, {"items": service.list_applications(_query_one(query, "contract_id"))}
    if method == "GET" and path == "/payment/ledger":
        return 200, {"items": service.contract_ledger(_query_one(query, "contract_id"))}
    if method == "GET" and path == "/payment/balance":
        return 200, service.contract_balance(_query_one(query, "contract_id"))
    if method == "GET" and path == "/payment/evidence":
        return 200, service.trace_evidence(_query_one(query, "evidence_type"),
                                           _query_one(query, "evidence_id"))
    return 404, {"error": "route_not_found", "message": "接口不存在"}
