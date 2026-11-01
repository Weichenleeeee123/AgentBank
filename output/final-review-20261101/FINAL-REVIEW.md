# AgentBank 参赛前全量复审

复审日期：2026-11-01（UTC+8）
复审对象：当前工作树，历史 HEAD `cc2eb71c0dacba2458ae9386d23498a07a52deb4` 加未提交修复。
结论：**本地候选达到提交前技术放行条件；未替队伍完成平台提交，也不对获奖结果作保证。**

## 结论依据

- 最新完整门禁与源码包：`.tmp/releases/20261101T061528Z-e47b43b4/`
- `release.py --package`：13/13 门禁通过，源码验证期间无变动。
- Python：704 passed；Node 前端：34/34 passed；227/227 固定评测通过。
- HTTP 自测：4/4；审计链验证通过；样例中的未授权账本写入为 0。
- Ruff、严格 Mypy、依赖一致性和全部前端语法检查通过。
- 最新答辩 PPT：10 页、10 份讲稿备注，预算 515 秒（8 分 35 秒）。当前 PPT 已重新渲染逐页检查；溢出扫描通过。目标机器上的 PowerPoint 原生放映仍待队伍实测。
- 375×812 浏览器中真实进入十题风险问卷；滚到页面底部时末题和提交按钮没有被输入框遮住，App 页无控制台错误。

固定评测衡量的是仓库内合成样例，不等于线上成功率或误拦率。此次没有重新连接真实 LLM、Docker daemon、公网、真实飞书或真实银行。

## 复审发现与修复

当前没有确认的 P0/P1 金融安全缺陷。复核了准备与提交阶段的授权重检、未来付款、双余额与流水、理财订单持仓、PIN/OTP、计划暂停/恢复/取消、幂等和审计闭环；相关交叉路径回归纳入本次 704 项测试。

本轮额外复现并修复一个 P2：生日计划 X02 资金预留在账户冻结后仍可确认，会降低可用余额并留下 active reservation。现在预留事务会复核账户仍为 active；冻结发生在准备之后时返回 `ACCOUNT_NOT_ACTIVE`，余额不变且不创建预留。先新增回归并确认其按预期失败，再修复；当前该测试与财务闭环通过。

提交材料方面，README 增加 GitHub 仓库入口和评委演示指南，历史 M3 门禁表述改成历史状态；提交简介控制在 200 字内且明确本地模拟、不接触真实资金。App/PWA 主入口补充现有图标，移动端问卷增加滚动空间。

## 仍需队伍完成

- 确认 GitHub 仓库对评委可访问；README 已列地址，但本轮未验证评委权限。
- 在报名平台核对队伍信息、上传格式与 11 月 2 日的具体截止时刻，提交后保存有效回执。
- 队伍按讲稿实际排练一次；有条件时在答辩设备用 PowerPoint 原生播放当前 PPT。
- Docker Compose build/start/health 未通过本机 daemon 实测。公网、真实飞书及真实金融机构接入也未验收；项目材料已明确这些边界。
- 演示视频按赛题规则为可选项，本轮没有录制。

## 提交产物

- 最新源码包：`.tmp/releases/20261101T061528Z-e47b43b4/agentbank-m4-source.zip`
- 逐文件摘要与门禁：同目录 `manifest.json`、`gates.json` 和 `check-*.txt`
- 评测与 HTTP 原始结果：同目录 `eval-cases.json`、`http-self-test.json`
- 最新 PPT：`docs/submission/AgentBank-M4-Defense.pptx`
- 移动端问卷复查截图：`output/playwright/review-20261101-questionnaire-mobile.png`
- 本报告：`output/final-review-20261101/FINAL-REVIEW.md`

源码包 SHA-256：`0fbd12348b0cf6eefac2f47962ce5277507d2aa096ec4d33ff047d1fd922f63d`。发布 manifest 记录 269 个源码文件及完整门禁证据。

工作树仍包含未提交改动。没有删除旧报告、旧候选、测试环境或截图；本地源码候选通过不代表 GitHub 已推送或平台已提交。
