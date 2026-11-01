# 行止（ActSure）

行止（ActSure）是参加深圳国际金融科技大赛人工智能赛道的银行 AI 智能体作品；源码仓库和 Python 包仍使用内部名称 AgentBank。它通过自然语言对话办理转账、账单分析、理财、卡片、订阅和跨场景计划等业务。

**模型负责理解和提出计划，确定性业务内核负责权限判断与执行，用户在操作前明确授权。**行止把授权绑定到具体对象和金额：任务修订会使旧授权失效，提交时重新读取状态并复核风险；结果未知时沿原操作核实，不重复发起付款；计划可暂停、恢复或取消。系统使用本地模拟账户、交易、商户和设备，不连接真实银行，也不处理真实资金。

## 评委快速开始

先按下方“本地运行”安装依赖并启动服务，再打开 [银行 App](http://127.0.0.1:8001/app/)。这是本机地址，服务运行后才能访问。

1. 选择演示账户 `user_001`，输入“查余额”。
2. 输入“给房东转账”，按追问补充“20 元”，核对确认卡后确认。
3. 再查余额和交易记录，观察扣款与流水；不要把待确认状态当作成功。

基础路径无需模型 API Key。自由口语规划需配置模型；设备验证、模拟 IM、生日计划与评测面板的完整步骤见 [评委体验指南](docs/LOCAL-REVIEW-GUIDE.md)。

## 提交材料

- [作品简介](docs/submission-intro.md)
- [系统架构与架构图](docs/architecture.md) · [可单独查看的架构图](docs/architecture.svg)
- [核心算法说明](docs/algorithms.md)
- [能力追溯表](docs/capability-traceability.md)
- [安全设计](docs/security-design.md) · [安全自评报告](docs/security-self-eval.md)
- [评委本地复现与演示指南](docs/LOCAL-REVIEW-GUIDE.md)
- [第三方组件许可清单](docs/THIRD-PARTY-LICENSES.md)
- [正式答辩演示稿](docs/submission/行止（ActSure）-答辩提交版-v3.pptx) 保存在 `docs/submission/`。

代码仓库（内部项目名 AgentBank）：[github.com/Weichenleeeee123/AgentBank](https://github.com/Weichenleeeee123/AgentBank)

## 系统概览

系统覆盖 33 项业务能力，横跨转账、账单分析、理财、卡片管理、订阅管理和跨场景联动。Agent 支持口语化输入、多轮补充信息、计划展示和中断恢复；关键业务状态由服务端事实和账本记录决定。

查询类操作可直接执行；普通风险写操作需用户确认；高风险操作需要独立模拟设备验证码。模型没有直接写账本、发放授权或确认交易成功的权限。授权凭证绑定对象、金额、任务版本和确认卡哈希，修订即失效；提交时复核当前权限和风险。系统以幂等键、事务、审计链和恢复流程约束重复请求与部分失败，未知结果不重复付款。

### 行动前尽调

对公转账和理财申购会先展示行动前尽调，包含主体与资质、风险/财务/信用/口碑四类信号、已知缺口和建议核对问题。尽调摘要参与授权展示校验；主体无法匹配或模拟资质证据未命中时，不生成资金操作，拒绝结果写入审计链。演示仅使用本地模拟企业与产品资料，不连接实时外部数据源；缺失信息明确显示为未覆盖，不代表现实调查结论。理财申购仍须通过原有适当性检查和设备强验证。

## 本地运行

需要 Python 3.12 或更新版本和 Node.js。获取仓库或解压源码包后，在项目根目录的 PowerShell 中运行：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.venv\Scripts\python.exe -m pip install --no-deps -e .
.venv\Scripts\python.exe -m pip check
./scripts/Start-Local.ps1 -Python .venv\Scripts\python.exe
```

默认评测服务只监听本机，端口为 8001。打开 [银行 App](http://127.0.0.1:8001/app/) 后选择演示账户即可开始。其他入口如下：

| 入口 | 地址 | 用途 |
|---|---|---|
| 银行 App | `/app/` | 查询账户、办理业务和用户确认 |
| 模拟 IM | `/app/webim.html` | 独立 IM 身份绑定、私聊与群聊场景 |
| 模拟手机 | `/app/device.html` | 设备配对和接收模拟 OTP |
| 评测面板 | `/app/eval.html` | 运行固定本地评测 |
| 坐席台 | `/app/staff.html` | 查看待办和提交重试申请；不能代用户付款 |

无模型服务配置时，确定性查询、表单和安全门禁仍可运行。接入模型时，按 `.env.example` 配置服务商凭证。请使用合成数据，不要输入真实账号、密码、验证码或个人金融信息。

## 测试与部署

完整测试入口会运行依赖检查、静态检查、Python 与前端测试、固定评测和 HTTP 自测：

```powershell
.venv\Scripts\python.exe scripts/release.py
```

固定评测由 99 条能力旅程、80 条安全用例、27 条误拦用例、10 条额外欺诈用例和 11 条恢复用例组成，共 227 条合成测试。评测通过只说明这些固定样例通过，不代表真实用户任务完成率或线上安全效果。

项目包含双实例 Docker Compose 配置。Compose 构建和本地/CI 检查不等于真实生产部署验证；公网 HTTPS、真实飞书租户、短信/人脸/U 盾和真实银行或商户接口需要分别配置和验收。详细部署步骤与约束见 [部署说明](docs/LOCAL-RELEASE.md)。
