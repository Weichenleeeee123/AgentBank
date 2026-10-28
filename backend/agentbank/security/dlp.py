from __future__ import annotations

import re
from typing import Any


_PATTERNS = (
    (re.compile(r"(?<!\d)(\d{16,19})(?!\d)"), "[CARD_REDACTED]"),
    (re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)"), "[PHONE_REDACTED]"),
    (re.compile(r"(?i)(otp|验证码)\s*[:：]?\s*\d{6}"), "[OTP_REDACTED]"),
)


def redact_text(value: str) -> str:
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def redact_payload(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(key): redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    return value
