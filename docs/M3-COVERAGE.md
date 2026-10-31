# M3 安全闭环门禁表

| 门禁 | 代码/测试证据 | 状态 |
|---|---|---|
| unknown 与 Saga 恢复 | `workflow/recovery.py`, `test_m3_security.py` | 已实现 |
| 人工接管 | `HandoffService`, `/staff/v1/handoffs`, `test_staff_api.py` | 已实现 |
| 反诈 high/medium/unknown | `security/fraud.py`, `evals/security_cases.yaml` | 已实现 |
| 冷静期 | `security/cooling.py`, `test_m3_security.py`, `test_m3_recovery_api.py`, `test_m3_security_closure.py` | 已实现：真实时间、action/revision 绑定、10 分钟复核窗口 |
| 熔断 | `security/breaker.py`, `test_m3_security.py` | 已实现 |
| 回复片段校验 | `security/fragments.py`, `test_m3_security.py`, `test_m3_security_closure.py` | 已实现：数字、金额、状态和金融标签不得由 text 片段伪造 |
| 可解释拒绝 | `security/explain.py`, API error handler | 已实现 |
| 评委自测 run | `/admin/v1/self-test`, `DemoRun`, `test_m3_gate_controls.py` | 已实现：返回 suite/category、audit verify、末尾 checkpoint 和未授权记账计数 |
| 表单直达 | `/v1/forms/transfer`, M1 transfer path | 已实现 |
| 攻击/误拦集 | `evals/*.yaml`, 当前 227 个确定性用例（99 CAP + 117 fraud/SEC/FP + 11 M3 恢复边界） | 已实现 |
| 实测性能 | `evals/api_performance.py`, `test_m3_performance_api.py` | 已实现基础基准 |
| 审计检查点/CLI | `AuditCheckpoint`, `checkpoint_and_verify`, `agentbank-audit`, `test_audit_cli.py`, `test_m3_security_closure.py` | 已实现 |
| 一次性设备配对 | `PairingCode`, `/admin/v1/devices/pairing-code`, `/device/v1/pair` | 已实现 |
| 限流/DLP | `SlidingWindowLimiter`, `security/dlp.py`, `test_m3_security_closure.py` | 已实现基础控制和嵌套/裸 OTP 脱敏 |
| CI secret/frontend/image smoke | `.github/workflows/ci.yml` | 已配置，需 CI 实跑证据 |
| 评测/模拟手机 UI | `/app/eval.html`, `/app/device.html`, `test_m3_ui_surfaces.py` | 已实现 |

“已实现”表示本地代码和确定性测试具备；M4 仍需要实际 Docker、CI、部署和提交材料证据。
