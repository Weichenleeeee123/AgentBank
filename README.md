# AgentBank

2026 深圳国际金融科技大赛（微众银行金融科技大学生挑战赛），人工智能赛道（国内）参赛作品：银行 AI 智能体。

核心原则：**模型只负责提议，执行与否由代码裁决，最终由用户在带外确认。**

## 文档

| 文件 | 内容 |
|---|---|
| [docs/PLAN.md](docs/PLAN.md) | **整体方案与开发计划（定稿）**：33 项能力矩阵、架构、契约、权限规则、安全机制、测试、里程碑、提交物 |
| [docs/brief.md](docs/brief.md) | 赛题事实，是所有设计判断的唯一依据 |
| [docs/architecture.md](docs/architecture.md) | 当前系统边界、Agent、执行内核、DAG 和渠道 |
| [docs/algorithms.md](docs/algorithms.md) | 已实现算法和验证口径 |
| [docs/security-self-eval.md](docs/security-self-eval.md) | 已验证安全行为、已知边界和禁止的过度结论 |
| [docs/submission-checklist.md](docs/submission-checklist.md) | M4 提交门禁清单（当前未冻结） |
| [docs/M3-COVERAGE.md](docs/M3-COVERAGE.md) | M3 安全闭环逐项证据 |
| [docs/plan-claude.md](docs/plan-claude.md)、[docs/plan-codex.md](docs/plan-codex.md) | 两份独立写成的初稿 |
| [docs/debate/](docs/debate/) | 对抗评审记录：第 1 轮互评，第 2 轮反驳，第 3 轮终审，第 4 轮复核 |

当前实现包含 M0-M3：M1 的转账授权与账本链路、M2 的 33 项能力处理器与业务状态、M3 的反诈/冷静期/熔断/Saga/人工接管和全量评测入口。逐项证据见 [docs/M2-M3-STATUS.md](docs/M2-M3-STATUS.md)。

## 本地运行

```powershell
python -m pip install -e ".[dev]"
python -m agentbank.auth.cli --db sqlite:///agentbank.db --user user_001
uvicorn agentbank.api.run:app --reload
```

通过 `POST /v1/sessions` 获取演示会话令牌，再调用余额、转账准备、确认和验证接口。模拟手机只通过配对后的设备令牌访问 `/device/v1/inbox`，不会复用 App 会话。

渠道入口包括：PWA `/app/`、对话 API `/v1/conversations/{id}/messages`、签名 Web IM `/v1/im/webim/events` 和签名飞书适配器 `/v1/im/feishu/events`。飞书生产联调时设置 `AGENTBANK_FEISHU_SECRET`；没有租户凭证时使用 Web IM 或开发 secret 复现相同 Agent 流程。

所有能力也可以通过统一入口调用：

```json
POST /v1/capabilities/Q01
{"capability_id":"Q01","idempotency_key":"demo-q01-1","payload":{}}
```

命令结果会写入 `capability_records`，并返回可追溯的审计事件。需要用户确认的 T01/T02/T03 先返回 `record_id`、`plan_id`、`receipt_id` 和 `display_hash`，再调用 `/v1/capability-records/{record_id}/confirm`。

运行全量确定性评测：

```powershell
python -c "from pathlib import Path; from agentbank.evals.runner import run_cases; print(run_cases(Path('evals')))"
```

## 双实例部署

在仓库根目录设置 `PUBLIC_SESSION_SECRET` 和 `EVAL_SESSION_SECRET` 后运行 `docker compose -f deploy/docker-compose.yml up --build`。公共实例使用 8000 端口，评测实例使用 8001 端口，各自使用独立 SQLite 数据卷。
