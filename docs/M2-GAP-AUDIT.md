# M2 综合缺口复核与关闭条件

2026-10-30 交叉复核，覆盖 PLAN §3、§6、§7、§8、§10、§11、§12、§15.1、§15.3 和早期产品漏洞表。状态依据本轮代码、测试和评测，不以历史提交代替证据。本地模拟通过只代表本地模拟代码闭环；真实银行、商户、推送租户及公网部署要单列外部验收。本轮 DeepSeek Flash planner/reply/fraud 在线 smoke 已成功。

| ID | 要求 | 当前实现与测试证据 | 结果与边界 |
|---|---|---|---|
| A | 理解/规划/只读探索/回复/反诈分类 LLM 角色 | llm gateway + 版本化 fraud prompt；结构化 schema、主备、2秒 fraud timeout；转账 prepare 前规则+分类检查，不安全/未知走 handoff；test_fraud_llm_role、test_llm_gateway、test_agent_planning | 本地闭环；DeepSeek Flash 在线 smoke 已测；backup provider故障域与费用预算未联调 |
| B | 结构化对话持久化、最近 6 条用户消息、用户隔离 | ConversationState/Reference；test_agent_core/test_agent_planning | 本地闭环 |
| C | 有界 ReAct 和计划 DAG | 五步只读探索；固定 query_analysis_report、investment_review、birthday 模板；模型可提出注册能力 DAG；服务端对只读前置节点做事实预执行并解析 `$ref`，随后生成不可变 action/display hash；composite/generic checkpoint 用幂等键恢复；test_llm_gateway/test_composite_plans/test_free_dag_security/test_agent_planning | 本地闭环；写工具不进入探索循环，模型不能提供授权或服务端字段 |
| D | 33 项输入/输出契约、策略前置条件、补偿、展示元数据 | capability_contracts 为33项定义严格输入、授权、业务/状态结果、嵌套终态证据；tools 注册表由合同派生；CapabilityService 返回强制校验；test_capability_contracts_full | 本地闭环；补偿是本地模拟声明，不等于外部补偿已履约 |
| E | Prepare、receipt/mandate、hash复算、业务与审计原子提交 | Database.atomic scoped session；确认/MFA/回执/领域副作用/审计原子提交；C08 redaction、X05 staged hash；test_m2_atomic_commit/test_m2_unified_authorization | 本地闭环 |
| F | 会话身份、强类型字段、来源与所有权 | 33项严格输入合同、服务端 user_id、owner 检查、模型 slot grounding；test_capability_contracts_full/test_m2_api_matrix/test_llm_runtime | 本地闭环 |
| G | T04 周期授权、重新判级、撤销/过期/并发幂等 | schedule dispatch 单事务；余额/账户/风险/额度/mandate重检；失败回滚次数消费；test_schedule_atomic/test_m2_qt_invariants | 本地闭环 |
| H | 生日历史发现只建议、不消费 | scheduler 30日触发、上一年礼物流水 evidence、去重 inbox；test_notification_delivery/test_m2_x_invariants | 本地闭环；通知是应用内 inbox，未接外部推送租户 |
| I | 鲜花/蛋糕分别报价、预算、mandate、缺货/涨价/补偿恢复 | 持久 MerchantCatalog/Quote；生日DAG含两个quote节点，先验证报价再预留；变价须新报价及新确认；缺货暂停和恢复；test_merchant_quotes/test_order_progression/test_m2_plan_lifecycle | 本地商户模拟闭环；商户租户、实际库存/配送/退款未联调 |
| J | DAG 拓扑、暂停/取消/续跑/重启恢复 | 六节点生日计划；启动把 running 转 paused；能力 idempotency恢复中断后已提交节点；test_m2_plan_lifecycle/test_m2_dag_recovery/test_composite_plans | 本地模拟闭环；外部 unknown 银行状态仍需租户 status API 对账 |
| K | SSE、操作查询、表单、AA、handoff、评测入口 | app.py routes + owner tests；empty eval returns 503；test_m2_interface_contracts/test_m3_recovery_api/test_m3_gate_controls | 本地接口闭环 |
| L | Web IM 签名、事件去重、一次性 App nonce、群聊隐私 | 持久 WebImEvent/Nonce；nonce哈希、过期、单次原子消费；callback实执行；test_webim_callbacks | 本地闭环；多租户 IM callback 联调未做 |
| M | 真实计划决策证据与交互 | Graph只投影持久 receipt/decision/audit/operation/ledger/capability记录；前端读SSE并有 pause/resume/cancel；test_m2_plan_ui/test_plan_api | 本地闭环 |
| N | 保护性冻结和独立 MFA 分段挂失 | X05 freeze先行，独立 receipt/OTP 后 lost，失败仍冻结；test_m2_unified_authorization/test_m2_x_invariants/test_m2_red_api_flow | 本地模拟手机闭环；不能宣称真实独立因子 |
| O | SQLite只读/参数化/2秒完整时限/5万源行 | mode=ro、固定字段映射、绑定参数、SQL+Python组装 deadline、超行拒绝；test_m2_analysis_closure | 本地边界闭环；受限 DSL，不执行任意Python |
| P | 月报/年报、MoM/YoY、内部互转拆分、逐笔证据 | Asia/Shanghai timestamp归月、B04 calendar month+year保兼容、基期超窗口读取、收入/支出/转账 txn_ids；test_m2_analysis_closure/test_m2_b_invariants | 确定性模拟闭环 |
| Q | PWA Q01/Q02/T01-T03 直达和修订授权 | 搜索同名受款候选仅回掩码尾号；选 recipient_id；确认后修订作废旧回执；mfa/cooling状态准确；test_m2_form_recovery/test_pwa_status/test_frontend | 本地双通道闭环 |
| R | 可复现种子和PLAN §11边界追溯 | seed 三用户/同名收款人/多月流水/R1-R5/订阅/生日/卡/商户目录；专用 boundary suite；traceability manifest 33/33 mappings | 本地边界已映射；真实业务输入仍需上线数据验证 |
| S | 33×3业务终态、审计、附录追溯 | 99独立 CAP、runner逐项终态；可执行manifest连实现/runner/test/policy/fixture；artifact绑定git/matrix/prompt hash和实有evidence ID；test_cap_journeys/test_capability_matrix/test_traceability | 本地验收闭环；未记录的 audit/operation/ledger ID 保持空值，不推断 |

## 早期 10 项产品漏洞交叉结论

33项CAP：99条独立业务旅程已覆盖；LLM Agent：不再只是关键词，有四角色网关及组合只读计划；自然语言：核心示例/补槽/消歧/持仓选择/修订已测，超范围恢复仍限模板；Web IM/PWA：本地确认和红色App-only流程闭环；X01主动发现：有历史消费建议；W04/W05：账务和模拟时变估值；X03/X04：报价/锁价/缺货恢复/二次确认；S02：重试后应用内通知可见；C01：scheduler自动流转且可拒绝重试；Docker：配置已有双实例和healthcheck，本轮fresh build/重启另行验收。

## 本轮门禁

最终运行：全量 pytest 499 passed；确定性评测 223/223；Ruff、Mypy、Node syntax、diff-check通过；99 case manifest无错误。DeepSeek Flash planner/reply/fraud在线 smoke成功。Docker使用本地测试秘密完成config、fresh build、重启、health与eval self-test 223/223。真实银行/商户/推送/飞书租户、公网HTTPS和M4材料仍未验证。
