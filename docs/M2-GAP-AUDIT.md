# M2 总缺口与关闭条件

本表按 `docs/PLAN.md` §3、§6、§7、§8、§10、§12、§15.1、§15.3 审核。这里把“有 endpoint/返回 record”与“完整业务实现”分开：只有业务终态、授权、审计、恢复和渠道行为均有可执行测试，才可标记关闭。

| ID | M2 设计要求 | 当前缺口/风险 | 主要代码 | 关闭条件 | 状态 |
|---|---|---|---|---|---|
| M2-A | LLM Agent 三角色、网关、主备 provider、prompt 版本 | 当前产品流程使用确定性解析器；无模型网关、Schema 重试、provider fallback、prompt 审计 | `agent/`, 缺 `llm/` | 可注入 provider；结构化 IntentFrame；超时/无效 JSON/fallback 有测试；无凭据时模板降级明确 | 未完成 |
| M2-B | 结构化对话状态、最近 6 条用户消息、无助手自由文本 | 槽位状态只在进程内 dict，重启丢失；跨会话实体/计划引用未持久化 | `agent/orchestrator.py`, DB | 持久化结构化 turn state；重启恢复；只存用户消息和结构化结果；跨用户隔离 | 未完成 |
| M2-C | G 级受限探索 ReAct，最多 5 步，每步审计 | 当前没有探索工具循环；多步自然语言不执行计划模板 | `agent/`, `planning/` | 只读工具 allowlist、步数上限、untrusted 字段剔除、每步审计、组合任务测试 | 未完成 |
| M2-D | 33 项能力统一契约、策略、前置条件、补偿、卡片 | `tools.py` 是粗粒度静态映射；输入 schema/状态/补偿/规则未逐工具定义，通用 capability 入口和专用 endpoint 语义不一致 | `tools.py`, `capabilities_full.py` | 每个能力注册契约、最低等级、前置条件、补偿、展示配置；注册表驱动 validation；33 项契约测试 | 未完成 |
| M2-E | 全部写操作统一 Prepare → 策略 → receipt/mandate → 提交前复核 | 转账和生日 DAG 有单独路径；大量写能力直接在 `execute()` 内副作用；receipt 节点消费没有统一覆盖 | `bank/service.py`, `capabilities_full.py`, `planner.py` | 所有写能力产生不可变 Prepare；提交时复算 action/display hash；receipt 按节点唯一消费；失败不留半副作用 | 未完成 |
| M2-F | 契约严格、服务端身份注入、参数/来源校验 | 通用 API 接收自由 `dict`；多数能力不使用 Pydantic 专属契约；外部字符串/状态参数校验不一致 | `contracts.py`, `api/app.py` | 33 个输入/结果 schema；unknown field 拒绝；身份仅由 session 注入；owner/source validation | 未完成 |
| M2-G | T04 周期 mandate、独立期次、同一期幂等、执行时重新判级 | 基础路径已能执行两期；额度失败/mandate 风险升级/执行状态和 scheduled job 与 operation 原子关联仍需补严 | `capabilities_full.py`, `ScheduledJob`, `Mandate` | 并发重复派发只记一次；每日额度与 mandate 原子消费；每期唯一 operation；过期/撤销/升级暂停 | 部分完成 |
| M2-H | X01 主动发现：可信生日 + 去年消费线索；只建议 | 当前只读 events；未基于去年花店/蛋糕流水生成带证据建议 | `X01`, `seed/generator.py` | 输出可信事件和匹配 txn_ids；没有事件/用户确认时不建议；绝不消费资金 | 未完成 |
| M2-I | 生日 DAG 完整计划、报价、mandate、资金预算、两订单 | DAG 仅固定模板；quote/价格变化/缺货/U22/日期追问未落实；复用同一 reserve 未按商户拆账 | `workflow/planner.py`, X02-X04 | quote 节点、价格锁、两商户独立订单/金额/状态、预留预算扣减、取消释放、失败 Saga 恢复 | 未完成 |
| M2-J | DAG 状态机：ready/running/unknown/compensating，暂停/取消/续跑 | 当前暂停/恢复只改计划状态，节点派发与中断不联动；失败步骤和 receipt consumption 不能可靠 resume | `workflow/planner.py`, `PlanNodeRecord` | 重启恢复；只派发依赖满足的 ready 节点；暂停即停止新派发；恢复跳过已消费节点；失败进入人工/Saga | 未完成 |
| M2-K | `GET /plans/{id}/events` SSE、操作查询、表单直达、handoff 等接口 | 缺 SSE、`GET /operations/{id}`、通用 forms、计划 handoff、独立 AA create/status 和 eval run 接口 | `api/app.py` | PLAN §7.10 所列 M2 接口实现、owner 权限、状态/event 测试 | 未完成 |
| M2-L | Web IM 独立身份、签名、事件去重、卡片 nonce、深链 | 基础签名/callback 存在；nonce 和事件去重只在内存，重启失效；callback 只 ack，不执行对应 confirm；R deep link 未充分绑定计划 revision | `api/app.py`, `channels/` | 持久化 nonce/事件；callback 绑定 user/record/revision/hash；重放拒绝；R 只能 App OTP；群聊 DLP | 未完成 |
| M2-M | 计划进度卡和可展开决策链 | 有静态 graph HTML/API，但无实时 SSE；规则/决策/receipt/账本证据部分为合成字段 | `frontend/plan.html`, `/graph` | 展示真实 decision/audit/receipt/ledger；SSE 实时更新；暂停/取消按钮调用服务端状态机 | 未完成 |
| M2-N | P09 保护性操作快速通道与 X05 分段授权 | C07 冻结能独立做；X05 仍以简化 payload 分支模拟 MFA，未使用真实 receipt 分段执行 | `C07`, `X05`, policy | 冻结段 Y receipt 立即消费；挂失段 R receipt 单独签发；R 失败卡仍冻结；调用和账本/状态证据齐全 | 未完成 |
| M2-O | DSL 真沙箱：SQLite mode=ro、参数化 SQL、2秒、5万行 | 当前 ORM 全量加载后在 Python 过滤/分组；`PRAGMA query_only` 与审计共用连接切换，非独立只读连接；limit 在聚合后截断 | `analysis/dsl.py` | 只读 URI/连接；服务端强制 user_id SQL 条件；SQL 参数化；progress timeout；源行 hard cap；审计摘要 | 未完成 |
| M2-P | B03/B04 确定性报告，12月、同比/环比、互转排除 | 报告没有同比/环比；transfer 样本 grain/内部转账口径不足；API 年报入口不明确 | `analysis/insights.py`, API | 12 月完整、missing flags、MoM/YoY、内部互转排除、逐笔 evidence 测试 | 未完成 |
| M2-Q | App 表单直达覆盖 Q01/Q02/T01-T03，共用 Prepare/策略 | 只有 transfer form endpoint；PWA 没有完整查询/收款人选择/修订表单流程 | `/forms/transfer`, `frontend/` | Q01/Q02/T01-T03 表单；和 Agent 走同一 handler/Prepare；确认与状态查询一致 | 未完成 |
| M2-R | 种子覆盖 3 用户、同名收款人、全年流水、风险/授权/产品/商户边界 | 有基础交易和商户种子，但多用户、六产品边界、缺货/涨价/超时、过期撤销授权等不全 | `seed/generator.py` | 固定 seed 可重现；所有 PLAN §11 fixtures 有存在性和行为测试 | 未完成 |
| M2-S | DoD：每能力 3 CAP，断言业务终态、审计字段，更新附录 A | `test_capability_matrix.py` 和 runner 仍主要断言 record/audit；附录 A 是静态需求表，未关联实际 case ids | `tests/`, `evals/`, PLAN appendix A | 每个能力三条正常/口语/边界 CAP；断言业务终态/拒绝/审计；报告能追溯 case id 和版本 | 未完成 |

## M2 完成门禁

M2 只有在以上 M2-A 至 M2-S 全部关闭、每条有代码路径与业务不变量测试，并通过全量 API/Agent 流程后才算完成。`pytest`、Ruff、Mypy 或当前 `223/223` 单独通过不等于 M2 完成。
