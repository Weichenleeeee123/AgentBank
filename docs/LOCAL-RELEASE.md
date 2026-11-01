# 本地运行与发布包说明

## 获取源码与安装

获取 [AgentBank 源码仓库](https://github.com/Weichenleeeee123/AgentBank) 或解压源码包。需要 Python 3.12 或更新版本和 Node.js；以下命令适用于 Windows PowerShell，在包含 `pyproject.toml` 的项目根目录执行。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.venv\Scripts\python.exe -m pip install --no-deps -e .
.venv\Scripts\python.exe -m pip check
```

项目使用锁定依赖。源码包不包含私有环境文件、数据库、日志、审计密钥或 Git 元数据。

## 启动本地演示

```powershell
./scripts/Start-Local.ps1 -Python .venv\Scripts\python.exe
```

保持终端运行，打开 [银行 App](http://127.0.0.1:8001/app/)。评测模式只监听本机，默认端口 8001。选择演示账户 `user_001`，按 [评委体验指南](LOCAL-REVIEW-GUIDE.md) 完成查询、补充金额和确认转账。

启动器在 `.tmp/local-demo/eval.env` 生成独立的会话、审计、管理员和坐席凭证，数据库位于 `.tmp/local-demo/eval.db`。评测面板使用 `AGENTBANK_ADMIN_TOKEN`，坐席台使用 `AGENTBANK_STAFF_TOKEN`。在本机查看并输入对应页面，不将令牌放入聊天、URL、截图或提交材料。

重启时保留数据库、凭证和审计锚点；种子初始化不会重置已有演示记录。公共演示模式同样只监听本机，端口 8000，使用独立数据和凭证，并关闭管理员与坐席接口：

```powershell
./scripts/Start-Local.ps1 -Mode public -Python .venv\Scripts\python.exe
```

## 配对模拟设备

保持评测服务运行，在项目根目录另开 PowerShell：

```powershell
./scripts/New-DevicePairing.ps1 -UserId user_001
```

将一次性配对码输入 [模拟手机](http://127.0.0.1:8001/app/device.html)。红色业务准备后，在设备页读取对应模拟 OTP，再回 App 安全表单提交。这不是短信、人脸或 U 盾认证。

## 可选模型配置

基础查询、固定话术、表单和固定评测无需模型服务。体验自由口语和模型规划时，将 `.env.example` 复制为项目根目录下的 `.env`；已有文件则直接编辑，保留现有配置。填写以下字段：

| 配置项 | 含义 |
|---|---|
| `AGENTBANK_LLM_PRIMARY_BASE_URL` | 兼容接口的服务地址 |
| `AGENTBANK_LLM_PRIMARY_API_KEY` | 自行提供的模型服务密钥 |
| `AGENTBANK_LLM_PRIMARY_MODEL` | 模型名称 |
| `AGENTBANK_LLM_PRIMARY_TIMEOUT_SECONDS` | 请求超时设置 |

保存后重启本地服务。启动脚本按字面值读取 `.env`，不执行其中的内容。备用服务为可选配置。模型请求可能产生服务商费用；模型及中继的数据处理边界见 [安全设计](security-design.md)。

## 自动检查与源码打包

仅检查并生成报告：

```powershell
.venv\Scripts\python.exe scripts/release.py
```

检查通过后额外生成源码包：

```powershell
.venv\Scripts\python.exe scripts/release.py --package
```

两条命令按需要选择一条即可。检查包括依赖一致性、Ruff、严格 Mypy、Python 测试、前端测试与语法检查、227 条固定评测和 HTTP 自测。HTTP 自测使用新建的隔离数据库，不连接线上服务或真实模型。

每次运行在 `.tmp/releases/` 下创建独立目录，包含 `gates.json`、逐项检查日志、`eval-cases.json` 和 `http-self-test.json`。检查通过后生成 `manifest.json`；使用 `--package` 时还生成带逐文件校验值的源码包。检查失败或运行期间源码变化时拒绝打包。可以据此核对源码与测试证据。

固定评测包含 99 条能力旅程、80 条安全用例、27 条误拦用例、10 条额外欺诈用例和 11 条恢复用例。它们使用合成账户和交易，结果只描述固定样例行为。

## Docker Compose 配置

配置位于 `deploy/docker-compose.yml`。为 `PUBLIC_SESSION_SECRET`、`PUBLIC_AUDIT_SECRET`、`EVAL_SESSION_SECRET`、`EVAL_AUDIT_SECRET`、`EVAL_ADMIN_TOKEN`、`EVAL_STAFF_TOKEN` 分别设置独立随机值，并存入私有环境文件或环境变量。

在可用的 Docker Linux 环境中执行：

```powershell
docker compose --env-file .env -f deploy/docker-compose.yml config --quiet
docker compose --env-file .env -f deploy/docker-compose.yml build
docker compose --env-file .env -f deploy/docker-compose.yml up -d
docker compose --env-file .env -f deploy/docker-compose.yml ps
```

公共实例映射端口 8000，评测实例映射端口 8001，使用独立 SQLite 数据卷和审计密钥，健康检查为 `/healthz`。公共实例关闭管理员与坐席接口；评测管理接口要求 `X-Admin-Token`，坐席接口要求独立的 `X-Staff-Token`。Compose 端口映射与仅本机监听的启动脚本不同，应按运行环境限制端口访问。

## 部署边界

本地检查与 CI 构建不等同于生产部署验收。Docker 双实例运行、公网 HTTPS、真实飞书租户、短信/人脸/U 盾，以及真实银行和商户接口需要在目标环境分别配置与验收。
