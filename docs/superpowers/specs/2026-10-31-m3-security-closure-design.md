# M3 安全闭环设计

> 日期：2026-10-31  
> 状态：已获用户确认，进入实现前审阅  
> 范围：AgentBank 本地模拟环境的 M3 安全与恢复闭环

## 1. 目标与边界

M3 的目标是让所有敏感写操作在运行时形成可验证的安全闭环：风险判定、授权、提交前复核、账本副作用、审计证据和恢复路径必须一致。模型只提出结构化意图，安全决策、授权凭证和记账继续由服务端代码完成。

本轮覆盖原设计中的 M3 门禁：

- unknown 结果恢复、Saga 补偿、人工接管；
- 反诈 high/medium/unknown；
- 真实时间冷静期与带外二次确认；
- 按用户持久化的 OTP 失败锁定和安全熔断；
- 回复事实片段校验与 DLP；
- 可解释拒绝；
- 一次性设备配对、OTP 绑定和设备访问边界；
- 审计哈希链、检查点和提交失败保护；
- 攻击集、误拦集、恢复集、并发和重放测试；
- 自测面板输出可复核证据，并明确“未授权记账为 0”。

公网部署、真实短信/人脸/U 盾、真实银行或商户不在本轮承诺范围。

## 2. 当前缺口

现有代码已有安全模块和测试骨架，但部分语义仍是最小演示实现：

1. 冷静期只按记录 ID 放行，未完整绑定 action hash、plan revision、过期后十分钟确认窗口和参数变更失效。
2. OTP 挑战的失败次数保存在单条 challenge，尚未形成按用户跨会话的锁定状态和真实时间解锁。
3. 熔断已有持久化计数，但需要统一接入所有敏感写入口，并保持保护性操作例外。
4. 设备服务已有配对和收件箱，需要补齐 App 会话不可注册/重置/读取设备、OTP 单次消费和跨操作绑定证据。
5. 回复片段检查、DLP、审计检查点已有独立能力，需要接入统一提交路径和自测证据。
6. 自测评测需要把安全结果、审计完整性和未授权账本副作用作为门禁，而不只返回通过数量。

## 3. 设计方案

### 3.1 安全上下文与哈希绑定

所有需要确认或强验证的操作都使用服务端计算的：

- `action_hash`：规范化操作参数、工具、用户、计划 ID、计划 revision 和目标资源；
- `display_hash`：用户实际看到的完整确认内容；
- `receipt_id`：一次性授权凭证；
- `plan_revision`：计划版本。

提交前重新计算两种 hash，并检查 receipt 的 subject、plan、revision、有效期、assurance 和节点消费记录。任何参数、收款人、金额、商品、地址或时间变更都使旧 receipt 失效，必须重新 Prepare。

### 3.2 冷静期状态机

冷静期记录扩展为：

- `user_id`
- `plan_id`
- `plan_revision`
- `action_hash`
- `amount_minor`
- `started_at`、`expires_at`
- `reconfirm_deadline`
- `status`: `active | completed | cancelled | invalidated`

规则：

1. medium 风险或分类器 unknown 的写操作进入 `active`，资金保持待执行占用，不出账。
2. 真实时间满 30 分钟后进入可复核窗口；业务时钟快进不能提前结束。
3. 用户必须在过期后 10 分钟内重新确认并完成所需 MFA，服务端将状态变为 `completed`。
4. action hash、plan revision 或风险等级变化时，旧记录变为 `invalidated`。
5. 释放接口必须校验用户所有权、hash/revision、窗口和二次确认；冷静期中、窗口过期或参数不一致均拒绝。
6. 取消计划时释放占用并将冷静期标记为 `cancelled`。

### 3.3 OTP 与熔断

OTP 继续只通过设备令牌访问模拟手机：

- 设备令牌只保存 hash；
- 配对码一次性、短时有效；
- challenge 绑定 `user_id + receipt_id + action_hash`；
- OTP 5 分钟有效、单次消费、单个 challenge 最多 3 次；
- 用户级安全计数记录连续失败和锁定截止时间，重新登录不清零；
- 锁定期间所有普通敏感写操作返回结构化拒绝，保护性操作（冻结、加限制、降额）仍可执行；
- 所有安全拦截计数按真实时间窗口计算，业务时钟不能绕过。

现有 `CircuitBreaker` 作为持久化安全计数入口；新增统一检查函数供 CapabilityService、TransferService 和计划执行器调用。

### 3.4 统一提交前安全门

敏感写操作的执行顺序固定为：

1. 会话身份与资源所有权校验；
2. 输入契约和来源追踪校验；
3. 反诈与策略决策；
4. 熔断/OTP 用户锁定检查；
5. receipt 或 mandate 的对象、金额、时间、revision、hash 和剩余次数校验；
6. 冷静期状态校验；
7. 账本余额/额度和业务前置条件复核；
8. 审计可写性检查；
9. 在同一数据库事务中消费凭证、写业务状态、写借贷平衡分录、写审计事件和 outbox；
10. 提交后才允许通知投递。

任何步骤失败都回滚资金和业务副作用；通知失败只重试通知，不重试资金操作。

### 3.5 unknown、Saga 与人工接管

