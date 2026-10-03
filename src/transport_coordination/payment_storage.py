"""进度支付核验服务的 SQLite 表结构。

金额一律使用整数分（cents），出资比例使用基点（万分之一），工程量使用
千分之一单位（milli），避免浮点误差进入资金核算。所有资金变动写入
ledger_entries 追加式分录，历史分录不允许更新或删除。
"""

from __future__ import annotations

PAYMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts (
    contract_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    contract_no TEXT NOT NULL,
    name TEXT NOT NULL,
    contractor TEXT NOT NULL,
    retention_rate_bp INTEGER NOT NULL CHECK(retention_rate_bp BETWEEN 0 AND 10000),
    defect_liability_end TEXT NOT NULL,
    requires_quality_certificate INTEGER NOT NULL CHECK(requires_quality_certificate IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'active',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(site_id, contract_no)
);
CREATE TABLE IF NOT EXISTS contract_items (
    item_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    item_code TEXT NOT NULL,
    name TEXT NOT NULL,
    unit TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL CHECK(unit_price_cents > 0),
    created_at TEXT NOT NULL,
    UNIQUE(contract_id, item_code)
);
CREATE TABLE IF NOT EXISTS funding_sources (
    owner_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    source_code TEXT NOT NULL,
    ratio_bp INTEGER NOT NULL CHECK(ratio_bp > 0),
    PRIMARY KEY(owner_type, owner_id, source_code)
);
CREATE TABLE IF NOT EXISTS quantity_layers (
    layer_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES contract_items(item_id),
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    quantity_milli INTEGER NOT NULL CHECK(quantity_milli >= 0),
    consumed_milli INTEGER NOT NULL DEFAULT 0 CHECK(consumed_milli >= 0),
    sequence INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS quantity_versions (
    version_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES contract_items(item_id),
    version INTEGER NOT NULL,
    quantity_milli INTEGER NOT NULL CHECK(quantity_milli >= 0),
    change_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(item_id, version)
);
CREATE TABLE IF NOT EXISTS change_orders (
    change_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    change_no TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    UNIQUE(contract_id, change_no)
);
CREATE TABLE IF NOT EXISTS change_order_lines (
    change_id TEXT NOT NULL REFERENCES change_orders(change_id),
    item_id TEXT NOT NULL REFERENCES contract_items(item_id),
    quantity_delta_milli INTEGER NOT NULL,
    PRIMARY KEY(change_id, item_id)
);
CREATE TABLE IF NOT EXISTS acceptances (
    acceptance_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    item_id TEXT NOT NULL REFERENCES contract_items(item_id),
    acceptance_no TEXT NOT NULL,
    quantity_milli INTEGER NOT NULL CHECK(quantity_milli > 0),
    accepted_on TEXT NOT NULL,
    inspector TEXT NOT NULL,
    independent INTEGER NOT NULL CHECK(independent IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(contract_id, acceptance_no)
);
CREATE TABLE IF NOT EXISTS invoices (
    invoice_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    invoice_no TEXT NOT NULL UNIQUE,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    issued_on TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quality_certificates (
    certificate_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    certificate_no TEXT NOT NULL UNIQUE,
    issued_on TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_applications (
    application_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    period TEXT NOT NULL,
    status TEXT NOT NULL,
    claimed_amount_cents INTEGER NOT NULL,
    approved_amount_cents INTEGER NOT NULL DEFAULT 0,
    pending_reasons_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS application_lines (
    application_id TEXT NOT NULL REFERENCES payment_applications(application_id),
    acceptance_id TEXT NOT NULL REFERENCES acceptances(acceptance_id),
    invoice_id TEXT NOT NULL REFERENCES invoices(invoice_id),
    item_id TEXT NOT NULL REFERENCES contract_items(item_id),
    quantity_milli INTEGER NOT NULL CHECK(quantity_milli > 0),
    approved_quantity_milli INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(application_id, acceptance_id)
);
CREATE TABLE IF NOT EXISTS evidence_occupations (
    occupation_id TEXT PRIMARY KEY,
    evidence_type TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    application_id TEXT NOT NULL REFERENCES payment_applications(application_id),
    status TEXT NOT NULL,
    occupied_at TEXT NOT NULL,
    released_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_active
    ON evidence_occupations(evidence_type, evidence_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    application_id TEXT,
    entry_type TEXT NOT NULL,
    period TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    references_entry_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_contract ON ledger_entries(contract_id);
CREATE INDEX IF NOT EXISTS idx_ledger_application ON ledger_entries(application_id);
CREATE TABLE IF NOT EXISTS funding_splits (
    entry_id TEXT NOT NULL REFERENCES ledger_entries(entry_id),
    source_code TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    PRIMARY KEY(entry_id, source_code)
);
CREATE TABLE IF NOT EXISTS payment_periods (
    period TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    closed_by TEXT,
    closed_at TEXT
);
"""
