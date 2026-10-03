# 核验交通建设进度支付协同基础服务

本项目提供综合交通运输业务共享的服务端基础能力，负责运营机构、交通节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域服务可以在这些稳定边界上扩展自己的状态、规则和接口。

## 进度支付核验服务

在基础边界之上实现了建设项目进度支付核验（`payment_service.py`）：把合同清单、工程量版本、现场验收、发票、变更令、共同出资比例和质保条件关联到可追踪的支付申请。

- **同一事务核验**：每笔申报在一个事务中完成证据占用（验收单、发票同一时刻只能被一笔有效申请占用）、累计量校验（已计量 + 在途 + 本次不超过当前工程量版本）和资金份额拆分（按工程量分层归属合同或变更令的出资比例，末位来源承接取整差额）。
- **待处理而不是付款**：重复材料（含总包与分包共用同一验收单号）、超合同计量、缺少独立验收的申请进入 `pending`，不占用证据、不生成付款；批准时点会重新校验累计量。
- **追加式分录**：撤回、驳回、部分批准、支付、质保金留置/释放、审计追回全部写入 `ledger_entries` 新分录，历史分录绝不改写；关账后的更正在当前开放期间新立分录并引用原分录。
- **质保条件**：缺陷责任期未满或缺少质量合格证明时，质保金释放被拒绝，防止提前释放。
- **可解释、可反查**：`GET /payment/applications?application_id=` 还原一笔款项从申报、会签到支付和留置的全过程；`GET /payment/evidence` 沿证据反查全部占用；`GET /payment/balance` 重算合同金额、已付金额、质保金留置与剩余义务并逐项校验平衡。

金额使用整数分，出资比例使用基点（万分之一），工程量使用千分之一单位，避免浮点误差进入资金核算。

## 目录

- `src/transport_coordination/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/transport_coordination/payment_*.py`：进度支付核验的模型、表结构、服务、路由与离线验收；
- `tests/`：基础规则、事务边界、接口路由、支付核验规则和端到端验收测试。

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
PYTHONPATH=src python3 -m transport_coordination.payment_acceptance
```

两条验收命令都会在临时 SQLite 数据库中执行完整业务链：基础验收核对登记、幂等回执与审计链；支付验收覆盖申报拆分、重复材料拦截、变更跨资金来源拆分、质保金拦截与释放、关账更正和审计追回，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

支付核验接口（写入均为 POST，查询为 GET）：

- 主数据：`/payment/contracts`、`/payment/change-orders`、`/payment/change-order-approvals`、`/payment/acceptances`、`/payment/invoices`、`/payment/quality-certificates`；
- 申请生命周期：`/payment/applications`、`/payment/countersigns`、`/payment/approvals`（支持部分批准）、`/payment/rejections`、`/payment/withdrawals`、`/payment/payments`；
- 资金后续：`/payment/retention-releases`、`/payment/recoveries`、`/payment/corrections`、`/payment/period-closes`；
- 查询：`GET /payment/contracts`、`GET /payment/applications`、`GET /payment/ledger`、`GET /payment/balance`、`GET /payment/evidence`。
