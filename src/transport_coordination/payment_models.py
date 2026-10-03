"""定义进度支付核验服务使用的状态、分录类型与待处理原因。"""

# 支付申请状态机：pending/submitted/countersigned 可撤回；
# submitted/countersigned 可驳回；countersigned 可批准；approved 可支付。
STATUS_PENDING = "pending"
STATUS_SUBMITTED = "submitted"
STATUS_COUNTERSIGNED = "countersigned"
STATUS_APPROVED = "approved"
STATUS_PAID = "paid"
STATUS_WITHDRAWN = "withdrawn"
STATUS_REJECTED = "rejected"

# 追加式分录类型：所有资金变动都以新分录保留，绝不改写历史分录。
ENTRY_SUBMITTED = "application.submitted"
ENTRY_PENDING = "application.pending"
ENTRY_COUNTERSIGNED = "application.countersigned"
ENTRY_APPROVED = "application.approved"
ENTRY_REJECTED = "application.rejected"
ENTRY_WITHDRAWN = "application.withdrawn"
ENTRY_PAYMENT = "payment.recorded"
ENTRY_RETENTION_WITHHELD = "retention.withheld"
ENTRY_RETENTION_RELEASED = "retention.released"
ENTRY_RECOVERY = "audit.recovered"
ENTRY_CORRECTION = "correction"

# 允许被更正分录引用的原始分录类型（更正只针对资金类分录）。
CORRECTABLE_ENTRY_TYPES = frozenset({
    ENTRY_APPROVED,
    ENTRY_PAYMENT,
    ENTRY_RETENTION_WITHHELD,
    ENTRY_RETENTION_RELEASED,
    ENTRY_RECOVERY,
})

# 申报核验不通过时进入待处理的原因编码。
REASON_DUPLICATE_EVIDENCE = "duplicate_evidence"
REASON_DUPLICATE_MATERIAL = "duplicate_material"
REASON_EXCEEDS_QUANTITY = "exceeds_contract_quantity"
REASON_MISSING_INDEPENDENT = "missing_independent_acceptance"

# 可作为支付证据被占用的资料类别。
EVIDENCE_ACCEPTANCE = "acceptance"
EVIDENCE_INVOICE = "invoice"
