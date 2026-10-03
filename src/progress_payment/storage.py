"""封装进度支付核验服务的 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(organization_id, code)
);
CREATE TABLE IF NOT EXISTS funding_sources (
    funding_source_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    ratio_bp INTEGER NOT NULL CHECK(ratio_bp > 0),
    UNIQUE(project_id, code)
);
CREATE TABLE IF NOT EXISTS contracts (
    contract_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    contractor_name TEXT NOT NULL,
    retention_rate_bp INTEGER NOT NULL CHECK(retention_rate_bp >= 0 AND retention_rate_bp <= 10000),
    retention_release_after TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, code)
);
CREATE TABLE IF NOT EXISTS boq_items (
    item_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    item_code TEXT NOT NULL,
    name TEXT NOT NULL,
    unit TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL CHECK(unit_price_cents >= 0),
    base_quantity_milli INTEGER NOT NULL CHECK(base_quantity_milli >= 0),
    origin TEXT NOT NULL CHECK(origin IN ('base', 'change_order')),
    change_order_id TEXT,
    UNIQUE(contract_id, item_code)
);
CREATE TABLE IF NOT EXISTS change_orders (
    change_order_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    code TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft', 'approved')),
    funding_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    UNIQUE(contract_id, code)
);
CREATE TABLE IF NOT EXISTS change_order_adjustments (
    change_order_id TEXT NOT NULL REFERENCES change_orders(change_order_id),
    item_id TEXT NOT NULL REFERENCES boq_items(item_id),
    delta_quantity_milli INTEGER NOT NULL,
    PRIMARY KEY(change_order_id, item_id)
);
CREATE TABLE IF NOT EXISTS change_order_new_items (
    change_order_id TEXT NOT NULL REFERENCES change_orders(change_order_id),
    item_code TEXT NOT NULL,
    name TEXT NOT NULL,
    unit TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL CHECK(unit_price_cents >= 0),
    quantity_milli INTEGER NOT NULL CHECK(quantity_milli > 0),
    PRIMARY KEY(change_order_id, item_code)
);
CREATE TABLE IF NOT EXISTS quantity_versions (
    version_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    change_order_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(contract_id, version_no)
);
CREATE TABLE IF NOT EXISTS quantity_version_items (
    version_id TEXT NOT NULL REFERENCES quantity_versions(version_id),
    item_id TEXT NOT NULL REFERENCES boq_items(item_id),
    quantity_milli INTEGER NOT NULL CHECK(quantity_milli >= 0),
    PRIMARY KEY(version_id, item_id)
);
CREATE TABLE IF NOT EXISTS periods (
    period_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'closed')),
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    closed_by TEXT,
    UNIQUE(project_id, name)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_period_per_project
    ON periods(project_id) WHERE status = 'open';
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    evidence_type TEXT NOT NULL CHECK(evidence_type IN ('material_acceptance', 'site_acceptance', 'invoice')),
    external_key TEXT NOT NULL,
    independent INTEGER NOT NULL CHECK(independent IN (0, 1)),
    boq_item_id TEXT,
    quantity_milli INTEGER CHECK(quantity_milli IS NULL OR quantity_milli >= 0),
    amount_cents INTEGER CHECK(amount_cents IS NULL OR amount_cents >= 0),
    detail_json TEXT NOT NULL,
    registered_by TEXT NOT NULL REFERENCES actors(actor_id),
    registered_at TEXT NOT NULL,
    UNIQUE(project_id, evidence_type, external_key)
);
CREATE TABLE IF NOT EXISTS payment_applications (
    application_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    period_id TEXT NOT NULL REFERENCES periods(period_id),
    quantity_version_id TEXT NOT NULL REFERENCES quantity_versions(version_id),
    status TEXT NOT NULL CHECK(status IN
        ('pending', 'submitted', 'approved', 'partially_approved', 'rejected', 'withdrawn', 'paid')),
    pending_reasons_json TEXT NOT NULL,
    gross_cents INTEGER NOT NULL CHECK(gross_cents >= 0),
    retention_cents INTEGER NOT NULL CHECK(retention_cents >= 0),
    net_cents INTEGER NOT NULL CHECK(net_cents >= 0),
    submitted_by TEXT NOT NULL REFERENCES actors(actor_id),
    submitted_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS application_lines (
    line_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES payment_applications(application_id),
    item_id TEXT NOT NULL REFERENCES boq_items(item_id),
    change_order_id TEXT,
    quantity_milli INTEGER NOT NULL CHECK(quantity_milli > 0),
    unit_price_cents INTEGER NOT NULL CHECK(unit_price_cents >= 0),
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
    retention_cents INTEGER NOT NULL CHECK(retention_cents >= 0),
    decision TEXT NOT NULL CHECK(decision IN ('pending', 'approved', 'rejected')) DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS application_line_evidence (
    line_id TEXT NOT NULL REFERENCES application_lines(line_id),
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    PRIMARY KEY(line_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS application_invoices (
    application_id TEXT NOT NULL REFERENCES payment_applications(application_id),
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    PRIMARY KEY(application_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS evidence_occupations (
    occupation_id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    application_id TEXT NOT NULL REFERENCES payment_applications(application_id),
    occupied_at TEXT NOT NULL,
    released_at TEXT,
    release_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_occupation_per_evidence
    ON evidence_occupations(evidence_id) WHERE released_at IS NULL;
CREATE TABLE IF NOT EXISTS funding_allocations (
    allocation_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES payment_applications(application_id),
    funding_source_id TEXT NOT NULL REFERENCES funding_sources(funding_source_id),
    stage TEXT NOT NULL CHECK(stage IN ('planned', 'actual', 'retention_release', 'audit_recovery')),
    amount_cents INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_entries (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    contract_id TEXT REFERENCES contracts(contract_id),
    application_id TEXT REFERENCES payment_applications(application_id),
    period_id TEXT NOT NULL REFERENCES periods(period_id),
    kind TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    corrects_entry_id TEXT,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
