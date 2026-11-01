# AgentBank

2026 深圳国际金融科技大赛（微众银行金融科技大学生挑战赛），人工智能赛道（国内）参赛作品：银行 AI 智能体。

核心原则：**模型只负责提议，执行与否由代码裁决，最终由用户在带外确认。**

在线自然语言理解由 LLM 负责，输出结构化业务动作、槽位和原文依据；代码校验来源、单位、日期、权限和业务终态，不再让正则解析覆盖模型判断。手机号以不透明引用交给模型，复杂操作无法可靠理解时追问。保留的离线解析用于确定性评测与受限降级。

## 文档

| 文件 | 内容 |
|---|---|
| [docs/PLAN.md](docs/PLAN.md) | **整体方案与开发计划（定稿）**：33 项能力矩阵、架构、契约、权限规则、安全机制、测试、里程碑、提交物 |
| [docs/brief.md](docs/brief.md) | 赛题事实，是所有设计判断的唯一依据 |
| [docs/architecture.md](docs/architecture.md) | 当前系统边界、Agent、执行内核、DAG 和渠道 |
| [docs/algorithms.md](docs/algorithms.md) | 已实现算法和验证口径 |
| [docs/security-self-eval.md](docs/security-self-eval.md) | 已验证安全行为、已知边界和禁止的过度结论 |
| [docs/M4-LOCAL-CANDIDATE.md](docs/M4-LOCAL-CANDIDATE.md)、[docs/LOCAL-RELEASE.md](docs/LOCAL-RELEASE.md) | 本地候选范围、锁定安装、验收和打包 |
| [docs/submission-checklist.md](docs/submission-checklist.md) | M4 提交门禁清单（当前未冻结） |
| [docs/M3-COVERAGE.md](docs/M3-COVERAGE.md) | M3 安全闭环逐项证据 |
| [docs/plan-claude.md](docs/plan-claude.md)、[docs/plan-codex.md](docs/plan-codex.md) | 两份独立写成的初稿 |
| [docs/debate/](docs/debate/) | 对抗评审记录：第 1 轮互评，第 2 轮反驳，第 3 轮终审，第 4 轮复核 |

AgentBank 的 M2 本地模拟业务闭环已实现：33项能力、planner/explorer/reply/fraud角色、有限只读组合计划、原子授权、报价型生日DAG、PWA直达表单、持久Web IM防重放和可执行CAP追溯。无provider时，模型不能为转账背书，系统保留确定性查询/表单路径。真实银行、模型/商户/推送/飞书租户、真实环境红队与M4发布材料分别验收；当前优先收口可复现的本地候选，不要求立即公网发布。逐项证据见 docs/M2-M3-STATUS.md。M3 本地安全门禁已通过，验证与原始报告见 [M3 本地验收证据](docs/M3-LOCAL-EVIDENCE.md)。

## 本地运行

需要 Python 3.12+ 和 Node.js。已在 Windows / Python 3.13 的独立环境验证锁定安装；每次复核使用新的目录名，保留旧环境和证据：

```powershell
python -m venv .tmp/m4-review-001
.tmp/m4-review-001/Scripts/python.exe -m pip install -r requirements-dev.lock
.tmp/m4-review-001/Scripts/python.exe -m pip install --no-deps -e .
.tmp/m4-review-001/Scripts/python.exe -m pip check
./scripts/Start-Local.ps1 -Python .tmp/m4-review-001/Scripts/python.exe
```

启动器仅监听 `127.0.0.1`，默认评测实例端口为 8001。可选的模型配置从 `.env` 按字面值读取，不执行内容；独立生成的会话、审计、管理员和坐席凭证持久保存在忽略跟踪的 `.tmp/local-demo/eval.env`，数据库在 `.tmp/local-demo/eval.db`。这些文件和审计锚点应一起保留，不提交、不截图。空 provider 配置仍支持确定性查询、表单和本地评测。

