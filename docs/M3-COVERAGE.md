# M3 安全闭环门禁表

范围：按 PLAN §6–§10、§14、§15.1 的本地模拟门禁验收。最终实测见 [M3-LOCAL-EVIDENCE.md](M3-LOCAL-EVIDENCE.md)。

| 门禁 | 实现及验证证据 |
|---|---|
| MFA 提交前复核 | `bank/service.py` 的统一 `_validate`；`test_m3_commit_guards.py` 验证 high 反诈不能经 MFA 绕过、改参、跨会话锁定、过期凭证 |
| 冷静期 | `security/cooling.py` 与 `TransferService`；绑定用户/action/revision，真实时间 30 分钟，冻结可用资金，账面不出账，到期后十分钟内新凭证与新 OTP，取消释放 |
| 反诈 | `RiskContextRecord` 由服务端维护，模型删备注无法清除；high 拒绝并开工单，medium/unknown 冷静期；无 provider 的确定性规则路径单独说明 |
| U16/U17/U18 熔断 | OTP 用户锁定及认证 API 伪造计数持久化；保护操作按动作判定；工具基础设施三次失败后进程内熔断 60 秒，查询仍可用 |
| unknown / Saga | 保持原 operation/idempotency key，不再次调用资金动作；补偿失败逐项记录，继续执行其余可补偿项 |
| 人工接管 | 工单保存计划/节点/授权快照，坐席备注脱敏并审计；`/app/staff.html`；无代替用户授权入口 |
| 防幻觉与 DLP | text/content 片段禁止金融标签、状态和数值伪造；模型输入和审计脱敏；设备 OTP 页面独立于 App |
| 设备边界 | 一次性配对事务及审计，receipt/action 绑定、单次 OTP、错误次数持久化；模拟手机支持配对 |
| 渠道 | Web IM 和飞书模拟协议签名/持久重放保护；群聊及群聊回调只返回通用状态；R 走 App |
| 审计 | 全字段哈希链、数据库外 JSONL HMAC 检查点、重复 checkpoint 幂等、数据库/库内 checkpoint 同时截断仍失败；资金提交/对话/自测检查点 |
| 自测 | `POST /admin/v1/self-test`，按用例的实际数据库核查分录及凭证，保存报告；`GET /admin/v1/self-test/{run_id}`；前端可下载证据 |
| SEC / FP | 227 例：99 CAP、80 八类 SEC、27 FP、10 额外 fraud、11 M3；每条 SEC 有 category、观测结果、账本/凭证检查及审计 ID |
| 部署面本地边界 | public 禁用 admin，即使提供正确 token；用户/IP 消息限流；每小时模型调用预算包括重试、主备和各角色 |
| 性能 | `evals/api_performance.py` 与 `M3-LOCAL-EVIDENCE.md`；只报告本地模拟测量 |

不把分类器样例匹配率称为真实世界诈骗识别率。飞书真实租户、不同故障域 provider 在线切换、公网部署、CI/Docker 实跑属于独立验收；亲情守护为可选，本轮未启用。
