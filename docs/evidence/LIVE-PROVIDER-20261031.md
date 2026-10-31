# 真实主模型本地冒烟

最终证据：[live-provider-20261031-final.json](live-provider-20261031-final.json)。由 `scripts/validate_live_provider.py` 通过真实主 provider HTTP 运行，使用本地种子数据和内存数据库，没有执行转账或连接真实银行。凭证、URL、模型名和原始响应不写入此报告。

- planner / explorer / reply / fraud 四角色均获得可用结果，未以模板降级冒充回复模型成功。
- 六步业务：查余额、给房东转账的澄清、补充 20 元后的确认、查理财持仓、查订阅、查交易记录，均通过语义断言。转账额和收款人另查服务端计划节点，确认为房东、2000 分；准备后账本分录数未变。
- 共 14 次真实请求：planner 7、reply 4、explorer 1、fraud 2。六步耗时分别 3.669、2.855、7.952、3.118、1.758、3.620 秒。这是单次、低样本本地网络结果，不代表并发吞吐或线上 SLA。
- 主 provider 配置超时 8 秒，fraud 角色仍使用代码限定的最多 2 秒超时。未配置备用 provider，因此不声明真实主备切换；备用失败/降级由确定性 transport 回归验证。

先前失败证据保留：[live-provider-20261031.json](live-provider-20261031.json)。金额补充时模型切换 T01/T03 曾导致收款上下文丢失，修复已提交 `edfb6e1`，9 项回归覆盖路由变化、模型虚构收款人、错误金额和用户明确改收款人。真实模型重跑后六步全部通过。回复 prompt 同时收紧为正文不输出数字，金额等事实由原样片段渲染，保留服务端事实检查器。

复验（可产生模型调用费用，显式运行，与默认离线门禁分开）：

```powershell
. ./scripts/Import-LocalEnv.ps1
Import-LocalEnv .env
python scripts/validate_live_provider.py --output .tmp/live-provider-new-run.json
```

输出路径必须是新文件，旧证据不覆盖。调用失败或事实校验降级会反映在报告和退出码中。模型输出具有波动性，单次冒烟不替代全部业务和安全回归。
