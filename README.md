# 核验交通建设进度支付协同基础服务

本项目提供综合交通运输业务共享的服务端基础能力，负责运营机构、交通节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域服务可以在这些稳定边界上扩展自己的状态、规则和接口。

## 目录

- `src/transport_coordination/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `tests/`：基础规则、事务边界、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m transport_coordination.acceptance
```

验收命令会在临时 SQLite 数据库中登记运营机构、操作者、交通节点和参考资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

## 建设项目进度支付核验服务

`src/progress_payment/` 是在同一套架构约定（SQLite 事务、请求幂等、哈希链审计、无框架 HTTP）上实现的完整领域服务，把合同清单、工程量版本、现场验收、发票、变更令、共同出资比例和质保条件关联到可追踪的支付申请。

### 领域规则

- **同一事务三校验**：每笔支付申请在一个事务内完成证据占用（同一批材料/验收单/发票只能被一笔有效申请占用，由部分唯一索引兜底）、累计量校验（已占用量 + 本次申报量不得超过当前工程量版本）和资金份额拆分（按出资基点用最大余数法精确到分）；
- **待处理而非付款**：重复材料（`duplicate_evidence`）、超合同计量（`quantity_exceeds_contract`）或缺少独立验收（`missing_independent_acceptance`）的申请进入 `pending`，不占用证据、不生成付款；
- **追加式台账**：撤回、驳回、部分批准、支付、质保金留置/释放和审计追回全部以新分录保留；会计期间关闭后，更正只能落在当前开放期间并通过 `corrects_entry_id` 引用原分录，原期间不被改写；
- **质保条件**：质保金在会签批准时按比例留置，只有到达合同约定释放日期后才能释放，防止提前释放；
- **变更令**：批准后生成新的工程量版本，可携带独立于项目默认比例的出资拆分（设计变更跨越不同资金来源），且变更后工程量不得低于已被申请占用的累计量；
- **平衡恒等式**：合同金额 = 净已付 + 质保金留置 + 应付未付 + 剩余义务，`GET /contract-balance` 从台账分录重算并逐项校验；
- **可追溯**：`GET /payment-application?id=` 给出申报、会签、支付、留置的全部分录与资金拆分；`GET /evidence-occupations?evidence_id=` 让审计员沿证据反查所有占用。

### 运行

```bash
PYTHONPATH=src python3 -m progress_payment.acceptance
PYTHONPATH=src python3 -m progress_payment.api --database progress_payment.sqlite3 --host 127.0.0.1 --port 8081
```

角色约定：`admin`（建档/期间）、`applicant`（申报/撤回）、`inspector`（独立验收）、`reviewer`（会签/质保释放）、`finance`（支付/更正）、`auditor`（审计追回）。金额一律为整数分，工程量为千分之一单位，出资比例为基点（合计 10000）。
