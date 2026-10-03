"""建设项目进度支付核验服务。

把合同清单、工程量版本、现场验收、发票、变更令、共同出资比例和质保条件
关联到可追踪的支付申请。每笔申报在同一事务中完成证据占用、累计量校验和
资金份额拆分；重复材料、超合同计量或缺少独立验收的申请进入待处理而不是
生成付款。撤回、驳回、部分批准和审计追回都以新分录保留，关账后的更正
只能落在当前开放期间，绝不重写原期间。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

from .audit import append_event, canonical_json, digest, verify_chain
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .payment_models import (
    CORRECTABLE_ENTRY_TYPES,
    ENTRY_APPROVED,
    ENTRY_CORRECTION,
    ENTRY_COUNTERSIGNED,
    ENTRY_PAYMENT,
    ENTRY_PENDING,
    ENTRY_RECOVERY,
    ENTRY_REJECTED,
    ENTRY_RETENTION_RELEASED,
    ENTRY_RETENTION_WITHHELD,
    ENTRY_SUBMITTED,
    ENTRY_WITHDRAWN,
    EVIDENCE_ACCEPTANCE,
    EVIDENCE_INVOICE,
    REASON_DUPLICATE_EVIDENCE,
    REASON_DUPLICATE_MATERIAL,
    REASON_EXCEEDS_QUANTITY,
    REASON_MISSING_INDEPENDENT,
    STATUS_APPROVED,
    STATUS_COUNTERSIGNED,
    STATUS_PAID,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_SUBMITTED,
    STATUS_WITHDRAWN,
)
from .payment_storage import PAYMENT_SCHEMA
from .service import DomainService

PERIOD_FORMAT = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
IN_FLIGHT_STATUSES = (STATUS_SUBMITTED, STATUS_COUNTERSIGNED)


class PaymentService:
    """在基础服务的权限、幂等与审计边界上实现进度支付核验。"""

    def __init__(self, domain: DomainService) -> None:
        self.domain = domain
        self.database = domain.database
        self.database.connection.executescript(PAYMENT_SCHEMA)

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.domain._now()

    def _period(self) -> str:
        return self._now()[:7]

    def _integer(self, value: Any, field: str, minimum: int | None = None,
                 maximum: int | None = None) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{field} 必须是整数")
        if minimum is not None and value < minimum:
            raise ValidationError(f"{field} 不能小于 {minimum}")
        if maximum is not None and value > maximum:
            raise ValidationError(f"{field} 不能大于 {maximum}")
        return value

    def _boolean(self, value: Any, field: str) -> bool:
        if not isinstance(value, bool):
            raise ValidationError(f"{field} 必须是布尔值")
        return value

    def _date(self, value: Any, field: str) -> str:
        value = str(value).strip()
        match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value)
        if not match:
            raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期")
        year, month, day = (int(part) for part in match.groups())
        if not (1 <= month <= 12 and 1 <= day <= 31):
            raise ValidationError(f"{field} 必须是有效日期")
        return value

    def _sources(self, raw: Any) -> list[tuple[str, int]]:
        """校验共同出资比例，返回按编码排序的 (编码, 基点) 列表。"""

        if not isinstance(raw, list) or not raw:
            raise ValidationError("funding_sources 不能为空")
        sources: list[tuple[str, int]] = []
        seen: set[str] = set()
        for entry in raw:
            if not isinstance(entry, dict):
                raise ValidationError("funding_sources 元素必须是对象")
            code = self.domain._text(str(entry.get("source_code", "")), "source_code", 40)
            ratio = self._integer(entry.get("ratio_bp"), "ratio_bp", 1, 10000)
            if code in seen:
                raise ValidationError("出资来源编码重复")
            seen.add(code)
            sources.append((code, ratio))
        if sum(ratio for _, ratio in sources) != 10000:
            raise ValidationError("出资比例合计必须等于 10000 基点")
        return sorted(sources)

    def _split_amount(self, amount_cents: int, sources: list[tuple[str, int]]) -> dict[str, int]:
        """按出资基点拆分金额，末位来源承接取整差额，保证合计守恒。"""

        shares: dict[str, int] = {}
        remaining = amount_cents
        for index, (code, ratio_bp) in enumerate(sources):
            if index == len(sources) - 1:
                share = remaining
            else:
                share = amount_cents * ratio_bp // 10000
                remaining -= share
            shares[code] = shares.get(code, 0) + share
        return shares

    def _proportional(self, amount_cents: int, base_splits: dict[str, int],
                      base_total: int) -> dict[str, int]:
        """按既有拆分的比例分摊一笔金额（用于质保金、追回与更正）。"""

        if base_total == 0:
            raise ValidationError("原始分录金额为零，无法按比例拆分")
        shares: dict[str, int] = {}
        remaining = amount_cents
        items = sorted(base_splits.items())
        for index, (code, base) in enumerate(items):
            if index == len(items) - 1:
                share = remaining
            else:
                share = amount_cents * base // base_total
                remaining -= share
            shares[code] = share
        return shares

    def _check_org(self, actor: Any, organization_id: str) -> None:
        if actor.role != "admin" and actor.organization_id != organization_id:
            raise PermissionDenied("不能操作其他组织的合同")

    def _ensure_period_open(self, connection: Any, period: str) -> None:
        row = connection.execute(
            "SELECT status FROM payment_periods WHERE period=?", (period,)
        ).fetchone()
        if row and row["status"] == "closed":
            raise ConflictError("期间已关账，不能写入该期间")

    def _contract_row(self, connection: Any, contract_id: str) -> Any:
        row = connection.execute(
            "SELECT c.*, s.organization_id AS organization_id FROM contracts c "
            "JOIN sites s ON s.site_id=c.site_id WHERE c.contract_id=?",
            (contract_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("合同不存在")
        return row

    def _application_row(self, connection: Any, application_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM payment_applications WHERE application_id=?", (application_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("支付申请不存在")
        return row

    def _item_row(self, connection: Any, contract_id: str, item_code: str) -> Any:
        row = connection.execute(
            "SELECT * FROM contract_items WHERE contract_id=? AND item_code=?",
            (contract_id, item_code),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"清单项不存在: {item_code}")
        return row

    def _sources_for(self, connection: Any, owner_type: str, owner_id: str) -> list[tuple[str, int]]:
        rows = connection.execute(
            "SELECT source_code, ratio_bp FROM funding_sources WHERE owner_type=? AND owner_id=? "
            "ORDER BY source_code",
            (owner_type, owner_id),
        ).fetchall()
        return [(row["source_code"], row["ratio_bp"]) for row in rows]

    def _layer_totals(self, connection: Any, item_id: str) -> tuple[int, int]:
        row = connection.execute(
            "SELECT COALESCE(SUM(quantity_milli),0) AS total, COALESCE(SUM(consumed_milli),0) AS consumed "
            "FROM quantity_layers WHERE item_id=?",
            (item_id,),
        ).fetchone()
        return row["total"], row["consumed"]

    def _in_flight(self, connection: Any, contract_id: str, exclude_application_id: str) -> dict[str, int]:
        """统计在途申请（已申报/已会签）占用的各清单项工程量。"""

        rows = connection.execute(
            "SELECT al.item_id AS item_id, COALESCE(SUM(al.quantity_milli),0) AS qty "
            "FROM application_lines al "
            "JOIN payment_applications pa ON pa.application_id=al.application_id "
            "WHERE pa.contract_id=? AND pa.status IN ('submitted','countersigned') "
            "AND pa.application_id!=? GROUP BY al.item_id",
            (contract_id, exclude_application_id),
        ).fetchall()
        return {row["item_id"]: row["qty"] for row in rows}

    def _attribute_layers(self, connection: Any, item_id: str, quantity_milli: int,
                          unit_price_cents: int, consume: bool) -> tuple[dict[str, int], int, list[dict[str, Any]]]:
        """把工程量按层（基础合同→各变更令）归属并计算资金份额。

        基础合同层使用合同出资比例，变更层使用变更令自己的出资比例，
        因此跨越不同资金来源的设计变更会被正确拆分。consume 为真时
        同步扣减分层余量（批准时点才发生）。
        """

        layers = connection.execute(
            "SELECT * FROM quantity_layers WHERE item_id=? ORDER BY sequence", (item_id,)
        ).fetchall()
        available_total = sum(layer["quantity_milli"] - layer["consumed_milli"] for layer in layers)
        if quantity_milli > available_total:
            raise ConflictError("可计量工程量不足")
        portions: list[tuple[Any, int]] = []
        remaining = quantity_milli
        for layer in layers:
            available = layer["quantity_milli"] - layer["consumed_milli"]
            take = min(available, remaining)
            if take > 0:
                portions.append((layer, take))
                remaining -= take
            if remaining == 0:
                break
        total_amount = quantity_milli * unit_price_cents // 1000
        per_source: dict[str, int] = {}
        details: list[dict[str, Any]] = []
        accumulated = 0
        for index, (layer, take) in enumerate(portions):
            if index < len(portions) - 1:
                portion_amount = take * unit_price_cents // 1000
                accumulated += portion_amount
            else:
                portion_amount = total_amount - accumulated
            sources = self._sources_for(connection, layer["source_type"], layer["source_id"])
            shares = self._split_amount(portion_amount, sources)
            for code, amount in shares.items():
                per_source[code] = per_source.get(code, 0) + amount
            details.append({
                "layer_id": layer["layer_id"],
                "source_type": layer["source_type"],
                "source_id": layer["source_id"],
                "quantity_milli": take,
                "amount_cents": portion_amount,
                "funding_split": shares,
            })
            if consume:
                connection.execute(
                    "UPDATE quantity_layers SET consumed_milli=consumed_milli+? WHERE layer_id=?",
                    (take, layer["layer_id"]),
                )
        return per_source, total_amount, details

    def _append_entry(self, connection: Any, *, contract_id: str, application_id: str | None,
                      entry_type: str, period: str, amount_cents: int, detail: dict[str, Any],
                      actor_id: str, references_entry_id: str | None = None,
                      splits: dict[str, int] | None = None) -> str:
        """追加一条不可变分录及其资金份额拆分。"""

        entry_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO ledger_entries(entry_id,contract_id,application_id,entry_type,period,"
            "amount_cents,detail_json,references_entry_id,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (entry_id, contract_id, application_id, entry_type, period, amount_cents,
             canonical_json(detail), references_entry_id, actor_id, self._now()),
        )
        for code, amount in sorted((splits or {}).items()):
            connection.execute(
                "INSERT INTO funding_splits(entry_id,source_code,amount_cents) VALUES(?,?,?)",
                (entry_id, code, amount),
            )
        return entry_id

    def _entry_splits(self, connection: Any, entry_id: str) -> dict[str, int]:
        rows = connection.execute(
            "SELECT source_code, amount_cents FROM funding_splits WHERE entry_id=? ORDER BY source_code",
            (entry_id,),
        ).fetchall()
        return {row["source_code"]: row["amount_cents"] for row in rows}

    def _release_occupations(self, connection: Any, application_id: str, released_at: str,
                             only: set[tuple[str, str]] | None = None) -> None:
        rows = connection.execute(
            "SELECT * FROM evidence_occupations WHERE application_id=? AND status='active'",
            (application_id,),
        ).fetchall()
        for row in rows:
            if only is not None and (row["evidence_type"], row["evidence_id"]) not in only:
                continue
            connection.execute(
                "UPDATE evidence_occupations SET status='released', released_at=? WHERE occupation_id=?",
                (released_at, row["occupation_id"]),
            )

    def _idempotent_result(self, connection: Any, *, request_id: str, action: str,
                           payload: dict[str, Any],
                           create: Callable[[], tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
        """与基础服务共用 request_receipts 表，重放时返回首次的稳定结果。"""

        request_id = self.domain._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {"request_id": request_id, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True,
                    "result": json.loads(row["response_json"])}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {"request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False, "result": response}

    # ------------------------------------------------------------------
    # 主数据登记
    # ------------------------------------------------------------------

    def register_contract(self, *, request_id: str, actor_id: str, contract_id: str, site_id: str,
                          contract_no: str, name: str, contractor: str, retention_rate_bp: int,
                          defect_liability_end: str, requires_quality_certificate: bool,
                          funding_sources: list[dict[str, Any]],
                          items: list[dict[str, Any]]) -> dict[str, Any]:
        """登记合同、清单、共同出资比例和质保条件，并建立工程量首版本。"""

        payload = {"actor_id": actor_id, "contract_id": contract_id, "site_id": site_id,
                   "contract_no": contract_no, "name": name, "contractor": contractor,
                   "retention_rate_bp": retention_rate_bp,
                   "defect_liability_end": defect_liability_end,
                   "requires_quality_certificate": requires_quality_certificate,
                   "funding_sources": funding_sources, "items": items}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "admin", "operator")
            site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")
            self._check_org(actor, site["organization_id"])
            contract_id = self.domain._identifier(contract_id, "contract_id")
            contract_no = self.domain._text(contract_no, "contract_no", 80)
            name = self.domain._text(name, "name")
            contractor = self.domain._text(contractor, "contractor")
            rate_bp = self._integer(retention_rate_bp, "retention_rate_bp", 0, 10000)
            defect_end = self._date(defect_liability_end, "defect_liability_end")
            requires_cert = self._boolean(requires_quality_certificate, "requires_quality_certificate")
            sources = self._sources(funding_sources)
            if not isinstance(items, list) or not items:
                raise ValidationError("items 不能为空")
            normalized_items: list[dict[str, Any]] = []
            seen_codes: set[str] = set()
            for raw in items:
                if not isinstance(raw, dict):
                    raise ValidationError("items 元素必须是对象")
                item_code = self.domain._text(str(raw.get("item_code", "")), "item_code", 40)
                if item_code in seen_codes:
                    raise ValidationError("清单项编码重复")
                seen_codes.add(item_code)
                normalized_items.append({
                    "item_code": item_code,
                    "name": self.domain._text(str(raw.get("name", "")), "item name"),
                    "unit": self.domain._text(str(raw.get("unit", "")), "unit", 20),
                    "unit_price_cents": self._integer(raw.get("unit_price_cents"), "unit_price_cents", 1),
                    "quantity_milli": self._integer(raw.get("quantity_milli"), "quantity_milli", 1),
                })

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO contracts(contract_id,site_id,contract_no,name,contractor,"
                        "retention_rate_bp,defect_liability_end,requires_quality_certificate,status,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,'active',?,?)",
                        (contract_id, site_id, contract_no, name, contractor, rate_bp, defect_end,
                         1 if requires_cert else 0, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("合同编号已经存在") from exc
                total_amount = 0
                for item in normalized_items:
                    item_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO contract_items(item_id,contract_id,item_code,name,unit,"
                        "unit_price_cents,created_at) VALUES(?,?,?,?,?,?,?)",
                        (item_id, contract_id, item["item_code"], item["name"], item["unit"],
                         item["unit_price_cents"], self._now()),
                    )
                    connection.execute(
                        "INSERT INTO quantity_layers(layer_id,item_id,source_type,source_id,"
                        "quantity_milli,consumed_milli,sequence) VALUES(?,?,?,?,?,0,1)",
                        (uuid.uuid4().hex, item_id, "contract", contract_id, item["quantity_milli"]),
                    )
                    connection.execute(
                        "INSERT INTO quantity_versions(version_id,item_id,version,quantity_milli,"
                        "change_id,created_at) VALUES(?,?,1,?,NULL,?)",
                        (uuid.uuid4().hex, item_id, item["quantity_milli"], self._now()),
                    )
                    total_amount += item["quantity_milli"] * item["unit_price_cents"] // 1000
                for code, ratio_bp in sources:
                    connection.execute(
                        "INSERT INTO funding_sources(owner_type,owner_id,source_code,ratio_bp) "
                        "VALUES('contract',?,?,?)",
                        (contract_id, code, ratio_bp),
                    )
                append_event(connection, actor_id=actor_id, action="payment.contract.registered",
                             resource_type="contract", resource_id=contract_id,
                             detail={"contract_no": contract_no, "site_id": site_id,
                                     "items": len(normalized_items),
                                     "contract_amount_cents": total_amount},
                             occurred_at=self._now())
                return "contract", contract_id, {"contract_id": contract_id,
                                                 "items": len(normalized_items),
                                                 "contract_amount_cents": total_amount}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="payment.register_contract",
                                           payload=payload, create=create)

    def register_change_order(self, *, request_id: str, actor_id: str, change_id: str,
                              contract_id: str, change_no: str, description: str,
                              lines: list[dict[str, Any]],
                              funding_sources: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """登记设计变更令（草稿），增加工程量时必须声明自己的出资比例。"""

        payload = {"actor_id": actor_id, "change_id": change_id, "contract_id": contract_id,
                   "change_no": change_no, "description": description, "lines": lines,
                   "funding_sources": funding_sources}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "admin", "operator")
            contract = self._contract_row(connection, contract_id)
            self._check_org(actor, contract["organization_id"])
            change_id = self.domain._identifier(change_id, "change_id")
            change_no = self.domain._text(change_no, "change_no", 80)
            description = self.domain._text(description, "description", 500)
            if not isinstance(lines, list) or not lines:
                raise ValidationError("lines 不能为空")
            normalized: list[tuple[Any, int]] = []
            for raw in lines:
                if not isinstance(raw, dict):
                    raise ValidationError("lines 元素必须是对象")
                item = self._item_row(connection, contract_id, str(raw.get("item_code", "")))
                delta = self._integer(raw.get("quantity_delta_milli"), "quantity_delta_milli")
                if delta == 0:
                    raise ValidationError("变更数量不能为零")
                normalized.append((item, delta))
            has_positive = any(delta > 0 for _, delta in normalized)
            sources = self._sources(funding_sources) if funding_sources is not None else []
            if has_positive and not sources:
                raise ValidationError("增加工程量的变更令必须提供出资比例")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO change_orders(change_id,contract_id,change_no,description,status,"
                        "created_by,created_at) VALUES(?,?,?,?,'draft',?,?)",
                        (change_id, contract_id, change_no, description, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("变更令编号已经存在") from exc
                for item, delta in normalized:
                    connection.execute(
                        "INSERT INTO change_order_lines(change_id,item_id,quantity_delta_milli) "
                        "VALUES(?,?,?)",
                        (change_id, item["item_id"], delta),
                    )
                for code, ratio_bp in sources:
                    connection.execute(
                        "INSERT INTO funding_sources(owner_type,owner_id,source_code,ratio_bp) "
                        "VALUES('change_order',?,?,?)",
                        (change_id, code, ratio_bp),
                    )
                append_event(connection, actor_id=actor_id, action="payment.change_order.registered",
                             resource_type="change_order", resource_id=change_id,
                             detail={"contract_id": contract_id, "change_no": change_no,
                                     "lines": len(normalized)},
                             occurred_at=self._now())
                return "change_order", change_id, {"change_id": change_id, "status": "draft"}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="payment.register_change_order",
                                           payload=payload, create=create)

    def approve_change_order(self, *, request_id: str, actor_id: str, change_id: str) -> dict[str, Any]:
        """批准变更令：生成新的工程量版本，并按变更令出资比例建立工程量分层。"""

        payload = {"actor_id": actor_id, "change_id": change_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "admin", "reviewer")
            change_id = self.domain._identifier(change_id, "change_id")
            change = connection.execute(
                "SELECT * FROM change_orders WHERE change_id=?", (change_id,)
            ).fetchone()
            if change is None:
                raise NotFoundError("变更令不存在")
            contract = self._contract_row(connection, change["contract_id"])
            self._check_org(actor, contract["organization_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                if change["status"] != "draft":
                    raise ConflictError("变更令已批准")
                lines = connection.execute(
                    "SELECT * FROM change_order_lines WHERE change_id=?", (change_id,)
                ).fetchall()
                for line in lines:
                    item_id = line["item_id"]
                    delta = line["quantity_delta_milli"]
                    layers = connection.execute(
                        "SELECT * FROM quantity_layers WHERE item_id=? ORDER BY sequence", (item_id,)
                    ).fetchall()
                    total = sum(layer["quantity_milli"] for layer in layers)
                    if delta > 0:
                        sequence = max(layer["sequence"] for layer in layers) + 1
                        connection.execute(
                            "INSERT INTO quantity_layers(layer_id,item_id,source_type,source_id,"
                            "quantity_milli,consumed_milli,sequence) VALUES(?,?,?,?,?,0,?)",
                            (uuid.uuid4().hex, item_id, "change_order", change_id, delta, sequence),
                        )
                        new_total = total + delta
                    else:
                        consumed = sum(layer["consumed_milli"] for layer in layers)
                        if -delta > total - consumed:
                            raise ConflictError("变更扣减超过尚未计量的工程量")
                        remaining = -delta
                        for layer in reversed(layers):
                            available = layer["quantity_milli"] - layer["consumed_milli"]
                            take = min(available, remaining)
                            if take:
                                connection.execute(
                                    "UPDATE quantity_layers SET quantity_milli=quantity_milli-? "
                                    "WHERE layer_id=?",
                                    (take, layer["layer_id"]),
                                )
                                remaining -= take
                            if remaining == 0:
                                break
                        new_total = total + delta
                    row = connection.execute(
                        "SELECT MAX(version) AS version FROM quantity_versions WHERE item_id=?",
                        (item_id,),
                    ).fetchone()
                    connection.execute(
                        "INSERT INTO quantity_versions(version_id,item_id,version,quantity_milli,"
                        "change_id,created_at) VALUES(?,?,?,?,?,?)",
                        (uuid.uuid4().hex, item_id, row["version"] + 1, new_total, change_id, self._now()),
                    )
                connection.execute(
                    "UPDATE change_orders SET status='approved', approved_by=?, approved_at=? "
                    "WHERE change_id=?",
                    (actor_id, self._now(), change_id),
                )
                append_event(connection, actor_id=actor_id, action="payment.change_order.approved",
                             resource_type="change_order", resource_id=change_id,
                             detail={"contract_id": change["contract_id"], "lines": len(lines)},
                             occurred_at=self._now())
                return "change_order", change_id, {"change_id": change_id, "status": "approved"}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="payment.approve_change_order",
                                           payload=payload, create=create)

    def register_acceptance(self, *, request_id: str, actor_id: str, acceptance_id: str,
                            contract_id: str, acceptance_no: str, item_code: str,
                            quantity_milli: int, accepted_on: str, inspector: str,
                            independent: bool) -> dict[str, Any]:
        """登记现场验收单，independent 标记是否为独立第三方验收。"""

        payload = {"actor_id": actor_id, "acceptance_id": acceptance_id, "contract_id": contract_id,
                   "acceptance_no": acceptance_no, "item_code": item_code,
                   "quantity_milli": quantity_milli, "accepted_on": accepted_on,
                   "inspector": inspector, "independent": independent}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "admin", "operator")
            contract = self._contract_row(connection, contract_id)
            self._check_org(actor, contract["organization_id"])
            acceptance_id = self.domain._identifier(acceptance_id, "acceptance_id")
            acceptance_no = self.domain._text(acceptance_no, "acceptance_no", 80)
            item = self._item_row(connection, contract_id, str(item_code))
            quantity = self._integer(quantity_milli, "quantity_milli", 1)
            accepted_on = self._date(accepted_on, "accepted_on")
            inspector = self.domain._text(inspector, "inspector")
            independent = self._boolean(independent, "independent")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO acceptances(acceptance_id,contract_id,item_id,acceptance_no,"
                        "quantity_milli,accepted_on,inspector,independent,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (acceptance_id, contract_id, item["item_id"], acceptance_no, quantity,
                         accepted_on, inspector, 1 if independent else 0, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("验收单编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="payment.acceptance.registered",
                             resource_type="acceptance", resource_id=acceptance_id,
                             detail={"contract_id": contract_id, "acceptance_no": acceptance_no,
                                     "item_code": item["item_code"], "quantity_milli": quantity,
                                     "independent": independent},
                             occurred_at=self._now())
                return "acceptance", acceptance_id, {"acceptance_id": acceptance_id}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="payment.register_acceptance",
                                           payload=payload, create=create)

    def register_invoice(self, *, request_id: str, actor_id: str, invoice_id: str,
                         contract_id: str, invoice_no: str, amount_cents: int,
                         issued_on: str) -> dict[str, Any]:
        """登记发票，发票号全局唯一。"""

        payload = {"actor_id": actor_id, "invoice_id": invoice_id, "contract_id": contract_id,
                   "invoice_no": invoice_no, "amount_cents": amount_cents, "issued_on": issued_on}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "admin", "operator")
            contract = self._contract_row(connection, contract_id)
            self._check_org(actor, contract["organization_id"])
            invoice_id = self.domain._identifier(invoice_id, "invoice_id")
            invoice_no = self.domain._text(invoice_no, "invoice_no", 80)
            amount = self._integer(amount_cents, "amount_cents", 1)
            issued_on = self._date(issued_on, "issued_on")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO invoices(invoice_id,contract_id,invoice_no,amount_cents,"
                        "issued_on,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (invoice_id, contract_id, invoice_no, amount, issued_on, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("发票编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="payment.invoice.registered",
                             resource_type="invoice", resource_id=invoice_id,
                             detail={"contract_id": contract_id, "invoice_no": invoice_no,
                                     "amount_cents": amount},
                             occurred_at=self._now())
                return "invoice", invoice_id, {"invoice_id": invoice_id}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="payment.register_invoice",
                                           payload=payload, create=create)

    def register_quality_certificate(self, *, request_id: str, actor_id: str, certificate_id: str,
                                     contract_id: str, certificate_no: str,
                                     issued_on: str) -> dict[str, Any]:
        """登记质量合格证明，是释放质保金的前置条件之一。"""

        payload = {"actor_id": actor_id, "certificate_id": certificate_id,
                   "contract_id": contract_id, "certificate_no": certificate_no,
                   "issued_on": issued_on}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "admin", "operator")
            contract = self._contract_row(connection, contract_id)
            self._check_org(actor, contract["organization_id"])
            certificate_id = self.domain._identifier(certificate_id, "certificate_id")
            certificate_no = self.domain._text(certificate_no, "certificate_no", 80)
            issued_on = self._date(issued_on, "issued_on")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO quality_certificates(certificate_id,contract_id,certificate_no,"
                        "issued_on,created_by,created_at) VALUES(?,?,?,?,?,?)",
                        (certificate_id, contract_id, certificate_no, issued_on, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("质量合格证明编号已经存在") from exc
                append_event(connection, actor_id=actor_id,
                             action="payment.certificate.registered",
                             resource_type="quality_certificate", resource_id=certificate_id,
                             detail={"contract_id": contract_id, "certificate_no": certificate_no},
                             occurred_at=self._now())
                return "quality_certificate", certificate_id, {"certificate_id": certificate_id}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="payment.register_quality_certificate",
                                           payload=payload, create=create)

    # ------------------------------------------------------------------
    # 支付申请生命周期
    # ------------------------------------------------------------------

    def submit_application(self, *, request_id: str, actor_id: str, application_id: str,
                           contract_id: str, lines: list[dict[str, Any]]) -> dict[str, Any]:
        """申报支付申请。

        在同一事务中完成：证据占用（验收单与发票只能被一笔有效申请占用）、
        累计量校验（已计量 + 在途 + 本次不超过工程量版本）、资金份额拆分
        （按工程量分层归属合同或变更令的出资比例）。发现重复材料、超合同
        计量或缺少独立验收时，申请进入待处理，不占用证据也不生成付款。
        """

        payload = {"actor_id": actor_id, "application_id": application_id,
                   "contract_id": contract_id, "lines": lines}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "operator")
            contract = self._contract_row(connection, contract_id)
            self._check_org(actor, contract["organization_id"])
            period = self._period()
            self._ensure_period_open(connection, period)
            application_id = self.domain._identifier(application_id, "application_id")
            if not isinstance(lines, list) or not lines:
                raise ValidationError("lines 不能为空")
            normalized: list[tuple[Any, Any]] = []
            seen_acceptances: set[str] = set()
            for raw in lines:
                if not isinstance(raw, dict):
                    raise ValidationError("lines 元素必须是对象")
                acceptance_id = self.domain._identifier(str(raw.get("acceptance_id", "")),
                                                        "acceptance_id")
                invoice_id = self.domain._identifier(str(raw.get("invoice_id", "")), "invoice_id")
                if acceptance_id in seen_acceptances:
                    raise ValidationError("同一验收单在申请中重复")
                seen_acceptances.add(acceptance_id)
                acceptance = connection.execute(
                    "SELECT * FROM acceptances WHERE acceptance_id=?", (acceptance_id,)
                ).fetchone()
                if acceptance is None or acceptance["contract_id"] != contract_id:
                    raise ValidationError("验收单不存在或不属于本合同")
                invoice = connection.execute(
                    "SELECT * FROM invoices WHERE invoice_id=?", (invoice_id,)
                ).fetchone()
                if invoice is None or invoice["contract_id"] != contract_id:
                    raise ValidationError("发票不存在或不属于本合同")
                normalized.append((acceptance, invoice))

            def create() -> tuple[str, str, dict[str, Any]]:
                reasons: list[dict[str, Any]] = []
                for acceptance, _ in normalized:
                    if not acceptance["independent"]:
                        reasons.append({"reason": REASON_MISSING_INDEPENDENT,
                                        "acceptance_id": acceptance["acceptance_id"],
                                        "acceptance_no": acceptance["acceptance_no"]})
                for acceptance, invoice in normalized:
                    for evidence_type, evidence_id in ((EVIDENCE_ACCEPTANCE, acceptance["acceptance_id"]),
                                                       (EVIDENCE_INVOICE, invoice["invoice_id"])):
                        occupied = connection.execute(
                            "SELECT application_id FROM evidence_occupations "
                            "WHERE evidence_type=? AND evidence_id=? AND status='active'",
                            (evidence_type, evidence_id),
                        ).fetchone()
                        if occupied:
                            reasons.append({"reason": REASON_DUPLICATE_EVIDENCE,
                                            "evidence_type": evidence_type,
                                            "evidence_id": evidence_id,
                                            "occupied_by": occupied["application_id"]})
                # 同一验收单号被其他合同（如总包与分包）占用，视为重复材料。
                for acceptance, _ in normalized:
                    row = connection.execute(
                        "SELECT a.acceptance_id, eo.application_id FROM evidence_occupations eo "
                        "JOIN acceptances a ON a.acceptance_id=eo.evidence_id "
                        "WHERE eo.evidence_type='acceptance' AND eo.status='active' "
                        "AND a.acceptance_no=? AND a.contract_id!=? LIMIT 1",
                        (acceptance["acceptance_no"], contract_id),
                    ).fetchone()
                    if row:
                        reasons.append({"reason": REASON_DUPLICATE_MATERIAL,
                                        "acceptance_id": acceptance["acceptance_id"],
                                        "acceptance_no": acceptance["acceptance_no"],
                                        "occupied_by": row["application_id"]})
                by_item: dict[str, int] = {}
                for acceptance, _ in normalized:
                    by_item[acceptance["item_id"]] = by_item.get(acceptance["item_id"], 0) + \
                        acceptance["quantity_milli"]
                in_flight = self._in_flight(connection, contract_id, application_id)
                for item_id, quantity in sorted(by_item.items()):
                    total, consumed = self._layer_totals(connection, item_id)
                    pending = in_flight.get(item_id, 0)
                    if consumed + pending + quantity > total:
                        reasons.append({"reason": REASON_EXCEEDS_QUANTITY,
                                        "item_id": item_id,
                                        "requested_milli": quantity,
                                        "available_milli": total - consumed - pending})
                claimed = 0
                for acceptance, _ in normalized:
                    item = connection.execute(
                        "SELECT unit_price_cents FROM contract_items WHERE item_id=?",
                        (acceptance["item_id"],),
                    ).fetchone()
                    claimed += acceptance["quantity_milli"] * item["unit_price_cents"] // 1000
                try:
                    connection.execute(
                        "INSERT INTO payment_applications(application_id,contract_id,period,status,"
                        "claimed_amount_cents,approved_amount_cents,pending_reasons_json,created_by,"
                        "created_at) VALUES(?,?,?,?,?,0,?,?,?)",
                        (application_id, contract_id, period,
                         STATUS_PENDING if reasons else STATUS_SUBMITTED, claimed,
                         canonical_json(reasons), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("支付申请编号已经存在") from exc
                for acceptance, invoice in normalized:
                    connection.execute(
                        "INSERT INTO application_lines(application_id,acceptance_id,invoice_id,"
                        "item_id,quantity_milli,approved_quantity_milli) VALUES(?,?,?,?,?,0)",
                        (application_id, acceptance["acceptance_id"], invoice["invoice_id"],
                         acceptance["item_id"], acceptance["quantity_milli"]),
                    )
                if reasons:
                    self._append_entry(
                        connection, contract_id=contract_id, application_id=application_id,
                        entry_type=ENTRY_PENDING, period=period, amount_cents=0,
                        detail={"reasons": reasons, "claimed_amount_cents": claimed},
                        actor_id=actor_id)
                    append_event(connection, actor_id=actor_id,
                                 action="payment.application.pending",
                                 resource_type="payment_application", resource_id=application_id,
                                 detail={"contract_id": contract_id, "reasons": reasons},
                                 occurred_at=self._now())
                    return "payment_application", application_id, {
                        "application_id": application_id, "status": STATUS_PENDING,
                        "reasons": reasons}
                occupied: set[tuple[str, str]] = set()
                for acceptance, invoice in normalized:
                    for evidence_type, evidence_id in ((EVIDENCE_ACCEPTANCE, acceptance["acceptance_id"]),
                                                       (EVIDENCE_INVOICE, invoice["invoice_id"])):
                        if (evidence_type, evidence_id) in occupied:
                            continue
                        occupied.add((evidence_type, evidence_id))
                        connection.execute(
                            "INSERT INTO evidence_occupations(occupation_id,evidence_type,evidence_id,"
                            "application_id,status,occupied_at) VALUES(?,?,?,?,'active',?)",
                            (uuid.uuid4().hex, evidence_type, evidence_id, application_id, self._now()),
                        )
                claimed_split: dict[str, int] = {}
                layer_details: dict[str, Any] = {}
                for item_id, quantity in sorted(by_item.items()):
                    item = connection.execute(
                        "SELECT unit_price_cents FROM contract_items WHERE item_id=?", (item_id,)
                    ).fetchone()
                    per_source, _, details = self._attribute_layers(
                        connection, item_id, quantity, item["unit_price_cents"], consume=False)
                    for code, amount in per_source.items():
                        claimed_split[code] = claimed_split.get(code, 0) + amount
                    layer_details[item_id] = details
                self._append_entry(
                    connection, contract_id=contract_id, application_id=application_id,
                    entry_type=ENTRY_SUBMITTED, period=period, amount_cents=claimed,
                    detail={"funding_split": claimed_split, "layers": layer_details},
                    actor_id=actor_id, splits=claimed_split)
                append_event(connection, actor_id=actor_id,
                             action="payment.application.submitted",
                             resource_type="payment_application", resource_id=application_id,
                             detail={"contract_id": contract_id, "claimed_amount_cents": claimed,
                                     "lines": len(normalized)},
                             occurred_at=self._now())
                return "payment_application", application_id, {
                    "application_id": application_id, "status": STATUS_SUBMITTED,
                    "claimed_amount_cents": claimed, "funding_split": claimed_split}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="payment.submit_application",
                                           payload=payload, create=create)

    def countersign_application(self, *, request_id: str, actor_id: str,
                                application_id: str) -> dict[str, Any]:
        """会签已申报的支付申请。"""

        payload = {"actor_id": actor_id, "application_id": application_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "reviewer")
            application = self._application_row(connection, application_id)
            contract = self._contract_row(connection, application["contract_id"])
            self._check_org(actor, contract["organization_id"])
            period = self._period()
            self._ensure_period_open(connection, period)

            def create() -> tuple[str, str, dict[str, Any]]:
                if application["status"] != STATUS_SUBMITTED:
                    raise ConflictError("只有已申报的申请可以会签")
                connection.execute(
                    "UPDATE payment_applications SET status=? WHERE application_id=?",
                    (STATUS_COUNTERSIGNED, application_id),
                )
                self._append_entry(
                    connection, contract_id=application["contract_id"],
                    application_id=application_id, entry_type=ENTRY_COUNTERSIGNED, period=period,
                    amount_cents=application["claimed_amount_cents"],
                    detail={"claimed_amount_cents": application["claimed_amount_cents"]},
                    actor_id=actor_id)
                append_event(connection, actor_id=actor_id,
                             action="payment.application.countersigned",
                             resource_type="payment_application", resource_id=application_id,
                             detail={"contract_id": application["contract_id"]},
                             occurred_at=self._now())
                return "payment_application", application_id, {
                    "application_id": application_id, "status": STATUS_COUNTERSIGNED}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="payment.countersign_application",
                                           payload=payload, create=create)

    def approve_application(self, *, request_id: str, actor_id: str, application_id: str,
                            approved_acceptance_ids: list[str]) -> dict[str, Any]:
        """批准已会签的申请，支持按验收单部分批准。

        批准时在事务内重新校验累计量并扣减工程量分层；未批准的明细行
        释放证据占用，可随后续申请重新申报。
        """

        payload = {"actor_id": actor_id, "application_id": application_id,
                   "approved_acceptance_ids": approved_acceptance_ids}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "reviewer")
            application = self._application_row(connection, application_id)
            contract = self._contract_row(connection, application["contract_id"])
            self._check_org(actor, contract["organization_id"])
            period = self._period()
            self._ensure_period_open(connection, period)
            if not isinstance(approved_acceptance_ids, list) or not approved_acceptance_ids:
                raise ValidationError("approved_acceptance_ids 不能为空")
            approved_set = {self.domain._identifier(str(value), "acceptance_id")
                            for value in approved_acceptance_ids}

            def create() -> tuple[str, str, dict[str, Any]]:
                if application["status"] != STATUS_COUNTERSIGNED:
                    raise ConflictError("只有已会签的申请可以批准")
                lines = connection.execute(
                    "SELECT * FROM application_lines WHERE application_id=?", (application_id,)
                ).fetchall()
                line_acceptances = {line["acceptance_id"] for line in lines}
                unknown = approved_set - line_acceptances
                if unknown:
                    raise ValidationError("批准列表包含不属于本申请的验收单")
                approved_lines = [line for line in lines if line["acceptance_id"] in approved_set]
                by_item: dict[str, int] = {}
                for line in approved_lines:
                    by_item[line["item_id"]] = by_item.get(line["item_id"], 0) + line["quantity_milli"]
                in_flight = self._in_flight(connection, application["contract_id"], application_id)
                reasons: list[dict[str, Any]] = []
                for item_id, quantity in sorted(by_item.items()):
                    total, consumed = self._layer_totals(connection, item_id)
                    pending = in_flight.get(item_id, 0)
                    if consumed + pending + quantity > total:
                        reasons.append({"reason": REASON_EXCEEDS_QUANTITY,
                                        "item_id": item_id,
                                        "requested_milli": quantity,
                                        "available_milli": total - consumed - pending})
                if reasons:
                    connection.execute(
                        "UPDATE payment_applications SET status=?, pending_reasons_json=? "
                        "WHERE application_id=?",
                        (STATUS_PENDING, canonical_json(reasons), application_id),
                    )
                    self._release_occupations(connection, application_id, self._now())
                    self._append_entry(
                        connection, contract_id=application["contract_id"],
                        application_id=application_id, entry_type=ENTRY_PENDING, period=period,
                        amount_cents=0, detail={"reasons": reasons, "stage": "approval"},
                        actor_id=actor_id)
                    append_event(connection, actor_id=actor_id,
                                 action="payment.application.pending",
                                 resource_type="payment_application", resource_id=application_id,
                                 detail={"contract_id": application["contract_id"],
                                         "reasons": reasons, "stage": "approval"},
                                 occurred_at=self._now())
                    return "payment_application", application_id, {
                        "application_id": application_id, "status": STATUS_PENDING,
                        "reasons": reasons}
                per_source: dict[str, int] = {}
                approved_total = 0
                layer_details: dict[str, Any] = {}
                for item_id, quantity in sorted(by_item.items()):
                    item = connection.execute(
                        "SELECT unit_price_cents FROM contract_items WHERE item_id=?", (item_id,)
                    ).fetchone()
                    item_split, item_amount, details = self._attribute_layers(
                        connection, item_id, quantity, item["unit_price_cents"], consume=True)
                    for code, amount in item_split.items():
                        per_source[code] = per_source.get(code, 0) + amount
                    approved_total += item_amount
                    layer_details[item_id] = details
                for line in lines:
                    approved_qty = line["quantity_milli"] if line["acceptance_id"] in approved_set else 0
                    connection.execute(
                        "UPDATE application_lines SET approved_quantity_milli=? "
                        "WHERE application_id=? AND acceptance_id=?",
                        (approved_qty, application_id, line["acceptance_id"]),
                    )
                approved_invoices = {line["invoice_id"] for line in approved_lines}
                releasable: set[tuple[str, str]] = set()
                for line in lines:
                    if line["acceptance_id"] in approved_set:
                        continue
                    releasable.add((EVIDENCE_ACCEPTANCE, line["acceptance_id"]))
                    if line["invoice_id"] not in approved_invoices:
                        releasable.add((EVIDENCE_INVOICE, line["invoice_id"]))
                self._release_occupations(connection, application_id, self._now(), only=releasable)
                connection.execute(
                    "UPDATE payment_applications SET status=?, approved_amount_cents=? "
                    "WHERE application_id=?",
                    (STATUS_APPROVED, approved_total, application_id),
                )
                self._append_entry(
                    connection, contract_id=application["contract_id"],
                    application_id=application_id, entry_type=ENTRY_APPROVED, period=period,
                    amount_cents=approved_total,
                    detail={"funding_split": per_source,
                            "approved_acceptance_ids": sorted(approved_set),
                            "layers": layer_details},
                    actor_id=actor_id, splits=per_source)
                append_event(connection, actor_id=actor_id,
                             action="payment.application.approved",
                             resource_type="payment_application", resource_id=application_id,
                             detail={"contract_id": application["contract_id"],
                                     "approved_amount_cents": approved_total,
                                     "approved_lines": len(approved_lines),
                                     "total_lines": len(lines)},
                             occurred_at=self._now())
                return "payment_application", application_id, {
                    "application_id": application_id, "status": STATUS_APPROVED,
                    "approved_amount_cents": approved_total, "funding_split": per_source}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="payment.approve_application",
                                           payload=payload, create=create)

    def reject_application(self, *, request_id: str, actor_id: str, application_id: str,
                           reason: str) -> dict[str, Any]:
        """驳回申请并释放其证据占用，驳回本身以新分录保留。"""

        payload = {"actor_id": actor_id, "application_id": application_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "reviewer")
            application = self._application_row(connection, application_id)
            contract = self._contract_row(connection, application["contract_id"])
            self._check_org(actor, contract["organization_id"])
            period = self._period()
            self._ensure_period_open(connection, period)
            reason = self.domain._text(reason, "reason", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                if application["status"] not in (STATUS_SUBMITTED, STATUS_COUNTERSIGNED):
                    raise ConflictError("当前状态不能驳回")
                connection.execute(
                    "UPDATE payment_applications SET status=? WHERE application_id=?",
                    (STATUS_REJECTED, application_id),
                )
                self._release_occupations(connection, application_id, self._now())
                self._append_entry(
                    connection, contract_id=application["contract_id"],
                    application_id=application_id, entry_type=ENTRY_REJECTED, period=period,
                    amount_cents=0, detail={"reason": reason}, actor_id=actor_id)
                append_event(connection, actor_id=actor_id,
                             action="payment.application.rejected",
                             resource_type="payment_application", resource_id=application_id,
                             detail={"contract_id": application["contract_id"], "reason": reason},
                             occurred_at=self._now())
                return "payment_application", application_id, {
                    "application_id": application_id, "status": STATUS_REJECTED}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="payment.reject_application",
                                           payload=payload, create=create)

    def withdraw_application(self, *, request_id: str, actor_id: str,
                             application_id: str) -> dict[str, Any]:
        """撤回申请（含待处理申请）并释放证据占用，撤回收录为新分录。"""

        payload = {"actor_id": actor_id, "application_id": application_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "operator")
            application = self._application_row(connection, application_id)
            contract = self._contract_row(connection, application["contract_id"])
            self._check_org(actor, contract["organization_id"])
            period = self._period()
            self._ensure_period_open(connection, period)

            def create() -> tuple[str, str, dict[str, Any]]:
                if application["status"] not in (STATUS_PENDING, STATUS_SUBMITTED, STATUS_COUNTERSIGNED):
                    raise ConflictError("当前状态不能撤回")
                connection.execute(
                    "UPDATE payment_applications SET status=? WHERE application_id=?",
                    (STATUS_WITHDRAWN, application_id),
                )
                self._release_occupations(connection, application_id, self._now())
                self._append_entry(
                    connection, contract_id=application["contract_id"],
                    application_id=application_id, entry_type=ENTRY_WITHDRAWN, period=period,
                    amount_cents=0, detail={}, actor_id=actor_id)
                append_event(connection, actor_id=actor_id,
                             action="payment.application.withdrawn",
                             resource_type="payment_application", resource_id=application_id,
                             detail={"contract_id": application["contract_id"]},
                             occurred_at=self._now())
                return "payment_application", application_id, {
                    "application_id": application_id, "status": STATUS_WITHDRAWN}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="payment.withdraw_application",
                                           payload=payload, create=create)

    def record_payment(self, *, request_id: str, actor_id: str,
                       application_id: str) -> dict[str, Any]:
        """登记支付：按批准金额拆分净付款与质保金留置两笔分录。"""

        payload = {"actor_id": actor_id, "application_id": application_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "operator")
            application = self._application_row(connection, application_id)
            contract = self._contract_row(connection, application["contract_id"])
            self._check_org(actor, contract["organization_id"])
            period = self._period()
            self._ensure_period_open(connection, period)

            def create() -> tuple[str, str, dict[str, Any]]:
                if application["status"] != STATUS_APPROVED:
                    raise ConflictError("只有已批准的申请可以登记支付")
                approved = application["approved_amount_cents"]
                retention = approved * contract["retention_rate_bp"] // 10000
                net = approved - retention
                approved_entry = connection.execute(
                    "SELECT entry_id, amount_cents FROM ledger_entries WHERE application_id=? "
                    "AND entry_type=? ORDER BY rowid DESC LIMIT 1",
                    (application_id, ENTRY_APPROVED),
                ).fetchone()
                gross_splits = self._entry_splits(connection, approved_entry["entry_id"])
                retention_split = self._proportional(retention, gross_splits, approved) \
                    if retention else {}
                net_split = {code: amount - retention_split.get(code, 0)
                             for code, amount in gross_splits.items()}
                self._append_entry(
                    connection, contract_id=application["contract_id"],
                    application_id=application_id, entry_type=ENTRY_PAYMENT, period=period,
                    amount_cents=net,
                    detail={"approved_amount_cents": approved, "retention_cents": retention},
                    actor_id=actor_id, splits=net_split)
                if retention:
                    self._append_entry(
                        connection, contract_id=application["contract_id"],
                        application_id=application_id, entry_type=ENTRY_RETENTION_WITHHELD,
                        period=period, amount_cents=retention,
                        detail={"approved_amount_cents": approved},
                        actor_id=actor_id, splits=retention_split)
                connection.execute(
                    "UPDATE payment_applications SET status=? WHERE application_id=?",
                    (STATUS_PAID, application_id),
                )
                append_event(connection, actor_id=actor_id, action="payment.payment.recorded",
                             resource_type="payment_application", resource_id=application_id,
                             detail={"contract_id": application["contract_id"],
                                     "paid_amount_cents": net,
                                     "retention_withheld_cents": retention},
                             occurred_at=self._now())
                return "payment_application", application_id, {
                    "application_id": application_id, "status": STATUS_PAID,
                    "paid_amount_cents": net, "retention_withheld_cents": retention,
                    "payment_split": net_split, "retention_split": retention_split}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="payment.record_payment",
                                           payload=payload, create=create)

    def release_retention(self, *, request_id: str, actor_id: str, contract_id: str,
                          amount_cents: int) -> dict[str, Any]:
        """释放质保金：缺陷责任期满且（如要求）具备质量合格证明才允许。"""

        payload = {"actor_id": actor_id, "contract_id": contract_id, "amount_cents": amount_cents}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "operator")
            contract = self._contract_row(connection, contract_id)
            self._check_org(actor, contract["organization_id"])
            period = self._period()
            self._ensure_period_open(connection, period)
            amount = self._integer(amount_cents, "amount_cents", 1)

            def create() -> tuple[str, str, dict[str, Any]]:
                if contract["requires_quality_certificate"]:
                    certificate = connection.execute(
                        "SELECT 1 FROM quality_certificates WHERE contract_id=? LIMIT 1",
                        (contract_id,),
                    ).fetchone()
                    if certificate is None:
                        raise ConflictError("缺少质量合格证明，不能释放质保金")
                if self._now()[:10] < contract["defect_liability_end"]:
                    raise ConflictError("缺陷责任期未满，不能提前释放质保金")
                row = connection.execute(
                    "SELECT entry_type, COALESCE(SUM(amount_cents),0) AS amount FROM ledger_entries "
                    "WHERE contract_id=? AND entry_type IN (?,?) GROUP BY entry_type",
                    (contract_id, ENTRY_RETENTION_WITHHELD, ENTRY_RETENTION_RELEASED),
                ).fetchall()
                sums = {entry["entry_type"]: entry["amount"] for entry in row}
                held = sums.get(ENTRY_RETENTION_WITHHELD, 0) - sums.get(ENTRY_RETENTION_RELEASED, 0)
                if amount > held:
                    raise ConflictError("释放金额超过质保金留置余额")
                sources = self._sources_for(connection, "contract", contract_id)
                split = self._split_amount(amount, sources)
                self._append_entry(
                    connection, contract_id=contract_id, application_id=None,
                    entry_type=ENTRY_RETENTION_RELEASED, period=period, amount_cents=amount,
                    detail={"held_before_cents": held}, actor_id=actor_id, splits=split)
                append_event(connection, actor_id=actor_id, action="payment.retention.released",
                             resource_type="contract", resource_id=contract_id,
                             detail={"amount_cents": amount, "held_before_cents": held},
                             occurred_at=self._now())
                return "contract", contract_id, {
                    "contract_id": contract_id, "released_amount_cents": amount,
                    "retention_held_cents": held - amount, "funding_split": split}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="payment.release_retention",
                                           payload=payload, create=create)

    def recover_payment(self, *, request_id: str, actor_id: str, application_id: str,
                        amount_cents: int, reason: str) -> dict[str, Any]:
        """审计追回：以负数新分录冲减已付款，不修改原支付分录。"""

        payload = {"actor_id": actor_id, "application_id": application_id,
                   "amount_cents": amount_cents, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "auditor")
            application = self._application_row(connection, application_id)
            contract = self._contract_row(connection, application["contract_id"])
            self._check_org(actor, contract["organization_id"])
            period = self._period()
            self._ensure_period_open(connection, period)
            amount = self._integer(amount_cents, "amount_cents", 1)
            reason = self.domain._text(reason, "reason", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                if application["status"] != STATUS_PAID:
                    raise ConflictError("只有已支付的申请可以追回")
                paid_row = connection.execute(
                    "SELECT COALESCE(SUM(amount_cents),0) AS amount FROM ledger_entries "
                    "WHERE application_id=? AND entry_type=?",
                    (application_id, ENTRY_PAYMENT),
                ).fetchone()
                recovered_row = connection.execute(
                    "SELECT COALESCE(SUM(amount_cents),0) AS amount FROM ledger_entries "
                    "WHERE application_id=? AND entry_type=?",
                    (application_id, ENTRY_RECOVERY),
                ).fetchone()
                paid_net = paid_row["amount"] + recovered_row["amount"]
                if amount > paid_net:
                    raise ConflictError("追回金额超过已支付净额")
                payment_entry = connection.execute(
                    "SELECT entry_id, amount_cents FROM ledger_entries WHERE application_id=? "
                    "AND entry_type=? ORDER BY rowid DESC LIMIT 1",
                    (application_id, ENTRY_PAYMENT),
                ).fetchone()
                payment_splits = self._entry_splits(connection, payment_entry["entry_id"])
                split = self._proportional(amount, payment_splits, payment_entry["amount_cents"])
                negative_split = {code: -value for code, value in split.items()}
                self._append_entry(
                    connection, contract_id=application["contract_id"],
                    application_id=application_id, entry_type=ENTRY_RECOVERY, period=period,
                    amount_cents=-amount, detail={"reason": reason},
                    actor_id=actor_id, splits=negative_split)
                append_event(connection, actor_id=actor_id, action="payment.recovery.recorded",
                             resource_type="payment_application", resource_id=application_id,
                             detail={"contract_id": application["contract_id"],
                                     "amount_cents": amount, "reason": reason},
                             occurred_at=self._now())
                return "payment_application", application_id, {
                    "application_id": application_id, "recovered_amount_cents": amount,
                    "paid_net_cents": paid_net - amount, "funding_split": negative_split}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="payment.recover_payment",
                                           payload=payload, create=create)

    def correct_entry(self, *, request_id: str, actor_id: str, entry_id: str,
                      amount_cents: int, reason: str) -> dict[str, Any]:
        """更正既有分录：更正在当前开放期间新立分录，原期间分录保持不动。"""

        payload = {"actor_id": actor_id, "entry_id": entry_id,
                   "amount_cents": amount_cents, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "admin", "operator")
            original = connection.execute(
                "SELECT * FROM ledger_entries WHERE entry_id=?", (entry_id,)
            ).fetchone()
            if original is None:
                raise NotFoundError("原始分录不存在")
            contract = self._contract_row(connection, original["contract_id"])
            self._check_org(actor, contract["organization_id"])
            period = self._period()
            self._ensure_period_open(connection, period)
            amount = self._integer(amount_cents, "amount_cents")
            if amount == 0:
                raise ValidationError("更正金额不能为零")
            reason = self.domain._text(reason, "reason", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                if original["entry_type"] not in CORRECTABLE_ENTRY_TYPES:
                    raise ValidationError("该分录类型不支持更正")
                original_splits = self._entry_splits(connection, entry_id)
                splits = self._proportional(amount, original_splits,
                                            original["amount_cents"]) if original_splits else None
                correction_id = self._append_entry(
                    connection, contract_id=original["contract_id"],
                    application_id=original["application_id"], entry_type=ENTRY_CORRECTION,
                    period=period, amount_cents=amount,
                    detail={"reason": reason, "original_period": original["period"],
                            "original_entry_type": original["entry_type"]},
                    actor_id=actor_id, references_entry_id=entry_id, splits=splits)
                append_event(connection, actor_id=actor_id, action="payment.correction.recorded",
                             resource_type="ledger_entry", resource_id=correction_id,
                             detail={"contract_id": original["contract_id"],
                                     "original_entry_id": entry_id,
                                     "original_period": original["period"],
                                     "correction_period": period, "amount_cents": amount},
                             occurred_at=self._now())
                return "ledger_entry", correction_id, {
                    "entry_id": correction_id, "original_entry_id": entry_id,
                    "original_period": original["period"], "correction_period": period,
                    "amount_cents": amount}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="payment.correct_entry",
                                           payload=payload, create=create)

    def close_period(self, *, request_id: str, actor_id: str, period: str) -> dict[str, Any]:
        """关闭会计期间：关账后该期间不再接受任何新分录。"""

        payload = {"actor_id": actor_id, "period": period}
        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "admin")
            period = str(period).strip()
            if not PERIOD_FORMAT.fullmatch(period):
                raise ValidationError("period 必须是 YYYY-MM 格式")
            if period > self._period():
                raise ValidationError("不能关闭未来期间")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT status FROM payment_periods WHERE period=?", (period,)
                ).fetchone()
                if existing:
                    raise ConflictError("期间已关账")
                connection.execute(
                    "INSERT INTO payment_periods(period,status,closed_by,closed_at) "
                    "VALUES(?,'closed',?,?)",
                    (period, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="payment.period.closed",
                             resource_type="payment_period", resource_id=period,
                             detail={}, occurred_at=self._now())
                return "payment_period", period, {"period": period, "status": "closed"}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="payment.close_period",
                                           payload=payload, create=create)

    # ------------------------------------------------------------------
    # 查询：全过程解释、证据反查与平衡校验
    # ------------------------------------------------------------------

    def explain_application(self, application_id: str) -> dict[str, Any]:
        """解释一笔款项从申报、会签到支付和留置的全过程。"""

        connection = self.database.connection
        application = connection.execute(
            "SELECT * FROM payment_applications WHERE application_id=?", (application_id,)
        ).fetchone()
        if application is None:
            raise NotFoundError("支付申请不存在")
        lines = connection.execute(
            "SELECT al.*, a.acceptance_no, i.invoice_no, ci.item_code "
            "FROM application_lines al "
            "JOIN acceptances a ON a.acceptance_id=al.acceptance_id "
            "JOIN invoices i ON i.invoice_id=al.invoice_id "
            "JOIN contract_items ci ON ci.item_id=al.item_id "
            "WHERE al.application_id=? ORDER BY al.acceptance_id",
            (application_id,),
        ).fetchall()
        occupations = connection.execute(
            "SELECT * FROM evidence_occupations WHERE application_id=? ORDER BY occupied_at",
            (application_id,),
        ).fetchall()
        entries = connection.execute(
            "SELECT * FROM ledger_entries WHERE application_id=? ORDER BY rowid",
            (application_id,),
        ).fetchall()
        timeline = []
        for entry in entries:
            timeline.append({
                "entry_id": entry["entry_id"],
                "entry_type": entry["entry_type"],
                "period": entry["period"],
                "amount_cents": entry["amount_cents"],
                "funding_split": self._entry_splits(connection, entry["entry_id"]),
                "detail": json.loads(entry["detail_json"]),
                "references_entry_id": entry["references_entry_id"],
                "created_by": entry["created_by"],
                "created_at": entry["created_at"],
            })
        paid = sum(entry["amount_cents"] for entry in entries
                   if entry["entry_type"] == ENTRY_PAYMENT)
        withheld = sum(entry["amount_cents"] for entry in entries
                       if entry["entry_type"] == ENTRY_RETENTION_WITHHELD)
        recovered = -sum(entry["amount_cents"] for entry in entries
                         if entry["entry_type"] == ENTRY_RECOVERY)
        return {
            "application": {
                "application_id": application["application_id"],
                "contract_id": application["contract_id"],
                "period": application["period"],
                "status": application["status"],
                "claimed_amount_cents": application["claimed_amount_cents"],
                "approved_amount_cents": application["approved_amount_cents"],
                "pending_reasons": json.loads(application["pending_reasons_json"]),
                "created_by": application["created_by"],
                "created_at": application["created_at"],
            },
            "lines": [{
                "acceptance_id": line["acceptance_id"],
                "acceptance_no": line["acceptance_no"],
                "invoice_id": line["invoice_id"],
                "invoice_no": line["invoice_no"],
                "item_code": line["item_code"],
                "quantity_milli": line["quantity_milli"],
                "approved_quantity_milli": line["approved_quantity_milli"],
            } for line in lines],
            "occupations": [{
                "evidence_type": row["evidence_type"],
                "evidence_id": row["evidence_id"],
                "status": row["status"],
                "occupied_at": row["occupied_at"],
                "released_at": row["released_at"],
            } for row in occupations],
            "entries": timeline,
            "summary": {
                "claimed_amount_cents": application["claimed_amount_cents"],
                "approved_amount_cents": application["approved_amount_cents"],
                "paid_amount_cents": paid,
                "retention_withheld_cents": withheld,
                "recovered_amount_cents": recovered,
            },
        }

    def trace_evidence(self, evidence_type: str, evidence_id: str) -> dict[str, Any]:
        """沿证据反查其全部占用及对应申请，供审计核对。"""

        connection = self.database.connection
        if evidence_type not in (EVIDENCE_ACCEPTANCE, EVIDENCE_INVOICE):
            raise ValidationError("evidence_type 必须是 acceptance 或 invoice")
        table = "acceptances" if evidence_type == EVIDENCE_ACCEPTANCE else "invoices"
        evidence = connection.execute(
            f"SELECT * FROM {table} WHERE {'acceptance_id' if evidence_type == EVIDENCE_ACCEPTANCE else 'invoice_id'}=?",
            (evidence_id,),
        ).fetchone()
        if evidence is None:
            raise NotFoundError("证据不存在")
        occupations = connection.execute(
            "SELECT * FROM evidence_occupations WHERE evidence_type=? AND evidence_id=? "
            "ORDER BY occupied_at",
            (evidence_type, evidence_id),
        ).fetchall()
        applications = []
        for occupation in occupations:
            application = connection.execute(
                "SELECT application_id, contract_id, status, claimed_amount_cents, "
                "approved_amount_cents FROM payment_applications WHERE application_id=?",
                (occupation["application_id"],),
            ).fetchone()
            applications.append({
                "application_id": application["application_id"],
                "contract_id": application["contract_id"],
                "status": application["status"],
                "claimed_amount_cents": application["claimed_amount_cents"],
                "approved_amount_cents": application["approved_amount_cents"],
                "occupation_status": occupation["status"],
                "occupied_at": occupation["occupied_at"],
                "released_at": occupation["released_at"],
            })
        return {"evidence": dict(evidence), "evidence_type": evidence_type,
                "occupations": applications}

    def list_applications(self, contract_id: str) -> list[dict[str, Any]]:
        """列出合同下的全部支付申请。"""

        rows = self.database.connection.execute(
            "SELECT * FROM payment_applications WHERE contract_id=? ORDER BY created_at, application_id",
            (contract_id,),
        ).fetchall()
        return [{
            "application_id": row["application_id"],
            "period": row["period"],
            "status": row["status"],
            "claimed_amount_cents": row["claimed_amount_cents"],
            "approved_amount_cents": row["approved_amount_cents"],
            "pending_reasons": json.loads(row["pending_reasons_json"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        } for row in rows]

    def get_contract(self, contract_id: str) -> dict[str, Any]:
        """返回合同台账：清单、工程量版本、出资比例与质保条件。"""

        connection = self.database.connection
        contract = self._contract_row(connection, contract_id)
        items = connection.execute(
            "SELECT * FROM contract_items WHERE contract_id=? ORDER BY item_code", (contract_id,)
        ).fetchall()
        item_views = []
        for item in items:
            version = connection.execute(
                "SELECT version, quantity_milli FROM quantity_versions WHERE item_id=? "
                "ORDER BY version DESC LIMIT 1",
                (item["item_id"],),
            ).fetchone()
            total, consumed = self._layer_totals(connection, item["item_id"])
            item_views.append({
                "item_code": item["item_code"],
                "name": item["name"],
                "unit": item["unit"],
                "unit_price_cents": item["unit_price_cents"],
                "current_version": version["version"],
                "current_quantity_milli": version["quantity_milli"],
                "consumed_milli": consumed,
            })
        changes = connection.execute(
            "SELECT change_id, change_no, status, description FROM change_orders "
            "WHERE contract_id=? ORDER BY created_at",
            (contract_id,),
        ).fetchall()
        return {
            "contract_id": contract["contract_id"],
            "site_id": contract["site_id"],
            "contract_no": contract["contract_no"],
            "name": contract["name"],
            "contractor": contract["contractor"],
            "status": contract["status"],
            "retention_rate_bp": contract["retention_rate_bp"],
            "defect_liability_end": contract["defect_liability_end"],
            "requires_quality_certificate": bool(contract["requires_quality_certificate"]),
            "funding_sources": [
                {"source_code": code, "ratio_bp": ratio}
                for code, ratio in self._sources_for(connection, "contract", contract_id)
            ],
            "items": item_views,
            "change_orders": [dict(row) for row in changes],
        }

    def contract_ledger(self, contract_id: str) -> list[dict[str, Any]]:
        """返回合同的全部追加式分录（含资金份额拆分）。"""

        connection = self.database.connection
        self._contract_row(connection, contract_id)
        entries = connection.execute(
            "SELECT * FROM ledger_entries WHERE contract_id=? ORDER BY rowid", (contract_id,)
        ).fetchall()
        return [{
            "entry_id": entry["entry_id"],
            "application_id": entry["application_id"],
            "entry_type": entry["entry_type"],
            "period": entry["period"],
            "amount_cents": entry["amount_cents"],
            "funding_split": self._entry_splits(connection, entry["entry_id"]),
            "detail": json.loads(entry["detail_json"]),
            "references_entry_id": entry["references_entry_id"],
            "created_by": entry["created_by"],
            "created_at": entry["created_at"],
        } for entry in entries]

    def contract_balance(self, contract_id: str) -> dict[str, Any]:
        """核验合同金额、已付金额与剩余义务始终平衡。

        从追加式分录重算各组成部分，并独立检查：批准不超过合同金额、
        质保金留置不为负、已计量不超过工程量版本、证据占用不重复、
        审计哈希链完整。
        """

        connection = self.database.connection
        contract = self._contract_row(connection, contract_id)
        items = connection.execute(
            "SELECT * FROM contract_items WHERE contract_id=?", (contract_id,)
        ).fetchall()
        contract_amount = 0
        quantities_ok = True
        for item in items:
            version = connection.execute(
                "SELECT quantity_milli FROM quantity_versions WHERE item_id=? "
                "ORDER BY version DESC LIMIT 1",
                (item["item_id"],),
            ).fetchone()
            contract_amount += version["quantity_milli"] * item["unit_price_cents"] // 1000
            _, consumed = self._layer_totals(connection, item["item_id"])
            if consumed > version["quantity_milli"]:
                quantities_ok = False
        entries = connection.execute(
            "SELECT * FROM ledger_entries WHERE contract_id=? ORDER BY rowid", (contract_id,)
        ).fetchall()
        by_id = {entry["entry_id"]: entry for entry in entries}

        def bucket(entry: Any) -> str:
            seen: set[str] = set()
            current = entry
            while current["entry_type"] == ENTRY_CORRECTION and current["entry_id"] not in seen:
                seen.add(current["entry_id"])
                target = by_id.get(current["references_entry_id"])
                if target is None:
                    break
                current = target
            return current["entry_type"]

        sums: dict[str, int] = {}
        source_sums: dict[str, dict[str, int]] = {}
        for entry in entries:
            kind = bucket(entry)
            sums[kind] = sums.get(kind, 0) + entry["amount_cents"]
            for code, amount in self._entry_splits(connection, entry["entry_id"]).items():
                source_sums.setdefault(code, {})
                source_sums[code][kind] = source_sums[code].get(kind, 0) + amount
        approved = sums.get(ENTRY_APPROVED, 0)
        payments = sums.get(ENTRY_PAYMENT, 0)
        withheld = sums.get(ENTRY_RETENTION_WITHHELD, 0)
        released = sums.get(ENTRY_RETENTION_RELEASED, 0)
        recovered = -sums.get(ENTRY_RECOVERY, 0)
        cash_paid = payments + released - recovered
        retention_held = withheld - released
        unpaid_approved = approved - cash_paid - retention_held
        remaining_obligation = contract_amount - cash_paid - retention_held
        duplicate = connection.execute(
            "SELECT evidence_type, evidence_id, COUNT(*) AS count FROM evidence_occupations "
            "WHERE status='active' GROUP BY evidence_type, evidence_id HAVING COUNT(*)>1"
        ).fetchall()
        audit_valid, audit_events = verify_chain(connection)
        checks = {
            "approved_within_contract": approved <= contract_amount,
            "retention_held_non_negative": retention_held >= 0,
            "unpaid_approved_non_negative": unpaid_approved >= 0,
            "remaining_obligation_non_negative": remaining_obligation >= 0,
            "quantities_within_versions": quantities_ok,
            "occupations_unique": len(duplicate) == 0,
            "audit_chain_valid": audit_valid,
        }
        return {
            "contract_id": contract_id,
            "contract_amount_cents": contract_amount,
            "approved_amount_cents": approved,
            "cash_paid_cents": cash_paid,
            "retention_held_cents": retention_held,
            "recovered_amount_cents": recovered,
            "unpaid_approved_cents": unpaid_approved,
            "remaining_obligation_cents": remaining_obligation,
            "funding_sources": [
                {"source_code": code,
                 "approved_cents": kinds.get(ENTRY_APPROVED, 0),
                 "cash_paid_cents": kinds.get(ENTRY_PAYMENT, 0)
                 + kinds.get(ENTRY_RETENTION_RELEASED, 0) + kinds.get(ENTRY_RECOVERY, 0),
                 "retention_held_cents": kinds.get(ENTRY_RETENTION_WITHHELD, 0)
                 - kinds.get(ENTRY_RETENTION_RELEASED, 0)}
                for code, kinds in sorted(source_sums.items())
            ],
            "checks": checks,
            "audit_events": audit_events,
            "balanced": all(checks.values()),
        }
