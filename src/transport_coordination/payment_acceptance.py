"""运行进度支付核验服务的离线端到端验收。

场景覆盖：合同与清单登记、申报同事务的证据占用/累计量校验/资金份额拆分、
总包与分包共用同一验收单号被拦截、设计变更跨越资金来源的分层拆分、
质保金未满期拦截与条件满足后释放、关账后更正落入当前期间、审计追回，
最后核验合同金额、已付金额与剩余义务平衡。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import MutableClock
from .errors import ConflictError
from .service import DomainService
from .payment_service import PaymentService
from .storage import Database


def run() -> dict[str, object]:
    """执行完整业务链并返回可机读的验收结果。"""

    with tempfile.TemporaryDirectory() as directory:
        clock = MutableClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        database = Database(Path(directory) / "payment_acceptance.sqlite3")
        domain = DomainService(database, clock)
        service = PaymentService(domain)
        domain.register_organization(request_id="req-org", actor_id="bootstrap",
                                     organization_id="org-001", name="交通投资集团")
        domain.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                              display_name="系统管理员", role="admin", organization_id="org-001")
        domain.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="fin-001",
                              display_name="财务经办", role="operator", organization_id="org-001")
        domain.register_actor(request_id="req-reviewer", actor_id="admin-001", new_actor_id="rev-001",
                              display_name="会签审核", role="reviewer", organization_id="org-001")
        domain.register_actor(request_id="req-auditor", actor_id="admin-001", new_actor_id="aud-001",
                              display_name="审计员", role="auditor", organization_id="org-001")
        domain.register_site(request_id="req-site", actor_id="admin-001", site_id="site-001",
                             organization_id="org-001", name="绕城高速项目部",
                             timezone_name="Asia/Shanghai")

        # 总包合同：两项清单，中央/地方 60/40 出资，质保金 3%，需质量合格证明。
        service.register_contract(
            request_id="req-c1", actor_id="fin-001", contract_id="c-001", site_id="site-001",
            contract_no="HT-2026-001", name="绕城高速路基路面工程", contractor="总包一局",
            retention_rate_bp=300, defect_liability_end="2026-10-31",
            requires_quality_certificate=True,
            funding_sources=[{"source_code": "central", "ratio_bp": 6000},
                             {"source_code": "local", "ratio_bp": 4000}],
            items=[{"item_code": "I-1", "name": "路基填筑", "unit": "m3",
                    "unit_price_cents": 12000, "quantity_milli": 1000000},
                   {"item_code": "I-2", "name": "沥青路面", "unit": "m2",
                    "unit_price_cents": 30000, "quantity_milli": 2000000}])
        # 分包合同：同一批材料验收单号将被用来验证重复材料拦截。
        service.register_contract(
            request_id="req-c2", actor_id="fin-001", contract_id="c-002", site_id="site-001",
            contract_no="HT-2026-002", name="桥梁构件分包", contractor="分包三公司",
            retention_rate_bp=300, defect_liability_end="2026-10-31",
            requires_quality_certificate=False,
            funding_sources=[{"source_code": "local", "ratio_bp": 10000}],
            items=[{"item_code": "S-1", "name": "预制梁", "unit": "片",
                    "unit_price_cents": 500000, "quantity_milli": 500000}])

        service.register_acceptance(request_id="req-a1", actor_id="fin-001", acceptance_id="ac-001",
                                    contract_id="c-001", acceptance_no="AC-1001", item_code="I-1",
                                    quantity_milli=400000, accepted_on="2026-09-20",
                                    inspector="监理工程师甲", independent=True)
        service.register_invoice(request_id="req-i1", actor_id="fin-001", invoice_id="inv-001",
                                 contract_id="c-001", invoice_no="INV-1001",
                                 amount_cents=6000000, issued_on="2026-09-21")
        submitted = service.submit_application(
            request_id="req-app1", actor_id="fin-001", application_id="app-001",
            contract_id="c-001", lines=[{"acceptance_id": "ac-001", "invoice_id": "inv-001"}])
        replay = service.submit_application(
            request_id="req-app1", actor_id="fin-001", application_id="app-001",
            contract_id="c-001", lines=[{"acceptance_id": "ac-001", "invoice_id": "inv-001"}])

        # 分包用同一验收单号申报，被识别为重复材料进入待处理。
        service.register_acceptance(request_id="req-a2", actor_id="fin-001", acceptance_id="ac-101",
                                    contract_id="c-002", acceptance_no="AC-1001", item_code="S-1",
                                    quantity_milli=100000, accepted_on="2026-09-20",
                                    inspector="监理工程师甲", independent=True)
        service.register_invoice(request_id="req-i2", actor_id="fin-001", invoice_id="inv-101",
                                 contract_id="c-002", invoice_no="INV-2001",
                                 amount_cents=50000000, issued_on="2026-09-21")
        duplicate = service.submit_application(
            request_id="req-app3", actor_id="fin-001", application_id="app-003",
            contract_id="c-002", lines=[{"acceptance_id": "ac-101", "invoice_id": "inv-101"}])

        service.countersign_application(request_id="req-cs1", actor_id="rev-001",
                                        application_id="app-001")
        service.approve_application(request_id="req-ap1", actor_id="rev-001",
                                    application_id="app-001", approved_acceptance_ids=["ac-001"])
        payment = service.record_payment(request_id="req-pay1", actor_id="fin-001",
                                         application_id="app-001")

        # 设计变更：I-1 增加 200 m3，变更令资金为中央 25% / 地方 75%。
        service.register_change_order(
            request_id="req-co1", actor_id="fin-001", change_id="co-001", contract_id="c-001",
            change_no="BG-001", description="软基处理范围扩大",
            lines=[{"item_code": "I-1", "quantity_delta_milli": 200000}],
            funding_sources=[{"source_code": "central", "ratio_bp": 2500},
                             {"source_code": "local", "ratio_bp": 7500}])
        service.approve_change_order(request_id="req-co1a", actor_id="rev-001", change_id="co-001")
        service.register_acceptance(request_id="req-a3", actor_id="fin-001", acceptance_id="ac-002",
                                    contract_id="c-001", acceptance_no="AC-1002", item_code="I-1",
                                    quantity_milli=700000, accepted_on="2026-09-24",
                                    inspector="监理工程师乙", independent=True)
        service.register_invoice(request_id="req-i3", actor_id="fin-001", invoice_id="inv-002",
                                 contract_id="c-001", invoice_no="INV-1002",
                                 amount_cents=9000000, issued_on="2026-09-24")
        crossed = service.submit_application(
            request_id="req-app2", actor_id="fin-001", application_id="app-002",
            contract_id="c-001", lines=[{"acceptance_id": "ac-002", "invoice_id": "inv-002"}])

        # 缺陷责任期未满时释放质保金必须被拦截。
        blocked = False
        try:
            service.release_retention(request_id="req-rel0", actor_id="fin-001",
                                      contract_id="c-001", amount_cents=100000)
        except ConflictError:
            blocked = True

        # 进入 11 月：具备质量合格证明且责任期满，释放部分质保金。
        clock.set(datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc))
        service.register_quality_certificate(request_id="req-qc1", actor_id="fin-001",
                                             certificate_id="qc-001", contract_id="c-001",
                                             certificate_no="ZM-001", issued_on="2026-10-20")
        released = service.release_retention(request_id="req-rel1", actor_id="fin-001",
                                             contract_id="c-001", amount_cents=100000)

        # 关闭 9 月、10 月期间；对 9 月支付分录的更正只能落在当前 11 月。
        service.close_period(request_id="req-close1", actor_id="admin-001", period="2026-09")
        service.close_period(request_id="req-close2", actor_id="admin-001", period="2026-10")
        ledger = service.contract_ledger("c-001")
        payment_entry = next(entry for entry in ledger if entry["entry_type"] == "payment.recorded")
        correction = service.correct_entry(request_id="req-fix1", actor_id="fin-001",
                                           entry_id=payment_entry["entry_id"],
                                           amount_cents=-50000, reason="审计抽查核减重复计量")
        recovery = service.recover_payment(request_id="req-rec1", actor_id="aud-001",
                                           application_id="app-001", amount_cents=200000,
                                           reason="审计追回重复材料对应款项")

        balance = service.contract_balance("c-001")
        explained = service.explain_application("app-001")
        traced = service.trace_evidence("acceptance", "ac-001")
        valid, event_count = domain.verify_audit()
        original = next(entry for entry in service.contract_ledger("c-001")
                        if entry["entry_id"] == payment_entry["entry_id"])
        result = {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "submitted_split": submitted["result"]["funding_split"],
            "submit_replayed": replay["replayed"],
            "duplicate_status": duplicate["result"]["status"],
            "duplicate_reasons": [item["reason"] for item in duplicate["result"]["reasons"]],
            "paid_amount_cents": payment["result"]["paid_amount_cents"],
            "retention_withheld_cents": payment["result"]["retention_withheld_cents"],
            "crossed_split": crossed["result"]["funding_split"],
            "early_release_blocked": blocked,
            "released_amount_cents": released["result"]["released_amount_cents"],
            "correction_period": correction["result"]["correction_period"],
            "original_period": correction["result"]["original_period"],
            "original_entry_untouched": original["amount_cents"] == payment_entry["amount_cents"]
            and original["period"] == "2026-09",
            "recovered_amount_cents": recovery["result"]["recovered_amount_cents"],
            "app001_entries": len(explained["entries"]),
            "evidence_occupations": len(traced["occupations"]),
            "balanced": balance["balanced"],
            "balance": {key: balance[key] for key in (
                "contract_amount_cents", "approved_amount_cents", "cash_paid_cents",
                "retention_held_cents", "recovered_amount_cents", "unpaid_approved_cents",
                "remaining_obligation_cents")},
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"] and result["balanced"]
          and result["early_release_blocked"] and result["original_entry_untouched"]
          and result["duplicate_status"] == "pending")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
