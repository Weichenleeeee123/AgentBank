# M3 本地验收证据

日期：2026-10-31。验收范围是用户确认的 A：本地模拟完整收口；不包含 M4 部署/发布。环境为 Windows、Python 3.13.3、SQLite，模型调用使用确定性测试 transport 或无 provider 路径。

## 验收结果

| 检查 | 实测 |
|---|---|
| 全量后端和部署配置 pytest | **538 passed**，0 failed / 0 errors / 0 skipped；293.72 秒 |
| Ruff | 通过 |
| Mypy | 62 个源文件通过 |
| Node 语法 | `app.js`、`eval.js`、`staff.js` 均通过 |
| Compose | YAML 解析及 `tests/test_deployment_smoke.py` 通过；本轮未执行 Docker 构建/部署 |
| 完整评测 | **227/227** |
| HTTP 自测 | 门禁全部通过，报告保存后按 run ID 读取一致 |
| 实际账本检查 | 80 条 SEC 均检查自身测试数据库；未授权分录合计 **0** |
| 审计 | SEC 各库的链及末尾 checkpoint、HTTP 自测库检查点均通过；数据库外 HMAC 文件的截断检测有单独负向测试 |
| FP 样例 | **27/27** 与预期一致；仅代表这些样例，非真实流量误拦率 |

完整评测组成：99 CAP + 80 八类 SEC + 27 FP + 10 额外 fraud + 11 M3 恢复边界。八类 SEC 各 10 条：直接注入、间接注入、两轮污染、诱导转账、信息套取、越权、累计限额、确认篡改/重放。每条证据包含实际观测、错误码、operation/ledger/审计 ID、凭证消费检查和审计完整性结果。

## 关键运行时保证

- 普通确认与 MFA 均复核 owner、receipt/action/display、revision、真实时间有效期和写锁；MFA 不再跳过反诈或哈希检查。
- 冷静期占用可用余额但不出账。到期后二次确认产生新短效凭证及 OTP，十分钟复核窗口内验证后提交；取消释放占用。
- OTP 按 challenge 和用户计数；跨登录锁定仍有效。API 的认证伪造/越权自动计入 U17；保护操作按具体动作判断。
- U18 熔断区分基础设施故障与业务拒绝；查询可继续。模型每小时实际调用次数预算覆盖所有角色、主备及重试。
- 服务端风险记录不由模型清除；模型省略备注时仍受风险限制。high 拒绝并转人工，medium/unknown 在转账提交路径中进入冷静期。
- 设备配对一次性事务及审计；OTP 不从 App 会话读取，不在聊天中消费，用户贴入聊天的 OTP 在历史中脱敏。
- 人工工单保存节点/授权快照、脱敏处理备注并写审计；无替代用户授权接口。Saga 的补偿失败有明确列表，不阻断其他补偿项的尝试。
- 公共实例禁用全部管理接口；Web IM/飞书模拟适配器持久防重放，群聊结果和卡回调不公开账户内容。
- 自测从各攻击用例的数据库汇总未授权分录，缺少证据时不报告测量通过；报告持久保存并支持下载。

## 本地性能

在全量回归同时运行时测量，使用 FastAPI TestClient 和本地 SQLite；不包含公网延迟和真实模型延迟。

| 路径 | 次数 | P50 | P95 |
|---|---:|---:|---:|
| 余额 API | 20 | 23.367 ms | 37.816 ms |
| 无 provider 对话余额查询 | 20 | 70.021 ms | 103.133 ms |
| 转账 prepare + confirm | 20 | 95.474 ms | 180.247 ms |
| 确定性反诈规则 | 100 | 0.001 ms | 0.002 ms |

HTTP 完整自测测得 13,510.581 ms。上述数字是本机基线，不是线上 SLA。

## 复现

```powershell
$env:PYTHONPATH = (Get-Location).Path + '\backend'
python -m pytest backend/tests tests -o addopts='' -q
python -m ruff check backend
python -m mypy backend/agentbank
node --check frontend/app.js
node --check frontend/eval.js
node --check frontend/staff.js
python -c "from pathlib import Path; from agentbank.evals.runner import run_cases; r=run_cases(Path('evals')); print(r.total,r.passed,r.failed); assert r.failed==0"
```

页面入口：`/app/`、`/app/eval.html`、`/app/device.html`、`/app/staff.html`。自测使用评测实例管理员 token；报告读取为 `GET /admin/v1/self-test/{run_id}`。设备通过管理员配对码在独立模拟手机页面配对。

## 原始产物与追溯

- [全部评测结果](evidence/m3-local/evaluation.json)
- [HTTP 自测及逐用例证据](evidence/m3-local/api-self-test.json)
- [性能实测](evidence/m3-local/performance.json)
- [pytest 结果摘要](evidence/m3-local/pytest-summary.json)
- [已验收代码/前端/提示词/规则/用例源码哈希](evidence/m3-local/source-manifest.json)

原始 HTTP 报告保留当时的 git revision 与 dirty 标记；源码哈希清单将 CRLF 规范为 LF 后计算 SHA-256，以适配仓库换行规则，用于对应验收时的工作树，最终文档提交不会冒充更早的运行版本。

## 保留边界

设备与银行/商户均为模拟；无 provider 时保留确定性路径，真实 provider 的效果和不同故障域切换不能由这些样例证明。飞书是本地签名模拟协议，真实租户、推送、HTTPS、CI/Docker 实跑与 M4 材料仍独立验收。亲情守护是原设计可选项，本轮未启用。U18 工具熔断为进程内状态，用户锁定和模型调用预算为持久化状态。

外部 HMAC 检查点是单独文件；生产应将密钥与检查点放入独立受控存储。拥有密钥的人能伪造签名，最新检查点之后尚未锚定的尾段仍存在截断窗口。固定样例不能证明真实世界所有攻击均被拦截，也不能直接推算全部四类业务误拦率。
