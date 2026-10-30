# M2 总缺口与关闭条件

2026-10-30 复核，代码基线 `4574dc2`。本表按 `docs/PLAN.md` §3、§6、§7、§8、§10、§12、§15.1、§15.3 复核。未提交工作区改动不作为关闭证据。提交号表示实现/验收变化的历史定位，不替代当前测试结果。下列测试文件位于 `backend/tests/`，缩写源码路径位于 `backend/agentbank/`。`已关闭（本地模拟）` 不代表真实银行、真实租户或生产环境验证。

| ID | M2 设计要求 | 当前代码级证据 | 当前测试/历史证据 | 状态与剩余边界 |
|---|---|---|---|---|
| M2-A | LLM 理解规划、只读探索、回复与反诈分类角色；网关、主备、版本化提示词 | `backend/agentbank/llm/gateway.py` 实现 planner/explorer/reply 和旧 intent 分类；运行中的 `FraudDetector` (`security/fraud.py`) 是规则兜底，尚未接 PLAN §6.2 的独立 LLM 反诈分类角色 | `test_llm_gateway.py`、`test_llm_runtime.py`、`test_agent_planning.py`；`0ffeecb`、`9346220`、`a2abf66` | 部分完成：planner/explorer/reply 的结构化契约和主备/fallback 有测试；反诈分类角色未闭合。真实 provider 的在线可用性、延迟和预算也不是本地门禁证据。 |
| M2-B | 结构化对话状态、最近 6 条用户消息、不保存助手自由文本 | `ConversationState`、`ConversationReference` 持久化表；`agent/orchestrator.py` 恢复待补槽与引用 | `test_agent_core.py` 持久化重建用例、`test_agent_planning.py` 引用往返；`ca1e823`、`067125f`、`485c53d` | 已关闭（本地模拟）。 |
| M2-C | G 级受限探索，最多 5 步，只读工具，结构化观察与审计 | `LLMGateway.explore_read_only` 设置 5 步上限和只读 allowlist；Agent 执行真实只读能力后回传清洗后的观察 | `test_llm_gateway.py`、`test_llm_runtime.py`、`test_agent_core.py`；`b7bc569`、`857a94e` | 部分完成：受限探索闭合；LLM 计划模板目前只注册 birthday，PLAN §6.6 的通用组合模板尚无对应执行器与端到端用例。 |
| M2-D | 33 项能力契约、策略、前置条件、补偿、卡片 | `tools.py` 有 33 项 ID、输入字段校验、最低 assurance、前置条件、补偿和卡片目录；`capabilities_full.py` 统一分发 | `test_m2_registry.py`、`test_m2_contracts.py`、`test_m2_full_capabilities.py`；`2a13ddd`、`38c988e` | 部分完成：目录和执行入口存在，但多数能力仍共享通用字段/结果 dict、通用 `explicit_command` 补偿描述及通用卡片配置，不是 PLAN 所述逐能力完整输入/输出/补偿契约。 |
| M2-E | 写操作统一 Prepare、策略、一次性 receipt/mandate、提交复核 | `CapabilityService.execute/confirm_capability/verify_mfa` 有 Prepare、hash 比较和 `ReceiptConsumption`；生日计划有逐节点消费 | `test_m2_unified_authorization.py`、`test_authorization_integrity.py`、`test_plan_orchestration.py`；`067125f`、`2a13ddd`、`cfaff7a`、`222d138` | 部分完成：确认/设备 MFA/并发消费已有测试；通用确认先提交 receipt 消费再调用另起事务的 `_dispatch`，不能证明 PLAN §9.3 要求的授权消费、业务与审计同事务；也未对通用保存的 payload 重算 hash。领域服务默认仍可关闭 Prepare/MFA。 |
| M2-F | 严格契约、会话注入身份、参数/来源/资源所有权校验 | Pydantic `extra="forbid"` 合同、能力级 `INPUT_FIELDS`/类型范围验证、API `current_user`、仓储 owner 检查、LLM slot grounding | `test_m2_contracts.py`、`test_m2_api_matrix.py`、`test_llm_runtime.py`；`2a13ddd`、`857a94e` | 部分完成：身份、能力 payload 和来源边界有验证；API 请求体仍广泛使用 `dict[str, Any]`，并非 33 个独立强类型请求/结果 Schema。 |
| M2-G | T04 周期 mandate、每期 operation、幂等、执行时重新判级及撤销/过期 | `ScheduledJob`、`Mandate`、`CapabilityService.dispatch_due` 与 mandate 校验 | `test_m2_qt_invariants.py`、`test_m2_full_capabilities.py`、`test_m2_gate.py`；`14ac85e`、`0880a43` | 部分完成：两期、不同 operation、重复派发、撤销后暂停和执行时日额度复核有测试；未找到 T04 过期/风险升级及并发 scheduler 派发的完整组合验收。 |
| M2-H | X01 可信生日与去年消费线索，只作建议、不花钱 | X01 查询本人可信 `Event`，`birthday_history_clues` 返回花店/蛋糕历史交易 ID；scheduler 生成 recommendation-only 通知 | `test_m2_x_invariants.py`、`test_notification_delivery.py`；`e12bf07`、`4fcb48f` | 已关闭（本地模拟）；已测试日期窗口、历史证据、去重及不产生预留/账务副作用。 |
| M2-I | 生日 DAG、报价、mandate、预算、鲜花/蛋糕两订单及失败恢复 | `BusinessPlanService` 创建事件/预留/鲜花/蛋糕四节点；X03/X04 有 quote ID、独立订单、mandate、价格变化暂停/重新确认和预留结算 | `test_m2_plan_lifecycle.py`、`test_m2_x_invariants.py`、`test_m3_security.py`；`f5c62d0`、`805b248` | 部分完成：固定生日流程和价格变化/补偿有本地证据；没有方案要求的独立报价节点、可选商品/缺货处理与完整 U22 评测矩阵。 |
| M2-J | DAG ready/running/unknown/compensating、暂停/取消/恢复、拓扑与重启续跑 | `workflow/planner.py` 持久化节点状态、拓扑排序、暂停/取消/恢复及已消费节点跳过 | `test_m2_dag_recovery.py`、`test_m2_plan_lifecycle.py`、`test_plan_execution.py`；`9539b91`、`ede8207` | 部分完成：拓扑、暂停、取消、恢复及部分成功有测试；未找到进程中断/重启后 unknown 结果查询对账的完整集成验收。 |
| M2-K | 计划事件 SSE、操作查询、表单、handoff、AA、eval 接口 | `api/app.py` 提供 `/plans/{id}/events`、`/operations/{id}`、`/forms/{kind}`、handoff、AA 与 eval 接口 | `test_m2_interface_contracts.py`、`test_m3_recovery_api.py`；`ede8207`、`57487d7` | 已关闭（本地模拟）。接口与 owner 权限有测试；表单前端体验计入 M2-Q。 |
| M2-L | Web IM 身份、签名、事件去重、一次性卡片 nonce、App deep link、群聊隐私 | Web IM 签名校验、callback 执行确认、群聊摘要；但 `app.state.webim_events` 与 `app.state.webim_nonces` 是进程内集合/dict | `test_webim_callbacks.py`、`test_channel_ui.py`；`92050f4`、`d89985c` | 部分完成：签名、确认回调、群聊隐藏和进程内重放防护有测试；事件/nonce 未持久化，重启后重放仍是未关闭缺口。 |
| M2-M | 计划进度卡和可展开决策链 | `/plans/{id}/graph` 与 `frontend/plan.html` 展示节点依赖/状态；当前 `BusinessPlanService.graph` 的部分 rule/authorization decision_chain 是按状态合成 | `test_m2_plan_ui.py`、`test_plan_api.py`；`92050f4` | 部分完成：计划图和事件流可用；图中决策链未完全从真实 decision/audit/receipt/ledger 记录投影，前端实时更新/控制的端到端覆盖有限。 |
| M2-N | P09 保护性操作快速通道与 X05 分段授权 | X05 先冻结卡，再签发独立 MFA receipt；OTP 失败不撤销冻结；成功后才转 lost | `test_m2_unified_authorization.py`、`test_m2_x_invariants.py`、`test_m2_red_api_flow.py`；`f3c398e`、`e12bf07` | 已关闭（本地模拟设备/MFA流程）。设备是真实独立因子的声明仍受 M3 设备边界门禁约束。 |
| M2-O | DSL SQLite 只读、参数化字段、2 秒、50,000 源行硬限制、用户隔离 | `analysis/dsl.py` 对文件 SQLite 建 `mode=ro` 独立 engine；固定字段映射构造 SQL；用户条件服务端注入；progress handler 与 50,001 探测上限 | `test_m2_analysis.py`、`test_m2_gate.py`；`cc0ea7e` | 部分完成：实现具备只读 URI、超时与行上限；现有测试验证用户隔离、非法 DSL 和超行数，但未直接验证 SQLite 拒写及 progress-handler 超时。DSL 是受限解释器，不是通用 Python 沙箱。 |
| M2-P | B03/B04 12 月、MoM/YoY、流入/流出/转账拆分与证据 | `analysis/insights.py:monthly_report` 输出 12 月、missing、mom_change、yoy_change 和 transfer_minor；B01/B02 提供 txn_ids/reason evidence | `test_m2_analysis.py`、`test_m2_b_invariants.py`；`e12bf07` | 部分完成：报表字段与 12 月覆盖已有实现；当前测试仅检查 MoM/YoY 字段存在和流入/流出/转账字段，没有校验同比/环比期望值、缺失基期、内部互转排除及逐笔对账。 |
| M2-Q | Q01/Q02/T01-T03 App 表单直达，共用能力校验与 Prepare | `/v1/forms/{kind}` 映射余额、交易、姓名/手机号/别名转账到 `CapabilityService`；前端以 Agent 聊天为主 | `test_m2_interface_contracts.py`、`test_m2_api_matrix.py`、`test_frontend.py` | 部分完成：HTTP 表单入口和统一服务路径存在；前端没有覆盖 Q01/Q02/T01-T03 的完整表单导航、收款人选择、修订确认旅程。 |
| M2-R | 确定性多用户、收款人、流水、风险/产品/卡/商户边界种子 | `seed/generator.py` 固定随机种子，含 3 用户、同名收款人、多月及历史交易、R1-R5 产品、订阅/生日/卡片等数据 | `test_m0_seed.py`、集群不变量测试；`436bdcc`、`e12bf07` | 部分完成：主要夹具存在且可复现；仍缺 PLAN §11 全部边界夹具逐项存在性和行为追溯（如缺货、完整变价/超时组合、授权过期/撤销组合）。 |
| M2-S | 每能力 3 CAP、业务终态、审计、附录 A 需求追溯 | `evals/m2_m3_capability_matrix.yaml` + `evals/runner.py` 的独立 normal/colloquial/boundary journey；`_assert_terminal_state` 对各能力查持久业务状态 | `test_cap_journeys.py`、`test_capability_matrix.py`；`9771caa`、`a26adf1`、`e472f9a` | 部分完成：99 个 CAP 有业务终态断言，旧表所称“runner 只查 record/audit”已过时；PLAN 附录 A 尚未逐行链接 case ID、代码/提示词/用例集版本。 |