| 本地入口 | 使用方式 |
|---|---|
| 银行 App：[http://127.0.0.1:8001/app/](http://127.0.0.1:8001/app/) | 选择演示账户 `user_001` 等，查询、办理业务、确认与验证 |
| 模拟 IM：[http://127.0.0.1:8001/app/webim.html](http://127.0.0.1:8001/app/webim.html) | 先在 App 选择账户，再连接独立 IM 身份并明确绑定当前账户；高风险操作回到 App 验证 |
| 模拟手机：[http://127.0.0.1:8001/app/device.html](http://127.0.0.1:8001/app/device.html) | 输入管理员一次性配对码后读取 OTP；设备令牌独立于银行会话 |
| 评测面板：[http://127.0.0.1:8001/app/eval.html](http://127.0.0.1:8001/app/eval.html) | 输入本地 `AGENTBANK_ADMIN_TOKEN` |
| 坐席台：[http://127.0.0.1:8001/app/staff.html](http://127.0.0.1:8001/app/staff.html) | 输入独立 `AGENTBANK_STAFF_TOKEN`；处理待办和重试申请，付款仍需用户重新授权 |

另开终端，为默认用户 `user_001` 生成一次性配对码：

```powershell
./scripts/New-DevicePairing.ps1
```

页面需要的本地令牌由操作者从 `.tmp/local-demo/eval.env` 读取并直接输入密码框，避免粘贴到聊天、URL 或提交物。公共模式使用 `./scripts/Start-Local.ps1 -Mode public -Python .tmp/m4-review-001/Scripts/python.exe`，端口 8000、数据与凭证独立，管理员与坐席令牌关闭。

API 包括 `POST /v1/sessions`、`/v1/conversations/{id}/messages`、签名 Web IM `/v1/im/webim/events` 和飞书适配器 `/v1/im/feishu/events`。模拟 IM 页面不需要把网关签名密钥暴露给浏览器。飞书适配器测试不等于真实飞书租户联调；实际租户凭证、回调和权限须另行验证。

所有能力也可以通过统一入口调用：

```json
POST /v1/capabilities/Q01
{"capability_id":"Q01","idempotency_key":"demo-q01-1","payload":{}}
```

命令结果会写入 `capability_records`，并返回可追溯的审计事件。需要用户确认的 T01/T02/T03 先返回 `record_id`、`plan_id`、`receipt_id` 和 `display_hash`，再调用 `/v1/capability-records/{record_id}/confirm`。

本地最终验收与源码候选包由同一脚本生成；完整命令、证据文件和排除规则见 [LOCAL-RELEASE.md](docs/LOCAL-RELEASE.md)：

```powershell
.tmp/m4-review-001/Scripts/python.exe scripts/release.py
.tmp/m4-review-001/Scripts/python.exe scripts/release.py --package
```

脚本检查依赖、Ruff、Mypy、所有 Python/前端测试、前端语法、227 条确定性评测和 HTTP 自测。全部通过且源码在检查期间未变化，才写入通过清单并允许打包；每次输出到唯一 `.tmp/releases/` 子目录。源码包包含锁文件、静态资源、提示词、测试、文档与 CI，不包含 `.env`、数据库、日志或本地审计密钥。当前冻结状态以 [本地候选说明](docs/M4-LOCAL-CANDIDATE.md) 和最终清单为准。

## 双实例 Compose（单独验收）

Compose 配置保留为可选部署路径，当前本地候选不以 Docker 或公网可用为完成依据。为 `PUBLIC_SESSION_SECRET`、`PUBLIC_AUDIT_SECRET`、`EVAL_SESSION_SECRET`、`EVAL_AUDIT_SECRET`、`EVAL_ADMIN_TOKEN`、`EVAL_STAFF_TOKEN` 分别生成独立随机值，并通过环境变量或私有环境文件提供；`.env.example` 只列变量名，不提供可用于运行的默认密钥。

在准备验证 Docker 时，先运行 `docker compose --env-file .env -f deploy/docker-compose.yml config --quiet`，避免输出展开后的凭证；Docker daemon 可用后再单独执行 build/start/health 验证。公共实例 8000、评测实例 8001，使用独立数据卷和审计密钥；公共实例禁用管理员与坐席令牌，评测自测要求 `X-Admin-Token`，坐席要求独立的 `X-Staff-Token`。目前未执行本轮容器构建、公网 HTTPS、真实飞书或真实银行验收。

升级兼容说明：本轮将通用业务确认页内容绑定到确认哈希。升级前尚未确认的旧凭证可能校验失败，需要重新发起并核对确认；已执行业务不受影响。