- 银行返回 `unknown` 时只允许由原 `operation_id` 查询，状态转换为 `processing`，保留原幂等键和资金占用，不重新创建操作。
- Saga 按已完成步骤逆序执行补偿；补偿是独立命令，重新经过策略和授权。
- 部分完成返回 `partially_completed`，明确成功、失败、已补偿和待用户决策节点。
- 人工接管只允许暂停/查看/申请重试，不能代替用户确认或 MFA；人工操作写入审计，计划从断点恢复。

### 3.6 设备、DLP 与回复片段

- App 会话不提供设备注册、重置或 OTP 读取能力；
- `/device/v1/*` 只接受有效设备令牌；
- 设备收件箱只返回该设备所属用户的未消费挑战；
- 日志、模型输入、出站消息和审计 payload 统一调用 DLP，隐藏卡号、手机号和 OTP；
- 回复中的金融事实只能使用结构化 fact/status 片段渲染；text 片段不得伪造金额、日期、状态或金融标签；
- 群聊只返回通用状态和私聊/App 入口，不泄漏账户事实。

### 3.7 审计与自测证据

审计事件继续使用顺序号和前哈希链。每轮对话、资金提交和安全自测前后生成检查点。自测接口返回：

- 评测 suite 内容哈希；
- 每条 SEC/FP/M3 用例结果；
- audit checkpoint 和 verify 结果；
- 未授权账本写入计数；
- unknown/Saga/cooling/breaker/device 的可复核 ID；
- 失败用例的结构化原因。

安全自测通过条件：

- 必须存在非空攻击集和误拦集；
- 所有高风险资金/权限攻击被拒或升级；
- 关键恢复用例通过；
- 审计链完整；
- 未授权记账计数为 0；
- 评测结果和代码版本可追溯。

## 4. 文件边界

预计修改：

- `backend/agentbank/db.py`：冷静期、OTP 用户锁定和安全证据字段；
- `backend/agentbank/security/cooling.py`：冷静期状态机；
- `backend/agentbank/security/breaker.py`：统一安全锁定判断；
- `backend/agentbank/auth/devices.py`：设备边界和 OTP 绑定/失败策略；
- `backend/agentbank/bank/capabilities_full.py`、`backend/agentbank/bank/service.py`：提交前安全门；
- `backend/agentbank/api/app.py`：安全接口的所有权、结构化错误和自测证据；
- `backend/agentbank/audit/store.py`：检查点与提交保护；
- `backend/agentbank/evals/runner.py`、`evals/*.yaml`：SEC/FP/M3 门禁；
- `backend/tests/test_m3_*.py` 及新增聚焦测试；
- `docs/M3-COVERAGE.md`、`docs/M2-M3-STATUS.md`、`docs/security-self-eval.md`：基于实测结果更新证据。

不删除任何现有文件；新增文件只用于聚焦测试或安全证据。

## 5. 错误与恢复契约

所有安全拒绝使用稳定错误码，并由 API 转成安全的公开说明：

- `AUTH_REQUIRED`、`NOT_OWNER`
- `RECEIPT_INVALID`、`RECEIPT_REPLAYED`、`ACTION_CHANGED`
- `COOLING_PERIOD_ACTIVE`、`COOLING_RECONFIRMATION_REQUIRED`、`COOLING_EXPIRED`
- `OTP_INVALID`、`OTP_EXPIRED`、`OTP_LOCKED`
- `SECURITY_LOCKED`、`RATE_LIMITED`
- `FRAUD_HIGH_DENIED`、`FRAUD_REVIEW_REQUIRED`
- `OPERATION_UNKNOWN`、`HANDOFF_REQUIRED`
- `AUDIT_UNAVAILABLE`

公开回复只说明发生了什么、原因类别和安全下一步，不暴露关键词、阈值、模型分数或内部密钥。

## 6. 测试策略

遵循 TDD，每个行为先写最小失败测试，再实现最小代码，最后重构：

1. 冷静期：真实时间窗口、业务时钟不绕过、hash/revision 变更、取消释放、过期后二次确认。
2. OTP/熔断：跨会话失败计数、重登录仍锁定、保护性操作例外、OTP 重放/跨 receipt/过期/三次失败。
3. 提交门：确认后改参、并发提交、审计写失败、未授权账本副作用为零。
4. unknown/Saga/人工：原幂等键、逆序补偿、部分完成、人工不可授权。
5. 设备/DLP/片段：App 越权接口拒绝、设备隔离、敏感值脱敏、假成功文本和错误金融标签被拒。
6. 评测门禁：SEC/FP/M3 非空、结果哈希、audit verify、未授权记账计数。
7. 全量回归：`pytest`、`ruff`、`mypy`、本地评测矩阵和 Docker compose 配置校验。

验收目标：

- 全量测试通过；
- 安全评测不低于现有 223 条且新增 M3 用例全部通过；
- 未授权记账为 0；
- audit verify 为真；
- 现有 33 项 M2 能力业务终态不回归；
- 不对外宣称真实银行、真实设备因子或公网部署已完成。

## 7. 非目标

本轮不新增真实支付、短信、人脸、飞书租户、生产数据库或公网 HTTPS；不训练模型；不引入新的编排框架；不删除历史测试、文档或数据文件。
