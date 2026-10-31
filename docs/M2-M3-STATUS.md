# M2-M3 实现与验收状态

这份文件记录代码当前实际提供的入口和验证方式。它是开发中的状态文件，不是 M4 发布声明。能力注册表只是目录，只有下面的处理器、业务记录、审计事件和 CAP 用例共同成立时才算交付。

## 当前开发阶段

本地模拟范围内，33项能力、结构化LLM planner/explorer/reply/fraud角色、自由生成DAG与只读前置事实解析、两类复合只读计划、生日报价DAG、原子授权、Web IM持久防重放、PWA直达表单/修订、DSL完整deadline、方向可追溯月/年报及CAP traceability均已实现并加回归。当前本地验证为 pytest 全量通过、evaluation 227/227、Ruff/Mypy通过。本轮已补齐 M3 冷静期 action/revision 绑定、OTP 用户级锁定、设备边界、DLP/片段防伪造、审计末尾 checkpoint 和自测未授权记账门禁。本轮仍不把模拟设备、外部商户/推送/银行租户、飞书生产租户、公网HTTPS和M4材料当作真实生产证据。

六个业务集群的边界用例与主要不变量见 docs/M2-COVERAGE.md；PLAN §11 boundary fixtures 与99条CAP case的代码/测试/版本追溯由 docs/M2-CAP-TRACEABILITY.md 及 executable manifest提供。

M2的阶段内本地门禁已经通过，当前进入 M3 恢复与安全闭环。M3 的本地安全/恢复测试和评测矩阵已通过；外部租户、生产部署和M4发布物仍各自验收，不因本地模拟通过而视为线上完成。

99 个 CAP journey 已由 `docs/M2-CAP-TRACEABILITY.md` 逐项绑定到 normal、colloquial、boundary/recovery 分支；这证明本地确定性验收闭环，不等同于真实 provider 联调或 M4 发布完成。

## M2 能力入口

33 项能力全部由 `CapabilityService.execute(user_id, capability_id, payload, idempotency_key)` 处理，并通过 `POST /v1/capabilities/{capability_id}` 对外暴露。命令或查询都会生成 `CapabilityRecord`，结果包含 `record_id`、状态、业务数据和审计事件名。

| 能力组 | 已实现的业务状态证据 |
|---|---|
| Q01-Q02 | 余额含账面余额、可用余额、币种和 `as_of`；交易按用户隔离 |
| T01-T05 | 人名/手机号/别名解析；确认后转账写借贷分录；定时转账按业务时钟派发并用 operation id 去重；AA 分摊按分计算且只创建收款请求 |
| B01-B04 | DSL 分组带 `txn_ids`；异常带原因码；月报和年报区分收入、支出、转账并标出缺失月份 |
| W01-W06 | 风险画像确定性计分和有效期；推荐/对比；申购扣减可用余额；赎回返回 processing 和预计时间；持仓返回来源和时间 |
| C01-C08 | 虚拟/实体卡状态；限额；限制和支付阻断；挂失/解挂；临时冻结/解冻；PIN 只保留变更结果不落明文 |
| S01-S03 | 已确认/疑似订阅识别；提醒记录去重键；取消订阅同时撤销代扣状态 |
| X01-X05 | 生日事件建议；资金预留与释放；鲜花/蛋糕订单和 Saga 失败补偿；盗刷先冻结再强验证挂失 |

## M3 安全与恢复入口

- `FraudDetector`：`high / medium / low / unknown`，高风险在转账提交前拒绝，中风险创建真实时间冷静期。
- `POST /v1/cooling/{cooling_id}/release`：冷静期过期后才允许带外二次确认放行。
- `CircuitBreaker`：30 分钟内同一用户累计 3 次安全拦截后锁定普通写操作，保护性操作仍可执行。
- `SagaRunner`：订单部分失败时执行已完成步骤的补偿，并返回 `partially_completed`。
- `UnknownRecoveryService`：unknown 结果转为 processing，保持原幂等键，不重复产生副作用。
- `HandoffService` 和 `POST /v1/handoffs`：记录人工接管原因和状态。
- `ReplyFragmentChecker`：事实片段校验数值，禁止自由文本伪造金融标签。
- `POST /admin/v1/self-test`：执行 `evals/` 下的 CAP、SEC、FP 和 M3 恢复矩阵；当前本地评测包含 227 个用例，并返回 `audit_verified`、`final_checkpoint`、`case_categories` 和 `unauthorized_ledger_writes`。

## 当前验证命令

```powershell
python -m pytest backend/tests -q
python -m ruff check backend
python -m mypy backend/agentbank
python -c "from pathlib import Path; from agentbank.evals.runner import run_cases; print(run_cases(Path('evals')))"
```

最近一次结果：全量 pytest 通过；Ruff 通过；Mypy 通过；评测矩阵 `227/227` 通过。评测数字是当前本地模拟数据和代码版本的实测值，不代表线上部署性能或真实银行效果。

## M2 分阶段提交证据

- `2a13ddd`：统一能力 payload 校验、幂等请求哈希和业务状态测试；
- `38c988e`、`77c2fbc`：业务 HTTP 查询/报告/推荐入口归一到能力内核；
- `cfaff7a`：确认回执并发消费串行化；
- `f3c398e`：X05 先冻结、后设备 MFA 挂失，拒绝客户端伪造验证字段；
- `a26adf1`、`25b1672`：CAP 评测和 HTTP 矩阵改为业务终态断言；
- `cc0ea7e`：DSL 字段、metrics、排序、只读和行数硬限制；
- `f5c62d0`：生日计划日期、修订、暂停/恢复/取消和订单资金结算；
- `3e94626`：剩余 M2 能力自然语言 Agent 路由。
- `a353fee`：模型提出的通用能力 DAG、分级确认与计划级 MFA。
- `aa800fe`：只读前置事实解析、引用来源约束、计划 MFA 分流和 PWA 计划确认交互。
