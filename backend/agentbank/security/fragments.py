from __future__ import annotations

from typing import Any

from agentbank.errors import DomainError


class ReplyFragmentChecker:
    _forbidden = ("收入", "支出", "余额", "转账", "扣款", "到账")

    def validate(self, fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for fragment in fragments:
            if fragment.get("type") == "fact":
                value = fragment.get("value_minor")
                if not isinstance(value, int) or value < 0:
                    raise DomainError("REPLY_FACT_MISMATCH")
            if fragment.get("type") == "text":
                text = str(fragment.get("text", ""))
                if any(label in text for label in self._forbidden):
                    raise DomainError("REPLY_TEXT_FORBIDDEN_LABEL")
        return fragments
