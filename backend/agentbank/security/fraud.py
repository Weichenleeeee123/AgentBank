from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class FraudLevel(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"
    unknown = "unknown"


@dataclass(frozen=True)
class FraudAssessment:
    level: FraudLevel
    reason_codes: list[str]
    confidence: float | None
    signals: dict[str, Any]


class FraudDetector:
    _strong_phrases = ("安全账户", "公检法", "刷单", "验证码", "客服退款", "荐股群", "别告诉别人")
    _transfer_pattern = re.compile(r"转账|转给|汇款|付款|打钱|转\s*\d")

    def evaluate(self, text: str, *, relation: dict[str, Any], behavior: dict[str, Any], classifier_failed: bool = False) -> FraudAssessment:
        if classifier_failed:
            return FraudAssessment(FraudLevel.unknown, ["CLASSIFIER_UNAVAILABLE"], None, {})
        transfer_intent = bool(self._transfer_pattern.search(text))
        phrase_hits = [phrase for phrase in self._strong_phrases if phrase in text]
        relation_strong = bool(relation.get("risk_listed") or relation.get("new_recipient"))
        behavior_strong = bool(behavior.get("night") or behavior.get("p95_amount") or behavior.get("multiple_recipients"))
        signals = {"transfer_intent": transfer_intent, "phrase_hits": phrase_hits,
                   "relation_strong": relation_strong, "behavior_strong": behavior_strong}
        if transfer_intent and phrase_hits and (relation_strong or behavior_strong):
            return FraudAssessment(FraudLevel.high, ["U07", "U08"], 0.95, signals)
        if transfer_intent and (phrase_hits or relation_strong or behavior_strong):
            return FraudAssessment(FraudLevel.medium, ["U07"], 0.65, signals)
        return FraudAssessment(FraudLevel.low, [], 0.1, signals)
