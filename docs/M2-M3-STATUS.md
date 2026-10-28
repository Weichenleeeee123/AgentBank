# M2-M3 实现与验收状态

这份文件记录代码当前实际提供的入口和验证方式。它是开发中的状态文件，不是 M4 发布声明。能力注册表只是目录，只有下面的处理器、业务记录、审计事件和 CAP 用例共同成立时才算交付。

## 当前开发阶段

M2/M3 仍在开发中。已新增确定性 Agent 编排器：自然语言槽位解析、多轮补槽、收款人消歧、能力路由、确认卡、MFA 接续和 Web IM/App 共用入口；新增生日四节点 DAG 和图查询；新增 PWA 壳和完整的 AA、定时任务、投资订单、卡片申请、订阅提醒/代扣、商户订单持久化。仍需继续补齐所有能力的真实授权边界、完整 DAG 执行推进、前端部署、飞书适配器、攻击集规模和 M4 必交材料。

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
- `POST /admin/v1/self-test`：执行 `evals/` 下的 CAP、SEC、FP 和 M3 恢复矩阵。

## 当前验证命令

```powershell
python -m pytest backend/tests -q
python -m ruff check backend
python -m mypy backend/agentbank
python -c "from pathlib import Path; from agentbank.evals.runner import run_cases; print(run_cases(Path('evals')))"
```

最近一次结果：全量 pytest 通过；Ruff 通过；Mypy 通过；评测矩阵 `108/108` 通过。评测数字是当前本地模拟数据和代码版本的实测值，不代表线上部署性能或真实银行效果。
