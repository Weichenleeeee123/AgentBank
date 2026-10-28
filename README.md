# AgentBank

2026 深圳国际金融科技大赛（微众银行金融科技大学生挑战赛），人工智能赛道（国内）参赛作品：银行 AI 智能体。

核心原则：**模型只负责提议，执行与否由代码裁决，最终由用户在带外确认。**

## 文档

| 文件 | 内容 |
|---|---|
| [docs/PLAN.md](docs/PLAN.md) | **整体方案与开发计划（定稿）**：33 项能力矩阵、架构、契约、权限规则、安全机制、测试、里程碑、提交物 |
| [docs/brief.md](docs/brief.md) | 赛题事实，是所有设计判断的唯一依据 |
| [docs/plan-claude.md](docs/plan-claude.md)、[docs/plan-codex.md](docs/plan-codex.md) | 两份独立写成的初稿 |
| [docs/debate/](docs/debate/) | 对抗评审记录：第 1 轮互评，第 2 轮反驳，第 3 轮终审，第 4 轮复核 |

当前已完成 M0 契约冻结：后端包、SQLite 表结构、规则初稿、确定性种子和评测运行器骨架均已建立。M1 将继续打通 Q01/T01 的全链路。

## 本地运行

```powershell
python -m pip install -e ".[dev]"
python -m agentbank.auth.cli --db sqlite:///agentbank.db --user user_001
uvicorn agentbank.api.run:app --reload
```

通过 `POST /v1/sessions` 获取演示会话令牌，再调用余额、转账准备、确认和验证接口。模拟手机只通过配对后的设备令牌访问 `/device/v1/inbox`，不会复用 App 会话。

## 双实例部署

在仓库根目录设置 `PUBLIC_SESSION_SECRET` 和 `EVAL_SESSION_SECRET` 后运行 `docker compose -f deploy/docker-compose.yml up --build`。公共实例使用 8000 端口，评测实例使用 8001 端口，各自使用独立 SQLite 数据卷。
