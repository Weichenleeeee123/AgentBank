# M2 业务能力逐项覆盖表

这份表只接受业务终态证据：处理器、API、状态持久化、审计和 CAP 用例必须同时存在。`基础` 表示代码已有入口但还需要补不变量或边界；`完成` 只有在对应集群的逐项验收提交后才能填写。

| ID | 能力 | 当前状态 | 必须验证的业务终态 |
|---|---|---|---|
| Q01 | 查余额 | 完成 | 账面、可用余额、as_of、事实片段、本人隔离 |
| Q02 | 查交易记录 | 完成 | 日期/分类/limit 过滤且只返回本人流水 |
| T01 | 人名转账 | 完成 | 同名消歧、确认后借贷平衡、哈希复核、重放拒绝 |
| T02 | 手机号转账 | 完成 | 掩码户名、新收款人升 R、OTP 后入账 |
| T03 | 别名转账 | 完成 | 别名只解析收款人，确认后入账 |
| T04 | 定时转账 | 完成 | mandate、两周期、每期 operation_id、重复派发去重、撤销暂停 |
| T05 | AA 收款 | 完成 | 参与者各自付款、账单结算、本人隔离 |
| B01-B04 | 账单分析 | 完成 | DSL/异常/月份缺失/收入支出转账追溯 |
| W01-W06 | 理财 | 完成 | 风险画像、适当性、订单、持仓、赎回结算 |
| C01-C08 | 卡片 | 完成 | 虚拟卡即时可用；实体卡 application→review→production→delivered；限额上限、限制、挂失、冻结和真实支付阻断 |
| S01-S03 | 订阅 | 完成 | 已确认/疑似、提醒持久化与跨请求去重、到期窗口 outbox 投递、代扣撤销、模拟扣费停止 |
| X01-X05 | 跨场景 | 完成 | 可信事件、预留、商户订单、Saga、盗刷分段授权 |

每个集群完成后必须把对应行改为 `完成`，并链接测试文件和提交号。

## M2 门禁证据

- 33 项能力：`test_capability_matrix.py`、各集群不变量测试；
- HTTP 端到端入口：`test_m2_api_matrix.py`、`test_m2_red_api_flow.py`（Q01-X05 逐项调用统一能力 API，包含红色 OTP 接续）；
- 生日 DAG/mandate/计划图：`test_plan_orchestration.py`、`test_plan_execution.py`、`test_m2_qt_invariants.py`；
- 计划状态机（pause/cancel/resume）和拓扑执行：`test_plan_orchestration.py`、`test_plan_execution.py`；
- DSL 沙箱边界和用户隔离：`test_m2_gate.py`、`test_m2_analysis.py`；
- Web IM 事件签名/去重/Agent 路由/卡片 nonce/群聊脱敏：`test_m2_gate.py`、`test_m2_api.py`、`test_webim_callbacks.py`；
- 计划可视化和决策链：`test_m2_plan_ui.py`、`test_plan_orchestration.py`；
- 保护性操作快速通道：`test_m3_security.py`、`test_m2_gate.py`；
- 主动发现：`test_agent_core.py`、`test_m2_gate.py`。

## M2 验收边界

- 33×3 CAP journey 已绑定 normal、colloquial、boundary/recovery 分支，并由 `docs/M2-CAP-TRACEABILITY.md` 记录终态证据。
- 本地验收使用确定性数据和可注入 transport；真实 LLM provider 联调、外部通知服务、Docker daemon 实际 build/start 仍属于独立环境验收，不能由 `223/223` 替代。
