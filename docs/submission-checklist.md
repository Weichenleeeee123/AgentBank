# M4 本地候选与正式提交核对清单

- [x] 33 项能力逐项 CAP 业务终态用例与追溯，最终执行结果看发布 manifest
- [x] Agent 自然语言、多轮补槽、确认和设备 MFA，本地浏览器证据见 M4 候选说明
- [x] 生日 DAG、快进业务时钟、部分失败补偿回归；计划深链浏览器确认
- [x] PWA、Web IM、模拟手机入口和本地评审说明
- [ ] Docker 双实例实际 build/start/health
- [x] [系统架构](architecture.md)、[算法](algorithms.md)、[安全设计](security-design.md)、[安全自评](security-self-eval.md) 和 [第三方许可清单](THIRD-PARTY-LICENSES.md)
- [x] 固定攻击集、误拦集及性能回归；原始结果随最终门禁归档
- [x] 10 页 PPT 和讲稿（讲稿预算 8 分 35 秒，队伍仍需实际排练）
- [x] 200 字以内作品简介
- [x] README 锁定依赖安装和独立本地运行环境复现
- [ ] 正式提交：核对仓库地址、访问权限、队伍信息、平台实际截止时刻，归档提交回执

演示视频可选，本轮未录制；如制作需不超过 5 分钟。用户要求先本地，公网部署暂缓。

本地冻结以 [M4-LOCAL-CANDIDATE.md](M4-LOCAL-CANDIDATE.md) 规定的完整门禁清单、逐文件摘要和 ZIP 为准。正式平台提交及外部部署需要单独确认，不把本地冻结等同于已提交。

部署验证说明：Compose 配置包含公共/评测双实例、独立 SQLite 数据卷和 `/healthz` 健康检查；评测实例通过 `EVAL_ADMIN_TOKEN` 开启 `/admin/v1/self-test`。本地已验证配置可解析，但当前工作机的 Docker Linux daemon 不可用，因此 build/start/health 仍需在 daemon 恢复后完成。

最新本地修复与证据见 [REVIEW-FIXES-20261101.md](REVIEW-FIXES-20261101.md)。历史 HEAD 的 CI 镜像构建通过，但不等价于修改后容器运行验收。
