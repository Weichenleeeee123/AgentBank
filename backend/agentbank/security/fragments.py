from __future__ import annotations

from typing import Any
import re

from agentbank.errors import DomainError


class ReplyFragmentChecker:
    _forbidden = ("收入", "支出", "余额", "转账", "扣款", "到账")
    _forbidden_status = ("成功", "已成功", "已到账", "已转出", "已完成", "已取消", "余额充足")
    _numeric = re.compile(r"(?:\d|零|一|二|两|三|四|五|六|七|八|九|十|百|千|万|亿)")

    def validate(self, fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for fragment in fragments:
            if fragment.get("type") == "fact":
                value = fragment.get("value_minor")
                if not isinstance(value, int) or value < 0:
                    raise DomainError("REPLY_FACT_MISMATCH")
            if fragment.get("type") == "text":
                text = str(fragment.get("text", ""))
                if (any(label in text for label in self._forbidden)
                        or any(status in text for status in self._forbidden_status)
                        or self._numeric.search(text)):
                    raise DomainError("REPLY_TEXT_FORBIDDEN_LABEL")
        return fragments