## M2 完成门禁

M2 阶段门禁要求 M2-A 至 M2-S 全部关闭，并通过逐项业务终态、统一授权、跨渠道与 Agent 全流程验收。当前表中 19 项为 4 项已关闭（本地模拟）、15 项部分完成；因此 M2 总门禁未通过。

## 当前复核结论

旧表中“没有 LLM 网关/探索”“没有计划事件、操作查询和通用表单”“生日发现没有历史消费线索”“报告没有同比/环比”“CAP 只检查 record/audit”等描述已被后续代码和提交推翻；不得再把它们作为当前缺口。相应证据和改动分别见上表、`docs/M2-CAP-TRACEABILITY.md` 以及 `0ffeecb`、`9771caa`、`a26adf1`、`e12bf07`、`ede8207` 等提交。

当前仍有明确未关闭项：M2-A 反诈 LLM 分类角色；M2-C/D/F 的通用组合规划和逐能力契约深度；M2-E 提交事务内原子性；M2-G mandate 风险边界测试；M2-I birthday quote/缺货/U22 完整旅程；M2-J 进程重启与 unknown 对账；M2-L nonce/事件持久化；M2-M 决策链真实来源与实时 UI；M2-O 拒写/超时实测；M2-P 报表同比环比与互转正确性；M2-Q 完整前端表单旅程；M2-R 方案夹具逐项追溯；M2-S 附录 A 与 case/version 绑定。因此这份表更新后仍不支持“方案级 M2 全部关闭”的声明。

上表状态的最小门禁是：实现与该行所引测试同时存在且通过；提交历史只用于追踪何时增加证据。完整 Python 测试、Ruff、Mypy 或 `223/223` 总分都不能单独替代逐行验收。
