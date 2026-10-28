# AgentBank 系统架构

## 边界

AgentBank 把模型/自然语言层放在执行内核之外。当前可运行的 Agent 编排器位于 `backend/agentbank/agent/`，负责意图、槽位、消歧、能力路由和确认卡；`CapabilityService` 位于 `backend/agentbank/bank/capabilities_full.py`，负责身份、幂等、业务状态和审计；M1 转账的最终账本提交仍由 `TransferService` 完成。

```text
PWA / Web IM
    |
AgentOrchestrator: parse -> fill slots -> route -> confirmation/MFA
    |
CapabilityService + PlanValidator + Policy/Security
    |                    |
SQLite business state   audit hash chain
    |
simulated bank ledger / orders / cards / subscriptions
```

## 一次对话

1. `IntentParser` 只产生结构化槽位；金额、收款人和产品缺失时返回澄清。
2. `CapabilityService.execute` 先查幂等键，再检查熔断和红色 MFA 要求。
3. 写操作生成 `CapabilityRecord`；转账写操作还生成 M1 plan/receipt。
4. 用户确认或模拟手机 OTP 后，才调用 `TransferService.confirm/verify`。
5. 每个结果包含业务状态和审计事件名，事实回复用片段检查器校验。

## 计划

生日计划由 `BusinessPlanService` 生成四节点 DAG：事件读取、资金预留、鲜花订单、蛋糕订单。`PlanValidator` 检查工具白名单、拓扑顺序、来源和补偿声明；`GET /v1/plans/{id}/graph` 返回节点状态、授权和补偿信息。

## 渠道

- `/v1/conversations/{id}/messages`：App/PWA 对话入口；
- `/v1/im/webim/events`：签名和事件去重后的 Web IM 入口；
- `/app/`：静态 PWA 壳，调用同一套 API；
- `device/v1/inbox`：模拟手机 OTP 入口。
