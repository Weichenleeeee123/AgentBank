# 2026-11-01 审查修复与验收说明

本轮对应历史全项目审查的 F1–F7。未删除任何文件；旧候选、环境及证据保留。源码为本地工作树修改，未推送、未部署、未平台提交；最终文件摘要由新 release manifest 唯一标识，不把历史 HEAD 的 CI 结果冒充新源码结果。

## 问题关闭与回归

| 项目 | 修复后行为 | 长期回归 |
|---|---|---|
| F1 旧转账授权 | 结构化任务关系及当前话语依据；语义修订原子作废旧记录；App/模拟 IM 禁用旧按钮；显式新任务可保留旧任务 | test_review_conversation.py、前端 app.test.mjs |
| F2 旧降额凭证提额 | 提交事务内以最新卡状态重算权限，低等级旧授权拒绝并要求重新准备；确认/验证/DAG 共用防线 | test_review_payment_guards.py |
| F3 AA 高额支付 | 应付份额、金额、付款人及发起人账户绑定操作和展示；复核日累计、风险、锁定及 MFA | test_review_payment_guards.py（含真实 DeviceService OTP、1000元边界、累计升级） |
| F4 生日计划过早成功 | scheduled 后父计划 waiting_trigger；暂停阻断调度；取消撤销未执行授权、订单和任务并释放剩余预留；实际终态回填父计划 | test_review_plan_lifecycle.py、更新既有生命周期断言 |
| F5 取消否定/对象 | 在线由 planner 解析当前任务关联，验证当前原文；离线仅有限命令语法；取消视频订阅不取消转账 | test_review_conversation.py |
| F6 AA 发起人不到账 | 同一事务真实模拟账户借贷；份额/Operation 幂等；发起人可用与账面余额实际增加 | test_review_payment_guards.py |
| F7 问卷缺失/到期写死 | 十题题干及六档选项，无默认答案；完整校验、版本绑定、确认和答案证据持久化；有效期次年同日、闰日夹至月末 | test_review_risk_questionnaire.py、前端 app.test.mjs |

另修复单笔消费误列订阅；新种子提供所有三名演示用户连续 24 月合成收入/支出（2024-11 至 2026-10）。已有数据库不重置，使用新演示库才能取得补充种子；旧余额和旧交易不被篡改。覆盖见 test_review_analysis_quality.py。

## 功能复验

真实主模型六场景：语义改金额、否定取消、切换取消订阅、显式另建转账、风险问卷、生日确认/暂停/取消均通过。[脱敏摘要](evidence/review-fixes-20261101/live-summary.json)；本机原始结果保留在 .tmp/review-fixes-20261101/live-regressions.json。这是小样本网络联调，不等于所有能力的线上成功率。

Playwright 实际页面已验证：20元改30元后旧按钮禁用；十题均选3分、核对确认后返回C3/30分/2027-11-01；生日确认后显示等待到期执行，页面暂停、取消成功且取消后继续按钮禁用。截图保留在 output/playwright/review-fix-*.png。2026-11-01 复查补充 375×812 移动视口，十题问卷末题与提交按钮滚动到底部后不被 sticky 输入框遮挡；App 主入口补充 PWA favicon，页面无业务脚本错误。

## 统一验收与复现

最终命令：`.tmp/m4-clean-20261031-1635/Scripts/python.exe scripts/release.py --package`。

通过证据写入唯一 `.tmp/releases/<UTC时间-随机值>/`：manifest.json（逐文件 SHA-256、总摘要和门禁）、gates.json、各门禁原始日志、eval-cases.json、http-self-test.json、agentbank-m4-source.zip。只有全部检查通过且检查期间源码未变化才生成通过 manifest 与源码 ZIP；本说明不预填尚未完成的最终测试数字。

本轮先行固定评测为227/227，HTTP自测4/4、样例内未授权分录0、审计通过；最终以新 manifest 同目录结果为准。固定样例只证明样例范围，27 FP通过不是生产误拦率。未取得真实线上总体任务成功率、四类误拦率或端到端P95，不用目标数字填充。

## 全项目跨路径闭环（2026-11-01）

针对重复审查后仍漏出的交叉路径问题，新增长期回归：`test_financial_integrity_closure.py`、`test_plan_integrity_closure.py`、`test_pin_integrity_closure.py`。共同记账模块现把付款、赎回、流水和本地对手方入账放在同一原子事务；延期转账、商户派发、订阅续费在执行当天检查账户状态、用户锁、风险和日累计。实时转账、AA 与延期转账采用同一个可推进业务日。投资申购和赎回按订单关联独立 lot；旧版无法唯一归属的订单进入待人工对账并单独跳过。缺失月份/不完整年度不生成误导性百分比。

生日计划修改原子取消旧计划并释放预留、撤销未执行授权；暂停恢复会恢复已消费 DAG 节点的持久化输出。任意通用节点名不再触发模板分支，通用转账 DAG 提前拒绝并要求使用带有专属收款人核对及安全验证的单笔流程。PIN 在 MFA 前只暂存带盐哈希，MFA 成功后才写入卡状态，明文不存业务记录；此实现仍是本地模拟，不是银行密码处理系统。

全量结果、Ruff/Mypy、真实 provider 冒烟、浏览器页面与资金查询证据以本轮新产物为准；不沿用前一轮的发布结论。

本轮追加 P2 回归：生日计划资金预留在确认事务内复核资金账户状态；准备后账户被冻结时返回 `ACCOUNT_NOT_ACTIVE`，不扣可用余额、不创建 active reservation。长期回归见 `test_financial_integrity_closure.py::test_frozen_account_cannot_reserve_birthday_funds_after_confirmation`。

## 材料同步与范围

已更新架构（在线LLM/离线规则、六节点DAG、实际静态HTML/JS）、逐算法输入输出/失败条件/例子、独立安全设计、第三方依赖许可清单、安全自评及提交核对表。PPT保持10页及原尺寸/字体，讲稿预算515秒（8分35秒），需实际排练；包结构、布局和字体校验通过，原稿备份在 .tmp/review-fixes-20261101/deck-original.pptx。修改页已渲染检查，全稿已扫描。

仍未声称兑现的设计扩展：在线关系图/行为特征完整风控、独立故障域真实备用provider、真实大样本指标；离线规则不承诺理解任意语言。DSL替代任意代码执行，是否满足主办方对沙箱的具体解释需核对答疑。视频官方为可选，本轮未录制。

提交外部条件仍需队伍办理：核实最新赛题答疑/截止具体时刻和上传格式、评委对私有仓库的访问权限、平台上传及有效回执。历史HEAD的Linux CI镜像build成功；本轮修改后镜像与Compose双实例运行/health、公网HTTPS和真实飞书租户尚未验收。真实银行/商户/短信不属于本地模拟原型的接入范围。代码修复完成与平台正式提交是两个不同状态。
