"""建设项目进度支付核验服务的领域规则。

核心约束：
- 每笔支付申请在同一事务内完成证据占用、累计量校验和资金份额拆分；
- 重复材料、超合同计量或缺少独立验收的申请进入 pending（待处理），不生成付款；
- 撤回、驳回、部分批准和审计追回都以新的台账分录保留，历史分录不可改写；
- 会计期间关闭后，更正只能落在当前开放期间并通过 corrects_entry_id 引用原分录；
- 合同金额 = 净已付 + 质保金留置 + 应付未付 + 剩余义务，审计端点从台账重算验证。

金额约定：金额一律为整数分（cents），工程量为整数毫单位（milli，千分之一），
出资比例为基点（bp，万分之一），同一组出资比例合计必须为 10000。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

from .audit import append_event, canonical_json, digest, verify_chain
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor, WriteReceipt
from .storage import Database


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
ROLES = frozenset({"admin", "applicant", "inspector", "reviewer", "finance", "auditor"})
EVIDENCE_TYPES = frozenset({"material_acceptance", "site_acceptance", "invoice"})
LINE_EVIDENCE_TYPES = frozenset({"material_acceptance", "site_acceptance"})
ACTIVE_APPLICATION_STATUSES = ("submitted", "approved", "partially_approved", "paid")
FULL_BASIS_POINTS = 10000


class PaymentService:
    """协调权限、幂等、事务、台账和审计规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _positive_int(self, value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValidationError(f"{field} 必须是正整数")
        return value

    def _non_negative_int(self, value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationError(f"{field} 必须是非负整数")
        return value

    def _line_amount(self, quantity_milli: int, unit_price_cents: int) -> int:
        product = quantity_milli * unit_price_cents
        if product % 1000 != 0:
            raise ValidationError("数量与单价的乘积必须能精确到分")
        return product // 1000

    def _retention(self, amount_cents: int, rate_bp: int) -> int:
        return (amount_cents * rate_bp + 5000) // FULL_BASIS_POINTS

    def _split(self, total_cents: int, shares: list[tuple[str, int]]) -> dict[str, int]:
        """按基点比例用最大余数法拆分，保证各部分之和等于总额。"""

        if total_cents < 0:
            raise ValidationError("拆分金额不能为负")
        parts = []
        for source_id, ratio_bp in shares:
            floor, fraction = divmod(total_cents * ratio_bp, FULL_BASIS_POINTS)
            parts.append([source_id, floor, fraction])
        remainder = total_cents - sum(part[1] for part in parts)
        parts.sort(key=lambda part: (-part[2], part[0]))
        for index in range(remainder):
            parts[index][1] += 1
        return {part[0]: part[1] for part in parts}

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"], row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> tuple[WriteReceipt, dict[str, Any]]:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True), json.loads(row["response_json"])
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id, canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False), response

    def _current_period(self, connection, project_id: str):
        row = connection.execute(
            "SELECT * FROM periods WHERE project_id=? AND status='open'", (project_id,)
        ).fetchone()
        if row is None:
            raise ConflictError("项目没有开放的会计期间")
        return row

    def _latest_version(self, connection, contract_id: str):
        row = connection.execute(
            "SELECT * FROM quantity_versions WHERE contract_id=? ORDER BY version_no DESC LIMIT 1",
            (contract_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("合同没有工程量版本")
        return row

    def _version_quantities(self, connection, version_id: str) -> dict[str, int]:
        rows = connection.execute(
            "SELECT item_id, quantity_milli FROM quantity_version_items WHERE version_id=?", (version_id,)
        ).fetchall()
        return {row["item_id"]: row["quantity_milli"] for row in rows}

    def _reserved_quantity(self, connection, item_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(SUM(al.quantity_milli),0) AS total "
            "FROM application_lines al "
            "JOIN payment_applications a ON a.application_id = al.application_id "
            "WHERE al.item_id=? AND al.decision != 'rejected' "
            "AND a.status IN ('submitted','approved','partially_approved','paid')",
            (item_id,),
        ).fetchone()
        return row["total"]

    def _project_shares(self, connection, project_id: str) -> list[tuple[str, int]]:
        rows = connection.execute(
            "SELECT funding_source_id, ratio_bp FROM funding_sources WHERE project_id=? ORDER BY code",
            (project_id,),
        ).fetchall()
        return [(row["funding_source_id"], row["ratio_bp"]) for row in rows]

    def _add_ledger(self, connection, *, project_id: str, contract_id: str | None,
                    application_id: str | None, period_id: str, kind: str, amount_cents: int,
                    payload: dict[str, Any], actor_id: str, corrects_entry_id: str | None = None) -> str:
        entry_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO ledger_entries(entry_id,project_id,contract_id,application_id,period_id,kind,"
            "amount_cents,payload_json,corrects_entry_id,actor_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (entry_id, project_id, contract_id, application_id, period_id, kind, amount_cents,
             canonical_json(payload), corrects_entry_id, actor_id, self._now()),
        )
        return entry_id

    def _release_occupations(self, connection, application_id: str, reason: str,
                             evidence_ids: list[str] | None = None) -> list[str]:
        query = ("SELECT * FROM evidence_occupations WHERE application_id=? AND released_at IS NULL")
        parameters: list[Any] = [application_id]
        rows = connection.execute(query, parameters).fetchall()
        released = []
        wanted = set(evidence_ids) if evidence_ids is not None else None
        for row in rows:
            if wanted is not None and row["evidence_id"] not in wanted:
                continue
            connection.execute(
                "UPDATE evidence_occupations SET released_at=?, release_reason=? WHERE occupation_id=?",
                (self._now(), reason, row["occupation_id"]),
            )
            released.append(row["evidence_id"])
        return released

    def _application_money(self, connection, application_id: str) -> dict[str, int]:
        totals: dict[str, int] = {}
        for row in connection.execute(
            "SELECT kind, COALESCE(SUM(amount_cents),0) AS total FROM ledger_entries "
            "WHERE application_id=? AND kind IN ('application.approved','application.partially_approved',"
            "'retention.withheld','retention.released','payment.made','audit.recovered') "
            "GROUP BY kind",
            (application_id,),
        ):
            totals[row["kind"]] = row["total"]
        approved_gross = totals.get("application.approved", 0) + totals.get("application.partially_approved", 0)
        withheld = totals.get("retention.withheld", 0)
        released = totals.get("retention.released", 0)
        paid = totals.get("payment.made", 0)
        recovered = totals.get("audit.recovered", 0)
        approved_net = approved_gross - withheld
        return {
            "approved_gross_cents": approved_gross,
            "approved_net_cents": approved_net,
            "retention_withheld_cents": withheld,
            "retention_released_cents": released,
            "retention_held_cents": withheld - released,
            "payments_cents": paid,
            "recoveries_cents": recovered,
            "paid_net_cents": paid - recovered,
            "outstanding_cents": approved_net + released - paid,
        }

    # ------------------------------------------------------------------
    # 组织与操作者
    # ------------------------------------------------------------------

    def register_organization(self, *, request_id: str, actor_id: str,
                              organization_id: str, name: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "organization_id": organization_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            existing_actors = connection.execute("SELECT COUNT(*) AS count FROM actors").fetchone()["count"]
            if existing_actors:
                actor = self._actor(connection, actor_id)
                self._require(actor, "admin")
            elif actor_id != "bootstrap":
                raise PermissionDenied("首次建档必须使用 bootstrap")
            organization_id = self._identifier(organization_id, "organization_id")
            name = self._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO organizations(organization_id,name,created_at) VALUES(?,?,?)",
                        (organization_id, name, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("组织编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="organization.registered",
                             resource_type="organization", resource_id=organization_id,
                             detail={"name": name}, occurred_at=self._now())
                return "organization", organization_id, {"organization_id": organization_id}

            receipt, _ = self._idempotent(connection, request_id=request_id,
                                          action="register_organization", payload=payload, create=create)
            return receipt

    def register_actor(self, *, request_id: str, actor_id: str, new_actor_id: str,
                       display_name: str, role: str, organization_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "new_actor_id": new_actor_id, "display_name": display_name,
                   "role": role, "organization_id": organization_id}
        with self.database.transaction(immediate=True) as connection:
            count = connection.execute("SELECT COUNT(*) AS count FROM actors").fetchone()["count"]
            if count:
                actor = self._actor(connection, actor_id)
                self._require(actor, "admin")
            elif actor_id != "bootstrap":
                raise PermissionDenied("首位管理员必须由 bootstrap 创建")
            new_actor_id = self._identifier(new_actor_id, "new_actor_id")
            display_name = self._text(display_name, "display_name")
            if role not in ROLES:
                raise ValidationError("role 不在允许范围内")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?", (organization_id,)).fetchone() is None:
                raise NotFoundError("组织不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO actors(actor_id,display_name,role,organization_id,active,created_at) VALUES(?,?,?,?,1,?)",
                        (new_actor_id, display_name, role, organization_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("操作者编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="actor.registered",
                             resource_type="actor", resource_id=new_actor_id,
                             detail={"display_name": display_name, "role": role, "organization_id": organization_id},
                             occurred_at=self._now())
                return "actor", new_actor_id, {"actor_id": new_actor_id}

            receipt, _ = self._idempotent(connection, request_id=request_id,
                                          action="register_actor", payload=payload, create=create)
            return receipt

    # ------------------------------------------------------------------
    # 项目、合同、变更令与会计期间
    # ------------------------------------------------------------------

    def create_project(self, *, request_id: str, actor_id: str, project_id: str,
                       organization_id: str, code: str, name: str,
                       funding_sources: list[dict[str, Any]]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "project_id": project_id, "organization_id": organization_id,
                   "code": code, "name": name, "funding_sources": funding_sources}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?", (organization_id,)).fetchone() is None:
                raise NotFoundError("组织不存在")
            project_id = self._identifier(project_id, "project_id")
            code = self._identifier(code, "code")
            name = self._text(name, "name")
            if not isinstance(funding_sources, list) or not funding_sources:
                raise ValidationError("funding_sources 必须是非空数组")
            total_bp = 0
            seen_codes: set[str] = set()
            for source in funding_sources:
                source_code = self._identifier(source.get("code", ""), "funding_sources.code")
                self._text(source.get("name", ""), "funding_sources.name")
                ratio_bp = self._positive_int(source.get("ratio_bp"), "funding_sources.ratio_bp")
                if source_code in seen_codes:
                    raise ValidationError("出资来源代码重复")
                seen_codes.add(source_code)
                total_bp += ratio_bp
            if total_bp != FULL_BASIS_POINTS:
                raise ValidationError("共同出资比例合计必须等于 10000 基点")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO projects(project_id,organization_id,code,name,created_at) VALUES(?,?,?,?,?)",
                        (project_id, organization_id, code, name, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("项目编号或代码已经存在") from exc
                for source in funding_sources:
                    connection.execute(
                        "INSERT INTO funding_sources(funding_source_id,project_id,code,name,ratio_bp) VALUES(?,?,?,?,?)",
                        (uuid.uuid4().hex, project_id, source["code"].strip(),
                         source["name"].strip(), source["ratio_bp"]),
                    )
                connection.execute(
                    "INSERT INTO periods(period_id,project_id,name,status,opened_at) VALUES(?,?,?,?,?)",
                    (uuid.uuid4().hex, project_id, "P-0001", "open", self._now()),
                )
                append_event(connection, actor_id=actor_id, action="project.created",
                             resource_type="project", resource_id=project_id,
                             detail={"code": code, "name": name,
                                     "funding_sources": [{"code": s["code"], "ratio_bp": s["ratio_bp"]} for s in funding_sources]},
                             occurred_at=self._now())
                return "project", project_id, {"project_id": project_id}

            receipt, _ = self._idempotent(connection, request_id=request_id,
                                          action="create_project", payload=payload, create=create)
            return receipt

    def create_contract(self, *, request_id: str, actor_id: str, contract_id: str,
                        project_id: str, code: str, name: str, contractor_name: str,
                        retention_rate_bp: int, retention_release_after: str,
                        boq_items: list[dict[str, Any]]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "contract_id": contract_id, "project_id": project_id,
                   "code": code, "name": name, "contractor_name": contractor_name,
                   "retention_rate_bp": retention_rate_bp, "retention_release_after": retention_release_after,
                   "boq_items": boq_items}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            if connection.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone() is None:
                raise NotFoundError("项目不存在")
            contract_id = self._identifier(contract_id, "contract_id")
            code = self._identifier(code, "code")
            name = self._text(name, "name")
            contractor_name = self._text(contractor_name, "contractor_name")
            retention_rate_bp = self._non_negative_int(retention_rate_bp, "retention_rate_bp")
            if retention_rate_bp > FULL_BASIS_POINTS:
                raise ValidationError("retention_rate_bp 不能超过 10000")
            retention_release_after = str(retention_release_after).strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", retention_release_after):
                raise ValidationError("retention_release_after 必须是 YYYY-MM-DD 日期")
            if not isinstance(boq_items, list) or not boq_items:
                raise ValidationError("boq_items 必须是非空数组")
            seen_item_codes: set[str] = set()
            for item in boq_items:
                item_code = self._identifier(item.get("item_code", ""), "boq_items.item_code")
                if item_code in seen_item_codes:
                    raise ValidationError("清单项代码重复")
                seen_item_codes.add(item_code)
                self._text(item.get("name", ""), "boq_items.name")
                self._text(item.get("unit", ""), "boq_items.unit", 20)
                price = self._non_negative_int(item.get("unit_price_cents"), "boq_items.unit_price_cents")
                quantity = self._positive_int(item.get("quantity_milli"), "boq_items.quantity_milli")
                self._line_amount(quantity, price)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO contracts(contract_id,project_id,code,name,contractor_name,"
                        "retention_rate_bp,retention_release_after,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (contract_id, project_id, code, name, contractor_name,
                         retention_rate_bp, retention_release_after, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("合同编号或代码已经存在") from exc
                version_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO quantity_versions(version_id,contract_id,version_no,change_order_id,created_by,created_at) "
                    "VALUES(?,?,1,NULL,?,?)",
                    (version_id, contract_id, actor_id, self._now()),
                )
                for item in boq_items:
                    item_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO boq_items(item_id,contract_id,item_code,name,unit,unit_price_cents,"
                        "base_quantity_milli,origin,change_order_id) VALUES(?,?,?,?,?,?,?,'base',NULL)",
                        (item_id, contract_id, item["item_code"].strip(), item["name"].strip(),
                         item["unit"].strip(), item["unit_price_cents"], item["quantity_milli"]),
                    )
                    connection.execute(
                        "INSERT INTO quantity_version_items(version_id,item_id,quantity_milli) VALUES(?,?,?)",
                        (version_id, item_id, item["quantity_milli"]),
                    )
                append_event(connection, actor_id=actor_id, action="contract.created",
                             resource_type="contract", resource_id=contract_id,
                             detail={"project_id": project_id, "code": code, "name": name,
                                     "retention_rate_bp": retention_rate_bp,
                                     "retention_release_after": retention_release_after,
                                     "boq_item_count": len(boq_items)},
                             occurred_at=self._now())
                return "contract", contract_id, {"contract_id": contract_id}

            receipt, _ = self._idempotent(connection, request_id=request_id,
                                          action="create_contract", payload=payload, create=create)
            return receipt

    def create_change_order(self, *, request_id: str, actor_id: str, change_order_id: str,
                            contract_id: str, code: str, reason: str,
                            funding_shares: list[dict[str, Any]],
                            adjustments: list[dict[str, Any]] | None = None,
                            new_items: list[dict[str, Any]] | None = None) -> WriteReceipt:
        adjustments = adjustments or []
        new_items = new_items or []
        payload = {"actor_id": actor_id, "change_order_id": change_order_id, "contract_id": contract_id,
                   "code": code, "reason": reason, "funding_shares": funding_shares,
                   "adjustments": adjustments, "new_items": new_items}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            contract = connection.execute("SELECT * FROM contracts WHERE contract_id=?", (contract_id,)).fetchone()
            if contract is None:
                raise NotFoundError("合同不存在")
            change_order_id = self._identifier(change_order_id, "change_order_id")
            code = self._identifier(code, "code")
            reason = self._text(reason, "reason", 500)
            if not adjustments and not new_items:
                raise ValidationError("变更令必须至少包含一项工程量调整或新增清单项")
            shares = self._validate_funding_shares(connection, contract["project_id"], funding_shares)
            for adjustment in adjustments:
                item = connection.execute("SELECT * FROM boq_items WHERE item_id=?",
                                          (adjustment.get("item_id", ""),)).fetchone()
                if item is None or item["contract_id"] != contract_id:
                    raise NotFoundError("调整的清单项不存在或不属于该合同")
                delta = adjustment.get("delta_quantity_milli")
                if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0:
                    raise ValidationError("delta_quantity_milli 必须是非零整数")
            seen_new_codes: set[str] = set()
            for item in new_items:
                item_code = self._identifier(item.get("item_code", ""), "new_items.item_code")
                if item_code in seen_new_codes:
                    raise ValidationError("新增清单项代码重复")
                seen_new_codes.add(item_code)
                if connection.execute("SELECT 1 FROM boq_items WHERE contract_id=? AND item_code=?",
                                      (contract_id, item_code)).fetchone():
                    raise ConflictError("新增清单项代码与既有清单冲突")
                self._text(item.get("name", ""), "new_items.name")
                self._text(item.get("unit", ""), "new_items.unit", 20)
                price = self._non_negative_int(item.get("unit_price_cents"), "new_items.unit_price_cents")
                quantity = self._positive_int(item.get("quantity_milli"), "new_items.quantity_milli")
                self._line_amount(quantity, price)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO change_orders(change_order_id,contract_id,code,reason,status,funding_json,"
                        "created_by,created_at) VALUES(?,?,?,?,'draft',?,?,?)",
                        (change_order_id, contract_id, code, reason,
                         canonical_json([{"funding_source_id": sid, "ratio_bp": bp} for sid, bp in shares]),
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("变更令编号或代码已经存在") from exc
                for adjustment in adjustments:
                    connection.execute(
                        "INSERT INTO change_order_adjustments(change_order_id,item_id,delta_quantity_milli) VALUES(?,?,?)",
                        (change_order_id, adjustment["item_id"], adjustment["delta_quantity_milli"]),
                    )
                for item in new_items:
                    connection.execute(
                        "INSERT INTO change_order_new_items(change_order_id,item_code,name,unit,unit_price_cents,quantity_milli) "
                        "VALUES(?,?,?,?,?,?)",
                        (change_order_id, item["item_code"].strip(), item["name"].strip(), item["unit"].strip(),
                         item["unit_price_cents"], item["quantity_milli"]),
                    )
                append_event(connection, actor_id=actor_id, action="change_order.created",
                             resource_type="change_order", resource_id=change_order_id,
                             detail={"contract_id": contract_id, "code": code,
                                     "adjustments": len(adjustments), "new_items": len(new_items)},
                             occurred_at=self._now())
                return "change_order", change_order_id, {"change_order_id": change_order_id}

            receipt, _ = self._idempotent(connection, request_id=request_id,
                                          action="create_change_order", payload=payload, create=create)
            return receipt

    def _validate_funding_shares(self, connection, project_id: str,
                                 funding_shares: list[dict[str, Any]]) -> list[tuple[str, int]]:
        if not isinstance(funding_shares, list) or not funding_shares:
            raise ValidationError("funding_shares 必须是非空数组")
        project_sources = {row["funding_source_id"] for row in connection.execute(
            "SELECT funding_source_id FROM funding_sources WHERE project_id=?", (project_id,))}
        shares: list[tuple[str, int]] = []
        seen: set[str] = set()
        total_bp = 0
        for share in funding_shares:
            source_id = str(share.get("funding_source_id", "")).strip()
            if source_id not in project_sources:
                raise ValidationError("funding_shares 引用了不属于该项目的出资来源")
            if source_id in seen:
                raise ValidationError("funding_shares 中出资来源重复")
            seen.add(source_id)
            ratio_bp = self._positive_int(share.get("ratio_bp"), "funding_shares.ratio_bp")
            total_bp += ratio_bp
            shares.append((source_id, ratio_bp))
        if total_bp != FULL_BASIS_POINTS:
            raise ValidationError("变更令出资比例合计必须等于 10000 基点")
        shares.sort(key=lambda share: share[0])
        return shares

    def approve_change_order(self, *, request_id: str, actor_id: str, change_order_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "change_order_id": change_order_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            order = connection.execute("SELECT * FROM change_orders WHERE change_order_id=?",
                                       (change_order_id,)).fetchone()
            if order is None:
                raise NotFoundError("变更令不存在")
            if order["status"] != "draft":
                raise ConflictError("变更令已经审批")
            contract_id = order["contract_id"]

            def create() -> tuple[str, str, dict[str, Any]]:
                latest = self._latest_version(connection, contract_id)
                quantities = self._version_quantities(connection, latest["version_id"])
                adjustments = connection.execute(
                    "SELECT * FROM change_order_adjustments WHERE change_order_id=?", (change_order_id,)).fetchall()
                for adjustment in adjustments:
                    item_id = adjustment["item_id"]
                    item = connection.execute("SELECT * FROM boq_items WHERE item_id=?", (item_id,)).fetchone()
                    new_quantity = quantities.get(item_id, 0) + adjustment["delta_quantity_milli"]
                    if new_quantity < 0:
                        raise ValidationError("变更后工程量不能为负")
                    if new_quantity < self._reserved_quantity(connection, item_id):
                        raise ValidationError("变更后工程量不得低于已被申请占用的累计量")
                    self._line_amount(new_quantity, item["unit_price_cents"])
                    quantities[item_id] = new_quantity
                new_item_rows = connection.execute(
                    "SELECT * FROM change_order_new_items WHERE change_order_id=?", (change_order_id,)).fetchall()
                new_version_id = uuid.uuid4().hex
                new_version_no = latest["version_no"] + 1
                connection.execute(
                    "INSERT INTO quantity_versions(version_id,contract_id,version_no,change_order_id,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (new_version_id, contract_id, new_version_no, change_order_id, actor_id, self._now()),
                )
                for item_id, quantity in quantities.items():
                    connection.execute(
                        "INSERT INTO quantity_version_items(version_id,item_id,quantity_milli) VALUES(?,?,?)",
                        (new_version_id, item_id, quantity),
                    )
                for new_item in new_item_rows:
                    item_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO boq_items(item_id,contract_id,item_code,name,unit,unit_price_cents,"
                        "base_quantity_milli,origin,change_order_id) VALUES(?,?,?,?,?,?,?,'change_order',?)",
                        (item_id, contract_id, new_item["item_code"], new_item["name"], new_item["unit"],
                         new_item["unit_price_cents"], new_item["quantity_milli"], change_order_id),
                    )
                    connection.execute(
                        "INSERT INTO quantity_version_items(version_id,item_id,quantity_milli) VALUES(?,?,?)",
                        (new_version_id, item_id, new_item["quantity_milli"]),
                    )
                connection.execute(
                    "UPDATE change_orders SET status='approved', approved_by=?, approved_at=? WHERE change_order_id=?",
                    (actor_id, self._now(), change_order_id),
                )
                append_event(connection, actor_id=actor_id, action="change_order.approved",
                             resource_type="change_order", resource_id=change_order_id,
                             detail={"contract_id": contract_id, "version_no": new_version_no},
                             occurred_at=self._now())
                return "change_order", change_order_id, {"change_order_id": change_order_id,
                                                         "version_no": new_version_no}

            receipt, _ = self._idempotent(connection, request_id=request_id,
                                          action="approve_change_order", payload=payload, create=create)
            return receipt

    def close_period(self, *, request_id: str, actor_id: str, project_id: str,
                     new_period_name: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "project_id": project_id, "new_period_name": new_period_name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            if connection.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone() is None:
                raise NotFoundError("项目不存在")
            new_period_name = self._text(new_period_name, "new_period_name", 80)
            current = self._current_period(connection, project_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE periods SET status='closed', closed_at=?, closed_by=? WHERE period_id=?",
                    (self._now(), actor_id, current["period_id"]),
                )
                new_period_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO periods(period_id,project_id,name,status,opened_at) VALUES(?,?,?,?,?)",
                        (new_period_id, project_id, new_period_name, "open", self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("新期间名称已经存在") from exc
                append_event(connection, actor_id=actor_id, action="period.closed",
                             resource_type="period", resource_id=current["period_id"],
                             detail={"project_id": project_id, "closed_period": current["name"],
                                     "new_period": new_period_name},
                             occurred_at=self._now())
                return "period", new_period_id, {"period_id": new_period_id,
                                                 "closed_period_id": current["period_id"]}

            receipt, _ = self._idempotent(connection, request_id=request_id,
                                          action="close_period", payload=payload, create=create)
            return receipt

    # ------------------------------------------------------------------
    # 证据登记
    # ------------------------------------------------------------------

    def register_evidence(self, *, request_id: str, actor_id: str, project_id: str,
                          evidence_type: str, external_key: str, boq_item_id: str | None = None,
                          quantity_milli: int | None = None, amount_cents: int | None = None,
                          detail: dict[str, Any] | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "project_id": project_id, "evidence_type": evidence_type,
                   "external_key": external_key, "boq_item_id": boq_item_id,
                   "quantity_milli": quantity_milli, "amount_cents": amount_cents,
                   "detail": detail or {}}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "applicant", "inspector", "reviewer")
            if connection.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone() is None:
                raise NotFoundError("项目不存在")
            if evidence_type not in EVIDENCE_TYPES:
                raise ValidationError("evidence_type 不在允许范围内")
            external_key = self._identifier(external_key, "external_key")
            if boq_item_id is not None:
                item = connection.execute(
                    "SELECT i.item_id FROM boq_items i JOIN contracts c ON c.contract_id = i.contract_id "
                    "WHERE i.item_id=? AND c.project_id=?", (boq_item_id, project_id)).fetchone()
                if item is None:
                    raise NotFoundError("清单项不存在或不属于该项目")
            if quantity_milli is not None:
                quantity_milli = self._non_negative_int(quantity_milli, "quantity_milli")
            if amount_cents is not None:
                amount_cents = self._non_negative_int(amount_cents, "amount_cents")
            independent = 1 if actor.role in ("inspector", "reviewer", "admin") else 0

            def create() -> tuple[str, str, dict[str, Any]]:
                evidence_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO evidence(evidence_id,project_id,evidence_type,external_key,independent,"
                        "boq_item_id,quantity_milli,amount_cents,detail_json,registered_by,registered_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (evidence_id, project_id, evidence_type, external_key, independent,
                         boq_item_id, quantity_milli, amount_cents,
                         canonical_json(detail or {}), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("同一项目下相同类型与单号的证据已经登记") from exc
                append_event(connection, actor_id=actor_id, action="evidence.registered",
                             resource_type="evidence", resource_id=evidence_id,
                             detail={"project_id": project_id, "evidence_type": evidence_type,
                                     "external_key": external_key, "independent": bool(independent)},
                             occurred_at=self._now())
                return "evidence", evidence_id, {"evidence_id": evidence_id, "independent": bool(independent)}

            receipt, _ = self._idempotent(connection, request_id=request_id,
                                          action="register_evidence", payload=payload, create=create)
            return receipt

    # ------------------------------------------------------------------
    # 支付申请：同一事务内完成证据占用、累计量校验和资金份额拆分
    # ------------------------------------------------------------------

    def submit_payment_application(self, *, request_id: str, actor_id: str, contract_id: str,
                                   lines: list[dict[str, Any]],
                                   invoice_ids: list[str] | None = None) -> dict[str, Any]:
        invoice_ids = invoice_ids or []
        payload = {"actor_id": actor_id, "contract_id": contract_id,
                   "lines": lines, "invoice_ids": invoice_ids}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "applicant", "admin")
            contract = connection.execute("SELECT * FROM contracts WHERE contract_id=?", (contract_id,)).fetchone()
            if contract is None:
                raise NotFoundError("合同不存在")
            project_id = contract["project_id"]
            period = self._current_period(connection, project_id)
            version = self._latest_version(connection, contract_id)
            version_quantities = self._version_quantities(connection, version["version_id"])
            project_shares = self._project_shares(connection, project_id)
            if not isinstance(lines, list) or not lines:
                raise ValidationError("lines 必须是非空数组")

            prepared_lines: list[dict[str, Any]] = []
            reasons: list[dict[str, Any]] = []
            for index, line in enumerate(lines):
                item = connection.execute("SELECT * FROM boq_items WHERE item_id=?",
                                          (line.get("item_id", ""),)).fetchone()
                if item is None or item["contract_id"] != contract_id:
                    raise NotFoundError(f"第 {index + 1} 行清单项不存在或不属于该合同")
                quantity_milli = self._positive_int(line.get("quantity_milli"), "lines.quantity_milli")
                amount_cents = self._line_amount(quantity_milli, item["unit_price_cents"])
                evidence_ids = line.get("evidence_ids") or []
                if not isinstance(evidence_ids, list) or not evidence_ids:
                    raise ValidationError(f"第 {index + 1} 行必须引用验收证据")
                change_order_id = line.get("change_order_id")
                line_shares = project_shares
                if change_order_id is not None:
                    order = connection.execute("SELECT * FROM change_orders WHERE change_order_id=?",
                                               (change_order_id,)).fetchone()
                    if order is None or order["contract_id"] != contract_id:
                        raise NotFoundError("变更令不存在或不属于该合同")
                    if order["status"] != "approved":
                        raise ValidationError("引用的变更令尚未批准")
                    touched = connection.execute(
                        "SELECT 1 FROM change_order_adjustments WHERE change_order_id=? AND item_id=? "
                        "UNION SELECT 1 FROM change_order_new_items ni "
                        "JOIN boq_items i ON i.change_order_id = ni.change_order_id AND i.item_code = ni.item_code "
                        "WHERE ni.change_order_id=? AND i.item_id=?",
                        (change_order_id, item["item_id"], change_order_id, item["item_id"])).fetchone()
                    if touched is None:
                        raise ValidationError("申请行引用的变更令未涉及该清单项")
                    line_shares = [(share["funding_source_id"], share["ratio_bp"])
                                   for share in json.loads(order["funding_json"])]
                prepared_lines.append({
                    "item": item, "quantity_milli": quantity_milli, "amount_cents": amount_cents,
                    "retention_cents": self._retention(amount_cents, contract["retention_rate_bp"]),
                    "evidence_ids": list(dict.fromkeys(evidence_ids)),
                    "change_order_id": change_order_id, "shares": line_shares,
                })

            invoices: list[Any] = []
            for invoice_id in dict.fromkeys(invoice_ids):
                invoice = connection.execute("SELECT * FROM evidence WHERE evidence_id=?", (invoice_id,)).fetchone()
                if invoice is None or invoice["project_id"] != project_id:
                    raise NotFoundError("发票证据不存在或不属于该项目")
                if invoice["evidence_type"] != "invoice":
                    raise ValidationError("invoice_ids 必须引用发票类型证据")
                invoices.append(invoice)

            # 证据占用与独立验收校验（同一事务内预检，随后统一占用）
            seen_evidence: dict[str, Any] = {}
            for prepared in prepared_lines:
                has_independent_acceptance = False
                for evidence_id in prepared["evidence_ids"]:
                    if evidence_id not in seen_evidence:
                        row = connection.execute("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
                        if row is None or row["project_id"] != project_id:
                            raise NotFoundError("验收证据不存在或不属于该项目")
                        if row["evidence_type"] not in LINE_EVIDENCE_TYPES:
                            raise ValidationError("申请行只能引用材料验收单或现场验收记录")
                        seen_evidence[evidence_id] = row
                    evidence_row = seen_evidence[evidence_id]
                    if evidence_row["evidence_type"] == "site_acceptance" and evidence_row["independent"]:
                        has_independent_acceptance = True
                    occupied = connection.execute(
                        "SELECT application_id FROM evidence_occupations "
                        "WHERE evidence_id=? AND released_at IS NULL", (evidence_id,)).fetchone()
                    if occupied is not None:
                        reasons.append({"reason": "duplicate_evidence", "evidence_id": evidence_id,
                                        "occupied_by": occupied["application_id"]})
                if not has_independent_acceptance:
                    reasons.append({"reason": "missing_independent_acceptance",
                                    "item_id": prepared["item"]["item_id"]})
            for invoice in invoices:
                occupied = connection.execute(
                    "SELECT application_id FROM evidence_occupations "
                    "WHERE evidence_id=? AND released_at IS NULL", (invoice["evidence_id"],)).fetchone()
                if occupied is not None:
                    reasons.append({"reason": "duplicate_evidence", "evidence_id": invoice["evidence_id"],
                                    "occupied_by": occupied["application_id"]})

            # 累计量校验：已占用量 + 本次申请量 不得超过当前工程量版本
            for prepared in prepared_lines:
                item_id = prepared["item"]["item_id"]
                available = version_quantities.get(item_id, 0) - self._reserved_quantity(connection, item_id)
                if prepared["quantity_milli"] > available:
                    reasons.append({"reason": "quantity_exceeds_contract", "item_id": item_id,
                                    "requested_milli": prepared["quantity_milli"],
                                    "available_milli": max(available, 0)})

            gross_cents = sum(line["amount_cents"] for line in prepared_lines)
            retention_cents = sum(line["retention_cents"] for line in prepared_lines)
            net_cents = gross_cents - retention_cents

            def create() -> tuple[str, str, dict[str, Any]]:
                application_id = uuid.uuid4().hex
                status = "pending" if reasons else "submitted"
                connection.execute(
                    "INSERT INTO payment_applications(application_id,project_id,contract_id,period_id,"
                    "quantity_version_id,status,pending_reasons_json,gross_cents,retention_cents,net_cents,"
                    "submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (application_id, project_id, contract_id, period["period_id"], version["version_id"],
                     status, canonical_json(reasons), gross_cents, retention_cents, net_cents,
                     actor_id, self._now()),
                )
                for prepared in prepared_lines:
                    line_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO application_lines(line_id,application_id,item_id,change_order_id,"
                        "quantity_milli,unit_price_cents,amount_cents,retention_cents,decision) "
                        "VALUES(?,?,?,?,?,?,?,?,'pending')",
                        (line_id, application_id, prepared["item"]["item_id"], prepared["change_order_id"],
                         prepared["quantity_milli"], prepared["item"]["unit_price_cents"],
                         prepared["amount_cents"], prepared["retention_cents"]),
                    )
                    for evidence_id in prepared["evidence_ids"]:
                        connection.execute(
                            "INSERT INTO application_line_evidence(line_id,evidence_id) VALUES(?,?)",
                            (line_id, evidence_id),
                        )
                for invoice in invoices:
                    connection.execute(
                        "INSERT INTO application_invoices(application_id,evidence_id) VALUES(?,?)",
                        (application_id, invoice["evidence_id"]),
                    )
                response: dict[str, Any] = {
                    "application_id": application_id, "status": status, "reasons": reasons,
                    "gross_cents": gross_cents, "retention_cents": retention_cents,
                    "net_cents": net_cents, "period": period["name"],
                    "quantity_version_no": version["version_no"],
                }
                if reasons:
                    self._add_ledger(connection, project_id=project_id, contract_id=contract_id,
                                     application_id=application_id, period_id=period["period_id"],
                                     kind="application.pending", amount_cents=gross_cents,
                                     payload={"reasons": reasons, "net_cents": net_cents}, actor_id=actor_id)
                    append_event(connection, actor_id=actor_id, action="payment_application.pending",
                                 resource_type="payment_application", resource_id=application_id,
                                 detail={"contract_id": contract_id, "reasons": reasons},
                                 occurred_at=self._now())
                    return "payment_application", application_id, response

                # 证据占用：同一批材料/验收单/发票只能被一笔有效申请占用
                occupied_ids = list(dict.fromkeys(
                    [eid for prepared in prepared_lines for eid in prepared["evidence_ids"]]
                    + [invoice["evidence_id"] for invoice in invoices]))
                for evidence_id in occupied_ids:
                    connection.execute(
                        "INSERT INTO evidence_occupations(occupation_id,evidence_id,application_id,occupied_at) "
                        "VALUES(?,?,?,?)",
                        (uuid.uuid4().hex, evidence_id, application_id, self._now()),
                    )
                # 资金份额拆分：按行适用的出资比例（变更令行用变更令比例）拆分净额
                planned: dict[str, int] = {}
                for prepared in prepared_lines:
                    line_net = prepared["amount_cents"] - prepared["retention_cents"]
                    for source_id, amount in self._split(line_net, prepared["shares"]).items():
                        planned[source_id] = planned.get(source_id, 0) + amount
                for source_id, amount in planned.items():
                    connection.execute(
                        "INSERT INTO funding_allocations(allocation_id,application_id,funding_source_id,stage,"
                        "amount_cents,created_at) VALUES(?,?,?,'planned',?,?)",
                        (uuid.uuid4().hex, application_id, source_id, amount, self._now()),
                    )
                self._add_ledger(connection, project_id=project_id, contract_id=contract_id,
                                 application_id=application_id, period_id=period["period_id"],
                                 kind="application.submitted", amount_cents=gross_cents,
                                 payload={"net_cents": net_cents, "retention_cents": retention_cents,
                                          "quantity_version_no": version["version_no"],
                                          "evidence_ids": occupied_ids,
                                          "planned_funding": planned},
                                 actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action="payment_application.submitted",
                             resource_type="payment_application", resource_id=application_id,
                             detail={"contract_id": contract_id, "gross_cents": gross_cents,
                                     "net_cents": net_cents, "period": period["name"]},
                             occurred_at=self._now())
                response["planned_funding"] = planned
                return "payment_application", application_id, response

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="submit_payment_application",
                                                 payload=payload, create=create)
            return {"request_id": receipt.request_id, "replayed": receipt.replayed, **response}

    # ------------------------------------------------------------------
    # 撤回与会签（驳回、部分批准都以新分录保留）
    # ------------------------------------------------------------------

    def withdraw_application(self, *, request_id: str, actor_id: str, application_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "application_id": application_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            application = connection.execute("SELECT * FROM payment_applications WHERE application_id=?",
                                             (application_id,)).fetchone()
            if application is None:
                raise NotFoundError("支付申请不存在")
            self._require(actor, "applicant", "admin")
            if actor.role != "admin" and application["submitted_by"] != actor_id:
                raise PermissionDenied("只能撤回本人提交的申请")
            if application["status"] not in ("pending", "submitted"):
                raise ConflictError("当前状态不允许撤回")

            def create() -> tuple[str, str, dict[str, Any]]:
                period = self._current_period(connection, application["project_id"])
                released = self._release_occupations(connection, application_id, "withdrawn")
                connection.execute(
                    "UPDATE payment_applications SET status='withdrawn' WHERE application_id=?",
                    (application_id,),
                )
                self._add_ledger(connection, project_id=application["project_id"],
                                 contract_id=application["contract_id"], application_id=application_id,
                                 period_id=period["period_id"], kind="application.withdrawn",
                                 amount_cents=0, payload={"released_evidence_ids": released},
                                 actor_id=actor_id)
                if released:
                    self._add_ledger(connection, project_id=application["project_id"],
                                     contract_id=application["contract_id"], application_id=application_id,
                                     period_id=period["period_id"], kind="evidence.released",
                                     amount_cents=0, payload={"evidence_ids": released, "reason": "withdrawn"},
                                     actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action="payment_application.withdrawn",
                             resource_type="payment_application", resource_id=application_id,
                             detail={"released_evidence_ids": released}, occurred_at=self._now())
                return "payment_application", application_id, {"application_id": application_id,
                                                               "status": "withdrawn"}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="withdraw_application", payload=payload, create=create)
            return {"request_id": receipt.request_id, "replayed": receipt.replayed, **response}

    def review_application(self, *, request_id: str, actor_id: str, application_id: str,
                           decision: str, approved_line_ids: list[str] | None = None,
                           comment: str | None = None) -> dict[str, Any]:
        approved_line_ids = approved_line_ids or []
        payload = {"actor_id": actor_id, "application_id": application_id, "decision": decision,
                   "approved_line_ids": approved_line_ids, "comment": comment}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer", "admin")
            application = connection.execute("SELECT * FROM payment_applications WHERE application_id=?",
                                             (application_id,)).fetchone()
            if application is None:
                raise NotFoundError("支付申请不存在")
            if application["status"] != "submitted":
                raise ConflictError("只有待会签的申请可以会签")
            if decision not in ("approve", "partial", "reject"):
                raise ValidationError("decision 必须是 approve、partial 或 reject")
            lines = connection.execute(
                "SELECT * FROM application_lines WHERE application_id=?", (application_id,)).fetchall()
            line_ids = {line["line_id"] for line in lines}
            approved_set = set(approved_line_ids)
            if not approved_set.issubset(line_ids):
                raise ValidationError("approved_line_ids 包含不属于该申请的行")
            if decision == "approve":
                approved_set = set(line_ids)
            elif decision == "reject":
                approved_set = set()
            elif not approved_set:
                raise ValidationError("部分批准必须给出 approved_line_ids")

            def create() -> tuple[str, str, dict[str, Any]]:
                period = self._current_period(connection, application["project_id"])
                approved_gross = 0
                approved_retention = 0
                for line in lines:
                    line_decision = "approved" if line["line_id"] in approved_set else "rejected"
                    connection.execute("UPDATE application_lines SET decision=? WHERE line_id=?",
                                       (line_decision, line["line_id"]))
                    if line_decision == "approved":
                        approved_gross += line["amount_cents"]
                        approved_retention += line["retention_cents"]
                if not approved_set:
                    status = "rejected"
                elif approved_set == line_ids:
                    status = "approved"
                else:
                    status = "partially_approved"
                connection.execute(
                    "UPDATE payment_applications SET status=?, decided_by=?, decided_at=? WHERE application_id=?",
                    (status, actor_id, self._now(), application_id),
                )
                # 释放只被驳回行占用的证据；全部驳回时同时释放发票
                released: list[str] = []
                if status == "rejected":
                    released = self._release_occupations(connection, application_id, "rejected")
                elif status == "partially_approved":
                    line_evidence = connection.execute(
                        "SELECT le.evidence_id, l.decision FROM application_line_evidence le "
                        "JOIN application_lines l ON l.line_id = le.line_id WHERE l.application_id=?",
                        (application_id,)).fetchall()
                    evidence_decisions: dict[str, set[str]] = {}
                    for row in line_evidence:
                        evidence_decisions.setdefault(row["evidence_id"], set()).add(row["decision"])
                    releasable = [eid for eid, decisions in evidence_decisions.items()
                                  if "approved" not in decisions]
                    released = self._release_occupations(connection, application_id,
                                                         "line_rejected", releasable)
                approved_net = approved_gross - approved_retention
                kind = {"approved": "application.approved",
                        "partially_approved": "application.partially_approved",
                        "rejected": "application.rejected"}[status]
                self._add_ledger(connection, project_id=application["project_id"],
                                 contract_id=application["contract_id"], application_id=application_id,
                                 period_id=period["period_id"], kind=kind, amount_cents=approved_gross,
                                 payload={"approved_line_ids": sorted(approved_set),
                                          "approved_gross_cents": approved_gross,
                                          "approved_retention_cents": approved_retention,
                                          "approved_net_cents": approved_net,
                                          "comment": comment}, actor_id=actor_id)
                if approved_retention:
                    self._add_ledger(connection, project_id=application["project_id"],
                                     contract_id=application["contract_id"], application_id=application_id,
                                     period_id=period["period_id"], kind="retention.withheld",
                                     amount_cents=approved_retention,
                                     payload={"rate_bp": self._contract_retention_rate(connection, application["contract_id"])},
                                     actor_id=actor_id)
                if released:
                    self._add_ledger(connection, project_id=application["project_id"],
                                     contract_id=application["contract_id"], application_id=application_id,
                                     period_id=period["period_id"], kind="evidence.released",
                                     amount_cents=0,
                                     payload={"evidence_ids": released, "reason": "review"},
                                     actor_id=actor_id)
                # 实际资金份额拆分：按批准行的净额重新计算
                if approved_net:
                    actual: dict[str, int] = {}
                    project_shares = self._project_shares(connection, application["project_id"])
                    order_shares: dict[str, list[tuple[str, int]]] = {}
                    for line in lines:
                        if line["line_id"] not in approved_set:
                            continue
                        shares = project_shares
                        if line["change_order_id"]:
                            if line["change_order_id"] not in order_shares:
                                order_row = connection.execute(
                                    "SELECT funding_json FROM change_orders WHERE change_order_id=?",
                                    (line["change_order_id"],)).fetchone()
                                order_shares[line["change_order_id"]] = [
                                    (share["funding_source_id"], share["ratio_bp"])
                                    for share in json.loads(order_row["funding_json"])]
                            shares = order_shares[line["change_order_id"]]
                        line_net = line["amount_cents"] - line["retention_cents"]
                        for source_id, amount in self._split(line_net, shares).items():
                            actual[source_id] = actual.get(source_id, 0) + amount
                    for source_id, amount in actual.items():
                        connection.execute(
                            "INSERT INTO funding_allocations(allocation_id,application_id,funding_source_id,stage,"
                            "amount_cents,created_at) VALUES(?,?,?,'actual',?,?)",
                            (uuid.uuid4().hex, application_id, source_id, amount, self._now()),
                        )
                append_event(connection, actor_id=actor_id, action="payment_application.reviewed",
                             resource_type="payment_application", resource_id=application_id,
                             detail={"decision": decision, "status": status,
                                     "approved_gross_cents": approved_gross},
                             occurred_at=self._now())
                return "payment_application", application_id, {
                    "application_id": application_id, "status": status,
                    "approved_gross_cents": approved_gross,
                    "approved_net_cents": approved_net,
                    "released_evidence_ids": released}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="review_application", payload=payload, create=create)
            return {"request_id": receipt.request_id, "replayed": receipt.replayed, **response}

    def _contract_retention_rate(self, connection, contract_id: str) -> int:
        row = connection.execute("SELECT retention_rate_bp FROM contracts WHERE contract_id=?",
                                 (contract_id,)).fetchone()
        return row["retention_rate_bp"]

    # ------------------------------------------------------------------
    # 支付、质保金留置/释放与审计追回
    # ------------------------------------------------------------------

    def record_payment(self, *, request_id: str, actor_id: str, application_id: str,
                       amount_cents: int, payment_reference: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "application_id": application_id,
                   "amount_cents": amount_cents, "payment_reference": payment_reference}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "finance", "admin")
            application = connection.execute("SELECT * FROM payment_applications WHERE application_id=?",
                                             (application_id,)).fetchone()
            if application is None:
                raise NotFoundError("支付申请不存在")
            if application["status"] not in ("approved", "partially_approved"):
                raise ConflictError("只有会签通过的申请可以付款")
            amount_cents = self._positive_int(amount_cents, "amount_cents")
            money = self._application_money(connection, application_id)
            if amount_cents > money["outstanding_cents"]:
                raise ValidationError("付款金额超过应付未付余额")

            def create() -> tuple[str, str, dict[str, Any]]:
                period = self._current_period(connection, application["project_id"])
                self._add_ledger(connection, project_id=application["project_id"],
                                 contract_id=application["contract_id"], application_id=application_id,
                                 period_id=period["period_id"], kind="payment.made",
                                 amount_cents=amount_cents,
                                 payload={"payment_reference": payment_reference}, actor_id=actor_id)
                new_money = self._application_money(connection, application_id)
                new_status = application["status"]
                if new_money["outstanding_cents"] == 0 and new_money["retention_held_cents"] == 0:
                    new_status = "paid"
                    connection.execute("UPDATE payment_applications SET status='paid' WHERE application_id=?",
                                       (application_id,))
                append_event(connection, actor_id=actor_id, action="payment.made",
                             resource_type="payment_application", resource_id=application_id,
                             detail={"amount_cents": amount_cents,
                                     "outstanding_cents": new_money["outstanding_cents"]},
                             occurred_at=self._now())
                return "payment_application", application_id, {
                    "application_id": application_id, "status": new_status,
                    "paid_total_cents": new_money["payments_cents"],
                    "outstanding_cents": new_money["outstanding_cents"]}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="record_payment", payload=payload, create=create)
            return {"request_id": receipt.request_id, "replayed": receipt.replayed, **response}

    def release_retention(self, *, request_id: str, actor_id: str, application_id: str,
                          amount_cents: int) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "application_id": application_id, "amount_cents": amount_cents}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer", "admin")
            application = connection.execute("SELECT * FROM payment_applications WHERE application_id=?",
                                             (application_id,)).fetchone()
            if application is None:
                raise NotFoundError("支付申请不存在")
            if application["status"] not in ("approved", "partially_approved", "paid"):
                raise ConflictError("当前状态不允许释放质保金")
            contract = connection.execute("SELECT * FROM contracts WHERE contract_id=?",
                                          (application["contract_id"],)).fetchone()
            today = self.clock.now().date().isoformat()
            if today < contract["retention_release_after"]:
                raise ConflictError("质保条件未满足，不能提前释放质保金")
            amount_cents = self._positive_int(amount_cents, "amount_cents")
            money = self._application_money(connection, application_id)
            if amount_cents > money["retention_held_cents"]:
                raise ValidationError("释放金额超过质保金留置余额")

            def create() -> tuple[str, str, dict[str, Any]]:
                period = self._current_period(connection, application["project_id"])
                self._add_ledger(connection, project_id=application["project_id"],
                                 contract_id=application["contract_id"], application_id=application_id,
                                 period_id=period["period_id"], kind="retention.released",
                                 amount_cents=amount_cents, payload={}, actor_id=actor_id)
                shares = self._split(amount_cents, self._project_shares(connection, application["project_id"]))
                for source_id, amount in shares.items():
                    connection.execute(
                        "INSERT INTO funding_allocations(allocation_id,application_id,funding_source_id,stage,"
                        "amount_cents,created_at) VALUES(?,?,?,'retention_release',?,?)",
                        (uuid.uuid4().hex, application_id, source_id, amount, self._now()),
                    )
                append_event(connection, actor_id=actor_id, action="retention.released",
                             resource_type="payment_application", resource_id=application_id,
                             detail={"amount_cents": amount_cents}, occurred_at=self._now())
                return "payment_application", application_id, {
                    "application_id": application_id,
                    "retention_held_cents": money["retention_held_cents"] - amount_cents}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="release_retention", payload=payload, create=create)
            return {"request_id": receipt.request_id, "replayed": receipt.replayed, **response}

    def record_recovery(self, *, request_id: str, actor_id: str, application_id: str,
                        amount_cents: int, reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "application_id": application_id,
                   "amount_cents": amount_cents, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "auditor", "admin")
            application = connection.execute("SELECT * FROM payment_applications WHERE application_id=?",
                                             (application_id,)).fetchone()
            if application is None:
                raise NotFoundError("支付申请不存在")
            if application["status"] not in ("approved", "partially_approved", "paid"):
                raise ConflictError("当前状态不允许审计追回")
            reason = self._text(reason, "reason", 500)
            amount_cents = self._positive_int(amount_cents, "amount_cents")
            money = self._application_money(connection, application_id)
            if amount_cents > money["paid_net_cents"]:
                raise ValidationError("追回金额超过净已付金额")

            def create() -> tuple[str, str, dict[str, Any]]:
                period = self._current_period(connection, application["project_id"])
                self._add_ledger(connection, project_id=application["project_id"],
                                 contract_id=application["contract_id"], application_id=application_id,
                                 period_id=period["period_id"], kind="audit.recovered",
                                 amount_cents=amount_cents, payload={"reason": reason}, actor_id=actor_id)
                shares = self._split(amount_cents, self._project_shares(connection, application["project_id"]))
                for source_id, amount in shares.items():
                    connection.execute(
                        "INSERT INTO funding_allocations(allocation_id,application_id,funding_source_id,stage,"
                        "amount_cents,created_at) VALUES(?,?,?,'audit_recovery',?,?)",
                        (uuid.uuid4().hex, application_id, source_id, -amount, self._now()),
                    )
                append_event(connection, actor_id=actor_id, action="audit.recovered",
                             resource_type="payment_application", resource_id=application_id,
                             detail={"amount_cents": amount_cents, "reason": reason},
                             occurred_at=self._now())
                return "payment_application", application_id, {
                    "application_id": application_id,
                    "recovered_total_cents": money["recoveries_cents"] + amount_cents}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="record_recovery", payload=payload, create=create)
            return {"request_id": receipt.request_id, "replayed": receipt.replayed, **response}

    def post_correction(self, *, request_id: str, actor_id: str, corrects_entry_id: str,
                        amount_cents: int, reason: str) -> dict[str, Any]:
        """对既有分录追加更正：永远落在当前开放期间，绝不改写原期间。"""

        payload = {"actor_id": actor_id, "corrects_entry_id": corrects_entry_id,
                   "amount_cents": amount_cents, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "finance", "reviewer", "admin")
            original = connection.execute("SELECT * FROM ledger_entries WHERE entry_id=?",
                                          (corrects_entry_id,)).fetchone()
            if original is None:
                raise NotFoundError("被更正的分录不存在")
            if isinstance(amount_cents, bool) or not isinstance(amount_cents, int) or amount_cents == 0:
                raise ValidationError("amount_cents 必须是非零整数")
            reason = self._text(reason, "reason", 500)
            original_period = connection.execute("SELECT * FROM periods WHERE period_id=?",
                                                 (original["period_id"],)).fetchone()

            def create() -> tuple[str, str, dict[str, Any]]:
                period = self._current_period(connection, original["project_id"])
                entry_id = self._add_ledger(
                    connection, project_id=original["project_id"], contract_id=original["contract_id"],
                    application_id=original["application_id"], period_id=period["period_id"],
                    kind="correction.posted", amount_cents=amount_cents,
                    payload={"reason": reason, "original_kind": original["kind"],
                             "original_period": original_period["name"],
                             "original_period_status": original_period["status"]},
                    actor_id=actor_id, corrects_entry_id=corrects_entry_id)
                append_event(connection, actor_id=actor_id, action="correction.posted",
                             resource_type="ledger_entry", resource_id=entry_id,
                             detail={"corrects_entry_id": corrects_entry_id, "amount_cents": amount_cents,
                                     "posted_period": period["name"],
                                     "original_period": original_period["name"]},
                             occurred_at=self._now())
                return "ledger_entry", entry_id, {
                    "entry_id": entry_id, "posted_period": period["name"],
                    "original_period": original_period["name"],
                    "original_period_status": original_period["status"]}

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="post_correction", payload=payload, create=create)
            return {"request_id": receipt.request_id, "replayed": receipt.replayed, **response}

    # ------------------------------------------------------------------
    # 查询：财务解释全过程，审计沿证据反查并验证平衡
    # ------------------------------------------------------------------

    def application_detail(self, application_id: str) -> dict[str, Any]:
        connection = self.database.connection
        application = connection.execute("SELECT * FROM payment_applications WHERE application_id=?",
                                         (application_id,)).fetchone()
        if application is None:
            raise NotFoundError("支付申请不存在")
        period = connection.execute("SELECT * FROM periods WHERE period_id=?",
                                    (application["period_id"],)).fetchone()
        version = connection.execute("SELECT * FROM quantity_versions WHERE version_id=?",
                                     (application["quantity_version_id"],)).fetchone()
        lines = []
        for line in connection.execute(
                "SELECT l.*, i.item_code FROM application_lines l "
                "JOIN boq_items i ON i.item_id = l.item_id "
                "WHERE l.application_id=? ORDER BY l.rowid", (application_id,)):
            evidence_ids = [row["evidence_id"] for row in connection.execute(
                "SELECT evidence_id FROM application_line_evidence WHERE line_id=? ORDER BY evidence_id",
                (line["line_id"],))]
            lines.append({"line_id": line["line_id"], "item_id": line["item_id"],
                          "item_code": line["item_code"], "change_order_id": line["change_order_id"],
                          "quantity_milli": line["quantity_milli"],
                          "unit_price_cents": line["unit_price_cents"],
                          "amount_cents": line["amount_cents"],
                          "retention_cents": line["retention_cents"],
                          "decision": line["decision"], "evidence_ids": evidence_ids})
        invoice_ids = [row["evidence_id"] for row in connection.execute(
            "SELECT evidence_id FROM application_invoices WHERE application_id=? ORDER BY evidence_id",
            (application_id,))]
        allocations = [{"funding_source_id": row["funding_source_id"], "stage": row["stage"],
                        "amount_cents": row["amount_cents"]}
                       for row in connection.execute(
                           "SELECT * FROM funding_allocations WHERE application_id=? "
                           "ORDER BY stage, funding_source_id", (application_id,))]
        ledger = self._application_ledger(connection, application_id)
        money = self._application_money(connection, application_id)
        return {
            "application_id": application_id,
            "project_id": application["project_id"],
            "contract_id": application["contract_id"],
            "status": application["status"],
            "pending_reasons": json.loads(application["pending_reasons_json"]),
            "period": {"period_id": period["period_id"], "name": period["name"], "status": period["status"]},
            "quantity_version_no": version["version_no"],
            "gross_cents": application["gross_cents"],
            "retention_cents": application["retention_cents"],
            "net_cents": application["net_cents"],
            "submitted_by": application["submitted_by"],
            "submitted_at": application["submitted_at"],
            "decided_by": application["decided_by"],
            "decided_at": application["decided_at"],
            "lines": lines,
            "invoice_ids": invoice_ids,
            "funding_allocations": allocations,
            "money": money,
            "ledger": ledger,
        }

    def _application_ledger(self, connection, application_id: str) -> list[dict[str, Any]]:
        entries = []
        for row in connection.execute(
                "SELECT e.*, p.name AS period_name FROM ledger_entries e "
                "JOIN periods p ON p.period_id = e.period_id "
                "WHERE e.application_id=? ORDER BY e.sequence", (application_id,)):
            entries.append({"sequence": row["sequence"], "entry_id": row["entry_id"],
                            "kind": row["kind"], "amount_cents": row["amount_cents"],
                            "period": row["period_name"], "corrects_entry_id": row["corrects_entry_id"],
                            "actor_id": row["actor_id"], "created_at": row["created_at"],
                            "payload": json.loads(row["payload_json"])})
        return entries

    def evidence_occupations(self, evidence_id: str) -> dict[str, Any]:
        connection = self.database.connection
        evidence = connection.execute("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
        if evidence is None:
            raise NotFoundError("证据不存在")
        occupations = []
        for row in connection.execute(
                "SELECT o.*, a.status AS application_status, a.contract_id FROM evidence_occupations o "
                "JOIN payment_applications a ON a.application_id = o.application_id "
                "WHERE o.evidence_id=? ORDER BY o.rowid", (evidence_id,)):
            occupations.append({"occupation_id": row["occupation_id"],
                                "application_id": row["application_id"],
                                "application_status": row["application_status"],
                                "contract_id": row["contract_id"],
                                "occupied_at": row["occupied_at"],
                                "released_at": row["released_at"],
                                "release_reason": row["release_reason"]})
        return {"evidence_id": evidence_id, "project_id": evidence["project_id"],
                "evidence_type": evidence["evidence_type"], "external_key": evidence["external_key"],
                "independent": bool(evidence["independent"]), "occupations": occupations}

    def contract_balance(self, contract_id: str) -> dict[str, Any]:
        connection = self.database.connection
        contract = connection.execute("SELECT * FROM contracts WHERE contract_id=?", (contract_id,)).fetchone()
        if contract is None:
            raise NotFoundError("合同不存在")
        version = self._latest_version(connection, contract_id)
        product = connection.execute(
            "SELECT COALESCE(SUM(vi.quantity_milli * i.unit_price_cents),0) AS total "
            "FROM quantity_version_items vi JOIN boq_items i ON i.item_id = vi.item_id "
            "WHERE vi.version_id=?", (version["version_id"],)).fetchone()["total"]
        contract_amount = product // 1000
        totals: dict[str, int] = {}
        for row in connection.execute(
                "SELECT kind, COALESCE(SUM(amount_cents),0) AS total FROM ledger_entries "
                "WHERE contract_id=? AND kind IN ('application.approved','application.partially_approved',"
                "'retention.withheld','retention.released','payment.made','audit.recovered') GROUP BY kind",
                (contract_id,)):
            totals[row["kind"]] = row["total"]
        approved_gross = totals.get("application.approved", 0) + totals.get("application.partially_approved", 0)
        withheld = totals.get("retention.withheld", 0)
        released = totals.get("retention.released", 0)
        payments = totals.get("payment.made", 0)
        recoveries = totals.get("audit.recovered", 0)
        held = withheld - released
        paid_net = payments - recoveries
        payable_outstanding = approved_gross - withheld + released - payments
        remaining_obligation = contract_amount - approved_gross + recoveries

        # 交叉核对：台账批准总额必须等于明细行批准总额
        approved_lines_total = connection.execute(
            "SELECT COALESCE(SUM(l.amount_cents),0) AS total FROM application_lines l "
            "JOIN payment_applications a ON a.application_id = l.application_id "
            "WHERE a.contract_id=? AND l.decision='approved' "
            "AND a.status IN ('approved','partially_approved','paid')",
            (contract_id,)).fetchone()["total"]
        # 交叉核对：任何申请不得超付或超追回
        violations = 0
        for row in connection.execute(
                "SELECT application_id FROM payment_applications WHERE contract_id=? "
                "AND status IN ('approved','partially_approved','paid')", (contract_id,)):
            money = self._application_money(connection, row["application_id"])
            if money["outstanding_cents"] < 0 or money["paid_net_cents"] < 0 or money["retention_held_cents"] < 0:
                violations += 1

        identity = paid_net + held + payable_outstanding + remaining_obligation == contract_amount
        checks = [
            {"name": "identity", "ok": identity},
            {"name": "retention_held_non_negative", "ok": held >= 0},
            {"name": "payable_outstanding_non_negative", "ok": payable_outstanding >= 0},
            {"name": "remaining_obligation_non_negative", "ok": remaining_obligation >= 0},
            {"name": "paid_net_non_negative", "ok": paid_net >= 0},
            {"name": "ledger_matches_approved_lines", "ok": approved_gross == approved_lines_total},
            {"name": "no_application_overpayment", "ok": violations == 0},
        ]
        return {
            "contract_id": contract_id,
            "project_id": contract["project_id"],
            "quantity_version_no": version["version_no"],
            "contract_amount_cents": contract_amount,
            "approved_gross_cents": approved_gross,
            "retention_withheld_cents": withheld,
            "retention_released_cents": released,
            "retention_held_cents": held,
            "payments_cents": payments,
            "recoveries_cents": recoveries,
            "paid_net_cents": paid_net,
            "payable_outstanding_cents": payable_outstanding,
            "remaining_obligation_cents": remaining_obligation,
            "checks": checks,
            "balanced": all(check["ok"] for check in checks),
        }

    def contract_ledger(self, contract_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        if connection.execute("SELECT 1 FROM contracts WHERE contract_id=?", (contract_id,)).fetchone() is None:
            raise NotFoundError("合同不存在")
        entries = []
        for row in connection.execute(
                "SELECT e.*, p.name AS period_name FROM ledger_entries e "
                "JOIN periods p ON p.period_id = e.period_id "
                "WHERE e.contract_id=? ORDER BY e.sequence", (contract_id,)):
            entries.append({"sequence": row["sequence"], "entry_id": row["entry_id"],
                            "kind": row["kind"], "amount_cents": row["amount_cents"],
                            "application_id": row["application_id"], "period": row["period_name"],
                            "corrects_entry_id": row["corrects_entry_id"],
                            "actor_id": row["actor_id"], "created_at": row["created_at"],
                            "payload": json.loads(row["payload_json"])})
        return entries

    def audit_events(self, after_sequence: int = 0) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM audit_events WHERE sequence>? ORDER BY sequence", (after_sequence,)
        ).fetchall()
        return [{"sequence": row["sequence"], "event_id": row["event_id"], "actor_id": row["actor_id"],
                 "action": row["action"], "resource_type": row["resource_type"],
                 "resource_id": row["resource_id"], "detail": json.loads(row["detail_json"]),
                 "previous_hash": row["previous_hash"], "event_hash": row["event_hash"],
                 "occurred_at": row["occurred_at"]} for row in rows]

    def verify_audit(self) -> tuple[bool, int]:
        return verify_chain(self.database.connection)
