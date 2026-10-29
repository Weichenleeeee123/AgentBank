# M4 提交核对清单（未冻结）

- [ ] 33 项能力逐项 CAP 业务终态证据
- [ ] Agent 自然语言、多轮补槽、确认和 MFA 演示
- [ ] 生日 DAG、快进业务时钟、部分失败补偿演示
- [ ] PWA、Web IM、模拟手机入口和评委说明
- [ ] Docker 双实例实际 build/start/health
- [ ] 系统架构图、核心算法说明、安全设计和安全自评
- [ ] 攻击集、误拦集、性能实测报告
- [ ] PPT（10 分钟以内）和演示视频（5 分钟以内）
- [ ] README 从干净环境复现
- [ ] 提交版本、文件校验值和回执归档

当前清单仍未完成，不代表可以提交。

部署验证说明：Compose 配置包含公共/评测双实例、独立 SQLite 数据卷和 `/healthz` 健康检查；评测实例通过 `EVAL_ADMIN_TOKEN` 开启 `/admin/v1/self-test`。本地已验证配置可解析，但当前工作机的 Docker Linux daemon 不可用，因此 build/start/health 仍需在 daemon 恢复后完成。
