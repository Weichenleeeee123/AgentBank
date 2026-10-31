from __future__ import annotations

_REASONS = {
    "U07": {"category": "反诈风险", "message": "检测到疑似诱导转账话术。", "next_step": "请停止转账并联系人工坐席核实。"},
    "U17": {"category": "安全锁定", "message": "近期安全拦截次数达到阈值。", "next_step": "保护性操作仍可执行，其他写操作请联系人工处理。"},
    "P02": {"category": "权限不足", "message": "当前身份无权操作该资源。", "next_step": "切换到资源所有者的已认证会话。"},
}


def explain_denial(reason_code: str) -> dict[str, str]:
    aliases = {"FRAUD_HIGH": "U07", "FRAUD_REVIEW_REQUIRED": "U07", "SECURITY_LOCKED": "U17",
               "OTP_LOCKED": "U16", "OTP_INVALID": "U16", "OTP_EXPIRED": "U16",
               "NOT_OWNER": "P02", "TOOL_CIRCUIT_OPEN": "U18", "AUDIT_UNAVAILABLE": "U19",
               "HASH_MISMATCH": "U09", "ACTION_CHANGED": "U09", "RECEIPT_REPLAYED": "U09",
               "MESSAGE_PROCESSING_FAILED": "U20", "MESSAGE_OUTCOME_UNKNOWN": "U20",
               "CONVERSATION_BUSY": "U21",
               "COOLING_PERIOD_ACTIVE": "U08", "COOLING_RECONFIRMATION_REQUIRED": "U08"}
    additions = {
        "U08": {"category": "冷静期", "message": "付款尚未执行，需等待并重新验证。", "next_step": "等待期满后在 App 强验证，或取消并释放资金。"},
        "U09": {"category": "授权不匹配", "message": "确认内容已变更或凭证已使用。", "next_step": "重新核对操作并发起新的确认。"},
        "U16": {"category": "设备验证", "message": "设备验证未通过或已锁定。", "next_step": "在模拟手机查看新验证码，锁定时等待或联系人工。"},
        "U18": {"category": "工具暂不可用", "message": "服务暂时中断，尚未继续提交。", "next_step": "稍后查询原操作状态再申请重试。"},
        "U19": {"category": "审计异常", "message": "无法验证审计完整性。", "next_step": "停止新的付款并联系管理员核实原操作状态。"},
        "U20": {"category": "消息结果待核实", "message": "消息处理未完成，原操作可能已经准备或提交。", "next_step": "先在 App 查询原操作和账本状态，必要时联系人工；不要重复发起付款。"},
        "U21": {"category": "会话处理中", "message": "当前会话正在处理上一条消息，本条尚未开始。", "next_step": "等待上一条消息完成后重试本条消息；若结果未知，请先查询原操作状态。"},
    }
    rule = aliases.get(reason_code, reason_code)
    result = dict({**_REASONS, **additions}.get(rule, {"category": "策略拒绝", "message": "操作不满足当前安全策略。", "next_step": "检查参数后重新授权或联系人工。"}))
    result["rule_id"] = rule
    return result
