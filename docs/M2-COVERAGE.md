# M2 业务能力逐项覆盖表

这份表记录本地模拟的业务终态；完成状态同时要求处理器/API、持久状态、审计、回归和 CAP 可追溯。真实银行、商户、推送租户和公网部署另作外部验收，不能由本地模拟推断。

| ID | 能力 | 本地状态 | 主要终态证据 |
|---|---|---|---|
| Q01-Q02 | 余额与交易查询 | 完成 | test_m2_api_matrix.py, test_m2_qt_invariants.py, test_cap_boundary_closure.py |
| T01-T03 | 姓名/手机号/别名转账 | 完成 | test_m2_unified_authorization.py, test_m2_red_api_flow.py, test_m2_form_recovery.py |
| T04-T05 | 定时转账与 AA | 完成 | test_schedule_atomic.py, test_m2_qt_invariants.py, test_m2_completion_flows.py |
| B01-B04 | DSL、异常、月报、年报 | 完成 | test_m2_analysis_closure.py, test_m2_b_invariants.py |
| W01-W06 | 风险画像、申购、赎回、估值 | 完成 | test_m2_w_invariants.py, test_m2_completion_flows.py, test_cap_boundary_closure.py |
| C01-C08 | 卡片申请/控制/挂失/PIN | 完成 | test_m2_c_invariants.py, test_m2_unified_authorization.py, test_m2_red_api_flow.py |
| S01-S03 | 订阅识别/提醒/取消 | 完成 | test_m2_s_invariants.py, test_notification_delivery.py, test_cap_boundary_closure.py |
| X01-X05 | 主动发现/预留/订单/Saga/防盗刷 | 完成 | test_notification_delivery.py, test_merchant_quotes.py, test_m2_x_invariants.py, test_m2_unified_authorization.py |

33项×3 journeys和逐项终态映射见 M2-CAP-TRACEABILITY.md。边界 fixture 到具体测试的可执行映射由 agentbank.evals.traceability 校验。
## M2 门禁证据

- 33项、三类独立旅程：`test_cap_journeys.py`、`test_capability_matrix.py`；
- HTTP业务终态和红色授权：`test_m2_api_matrix.py`、`test_m2_red_api_flow.py`；
- 全部能力强类型输入/结果：`test_capability_contracts_full.py`；
- 原子授权、MFA、重放：`test_m2_atomic_commit.py`、`test_m2_unified_authorization.py`；
- T04并发、撤销、过期、额度、受款人风险：`test_schedule_atomic.py`；
- 生日/复合DAG重启、暂停、恢复、quote Saga：`test_m2_plan_lifecycle.py`、`test_composite_plans.py`、`test_merchant_quotes.py`；
- DSL、月/年报告和UTC/本地时区边界：`test_m2_analysis_closure.py`；
- Web IM持久nonce、回放与PWA表单修订：`test_webim_callbacks.py`、`test_m2_form_recovery.py`、`test_pwa_status.py`；
- executable manifest、case/prompt/source/evidence绑定：`test_traceability.py`、`docs/M2-CAP-TRACEABILITY.md`。

## M2 验收边界

- 33×3 CAP journey 已绑定 normal、colloquial、boundary/recovery 分支，并由 `docs/M2-CAP-TRACEABILITY.md` 记录终态证据。
- 本地验收使用确定性数据和可注入 transport；真实LLM、外部通知/商户/银行租户、飞书及公网部署属于外部验收，不能由 `223/223` 替代。
