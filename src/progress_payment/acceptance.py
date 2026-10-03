"""运行进度支付核验服务的离线端到端验收。

场景覆盖：建档 → 项目与共同出资 → 合同清单与质保条件 → 证据登记 →
申报（同一事务内证据占用、累计量校验、资金份额拆分）→ 重复材料进入待处理 →
会签 → 支付 → 质保金释放 → 关账后更正 → 审计追回 → 平衡验证与审计链校验。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .service import PaymentService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整支付核验链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = PaymentService(database, FixedClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)))

        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="交通投资集团")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        for request_id, new_id, name, role in [
            ("req-applicant", "applicant-001", "施工总包经办", "applicant"),
            ("req-inspector", "inspector-001", "驻地监理", "inspector"),
            ("req-reviewer", "reviewer-001", "会签负责人", "reviewer"),
            ("req-finance", "finance-001", "财务出纳", "finance"),
            ("req-auditor", "auditor-001", "审计员", "auditor"),
        ]:
            service.register_actor(request_id=request_id, actor_id="admin-001", new_actor_id=new_id,
                                   display_name=name, role=role, organization_id="org-001")

        service.create_project(request_id="req-project", actor_id="admin-001", project_id="proj-001",
                               organization_id="org-001", code="GS-2026-001", name="高架快速路一期工程",
                               funding_sources=[
                                   {"code": "central", "name": "中央补助资金", "ratio_bp": 6000},
                                   {"code": "local", "name": "地方配套资金", "ratio_bp": 4000},
                               ])
        service.create_contract(request_id="req-contract", actor_id="admin-001", contract_id="ctr-001",
                                project_id="proj-001", code="HT-001", name="路基路面施工合同",
                                contractor_name="第一工程局", retention_rate_bp=300,
                                retention_release_after="2026-09-01",
                                boq_items=[
                                    {"item_code": "A-101", "name": "路基填方", "unit": "m3",
                                     "unit_price_cents": 100000, "quantity_milli": 100000},
                                    {"item_code": "A-102", "name": "沥青面层", "unit": "m2",
                                     "unit_price_cents": 50000, "quantity_milli": 200000},
                                ])

        site_acceptance = service.register_evidence(
            request_id="req-ev-site", actor_id="inspector-001", project_id="proj-001",
            evidence_type="site_acceptance", external_key="YS-2026-0901", quantity_milli=40000)
        material = service.register_evidence(
            request_id="req-ev-material", actor_id="applicant-001", project_id="proj-001",
            evidence_type="material_acceptance", external_key="CL-2026-0901", quantity_milli=40000)
        invoice = service.register_evidence(
            request_id="req-ev-invoice", actor_id="applicant-001", project_id="proj-001",
            evidence_type="invoice", external_key="FP-2026-0901", amount_cents=4000000)

        items = database.connection.execute(
            "SELECT item_id, item_code FROM boq_items WHERE contract_id='ctr-001'").fetchall()
        item_a = next(row["item_id"] for row in items if row["item_code"] == "A-101")

        first = service.submit_payment_application(
            request_id="req-app-1", actor_id="applicant-001", contract_id="ctr-001",
            lines=[{"item_id": item_a, "quantity_milli": 40000,
                    "evidence_ids": [site_acceptance.resource_id, material.resource_id]}],
            invoice_ids=[invoice.resource_id])
        replay = service.submit_payment_application(
            request_id="req-app-1", actor_id="applicant-001", contract_id="ctr-001",
            lines=[{"item_id": item_a, "quantity_milli": 40000,
                    "evidence_ids": [site_acceptance.resource_id, material.resource_id]}],
            invoice_ids=[invoice.resource_id])
        duplicate = service.submit_payment_application(
            request_id="req-app-2", actor_id="applicant-001", contract_id="ctr-001",
            lines=[{"item_id": item_a, "quantity_milli": 10000,
                    "evidence_ids": [site_acceptance.resource_id]}])
        service.withdraw_application(request_id="req-withdraw", actor_id="applicant-001",
                                     application_id=duplicate["application_id"])

        application_id = first["application_id"]
        service.review_application(request_id="req-review", actor_id="reviewer-001",
                                   application_id=application_id, decision="approve")
        service.record_payment(request_id="req-pay-1", actor_id="finance-001",
                               application_id=application_id, amount_cents=3880000,
                               payment_reference="PAY-2026-1001")
        service.release_retention(request_id="req-retention", actor_id="reviewer-001",
                                  application_id=application_id, amount_cents=120000)
        service.record_payment(request_id="req-pay-2", actor_id="finance-001",
                               application_id=application_id, amount_cents=120000,
                               payment_reference="PAY-2026-1002")

        service.close_period(request_id="req-close", actor_id="admin-001",
                             project_id="proj-001", new_period_name="P-0002")
        payment_entry = next(
            entry for entry in service.contract_ledger("ctr-001") if entry["kind"] == "payment.made")
        correction = service.post_correction(request_id="req-correction", actor_id="finance-001",
                                             corrects_entry_id=payment_entry["entry_id"],
                                             amount_cents=-1000, reason="付款手续费重分类")
        service.record_recovery(request_id="req-recovery", actor_id="auditor-001",
                                application_id=application_id, amount_cents=500000,
                                reason="审计抽查核减重复计量")

        balance = service.contract_balance("ctr-001")
        detail = service.application_detail(application_id)
        occupations = service.evidence_occupations(site_acceptance.resource_id)
        valid, event_count = service.verify_audit()
        result = {
            "status": "ok",
            "application_status": detail["status"],
            "first_replayed": first["replayed"],
            "second_replayed": replay["replayed"],
            "duplicate_blocked": duplicate["status"] == "pending"
            and any(r["reason"] == "duplicate_evidence" for r in duplicate["reasons"]),
            "correction_in_new_period": correction["posted_period"] == "P-0002"
            and correction["original_period_status"] == "closed",
            "evidence_occupations": len(occupations["occupations"]),
            "balanced": balance["balanced"],
            "contract_amount_cents": balance["contract_amount_cents"],
            "paid_net_cents": balance["paid_net_cents"],
            "remaining_obligation_cents": balance["remaining_obligation_cents"],
            "audit_events": event_count,
            "audit_valid": valid,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"] and result["balanced"]
          and result["duplicate_blocked"] and result["correction_in_new_period"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
