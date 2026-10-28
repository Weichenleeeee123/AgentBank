from __future__ import annotations

_REASONS = {
    "U07": {"category": "反诈风险", "message": "检测到疑似诱导转账话术。", "next_step": "请停止转账并联系人工坐席核实。"},
    "U17": {"category": "安全锁定", "message": "近期安全拦截次数达到阈值。", "next_step": "保护性操作仍可执行，其他写操作请联系人工处理。"},
    "P02": {"category": "权限不足", "message": "当前身份无权操作该资源。", "next_step": "切换到资源所有者的已认证会话。"},
}


def explain_denial(reason_code: str) -> dict[str, str]:
    return dict(_REASONS.get(reason_code, {"category": "策略拒绝", "message": "操作不满足当前安全策略。", "next_step": "检查参数后重新授权或联系人工。"}))
